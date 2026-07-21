#!/usr/bin/env python3
"""box_engine.py — the multi-ladder FIB-BOX monitor (F260714, operator legend 2026-07-14).

THE BOX STRATEGY, CORRECTLY (supersedes box_highlighter.py, which used the wrong object):
  The "boxes" are the FIB tool's own boxes — the band from the .115 to the .618 rung at each
  extension of the operator's custom preset: BOX1=1.115->1.618, BOX2=2.115->2.618, BOX3=3.115->3.618,
  BOX4=4.115->4.618. They are computed straight from a ladder's 0->1 anchor (no drawing needed).
  These are NOT the operator's colored confluence/watch/buy zones (his interpretive layer).

Operator rules baked in (memory box-strategy-and-fib-legend):
  - MULTI-LADDER: track price vs ALL a name's ladders' boxes at once — price can react to an old/long-TF
    fib AND a recent fib simultaneously. Longer-TF ladders weighted higher (they measure the bigger move).
  - Fresh-ticker exception: names with no usable long-TF leg use whatever ladder exists.
  - CONFLUENCE = where boxes from TWO DIFFERENT ladders overlap/tighten (= higher conviction).
  - VERIFIED reads ONLY (operator does not trust METHOD-B auto reads yet).
  - DIVERGENT observation: surface anything notable (2-box move +/- retrace, long consolidation in a box,
    boundary test, price-not-responding, price-left-all-zones=re-draw trigger). DON'T hard-code "positive"
    this early. Box-vs-stack conflicts are flagged as caution, never as clean-positive.
  - P0-ON-DISCREPANCY: anything that doesn't compute / strays from the method is flagged P0, never hidden.

Deterministic, unit-tested (TESTS/test_box_engine.py).
"""
from __future__ import annotations
import csv
import json
from pathlib import Path

VAULT = Path(__file__).resolve().parents[2]
BANK_DIR = VAULT / "00_SYSTEM" / "EDGE" / "LEVELS"
HIST_CSV = VAULT / "00_SYSTEM" / "GENERATORS" / "_cache" / "historical_ohlc.csv"
DAILY_CSV = VAULT / "00_SYSTEM" / "GENERATORS" / "_cache" / "daily_prices.csv"
OUT_JSON = VAULT / "00_SYSTEM" / "EDGE" / "box_observations.json"

# the 4 fib boxes = the .115 -> .618 band at each extension (operator legend 260714)
BOX_PAIRS = [(1.115, 1.618, "BOX1"), (2.115, 2.618, "BOX2"),
             (3.115, 3.618, "BOX3"), (4.115, 4.618, "BOX4")]
# longer timeframe = bigger move = higher weight (operator: prefer long-TF fibs for trades)
TF_WEIGHT = {"12M": 5.0, "6M": 4.0, "3M": 3.5, "1M": 3.0, "1W": 2.0, "1D": 1.0, "4H": 0.5, "1H": 0.3}
CONFLUENCE_TOL = 0.015     # two ladders' box edges within ~1.5% = a confluence
BOUNDARY_TOL = 0.02        # price within ~2% of a box edge = "at a boundary"
CONSOLIDATION_N = 15       # >= this many bars in one box = notable consolidation
LEAP_LOOKBACK = 45


def boxes_from_anchor(low, high):
    """The 4 fib boxes for one ladder anchored at 0=low, 1=high. [] if degenerate (caller flags P0)."""
    try:
        low = float(low); high = float(high)
    except (TypeError, ValueError):
        return []
    rng = high - low
    if rng <= 0:
        return []
    return [{"lo": round(low + rlo * rng, 2), "hi": round(low + rhi * rng, 2), "label": lbl}
            for rlo, rhi, lbl in BOX_PAIRS]


def tf_weight(tf):
    return TF_WEIGHT.get((tf or "").upper().strip(), 1.0)


def price_state(price, boxes):
    """Where price sits on ONE ladder's box ladder: which box it's inside, or the gap/zone it's in,
    plus how many boxes it has cleared (its 'rung')."""
    if not boxes:
        return {"where": "no-boxes", "in_box": None, "cleared": 0}
    cleared = sum(1 for b in boxes if b["hi"] < price)
    inside = next((b["label"] for b in boxes if b["lo"] <= price <= b["hi"]), None)
    if inside:
        where = "in " + inside
    elif price < boxes[0]["lo"]:
        where = "below all boxes (still in the base leg)"
    elif price > boxes[-1]["hi"]:
        where = "above all boxes (left the ladder behind)"
    else:
        # in a gap between two boxes
        below = [b["label"] for b in boxes if b["hi"] < price][-1:]
        above = [b["label"] for b in boxes if b["lo"] > price][:1]
        where = "gap between %s and %s" % (below[0] if below else "base", above[0] if above else "top")
    return {"where": where, "in_box": inside, "cleared": cleared}


def ladder_boxes(anchor):
    """{timeframe, low_px, high_px} -> {tf, weight, boxes} (or None if degenerate)."""
    boxes = boxes_from_anchor(anchor.get("low_px"), anchor.get("high_px"))
    if not boxes:
        return None
    tf = anchor.get("timeframe")
    return {"tf": tf, "weight": tf_weight(tf), "boxes": boxes,
            "anchor": [anchor.get("low_px"), anchor.get("high_px")]}


def cross_confluence(ladders, tol=CONFLUENCE_TOL):
    """Where box EDGES from TWO DIFFERENT ladders sit within tol of each other = a confluence point
    (the operator's colored zones' derivation). Returns [{px, tfs, edges}]."""
    edges = []                       # (px, tf, label-edge)
    for L in ladders:
        for b in L["boxes"]:
            edges.append((b["lo"], L["tf"], b["label"] + ".lo"))
            edges.append((b["hi"], L["tf"], b["label"] + ".hi"))
    edges.sort(key=lambda e: e[0])
    out = []
    used = [False] * len(edges)
    for i in range(len(edges)):
        if used[i]:
            continue
        cluster = [edges[i]]
        for j in range(i + 1, len(edges)):
            if edges[j][0] - edges[i][0] <= edges[i][0] * tol:
                cluster.append(edges[j]); used[j] = True
            else:
                break
        tfs = {c[1] for c in cluster}
        if len(tfs) >= 2:            # must be from >= 2 DIFFERENT ladders
            pxs = [c[0] for c in cluster]
            out.append({"px": round(sum(pxs) / len(pxs), 2), "tfs": sorted(tfs),
                        "edges": [c[2] for c in cluster], "n_ladders": len(tfs)})
    return out


def _box_path(prices, boxes):
    return [price_state(p, boxes)["cleared"] for p in prices]


def observe(read, closes, current_px):
    """Full multi-ladder observation for one verified name. Returns {ticker, states[], confluence[],
    notes[], p0[]} — DIVERGENT (surfaces anything notable, doesn't hard-filter to one 'positive' rule)."""
    if not read:
        return None
    p0 = []
    if not read.get("verified"):
        # engine is verified-only; an unverified read reaching here is itself a discrepancy
        p0.append("read not verified — box engine must run on operator-verified reads only")
    anchors = read.get("fib_anchors") or []
    ladders, incomplete = [], []
    for a in anchors:
        lo, hi = a.get("low_px"), a.get("high_px")
        lb = ladder_boxes(a)
        if lb is not None:
            ladders.append(lb)
        elif lo is None or hi is None:
            # a missing 0/1 point = PENDING per V-01 (illegible/undrawn) — info, not a discrepancy
            incomplete.append("incomplete fib anchor %s (missing %s point, PENDING per V-01)"
                              % (a.get("timeframe"), "0/low" if lo is None else "1/high"))
        else:
            # both present but low>=high = a genuine INVERSION -> P0 (a real data discrepancy)
            p0.append("inverted fib anchor %s (low %s >= high %s) — impossible, needs fixing" % (a.get("timeframe"), lo, hi))
    if not ladders:
        return {"ticker": read.get("ticker"), "states": [], "confluence": [], "notes": [],
                "incomplete": incomplete, "p0": p0 or ["no usable fib ladder to build boxes"]}

    states, notes = [], []
    for L in sorted(ladders, key=lambda x: -x["weight"]):     # longer-TF (higher weight) first
        st = price_state(current_px, L["boxes"])
        # boundary proximity (nearest box edge)
        nearest = min(((abs(e - current_px) / current_px, lbl, e)
                       for b in L["boxes"] for lbl, e in ((b["label"] + " floor", b["lo"]),
                                                          (b["label"] + " ceiling", b["hi"]))),
                      key=lambda t: t[0])
        at_boundary = nearest[0] <= BOUNDARY_TOL
        states.append({"tf": L["tf"], "weight": L["weight"], "where": st["where"],
                       "cleared": st["cleared"], "at_boundary": at_boundary,
                       "nearest_edge": {"what": nearest[1], "px": round(nearest[2], 2),
                                        "dist_pct": round(nearest[0] * 100, 2)}})
        # divergent notes per ladder
        if st["where"].startswith("above all"):
            notes.append("[%s] price has LEFT ALL BOXES behind — candidate for a fib RE-DRAW (op rule Q5)" % L["tf"])
        if at_boundary:
            notes.append("[%s] price AT a box boundary: %s (%.2f%%)" % (L["tf"], nearest[1], nearest[0] * 100))
        # leap / retrace / consolidation from the recent path
        if closes and len(closes) >= 5:
            path = _box_path(closes[-LEAP_LOOKBACK:], L["boxes"])
            peak, cur, base = max(path), path[-1], min(path[:path.index(max(path)) + 1])
            if peak - base >= 2:
                if peak - cur >= 1 and cur > base:
                    notes.append("[%s] %d-box LEAP then retraced %d (now cleared %d)" % (L["tf"], peak - base, peak - cur, cur))
                elif cur >= peak:
                    notes.append("[%s] %d-box LEAP holding with NO retrace (strong continuation?)" % (L["tf"], peak - base))
            # consolidation: same cleared-rung for a long stretch at the tail
            tail = path[-CONSOLIDATION_N:]
            if len(tail) >= CONSOLIDATION_N and len(set(tail)) == 1 and st["in_box"]:
                notes.append("[%s] CONSOLIDATING in %s for %d+ sessions" % (L["tf"], st["in_box"], CONSOLIDATION_N))

    confl = cross_confluence(ladders)
    # highlight confluence the price is currently AT (highest conviction: fib boxes of 2 ladders + price)
    for c in confl:
        if abs(c["px"] - current_px) / current_px <= BOUNDARY_TOL:
            notes.append("** price AT a CROSS-LADDER CONFLUENCE %.2f (%s) -- higher conviction" % (c["px"], "+".join(c["tfs"])))

    return {"ticker": read.get("ticker"), "current_px": round(current_px, 2),
            "n_ladders": len(ladders), "states": states, "confluence": confl,
            "notes": notes, "incomplete": incomplete, "p0": p0}


# ── I/O ──────────────────────────────────────────────────────────────────────
def _recent_closes(tkr, n=LEAP_LOOKBACK):
    closes = []
    if HIST_CSV.exists():
        with open(HIST_CSV, newline="", encoding="utf-8", errors="ignore") as fh:
            for row in csv.DictReader(fh):
                if row.get("ticker") == tkr:
                    try:
                        closes.append((row["date"], float(row["close"])))
                    except (TypeError, ValueError):
                        pass
    closes.sort(key=lambda r: r[0])
    series = [c for _, c in closes]
    live = None
    if DAILY_CSV.exists():
        with open(DAILY_CSV, newline="", encoding="utf-8", errors="ignore") as fh:
            rows = [r for r in csv.DictReader(fh) if r.get("ticker") == tkr and r.get("close")]
        if rows:
            try:
                live = float(rows[-1]["close"])
            except (TypeError, ValueError):
                live = None
    if live is not None:
        series = series + [live]
    return series[-n:], (live if live is not None else (series[-1] if series else None))


def _load_verified(tkr):
    fp = BANK_DIR / f"{tkr}.json"          # verified bank only (NOT _auto)
    if fp.exists():
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
            return d if d.get("verified") else None
        except Exception:
            return None
    return None


def build_all(write=True):
    obs, p0_items = [], []
    for fp in sorted(BANK_DIR.glob("*.json")):
        read = _load_verified(fp.stem)
        if not read:
            continue
        closes, cur = _recent_closes(fp.stem)
        if cur is None:
            p0_items.append({"ticker": fp.stem, "p0": ["no live price to place against boxes"]})
            continue
        o = observe(read, closes, cur)
        if o:
            if o.get("p0"):
                p0_items.append({"ticker": fp.stem, "p0": o["p0"]})
            if o.get("notes") or o.get("confluence"):
                obs.append(o)
    # rank: names with a ★ confluence note first, then by number of notes
    obs.sort(key=lambda o: (not any(n.startswith("**") for n in o["notes"]), -len(o["notes"])))
    payload = {"n": len(obs), "observations": obs, "p0": p0_items,
               "note": "multi-ladder fib-box monitor (verified reads only). Boxes = .115->.618 bands per "
                       "ladder; confluence = cross-ladder box overlap. DIVERGENT: surfaces anything notable, "
                       "no hard 'positive' filter. P0 = data/method discrepancies to clarify."}
    if write:
        try:
            OUT_JSON.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return payload


if __name__ == "__main__":
    p = build_all(write=True)
    print(f"box observations: {p['n']} verified names with something notable | P0 items: {len(p['p0'])}")
    for o in p["observations"][:12]:
        print(f"\n{o['ticker']} @ {o['current_px']} ({o['n_ladders']} ladders)")
        for n in o["notes"][:6]:
            print("   - " + n)
    if p["p0"]:
        print("\nP0 discrepancies:")
        for x in p["p0"][:10]:
            print(f"   {x['ticker']}: {'; '.join(x['p0'])}")
