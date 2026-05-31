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


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"missing input YAML: {path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _latest_silverbees_close(csv_path: Path) -> dict[str, Any]:
    if not csv_path.exists():
        return {"price": None, "date": None, "source": "missing daily_prices.csv"}
    last = None
    with csv_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
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
                }
            except (ValueError, TypeError):
                continue
    if last is None:
        return {"price": None, "date": None, "source": "no SILVERBEES rows found"}
    last["source"] = str(csv_path.relative_to(ROOT))
    return last


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


def emit() -> Path:
    cfg = _read_yaml(INPUT_YAML)
    price = _latest_silverbees_close(PRICE_CSV)
    live = _fetch_live_market_snapshot()
    # Apply operator overrides BEFORE deriving anything
    overrides = (cfg.get("overrides") or {})
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

    # Prefer live SILVERBEES price when available; fall back to cache close
    live_sbees = live.get("silverbees", {})
    if live_sbees.get("status") == "ok" and live_sbees.get("price"):
        primary = live_sbees["price"]
    else:
        primary = price["price"]
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
            "primary_date": price.get("date"),
            "primary_source": price.get("source"),
            "primary_staleness_days": _staleness_days(price.get("date")),
            "primary_day_chg_pct": price.get("day_chg_pct"),
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
                f"NSE SILVERBEES close is {_staleness_days(price.get('date'))} day(s) stale (weekend expected)."
                if _staleness_days(price.get("date")) and _staleness_days(price.get("date")) > 1
                else None,
            ] if w
        ],
    }

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
