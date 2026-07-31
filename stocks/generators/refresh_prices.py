#!/usr/bin/env python3
"""refresh_prices.py — CI price + Day-Trade-Fires refresh (F129, 2026-06-09).

Runs in GitHub Actions (vanilla yfinance, no vault access). Two jobs, both fed by
ONE batched live-price pull so the dashboard is always on the latest NSE close:

  1. HELD BOOK  — refresh every held name's price / MV / unreal / book totals /
     concentration (held list read from the aggregate, not a hardcoded map).
  2. DAY-TRADE FIRES — re-score the fires on live prices using the SAME shared
     scorer the vault emit uses (daytrade_core), reading the screener/fundamental
     layer from the `daytrade_inputs` block the vault persisted. This fixes the
     "fires sit on multi-session-stale prices" root cause.

FAIL-CLOSED: if the live pull clearly failed (too few candidates got fresh bars)
the prior fires panel is LEFT UNTOUCHED and daytrade_freshness is stamped STALE
with an error note — the dashboard banner shows staleness rather than a silently
wrong / emptied panel. Never silently mislead.
"""
from __future__ import annotations
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yfinance as yf

HERE = Path(__file__).resolve().parent
DATA_JSON = HERE.parent / "data" / "equity_dashboard_aggregate.json"

try:
    import daytrade_core
except ImportError:
    sys.path.insert(0, str(HERE))
    import daytrade_core

# staleness_contract: single source of truth for the staleness contract (F260702).
try:
    import staleness_contract
except ImportError:
    import sys as _sys
    from pathlib import Path as _P
    _sys.path.insert(0, str(_P(__file__).resolve().parent))
    try:
        import staleness_contract
    except ImportError:
        staleness_contract = None

MIN_COVERAGE = 0.5   # fail-closed: fraction of candidates needing >=2 fresh bars


def _nse(ticker):
    return ticker + ".NS"


def tickers_behind_batch(out, batch_bar_date=None):
    """Tickers whose newest bar is MISSING or BEHIND the batch's newest bar -- i.e. the ones a
    per-ticker re-fetch should be spent on.

    F260731-CLOUDPRICESTALE: `if out[t]: continue` could only see an EMPTY series, so a symbol that
    returned bars but was one session short was treated as a success and never retried.

    The reference session is the newest bar_date present in THIS batch, which is the same reference
    write_prices_json() uses to set `stale` -- so what gets re-fetched and what gets flagged stale
    can never disagree.
    """
    if batch_bar_date is None:
        batch_bar_date = max((b[-1][0] for b in out.values() if b), default=None)
    if batch_bar_date is None:
        return sorted(out)          # the whole batch came back empty -- everything is a straggler
    return sorted(t for t, b in out.items() if not b or b[-1][0] < batch_bar_date)

def batch_ohlc(tickers):
    """Return {ticker: sorted [(date_iso, o, h, l, c), ...]} via one batched pull,
    per-ticker fallback for stragglers. Missing names stay empty (fail-closed)."""
    out = {t: [] for t in tickers}
    syms = [_nse(t) for t in tickers]
    df = None
    try:
        df = yf.download(syms, period="20d", interval="1d", group_by="ticker",
                         auto_adjust=False, progress=False, threads=True)
    except Exception as e:
        print("  batch download failed: " + str(e), file=sys.stderr)
    if df is not None and not df.empty:
        lvl0 = set(df.columns.get_level_values(0))
        for t in tickers:
            sym = _nse(t)
            if sym not in lvl0:
                continue
            sub = df[sym].dropna(subset=["Close"])
            for idx, row in sub.iterrows():
                try:
                    d = idx.date().isoformat()
                    o, h, l, c = float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])
                except Exception:
                    continue
                if c > 0:
                    out[t].append((d, o, h, l, c))
            out[t].sort()
    # F260731-CLOUDPRICESTALE. The straggler guard used to be `if out[t]: continue`, i.e. only a
    # ticker that came back COMPLETELY EMPTY was retried. A batched multi-symbol pull routinely
    # returns a SHORT series for a subset of symbols -- bars present, latest session missing -- and
    # those sailed through as success. On 2026-07-30 that pinned 25 of 74 tickers to the 29-Jul bar,
    # MTARTECH among them at 5,194.0 against a real close of 5,453.50, omitting a whole +5% limit-up
    # session. The `stale` flag was made honest that same day (F260730-STALEFLAG) but the FETCH was
    # not, so the feed announced bad data instead of going back for good data. Now a ticker BEHIND
    # the batch's newest bar is a straggler too.
    for t in tickers_behind_batch(out):
        try:
            h = yf.Ticker(_nse(t)).history(period="20d", interval="1d")
        except Exception:
            continue
        retry = []
        try:
            for idx, row in h.iterrows():
                d = idx.date().isoformat()
                o, hi, lo, c = float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])
                if c > 0:
                    retry.append((d, o, hi, lo, c))
        except Exception:
            continue
        retry.sort()
        # Keep the retry only if it is genuinely newer. A retry that comes back shorter or equally
        # stale must never overwrite what the batch already gave us (fail-closed, never fail-worse).
        if retry and (not out[t] or retry[-1][0] > out[t][-1][0]):
            out[t] = retry
    return out


def refresh_held(data, ohlc):
    new_mv = new_invested = new_unreal = day_chg_inr = 0.0
    for h in data.get("held", []):
        ticker = h.get("ticker")
        qty = float(h.get("qty") or 0)
        avg = float(h.get("avg_cost") or 0)
        bars = ohlc.get(ticker, [])
        if len(bars) >= 1:
            price = bars[-1][4]
            prev = bars[-2][4] if len(bars) >= 2 else price
            mv = price * qty
            invested = avg * qty
            unreal = mv - invested
            chg = ((price - prev) / prev * 100) if prev else 0
            h["current_price"] = round(price, 2)
            h["current_value"] = round(mv, 2)
            h["invested"] = round(invested, 2)
            h["unreal_inr"] = round(unreal, 2)
            h["unreal_pct"] = round((unreal / invested * 100) if invested else 0, 2)
            h["day_chg_pct"] = round(chg, 2)
            new_mv += mv
            new_invested += invested
            new_unreal += unreal
            day_chg_inr += (price - prev) * qty
            print("  held " + str(ticker) + " -> Rs." + format(price, ".2f"))
        else:
            print("  held " + str(ticker) + ": no price; keeping prior")
            new_mv += float(h.get("current_value") or 0)
            new_invested += avg * qty
            new_unreal += float(h.get("unreal_inr") or 0)
    tot = data.setdefault("book", {}).setdefault("totals", {})
    tot["current_value"] = round(new_mv, 2)
    tot["invested"] = round(new_invested, 2)
    tot["unrealized_pnl_abs"] = round(new_unreal, 2)
    tot["unrealized_pnl_pct"] = round((new_unreal / new_invested * 100) if new_invested else 0, 2)
    tot["change_today_abs"] = round(day_chg_inr, 2)
    prev_mv = new_mv - day_chg_inr
    tot["change_today_pct"] = round((day_chg_inr / prev_mv * 100) if prev_mv else 0, 2)
    for h in data.get("held", []):
        cv = float(h.get("current_value") or 0)
        h["concentration_pct"] = round((cv / new_mv * 100) if new_mv else 0, 2)


def write_prices_json(ohlc, now_iso, out_path):
    """F131 Phase-0 seed of the single-source price store: ticker -> {last, prev_close, day_chg,
    asof}, built from the SAME batched pull that feeds the held book + fires. Consumers (shell
    overlay, _index cards) resolve prices from HERE rather than embedding their own snapshot — one
    price per ticker, one asof, one place to see staleness. Per the Price-Printer SSOT contract."""
    # F260730-STALEFLAG. `stale` was hardcoded False while `bar_date` was recorded faithfully right
    # beside it, so a name a full session behind still declared itself fresh -- 29 of 74 names on
    # 2026-07-30, and nothing read either field. The reference session is the NEWEST bar_date IN THIS
    # SAME PULL: the batch is its own control, which needs no calendar and cannot be fooled by a
    # holiday, a weekend or a pre-open run. A name lagging the rest of its own batch is exactly the
    # Yahoo-omission anomaly that staled the fib radar's spot price.
    batch_bar_date = max((b[-1][0] for b in ohlc.values() if b), default=None)
    tickers = {}
    for t, bars in ohlc.items():
        if not bars:
            continue
        last = bars[-1][4]
        prev = bars[-2][4] if len(bars) >= 2 else last
        bar_date = bars[-1][0]
        tickers[t] = {
            "last": round(last, 2),
            "prev_close": round(prev, 2),
            "day_chg_pct": round(((last - prev) / prev * 100) if prev else 0, 2),
            "bar_date": bar_date,
            "asof_utc": now_iso,
            "stale": bool(batch_bar_date and bar_date < batch_bar_date),
        }
    store = {"schema": "price-printer/v1", "asof_utc": now_iso, "session": "intraday",
             "source": "yfinance", "tickers": tickers}
    out_path.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")
    print("OK wrote " + out_path.name + " (" + str(len(tickers)) + " tickers)")


def write_profit_lock_status(universe_n, now_iso, out_path):
    """Privacy-safe freshness heartbeat for the client-side +1R Profit-Lock advisory.

    The advisory is evaluated IN THE BROWSER from the operator-decrypted ledger
    (entry/stop never leave the client), so this file deliberately carries NO
    ticker / entry / stop -- only that the public live-price feed the advisory
    rides was refreshed, and when. (F profit-lock, 2026-06-24.)"""
    status = {
        "schema": "profit-lock-status/v1",
        "evaluated": "client-side (Trade Lab, AES-GCM unlocked)",
        "ran_at_utc": now_iso,
        "session": "intraday",
        "prices_asof_utc": now_iso,
        "universe_n": universe_n,
        "high_r": 1.0,
        "note": ("Advisory +1R Profit-Lock is computed in-browser from the decrypted "
                 "ledger vs the public live price; this file is a freshness heartbeat "
                 "only (no ticker/entry/stop)."),
    }
    out_path.write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")
    print("OK wrote " + out_path.name + " (profit-lock heartbeat)")


def _held_from_ciphertext(data: dict) -> list:
    """Read the held tickers out of the sealed block, using the desk password.

    F260729-HELDLEAK. The held list used to ride in the PUBLIC aggregate, which meant anyone
    could fetch the operator's positions by URL. It now lives inside sensitive_enc. This job
    still needs the names to mark rows and score the day-trade panel, so it DECRYPTS to read
    them — and never re-seals, so it cannot corrupt the blob or change what the page opens with.

    Returns [] when the secret is absent (a fork, or a run without it). The panel then simply
    marks nothing as held, which is visibly wrong rather than quietly wrong.
    """
    import base64
    import hashlib
    import os
    enc = data.get("sensitive_enc") or {}
    pw = os.environ.get("DESK_UNLOCK_PW") or ""
    if not enc or not pw:
        return []
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        key = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"),
                                  base64.b64decode(enc["salt"]), int(enc.get("iter", 600000)), 32)
        opened = json.loads(AESGCM(key).decrypt(base64.b64decode(enc["iv"]),
                                                base64.b64decode(enc["ct"]), None))
    except Exception as exc:
        print(f"WARN: could not read held from the sealed block ({type(exc).__name__}) — "
              "day-trade rows will not be marked held", file=sys.stderr)
        return []
    return list(opened.get("daytrade_held")
                or [h.get("ticker") for h in (opened.get("held") or [])])

def main():
    # F260622 addendum — NSE-session gate: silent on weekends/holidays. The 5-min market-hours cron
    # would otherwise re-stamp identical closed-market data on an NSE holiday. US-macro readings
    # (ci_regime_refresh) are a SEPARATE, ungated workflow step, so overnight macro keeps updating.
    # Reuses stocks/engine/nse_calendar (the single NSE-session authority).
    try:
        sys.path.insert(0, str(HERE.parent / "engine"))
        from nse_calendar import is_session as _is_session
        _ist = datetime.now(timezone(timedelta(hours=5, minutes=30))).date()
        if not _is_session(_ist):
            print("refresh_prices: %s is not an NSE session - skipping (silent on weekends/holidays)" % _ist)
            return 0
    except Exception as _e:  # never let the gate itself break a refresh
        print("refresh_prices: nse_calendar gate unavailable (%s) - proceeding ungated" % _e, file=sys.stderr)
    if not DATA_JSON.exists():
        print("ERROR: " + str(DATA_JSON) + " not found", file=sys.stderr)
        return 1
    data = json.loads(DATA_JSON.read_text(encoding="utf-8"))

    dt_inputs = data.get("daytrade_inputs") or {}
    candidates = dt_inputs.get("candidates") or {}
    # held identities live inside the ciphertext now (F260729-HELDLEAK); decrypt to read them.
    held_tickers = (dt_inputs.get("held")
                    or [h.get("ticker") for h in data.get("held", [])]
                    or _held_from_ciphertext(data))
    held_set = set(held_tickers)

    if not candidates:
        print("WARN: no daytrade_inputs.candidates — run the full vault emit first so CI "
              "has the screener layer to re-score. Skipping fires.", file=sys.stderr)

    universe = sorted(set(candidates) | held_set)
    print("Live pull for " + str(len(universe)) + " names")
    ohlc = batch_ohlc(universe)
    refresh_held(data, ohlc)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    write_prices_json(ohlc, now_iso, DATA_JSON.parent / "prices.json")  # F131 single-source store
    write_profit_lock_status(len(universe), now_iso, DATA_JSON.parent / "profit_lock_status.json")

    # F260730-DTREADERS: this job used to RE-SCORE the day-trade panel here and publish it as
    # `live_daytrade`. The day-trade class was retired 25-Jul (measured negative expectancy) and
    # the block is gone, so the scoring is gone with it — re-scoring a signal nothing may act on
    # is decay surface that still has to be kept green. `daytrade_inputs.candidates` is still read
    # ABOVE, for the one thing it is still good for: defining the price universe to pull.
    #
    # What survives is the price-freshness stamp, now derived from the pull itself (the newest bar
    # in this batch) instead of from the retired scorer's return value. Same number, no dependency:
    # build_panel's price_as_of was always just max(bar_date) over the same OHLC.
    if candidates:
        covered = sum(1 for t in candidates if len(ohlc.get(t, [])) >= 2)
        coverage = covered / max(1, len(candidates))
        print("price coverage: " + str(covered) + "/" + str(len(candidates)))
        price_as_of = max((b[-1][0] for b in ohlc.values() if b), default=None)
        if coverage >= MIN_COVERAGE:
            # Freshness-regression guard (F138 / 260701 grayed-fires incident): a live yfinance
            # pull off-hours sometimes LAGS a session — its newest bar is the prior day's, not the
            # latest close. Stamping that would regress price_as_of, mark the desk STALE and gray
            # cards that are in fact current. Never regress: only overwrite when the live pull is
            # at least as fresh as what is already published.
            _prior_paf = (data.get("daytrade_freshness") or {}).get("price_as_of")
            if _prior_paf and price_as_of and str(price_as_of) < str(_prior_paf):
                fr = dict(data.get("daytrade_freshness") or {})
                fr["refreshed_at_utc"] = now_iso
                fr["error"] = ("live pull lagged (price_as_of " + str(price_as_of)
                               + " < published " + str(_prior_paf) + "); kept the fresher stamp — no regression")
                data["daytrade_freshness"] = fr
                print("  NO-REGRESS: live pull " + str(price_as_of) + " older than published "
                      + str(_prior_paf) + " — kept the fresher stamp (F138)", file=sys.stderr)
            else:
                data["daytrade_freshness"] = daytrade_core.daytrade_freshness(
                    price_as_of, refreshed_at_utc=now_iso)
                print("  price_as_of " + str(price_as_of) + " | "
                      + data["daytrade_freshness"]["status"])
        else:
            fr = dict(data.get("daytrade_freshness") or {})
            fr["status"] = "STALE"
            fr["refreshed_at_utc"] = now_iso
            fr["error"] = "live price pull failed (coverage below threshold); kept the prior stamp"
            data["daytrade_freshness"] = fr
            print("  FAIL-CLOSED: coverage too low — kept the prior stamp, marked STALE", file=sys.stderr)
    # A block cannot outlive its producer: drop any `live_daytrade` carried in an older aggregate.
    data.pop("live_daytrade", None)

    data["emitted_at_utc"] = now_iso
    data.setdefault("meta", {})["last_price_refresh"] = now_iso
    # Refresh the canonical staleness contract on the freshly-overlaid data (F260702)
    # so the client + watchdog read a current contract every cloud cycle.
    try:
        if staleness_contract:
            data["staleness"] = staleness_contract.build_staleness(data, "equity", now_utc_iso=now_iso)
    except Exception as _e:
        print(f"[staleness] equity overlay build skipped: {_e}", file=sys.stderr)
    DATA_JSON.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print("OK wrote " + DATA_JSON.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
