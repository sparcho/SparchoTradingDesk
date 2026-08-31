#!/usr/bin/env python3
"""staleness_contract.py — the SINGLE source of truth for "what is stale" on a
dashboard aggregate.

WHY THIS EXISTS (F260702 self-healing upgrade). Before this module, staleness was
defined in two places that silently drifted: the CLIENT grayed the day-trade card
off `daytrade_freshness.status`, while the WATCHDOG/DOCTOR judged health off
`emitted_at_utc` age. That let the UI render GRAYED while the doctor reported GREEN
(the 260701 overnight grayed-fires incident). This module makes staleness ONE
machine-readable contract that:
  1. the emit/overlay writes into the aggregate as `data["staleness"]`,
  2. the CLIENT renders opacity + banners from (nothing grays without a contract row),
  3. the WATCHDOG reads to auto-probe + heal each stale item (the closed loop),
  4. the DOCTOR reads (from the LIVE published aggregate) to derive health.
Because all four read the SAME contract, "gray UI + green doctor" becomes impossible.

DESIGN: pure + deterministic given (data, now). Stdlib only. No I/O, no network.
Every detector is wrapped so a missing/renamed field degrades to "not emitted",
never crashes an emit. Kept byte-identical across every copy (repo stocks/generators,
repo silver/generators, vault 00_SYSTEM/GENERATORS) — edit once, copy everywhere.

Item schema:
  { id, subsystem, label, is_stale, severity(info|warn|alert), dim(bool),
    reason, since, age_min, sessions_stale, heal(registry key|None), ui_targets[] }

`heal` keys are consumed by the watchdog's HEAL_ACTIONS registry (status/watchdog_ci.py).
`ui_targets` are DOM element ids the client dims when is_stale AND dim.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone, timedelta

SCHEMA = "v1"

# NOTE (2026-08-26): the SILVER desk was carved out of this module into
# `silver_staleness.py`, which owns its detectors and its own block registry.
# This file must stay single-desk. The two desks interface through published
# data, never a shared import -- operator directive: they do not share engines.
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
# STOCK_TA_WARN_DAYS retired 2026-07-27 with the equity `analysis` block (F260723-ANALYSIS-RETIRE).
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


# ---------------------------------------------------------------- equity detectors
def _equity_items(data, now):
    items = []

    # 1. Day-trade fires — the STAR item. Read daytrade_freshness.status, never recompute.
    try:
        df = data.get("daytrade_freshness") or {}
        status = df.get("status")
        ss = df.get("sessions_stale")
        stale = bool(status and status != "OK")
        items.append(_item(
            id="daytrade_fires", subsystem="equity", label="Day-trade fires",
            is_stale=stale, severity="warn" if stale else "info", dim=stale,
            reason=(f"prices as of {df.get('price_as_of')} · {ss} session(s) behind "
                    f"expected {df.get('expected_session')} ({status})") if stale
                   else f"fresh — prices as of {df.get('price_as_of')} ({status})",
            since=df.get("price_as_of"), sessions_stale=ss,
            heal="refresh_prices_equity", ui_targets=["card-fires"],
        ))
    except Exception:
        pass

    # 2. Whole-aggregate emit recency — is the pipeline refreshing at all?
    try:
        age = _age_min(data.get("emitted_at_utc"), now)
        tol = EMIT_TOL_MARKET_MIN if _is_market_hours(now) else EMIT_TOL_OFFHOURS_MIN
        stale = age is not None and age > tol
        items.append(_item(
            id="equity_emit_recency", subsystem="equity", label="Equity data refresh",
            is_stale=stale, severity="alert" if stale else "info", dim=False,
            reason=(f"aggregate {int(age)}m old (tol {tol}m)") if stale
                   else f"refreshed {int(age)}m ago" if age is not None else "unknown",
            age_min=age, heal="refresh_prices_equity", ui_targets=[],
        ))
    except Exception:
        pass

    # 3. Regime freshness — stale regime silently misprices every gate.
    try:
        rg = data.get("regime") or {}
        lu = rg.get("last_updated")
        d = _parse_iso(lu)
        today_ist = now.astimezone(_IST).date()
        sess_behind = 0
        if d:
            probe = d.date() if hasattr(d, "date") else d
            cur = probe
            while cur < today_ist:
                cur = cur + timedelta(days=1)
                if cur.weekday() < 5:
                    sess_behind += 1
        stale = sess_behind > 1
        items.append(_item(
            id="regime", subsystem="equity", label="Regime read",
            is_stale=stale, severity="warn" if stale else "info", dim=stale,
            reason=(f"regime card {sess_behind} session(s) old (last {lu})") if stale
                   else f"fresh — {rg.get('zone')} (score {rg.get('score')}, {lu})",
            since=lu, sessions_stale=sess_behind,
            heal="regime_refresh", ui_targets=["regime-card"],
        ))
    except Exception:
        pass

    # 4. Held book — operator-driven (they drop when they trade); surface, don't dim.
    try:
        bk = data.get("book") or {}
        ss = bk.get("sessions_stale")
        stale = isinstance(ss, int) and ss >= BOOK_STALE_SESSIONS
        items.append(_item(
            id="held_book", subsystem="equity", label="Held book snapshot",
            is_stale=stale, severity="info", dim=False,
            reason=(f"broker drop {ss} session(s) old (as of {bk.get('snapshot_date')})") if stale
                   else f"current — as of {bk.get('snapshot_date')}",
            since=bk.get("snapshot_date"), sessions_stale=ss,
            heal=None, ui_targets=[],
        ))
    except Exception:
        pass

    # 5. RETIRED 2026-07-27 (F260723-ANALYSIS-RETIRE, operator decision) — `equity_analysis_freshness`.
    #    It measured the age of STOCK-TA chart reads, a PDF-era cycle retired on 2026-07-06, and so
    #    could only ever age. It sat permanently warn ("13 STOCK-TA read(s) >60d old") on a desk
    #    where nothing consumed the block it read from — a stale row that can never go green trains
    #    the eye to ignore the banner, which is the same failure as a permanently red test suite.
    #    Retired together with its producer in the emit and the `analysis` EQUITY_BLOCKS entry;
    #    "how fresh is the operator's analysis" is now carried by fib_coverage + the fib bank's
    #    studied dates. The silver detector below is UNAFFECTED — that surface is live.

    return items


_SEV_RANK = {"ok": 0, "info": 1, "warn": 2, "alert": 3}


# ------------------------------------------------------- D1: BLOCK PROVENANCE CONTRACT
# F260721-BLOCKROT. The equity aggregate is ~27 INDEPENDENTLY-PRODUCED blocks living
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
EQUITY_BLOCKS = {
    "schema_version":        _blk("emit", "struct", None, severity="info"),
    "doc_type":              _blk("emit", "struct", None, severity="info"),
    "meta":                  _blk("emit", "struct", None, severity="info"),
    "privacy":               _blk("emit", "struct", None, severity="info"),
    "staleness":             _blk("staleness_contract.py", "struct", None, severity="info"),
    "sensitive_enc":         _blk("emit (AES-GCM)", "struct", None, severity="info"),
    "emitted_at_utc":        _blk("emit", "struct", None, severity="info"),

    # --- cloud-produced: current every session
    "held":                  _blk("refresh_prices.py", "cloud", 1),
    "regime":                _blk("regime_auto_refresh.py", "cloud", 1,
                                  _key_date("last_updated")),
    "daytrade_freshness":    _blk("refresh_prices.py", "cloud", 1,
                                  _key_date("price_as_of")),
    "news":                  _blk("refresh-news.yml", "cloud", 2),
    # "next_session" RETIRED 2026-07-26 with the day desk — no producer, no surface, so no
    # contract entry. Re-add here first if it is ever revived, or it lands as an
    # UNREGISTERED BLOCK finding.
    "screeners":             _blk("screener_runner.py", "laptop", 1),
    "daytrade_inputs":       _blk("screener_runner.py", "laptop", 1,
                                  note="carries no as-of field — needs a producer stamp"),
    "signal_perf":           _blk("signal_ledger.py", "laptop", 1),
    # A STUDY, not a feed: it does not go stale a session at a time, it sharpens as the
    # out-of-sample window grows. Tolerance is deliberately loose (14 sessions) because a
    # daily re-run would add ~1 session of new evidence to a ~32-session window and change
    # nothing -- a check that fires daily on a study nobody needs to re-run is a check that
    # teaches you to ignore the row it sits in.
    "fib_level_quality":     _blk("fib_level_quality.py", "laptop", 14,
                                  _key_date("generated_at"),
                                  note="Measures banked levels against price, real vs a "
                                       "displaced AND a scrambled ladder. Replaced the "
                                       "trade-outcome scorecard, which was structurally "
                                       "n=0 and rendered dashes for months."),
    "fib_confluences":       _blk("ci_fib_refresh.py (cloud) / fib_confluence_feed.py", "cloud", 1,
                                  _key_date("price_as_of"),
                                  note="F260721-FIBPROV: stamps price_as_of/basis. Scored on the "
                                       "LAST CLOSE, never intraday - the card must say so. "
                                       "F260721-LEVELSCLOUD: substrate is now CLOUD - the banked "
                                       "levels ship encrypted and refresh-stocks-dashboard.yml "
                                       "rebuilds this block post-close, so laptop-off is NO LONGER "
                                       "an excuse for it being stale."),
    "fib_radar":             _blk("fib_confluence_source.py via emit", "cloud", 1,
                                  _key_date("price_as_of"),
                                  count=lambda b: len((b or {}).get("fires") or []),
                                  allow_empty=True, severity="warn",
                                  note="F260725: the desk's actionable fire surface. Derived from "
                                       "fib_confluences, so it inherits that block's CLOUD "
                                       "substrate and its last-close basis. fires=[] is LEGAL - a "
                                       "day where no verified bank sits at a support with 1.5 R:R "
                                       "genuinely fires nothing - but it must still be DATED, or "
                                       "'no fires' is indistinguishable from 'radar is dead'."),
    "decisions":             _blk("decision_ledger.py", "operator", None, severity="info",
                                  count=lambda b: len((b or {}).get("open") or []),
                                  allow_empty=True,
                                  note="F260727 routing layer: open decisions with owners and "
                                       "deadlines, derived from FLAGS frontmatter. allow_empty is "
                                       "correct - zero open decisions is a real and good state. "
                                       "It has no freshness SLA because it moves only when a flag "
                                       "does; what matters is that OVERDUE items reach the desk."),
    # RETIRED 2026-07-30 (F260730-DTREADERS, operator decision) - `live_daytrade`.
    #    The day-trade CLASS was retired 25-Jul on measured evidence (negative expectancy; its
    #    highest-scored tier performed WORST), and horizon buckets were rejected outright 30-Jul
    #    ("holding period is an outcome of a setup's structure, not a category"). This block was
    #    the public 5-minute panel feeding a card that already showed a tombstone - while three
    #    surfaces still READ it, including the Radar's own hero ranking. Producers removed from
    #    the emit and from cloud refresh_prices; the price-freshness stamp it shared
    #    (`daytrade_freshness`) survives on its own and is now derived from the price pull
    #    directly. It was also mis-declared - the as-of lambda sat in the max_sessions slot, so
    #    the detector raised TypeError for eight days; test_block_contract now checks the
    #    registry's shape so that specific slip cannot recur.
    "fib_coverage":          _blk("emit (EDGE/LEVELS x lens universe)", "operator", None,
                                  severity="info",
                                  count=lambda b: (b or {}).get("verified"),
                                  note="F260725 C1: fib study coverage, the tracker that replaced "
                                       "the retired screener-alpha panel. Moves only when the "
                                       "operator banks a new read, so it has no freshness SLA - "
                                       "but the count must be non-zero, since zero verified banks "
                                       "means the fire gate can never fire."),
    "positional_assessment": _blk("signal_ledger.py", "laptop", 2),
    "regime_history":        _blk("regime_history_append.py", "laptop", 3),
    "flags":                 _blk("flag ledger", "laptop", 3),
    "risk_gates":            _blk("emit", "laptop", 2,
                                  note="shipped EMPTY on 2026-07-21, uncovered by any check"),
    "recent_trades":         _blk("trade_tracker_emit.py", "laptop", 3,
                                  allow_empty=True, severity="warn"),
    "recent_closed":         _blk("trade_tracker_emit.py", "laptop", 3,
                                  allow_empty=True, severity="warn"),
    "order_book":            _blk("order_book_tracker.py (F100)", "laptop", 70,
                                  _key_date("emitted_at"), severity="warn",
                                  note="quarterly-cadence backlog data — refresh on results, "
                                       "never daily-restamp without new data (260723 triage)"),
    "dr":                    _blk("DR pipeline", "laptop", 20, severity="warn"),
    "fundamentals":          _blk("fundamental_autopilot.py", "laptop", 100,
                                  _key_date("newest_period_end"), severity="warn",
                                  note="REBUILD-PLAN phase 6: the business read behind each fire "
                                       "and on every ticker page. Dated by its SUBSTANCE (the "
                                       "newest reporting period across the bank), never by the "
                                       "build - the autopilot rebuilds it daily while the numbers "
                                       "move once a quarter, so a build stamp would let a "
                                       "two-quarter-old read pass forever (a-date-is-not-an-age). "
                                       "max_sessions is a QUARTER-scale 100: a name being one "
                                       "period behind inside its filing window is normal, and a "
                                       "check that cries wolf every quarter stops being read. "
                                       "Substrate LAPTOP - the screener fetch runs on the "
                                       "operator's machine and stops when he is away."),
    # "analysis" RETIRED 2026-07-27 (F260723-ANALYSIS-RETIRE, operator decision) — orphaned (no
    # renderer), 66d stale, and tracking two permanently-retired PDF cycles. Producer removed from
    # equity_dashboard_emit and the equity_analysis_freshness detector removed with it, so no half
    # of the pair survives. Re-add here FIRST if it is ever revived, or it lands as an
    # UNREGISTERED BLOCK finding.

    # --- operator-private: encrypted into sensitive_enc and stripped from the public
    # aggregate. They MUST still report freshness, but under a codename (see the detector).
    # "family_account_b" RETIRED 2026-08-24 - the desk tracks one account, the operator's. A
    # contract entry for a block nobody produces is a nag for a file that will never arrive.
    "performance":           _blk("parse_capital_gains.py", "laptop", 10, severity="warn",
                                  public_as="perf_private", allow_empty=True),
    # F260728-ORDERLOG: the order-log behaviour half rides INSIDE real_scorecard.behaviour rather
    # than as its own block — it is optional by design (the Scorecard was complete before it and
    # stays complete if the export goes missing), and an optional block with its own contract
    # entry would nag for a file the operator has no obligation to re-drop.
    "real_scorecard":        _blk("real_scorecard.py", "operator", 10,
                                  _key_date("as_of"), severity="warn",
                                  public_as="scorecard_private", allow_empty=True,
                                  note="F260728-SCORECARD: the operator's REAL-money record, "
                                       "built from the capital-gains reports + the broker book. "
                                       "Substrate is OPERATOR because it advances only when a "
                                       "cap-gains report is dropped - so staleness here is a nag "
                                       "to drop one, not a system fault. It must still be DATED: "
                                       "an undated scorecard cannot be told apart from a current "
                                       "one, and the whole point of the block is that the number "
                                       "on it is TRUE TODAY."),
    "trade_lab":             _blk("trade_tracker_emit.py", "laptop", 2, severity="warn",
                                  public_as="trade_lab_private", allow_empty=True),
    "conviction":            _blk("conviction_book", "laptop", 10, severity="warn",
                                  public_as="conviction_private", allow_empty=True),

    # --- operator-driven
    "book":                  _blk("operator broker drop", "operator", 5, severity="warn"),
    "catalysts":             _blk("catalyst ledger", "operator", None, severity="info",
                                  note="forward-dated by nature; not a freshness signal"),
}

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
                        "EQUITY_BLOCKS (F260721-BLOCKROT)."),
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


def build_staleness(data, desk, now_utc_iso=None):
    """Build the canonical staleness contract block for an aggregate.

    desk: 'equity' | 'silver'. Returns a dict to assign to data['staleness'].
    Safe on partial/empty data — each detector self-guards.
    """
    now = _now_utc(now_utc_iso)
    # Single-desk by directive (2026-08-26). The silver branch moved to
    # `silver_staleness.build_staleness`; a function that dispatches on which desk
    # you are IS the shared engine that split exists to remove.
    if desk != "equity":
        raise ValueError(
            "staleness_contract is single-desk; %r has its own module" % (desk,))
    # bespoke detectors (the scar-tissue layer, kept for their UI dim targets + heal keys)
    items = _equity_items(data, now)
    # + the generic per-block contract (D1 / F260721-BLOCKROT): judges EVERY block
    # against TODAY so a frozen block can no longer hide under a fresh envelope.
    items = items + _block_items(data, now, EQUITY_BLOCKS, "equity")

    stale_items = [i for i in items if i.get("is_stale")]
    worst = "ok"
    for i in stale_items:
        if _SEV_RANK.get(i.get("severity", "info"), 1) > _SEV_RANK.get(worst, 0):
            worst = i.get("severity")
    return {
        "schema": SCHEMA,
        "computed_at_utc": now.isoformat(timespec="seconds"),
        "desk": desk,
        "any_stale": bool(stale_items),
        "any_dim": any(i.get("is_stale") and i.get("dim") for i in items),
        "worst": worst,
        "items": items,
    }


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) >= 3:
        desk = sys.argv[1]
        data = json.load(open(sys.argv[2], encoding="utf-8"))
        print(json.dumps(build_staleness(data, desk), indent=2))
    else:
        print("usage: staleness_contract.py <equity|silver> <aggregate.json>")
