"""fib_confluence_source — turn the fib-confluence bank into the desk's actionable SIGNAL SOURCE.

WHY (settled 2026-07-25, do not relitigate). The paper-lab was a self-grading science project: a
measured -0.32R expectancy whose HIGHEST-scored tier was the WORST (trade_lab.json 2026-07-24,
n=193; hi -0.34R / lo -0.28R). Across the 16,754-signal ledger, gate-count score is INVERSELY
related to forward return in most lenses. The only two things carrying real edge were (a) the
pre-filtered universe and (b) full technical confluence. Meanwhile the fib bank's own ecosystem
review (260714) named its biggest gap: no forward-outcome scoring.

This module is the join: it reads the fib bank (EDGE/fib_confluences.json, itself built from the
operator's VERIFIED EDGE/LEVELS/*.json banks) and emits a FIRE / WATCH / EXCLUDE verdict per name
with real entry / stop / target levels. system_autotrade_logger consumes the FIREs as PLANNED
trades; the existing resolver then forward-scores them — which is exactly the scorer the bank
lacked.

READ-ONLY. This module never writes; it classifies. Levels come from the bank, never from price
action (no fire-day-low, no AUTO_SR — both retired 2026-07-13).
"""
from __future__ import annotations

import json
from pathlib import Path

VAULT = Path(__file__).resolve().parents[2]
BANK = VAULT / "00_SYSTEM" / "EDGE" / "fib_confluences.json"


def load_bank(path=None) -> dict:
    """The fib bank payload, or {} if it is absent/unreadable.

    {} is an HONEST empty — the caller sees zero names and zero provenance, so a dead bank reads
    as dead rather than as a healthy day with no fires."""
    try:
        return json.loads(Path(path or BANK).read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}


# ── the gate ──────────────────────────────────────────────────────────────────
# Operator-set 2026-07-25, live-verified against the 24-Jul bank run
# (FIRE PARADEEP + HAL · 5 WATCH · 34 EXCLUDE, HSCL correctly excluded).
STOP_BUFFER = 0.005    # invalidation sits 0.5% UNDER the banked support zone's floor, not at it
RR_MIN = 1.5           # reward:risk floor to the next banked level — below this it is a WATCH
MIN_SOURCES = 3        # a "confluence" needs 3+ independent sources; 2 is a coincidence

FIRE_SETUPS = ("buy-support", "at-support")     # price is AT a support confluence
WATCH_SETUPS = ("approaching", "in-zone")       # structurally sound, not actionable yet
OK_TRENDS = ("up", "mixed")                     # never buy support into a downtrend


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def classify(name: dict) -> dict:
    """FIRE / WATCH / EXCLUDE for one fib-bank name, with the levels and the WHY (V-03).

    FIRE  = at_confluence & grade STRONG & key_support verified with 3+ sources & setup is
            at/buy-support & trend up-or-mixed & no unconfirmed supply overhead & R:R >= 1.5.
    WATCH = clears every STRUCTURAL gate above but is not actionable yet (price not at the level,
            price inside the band, thin R:R, or no target to aim at). WATCH is a DEMOTION of a
            qualifying candidate — not a bucket for everything that failed.
    EXCLUDE = everything else.
    """
    ks = name.get("key_support") or {}
    kr = name.get("key_resistance") or {}
    zone = ks.get("zone") or {}

    entry = _num(ks.get("px"))
    zone_lo = _num(zone.get("lo"))
    stop = round(zone_lo * (1 - STOP_BUFFER), 2) if zone_lo is not None else None
    target = _num(kr.get("px"))
    rr = None
    if entry is not None and stop is not None and target is not None and entry > stop:
        rr = (target - entry) / (entry - stop)

    setup = str(name.get("setup") or "")
    trend = str(name.get("trend") or "")
    flags = [str(f) for f in (name.get("flags") or [])]
    why: list = []

    # ── STRUCTURAL gates. Failing ANY of these is an EXCLUDE, not a WATCH: the name is not a
    #    candidate at all. (Reading these as demotions gives 23 WATCH / 16 EXCLUDE against the
    #    operator's live-verified 5 / 34.)
    if setup not in FIRE_SETUPS + WATCH_SETUPS:
        why.append("setup=%s is not a support set-up" % (setup or "unknown"))
    if not name.get("at_confluence"):
        why.append("price is not AT any confluence on this chart")
    if str(name.get("grade") or "") != "STRONG":
        why.append("top cluster grades %s, not STRONG" % (name.get("grade") or "unknown"))
    if trend not in OK_TRENDS:
        why.append("trend=%s — never buy support into a downtrend" % (trend or "unknown"))
    if "supply-unconfirmed" in flags:
        why.append("cluster carries SUPPLY semantics price has NOT accepted above — not a floor")
    if not ks.get("verified"):
        why.append("key support is not a hand-verified Kaarin-Fib read (A2: verified banks only)")
    if (ks.get("sources") or 0) < MIN_SOURCES:
        why.append("key support has %s source(s), under the %d-source confluence floor"
                   % (ks.get("sources") or 0, MIN_SOURCES))
    if stop is None:
        why.append("key support has no banked zone — no invalidation level to stop against")

    if why:
        return {"ticker": name.get("ticker"), "verdict": "EXCLUDE", "entry": entry, "stop": stop,
                "target": target, "rr": rr, "why_verdict": why}

    # ── ACTIONABILITY. Everything below reaches here already structurally sound, so a failure is
    #    a DEMOTION to WATCH — the name stays on the radar, it just is not a fire today.
    if setup in WATCH_SETUPS:
        why.append("price is not at the level yet (setup=%s) — watch for the tag" % setup
                   if setup == "approaching" else
                   "price is INSIDE the band (setup=in-zone) — neither support nor resistance yet")
    if target is None:
        why.append("no banked level overhead to target — nothing to size a reward against")
    elif rr is not None and rr < RR_MIN:
        why.append("R:R %.2f to %.2f is under the %.1f floor — not enough room to the next level"
                   % (rr, target, RR_MIN))

    if why:
        return {"ticker": name.get("ticker"), "verdict": "WATCH", "entry": entry, "stop": stop,
                "target": target, "rr": rr, "why_verdict": why}

    # V-03 — a FIRE states its WHY too. This is the line the desk renders under the level set.
    why.append("AT a %d-source hand-verified support at %s in a %s trend; %.2f R:R to the next "
               "banked level %s, invalidation %s (%.1f%% under the zone floor)"
               % (ks.get("sources") or 0, entry, trend, rr, target, stop, STOP_BUFFER * 100))
    verdict = "FIRE"

    return {"ticker": name.get("ticker"), "verdict": verdict, "entry": entry, "stop": stop,
            "target": target, "rr": rr, "why_verdict": why}


def select_fib_confluence(payload) -> dict:
    """Bucket a whole fib_confluences.json payload into FIRE / WATCH / EXCLUDE.

    Fires rank by the name's confluence score — the autotrade class quota is "however many the
    source fires", so the ordering only matters when a cap is applied downstream.

    Never raises. A malformed or absent bank must degrade to "no fires" rather than break the
    daily driver; but it must also never SILENTLY look healthy, which is why the provenance
    fields (price_as_of / generated_at_utc) are carried through verbatim for the freshness
    checks to judge (F260721-FIBPROV, and the absence-of-evidence-is-not-health rule)."""
    payload = payload if isinstance(payload, dict) else {}
    buckets: dict = {"FIRE": [], "WATCH": [], "EXCLUDE": []}
    for name in (payload.get("names") or []):
        if not isinstance(name, dict):
            continue
        r = classify(name)
        r["setup"] = name.get("setup")
        r["trend"] = name.get("trend")
        r["grade"] = name.get("grade")
        r["score"] = name.get("score")
        r["current_px"] = name.get("current_px")
        r["flags"] = list(name.get("flags") or [])
        r["why"] = list(name.get("why") or [])          # the bank's own reasoning, carried through
        r["key_support"] = name.get("key_support")
        r["key_resistance"] = name.get("key_resistance")
        buckets[r["verdict"]].append(r)

    for b in buckets.values():
        b.sort(key=lambda r: -(_num(r.get("score")) or 0))

    return {
        "fires": buckets["FIRE"],
        "watch": buckets["WATCH"],
        "excluded": buckets["EXCLUDE"],
        "counts": {"fire": len(buckets["FIRE"]), "watch": len(buckets["WATCH"]),
                   "exclude": len(buckets["EXCLUDE"])},
        "price_as_of": payload.get("price_as_of"),
        "generated_at_utc": payload.get("generated_at_utc"),
        "basis": payload.get("basis"),
        "gate": {"rr_min": RR_MIN, "min_sources": MIN_SOURCES, "stop_buffer": STOP_BUFFER,
                 "fire_setups": list(FIRE_SETUPS), "watch_setups": list(WATCH_SETUPS),
                 "ok_trends": list(OK_TRENDS),
                 "note": "verified banks only (A2) — PERMANENT, operator decision 2026-07-27 "
                         "(F260727-A2-WIDEN). The gate does not widen at any coverage level; "
                         "hand-verification IS the edge. Coverage is the work queue, not a trigger."},
    }


# ── the desk Radar block ──────────────────────────────────────────────────────
# F260727-RADARSUBSTRATE: this projection used to live in equity_dashboard_emit, i.e. only the
# laptop could build it — while EQUITY_BLOCKS declared the block CLOUD, reasoning that it inherits
# the substrate of the data it derives from. Substrate is not inherited; it is who runs the writer.
# It moved here, into the module that owns the gate, so BOTH the laptop emit and the cloud Action
# (stocks/engine/ci_fib_refresh.py) build the block from the same code — the ci_screener_emit
# convention: port byte-identically, repoint paths, never fork the scoring.

# F260731-RADARRESTAMP — px_asof/px_stale_days travel WITH current_px. The block's
# price_as_of is a max across the whole universe, so on a partially-rolled pull it describes
# only the freshest ticker; a row quoting a price with no basis of its own inherits that lie.
# POLYCAB shipped as a FIRE at the 30-Jul close under a 31-Jul stamp while spot was 1.4%
# higher. A price and the session it came from are one fact and must not be separated.
RADAR_KEYS = ("ticker", "verdict", "entry", "stop", "target", "rr", "setup", "trend",
              "grade", "score", "current_px", "px_asof", "px_stale_days")


def radar_row(r: dict) -> dict:
    """One radar row: the LEVELS and the verdict WHY, nothing else.

    Deliberately drops key_support/key_resistance/points/zone/ma/ext — those carry the operator's
    banked ladder analysis and its prose notes, which live inside sensitive_enc (F260721-FIBLOCK).
    The Radar is the UNLOCKED hero, so what it ships is public by construction: a ticker, four
    numbers, and the sentence that justifies them (invariant #5 permits R/%/levels; invariant #6
    V-03 requires the why)."""
    out = {k: r.get(k) for k in RADAR_KEYS if r.get(k) is not None}
    out["why"] = list(r.get("why_verdict") or [])
    return out


def build_radar(bank=None) -> dict:
    """The actionable fib surface for the desk Radar: FIRE / WATCH / the whole board, with levels.

    Same gate the autotrade logger fires on — ONE definition, never re-implemented per surface.

    F260727 THE BOARD — every scored name, not just the ones that clear the buy gate. The emit used
    to ship `fires` + `watch` and drop `excluded` on the floor, keeping only its COUNT. So the desk
    knew AMBER sat at resistance in a downtrend on a 100-strength cluster whose supply price had not
    accepted above — the full read, reason included — and rendered the integer 35. "35 excluded" is
    not an answer to "what is everything doing"; it is that answer being binned at the publish
    boundary. Operator 2026-07-27: *"each stock that has a verified fib bank should be doing
    something in relation to its levels that the system can highlight."* It already was. It simply
    was not published.
    """
    sel = select_fib_confluence(bank if bank is not None else load_bank())
    return {
        "fires": [radar_row(r) for r in sel["fires"]],
        "watch": [radar_row(r) for r in sel["watch"]],
        "board": [radar_row(r) for r in (sel["fires"] + sel["watch"] + sel["excluded"])],
        "counts": sel["counts"],
        "price_as_of": sel.get("price_as_of"),
        "generated_at_utc": sel.get("generated_at_utc"),
        "basis": sel.get("basis"),
        "gate": sel.get("gate"),
        "note": "fires are gated on hand-verified fib banks only (A2). Levels are BANKED "
                "structure, scored on the last close — not intraday.",
    }


def _report(out: dict) -> str:
    """The human-readable run log (what fib_source_LIVE-RUN_*.txt captures)."""
    L = ["fib_confluence_source — bank as of %s (built %s)"
         % (out.get("price_as_of") or "unknown", out.get("generated_at_utc") or "unknown"),
         "gate: at-confluence + STRONG + verified support >=%d sources + setup in %s + trend in %s"
         % (MIN_SOURCES, "/".join(FIRE_SETUPS), "/".join(OK_TRENDS)),
         "      + no unconfirmed supply + R:R >= %.1f    stop = zone.lo - %.1f%%"
         % (RR_MIN, STOP_BUFFER * 100), ""]
    for f in out["fires"]:
        L.append("FIRE   %-11s entry %-10s stop %-10s target %-10s R:R %.2f"
                 % (f["ticker"], f["entry"], f["stop"], f["target"], f["rr"]))
        L.append("       %s" % f["why_verdict"][0])
    for w in out["watch"]:
        L.append("WATCH  %-11s %s" % (w["ticker"], w["why_verdict"][0]))
    for e in out["excluded"]:
        L.append("EXCL   %-11s %s" % (e["ticker"], e["why_verdict"][0]))
    c = out["counts"]
    L += ["", "%d FIRE · %d WATCH · %d EXCLUDE" % (c["fire"], c["watch"], c["exclude"])]
    return "\n".join(L)


if __name__ == "__main__":
    print(_report(select_fib_confluence(load_bank())))
