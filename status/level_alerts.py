#!/usr/bin/env python3
"""level_alerts.py — F260607-F126: doctrine level-tripwires for the cloud watchdog.

Reads the live silver aggregate (XAG refreshed every 20 min by the silver cron) +
the repo daily_prices.csv (TNX/DXY/USDINR, daily granularity) and fires a
notifications-feed event when a doctrine level is crossed. Each tripwire fires
ONCE and re-arms only when the predicate goes false again (state kept under the
feed's "tripwires" key — same file, so the existing workflow commit covers it).

Thresholds mirror DICTIONARY ladders / CLAUDE §5 — change them THERE first.
"""
from __future__ import annotations

import csv
import json
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FEED = ROOT / "status" / "notifications.json"
SILVER_AGG = ROOT / "silver" / "data" / "silver_dashboard_aggregate.json"
PRICES_CSV = ROOT / "silver" / "_cache" / "daily_prices.csv"
SOURCE = "cloud-watchdog"

sys.path.insert(0, str(ROOT / "status"))
import notif_feed as nf  # noqa: E402


def _xag_live():
    try:
        d = json.loads(SILVER_AGG.read_text(encoding="utf-8"))
        cp = d.get("current_price") or {}
        v = cp.get("price") or cp.get("xagusd")
        return float(v) if v else None
    except Exception:
        return None


def _csv_latest(ticker):
    try:
        last = None
        with io.open(PRICES_CSV, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("ticker") == ticker and (row.get("close") or "").strip():
                    last = float(row["close"])
        return last
    except Exception:
        return None


def tripwires():
    xag = _xag_live()
    tnx = _csv_latest("TNX")
    dxy = _csv_latest("DXY")
    inr = _csv_latest("USDINR")
    T = []
    if xag is not None:
        T += [
            ("xag_f2_6787", xag < 67.87, "🔴", f"XAG {xag:.2f} BELOW F2 $67.87 — §5.3 suspend watch (daily CLOSE decides; do not act on wick)"),
            ("xag_exitall_63", xag < 63.0, "🔴", f"XAG {xag:.2f} BELOW $63 — WEEKLY close <63 = EXIT-ALL (V-18 ladder); watch Friday close"),
            ("xag_v18_52", xag < 52.0, "🔴", f"XAG {xag:.2f} BELOW $52 — V-18 unconditional abort level"),
            ("xag_range_72", xag > 72.0, "🟢", f"XAG {xag:.2f} back ABOVE $72 — re-entered the old accumulation range (bull-case marker)"),
        ]
    if tnx is not None:
        T += [
            ("tnx_rearm_445", tnx < 4.45, "🟢", f"TNX {tnx:.2f}% back BELOW 4.45% — abort-trigger re-arm leg 1 (gates still ALL-required)"),
            ("tnx_fail_445", tnx > 4.45, "🟡", f"TNX {tnx:.2f}% above 4.45% — §5.2 abort-trigger condition present"),
        ]
    if dxy is not None:
        T += [("dxy_reduce_102", dxy > 102.0, "🔴", f"DXY {dxy:.1f} above 102 — weekly close >102 = reduce-50 (V-06)")]
    if inr is not None:
        T += [("inr_reduce_96", inr > 96.0, "🔴", f"USDINR {inr:.2f} above 96 — weekly close >96 = reduce-25 (5.2a)")]
    return T


def run() -> list:
    feed = nf.load_feed(FEED)
    state = feed.get("tripwires") or {}
    events = []
    for tid, hit, sev, msg in tripwires():
        st = state.get(tid) or {"armed": True}
        if hit and st.get("armed", True):
            events.append(nf.make_event(sev, "level-alert", msg,
                                        "logged to dashboard feed (F126 tripwire)", "alert", SOURCE))
            st = {"armed": False, "fired_at": nf.now_ist().isoformat()}
        elif not hit and not st.get("armed", True):
            st = {"armed": True, "rearmed_at": nf.now_ist().isoformat()}
        state[tid] = st
    if events:
        nf.append_events(FEED, events)
    # persist state (same file; second small write after append)
    try:
        d = json.loads(FEED.read_text(encoding="utf-8"))
        d["tripwires"] = state
        FEED.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        print(f"[level-alerts] state persist failed: {e}", file=sys.stderr)
    return events


if __name__ == "__main__":
    evs = run()
    for e in evs:
        print(f"{e['severity']} {e['subsystem']}: {e['spotted']}")
    print(f"[level-alerts] {len(evs)} fired")
