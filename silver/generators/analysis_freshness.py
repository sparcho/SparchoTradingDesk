#!/usr/bin/env python3
"""
analysis_freshness.py — F145: compute freshness of operator-fed ANALYSIS INPUTS
(SILV-TA / STOCK-TA chart reads) for the staleness contract.

WHY: staleness_contract.py is pure + no-I/O by design (it only reads fields off the
aggregate). The freshness of hand-fed chart reads lives on DISK (dated .md files),
not in the aggregate. This helper does the filesystem scan in the EMIT layer and
stamps `aggregate["analysis"]`, which the contract's analysis_freshness detector
then reads. This closes the F144 blind spot (a 3.5-mo-stale SILV-TA was invisible
because nothing measured input freshness). Markdown deliverables only (§0.1a).

Date source precedence: the V-07 filename prefix (YYMMDD_...) — reliable and cheap —
falling back to a `date:`/`as_of:` frontmatter line. Returns ISO YYYY-MM-DD or None.
"""
from __future__ import annotations
import re
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]      # GENERATORS -> 00_SYSTEM -> TRADER
_DAILY = _ROOT / "01_DAILY"
_STOCKS = _ROOT / "02_STOCKS"
_FN_DATE = re.compile(r"(\d{6})_")               # YYMMDD_ filename prefix
_FM_DATE = re.compile(r"^(?:date|as_of)\s*:\s*[\"']?(\d{4}-\d{2}-\d{2})", re.M)


def _iso_from_yymmdd(s: str) -> str | None:
    try:
        yy, mm, dd = int(s[:2]), int(s[2:4]), int(s[4:6])
        return date(2000 + yy, mm, dd).isoformat()
    except (ValueError, IndexError):
        return None


def _file_date(p: Path) -> str | None:
    m = _FN_DATE.search(p.name)
    if m:
        iso = _iso_from_yymmdd(m.group(1))
        if iso:
            return iso
    try:
        m = _FM_DATE.search(p.read_text(encoding="utf-8", errors="replace")[:600])
        return m.group(1) if m else None
    except OSError:
        return None


def _newest(paths) -> str | None:
    dates = [d for d in (_file_date(p) for p in paths) if d]
    return max(dates) if dates else None


def newest_silv_ta() -> str | None:
    if not _DAILY.exists():
        return None
    return _newest(_DAILY.glob("**/*_XAG_SILV-TA.md"))


def newest_stock_ta_by_ticker() -> dict:
    out: dict = {}
    if not _STOCKS.exists():
        return out
    for tdir in _STOCKS.iterdir():
        if not tdir.is_dir() or tdir.name.startswith("_"):
            continue
        d = _newest(tdir.glob(f"*_{tdir.name}_STOCK-TA.md")) or _newest(tdir.glob("*_STOCK-TA.md"))
        if d:
            out[tdir.name] = d
    return out


def compute(desk: str) -> dict:
    """Freshness metadata for the aggregate['analysis'] block. Never raises."""
    try:
        if desk == "silver":
            return {"last_silv_ta": newest_silv_ta()}
        if desk == "equity":
            return {"stock_ta": newest_stock_ta_by_ticker()}
    except Exception:
        pass
    return {}


if __name__ == "__main__":
    import json, sys
    print(json.dumps(compute(sys.argv[1] if len(sys.argv) > 1 else "silver"), indent=2))
