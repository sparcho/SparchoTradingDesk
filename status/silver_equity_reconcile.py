#!/usr/bin/env python3
"""
silver_equity_reconcile.py — LOCAL cross-desk reconciliation (the real "catch the mismatch").

The silver desk and the equity family book both track the SAME SILVERBEES position from
DIFFERENT sources:
  • silver desk  = hand-maintained buy/sell history in silver_holdings.yaml (manual mirror)
  • equity book  = the live My-Stocks pull (SILVERBEES is held there, shown as the "silver pot")
They drift: the silver deployment/dry-powder figure goes stale while equity stays live. This
script decrypts BOTH aggregates locally, compares the SILVERBEES position, and — if they diverge
beyond tolerance — records a privacy-clean 🔴 on the notifications feed.

WHY LOCAL-ONLY: both sides are AES-GCM encrypted with DIFFERENT passwords, and the cloud has
neither. So this must run on the operator's machine (or be wired into the vault watchdog). The
cloud watchdog's silver-book staleness flag (watchdog_ci.py) is the cloud-side complement.

Usage:
  python status/silver_equity_reconcile.py            # report to stdout only
  python status/silver_equity_reconcile.py --write     # also append a feed event on divergence
  python status/silver_equity_reconcile.py --dump      # print decrypted structure keys (to tune extractors)
  python status/silver_equity_reconcile.py --tol 0.05  # divergence tolerance (default 5%)

Passwords (each side): env SILVER_PW / EQUITY_PW  →  the local .dashboard_pw files  →  --pw <shared>.
Exit 0 on match / can't-determine; exit 0 always when --write (a reconciler must not break a cron).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EQUITY_AGG = ROOT / "stocks" / "data" / "equity_dashboard_aggregate.json"
SILVER_AGG = ROOT / "silver" / "data" / "silver_dashboard_aggregate.json"
EQUITY_PW_FILES = [ROOT / "stocks" / "_inputs" / ".dashboard_pw",
                   ROOT.parent / "00_SYSTEM" / "GENERATORS" / "_inputs" / ".dashboard_pw"]
SILVER_PW_FILES = [ROOT / "silver" / "_inputs" / ".dashboard_pw"]
SILVER_TICKERS = {"SILVERBEES", "GOLDBEES"}

sys.path.insert(0, str(Path(__file__).resolve().parent))
import notif_feed as nf  # noqa: E402

FEED = ROOT / "status" / "notifications.json"
SOURCE = "reconcile"


# ── crypto (mirror of the dashboards' WebCrypto: PBKDF2-SHA256 → AES-256-GCM) ──
def decrypt_blob(blob: dict, pw: str):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt = base64.b64decode(blob["salt"]); iv = base64.b64decode(blob["iv"]); ct = base64.b64decode(blob["ct"])
    key = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, int(blob.get("iter", 200000)), 32)
    return json.loads(AESGCM(key).decrypt(iv, ct, None))


def resolve_pw(env_name: str, files: list[Path], shared: str | None) -> str | None:
    if os.environ.get(env_name):
        return os.environ[env_name].strip()
    for f in files:
        try:
            if f.exists():
                v = f.read_text(encoding="utf-8").strip()
                if v:
                    return v
        except Exception:
            continue
    return shared


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ── extractors ───────────────────────────────────────────────────────────────
def equity_silver_position(u: dict) -> dict:
    """SILVERBEES/GOLDBEES value + qty from the decrypted equity payload (v2 `held`, v1 `held_private`)."""
    rows = u.get("held") if isinstance(u.get("held"), list) else u.get("held_private")
    rows = rows if isinstance(rows, list) else []
    val = 0.0; qty = 0.0; hit = []
    for h in rows:
        if not isinstance(h, dict):
            continue
        if str(h.get("ticker") or "").upper() in SILVER_TICKERS:
            val += _num(h.get("current_value")) or 0.0
            qty += _num(h.get("qty")) or 0.0
            hit.append(h.get("ticker"))
    return {"value": val or None, "qty": qty or None, "tickers": hit}


def silver_desk_position(u: dict, live_price) -> dict:
    """Best-effort: locate the silver desk's current units/value in the decrypted silver payload.
    Structure is operator-curated and not fully visible to this repo, so we search common shapes and
    fall back to units×live_price. Run with --dump to see the real keys and pin the path if this misses."""
    val = None; qty = None; via = None
    cands = []
    ft = u.get("family_totals")
    if isinstance(ft, dict):
        cands.append(("family_totals", ft))
    acc = u.get("accounts")
    if isinstance(acc, dict):
        for k, v in acc.items():
            if isinstance(v, dict):
                cands.append((f"accounts.{k}", v))
    for label, d in cands:
        for k, v in d.items():
            n = _num(v)
            if n is None:
                continue
            kl = k.lower()
            if qty is None and ("unit" in kl or "qty" in kl or "quantity" in kl):
                qty = n; via = via or f"{label}.{k}"
            if val is None and ("current_value" in kl or "market_value" in kl or "mv" in kl
                                or kl in ("value", "current", "holding_value")):
                val = n; via = via or f"{label}.{k}"
    # deployment ladder fallback: sum remaining-tranche units if present
    if qty is None:
        dp = u.get("deployment_plan")
        tr = dp.get("tranches") if isinstance(dp, dict) else None
        if isinstance(tr, list):
            s = sum(_num(t.get("qty_remaining") or t.get("qty") or t.get("units")) or 0 for t in tr if isinstance(t, dict))
            if s:
                qty = s; via = "deployment_plan.tranches.sum(qty_remaining)"
    if val is None and qty is not None and _num(live_price):
        val = qty * float(live_price); via = (via or "") + " ×live_price"
    return {"value": val, "qty": qty, "via": via}


def reconcile(tol: float, shared_pw: str | None, dump: bool):
    """Returns (status, message, detail). status ∈ {match, drift, indeterminate, error}."""
    try:
        eq = json.loads(EQUITY_AGG.read_text(encoding="utf-8"))
        sv = json.loads(SILVER_AGG.read_text(encoding="utf-8"))
    except Exception as e:
        return "error", f"could not read an aggregate: {e}", {}

    eq_pw = resolve_pw("EQUITY_PW", EQUITY_PW_FILES, shared_pw)
    sv_pw = resolve_pw("SILVER_PW", SILVER_PW_FILES, shared_pw)
    if not eq_pw or not sv_pw:
        return "indeterminate", "missing a password (set EQUITY_PW / SILVER_PW, or pass --pw)", {}

    try:
        eu = decrypt_blob(eq["sensitive_enc"], eq_pw)
    except Exception as e:
        return "error", f"equity decrypt failed (wrong EQUITY_PW?): {e}", {}
    try:
        su = decrypt_blob(sv["sensitive_enc"], sv_pw)
    except Exception as e:
        return "error", f"silver decrypt failed (wrong SILVER_PW?): {e}", {}

    if dump:
        print("── equity sensitive keys:", list(eu.keys()))
        print("── silver sensitive keys:", list(su.keys()))
        for k in ("family_totals", "accounts", "deployment_plan"):
            if isinstance(su.get(k), dict):
                print(f"   silver.{k} keys:", list(su[k].keys()))

    live = (((sv.get("current_market") or {}).get("silverbees") or {}).get("price")
            or (sv.get("current_price") or {}).get("primary_inr"))
    eqp = equity_silver_position(eu)
    svp = silver_desk_position(su, live)
    detail = {"equity": eqp, "silver": svp, "live_price": live, "tol": tol}

    # prefer a price-independent UNITS comparison; fall back to value
    e_qty, s_qty = eqp.get("qty"), svp.get("qty")
    e_val, s_val = eqp.get("value"), svp.get("value")
    if e_qty and s_qty:
        rel = abs(e_qty - s_qty) / max(abs(e_qty), 1e-9)
        basis = "units"
    elif e_val and s_val:
        rel = abs(e_val - s_val) / max(abs(e_val), 1e-9)
        basis = "value"
    else:
        return "indeterminate", ("could not locate a comparable SILVERBEES figure on both sides "
                                 "(run --dump and tune silver_desk_position)"), detail
    detail["rel_diff"] = rel
    detail["basis"] = basis
    if rel > tol:
        return "drift", (f"silver desk vs family book disagree on the SILVERBEES position "
                         f"by {rel*100:.0f}% ({basis} basis, tol {tol*100:.0f}%)"), detail
    return "match", f"silver desk and family book agree on SILVERBEES ({basis}, diff {rel*100:.1f}%)", detail


def run(tol: float = 0.05, write: bool = False, shared_pw: str | None = None) -> list[dict]:
    """Importable entry (e.g. for the vault watchdog). Returns the feed events it would record."""
    status, msg, _ = reconcile(tol, shared_pw, dump=False)
    events = []
    if status == "drift":
        # privacy-clean: NO numbers, NO money tokens — just "the desks disagree, reconcile"
        ev = nf.make_event("🔴", "silver-reconcile",
                           "silver desk position disagrees with the family book beyond tolerance",
                           "reconcile the silver buy history with the live SILVERBEES holding, then re-emit",
                           "pending", SOURCE)
        try:
            nf.assert_clean(ev)
        except Exception:
            ev["spotted"] = "silver desk and family book disagree on the silver position"
        events.append(ev)
    if write and events:
        nf.append_events(FEED, events)
    return events


def main() -> int:
    ap = argparse.ArgumentParser(description="Reconcile the silver desk vs the equity family book (SILVERBEES).")
    ap.add_argument("--tol", type=float, default=0.05, help="divergence tolerance (fraction, default 0.05)")
    ap.add_argument("--write", action="store_true", help="append a feed event on drift")
    ap.add_argument("--dump", action="store_true", help="print decrypted structure keys to tune extractors")
    ap.add_argument("--pw", default=None, help="shared password fallback for both sides")
    args = ap.parse_args()
    try:                                   # Windows consoles default to cp1252 — don't crash on emoji
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    status, msg, detail = reconcile(args.tol, args.pw, args.dump)
    icon = {"match": "[OK]", "drift": "[DRIFT]", "indeterminate": "[?]", "error": "[ERR]"}.get(status, "[?]")
    print(f"{icon} reconcile [{status}]: {msg}")
    if detail.get("equity") or detail.get("silver"):
        eqp, svp = detail.get("equity", {}), detail.get("silver", {})
        print(f"   equity SILVERBEES → qty={eqp.get('qty')} value={eqp.get('value')} ({eqp.get('tickers')})")
        print(f"   silver desk      → qty={svp.get('qty')} value={svp.get('value')} via={svp.get('via')}")
        print(f"   live SILVERBEES price={detail.get('live_price')}  basis={detail.get('basis')}  diff={detail.get('rel_diff')}")
    if args.write and status == "drift":
        run(args.tol, write=True, shared_pw=args.pw)
        print("   → recorded a 🔴 silver-reconcile event on the feed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
