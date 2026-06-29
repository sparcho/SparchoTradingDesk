#!/usr/bin/env python3
"""
fetch_fundamentals.py  --  pull fundamentals from screener.in into a cache

WHY THIS EXISTS
===============
Rajiv's screener #1 (and any future fundamental-aware screener) needs PEG /
ROCE / ROE / market cap / PE per ticker. yfinance / Yahoo doesn't give us
these reliably for Indian names. screener.in does, and it's in the egress
allowlist (proven by yahoo_common.py L-01 lessons).

This is the ONE-SHOT fetcher: scrape screener.in's company page top-ratios
block per ticker, write to /00_SYSTEM/GENERATORS/_cache/fundamentals.csv.

CADENCE
=======
NOT a daily fetcher. Fundamentals change quarterly (when results drop) and
slowly. Recommended cadence:
  * Quarterly: full --refresh on the universe (run within a week of each
    quarter-end batch of results)
  * On-demand: --ticker <X> for any newly-added or specifically-watched name

USAGE
=====
   python3 fetch_fundamentals.py
       Fetches the full universe (ALL_TICKERS from yahoo_common.py).
       Skips tickers that already have fresh cache entries (<= 7 days).

   python3 fetch_fundamentals.py --refresh
       Force re-fetch all even if cache is fresh.

   python3 fetch_fundamentals.py --ticker BAJFINANCE
       Fetch one specific ticker.

   python3 fetch_fundamentals.py --tickers BAJFINANCE CHOLAFIN MCX
       Fetch a subset.

   python3 fetch_fundamentals.py --skip-commodities
       Skip XAGUSD/SILVERBEES/SILVER1 + ETFs (NIFTY/NIFTYBEES/etc.) which
       don't have meaningful equity fundamentals.

OUTPUT
======
/00_SYSTEM/GENERATORS/_cache/fundamentals.csv with columns:
    ticker, market_cap_cr, current_price, pe, roce, roe, book_value,
    div_yield, face_value, fetched_at, status, source_url

`status` is one of: ok | 404 | parse-empty | timeout | http-error | http-403 |
                    skipped-commodity | skipped-etf

PARSING NOTE
============
Only the top-ratios block is parsed (9 metrics). PEG ratio is NOT present in
that block on screener.in. Computing PEG requires scraping the
Quarterly-Results section (TTM EPS) and dividing by PE. Deferred until first
consumer (Rajiv #1) actually exercises the gap.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# Resolve module paths
SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / "_cache"
OUT_PATH = CACHE_DIR / "fundamentals.csv"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36")

REQUEST_TIMEOUT = 15
INTER_REQUEST_SLEEP = 1.5  # rate-limit etiquette per SCRN-MAP_LESSONS L-07
FRESH_THRESHOLD_DAYS = 7  # default cache TTL

# Tickers without meaningful equity fundamentals (commodities + ETFs/indices).
SKIP_COMMODITY = {'XAGUSD', 'SILVERBEES', 'SILVER1'}
SKIP_ETF_INDEX = {'NIFTY', 'NIFTYBEES', 'BANKBEES', 'ITBEES', 'PHARMABEES', 'PSUBNKBEES'}
# Intermarket scalars get auto-skipped too (no equity page on screener.in)
SKIP_INTERMARKET = {'USDINR', 'DXY', 'TNX'}

# Screener.in URL slug overrides (when our internal ticker != NSE symbol on screener.in)
SLUG_OVERRIDE = {
    'NAUKRI':     'NAUKRI',         # same
    'MAPMYINDIA': 'MAPMYINDIA',     # same
    'PBFINTECH':  'POLICYBZR',      # screener.in uses POLICYBZR
    'BLUESTAR':   'BLUESTARCO',     # screener.in uses BLUESTARCO
    'KPIT':       'KPITTECH',       # screener.in uses KPITTECH
    'AMBER':      'AMBER',          # AMBER ENTERPRISES INDIA
    'AREM':       'ARE%26M',        # screener.in keeps URL-encoded ampersand for Amara Raja Energy & Mobility
    'CHAMBLFERT': 'CHAMBLFERT',     # Chambal Fertilizers — same slug as NSE symbol
    # Add more as they surface
}

# CSV schema
COLUMNS = [
    'ticker', 'market_cap_cr', 'current_price', 'pe', 'roce', 'roe',
    'book_value', 'div_yield', 'face_value',
    # Growth tables (parsed from screener.in ranges-table blocks)
    'sales_g_3y', 'sales_g_5y', 'sales_g_ttm',
    'profit_g_3y', 'profit_g_5y', 'profit_g_ttm',
    'price_cagr_3y', 'price_cagr_5y',
    'roe_hist_3y', 'roe_hist_5y', 'roe_hist_lastyr',
    # Computed PEG (PE / profit-growth %); 3y is more stable, ttm more current
    'peg_3y', 'peg_ttm',
    # Sanjeev's 8 (added 2026-04-28; #4 ROIC deferred -- not exposed on screener.in free pages)
    'promoter_pct',                                      # Sanjeev #1: Promoter Holding %
    'sales_ttm', 'op_profit_ttm', 'interest_ttm',        # P&L annual TTM (Sales for non-NBFC, Revenue for NBFC -- captured separately)
    'revenue_ttm', 'financing_profit_ttm',               # NBFC equivalents
    'opm_pct', 'financing_margin_pct',                   # Sanjeev #5: OPM % (NBFC = Financing Margin %)
    'borrowings', 'reserves', 'equity_capital',          # Balance-sheet inputs for D/E
    'de_ratio',                                          # Sanjeev #3: computed Borrowings / (Equity + Reserves)
    'icr',                                               # Sanjeev #7: computed OpProfit / Interest
    'mcap_to_sales',                                     # Sanjeev #8: computed market_cap / sales_or_revenue
    'fetched_at', 'status', 'source_url',
]


# =============================================================================
# Fetch + parse
# =============================================================================
def _slug(ticker: str) -> str:
    return SLUG_OVERRIDE.get(ticker, ticker)


def fetch_company_html(ticker: str) -> tuple[str | None, str, str | None]:
    """Fetch screener.in company page. Returns (html, status, error_str)."""
    url = f"https://www.screener.in/company/{_slug(ticker)}/"
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
            html = r.read().decode('utf-8', errors='replace')
        return html, 'ok', None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, '404', f"404 Not Found"
        if e.code == 403:
            return None, 'http-403', f"403 Forbidden"
        return None, f'http-{e.code}', str(e)
    except urllib.error.URLError as e:
        return None, 'timeout' if 'timed out' in str(e) else 'url-error', str(e)
    except Exception as e:
        return None, 'fetch-error', f"{type(e).__name__}: {e}"


_METRIC_PATTERN = re.compile(
    r'<li[^>]*>\s*<span class="name">\s*([^<]+?)\s*</span>'
    r'.*?<span class="(?:nowrap )?value">.*?<span class="number">([^<]+)</span>',
    re.DOTALL,
)

# Map screener.in metric names -> our CSV column names
METRIC_NAME_MAP = {
    'Market Cap':     'market_cap_cr',
    'Current Price':  'current_price',
    'Stock P/E':      'pe',
    'P/E':            'pe',  # in case screener.in renames
    'ROCE':           'roce',
    'ROE':            'roe',
    'Book Value':     'book_value',
    'Dividend Yield': 'div_yield',
    'Face Value':     'face_value',
}


def parse_top_ratios(html: str) -> dict:
    """Extract the top-ratios block. Returns dict keyed by our column names."""
    out = {}
    for m in _METRIC_PATTERN.finditer(html):
        name = m.group(1).strip().replace('\n', ' ')
        while '  ' in name:
            name = name.replace('  ', ' ')
        col = METRIC_NAME_MAP.get(name)
        if not col:
            continue
        val_raw = m.group(2).strip().replace(',', '')
        try:
            out[col] = float(val_raw)
        except ValueError:
            out[col] = None
    return out


# Growth-table extraction (Compounded Sales / Profit Growth, Stock Price CAGR, ROE history)
TABLE_PATTERN = re.compile(
    r'<table class="ranges-table">\s*<tr>\s*<th colspan="2">([^<]+)</th>\s*</tr>(.*?)</table>',
    re.DOTALL
)
ROW_PATTERN = re.compile(
    r'<tr>\s*<td>\s*([^<]+?)\s*</td>\s*<td>\s*([^<]+?)\s*</td>\s*</tr>',
    re.DOTALL
)

# Map screener.in table titles -> our short keys
TABLE_KEY_MAP = {
    'Compounded Sales Growth':  'sales_g',
    'Compounded Profit Growth': 'profit_g',
    'Stock Price CAGR':         'price_cagr',
    'Return on Equity':         'roe_hist',
}
# Map period labels -> our short keys
PERIOD_KEY_MAP = {
    '10 Years:':  '10y',
    '5 Years:':   '5y',
    '3 Years:':   '3y',
    'TTM:':       'ttm',
    '1 Year:':    '1y',
    'Last Year:': 'lastyr',
}


def parse_growth_tables(html: str) -> dict:
    """Extract growth/CAGR tables -> dict[short_table_key][period_key] = float (%)."""
    out = {}
    for m in TABLE_PATTERN.finditer(html):
        title = m.group(1).strip()
        key = TABLE_KEY_MAP.get(title)
        if not key:
            continue
        body = m.group(2)
        out[key] = {}
        for r in ROW_PATTERN.finditer(body):
            period_label = r.group(1).strip()
            value_raw = r.group(2).strip().rstrip('%').replace(',', '')
            period_key = PERIOD_KEY_MAP.get(period_label)
            if not period_key:
                continue
            try:
                out[key][period_key] = float(value_raw)
            except ValueError:
                pass
    return out


# =============================================================================
# Section-aware row extraction (P&L, Balance Sheet, Shareholding) -- Sanjeev fields
# =============================================================================
def _section(html: str, section_id: str) -> str:
    """Return the HTML chunk for one screener.in section by id."""
    i = html.find(f'id="{section_id}"')
    if i == -1:
        return ''
    end = html.find('</section>', i)
    return html[i:end if end != -1 else i + 30000]


def _row_last_value(section_html: str, label: str) -> float | None:
    """Find <td class='text'>...LABEL...</td>; return LAST numeric <td> in that row."""
    if not section_html:
        return None
    pat = re.compile(
        r'<td class="text">\s*(?:.*?\b)?' + re.escape(label) + r'\b(?:.*?)</td>(.*?)</tr>',
        re.DOTALL,
    )
    m = pat.search(section_html)
    if not m:
        return None
    cells = re.findall(r'<td[^>]*>\s*([\d,.\-]+)\s*</td>', m.group(1))
    if not cells:
        return None
    raw = cells[-1].replace(',', '')
    try:
        return float(raw)
    except ValueError:
        return None


def _row_pct_qrt_latest(section_html: str, label: str) -> float | None:
    """For percentage rows in QUARTERLY section: highlight-cell holds latest."""
    if not section_html:
        return None
    pat = re.compile(
        r'<td class="text">\s*' + re.escape(label) + r'\s*</td>\s*<td class="highlight-cell">\s*([\d,.\-]+)\s*%?\s*</td>',
        re.DOTALL,
    )
    m = pat.search(section_html)
    if not m:
        return None
    try:
        return float(m.group(1).replace(',', ''))
    except ValueError:
        return None


def _promoter_pct(html: str) -> float | None:
    """Latest Promoter % from quarterly Shareholding Pattern table."""
    sec = _section(html, 'shareholding')
    if not sec:
        return None
    m = re.search(r'>\s*Promoters[\s\S]*?</button>\s*</td>(.*?)</tr>', sec, re.DOTALL)
    if not m:
        return None
    cells = re.findall(r'<td>\s*([\d.,]+)\s*%\s*</td>', m.group(1))
    if not cells:
        return None
    try:
        return float(cells[-1].replace(',', ''))
    except ValueError:
        return None


def parse_sanjeev_fields(html: str) -> dict:
    """Extract Sanjeev's 6 missing fields."""
    pl = _section(html, 'profit-loss')
    bs = _section(html, 'balance-sheet')
    qr = _section(html, 'quarters')
    return {
        'promoter_pct':           _promoter_pct(html),
        'sales_ttm':              _row_last_value(pl, 'Sales'),
        'revenue_ttm':            _row_last_value(pl, 'Revenue'),
        'op_profit_ttm':          _row_last_value(pl, 'Operating Profit'),
        'financing_profit_ttm':   _row_last_value(pl, 'Financing Profit'),
        'interest_ttm':           _row_last_value(pl, 'Interest'),
        'opm_pct':                _row_pct_qrt_latest(qr, 'OPM %'),
        'financing_margin_pct':   _row_pct_qrt_latest(qr, 'Financing Margin %'),
        'borrowings':             _row_last_value(bs, 'Borrowings'),
        'reserves':               _row_last_value(bs, 'Reserves'),
        'equity_capital':         _row_last_value(bs, 'Equity Capital'),
    }


def _to_float(v):
    """Safely coerce CSV string / None / float into float-or-None."""
    if v is None or v == '':
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def compute_sanjeev_derived(row: dict) -> dict:
    """Compute D/E + ICR + MCap/Sales from already-extracted fields. Defensive against CSV-string inputs."""
    out = {}
    borrow = _to_float(row.get('borrowings'))
    reserves = _to_float(row.get('reserves'))
    eq = _to_float(row.get('equity_capital'))
    if borrow is not None and reserves is not None and eq is not None:
        equity = (reserves or 0) + (eq or 0)
        if equity > 0:
            out['de_ratio'] = round(borrow / equity, 3)
    op = _to_float(row.get('op_profit_ttm')) or _to_float(row.get('financing_profit_ttm'))
    interest = _to_float(row.get('interest_ttm'))
    if op and interest and interest > 0:
        out['icr'] = round(op / interest, 2)
    mc = _to_float(row.get('market_cap_cr'))
    sales = _to_float(row.get('sales_ttm')) or _to_float(row.get('revenue_ttm'))
    if mc and sales and sales > 0:
        out['mcap_to_sales'] = round(mc / sales, 2)
    return out


def flatten_growth_to_columns(growth: dict) -> dict:
    """Flatten nested growth dict -> flat dict matching CSV column names."""
    flat = {}
    flat['sales_g_3y']      = growth.get('sales_g',  {}).get('3y')
    flat['sales_g_5y']      = growth.get('sales_g',  {}).get('5y')
    flat['sales_g_ttm']     = growth.get('sales_g',  {}).get('ttm')
    flat['profit_g_3y']     = growth.get('profit_g', {}).get('3y')
    flat['profit_g_5y']     = growth.get('profit_g', {}).get('5y')
    flat['profit_g_ttm']    = growth.get('profit_g', {}).get('ttm')
    flat['price_cagr_3y']   = growth.get('price_cagr', {}).get('3y')
    flat['price_cagr_5y']   = growth.get('price_cagr', {}).get('5y')
    flat['roe_hist_3y']     = growth.get('roe_hist',   {}).get('3y')
    flat['roe_hist_5y']     = growth.get('roe_hist',   {}).get('5y')
    flat['roe_hist_lastyr'] = growth.get('roe_hist',   {}).get('lastyr')
    return flat


def compute_peg(pe: float | None, profit_growth_pct: float | None) -> float | None:
    """PEG = PE / profit-growth (%). Returns None if either missing or growth <= 0.

    Negative growth makes PEG meaningless; very small positive growth makes PEG
    explode -- caller can interpret. We just return the raw division for any
    growth > 0; consumer screener decides on bounds.
    """
    if pe is None or profit_growth_pct is None:
        return None
    try:
        if profit_growth_pct <= 0:
            return None
        return round(pe / profit_growth_pct, 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


# =============================================================================
# Cache I/O
# =============================================================================
def load_cache() -> dict:
    """Read fundamentals.csv into dict[ticker] -> row."""
    if not OUT_PATH.exists():
        return {}
    out = {}
    with OUT_PATH.open('r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            out[row['ticker']] = row
    return out


def write_cache(rows: dict) -> None:
    """Write dict[ticker] -> row to fundamentals.csv (sorted by ticker)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for ticker in sorted(rows.keys()):
            row = rows[ticker]
            # Ensure all columns present
            full = {col: row.get(col, '') for col in COLUMNS}
            w.writerow(full)


def is_fresh(row: dict, threshold_days: int = FRESH_THRESHOLD_DAYS) -> bool:
    """Check if cached row is within threshold."""
    fetched_at = row.get('fetched_at')
    if not fetched_at:
        return False
    try:
        ts = datetime.fromisoformat(fetched_at.split('+')[0])
    except (ValueError, AttributeError):
        return False
    age = datetime.now() - ts
    return age.days < threshold_days


# =============================================================================
# Per-ticker processing
# =============================================================================
def process_ticker(ticker: str) -> dict:
    """Fetch + parse one ticker. Returns the CSV row (always populated)."""
    now = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    base_row = {col: '' for col in COLUMNS}
    base_row['ticker'] = ticker
    base_row['fetched_at'] = now
    base_row['source_url'] = f"https://www.screener.in/company/{_slug(ticker)}/"

    if ticker in SKIP_COMMODITY:
        base_row['status'] = 'skipped-commodity'
        return base_row
    if ticker in SKIP_ETF_INDEX:
        base_row['status'] = 'skipped-etf'
        return base_row
    if ticker in SKIP_INTERMARKET:
        base_row['status'] = 'skipped-intermarket'
        return base_row

    html, status, err = fetch_company_html(ticker)
    if html is None:
        base_row['status'] = status
        return base_row

    metrics = parse_top_ratios(html)
    if not metrics:
        base_row['status'] = 'parse-empty'
        return base_row

    base_row.update({k: v for k, v in metrics.items() if k in COLUMNS})

    # Add growth tables + computed PEG
    growth = parse_growth_tables(html)
    flat = flatten_growth_to_columns(growth)
    base_row.update({k: v for k, v in flat.items() if v is not None})

    pe_val = base_row.get('pe')
    peg_3y = compute_peg(pe_val, base_row.get('profit_g_3y'))
    peg_ttm = compute_peg(pe_val, base_row.get('profit_g_ttm'))
    if peg_3y is not None:
        base_row['peg_3y'] = peg_3y
    if peg_ttm is not None:
        base_row['peg_ttm'] = peg_ttm

    # Sanjeev fields (added 2026-04-28)
    sanjeev = parse_sanjeev_fields(html)
    base_row.update({k: v for k, v in sanjeev.items() if v is not None and k in COLUMNS})
    derived = compute_sanjeev_derived(base_row)
    base_row.update({k: v for k, v in derived.items() if k in COLUMNS})

    base_row['status'] = 'ok'
    return base_row


# =============================================================================
# Main
# =============================================================================
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument('--refresh', action='store_true',
                    help='Force re-fetch all tickers, even fresh ones')
    ap.add_argument('--ticker', help='Fetch one ticker')
    ap.add_argument('--tickers', nargs='+', help='Fetch a subset of tickers')
    ap.add_argument('--skip-commodities', action='store_true',
                    help='Skip commodities + ETFs + intermarket (default behaviour anyway)')
    ap.add_argument('--threshold-days', type=int, default=FRESH_THRESHOLD_DAYS,
                    help=f'Cache freshness threshold in days (default {FRESH_THRESHOLD_DAYS})')
    args = ap.parse_args()

    # Resolve target list
    if args.ticker:
        targets = [args.ticker.upper()]
    elif args.tickers:
        targets = [t.upper() for t in args.tickers]
    else:
        from yahoo_common import ALL_TICKERS
        targets = list(ALL_TICKERS)

    cache = load_cache()
    print(f"Cache loaded: {len(cache)} existing entries")
    print(f"Targets: {len(targets)}; threshold: {args.threshold_days}d; refresh-all: {args.refresh}")
    print(f"{'TICKER':>14}  STATUS         METRICS")

    fetched_count = skipped_fresh = err_count = 0
    for ticker in targets:
        existing = cache.get(ticker)
        if existing and not args.refresh and is_fresh(existing, args.threshold_days):
            print(f"{ticker:>14}  fresh-cached    skip")
            skipped_fresh += 1
            continue
        row = process_ticker(ticker)
        cache[ticker] = row
        # Inline log
        n_m = sum(1 for col in ['market_cap_cr', 'pe', 'roce', 'roe'] if row.get(col) not in ('', None))
        print(f"{ticker:>14}  {row['status']:14s}  {n_m}/4 key metrics")
        if row['status'] != 'ok':
            err_count += 1
        if row['status'] not in ('skipped-commodity', 'skipped-etf', 'skipped-intermarket'):
            fetched_count += 1
            time.sleep(INTER_REQUEST_SLEEP)

    write_cache(cache)
    print()
    print(f"Summary: fetched {fetched_count}, fresh-skipped {skipped_fresh}, errors {err_count}")
    print(f"Cache written to: {OUT_PATH}")
    return 0 if err_count == 0 else 1


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).parent))
    sys.exit(main())
