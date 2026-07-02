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

    return items


_SEV_RANK = {"ok": 0, "info": 1, "warn": 2, "alert": 3}


def build_staleness(data, desk, now_utc_iso=None):
    """Build the canonical staleness contract block for an aggregate.

    desk: 'equity' | 'silver'. Returns a dict to assign to data['staleness'].
    Safe on partial/empty data — each detector self-guards.
    """
    now = _now_utc(now_utc_iso)
    if desk == "equity":
        items = _equity_items(data, now)
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
