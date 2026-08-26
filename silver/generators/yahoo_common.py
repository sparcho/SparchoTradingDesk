#!/usr/bin/env python3
"""yahoo_common.py -- the SILVER desk's Yahoo access layer.

A deliberate copy taken 2026-08-26 when the desks were separated. It carries the
FETCH HELPERS only: silver's universe lives in `silver_rails.py`, so adding a rail
here can never change what the other desk pulls.

The public repo has done it this way all along (silver/generators/yahoo_common.py);
the vault was the odd one out, with one module serving both desks.
"""
import json, time, urllib.request, urllib.error
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36")

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
    return YAHOO_SYMBOL.get(ticker, [f"{ticker}.NS"])


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
