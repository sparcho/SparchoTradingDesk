#!/usr/bin/env python3
"""next_session_snapshot.py — FORWARD-GUIDANCE engine for Day-Trade Fires (Step 2, 2026-06-25).

Primary purpose: produce the NEXT-session candidate set ("Tomorrow") so the operator can check
by midnight or pre-open and see what to watch. Two cuts of the SAME snapshot:

  --cut evening   (~17:30 IST, after EOD data settles): compute next-session candidates on today's
                  COMPLETED NSE bar via the shared daytrade_core scorer (which already encodes the
                  Rajiv doji->confirm forward logic). basis="nse_close".
  --cut morning   (~08:30 IST, pre-open): RE-emit the snapshot now annotated with OVERNIGHT/global
                  context (US prior close, DXY/USDINR/Brent, USDJPY, Asia). basis="overnight_refreshed".
                  Pre-open there is no new NSE bar, so the candidate SET is the evening set; the
                  overnight CONTEXT is what's new (informs the gap read). USE ONLY feeds that exist;
                  degrade gracefully and record feeds_real vs feeds_missing (no fabrication).

Writes the `next_session` block into stocks/data/equity_dashboard_aggregate.json. The dashboard
Tomorrow/Today toggle reads it (basis badge + as_of so the operator knows which cut they see).
Last-good: a failed compute never blanks a prior good snapshot. NSE-session aware via nse_calendar.
"""
from __future__ import annotations
import argparse, json, sys
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

import yfinance as yf

HERE = Path(__file__).resolve().parent
DATA_JSON = HERE.parent / "data" / "equity_dashboard_aggregate.json"

try:
    import daytrade_core
except ImportError:
    sys.path.insert(0, str(HERE)); import daytrade_core
try:
    from refresh_prices import batch_ohlc            # single source for the live OHLC pull
except Exception:
    batch_ohlc = None

# Overnight rails — REAL yfinance tickers (probed 2026-06-25). GIFT/SGX Nifty FUTURES have no
# reliable free feed (NIFTY_F1.NS delisted) -> reported MISSING, never fabricated.
OVERNIGHT = [
    ("sp500",   "^GSPC",     "S&P 500"),
    ("nasdaq",  "^IXIC",     "Nasdaq Comp"),
    ("dxy",     "DX-Y.NYB",  "DXY"),
    ("usdinr",  "INR=X",     "USDINR"),
    ("brent",   "BZ=F",      "Brent"),
    ("usdjpy",  "JPY=X",     "USDJPY"),
    ("nikkei",  "^N225",     "Nikkei 225"),
    ("hangseng","^HSI",      "Hang Seng"),
    ("nifty_prev_close", "^NSEI", "Nifty 50 (prev close)"),
]
MISSING_FEEDS = ["gift_nifty_futures"]   # no reliable free feed; flagged as a follow-up


def _ist_now():
    return datetime.now(timezone(timedelta(hours=5, minutes=30)))


def _next_session(d: date):
    """Next NSE weekday session after d (holiday-blind, conservative)."""
    try:
        sys.path.insert(0, str(HERE.parent / "engine"))
        from nse_calendar import is_session
    except Exception:
        is_session = lambda x: x.weekday() < 5
    n = d + timedelta(days=1)
    for _ in range(7):
        if is_session(n):
            return n
        n = n + timedelta(days=1)
    return n


def _upcoming_session(now):
    """The session the forward-guidance tab should target. Pre-open (before 09:15 IST) on a trading
    day -> TODAY (the imminent session); otherwise the next session strictly after today. Mirrors the
    vault emit so both producers agree, and fixes the morning cut labelling today's session as
    tomorrow (F260717c)."""
    from datetime import time as _t
    d = now.date()
    try:
        sys.path.insert(0, str(HERE.parent / "engine"))
        from nse_calendar import is_session
    except Exception:
        is_session = lambda x: x.weekday() < 5
    if is_session(d) and now.time() < _t(9, 15):
        return d
    return _next_session(d)


def _read_json(p, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def fetch_overnight():
    """Pull the REAL overnight rails; per-item try/except; record real vs missing. Never raises."""
    real, ctx = [], {}
    for key, sym, label in OVERNIGHT:
        try:
            h = yf.Ticker(sym).history(period="3d", interval="1d")
            cl = h["Close"].dropna() if h is not None and not h.empty else None
            if cl is not None and len(cl):
                last = float(cl.iloc[-1])
                prev = float(cl.iloc[-2]) if len(cl) >= 2 else last
                ctx[key] = {"label": label, "last": round(last, 2),
                            "chg_pct": round(((last - prev) / prev * 100) if prev else 0, 2)}
                real.append(key)
        except Exception as e:
            print("  overnight %s (%s): %s" % (key, sym, str(e)[:50]), file=sys.stderr)
    return ctx, real


def _overnight_bias(ctx):
    """One honest directional line from whatever rails came back (degrades with fewer feeds)."""
    bits = []
    us = [ctx[k]["chg_pct"] for k in ("sp500", "nasdaq") if k in ctx]
    if us:
        avg = sum(us) / len(us)
        bits.append("US %s %+.1f%%" % ("up" if avg > 0 else "down", avg))
    for k, lbl in (("dxy", "DXY"), ("brent", "Brent"), ("usdinr", "USDINR")):
        if k in ctx:
            bits.append("%s %+.1f%%" % (lbl, ctx[k]["chg_pct"]))
    asia = [ctx[k]["chg_pct"] for k in ("nikkei", "hangseng") if k in ctx]
    if asia:
        a = sum(asia) / len(asia)
        bits.append("Asia %s %+.1f%%" % ("up" if a > 0 else "down", a))
    return " · ".join(bits) if bits else "overnight rails unavailable"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cut", choices=["evening", "morning", "auto"], default="auto")
    a = ap.parse_args()
    now = _ist_now()
    cut = a.cut
    if cut == "auto":                       # before ~12:00 IST = the pre-open morning cut, else evening
        cut = "morning" if now.hour < 12 else "evening"

    if not DATA_JSON.exists():
        print("ERROR: %s not found" % DATA_JSON, file=sys.stderr); return 1
    data = json.loads(DATA_JSON.read_text(encoding="utf-8"))
    di = data.get("daytrade_inputs") or {}
    candidates = di.get("candidates") or {}
    held = di.get("held") or [h.get("ticker") for h in data.get("held", [])]
    if not candidates:
        print("WARN: no daytrade_inputs.candidates — cannot compute next_session; keeping last-good", file=sys.stderr)
        return 0
    if batch_ohlc is None:
        print("ERROR: batch_ohlc unavailable", file=sys.stderr); return 1

    universe = sorted(set(candidates) | set(held))
    ohlc = batch_ohlc(universe)
    covered = sum(1 for t in candidates if len(ohlc.get(t, [])) >= 2)
    if covered < 0.5 * max(1, len(candidates)):
        print("FAIL-CLOSED: coverage %d/%d too low — kept last-good next_session" % (covered, len(candidates)), file=sys.stderr)
        return 0
    rows, price_as_of = daytrade_core.build_panel(candidates, ohlc, set(held))
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    snap = {
        "basis": "overnight_refreshed" if cut == "morning" else "nse_close",
        "as_of_utc": now_iso,
        "as_of_ist": now.strftime("%Y-%m-%d %H:%M IST"),
        "session_date": _upcoming_session(now).isoformat(),
        "price_as_of": price_as_of,
        "n": len(rows),
        "rows": rows,
        "score_version": daytrade_core.SCORE_VERSION,
        "feeds_missing": MISSING_FEEDS,
    }
    if cut == "morning":
        ctx, real = fetch_overnight()
        snap["overnight"] = ctx
        snap["overnight_bias"] = _overnight_bias(ctx)
        snap["feeds_real"] = real
        print("  overnight: real=%s missing=%s | %s" % (real, MISSING_FEEDS, snap["overnight_bias"]))
    else:
        snap["feeds_real"] = []

    data["next_session"] = snap
    data["meta"] = data.get("meta") or {}
    data["meta"]["next_session_cut"] = cut
    data["meta"]["next_session_at"] = now_iso
    DATA_JSON.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print("OK next_session [%s] %d rows for %s (price_as_of %s)" % (snap["basis"], snap["n"], snap["session_date"], price_as_of))
    return 0


if __name__ == "__main__":
    sys.exit(main())
