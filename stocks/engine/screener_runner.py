#!/usr/bin/env python3
"""
screener_runner.py  --  apply Rajiv #1, Sanjeev 8-gate fundamental, etc. to the universe

WHY THIS EXISTS
===============
Rajiv (Dad) + Tauji each have a defined screener spec. This script applies
each spec to the cached fundamentals + price data and outputs:
  * a markdown report per screener with the candidate list + per-ticker
    pass/fail breakdown across each gate (so we can see WHY a name made or
    missed the cut)
  * a CSV per screener for downstream consumption (scoring system later)

DATA INPUTS (must be populated before this runs)
================================================
  * /00_SYSTEM/GENERATORS/_cache/fundamentals.csv     -- fetch_fundamentals.py
  * /00_SYSTEM/GENERATORS/_cache/daily_prices.csv     -- fetch_daily_ohlc.py
  * /00_SYSTEM/GENERATORS/_cache/historical_closes.csv -- fetch_historical.py

CADENCE
=======
Twice daily (operator-stated 2026-04-28): pre-market open + post-close. Pattern
detection (squeeze / inside-candle / RSI divergence) needs continuous tracking.

USAGE
=====
   python3 screener_runner.py
       Run all screeners; output to /03_SCREENERS/screeners/<screener_name>/<YYMMDD>_<screener>.{md,csv}

   python3 screener_runner.py --screener Rajiv-10G
       Run one specific screener.

   python3 screener_runner.py --list
       List available screener specs.

OUTPUT LOCATIONS
================
  /03_SCREENERS/LAYERS/Rajiv-10G/YYMMDD_Rajiv-10G.md   + .csv
  /03_SCREENERS/LAYERS/Sanjeev-8G/YYMMDD_Sanjeev-8G.md + .csv
"""
from __future__ import annotations

import argparse
import csv
import os as _os
import sys
from datetime import datetime
from pathlib import Path
from collections import defaultdict
sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from nse_calendar import latest_session as _latest_nse_session, prev_session as _prev_nse_session  # F131 260616 — NSE-session date gate; F138: holiday-aware staleness

SCRIPT_DIR = Path(__file__).resolve().parent
TRADER_ROOT = SCRIPT_DIR.parent.parent
CACHE_DIR = SCRIPT_DIR / "_cache"
OUT_ROOT = TRADER_ROOT / "03_SCREENERS" / "LAYERS"   # renamed from "screeners" 2026-05-01

FUND_CSV = CACHE_DIR / "fundamentals.csv"
DAILY_CSV = CACHE_DIR / "daily_prices.csv"
HIST_CSV = CACHE_DIR / "historical_closes.csv"

# =============================================================================
# Sector mapping (matches CLAUDE.md §16 clusters)
# =============================================================================
SECTOR_MAP = {
    # Rajiv #1 needs: Power, IT, NBFC
    'POWER': {
        'NTPC', 'POWERGRID', 'ADANIGREEN', 'TATAPOWER',
        'IREDA', 'WAAREEENER', 'MGL',  # MGL = gas utility, treating as power-adjacent
    },
    'IT': {
        'TCS', 'INFY', 'HCLTECH', 'PERSISTENT', 'TATAELXSI',
    },
    'NBFC': {
        'BAJFINANCE', 'CHOLAFIN', 'SHRIRAMFIN', 'MCX', 'BSE',
    },
    # Other clusters (kept for context; not used by Rajiv #1 but useful for future screeners)
    'MINERALS': {'GMDCLTD', 'MOIL', 'HINDZINC', 'GRAPHITE', 'VEDL', 'DECNGOLD'},   # +DECNGOLD 260619 (gold explorer)
    'TELECOM': {'HFCL', 'STLTECH', 'TEJASNET', 'BHARTIARTL', 'POLYCAB', 'KEI', 'PACEDIGITK'},   # +PACEDIGITK 260619 (held; telecom-digital infra)
    'DEFENCE': {'BEL', 'GRSE', 'MAZDOCK', 'SOLARINDS', 'MTARTECH', 'HAL', 'HBLENGINE', 'APOLLO', 'ASTRAMICRO'},   # +APOLLO +ASTRAMICRO 260620 (full screener-verse onboard)
    'SEMI_EMS': {'KAYNES', 'DIXON', 'SYRMA', 'AMBER'},
    'DC': {'ANANTRAJ', 'BLUESTAR'},
    'L5_APPS': {'KPIT', 'NAUKRI', 'TANLA', 'MAPMYINDIA', 'NEWGEN'},
    'DPI': {'PAYTM', 'PBFINTECH', 'CDSL', 'ANGELONE'},
    'PHARMA': {'DRREDDY', 'SUNPHARMA', 'BIOCON'},
    'AGRI': {'AVANTIFEED', 'PARADEEP'},
    'FERT': {'PARADEEP'},
    'ETF': {'HDFCSML250'},   # Broad Smallcap index ETF (260620); tracked + level-ID'd, NOT value-screened (see G1_NON_SCREENED)
}

# Indian-market cap thresholds (₹ Cr)
LARGE_CAP_THRESHOLD = 20_000  # > 20K Cr = large cap
MID_CAP_THRESHOLD = 5_000     # 5K-20K = mid cap; < 5K = small cap


# =============================================================================
# Helpers
# =============================================================================
def f(v):
    if v is None or v == '':
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def cap_size(market_cap_cr):
    if market_cap_cr is None:
        return 'unknown'
    if market_cap_cr > LARGE_CAP_THRESHOLD:
        return 'large'
    if market_cap_cr > MID_CAP_THRESHOLD:
        return 'mid'
    return 'small'


def sectors_for(ticker):
    """Return list of sector tags for a ticker (most names map to one)."""
    return [s for s, names in SECTOR_MAP.items() if ticker in names]


# =============================================================================
# Cache loaders
# =============================================================================
def load_fundamentals():
    if not FUND_CSV.exists():
        return {}
    return {r['ticker']: r for r in csv.DictReader(FUND_CSV.open())}


def load_historical_closes():
    """Return dict[ticker] -> [(date, close), ...] sorted ascending.

    Close-only view for SMA / RSI / range computations that pre-date the volume
    column. Backed by historical_closes.csv (ticker, date, close, volume).
    """
    if not HIST_CSV.exists():
        return {}
    by_t = defaultdict(list)
    for r in csv.DictReader(HIST_CSV.open()):
        # Skip rows with missing/unparseable close (e.g. a truncated final row
        # from an interrupted cache write). Mirrors the defensive handling in
        # load_historical_with_volume(). Added 2026-06-15 after a catchup halt.
        try:
            close = float(r['close'])
        except (TypeError, ValueError):
            continue
        by_t[r['ticker']].append((r['date'], close))
    return {t: sorted(rows) for t, rows in by_t.items()}


def load_historical_with_volume():
    """Return dict[ticker] -> [(date, close, volume), ...] sorted ascending.

    For screeners that need volume context (e.g. Dad #2's volume-spike gate).
    Volume column added 2026-04-28 to historical_closes.csv via fetch_historical
    extension. Rows missing volume default to 0 (won't qualify as 'spike').
    """
    if not HIST_CSV.exists():
        return {}
    by_t = defaultdict(list)
    for r in csv.DictReader(HIST_CSV.open()):
        try:
            v = int(float(r.get('volume') or 0))
        except (TypeError, ValueError):
            v = 0
        by_t[r['ticker']].append((r['date'], float(r['close']), v))
    return {t: sorted(rows) for t, rows in by_t.items()}


def load_daily_ohlc():
    """Return dict[ticker] -> [{date, open, high, low, close, volume}, ...] sorted ascending."""
    if not DAILY_CSV.exists():
        return {}
    by_t = defaultdict(list)
    for r in csv.DictReader(DAILY_CSV.open()):
        try:
            by_t[r['ticker']].append({
                'date':   r['date'],
                'open':   float(r['open']) if r.get('open') else None,
                'high':   float(r['high']) if r.get('high') else None,
                'low':    float(r['low']) if r.get('low') else None,
                'close':  float(r['close']) if r.get('close') else None,
                'volume': int(float(r['volume'])) if r.get('volume') else None,
            })
        except Exception:
            continue
    return {t: sorted(rows, key=lambda x: x['date']) for t, rows in by_t.items()}


# =============================================================================
# F110 — stale-historical-cache guard (G-1 pre-run assertion, G-2 per-row age)
# Audit: 00_SYSTEM/AUDITS/260518_Rajiv-DIV_BULL_drift_audit.md
# =============================================================================
from datetime import timedelta as _timedelta


def _prev_business_day(d):
    """Most recent business day strictly before d (skips Sat/Sun; no holiday cal)."""
    x = d - _timedelta(days=1)
    while x.weekday() >= 5:
        x -= _timedelta(days=1)
    return x


def _business_days_between(d_from_iso, d_to):
    """Count business days strictly after d_from up to and incl. d_to. None on parse fail."""
    try:
        a = datetime.strptime(d_from_iso, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    if a >= d_to:
        return 0
    n = 0
    x = a
    while x < d_to:
        x += _timedelta(days=1)
        if x.weekday() < 5:
            n += 1
    return n


def historical_last_dates(historical):
    """dict ticker -> latest date string in historical_closes (or None)."""
    out = {}
    for t, rows in (historical or {}).items():
        out[t] = max((r[0] for r in rows), default=None) if rows else None
    return out


# Benchmark/ETF context series — mirror fetch_historical.SKIP_TICKERS. These are
# NOT screened candidates (no lens references them); they feed only the screener-map
# visual. The G-1 freshness gate protects SCREENED output, so it must exclude them
# from its denominator — otherwise their (deliberate) staleness false-aborts every run.
G1_NON_SCREENED = {'NIFTY', 'NIFTYBEES', 'BANKBEES', 'ITBEES', 'PHARMABEES', 'PSUBNKBEES', 'HDFCSML250'}


def historical_staleness(historical, run_date, threshold_frac=0.05):
    """G-1: flag if > threshold of SCREENED tickers are stale (last_date < run_date - 1 bday).
    Benchmark/ETF context series are excluded — the gate protects screened output, not display."""
    # F138: holiday-aware cutoff = last real NSE session before run_date (was _prev_business_day,
    # weekday-only -> false-aborted on NSE holidays like 2026-06-26 Muharram).
    cutoff = (_prev_nse_session(run_date) or _prev_business_day(run_date)).isoformat()
    last = {t: d for t, d in historical_last_dates(historical).items() if t not in G1_NON_SCREENED}
    stale = {t: d for t, d in last.items() if (d is None or d < cutoff)}
    total = len(last)
    frac = (len(stale) / total) if total else 0.0
    return {
        "cutoff": cutoff, "run_date": run_date.isoformat(),
        "total": total, "stale_count": len(stale), "stale_frac": frac,
        "stale_tickers": sorted(stale.keys()), "per_ticker_last": last,
        "threshold_frac": threshold_frac, "is_stale": frac > threshold_frac,
    }


def write_stale_flag(info, today):
    """Emit FLAG: STALE-HIST-CACHE + a stub output naming the stale tickers."""
    flags_dir = TRADER_ROOT / "00_SYSTEM" / "AGENTS" / "_FLAGS"
    flags_dir.mkdir(parents=True, exist_ok=True)
    yymmdd = today.strftime("%y%m%d")
    stub = flags_dir / f"{yymmdd}_STALE-HIST-CACHE.md"
    lines = [
        "# FLAG: STALE-HIST-CACHE (F110 G-1)",
        "",
        f"**Run date:** {info['run_date']}  ",
        f"**Freshness cutoff (run_date - 1 bday):** {info['cutoff']}  ",
        f"**Stale tickers:** {info['stale_count']} / {info['total']} "
        f"({info['stale_frac']*100:.0f}% > {info['threshold_frac']*100:.0f}% threshold) — RUN ABORTED  ",
        "",
        "Screener output NOT regenerated; consuming stale output is a documented bug (F110).",
        "Fix: re-run fetch_historical.py for the full universe through the latest NSE close, then re-run.",
        "Override (cite reason): screener_runner.py --skip-staleness.",
        "",
        "## Stale tickers (last_close_date)",
    ]
    for t in info["stale_tickers"]:
        lines.append(f"- {t}: {info['per_ticker_last'].get(t) or 'MISSING'}")
    stub.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return stub



# =============================================================================
# Technical computation helpers
# =============================================================================
def compute_sma(closes, n):
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def sma50_vs_sma200(historical_rows):
    """Return dict with SMA50, SMA200, gap_pct, sma50_5d_trend_pct."""
    closes = [c for _, c in historical_rows]
    sma50 = compute_sma(closes, 50)
    sma200 = compute_sma(closes, 200)
    if sma50 is None or sma200 is None:
        return {}
    gap_pct = (sma50 - sma200) / sma200 * 100
    sma50_5d_ago = compute_sma(closes[:-5] if len(closes) > 5 else closes, 50) if len(closes) >= 55 else None
    trend_pct = ((sma50 - sma50_5d_ago) / sma50_5d_ago * 100) if sma50_5d_ago else None
    return {'sma50': sma50, 'sma200': sma200, 'gap_pct': gap_pct, 'trend5d_pct': trend_pct}


def range_pct_10d(historical_rows):
    """% range over last 10 closes: (max - min) / min * 100."""
    closes = [c for _, c in historical_rows[-10:]]
    if len(closes) < 10:
        return None
    return (max(closes) - min(closes)) / min(closes) * 100


def bollinger_squeeze(historical_rows, period=20, lookback=120):
    """Return True if current Bollinger band-width is in the bottom 20% of the lookback window.

    BB-width = (BB upper - BB lower) / SMA = (4 * stddev) / SMA (using 2-sigma bands).
    """
    closes = [c for _, c in historical_rows]
    if len(closes) < period + lookback:
        return None
    widths = []
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1:i + 1]
        m = sum(window) / period
        var = sum((x - m) ** 2 for x in window) / period
        sd = var ** 0.5
        if m > 0:
            widths.append((sd / m) * 100)  # normalized as % of price
    recent_widths = widths[-lookback:]
    if not recent_widths:
        return None
    cur = widths[-1]
    sorted_widths = sorted(recent_widths)
    pctile_20 = sorted_widths[int(len(sorted_widths) * 0.2)]
    return cur <= pctile_20


def inside_candle(daily_rows):
    """True if last-complete-day's high < day-before's high AND low > day-before's low.

    Skips incomplete rows (today's row often has only `open` populated until
    the close-pull runs at 3:40 PM IST). Compares the last TWO COMPLETE days.
    """
    complete = [r for r in daily_rows if r.get('high') is not None and r.get('low') is not None]
    if len(complete) < 2:
        return None
    today = complete[-1]
    yest = complete[-2]
    return today['high'] < yest['high'] and today['low'] > yest['low']


def stddev_pct_10d(historical_rows):
    """Standard deviation of last-10 closes as % of mean (proxy for squeeze tightness)."""
    closes = [c for _, c in historical_rows[-10:]]
    if len(closes) < 10:
        return None
    m = sum(closes) / len(closes)
    if m == 0:
        return None
    var = sum((c - m) ** 2 for c in closes) / len(closes)
    return ((var ** 0.5) / m) * 100


def rsi(closes, period=14):
    """Standard 14-period RSI on a closes list. Returns latest RSI (None if insufficient data)."""
    if len(closes) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def volume_spike_count(historical_with_vol, lookback=10, baseline_window=30, threshold_mult=1.0):
    """Count how many of the last `lookback` days had volume > `threshold_mult` * (baseline_window-day avg vol).

    Args
    ----
    historical_with_vol  : list of (date, close, volume) tuples sorted ascending.
    lookback             : N most-recent days to test against the baseline (default 10).
    baseline_window      : avg-volume window length, taken BEFORE the lookback window
                           so the spike days don't pollute their own baseline (default 30).
    threshold_mult       : volume must be strictly greater than mult * baseline_avg.
                           Dad's spec: "higher than 30-day average" -> mult=1.0.

    Returns
    -------
    dict with keys: count (int or None), baseline_avg (float or None),
                    last10_avg (float or None), last10_max (int or None).
    Returns None-filled dict if insufficient history.
    """
    if not historical_with_vol or len(historical_with_vol) < lookback + baseline_window:
        return {'count': None, 'baseline_avg': None, 'last10_avg': None, 'last10_max': None}
    vols = [v for _, _, v in historical_with_vol]
    last_n = vols[-lookback:]
    baseline = vols[-(lookback + baseline_window):-lookback]
    if not baseline:
        return {'count': None, 'baseline_avg': None, 'last10_avg': None, 'last10_max': None}
    base_avg = sum(baseline) / len(baseline)
    if base_avg <= 0:  # currency / index symbols with synthetic 0 volume
        return {'count': None, 'baseline_avg': base_avg, 'last10_avg': None, 'last10_max': None}
    spikes = sum(1 for v in last_n if v > threshold_mult * base_avg)
    return {
        'count':        spikes,
        'baseline_avg': base_avg,
        'last10_avg':   sum(last_n) / len(last_n),
        'last10_max':   max(last_n),
    }


def rsi_divergence_simple(historical_rows, lookback=10):
    """Crude BULLISH RSI divergence only: price LL vs N days ago + RSI HL today.
    Returns True / False / None. Used by Dad #1 (which canonically wants bullish setups).
    """
    closes = [c for _, c in historical_rows]
    if len(closes) < lookback + 14:
        return None
    today_close = closes[-1]
    past_close = closes[-1 - lookback]
    today_rsi = rsi(closes)
    past_rsi = rsi(closes[:-lookback])
    if today_rsi is None or past_rsi is None:
        return None
    return today_close < past_close and today_rsi > past_rsi


def rsi_divergence_either(historical_rows, lookback=10):
    """RSI divergence in EITHER direction over N days. Used by Dad #2.

    BULLISH : price LL (today < past) + RSI HL (today_rsi > past_rsi)
              -> momentum strengthening despite price weakness; potential bottom.
    BEARISH : price HH (today > past) + RSI LH (today_rsi < past_rsi)
              -> momentum weakening despite price strength; potential top.

    Dad's #2 spec just says "RSI divergence" without specifying direction, so we
    accept either. The 'kind' field in the return surfaces which one fired so the
    operator can read the report directionally.

    Returns dict:
      present     : True / False / None
      kind        : 'BULL' / 'BEAR' / None
      today_close, past_close, today_rsi, past_rsi  : raw values for audit.
    """
    closes = [c for _, c in historical_rows]
    if len(closes) < lookback + 14:
        return {'present': None, 'kind': None, 'today_close': None,
                'past_close': None, 'today_rsi': None, 'past_rsi': None}
    today_c = closes[-1]
    past_c = closes[-1 - lookback]
    t_rsi = rsi(closes)
    p_rsi = rsi(closes[:-lookback])
    if t_rsi is None or p_rsi is None:
        return {'present': None, 'kind': None, 'today_close': today_c,
                'past_close': past_c, 'today_rsi': t_rsi, 'past_rsi': p_rsi}
    bull = today_c < past_c and t_rsi > p_rsi
    bear = today_c > past_c and t_rsi < p_rsi
    kind = 'BULL' if bull else ('BEAR' if bear else None)
    return {'present': bull or bear, 'kind': kind,
            'today_close': today_c, 'past_close': past_c,
            'today_rsi': t_rsi, 'past_rsi': p_rsi}


# =============================================================================
# Screener specs
# =============================================================================
def screener_rajiv_10g(ticker, fund, hist_rows, daily_rows):
    """Rajiv's screener #1. Returns dict[gate_name] -> bool/None + meta.

    Gates:
      G1 sector       : ticker in Power/IT/NBFC
      G2 cap_smid     : market cap small or mid (≤ 20K Cr)
      G3 peg_lt_1     : PEG (3y) < 1
      G4 roce_gt_15   : ROCE > 15
      G5 roe_gt_15    : ROE > 15
      G6 range_3pct   : 10-day price range within 3%
      G7 inside_candle: today's candle inside yesterday's
      G8 rsi_div      : bullish RSI divergence (crude impl)
      G9 bb_squeeze   : Bollinger band width in bottom 20% of 6mo
      G10 sma_setup   : SMA50 slightly above SMA200 (within 10%) AND SMA50 trending up over 5d
    """
    sectors = sectors_for(ticker)
    mc = f(fund.get('market_cap_cr'))
    csz = cap_size(mc)
    sma_data = sma50_vs_sma200(hist_rows or [])
    range_pct = range_pct_10d(hist_rows or [])
    rsi_div = rsi_divergence_simple(hist_rows or [])
    bb_sq = bollinger_squeeze(hist_rows or []) if hist_rows else None
    inside = inside_candle(daily_rows or []) if daily_rows else None

    g1 = any(s in {'POWER', 'IT', 'NBFC'} for s in sectors)
    g2 = csz in {'small', 'mid'}
    peg = f(fund.get('peg_3y'))
    g3 = peg is not None and 0 < peg < 1.5  # operator-confirmed 2026-04-28 (was <1)
    g4 = (f(fund.get('roce')) or 0) > 15
    g5 = (f(fund.get('roe')) or 0) > 15
    g6 = range_pct is not None and range_pct < 3
    g7 = inside  # may be None if insufficient daily data
    g8 = rsi_div  # may be None
    g9 = bb_sq    # may be None
    sma50_above = sma_data.get('sma50') and sma_data.get('sma200') and sma_data['sma50'] > sma_data['sma200']
    sma50_slight = sma50_above and sma_data.get('gap_pct') is not None and sma_data['gap_pct'] <= 10
    sma50_uptrend = sma_data.get('trend5d_pct') is not None and sma_data['trend5d_pct'] > 0
    g10 = sma50_slight and sma50_uptrend

    gates = {
        'G1_sector':       g1,
        'G2_cap_smid':     g2,
        'G3_peg_lt_1_5':   g3,
        'G4_roce_gt_15':   g4,
        'G5_roe_gt_15':    g5,
        'G6_range_3pct':   g6,
        'G7_inside_candle': g7,
        'G8_rsi_div':      g8,
        'G9_bb_squeeze':   g9,
        'G10_sma_setup':   g10,
    }
    meta = {
        'sector': '/'.join(sectors) or '-',
        'cap_size': csz,
        'mcap_cr': mc,
        'pe': fund.get('pe'),
        'peg_3y': fund.get('peg_3y'),
        'roce': fund.get('roce'),
        'roe': fund.get('roe'),
        'range_10d_pct': range_pct,
        'sma50': sma_data.get('sma50'),
        'sma200': sma_data.get('sma200'),
        'sma_gap_pct': sma_data.get('gap_pct'),
        'sma50_trend5d_pct': sma_data.get('trend5d_pct'),
    }
    return gates, meta


def screener_sanjeev_8g(ticker, fund, hist_rows, daily_rows):
    """Tauji's standard 8-gate fundamental screener. ROIC deferred (not on screener.in)."""
    prom = f(fund.get('promoter_pct'))
    pe = f(fund.get('pe'))
    de = f(fund.get('de_ratio'))
    roce = f(fund.get('roce'))
    roe = f(fund.get('roe'))
    opm = f(fund.get('opm_pct')) or f(fund.get('financing_margin_pct'))
    sg3 = f(fund.get('sales_g_3y'))
    icr = f(fund.get('icr'))
    mcs = f(fund.get('mcap_to_sales'))

    gates = {
        'G1_promoter_ge_50':  prom is not None and prom >= 50,
        'G2_pe_le_20':        pe is not None and pe <= 20,
        'G3_de_le_0_5':       de is not None and de <= 0.5,
        'G4_roce_ge_20':      roce is not None and roce >= 20,
        'G4b_roe_ge_20':      roe is not None and roe >= 20,
        'G5_opm_ge_20':       opm is not None and opm >= 20,
        'G6_sg3y_ge_20':      sg3 is not None and sg3 >= 20,
        'G7_icr_ge_5':        icr is not None and icr >= 5,
        'G8_mcs_le_2':        mcs is not None and mcs <= 2,
    }
    meta = {
        'promoter_pct': prom, 'pe': pe, 'de_ratio': de, 'roce': roce, 'roe': roe,
        'opm': opm, 'sales_g_3y': sg3, 'icr': icr, 'mcap_to_sales': mcs,
        'mcap_cr': f(fund.get('market_cap_cr')),
        'cap_size': cap_size(f(fund.get('market_cap_cr'))),
    }
    return gates, meta


def screener_rajiv_div(ticker, fund, hist_rows, daily_rows, hist_vol_rows=None):
    """Rajiv's screener #2 -- pure technical "squeeze + volume + RSI div".

    Operator-relayed spec (2026-04-28, from Rajiv's WhatsApp):
        "Finding a squeeze in the last 10 days defined as the standard
         deviation during those 10 days is about 3 % with a few days of
         volume higher than 30 day average volume and with rsi divergence."

    Gate operationalization
    -----------------------
    G1 stddev_le_3pct : 10-day stddev of closes ≤ 3% of mean   (squeeze)
    G2 volume_spike   : ≥2 of last 10 days have volume > 30d avg
                        (where 30d window = the 30 days BEFORE the lookback,
                         so spike days don't pollute their own baseline)
    G3 rsi_div        : RSI divergence in EITHER direction over 10d
                        (BULL: price LL + RSI HL; BEAR: price HH + RSI LH).
                        Direction is captured in meta.rsi_div_kind.

    Universe note
    -------------
    Volume gate skips currency / index proxies (USDINR, DXY, TNX, XAGUSD)
    where Yahoo returns volume=0; gate evaluates to None for those.

    Returns
    -------
    (gates, meta) tuple, same shape as other screener_* functions.
    """
    sd_pct = stddev_pct_10d(hist_rows or [])
    vol_data = volume_spike_count(hist_vol_rows or [])
    rsi_data = rsi_divergence_either(hist_rows or [])

    g1 = sd_pct is not None and sd_pct <= 3
    if vol_data['count'] is None:
        g2 = None  # insufficient history OR symbol has no volume (FX/index)
    else:
        g2 = vol_data['count'] >= 2
    g3 = rsi_data['present']  # True / False / None

    gates = {
        'G1_stddev_le_3pct':  g1,
        'G2_volume_spike':    g2,
        'G3_rsi_divergence':  g3,
    }
    sectors = sectors_for(ticker)
    meta = {
        'sector':           '/'.join(sectors) or '-',
        'mcap_cr':          f(fund.get('market_cap_cr')),
        'cap_size':         cap_size(f(fund.get('market_cap_cr'))),
        'stddev_10d_pct':   sd_pct,
        'vol_spike_days':   vol_data['count'],
        'vol_30d_avg':      vol_data['baseline_avg'],
        'vol_last10_avg':   vol_data['last10_avg'],
        'vol_last10_max':   vol_data['last10_max'],
        'rsi_div_kind':     rsi_data['kind'] or '-',
        'rsi_today':        rsi_data['today_rsi'],
        'rsi_10d_ago':      rsi_data['past_rsi'],
    }
    return gates, meta


SCREENER_REGISTRY = {
    'Rajiv-10G': {
        'fn': screener_rajiv_10g,
        'name': 'Rajiv-10G -- Power/IT/NBFC sector + PEG/ROCE/ROE quality + technical setup',
        'all_gates_required': True,  # candidate = passes ALL gates
        'needs_volume': False,
    },
    'Rajiv-DIV': {
        'fn': screener_rajiv_div,
        'name': 'Rajiv-DIV -- pure technical: 10d squeeze + volume spikes + RSI divergence',
        'all_gates_required': True,
        'needs_volume': True,
    },
    'Sanjeev-8G': {
        'fn': screener_sanjeev_8g,
        'name': 'Sanjeev 8-gate fundamental -- 8 quantitative fundamental gates',
        'all_gates_required': True,
        'needs_volume': False,
    },
}


# =============================================================================
# Runner
# =============================================================================
def run_screener(name, fundamentals, historical, daily, historical_with_vol=None, hist_last=None, run_date=None):
    spec = SCREENER_REGISTRY[name]
    hist_last = hist_last if hist_last is not None else historical_last_dates(historical)
    rows = []
    for ticker in sorted(fundamentals.keys()):
        fund = fundamentals[ticker]
        if fund.get('status') != 'ok':
            continue  # skip skipped tickers
        if spec.get('needs_volume'):
            gates, meta = spec['fn'](
                ticker, fund, historical.get(ticker), daily.get(ticker),
                hist_vol_rows=(historical_with_vol or {}).get(ticker),
            )
        else:
            gates, meta = spec['fn'](ticker, fund, historical.get(ticker), daily.get(ticker))
        # Count gates that are explicitly True (None counts as not-passed for this screener)
        passes = sum(1 for v in gates.values() if v is True)
        total = len(gates)
        passes_all = all(v is True for v in gates.values())
        # G-2: per-row freshness annotation (F110)
        _lcd = hist_last.get(ticker)
        meta['last_close_date'] = _lcd
        meta['data_age_business_days'] = (_business_days_between(_lcd, run_date) if (run_date and _lcd) else None)
        rows.append({
            'ticker': ticker,
            'passes_all': passes_all,
            'pass_count': passes,
            'total_gates': total,
            'gates': gates,
            'meta': meta,
        })
    return rows


def write_outputs(name, rows, today):
    spec = SCREENER_REGISTRY[name]
    outdir = OUT_ROOT / name
    outdir.mkdir(parents=True, exist_ok=True)
    # 2026-05-01 restructure: .md companion files now go into BACKEND/ subfolder
    # to keep the variant root cleaner (CSV is the primary consumed output).
    md_outdir = outdir / "BACKEND"
    md_outdir.mkdir(parents=True, exist_ok=True)
    yymmdd = today.strftime('%y%m%d')

    # CSV: ticker,pass_count,total_gates,passes_all,<each gate>,<each meta key>
    # Filename uses screener key as-is (mixed case e.g. Rajiv-10G) per nomenclature v2 (2026-05-05).
    csv_path = outdir / f"{yymmdd}_{name}.csv"
    if not rows:
        return None, None
    gate_keys = list(rows[0]['gates'].keys())
    meta_keys = list(rows[0]['meta'].keys())
    with csv_path.open('w', encoding='utf-8', newline='') as f:
        cw = csv.writer(f)
        cw.writerow(['ticker', 'passes_all', 'pass_count', 'total_gates'] + gate_keys + meta_keys)
        for r in sorted(rows, key=lambda x: (-x['pass_count'], x['ticker'])):
            cw.writerow(
                [r['ticker'], int(r['passes_all']), r['pass_count'], r['total_gates']]
                + [('' if r['gates'][k] is None else int(r['gates'][k])) for k in gate_keys]
                + [r['meta'].get(k, '') for k in meta_keys]
            )

    # Markdown report
    md_path = md_outdir / f"{yymmdd}_{name}.md"
    candidates = [r for r in rows if r['passes_all']]
    # Near-miss threshold: missing at most 1 gate, but cap so it's meaningful
    # for both small (3-gate) and large (10-gate) screeners.
    n_gates = len(gate_keys)
    near_threshold = max(1, n_gates - 1) if n_gates <= 4 else max(3, n_gates - 2)
    near_misses = [r for r in rows if not r['passes_all'] and r['pass_count'] >= near_threshold]
    with md_path.open('w', encoding='utf-8') as f:
        f.write(f"# {spec['name']}\n\n")
        f.write(f"**Run:** {today.isoformat()}  \n")
        f.write(f"**Universe:** {len(rows)} names (from /00_SYSTEM/GENERATORS/_cache/fundamentals.csv)  \n")
        f.write(f"**Gates:** {len(gate_keys)}  \n")
        f.write(f"**Cleanly passing all gates:** **{len(candidates)}**  \n")
        f.write(f"**Near-misses (within 2 gates of full pass):** {len(near_misses)}  \n\n")
        f.write("---\n\n")
        if candidates:
            f.write("## Candidates (pass ALL gates)\n\n")
            for r in candidates:
                m = r['meta']
                # Build a one-line "fact strip" from whichever meta keys are present.
                # Surfaces direction (rsi_div_kind), squeeze tightness, volume burst, etc.
                facts = [f"mcap {m.get('mcap_cr', '?')} Cr ({m.get('cap_size', '?')})"]
                if m.get('sector') and m['sector'] != '-':
                    facts.append(f"sector {m['sector']}")
                if m.get('stddev_10d_pct') is not None:
                    facts.append(f"stddev10d {m['stddev_10d_pct']:.2f}%")
                if m.get('vol_spike_days') is not None:
                    facts.append(f"vol-spike days {m['vol_spike_days']}/10")
                if m.get('rsi_div_kind') and m['rsi_div_kind'] != '-':
                    facts.append(f"RSI-div {m['rsi_div_kind']}")
                f.write(f"- **{r['ticker']}** -- " + "; ".join(facts) + "\n")
            f.write("\n")
        else:
            f.write("## Candidates (pass ALL gates)\n\n_None this run._\n\n")
        f.write("## Near-misses\n\n")
        for r in sorted(near_misses, key=lambda x: -x['pass_count']):
            failed = [k for k, v in r['gates'].items() if v is not True]
            f.write(f"- **{r['ticker']}** ({r['pass_count']}/{len(gate_keys)}; failed: {', '.join(failed)})\n")
        f.write("\n## Full pass-table\n\n")
        f.write("| Ticker | Pass | " + " | ".join(g.replace('_', ' ') for g in gate_keys) + " |\n")
        f.write("|" + "---|" * (2 + len(gate_keys)) + "\n")
        for r in sorted(rows, key=lambda x: (-x['pass_count'], x['ticker'])):
            check = lambda v: '✓' if v is True else ('✗' if v is False else '·')
            _age = r['meta'].get('data_age_business_days')
            _warn = (f"⚠ stale {_age}bd " if (_age is not None and _age > 1) else "")
            f.write(f"| {_warn}{r['ticker']} | {r['pass_count']}/{len(gate_keys)} | "
                    + " | ".join(check(r['gates'][g]) for g in gate_keys) + " |\n")

    # last_run.json — health-tracking artifact (added 2026-05-05 per [[F83_last-run-status-tracking]])
    # Lets SYSTEM_MAP and dashboard surface staleness without parsing every CSV.
    import json as _json
    from datetime import datetime as _dt2
    last_run = {
        "screener": name,
        "ran_at": _dt2.now().astimezone().isoformat(timespec='seconds'),
        "run_date": today.isoformat(),
        "rows_evaluated": len(rows),
        "candidates_passed": len(candidates),
        "near_misses": len(near_misses),
        "n_gates": n_gates,
        "csv_path": str(csv_path),
        "md_path": str(md_path),
        "status": "ok",
    }
    # Atomic write — prevents truncated last_run.json on mid-write interruption
    # (260603 broken-lens incident; see atomic_io.py).
    from atomic_io import atomic_write_json
    atomic_write_json(outdir / "last_run.json", last_run, indent=2)
    return md_path, csv_path


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument('--screener', help='Run one specific screener (default: all)')
    ap.add_argument('--list', action='store_true', help='List available screeners + exit')
    ap.add_argument('--skip-staleness', action='store_true',
                    help='F110 G-1 override: proceed despite stale historical_closes cache (CITE REASON)')
    ap.add_argument('--all-lenses', action='store_true',
                    help='Run every lens (default behaviour; accepted for scheduler/catchup compatibility)')
    args = ap.parse_args()

    # TA-Lib preflight — added 2026-05-15 per audit Tier 1.3.
    # Kaarin-TA's engulfing pattern silently returned 'TALIB-NOT-INSTALLED' before this;
    # one of five mechanical gates was dead and the screener didn't tell on itself.
    # Now: if Kaarin-TA is in the run list, talib MUST import or we halt loudly.
    _kaarin_in_run = (
        args.screener == 'Kaarin-TA'
        or (not args.screener and 'Kaarin-TA' in SCREENER_REGISTRY)
    )
    if _kaarin_in_run:
        try:
            import talib  # noqa: F401
            import numpy as _np  # noqa: F401
        except ImportError as _ie:
            import sys as _sys
            print("=" * 72, file=_sys.stderr)
            print("PRE-FLIGHT FAIL: screener_runner.py / Kaarin-TA", file=_sys.stderr)
            print("=" * 72, file=_sys.stderr)
            print(f"  TA-Lib not importable: {_ie}", file=_sys.stderr)
            print("  Kaarin-TA's CDLENGULFING gate (G5) requires the talib library.", file=_sys.stderr)
            print("  Fix: pip install TA-Lib --break-system-packages", file=_sys.stderr)
            print("  Then re-run. Halting per fail-closed discipline (was fail-open before 2026-05-15).", file=_sys.stderr)
            print("=" * 72, file=_sys.stderr)
            return 2

    if args.list:
        for k, v in SCREENER_REGISTRY.items():
            print(f"  {k:20s}  {v['name']}")
        return 0

    print(f"Loading caches...")
    fundamentals = load_fundamentals()
    historical = load_historical_closes()
    historical_v = load_historical_with_volume()
    daily = load_daily_ohlc()
    print(f"  fundamentals:        {len(fundamentals)} tickers")
    print(f"  historical (close):  {len(historical)} tickers")
    print(f"  historical (+vol):   {len(historical_v)} tickers")
    print(f"  daily ohlc:          {len(daily)} tickers")

    # F131 (260616) NSE-session gate: stamp/date on the latest real session, never a weekend/
    # holiday. A weekend run folds onto Friday's session (idempotent re-stamp) instead of
    # minting a bogus Sat/Sun lens CSV that the signal ledger would count as a phantom cohort.
    _cal = datetime.now().date()
    today = _latest_nse_session(_cal) or _cal
    if today != _cal:
        print(f"  [session-gate] {_cal} is a non-NSE session -> stamping as latest session {today}")
    # F110 G-1: pre-run staleness assertion on historical_closes.csv
    _stale = historical_staleness(historical, today)
    print(f"  hist freshness:      {_stale['total']-_stale['stale_count']}/{_stale['total']} fresh "
          f"(cutoff {_stale['cutoff']}); {_stale['stale_count']} stale ({_stale['stale_frac']*100:.0f}%)")
    if _stale['is_stale'] and not args.skip_staleness:
        _stub = write_stale_flag(_stale, today)
        print('=' * 72)
        print('F110 G-1 ABORT: historical_closes.csv is STALE — screener NOT run.')
        print(f"  {_stale['stale_count']}/{_stale['total']} tickers "
              f"({_stale['stale_frac']*100:.0f}%) older than {_stale['cutoff']} (>5% threshold).")
        print(f'  Flag written: {_stub}')
        print('  Fix: re-run fetch_historical.py through latest NSE close, then re-run.')
        print('  Override: --skip-staleness (cite reason).')
        print('=' * 72)
        return 3
    if _stale['is_stale']:
        print(f"  WARN: cache stale ({_stale['stale_frac']*100:.0f}%) but --skip-staleness set; proceeding.")
    targets = [args.screener] if args.screener else list(SCREENER_REGISTRY.keys())
    for name in targets:
        if name not in SCREENER_REGISTRY:
            print(f"  ERROR: unknown screener '{name}' (use --list to see available)")
            continue
        rows = run_screener(name, fundamentals, historical, daily, historical_with_vol=historical_v, hist_last=_stale['per_ticker_last'], run_date=today)
        md, csvp = write_outputs(name, rows, today)
        n_pass = sum(1 for r in rows if r['passes_all'])
        print(f"\n[{name}] {n_pass} candidates passing all gates; {len(rows)} evaluated")
        if md:
            print(f"  -> {md}")
            print(f"  -> {csvp}")
    return 0




# =============================================================================
# Kaarin-TA chart confluence screener (added 2026-05-02)
# =============================================================================
# Mechanical TA proxy for Kaarin's lens. v0 ships 4 gates; >=3 = HIGH-CONF.
# Per /00_SYSTEM/REFERENCES/CONVERSATIONS/KAARIN/LENS_ANALYSIS_v1.md sec 2.1 + 3.1.
# Bullish-bias only in v0; bear-fire detection deferred to v0.5/v1.
# Universe: same 65 names as the other screeners (operator-decided 2026-05-02).
# Calibration: placeholder weights for SCREENER-REPORT composite; first cycle
# mid-May after >=10 NSE sessions accumulate in OUTCOME_AUDIT.

def _ema(closes, period):
    """Latest EMA on a closes list. Returns None if insufficient data."""
    if not closes or len(closes) < period:
        return None
    multiplier = 2.0 / (period + 1)
    # Seed with SMA over first `period` closes
    ema_val = sum(closes[:period]) / period
    for c in closes[period:]:
        ema_val = (c - ema_val) * multiplier + ema_val
    return ema_val


def _weekly_close_series(historical_rows):
    """Resample daily (date, close) rows to weekly (last close per ISO week).
    Returns chronological list of weekly closes."""
    if not historical_rows:
        return []
    from collections import OrderedDict
    from datetime import datetime as _dt
    by_week = OrderedDict()
    for d, c in historical_rows:
        try:
            dt = d if hasattr(d, 'isocalendar') else _dt.strptime(str(d), '%Y-%m-%d').date()
        except Exception:
            continue
        iy, iw, _ = dt.isocalendar()
        by_week[(iy, iw)] = c  # last close in the week wins (input is chronological)
    return list(by_week.values())


def _monthly_close_series(historical_rows):
    """Resample daily (date, close) rows to monthly (last close per calendar month)."""
    if not historical_rows:
        return []
    from collections import OrderedDict
    from datetime import datetime as _dt
    by_month = OrderedDict()
    for d, c in historical_rows:
        try:
            dt = d if hasattr(d, 'year') else _dt.strptime(str(d), '%Y-%m-%d').date()
        except Exception:
            continue
        by_month[(dt.year, dt.month)] = c
    return list(by_month.values())


def _kaarin_ma_stack_w(weekly_closes):
    """Pillar 1: weekly close > 21EMA AND > 50SMA AND > 200EMA (bullish stack reclaimed)."""
    if len(weekly_closes) < 50:
        return None, {'ema21_w': None, 'sma50_w': None, 'ema200_w': None}
    last = weekly_closes[-1]
    ema21 = _ema(weekly_closes, 21)
    sma50 = sum(weekly_closes[-50:]) / 50
    # 1Y of daily data => ~52 weekly bars; weekly 200EMA not computable.
    # Substitute weekly 40EMA as long-trend proxy (best we can do at current data depth).
    ema_long = _ema(weekly_closes, 40) if len(weekly_closes) >= 40 else None
    if ema21 is None or ema_long is None:
        return None, {'ema21_w': ema21, 'sma50_w': sma50, 'ema200_w_proxy': ema_long}
    bullish = (last > ema21) and (last > sma50) and (last > ema_long)
    return bullish, {'ema21_w': ema21, 'sma50_w': sma50, 'ema200_w_proxy': ema_long}


def _kaarin_rsi_momentum_w(weekly_closes):
    """Pillar 2: weekly RSI(14) > 50 AND rising over last 4 weeks."""
    if len(weekly_closes) < 18:  # 14 RSI + 4 lookback
        return None, {'rsi_w': None, 'rsi_w_4w_ago': None}
    rsi_now = rsi(weekly_closes)
    rsi_past = rsi(weekly_closes[:-4])
    if rsi_now is None or rsi_past is None:
        return None, {'rsi_w': rsi_now, 'rsi_w_4w_ago': rsi_past}
    bullish = (rsi_now > 50) and (rsi_now > rsi_past)
    return bullish, {'rsi_w': rsi_now, 'rsi_w_4w_ago': rsi_past}


def _kaarin_multi_tf_align(historical_rows):
    """Pillar 3: daily > daily 200EMA AND weekly > weekly 30EMA-proxy AND monthly > monthly 6EMA-proxy.
    1Y data => no true 200EMA-weekly or 200EMA-monthly; proxies stand in until 3Y backfill."""
    if not historical_rows:
        return None, {'ema200_d': None, 'ema_long_w': None, 'ema_long_m': None}
    daily_closes = [c for _, c in historical_rows]
    if len(daily_closes) < 200:
        return None, {'ema200_d': None, 'ema_long_w': None, 'ema_long_m': None}
    daily_last = daily_closes[-1]
    daily_ema200 = _ema(daily_closes, 200)

    weekly = _weekly_close_series(historical_rows)
    if len(weekly) < 30:
        return None, {'ema200_d': daily_ema200, 'ema_long_w': None, 'ema_long_m': None}
    weekly_last = weekly[-1]
    weekly_ema_long = _ema(weekly, 30)

    monthly = _monthly_close_series(historical_rows)
    if len(monthly) < 6:
        return None, {'ema200_d': daily_ema200, 'ema_long_w': weekly_ema_long, 'ema_long_m': None}
    monthly_last = monthly[-1]
    monthly_ema_long = _ema(monthly, 6)

    if daily_ema200 is None or weekly_ema_long is None or monthly_ema_long is None:
        return None, {'ema200_d': daily_ema200, 'ema_long_w': weekly_ema_long, 'ema_long_m': monthly_ema_long}

    bullish = (daily_last > daily_ema200) and (weekly_last > weekly_ema_long) and (monthly_last > monthly_ema_long)
    return bullish, {'ema200_d': daily_ema200, 'ema_long_w': weekly_ema_long, 'ema_long_m': monthly_ema_long}


def _kaarin_aoi_proximity(historical_rows, threshold_pct=3.0):
    """Pillar 4: current price within +-threshold% of any of: 60D high, 60D low, 52W high, 52W low.
    'Area of Interest' proxy. Both highs (resistance test) and lows (bounce zone) qualify in v0."""
    if not historical_rows or len(historical_rows) < 60:
        return None, {'swing_high_60d': None, 'swing_low_60d': None, 'w52_high': None,
                       'w52_low': None, 'nearest_aoi_pct': None, 'nearest_aoi_kind': None}
    closes = [c for _, c in historical_rows]
    last = closes[-1]
    swing_high_60d = max(closes[-60:])
    swing_low_60d = min(closes[-60:])
    w52_window = min(252, len(closes))
    w52_high = max(closes[-w52_window:])
    w52_low = min(closes[-w52_window:])
    candidates = [
        ('60D_high', swing_high_60d),
        ('60D_low',  swing_low_60d),
        ('52W_high', w52_high),
        ('52W_low',  w52_low),
    ]
    distances = [(name, abs((last - lvl) / last * 100), lvl) for name, lvl in candidates]
    distances.sort(key=lambda x: x[1])
    nearest_kind, nearest_pct, _ = distances[0]
    fired = nearest_pct <= threshold_pct
    return fired, {
        'swing_high_60d':   swing_high_60d,
        'swing_low_60d':    swing_low_60d,
        'w52_high':         w52_high,
        'w52_low':          w52_low,
        'nearest_aoi_pct':  nearest_pct,
        'nearest_aoi_kind': nearest_kind,
    }



def _kaarin_engulfing_pattern(daily_rows):
    """Pillar 1 extension (G5 v0.5): TA-Lib CDLENGULFING on recent OHLC bars.
    Returns (fired, meta_dict). Fires TRUE if recent CDLENGULFING bullish (+>0),
    FALSE if bearish (<0), None if no fire OR insufficient OHLC bars.
    Wired 2026-05-03 per operator after TA-Lib smoke-test validation.
    Note: requires daily_prices.csv to be fresh; with current ~5-6 bars/ticker,
    pattern needs at least 2 consecutive bars to compute. Cache extension to full
    1Y OHLC is queued as follow-up task to enable broader pattern set later.
    """
    if not daily_rows or len(daily_rows) < 2:
        return None, {'engulfing_kind': None, 'engulfing_value': None, 'engulfing_date': None}
    try:
        import talib
        import numpy as np
    except ImportError:
        return None, {'engulfing_kind': 'TALIB-NOT-INSTALLED', 'engulfing_value': None, 'engulfing_date': None}
    o = np.array([r.get('open')  for r in daily_rows if r.get('open')  is not None], dtype=float)
    h = np.array([r.get('high')  for r in daily_rows if r.get('high')  is not None], dtype=float)
    l = np.array([r.get('low')   for r in daily_rows if r.get('low')   is not None], dtype=float)
    c = np.array([r.get('close') for r in daily_rows if r.get('close') is not None], dtype=float)
    if len(o) < 2 or len(h) < 2 or len(l) < 2 or len(c) < 2:
        return None, {'engulfing_kind': None, 'engulfing_value': None, 'engulfing_date': None}
    n = min(len(o), len(h), len(l), len(c))
    vals = talib.CDLENGULFING(o[:n], h[:n], l[:n], c[:n])
    # Find LATEST fire (scan backward)
    for i in range(len(vals) - 1, -1, -1):
        if vals[i] != 0:
            kind = 'BULL' if vals[i] > 0 else 'BEAR'
            d = daily_rows[i].get('date') if i < len(daily_rows) else None
            fired = vals[i] > 0  # gate fires TRUE on bullish only (matches Kaarin-TA bullish-bias convention)
            return fired, {'engulfing_kind': kind, 'engulfing_value': int(vals[i]), 'engulfing_date': d}
    return None, {'engulfing_kind': None, 'engulfing_value': None, 'engulfing_date': None}



def screener_kaarin_ta(ticker, fund, hist_rows, daily_rows):
    """Kaarin chart-confluence screener. v0. Bullish-only.

    Gates (fire TRUE if bullish):
      G1 ma_stack_w     : weekly close > 21EMA AND > 50SMA AND > 40EMA-proxy (200EMA when 3Y data lands)
      G2 rsi_momentum_w : weekly RSI(14) > 50 AND rising over last 4 weeks
      G3 multi_tf_align : daily > daily 200EMA AND weekly > 30EMA-proxy AND monthly > 6EMA-proxy
      G4 aoi_proximity  : current price within +-3% of any of: 60D high, 60D low, 52W high, 52W low

    Confluence count = sum of TRUE fires (0-4):
      4/4 = HIGH-CONF (passes_all = True; surfaces as candidate)
      3/4 = MEDIUM-CONF (near-miss)
      2/4 = LOW-CONF (also near-miss)
      <=1 = no setup

    Source: /00_SYSTEM/REFERENCES/CONVERSATIONS/KAARIN/LENS_ANALYSIS_v1.md
    """
    weekly = _weekly_close_series(hist_rows or [])
    g1_fired, g1_meta = _kaarin_ma_stack_w(weekly)
    g2_fired, g2_meta = _kaarin_rsi_momentum_w(weekly)
    g3_fired, g3_meta = _kaarin_multi_tf_align(hist_rows or [])
    g4_fired, g4_meta = _kaarin_aoi_proximity(hist_rows or [])

    g5_fired, g5_meta = _kaarin_engulfing_pattern(daily_rows or [])
    # v0.6 deepening (260602): HTF Supertrend (weekly) + HTF Fib proximity
    g6_fired, g6_st = _supertrend_bullish(_weekly_ohlc_from_closes(hist_rows or []))
    g7_fired, g7_meta = _fib_proximity([c for _, c in (hist_rows or [])])
    gates = {
        'ma_stack_w':       g1_fired,
        'rsi_momentum_w':   g2_fired,
        'multi_tf_align':   g3_fired,
        'aoi_proximity':    g4_fired,
        'engulfing_pattern': g5_fired,
        'htf_supertrend_w': g6_fired,
        'htf_fib_proximity': g7_fired,
    }
    meta = {}
    for m in (g1_meta, g2_meta, g3_meta, g4_meta, g5_meta, g7_meta):
        if m:
            meta.update(m)
    if g6_st is not None:
        meta['supertrend_w'] = g6_st
    meta['mcap_cr'] = fund.get('market_cap_cr') if fund else None
    meta['cap_size'] = cap_size(f(fund.get('market_cap_cr'))) if fund else None
    return gates, meta


# Register the screener (additive: keeps existing 3 entries untouched).
SCREENER_REGISTRY['Kaarin-TA'] = {
    'fn': screener_kaarin_ta,
    'name': 'Kaarin-TA chart confluence -- v0.6: MA stack + RSI momentum + multi-TF align + AOI proximity + CDLENGULFING + HTF Supertrend(W) + HTF Fib proximity (7/7 = HIGHEST-CONF)',
    'all_gates_required': True,  # 4/4 = HIGH-CONF candidate per LENS_ANALYSIS framework
    'needs_volume': False,
}



# =============================================================================
# Shared TA helpers (260602) — DayTrade lens + Kaarin deepening
# =============================================================================
_SR_CACHE = None


def load_sr_levels():
    """ticker -> [(kind, price), ...] from AUTO_SR_LEVELS.csv (cached)."""
    global _SR_CACHE
    if _SR_CACHE is not None:
        return _SR_CACHE
    path = TRADER_ROOT / "00_SYSTEM" / "AUTO_SR_LEVELS.csv"
    by = defaultdict(list)
    if path.exists():
        for r in csv.DictReader(path.open()):
            p = f(r.get('price'))
            if p is not None:
                by[r['ticker']].append((r.get('kind'), p))
    _SR_CACHE = dict(by)
    return _SR_CACHE


def _atr_from_ohlc(daily_rows, n=14):
    if not daily_rows or len(daily_rows) < n + 1:
        return None
    trs = []
    for i in range(1, len(daily_rows)):
        h, l, pc = daily_rows[i].get('high'), daily_rows[i].get('low'), daily_rows[i - 1].get('close')
        if None in (h, l, pc):
            continue
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return (sum(trs[-n:]) / n) if len(trs) >= n else None


def _weekly_ohlc_from_closes(hist_rows):
    """[(date,close)] -> weekly [(o,h,l,c)] (close-derived range proxy; no intraweek H/L)."""
    if not hist_rows:
        return []
    buckets, order = {}, []
    for ds, c in hist_rows:
        try:
            y, w, _ = datetime.strptime(ds, '%Y-%m-%d').isocalendar()
        except Exception:
            continue
        k = (y, w)
        if k not in buckets:
            buckets[k] = []
            order.append(k)
        buckets[k].append(c)
    return [(buckets[k][0], max(buckets[k]), min(buckets[k]), buckets[k][-1]) for k in order]


def _supertrend_bullish(bars, period=10, mult=3.0):
    """Standard Supertrend on (o,h,l,c) bars. Returns (is_bullish, st_value)."""
    n = len(bars) if bars else 0
    if n < period + 2:
        return None, None
    highs = [b[1] for b in bars]; lows = [b[2] for b in bars]; closes = [b[3] for b in bars]
    tr = [highs[0] - lows[0]]
    for i in range(1, n):
        tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    atr = [None] * n
    atr[period - 1] = sum(tr[:period]) / period
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    dir_ = [None] * n; st = [None] * n; fub = flb = None
    for i in range(period - 1, n):
        if atr[i] is None:
            continue
        mid = (highs[i] + lows[i]) / 2
        bub = mid + mult * atr[i]; blb = mid - mult * atr[i]
        if fub is None:
            fub, flb, dir_[i], st[i] = bub, blb, 1, blb
            continue
        fub = bub if (bub < fub or closes[i - 1] > fub) else fub
        flb = blb if (blb > flb or closes[i - 1] < flb) else flb
        prev = dir_[i - 1] if dir_[i - 1] is not None else 1
        dir_[i] = 1 if closes[i] > fub else (-1 if closes[i] < flb else prev)
        st[i] = flb if dir_[i] == 1 else fub
    return (dir_[-1] == 1), (round(st[-1], 2) if st[-1] is not None else None)


def _fib_proximity(closes, tol_pct=3.0, lookback=252):
    """Bullish-area confluence: price within tol% of a 52w 38.2/50/61.8 retracement."""
    if not closes or len(closes) < 30:
        return None, {}
    w = closes[-min(lookback, len(closes)):]
    hi, lo, last = max(w), min(w), closes[-1]
    if hi == lo:
        return None, {}
    levels = {'fib_382': hi - 0.382 * (hi - lo), 'fib_50': hi - 0.5 * (hi - lo), 'fib_618': hi - 0.618 * (hi - lo)}
    name, lvl = min(levels.items(), key=lambda kv: abs(last - kv[1]))
    dist = abs(last - lvl) / last * 100
    return (dist <= tol_pct), {'nearest_fib': name, 'fib_dist_pct': round(dist, 2), 'fib_level': round(lvl, 2)}


def _volume_poc(vol_rows, lookback=60, bins=20):
    """Volume-profile point-of-control price over lookback (close-as-price proxy)."""
    if not vol_rows or len(vol_rows) < 20:
        return None
    w = vol_rows[-min(lookback, len(vol_rows)):]
    prices = [c for _, c, _ in w]; vols = [v for _, _, v in w]
    lo, hi = min(prices), max(prices)
    if hi == lo:
        return None
    width = (hi - lo) / bins
    hist = [0.0] * bins
    for c, v in zip(prices, vols):
        hist[min(int((c - lo) / width), bins - 1)] += v
    poc = max(range(bins), key=lambda i: hist[i])
    return lo + (poc + 0.5) * width


def _cvd_proxy(daily_rows, lookback=5):
    """CVD PROXY (not true CVD — no tick data): signed daily volume by close-vs-prevclose."""
    if not daily_rows or len(daily_rows) < lookback + 1:
        return None, {}
    s = 0.0
    for i in range(len(daily_rows) - lookback, len(daily_rows)):
        c, pc, v = daily_rows[i].get('close'), daily_rows[i - 1].get('close'), (daily_rows[i].get('volume') or 0)
        if c is None or pc is None:
            continue
        s += (v if c >= pc else -v)
    return (s > 0), {'cvd_proxy_5d': int(s)}


# =============================================================================
# DayTrade-Confluence lens (260602) — EOD-DERIVED next-session setup.
# NOT live intraday: we have no L2/orderbook or tick data, so this is a daily-bar
# proxy for Kaarin's confluence method. CVD is a signed-volume PROXY; Orderbook
# is OUT OF SCOPE (documented). Feeds the WS-C day-trade trade-plan scaffold.
# =============================================================================
def screener_daytrade_confluence(ticker, fund, hist_rows, daily_rows, hist_vol_rows=None):
    meta = {}
    closes = [c for _, c in (hist_rows or [])]
    last = (daily_rows[-1]['close'] if (daily_rows and daily_rows[-1].get('close')) else (closes[-1] if closes else None))
    # G1 sr_proximity — within 1.5% of a daily S/R level (entry near structure)
    srs = [p for _, p in load_sr_levels().get(ticker, [])]
    g1 = None
    if last and srs:
        nearest = min(srs, key=lambda p: abs(p - last))
        d = abs(nearest - last) / last * 100
        g1 = d <= 1.5
        meta['sr_dist_pct'] = round(d, 2); meta['nearest_sr'] = round(nearest, 2)
    # G2 atr_room — tradeable daily range (1-6% ATR), not dead, not chaotic
    atr = _atr_from_ohlc(daily_rows or [])
    g2 = None
    if atr and last:
        atr_pct = atr / last * 100
        g2 = 1.0 <= atr_pct <= 6.0
        meta['atr14'] = round(atr, 2); meta['atr_pct'] = round(atr_pct, 2)
    # G3 daily_momentum — close>20EMA AND today's bar up
    g3 = None
    ema20 = _ema(closes, 20) if closes else None
    if last and ema20 and daily_rows:
        o, c = daily_rows[-1].get('open'), daily_rows[-1].get('close')
        g3 = (last > ema20) and (o is not None and c is not None and c > o)
        meta['ema20'] = round(ema20, 2)
    # G4 cvd_proxy — signed-volume delta bullish (PROXY)
    g4, m4 = _cvd_proxy(daily_rows or [])
    meta.update(m4)
    # G5 vol_profile_poc — within 2% of the 60d volume POC (acceptance node)
    g5 = None
    poc = _volume_poc(hist_vol_rows or [])
    if poc and last:
        d = abs(poc - last) / last * 100
        g5 = d <= 2.0
        meta['vol_poc'] = round(poc, 2); meta['poc_dist_pct'] = round(d, 2)
    gates = {'sr_proximity': g1, 'atr_room': g2, 'daily_momentum': g3, 'cvd_proxy': g4, 'vol_profile_poc': g5}
    meta['ltp'] = round(last, 2) if last else None
    meta['mcap_cr'] = fund.get('market_cap_cr') if fund else None
    meta['cap_size'] = cap_size(f(fund.get('market_cap_cr'))) if fund else None
    return gates, meta


SCREENER_REGISTRY['DayTrade-Confluence'] = {
    'fn': screener_daytrade_confluence,
    'name': 'DayTrade-Confluence -- EOD-derived intraday setup: S/R proximity + ATR room + daily momentum + CVD-proxy + volume-POC (5/5 = setup). NOT live intraday (no L2/tick).',
    'all_gates_required': True,
    'needs_volume': True,
}


if __name__ == '__main__':
    sys.exit(main())
# nomenclature v2 wired 2026-05-05
