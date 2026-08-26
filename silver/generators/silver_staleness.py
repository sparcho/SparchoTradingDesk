#!/usr/bin/env python3
"""silver_staleness.py -- the SILVER desk's staleness + block-provenance contract.

SEPARATE BY DIRECTIVE (operator, 2026-08-26): "fully separate things, lets keep it
clean ... they need to talk to each other, not be the same desk or have the same
engines."

Until this split, `staleness_contract.py` carried BOTH desks -- one desk's detectors
beside the other's, both block registries in one dict namespace -- and
`system_doctor.check_block_contract` ran a single loop across both. That is one
engine serving two desks. Silver now owns its contract end to end, including its own
copies of the primitives, so a change on the other desk can never move silver's
behaviour and vice versa.

The desks INTERFACE through published data -- each aggregate carries its own
`staleness` block -- never through a shared import.

Deliberate duplication: the helpers below started as near-copies. That is the point.
If they drift, they drift because a desk needed them to.

Tests: 00_SYSTEM/TESTS/test_silver_block_contract.py
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

SCHEMA = "v1"
_IST = timezone(timedelta(hours=5, minutes=30))

# Age tolerances (minutes) for the whole-aggregate emit-recency signal.
EMIT_TOL_MARKET_MIN = 45      # during NSE market hours the crons run every 5 min
EMIT_TOL_OFFHOURS_MIN = 180   # off-hours crons run every 15-20 min; allow slack
# Silver price overlay tolerance (20-min cron + margin).
SILVER_PRICE_TOL_MIN = 90
# Operator-driven book staleness (they drop when they trade) — informational nudge only.
BOOK_STALE_SESSIONS = 2       # >= this many sessions behind -> surface (not dim)
SILVER_BOOK_TOL_DAYS = 10
# F145 analysis-input freshness: the operator-fed SILV-TA weekly chart read.
# Severity ladder per F260706-F145 (>30d warn, >60d alert). Informational (dim=False).
# (a retired constant from this module's shared-origin lived here; the other desk
#  owns its own now.)
SILV_TA_WARN_DAYS = 30
SILV_TA_ALERT_DAYS = 60


def _now_utc(now_utc_iso=None) -> datetime:
    if now_utc_iso:
        try:
            return datetime.fromisoformat(str(now_utc_iso).replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.fromisoformat(str(s)[:10])
        except ValueError:
            return None


def _age_min(iso, now):
    dt = _parse_iso(iso)
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (now - dt).total_seconds() / 60.0)


def _is_market_hours(now):
    ist = now.astimezone(_IST)
    if ist.weekday() >= 5:
        return False
    hm = ist.hour + ist.minute / 60.0
    return 9.25 <= hm <= 15.6


def _item(**kw):
    base = dict(
        id=None, subsystem=None, label=None, is_stale=False, severity="info",
        dim=False, reason="", since=None, age_min=None, sessions_stale=None,
        heal=None, ui_targets=[],
    )
    base.update(kw)
    return base



# ---------------------------------------------------------------- silver detectors
def _silver_items(data, now):
    items = []

    # 1. Live silver prices overlay recency.
    try:
        cm = data.get("current_market") or {}
        src = cm.get("fetched_at_utc") or (data.get("meta") or {}).get("last_price_overlay_utc") \
            or data.get("emitted_at_utc")
        age = _age_min(src, now)
        stale = age is not None and age > SILVER_PRICE_TOL_MIN
        items.append(_item(
            id="silver_prices", subsystem="silver", label="Silver live prices",
            is_stale=stale, severity="warn" if stale else "info", dim=stale,
            reason=(f"prices {int(age)}m old (tol {SILVER_PRICE_TOL_MIN}m)") if stale
                   else f"fresh — {int(age)}m ago" if age is not None else "unknown",
            age_min=age, heal="refresh_prices_silver", ui_targets=["silver-price-card"],
        ))
    except Exception:
        pass

    # 2. Silver book (holdings/deployment) local emit age — operator-driven nudge.
    try:
        m = data.get("meta") or {}
        src = m.get("book_local_emit_utc") or m.get("last_synced")
        age = _age_min(src, now)
        days = (age / 1440.0) if age is not None else None
        stale = days is not None and days > SILVER_BOOK_TOL_DAYS
        items.append(_item(
            id="silver_book", subsystem="silver", label="Silver book",
            is_stale=stale, severity="warn" if stale else "info", dim=False,
            reason=(f"book emit {days:.0f}d old (tol {SILVER_BOOK_TOL_DAYS}d)") if stale
                   else (f"current — emitted {days:.1f}d ago" if days is not None else "unknown"),
            since=src, age_min=age, heal="silver_book_reemit_nudge", ui_targets=[],
        ))
    except Exception:
        pass

    # 3. Whole-aggregate emit recency.
    try:
        age = _age_min(data.get("emitted_at_utc"), now)
        tol = SILVER_PRICE_TOL_MIN if _is_market_hours(now) else EMIT_TOL_OFFHOURS_MIN
        stale = age is not None and age > tol
        items.append(_item(
            id="silver_emit_recency", subsystem="silver", label="Silver data refresh",
            is_stale=stale, severity="alert" if stale else "info", dim=False,
            reason=(f"aggregate {int(age)}m old (tol {tol}m)") if stale
                   else f"refreshed {int(age)}m ago" if age is not None else "unknown",
            age_min=age, heal="refresh_prices_silver", ui_targets=[],
        ))
    except Exception:
        pass

    # 4. Analysis-input freshness (F145): stale operator SILV-TA chart read.
    try:
        an = data.get("analysis") or {}
        last = an.get("last_silv_ta")
        dd = _parse_iso(last)
        days = None
        if dd:
            probe = dd.date() if hasattr(dd, "date") else dd
            days = (now.astimezone(_IST).date() - probe).days
        alert = days is not None and days > SILV_TA_ALERT_DAYS
        warn = days is not None and days > SILV_TA_WARN_DAYS
        stale = bool(warn or alert)
        items.append(_item(
            id="silver_analysis_freshness", subsystem="silver", label="Silver TA read",
            is_stale=stale, severity=("alert" if alert else "warn") if stale else "info", dim=False,
            reason=(f"latest SILV-TA {days}d old (warn >{SILV_TA_WARN_DAYS}d, alert >{SILV_TA_ALERT_DAYS}d)") if stale
                   else (f"fresh - latest SILV-TA {days}d ago ({last})" if days is not None else "no SILV-TA date emitted"),
            since=last, age_min=(days * 1440 if days is not None else None),
            heal=None, ui_targets=["silver-ta-card"],
        ))
    except Exception:
        pass

    # 6. COT-fetch freshness (F145 addendum): distinguish a dead fetcher from a
    #    holiday-delayed-but-fresh CFTC print. Reads booleans the emit already computed.
    try:
        fetch_stale = bool(data.get("cot_fetch_stale"))
        delayed = bool(data.get("cot_delayed"))
        fage = data.get("cot_fetch_age_days")
        rage = data.get("cot_report_age_days")
        if data.get("cot_fetched_at") is not None or fetch_stale or delayed:
            items.append(_item(
                id="cot_fetch_freshness", subsystem="silver", label="COT fetch",
                is_stale=fetch_stale, severity="alert" if fetch_stale else "info", dim=False,
                reason=(f"fetcher stale {fage}d (>8d = pipeline problem)") if fetch_stale
                       else (f"latest CFTC print is holiday-delayed ({rage}d) but fetch is fresh" if delayed
                             else f"fresh - fetched {fage}d ago" if fage is not None else "fetched"),
                since=data.get("cot_fetched_at"), age_min=(fage * 1440 if isinstance(fage,(int,float)) else None),
                heal=None, ui_targets=["cot-card"],
            ))
    except Exception:
        pass

    return items


_SEV_RANK = {"ok": 0, "info": 1, "warn": 2, "alert": 3}

# ------------------------------------------------------- D1: BLOCK PROVENANCE CONTRACT
# F260721-BLOCKROT. An aggregate is ~27 INDEPENDENTLY-PRODUCED blocks living
# under ONE `emitted_at_utc`, and the cloud price overlay re-stamps that container every
# 5 minutes. So the envelope is *always* fresh while any individual block can be frozen
# for weeks. Not hypothetical: on 2026-07-21 the live desk served order_book last-dated
# 2026-06-04 (47d), analysis 2026-05-16 (66d), an empty next_session.rows and a
# fib_confluences block carrying NO DATE FIELD AT ALL — under a container stamped minutes
# earlier, with the doctor reporting 30 green.
_DATE_LEN = 10


def _looks_like_date(s):
    return (len(s) == _DATE_LEN and s[4] == "-" and s[7] == "-"
            and s[:4].isdigit() and s[5:7].isdigit() and s[8:].isdigit())


def _iter_dates(obj, budget=4000):
    """Yield every YYYY-MM-DD-looking string prefix in a nested structure (bounded)."""
    stack = [obj]
    seen = 0
    while stack and seen < budget:
        x = stack.pop()
        if isinstance(x, dict):
            stack.extend(x.values())
        elif isinstance(x, (list, tuple)):
            stack.extend(x[:400])
        elif isinstance(x, str):
            seen += 1
            s = x[:_DATE_LEN]
            if _looks_like_date(s):
                yield s


def _max_date(obj):
    """Newest date string anywhere inside a block, or None — the generic as-of fallback."""
    best = None
    for d in _iter_dates(obj):
        if best is None or d > best:
            best = d
    return best


def _key_date(*path):
    """as-of extractor reading an explicit key path — preferred over the deep date scan."""
    def _get(blk):
        cur = blk
        for k in path:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(k)
        if cur is None:
            return None
        s = str(cur)[:_DATE_LEN]
        return s if _looks_like_date(s) else None
    return _get


def _sessions_between(d_str, today):
    """Weekday sessions between an ISO date and today. Holidays are ignored here (the NSE
    calendar lives outside this stdlib-only module); the doctor refines the count."""
    from datetime import date as _date
    try:
        y, m, d = (int(x) for x in str(d_str)[:10].split("-"))
        cur = _date(y, m, d)
    except Exception:
        return None
    if cur >= today:
        return 0
    n = 0
    while cur < today:
        cur = cur + timedelta(days=1)
        if cur.weekday() < 5:
            n += 1
    return n


def _blk(owner, substrate, max_sessions, asof=None, allow_empty=False,
         severity="alert", label=None, note="", count=None, public_as=None):
    """One block's declared contract.

    owner      — the producer responsible for the block
    substrate  — 'cloud' (GitHub Actions, survives laptop-off) | 'laptop' (Task Scheduler,
                 dies when the operator is away) | 'operator' | 'struct' (no freshness meaning)
    max_sessions — how many NSE sessions behind TODAY the block may fall (None = exempt)
    asof       — callable(block) -> 'YYYY-MM-DD'; falls back to the deep date scan
    allow_empty— emptiness is a legitimate state for this block
    """
    return dict(owner=owner, substrate=substrate, max_sessions=max_sessions,
                asof=asof, allow_empty=allow_empty, severity=severity,
                label=label, note=note, count=count, public_as=public_as)


# substrate legend:
#   cloud    — GitHub Actions; survives the laptop being off
#   laptop   — Windows Task Scheduler / daily_driver; DIES when the operator is away.
#              These are the surfaces that rot silently while prices keep ticking.
#   operator — only moves when the operator drops data
#   struct   — structural/metadata; EXEMPT BY DECLARATION, never by omission
# ---------------------------------------------------------------------------
# SILVER_BLOCKS -- the silver desk's block provenance contract (added 2026-08-25)
#
# WHY IT EXISTS. The other desk had a block contract since July; silver had none, and
# silver is the desk that rotted. `sr_levels` sat 65 days stale INSIDE a file
# written the previous day, so every file-level freshness check passed it while
# the card told the operator that a gate which had fired six times had not fired.
# An unregistered block has no owner, no substrate and no tolerance, so nothing
# can age it -- omission is never an exemption.
#
# Substrate matters more here than for a scheduled desk: almost every silver block is
# OPERATOR-authored YAML rather than a scheduled producer, which is precisely why
# they rot silently. Declaring `operator` substrate makes "nobody has touched this"
# a visible state instead of an invisible one.
SILVER_BLOCKS = {
    # --- structural: exempt BY DECLARATION, never by omission
    "schema_version":   _blk("silver_dashboard_emit.py", "struct", None, severity="info"),
    "doc_type":         _blk("silver_dashboard_emit.py", "struct", None, severity="info"),
    "emitted_at_utc":   _blk("silver_dashboard_emit.py", "struct", None, severity="info"),
    "meta":             _blk("silver_dashboard_emit.py", "struct", None, severity="info"),
    "privacy":          _blk("silver_dashboard_emit.py", "struct", None, severity="info"),
    "sensitive_enc":    _blk("silver_dashboard_emit.py (AES-GCM)", "struct", None, severity="info"),
    "staleness":        _blk("staleness_contract.py", "struct", None, severity="info"),
    "warnings":         _blk("silver_dashboard_emit.py", "struct", None,
                             allow_empty=True, severity="info"),
    "snapshot_tickers": _blk("silver_holdings.yaml", "struct", None, severity="info"),
    "tradingview_main_chart": _blk("silver_holdings.yaml", "struct", None, severity="info"),

    # --- live market layer: fetched on every emit, and it never fails.
    # That is the trap V-37 named: this layer proves freshness while the
    # substance layer below is dead. Do not read a green here as a green desk.
    "current_price":    _blk("silver_dashboard_emit.py (live fetch)", "cloud", 1),
    "current_market":   _blk("silver_dashboard_emit.py (live fetch)", "cloud", 1,
                             _key_date("fetched_at_utc")),
    "live_xagusd_used_for_ladders": _blk("silver_dashboard_emit.py (live fetch)", "cloud", 1,
                                         severity="info"),

    # --- computed from the price series on every emit (added 2026-08-25)
    "thesis_gates":     _blk("silver_dashboard_emit.py::_thesis_gates", "cloud", 1,
                             _key_date("computed_at"),
                             note="the gate STATE, computed from completed weekly XAGUSD "
                                  "closes. Replaces the typed sr_levels.thesis prose, which "
                                  "went 65 days wrong on two of four gates."),

    # --- OPERATOR-authored YAML. These are the blocks that rot, because nothing
    #     schedules them. Tolerances are generous but finite: a level map is
    #     structural and moves slowly, a thesis is not.
    "sr_levels":        _blk("silver_holdings.yaml (operator)", "operator", 30,
                             _key_date("last_updated"), severity="alert",
                             note="THE 260825 FINDING. Read 2026-06-21 and still live on "
                                  "2026-08-25. Its `thesis` string is prose and is NOT the "
                                  "gate state -- thesis_gates is. The verified level bank is "
                                  "EDGE/LEVELS/XAGUSD.json; this block should be derived "
                                  "from it rather than typed."),
    "forecast":         _blk("silver_holdings.yaml (operator)", "operator", 45,
                             _key_date("last_chart_read_date"), severity="warn"),
    "probability":      _blk("silver_dashboard_emit.py <- forecast", "operator", 45,
                             _key_date("as_of"), severity="warn"),
    "bull_bear":        _blk("silver_holdings.yaml (operator)", "operator", 60, severity="warn"),
    "floor_framework":  _blk("silver_holdings.yaml (operator)", "operator", 90, severity="warn",
                             note="V-13 tier structure -- structural, moves slowly"),
    "silver_strategy":  _blk("silver_strategy.yaml (operator)", "operator", 45, severity="warn"),
    "strategy_timeline_public": _blk("silver_holdings.yaml (operator)", "operator", 90,
                                     severity="warn"),
    "analysis":         _blk("SILV-TA pack", "operator", 45, _key_date("last_silv_ta"),
                             severity="warn"),
    "catalysts":        _blk("silver_holdings.yaml (operator)", "operator", 30,
                             allow_empty=True, severity="warn"),
    "news":             _blk("silver_holdings.yaml (operator)", "operator", 14,
                             allow_empty=True, severity="warn"),

    # --- externally-sourced data with real publication cadences
    "cot":              _blk("COT fetch (CFTC, weekly Fri release)", "laptop", 8,
                             _key_date("survey_date"), severity="warn",
                             note="dated by the SURVEY date, never the fetch -- the fetch "
                                  "succeeds every day while the survey moves weekly, so a "
                                  "fetch stamp would let a month-old survey pass forever "
                                  "(a-date-is-not-an-age)"),
    "global_inventory": _blk("inventory fetch (LBMA/COMEX/SHFE)", "laptop", 20,
                             _key_date("last_updated"), severity="warn"),
    "paper_physical":   _blk("paper-physical composite", "laptop", 20, severity="warn"),
}



# SHA-256 prefixes of the private first names. HASHED, never spelled out: this file has three
# copies in the PUBLIC repo, so a plaintext list here would be the leak it exists to prevent -- and
# a plaintext list is also silently rewritable, which is how the 2026-08-24 history scrub disarmed
# the silver leak gate. It cannot import the vault's privacy_scrub for the same reason: this file
# also runs in the cloud, where that module does not exist.
_PRIVATE_NAME_HASHES = frozenset({
    "af7a74864494b189", "8394e6f426a8d1cc", "eec47d9891bc884a",
    "fb3193a85f57ec2d", "262cc47030b18030",
})


def _safe_public_name(name):
    """A public label for an UNREGISTERED block key.

    Registered private blocks declare `public_as`. An unregistered one declares nothing, and the
    fallback used to be the RAW KEY -- so RETIRING a private block turned its freshness row back
    into a leak, which is the exact opposite of what retiring it was for. Split on runs of letters
    so `family_account_b` yields its parts; if any part is a private name, report the block under a
    neutral label. It is still REPORTED -- silence is the other failure mode.
    """
    for word in re.findall(r"[A-Za-z]+", str(name or "")):
        if hashlib.sha256(word.lower().encode()).hexdigest()[:16] in _PRIVATE_NAME_HASHES:
            return "private-block"
    return name


def _block_items(data, now, registry, desk):
    """Generic per-block freshness, judged against TODAY — the D1 detector.

    Two rules keep this self-extending rather than scar-shaped:
      * a block in the aggregate but MISSING from the registry is itself a finding, so
        adding a block to the emit forces a contract entry;
      * a block whose as-of cannot be derived is a finding, because an undated block can
        never be proven stale — unfalsifiable is treated as broken.
    """
    items = []
    for name in sorted((data or {}).keys()):
        spec = registry.get(name)
        # F260721-CONTRACTLEAK: this runs BEFORE _apply_privacy strips the operator-private
        # blocks, so a raw key name here lands on a PUBLIC surface. `family_account_b` did exactly
        # that - the DATA was stripped, and the health metadata describing it put the family
        # name back. It also evaded privacy_scrub, whose halt-on-survivor check is word-boundary
        # based and cannot see a name embedded in an identifier. Private blocks must still be
        # REPORTED (silence is the other failure mode) - just never by their own key.
        pub = (spec or {}).get("public_as") or _safe_public_name(name)
        if spec is None:
            items.append(_item(
                id="block:" + pub, subsystem=desk, label="Block %s" % pub,
                is_stale=True, severity="warn", dim=False,
                reason=("UNREGISTERED BLOCK — present in the aggregate but absent from the "
                        "block contract, so its staleness can never be proven. Add it to "
                        "SILVER_BLOCKS (F260721-BLOCKROT)."),
            ))
            continue
        if spec["substrate"] == "struct":
            continue

        blk = data.get(name)
        label = spec.get("label") or ("Block %s" % pub)
        # A detector that explodes becomes a FINDING. The old contract wrapped every
        # detector in `try/except: pass`, so a renamed field silently dropped the item
        # and the doctor reported "clean - fewer items, none stale" (F260721).
        try:
            # `count` lets a block declare WHERE its real payload lives. next_session is a
            # 4-key dict that never looks empty even when rows:[] blanks the whole card.
            if spec.get("count"):
                n = spec["count"](blk)
            else:
                n = len(blk) if isinstance(blk, (list, dict, str)) else None
            if blk is None or (n == 0 and not spec["allow_empty"]):
                items.append(_item(
                    id="block:" + pub, subsystem=desk, label=label,
                    is_stale=True, severity=spec["severity"], dim=False,
                    reason=("EMPTY — %s produces this on the %s substrate and it came out "
                            "empty. %s" % (spec["owner"], spec["substrate"], spec["note"])).strip(),
                ))
                continue

            asof = spec["asof"](blk) if spec["asof"] else _max_date(blk)
            if asof is None:
                if spec["max_sessions"] is None:
                    continue
                # 260723 triage: an EMPTY allow_empty block is the complete statement
                # "nothing here" — it has no content to date, and escalating every quiet
                # day to a finding is the cry-wolf failure (invariant #7). A NON-empty
                # undatable block remains a finding (invariant #4 — unfalsifiable is broken).
                if spec["allow_empty"] and n == 0:
                    items.append(_item(
                        id="block:" + pub, subsystem=desk, label=label,
                        is_stale=False, severity="info", dim=False,
                        reason="empty by declaration (allow_empty) — nothing to date; "
                               "producer %s liveness is judged by its dated sibling blocks"
                               % spec["owner"],
                    ))
                    continue
                items.append(_item(
                    id="block:" + pub, subsystem=desk, label=label,
                    is_stale=True, severity=spec["severity"], dim=False,
                    reason=("NO PROVENANCE — no as-of date can be derived, so this block can "
                            "never be proven fresh OR stale. Producer %s must stamp it. %s"
                            % (spec["owner"], spec["note"])).strip(),
                ))
                continue

            # Age against TODAY — never against another copy (the F260721 relative-lag trap:
            # two equally-stale copies agree, so copy-vs-copy reads "fresh" on a dead pipeline).
            sess = _sessions_between(asof, now.astimezone(_IST).date())
            # max_sessions None = age-exempt by declaration (e.g. forward-dated catalysts):
            # report the derived as-of, never judge it.
            stale = (spec["max_sessions"] is not None
                     and sess is not None and sess > spec["max_sessions"])
            items.append(_item(
                id="block:" + pub, subsystem=desk, label=label,
                is_stale=stale, severity=spec["severity"] if stale else "info", dim=False,
                since=asof, sessions_stale=sess,
                reason=(("STALE — as-of %s is %d session(s) behind today (tolerance %d); "
                         "produced by %s on the %s substrate."
                         % (asof, sess, spec["max_sessions"], spec["owner"], spec["substrate"]))
                        if stale else
                        "fresh — as-of %s (%s session(s) behind)" % (asof, sess)),
            ))
        except Exception as e:
            items.append(_item(
                id="block:" + pub, subsystem=desk, label=label,
                is_stale=True, severity="alert", dim=False,
                reason="DETECTOR ERROR on this block: %s: %s" % (type(e).__name__, e),
            ))
    return items


def build_staleness(data, now_utc_iso=None):
    """Build the SILVER desk's staleness block. Silver only -- there is no `desk` arg.

    A function that dispatches on which desk you are IS the shared engine this split
    exists to remove. Silver's detectors plus silver's block contract, nothing else.
    Safe on partial/empty data -- each detector self-guards.
    """
    now = _now_utc(now_utc_iso)
    # bespoke detectors (the scar-tissue layer, kept for their UI dim targets + heal keys)
    items = _silver_items(data, now)
    # + the generic per-block contract: judges EVERY declared block against TODAY, so a
    # frozen block can no longer hide under a fresh envelope. Silver needed this most --
    # its sr_levels sat 65 days stale inside a file written the previous day.
    items = items + _block_items(data, now, SILVER_BLOCKS, "silver")

    stale_items = [i for i in items if i.get("is_stale")]
    worst = "ok"
    for i in stale_items:
        if _SEV_RANK.get(i.get("severity", "info"), 1) > _SEV_RANK.get(worst, 0):
            worst = i.get("severity")
    return {
        "schema": SCHEMA,
        "computed_at_utc": now.isoformat(timespec="seconds"),
        "desk": "silver",
        "any_stale": bool(stale_items),
        "any_dim": any(i.get("is_stale") and i.get("dim") for i in items),
        "worst": worst,
        "items": items,
    }


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) >= 2:
        payload = json.load(open(sys.argv[1], encoding="utf-8"))
        print(json.dumps(build_staleness(payload), indent=2))
    else:
        print("usage: silver_staleness.py <silver_aggregate.json>")
