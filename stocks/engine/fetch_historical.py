#!/usr/bin/env python3
"""
fetch_historical.py  --  one-shot historical-close fetcher for SMA50 / SMA200

WHY THIS EXISTS
===============
Dad's screener #1 needs "SMA50 slightly above SMA200 with upward trend". For
SMA200 we need 200 trading days of history. The daily_prices.csv cache only
starts from 2026-04-24 (~5 days), insufficient.

This script does a one-shot 1-year backfill per ticker via Yahoo's chart API,
storing a separate cache: /00_SYSTEM/GENERATORS/_cache/historical_closes.csv.
Daily fetcher continues writing to daily_prices.csv; consumers UNION the two
when 200+ day windows are needed.

CADENCE
=======
- One-shot when first set up (now)
- Re-run on universe expansion (new tickers added)
- Re-run periodically (~quarterly) to refresh the rolling 1-year window
- daily_prices.csv handles the live daily updates between historical refreshes

USAGE
=====
   python3 fetch_historical.py
       Fetch 1-year closes for ALL_TICKERS + INTERMARKET_TICKERS, skip
       commodities/ETFs that don't apply.

   python3 fetch_historical.py --ticker MGL
       Fetch one ticker.

   python3 fetch_historical.py --tickers MGL MCX BSE
       Fetch a subset.

   python3 fetch_historical.py --range 2y
       Fetch 2 years (default 1y; valid: 1mo, 3mo, 6mo, 1y, 2y, 5y, 10y).

OUTPUT
======
/00_SYSTEM/GENERATORS/_cache/historical_closes.csv with columns:
    ticker, date, close
Sorted by (ticker, date). One row per (ticker, trading-day).

Schema is deliberately narrow -- this is for SMA computation only. If we later
want OHLCV / volume / adjusted-close for backtest, that's a different cache.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / "_cache"
OUT_PATH = CACHE_DIR / "historical_closes.csv"

# Re-use yahoo_common's UA + ticker maps + URL conventions
sys.path.insert(0, str(SCRIPT_DIR))
from yahoo_common import UA, ALL_TICKERS, INTERMARKET_TICKERS, yahoo_symbols

REQUEST_TIMEOUT = 15
INTER_REQUEST_SLEEP = 1.2

# Skip these in the universe-default fetch (no equity history concept)
SKIP_TICKERS = {'NIFTY', 'NIFTYBEES', 'BANKBEES', 'ITBEES', 'PHARMABEES', 'PSUBNKBEES'}


def fetch_chart_history(yahoo_sym: str, range_: str = '1y') -> list[tuple[str, float, int]]:
    """Fetch daily closes + volume for a Yahoo symbol. Returns [(YYYY-MM-DD, close, volume)] sorted ascending."""
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_sym}"
        f"?range={range_}&interval=1d"
    )
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
        data = json.loads(r.read().decode('utf-8'))
    chart = data.get('chart', {})
    if chart.get('error'):
        raise ValueError(chart['error'])
    result = chart.get('result', [])
    if not result:
        return []
    res = result[0]
    timestamps = res.get('timestamp', []) or []
    indicators = res.get('indicators', {})
    quotes = indicators.get('quote', [{}])[0] if indicators.get('quote') else {}
    closes = quotes.get('close', []) or []
    volumes = quotes.get('volume', []) or []
    out = []
    for i, (ts, c) in enumerate(zip(timestamps, closes)):
        if c is None:
            continue
        date_str = datetime.utcfromtimestamp(ts).strftime('%Y-%m-%d')
        v = volumes[i] if i < len(volumes) and volumes[i] is not None else 0
        out.append((date_str, float(c), int(v)))
    return sorted(out, key=lambda x: x[0])


def fetch_one_ticker(ticker: str, range_: str) -> tuple[str, list[tuple[str, float]], str]:
    """Try yahoo_symbols() in order; return (used_symbol, rows, status_str)."""
    for sym in yahoo_symbols(ticker):
        try:
            rows = fetch_chart_history(sym, range_)
            if rows:
                return sym, rows, 'ok'
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue  # try next fallback
            return sym, [], f'http-{e.code}'
        except Exception as e:
            return sym, [], f'fetch-error: {type(e).__name__}'
    return '', [], '404 (all fallbacks exhausted)'


def load_cache() -> dict:
    """Read existing cache into dict[(ticker, date)] -> (close, volume)."""
    if not OUT_PATH.exists():
        return {}
    out = {}
    with OUT_PATH.open('r', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            close = float(row['close'])
            vol = int(float(row.get('volume') or 0))
            out[(row['ticker'], row['date'])] = (close, vol)
    return out


def write_cache(rows: dict) -> None:
    """Write dict[(ticker, date)] -> (close, volume), sorted by (ticker, date)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open('w', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow(['ticker', 'date', 'close', 'volume'])
        for (t, d), val in sorted(rows.items()):
            # Backward compat: cache may hold either float (close-only) or (close, volume) tuple
            if isinstance(val, tuple):
                close, vol = val
            else:
                close, vol = val, 0
            w.writerow([t, d, f"{close:.4f}", vol])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument('--ticker', help='Fetch one ticker')
    ap.add_argument('--tickers', nargs='+', help='Fetch a subset')
    ap.add_argument('--range', default='1y', help='Yahoo range (1mo, 3mo, 6mo, 1y, 2y, 5y, 10y)')
    args = ap.parse_args()

    if args.ticker:
        targets = [args.ticker.upper()]
    elif args.tickers:
        targets = [t.upper() for t in args.tickers]
    else:
        # Default: ALL_TICKERS + INTERMARKET_TICKERS, minus skip-list
        targets = [t for t in (list(ALL_TICKERS) + list(INTERMARKET_TICKERS))
                   if t not in SKIP_TICKERS]

    print(f"Fetching {len(targets)} tickers @ range={args.range} -- ~{len(targets) * INTER_REQUEST_SLEEP:.0f}s expected")

    cache = load_cache()
    print(f"Cache loaded: {len(cache)} existing (ticker, date) pairs")

    fetched = 0
    errors = 0
    new_rows = 0
    for ticker in targets:
        sym, rows, status = fetch_one_ticker(ticker, args.range)
        if status == 'ok':
            for row_tuple in rows:
                if len(row_tuple) == 3:
                    d, c, v = row_tuple
                else:
                    d, c = row_tuple; v = 0
                key = (ticker, d)
                if key not in cache:
                    new_rows += 1
                cache[key] = (c, v)
            print(f"  {ticker:>14}  {sym:<14}  {status:<10}  {len(rows)} rows")
            fetched += 1
        else:
            print(f"  {ticker:>14}  {'(no symbol)' if not sym else sym:<14}  {status}")
            errors += 1
        time.sleep(INTER_REQUEST_SLEEP)

    write_cache(cache)
    print()
    print(f"Summary: fetched {fetched}, errors {errors}, new rows {new_rows}")
    print(f"Cache written: {OUT_PATH} ({len(cache)} total rows)")
    return 0 if errors == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
