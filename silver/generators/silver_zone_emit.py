#!/usr/bin/env python3
"""silver_zone_emit.py — SILVER + LANDING island data layer (Phase-3b, HANDOVER_260704_v7 L3-P3b).

Rolls the projection pattern to the silver dashboard tabs (Macro / Operator / Intel) and the
landing page. Each surface is fed by a slice PROJECTED from the authoritative silver aggregate
(00_SYSTEM/_state/silver_dashboard_aggregate.json) — NOT a fork, NOT a re-scan. Each payload
carries `count` = its authoritative source-slice length for the build-parity gate:

  silver-macro    -> sr_levels resistance+support   (public S/R ladder + rails/forecast/floor/prob)
  silver-operator -> strategy_timeline_public        (public timeline; family book money/qty LOCKED)
  silver-intel    -> global_inventory.items          (COT + inventory + news market intel)
  landing         -> current_market rails            (dual-desk public overview)

PRIVACY IS LOAD-BEARING (F123b, operator directive). This module:
  1. NEVER reads or emits `sensitive_enc` (the AES-GCM family blob).
  2. For the Operator surface, projects public advisory LEVELS only (S/R zones, thresholds) and
     NEVER absolute quantities / money — those stay encrypted; the island shows locked badges.
  3. SANITIZES every projected string: forbidden network/family tokens (Kaarin/Rajiv/Sanjeev/
     Kuduz/Kelkar/HDFC/Sparsh) are stripped and dict keys carrying them are renamed. The public
     silver aggregate today leaks "Kaarin-fib" (PEOPLE.md: network names must NEVER appear
     externally) — this neutralizes it. assert_clean() raises if any token survives.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VAULT = HERE.parents[1]
SILVER_AGG = VAULT / "00_SYSTEM" / "_state" / "silver_dashboard_aggregate.json"
ISLAND_DATA_DIR = VAULT / "00_SYSTEM" / "DASHBOARDS" / "astro-shell" / "src" / "data" / "zones"

sys.path.insert(0, str(HERE))
from dr_island_emit import load_source_aggregate as load_equity_aggregate  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SURFACES = ["silver-macro", "silver-operator", "silver-intel", "landing"]

# Real network/family/account identifiers that must NEVER appear in public output (PEOPLE.md,
# F123b). Case-insensitive; a trailing hyphen/space is absorbed so "Kaarin-fib" -> "fib".
FORBIDDEN = ["Rajiv", "Sanjeev", "Kaarin", "Kuduz", "Kelkar", "HDFC", "Sparsh"]
_STRIP_RE = re.compile(r"(?i)\b(" + "|".join(FORBIDDEN) + r")[-_ ]?")
_KEY_RE = re.compile(r"(?i)(" + "|".join(FORBIDDEN) + r")[_-]?")


def _clean_str(s: str) -> str:
    out = _STRIP_RE.sub("", s)
    return re.sub(r"\s{2,}", " ", out).strip()


def _clean_key(k: str) -> str:
    nk = _KEY_RE.sub("", k).strip("_-") or k
    return nk


def sanitize(obj):
    """Recursively strip forbidden tokens from all strings + rename carrying keys."""
    if isinstance(obj, dict):
        return {_clean_key(k): sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize(x) for x in obj]
    if isinstance(obj, str):
        return _clean_str(obj)
    return obj


def assert_clean(payload: dict) -> None:
    """Raise if any forbidden token survives, or sensitive_enc leaked. Defense in depth."""
    blob = json.dumps(payload, ensure_ascii=False)
    for tok in FORBIDDEN:
        if re.search(r"(?i)\b" + re.escape(tok) + r"\b", blob):
            raise ValueError("PRIVACY LEAK: forbidden token %r survived into a public payload" % tok)
    if "sensitive_enc" in payload or "SECRETCIPHERTEXT" in blob:
        raise ValueError("PRIVACY LEAK: sensitive_enc reference in a public payload")


def sanitize_public_aggregate(out: dict) -> dict:
    """LIVE-EMIT privacy boundary (F260704). Sanitize every PUBLIC field of a full silver
    aggregate (strip forbidden network/family tokens + rename carrying keys) while leaving the
    encrypted `sensitive_enc` ciphertext BYTE-EXACT (mangling it would break AES-GCM decrypt).
    Raises if any forbidden token survives in the public portion. This is the ONE shared
    boundary the island build gate and the live emitter both enforce, so a network name can
    never reach the public repo again (PEOPLE.md / F123b)."""
    clean = {}
    for k, v in out.items():
        clean[k] = v if k == "sensitive_enc" else sanitize(v)
    public = {k: v for k, v in clean.items() if k != "sensitive_enc"}
    blob = json.dumps(public, ensure_ascii=False, default=str)
    for tok in FORBIDDEN:
        if re.search(r"(?i)\b" + re.escape(tok) + r"\b", blob):
            raise ValueError("PRIVACY LEAK: network token %r survived into the public silver aggregate" % tok)
    return clean


def _rails(agg: dict) -> list:
    cm = agg.get("current_market") or {}
    out = []
    for k, v in cm.items():
        if k == "fetched_at_utc":
            continue
        if isinstance(v, dict):
            out.append({"key": k, "price": v.get("price"), "day_chg_pct": v.get("day_chg_pct")})
        else:
            out.append({"key": k, "price": v, "day_chg_pct": None})
    return out


def _meta(agg: dict) -> dict:
    return {"source_emitted_at_utc": agg.get("emitted_at_utc"),
            "verdict": (agg.get("forecast") or {}).get("composite_verdict")}


# Operator surface: ONLY these public advisory LEVEL fields are ever projected. Absolute
# quantities / money (units, freed_capital, Cr figures) are deliberately EXCLUDED — locked.
def _operator_levels(agg: dict) -> dict:
    ss = agg.get("silver_strategy") or {}
    ct = ss.get("core_vs_tactical") or {}
    ft = ss.get("final_tranche") or {}
    return {
        "core_tactical_threshold_xag": ct.get("threshold_xag"),
        "core_tactical_rule": ct.get("rule"),
        "final_tranche_zone_xag": ft.get("zone_xag"),
        "final_tranche_zone_sb": ft.get("zone_sb"),
        "floor_tiers": [
            {"tier": t.get("tier"), "band_low": t.get("band_low"), "band_high": t.get("band_high"),
             "prob_pct": t.get("prob_pct"), "mechanism": t.get("mechanism")}
            for t in (agg.get("floor_framework") or {}).get("tiers", [])
        ],
    }


def build_silver_zone(agg: dict, surface: str) -> dict:
    if surface == "silver-macro":
        sr = agg.get("sr_levels") or {}
        rows = [{**r, "side": "resistance"} for r in (sr.get("resistance") or [])] + \
               [{**r, "side": "support"} for r in (sr.get("support") or [])]
        payload = {
            "surface": surface, "count": len(rows), "rows": rows,
            "rails": _rails(agg),
            "forecast": {k: (agg.get("forecast") or {}).get(k) for k in ("composite_verdict", "consensus_state", "probability_mass")},
            "bull_bear_weights": (agg.get("bull_bear") or {}).get("scenario_weights"),
            "probability": (agg.get("probability") or {}).get("distribution") or [],
            "floor_tiers": [{"tier": t.get("tier"), "band_low": t.get("band_low"), "prob_pct": t.get("prob_pct"), "mechanism": t.get("mechanism")}
                            for t in (agg.get("floor_framework") or {}).get("tiers", [])],
            "catalysts": agg.get("catalysts") or [],
            "meta": _meta(agg),
        }
    elif surface == "silver-operator":
        tl = agg.get("strategy_timeline_public") or []
        payload = {
            "surface": surface, "count": len(tl), "rows": tl,
            "strategy_levels": _operator_levels(agg),   # LEVELS only — no qty/money
            "ledger_locked": True,
            "meta": _meta(agg),
        }
    elif surface == "silver-intel":
        items = (agg.get("global_inventory") or {}).get("items") or []
        cot = agg.get("cot") or {}
        payload = {
            "surface": surface, "count": len(items), "rows": items,
            "cot": {k: cot.get(k) for k in ("mm_net_k", "oi_k", "mm_net_percentile", "read", "ladder_rung", "g8_status", "survey_date", "verified")},
            "paper_physical": (agg.get("paper_physical") or {}).get("components") or [],
            "news": (agg.get("news") or {}).get("headlines") or [],
            "meta": _meta(agg),
        }
    else:
        raise ValueError("unknown silver surface: %s" % surface)
    payload = sanitize(payload)
    assert_clean(payload)
    return payload


def build_landing(silver_agg: dict, equity_agg: dict) -> dict:
    rails = _rails(silver_agg)
    reg = equity_agg.get("regime") or {}
    payload = {
        "surface": "landing", "count": len(rails), "rows": rails,
        "silver_verdict": (silver_agg.get("forecast") or {}).get("composite_verdict"),
        "equity": {"regime_zone": reg.get("zone"), "regime_score": reg.get("score"),
                   "held_count": len(equity_agg.get("held") or [])},
        "meta": {"silver_emitted": silver_agg.get("emitted_at_utc"), "equity_emitted": equity_agg.get("emitted_at_utc")},
    }
    payload = sanitize(payload)
    assert_clean(payload)
    return payload


def _load_silver() -> dict:
    return json.loads(SILVER_AGG.read_text(encoding="utf-8"))


def emit(out_dir=ISLAND_DATA_DIR) -> dict:
    silver = _load_silver()
    equity, _ = load_equity_aggregate()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    counts = {}
    for surface in ("silver-macro", "silver-operator", "silver-intel"):
        p = build_silver_zone(silver, surface)
        (out / (surface + ".json")).write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
        counts[surface] = p["count"]
    land = build_landing(silver, equity)
    (out / "landing.json").write_text(json.dumps(land, ensure_ascii=False, indent=2), encoding="utf-8")
    counts["landing"] = land["count"]
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description="Emit silver + landing island data (projected + privacy-sanitized)")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    silver = _load_silver()
    equity, _ = load_equity_aggregate()
    counts = {s: build_silver_zone(silver, s)["count"] for s in ("silver-macro", "silver-operator", "silver-intel")}
    counts["landing"] = build_landing(silver, equity)["count"]
    print("silver source:", SILVER_AGG.name, "| counts:", counts)
    if args.check:
        return 0
    emit()
    print("✓ wrote silver-macro / silver-operator / silver-intel / landing to", ISLAND_DATA_DIR.relative_to(VAULT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
