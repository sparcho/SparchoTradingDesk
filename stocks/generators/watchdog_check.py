#!/usr/bin/env python3
"""watchdog_check.py — cloud freshness watchdog for the published dashboards.

Runs in GitHub Actions on a schedule (no host in the loop). Reads the PUBLISHED
aggregates and judges whether the live data is actually current, using the
NSE-session-aware `daytrade_freshness` signal the refresh already produces. It
writes stocks/data/health.json AND exits NON-ZERO on any breach — a failed
scheduled workflow makes GitHub email the operator automatically. That is the
"tell me when it's stale" safety net: autonomous, fail-loud, never silent.

Repo layout assumed (SparchoTradingDesk): <root>/stocks/... and <root>/silver/...
with this file at <root>/stocks/generators/watchdog_check.py.
"""
from __future__ import annotations
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]                       # <root>/stocks/generators -> <root>
STOCKS_AGG = Path(os.environ.get("WATCHDOG_STOCKS_AGG", REPO / "stocks" / "data" / "equity_dashboard_aggregate.json"))
SILVER_AGG = REPO / "silver" / "data" / "silver_dashboard_aggregate.json"
HEALTH_OUT = Path(os.environ.get("WATCHDOG_HEALTH_OUT", STOCKS_AGG.parent / "health.json"))

MAX_SESSIONS_STALE = 1   # fires must be on the latest (or 1-prior) NSE close
EMIT_OVERDUE_UTC_HOUR = 12   # weekday past 12:00 UTC (17:30 IST) -> the nightly emit should have run


def _emit_overdue(now=None):
    """The nightly full engine emit runs post-close on weekdays (~16:25 IST =
    ~10:55 UTC). If it's a weekday and already past 12:00 UTC (17:30 IST) with
    fires freshness STILL unpublished, the emit almost certainly didn't run --
    that is a real failure worth paging on, not the quiet pre-emit state."""
    now = now or datetime.now(timezone.utc)
    return now.weekday() < 5 and now.hour >= EMIT_OVERDUE_UTC_HOUR


def check_stocks():
    """Judge the Day-Trade Fires freshness with a three-tier verdict:
      - present & current        -> OK
      - present & stale          -> FAIL (real staleness; page the operator)
      - missing/None (pre-emit)  -> WARN, quiet -- UNLESS the nightly full emit
                                    is overdue (weekday past 12:00 UTC), which
                                    escalates to FAIL ('the emit didn't run').
    Only FAIL sets breach (=> non-zero exit => GitHub failure email)."""
    try:
        d = json.loads(STOCKS_AGG.read_text(encoding="utf-8"))
    except Exception as e:
        return [{"check": "stocks_aggregate", "status": "FAIL", "detail": "unreadable: " + str(e)}], True
    out, breach = [], False
    fr = d.get("daytrade_freshness") or {}
    ss = fr.get("sessions_stale")
    st = fr.get("status")
    paf = fr.get("price_as_of")
    # RECOMPUTE staleness from price_as_of against the wall clock (shared scorer), so a
    # FROZEN stamp -- e.g. every refresh job dead -- can never read fresh forever. The
    # stamped price_as_of is ground truth (which close the data is on); how stale that is
    # relative to NOW is judged here, intraday-aware (260611 CC-FIX).
    if paf:
        try:
            import daytrade_core as _dc
        except ImportError:
            sys.path.insert(0, str(HERE)); import daytrade_core as _dc
        try:
            _rc = _dc.daytrade_freshness(paf)
            ss, st = _rc["sessions_stale"], _rc["status"]
        except Exception:
            pass
    present = (st is not None) or (ss is not None)
    note = ""
    if present:
        is_stale = (st == "STALE") or (ss is not None and ss > MAX_SESSIONS_STALE)
        if is_stale:
            status, breach = "FAIL", True
        else:
            status = "OK"
    elif _emit_overdue():
        status, breach = "FAIL", True
        note = " (nightly full emit OVERDUE -- weekday past 12:00 UTC and fires freshness still unpublished)"
    else:
        status = "WARN"
        note = " (fires freshness not yet published -- pre-emit graceful state; not paging)"
    out.append({
        "check": "daytrade_fires_freshness",
        "status": status,
        "detail": "status=" + str(st) + " sessions_stale=" + str(ss) + " price_as_of=" + str(paf) + note,
    })
    # Informational only (operator-driven broker drops are expected to lag): never a hard fail.
    book = d.get("book") or {}
    out.append({
        "check": "broker_book_staleness",
        "status": "INFO",
        "detail": "sessions_stale=" + str(book.get("sessions_stale")) + " snapshot=" + str(book.get("snapshot_date")),
    })
    return out, breach


def check_silver():
    """Best-effort: report silver freshness if present; never hard-fails on schema
    drift (the stocks check is the load-bearing one)."""
    if not SILVER_AGG.exists():
        return [{"check": "silver_aggregate", "status": "SKIP", "detail": "not found at " + str(SILVER_AGG)}], False
    try:
        d = json.loads(SILVER_AGG.read_text(encoding="utf-8"))
    except Exception as e:
        return [{"check": "silver_aggregate", "status": "WARN", "detail": "unreadable: " + str(e)}], False
    emitted = d.get("emitted_at_utc") or d.get("generated_utc") or "?"
    return [{"check": "silver_aggregate", "status": "INFO", "detail": "emitted " + str(emitted)}], False


# A.4 (F260616): morning daily-roll watchdog. The desk must re-emit every trading
# morning (Morning Roll task ~09:25 IST). 04:15 UTC = 09:45 IST gives the roll a grace
# buffer before we page on a miss.
ROLL_OVERDUE_UTC = (4, 15)


def _ist_date(dt_utc):
    return (dt_utc + timedelta(hours=5, minutes=30)).date()


def _roll_overdue(now=None):
    now = now or datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False  # weekend — NSE shut, no roll expected (NSE holidays not modelled)
    return (now.hour, now.minute) >= ROLL_OVERDUE_UTC


def check_daily_roll():
    """The published aggregate must be re-emitted today (IST) every trading day. If it
    hasn't been by ~09:45 IST on a weekday, the Morning Roll (09:25 IST) almost certainly
    didn't run -> FAIL (page) so a missed roll surfaces LOUD instead of the client-side
    TODAY anchor quietly masking it. Keyed to the emit timestamp, not the session date
    (pre-close the session date is legitimately yesterday)."""
    try:
        d = json.loads(STOCKS_AGG.read_text(encoding="utf-8"))
    except Exception as e:
        return [{"check": "daily_roll", "status": "FAIL", "detail": "aggregate unreadable: " + str(e)}], True
    em = d.get("emitted_at_utc")
    now = datetime.now(timezone.utc)
    try:
        em_dt = datetime.fromisoformat(str(em).replace("Z", "+00:00"))
        rolled_today = _ist_date(em_dt) >= _ist_date(now)
    except Exception:
        return [{"check": "daily_roll", "status": "WARN", "detail": "no parseable emitted_at_utc: " + str(em)}], False
    if rolled_today:
        return [{"check": "daily_roll", "status": "OK", "detail": "rolled today IST (emitted_at_utc=" + str(em) + ")"}], False
    if _roll_overdue(now):
        return [{"check": "daily_roll", "status": "FAIL",
                 "detail": "desk NOT rolled today -- emitted_at_utc=" + str(em) + " is < today IST and it is a weekday past 09:45 IST (Morning Roll missed)"}], True
    return [{"check": "daily_roll", "status": "WARN", "detail": "not yet rolled today (pre-morning-roll window); emitted_at_utc=" + str(em)}], False


PROFIT_LOCK_STATUS = STOCKS_AGG.parent / "profit_lock_status.json"
PL_STALE_MIN = 45   # price feed older than this during a session -> WARN (the advisory rides a quiet feed)


def _in_market_hours(now=None):
    """Rough NSE market window in UTC (~09:15-15:30 IST = 03:45-10:00 UTC), weekdays."""
    now = now or datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return (3 * 60 + 45) <= mins <= (10 * 60 + 0)


def check_profit_lock():
    """Freshness of the public live-price feed the client-side +1R Profit-Lock advisory
    rides (the advisory itself is computed in-browser from the operator-decrypted ledger,
    so nothing private is checkable cloud-side). Reads the PUBLISHED aggregate's
    meta.last_price_refresh (churn-free, always present) -- falls back to the local
    profit_lock_status.json heartbeat if absent. NON-breaching: the load-bearing
    staleness already pages via daytrade_fires; this is corroborating context so a
    quietly-dead live feed during market hours surfaces as WARN. (F profit-lock, 2026-06-24.)"""
    ran = None
    try:
        d = json.loads(STOCKS_AGG.read_text(encoding="utf-8"))
        ran = (d.get("meta") or {}).get("last_price_refresh") or d.get("emitted_at_utc")
    except Exception:
        ran = None
    if ran is None and PROFIT_LOCK_STATUS.exists():
        try:
            ran = json.loads(PROFIT_LOCK_STATUS.read_text(encoding="utf-8")).get("ran_at_utc")
        except Exception:
            ran = None
    if ran is None:
        return [{"check": "profit_lock_advisory", "status": "SKIP", "detail": "no price-refresh timestamp available"}], False
    now = datetime.now(timezone.utc)
    try:
        ran_dt = datetime.fromisoformat(str(ran).replace("Z", "+00:00"))
        age_min = (now - ran_dt).total_seconds() / 60.0
    except Exception:
        return [{"check": "profit_lock_advisory", "status": "WARN", "detail": "no parseable price-refresh ts: " + str(ran)}], False
    if _in_market_hours(now) and age_min > PL_STALE_MIN:
        return [{"check": "profit_lock_advisory", "status": "WARN",
                 "detail": "live-price feed %.0f min old during market hours (advisory rides a quiet feed)" % age_min}], False
    return [{"check": "profit_lock_advisory", "status": "OK",
             "detail": "live-price feed %.0f min old (advisory evaluated client-side)" % age_min}], False


def main():
    # Read the PRIOR verdict before overwriting health.json so we page only on a
    # state TRANSITION into breach (fresh->stale), not on every repeat-breach tick.
    # Repeat-breach still writes overall=FAIL (the dashboard banner + health.json
    # keep showing stale -- honesty invariant preserved) but exits 0, so GitHub
    # emails ONCE per incident instead of every 3h (260611 CC-FIX, directive item 4).
    # Auto-re-arms: after recovery (FAIL->OK) the next fresh->stale edge pages again.
    prior_overall = None
    try:
        prior_overall = json.loads(HEALTH_OUT.read_text(encoding="utf-8")).get("overall")
    except Exception:
        pass

    checks, breach = [], False
    c, b = check_stocks(); checks += c; breach = breach or b
    c, b = check_profit_lock(); checks += c; breach = breach or b
    c, b = check_daily_roll(); checks += c; breach = breach or b
    c, b = check_silver(); checks += c; breach = breach or b
    has_warn = any(ch.get("status") == "WARN" for ch in checks)
    overall = "FAIL" if breach else ("WARN" if has_warn else "OK")

    transition_into_breach = breach and (prior_overall != "FAIL")
    paged = bool(transition_into_breach)

    health = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "overall": overall,
        "prior_overall": prior_overall,
        "paged": paged,
        "checks": checks,
    }
    try:
        HEALTH_OUT.write_text(json.dumps(health, indent=2), encoding="utf-8")
    except Exception as e:
        print("WARN: could not write health.json: " + str(e), file=sys.stderr)
    print(json.dumps(health, indent=2))
    if breach and not paged:
        print("WATCHDOG repeat-breach — already paged on the fresh->stale transition; "
              "staying quiet (health.json overall=FAIL). No re-page.", file=sys.stderr)
        return 0
    if breach:
        print("WATCHDOG BREACH (fresh->stale transition) — published data is stale; see health.json", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
