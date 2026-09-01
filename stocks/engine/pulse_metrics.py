#!/usr/bin/env python3
"""PULSE metric core -- the single definition of what "unusual volume" means.

WHY THIS FILE EXISTS (2026-09-01 rebuild)
-----------------------------------------
The first PULSE measured ONE window -- 09:20-09:45 -- and then talked about it all day. That is
wrong in both directions, and the operator caught it:

  * a name that erupts at 11:15 or into the close NEVER appeared, because the only window that
    counted had already closed;
  * a name that spiked at 09:20 and died by 09:50 sat at the top of the strip at 37x until
    15:30, because nothing ever re-measured it.

Every measure here is time-of-day normalised against THAT NAME'S OWN prior sessions at the SAME
clock time -- never cross-sectional, never a fixed percentage
(cf. fixed-percent-barriers-measure-volatility).

  rvol_cum    volume since the open through the last bar, vs the same span on prior sessions.
              The headline. Updates on every run instead of freezing at 09:45.
  rvol_now    the last 30 minutes vs that same 30-minute slot on prior sessions. The
              "is it STILL going" number the old build could not ask.
  rvol_open   the original 09:20-09:45 measure, kept so the ORB evidence stays comparable and
              nothing already studied is silently redefined.

MIRRORED FILE -- THIS IS A DATA CONTRACT
----------------------------------------
Byte-identical copies live at:
    00_SYSTEM/GENERATORS/pulse_metrics.py        (laptop)
    stocks/engine/pulse_metrics.py               (cloud -- the AUTHORITATIVE producer)
TESTS/test_pulse_metrics.py asserts they are identical. Editing one and not the other is how
the cloud engine ran 28 names short for weeks with every job green
(cf. a-mirrored-file-is-a-data-contract).
"""
from __future__ import annotations

import bisect
import statistics

GRID_MIN = 5                    # bar width the desk archives
SESSION_OPEN = "09:15"
SESSION_CLOSE = "15:30"

# Never 09:15 for a volume measure -- that bar reports volume 0 in 58 of 60 sessions
# (measured 2026-08-26), so a feature anchored on it silently measures nothing.
OPEN_FROM, OPEN_TO = "09:20", "09:45"

NOW_WINDOW_MIN = 30             # the "is it still going" window
PROFILE_MIN = 30                # width of one published bucket of the day-shape
MIN_PRIOR = 3                   # below this many comparable sessions we say "cannot measure"
BASELINE_SESSIONS = 20
COVER_TOLERANCE_MIN = 5         # a prior session may end one bar short and still be comparable

# Freshness bands, tied to the ACTUAL run cadence (the cloud job fires every 30 min and the
# free feed lags ~15). So a healthy payload tops out around 45 min old, and STALE means a run
# was genuinely MISSED -- not that the clock moved. A check that cries wolf stops being read.
FRESH_LIVE_MIN = 25
FRESH_DELAYED_MIN = 50

ALIVE_RVOL = 1.5                # a DEFAULT, not a finding

# HOW FAR DOES VOLUME NORMALLY MOVE PRICE -- measured, not assumed.
# Fitted on 3,969 name-sessions of this desk's own 5m archive (2026-09-01):
#     log(range_mult) = 0.4511 * log(rvol_cum) - 0.032    R2 = 0.476
# i.e. range scales as roughly the SQUARE ROOT of relative volume, a whisker off the textbook
# law. The first cut of this classifier compared range_mult against 1.0 and duly labelled
# almost every live name ABSORBING -- because 3x volume simply does not buy 3x the range.
RANGE_ELASTICITY = 0.45
# Measured quartiles of the residual, so roughly a quarter of live names land in each tail.
TRAVEL_ABSORB = 0.81            # p25 -- moved less than this much volume usually buys
TRAVEL_DISPLACE = 1.24          # p75 -- moved more

LIQUIDITY_FLOOR_CR = 5.0        # median daily turnover below this is marked THIN, not hidden

# Published in every payload. These are DEFAULTS, not findings -- nothing here has beaten the
# 0.777% round-trip toll, and the strip must never be read as a proven edge. pulse_journal.py
# is what moves them, by measurement rather than by opinion.
THRESHOLDS = {
    "alive_rvol": ALIVE_RVOL,
    "range_elasticity": RANGE_ELASTICITY,
    "travel_absorb": TRAVEL_ABSORB,
    "travel_displace": TRAVEL_DISPLACE,
    "liquidity_floor_cr": LIQUIDITY_FLOOR_CR,
    "now_window_min": NOW_WINDOW_MIN,
    "min_prior_sessions": MIN_PRIOR,
    "status": ("range_elasticity/travel bands are MEASURED on 3969 name-sessions; "
               "alive_rvol and liquidity_floor are UNVALIDATED defaults -- move them from "
               "the journal, never from opinion"),
}


def to_min(hhmm):
    h, m = str(hhmm).split(":")[:2]
    return int(h) * 60 + int(m)


def to_hhmm(mins):
    return "%02d:%02d" % (int(mins) // 60, int(mins) % 60)


def prep(bars):
    """Index one session's bars for O(log n) lookups at any clock time.

    bars: iterable of (hhmm, open, high, low, close, volume).
    Running aggregates are stored per bar so every "as of time t" question is a bisect, not a
    re-scan -- the backfill asks this millions of times and a re-scan makes it unrunnable.
    """
    bb = sorted(bars, key=lambda b: str(b[0]))
    mins, cum, closes, his, los, tov = [], [], [], [], [], []
    run = tot = 0.0
    hh = ll = None
    for b in bb:
        h, l, c = float(b[2]), float(b[3]), float(b[4])
        v = float(b[5] or 0.0)
        hh = h if hh is None else max(hh, h)
        ll = l if ll is None else min(ll, l)
        run += v
        tot += c * v
        mins.append(to_min(b[0]))
        cum.append(run)
        closes.append(c)
        his.append(hh)
        los.append(ll)
        tov.append(tot)
    return {
        "mins": mins, "cum": cum, "close": closes, "bars": bb,
        "high": his, "low": los, "tov": tov,
        "day_turnover": tov[-1] if tov else 0.0,
        "first": mins[0] if mins else None,
        "last": mins[-1] if mins else None,
        "first_open": float(bb[0][1]) if bb else None,
        "last_close": closes[-1] if closes else None,
    }


def _at(p, key, t, default=None):
    i = bisect.bisect_right(p["mins"], t) - 1
    return p[key][i] if i >= 0 else default


def cum_vol(p, t):
    return _at(p, "cum", t, 0.0) or 0.0


def window_vol(p, lo, hi):
    """Volume in [lo, hi] inclusive of the bars stamped at both ends."""
    return (_at(p, "cum", hi, 0.0) or 0.0) - (_at(p, "cum", lo - 1, 0.0) or 0.0)


def covers(p, t):
    """Did this session actually trade through clock time t?

    A prior day whose feed died at 12:00 has a cumulative-to-15:25 equal to its 12:00 value.
    Counting it understates the baseline and INFLATES today's rvol.
    """
    return p["last"] is not None and p["last"] >= t - COVER_TOLERANCE_MIN


def _ratio(today_val, prior_vals):
    """Today over the median of its own comparable priors, or None -- never a guess."""
    pv = [float(x) for x in prior_vals if x and x > 0]
    if len(pv) < MIN_PRIOR or not today_val or today_val <= 0:
        return None
    base = statistics.median(pv)
    return round(today_val / base, 2) if base > 0 else None


def compute(today_bars, prior_bar_lists, t_now=None, want_peak=True):
    """The full per-name measurement. Returns a flat dict, or None if today has no bars.

    prior_bar_lists: chronological, oldest first, EXCLUDING today.
    t_now: clock minute to measure through; defaults to today's last bar.
    want_peak: the peak-window scan is the expensive part. The journal backfill asks this
        question at every 5-minute step of every session and does not need it, so it opts out.

    Bars may be raw tuples OR the output of prep(). The journal preps once and re-uses, which
    turns a re-prep of 20 prior sessions per step into a single one per name-session.
    """
    tp = today_bars if isinstance(today_bars, dict) else prep(today_bars)
    if tp["last"] is None:
        return None
    t = tp["last"] if t_now is None else int(t_now)
    priors = [(b if isinstance(b, dict) else prep(b))
              for b in prior_bar_lists if b is not None][-BASELINE_SESSIONS:]
    el_t = [p for p in priors if covers(p, t)]

    open_lo, open_hi = to_min(OPEN_FROM), to_min(OPEN_TO)
    now_lo = max(open_lo, t - (NOW_WINDOW_MIN - GRID_MIN))
    el_open = [p for p in priors if covers(p, open_hi)]

    # The busiest 30-minute window of the day so far, as a multiple of its own norm. This is
    # what turns "37x" from a claim about the whole day into a claim about a specific half hour
    # the operator can go and look at on a chart.
    width = NOW_WINDOW_MIN - GRID_MIN
    lo, best = open_lo, None
    while want_peak and lo + width <= t:
        hi = lo + width
        el = [p for p in priors if covers(p, hi)]
        rv = _ratio(window_vol(tp, lo, hi), [window_vol(p, lo, hi) for p in el])
        if rv is not None and (best is None or rv > best[0]):
            best = (rv, lo, hi)
        lo += GRID_MIN

    # THE DAY'S SHAPE, not just its three loudest moments. The scan above already visits every
    # window to find the peak, so publishing only the winner throws away information the engine
    # has already paid for. Operator, 2026-09-01: "i hope we are not dismissing any of the
    # information that we gather in between." Non-overlapping buckets, so a busy half hour
    # stands alone instead of smearing across neighbours.
    profile = []
    if want_peak:
        b = to_min(SESSION_OPEN)
        while b <= t:
            b_hi = min(b + PROFILE_MIN - GRID_MIN, t)
            el_b = [p for p in priors if covers(p, b_hi)]
            profile.append({"t": to_hhmm(b),
                            "rv": _ratio(window_vol(tp, b, b_hi),
                                         [window_vol(p, b, b_hi) for p in el_b])})
            b += PROFILE_MIN

    # How far did it TRAVEL, against how far it normally travels by this time of day?
    # 3x volume that goes nowhere and 3x with the range expanding are opposite trades; the old
    # strip printed an identical tile for both.
    def _range_to(p, tt):
        h, l = _at(p, "high", tt), _at(p, "low", tt)
        return (h - l) if (h is not None and l is not None) else None

    rvol_cum = _ratio(cum_vol(tp, t), [cum_vol(p, t) for p in el_t])
    range_mult = _ratio(_range_to(tp, t), [_range_to(p, t) for p in el_t])
    # Did it travel further than THIS MUCH VOLUME normally buys? Comparing range_mult against
    # 1.0 is the wrong question -- volume and range are not proportional (RANGE_ELASTICITY).
    travel = None
    if rvol_cum and rvol_cum > 0 and range_mult:
        travel = round(range_mult / (rvol_cum ** RANGE_ELASTICITY), 2)
    char = None
    if travel is not None:
        char = ("ABSORBING" if travel <= TRAVEL_ABSORB
                else "DISPLACING" if travel >= TRAVEL_DISPLACE else "MIXED")

    # Could the operator actually get size in and out? rvol is unbounded, so an illiquid name
    # printing 37x outranks a liquid one at 11x with no check on tradeability. Measured as the
    # name's OWN median full-session turnover -- a property of the name, not of the clock.
    adv = [p["day_turnover"] for p in priors if p["day_turnover"] > 0]
    adv_cr = round(statistics.median(adv) / 1e7, 2) if len(adv) >= MIN_PRIOR else None

    # WHERE is price, not just how much traded. 3x at the day's high after a gap up and 3x
    # bleeding at the low rendered as the same tile.
    prev_close = priors[-1]["last_close"] if priors else None
    last = _at(tp, "close", t)
    hi_t, lo_t = _at(tp, "high", t), _at(tp, "low", t)
    rng = (hi_t - lo_t) if (hi_t is not None and lo_t is not None) else None
    pv = sum(((float(b[2]) + float(b[3]) + float(b[4])) / 3.0) * float(b[5] or 0.0)
             for b in tp["bars"] if to_min(b[0]) <= t)
    cv = cum_vol(tp, t)
    vw = (pv / cv) if cv > 0 else None

    def _pct(a, b):
        return round((a - b) / b * 100.0, 2) if (a is not None and b) else None

    return {
        "rvol_cum": rvol_cum,
        "gap_pct": _pct(tp["first_open"], prev_close),
        "move_pct": _pct(last, prev_close),
        "pos_in_range": (round((last - lo_t) / rng, 2)
                         if (rng and last is not None and lo_t is not None) else None),
        "vwap": round(vw, 2) if vw else None,
        "vs_vwap_pct": _pct(last, vw),
        "price": round(last, 2) if last is not None else None,
        "day_high": round(hi_t, 2) if hi_t is not None else None,
        "day_low": round(lo_t, 2) if lo_t is not None else None,
        "turnover_cr": round((_at(tp, "tov", t, 0.0) or 0.0) / 1e7, 2),
        "adv_cr": adv_cr,
        "thin": (adv_cr is not None and adv_cr < LIQUIDITY_FLOOR_CR),
        "range_mult": range_mult,
        "travel": travel,
        "char": char,
        "profile": profile,
        "rvol_peak": best[0] if best else None,
        "peak_at": ("%s-%s" % (to_hhmm(best[1]), to_hhmm(best[2] + GRID_MIN))) if best else None,
        "rvol_now": _ratio(window_vol(tp, now_lo, t), [window_vol(p, now_lo, t) for p in el_t]),
        "rvol_open": (_ratio(window_vol(tp, open_lo, open_hi),
                             [window_vol(p, open_lo, open_hi) for p in el_open])
                      if t >= open_hi else None),
        "measured_through": to_hhmm(t),
        "n_prior_used": len(el_t),
    }


def freshness(session_date, last_bar_hhmm, now_dt, today_str=None):
    """How old is this really -- computed, never asserted.

    A freshness check that tests for a date's PRESENCE passes on a misdated payload, and a date
    is not an age. So this returns the measured age in minutes and a state derived from it, and
    the client recomputes both at render time -- a page can be served hours after the emit that
    built it.
    """
    today_str = today_str or now_dt.strftime("%Y-%m-%d")
    if not session_date or not last_bar_hhmm:
        return {"state": "UNKNOWN", "age_min": None,
                "note": "no completed session in the feed -- PULSE is blind, not quiet"}
    if session_date != today_str:
        return {"state": "PRIOR_SESSION", "age_min": None,
                "note": "showing %s -- not today's session" % session_date}
    lb = to_min(last_bar_hhmm)
    stamp = now_dt.replace(hour=lb // 60, minute=lb % 60, second=0, microsecond=0)
    age = int(round((now_dt - stamp).total_seconds() / 60.0))
    nowm = now_dt.hour * 60 + now_dt.minute
    if nowm < to_min(SESSION_OPEN):
        return {"state": "PRE_OPEN", "age_min": age, "note": "the market has not opened"}
    if nowm > to_min(SESSION_CLOSE) + 10:
        # A completed session is COMPLETE, not old. A check that cries wolf every evening
        # stops being read, which is worse than not having it.
        state = "CLOSED" if lb >= to_min(SESSION_CLOSE) - 15 else "TRUNCATED"
        return {"state": state, "age_min": age,
                "note": ("session complete" if state == "CLOSED"
                         else "the feed stopped at %s, before the close" % last_bar_hhmm)}
    state = ("LIVE" if age <= FRESH_LIVE_MIN
             else "DELAYED" if age <= FRESH_DELAYED_MIN else "STALE")
    return {"state": state, "age_min": age,
            "note": "last bar %s, %d min ago" % (last_bar_hhmm, age)}


# What the numbers MEAN, shipped with them. The strip is read by the operator mid-session, not
# by whoever wrote it, and a bare "3.4x" invites the reader to supply their own definition.
MEASURES = {
    "rvol_cum": "volume since the open vs what this name normally trades by this time of day",
    "rvol_now": "the last 30 minutes vs the same 30 minutes on its own normal days -- is it STILL going",
    "rvol_open": "the old 09:20-09:45 opening-range measure, kept for comparison only",
    "rvol_peak": "the busiest half hour of the day so far, and peak_at says when",
    "profile": "the whole session in half-hour buckets, each vs its own norm -- the day's shape",
    "range_mult": "how far it has travelled vs how far it normally travels by now",
    "char": "ABSORBING = it traded a lot and went nowhere · DISPLACING = the volume is genuinely moving price",
    "travel": "how far it moved vs how far this much volume normally moves it (1.0 = normal)",
    "adv_cr": "its own normal full-day turnover in Rs crore -- can you get size in and out",
    "pos_in_range": "0 = sitting on the day's low, 1 = on the day's high",
}


def build_payload(rows, session_date, last_bar, now_dt, n_measured, universe,
                  errors=None, notices=None, extra=None):
    """Assemble the published payload -- ONE definition, used by both producers.

    The freshness block is computed here rather than asserted by each substrate, so the laptop
    and the cloud can never describe the same staleness differently.
    """
    fresh = freshness(session_date, last_bar, now_dt)
    iso = None
    if session_date and last_bar:
        z = now_dt.strftime("%z")
        off = ("%s:%s" % (z[:3], z[3:])) if z else ""
        iso = "%sT%s:00%s" % (session_date, last_bar, off)
    payload = {
        "generated_at": now_dt.isoformat(),
        "as_of_ist": now_dt.strftime("%Y-%m-%d %H:%M IST"),
        "session_date": session_date,
        "bars_through": last_bar,
        # A bare "14:55" makes the client guess a date, and a client that guesses is how a
        # two-session-old payload renders under today's stamp. Ship the whole instant.
        "bars_through_iso": iso,
        "freshness": fresh,
        "thresholds": dict(THRESHOLDS),
        "measures": dict(MEASURES),
        "n_universe": universe,
        "n_measured": n_measured,
        "n_alive": len(rows),
        "rows": rows,
        "errors": list(errors or []),
        "notices": list(notices or []),
        "healthy": not (errors or []),
    }
    payload.update(extra or {})
    return payload


def is_alive(m):
    """On the strip if EITHER the day so far or the last half hour is unusual.

    The `or` is the whole point: cumulative alone misses the 14:00 eruption, and the old
    opening-only rule missed everything after 09:45.
    """
    if not m:
        return False
    return any((m.get(k) or 0) >= ALIVE_RVOL for k in ("rvol_cum", "rvol_now"))
