#!/usr/bin/env python3
"""ci_silver_refresh.py — rebuild the SILVER family book in the cloud (D5-SILVER).

WHY THIS EXISTS ([[OFFLINE_RESILIENCE]] §2 D5). silver_dashboard_emit.py computes the family
book (per-account tranches, totals, ladders, strategy) from the private
`_inputs/silver_holdings.yaml`, which only ever existed on the operator's laptop. The cloud
silver cron is a PRICE OVERLAY that never touches substance, so the book froze whenever the
laptop was off — fresh prices over a dead book, the exact V-37 failure class. This job is the
other half of silver_book_publish.py:

    silver/data/book_enc.json --(SILVER_BOOK_KEY)--> <tmp>/silver_holdings.yaml
    Yahoo SILVERBEES settled close ----------------> <tmp>/daily_prices.csv
    DESK_UNLOCK_PW -------------------------------> <tmp>/.dashboard_pw
    silver_dashboard_emit.emit() (REPOINTED) ------> <tmp>/aggregate.json
    leak gate (family tokens over the PUBLIC part) -> ABORT on any hit
    atomic copy ----------------------------------> silver/data/silver_dashboard_aggregate.json

PORTING CONVENTION (ci_fib_refresh.py's). silver_dashboard_emit.py is the SAME layout-flexible
module the vault runs — its module globals (INPUT_YAML / PRICE_CSV / PW_FILE / OUTPUT_JSON) are
REPOINTED at runtime into a tempfile workspace OUTSIDE the repo tree. Nothing about the book
math is re-implemented, so the cloud book cannot drift from the laptop's.

SAFETY. The decrypted YAML carries family names AND rupee amounts and this repo is PUBLIC:
  * all plaintext (YAML, pw, intermediate aggregate) lives ONLY in a TemporaryDirectory outside
    the checkout — no git path can ever stage it;
  * the emit runs with the desk password, so the sensitive block is FRESH-ENCRYPTED into
    `sensitive_enc` (same PBKDF2->AES-GCM envelope the browser unlock reads);
  * before anything is written into the repo tree, `assert_public_clean` scans the aggregate's
    PUBLIC portion for every family token and ABORTS on any hit — an aggregate that fails the
    leak gate must never reach disk;
  * no sensitive_enc in the emitted aggregate (missing pw would mean a PLAINTEXT family book on
    a public repo) is likewise an ABORT.

HONESTY. The emitted headline is CLOSE-based: the price CSV is fed SETTLED daily bars only
(today's still-forming bar is dropped before 15:45 IST, ci_fib_refresh.drop_forming_bar logic).
The 20-min price overlay then keeps the live tickers moving on top, exactly as before.
"""
from __future__ import annotations

import base64
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent              # silver/generators
SILVER = HERE.parent                                 # silver/
AGG = SILVER / "data" / "silver_dashboard_aggregate.json"
BOOK_ENC = SILVER / "data" / "book_enc.json"
sys.path.insert(0, str(HERE))

IST = timezone(timedelta(hours=5, minutes=30))
SETTLED_AFTER_IST = (15, 45)

# Personal/account tokens that must NEVER appear in the PUBLIC portion of the aggregate.
# Mirrors silver_dashboard_emit's family-token set minus the intended-public 'Sparcho' brand
# and the generic 'HUF' (which can never leak alone once the name beside it is gone).
# Scanned case-insensitively as SUBSTRINGS (privacy_scrub lesson: `_` is a \w char, so a
# word-boundary regex misses `rajiv_account`).
LEAK_TOKENS = ("Sparsh", "Rajiv", "Shalini", "Shalu", "Yash")

# Local fallbacks are a LAPTOP-dry-run convenience only; in Actions the env always wins.
VAULT_INPUTS = Path(r"C:/Users/user/Desktop/CLAUDE PLAY/TRADER/00_SYSTEM/GENERATORS/_inputs")


class SilverRefreshAbort(RuntimeError):
    """Raised instead of writing anything this job cannot fully justify."""


def assert_public_clean(agg: dict) -> None:
    """ABORT if any family token survives in the aggregate OUTSIDE sensitive_enc.

    The ciphertext is excluded: base64 can contain any substring by chance, and the whole point
    of the envelope is that its contents are unreadable. Everything else — keys, values, notes,
    warnings — is scanned lowercased as substrings.
    """
    public = {k: v for k, v in (agg or {}).items() if k != "sensitive_enc"}
    blob = json.dumps(public, ensure_ascii=False, default=str).lower()
    hits = sorted({t for t in LEAK_TOKENS if t.lower() in blob})
    if hits:
        raise SilverRefreshAbort(
            "PRIVACY LEAK: family token(s) %s present in the PUBLIC aggregate — refusing to "
            "write. The emit's privacy lock did not strip everything." % ", ".join(hits))


def _secret(env_name: str, *files) -> str:
    val = os.environ.get(env_name)
    if val and val.strip():
        return val.strip()
    for cand in files:
        if cand.exists():
            return cand.read_text(encoding="utf-8").strip()
    raise SilverRefreshAbort(f"no ${env_name} in env and no local fallback file — cannot proceed")


def load_book_key() -> str:
    """The MACHINE key for the book bundle (Actions secret SILVER_BOOK_KEY)."""
    return _secret("SILVER_BOOK_KEY", HERE / "_inputs" / ".silver_book_key",
                   VAULT_INPUTS / ".silver_book_key")


def load_desk_pw() -> str:
    """The operator password that seals `sensitive_enc` (Actions secret DESK_UNLOCK_PW)."""
    return _secret("DESK_UNLOCK_PW", HERE / "_inputs" / ".dashboard_pw",
                   VAULT_INPUTS / ".dashboard_pw")


def decrypt_book(blob: dict, key_b64: str) -> dict:
    """AES-256-GCM decrypt of the silver_book_publish bundle ({v, alg, iv, ct}, machine key).

    Same blob format levels_bank_publish writes; inlined because silver/generators does not
    carry that module (the format is 4 fields and one AESGCM call — a drifted copy would fail
    loudly on decrypt, never silently).
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = base64.b64decode(key_b64)
    if len(raw) != 32:
        raise SilverRefreshAbort("SILVER_BOOK_KEY must decode to exactly 32 bytes")
    iv = base64.b64decode(blob["iv"])
    ct = base64.b64decode(blob["ct"])
    return json.loads(AESGCM(raw).decrypt(iv, ct, None).decode("utf-8"))


def drop_forming_bar(rows, now_ist=None):
    """rows = [(YYYY-MM-DD, o, h, l, c, vol)] ascending — drop today's bar while still forming."""
    now = now_ist or datetime.now(IST)
    if (now.hour, now.minute) >= SETTLED_AFTER_IST:
        return list(rows)
    today = now.strftime("%Y-%m-%d")
    return [r for r in rows if r[0] != today]


def fetch_settled_silverbees(range_="15d", now_ist=None) -> list:
    """SILVERBEES daily OHLC bars from Yahoo (settled only), ascending."""
    from yahoo_common import fetch_chart
    r = fetch_chart("SILVERBEES.NS", interval="1d", range_=range_)
    res = r["chart"]["result"][0]
    ts = res["timestamp"]
    q = res["indicators"]["quote"][0]
    rows = []
    for i, t in enumerate(ts):
        d = datetime.fromtimestamp(t, IST).strftime("%Y-%m-%d")
        c = q["close"][i]
        if c is None:
            continue
        rows.append((d, q["open"][i], q["high"][i], q["low"][i], c, q["volume"][i] or 0))
    rows.sort(key=lambda x: x[0])
    return drop_forming_bar(rows, now_ist)


def write_price_csv(rows, path: Path) -> None:
    """Materialise the bars into the daily_prices.csv schema _silverbees_from_daily reads.

    Only the columns that reader consumes are populated; prev_close/day_chg come from the
    prior settled bar so the headline card computes the same numbers the laptop would.
    """
    import csv as _csv
    if not rows:
        raise SilverRefreshAbort("no settled SILVERBEES bars fetched — cannot emit a dated book")
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["date", "ticker", "yahoo_symbol", "prev_close", "open", "high", "low", "close",
            "volume", "gap_pct", "day_chg_pct", "open_pull_at", "close_pull_at", "status",
            "px_0930", "px_0930_at"]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = _csv.writer(f)
        w.writerow(cols)
        prev = None
        for d, o, h, l, c, v in rows:
            chg = round((c - prev) / prev * 100.0, 3) if prev else ""
            w.writerow([d, "SILVERBEES", "SILVERBEES.NS", prev or "", o or "", h or "", l or "",
                        c, int(v), "", chg, "", f"{d}T16:00:00+0530", "ok", "", ""])
            prev = c


def _sanity(agg: dict) -> None:
    """Refuse to publish a degenerate or unlocked book over a good one."""
    if not agg.get("sensitive_enc"):
        raise SilverRefreshAbort(
            "emitted aggregate has NO sensitive_enc — a cloud emit without the lock would ship "
            "the family book in plaintext on a public repo. Check DESK_UNLOCK_PW.")
    # v2 schema: the headline price rides at current_price.primary_date
    price_date = (agg.get("current_price") or {}).get("primary_date")
    if not price_date:
        raise SilverRefreshAbort("emitted aggregate carries no price date — an undateable block "
                                 "is a broken one (invariant #4)")


def main(argv=None) -> int:
    import argparse
    import tempfile
    ap = argparse.ArgumentParser(description="Cloud silver family-book refresh")
    ap.add_argument("--dry-run", action="store_true",
                    help="build + validate, write nothing into the repo tree")
    ap.add_argument("--agg", default=None)
    a = ap.parse_args(argv)

    if not BOOK_ENC.exists():
        print(f"[silver-cloud] ABORT: {BOOK_ENC} missing — run silver_book_publish.py on the "
              f"laptop first (the book has never been shipped)", file=sys.stderr)
        return 2
    try:
        bundle = decrypt_book(json.loads(BOOK_ENC.read_text(encoding="utf-8")), load_book_key())
    except SilverRefreshAbort as e:
        print(f"[silver-cloud] ABORT: {e}", file=sys.stderr)
        return 2
    except Exception as e:                                       # noqa: BLE001
        print(f"[silver-cloud] ABORT: book did not decrypt ({type(e).__name__}: {e})",
              file=sys.stderr)
        return 2
    print("[silver-cloud] book: as_of %s · sha %s… · shipped %s"
          % (bundle.get("book_as_of"), str(bundle.get("yaml_sha256"))[:12],
             bundle.get("generated_at_utc")))

    with tempfile.TemporaryDirectory(prefix="silvercloud-") as td:
        work = Path(td)                                  # OUTSIDE the repo tree by construction
        yaml_p = work / "silver_holdings.yaml"
        yaml_p.write_text(bundle["yaml_text"], encoding="utf-8")
        pw_p = work / ".dashboard_pw"
        try:
            _pw = load_desk_pw()
            # non-reversible fingerprint so a seal/unlock mismatch can be diagnosed from the
            # public run log without ever exposing the password (8 hex chars of sha256)
            import hashlib as _hl
            print("[silver-cloud] pw fp %s (len %d)" % (_hl.sha256(_pw.encode()).hexdigest()[:8], len(_pw)))
            pw_p.write_text(_pw, encoding="utf-8")
        except SilverRefreshAbort as e:
            print(f"[silver-cloud] ABORT: {e}", file=sys.stderr)
            return 2
        price_p = work / "daily_prices.csv"
        out_p = work / "silver_dashboard_aggregate.json"
        try:
            write_price_csv(fetch_settled_silverbees(), price_p)
        except SilverRefreshAbort as e:
            print(f"[silver-cloud] ABORT: {e}", file=sys.stderr)
            return 3

        import silver_dashboard_emit as SE
        SE.INPUT_YAML, SE.PRICE_CSV, SE.PW_FILE, SE.OUTPUT_JSON = yaml_p, price_p, pw_p, out_p
        try:
            SE.emit()
            agg = json.loads(out_p.read_text(encoding="utf-8"))
            _sanity(agg)
            assert_public_clean(agg)
        except SilverRefreshAbort as e:
            print(f"[silver-cloud] ABORT: {e}", file=sys.stderr)
            return 4
        print("[silver-cloud] book emitted LOCKED (%db ct) · price %s"
              % (len((agg.get("sensitive_enc") or {}).get("ct", "")),
                 (agg.get("current_price") or {}).get("primary_date")))
        if a.dry_run:
            print("[silver-cloud] --dry-run: repo aggregate NOT written")
            return 0

        agg_path = Path(a.agg) if a.agg else AGG
        from atomic_io import atomic_write_json
        atomic_write_json(agg_path, agg, indent=2)
        print(f"[silver-cloud] {agg_path} updated — family book refreshed laptop-off")
    return 0


if __name__ == "__main__":
    sys.exit(main())
