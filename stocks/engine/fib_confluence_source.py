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

# NOISE FLOOR on the stop (measured 2026-08-18, [[WHAT-WE-LEARNED_plain-english]]).
# On 22,947 real pullback entries a tighter stop returned less at EVERY step:
# ~1.5% kept +0.37%, 3% kept +0.60%, 2x ATR kept +0.86%, 3x ATR +1.05%, no stop +1.75%.
# The structural stop is still right in principle -- but on the live bank 34% of stops
# sat closer than ONE average day's range, so they were being hit by ordinary weather
# rather than by the thesis breaking. Keep the structural level; refuse to place it
# nearer than this many average daily ranges from entry.
ATR_FLOOR = 2.0

# How far price may sit from the level a FIRE names before the fire stops being true.
# Matches the bank's own AT tolerance (fib_confluence_feed.AT_TOL = 0.02, itself
# box_engine's BOUNDARY_TOL), so "AT the level" means the same thing on both sides.
#
# F260823-FIREGAP. The gate asked `at_confluence` -- is price at SOME confluence -- and then
# took `entry` from `key_support`, the STRONGEST support zone anywhere below. Nothing tied
# the two together, so a name could be at a minor confluence while its published entry sat
# far under spot: on the 21-Aug bank STLTECH fired at 627.15 telling you to buy at 315.07,
# and ASTRAMICRO at 1,662.20 telling you to buy at 1,145.75, both under "price has come back
# to this level". The ratio inherits the error too -- R:R off an unreachable entry read
# 9.76x on STLTECH. Such a name is structurally sound and simply not there yet, which is
# the definition of WATCH, so it is demoted rather than dropped.
AT_TOL_PCT = 2.0

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
    # Push the stop out if the structural level lands inside the name's own daily
    # noise. Only ever widens -- a structural stop already outside the floor is left
    # exactly where the bank put it.
    atr_pct = _num(name.get("atr_pct"))
    if stop is not None and entry is not None and atr_pct and atr_pct > 0:
        floor_px = entry * (1 - ATR_FLOOR * atr_pct)
        if floor_px < stop:
            stop = round(floor_px, 2)
    target = _num(kr.get("px"))
    rr = None
    if entry is not None and stop is not None and target is not None and entry > stop:
        rr = (target - entry) / (entry - stop)

    setup = str(name.get("setup") or "")
    trend = str(name.get("trend") or "")
    flags = [str(f) for f in (name.get("flags") or [])]
    why: list = []

    # Is the entry a price you could actually transact at today? Computed HERE, above the first
    # return, because all three exits publish `rr` and all three were publishing it off an
    # unreachable level. The first version of this fix guarded only the WATCH exit and looked
    # correct locally -- the live desk then moved STLTECH to EXCLUDE and it published rr 8.02
    # again from the exit nobody had patched ([[retire-a-signal-sweep-every-surface]]: one exit
    # is not the surface, the function is).
    _cur = _num(name.get("current_px"))
    _gap_raw = (abs(_cur - entry) / entry * 100) if (_cur is not None and entry) else None
    # Decide on the RAW gap and round only for DISPLAY. Rounding first put TEJASNET at exactly
    # 2.0 against a 2.0 tolerance, so a 2.004% gap compared as not-greater-than and the row kept
    # its ratio -- a boundary the rounding invented rather than one the data has.
    entry_gap_pct = None if _gap_raw is None else round(_gap_raw, 1)
    unreachable = _gap_raw is not None and _gap_raw > AT_TOL_PCT

    def _rr_fields():
        """R:R is a property of a trade you can take. Withheld -- not deleted -- when you cannot."""
        return {"rr": None if unreachable else rr,
                "rr_if_reached": rr if unreachable else None,
                "entry_gap_pct": entry_gap_pct if unreachable else None}

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
        why.append("key support is not a hand-verified LevelID-Fib read (A2: verified banks only)")
    if (ks.get("sources") or 0) < MIN_SOURCES:
        why.append("key support has %s source(s), under the %d-source confluence floor"
                   % (ks.get("sources") or 0, MIN_SOURCES))
    if stop is None:
        why.append("key support has no banked zone — no invalidation level to stop against")

    if why:
        return {"ticker": name.get("ticker"), "verdict": "EXCLUDE", "entry": entry, "stop": stop,
                "target": target, "why_verdict": why, **_rr_fields()}

    # ── ACTIONABILITY. Everything below reaches here already structurally sound, so a failure is
    #    a DEMOTION to WATCH — the name stays on the radar, it just is not a fire today.
    if setup in WATCH_SETUPS:
        why.append("price is not at the level yet (setup=%s) — watch for the tag" % setup
                   if setup == "approaching" else
                   "price is INSIDE the band (setup=in-zone) — neither support nor resistance yet")
    # F260823-FIREGAP — is price AT the level this row actually tells you to buy?
    # `at_confluence` above only says price is at SOME confluence on the chart. The entry
    # comes from key_support, which may be a different, much lower zone; nothing tied them
    # together, so a fire could publish an entry 99% below spot under "price has come back
    # to this level". Sound structure, price simply not there -> WATCH, which is what WATCH
    # is for. Never a silent exclusion: the level set is still shown, correctly labelled.
    cur, gap = _cur, entry_gap_pct
    if unreachable:
        if True:
            why.append("price %.2f is %.1f%% from the %s it would buy — the confluence price "
                       "is AT is not this level, so it is not a fire yet"
                       % (cur, gap, entry))
            # F260824 — the 23-Aug repair fixed the VERDICT and left the NUMBERS. The row stayed
            # on the desk still publishing an R:R computed off a price you cannot transact at,
            # and the card rendered it ("9.8x reward" on STLTECH at a 99.1% gap). Worse, the
            # buckets ORDER by R:R, and across the live bank the ratio is very nearly rank-
            # ordered by the gap itself (99.1%->9.76, 45.1%->6.94, 32.5%->5.40, 16.4%->5.07) —
            # so it was measuring distance-from-price and floating the LEAST actionable rows to
            # the top. The levels stay (real banked structure, the reason to watch the name);
            # the RATIO is withheld and its value preserved under a name that states the
            # condition ([[jointly-incoherent-fields]]).
            why.append("reward:risk withheld — %.2f only applies IF price returns to %s, and it "
                       "is %.1f%% away; a ratio off an unreachable price is not a trade that exists"
                       % (rr, entry, gap) if rr is not None else
                       "reward:risk withheld — the entry is %.1f%% away" % gap)
    if target is None:
        why.append("no banked level overhead to target — nothing to size a reward against")
    elif rr is not None and rr < RR_MIN:
        why.append("R:R %.2f to %.2f is under the %.1f floor — not enough room to the next level"
                   % (rr, target, RR_MIN))

    if why:
        return {"ticker": name.get("ticker"), "verdict": "WATCH", "entry": entry, "stop": stop,
                "target": target, "why_verdict": why, **_rr_fields()}

    # V-03 — a FIRE states its WHY too. This is the line the desk renders under the level set.
    why.append("AT a %d-source hand-verified support at %s in a %s trend; %.2f R:R to the next "
               "banked level %s, invalidation %s (%.1f%% under the zone floor)"
               % (ks.get("sources") or 0, entry, trend, rr, target, stop, STOP_BUFFER * 100))
    verdict = "FIRE"

    return {"ticker": name.get("ticker"), "verdict": verdict, "entry": entry, "stop": stop,
            "target": target, "rr": rr, "why_verdict": why}


def stop_atr_days(entry, stop, atr_pct):
    """How many of this name's own normal days sit between entry and the stop.

    The UNIT matters more than the number. A stop quoted as "3.4% away" says nothing about
    whether that is room or noise -- 3.4% is a fortnight on one name and half a session on
    another, which is exactly how a fixed-% barrier ends up measuring volatility instead of
    skill ([[fixed-percent-barriers-measure-volatility]]). In the name's own daily range it
    is comparable across the book, and it is the form the evidence is stated in: on 22,947
    entries a tighter stop returned LESS at every step (~1.5% kept +0.37%, 2xATR +0.86%,
    3xATR +1.05%, none +1.75%), and 34% of live bank stops sat inside a single average day.

    None when any input is missing or the levels are incoherent -- an unmeasurable stop must
    read as unmeasured, never as zero room.
    """
    e, s, a = _num(entry), _num(stop), _num(atr_pct)
    if e is None or s is None or not a or a <= 0 or e <= 0 or s >= e:
        return None
    return round((e - s) / (e * a), 2)


def order_key(row):
    """The order the desk shows gate-qualified names in -- and it is NOT a ranking.

    It used to be `score` descending. That composite is built from ladder count, MA hits
    and timeframe stacking, and all three measured NO_EDGE against a displaced-ladder
    control on 2026-08-18. Ordering fires by it meant the desk led with a number it had
    already proven carries nothing (21-Aug: LT at R:R 2.27 top, STLTECH at 9.76 last).

    Nothing replaces it as a CONVICTION ranking, because nothing has earned that: 13
    separate ways of choosing WHICH name were tested and all 13 failed. So the order is
    deterministic and makes no forecast -- R:R, which is arithmetic off the banked entry,
    stop and target rather than a prediction, then ticker so it never wobbles run to run.
    Whoever renders this must say so rather than let position imply conviction.
    """
    rr = _num((row or {}).get("rr"))
    # A WITHHELD ratio sorts LAST, not first. `rr` is None both for "no ratio computable" and
    # for "computed off a price you cannot reach", and in either case the row has not earned a
    # position above one that states a real number.
    return (-(rr if rr is not None else -1), str((row or {}).get("ticker") or ""))


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
        # ── the evidence-backed fields the card is allowed to LEAD with ──────────────
        # Everything else on a fib card (ladder count, MA hits, timeframe stacking, the
        # grade, the score) was measured against a displaced-ladder control and carries
        # nothing. These two were measured and do:
        #   stop_atr_days  -- the stop expressed in this name's own normal days. The one
        #                     quantity here measured against MONEY (n=22,947).
        #   entry_timing   -- the only two entry conditions that beat a fair control.
        # None is permitted; a MISSING KEY is not, because a renderer cannot then tell
        # "not measured" from "not there" ([[absence-of-evidence-is-not-health]]).
        r["atr_pct"] = _num(name.get("atr_pct"))
        r["entry_timing"] = name.get("entry_timing") or {}
        r["stop_atr_days"] = stop_atr_days(r.get("entry"), r.get("stop"), r["atr_pct"])
        buckets[r["verdict"]].append(r)

    for b in buckets.values():
        b.sort(key=order_key)

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
              "grade", "score", "current_px", "px_asof", "px_stale_days",
              # The two evidence-backed fields the card leads with (roadmap step 4). Public
              # by construction: arithmetic on public price history, carrying none of the
              # banked ladder prose this allowlist exists to withhold. They are listed HERE
              # rather than computed in the shell so the cloud Action and this emit ship the
              # same block -- an allowlist that silently drops a new field renders the card
              # as though it was never computed ([[moving-a-field-breaks-its-readers]]).
              "stop_atr_days", "entry_timing")


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
