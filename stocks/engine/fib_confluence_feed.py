#!/usr/bin/env python3
"""fib_confluence_feed.py — flatten box_engine's cross-ladder confluences into a
dashboard-ready feed (EDGE/fib_confluences.json) for the Trade Lab confluence grid.

Source: EDGE/box_observations.json (box_engine.py — verified reads only). Engine-detected
confluences surfaced for operator VALIDATION over time (observations, not calls). No fabrication.
"""
from __future__ import annotations
import json
import datetime as _dt
from pathlib import Path

VAULT = Path(__file__).resolve().parents[2]
OBS = VAULT / "00_SYSTEM" / "EDGE" / "box_observations.json"
OUT = VAULT / "00_SYSTEM" / "EDGE" / "fib_confluences.json"

AT_TOL = 0.02  # within ~2% of a confluence = price is "AT" it (matches box_engine BOUNDARY_TOL)


def sma(closes, n):
    """Simple moving average of the last n closes (uses all available if fewer). None if empty."""
    if not closes:
        return None
    window = closes[-n:]
    return sum(window) / len(window)


def ema(closes, n):
    """Exponential moving average (n-period) seeded on the first close. None if empty."""
    if not closes:
        return None
    k = 2.0 / (n + 1.0)
    e = closes[0]
    for c in closes[1:]:
        e = c * k + e * (1.0 - k)
    return e


def ma_stack(closes):
    """The daily EMA/SMA stack computed straight from a close series (no chart needed):
    EMA21 (fast), SMA50 (mid), EMA200 (slow/trend). None entries if no data."""
    return {"ema21": ema(closes, 21), "sma50": sma(closes, 50), "ema200": ema(closes, 200)}


MA_TOL = 0.012   # an EMA/SMA within ~1.2% of the confluence px = it reinforces that level
_MA_LABEL = {"ema21": "EMA21", "sma50": "SMA50", "ema200": "EMA200"}
# a confluence anchored on a longer timeframe measures a bigger move -> structurally weightier
_TF_RANK = {"12M": 12, "6M": 10, "3M": 9, "1M": 7, "1W": 5, "1D": 4, "4H": 3, "1H": 2}


def enrich_point(px, tfs, current_px, ma=None, trend=None, verified=False):
    """Enrich one confluence point with the dynamic, chart-free strength signal:
    MA reinforcement (EMA/SMA sitting at the level), source count, proximity, TF weight, trend
    alignment -> a 0-100 score + grade. This is what makes a confluence 'strong at THIS price'."""
    tfs = tfs or []
    ma = ma or {}
    dist = abs(px - current_px) / current_px if current_px else None
    ma_hits = []
    for key, val in ma.items():
        if val and abs(px - val) / px <= MA_TOL:
            ma_hits.append(_MA_LABEL.get(key, key))
    n_ladders = len(tfs)
    sources = n_ladders + len(ma_hits)
    at = (dist is not None and dist <= 0.02)
    side = ("above" if px > current_px else "below") if current_px else None
    tf_max = max((_TF_RANK.get(t, 3) for t in tfs), default=3)
    # ── transparent 0-100 score ──
    source_score = min(sources, 6) / 6.0 * 55.0                        # 0-55: sources compound
    if dist is None:
        prox = 3.0
    elif dist <= 0.02:
        prox = 25.0
    elif dist <= 0.05:
        prox = 16.0
    elif dist <= 0.10:
        prox = 8.0
    else:
        prox = 3.0                                                     # 0-25: nearer = more actionable
    tf_score = tf_max                                                  # 0-12: structural weight
    align = 0.0                                                        # +/-8: trend alignment
    if trend == "up" and side in ("below", None):
        align = 8.0                                                    # support in an uptrend = buy-the-dip
    elif trend == "down" and side == "above":
        align = 8.0                                                    # resistance in a downtrend = valid cap
    elif trend == "down" and side in ("below", None):
        align = -6.0                                                   # support in a downtrend = a knife
    score = max(0.0, min(100.0, source_score + prox + tf_score + align + (12.0 if verified else 0.0)))
    grade = "STRONG" if (sources >= 3 or (sources >= 2 and len(ma_hits) >= 1)) else ("SECONDARY" if sources >= 2 else "WATCH")
    return {"px": px, "tfs": tfs, "n_ladders": n_ladders, "ma_hits": ma_hits, "sources": sources,
            "dist_pct": round(dist * 100, 2) if dist is not None else None, "at": at, "side": side,
            "tf_max": tf_max, "score": round(score), "grade": grade, "verified": bool(verified)}


def _read_obs():
    if OBS.exists():
        try:
            return json.loads(OBS.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"observations": []}


HIST_CSV = VAULT / "00_SYSTEM" / "GENERATORS" / "_cache" / "historical_ohlc.csv"
DAILY_CSV = VAULT / "00_SYSTEM" / "GENERATORS" / "_cache" / "daily_prices.csv"


def ohlc_from_csv(hist_csv, daily_csv=None):
    """Per-ticker close series (date-sorted) from the historical OHLC cache + today's live close,
    so the MA stack reflects the LATEST price on every refresh. {ticker: [close, ...]}."""
    import csv
    series = {}
    if hist_csv and Path(hist_csv).exists():
        rows = {}
        with open(hist_csv, newline="", encoding="utf-8", errors="ignore") as fh:
            for r in csv.DictReader(fh):
                t = r.get("ticker")
                try:
                    rows.setdefault(t, []).append((r["date"], float(r["close"])))
                except (TypeError, ValueError):
                    pass
        for t, rr in rows.items():
            rr.sort(key=lambda x: x[0])
            series[t] = [c for _, c in rr]
    if daily_csv and Path(daily_csv).exists():
        with open(daily_csv, newline="", encoding="utf-8", errors="ignore") as fh:
            for r in csv.DictReader(fh):
                t = r.get("ticker")
                try:
                    series.setdefault(t, []).append(float(r["close"]))
                except (TypeError, ValueError):
                    pass
    return series


def _read_ohlc():
    return ohlc_from_csv(HIST_CSV, DAILY_CSV)


def _trend_of(cur, ma):
    """Price vs the daily MA stack (chart-free): above all -> up, below all -> down, else mixed."""
    vals = [v for v in (ma or {}).values() if v]
    if not vals or cur is None:
        return "unknown"
    if all(cur > v for v in vals):
        return "up"
    if all(cur < v for v in vals):
        return "down"
    return "mixed"


def _price_as_of():
    """The session date the scoring prices came from — read from the price cache the feed
    actually scored against, never assumed to be 'today'."""
    try:
        import csv as _csv
        rows = list(_csv.DictReader(DAILY_CSV.open(encoding="utf-8")))
        ds = sorted({r.get("date") for r in rows if r.get("date")})
        return ds[-1] if ds else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# F260721-FIBPOLARITY - a cluster is not "support" merely because its representative
# price rounded to the low side of spot. Three data-driven corrections live below:
# straddle detection, supply-polarity/acceptance, and MA-stretch (extension) penalty.
# ---------------------------------------------------------------------------
import re as _re

# Member strings carry their own prices ("fib-1.618 1M 1049.70", "recent ATH supply ~1085-1100").
# Keep only tokens that could plausibly BE a price for this name - a fib RATIO (1.618, 3.115) or a
# timeframe digit (12M -> 12) is not a level. The band is deliberately wide (a quarter to 4x spot):
# it only has to reject ratios/timeframes, never a real level.
# The negative lookbehind drops an indicator PERIOD glued to its name (EMA200 -> not 200,
# SMA50 -> not 50); a real level is always separated from any preceding letters.
_PRICE_TOKEN = _re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?")
_PLAUSIBLE_LO, _PLAUSIBLE_HI = 0.25, 4.0


def member_prices(pt, current_px):
    """Every price a confluence CLUSTER is actually built from. When the operator has BANKED an
    explicit lo/hi that band IS the cluster and is returned as-is (their drawn boundary beats any
    number we could scrape out of a label). Otherwise the prices are parsed from the member/source
    strings, with ratios, timeframe digits and indicator periods filtered out."""
    out = []
    zone = pt.get("zone") or {}
    if zone.get("lo") is not None and zone.get("hi") is not None:
        return [float(zone["lo"]), float(zone["hi"])]
    if current_px:
        for s in (pt.get("tfs") or []):
            for tok in _PRICE_TOKEN.findall(str(s)):
                v = float(tok)
                if _PLAUSIBLE_LO * current_px <= v <= _PLAUSIBLE_HI * current_px:
                    out.append(v)
    return sorted(set(out))


# SUPPLY POLARITY. A zone whose members describe SELLING (an all-time-high shelf, a supply/
# distribution band, a resistance/target/trim shelf) is not support just because price sits near it.
# Matched on WORD BOUNDARIES against the cluster's own member strings + the banked zone's note, so
# "ath" cannot fire from "path"/"breath" and "cap" cannot fire from "capital"/"capacity".
SUPPLY_TERMS = ("ath", "all-time-high", "supply", "resistance", "distribution", "overhead",
                "cap", "trim", "target", "swing-high", "sell")
_SUPPLY_RE = _re.compile(r"\b(?:%s)\b" % "|".join(_re.escape(t) for t in SUPPLY_TERMS),
                         _re.IGNORECASE)

# ACCEPTANCE. A supply band FLIPS to support only once price has demonstrably accepted above it.
# margin = AT_TOL (2%): inside 2% the level is still "AT" price by this feed's own definition, so
# only a close clear of that band counts as beyond it. sessions = 3: one close above is a poke and
# two can be a single 2-day thrust; three consecutive daily closes is the shortest window that
# cannot be produced by one impulse candle plus its follow-through. Daily closes are all the cache
# (historical_ohlc.csv / daily_prices.csv) gives us - no intraday, so no tighter test is honest.
ACCEPT_MARGIN = AT_TOL
ACCEPT_SESSIONS = 3


def has_supply_semantics(members):
    """True if ANY member string of this cluster describes supply/distribution (word-boundary)."""
    return any(_SUPPLY_RE.search(str(m)) for m in (members or []))


def accepted_above(closes, level, band_lo=None, margin=ACCEPT_MARGIN, sessions=ACCEPT_SESSIONS):
    """Has price ACCEPTED above `level`, and does that acceptance still stand?

    Acceptance = a run of `sessions` CONSECUTIVE daily closes each at least `margin` clear of the
    level. It is checked over the whole recorded history, not just the last N closes, because the
    prime case - break out, then RETEST the broken band from above - necessarily brings price back
    toward the level. Acceptance is then VOIDED if price has since closed back below the band
    (`band_lo`), i.e. it fell back inside/through what it broke. No closes on record -> False
    (unproven is not proven)."""
    if not closes or level is None or len(closes) < sessions:
        return False
    gate = level * (1.0 + margin)
    floor = band_lo if band_lo is not None else level
    run, accepted_at = 0, None
    for i, c in enumerate(closes):
        run = run + 1 if c > gate else 0
        if run >= sessions:
            accepted_at = i
    if accepted_at is None:
        return False
    return all(c > floor for c in closes[accepted_at + 1:])


def cluster_members(pt):
    """Everything this cluster says about itself: its member/source strings + the banked zone's
    note and strength label. This is what the supply-semantics match reads."""
    zone = pt.get("zone") or {}
    return [str(m) for m in (pt.get("tfs") or [])] +            [str(zone.get(k)) for k in ("note", "strength") if zone.get(k)]


def classify_point(pt, current_px, closes=None):
    """Re-classify an enriched point against the WHOLE cluster it stands for (not just its
    representative px), attaching `span`, `supply`/`accepted_above`, a corrected `side`, and
    explanatory `flags`."""
    flags = pt.setdefault("flags", [])
    prices = member_prices(pt, current_px)
    span = [min(prices), max(prices)] if prices else None
    pt["span"] = span
    if span and current_px and span[0] < current_px < span[1]:
        # price is INSIDE the band the cluster describes - neither support nor resistance
        pt["side"] = "in-zone"
        if "straddles-spot" not in flags:
            flags.append("straddles-spot")
    supply = has_supply_semantics(cluster_members(pt))
    pt["supply"] = supply
    top_of_band = span[1] if span else pt.get("px")
    band_lo = span[0] if span else None
    pt["accepted_above"] = accepted_above(closes, top_of_band, band_lo) if supply else None
    if supply:
        if pt["accepted_above"]:
            # polarity flip CONFIRMED: old supply, accepted above, is now valid support
            flags.append("polarity-flip-accepted")
        else:
            flags.append("supply-unconfirmed")
    return pt


# EXTENSION. The evidence that a move has ALREADY HAPPENED is distance from the moving averages -
# NOT proximity to the all-time high (a fresh breakout is at its high BY DEFINITION and deserves no
# penalty). Thresholds, and why:
#   EXT_FREE 5%  - a name can sit ~5% over its EMA21 inside normal trend noise; below this there is
#                  nothing to penalise. (BAJFINANCE 21-Jul: +5.9% EMA21 / +10.9% SMA50 = stretched.)
#   EXT_SLOPE 200 - 2 score points per 1% of stretch beyond the free band; linear and legible.
#   EXT_MAX 15   - caps the penalty at ~1 grade band so extension DEMOTES a name, never erases it.
#   EXT_RETEST_TOL = AT_TOL (2%) - the breakout-retest exemption. Price back ON its EMA21 has, by
#                  construction, given the move back to the mean; the run is no longer "already
#                  had" regardless of how far the slower SMA50 trails or how near the high it is.
EXT_FREE = 0.05
EXT_SLOPE = 200.0
EXT_MAX = 15.0
EXT_RETEST_TOL = AT_TOL


def extension_penalty(current_px, ma):
    """How far price has run from its mean, as a 0-EXT_MAX score penalty. Uses the FAST (EMA21) and
    MID (SMA50) averages only - EMA200 is a trend filter, not a stretch gauge."""
    ma = ma or {}
    e21, s50 = ma.get("ema21"), ma.get("sma50")
    ext21 = (current_px - e21) / e21 if (e21 and current_px) else None
    ext50 = (current_px - s50) / s50 if (s50 and current_px) else None
    stretch = max([x for x in (ext21, ext50) if x is not None], default=0.0)
    exempt = ext21 is not None and abs(ext21) <= EXT_RETEST_TOL
    penalty = 0.0 if (exempt or stretch <= EXT_FREE) else min(EXT_MAX, (stretch - EXT_FREE) * EXT_SLOPE)
    return {"ext21_pct": round(ext21 * 100, 2) if ext21 is not None else None,
            "ext50_pct": round(ext50 * 100, 2) if ext50 is not None else None,
            "stretch_pct": round(stretch * 100, 2), "exempt": bool(exempt),
            "penalty": round(penalty)}


def _setup_of(top, trend):
    """Terse actionable read for a name, from its top-ranked confluence point + trend.

    Polarity-aware (F260721-FIBPOLARITY): a cluster that STRADDLES spot is neither support nor
    resistance, and a cluster carrying supply semantics is not support until price has ACCEPTED
    above it. `side` alone no longer decides."""
    if not top:
        return "watch"
    side, at = top.get("side"), top.get("at")
    unflipped_supply = bool(top.get("supply")) and not top.get("accepted_above")
    if side == "in-zone":                          # price INSIDE the band the cluster describes
        return "in-supply-zone" if unflipped_supply else "in-zone"
    if unflipped_supply and side in ("below", None):
        # supply price has not accepted above is not a floor, however near it sits
        return "at-supply" if at else "approaching-supply"
    if trend == "down" and side in ("below", None):
        return "falling-knife"                     # support in a downtrend — stand aside
    if not at:
        return "approaching"                       # not at the level yet — watch
    if side == "above":
        return "at-resistance"
    if trend == "up":
        return "buy-support"                       # AT a support confluence in an uptrend
    return "at-support"


def _why_of(cur, top, trend, ext, base_score, score):
    """Short, machine-readable reasoning for a name's score + setup. The operator's standing rule is
    that a mechanical screen output is NOT a verdict (and V-03 requires every level to state its
    WHY), so the payload carries the reasoning that produced it."""
    w = ["trend=%s vs the daily EMA21/SMA50/EMA200 stack" % trend]
    span, side = top.get("span"), top.get("side")
    band_top = span[1] if span else top.get("px")
    if side == "in-zone" and span:
        w.append("top cluster %.2f-%.2f STRADDLES spot %.2f — price is INSIDE the band, so it is "
                 "neither support nor resistance" % (span[0], span[1], cur))
    elif top.get("dist_pct") is not None:
        w.append("top cluster px %.2f sits %s spot, %.2f%% away"
                 % (top.get("px"), side or "at", top["dist_pct"]))
    if top.get("supply"):
        if top.get("accepted_above"):
            w.append("supply/ATH wording, but price ACCEPTED above %.2f (%d+ closes %.0f%% clear, not "
                     "since lost) — POLARITY FLIPPED, valid support"
                     % (band_top, ACCEPT_SESSIONS, ACCEPT_MARGIN * 100))
        else:
            w.append("cluster members carry SUPPLY semantics (ATH/target/resistance wording) and price "
                     "has NOT accepted above %.2f — still supply, not support" % band_top)
    if ext["penalty"]:
        w.append("extended: %+.1f%% vs EMA21 / %+.1f%% vs SMA50 — the move has already happened "
                 "(-%d)" % (ext["ext21_pct"] or 0.0, ext["ext50_pct"] or 0.0, ext["penalty"]))
    elif ext["exempt"]:
        w.append("sitting on its EMA21 (%+.1f%%) — breakout-retest exemption, no extension penalty"
                 % (ext["ext21_pct"] or 0.0))
    w.append("score %d = %d cluster strength - %d extension" % (score, base_score, ext["penalty"]))
    return w


BANK_DIR = VAULT / "00_SYSTEM" / "EDGE" / "LEVELS"


def _bank_zones(ticker):
    """The operator's BANKED verified zones for a ticker (their explicit strong support/supply
    bands). [] if no bank / no zones."""
    if not ticker:
        return []
    fp = BANK_DIR / (str(ticker) + ".json")
    if not fp.exists():
        return []
    try:
        return (json.loads(fp.read_text(encoding="utf-8")) or {}).get("zones") or []
    except Exception:
        return []


def _zone_point(zone, current_px, ma, trend):
    """Turn a banked zone (lo/hi/sources) into a VERIFIED confluence point at the band edge nearest
    to price (support -> top edge, resistance -> bottom edge, inside -> midpoint)."""
    lo, hi = zone.get("lo"), zone.get("hi")
    if lo is None or hi is None or not current_px:
        return None
    if hi < current_px:
        px = hi
    elif lo > current_px:
        px = lo
    else:
        px = round((lo + hi) / 2.0, 2)
    srcs = [str(s) for s in (zone.get("sources") or [])]
    pt = enrich_point(px, srcs or ["zone"], current_px, ma, trend, verified=True)
    pt["zone"] = {"lo": lo, "hi": hi, "strength": zone.get("strength"),
                  "note": zone.get("note")}
    return pt


def _struct_strength(p):
    """Structural strength IGNORING proximity: sources + verified conviction + timeframe weight.
    Used to pick the strongest support/resistance to act on, not merely the nearest."""
    return p.get("sources", 0) * 10 + (12 if p.get("verified") else 0) + p.get("tf_max", 3)


def _key_level(pts, want):
    """Strongest support (want='below') or resistance (want='above') by structural strength, tiebreak
    nearest. Points AT price qualify for either side."""
    sides = ("below", None) if want == "below" else ("above",)
    cand = [p for p in pts if p.get("side") in sides]
    if not cand:
        return None
    best = max(cand, key=lambda p: (_struct_strength(p), -(p["dist_pct"] if p["dist_pct"] is not None else 1e9)))
    return {"px": best["px"], "score": best["score"], "sources": best["sources"],
            "verified": bool(best.get("verified")), "zone": best.get("zone"),
            "tfs": best.get("tfs"), "ma_hits": best.get("ma_hits"), "grade": best.get("grade")}


def build(obs=None, ohlc=None, write=True):
    if obs is None:
        obs = _read_obs()
    if ohlc is None:
        ohlc = _read_ohlc()
    names = []
    total_points = 0
    for o in obs.get("observations", []):
        cur = o.get("current_px")
        confl = o.get("confluence") or []
        if not cur or not confl:
            continue
        ma = ma_stack(ohlc.get(o.get("ticker"), []))
        trend = _trend_of(cur, ma)
        pts = []
        for c in confl:
            px = c.get("px")
            if px is None:
                continue
            pts.append(enrich_point(px, c.get("tfs") or [], cur, ma, trend))
        # ingest the operator's banked verified zones as strength-weighted points (dedup vs an
        # auto confluence within ~1.5%: keep the higher score, but carry the verified flag forward).
        for zp in filter(None, (_zone_point(z, cur, ma, trend) for z in _bank_zones(o.get("ticker")))):
            dup = next((p for p in pts if p["dist_pct"] is not None
                        and abs(zp["px"] - p["px"]) / cur <= 0.015), None)
            if dup is None:
                pts.append(zp)
            elif zp["score"] > dup["score"]:
                pts[pts.index(dup)] = zp
            else:
                dup["verified"] = True
        if not pts:
            continue
        # F260721-FIBPOLARITY — re-classify every point against the WHOLE cluster it stands
        # for (straddle + supply polarity/acceptance) before anything ranks or labels it.
        closes = ohlc.get(o.get("ticker"), [])
        for pt in pts:
            classify_point(pt, cur, closes)
        # rank AT-first (verdict reflects where price IS), then strength, then nearest. Strength-based
        # key_support/key_resistance below surface the strongest ZONES for the actionable level.
        pts.sort(key=lambda p: (not p["at"], -p["score"], p["dist_pct"] if p["dist_pct"] is not None else 1e9))
        top = pts[0]
        total_points += len(pts)
        ext = extension_penalty(cur, ma)
        base_score = top["score"]
        score = max(0, base_score - ext["penalty"])
        names.append({
            "ticker": o.get("ticker"),
            "current_px": round(cur, 2),
            "n_points": len(pts),
            "at_confluence": any(p["at"] for p in pts),
            "nearest_dist_pct": min(p["dist_pct"] for p in pts if p["dist_pct"] is not None),
            "trend": trend,
            "score": score,
            "base_score": base_score,
            "grade": top["grade"],
            "setup": _setup_of(top, trend),
            "flags": list(top.get("flags") or []),
            "ext": ext,
            "why": _why_of(cur, top, trend, ext, base_score, score),
            "ma": {k: (round(v, 2) if v else None) for k, v in ma.items()},
            "points": pts,
            "key_support": _key_level(pts, "below"),
            "key_resistance": _key_level(pts, "above"),
        })
    # most actionable first: highest strength score, then price-AT, then nearest
    names.sort(key=lambda n: (-n["score"], not n["at_confluence"], n["nearest_dist_pct"]))
    # F260721-FIBPROV — PROVENANCE. This payload shipped with NO date field of any kind, so
    # its staleness was UNFALSIFIABLE: the hero cards sat 2 sessions stale (priced on the
    # 17-Jul close, worst card 9.7% off live) and nothing in the system could prove it. The
    # card even claimed "live on every price refresh", which is false even when healthy —
    # the feed rebuilds ~2x/day and a pre-open build scores against the PRIOR close.
    # Declare the basis so the UI can render it and the block contract can judge it.
    _px_asof = _price_as_of()
    payload = {"n_names": len(names), "n_points": total_points,
               "n_at": sum(1 for n in names if n["at_confluence"]),
               "price_as_of": _px_asof,
               "generated_at_utc": _dt.datetime.now(_dt.timezone.utc)
                                      .isoformat(timespec="seconds"),
               "basis": "last close (%s) - NOT intraday live" % (_px_asof or "unknown"),
               "names": names,
               "note": "Engine-detected cross-ladder fib-box confluences from verified banks "
                       "(box_engine.py). Surfaced for operator validation over time — observations, not "
                       "calls. Every px = a banked ladder's box edge; distance is vs the live price. "
                       "AT = within ~2% of the confluence.",
               "method": "fib-box confluence = box edges from TWO+ different ladders within ~1.5% of each other"}
    if write:
        try:
            OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return payload


if __name__ == "__main__":
    p = build(write=True)
    print(f"fib_confluences: {p['n_names']} names · {p['n_points']} confluence points")
