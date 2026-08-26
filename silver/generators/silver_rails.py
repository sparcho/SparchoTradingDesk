#!/usr/bin/env python3
"""silver_rails.py -- the SILVER desk's own instrument + rail registry.

SEPARATE BY DIRECTIVE (operator, 2026-08-26): "fully separate things, lets keep it
clean ... they need to talk to each other, not be the same desk or have the same
engines, besides absolutely basic rules but even then they should be separated."

These rails used to live in the shared `yahoo_common.py`, which the other desk's
cloud fetchers also import -- so adding a silver rail changed what the other desk
pulled. Silver now declares its own universe here.

DELIBERATE DUPLICATION. DXY, TNX, USDINR and USDJPY are watched by both desks and
are declared twice, once per desk. That is the point: each desk can add, drop or
re-map a rail without reaching into the other's engine. If the two lists drift,
they drift because a desk needed them to.

Tests: 00_SYSTEM/TESTS/test_silver_macro_rails.py
"""
from __future__ import annotations

# --- the rails silver actually watches -------------------------------------
# Grouped by what they answer, because a flat list hides why a rail is here.
RAILS = [
    # the metal itself, and the two local instruments the operator trades
    "XAGUSD",      # silver
    "SILVERBEES",  # Nippon India Silver ETF (NSE)
    "SILVER1",     # SEE INSTRUMENT_BASIS -- currently the WRONG instrument
    "GOLD",        # gold: silver's dominant measured co-mover (r=+0.773 over 5y)

    # the dollar
    "DXY",
    "USDINR",
    "USDJPY",      # the yen-carry SYMPTOM; not the Japan rail (see UNFETCHABLE_RAILS)

    # the US yield CURVE. One point cannot tell a policy-rate move from a
    # term-premium move, and for silver those point OPPOSITE ways.
    "US3M",        # 13-week bill  -- the policy end
    "US5Y",        # the belly
    "TNX",         # 10Y
    "US30Y",       # the term-premium / fiscal end (the operator's TYX)

    # energy / geopolitics
    "BRENT",
]

YAHOO_SYMBOL = {
    "XAGUSD":     ["SI=F"],
    "SILVERBEES": ["SILVERBEES.NS"],
    "SILVER1":    ["SILVER1.NS", "SILVERIETF.NS"],
    "GOLD":       ["GC=F"],
    "DXY":        ["DX-Y.NYB"],
    "USDINR":     ["INR=X"],
    "USDJPY":     ["JPY=X"],
    "US3M":       ["^IRX"],
    "US5Y":       ["^FVX"],
    "TNX":        ["^TNX"],
    "US30Y":      ["^TYX"],
    "BRENT":      ["BZ=F"],
}


def yahoo_symbols(rail: str) -> list[str]:
    """Ordered Yahoo symbols to try for a silver-desk rail."""
    return list(YAHOO_SYMBOL.get(rail, [rail]))


# --- what a series ACTUALLY is ---------------------------------------------
# A series' NAME is not a claim about its INSTRUMENT. Two defects forced this:
#
#   * SILVER1 resolves to SILVER1.NS, an NSE silver ETF near Rs.23 -- while every
#     operator chart labelled SILVER1! is MCX Silver Futures near Rs.244,000. Same
#     name, two instruments, a factor of ~10,500 apart. Faithfully fetching the
#     WRONG instrument is invisible to every continuity / dead-feed / splice check,
#     which is how it survived -- and SILVER1 is exactly the series that was
#     EXEMPTED from the dead-feed guard, an exemption that then hid 394 dead rows.
#
#   * XAGUSD and GOLD are LABELLED spot and SOURCED from front-month futures, while
#     the operator's banked levels are read off TVC spot.
#
# Any claim comparing a cached close to a chart-read level must consult this.
INSTRUMENT_BASIS = {
    "XAGUSD":     {"basis": "futures",
                   "note": "SI=F COMEX front-month; the operator charts TVC spot. "
                           "Measured 2026-08-25: SI=F 68.94 vs charted spot 68.775 = +0.24%"},
    "GOLD":       {"basis": "futures",
                   "note": "GC=F front-month; the operator charts TVC spot. Measured "
                           "2026-08-25: GC=F 4703.10 vs charted spot 4642.965 = +1.29%, "
                           "and that basis flows into the published gold/silver ratio"},
    "BRENT":      {"basis": "futures", "note": "BZ=F ICE front-month"},
    "SILVER1":    {"basis": "MISMATCH",
                   "note": "resolves to SILVER1.NS, an NSE silver ETF (~Rs.23) -- NOT the "
                           "MCX Silver Futures (~Rs.244,000) the operator charts as "
                           "SILVER1!. See F260825-SILVERINSTRUMENT."},
    "SILVERBEES": {"basis": "spot-etf",
                   "note": "NSE ETF NAV; carries the India duty/GST premium over global "
                           "spot (F103) -- never read as a clean XAG proxy"},
    # Yields are banded in BASIS POINTS by silver's cache guard, not as a fraction of
    # the prior close: with ^IRX at 0.04% in 2021 a one-basis-point tick is a +25%
    # "step". Every yield rail must be declared here or a price band judges it.
    "US3M":       {"basis": "yield-pct", "note": "^IRX, 13-week bill (discount basis)"},
    "US5Y":       {"basis": "yield-pct", "note": "^FVX, US 5Y"},
    "TNX":        {"basis": "yield-pct", "note": "^TNX, US 10Y"},
    "US30Y":      {"basis": "yield-pct", "note": "^TYX, US 30Y"},
}


def is_yield_series(rail: str) -> bool:
    """True when a rail's cached values are yields in PERCENT, not prices."""
    return (INSTRUMENT_BASIS.get(rail) or {}).get("basis") == "yield-pct"


# --- rails with NO reachable feed ------------------------------------------
# The operator named Japanese yields as actively moving silver. No free JGB feed is
# reachable from this environment (Yahoo has no JGB symbol; stooq serves a JS
# challenge). Leaving them out would make "no Japan rail" indistinguishable from
# "Japan is fine", so they are DECLARED, each with its reason, the caveat on the
# obvious proxy, and a dated manual chart-read pointing at the frame it came from.
UNFETCHABLE_RAILS = {
    "JP10Y": {"why": "no free JGB feed reachable (Yahoo has no JGB symbol; stooq serves "
                     "a JS challenge)",
              "proxy": "USDJPY is the carry SYMPTOM, not the rail -- it moves for dollar "
                       "reasons too",
              "manual_read": {"as_of": "2026-08-25", "value_pct": 2.894,
                              "source": "_CHARTS/260825/JP10Y_2026-08-25_23-09-54.png"}},
    "JP30Y": {"why": "no free JGB feed reachable (Yahoo has no JGB symbol; stooq serves "
                     "a JS challenge)",
              "proxy": "USDJPY is the carry SYMPTOM, not the rail -- it moves for dollar "
                       "reasons too",
              "manual_read": {"as_of": "2026-08-25", "value_pct": 4.062,
                              "source": "_CHARTS/260825/JP30Y_2026-08-25_23-09-59.png"}},
    "US2Y":  {"why": "Yahoo serves no 2Y yield index; 2YY=F is a thin dated futures contract",
              "proxy": "US3M (^IRX) and US5Y (^FVX) bracket it -- the curve is measurable "
                       "without it",
              "manual_read": {"as_of": "2026-08-25", "value_pct": 4.204,
                              "source": "_CHARTS/260825/US02Y_2026-08-25_23-09-29.png"}},
}
