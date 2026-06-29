#!/usr/bin/env python3
"""
watchdog_ci.py — CLOUD watchdog (laptop-OFF coverage). Codified 2026-06-04.

Runs inside GitHub Actions (.github/workflows/watchdog.yml) so the self-heal +
notifications feed keep working even when the operator's laptop is off. It is the
cloud twin of the vault-side `system_watchdog.py`.

WHAT IT DOES
  1. Verify the LAST refresh of each data workflow succeeded (gh run list).
  2. Verify the PUBLISHED aggregates are fresh (emitted_at_utc age).
  3. For any stale/failed subsystem → re-trigger its refresh workflow
     (`gh workflow run`, built-in GITHUB_TOKEN with actions:write). As a
     laptop-off belt-and-suspenders, equity ALSO re-runs the price overlay inline
     so data heals even if dispatch is throttled.
  4. Append a "spotted → action → outcome" event to status/notifications.json
     (+ a daily 🟢 heartbeat so the panel is never empty), then commit.

HONESTY: a re-trigger we cannot confirm in-process is recorded "pending", never
"fixed". Only an inline re-run whose result we re-check is "fixed".

Stdlib + gh CLI only (gh is preinstalled on ubuntu-latest runners).
Run from the repo root. Exit 0 always (a watchdog must never fail the workflow).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # status/ → notif_feed
import notif_feed as nf  # noqa: E402

REPO = "sparcho/SparchoTradingDesk"
ROOT = Path(__file__).resolve().parents[1]
FEED = ROOT / "status" / "notifications.json"
EQUITY_AGG = ROOT / "stocks" / "data" / "equity_dashboard_aggregate.json"
SILVER_AGG = ROOT / "silver" / "data" / "silver_dashboard_aggregate.json"

EQUITY_STALE_MIN = 90
SILVER_STALE_MIN = 90
SILVER_BOOK_STALE_DAYS = 10   # the locked holdings/deployment block is carried-forward in cloud refreshes;
                              # flag if it hasn't been re-emitted locally in this many days (prices stay fresh meanwhile)
SOURCE = "cloud-watchdog"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def is_weekend() -> bool:
    return nf.now_ist().weekday() >= 5


# Intraday NSE cash session (09:15–15:30 IST, Mon–Fri): tighten the live-price staleness
# threshold so a stale equity overlay is caught faster while the market is open. The
# cron re-fire interval is tightened cron-side (watchdog.yml). Off-hours keeps the
# relaxed 90-min tolerance (GitHub throttles the */20 schedule to multi-hour gaps).
EQUITY_STALE_MIN_INTRADAY = 45
NSE_OPEN = (9, 15)
NSE_CLOSE = (15, 30)


def in_nse_intraday() -> bool:
    now = nf.now_ist()
    if now.weekday() >= 5:
        return False
    return NSE_OPEN <= (now.hour, now.minute) <= NSE_CLOSE


def equity_stale_tolerance() -> int:
    return EQUITY_STALE_MIN_INTRADAY if in_nse_intraday() else EQUITY_STALE_MIN


def agg_age_min(path: Path):
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        ts = d.get("emitted_at_utc")
        e = datetime.fromisoformat(ts)
        if e.tzinfo is None:
            e = e.replace(tzinfo=timezone.utc)
        return (_now_utc() - e).total_seconds() / 60.0
    except Exception:
        return None


def silver_book_age_days(path: Path):
    """Age of the LAST LOCAL book re-emit (meta.book_local_emit_utc) — NOT the price timestamp.
    Returns None if the stamp is missing (older build that never wrote it). The price timestamp
    (emitted_at_utc) is re-stamped every 20 min and so can't reveal a frozen holdings block."""
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        ts = (d.get("meta") or {}).get("book_local_emit_utc")
        if not ts:
            return None
        e = datetime.fromisoformat(ts)
        if e.tzinfo is None:
            e = e.replace(tzinfo=timezone.utc)
        return (_now_utc() - e).total_seconds() / 86400.0
    except Exception:
        return None


def gh(*args, timeout=60):
    try:
        return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        class R:  # noqa
            returncode = 1
            stdout = ""
            stderr = str(e)
        return R()


def last_run_failed(workflow: str) -> bool:
    r = gh("run", "list", "--repo", REPO, "--workflow", workflow, "--limit", "1",
           "--json", "conclusion,status")
    if r.returncode != 0:
        return False
    try:
        arr = json.loads(r.stdout or "[]")
        if not arr:
            return False
        c = (arr[0].get("conclusion") or "").lower()
        return c in ("failure", "cancelled", "timed_out")
    except Exception:
        return False


def trigger(workflow: str) -> bool:
    r = gh("workflow", "run", workflow, "--repo", REPO)
    return r.returncode == 0


def main() -> int:
    events = []

    # ── EQUITY ───────────────────────────────────────────────────────────────
    if not is_weekend():
        tol = equity_stale_tolerance()
        age = agg_age_min(EQUITY_AGG)
        failed = last_run_failed("refresh-stocks-live.yml")
        if age is None or age > tol or failed:
            _win = "intraday" if in_nse_intraday() else "off-hours"
            spotted = (f"equity aggregate {age:.0f}m stale (tol {tol}m, {_win})"
                       if age is not None else "equity aggregate unreadable")
            if failed:
                spotted += " + last live-refresh Action FAILED"
            # Inline heal (laptop-off belt-and-suspenders): re-run the price overlay.
            inline_ok = False
            try:
                pr = subprocess.run([sys.executable, "stocks/generators/refresh_prices.py"],
                                    cwd=str(ROOT), capture_output=True, text=True, timeout=300)
                inline_ok = pr.returncode == 0
            except Exception:
                inline_ok = False
            new_age = agg_age_min(EQUITY_AGG)
            trig = trigger("refresh-stocks-live.yml")
            if inline_ok and new_age is not None and new_age <= tol:
                outcome, action = "fixed", "re-ran price overlay inline (now fresh) + re-triggered live Action"
                sev = "🟢"
            else:
                outcome = "pending"
                action = ("re-triggered live-refresh Action" if trig
                          else "tried to re-trigger live-refresh Action (dispatch failed)")
                sev = "🔴" if failed else "🟡"
            events.append(nf.make_event(sev, "equity-dashboard", spotted, action, outcome, SOURCE))

    # ── SILVER ───────────────────────────────────────────────────────────────
    age = agg_age_min(SILVER_AGG)
    failed = last_run_failed("refresh-dashboard.yml")
    if age is None or age > SILVER_STALE_MIN or failed:
        spotted = (f"silver aggregate {age:.0f}m stale" if age is not None
                   else "silver aggregate unreadable")
        if failed:
            spotted += " + last silver-refresh Action FAILED"
        trig = trigger("refresh-dashboard.yml")
        action = ("re-triggered silver 20-min refresh Action" if trig
                  else "tried to re-trigger silver Action (dispatch failed)")
        events.append(nf.make_event("🔴" if failed else "🟡", "silver-dashboard",
                                    spotted, action, "pending", SOURCE))

    # ── silver BOOK freshness (content, not just the price timestamp) ─────────
    # The price timestamp is fresh every 20 min, but the locked holdings/deployment block is
    # carried forward unchanged until the operator re-emits locally. A fresh file with an OLD
    # book emit-date = deployment/dry-powder/P&L frozen vs the live book. Once-per-day so it nudges,
    # not spams. (True value-vs-reality reconciliation runs locally — see silver_equity_reconcile.py.)
    bage = silver_book_age_days(SILVER_AGG)
    if bage is None or bage > SILVER_BOOK_STALE_DAYS:
        feed_now = nf.load_feed(FEED)
        today_iso2 = nf.now_ist().date().isoformat()
        already = any(e.get("subsystem") == "silver-book" and str(e.get("ts", ""))[:10] == today_iso2
                      for e in feed_now.get("events", []))
        if not already:
            spotted = (f"silver book not re-emitted locally in {bage:.0f}d — prices are fresh but holdings/deployment are frozen"
                       if bage is not None
                       else "silver book emit-date missing — can't confirm the locked holdings/deployment are current")
            events.append(nf.make_event("🟡", "silver-book", spotted,
                                        "nudge: re-run the silver emit locally so deployment & P&L reflect the live book",
                                        "pending", SOURCE))

    # ── cross-desk reconciliation (silver desk vs equity SILVERBEES) ──────────
    # Only emits where BOTH dashboard passwords exist (operator laptop / vault watchdog); in the
    # cloud there are no passwords so it returns nothing. The real "catch the mismatch" check.
    try:
        import silver_equity_reconcile as recon
        events.extend(recon.run(write=False))
    except Exception as _ex:
        print(f"reconcile skipped: {_ex}", file=sys.stderr)

    # ── level tripwires (F260607-F126) ───────────────────────────────────────
    try:
        import level_alerts
        for _e in level_alerts.run():
            print(f"{_e['severity']} level-alert: {_e['spotted']}")
    except Exception as _ex:  # alerts must never break the watchdog
        print(f"level-alerts skipped: {_ex}", file=sys.stderr)

    # ── heartbeat ────────────────────────────────────────────────────────────
    feed = nf.load_feed(FEED)
    today_iso = nf.now_ist().date().isoformat()
    no_alerts = all(e["outcome"] in ("fixed", "clear") for e in events)
    if no_alerts and not nf.has_heartbeat_today(feed, SOURCE, today_iso):
        events.append(nf.heartbeat_event(
            SOURCE, "cloud check — published dashboards fresh, refresh Actions healthy"))

    if not events:
        print("watchdog_ci: nothing to record (already healthy + heartbeat logged today)")
        return 0

    nf.append_events(FEED, events)
    for e in events:
        print(f"{e['severity']} {e['subsystem']}: {e['spotted']} -> {e['action_taken']} [{e['outcome']}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
