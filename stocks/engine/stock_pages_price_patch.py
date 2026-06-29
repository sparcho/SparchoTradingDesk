#!/usr/bin/env python3
"""stock_pages_price_patch.py — fast intraday price refresh for the per-ticker pages.

WHY THIS EXISTS
===============
stock_page_emit.py does the FULL analysis render (needs the vault _index.md) once a
day at the 16:25 publish. But the price + the latest candle should track the live
20-min refresh. This script patches ONLY the `public.live` block + the last OHLC bar
of each already-emitted data.json, straight from daily_prices.csv. It needs NO
_index.md, so it can run anywhere the price cache + the data.json files exist —
including the cloud 20-min job (the data.json live in the repo; the cloud fetches
daily OHLC into its own cache, pointed at via --prices).

PRIVACY: only touches `public.live` and `public.ohlc` (price/volume — public market
data). It NEVER adds holdings; an existing encrypted block is left untouched.

USAGE
-----
  python3 stock_pages_price_patch.py                 # patch vault + repo from the vault cache
  python3 stock_pages_price_patch.py --prices PATH   # use a specific daily_prices.csv (cloud)
  python3 stock_pages_price_patch.py --dirs D1 D2     # patch specific ticker roots
"""
from __future__ import annotations
import argparse, csv, json, os, sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _root():
    p = HERE
    for _ in range(8):
        if (p / "02_STOCKS").is_dir():
            return p
        if p.parent == p:
            break
        p = p.parent
    return HERE.parent.parent


ROOT = _root()
DEFAULT_PRICES = ROOT / "00_SYSTEM" / "GENERATORS" / "_cache" / "daily_prices.csv"
REPO = Path(os.environ.get("SPARCHO_DESK_REPO", r"C:\Users\user\Documents\GitHub\SparchoTradingDesk"))
DEFAULT_DIRS = [
    ROOT / "00_SYSTEM" / "DASHBOARDS" / "equity" / "stocks" / "ticker",
    REPO / "stocks" / "ticker",
]


def load_prices(path: Path):
    """ticker -> sorted [(date, open, high, low, close, day_chg_pct)] (valid closes only)."""
    series = {}
    if not path.exists():
        return series
    with path.open("r", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            t, d = r.get("ticker"), r.get("date")
            if not t or not d:
                continue
            try:
                c = float(r.get("close") or 0)
            except ValueError:
                continue
            if c <= 0:
                continue
            def g(k):
                try:
                    v = float(r.get(k) or 0)
                    return v if v > 0 else c
                except ValueError:
                    return c
            try:
                day = float(r.get("day_chg_pct") or 0)
            except ValueError:
                day = None
            series.setdefault(t, []).append((d, g("open"), g("high"), g("low"), c, day))
    for t in series:
        series[t].sort()
    return series


def patch_dir(base: Path, series, candles: bool):
    n = ok = 0
    if not base.exists():
        return 0, 0, "absent"
    for dj in sorted(base.glob("*/data.json")):
        n += 1
        try:
            d = json.loads(dj.read_text(encoding="utf-8"))
        except Exception:
            continue
        p = d.get("public") or {}
        tk = p.get("ticker") or dj.parent.name
        pts = series.get(tk)
        if not pts:
            continue
        last = pts[-1]
        ld, lo, lh, ll, lc, lday = last
        ref = pts[-6][4] if len(pts) >= 6 else None
        week = ((lc - ref) / ref * 100) if ref else (p.get("live") or {}).get("week_chg_pct")
        p["live"] = {"price": lc, "day_chg_pct": lday, "week_chg_pct": week, "date": ld}
        # refresh/append the latest candle in the OHLC series (only for candle charts)
        if candles and p.get("chart_kind") == "candles" and isinstance(p.get("ohlc"), list) and p["ohlc"]:
            oh = p["ohlc"]
            bar = [ld, lo, lh, ll, lc]
            if oh and oh[-1] and oh[-1][0] == ld:
                oh[-1] = bar
            elif oh and oh[-1] and oh[-1][0] < ld:
                oh.append(bar)
        # refresh gate_check distances + scenario base level vs the new price
        gl = p.get("gate_levels") or []
        if gl:
            gc = []
            for g in gl:
                gc.append({"label": g["label"], "price": g["price"], "dir": g.get("dir"),
                           "kind": g.get("kind"), "dist_pct": (g["price"] - lc) / lc * 100})
            gc.sort(key=lambda x: abs(x["dist_pct"]))
            p["gate_check"] = gc
        d["public"] = p
        d["price_patched_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        tmp = dj.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        tmp.replace(dj)
        ok += 1
    return n, ok, "ok"


def main():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--prices", default=str(DEFAULT_PRICES))
    ap.add_argument("--dirs", nargs="+")
    ap.add_argument("--no-candles", action="store_true", help="patch live block only, leave OHLC")
    args = ap.parse_args()
    series = load_prices(Path(args.prices))
    if not series:
        print(f"[warn] no prices loaded from {args.prices} — nothing to patch")
        return 1
    dirs = [Path(d) for d in args.dirs] if args.dirs else DEFAULT_DIRS
    total = 0
    for base in dirs:
        n, ok, status = patch_dir(base, series, not args.no_candles)
        print(f"  {base}: {ok}/{n} patched ({status})")
        total += ok
    print(f"[ok] price-patched {total} page(s) from {Path(args.prices).name} ({len(series)} tickers in cache)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
