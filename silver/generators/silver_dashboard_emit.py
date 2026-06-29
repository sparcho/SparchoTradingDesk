#!/usr/bin/env python3
"""
silver_dashboard_emit.py — emit silver_dashboard_aggregate.json (v2 schema)

Reads:
  - _inputs/silver_holdings.yaml          (operator-curated; full silver-desk state)
  - _cache/daily_prices.csv               (latest SILVERBEES NSE close)

Writes:
  - 00_SYSTEM/_state/silver_dashboard_aggregate.json

What this generator does:
  - Computes per-tranche and per-account P&L against the latest NSE close
  - Rolls up family totals
  - Passes through narrative sections (forecast, ladders, S/R, floor framework,
    strategy timeline, COT, global inventory, catalysts, news) verbatim from YAML
  - Adds derived helpers (trim-distance percentages, ladder cash sums)

v2 changes vs v1:
  - Removed V-26 "Excel price discrepancy" warning — operator clarified the
    Excel "Current Price" cell is a manual tracker, not a live price source
  - Added all new narrative sections from the expanded YAML
  - Added trim/add ladder cash-sum derivation
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml  # PyYAML

HERE = Path(__file__).resolve().parent
# Layout-flexible (F260531-F116): ONE emit serves both the vault (00_SYSTEM/GENERATORS/)
# and the public repo (silver/generators/). Vault is the single source of truth; this file
# + _inputs/silver_holdings.yaml are synced to the repo by sync_silver_to_repo.sh.
if (HERE / "_inputs" / "silver_holdings.yaml").exists():
    INPUT_YAML = HERE / "_inputs" / "silver_holdings.yaml"            # vault layout
    PRICE_CSV  = HERE / "_cache" / "daily_prices.csv"
    OUTPUT_JSON = HERE.parent.parent / "00_SYSTEM" / "_state" / "silver_dashboard_aggregate.json"
    ROOT = HERE.parent.parent
else:
    _R = HERE.parent                                                 # repo layout: silver/
    INPUT_YAML = _R / "_inputs" / "silver_holdings.yaml"
    PRICE_CSV  = _R / "_cache" / "daily_prices.csv"
    OUTPUT_JSON = _R / "data" / "silver_dashboard_aggregate.json"
    ROOT = _R

# F260607-F122: local-only operator password (gitignored, never pushed). Same file the
# equity emit uses in the vault layout; absent in the repo/cloud layout by design.
PW_FILE = INPUT_YAML.parent / ".dashboard_pw"

# Top-level aggregate keys that carry family-identifiable data / rupee amounts.
SENSITIVE_TOP = ("accounts", "family_totals", "strategy", "deployment_plan",
                 "trim_ladder", "add_ladder", "strategy_timeline", "f98_redeployment")
_FAMILY_TOKENS = ("Sparsh", "Rajiv", "Shalini", "Shalu", "Yash", "HUF", "2P2", "Kite", "SPARCHO")


def _apply_privacy(out: dict) -> None:
    """F260607-F122 — same lock as the equity dashboard (PBKDF2-SHA256 200k -> AES-256-GCM,
    matching the page's WebCrypto unlock). Three modes:
      pw present (vault)        -> encrypt fresh sensitive block, strip plaintext.
      no pw, prior ct (cloud)   -> carry the prior ciphertext forward, STILL strip plaintext
                                   so the public file never regresses to plaintext.
      no pw, no prior ct        -> plaintext emit (explicitly unlocked system).
    """
    sensitive = {k: out.get(k) for k in SENSITIVE_TOP}
    meta = out.get("meta") or {}
    sensitive["meta_private"] = {k: meta.get(k) for k in ("holdings_status", "capital_pnl_banner")}
    pw = None
    if PW_FILE.exists():
        try:
            pw = PW_FILE.read_text(encoding="utf-8").strip() or None
        except Exception:
            pw = None
    enc = None
    if pw:
        try:
            import os as _os, base64 as _b64, hashlib as _hl
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            payload = json.dumps(sensitive, ensure_ascii=False, default=str).encode("utf-8")
            salt = _os.urandom(16); iv = _os.urandom(12)
            key = _hl.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 200000, 32)
            ct = AESGCM(key).encrypt(iv, payload, None)
            enc = {"v": 1, "iter": 200000,
                   "salt": _b64.b64encode(salt).decode(),
                   "iv": _b64.b64encode(iv).decode(),
                   "ct": _b64.b64encode(ct).decode()}
            print(f"[privacy] silver LOCKED fresh ({len(ct)}b ct); plaintext stripped")
        except Exception as e:  # pragma: no cover
            print(f"[privacy] encrypt failed ({e}) -> trying carry-forward", file=sys.stderr)
    if enc is None:
        try:
            prior = json.loads(OUTPUT_JSON.read_text(encoding="utf-8"))
            enc = prior.get("sensitive_enc") or None
        except Exception:
            enc = None
        if enc:
            print("[privacy] silver LOCKED carry-forward (no pw in this environment; prior ciphertext kept)")
    if enc is None:
        print("[privacy] no pw + no prior ciphertext -> PLAINTEXT emit")
        return
    out["sensitive_enc"] = enc
    out["privacy"] = {"locked": True, "hidden": list(SENSITIVE_TOP) + ["meta_private"]}
    for k in SENSITIVE_TOP:
        out.pop(k, None)
    meta.pop("holdings_status", None)
    meta.pop("capital_pnl_banner", None)
    out["warnings"] = [w for w in (out.get("warnings") or [])
                       if not any(t in str(w) for t in _FAMILY_TOKENS)]

# F260607-F122: local-only operator password (gitignored, never pushed). Same file the
# equity emit uses in the vault layout; absent in the repo/cloud layout by design.
PW_FILE = INPUT_YAML.parent / ".dashboard_pw"

# Top-level aggregate keys that carry family-identifiable data / rupee amounts.
SENSITIVE_TOP = ("accounts", "family_totals", "strategy", "deployment_plan",
                 "trim_ladder", "add_ladder", "strategy_timeline", "f98_redeployment")
_FAMILY_TOKENS = ("Sparsh", "Rajiv", "Shalini", "Shalu", "Yash", "HUF", "2P2", "Kite", "SPARCHO")


def _apply_privacy(out):
    """F260607-F122 -- same lock as the equity dashboard (PBKDF2-SHA256 200k -> AES-256-GCM,
    matching the page's WebCrypto unlock). Modes: pw present (vault) -> encrypt fresh + strip;
    no pw but prior ciphertext exists (cloud cron) -> carry ciphertext forward + STILL strip
    (public file never regresses to plaintext); neither -> plaintext emit."""
    sensitive = {k: out.get(k) for k in SENSITIVE_TOP}
    meta = out.get("meta") or {}
    sensitive["meta_private"] = {k: meta.get(k) for k in ("holdings_status", "capital_pnl_banner")}
    pw = None
    if PW_FILE.exists():
        try:
            pw = PW_FILE.read_text(encoding="utf-8").strip() or None
        except Exception:
            pw = None
    enc = None
    if pw:
        try:
            import os as _os, base64 as _b64, hashlib as _hl
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            payload = json.dumps(sensitive, ensure_ascii=False, default=str).encode("utf-8")
            salt = _os.urandom(16); iv = _os.urandom(12)
            key = _hl.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 200000, 32)
            ct = AESGCM(key).encrypt(iv, payload, None)
            enc = {"v": 1, "iter": 200000,
                   "salt": _b64.b64encode(salt).decode(),
                   "iv": _b64.b64encode(iv).decode(),
                   "ct": _b64.b64encode(ct).decode()}
            # public, numbers-free stamp: WHEN the locked book (holdings/deployment) was last re-emitted
            # locally. The 20-min cloud refresh re-stamps emitted_at_utc (prices) but NOT this — so a fresh
            # file with an OLD book_local_emit_utc is the tell that deployment/P&L is frozen vs reality.
            out.setdefault("meta", {})["book_local_emit_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            print(f"[privacy] silver LOCKED fresh ({len(ct)}b ct); plaintext stripped")
        except Exception as e:
            print(f"[privacy] encrypt failed ({e}) -> trying carry-forward", file=sys.stderr)
    if enc is None:
        try:
            prior = json.loads(OUTPUT_JSON.read_text(encoding="utf-8"))
            enc = prior.get("sensitive_enc") or None
            prior_stamp = ((prior.get("meta") or {}).get("book_local_emit_utc"))
            if prior_stamp:   # carry the book-emit stamp forward too (cloud refresh must not lose it)
                out.setdefault("meta", {})["book_local_emit_utc"] = prior_stamp
        except Exception:
            enc = None
        if enc:
            print("[privacy] silver LOCKED carry-forward (no pw in this environment; prior ciphertext kept)")
    if enc is None:
        print("[privacy] no pw + no prior ciphertext -> PLAINTEXT emit")
        return
    out["sensitive_enc"] = enc
    out["privacy"] = {"locked": True, "hidden": list(SENSITIVE_TOP) + ["meta_private"]}
    for k in SENSITIVE_TOP:
        out.pop(k, None)
    meta.pop("holdings_status", None)
    meta.pop("capital_pnl_banner", None)
    out["warnings"] = [w for w in (out.get("warnings") or [])
                       if not any(t in str(w) for t in _FAMILY_TOKENS)]


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"missing input YAML: {path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _silverbees_from_daily(csv_path: Path) -> dict[str, Any] | None:
    """Latest SILVERBEES close from daily_prices.csv (full OHLC). None if absent/empty."""
    if not csv_path.exists():
        return None
    last = None
    with csv_path.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("ticker") != "SILVERBEES":
                continue
            close = (row.get("close") or "").strip()
            if not close:
                continue
            try:
                last = {
                    "price": float(close),
                    "date": row["date"],
                    "open": float(row.get("open") or 0) or None,
                    "high": float(row.get("high") or 0) or None,
                    "low": float(row.get("low") or 0) or None,
                    "prev_close": float(row.get("prev_close") or 0) or None,
                    "day_chg_pct": float(row.get("day_chg_pct") or 0) or None,
                    "volume": int(float(row.get("volume") or 0)) or None,
                    "close_pull_at": row.get("close_pull_at") or None,
                    "source": str(csv_path.relative_to(ROOT)),
                }
            except (ValueError, TypeError):
                continue
    return last


def _silverbees_from_hist(hist_path: Path) -> dict[str, Any] | None:
    """Latest SILVERBEES close from historical_closes.csv (close+volume only). None if absent."""
    if not hist_path.exists():
        return None
    last = None
    with hist_path.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("ticker") != "SILVERBEES":
                continue
            close = (row.get("close") or "").strip()
            if not close:
                continue
            try:
                last = {"price": float(close), "date": row["date"],
                        "volume": int(float(row.get("volume") or 0)) or None,
                        "source": "historical_closes.csv"}
            except (ValueError, TypeError):
                continue
    return last


def _latest_silverbees_close(csv_path: Path) -> dict[str, Any]:
    """Freshest-wins across the two daily caches so the headline can't freeze when one stalls.

    daily_prices.csv is advanced by fetch_daily_ohlc (--pull close); historical_closes.csv is
    advanced DAILY by fetch_historical via daily_driver. The 260609 stall happened because
    daily_prices.csv was NOT in the daily chain (it lagged at Fri 06-05) while historical_closes
    was current — so the silver headline froze on the stale cache. Taking the most RECENT
    trading-day close across BOTH removes that single point of failure. (In the repo/cloud layout
    historical_closes.csv is absent -> this gracefully uses daily_prices.csv alone.)"""
    daily = _silverbees_from_daily(csv_path)
    hist = _silverbees_from_hist(csv_path.parent / "historical_closes.csv")
    cands = [c for c in (daily, hist) if c and c.get("date") and c.get("price") is not None]
    if not cands:
        return {"price": None, "date": None,
                "source": "no SILVERBEES rows found" if csv_path.exists() else "missing daily_prices.csv"}
    return max(cands, key=lambda c: c["date"])


def _staleness_days(date_str: str | None) -> int | None:
    if not date_str:
        return None
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        return (datetime.now(timezone.utc).date() - d).days
    except ValueError:
        return None


def _fifo_match(buys: list[dict], sells: list[dict]) -> dict[str, Any]:
    """FIFO-match sells against buys (both chronologically sorted).
    Returns realized_sells (each sell + matched buy lots + realized_pnl) +
    remaining_tranches (current holdings, derived from unconsumed buys)."""
    # Sort both by date
    buy_queue = sorted(
        ({"date": b["date"], "qty_remaining": int(b["qty"]),
          "buy_price": float(b["buy_price"]),
          "invested": float(b.get("trade_value") or int(b["qty"]) * float(b["buy_price"])),
          "brokerage": float(b.get("brokerage") or 0)}
         for b in buys),
        key=lambda x: x["date"]
    )
    sells_sorted = sorted(sells, key=lambda s: s["date"])

    realized_sells = []
    for s in sells_sorted:
        s_qty = int(s["qty"])
        s_price = float(s["sell_price"])
        s_proceeds = float(s.get("trade_value") or s_qty * s_price)
        s_brokerage = float(s.get("brokerage") or 0)
        qty_remaining_to_match = s_qty
        cost_basis = 0.0
        matched_lots = []
        while qty_remaining_to_match > 0 and buy_queue:
            head = buy_queue[0]
            take = min(head["qty_remaining"], qty_remaining_to_match)
            lot_cost = take * head["buy_price"]
            cost_basis += lot_cost
            matched_lots.append({
                "buy_date": head["date"],
                "buy_price": head["buy_price"],
                "qty_matched": take,
                "lot_cost_inr": round(lot_cost, 2),
            })
            head["qty_remaining"] -= take
            qty_remaining_to_match -= take
            if head["qty_remaining"] == 0:
                buy_queue.pop(0)
        # Realized P&L = proceeds - cost_basis (GROSS; brokerage tracked separately)
        realized_pnl = s_proceeds - cost_basis
        realized_pct = (realized_pnl / cost_basis * 100.0) if cost_basis else None
        realized_sells.append({
            "date": s["date"],
            "qty": s_qty,
            "sell_price": s_price,
            "proceeds_inr": round(s_proceeds, 2),
            "cost_basis_inr": round(cost_basis, 2),
            "realized_pnl_inr": round(realized_pnl, 2),
            "realized_pnl_pct": round(realized_pct, 2) if realized_pct is not None else None,
            "brokerage": s_brokerage,
            "matched_lots": matched_lots,
            "underflow": qty_remaining_to_match > 0,  # True if sell qty exceeded buy queue
        })

    # Remaining tranches = unconsumed entries in buy_queue
    remaining_tranches = []
    for b in buy_queue:
        if b["qty_remaining"] > 0:
            inv_remaining = b["qty_remaining"] * b["buy_price"]
            remaining_tranches.append({
                "date": b["date"],
                "qty": b["qty_remaining"],
                "buy_price": b["buy_price"],
                "invested_inr": round(inv_remaining, 2),
            })

    return {
        "realized_sells": realized_sells,
        "remaining_tranches": remaining_tranches,
    }


def _mark_remaining(tranches: list[dict], price: float | None) -> list[dict]:
    """Enrich remaining tranches with current MV + unrealized P&L."""
    out = []
    for t in tranches:
        qty = t["qty"]; buy = t["buy_price"]; invested = t["invested_inr"]
        cv = qty * price if price else None
        pnl = (cv - invested) if cv is not None else None
        pct = (pnl / invested * 100.0) if (pnl is not None and invested) else None
        out.append({
            **t,
            "current_value_inr": round(cv, 2) if cv is not None else None,
            "unrealized_pnl_inr": round(pnl, 2) if pnl is not None else None,
            "unrealized_pnl_pct": round(pct, 2) if pct is not None else None,
        })
    return out


def _aggregate_account(key: str, data: dict, price: float | None) -> dict[str, Any]:
    buys = data.get("buys") or data.get("tranches") or []   # backward-compat: 'tranches' meant 'buys'
    sells = data.get("sells") or []
    fifo = _fifo_match(buys, sells)
    remaining = _mark_remaining(fifo["remaining_tranches"], price)
    realized_sells = fifo["realized_sells"]

    rem_qty = sum(t["qty"] for t in remaining)
    rem_invested = sum(t["invested_inr"] for t in remaining)
    rem_cv = sum((t["current_value_inr"] or 0) for t in remaining) if price is not None else None
    rem_unrealized = (rem_cv - rem_invested) if rem_cv is not None else None
    rem_unrealized_pct = (rem_unrealized / rem_invested * 100.0) if (rem_unrealized is not None and rem_invested) else None
    avg_buy = (rem_invested / rem_qty) if rem_qty else None

    realized_total = sum(s["realized_pnl_inr"] for s in realized_sells)
    realized_proceeds = sum(s["proceeds_inr"] for s in realized_sells)
    realized_cost_basis = sum(s["cost_basis_inr"] for s in realized_sells)
    realized_brokerage = sum(s.get("brokerage", 0) for s in realized_sells)
    realized_qty = sum(s["qty"] for s in realized_sells)
    realized_pct_blended = (realized_total / realized_cost_basis * 100.0) if realized_cost_basis else None

    total_buys_qty = sum(int(b["qty"]) for b in buys)
    total_sells_qty = sum(int(s["qty"]) for s in sells)

    status = "active" if rem_qty > 0 else "exited"

    return {
        "account_key": key,
        "holder": data.get("holder", key),
        "account_type": data.get("account_type"),
        "custodian": data.get("custodian"),
        "accent_color": data.get("accent_color"),
        "emoji": data.get("emoji"),
        "status": status,
        "source_file": data.get("source_file"),
        # Activity totals
        "total_buy_qty": total_buys_qty,
        "total_sell_qty": total_sells_qty,
        "trade_count": len(buys) + len(sells),
        # Holdings (current)
        "holdings_qty": rem_qty,
        "tranche_count": len(remaining),
        "avg_buy_inr": round(avg_buy, 2) if avg_buy is not None else None,
        "invested_inr": round(rem_invested, 2),
        "current_value_inr": round(rem_cv, 2) if rem_cv is not None else None,
        "unrealized_pnl_inr": round(rem_unrealized, 2) if rem_unrealized is not None else None,
        "unrealized_pnl_pct": round(rem_unrealized_pct, 2) if rem_unrealized_pct is not None else None,
        "tranches": remaining,           # remaining tranches only (UI shows these as "holdings")
        # Realized
        "realized_pnl_inr": round(realized_total, 2),
        "realized_pnl_pct": round(realized_pct_blended, 2) if realized_pct_blended is not None else None,
        "realized_proceeds_inr": round(realized_proceeds, 2),
        "realized_cost_basis_inr": round(realized_cost_basis, 2),
        "realized_brokerage_inr": round(realized_brokerage, 2),
        "realized_qty": realized_qty,
        "realized_sells": realized_sells,
        # Combined return
        "combined_pnl_inr": round(realized_total + (rem_unrealized or 0), 2),
    }


def _enrich_trim_ladder(ladder: list[dict], xag_now: float | None) -> list[dict]:
    """Add distance-to-trigger pct to each tier when XAG estimate present."""
    out = []
    for t in (ladder or []):
        item = dict(t)
        if xag_now and "$" in (t.get("trigger") or ""):
            # crude extract: first $NN.NN in trigger string
            import re
            m = re.search(r"\$\s*(\d+(?:\.\d+)?)", t["trigger"])
            if m:
                lvl = float(m.group(1))
                item["distance_pct"] = (lvl - xag_now) / xag_now * 100.0
                item["distance_label"] = f"{((lvl - xag_now) / xag_now * 100.0):+.1f}% from {xag_now}"
        out.append(item)
    return out


def _enrich_add_ladder(ladder: dict, xag_now: float | None) -> dict:
    out = dict(ladder or {})
    out["rungs"] = []
    for r in (ladder.get("rungs") or []):
        item = dict(r)
        if xag_now and "$" in (r.get("trigger") or ""):
            import re
            m = re.search(r"\$\s*(\d+(?:\.\d+)?)", r["trigger"])
            if m:
                lvl = float(m.group(1))
                item["distance_pct"] = (lvl - xag_now) / xag_now * 100.0
                item["distance_label"] = f"{((lvl - xag_now) / xag_now * 100.0):+.1f}% from {xag_now}"
        out["rungs"].append(item)
    out["total_capacity_inr"] = sum(
        float(r.get("deploy_inr") or 0) for r in (ladder.get("rungs") or [])
    )
    return out


def _enrich_sr(sr: dict, xag_now: float | None) -> dict:
    out = dict(sr or {})
    if not xag_now:
        return out
    def _enrich(levels):
        for lvl in levels or []:
            p = float(lvl["price"])
            lvl["distance_pct"] = (p - xag_now) / xag_now * 100.0
            lvl["distance_label"] = f"{lvl['distance_pct']:+.1f}%"
    _enrich(out.get("resistance"))
    _enrich(out.get("support"))
    return out




# ─────────────────────────────────────────────────────────────────────────────
# Live market data — pulls fresh quotes from Yahoo on every emit.
# Falls back gracefully if Yahoo unreachable; uses daily_prices.csv as backup.
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_live_quote(ticker: str) -> dict[str, Any]:
    """Pull live quote via yahoo_common. Returns {} on failure."""
    try:
        sys.path.insert(0, str(HERE))
        from yahoo_common import fetch_with_fallback  # noqa: PLC0415
        payload, sym, status = fetch_with_fallback(ticker, interval="1m", range_="1d", timeout=10)
        if status != "ok":
            return {"status": status}
        meta = payload["chart"]["result"][0]["meta"]
        rmp = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        rmt = meta.get("regularMarketTime")
        return {
            "status": "ok",
            "ticker": ticker,
            "yahoo_symbol": sym,
            "currency": meta.get("currency"),
            "market_state": meta.get("marketState"),
            "price": rmp,
            "prev_close": prev,
            "day_chg_pct": ((rmp - prev) / prev * 100) if (rmp and prev) else None,
            "as_of_utc": datetime.fromtimestamp(rmt, tz=timezone.utc).isoformat() if rmt else None,
        }
    except Exception as e:  # noqa: BLE001
        return {"status": f"error: {e}"}


def _fetch_live_market_snapshot() -> dict[str, Any]:
    """Pull live SILVERBEES + XAGUSD + GOLD + USDINR + DXY in one pass."""
    tickers = ["SILVERBEES", "XAGUSD", "USDINR", "DXY"]
    out = {}
    for t in tickers:
        out[t.lower()] = _fetch_live_quote(t)
    # GOLD spot via GC=F (gold futures proxy) — manually since yahoo_common doesn't list it
    try:
        sys.path.insert(0, str(HERE))
        from yahoo_common import fetch_chart  # noqa: PLC0415
        gold_payload = fetch_chart("GC=F", interval="1m", range_="1d", timeout=10)
        gold_meta = gold_payload["chart"]["result"][0]["meta"]
        out["gold"] = {
            "status": "ok",
            "ticker": "GOLD",
            "yahoo_symbol": "GC=F",
            "price": gold_meta.get("regularMarketPrice"),
            "prev_close": gold_meta.get("chartPreviousClose") or gold_meta.get("previousClose"),
            "as_of_utc": datetime.fromtimestamp(gold_meta["regularMarketTime"], tz=timezone.utc).isoformat() if gold_meta.get("regularMarketTime") else None,
            "currency": gold_meta.get("currency"),
        }
    except Exception as e:  # noqa: BLE001
        out["gold"] = {"status": f"error: {e}"}

    # WTI crude (CL=F) + US 10Y yield (^TNX) — added 260531 per operator (indicator tiles)
    for _key, _sym, _tk in [("wti", "CL=F", "WTI"), ("tnx", "^TNX", "TNX")]:
        try:
            sys.path.insert(0, str(HERE))
            from yahoo_common import fetch_chart  # noqa: PLC0415
            _pl = fetch_chart(_sym, interval="1m", range_="1d", timeout=10)
            _m = _pl["chart"]["result"][0]["meta"]
            _rmp = _m.get("regularMarketPrice"); _prev = _m.get("chartPreviousClose") or _m.get("previousClose")
            out[_key] = {
                "status": "ok", "ticker": _tk, "yahoo_symbol": _sym,
                "price": _rmp, "prev_close": _prev,
                "day_chg_pct": ((_rmp - _prev) / _prev * 100) if (_rmp and _prev) else None,
                "as_of_utc": datetime.fromtimestamp(_m["regularMarketTime"], tz=timezone.utc).isoformat() if _m.get("regularMarketTime") else None,
                "currency": _m.get("currency"),
            }
        except Exception as _e:  # noqa: BLE001
            out[_key] = {"status": f"error: {_e}"}

    # GSR = Gold / Silver (spot-spot derivation)
    g = out.get("gold", {}).get("price")
    s = out.get("xagusd", {}).get("price")
    if g and s:
        out["gsr"] = {
            "status": "ok",
            "ticker": "GSR",
            "value": g / s,
            "derivation": "GOLD / XAGUSD (spot/spot)",
        }
    else:
        out["gsr"] = {"status": "derive_failed"}

    out["fetched_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return out



# -----------------------------------------------------------------------------
# TWO-LAYER DERIVATION (Rajiv 260602): separate PROBABILITY (book-independent)
# from STRATEGY (derived, state-aware recommendation). See 01_SILVER_DOCTRINE 5.
# -----------------------------------------------------------------------------

def _parse_zone(z):
    try:
        lo, hi = str(z).replace("$", "").strip().split("-")
        return float(lo), float(hi)
    except Exception:
        return None, None


def _inr_cr(x):
    return ("₹%.2fCr" % (x / 1e7)) if x is not None else "—"


def _inr_l(x):
    return ("₹%.0fL" % (x / 1e5)) if x is not None else "—"


def derive_probability(cfg, live, xag_now):
    """BOOK-INDEPENDENT layer: where is silver headed, regardless of what we hold."""
    fc = cfg.get("forecast", {}) or {}
    ff = cfg.get("floor_framework", {}) or {}
    regime = cfg.get("regime", {}) or {}
    cot = cfg.get("cot", {}) or {}
    inv = cfg.get("global_inventory", {}) or {}
    gsr = (live or {}).get("gsr", {}) or {}
    inv_items = inv.get("items", []) or []
    inv_head = next((i for i in inv_items if "bull" in str(i.get("bias", "")).lower()),
                    inv_items[0] if inv_items else {})
    return {
        "layer": "PROBABILITY",
        "subtitle": "Where silver is headed — objective, independent of what we hold",
        "as_of": fc.get("last_chart_read_date"),
        "composite_verdict": fc.get("composite_verdict"),
        "composite_source": fc.get("composite_source"),
        "consensus_state": fc.get("consensus_state"),
        "v15_binding": fc.get("v15_binding"),
        "distribution": fc.get("probability_mass", []),
        "upper_half_69plus_pct": fc.get("upper_half_69plus_pct"),
        "floor_tiers": ff.get("tiers", []),
        "velocity_note": ff.get("velocity_note"),
        "world_numbers": {
            "regime_zone": regime.get("zone"),
            "regime_headline": regime.get("headline_short"),
            "macro_gates": regime.get("gates", []),
            "cot": {"mm_net_k": cot.get("mm_net_k"), "ladder_rung": cot.get("ladder_rung"),
                    "g8_status": cot.get("g8_status"), "read": cot.get("read"), "as_of": cot.get("as_of")},
            "gsr": gsr.get("value"),
            "inventory_headline": inv_head,
            "inventory_as_of": inv.get("last_updated"),
        },
        "bull_bear": cfg.get("bull_bear", {}),
    }


def _tranche_live_status(t, xag, regime_red, abort_suspend, abort_all):
    """Return (status, reason, action, distance_pct, remaining_inr) for one tranche."""
    lo, hi = _parse_zone(t.get("zone_xagusd"))
    base = t.get("status")
    size = float(t.get("size_inr") or 0)
    deployed_t = float(t.get("deployed_inr") or 0)
    remaining_t = float(t.get("remaining_inr") if t.get("remaining_inr") is not None else (size - deployed_t))
    zl = t.get("zone_xagusd")
    if xag is None or lo is None:
        return ("UNKNOWN", "live XAG unavailable", "Await live price.", None, remaining_t)
    in_zone = lo <= xag <= hi
    above = xag > hi
    dist_pct = ((hi - xag) / xag * 100.0) if above else (0.0 if in_zone else (lo - xag) / xag * 100.0)
    if abort_all:
        return ("BLOCKED", "XAG < $52 — V-18 abort-all (exit, no adds)",
                "Hold/exit per V-18; thesis invalidated below $52 daily close.", dist_pct, remaining_t)
    if base == "in-progress" and (in_zone or above):
        if regime_red:
            return ("BLOCKED", "price $%.2f in/above the %s accumulate band BUT regime RED — §3.2 regime gate wins" % (xag, zl),
                    "Hold dry powder; no adds while RED. Resume accumulation when regime is not-RED.", dist_pct, remaining_t)
        if abort_suspend:
            return ("BLOCKED", "price $%.2f but <$67.87 F2 abort-suspend (§5.3)" % xag,
                    "Suspend adds; re-evaluate thesis.", dist_pct, remaining_t)
        return ("ACCUMULATING",
                "price $%.2f sits in/above the %s accumulate band; %s of this tranche still to deploy" % (xag, zl, _inr_l(remaining_t)),
                "Keep adding toward target at <=₹50L/day (§8.4). The 260531 re-weight judges a deep dip LESS likely → the live risk is UNDER-deployment, not over-paying.",
                dist_pct, remaining_t)
    if in_zone:
        if regime_red:
            return ("BLOCKED", "price $%.2f inside %s BUT regime RED — §3.2 regime gate wins over bullish signals" % (xag, zl),
                    "No deploy while RED. Re-arms automatically when regime clears.", dist_pct, remaining_t)
        if abort_suspend:
            return ("BLOCKED", "price $%.2f inside %s but <$67.87 F2 abort-suspend active (§5.3)" % (xag, zl),
                    "Suspend deployment; thesis re-eval per V-13.", dist_pct, remaining_t)
        return ("WOULD-FIRE-NOW", "price $%.2f is INSIDE %s; regime not-RED; COT washed-out (V-32 MET)" % (xag, zl),
                "Deploy this %s tranche, tiered over 2-3 sessions (§8.3/§8.4)." % _inr_l(size), dist_pct, remaining_t)
    if above:
        return ("ARMED", "price $%.2f is %.1f%% above the $%.0f zone top — awaiting dip" % (xag, abs(dist_pct), hi),
                "IF XAG dips into %s AND regime not-RED AND COT washed-out → deploy %s." % (zl, _inr_l(size)), dist_pct, remaining_t)
    return ("BELOW-ZONE", "price $%.2f has fallen below this rung's $%.0f floor" % (xag, lo),
            "Zone passed without a fill; deploy %s only on a re-test back up into %s." % (_inr_l(size), zl), dist_pct, remaining_t)


def derive_strategy(cfg, xag_now, book_totals, regime, sbees_now=None):
    """DERIVED layer: what to do NOW = probability x deployment state x regime x live XAG.

    book_totals is scoped per deployment_plan.envelope_scope_account. Operator ruling
    260608: the Rs.5Cr is the TOTAL family silver allocation, INCLUSIVE of the existing
    ~Rs.1.1Cr (45,000u: Sparsh 18k + Rajiv HUF 27k) -- NOT a separate new-money budget.
    So scope = family_totals: deployed ~= Rs.1.1Cr, dry ~= Rs.3.9Cr. (Supersedes the
    260607 F120 Sparsh-only scoping, which under-counted deployed as just Sparsh's 18k.)
    """
    dp = cfg.get("deployment_plan", {}) or {}
    envelope = float(dp.get("envelope_inr") or 0)
    deployed = float((book_totals or {}).get("invested_inr") or 0)
    book_units = (book_totals or {}).get("holdings_qty")
    book_pnl_pct = (book_totals or {}).get("unrealized_pnl_pct")
    deployed_pct = (deployed / envelope * 100.0) if envelope else None
    dry = (envelope - deployed) if envelope else None
    zone = (regime or {}).get("zone", "UNKNOWN")
    regime_red = (zone == "RED")
    hs = dp.get("hard_stops", {}) or {}
    f2 = hs.get("abort_all_trigger_xagusd")
    v18 = hs.get("portfolio_level_xagusd")
    abort_suspend = (xag_now is not None and f2 is not None and xag_now < f2)
    abort_all = (xag_now is not None and v18 is not None and xag_now < v18)

    ladder = []
    for t in dp.get("tranches", []) or []:
        status, reason, action, dist, remaining_t = _tranche_live_status(t, xag_now, regime_red, abort_suspend, abort_all)
        ladder.append({
            "id": t.get("id"), "label": t.get("label"), "zone_xagusd": t.get("zone_xagusd"),
            "size_inr": t.get("size_inr"), "remaining_inr": remaining_t, "prob_pct": t.get("prob_pct"),
            "live_status": status, "status_reason": reason, "conditional_action": action,
            "distance_pct": round(dist, 1) if dist is not None else None,
        })

    acc = next((r for r in ladder if r["live_status"] in ("ACCUMULATING", "WOULD-FIRE-NOW")), None)
    armed = [r for r in ladder if r["live_status"] == "ARMED"]
    armed.sort(key=lambda r: abs(r["distance_pct"]) if r["distance_pct"] is not None else 1e9)
    next_armed = armed[0] if armed else None

    if abort_all:
        verdict = "ABORT (V-18)"
    elif abort_suspend:
        verdict = "SUSPEND (F2)"
    elif regime_red:
        verdict = "HOLD (regime RED)"
    elif acc:
        verdict = "ACCUMULATE"
    else:
        verdict = "AWAIT-DIP"

    parts = []
    lead = "%s regime · %.0f%% of %s deployed (%s in, %s dry powder)" % (
        zone, deployed_pct or 0, _inr_cr(envelope), _inr_l(deployed), _inr_cr(dry))
    if book_pnl_pct is not None:
        lead += " · book %+.1f%%" % book_pnl_pct
    parts.append(lead + ".")
    if abort_all:
        parts.append("XAG below $52 — V-18 abort-all: exit silver, no adds.")
    elif acc and acc["live_status"] == "ACCUMULATING":
        parts.append("Silver $%.2f sits in the ACCUMULATE band (%s). Recommended posture: keep deploying dry powder at/near current levels toward the %s current-zone target (~%s left), <=₹50L/day. The 260531 re-weight judges a deep dip LESS likely, so the live risk is UNDER-deployment, not over-paying." % (
            xag_now, acc["zone_xagusd"], _inr_cr(float(acc["size_inr"])), _inr_l(acc["remaining_inr"])))
    elif acc and acc["live_status"] == "WOULD-FIRE-NOW":
        parts.append("Silver $%.2f is INSIDE the %s trigger zone and regime is not-RED → deploy the %s tranche now (tiered, §8.3)." % (
            xag_now, acc["zone_xagusd"], _inr_cr(float(acc["size_inr"]))))
    elif regime_red:
        parts.append("Silver $%.2f; regime RED blocks all adds (§3.2) — hold the %s dry powder until regime clears." % (xag_now, _inr_cr(dry)))
    else:
        parts.append("Silver $%.2f is above all add zones — no tranche fires; hold dry powder and wait for a dip into the ladder." % xag_now)
    if next_armed:
        parts.append("Next armed rung: %s %s (%s%% prob, %.1f%% below spot) — %s" % (
            next_armed["id"], next_armed["zone_xagusd"], next_armed["prob_pct"],
            abs(next_armed["distance_pct"]), next_armed["conditional_action"]))
    if f2 is not None and v18 is not None:
        parts.append("Abort-suspend new adds if daily close <$%s (F2); hard-stop exit <$%d (V-18)." % (f2, int(v18)))
    headline = " ".join(parts)

    dpp = []
    for r in ladder:
        amt = r["remaining_inr"] if r["live_status"] in ("ACCUMULATING", "WOULD-FIRE-NOW") else r["size_inr"]
        dpp.append({"id": r["id"], "zone": r["zone_xagusd"], "amount_inr": amt,
                    "prob_pct": r["prob_pct"], "status": r["live_status"]})

    # ── HARVEST ladder (upside trim) + EV rationale + decision tree (260602 r2) ──
    units = (book_totals or {}).get("holdings_qty") or 0
    blended = (deployed / units) if units else None
    ratio = (sbees_now / xag_now) if (sbees_now and xag_now) else None  # SBees per $1 XAG
    harvest = []
    hl = (dp.get("harvest_ladder") or {})
    CORE_ILLUS = 40000000  # ₹4Cr core illustration (operator's example)
    for z in (hl.get("zones") or []):
        tx = z.get("trigger_xagusd")
        proj = (tx * ratio) if (tx and ratio) else None
        gain_pct = ((proj / blended - 1) * 100) if (proj and blended) else None
        prof_now = (units * (proj - blended)) if (proj and blended and units) else None
        prof_core = ((CORE_ILLUS / blended) * (proj - blended)) if (proj and blended) else None
        dist = ((tx - xag_now) / xag_now * 100) if (tx and xag_now) else None
        harvest.append({
            "id": z.get("id"), "trigger_xagusd": tx, "label": z.get("label"),
            "trim_pct": z.get("trim_pct"), "proj_sbees_inr": round(proj, 1) if proj else None,
            "gain_pct_from_blended": round(gain_pct, 1) if gain_pct is not None else None,
            "profit_on_current_book_inr": round(prof_now) if prof_now is not None else None,
            "profit_if_4cr_core_inr": round(prof_core) if prof_core is not None else None,
            "distance_pct": round(dist, 1) if dist is not None else None,
            "status": "ARMED-UP" if (dist is not None and dist > 0) else "REACHED",
        })

    # EV rationale (computed from the live ladder)
    below69 = sum(float(t.get("size_inr") or 0) for t in (dp.get("tranches") or [])
                  if (_parse_zone(t.get("zone_xagusd"))[1] or 99) <= 69)
    deep_tail = sum(float(t.get("size_inr") or 0) for t in (dp.get("tranches") or [])
                    if (_parse_zone(t.get("zone_xagusd"))[1] or 99) <= 59)
    upper_half = ((cfg.get("forecast") or {}).get("upper_half_69plus_pct"))
    stance = (dp.get("stance") or {})
    ev_rationale = {
        "question": "Why reserve cash for price levels the re-weight calls unlikely?",
        "answer": (f"You currently hold {_inr_cr(below69)} of the {_inr_cr(envelope)} envelope earmarked BELOW $69 "
                   f"(the deepest {_inr_cr(deep_tail)} for $52-59, only ~14% probable), while just {_inr_l(deployed)} "
                   f"is deployed on the {upper_half}%-likely $69+ up-path and — until now — there was NO trim plan to "
                   f"harvest a rally. Reserving for the deep tail is INSURANCE (it only pays in a cascade the 260531 "
                   f"read judges unlikely), not the expected-value-max play."),
        "recommended_stance": stance.get("recommended"),
        "stance_detail": stance.get("detail"),
        "reserved_below_69_inr": below69, "deep_tail_inr": deep_tail, "upper_half_69plus_pct": upper_half,
    }

    # ── Scenario-keyed plan (UP vs DOWN) for the dashboard toggle (260602 r3) ──
    tr = dp.get("tranches", []) or []
    def _tsize(t): return float(t.get("size_inr") or 0)
    acc_t = next((t for t in tr if t.get("id") == "ACC"), None)
    acc_rem = float(acc_t.get("remaining_inr") or 0) if acc_t else 0.0
    cur_units = units or 0
    cur_inv = deployed or 0

    def _fill(fills):  # fills: [(size_inr, target_xag)] -> (invested, units, blended)
        ti, tu = cur_inv, cur_units
        for size, tx in fills:
            sb = (tx * ratio) if (tx and ratio) else None
            if sb:
                tu += size / sb
                ti += size
        return ti, tu, (ti / tu if tu else None)

    def _harvest_for(bl, pos_units):
        out = []
        for z in (hl.get("zones") or []):
            tx = z.get("trigger_xagusd")
            proj = (tx * ratio) if (tx and ratio) else None
            gain = ((proj / bl - 1) * 100) if (proj and bl) else None
            prof = (pos_units * (proj - bl)) if (proj and bl) else None
            out.append({"id": z.get("id"), "trigger_xagusd": tx, "trim_pct": z.get("trim_pct"),
                        "label": z.get("label"), "gain_pct": round(gain, 1) if gain is not None else None,
                        "profit_inr": round(prof) if prof is not None else None,
                        "distance_pct": round((tx - xag_now) / xag_now * 100, 1) if (tx and xag_now) else None})
        return out

    up_inv, up_units, up_bl = _fill([(acc_rem, xag_now)])
    # dynamic daily-lot guide for the core accumulation zone (buy more on cheaper days)
    ap = dp.get("accumulation_pacing", {}) or {}
    _bands = ap.get("bands", []) or []
    def _find_band(x):
        for b in _bands:
            if b.get("lo") is not None and b.get("hi") is not None and b["lo"] <= x <= b["hi"]:
                return b
        return None
    _tb = _find_band(xag_now) if xag_now else None
    _above = bool(_bands and xag_now and xag_now > _bands[0]["hi"])
    daily_guide = {
        "xag": xag_now,
        "per_day_inr": (_tb["per_day_inr"] if _tb else (0 if _above else (_bands[-1]["per_day_inr"] if _bands else None))),
        "band_label": (_tb["label"] if _tb else ("above zone — pause adds, wait for re-entry" if _above else "below zone — switch to the dip ladder")),
        "in_zone": _tb is not None,
        "pace_cap_inr": ap.get("pace_cap_inr_per_day"),
        "zone": ap.get("zone_xagusd"),
        "note": ap.get("note"),
        "bands": [dict(b, active=(b is _tb)) for b in _bands],
    }
    dn_fills = [(acc_rem, xag_now)] + [(_tsize(t), t.get("target_xagusd")) for t in tr if t.get("id") != "ACC"]
    dn_inv, dn_units, dn_bl = _fill(dn_fills)

    _pace = (ap.get("pace_cap_inr_per_day") or 5000000)
    _f1 = next((t for t in tr if t.get("id") == "F1"), None)
    _f1_hi = _parse_zone(_f1.get("zone_xagusd"))[1] if _f1 else None
    down_dip_guide = {
        "pace_cap_inr": _pace,
        "next_rung": ({"id": _f1.get("id"), "zone_xagusd": _f1.get("zone_xagusd"),
                       "size_inr": _tsize(_f1), "prob_pct": _f1.get("prob_pct"),
                       "distance_pct": (round((_f1_hi - xag_now) / xag_now * 100, 1) if (_f1_hi and xag_now) else None)}
                      if _f1 else None),
        "note": "When a dip zone triggers, deploy that tranche tiered over the listed sessions at <=₹50L/day (§8.4) — never dump it in one go. Lower fills drop your blended cost (see harvest).",
    }
    scenarios = {
        "up": {
            "label": "UP — silver grinds higher (base case)", "prob_pct": upper_half,
            "deploy_inr": round(up_inv), "blended_inr": round(up_bl, 1) if up_bl else None,
            "adds": [{"id": "CORE", "zone_xagusd": (acc_t or {}).get("zone_xagusd", "72-78"),
                      "size_inr": acc_rem, "action": "deploy the core NOW on strength (pyramid up toward ~$82); don't wait for a dip that's < 50% likely"}],
            "harvest": _harvest_for(up_bl, up_units),
            "daily_guide": daily_guide,
            "summary": ("Deploy %s core now -> ~%s deployed at blended ~%s. Harvest into the rally above; "
                        "this is the %s%%-likely path." % (_inr_cr(acc_rem), _inr_cr(up_inv),
                        ("₹%.0f" % up_bl) if up_bl else "—", upper_half)),
        },
        "down": {
            "label": "DOWN — silver dips into the ladder", "prob_pct": (100 - upper_half) if upper_half is not None else None,
            "deploy_inr": round(dn_inv), "blended_inr": round(dn_bl, 1) if dn_bl else None,
            "adds": [{"id": t.get("id"), "zone_xagusd": t.get("zone_xagusd"), "size_inr": _tsize(t),
                      "prob_pct": t.get("prob_pct"),
                      "sessions": (int(-(-_tsize(t) // _pace)) if (_pace and t.get("id") != "V18") else None),
                      "action": ("ABORT — exit, thesis invalid" if t.get("id") == "V18" else "add on the dip")}
                     for t in tr if t.get("id") != "ACC"],
            "dip_guide": down_dip_guide,
            "harvest": _harvest_for(dn_bl, dn_units),
            "summary": ("If dips fill, you accumulate to ~%s at a LOWER blended ~%s (vs ~%s in the up-case) -> "
                        "the SAME harvest levels pay MORE. That's how reserving for dips works out IF they come." % (
                        _inr_cr(dn_inv), ("₹%.0f" % dn_bl) if dn_bl else "—", ("₹%.0f" % up_bl) if up_bl else "—")),
        },
    }

    # Decision tree — both branches from NOW
    up_nodes = [{"zone": "$78-84", "kind": "buy", "action": "keep deploying core on strength (the likely path)"}]
    for h in harvest:
        up_nodes.append({"zone": f"${h['trigger_xagusd']}", "kind": "trim",
                         "action": f"{h['label']} — trim {h['trim_pct']}%",
                         "profit_current": h["profit_on_current_book_inr"],
                         "profit_4cr": h["profit_if_4cr_core_inr"]})
    down_nodes = []
    for r in ladder:
        if r["live_status"] in ("ARMED", "BELOW-ZONE", "WOULD-FIRE-NOW"):
            down_nodes.append({"zone": f"${r['zone_xagusd']}", "kind": ("abort" if r["id"] == "V18" else "buy"),
                               "action": (r["conditional_action"] or ""), "prob_pct": r["prob_pct"],
                               "size_inr": r["size_inr"]})
    decision_tree = {
        "now": {"xag": xag_now, "deployed_pct": round(deployed_pct, 1) if deployed_pct is not None else None,
                "dry_inr": dry},
        "up": {"label": f"grind up to $84-98 ({upper_half}% put price ≥$69; modal $69-78)",
               "prob_pct": upper_half, "nodes": up_nodes},
        "down": {"label": "dip into the accumulation ladder",
                 "prob_pct": (100 - upper_half) if upper_half is not None else None, "nodes": down_nodes},
    }

    core_size = next((float(t.get("size_inr") or 0) for t in (dp.get("tranches") or []) if t.get("id") == "ACC"), 0)
    core_pct = round(core_size / envelope * 100) if envelope else None
    overview_stats = [
        {"label": "Core target", "value": (f"{core_pct}%" if core_pct is not None else "—"),
         "sub": _inr_cr(core_size), "hero": True},
        {"label": "Deployed now", "value": (f"{round(deployed_pct)}%" if deployed_pct is not None else "—"),
         "sub": _inr_l(deployed)},
        {"label": "Dry powder", "value": _inr_cr(dry)},
        {"label": "Live silver", "value": (f"${xag_now:.2f}" if xag_now else "—"), "sub": f"{zone} regime"},
        {"label": "Book P&L", "value": (f"{book_pnl_pct:+.1f}%" if book_pnl_pct is not None else "—")},
        {"label": "Stops", "value": f"${int(v18)} / ${f2}", "sub": "V-18 abort / F2 suspend"},
    ]
    return {
        "layer": "STRATEGY",
        "subtitle": "What to do now — derived from your position x price x regime (updates as they move)",
        "posture_verdict": verdict,
        "posture_headline": headline,
        "deployment_state": {
            "envelope_inr": envelope, "deployed_inr": deployed,
            "deployed_pct": round(deployed_pct, 1) if deployed_pct is not None else None,
            "dry_powder_inr": dry, "book_units": book_units, "book_unrealized_pct": book_pnl_pct,
            "xag_now": xag_now, "regime_zone": zone,
        },
        "ladder": ladder,
        "dry_powder_plan": dpp,
        "next_trigger": next_armed,
        "abort_levels": {"f2_suspend_xagusd": f2, "v18_stop_xagusd": v18,
                         "f2_buffer_pct": round((xag_now - f2) / f2 * 100, 1) if (xag_now and f2) else None,
                         "abort_suspend_active": abort_suspend, "abort_all_active": abort_all},
        "strategic_finding": dp.get("strategic_finding_260531"),
        "blended_cost_inr": round(blended, 2) if blended else None,
        "harvest_ladder": harvest,
        "ev_rationale": ev_rationale,
        "decision_tree": decision_tree,
        "scenarios": scenarios,
        "overview_stats": overview_stats,
        "core_pct": core_pct,
    }



def emit() -> Path:
    cfg = _read_yaml(INPUT_YAML)
    price = _latest_silverbees_close(PRICE_CSV)
    live = _fetch_live_market_snapshot()
    # Apply operator overrides BEFORE deriving anything.
    # AUTO-EXPIRY (260608): an override is a weekend stopgap, not a permanent pin. If its
    # _at date is older than today's UTC date, it is treated as RELEASED automatically so a
    # forgotten weekend pin can never suppress the live refresh on a market day (the F121-class
    # failure the operator hit 260608). The live goldapi/Yahoo feed then flows as normal.
    overrides = dict(cfg.get("overrides") or {})
    def _ov_expired(at):
        try:
            import re as _re
            m = _re.search(r"(\d{4})-(\d{2})-(\d{2})", str(at or ""))
            if not m: return False
            from datetime import date as _date
            d = _date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return d < datetime.now(timezone.utc).date()
        except Exception:
            return False
    if overrides.get("live_xagusd_override") is not None and _ov_expired(overrides.get("live_xagusd_override_at")):
        print(f"[override] live_xagusd_override auto-EXPIRED (set {overrides.get('live_xagusd_override_at')} < today) -> released; live feed flows", file=sys.stderr)
        overrides["live_xagusd_override"] = None
    if overrides.get("live_xagusd_override") is not None:
        live["xagusd"] = {
            "status": "ok",
            "ticker": "XAGUSD",
            "yahoo_symbol": "operator-override",
            "currency": "USD",
            "market_state": "OPERATOR-PINNED",
            "price": float(overrides["live_xagusd_override"]),
            "prev_close": (live.get("xagusd") or {}).get("price"),
            "day_chg_pct": None,
            "as_of_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source_note": overrides.get("live_xagusd_override_at") or "operator override",
        }
    if overrides.get("live_silverbees_override") is not None:
        live["silverbees"] = {
            **(live.get("silverbees") or {}),
            "status": "ok",
            "price": float(overrides["live_silverbees_override"]),
            "market_state": "OPERATOR-PINNED",
            "source_note": "operator override",
            "as_of_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    # Prefer live SILVERBEES price when available; fall back to cache close.
    # When the LIVE price is used, the date + staleness must reflect the LIVE pull, NOT the
    # daily-close cache (260609 bug: a fresh live price was tagged with the stale cache date,
    # so the headline read "4 days stale" though the price was current).
    live_sbees = live.get("silverbees", {})
    if live_sbees.get("status") == "ok" and live_sbees.get("price"):
        primary = live_sbees["price"]
        _live_date = (live_sbees.get("as_of_utc") or "")[:10] or price.get("date")
        primary_date = _live_date
        primary_source = "live:" + str(live_sbees.get("yahoo_symbol") or "SILVERBEES")
        primary_staleness = _staleness_days(_live_date)
        primary_day_chg = live_sbees.get("day_chg_pct") if live_sbees.get("day_chg_pct") is not None else price.get("day_chg_pct")
    else:
        primary = price["price"]
        primary_date = price.get("date")
        primary_source = price.get("source")
        primary_staleness = _staleness_days(price.get("date"))
        primary_day_chg = price.get("day_chg_pct")
    # XAGUSD: prefer live fetch, fall back to YAML estimates
    live_xag = live.get("xagusd", {})
    if live_xag.get("status") == "ok" and live_xag.get("price"):
        xag_now = live_xag["price"]
    else:
        xag_now = (cfg.get("f98_redeployment") or {}).get("current_xagusd_estimate") \
            or (cfg.get("sr_levels") or {}).get("current_xagusd_estimate")

    # Per-account roll-up
    accounts_in = cfg.get("accounts") or {}
    accounts_out = [_aggregate_account(k, v, primary) for k, v in accounts_in.items()]
    accounts_out.sort(key=lambda a: (-(a["holdings_qty"] or 0), a["holder"]))

    family_qty = sum(a["holdings_qty"] for a in accounts_out)
    family_inv = sum(a["invested_inr"] for a in accounts_out)
    family_cv = sum((a["current_value_inr"] or 0) for a in accounts_out) if primary is not None else None
    family_pnl = (family_cv - family_inv) if family_cv is not None else None
    family_pct = (family_pnl / family_inv * 100.0) if (family_pnl is not None and family_inv) else None
    family_realized = sum(a["realized_pnl_inr"] for a in accounts_out)
    family_realized_cost_basis = sum(a["realized_cost_basis_inr"] for a in accounts_out)
    family_realized_proceeds = sum(a["realized_proceeds_inr"] for a in accounts_out)
    family_realized_qty = sum(a["realized_qty"] for a in accounts_out)
    family_realized_pct = (family_realized / family_realized_cost_basis * 100.0) if family_realized_cost_basis else None
    family_combined_pnl = family_realized + (family_pnl or 0)
    family_total_invested_lifetime = family_realized_cost_basis + family_inv   # everything ever bought (cost basis)
    family_combined_pct = (family_combined_pnl / family_total_invested_lifetime * 100.0) if family_total_invested_lifetime else None
    family_accounts_active = sum(1 for a in accounts_out if a["status"] == "active")
    family_accounts_exited = sum(1 for a in accounts_out if a["status"] == "exited")

    out = {
        "schema_version": "v2",
        "doc_type": "silver_dashboard_aggregate",
        "emitted_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "meta": cfg.get("meta", {}),

        # Price layer
        "current_price": {
            "primary_inr": primary,
            "primary_date": primary_date,
            "primary_source": primary_source,
            "primary_staleness_days": primary_staleness,
            "primary_day_chg_pct": primary_day_chg,
            "primary_intraday": {
                "open": price.get("open"),
                "high": price.get("high"),
                "low": price.get("low"),
                "prev_close": price.get("prev_close"),
                "volume": price.get("volume"),
            },
        },

        # Live market snapshot — fresh each emit
        "current_market": live,
        "live_xagusd_used_for_ladders": xag_now,

        # Holdings layer
        "family_totals": {
            "holdings_qty": family_qty,
            "invested_inr": family_inv,
            "current_value_inr": family_cv,
            "unrealized_pnl_inr": family_pnl,
            "unrealized_pnl_pct": family_pct,
            "realized_pnl_inr": family_realized,
            "realized_pnl_pct": family_realized_pct,
            "realized_proceeds_inr": family_realized_proceeds,
            "realized_cost_basis_inr": family_realized_cost_basis,
            "realized_qty": family_realized_qty,
            "combined_pnl_inr": family_combined_pnl,
            "combined_pnl_pct": family_combined_pct,
            "total_invested_lifetime_inr": family_total_invested_lifetime,
            "account_count_active": family_accounts_active,
            "account_count_exited": family_accounts_exited,
            "tranche_count": sum(a["tranche_count"] for a in accounts_out),
        },
        "accounts": accounts_out,

        # Narrative + framework layers (passed through from YAML)
        "snapshot_tickers": cfg.get("snapshot_tickers", []),
        "tradingview_main_chart": cfg.get("tradingview_main_chart", {}),
        "forecast": cfg.get("forecast", {}),
        "bull_bear": cfg.get("bull_bear", {}),
        "news": cfg.get("news", {}),
        "catalysts": cfg.get("catalysts", []),
        "sr_levels": _enrich_sr(cfg.get("sr_levels", {}), xag_now),
        "trim_ladder": _enrich_trim_ladder(cfg.get("trim_ladder", []), xag_now),
        "add_ladder": _enrich_add_ladder(cfg.get("add_ladder", {}), xag_now),
        "floor_framework": cfg.get("floor_framework", {}),
        "strategy_timeline": cfg.get("strategy_timeline", []),
        "global_inventory": cfg.get("global_inventory", {}),
        "f98_redeployment": cfg.get("f98_redeployment", {}),
        "deployment_plan": cfg.get("deployment_plan", {}),
        "cot": cfg.get("cot", {}),

        # Warnings (sells pending is the only one in v2)
        "warnings": [
            w for w in [
                f"{family_accounts_exited} of {len(accounts_out)} accounts EXITED — only {family_accounts_active} active holding (Shalu only post 15-May liquidation)."
                if family_accounts_exited >= 2 else None,
                f"SILVERBEES price is {primary_staleness} day(s) stale (no fresh live quote and daily-close cache lagging)."
                if primary_staleness and primary_staleness > 1
                else None,
            ] if w
        ],
    }

    # DERIVED two-layer blocks (Rajiv 260602) -- built from cfg + live state
    out["probability"] = derive_probability(cfg, live, xag_now)
    # Envelope-scoped book totals for the strategy layer: the Rs.5Cr envelope is the
    # Sparsh F109 book ONLY (CLAUDE 8.1.1); Rajiv's units are a separate re-entry
    # OUTSIDE it. Use the scoped account's totals so deployed_pct / blended avg are
    # measured against the right book (F120 / audit 3 fix, 260607).
    _scope_key = str((cfg.get("deployment_plan", {}) or {}).get("envelope_scope_account", "family")).strip().lower()
    if _scope_key in ("family", "all", "", "none"):
        _scoped = out["family_totals"]  # INCLUSIVE: Rs.5Cr = total family silver allocation (operator 260608)
    else:
        _scoped = next((a for a in accounts_out if a.get("account_key") == _scope_key), None) or out["family_totals"]
    out["strategy"] = derive_strategy(cfg, xag_now, _scoped, cfg.get("regime", {}), primary)

    _apply_privacy(out)  # F260607-F122 -- must be the LAST transform before write

    _apply_privacy(out)  # F260607-F122 — must be the LAST transform before write

    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return OUTPUT_JSON


def main() -> int:
    import argparse, shutil, subprocess
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1] if __doc__ else None)
    ap.add_argument("--publish", action="store_true",
                    help="Copy aggregate JSON to DASHBOARDS/silver/web/data/ for GitHub Pages publish")
    ap.add_argument("--git-push", action="store_true",
                    help="git add/commit/push the web/data/ change (requires git CLI + auth set up; runs in web/)")
    ap.add_argument("--publish-target", default="00_SYSTEM/DASHBOARDS/silver/web",
                    help="Path (relative to TRADER root) of the web/ folder to publish into")
    args = ap.parse_args()

    try:
        path = emit()
    except Exception as e:  # noqa: BLE001
        print(f"[silver_dashboard_emit] FAILED: {e}", file=sys.stderr)
        return 1
    print(f"[silver_dashboard_emit] wrote {path}")

    if args.publish:
        web_root = ROOT / args.publish_target
        data_dir = web_root / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        target = data_dir / path.name
        shutil.copy2(path, target)
        print(f"[silver_dashboard_emit] published → {target}")

        if args.git_push:
            try:
                subprocess.run(["git", "add", "data/"], cwd=str(web_root), check=True)
                # Only commit if there are staged changes
                diff = subprocess.run(
                    ["git", "diff", "--cached", "--name-only"],
                    cwd=str(web_root), check=True, capture_output=True, text=True,
                )
                if not diff.stdout.strip():
                    print("[silver_dashboard_emit] git: no changes to commit")
                else:
                    msg = f"refresh silver dashboard {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
                    subprocess.run(["git", "commit", "-m", msg], cwd=str(web_root), check=True)
                    subprocess.run(["git", "push"], cwd=str(web_root), check=True)
                    print("[silver_dashboard_emit] git: pushed to remote")
            except subprocess.CalledProcessError as e:
                print(f"[silver_dashboard_emit] git step FAILED: {e}", file=sys.stderr)
                return 2
            except FileNotFoundError:
                print("[silver_dashboard_emit] git CLI not found — install git or skip --git-push", file=sys.stderr)
                return 2

    return 0
if __name__ == "__main__":
    sys.exit(main())
