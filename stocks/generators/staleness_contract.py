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

from datetime import datetime, timezone, timedelta

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
# F145 analysis-input freshness: operator-fed chart reads (SILV-TA weekly / STOCK-TA).
# Severity ladder per F260706-F145 (>30d warn, >60d alert for SILV-TA). Informational (dim=False).
SILV_TA_WARN_DAYS = 30
SILV_TA_ALERT_DAYS = 60
STOCK_TA_WARN_DAYS = 60


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

    # 5. Analysis-input freshness (F145): stale STOCK-TA chart reads across covered tickers.
    try:
        stmap = ((data.get("analysis") or {}).get("stock_ta")) or {}
        today_ist = now.astimezone(_IST).date()
        ages = {}
        for tk, ds in stmap.items():
            pd = _parse_iso(ds)
            if pd:
                probe = pd.date() if hasattr(pd, "date") else pd
                ages[tk] = (today_ist - probe).days
        stale_tks = {tk: a for tk, a in ages.items() if a > STOCK_TA_WARN_DAYS}
        oldest = max(ages.items(), key=lambda kv: kv[1]) if ages else None
        stale = bool(stale_tks)
        items.append(_item(
            id="equity_analysis_freshness", subsystem="equity", label="STOCK-TA reads",
            is_stale=stale, severity="warn" if stale else "info", dim=False,
            reason=(f"{len(stale_tks)} STOCK-TA read(s) >{STOCK_TA_WARN_DAYS}d old"
                    + (f" (oldest {oldest[0]} {oldest[1]}d)" if oldest else "")) if stale
                   else (f"fresh - {len(ages)} covered, oldest {oldest[1]}d" if oldest else "no STOCK-TA dates emitted"),
            since=None, sessions_stale=len(stale_tks) or None,
            heal=None, ui_targets=[],
        ))
    except Exception:
        pass

    return items


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
         severity="alert", label=None, note="", count=None):
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
                label=label, note=note, count=count)


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
    "next_session":          _blk("next_session_snapshot.py / emit", "cloud", 1,
                                  _key_date("as_of_ist"),
                                  count=lambda b: len((b or {}).get("rows") or []),
                                  note="rows=[] is the F260717 failure mode — empty is NOT legal"),

    # --- laptop-produced: the surfaces that rot when the operator is away
    "screeners":             _blk("screener_runner.py", "laptop", 1),
    "daytrade_inputs":       _blk("screener_runner.py", "laptop", 1,
                                  note="carries no as-of field — needs a producer stamp"),
    "signal_perf":           _blk("signal_ledger.py", "laptop", 1),
    "fib_confluences":       _blk("fib_confluence_feed.py", "laptop", 1,
                                  _key_date("price_as_of"),
                                  note="F260721-FIBPROV: now stamps price_as_of/basis. Scored on "
                                       "the LAST CLOSE, never intraday - the card must say so."),
    "positional_assessment": _blk("signal_ledger.py", "laptop", 2),
    "regime_history":        _blk("regime_history_append.py", "laptop", 3),
    "flags":                 _blk("flag ledger", "laptop", 3),
    "risk_gates":            _blk("emit", "laptop", 2,
                                  note="shipped EMPTY on 2026-07-21, uncovered by any check"),
    "recent_trades":         _blk("trade_tracker_emit.py", "laptop", 3,
                                  allow_empty=True, severity="warn"),
    "recent_closed":         _blk("trade_tracker_emit.py", "laptop", 3,
                                  allow_empty=True, severity="warn"),
    "order_book":            _blk("order_book_emit (F100)", "laptop", 10,
                                  _key_date("emitted_at"), severity="warn"),
    "dr":                    _blk("DR pipeline", "laptop", 20, severity="warn"),
    "analysis":              _blk("emit", "laptop", 20, severity="warn"),

    # --- operator-driven
    "book":                  _blk("operator broker drop", "operator", 5, severity="warn"),
    "catalysts":             _blk("catalyst ledger", "operator", None, severity="info",
                                  note="forward-dated by nature; not a freshness signal"),
}


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
        if spec is None:
            items.append(_item(
                id="block:" + name, subsystem=desk, label="Block %s" % name,
                is_stale=True, severity="warn", dim=False,
                reason=("UNREGISTERED BLOCK — present in the aggregate but absent from the "
                        "block contract, so its staleness can never be proven. Add it to "
                        "EQUITY_BLOCKS (F260721-BLOCKROT)."),
            ))
            continue
        if spec["substrate"] == "struct":
            continue

        blk = data.get(name)
        label = spec.get("label") or ("Block %s" % name)
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
                    id="block:" + name, subsystem=desk, label=label,
                    is_stale=True, severity=spec["severity"], dim=False,
                    reason=("EMPTY — %s produces this on the %s substrate and it came out "
                            "empty. %s" % (spec["owner"], spec["substrate"], spec["note"])).strip(),
                ))
                continue

            asof = spec["asof"](blk) if spec["asof"] else _max_date(blk)
            if asof is None:
                if spec["max_sessions"] is None:
                    continue
                items.append(_item(
                    id="block:" + name, subsystem=desk, label=label,
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
                id="block:" + name, subsystem=desk, label=label,
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
                id="block:" + name, subsystem=desk, label=label,
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
    if desk == "equity":
        # bespoke detectors (the scar-tissue layer, kept for their UI dim targets + heal keys)
        items = _equity_items(data, now)
        # + the generic per-block contract (D1 / F260721-BLOCKROT): judges EVERY block
        # against TODAY so a frozen block can no longer hide under a fresh envelope.
        items = items + _block_items(data, now, EQUITY_BLOCKS, "equity")
    elif desk == "silver":
        items = _silver_items(data, now)
    else:
        items = []

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
