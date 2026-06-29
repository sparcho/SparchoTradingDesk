#!/usr/bin/env python3
"""
nse_calendar.py — the single NSE-session gate (F131 data-integrity, 260616).

Why this exists: the screener stamped lens CSVs with the raw *calendar* date, so weekend
runs created Sat/Sun-dated lens files -> weekend as-of cohorts -> the fires curve counted the
same signal multiple times (260616 P0). Every generator that DATES a signal/trade/fire must
route its date through here so no as-of / entry / fill / exit / fire date ever lands on a
non-NSE session.

Gate = weekday Mon-Fri AND not an NSE holiday. Weekends are definitive. Holidays are read from
an OPTIONAL data file (00_SYSTEM/DATA/nse_holidays.csv, one ISO date per line / first column) —
empty if absent. We do NOT hard-code guessed holiday dates here (fabricated dates would be worse
than the weekday gate that fixes the actual bug); populate the data file to add holidays.

stdlib only.
"""
from __future__ import annotations
import csv
import os
from datetime import date, datetime, timedelta
from functools import lru_cache

_HOLIDAY_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "data", "nse_holidays.csv")  # F260626: lowercase to match repo dir (Linux cloud is case-sensitive)


@lru_cache(maxsize=1)
def holidays() -> frozenset:
    """Set of ISO date strings that are NSE holidays (optional data file; empty if absent)."""
    out = set()
    try:
        with open(_HOLIDAY_FILE, newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                if not row:
                    continue
                cell = row[0].strip()
                if len(cell) == 10 and cell[4] == "-" and cell[:4].isdigit():
                    out.add(cell)
    except FileNotFoundError:
        pass
    return frozenset(out)


def _as_date(d):
    """Accept a date / datetime / 'YYYY-MM-DD' (or longer ISO) string -> date. None on failure."""
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    if isinstance(d, str):
        s = d.strip()[:10]
        try:
            return date.fromisoformat(s)
        except ValueError:
            return None
    return None


SPECIAL_SESSIONS = frozenset({
    "2025-02-01",   # Union Budget 2025 special live session (Saturday)
})


def is_session(d) -> bool:
    """True iff a real NSE session: weekday not a holiday, OR a special weekend session. (F260626)"""
    dd = _as_date(d)
    if dd is None:
        return False
    iso = dd.isoformat()
    if iso in holidays():
        return False
    if iso in SPECIAL_SESSIONS:
        return True
    return dd.weekday() < 5


def latest_session(d) -> date | None:
    """The most recent NSE session on or before d (snaps a weekend/holiday back)."""
    dd = _as_date(d)
    if dd is None:
        return None
    for _ in range(15):                      # >= a fortnight of slack covers any holiday cluster
        if is_session(dd):
            return dd
        dd -= timedelta(days=1)
    return dd


def prev_session(d) -> date | None:
    """The most recent NSE session strictly before d."""
    dd = _as_date(d)
    if dd is None:
        return None
    return latest_session(dd - timedelta(days=1))


def next_session(d) -> date | None:
    """The first NSE session strictly after d."""
    dd = _as_date(d)
    if dd is None:
        return None
    for _ in range(15):
        dd += timedelta(days=1)
        if is_session(dd):
            return dd
    return dd


if __name__ == "__main__":
    import sys
    args = sys.argv[1:] or [date.today().isoformat()]
    for a in args:
        print(f"{a}: session={is_session(a)} latest={latest_session(a)} "
              f"prev={prev_session(a)} next={next_session(a)}")
    print(f"holidays loaded: {len(holidays())}")
