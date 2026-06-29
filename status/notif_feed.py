#!/usr/bin/env python3
"""
notif_feed.py — the SYSTEM-STATUS notifications feed (codified 2026-06-04).

WHY THIS EXISTS
  Operator wants a ZERO-NAG, NO-EMAIL system. Instead of pushing alerts, the
  watchdog (`system_watchdog.py` on the laptop + `status/watchdog_ci.py` in the
  cloud) records every "spotted → action → outcome" event to this append-only,
  capped JSON. The landing page (sparcho.github.io/SparchoTradingDesk/) renders
  it newest-first as a "System status" panel. The operator reads it on their own
  time — no push, no inbox.

CANONICAL LOCATION
  The feed lives in the PUBLIC dashboard repo so it deploys with the site:
      <repo>/status/notifications.json
  This module is the single source of truth for the schema + writer. A byte-for-byte
  copy is mirrored to <repo>/status/notif_feed.py so the cloud GitHub Action (which
  cannot import the vault) uses the IDENTICAL logic. Keep them in sync — the vault
  copy is authoritative; `system_watchdog.py` re-copies it on every run.

PRIVACY (HARD RULE)
  The feed is OPS/STATUS METADATA ONLY. It is published to the PUBLIC landing
  surface, so it must NEVER carry holdings, quantities, avg-cost, or P&L. Event
  strings describe subsystem health only ("equity aggregate 47h stale → re-emit →
  fixed"). `assert_clean()` hard-blocks any event whose text smells of money.

EVENT SCHEMA
  {
    "ts":           "2026-06-04T14:50:00+05:30",   # IST, human display
    "ts_utc":       "2026-06-04T09:20:00+00:00",    # UTC, sorting/age
    "severity":     "🟢" | "🟡" | "🔴",
    "level":        "clear" | "info" | "warn" | "alert",
    "subsystem":    "equity-dashboard" | "silver-dashboard" | "cron" | ...,
    "spotted":      "what the watchdog detected",
    "action_taken": "what it attempted (or 'none — all clear')",
    "outcome":      "clear" | "fixed" | "pending" | "failed",
    "source":       "vault-watchdog" | "cloud-watchdog"
  }

Stdlib only — must run unchanged in the GitHub Actions runner.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "v1"
DEFAULT_CAP = 50
IST = timezone(timedelta(hours=5, minutes=30))

# Severity → level map (machine field the panel can style on without parsing emoji).
_LEVEL_FOR = {"🟢": "clear", "🟡": "warn", "🔴": "alert"}

# Privacy tripwire — substrings that must never appear in a public ops event.
_MONEY_SMELL = ("avg_cost", "invested", "₹", "qty", "p&l", "pnl", "unrealized", "realised", "realized")


# ── time ───────────────────────────────────────────────────────────────────

def now_ist() -> datetime:
    return datetime.now(IST)


def _atomic_write_json(path: Path, obj: Any) -> Path:
    """Crash-safe write (temp + os.replace in the same dir). Stdlib only so the
    CI copy needs no vault dependency."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, indent=2, ensure_ascii=False)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return path
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# ── events ─────────────────────────────────────────────────────────────────

def make_event(severity: str, subsystem: str, spotted: str, action_taken: str,
               outcome: str, source: str, ts: datetime | None = None) -> dict:
    """Build one well-formed event. `severity` is one of 🟢/🟡/🔴."""
    if severity not in _LEVEL_FOR:
        severity = "🟡"
    ts = ts or now_ist()
    ts_utc = ts.astimezone(timezone.utc)
    return {
        "ts": ts.isoformat(timespec="seconds"),
        "ts_utc": ts_utc.isoformat(timespec="seconds"),
        "severity": severity,
        "level": _LEVEL_FOR[severity],
        "subsystem": str(subsystem),
        "spotted": str(spotted),
        "action_taken": str(action_taken),
        "outcome": str(outcome),
        "source": str(source),
    }


def heartbeat_event(source: str, summary: str, ts: datetime | None = None) -> dict:
    """The daily 🟢 all-clear so the panel is never empty."""
    return make_event(
        "🟢", "heartbeat",
        spotted=summary,
        action_taken="none — routine check, nothing to fix",
        outcome="clear", source=source, ts=ts,
    )


def assert_clean(event: dict) -> None:
    """HARD privacy gate: refuse to record anything that smells of holdings/P&L on
    the public ops feed. Raises ValueError on violation — the watchdog catches it
    and downgrades the event to a neutral redacted note rather than leaking."""
    blob = " ".join(str(event.get(k, "")) for k in ("spotted", "action_taken", "subsystem")).lower()
    for needle in _MONEY_SMELL:
        if needle in blob:
            raise ValueError(f"notif event smells of money ('{needle}') — refused: {blob[:120]}")


# ── feed file ──────────────────────────────────────────────────────────────

def load_feed(path: Path) -> dict:
    """Read the feed; tolerate missing/corrupt → return a fresh empty feed."""
    path = Path(path)
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "doc_type": "notifications_feed",
                "updated_at_utc": None, "events": []}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(d, dict) or not isinstance(d.get("events"), list):
            raise ValueError("shape")
        return d
    except Exception:
        # Corrupt feed must never block the watchdog — start clean (old events lost
        # but a half-written feed is worse). The watchdog records this very fact.
        return {"schema_version": SCHEMA_VERSION, "doc_type": "notifications_feed",
                "updated_at_utc": None, "events": []}


def has_heartbeat_today(feed: dict, source: str, today_iso: str) -> bool:
    for e in feed.get("events", []):
        if e.get("subsystem") == "heartbeat" and e.get("source") == source \
                and str(e.get("ts", ""))[:10] == today_iso:
            return True
    return False


def append_events(path: Path, events: Iterable[dict], cap: int = DEFAULT_CAP) -> dict:
    """Prepend `events` (newest-first), cap the list, atomic-write. Returns the
    written feed dict. Each event is privacy-checked; violators are redacted, not
    dropped, so the panel still shows that *something* happened."""
    feed = load_feed(path)
    clean: list[dict] = []
    for e in events:
        try:
            assert_clean(e)
        except ValueError:
            e = make_event(e.get("severity", "🟡"), e.get("subsystem", "redacted"),
                           spotted="(event redacted — would have exposed sensitive data)",
                           action_taken="(redacted)", outcome=e.get("outcome", "pending"),
                           source=e.get("source", "vault-watchdog"))
        clean.append(e)
    # Newest first: new events ahead of the existing list.
    feed["events"] = (clean + feed.get("events", []))[:cap]
    feed["schema_version"] = SCHEMA_VERSION
    feed["doc_type"] = "notifications_feed"
    feed["updated_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _atomic_write_json(path, feed)
    return feed


if __name__ == "__main__":
    # Self-test: write a heartbeat + a synthetic fix event to a temp feed and dump.
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows cp1252 guard
    except Exception:
        pass
    p = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("_notif_selftest.json")
    append_events(p, [
        heartbeat_event("vault-watchdog", "all subsystems fresh"),
        make_event("🟡", "equity-dashboard",
                   "aggregate 47h stale (emitted_at_utc 2026-06-02)",
                   "re-ran equity_dashboard_emit + pushed",
                   "fixed", "vault-watchdog"),
    ])
    print(p.read_text(encoding="utf-8"))
