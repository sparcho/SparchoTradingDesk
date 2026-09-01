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
import pulse_metrics as PM  # noqa: E402

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
    """5m bars, 60 days, batched through yfinance.

    Returns {ticker: {date: [(hhmm, o, h, l, c, v)]}}. The old shape carried close and volume
    only, which is why the strip could never tell 3x-volume-going-nowhere (absorption) from
    3x-with-the-range-expanding (displacement) -- it had no high or low to measure travel with.
    """
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
                for ts, o, h, l, c, v in zip(idx, sub["Open"].values, sub["High"].values,
                                             sub["Low"].values, sub["Close"].values,
                                             sub["Volume"].values):
                    # A NaN bar is a hole in the feed, not a trade at zero. Dropping it keeps
                    # the hole visible as missing coverage instead of printing a fake price.
                    if any(x != x for x in (o, h, l, c)):
                        continue
                    per.setdefault(ts.strftime("%Y-%m-%d"), []).append(
                        (ts.strftime("%H:%M"), float(o), float(h), float(l), float(c),
                         float(v or 0)))
                out[tk] = per
            except Exception:
                continue
        time.sleep(0.5)
    return out


def open_vol(bars):
    """Opening-range volume -- used ONLY to decide which session actually traded."""
    return sum(b[5] for b in bars if OPEN_FROM <= b[0] <= OPEN_TO)


def is_frozen(bars):
    return len({round(b[4], 4) for b in bars}) <= 1


def last_real_bar(intr, today):
    """The latest bar of `today` that actually TRADED, across the whole universe.

    Volume-zero bars are stamped by the feed for periods that never printed, so taking the
    newest stamp once made the desk advertise a session that had not happened. Kept as a named
    function rather than an inline generator so it can be tested: this line was the one reader
    missed when the bar tuple grew, and it took a cloud run down.
    """
    stamps = [b[0] for per in intr.values() for b in (per.get(today) or []) if b[5] > 0]
    return max(stamps) if stamps else None


def session_bvc(bars):
    """Volume-weighted BUY fraction over whatever bars are handed in.

    Handed the whole session it answers "who has been in control today"; handed the last half
    hour it answers "who is in control NOW". The old build only ever asked the first question
    and then printed the answer as if it were the second.
    """
    closes = [b[4] for b in bars]
    if len(closes) < 5:
        return None, 0.0
    diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    n = len(diffs)
    mean = sum(diffs) / n
    var = sum((d - mean) ** 2 for d in diffs) / (n - 1) if n > 1 else 0.0
    sigma = math.sqrt(var)
    buy = tot = 0.0
    for i in range(1, len(bars)):
        v = bars[i][5]
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
    # Stamp the catalyst READ, not the run. PULSE's own rule is that a stale strip must
    # look stale; an undated filing passes every freshness gate ever written because
    # you cannot tell one read four minutes ago from one read four days ago
    # (dated-is-not-the-same-as-correctly-dated). A fetch that FAILED is dated None
    # rather than being stamped with the time we failed at.
    catalysts_as_of = None if cat_err else datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
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
        # Every prior session in the archive, oldest first. PM.compute picks its own
        # baseline window AND drops any prior day that did not trade through the same clock
        # time -- a truncated prior deflates the baseline and inflates today.
        prior = [per[d] for d in sorted(per) if d < today]
        m = PM.compute(bars, prior)
        if not m or m.get("rvol_cum") is None:
            thin += 1
            continue
        measured += 1
        if not PM.is_alive(m):
            continue

        bf, vol = session_bvc(bars)
        # Who is in control NOW, as opposed to who has been in control since the open. Same
        # window as rvol_now so the two numbers describe the same slice of the day.
        t_now = PM.to_min(m["measured_through"])
        recent = [b for b in bars
                  if PM.to_min(b[0]) >= t_now - (PM.NOW_WINDOW_MIN - PM.GRID_MIN)]
        bf_now, _ = session_bvc(recent) if len(recent) >= 5 else (None, 0.0)

        def _stance(x):
            if x is None:
                return "--"
            return "BUYING" if x >= BUY_SHOUT else "SELLING" if x <= SELL_SHOUT else "--"

        tiers = [a["tier"] for a in cats.get(tk, [])]
        best = "T1" if "T1" in tiers else ("T2" if "T2" in tiers else None)
        row = {
            "ticker": tk,
            # `rvol` is kept as an alias of rvol_cum so an older client cannot go blank the
            # moment this ships. Deprecated -- new readers use rvol_cum.
            "rvol": m["rvol_cum"],
            "buy_frac": round(bf, 3) if bf is not None else None,
            "buy_frac_now": round(bf_now, 3) if bf_now is not None else None,
            "stance": _stance(bf),
            "stance_now": _stance(bf_now),
            "session_volume": int(vol),
            "catalyst_tier": best,
            "catalysts": [a for a in cats.get(tk, []) if a["tier"] in ("T1", "T2")][:3],
            "news": (news.get(tk) or [])[:2],
            # Level marker comes from the PUBLIC fib_radar, so no private bank is read here.
            "level": {"banked": True} if tk.upper() in banked else None,
            "at_level_and_alive": tk.upper() in banked,
        }
        row.update(m)
        rows.append(row)

    # Rank on the LOUDER of "the day so far" and "right now", so a name that just woke up at
    # 14:00 is not buried under one that was busy at 09:20 and has been dead since. Tradeable
    # names sort above thin ones at equal loudness -- thin is marked, never hidden.
    rows.sort(key=lambda r: (bool(r.get("thin")),
                             -max(r.get("rvol_cum") or 0, r.get("rvol_now") or 0)))
    if measured == 0 and not errors:
        errors.append("zero names measurable — no baseline could be built for any ticker")

    last_bar = last_real_bar(intr, today)

    payload = PM.build_payload(
        rows=rows, session_date=today, last_bar=last_bar, now_dt=started,
        n_measured=measured, universe=len(universe), errors=errors, notices=notices,
        extra={
        "opening_range": "%s-%s" % (OPEN_FROM, OPEN_TO),
        "catalysts_as_of": catalysts_as_of,
        "alive_threshold": ALIVE_RVOL,
        "n_skipped_no_baseline": thin,
        "n_frozen": frozen,
        "substrate": "cloud",
        # State which checks actually ran. The laptop build reconciles every session against
        # the exchange's own daily volume; there is no bhavcopy here, so it cannot — and a
        # thinner run must never pass itself off as the full one.
        "checks_run": ["frozen_session", "unstarted_session", "baseline_depth"],
        "checks_skipped": ["bhavcopy_reconciliation (no bhavcopy in the cloud)"],
        "book_disclosure": "none — position overlap is deliberately NOT published",
        })
    DATA.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print("ci_pulse: session %s | measured %d/%d | ALIVE %d | thin %d | frozen %d | errors %d"
          % (today, measured, len(universe), len(rows), thin, frozen, len(errors)))
    for r in rows[:10]:
        print("   %-13s cum %5.1fx  now %5s  peak %5s @ %-11s %-10s %s"
              % (r["ticker"], r["rvol_cum"] or 0,
                 ("%.1fx" % r["rvol_now"]) if r["rvol_now"] else "-",
                 ("%.1fx" % r["rvol_peak"]) if r["rvol_peak"] else "-",
                 r["peak_at"] or "-", r["char"] or "-", r["catalyst_tier"] or ""))
    for e in errors[:4]:
        print("   ERR", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
