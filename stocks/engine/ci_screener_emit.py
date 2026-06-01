#!/usr/bin/env python3
"""ci_screener_emit.py — rebuild the SCREENER section of the published aggregate in CI.

Runs in GitHub Actions (laptop-off). Imports the vault emit's builder functions
(no logic divergence), points their path constants at the repo's freshly-generated
lens CSVs + caches + taxonomy, rebuilds screeners.*, and MERGES into the existing
published aggregate — preserving the operator/intel sections (book, held, risk_gates,
regime, dr, flags, catalysts) that the vault pipeline owns.

Sanity-gated: refuses to write if the rebuilt screener section looks degenerate, so a
bad CI run can never publish empty fires to the father-facing dashboard.

Flow in the workflow:
  fetch_*  ->  screener_runner.py  ->  ci_screener_emit.py  ->  refresh_prices.py  ->  commit
"""
from __future__ import annotations
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent          # stocks/engine
REPO_STOCKS = HERE.parent                        # stocks
AGG = REPO_STOCKS / "data" / "equity_dashboard_aggregate.json"

import equity_dashboard_emit as E

# Repoint the vault emit's path constants at the repo layout
E.LENS_DIR = REPO_STOCKS / "03_SCREENERS" / "LAYERS"
E.WATCHLIST_DIR = REPO_STOCKS / "03_SCREENERS" / "WATCHLIST"
E.DAILY_PRICES = HERE / "_cache" / "daily_prices.csv"
E.TAXONOMY_YAML = HERE / "equity_taxonomy.yaml"


def main() -> int:
    if not AGG.exists():
        print(f"ERROR: {AGG} missing", file=sys.stderr)
        return 1
    agg = json.loads(AGG.read_text(encoding="utf-8"))
    held = agg.get("held", []) or []
    held_tickers = [h.get("ticker") for h in held if h.get("ticker")]

    lens_runs = [E._read_lens_run(l) for l in E.LENSES]
    # Hard gate: every lens must have produced a dated run with hits, else abort (keep prior).
    bad = [r["lens"] for r in lens_runs if r.get("status") == "missing" or not r.get("run_date")]
    if bad:
        print(f"ABORT: lenses missing/undated: {bad} — keeping prior aggregate (no publish)", file=sys.stderr)
        return 3

    name_index = E._build_name_index(lens_runs)
    universe_meta = E._read_universe_meta()
    prices = E._load_daily_prices()
    taxonomy = E._load_taxonomy()

    screeners = {
        "lenses": lens_runs,
        "lens_timeline": E._build_lens_timeline(E.LENSES, days=14),
        # watchlist_runner is not run in CI -> preserve the vault-emitted watchlists block
        "watchlists": (agg.get("screeners", {}) or {}).get("watchlists", []),
        "hit_matrix": E._build_hit_matrix(lens_runs, held_tickers),
        "promotion_candidates": E._build_promotion_candidates(lens_runs, held_tickers),
        "held_lens_grid": E._build_held_lens_grid(lens_runs, held_tickers),
        "bubble_set": E._build_bubble_set(name_index, universe_meta, held_tickers, taxonomy),
        "watchlist_rundown": E._build_watchlist_rundown(name_index, universe_meta, prices, held_tickers, taxonomy),
        "universe_count": len(set(name_index) | set(universe_meta) | set(taxonomy.get("tickers", {}))),
        "layer_order": taxonomy.get("layer_order", []),
        "layer_names": taxonomy.get("layer_names", {}),
        "daytrade_panel": E._build_daytrade_panel(name_index, universe_meta, prices, E._prior_multi_hits(), held_tickers),
    }

    # ── sanity gate: never publish a degenerate screener section ──
    nb = len(screeners["bubble_set"])
    nr = len(screeners["watchlist_rundown"])
    if nb < 55 or nr < 55:
        print(f"ABORT: degenerate screener (bubble={nb} rundown={nr}) — keeping prior aggregate", file=sys.stderr)
        return 4
    if not screeners["lenses"][0].get("run_date"):
        print("ABORT: lens run_date missing", file=sys.stderr)
        return 5

    agg["screeners"] = screeners
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    agg["emitted_at_utc"] = now
    agg.setdefault("meta", {})["last_screener_refresh_ci"] = now

    def _jd(o):
        return o.isoformat() if hasattr(o, "isoformat") else str(o)
    AGG.write_text(json.dumps(agg, indent=2, ensure_ascii=False, default=_jd), encoding="utf-8")
    lr = ", ".join(f"{r['lens']}={r.get('run_date')}" for r in lens_runs)
    print(f"[ci] screeners rebuilt: bubble={nb} rundown={nr} daytrade={len(screeners['daytrade_panel'])} | {lr}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
