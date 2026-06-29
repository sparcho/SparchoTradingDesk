#!/usr/bin/env python3
"""refresh_news.py — live Company News FETCH + deterministic VET (JOURNALIST L2 pipeline · F260622).

Marketaux API (NEWS_API_KEY secret) PRIMARY + Google-News-RSS ALWAYS-ON fallback, merged. Writes
stocks/data/news_candidates.json. The hourly journalist curation (curate_news.py) ranks/sentiment-
tags this into news_curated.json (the published set).

BULLETPROOF by design:
  - multi-source: API + RSS; if the API errors or no key, RSS alone carries the feed (graceful degrade).
  - per-source / per-item try/except: one bad source or item can never blank the run.
  - last-good cache: a 0-result run NEVER overwrites a good file — it keeps the last good set and
    stamps `stale: true`.
  - NSE-session gated (reuses engine/nse_calendar): silent on weekends/holidays.

Deterministic VET: relevance (query-scoped), canonical-URL + normalized-title dedup, source allowlist
(credibility weight, promo/non-English drop), recency window, per-ticker cap.

stdlib only (urllib + xml.etree). No NEWS_API_KEY → RSS-only, logged "API key not set — RSS-only".
"""
from __future__ import annotations
import json, os, re, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
AGG = DATA / "equity_dashboard_aggregate.json"
OUT = DATA / "news_candidates.json"

# ── CONFIG (tunable) ──────────────────────────────────────────────────────────
MAX_PER_TICKER = 4
RECENCY_HOURS = 72
RSS_UNIVERSE_CAP = 30          # held + watchlist + top screener names; caps per-company RSS fetches/run
MARKETAUX_URL = "https://api.marketaux.com/v1/news/all"
HTTP_TIMEOUT = 12
UA = {"User-Agent": "Mozilla/5.0 (SparchoTradingDesk news bot)"}

# credible finance sources (lowercased substrings of the source name / domain). Allowlisted = high
# credibility (kept + weighted up in curation); promo/unknown still kept but ranked lower.
SOURCE_ALLOW = (
    "economic times", "livemint", "mint", "business standard", "moneycontrol", "reuters",
    "bloomberg", "hindu businessline", "businessline", "financial express", "cnbc", "ndtv profit",
    "the hindu", "times of india", "business today", "zee business", "outlook business",
    "etmarkets", "investing.com", "forbes", "fortune india", "the ken", "bqprime", "ndtvprofit",
)
PROMO_RE = re.compile(r"\b(sponsored|press release|advertorial|webinar|coupon|promo code|"
                      r"buy now|discount|giveaway|paid post)\b", re.I)


def _now_utc():
    return datetime.now(timezone.utc)


def _ist_today():
    return datetime.now(timezone(timedelta(hours=5, minutes=30))).date()


def _is_session():
    """NSE-session gate (silent on weekends/holidays). Never let a missing gate stop a fetch."""
    try:
        sys.path.insert(0, str(HERE.parent / "engine"))
        from nse_calendar import is_session
        return is_session(_ist_today())
    except Exception as e:
        print("refresh_news: nse_calendar gate unavailable (%s) — proceeding" % e, file=sys.stderr)
        return True


def _read_json(p, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def universe():
    """{ticker: name} for held + screener candidates + watchlist (held first, RSS-capped).
    Names (held only) sharpen the RSS query; ticker-only names fall back to a symbol query."""
    d = _read_json(AGG, {})
    name_of, order = {}, []
    for h in (d.get("held") or []):
        t = h.get("ticker")
        if t and t not in name_of:
            name_of[t] = h.get("name") or h.get("company") or ""
            order.append(t)
    di = d.get("daytrade_inputs") or {}
    cands = di.get("candidates") or {}
    cand_tickers = sorted(cands.keys()) if isinstance(cands, dict) else [c for c in cands]
    for t in (di.get("held") or []) + cand_tickers:
        if t and t not in name_of:
            name_of[t] = ""
            order.append(t)
    for w in (d.get("watchlist_rundown") or []):
        t = w.get("ticker")
        if t and t not in name_of:
            name_of[t] = ""
            order.append(t)
    order = order[:RSS_UNIVERSE_CAP]
    return [(t, name_of.get(t, "")) for t in order]


def canon_url(u):
    """Canonicalise a URL for dedup: drop query/fragment + trailing slash, lowercase host."""
    try:
        p = urllib.parse.urlsplit(u)
        host = (p.netloc or "").lower()
        path = (p.path or "").rstrip("/")
        return host + path
    except Exception:
        return (u or "").split("?")[0].rstrip("/").lower()


def norm_title(t):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", (t or "").lower())).strip()


def _parse_dt(s):
    try:
        return parsedate_to_datetime(s)
    except Exception:
        return None


def fetch_rss(ticker, name):
    """Google News RSS for one company (free, no key). Returns a list of raw items. Never raises."""
    q = ('"%s" stock' % name) if name else ("%s share price NSE" % ticker)
    url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q)
           + "&hl=en-IN&gl=IN&ceid=IN:en")
    out = []
    try:
        raw = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=HTTP_TIMEOUT).read()
        root = ET.fromstring(raw)
        for it in root.iter("item"):
            try:
                title = (it.findtext("title") or "").strip()
                link = (it.findtext("link") or "").strip()
                pub = (it.findtext("pubDate") or "").strip()
                src_el = it.find("source")
                source = (src_el.text.strip() if src_el is not None and src_el.text else "")
                if not source and " - " in title:    # Google appends " - Source" to the title
                    title, source = title.rsplit(" - ", 1)
                if not title or not link:
                    continue
                out.append({"ticker": ticker, "source": source or "Google News",
                            "url": link, "headline": title.strip(),
                            "published_at": pub, "_dt": _parse_dt(pub), "sentiment": None, "via": "rss"})
            except Exception:
                continue
    except Exception as e:
        print("  rss %s: %s" % (ticker, str(e)[:80]), file=sys.stderr)
    return out


def fetch_marketaux(tickers, key):
    """Marketaux batched entity query (equity news + sentiment). Returns items or [] on any error."""
    if not key:
        return []
    syms = ",".join((t + ".NS") for t in tickers[:50])   # Marketaux symbol = Yahoo-style for NSE
    params = urllib.parse.urlencode({
        "symbols": syms, "filter_entities": "true", "language": "en",
        "must_have_entities": "true", "limit": "50", "api_token": key})
    out = []
    try:
        raw = urllib.request.urlopen(urllib.request.Request(MARKETAUX_URL + "?" + params, headers=UA),
                                     timeout=HTTP_TIMEOUT).read()
        d = json.loads(raw)
        for a in (d.get("data") or []):
            try:
                ents = a.get("entities") or []
                tks = sorted({str(e.get("symbol", "")).replace(".NS", "") for e in ents
                              if e.get("symbol")}) or [tickers[0] if tickers else ""]
                # marketaux sentiment_score in [-1,1] per entity; take the strongest-magnitude one
                ss = [e.get("sentiment_score") for e in ents if isinstance(e.get("sentiment_score"), (int, float))]
                sent = None
                if ss:
                    m = max(ss, key=lambda x: abs(x))
                    sent = "positive" if m > 0.15 else ("negative" if m < -0.15 else "neutral")
                out.append({"ticker": tks[0], "tickers": tks, "source": a.get("source") or "Marketaux",
                            "url": a.get("url") or "", "headline": (a.get("title") or "").strip(),
                            "published_at": a.get("published_at") or "",
                            "_dt": _parse_dt(a.get("published_at") or ""), "sentiment": sent, "via": "marketaux"})
            except Exception:
                continue
    except Exception as e:
        print("  marketaux error (%s) — RSS carries the feed" % str(e)[:90], file=sys.stderr)
    return out


def vet(items):
    """Deterministic vetting: drop promo, recency window, canonical+title dedup, per-ticker cap."""
    now = _now_utc()
    seen_url, seen_title, per_tk, kept = set(), set(), {}, []
    # freshest first so dedup/cap keep the newest
    items.sort(key=lambda it: (it.get("_dt") or datetime(1970, 1, 1, tzinfo=timezone.utc)), reverse=True)
    for it in items:
        try:
            h = it.get("headline") or ""
            if not h or not it.get("url"):
                continue
            if PROMO_RE.search(h):
                continue
            dt = it.get("_dt")
            if dt is not None:
                age_h = (now - dt.astimezone(timezone.utc)).total_seconds() / 3600.0
                if age_h > RECENCY_HOURS:
                    continue
            cu, nt = canon_url(it["url"]), norm_title(h)
            if cu in seen_url or (nt and nt in seen_title):
                continue
            tk = it.get("ticker") or "?"
            if per_tk.get(tk, 0) >= MAX_PER_TICKER:
                continue
            seen_url.add(cu)
            if nt:
                seen_title.add(nt)
            per_tk[tk] = per_tk.get(tk, 0) + 1
            src = (it.get("source") or "").lower()
            it["allowlisted"] = any(a in src for a in SOURCE_ALLOW)
            it.setdefault("tickers", [tk])
            it.pop("_dt", None)
            kept.append(it)
        except Exception:
            continue
    return kept


def main():
    cal = _ist_today()
    if not _is_session():
        print("refresh_news: %s is not an NSE session — skipping (last-good news kept)" % cal)
        return 0
    key = os.environ.get("NEWS_API_KEY", "").strip()
    if not key:
        print("refresh_news: NEWS_API_KEY not set — RSS-only mode (still fully functional)")
    uni = universe()
    print("refresh_news: fetching news for %d names (%s)" % (len(uni), "API+RSS" if key else "RSS-only"))

    raw = []
    # PRIMARY — Marketaux (batched), if a key is present
    raw += fetch_marketaux([t for t, _ in uni], key)
    # FALLBACK — Google News RSS per company, ALWAYS
    for t, nm in uni:
        raw += fetch_rss(t, nm)
        time.sleep(0.05)   # be gentle

    items = vet(raw)
    now_iso = _now_utc().isoformat(timespec="seconds")
    if not items:
        # NEVER blank a good file — keep last-good, stamp stale
        prev = _read_json(OUT, None)
        if prev and prev.get("items"):
            prev["stale"] = True
            prev["last_attempt_utc"] = now_iso
            _atomic_write(OUT, prev)
            print("refresh_news: 0 vetted items this run — kept last-good (%d items), stamped stale"
                  % len(prev["items"]), file=sys.stderr)
            return 0
        # no prior good file either — write an explicit empty (panel shows the empty-state, not a crash)
        _atomic_write(OUT, {"schema": "news/v1", "fetched_at_utc": now_iso, "session": "intraday",
                            "source_mode": ("marketaux+rss" if key else "rss-only"),
                            "n": 0, "stale": True, "items": []})
        print("refresh_news: 0 items and no prior good file — wrote empty (stale)", file=sys.stderr)
        return 0

    payload = {"schema": "news/v1", "fetched_at_utc": now_iso, "session": "intraday",
               "source_mode": ("marketaux+rss" if key else "rss-only"),
               "n": len(items), "stale": False, "items": items}
    _atomic_write(OUT, payload)
    print("refresh_news: wrote %d vetted candidates (%s) -> %s"
          % (len(items), payload["source_mode"], OUT.name))
    return 0


def _atomic_write(path, obj):
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, indent=2))
    os.replace(tmp, str(path))


if __name__ == "__main__":
    raise SystemExit(main())
