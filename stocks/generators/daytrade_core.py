#!/usr/bin/env python3
"""daytrade_core.py — single source of truth for the Day-Trade Fires scoring.

WHY THIS EXISTS (F129, 2026-06-09): the fires were computed inside
equity_dashboard_emit.py only, in THREE divergent copies (GENERATORS had the
Rajiv-approved F128 falling-knife logic; web/ + stocks/ were still pre-F128), and
the cloud refresh (refresh_prices.py) never recomputed them at all — so the live
dashboard could sit on multi-session-stale prices with nothing surfacing it.

This module factors the scoring into ONE pure, I/O-free function that BOTH the
vault full-emit AND the CI price-refresh import, so the logic can never drift
across copies again and the CI job can re-score the fires on the latest prices.
The scoring is a verbatim port of the GENERATORS F128 _build_daytrade_panel
(encodes Rajiv's doji->HHHL discipline) — verified byte-identical by
daytrade_parity_test.py.

CONTRACT — build_panel(candidates, ohlc, held_tickers):
  candidates : {ticker: {composite(0-100), multi_hit_count, fresh(bool),
                         lean(+1/0/-1), sector, stddev_10d_pct, vol_spike(bool),
                         vol_spike_days, rsi_today}}
                 -> SCREENER / fundamental + slow-technical layer (deep history;
                    produced by the lens runs, persisted into the aggregate as
                    `daytrade_inputs` so CI can reuse it without the vault).
  ohlc       : {ticker: sorted [(date_iso, open, high, low, close), ...]}
                 -> PRICE-ACTION layer (shallow recent bars; vault feeds it from
                    daily_prices.csv, CI feeds it from live yfinance). The knife /
                    doji / confirmation / d1-d3-d5 reads all come from here, so
                    fresh OHLC == fresh fires.
  held_tickers : iterable of held tickers (for the held flag).

Returns (rows, price_as_of). price_as_of = max bar date used = the freshness
anchor the caller stamps into daytrade_freshness.
"""
from __future__ import annotations

SCORE_VERSION = "F128"  # bump if the scoring math changes


def build_panel(candidates: dict, ohlc: dict, held_tickers=None, top_n: int = 12):
    held = set(held_tickers or [])
    ohlc = ohlc or {}
    rows = []
    price_dates = []
    for tk, m in candidates.items():
        std = m.get("stddev_10d_pct")
        if std is None or std < 1.5:
            continue
        if (m.get("lean") or 0) < 0:        # LONG-ONLY: drop bearish-lean names (no shorting)
            continue
        bars = ohlc.get(tk, []) or []
        closes = [b[4] for b in bars]
        d1 = round((closes[-1] - closes[-2]) / closes[-2] * 100, 2) if len(closes) >= 2 and closes[-2] else None
        d3 = round((closes[-1] - closes[-4]) / closes[-4] * 100, 2) if len(closes) >= 4 and closes[-4] else None
        rsi0 = m.get("rsi_today")
        last_red = last_doji = recent_down = False
        if bars:
            _d0, o0, h0, l0, c0 = bars[-1]
            rng0 = (h0 - l0) or 1.0
            body0 = abs(c0 - o0) / rng0
            last_red = c0 < o0
            last_doji = body0 <= 0.18
        if len(closes) >= 4:
            recent_down = closes[-1] < closes[-3] and closes[-1] < closes[-4]
        # FALLING-KNIFE EXCLUSION (F128, encodes Rajiv 260608) -- fail CLOSED, never no-ops on missing d1:
        knife = False
        if d1 is not None and d1 <= -3:
            knife = True
        if len(closes) >= 4 and closes[-1] < closes[-2] < closes[-3] < closes[-4]:
            knife = True
        if rsi0 is not None and rsi0 < 40 and (last_red or recent_down) and not last_doji:
            knife = True
        if d1 is None and recent_down and not last_doji:
            knife = True
        if knife:
            continue
        vol_spike = bool(m.get("vol_spike")) or (m.get("vol_spike_days", 0) >= 3)
        # confirmation candle from latest daily OHLC bar
        confirmed = False
        if bars:
            _d, o, h, l, c = bars[-1]
            prevc = bars[-2][4] if len(bars) >= 2 else None
            rng = (h - l) or 1.0
            green = c > o
            up = (prevc is not None and c > prevc)
            strong = ((c - l) / rng) >= 0.6
            if green and up and strong:
                confirmed = True
                desc = "Confirmation candle: green, closed strong" + (" on a volume spike" if vol_spike else " (volume light)")
            elif green and up:
                desc = "Up + green but closed mid-range -- wait for a strong-close confirmation candle"
            elif d1 is not None and abs(d1) < 1:
                desc = "Basing -- needs a green confirmation candle to trigger"
            else:
                desc = "No confirmation candle yet -- hold for a green close"
        else:
            desc = "Insufficient candle data -- confirm on the chart"
        if d1 is not None and d1 < -0.5 and not confirmed:
            continue   # down on the day with no reversal candle -> not an upward opportunity
        if recent_down and not confirmed and not last_doji:
            continue   # Rajiv doji->HHHL gate: downtrending names wait for rest(doji)+turn(green)
        d5 = round((closes[-1] - closes[-6]) / closes[-6] * 100, 2) if len(closes) >= 6 and closes[-6] else None
        rsi = m.get("rsi_today")
        mh = m.get("multi_hit_count", 0)
        fresh = bool(m.get("fresh"))
        composite = m.get("composite")
        # FORWARD-LOOKING (F-260605): endorse setups that have NOT yet run; penalise over-extension.
        ext_pen = 0.0
        if rsi is not None and rsi >= 68:
            ext_pen += (rsi - 68) * 1.7
        if d3 is not None and d3 >= 10:
            ext_pen += (d3 - 10) * 1.4
        if d5 is not None and d5 >= 15:
            ext_pen += (d5 - 15) * 0.8
        extended = (rsi is not None and rsi >= 70) and (((d3 or 0) >= 10) or ((d5 or 0) >= 15))
        score = (composite * 0.4
                 + min(std, 5.0) * 3.6
                 + (24 if confirmed else 0)
                 + (10 if (last_doji and not last_red) else 0)
                 + (8 if vol_spike else 0)
                 + (12 if fresh else 0)
                 + (max(0.0, min(d1, 3.0)) * 1.2 if d1 is not None else 0)
                 - ext_pen
                 - (max(0.0, -d1) * 1.5 if d1 is not None else 0))
        if extended:
            bits = []
            if d3 is not None: bits.append(f"+{d3:.0f}% 3d")
            if rsi is not None: bits.append(f"RSI {rsi:.0f}")
            desc = f"⚠ EXTENDED — already ran ({', '.join(bits)}); a chase, not a fresh entry. " + desc
        if bars:
            price_dates.append(bars[-1][0])
        rows.append({
            "ticker": tk, "composite": composite, "lean": m.get("lean", 0),
            "multi_hit_count": mh, "fresh": fresh, "vol_spike": vol_spike,
            "stddev_10d_pct": round(std, 2), "d1": d1, "d3": d3, "d5": d5,
            "confirmed": confirmed, "descriptor": desc, "extended": extended,
            "sector": m.get("sector"), "rsi_today": rsi,
            "held": tk in held, "score": round(score, 1),
        })
    rows.sort(key=lambda r: -r["score"])
    price_as_of = max(price_dates) if price_dates else None
    return rows[:top_n], price_as_of


def assemble_candidates(name_index: dict, universe_meta: dict, prior_mh: dict) -> dict:
    """Merge the lens index + universe_meta into the flat `candidates` map the
    scorer consumes (also what gets persisted as `daytrade_inputs` for CI).
    Mirrors exactly the fields the pre-refactor _build_daytrade_panel read."""
    prior_mh = prior_mh or {}
    out = {}
    for tk, e in name_index.items():
        m = universe_meta.get(tk, {})
        mh = e.get("multi_hit_count", 0)
        out[tk] = {
            "composite": round(e["composite"] * 100, 1),
            "multi_hit_count": mh,
            "fresh": (mh >= 2) and (mh > prior_mh.get(tk, 0)),
            "lean": m.get("lean", 0),
            "sector": m.get("sector"),
            "stddev_10d_pct": m.get("stddev_10d_pct"),
            "vol_spike": bool(m.get("vol_spike")),
            "vol_spike_days": m.get("vol_spike_days", 0),
            "rsi_today": m.get("rsi_today"),
        }
    return out


def _ist_now():
    """Current IST datetime without external deps (UTC + 5:30)."""
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)


def _ist_today():
    """Today's date in IST without external deps (UTC + 5:30)."""
    return _ist_now().date()


# NSE rings the close at 15:30 IST; a settle margin lets the daily bar land before
# we expect to have pulled it.
_NSE_CLOSE_HH, _NSE_CLOSE_MM = 15, 40


def _latest_expected_session(now_ist=None):
    """The most recent NSE *weekday* session whose close has ALREADY happened as of
    now (IST). This is the freshness yardstick: BEFORE today's close (and on
    weekends/over the weekend) today's bar does not exist yet, so the prior trading
    day's close is still CURRENT, not stale. Counting raw `today` as a required
    session from 00:00 IST was the bug that made every pre-close morning read STALE
    and paged the watchdog 8x/day (260611 CC-FIX). A mid-week NSE holiday can still
    over-count by 1 (conservative -- banner shows stale, never hides it), matching
    the prior contract; no holiday calendar is consulted."""
    from datetime import timedelta
    now = now_ist or _ist_now()
    d = now.date()
    closed_today = (now.hour, now.minute) >= (_NSE_CLOSE_HH, _NSE_CLOSE_MM)
    if d.weekday() < 5 and closed_today:
        return d
    d = d - timedelta(days=1)
    while d.weekday() >= 5:          # walk back over Sat/Sun to the prior weekday
        d = d - timedelta(days=1)
    return d


def daytrade_freshness(price_as_of, refreshed_at_utc=None):
    """Compute the fail-loud freshness object the dashboard banner reads.

    sessions_stale = count of NSE *weekday* sessions strictly after price_as_of up
    to & including the latest session whose close has ALREADY happened (IST). 0 =>
    fires are on the latest *available* close -- the freshest data that can exist
    right now. Pre-close and on weekends that latest-available close is yesterday's,
    which is correctly OK rather than STALE. NSE holidays can over-count by 1
    (conservative).
    """
    from datetime import date, datetime, timezone, timedelta
    now = _ist_now()
    today = now.date()
    expected = _latest_expected_session(now)
    paf = None
    if price_as_of:
        try:
            paf = date.fromisoformat(str(price_as_of)[:10])
        except ValueError:
            paf = None
    sessions_stale = None
    if paf:
        sessions_stale = 0
        d = paf
        while d < expected:
            d = d + timedelta(days=1)
            if d.weekday() < 5:   # Mon-Fri
                sessions_stale += 1
    if sessions_stale is None:
        status = "UNKNOWN"
    elif sessions_stale == 0:
        status = "OK"
    else:
        status = "STALE"
    return {
        "price_as_of": str(price_as_of) if price_as_of else None,
        "today_ist": today.isoformat(),
        "expected_session": expected.isoformat(),
        "sessions_stale": sessions_stale,
        "status": status,
        "score_version": SCORE_VERSION,
        "refreshed_at_utc": refreshed_at_utc or datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
