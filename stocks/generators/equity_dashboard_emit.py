#!/usr/bin/env python3
"""equity_dashboard_emit.py v1.1 - emit equity_dashboard_aggregate.json"""

from __future__ import annotations
import csv, json, re, sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
POSITIONS_JSON = HERE / "_cache" / "positions_unified.json"
SR_CSV = ROOT / "00_SYSTEM" / "AUTO_SR_LEVELS.csv"
STOCKS_DIR = ROOT / "02_STOCKS"
LENS_DIR = ROOT / "03_SCREENERS" / "LAYERS"
WATCHLIST_DIR = ROOT / "03_SCREENERS" / "WATCHLIST"
FLAGS_DIR = ROOT / "00_SYSTEM" / "FLAGS"
DR_MOC = ROOT / "09_DEEP_RESEARCH" / "_MOC.md"
DR_CROSS = ROOT / "09_DEEP_RESEARCH" / "260512_DR-CROSS-BRIEF-STITCHING.md"
OUTPUT_JSON = ROOT / "00_SYSTEM" / "_state" / "equity_dashboard_aggregate.json"

LENSES = ["Rajiv-10G", "Rajiv-DIV", "Sanjeev-8G", "Kaarin-TA"]
WATCHLISTS = ["Rajiv-DIV_BULL", "Rajiv-DIV_BEAR", "warsh_steepener_2026", "india_stack_dpi_2026"]


def _parse_frontmatter(md_path):
    if not md_path.exists():
        return {}
    try:
        body = md_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}
    if not body.startswith("---"):
        return {}
    parts = body.split("---", 2)
    if len(parts) < 3:
        return {}
    fm_text = parts[1]
    if HAVE_YAML:
        try:
            return yaml.safe_load(fm_text) or {}
        except yaml.YAMLError:
            pass
    out = {}
    for line in fm_text.splitlines():
        m = re.match(r"^([a-z_][a-z0-9_]*):\s*(.+?)\s*$", line)
        if not m:
            continue
        k, v = m.group(1), m.group(2)
        if v.startswith('"') and v.endswith('"'):
            v = v[1:-1]
        out[k] = v
    return out


def _read_sr_levels():
    out = {}
    if not SR_CSV.exists():
        return out
    with SR_CSV.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            t = row.get("ticker")
            if not t:
                continue
            try:
                price = float(row.get("price") or 0)
            except ValueError:
                continue
            out.setdefault(t, []).append({"method": row.get("method"), "kind": row.get("kind"), "price": price, "as_of": row.get("as_of")})
    return out


def _read_lens_run(lens):
    meta_path = LENS_DIR / lens / "last_run.json"
    if not meta_path.exists():
        return {"lens": lens, "status": "missing"}
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"lens": lens, "status": f"parse-error: {e}"}
    today_str = datetime.now().strftime("%y%m%d")
    run_date_yymmdd = None
    rd = meta.get("run_date", "")
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", rd)
    if m:
        run_date_yymmdd = m.group(1)[2:] + m.group(2) + m.group(3)
    csv_path = None
    for d in [today_str, run_date_yymmdd]:
        if not d:
            continue
        p = LENS_DIR / lens / f"{d}_{lens}.csv"
        if p.exists():
            csv_path = p
            break
    hits = []
    if csv_path and csv_path.exists():
        with csv_path.open("r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                hits.append({"ticker": row.get("ticker"), "passes_all": row.get("passes_all") == "1",
                             "pass_count": int(row.get("pass_count") or 0), "total_gates": int(row.get("total_gates") or 0)})
    return {"lens": lens, "status": meta.get("status", "ok"), "run_date": meta.get("run_date"),
            "rows_evaluated": meta.get("rows_evaluated", 0), "candidates_passed": meta.get("candidates_passed", 0),
            "near_misses": meta.get("near_misses", 0), "n_gates": meta.get("n_gates"), "hits": hits,
            "csv_used": str(csv_path.name) if csv_path else None}


def _read_watchlist(wl):
    meta_path = WATCHLIST_DIR / wl / "last_run.json"
    if not meta_path.exists():
        return {"watchlist": wl, "status": "missing"}
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"watchlist": wl, "status": f"parse-error: {e}"}
    md_path = None
    try:
        files = sorted([p for p in (WATCHLIST_DIR / wl).iterdir() if p.suffix == ".md"], reverse=True)
        if files:
            md_path = files[0]
    except Exception:
        pass
    return {"watchlist": wl, "status": meta.get("status", "ok"), "run_date": meta.get("run_date"),
            "triggers": meta.get("triggers", meta.get("candidates_passed", 0)),
            "rows_evaluated": meta.get("rows_evaluated", 0), "latest_md": md_path.name if md_path else None}


def _read_open_flags():
    by_pri = {"P0": [], "P1": [], "P2": [], "P3": []}
    if not FLAGS_DIR.exists():
        return by_pri
    for p in FLAGS_DIR.glob("*.md"):
        fm = _parse_frontmatter(p)
        if fm.get("status") not in ("open", "open-urgent"):
            continue
        prio = fm.get("priority", "P3")
        if prio not in by_pri:
            continue
        by_pri[prio].append({"flag_id": fm.get("flag_id") or p.stem, "ticker": fm.get("ticker"),
                             "description": fm.get("description", "").strip().strip('"'),
                             "opened_date": fm.get("opened_date"), "target_date": fm.get("target_date"),
                             "domain": fm.get("domain"), "file": p.name})
    for pri in by_pri:
        by_pri[pri].sort(key=lambda x: str(x.get("opened_date") or ""))
    for pri, flags in by_pri.items():
        for f in flags:
            for k in ("opened_date", "target_date"):
                if hasattr(f.get(k), "isoformat"):
                    f[k] = f[k].isoformat()
    return by_pri


def _latest_rajiv_verdict(idx):
    latest_date = None
    latest_verdict = None
    for k, v in idx.items():
        m = re.match(r"^rajiv_verdict_(\d{6})$", k)
        if m and v:
            if not latest_date or m.group(1) > latest_date:
                latest_date = m.group(1)
                latest_verdict = v
    if not latest_verdict:
        return None
    return {"date": latest_date, "verdict": latest_verdict}


def _summarise_sr(levels, ltp):
    if not levels or not ltp:
        return {}
    try:
        ltp_f = float(ltp)
    except (TypeError, ValueError):
        return {}
    above = sorted([l for l in levels if l["price"] > ltp_f], key=lambda x: x["price"])[:4]
    below = sorted([l for l in levels if l["price"] < ltp_f], key=lambda x: -x["price"])[:4]
    return {"above": above, "below": below}


def _build_hit_matrix(lens_runs, held_tickers):
    by_ticker = {}
    for lens_data in lens_runs:
        lens = lens_data["lens"]
        for hit in lens_data.get("hits", []):
            t = hit["ticker"]
            if t not in by_ticker:
                by_ticker[t] = {"ticker": t, "lens": {}, "multi_hit_count": 0, "held": t in held_tickers}
            status = "pass" if hit["passes_all"] else ("near" if hit["pass_count"] >= hit["total_gates"] - 1 else "fail")
            score = f"{hit['pass_count']}/{hit['total_gates']}"
            ratio = hit["pass_count"] / hit["total_gates"] if hit["total_gates"] else 0
            by_ticker[t]["lens"][lens] = {"status": status, "score": score, "ratio": ratio,
                                          "pass_count": hit["pass_count"], "total_gates": hit["total_gates"]}
            if status in ("pass", "near"):
                by_ticker[t]["multi_hit_count"] += 1
    rows = list(by_ticker.values())
    rows.sort(key=lambda r: (-r["multi_hit_count"], not r["held"], r["ticker"]))
    return rows[:30]


def _build_promotion_candidates(lens_runs, held_tickers):
    matrix = _build_hit_matrix(lens_runs, held_tickers)
    return [r for r in matrix if not r["held"] and r["multi_hit_count"] >= 2][:12]


def _build_lens_timeline(lenses, days=14):
    out = {}
    for lens in lenses:
        d = LENS_DIR / lens
        if not d.exists():
            out[lens] = []
            continue
        csvs = sorted([p for p in d.glob(f"*_{lens}.csv")], reverse=True)[:days]
        rows = []
        for p in csvs:
            try:
                date = p.stem.split("_")[0]
                date_iso = f"20{date[:2]}-{date[2:4]}-{date[4:6]}" if len(date) == 6 else date
                with p.open("r", encoding="utf-8") as f:
                    passed = near = total = 0
                    for r in csv.DictReader(f):
                        total += 1
                        pc = int(r.get("pass_count") or 0)
                        tg = int(r.get("total_gates") or 0)
                        if r.get("passes_all") == "1":
                            passed += 1
                        elif tg > 0 and pc >= tg - 1:
                            near += 1
                    rows.append({"date": date_iso, "passed": passed, "near": near, "evaluated": total})
            except Exception:
                continue
        rows.sort(key=lambda r: r["date"])
        out[lens] = rows
    return out


def _build_held_lens_grid(lens_runs, held_tickers):
    rows = []
    for t in held_tickers:
        row = {"ticker": t, "lens": {}}
        for lens_data in lens_runs:
            lens = lens_data["lens"]
            hit = next((h for h in lens_data.get("hits", []) if h["ticker"] == t), None)
            if hit:
                status = "pass" if hit["passes_all"] else ("near" if hit["pass_count"] >= hit["total_gates"] - 1 else "fail")
                ratio = hit["pass_count"] / hit["total_gates"] if hit["total_gates"] else 0
                row["lens"][lens] = {"status": status, "score": f"{hit['pass_count']}/{hit['total_gates']}", "ratio": ratio}
            else:
                row["lens"][lens] = {"status": "n/a", "score": "—", "ratio": 0}
        rows.append(row)
    return rows


def _build_risk_gate_view(held):
    rows = []
    for h in held:
        g = h.get("risk_gates") or {}
        ltp = h.get("current_price")
        if not isinstance(g, dict) or not ltp:
            continue
        try:
            ltp_f = float(ltp)
        except (TypeError, ValueError):
            continue
        for key, label, direction in [
            ("invalidation_close_below", "🔴 INV", "below"),
            ("trim_50_close_below", "🟠 TRIM-50", "below"),
            ("soft_watch_close_below", "🟡 SOFT", "below"),
            ("trim_30_close_above", "🟢 TRIM-30 ↑", "above"),
            ("trim_additional_close_above", "🟢 TRIM-add ↑", "above"),
            ("trim_60_close_above", "🟢 TRIM-60 ↑", "above"),
        ]:
            v = g.get(key)
            if v is None:
                continue
            try:
                vf = float(v)
            except (TypeError, ValueError):
                continue
            dist_pct = (vf - ltp_f) / ltp_f * 100
            rows.append({"ticker": h["ticker"], "gate": label, "trigger_inr": vf, "ltp_inr": ltp_f,
                         "distance_pct": dist_pct, "direction": direction})
    rows.sort(key=lambda r: abs(r["distance_pct"]))
    return rows


def _parse_dr_cross_brief(path):
    cross = [
        {"ticker": "TARIL", "appears_in": ["Energy v2", "DC v2", "Defence-Railways"], "story": "T&D transformer = triple-convergence bottleneck (renewables / DC / rail)", "held": True},
        {"ticker": "HBLENGINE", "appears_in": ["Defence-Railways", "DC v2 (storage adj)"], "story": "Kavach + defence batteries + lithium scaling", "held": True},
        {"ticker": "POWERGRID", "appears_in": ["Energy v2", "DC v2", "Defence-Railways"], "story": "T&D backbone across all three cycles", "held": False},
        {"ticker": "NTPC", "appears_in": ["Energy v2", "DC v2"], "story": "Generation + DC power-source", "held": False},
        {"ticker": "KEC", "appears_in": ["Energy v2", "Defence-Railways"], "story": "T&D + railways EPC", "held": False},
        {"ticker": "POLYCAB / KEI", "appears_in": ["Energy v2", "DC v2"], "story": "Cables (T&D + DC)", "held": False},
        {"ticker": "L&T", "appears_in": ["Defence-Railways", "DC v2"], "story": "AMCA + EPC", "held": False},
        {"ticker": "TATAELXSI", "appears_in": ["AI Players", "DC v2"], "story": "Engineering + chip design", "held": False},
    ]
    triggers = [
        {"trigger": "TARIL revenue growth <12% YoY", "direction": "bear", "status": "✅ on-track (FY26 +23%)"},
        {"trigger": "TARIL OB <Rs 4,000 cr", "direction": "bear", "status": "✅ on-track (Rs 5,005 cr)"},
        {"trigger": "HBL Kavach <800 km/Q for 2 consecutive Q", "direction": "trim", "status": "⚠️ watch Q1 FY27 data"},
        {"trigger": "FY27 actual capex <FY26 RE by >5% at H1", "direction": "de-risk", "status": "⏰ data pending Q2 FY27"},
        {"trigger": "AMCA RFP issued in 90 days from 26-Feb shortlist", "direction": "bull confirm", "status": "⏰ due ~late May"},
        {"trigger": "US 10Y rises >50bp post-Warsh", "direction": "confirms steepener", "status": "🟡 +16bp pre-conf; T+30 = 6/12"},
        {"trigger": "USDINR breaches 96", "direction": "FX-channel impact", "status": "✅ on-track (95.96; duty hike is forex defence)"},
        {"trigger": "AI-deflation: CPI/PCE decel beyond consensus", "direction": "confirms dovish-short", "status": "❌ Apr CPI 3.8% / core 2.8% counter"},
        {"trigger": "Warsh confirmed by 5/15", "direction": "tracking", "status": "✅✅ CHAIR CONFIRMED 5/13 (54-45)"},
        {"trigger": "Trump-Xi Hormuz commitment vs on-ground tightening", "direction": "dual-signal", "status": "🟡 BOTH agreed; Brent +3.4% $107.77"},
        {"trigger": "Oil shock tail (Aramco 2027 normalisation if Hormuz>mid-Jun)", "direction": "upgrade prob", "status": "🚨 UPGRADED"},
        {"trigger": "MTARTECH multi-cycle (AMCA + DC + nuclear)", "direction": "bull confirm", "status": "🚀 +21% Rs 2,279cr; FY27 guidance 50→80%"},
        {"trigger": "KAYNES Q4 credibility", "direction": "EMS basket signal", "status": "❌ -17.6%; JPMorgan downgrade; 179d WC"},
        {"trigger": "MAPMYINDIA: one clean re-accel quarter", "direction": "re-engage", "status": "⏰ Q4 FY26 results May"},
    ]
    gaps = [
        "Financials (NBFC vs PSU bank — partial coverage in Warsh brief only)",
        "Auto / EV (KPIT mentioned; EXIDEIND no thesis doc)",
        "Pharma (BIOCON/SUNPHARMA/DRREDDY — zero coverage)",
        "Cement / Building Materials (zero coverage)",
    ]
    themes = [
        {"theme": "Warsh steepener", "status": "T+5 post-confirmation; T+30 6/12", "start": "2026-05-01", "key_date": "2026-06-12", "color": "warning"},
        {"theme": "Energy v2 (T&D + renewables)", "status": "active; TARIL/POWERGRID/KEC/POLYCAB", "start": "2026-05-03", "key_date": None, "color": "positive"},
        {"theme": "Data Center v2", "status": "active; NTPC/TATAELXSI/POLYCAB/KEI", "start": "2026-05-03", "key_date": None, "color": "info"},
        {"theme": "Defence + Railways capex", "status": "active; HBL/TARIL/L&T/MTARTECH", "start": "2026-05-11", "key_date": "2026-05-23", "color": "accent"},
        {"theme": "India Stack DPI", "status": "active; CDSL/ANGELONE/PAYTM", "start": "2026-04-22", "key_date": None, "color": "info"},
        {"theme": "Silver F98 redeployment", "status": "🔴 P0 PENDING — ~₹2.24Cr cash", "start": "2026-05-15", "key_date": "2026-05-18", "color": "negative"},
    ]
    return {
        "source": str(path.relative_to(ROOT)) if path.exists() else "fallback",
        "cross_thesis_names": cross,
        "falsifiable_triggers": triggers,
        "coverage_gaps": gaps,
        "active_themes": themes,
    }


def _build_catalyst_calendar(held):
    cal = [
        {"date": "2026-05-18", "kind": "macro", "weight": "high", "event": "Japan Q1 GDP + China April activity dump"},
        {"date": "2026-05-19", "kind": "macro", "weight": "med", "event": "RBA minutes (Australia rates)"},
        {"date": "2026-05-20", "kind": "macro", "weight": "med", "event": "BoJ rinban operations"},
        {"date": "2026-05-21", "kind": "macro", "weight": "med", "event": "Samsung union strike begins (KOSPI tech impact)"},
        {"date": "2026-05-22", "kind": "macro", "weight": "high", "event": "Japan April CPI"},
        {"date": "2026-05-23", "kind": "earnings", "weight": "high", "event": "HBLENGINE Q4 FY26 board"},
        {"date": "2026-06-12", "kind": "macro", "weight": "high", "event": "Warsh T+30 deadline"},
        {"date": "2026-06-16", "kind": "macro", "weight": "high", "event": "FOMC (Warsh's first)"},
    ]
    for h in held:
        ed = h.get("next_earnings_date")
        if ed and ed not in ("TBD", "—") and not str(ed).startswith("POSTED"):
            cal.append({"date": str(ed), "kind": "earnings", "weight": "high", "event": f"{h['ticker']} earnings"})
    cal.sort(key=lambda c: c["date"])
    return cal


def _read_recently_closed():
    closed = []
    if not STOCKS_DIR.exists():
        return closed
    for d in STOCKS_DIR.iterdir():
        if not d.is_dir():
            continue
        idx = _parse_frontmatter(d / "_index.md")
        if idx.get("position_status") != "closed":
            continue
        closed.append({"ticker": idx.get("ticker") or d.name, "cluster": idx.get("cluster"),
                       "exit_date": idx.get("exit_date"), "realised_pnl": idx.get("realised_pnl"),
                       "exit_reason": idx.get("exit_reason")})
    closed.sort(key=lambda c: str(c.get("exit_date") or ""), reverse=True)
    return closed[:10]


def main():
    now_utc = datetime.now(timezone.utc)
    out = {
        "schema_version": "v1.1",
        "doc_type": "equity_dashboard_aggregate",
        "emitted_at_utc": now_utc.isoformat(timespec="seconds"),
        "meta": {"account": "SPARCHO-HDFC", "doctrine_refs": ["V-31", "V-32", "V-33"]},
    }
    positions_data = {}
    if POSITIONS_JSON.exists():
        try:
            positions_data = json.loads(POSITIONS_JSON.read_text(encoding="utf-8"))
        except Exception as e:
            positions_data = {"_error": str(e)}
    op = positions_data.get("operator_hdfc", {})
    out["book"] = {"as_of": op.get("as_of"), "snapshot_date": op.get("snapshot_date"),
                   "staleness": op.get("staleness_note"), "totals": op.get("portfolio_totals", {}),
                   "ledger": op.get("ledger", {})}
    held = []
    sr_by_ticker = _read_sr_levels()
    positions = op.get("positions", {}) or {}
    mv_total = sum(float(p.get("current_value") or 0) for p in positions.values()) or 1
    for ticker, pos in positions.items():
        idx = _parse_frontmatter(STOCKS_DIR / ticker / "_index.md")
        sr_levels = sr_by_ticker.get(ticker, [])
        held.append({
            "ticker": ticker, "name": pos.get("name") or idx.get("full_name"),
            "qty": pos.get("qty"), "avg_cost": pos.get("avg_cost"),
            "current_price": pos.get("current_price"), "ltp_date": idx.get("ltp_refresh_date"),
            "invested": pos.get("invested"), "current_value": pos.get("current_value"),
            "unreal_inr": pos.get("unrealized_pnl_abs"), "unreal_pct": pos.get("unrealized_pnl_pct"),
            "day_chg_pct": pos.get("change_today_pct"),
            "concentration_pct": (float(pos.get("current_value") or 0) / mv_total * 100) if mv_total else None,
            "sector": idx.get("sector"), "cluster": idx.get("cluster"), "theme": idx.get("theme"),
            "conviction": idx.get("conviction"), "tier": idx.get("tier"),
            "next_earnings_date": idx.get("next_earnings_date"), "risk_gates": idx.get("risk_gates", {}),
            "regime_lens": idx.get("regime_lens"), "regime_lens_refreshed": idx.get("regime_lens_refreshed"),
            "flow_lens": idx.get("flow_lens"), "fa_lens_score": idx.get("fa_lens_score"),
            "journalist_brief": idx.get("journalist_brief"),
            "rajiv_verdict_latest": _latest_rajiv_verdict(idx),
            "friday_trade": pos.get("friday_trade"),
            "sr_levels": _summarise_sr(sr_levels, pos.get("current_price")),
            "tv_exchange": idx.get("tv_exchange") or "NSE", "tv_symbol": idx.get("tv_symbol") or ticker,
        })
    held.sort(key=lambda h: h.get("concentration_pct") or 0, reverse=True)
    out["held"] = held
    lens_runs = [_read_lens_run(l) for l in LENSES]
    out["screeners"] = {
        "lenses": lens_runs,
        "lens_timeline": _build_lens_timeline(LENSES, days=14),
        "watchlists": [_read_watchlist(w) for w in WATCHLISTS],
        "hit_matrix": _build_hit_matrix(lens_runs, [h["ticker"] for h in held]),
        "promotion_candidates": _build_promotion_candidates(lens_runs, [h["ticker"] for h in held]),
        "held_lens_grid": _build_held_lens_grid(lens_runs, [h["ticker"] for h in held]),
    }
    out["risk_gates"] = _build_risk_gate_view(held)
    out["dr"] = _parse_dr_cross_brief(DR_CROSS)
    out["flags"] = _read_open_flags()
    out["catalysts"] = _build_catalyst_calendar(held)
    out["recent_trades"] = [{
        "ticker": t, "side": pos["friday_trade"].get("side"), "qty": pos["friday_trade"].get("qty"),
        "price": pos["friday_trade"].get("avg_price"), "pnl_today": pos["friday_trade"].get("pnl_today"),
        "date": "2026-05-15",
    } for t, pos in positions.items() if pos.get("friday_trade")]
    out["recent_closed"] = _read_recently_closed()

    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    def _jd(o):
        if hasattr(o, "isoformat"):
            return o.isoformat()
        return str(o)
    body = json.dumps(out, indent=2, ensure_ascii=False, default=_jd)
    OUTPUT_JSON.write_text(body, encoding="utf-8")
    print(f"✅ emitted: {OUTPUT_JSON}  ({len(body)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
