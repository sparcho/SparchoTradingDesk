#!/usr/bin/env python3
"""
fetch_daily_ohlc.py  --  daily OHLCV log for the SCRN-MAP universe
======================================================================
Back-end-only daily price-accumulation job. Two runs per weekday:
    * Open pull   -- 9:25 AM IST (~10 min after NSE open)
    * Close pull  -- 3:40 PM IST (~10 min after NSE close)

No report is produced. Writes append/upsert rows to
    /TRADER/00_SYSTEM/GENERATORS/_cache/daily_prices.csv
plus a status snapshot at
    /TRADER/00_SYSTEM/GENERATORS/_cache/last_pull.json

Schema of daily_prices.csv (header on first write, one row per ticker
per trading date; close-pull overwrites open-pull values for same date+ticker):
    date,ticker,yahoo_symbol,prev_close,open,high,low,close,volume,
    gap_pct,day_chg_pct,open_pull_at,close_pull_at,status

Exit codes: 0 OK / 0 partial<=10% / 2 LOUD>10% / 3 FAIL no successes


EXTENDED 2026-05-20 — Phase 4 of CC infra build:
  After save_rows() the task ALSO updates `_inputs/regime_state.yaml`
  raw_inputs:* block via `update_regime_raw_inputs.update_raw_inputs()`.
  This keeps V-36 freshness overlay in the morning brief live without an
  operator-curated YAML edit. Failure of the regime-write step is logged
  but does NOT fail the price-cache task (the CSV is the primary artefact).
"""
import argparse, csv, json, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from yahoo_common import (ALL_TICKERS, INTERMARKET_TICKERS, ensure_cache_dir,
                          fetch_with_fallback, yahoo_symbols)

IST = timezone(timedelta(hours=5, minutes=30))

CACHE = ensure_cache_dir()
CSV_PATH = CACHE / 'daily_prices.csv'
STATUS_PATH = CACHE / 'last_pull.json'

CSV_FIELDS = [
    'date','ticker','yahoo_symbol','prev_close',
    'open','high','low','close','volume',
    'gap_pct','day_chg_pct',
    'open_pull_at','close_pull_at','status',
]


def today_ist():
    return datetime.now(IST).strftime('%Y-%m-%d')


def now_ist_iso():
    return datetime.now(IST).strftime('%Y-%m-%dT%H:%M:%S%z')


def extract_latest_bar(payload):
    """Return (yh_prev_close, bar1_close, o, h, l, c, v) from Yahoo v8 chart payload.
    yh_prev_close: meta.chartPreviousClose (kept for audit -- DO NOT use as
      ground truth; see L-09. Yahoo's previousClose is split/dividend-adjusted
      and can disagree with the prior bar's raw close in the same response.)
    bar1_close: raw close of the bar immediately before the last non-null close.
    """
    try:
        res = payload['chart']['result'][0]
        meta = res.get('meta', {})
        yh_prev_close = meta.get('chartPreviousClose') or meta.get('previousClose')
        quote = res['indicators']['quote'][0]
        opens = quote.get('open') or []
        highs = quote.get('high') or []
        lows  = quote.get('low') or []
        closes = quote.get('close') or []
        vols   = quote.get('volume') or []
        last_i = None
        for i in range(len(closes) - 1, -1, -1):
            if closes[i] is not None:
                last_i = i
                break
        if last_i is None:
            return None
        bar1_close = None
        for j in range(last_i - 1, -1, -1):
            if closes[j] is not None:
                bar1_close = closes[j]
                break
        return (yh_prev_close,
                bar1_close,
                opens[last_i] if last_i < len(opens) else None,
                highs[last_i] if last_i < len(highs) else None,
                lows[last_i]  if last_i < len(lows)  else None,
                closes[last_i],
                vols[last_i]  if last_i < len(vols)  else None)
    except (KeyError, IndexError, TypeError):
        return None


def derive_prev_close(rows, ticker, target_date, bar1_close, yh_prev_close, verbose=False):
    """Choose authoritative prev_close. See L-09.
    Priority: own CSV t-1 close > chart bar-1 close > yahoo meta (last resort).
    Logs divergence warning when yahoo meta differs from chosen by >0.5%.
    """
    chosen = None; src = None
    prior = [r for r in rows
             if r.get('ticker') == ticker
             and r.get('date', '') < target_date
             and r.get('close') not in (None, '')]
    if prior:
        prior.sort(key=lambda r: r['date'])
        try:
            chosen = float(prior[-1]['close'])
            src = f"csv:{prior[-1]['date']}"
        except (TypeError, ValueError):
            chosen = None
    if chosen is None and bar1_close is not None:
        chosen = float(bar1_close); src = "chart:bar-1"
    if chosen is None and yh_prev_close is not None:
        chosen = float(yh_prev_close); src = "yahoo:meta"
    if (chosen is not None and yh_prev_close is not None and chosen != 0
            and abs(float(yh_prev_close) - chosen) / chosen > 0.005):
        msg = (f"  [PREV-CLOSE WARN] {ticker:12s} chosen={chosen} "
               f"(src={src}) yahoo_meta={yh_prev_close} "
               f"divergence={(float(yh_prev_close)-chosen)/chosen*100:+.2f}%")
        print(msg, file=sys.stderr)
    return chosen, src


def round_or_none(v, n=4):
    return None if v is None else round(float(v), n)


def pct(new, old, n=3):
    if new is None or old in (None, 0):
        return None
    return round((new / old - 1) * 100, n)


def load_rows():
    if not CSV_PATH.exists():
        return []
    with CSV_PATH.open('r', newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def save_rows(rows):
    with CSV_PATH.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, '') for k in CSV_FIELDS})


def upsert(rows, date, ticker, updates):
    for r in rows:
        if r['date'] == date and r['ticker'] == ticker:
            for k, v in updates.items():
                r[k] = '' if v is None else v
            return rows
    new = {k: '' for k in CSV_FIELDS}
    new['date'] = date
    new['ticker'] = ticker
    for k, v in updates.items():
        new[k] = '' if v is None else v
    rows.append(new)
    return rows


def run_pull(mode, target_date, verbose=False):
    t0 = time.time()
    rows = load_rows()
    yahoo_range = '5d'
    ok, err_map = [], {}
    stamp = now_ist_iso()
    universe = list(ALL_TICKERS) + list(INTERMARKET_TICKERS)
    for ticker in universe:
        payload, sym, status = fetch_with_fallback(
            ticker, interval='1d', range_=yahoo_range, timeout=12)
        if payload is None:
            err_map[ticker] = {'attempts': yahoo_symbols(ticker), 'status': status}
            upsert(rows, target_date, ticker, {
                'status': f'err:{status}',
                ('open_pull_at' if mode == 'open' else 'close_pull_at'): stamp,
            })
            if verbose:
                print(f"  [ERR] {ticker:12s} -> {status}", file=sys.stderr)
            time.sleep(0.10); continue

        bar = extract_latest_bar(payload)
        if not bar:
            err_map[ticker] = {'attempts': [sym], 'status': 'parse_fail'}
            upsert(rows, target_date, ticker, {
                'status': 'err:parse_fail',
                ('open_pull_at' if mode == 'open' else 'close_pull_at'): stamp,
            })
            if verbose:
                print(f"  [PARSE] {ticker:12s}", file=sys.stderr)
            time.sleep(0.10); continue

        yh_prev_close, bar1_close, o, h, l, c, v = bar
        # L-09: derive prev_close ourselves; never trust Yahoo's previousClose blindly.
        prev_close, _src = derive_prev_close(
            rows, ticker, target_date, bar1_close, yh_prev_close, verbose=verbose)
        prev_close = round_or_none(prev_close)
        o = round_or_none(o); h = round_or_none(h); l = round_or_none(l)
        c = round_or_none(c); v = None if v is None else int(v)

        if mode == 'open':
            upsert(rows, target_date, ticker, {
                'yahoo_symbol': sym, 'prev_close': prev_close, 'open': o,
                'gap_pct': pct(o, prev_close), 'open_pull_at': stamp,
                'status': 'ok',
            })
        else:  # close
            upsert(rows, target_date, ticker, {
                'yahoo_symbol': sym, 'prev_close': prev_close,
                'open': o, 'high': h, 'low': l, 'close': c, 'volume': v,
                'gap_pct': pct(o, prev_close), 'day_chg_pct': pct(c, prev_close),
                'close_pull_at': stamp, 'status': 'ok',
            })

        ok.append(ticker)
        if verbose:
            print(f"  [OK]  {ticker:12s} {sym:15s} prev={prev_close} o={o} c={c}",
                  file=sys.stderr)
        time.sleep(0.10)

    order = {t: i for i, t in enumerate(universe)}
    rows.sort(key=lambda r: (r['date'], order.get(r['ticker'], 999)))
    save_rows(rows)

    # Phase 4 (2026-05-20): keep regime_state.yaml raw_inputs in lockstep with the
    # fresh CSV. Non-fatal — failure here doesn't fail the price-cache task.
    try:
        from update_regime_raw_inputs import update_raw_inputs
        regime_summary = update_raw_inputs(target_date=target_date, verbose=verbose)
        print(f"[fetch_daily_ohlc] regime raw_inputs: "
              f"updated={len(regime_summary.get('updated', []))} "
              f"skipped={len(regime_summary.get('skipped', []))} "
              f"pending={len(regime_summary.get('pending', []))} "
              f"errors={len(regime_summary.get('errors', []))}", file=sys.stderr)
    except Exception as e:
        print(f"[fetch_daily_ohlc] regime raw_inputs update FAILED (non-fatal): {e}", file=sys.stderr)

    elapsed = round(time.time() - t0, 1)
    n_total = len(universe); n_ok = len(ok); n_err = len(err_map)
    err_rate = round(n_err / n_total, 4) if n_total else 0.0
    if n_ok == 0:
        flag = 'FAIL'
    elif err_rate > 0.10:
        flag = 'LOUD'
    else:
        flag = 'OK'
    status_blob = {
        'mode': mode, 'date': target_date, 'ran_at': stamp,
        'elapsed_sec': elapsed,
        'count_total': n_total, 'count_ok': n_ok, 'count_err': n_err,
        'err_rate': err_rate, 'errors': err_map,
        'csv_path': str(CSV_PATH), 'flag': flag,
    }
    with STATUS_PATH.open('w', encoding='utf-8') as f:
        json.dump(status_blob, f, indent=2)
    print(json.dumps(status_blob, indent=2))
    return status_blob


def main(argv=None):
    ap = argparse.ArgumentParser(description='Daily OHLCV pull.')
    ap.add_argument('--pull', choices=('open', 'close'), required=True)
    ap.add_argument('--date', default=None, help='YYYY-MM-DD (default today IST).')
    ap.add_argument('--verbose', action='store_true')
    args = ap.parse_args(argv)
    target_date = args.date or today_ist()
    blob = run_pull(args.pull, target_date, verbose=args.verbose)
    if blob['flag'] == 'FAIL':
        return 3
    if blob['flag'] == 'LOUD':
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
