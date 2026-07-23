#!/usr/bin/env python3
"""dr_island_emit.py — Research/Intel ISLAND data layer (Phase-2, HANDOVER_260703_v4 L3-P2).

WHY THIS EXISTS
  The Astro Research/Intel island must render the SAME deep-research corpus the live
  equity desk's renderResearch() shows — never a forked/re-scanned copy. renderResearch()
  reads DATA.dr from the published equity_dashboard_aggregate.json. So this emitter simply
  PROJECTS that aggregate's `dr` block into the island's build-time JSON
  (astro-shell/src/data/dr.json). Parity is guaranteed by construction: same bytes in,
  same streams out. The only ADDITIVE step is a derived `sectors` facet per stream, mapped
  from the aggregate's OWN ticker->sector data (held / bubble_set / watchlist_rundown /
  daytrade_panel) — same source, so still not a fork.

AUTHORITATIVE SOURCE
  The REPO working copy (what actually gets published/served) is authoritative and fresh;
  the in-vault dashboard copy can lag (per the equity-dashboard-pipeline map). We prefer
  the repo copy, fall back to the vault copy, so the island shows what the live desk shows.

USAGE
  python3 dr_island_emit.py            # emit astro-shell/src/data/dr.json from the live aggregate
  python3 dr_island_emit.py --check    # print parity summary, do not write
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VAULT = HERE.parents[1]
REPO = Path(r"C:/Users/user/Documents/GitHub/SparchoTradingDesk")

# Authoritative first (published/served), vault copy as fallback.
AGG_CANDIDATES = [
    REPO / "stocks" / "data" / "equity_dashboard_aggregate.json",
    VAULT / "00_SYSTEM" / "DASHBOARDS" / "equity" / "stocks" / "data" / "equity_dashboard_aggregate.json",
]
ISLAND_DATA = VAULT / "00_SYSTEM" / "DASHBOARDS" / "astro-shell" / "src" / "data" / "dr.json"

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def load_source_aggregate(candidates=None) -> tuple[dict, Path]:
    """Return (aggregate_dict, path) from the first readable candidate. Raises if none."""
    for p in (candidates or AGG_CANDIDATES):
        if Path(p).is_file():
            return json.loads(Path(p).read_text(encoding="utf-8")), Path(p)
    raise FileNotFoundError("no equity_dashboard_aggregate.json found in " + ", ".join(str(c) for c in (candidates or AGG_CANDIDATES)))


def build_ticker_sector_map(agg: dict) -> dict:
    """ticker -> sector, harvested from the aggregate's own labelled rows. Prefer a real
    sector; ignore the '-' placeholder. Same source as the desk — not a new taxonomy."""
    smap: dict[str, str] = {}
    rows = []
    rows += agg.get("held") or []
    sc = agg.get("screeners") or {}
    for key in ("bubble_set", "watchlist_rundown", "daytrade_panel"):
        rows += sc.get(key) or []
    for r in rows:
        if not isinstance(r, dict):
            continue
        t = r.get("ticker")
        sec = (r.get("sector") or "").strip()
        if t and sec and sec != "-" and t not in smap:
            smap[t] = sec
    return smap


def _dominant_sectors(names, smap: dict) -> list[str]:
    """The distinct sectors a brief's referenced tickers fall into (order-stable, deduped)."""
    seen, out = set(), []
    for n in names or []:
        sec = smap.get(n)
        if sec and sec not in seen:
            seen.add(sec)
            out.append(sec)
    return out


def build_island_dr(agg: dict) -> dict:
    """Project the aggregate's dr block into the island payload. Streams are carried through
    UNCHANGED in identity/order (parity); each gets an additive `sectors` facet."""
    dr = agg.get("dr") or {}
    streams = list(dr.get("streams") or [])
    smap = build_ticker_sector_map(agg)
    enriched = []
    for s in streams:
        s2 = dict(s)                                    # copy — never mutate the source row
        s2["sectors"] = _dominant_sectors(s.get("names"), smap)
        enriched.append(s2)
    return {
        "as_of": dr.get("as_of"),
        "source_emitted_at_utc": agg.get("emitted_at_utc"),
        "stream_count": len(streams),
        "streams": enriched,
        "active_themes": dr.get("active_themes") or [],
        "falsifiable_triggers": dr.get("falsifiable_triggers") or [],
        "coverage_gaps": dr.get("coverage_gaps") or [],
    }


def emit(out_path=ISLAND_DATA, candidates=None) -> dict:
    agg, src = load_source_aggregate(candidates)
    payload = build_island_dr(agg)
    payload["_source_path"] = str(src).replace("\\", "/")
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description="Emit the Research/Intel island DR data (projected from the live aggregate)")
    ap.add_argument("--check", action="store_true", help="print parity summary without writing")
    args = ap.parse_args()
    agg, src = load_source_aggregate()
    payload = build_island_dr(agg)
    print(f"source: {src}")
    print(f"as_of={payload['as_of']} · streams={payload['stream_count']} · "
          f"source_emitted={payload['source_emitted_at_utc']}")
    by_type: dict[str, int] = {}
    for s in payload["streams"]:
        by_type[s.get("type", "?")] = by_type.get(s.get("type", "?"), 0) + 1
    print("by type:", by_type)
    if args.check:
        return 0
    emit()
    print(f"✓ wrote {ISLAND_DATA.relative_to(VAULT)} ({payload['stream_count']} streams)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
