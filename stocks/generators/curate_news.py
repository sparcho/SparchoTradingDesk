#!/usr/bin/env python3
"""curate_news.py — HOURLY JOURNALIST curation of the news candidate set (JOURNALIST L2 · F260622).

Routes stocks/data/news_candidates.json through the JOURNALIST agent's vetting and writes
stocks/data/news_curated.json (the PUBLISHED set the dashboard reads). When a live LLM/agent pass
is unavailable in cron, the vetting RULES below ARE the journalist — codified so the curation is
rigorous deterministically (a later upgrade can swap in an LLM pass; the contract stays the same):

  - SOURCE CREDIBILITY weight (tier-1 wire/national business desks > tier-2 > aggregators).
  - RELEVANCE: ticker/company in the headline scores higher than a body-only mention.
  - RECENCY: newer ranks higher (smooth decay).
  - SENTIMENT: Marketaux entity tag when present, else a finance lexicon.
  - MATERIALITY: order/result/M&A/regulatory/rating keywords lift the score.
  - "WHY IT MATTERS": a templated one-liner from the dominant materiality signal + sentiment.

DEGRADE-SAFE: if candidates are missing/empty, keep the last good curated set (stamped stale) — the
panel never empties. stdlib only.
"""
from __future__ import annotations
import json, os, re, sys, tempfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
CAND = DATA / "news_candidates.json"
OUT = DATA / "news_curated.json"
TOP_N = 24

TIER1 = ("reuters", "bloomberg", "economic times", "etmarkets", "livemint", "mint",
         "business standard", "businessline", "hindu businessline", "financial express", "the hindu")
TIER2 = ("moneycontrol", "cnbc", "ndtv profit", "ndtvprofit", "business today", "zee business",
         "investing.com", "forbes", "fortune india", "the ken", "times of india", "bqprime")
SENT_POS = set(("surge surges jump jumps gain gains gained rally rallies soar soars climb beat beats "
                "win wins won bag bags secures order orders record high profit upgrade upgraded approval "
                "approved expansion launch strong outperform").split())
SENT_NEG = set(("fall falls fell drop drops plunge plunges slump slips loss losses probe raid downgrade "
                "downgraded cut cuts miss misses warning fraud default weak slowdown lawsuit penalty "
                "ban recall delay").split())
# (regex, why-it-matters template, materiality weight)
MATERIAL = [
    (re.compile(r"\b(order|orders|contract|deal|bags|wins|won|loi|tender|awarded)\b", re.I),
     "Order / contract news — watch the backlog.", 3),
    (re.compile(r"\b(q[1-4]|result|results|earnings|profit|pat|revenue|ebitda|margin|guidance)\b", re.I),
     "Earnings / guidance — check vs estimates.", 3),
    (re.compile(r"\b(merger|acquisition|acquire|acquires|stake|buyout|qip|fundrais|fund-rais|raise|rights issue)\b", re.I),
     "Corporate action — M&A / capital raise.", 3),
    (re.compile(r"\b(sebi|rbi|probe|raid|fraud|penalty|lawsuit|investigation|ban|recall)\b", re.I),
     "Regulatory / legal — risk watch.", 3),
    (re.compile(r"\b(upgrade|downgrade|target price|rating|buy call|sell call|brokerage)\b", re.I),
     "Analyst rating change.", 2),
    (re.compile(r"\b(approval|approved|launch|expansion|capacity|plant|commission|capex)\b", re.I),
     "Growth catalyst — approval / launch / capex.", 2),
    (re.compile(r"\b(dividend|bonus|split|buyback|record date)\b", re.I),
     "Shareholder return — dividend / bonus / buyback.", 2),
]


def _read(p, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _dt(s):
    try:
        return parsedate_to_datetime(s) if "," in str(s) else datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def _sentiment(it):
    if it.get("sentiment") in ("positive", "negative", "neutral"):
        return it["sentiment"]                       # Marketaux entity tag — trust it
    toks = set(re.sub(r"[^a-z ]", " ", (it.get("headline") or "").lower()).split())
    pos, neg = len(toks & SENT_POS), len(toks & SENT_NEG)
    return "positive" if pos > neg else ("negative" if neg > pos else "neutral")


def _cred(src):
    s = (src or "").lower()
    if any(t in s for t in TIER1):
        return 3.0
    if any(t in s for t in TIER2):
        return 2.0
    if it_allow(s):
        return 1.5
    return 1.0


def it_allow(s):
    return any(t in s for t in TIER1 + TIER2)


def _materiality(headline):
    why, w = None, 0
    for rgx, tmpl, wt in MATERIAL:
        if rgx.search(headline or ""):
            if wt > w:
                w, why = wt, tmpl
    return w, why


def _curate(cand):
    now = datetime.now(timezone.utc)
    items = cand.get("items") or []
    scored = []
    for it in items:
        try:
            h = it.get("headline") or ""
            cred = _cred(it.get("source"))
            tk = str(it.get("ticker") or "")
            relevance = 2.0 if (tk and tk.lower() in h.lower()) else 1.0
            d = _dt(it.get("published_at"))
            age_h = ((now - d.astimezone(timezone.utc)).total_seconds() / 3600.0) if d else 48.0
            recency = max(0.2, 1.0 - min(age_h, 72.0) / 96.0)     # smooth decay over ~3 days
            sent = _sentiment(it)
            mat_w, why = _materiality(h)
            if not why:
                why = ("Positive headline." if sent == "positive"
                       else ("Negative headline — watch." if sent == "negative" else "General coverage."))
            score = round(cred * relevance * (1.0 + mat_w) * recency, 3)
            o = dict(it)
            o.update({"sentiment": sent, "materiality": mat_w, "why": why,
                      "credibility": cred, "score": score})
            o.pop("allowlisted", None)
            scored.append(o)
        except Exception:
            continue
    scored.sort(key=lambda x: x.get("score", 0), reverse=True)
    # guarantee every held/lead ticker with news keeps at least its top item, then fill by score
    top, seen_tk, seen_url = [], set(), set()
    for it in scored:                                  # one best item per ticker first (breadth)
        tk = it.get("ticker")
        if tk in seen_tk:
            continue
        u = (it.get("url") or "").split("?")[0]
        if u in seen_url:
            continue
        seen_tk.add(tk); seen_url.add(u); top.append(it)
    for it in scored:                                  # then fill remaining slots by score (depth)
        if len(top) >= TOP_N:
            break
        u = (it.get("url") or "").split("?")[0]
        if it in top or u in seen_url:
            continue
        seen_url.add(u); top.append(it)
    top.sort(key=lambda x: x.get("score", 0), reverse=True)
    return top[:TOP_N]


def main():
    cand = _read(CAND, None)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not cand or not cand.get("items"):
        prev = _read(OUT, None)
        if prev and prev.get("items"):
            prev["stale"] = True
            prev["curated_at_utc"] = now_iso
            _atomic(OUT, prev)
            print("curate_news: no fresh candidates — kept last-good curated (%d), stamped stale"
                  % len(prev["items"]), file=sys.stderr)
            return 0
        _atomic(OUT, {"schema": "news-curated/v1", "fetched_at_utc": now_iso, "curated_at_utc": now_iso,
                      "source_mode": (cand or {}).get("source_mode", "rss-only"), "n": 0,
                      "stale": True, "curated_by": "journalist-rules/v1", "items": []})
        print("curate_news: no candidates and no prior curated — wrote empty (stale)", file=sys.stderr)
        return 0
    try:
        items = _curate(cand)
    except Exception as e:                              # never crash the panel; degrade to raw candidates
        print("curate_news: curation error (%s) — degrading to raw candidates" % str(e)[:90], file=sys.stderr)
        items = cand.get("items", [])[:TOP_N]
    payload = {"schema": "news-curated/v1",
               "fetched_at_utc": cand.get("fetched_at_utc", now_iso),
               "curated_at_utc": now_iso,
               "source_mode": cand.get("source_mode", "rss-only"),
               "n": len(items), "stale": bool(cand.get("stale")),
               "curated_by": "journalist-rules/v1", "items": items}
    _atomic(OUT, payload)
    print("curate_news: curated %d items (journalist-rules/v1, %s) -> %s"
          % (len(items), payload["source_mode"], OUT.name))
    return 0


def _atomic(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, indent=2))
    os.replace(tmp, str(path))


if __name__ == "__main__":
    raise SystemExit(main())
