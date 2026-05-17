#!/usr/bin/env python3
"""
yahoo_common.py  --  shared Yahoo-Finance access layer
=======================================================
Single source of truth for:
    * SCRN-MAP 71-ticker universe (ALL_TICKERS) — was 69 pre-2026-05-11; +2 AREM/CHAMBLFERT per F101 P3 wrap-up (closes held-position fundamentals gap)
    * Special ticker -> Yahoo symbol mappings (SPECIAL)
    * User-Agent string (UA)
    * fetch_chart() 1d/5d/14d helper
    * yahoo_symbols() fallback-list helper

Consumers:
    * fetch_weekly_chg.py   (Sunday SCRN-MAP refresh)
    * fetch_daily_ohlc.py   (Mon-Fri 9:25 AM + 3:40 PM IST daily price log)
    * any future screener-universe fetcher

Reachability sanity (2026-04-23 verified post-domain-allowlist):
    query1.finance.yahoo.com   -> 200 (with UA)
    www.screener.in            -> 200
    www.nseindia.com           -> reachable but TLS quirks; fallback only
    in.investing.com           -> 403 UA-block (avoid)

Stdlib-only on purpose -- yfinance's curl-cffi backend gets 403'd by the
cowork egress proxy; plain urllib with a real User-Agent passes cleanly.
"""
import json, time, urllib.request, urllib.error
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36")

# ---------------------------------------------------------------------------
# Universe  --  must stay in sync with LAYERS in generate_screener_map_bento.py
# ---------------------------------------------------------------------------
ALL_TICKERS = [
    # 10 TA-analysed
    'HFCL','GMDCLTD','HINDZINC','IREDA','MOIL','PARADEEP','EXIDEIND',
    'HBLENGINE','GRAPHITE','STLTECH',
    # 2 held-but-missing (added 2026-05-02 to close pack-coverage gap)
    'GENESYS','TARIL',
    # L1 Energy
    'WAAREEENER','MGL','NTPC','POWERGRID','ADANIGREEN','TATAPOWER','VEDL',
    'XAGUSD','SILVERBEES','SILVER1','AVANTIFEED',
    # L2 Chips/Semi/EMS
    'KAYNES','DIXON','SYRMA','TATAELXSI','AMBER',
    # L3 Infra
    'BEL','GRSE','MAZDOCK','SOLARINDS','MTARTECH','HAL','TEJASNET',
    'BHARTIARTL','POLYCAB','KEI','ANANTRAJ','BLUESTAR',
    # L4 IT
    'TCS','INFY','HCLTECH','PERSISTENT',
    # L5 Apps
    'KPIT','NAUKRI','TANLA','MAPMYINDIA','NEWGEN',
    # SUB
    'PAYTM','PBFINTECH','CDSL','ANGELONE','DRREDDY','SUNPHARMA','BIOCON',
    # ETF
    'NIFTY','NIFTYBEES','BANKBEES','ITBEES','PHARMABEES','PSUBNKBEES',
    # NBFC + Exchanges (added 2026-04-27 to enable Rajiv screener #1)
    'BAJFINANCE','CHOLAFIN','SHRIRAMFIN','MCX','BSE',
    # PROMOTE-from-RESERVE (added 2026-05-03 per 260503_WATCHLIST-REVIEW F-WLR-04;
    # IDEAFORGE = drone OEM defence sibling to held HBLENGINE; POWERINDIA = Hitachi Energy India HVDC/T&D pure-play)
    'IDEAFORGE','POWERINDIA',
    # Held positions missing from earlier universe (added 2026-05-11 per F101 P3 wrap-up;
    # AREM = Amara Raja Energy & Mobility (held), CHAMBLFERT = Chambal Fertilizers (held))
    'AREM','CHAMBLFERT',
]

# Special ticker -> Yahoo symbol mappings (default rule = ticker + '.NS')
SPECIAL = {
    'XAGUSD':    ['SI=F'],                    # silver futures proxy for spot
    'NIFTY':     ['^NSEI'],
    'BLUESTAR':  ['BLUESTARCO.NS'],
    'KPIT':      ['KPITTECH.NS'],
    'PBFINTECH': ['POLICYBZR.NS'],
    'WAAREEENER':['WAAREEENER.NS','WAAREE.NS'],
    'SILVER1':   ['SILVER1.NS','SILVERIETF.NS'],
    'TRANSRAIL': ['TRANSRAILL.NS'],           # Reserve-pulse fix 2026-05-01 (extra-L spelling)
    'AREM':      ['ARE%26M.NS','AMARAJABAT.NS'],  # ARE&M (Amara Raja Energy & Mobility); legacy 'AMARAJABAT' as fallback
}

# F33 fix (260427): intermarket scalars used in QT synthesis (USDINR, DXY, TNX).
# These get the same daily-cache treatment as the equity universe so the SILV-TA
# Saturday cycle has fresh values without manual chart-reads or one-shot pulls.
# Kept SEPARATE from ALL_TICKERS to preserve the screener-universe semantics --
# fetchers that want intermarket too should iterate ALL_TICKERS + INTERMARKET_TICKERS.
INTERMARKET_TICKERS = [
    'USDINR',  # USD/INR spot
    'DXY',     # US Dollar Index
    'TNX',     # US 10Y Treasury yield
]

# Yahoo symbols for the intermarket scalars (used by yahoo_symbols() + fetchers)
SPECIAL.update({
    'USDINR': ['INR=X'],
    'DXY':    ['DX-Y.NYB'],
    'TNX':    ['^TNX'],
})

# Paths
MODULE_DIR = Path(__file__).parent
CACHE_DIR = MODULE_DIR / '_cache'


def ensure_cache_dir():
    """Make sure the shared CACHE_DIR exists and return its path.

    Restored 2026-05-03: removed at some point during refactor; both
    fetch_weekly_chg.py and fetch_daily_ohlc.py import it. Without it the
    Sunday SCRN-MAP refresh hook silently flips PERF_DATA_IS_SAMPLE=True
    even on successful in-source dict updates.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR


def yahoo_symbols(ticker):
    """Return ordered list of Yahoo symbols to try for an internal ticker code."""
    return SPECIAL.get(ticker, [f"{ticker}.NS"])


def fetch_chart(sym, interval='1d', range_='14d', timeout=12):
    """Raw Yahoo v8 chart pull. Returns parsed JSON dict.

    Default interval/range matches the weekly fetcher's historical behaviour
    so drop-in usage stays identical. Daily-OHLC users should pass range_='5d'
    to minimise payload.
    """
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
           f"?interval={interval}&range={range_}")
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def fetch_with_fallback(ticker, interval='1d', range_='14d', timeout=12,
                        inter_attempt_sleep=0.15):
    """Try each yahoo_symbols(ticker) in order. Return (payload, symbol_used, status).

    On success status == 'ok'. On failure payload is None and status carries a
    short reason ('HTTP_429', 'no_result', 'URLError', etc.).
    """
    last_status = 'unknown'
    for sym in yahoo_symbols(ticker):
        try:
            payload = fetch_chart(sym, interval=interval, range_=range_,
                                  timeout=timeout)
            res = payload.get('chart', {}).get('result')
            if res and res[0].get('indicators', {}).get('quote'):
                return payload, sym, 'ok'
            last_status = 'no_result'
        except urllib.error.HTTPError as e:
            last_status = f'HTTP_{e.code}'
        except Exception as e:
            last_status = type(e).__name__
        time.sleep(inter_attempt_sleep)
    return None, None, last_status
