#!/usr/bin/env python3
"""silver_refresh_prices.py — FAMILY-DATA-FREE live-price overlay for the silver dashboard.

F123b: the silver cloud cron used to run the FULL silver_dashboard_emit.py every 20 min, which
needs the family holdings input (silver_holdings.yaml) in the repo -> that input + the generator
were public. This overlay replaces the cloud full-emit: it refreshes ONLY the live market-price
fields on the already-committed aggregate and touches NOTHING sensitive. The HOST keeps owning the
full emit (strategy/holdings/ladders) with private inputs + the operator password.

Updates (public market data only): current_market.*, current_price.primary_*, live_xagusd_used_for_ladders,
meta.last_price_overlay_utc, emitted_at_utc. Leaves sensitive_enc, strategy/narrative/sr_levels/forecast
and EVERYTHING else exactly as the host last emitted. MCX is carried forward (host refreshes it daily).

Runs in GitHub Actions with vanilla deps (uses the repo's yahoo_common). No vault access, no secrets.
"""
from __future__ import annotations
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_JSON = HERE.parent / "data" / "silver_dashboard_aggregate.json"
sys.path.insert(0, str(HERE))

# staleness_contract: single source of truth for the staleness contract (F260702).
try:
    import staleness_contract
except ImportError:
    import sys as _sys
    from pathlib import Path as _P
    _sys.path.insert(0, str(_P(__file__).resolve().parent))
    try:
        import staleness_contract
    except ImportError:
        staleness_contract = None


def _now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _quote(ticker, symbol=None):
    """Live quote via the repo's yahoo_common. Returns a dict (status='ok' on success)."""
    try:
        from yahoo_common import fetch_with_fallback  # noqa: PLC0415
        payload, sym, status = fetch_with_fallback(ticker, interval="1m", range_="1d", timeout=10)
        if status != "ok":
            return {"status": status, "ticker": ticker}
        meta = payload["chart"]["result"][0]["meta"]
        rmp = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        rmt = meta.get("regularMarketTime")
        return {
            "status": "ok", "ticker": ticker, "yahoo_symbol": sym,
            "currency": meta.get("currency"), "market_state": meta.get("marketState"),
            "price": rmp, "prev_close": prev,
            "day_chg_pct": ((rmp - prev) / prev * 100) if (rmp and prev) else None,
            "as_of_utc": datetime.fromtimestamp(rmt, tz=timezone.utc).isoformat() if rmt else None,
        }
    except Exception as e:  # noqa: BLE001
        return {"status": f"error: {e}", "ticker": ticker}


def _quote_sym(symbol, ticker):
    """Live quote for a raw Yahoo symbol (e.g. GC=F) via yahoo_common.fetch_chart."""
    try:
        from yahoo_common import fetch_chart  # noqa: PLC0415
        pl = fetch_chart(symbol, interval="1m", range_="1d", timeout=10)
        m = pl["chart"]["result"][0]["meta"]
        rmp = m.get("regularMarketPrice")
        prev = m.get("chartPreviousClose") or m.get("previousClose")
        rmt = m.get("regularMarketTime")
        return {
            "status": "ok", "ticker": ticker, "yahoo_symbol": symbol,
            "currency": m.get("currency"), "price": rmp, "prev_close": prev,
            "day_chg_pct": ((rmp - prev) / prev * 100) if (rmp and prev) else None,
            "as_of_utc": datetime.fromtimestamp(rmt, tz=timezone.utc).isoformat() if rmt else None,
        }
    except Exception as e:  # noqa: BLE001
        return {"status": f"error: {e}", "ticker": ticker}


def _atomic_write_json(path: Path, obj) -> None:
    text = json.dumps(obj, indent=2, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try: os.unlink(tmp)
        except OSError: pass
        raise


def main():
    if not DATA_JSON.exists():
        print(f"[silver-overlay] aggregate missing: {DATA_JSON}", file=sys.stderr)
        return 1
    data = json.loads(DATA_JSON.read_text(encoding="utf-8"))

    sb = _quote("SILVERBEES")
    xag = _quote("XAGUSD")
    usdinr = _quote("USDINR")
    dxy = _quote("DXY")
    gold = _quote_sym("GC=F", "GOLD")
    wti = _quote_sym("CL=F", "WTI")
    tnx = _quote_sym("^TNX", "TNX")

    cm = data.get("current_market") or {}
    prior_mcx = cm.get("mcx_silver")  # carried forward (host refreshes it daily; no groww fetch here)
    newcm = {"silverbees": sb, "xagusd": xag, "usdinr": usdinr, "dxy": dxy,
             "gold": gold, "wti": wti, "tnx": tnx}
    if prior_mcx is not None:
        newcm["mcx_silver"] = prior_mcx
    g = (gold or {}).get("price"); s = (xag or {}).get("price")
    if g and s:
        newcm["gsr"] = {"status": "ok", "ticker": "GSR", "value": g / s,
                        "derivation": "GOLD / XAGUSD (spot/spot)"}
    elif cm.get("gsr") is not None:
        newcm["gsr"] = cm["gsr"]
    newcm["fetched_at_utc"] = _now_utc()
    data["current_market"] = newcm

    # primary price block (SILVERBEES is the INR primary)
    if sb.get("status") == "ok" and sb.get("price") is not None:
        cp = data.get("current_price") or {}
        cp["primary_inr"] = sb["price"]
        cp["primary_source"] = "live:SILVERBEES.NS"
        cp["primary_day_chg_pct"] = sb.get("day_chg_pct")
        cp["primary_date"] = datetime.now(timezone.utc).date().isoformat()
        cp["primary_staleness_days"] = 0
        data["current_price"] = cp

    if xag.get("status") == "ok" and xag.get("price") is not None:
        data["live_xagusd_used_for_ladders"] = xag["price"]

    data.setdefault("meta", {})["last_price_overlay_utc"] = _now_utc()
    data["emitted_at_utc"] = _now_utc()
    # Refresh the canonical staleness contract on the freshly-overlaid silver data (F260702).
    try:
        if staleness_contract:
            data["staleness"] = staleness_contract.build_staleness(data, "silver", now_utc_iso=data["emitted_at_utc"])
    except Exception as _e:
        print(f"[staleness] silver overlay build skipped: {_e}", file=sys.stderr)

    # SAFETY: this overlay must never resurrect plaintext family data. If the prior aggregate was
    # locked, it must stay locked (we never touch sensitive_enc / privacy).
    _atomic_write_json(DATA_JSON, data)
    n_ok = sum(1 for q in (sb, xag, usdinr, dxy, gold, wti, tnx) if q.get("status") == "ok")
    print(f"[silver-overlay] refreshed {n_ok}/7 live quotes · SBees={sb.get('price')} XAG={xag.get('price')} "
          f"· sensitive_enc preserved={'sensitive_enc' in data} · {DATA_JSON.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
