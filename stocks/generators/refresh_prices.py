#!/usr/bin/env python3
"""
refresh_prices.py — pull live NSE prices for held tickers and update the
equity_dashboard_aggregate.json in place. Only price-related fields are
touched; thesis data (gates, briefs, regime, screener scores) is left as-is.

Runs in GitHub Actions (vanilla yfinance, no proxy). Does NOT require
access to the operator's private vault.

Flow:
  1. Load stocks/data/equity_dashboard_aggregate.json
  2. For each held ticker, fetch current NSE price via yfinance
  3. Recompute current_value, unreal, day_chg per holding + book totals + concentration
  4. Write back. Workflow commits if changed.
"""

from __future__ import annotations
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf

HERE = Path(__file__).resolve().parent
DATA_JSON = HERE.parent / "data" / "equity_dashboard_aggregate.json"

# NSE symbol mapping (yfinance NSE suffix is .NS)
NSE_SYMBOLS = {
    "TARIL":     "TARIL.NS",
    "GMDCLTD":   "GMDCLTD.NS",
    "HBLENGINE": "HBLENGINE.NS",
    "MTARTECH":  "MTARTECH.NS",
}


def fetch_price(symbol: str):
    """Return (current_price, previous_close) or (None, None) on failure."""
    try:
        t = yf.Ticker(symbol)
        fi = t.fast_info
        price = float(fi.last_price)
        prev = float(fi.previous_close)
        return price, prev
    except Exception:
        pass
    try:
        h = yf.Ticker(symbol).history(period="5d", interval="1d")
        if h.empty:
            return None, None
        price = float(h["Close"].iloc[-1])
        prev = float(h["Close"].iloc[-2]) if len(h) > 1 else price
        return price, prev
    except Exception as e:
        print(f"  warning {symbol}: fetch failed: {e}", file=sys.stderr)
        return None, None


# Universe symbol overrides (mirror yahoo_common SPECIAL for yfinance)
UNIVERSE_SPECIAL = {
    "BLUESTAR": "BLUESTARCO.NS", "KPIT": "KPITTECH.NS", "PBFINTECH": "POLICYBZR.NS",
    "AREM": "AMARAJABAT.NS", "WAAREEENER": "WAAREEENER.NS",
}


def fetch_closes(sym, period="1mo"):
    """Recent daily closes for a symbol. Returns (list[float], as_of_date) or (None, None)."""
    try:
        h = yf.Ticker(sym).history(period=period, interval="1d")
        if h is None or h.empty:
            return None, None
        closes = [float(x) for x in h["Close"].tolist() if x == x]
        if not closes:
            return None, None
        return closes, str(h.index[-1].date())
    except Exception:
        return None, None


def refresh_universe_moves(data):
    """Refresh 1d/3d/5d moves + ltp for every watchlist_rundown name, then re-rank the
    day-trade panel on fresh 1d momentum. Per-ticker guarded; keeps prior on any failure."""
    sc = data.get("screeners", {})
    rundown = sc.get("watchlist_rundown", [])
    if not rundown:
        return
    d1_by = {}
    ok = 0
    for r in rundown:
        tk = r.get("ticker")
        sym = UNIVERSE_SPECIAL.get(tk, f"{tk}.NS")
        closes, asof = fetch_closes(sym)
        if not closes or len(closes) < 2:
            continue
        latest = closes[-1]

        def pct(n):
            i = len(closes) - 1 - n
            return round((latest - closes[i]) / closes[i] * 100, 2) if (i >= 0 and closes[i]) else None
        r["moves"] = {"as_of": asof, "ltp": round(latest, 2), "d1": pct(1), "d3": pct(3), "d5": pct(5)}
        r["ltp"] = round(latest, 2)
        r["as_of"] = asof
        if r.get("pending"):
            r["pending"] = False
        d1_by[tk] = pct(1)
        ok += 1
    # re-rank day-trade panel on fresh d1 (static signals composite/stddev/vol/fresh unchanged)
    for p in sc.get("daytrade_panel", []):
        d1 = d1_by.get(p.get("ticker"))
        if d1 is not None:
            p["d1"] = d1
        std = p.get("stddev_10d_pct") or 0
        dd = p.get("d1")
        p["score"] = round(p.get("composite", 0) * 0.45 + min(std, 6.0) * 7.0
                           + (10 if p.get("vol_spike") else 0) + (14 if p.get("fresh") else 0)
                           + (abs(dd) * 2.0 if dd is not None else 0), 1)
    sc.get("daytrade_panel", []).sort(key=lambda x: -(x.get("score") or 0))
    print(f"  universe moves refreshed: {ok}/{len(rundown)} names")


def main() -> int:
    if not DATA_JSON.exists():
        print(f"ERROR: {DATA_JSON} not found", file=sys.stderr)
        return 1
    data = json.loads(DATA_JSON.read_text(encoding="utf-8"))
    print(f"Loaded {DATA_JSON.name} - {len(data.get('held', []))} holdings")

    new_mv = 0.0
    new_invested = 0.0
    new_unreal = 0.0
    day_chg_inr = 0.0

    for h in data.get("held", []):
        ticker = h.get("ticker")
        sym = NSE_SYMBOLS.get(ticker)
        qty = float(h.get("qty") or 0)
        avg = float(h.get("avg_cost") or 0)
        if not sym:
            print(f"  {ticker}: no NSE symbol; keeping prior price")
            new_mv += float(h.get("current_value") or 0)
            new_invested += avg * qty
            new_unreal += float(h.get("unreal_inr") or 0)
            continue
        price, prev = fetch_price(sym)
        if price is None:
            print(f"  {ticker} ({sym}): no price; keeping prior")
            new_mv += float(h.get("current_value") or 0)
            new_invested += avg * qty
            new_unreal += float(h.get("unreal_inr") or 0)
            continue
        prior = h.get("current_price")
        mv = price * qty
        invested = avg * qty
        unreal = mv - invested
        day_chg_pct = ((price - prev) / prev * 100) if prev else 0
        h["current_price"] = round(price, 2)
        h["current_value"] = round(mv, 2)
        h["invested"]      = round(invested, 2)
        h["unreal_inr"]    = round(unreal, 2)
        h["unreal_pct"]    = round((unreal / invested * 100) if invested else 0, 2)
        h["day_chg_pct"]   = round(day_chg_pct, 2)
        new_mv += mv
        new_invested += invested
        new_unreal += unreal
        day_chg_inr += (price - prev) * qty
        print(f"  {ticker:10s} {sym:14s} Rs.{prior} -> Rs.{price:.2f}  day {day_chg_pct:+.2f}%")

    tot = data.setdefault("book", {}).setdefault("totals", {})
    tot["current_value"]      = round(new_mv, 2)
    tot["invested"]           = round(new_invested, 2)
    tot["unrealized_pnl_abs"] = round(new_unreal, 2)
    tot["unrealized_pnl_pct"] = round((new_unreal / new_invested * 100) if new_invested else 0, 2)
    tot["change_today_abs"]   = round(day_chg_inr, 2)
    prev_mv = new_mv - day_chg_inr
    tot["change_today_pct"]   = round((day_chg_inr / prev_mv * 100) if prev_mv else 0, 2)

    for h in data.get("held", []):
        cv = float(h.get("current_value") or 0)
        h["concentration_pct"] = round((cv / new_mv * 100) if new_mv else 0, 2)

    try:
        refresh_universe_moves(data)
    except Exception as e:
        print(f"  universe-moves refresh skipped (held/book refresh intact): {e}", file=sys.stderr)

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data["emitted_at_utc"] = now_iso
    data.setdefault("meta", {})["last_price_refresh"] = now_iso

    DATA_JSON.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"OK wrote {DATA_JSON.name}  MV Rs.{new_mv:,.0f}  unreal {tot['unrealized_pnl_pct']}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
