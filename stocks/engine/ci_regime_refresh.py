#!/usr/bin/env python3
"""
ci_regime_refresh.py — keep the equity dashboard's REGIME live readings <=20 min fresh.

Runs in the light 20-min GitHub Actions job (refresh-stocks-live.yml). It refreshes
ONLY the numeric live readings on the regime card — the macro scalars that actually
move intraday (TNX / DXY / USDINR / Brent) + CCC OAS — and leaves every CURATED field
(zone / score / headline / status / note / threshold) untouched.

Discipline: the cloud is NOT an agent. It updates the READING numbers only; it never
changes a gate's status or the regime zone (those are MACRO_REGIME/agent calls per the
vault doctrine §0.2b). If a fresh reading crosses a threshold, the number shows it and
the next agent pass updates the call.

Sources: Yahoo (5 intermarket quotes via the engine's yahoo_common) + FRED open CSV for
CCC OAS. ~6 network calls per run — light enough for a 20-min cadence. Fully defensive:
any failure leaves the aggregate's regime block exactly as it was (the workflow step is
also `continue-on-error`, so a hiccup never blocks the price refresh).
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ENGINE = Path(__file__).resolve().parent
sys.path.insert(0, str(ENGINE))
AGG = ENGINE.parents[0] / "data" / "equity_dashboard_aggregate.json"  # stocks/data/
FRED_CCC = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=BAMLH0A3HYC"


def _latest_close(ticker: str):
    """Return the latest close for an intermarket ticker via the engine fetcher."""
    try:
        from yahoo_common import fetch_with_fallback
        from fetch_daily_ohlc import extract_latest_bar
        payload, _sym, _status = fetch_with_fallback(ticker, interval="1d", range_="5d", timeout=12)
        if not payload:
            return None
        bar = extract_latest_bar(payload)  # (yh_prev, bar1_close, o, h, l, c, v)
        return float(bar[5]) if bar and bar[5] is not None else None
    except Exception:
        return None


def _ccc_bp():
    try:
        req = urllib.request.Request(FRED_CCC, headers={"User-Agent": "Mozilla/5.0 (TRADER)"})
        for line in urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace").strip().splitlines()[1:]:
            p = line.split(",")
            if len(p) == 2 and p[1].strip() not in ("", ".", "NaN"):
                last = p
        return round(float(last[1]) * 100, 1)  # percent -> bp
    except Exception:
        return None


def main() -> int:
    if not AGG.exists():
        print(f"[ci_regime_refresh] aggregate missing at {AGG} — skip", file=sys.stderr)
        return 0
    try:
        data = json.loads(AGG.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[ci_regime_refresh] aggregate unreadable ({e}) — skip", file=sys.stderr)
        return 0

    regime = data.get("regime") or {}
    gates = regime.get("gates") or []

    dxy = _latest_close("DXY")
    tnx = _latest_close("TNX")
    inr = _latest_close("USDINR")
    brent = _latest_close("BRENT")
    ccc = _ccc_bp()

    # id -> formatted reading (only update when we actually got a fresh number)
    fresh = {}
    if dxy is not None:
        fresh["DXY"] = f"{dxy:.2f}"
    if tnx is not None:
        fresh["TNX"] = f"{tnx:.2f}%"
    if inr is not None:
        fresh["USDINR"] = f"{inr:.3f}"
    if brent is not None:
        fresh["HORMUZ"] = f"oil ${brent:.2f}"
    if ccc is not None:
        fresh["CCC_OAS"] = f"{ccc:.0f} bp"

    if not fresh:
        print("[ci_regime_refresh] no fresh readings (network?) — aggregate unchanged", file=sys.stderr)
        return 0

    updated = []
    for g in gates:
        gid = g.get("id")
        if gid in fresh:
            g["reading"] = fresh[gid]          # READING only — status/note/threshold untouched
            updated.append(f"{gid}={fresh[gid]}")

    # also refresh a flat macro-scalars block if the card uses one (defensive)
    macro = regime.get("macro")
    if isinstance(macro, dict):
        for k, v in (("DXY", dxy), ("TNX", tnx), ("USDINR", inr), ("BRENT", brent)):
            if v is not None and k in macro:
                if isinstance(macro[k], dict) and "value" in macro[k]:
                    macro[k]["value"] = v
                else:
                    macro[k] = v

    AGG.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[ci_regime_refresh] refreshed regime readings: {', '.join(updated) or 'none matched'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
