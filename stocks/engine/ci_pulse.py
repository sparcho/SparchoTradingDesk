#!/usr/bin/env python3
"""PULSE in the cloud — "who is alive today", with no laptop involved.

Operator rule, 2026-08-31: *"we should structure our processes in a way that they can run
without my specific system being on at any time."* PULSE was originally built on the laptop,
which was the wrong call — and the desk already had a red caused by exactly that (signal_perf
stale, "produced on the laptop substrate").

Everything PULSE needs turns out to be public:
  * intraday bars      -> yfinance (already a dependency of the other cloud jobs)
  * catalysts          -> NSE corporate-announcements API + Google News RSS
  * the level marker   -> fib_radar.fires / .watch, already PUBLIC in the equity aggregate

So no private data crosses into this job, and it runs whether the laptop is on or off.

DELIBERATE DEGRADATION vs the laptop build: there is no bhavcopy here, so a session cannot be
reconciled against the exchange's own daily volume. The frozen-session check still runs (it
needs only the bars). Every payload states which checks actually ran, so a thinner run can
never pass itself off as the full one.
"""
from __future__ import annotations

import json
import math
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from yahoo_common import ALL_TICKERS, INTERMARKET_TICKERS, yahoo_symbols  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
REPO = HERE.parent.parent
DATA = REPO / "stocks" / "data"
OUT = DATA / "pulse.json"
AGG = DATA / "equity_dashboard_aggregate.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# Never 09:15 — that bar reports volume 0 in 58 of 60 sessions (measured 2026-08-26), so an
# opening-range feature built on it silently measures nothing.
OPEN_FROM, OPEN_TO = "09:20", "09:45"
ALIVE_RVOL = 1.5
BUY_SHOUT, SELL_SHOUT = 0.60, 0.40
BASELINE_SESSIONS = 20
MIN_NAMES_FOR_SESSION = 20      # a session that has not opened is not "today"

T1 = [r"result", r"financial result", r"borrow", r"debt\b", r"bagging", r"receiving of order",
      r"contract", r"award", r"capacity", r"capex", r"expansion", r"acquisi", r"amalgamat",
      r"rating", r"merger", r"demerger", r"fund rais", r"offer for sale", r"\bofs\b",
      r"investment", r"agreement", r"commission", r"letter of intent", r"\bloa\b",
      r"allotment", r"preferential issue", r"qip", r"stake", r"divest", r"open offer"]
T2 = [r"analyst", r"institutional investor", r"guidance", r"buy.?back", r"dividend",
      r"management", r"appointment", r"resignation", r"credit rating", r"concall",
      r"earnings call", r"press release", r"investor present"]
T3 = [r"shareholders meeting", r"annual general meeting", r"agm\b", r"postal ballot",
      r"trading window", r"newspaper publication", r"compliance certificate",
      r"reg\.? 74", r"certificate under", r"disclosure under regulation 3",
      r"loss of share", r"duplicate share", r"record date", r"book closure"]
REGULATORY = [r"action\(s\) taken", r"orders passed", r"penalt", r"show cause"]


def _get(url, extra=None, timeout=45):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    h = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}
    if extra:
        h.update(extra)
    return urllib.request.urlopen(urllib.request.Request(url, headers=h),
                                  timeout=timeout, context=ctx).read()


def tier_of(text):
    t = (text or "").lower()
    for pat in REGULATORY:
        if re.search(pat, t):
            return "T2"          # material, but not an order win — do not mislabel it as one
    for pat in T3:
        if re.search(pat, t):
            return "T3"
    for pat in T1:
        if re.search(pat, t):
            return "T1"
    for pat in T2:
        if re.search(pat, t):
            return "T2"
    return "T3"


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bvc_buy_fraction(dp, sigma):
    """Bulk Volume Classification (Easley, Lopez de Prado & O'Hara).

    Splits a bar's volume into buy/sell from the price change alone — the free substitute for
    tick data. A no-information bar must say so (0.5), never shout a direction it cannot know.
    """
    if not sigma or sigma <= 0:
        return 0.5
    return norm_cdf(dp / sigma)


def fetch_catalysts(universe):
    errors, by_ticker, news = [], {}, {}
    to_d = datetime.now(IST)
    from_d = to_d - timedelta(days=4)
    url = ("https://www.nseindia.com/api/corporate-announcements?index=equities"
           f"&from_date={from_d.strftime('%d-%m-%Y')}&to_date={to_d.strftime('%d-%m-%Y')}")
    try:
        rows = json.loads(_get(url, timeout=75))
        if not isinstance(rows, list):
            raise ValueError("unexpected payload shape")
    except Exception as e:
        errors.append("nse_announcements: %s %s" % (type(e).__name__, str(e)[:90]))
        rows = []

    seen = set()
    for r in rows:
        sym = r.get("symbol")
        if sym not in universe:
            continue
        desc = (r.get("desc") or "").strip()
        body = (r.get("attchmntText") or "").strip()
        key = (sym, r.get("an_dt"), desc)
        if key in seen:
            continue
        seen.add(key)
        by_ticker.setdefault(sym, []).append({
            "ticker": sym, "at": r.get("an_dt"), "desc": desc, "detail": body[:400],
            "tier": tier_of(desc + " " + body[:400]),
            "url": r.get("attchmntFile") or "", "source": "NSE"})

    hot = [t for t, v in by_ticker.items() if any(x["tier"] in ("T1", "T2") for x in v)]
    for t in hot[:30]:
        try:
            q = urllib.parse.quote('"%s" NSE' % t)
            body = _get("https://news.google.com/rss/search?q=%s&hl=en-IN&gl=IN&ceid=IN:en" % q,
                        timeout=25).decode("utf8", "ignore")
            items = re.findall(r"<item>(.*?)</item>", body, re.S)[:4]
            got = []
            for it in items:
                m = re.search(r"<title>(.*?)</title>", it, re.S)
                if m:
                    got.append({"title": re.sub(r"<.*?>", "", m.group(1)).strip()[:180],
                                "source": "news"})
            if got:
                news[t] = got
        except Exception:
            pass
        time.sleep(0.25)
    return by_ticker, news, errors


def fetch_intraday(symbols):
    """5m bars, 60 days, batched through yfinance. Returns {ticker: {date: [(hhmm,c,v)]}}."""
    import yfinance as yf
    out = {}
    items = list(symbols.items())
    for i in range(0, len(items), 25):
        chunk = dict(items[i:i + 25])
        try:
            df = yf.download(list(chunk.values()), period="60d", interval="5m",
                             group_by="ticker", auto_adjust=False, progress=False,
                             threads=True)
        except Exception as e:
            print("   yfinance chunk failed:", type(e).__name__, str(e)[:80])
            continue
        for tk, sym in chunk.items():
            try:
                sub = df[sym] if len(chunk) > 1 else df
                sub = sub.dropna(subset=["Close"])
                if sub.empty:
                    continue
                idx = sub.index.tz_convert(IST) if sub.index.tz is not None else sub.index
                per = {}
                for ts, c, v in zip(idx, sub["Close"].values, sub["Volume"].values):
                    per.setdefault(ts.strftime("%Y-%m-%d"), []).append(
                        (ts.strftime("%H:%M"), float(c), float(v or 0)))
                out[tk] = per
            except Exception:
                continue
        time.sleep(0.5)
    return out


def open_vol(bars):
    return sum(v for hm, _c, v in bars if OPEN_FROM <= hm <= OPEN_TO)


def is_frozen(bars):
    return len({round(c, 4) for _hm, c, _v in bars}) <= 1


def session_bvc(bars):
    closes = [c for _hm, c, _v in bars]
    if len(closes) < 5:
        return None, 0.0
    diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    n = len(diffs)
    mean = sum(diffs) / n
    var = sum((d - mean) ** 2 for d in diffs) / (n - 1) if n > 1 else 0.0
    sigma = math.sqrt(var)
    buy = tot = 0.0
    for i in range(1, len(bars)):
        v = bars[i][2]
        if not v:
            continue
        buy += v * bvc_buy_fraction(diffs[i - 1], sigma)
        tot += v
    return (buy / tot if tot else None), tot


def banked_levels():
    """Tickers carrying a hand-verified level, from the PUBLIC part of the aggregate."""
    try:
        d = json.loads(AGG.read_text(encoding="utf-8"))
        fr = d.get("fib_radar") or {}
        out = set()
        for key in ("fires", "watch", "board"):
            for row in (fr.get(key) or []):
                t = row.get("ticker")
                if t:
                    out.add(str(t).upper())
        return out
    except Exception:
        return set()


def main():
    started = datetime.now(IST)
    errors = []
    universe = set(ALL_TICKERS) - set(INTERMARKET_TICKERS) - {"NIFTY"}
    # yahoo_symbols() maps ONE ticker at a time and may return a list of candidates.
    symbols = {}
    for t in sorted(universe):
        try:
            s = yahoo_symbols(t)
            if isinstance(s, (list, tuple)):
                s = s[0] if s else None
        except Exception:
            s = None
        symbols[t] = s or (t + ".NS")

    cats, news, cat_err = fetch_catalysts(universe)
    errors.extend(cat_err)

    intr = fetch_intraday(symbols)
    if not intr:
        errors.append("no intraday bars returned — PULSE is blind this run")

    # Pick the latest session that ACTUALLY TRADED. Yahoo stamps bars for sessions that have
    # not opened; taking max(date) once selected an empty future day and reported 0 of 106.
    cover = {}
    for tk, per in intr.items():
        for d, bars in per.items():
            if open_vol(bars) > 0:
                cover[d] = cover.get(d, 0) + 1
    live = [d for d, n in cover.items() if n >= MIN_NAMES_FOR_SESSION]
    today = max(live) if live else None
    newest = max((d for per in intr.values() for d in per), default=None)
    notices = []
    if not today:
        errors.append("no session has real opening volume — PULSE is blind")
    elif newest and newest != today:
        notices.append("Showing the last completed session (%s). The feed also carries %s with "
                       "only %d names trading — today has not opened yet, or is incomplete."
                       % (today, newest, cover.get(newest, 0)))

    banked = banked_levels()
    rows, measured, thin, frozen = [], 0, 0, 0
    for tk in sorted(universe):
        per = intr.get(tk) or {}
        bars = per.get(today) or []
        if not bars:
            continue
        if is_frozen(bars):
            frozen += 1
            continue
        prior = []
        for d in sorted(per):
            if d >= today:
                continue
            ov = open_vol(per[d])
            if ov > 0:
                prior.append(ov)
        prior = prior[-BASELINE_SESSIONS:]
        if len(prior) < 3:
            thin += 1
            continue
        base = sorted(prior)[len(prior) // 2]
        ov = open_vol(bars)
        if not base or not ov:
            thin += 1
            continue
        measured += 1
        rv = round(ov / base, 2)
        if rv < ALIVE_RVOL:
            continue

        bf, vol = session_bvc(bars)
        tiers = [a["tier"] for a in cats.get(tk, [])]
        best = "T1" if "T1" in tiers else ("T2" if "T2" in tiers else None)
        stance = ("BUYING" if bf is not None and bf >= BUY_SHOUT
                  else "SELLING" if bf is not None and bf <= SELL_SHOUT else "--")
        rows.append({
            "ticker": tk, "rvol": rv,
            "buy_frac": round(bf, 3) if bf is not None else None,
            "stance": stance, "price": round(bars[-1][1], 2),
            "session_volume": int(vol),
            "catalyst_tier": best,
            "catalysts": [a for a in cats.get(tk, []) if a["tier"] in ("T1", "T2")][:3],
            "news": (news.get(tk) or [])[:2],
            # Level marker comes from the PUBLIC fib_radar, so no private bank is read here.
            "level": {"banked": True} if tk.upper() in banked else None,
            "at_level_and_alive": tk.upper() in banked,
        })

    rows.sort(key=lambda r: -r["rvol"])
    if measured == 0 and not errors:
        errors.append("zero names measurable — no baseline could be built for any ticker")

    last_bar = max((hm for tk, per in intr.items() for hm, _c, v in (per.get(today) or [])
                    if v > 0), default=None)

    payload = {
        "generated_at": started.isoformat(),
        "as_of_ist": started.strftime("%Y-%m-%d %H:%M IST"),
        "session_date": today,
        "bars_through": last_bar,
        "opening_range": "%s-%s" % (OPEN_FROM, OPEN_TO),
        "alive_threshold": ALIVE_RVOL,
        "n_universe": len(universe),
        "n_measured": measured,
        "n_alive": len(rows),
        "n_skipped_no_baseline": thin,
        "n_frozen": frozen,
        "substrate": "cloud",
        # State which checks actually ran. The laptop build reconciles every session against
        # the exchange's own daily volume; there is no bhavcopy here, so it cannot — and a
        # thinner run must never pass itself off as the full one.
        "checks_run": ["frozen_session", "unstarted_session", "baseline_depth"],
        "checks_skipped": ["bhavcopy_reconciliation (no bhavcopy in the cloud)"],
        "book_disclosure": "none — position overlap is deliberately NOT published",
        "rows": rows,
        "errors": errors,
        "notices": notices,
        "healthy": not errors,
    }
    DATA.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print("ci_pulse: session %s | measured %d/%d | ALIVE %d | thin %d | frozen %d | errors %d"
          % (today, measured, len(universe), len(rows), thin, frozen, len(errors)))
    for r in rows[:10]:
        print("   %-13s %6.2fx  %-8s %s" % (r["ticker"], r["rvol"], r["stance"],
                                            r["catalyst_tier"] or ""))
    for e in errors[:4]:
        print("   ERR", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
