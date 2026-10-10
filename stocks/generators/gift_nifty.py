#!/usr/bin/env python3
"""gift_nifty.py — GIFT Nifty pre-open log (F261009-SWITCH). Runs in the cloud (gift-nifty.yml).

Operator, 09-Oct: GIFT Nifty in the 3-4 hours before the open "is a good gauge of how the market
reacts". Each run appends one reading to stocks/data/gift_nifty.json for TODAY (IST):
    {at_ist, price, gap_pct, gap_basis}
gap_pct = where GIFT trades now vs where it traded at YESTERDAY's 15:30 IST (the `--ref` run stores
that reference). That cancels the futures premium over the Nifty spot, so the number reads as the
expected opening gap. Without a reference it falls back to the feed's own day change (labelled).
Public data only; no holdings. Every failure is recorded in `errors`, never a silent blank.
"""
from __future__ import annotations
import json, sys, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
OUT = Path(__file__).resolve().parent.parent / "data" / "gift_nifty.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
      "Content-Type": "application/json", "Origin": "https://www.tradingview.com", "Referer": "https://www.tradingview.com/"}


def tv_quote():
    body = json.dumps({"symbols": {"tickers": ["NSEIX:NIFTY1!"], "query": {"types": []}},
                       "columns": ["close", "change", "change_abs", "update_mode"]}).encode()
    req = urllib.request.Request("https://scanner.tradingview.com/global/scan", data=body, headers=UA, method="POST")
    d = json.loads(urllib.request.urlopen(req, timeout=20).read())
    row = (d.get("data") or [])[0]["d"]
    return float(row[0]), (float(row[1]) if row[1] is not None else None), "tradingview NSEIX:NIFTY1!"


def main():
    ref_mode = "--ref" in sys.argv
    now = datetime.now(IST)
    today = now.date().isoformat()
    try:
        cur = json.loads(OUT.read_text(encoding="utf-8"))
    except Exception:
        cur = {}
    ref = cur.get("ref") or {}
    errors = []
    try:
        price, chg, src = tv_quote()
    except Exception as e:
        price, chg, src = None, None, None
        errors.append("tradingview: %s %s" % (type(e).__name__, str(e)[:80]))
    if ref_mode:
        if price is not None:
            ref = {"date": today, "at_ist": now.strftime("%H:%M"), "price": round(price, 2)}
        cur["ref"] = ref
        cur["ref_errors"] = errors
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(cur, indent=1), encoding="utf-8")
        print("[gift] reference %s" % (ref if price is not None else errors))
        return 0 if price is not None else 1
    if cur.get("date") != today:
        cur = {"date": today, "readings": [], "ref": ref}
    reading = {"at_ist": now.strftime("%H:%M"), "price": None, "gap_pct": None, "gap_basis": None}
    if price is not None:
        reading["price"] = round(price, 2)
        if ref.get("price") and ref.get("date") and ref["date"] < today:
            reading["gap_pct"] = round((price / ref["price"] - 1) * 100, 2)
            reading["gap_basis"] = "vs GIFT at %s %s" % (ref["date"], ref.get("at_ist", "15:30"))
        elif chg is not None:
            reading["gap_pct"] = round(chg, 2)
            reading["gap_basis"] = "feed day change (no 15:30 reference yet)"
    reading["source"] = src
    cur["readings"] = (cur.get("readings") or []) + [reading]
    cur["errors"] = errors
    cur["updated_ist"] = now.isoformat(timespec="seconds")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(cur, indent=1), encoding="utf-8")
    print("[gift] %s %s gap %s (%s)%s" % (today, reading["at_ist"], reading["gap_pct"], reading["gap_basis"],
                                          (" ERRORS " + "; ".join(errors)) if errors else ""))
    return 0 if price is not None else 1


if __name__ == "__main__":
    sys.exit(main())
