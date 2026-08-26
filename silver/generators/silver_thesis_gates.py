#!/usr/bin/env python3
"""silver_thesis_gates.py -- evaluate the silver thesis gates from the SERIES.

WHY THIS EXISTS. The silver card's gate state was a sentence typed into
`silver_holdings.yaml`. On 2026-06-21 that sentence read "F2 FLOOR BROKEN ...
no EXIT trigger hit". Both halves went wrong while nobody looked:

  * the EXIT-ALL condition (weekly close < $63) fired 2026-06-26 and stayed fired
    for SIX consecutive weeks -- the card never said so;
  * the re-arm condition (weekly reclaim > $67.87) fired 2026-08-21 at 69.466 --
    the card never said so either.

Prose does not re-evaluate. A gate that is computed from the series cannot report
the state it held two months ago, and it dates itself to the bar it actually read.

Tests: 00_SYSTEM/TESTS/test_silver_thesis_gates.py
"""
from __future__ import annotations

from datetime import date


def block_age(block_last_updated: str | None, today: str,
              tolerance_days: int) -> dict:
    """Age a BLOCK by its own declared date, never by its file's mtime.

    `silver_holdings.yaml` was written 2026-08-24 while its `sr_levels` block had
    not been touched since 2026-06-21. Every freshness check on this desk aged the
    FILE and passed. An undated block is the worst case and is reported stale --
    absence of a date is never evidence of freshness.
    """
    if not block_last_updated:
        return {"age_days": None, "stale": True, "tolerance_days": tolerance_days,
                "last_updated": None,
                "reason": "undated block -- no last_updated to age against, "
                          "which is a finding, not freshness"}

    age = (date.fromisoformat(today) - date.fromisoformat(block_last_updated)).days
    stale = age > tolerance_days
    return {
        "age_days": age,
        "stale": stale,
        "tolerance_days": tolerance_days,
        "last_updated": block_last_updated,
        "reason": (f"block content is {age}d old (tolerance {tolerance_days}d)"
                   if stale else
                   f"block content is {age}d old, inside the {tolerance_days}d tolerance"),
    }


def completed_weeks(daily: list[tuple[str, float]], as_of: str) -> list[tuple[str, float]]:
    """Last close of each week whose week has actually FINISHED by `as_of`.

    A Tuesday is not a weekly close. Run live on Tue 2026-08-25 the F2 gate read
    the in-progress week's running 67.86 -- one cent under the 67.87 line -- and
    reported the re-arm clear, when the last COMPLETED weekly close (Fri 21-Aug)
    was 69.466 and comfortably above. The partial bar inverted the answer.
    """
    ref = date.fromisoformat(as_of)
    ref_week = ref.isocalendar()[:2]
    ref_is_week_end = ref.isoweekday() >= 5   # Friday or later closes the week

    weeks: dict[tuple, tuple[str, float]] = {}
    for d, c in sorted(daily):
        dt = date.fromisoformat(d)
        if dt > ref:
            continue
        wk = dt.isocalendar()[:2]
        if wk == ref_week and not ref_is_week_end:
            continue                       # the current week has not closed yet
        weeks[wk] = (d, c)
    return [weeks[k] for k in sorted(weeks)]


DEFAULT_TRAILING_WEEKS = 52


def weeks_since(weekly: list[tuple[str, float]], since: str | None,
                trailing_weeks: int = DEFAULT_TRAILING_WEEKS) -> list[tuple[str, float]]:
    """Narrow a weekly series to the period the CURRENT thesis has been live.

    A gate scored over the whole cache reports things like "fired 234 times since
    2021" -- true, useless, and it buries the number that matters. What the operator
    needs is the firings since HE WROTE THESE LEVELS.

    `since` is normally the thesis block's own `last_updated`. When it is absent the
    fallback is a trailing window, never the full history: "since forever" is the
    same non-answer in a different costume.
    """
    bars = sorted(weekly)
    if since:
        return [(d, c) for d, c in bars if d >= since]
    return bars[-trailing_weeks:] if trailing_weeks else bars


def evaluate_gate(series: list[tuple[str, float]], threshold: float,
                  direction: str) -> dict:
    """Evaluate one threshold gate over a dated close series.

    `series` is [(date, close)] in any order; `direction` is 'below' or 'above'.
    Returns the gate's WHOLE history over the window -- whether it ever fired, when
    it first fired, how many bars fired, and where it stands on the LAST bar -- so
    a consumer cannot print "not triggered" for a gate that fired and then cleared.
    """
    if direction not in ("below", "above"):
        raise ValueError(f"direction must be 'below' or 'above', got {direction!r}")
    if not series:
        raise ValueError("no series to evaluate -- a gate with no data is a finding, not 'clear'")

    bars = sorted(series)
    fired = [(d, c) for d, c in bars
             if (c < threshold if direction == "below" else c > threshold)]
    last_date, last_close = bars[-1]
    now_fired = (last_close < threshold if direction == "below" else last_close > threshold)

    return {
        "threshold": threshold,
        "direction": direction,
        "ever_fired": bool(fired),
        "first_fired_on": fired[0][0] if fired else None,
        "last_fired_on": fired[-1][0] if fired else None,
        "fired_count": len(fired),
        "state_now": "fired" if now_fired else "clear",
        "last_close": last_close,
        "as_of": last_date,
        "bars_read": len(bars),
    }
