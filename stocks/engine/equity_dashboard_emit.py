#!/usr/bin/env python3
"""equity_dashboard_emit.py v1.2 - emit equity_dashboard_aggregate.json
v1.2 (2026-06-01): location-robust paths; bubble_set + full watchlist_rundown;
live 09_DEEP_RESEARCH stream scan (fallback-guarded); writes web/data + _state."""

from __future__ import annotations
import csv, json, re, sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# daytrade_core: single source of truth for the Day-Trade Fires scoring.
try:
    import daytrade_core
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import daytrade_core

try:
    import yaml
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False

HERE = Path(__file__).resolve().parent


def _find_vault_root(start):
    """Walk up until we find the dir carrying both 02_STOCKS and 03_SCREENERS.
    Makes the emit work regardless of which copy (GENERATORS/ or web/generators/)
    is invoked -- the prior `HERE.parent.parent` broke for the web copy."""
    p = start
    for _ in range(8):
        if (p / "02_STOCKS").is_dir() and (p / "03_SCREENERS").is_dir():
            return p
        if p.parent == p:
            break
        p = p.parent
    return start.parent.parent  # legacy fallback


ROOT = _find_vault_root(HERE)
POSITIONS_JSON = ROOT / "00_SYSTEM" / "GENERATORS" / "_cache" / "positions_unified.json"
TRADE_LAB_JSON = ROOT / "00_SYSTEM" / "EDGE" / "trade_lab.json"  # TRADE LAB payload (spec §7)
DAILY_PRICES = ROOT / "00_SYSTEM" / "GENERATORS" / "_cache" / "daily_prices.csv"
HIST_CSV = ROOT / "00_SYSTEM" / "GENERATORS" / "_cache" / "historical_closes.csv"
SR_CSV = ROOT / "00_SYSTEM" / "AUTO_SR_LEVELS.csv"
STOCKS_DIR = ROOT / "02_STOCKS"
LENS_DIR = ROOT / "03_SCREENERS" / "LAYERS"
WATCHLIST_DIR = ROOT / "03_SCREENERS" / "WATCHLIST"
FLAGS_DIR = ROOT / "00_SYSTEM" / "FLAGS"
DR_DIR = ROOT / "09_DEEP_RESEARCH"
DR_CROSS = DR_DIR / "260512_DR-CROSS-BRIEF-STITCHING.md"
REGIME_YAML = ROOT / "00_SYSTEM" / "GENERATORS" / "_inputs" / "regime_state.yaml"
TAXONOMY_YAML = ROOT / "00_SYSTEM" / "GENERATORS" / "_inputs" / "equity_taxonomy.yaml"
PW_FILE = ROOT / "00_SYSTEM" / "GENERATORS" / "_inputs" / ".dashboard_pw"  # local-only operator MASTER password (gitignored)
CLIENTS_FILE = ROOT / "00_SYSTEM" / "GENERATORS" / "_inputs" / ".dashboard_clients.json"  # {account_key: client_password} (gitignored)
PBKDF2_ITERS = 600000  # OWASP 2023 floor for PBKDF2-SHA256; the browser reads this from the file's `iter`
# account lenses — mirror the dashboard's FAM_* maps so accounts_public carries real labels (no numbers)
FAM_ORDER = ["Account A", "Account B-HUF", "Account E", "Account B", "HDFC", "Others"]  # F123b codenamed
FAM_COL = {"Account A": "#8b7ae0", "Account B-HUF": "#3aa98b", "Account E": "#e0764a", "Account B": "#d9a23a", "HDFC": "#5b8ee0", "Others": "#8a8f98"}  # F123b codenamed
FAM_NAME = {"Account A": "Account A", "Account B-HUF": "Account B-HUF", "Account E": "Account E", "Account B": "Account B", "HDFC": "Operated", "Others": "Acct C · Acct D · Acct F"}  # F123b: codenamed (dead constant)
def _fam_norm(k):
    return "HDFC" if k == "HDFC" else (k if k in ("Account A", "Account B-HUF", "Account E", "Account B") else "Others")
DR_INTEL_YAML = ROOT / "00_SYSTEM" / "GENERATORS" / "_inputs" / "dr_intel.yaml"
# Primary output = the web dashboard's own data folder (what the page + deploy read).
# Mirror to 00_SYSTEM/_state for parity. WEB_DATA only written if its parent exists
# (true when the web/generators copy runs; the GENERATORS copy just writes _state).
WEB_DATA = HERE.parent / "data" / "equity_dashboard_aggregate.json"
STATE_OUT = ROOT / "00_SYSTEM" / "_state" / "equity_dashboard_aggregate.json"

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
    if DR_INTEL_YAML.exists() and HAVE_YAML:
        try:
            y = yaml.safe_load(DR_INTEL_YAML.read_text(encoding="utf-8")) or {}
            if y.get("cross_thesis_names") or y.get("falsifiable_triggers"):
                return {
                    "source": "live", "intel_source": "dr_intel.yaml",
                    "as_of": str(y.get("as_of") or ""),
                    "cross_thesis_names": y.get("cross_thesis_names", []),
                    "falsifiable_triggers": y.get("falsifiable_triggers", []),
                    "coverage_gaps": y.get("coverage_gaps", []),
                    "active_themes": y.get("active_themes", []),
                }
        except Exception:
            pass
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
    ]
    return {
        "source": str(path.relative_to(ROOT)) if path.exists() else "fallback",
        "cross_thesis_names": cross,
        "falsifiable_triggers": triggers,
        "coverage_gaps": gaps,
        "active_themes": themes,
    }


def _build_catalyst_calendar(held):
    """Today-forward calendar. Macro anchors per the 2026-06-01 build brief / design-spec
    section 5 (operator-authored): the June double-binary + CPI + Warsh window + FOMC.
    Past-dated rows are dropped so a father-facing timeline never shows stale events."""
    today = datetime.now().strftime("%Y-%m-%d")
    cal = [
        {"date": "2026-06-05", "kind": "macro", "weight": "high", "event": "US May NFP + RBI MPC decision -- the double-binary"},
        {"date": "2026-06-10", "kind": "macro", "weight": "high", "event": "US May CPI"},
        {"date": "2026-06-12", "kind": "macro", "weight": "med", "event": "Warsh T+30 deadline (steepener confirm window)"},
        {"date": "2026-06-16", "kind": "macro", "weight": "high", "event": "FOMC begins -- first under Warsh"},
        {"date": "2026-06-17", "kind": "macro", "weight": "high", "event": "FOMC decision + press conference"},
    ]
    for h in held:
        ed = h.get("next_earnings_date")
        if ed and ed not in ("TBD", "—") and not str(ed).startswith("POSTED"):
            cal.append({"date": str(ed), "kind": "earnings", "weight": "high", "event": f"{h['ticker']} earnings"})
    cal = [c for c in cal if c["date"] >= today]
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


def _read_universe_meta():
    """ticker -> {sector, lean(+1 BULL/-1 BEAR/0), rsi_div_kind} from latest Rajiv-DIV CSV."""
    meta = {}
    d = LENS_DIR / "Rajiv-DIV"
    csvs = sorted(d.glob("*_Rajiv-DIV.csv"), reverse=True) if d.exists() else []
    if not csvs:
        return meta
    with csvs[0].open("r", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            t = r.get("ticker")
            if not t:
                continue
            kind = (r.get("rsi_div_kind") or "").strip().upper()
            lean = 1 if kind == "BULL" else (-1 if kind == "BEAR" else 0)
            def _f(key):
                try:
                    return float(r.get(key) or 0)
                except (TypeError, ValueError):
                    return None
            meta[t] = {"sector": r.get("sector"), "lean": lean, "rsi_div_kind": kind or None,
                       "stddev_10d_pct": _f("stddev_10d_pct"),
                       "vol_spike_days": int(_f("vol_spike_days") or 0),
                       "vol_spike": (r.get("G2_volume_spike") == "1"),
                       "rsi_today": _f("rsi_today")}
    return meta


def _build_name_index(lens_runs):
    """ticker -> {lens:{status,ratio,score}, multi_hit_count, composite(0-1)}."""
    idx = {}
    for ld in lens_runs:
        lens = ld["lens"]
        for hit in ld.get("hits", []):
            t = hit["ticker"]
            e = idx.setdefault(t, {"ticker": t, "lens": {}, "multi_hit_count": 0})
            tg = hit["total_gates"]
            pc = hit["pass_count"]
            ratio = pc / tg if tg else 0.0
            status = "pass" if hit["passes_all"] else ("near" if tg and pc >= tg - 1 else "fail")
            e["lens"][lens] = {"status": status, "ratio": ratio, "score": f"{pc}/{tg}"}
            if status in ("pass", "near"):
                e["multi_hit_count"] += 1
    for e in idx.values():
        ratios = [e["lens"].get(l, {}).get("ratio", 0.0) for l in LENSES]
        e["composite"] = sum(ratios) / len(LENSES)
    return idx


def _load_daily_prices():
    """ticker -> sorted [(date_iso, close_float)] from daily_prices.csv."""
    series = {}
    if not DAILY_PRICES.exists():
        return series
    with DAILY_PRICES.open("r", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            t, dte, cl = r.get("ticker"), r.get("date"), r.get("close")
            if not t or not dte or not cl:
                continue
            try:
                clf = float(cl)
            except ValueError:
                continue
            series.setdefault(t, []).append((dte, clf))
    for t in series:
        series[t].sort()
    return series


def _week_change(series, ticker, sessions=5):
    """1-week % from the latest available close vs ~`sessions` trading rows earlier."""
    pts = series.get(ticker) or []
    if len(pts) < 2:
        return None
    latest_d, latest_c = pts[-1]
    ref_i = max(0, len(pts) - 1 - sessions)
    ref_d, ref_c = pts[ref_i]
    if not ref_c:
        return None
    return {"pct": (latest_c - ref_c) / ref_c * 100, "as_of": latest_d, "ref_date": ref_d, "ltp": latest_c}


def _load_taxonomy():
    """ticker -> {layer, cluster} + layer_order/layer_names from equity_taxonomy.yaml.
    The 5LC layer (primary) + USP cluster (secondary) grouping source of truth."""
    out = {"tickers": {}, "layer_order": [], "layer_names": {}}
    if not TAXONOMY_YAML.exists() or not HAVE_YAML:
        return out
    try:
        y = yaml.safe_load(TAXONOMY_YAML.read_text(encoding="utf-8")) or {}
    except Exception:
        return out
    out["tickers"] = y.get("tickers", {}) or {}
    out["layer_order"] = y.get("layer_order", []) or []
    out["layer_names"] = y.get("layer_names", {}) or {}
    return out


def _moves(series, ticker):
    """1d/3d/5d % moves from the latest available close. None if no history."""
    pts = series.get(ticker) or []
    if len(pts) < 2:
        return None
    latest_d, latest_c = pts[-1]
    def pct(sessions):
        i = len(pts) - 1 - sessions
        if i < 0 or not pts[i][1]:
            return None
        return round((latest_c - pts[i][1]) / pts[i][1] * 100, 2)
    return {"as_of": latest_d, "ltp": latest_c, "d1": pct(1), "d3": pct(3), "d5": pct(5)}


def _build_bubble_set(name_index, universe_meta, held_tickers, taxonomy):
    """Per-name bubble: composite(0-100), lean, multi_hit_count, held, sector, 5LC layer/cluster, lens chips."""
    tax = taxonomy.get("tickers", {})
    out = []
    for t, e in name_index.items():
        m = universe_meta.get(t, {})
        tx = tax.get(t, {})
        out.append({
            "ticker": t,
            "composite": round(e["composite"] * 100, 1),
            "lean": m.get("lean", 0),
            "multi_hit_count": e["multi_hit_count"],
            "held": t in held_tickers,
            "sector": m.get("sector"),
            "layer": tx.get("layer", "UNMAPPED"),
            "cluster": tx.get("cluster"),
            "lens": e["lens"],
        })
    out.sort(key=lambda r: (-r["multi_hit_count"], -r["composite"], r["ticker"]))
    return out


def _build_watchlist_rundown(name_index, universe_meta, prices, held_tickers, taxonomy):
    """Every tracked name: composite + 1d/3d/5d moves + 5LC layer/cluster + held/watch + pending.
    Union includes taxonomy names not yet lens-scored (the 11 new adds) -> shown 'pending'."""
    tax = taxonomy.get("tickers", {})
    names = set(name_index) | set(universe_meta) | set(tax)
    rows = []
    for t in sorted(names):
        e = name_index.get(t, {"composite": 0.0, "multi_hit_count": 0})
        m = universe_meta.get(t, {})
        tx = tax.get(t, {})
        mv = _moves(prices, t)
        rows.append({
            "ticker": t,
            "composite": round(e["composite"] * 100, 1),
            "multi_hit_count": e["multi_hit_count"],
            "moves": mv,
            "as_of": mv["as_of"] if mv else None,
            "ltp": mv["ltp"] if mv else None,
            "lean": m.get("lean", 0),
            "sector": m.get("sector"),
            "layer": tx.get("layer", "UNMAPPED"),
            "cluster": tx.get("cluster") or m.get("sector") or "Unmapped",
            "tag": "held" if t in held_tickers else "watch",
            "pending": t not in name_index,
        })
    rows.sort(key=lambda r: (-r["composite"], r["ticker"]))
    return rows


def _prior_multi_hits():
    """ticker -> multi-hit count from the SECOND-most-recent CSV per lens (for fresh-fire diff)."""
    prior = {}
    for lens in LENSES:
        d = LENS_DIR / lens
        csvs = sorted(d.glob(f"*_{lens}.csv"), reverse=True) if d.exists() else []
        if len(csvs) < 2:
            continue
        with csvs[1].open("r", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                tk = r.get("ticker")
                if not tk:
                    continue
                pc = int(r.get("pass_count") or 0)
                tg = int(r.get("total_gates") or 0)
                hit = (r.get("passes_all") == "1") or (tg > 0 and pc >= tg - 1)
                if hit:
                    prior[tk] = prior.get(tk, 0) + 1
    return prior


def _load_hist_closes():
    """ticker -> sorted [(date, close)] from historical_closes.csv (deep history, all names)."""
    out = {}
    if not HIST_CSV.exists():
        return out
    with HIST_CSV.open("r", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            tk, d, c = r.get("ticker"), r.get("date"), r.get("close")
            if not (tk and d and c):
                continue
            try:
                out.setdefault(tk, []).append((d, float(c)))
            except ValueError:
                continue
    for tk in out:
        out[tk].sort()
    return out


def _load_ohlc():
    """ticker -> sorted [(date,o,h,l,c)] from daily_prices.csv (for confirmation-candle reads)."""
    out = {}
    if not DAILY_PRICES.exists():
        return out
    with DAILY_PRICES.open("r", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            tk, d = r.get("ticker"), r.get("date")
            if not (tk and d):
                continue
            try:
                o, h, l, c = float(r.get("open") or 0), float(r.get("high") or 0), float(r.get("low") or 0), float(r.get("close") or 0)
            except ValueError:
                continue
            if c <= 0:
                continue
            out.setdefault(tk, []).append((d, o, h, l, c))
    for tk in out:
        out[tk].sort()
    return out


def _build_daytrade_panel(name_index, universe_meta, prices, prior_mh, held_tickers):
    """F128 reconciliation (2026-06-24): route through the single shared daytrade_core
    scorer instead of an inline copy, so ci_screener_emit (which calls this) and the
    full emit (which calls daytrade_core.build_panel directly) can never drift -- the
    Rajiv falling-knife discipline lives ONCE, in daytrade_core."""
    cands = daytrade_core.assemble_candidates(name_index, universe_meta, prior_mh)
    rows, _ = daytrade_core.build_panel(cands, _load_ohlc(), held_tickers)
    return rows


def _scan_dr_streams(universe):
    """Live scan of 09_DEEP_RESEARCH/{THEMATIC,COMPANY,COMPARISON}/*.md.
    Per file: title, type, mtime + age-in-days, names referenced, link, status."""
    streams = []
    today = datetime.now().date()
    for folder, typ in [("THEMATIC", "thematic"), ("COMPANY", "company"), ("COMPARISON", "comparison")]:
        d = DR_DIR / folder
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.md")):
            if p.name.startswith("_"):
                continue
            fm = _parse_frontmatter(p)
            try:
                body = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                body = ""
            title = fm.get("title")
            if not title:
                mh = re.search(r"^#\s+(.+)$", body, re.M)
                title = mh.group(1).strip() if mh else p.stem
            title = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", str(title))
            title = re.sub(r"\(computer://[^)]*\)", "", title)
            title = re.sub(r"\s+", " ", title).strip().rstrip("([ ")
            mtime = datetime.fromtimestamp(p.stat().st_mtime).date()
            age = (today - mtime).days
            status = "active" if age <= 30 else ("aging" if age <= 90 else "archived")
            names = sorted({t for t in universe if re.search(r"\b" + re.escape(t) + r"\b", body)})
            streams.append({
                "title": title, "type": typ, "file": p.name,
                "rel_path": str(p.relative_to(ROOT)).replace("\\", "/"),
                "last_modified": mtime.isoformat(), "age_days": age, "status": status,
                "names": names[:12], "name_count": len(names),
            })
    streams.sort(key=lambda s: s["age_days"])
    return streams


def _trading_dates():
    dates = set()
    if DAILY_PRICES.exists():
        with DAILY_PRICES.open("r", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("date"):
                    dates.add(r["date"])
    return sorted(dates)


def _derive_book_meta(op):
    """snapshot_date (from source_folder YYMMDD) + NSE-sessions-stale vs daily_prices."""
    src = op.get("source_folder") or ""
    m = re.search(r"(\d{6})", src)
    snap_iso = None
    if m:
        g = m.group(1)
        snap_iso = f"20{g[:2]}-{g[2:4]}-{g[4:6]}"
    sessions_stale = None
    if snap_iso:
        sessions_stale = sum(1 for d in _trading_dates() if d > snap_iso)
    return snap_iso, sessions_stale


def _read_regime():
    """Regime zone + gates + macro rails from regime_state.yaml (operator-signed source).
    Feeds the regime badge, hero gate tile, and ticker TNX/USDINR rails -- never hardcoded."""
    out = {"zone": None, "score": None, "headline": None, "last_updated": None,
           "gates": [], "macro": {}, "binding_gate": None}
    if not REGIME_YAML.exists() or not HAVE_YAML:
        return out
    try:
        y = yaml.safe_load(REGIME_YAML.read_text(encoding="utf-8")) or {}
    except Exception:
        return out
    reg = y.get("regime", {}) or {}
    out["zone"] = reg.get("zone")
    out["score"] = reg.get("score")
    out["headline"] = reg.get("headline")
    out["last_updated"] = str(y.get("last_updated")) if y.get("last_updated") else None
    gates = y.get("gates", []) or []
    out["gates"] = [{"id": g.get("id"), "status": g.get("status"), "reading": g.get("reading"),
                     "threshold": g.get("threshold"), "note": g.get("note")} for g in gates]
    ri = y.get("raw_inputs", {}) or {}
    for k in ("TNX", "USDINR", "DXY", "BRENT", "USDJPY"):
        v = ri.get(k) or {}
        if v.get("value") is not None:
            out["macro"][k] = {"value": v.get("value"), "as_of": str(v.get("as_of")) if v.get("as_of") else None}
    # gate-status lookup merged onto macro rails
    gate_by_src = {g.get("live_source"): g for g in gates}
    for k, m in out["macro"].items():
        g = gate_by_src.get(k)
        if g:
            m["gate_status"] = g.get("status")
            m["gate_threshold"] = g.get("threshold")
    # binding gate = first WATCH/BREACH (TNX in the 260531 read)
    out["binding_gate"] = next((g for g in out["gates"]
                                if str(g.get("status", "")).upper() in ("WATCH", "BREACH", "BREACHED")), None)
    return out


def _accounts_present(sensitive):
    """Ordered famNorm lens keys that actually hold something — drives accounts_public (no numbers)."""
    present = set()
    if sensitive.get("held"):
        present.add("HDFC")
    ra = sensitive.get("rajiv_account")
    if isinstance(ra, dict):
        for r in ((ra.get("holdings") or {}).get("per_stock") or []):
            for k in (r.get("accounts") or {}):
                present.add(_fam_norm(k))
    return [k for k in FAM_ORDER if k in present]


def _slice_account(sensitive, K):
    """A single account's private payload — same shape as the master, filtered to lens K.
    HDFC = the operator book (held); everything else = that lens's slice of the father's My-Stocks."""
    held = sensitive.get("held") or []
    if K == "HDFC":
        heldK, rajivK = held, None
        totals, ledger = sensitive.get("book_totals", {}), sensitive.get("book_ledger", {})
        perf, gates = sensitive.get("performance"), sensitive.get("risk_gates", [])
    else:
        heldK, totals, ledger, perf, gates = [], {}, {}, None, []
        rajivK = None
        ra = sensitive.get("rajiv_account")
        if isinstance(ra, dict):
            hh = ra.get("holdings") or {}
            ps = []
            for r in (hh.get("per_stock") or []):
                acc = {k: v for k, v in (r.get("accounts") or {}).items() if _fam_norm(k) == K}
                if acc:
                    r2 = dict(r); r2["accounts"] = acc; ps.append(r2)
            unl = [u for u in (hh.get("unlisted") or [])
                   if any(_fam_norm(k) == K for k in (u.get("accounts") or {}))]
            at = [a for a in (hh.get("account_totals") or []) if _fam_norm(a.get("account", "")) == K]
            rajivK = {"holdings": {"per_stock": ps, "unlisted": unl, "account_totals": at,
                                   "grand_totals": hh.get("grand_totals", {})},
                      "pv_history": ra.get("pv_history")}
    tickers = set(h.get("ticker") for h in heldK if h.get("ticker"))
    return {
        "client_account": K, "account_label": FAM_NAME.get(K, K),
        "book_totals": totals, "book_ledger": ledger,
        "held": heldK, "held_tickers": list(tickers),
        "held_lens_grid": [g for g in (sensitive.get("held_lens_grid") or []) if g.get("ticker") in tickers],
        "catalysts": [c for c in (sensitive.get("catalysts") or []) if c.get("ticker") in tickers],
        "risk_gates": gates, "recent_trades": [], "recent_closed": [],
        "rajiv_account": rajivK, "performance": perf,
    }


def _apply_privacy(out):
    """If a local operator password is set, encrypt the sensitive block + strip plaintext.
    Public aggregate then carries holdings only as AES-GCM ciphertext (browser-decryptable
    with the password). No password file -> plaintext (unchanged). The encrypted payload is
    PBKDF2-SHA256(200k) -> AES-256-GCM, matching the dashboard's WebCrypto unlock."""
    if not PW_FILE.exists():
        return
    try:
        pw = PW_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return
    if not pw:
        return
    try:
        import os as _os, base64 as _b64, hashlib as _hl
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except Exception as e:  # pragma: no cover
        print(f"[privacy] cryptography unavailable ({e}) -> PLAINTEXT emit", file=sys.stderr)
        return
    book = out.get("book", {}) or {}
    scr = out.get("screeners", {}) or {}
    held_tickers = [h.get("ticker") for h in out.get("held", []) if h.get("ticker")]
    # v2 — the WHOLE book is private: full holdings (names included), held-only screener views,
    # held catalysts, gates, trades, father's account + performance. held_tickers travels along so
    # the browser can re-mark the public screener after unlock.
    sensitive = {
        "book_totals": book.get("totals", {}),
        "book_ledger": book.get("ledger", {}),
        "held": out.get("held", []),                       # FULL holdings incl. names/sector/price
        "held_tickers": held_tickers,
        "held_lens_grid": scr.get("held_lens_grid", []),
        "catalysts": out.get("catalysts", []),
        "risk_gates": out.get("risk_gates", []),
        "recent_trades": out.get("recent_trades", []),
        "recent_closed": out.get("recent_closed", []),
        "rajiv_account": out.get("rajiv_account"),
        "performance": out.get("performance"),
        # TRADE LAB (spec §7): full payload is operator-only — same treatment as the book.
        "trade_lab": out.get("trade_lab"),
    }
    def enc(pw_, d):
        p = json.dumps(d, ensure_ascii=False, default=str).encode("utf-8")
        s_, i_ = _os.urandom(16), _os.urandom(12)
        k_ = _hl.pbkdf2_hmac("sha256", pw_.encode("utf-8"), s_, PBKDF2_ITERS, 32)
        c_ = AESGCM(k_).encrypt(i_, p, None)
        return {"v": 2, "iter": PBKDF2_ITERS, "salt": _b64.b64encode(s_).decode(),
                "iv": _b64.b64encode(i_).decode(), "ct": _b64.b64encode(c_).decode()}, len(c_)
    out["sensitive_enc"], ct_len = enc(pw, sensitive)   # MASTER — operator sees the whole family
    out["privacy"] = {"locked": True, "hidden": ["operator_section", "book_pnl_hero", "holdings"]}
    # numbers-free account roster for the locked landing (count + real labels, NO performance)
    present = _accounts_present(sensitive)
    out["accounts_public"] = [{"key": k, "label": FAM_NAME.get(k, k), "color": FAM_COL.get(k, "#8a8f98")} for k in present]
    # per-client blobs — each client password unlocks ONLY that account's slice
    clients = {}
    if CLIENTS_FILE.exists():
        try:
            clients = json.loads(CLIENTS_FILE.read_text(encoding="utf-8")) or {}
        except Exception as e:
            print(f"[privacy] clients config unreadable ({e}) — master-only", file=sys.stderr)
    accounts_enc = {}
    for K, cpw in clients.items():
        if cpw:
            accounts_enc[K], _ = enc(cpw, _slice_account(sensitive, K))
    if accounts_enc:
        out["accounts_enc"] = accounts_enc
    # ── strip EVERY position tell from the public aggregate ──
    book.pop("totals", None)
    book.pop("ledger", None)
    out["held"] = []                       # no holdings — names, sectors or money — in the public file
    out["risk_gates"] = []
    out["recent_trades"] = []
    out["recent_closed"] = []
    out["catalysts"] = []
    out.pop("rajiv_account", None)
    out.pop("performance", None)
    out.pop("trade_lab", None)
    # public screener stays useful but reveals NO held flag (held names look like any tracked name)
    scr["held_lens_grid"] = []
    for key in ("hit_matrix", "bubble_set", "daytrade_panel"):
        for r in scr.get(key, []) or []:
            if isinstance(r, dict) and "held" in r:
                r["held"] = False
    for r in scr.get("watchlist_rundown", []) or []:
        if isinstance(r, dict) and r.get("tag") == "held":
            r["tag"] = "watch"
    hm = scr.get("hit_matrix")               # was sorted held-first; re-sort neutrally so order can't leak
    if isinstance(hm, list):
        hm.sort(key=lambda r: (-(r.get("multi_hit_count") or 0), r.get("ticker") or ""))
    print(f"[privacy] LOCKED v2: master blob {ct_len}b; {len(present)} accounts public (labels only); "
          f"{len(out.get('accounts_enc', {}))} per-client blob(s); names + screener held-flags stripped")


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
    _snap_iso, _sessions_stale = _derive_book_meta(op)
    _ledger = op.get("ledger", {}) or {}
    out["book"] = {"as_of": op.get("as_of"),
                   "snapshot_date": op.get("snapshot_date") or _snap_iso,
                   "source_folder": op.get("source_folder"),
                   "sessions_stale": _sessions_stale,
                   "fetched_at": positions_data.get("fetched_at"),
                   "staleness": op.get("staleness_note"),
                   "ledger_available": any(v is not None for v in _ledger.values()),
                   "totals": op.get("portfolio_totals", {}),
                   "ledger": _ledger}
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
    held_tickers = [h["ticker"] for h in held]
    name_index = _build_name_index(lens_runs)
    universe_meta = _read_universe_meta()
    prices = _load_daily_prices()
    taxonomy = _load_taxonomy()
    _dt_cands = daytrade_core.assemble_candidates(name_index, universe_meta, _prior_multi_hits())
    _dt_rows, _dt_price_as_of = daytrade_core.build_panel(_dt_cands, _load_ohlc(), held_tickers)
    out["screeners"] = {
        "lenses": lens_runs,
        "lens_timeline": _build_lens_timeline(LENSES, days=14),
        "watchlists": [_read_watchlist(w) for w in WATCHLISTS],
        "hit_matrix": _build_hit_matrix(lens_runs, held_tickers),
        "promotion_candidates": _build_promotion_candidates(lens_runs, held_tickers),
        "held_lens_grid": _build_held_lens_grid(lens_runs, held_tickers),
        "bubble_set": _build_bubble_set(name_index, universe_meta, held_tickers, taxonomy),
        "watchlist_rundown": _build_watchlist_rundown(name_index, universe_meta, prices, held_tickers, taxonomy),
        "universe_count": len(set(name_index) | set(universe_meta) | set(taxonomy.get("tickers", {}))),
        "layer_order": taxonomy.get("layer_order", []),
        "layer_names": taxonomy.get("layer_names", {}),
        "daytrade_panel": _dt_rows,
    }
    out["daytrade_inputs"] = {"candidates": _dt_cands, "held": held_tickers,
                              "score_version": daytrade_core.SCORE_VERSION}
    out["daytrade_freshness"] = daytrade_core.daytrade_freshness(_dt_price_as_of)
    out["risk_gates"] = _build_risk_gate_view(held)
    dr = _parse_dr_cross_brief(DR_CROSS)
    try:
        universe_names = set(name_index) | set(universe_meta) | set(held_tickers)
        streams = _scan_dr_streams(universe_names)
        dr["streams"] = streams
        dr["streams_scanned"] = len(streams)
        if streams:
            dr["source"] = "live"
    except Exception as e:  # guarded fallback -- keep curated source string
        dr["streams"] = []
        dr["streams_error"] = str(e)
    out["dr"] = dr
    out["flags"] = _read_open_flags()
    out["regime"] = _read_regime()
    out["catalysts"] = _build_catalyst_calendar(held)
    out["recent_trades"] = [{
        "ticker": t, "side": pos["friday_trade"].get("side"), "qty": pos["friday_trade"].get("qty"),
        "price": pos["friday_trade"].get("avg_price"), "pnl_today": pos["friday_trade"].get("pnl_today"),
        "date": "2026-05-15",
    } for t, pos in positions.items() if pos.get("friday_trade")]
    out["recent_closed"] = _read_recently_closed()
    # TRADE LAB payload (trade_tracker_emit.py, spec §5/§7). Operator-only: _apply_privacy
    # encrypts it into sensitive_enc + strips the plaintext. If NO password is configured
    # (plaintext emit), apply --public-strip semantics: real-trade tickers masked. The payload
    # carries R-multiples/levels only — never share counts or notionals (spec §5).
    try:
        out["trade_lab"] = (json.loads(TRADE_LAB_JSON.read_text(encoding="utf-8"))
                            if TRADE_LAB_JSON.exists() else None)
    except Exception as _e:
        out["trade_lab"] = {"_error": str(_e)}
    if out["trade_lab"] and not PW_FILE.exists():
        for _rows in (out["trade_lab"].get("open"), out["trade_lab"].get("recent_closed")):
            for _r in (_rows or []):
                if isinstance(_r, dict) and _r.get("trade_type") == "real":
                    _r["ticker"] = "HELD-•"

    _apply_privacy(out)
    def _jd(o):
        if hasattr(o, "isoformat"):
            return o.isoformat()
        return str(o)
    body = json.dumps(out, indent=2, ensure_ascii=False, default=_jd)
    written = []
    for target in (STATE_OUT, WEB_DATA):
        if target is WEB_DATA and not target.parent.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        written.append(target)
    lens_dates = ", ".join(f"{r['lens']}={r.get('run_date')}" for r in out["screeners"]["lenses"])
    print(f"[ok] emitted ({len(body)} bytes) -> " + " | ".join(str(p) for p in written))
    print(f"[ok] lens run_dates: {lens_dates}")
    print(f"[ok] bubble_set names: {len(out['screeners']['bubble_set'])} | "
          f"watchlist_rundown: {len(out['screeners']['watchlist_rundown'])} | "
          f"dr.source={out['dr'].get('source')} | dr.streams={out['dr'].get('streams_scanned', 0)}")
    print(f"[ok] regime: {out['regime'].get('zone')} (score {out['regime'].get('score')}) | "
          f"macro rails: {list(out['regime'].get('macro', {}).keys())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
