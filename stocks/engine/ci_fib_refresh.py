#!/usr/bin/env python3
"""ci_fib_refresh.py — rebuild the FIB-CONFLUENCE radar in the cloud (F260721-LEVELSCLOUD).

WHY THIS EXISTS. box_engine.py + fib_confluence_feed.py read the operator's banked fib reads from
`00_SYSTEM/EDGE/LEVELS/`, which only ever existed on the laptop. So the one surface the operator
explicitly asked to keep alive unattended was the one surface that froze the moment the laptop went
off. levels_bank_publish.py now ships that bank to the repo as AES-256-GCM ciphertext; this job is
the other half — decrypt it in Actions, rebuild the radar against cloud price data, and put the
result back inside the operator's encrypted block.

    stocks/data/levels_enc.json --(LEVELS_BANK_KEY)--> <tmp>/LEVELS/*.json
    Yahoo daily closes ------------------------------> historical_ohlc.csv + daily_prices.csv
    box_engine.build_all() --------------------------> box_observations.json
    fib_confluence_feed.build() ---------------------> the radar payload
    inject_fib(aggregate, payload, DESK_UNLOCK_PW) --> sensitive_enc + a public provenance stub
    fib_confluence_source.build_radar(payload) ------> agg["fib_radar"]  (fires / watch / board)

F260727-RADARSUBSTRATE. Until 2026-07-30 this job stopped at the sealed payload, so `fib_radar` --
the fires, the watch list and the 41-name board the desk actually reads -- had exactly ONE producer:
equity_dashboard_emit, on the laptop. EQUITY_BLOCKS declared the block `cloud`, reasoning that it
inherits the substrate of the data it derives from; substrate is not inherited, it is who runs the
writer (V-37). The cost was concrete: on 30-Jul a bad laptop morning pull replaced a good post-close
build, 37 of 92 names carrying the 28-Jul close under a declared 30-Jul, and the board read 0 fires
where the true prices gave 2. Had the cloud owned it, the 29-Jul build would have stood.

PORTING CONVENTION (ci_screener_emit.py's). box_engine.py, fib_confluence_feed.py and
levels_bank_publish.py are BYTE-IDENTICAL copies of the vault modules — guarded by the vault's
test_copy_parity.py — and their VAULT-rooted path constants are REPOINTED here at runtime. Nothing
about the scoring is re-implemented, so the cloud radar cannot drift from the laptop's.

HONESTY. The feed is CLOSE-BASED and stamps `price_as_of` / `basis: last close ... NOT intraday
live`. This job therefore feeds it SETTLED daily bars only: Yahoo's 1d series carries today's
still-forming bar while NSE is open, and shipping that under today's date would make the feed's own
stamp false. See drop_forming_bar().

SAFETY. The output rides inside `sensitive_enc` — the same envelope the vault emit writes and the
dashboard's WebCrypto unlock reads. That block also holds the family book, so this job DECRYPTS it,
swaps one key, and re-seals it. If it cannot read the book it ABORTS: a public repo is the wrong
place to discover you have overwritten the ledger with a blob you composed yourself.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent            # stocks/engine
REPO_STOCKS = HERE.parent                          # stocks
AGG = REPO_STOCKS / "data" / "equity_dashboard_aggregate.json"
LEVELS_ENC = REPO_STOCKS / "data" / "levels_enc.json"
sys.path.insert(0, str(HERE))

IST = timezone(timedelta(hours=5, minutes=30))
# NSE closes 15:30 IST; the daily bar is settled a few minutes later. Before this, any bar dated
# today is still FORMING.
SETTLED_AFTER_IST = (15, 45)


class FibRefreshAbort(RuntimeError):
    """Raised instead of writing anything this job cannot fully justify."""


def materialise_bank(bundle: dict, dest: Path) -> int:
    """Write the decrypted bundle back out as the per-ticker JSON layout box_engine globs.

    Entries recorded by levels_bank_publish as {"_error": ...} (a bank file it could not parse) are
    SKIPPED and reported, not written: box_engine would read such a stub as a verified-less read and
    manufacture a phantom P0. The error belongs in the run log.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    written = 0
    for tkr, bank in sorted((bundle.get("banks") or {}).items()):
        if not isinstance(bank, dict) or "_error" in bank:
            print(f"[fib-cloud] SKIP {tkr}: {(bank or {}).get('_error', 'not an object')}",
                  file=sys.stderr)
            continue
        (dest / f"{tkr}.json").write_text(json.dumps(bank, ensure_ascii=False), encoding="utf-8")
        written += 1
    return written


# ── 2. honest, close-based price layer ─────────────────────────────────────
def drop_forming_bar(rows, now_ist=None):
    """Drop today's bar while it is still FORMING (rows = [(YYYY-MM-DD, close), ...] ascending).

    Yahoo's 1d series includes the current session's partial bar during market hours. The feed
    stamps `basis: last close (<date>) - NOT intraday live`; feeding it a forming bar would make
    that stamp false, which is the one thing this radar must never do. After 15:45 IST the bar is
    settled and kept — which is the case on the 11:00 UTC (16:30 IST) post-close cron.
    """
    now = now_ist or datetime.now(IST)
    if (now.hour, now.minute) >= SETTLED_AFTER_IST:
        return list(rows)
    today = now.strftime("%Y-%m-%d")
    return [r for r in rows if r[0] != today]


def write_price_caches(series: dict, hist_csv: Path, daily_csv: Path) -> int:
    """Materialise {ticker: [(date, close, high, low)]} into the two CSVs the ported engines read.

    F260907-NOFLOOR. This used to write ticker,date,close and nothing else, "same narrow schema on
    purpose". The schema was narrower than its READER. `atr_pct_from_csv` measures one normal day
    for a name as the median (high - low) / close over its last 60 sessions, and reads it from
    exactly this file; with no high and no low column it returned {} for every ticker, all 98 names
    arrived with `atr_pct: None`, and the stop noise floor, guarded by a bare `if atr_pct`, switched
    itself off in silence. ALLCARGO went to the top of the live desk at a 10.14 R:R on a stop a
    quarter of one average day wide. Measured: vault bank 98/98 carry atr_pct, published board 0/98.

    A missing column reads exactly like a missing file to that reader, which is why a present,
    fresh, correctly-dated cache still produced nothing ([[absence-of-evidence-is-not-health]]).
    Both sides had agreed on the FILE and never on the FIELDS ([[a-mirrored-file-is-a-data-contract]]).

    Nothing extra is fetched for this: Yahoo returns high and low in the same `quote` block as
    close, and the old fetch simply dropped them. Readers are unaffected -- box_engine and
    ohlc_from_csv both use csv.DictReader, so they take the columns they name and ignore the rest.
    The daily cache stays last-settled-bar-only per ticker: the feed reads its dates to stamp
    `price_as_of`, so anything else in there would mis-date the whole payload.
    """
    import csv
    hist_csv, daily_csv = Path(hist_csv), Path(daily_csv)
    hist_csv.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with hist_csv.open("w", encoding="utf-8", newline="") as fh, \
            daily_csv.open("w", encoding="utf-8", newline="") as fd:
        wh, wd = csv.writer(fh), csv.writer(fd)
        wh.writerow(["ticker", "date", "close", "high", "low"])
        wd.writerow(["ticker", "date", "close"])
        for tkr, rows in sorted(series.items()):
            rows = sorted(rows, key=lambda r: r[0])
            if not rows:
                continue
            for row in rows:
                # Fail LOUD on a close-only row. Tolerating the old 2-tuple here is how the defect
                # comes back: the writer keeps accepting it, the file keeps being written, and the
                # floor goes quiet again with nothing anywhere to show for it.
                if len(row) < 4:
                    raise ValueError(
                        "%s: price rows must carry (date, close, high, low) -- a close-only row "
                        "cannot support the stop noise floor, and writing it silently is the "
                        "F260907-NOFLOOR defect. Got: %r" % (tkr, row))
                d, c, hi, lo = row[0], row[1], row[2], row[3]
                wh.writerow([tkr, d, c, hi, lo])
            wd.writerow([tkr, rows[-1][0], rows[-1][1]])
            n += 1
    return n


# ── 3. the operator's envelope — reused, never re-invented ─────────────────
# equity_dashboard_emit._apply_privacy writes {v, iter, salt, iv, ct}: PBKDF2-HMAC-SHA256(iter) over
# the operator password -> a 32-byte AES-256-GCM key. The dashboard's WebCrypto unlock reads the
# iteration count from the payload's own `iter` field, so BOTH sides stay in step.
# This job deliberately does NOT choose those parameters: it carries the existing blob's `v` and
# `iter` forward. The crypto policy belongs to the emit that owns the block (the vault is currently
# migrating 200k -> 600k under F260721-FIBLOCK); a price-refresh job silently re-keying the family
# book on its own schedule would be a change nobody asked for and nobody could see.
FIB_PUBLIC_KEYS = ("price_as_of", "generated_at_utc", "basis", "method", "note",
                   "n_names", "n_points", "n_at")
_DEFAULT_ITERS = 600000


def fib_public_stub(fc):
    """Level-free, ticker-free provenance stub for the locked fib block.

    Mirrors equity_dashboard_emit._fib_public_stub (parity is asserted by the vault test suite). The
    analysis moves into the ciphertext, but the block must stay FALSIFIABLE while locked: the card
    can still say "scored on the <date> close" and staleness_contract can still prove the radar
    stale. A bare {} would re-create F260721-BLOCKROT.
    """
    if not isinstance(fc, dict):
        return fc
    stub = {k: fc[k] for k in FIB_PUBLIC_KEYS if k in fc}
    stub["locked"] = True
    return stub


def _derive(pw: str, salt: bytes, iters: int) -> bytes:
    import hashlib
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, iters, 32)


def decrypt_sensitive(blob: dict, pw: str) -> dict:
    import base64
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = _derive(pw, base64.b64decode(blob["salt"]), int(blob.get("iter") or _DEFAULT_ITERS))
    pt = AESGCM(key).decrypt(base64.b64decode(blob["iv"]), base64.b64decode(blob["ct"]), None)
    return json.loads(pt.decode("utf-8"))


def encrypt_sensitive(sensitive: dict, pw: str, v: int = 2, iters: int = _DEFAULT_ITERS) -> dict:
    import base64
    import os
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, iv = os.urandom(16), os.urandom(12)
    ct = AESGCM(_derive(pw, salt, iters)).encrypt(
        iv, json.dumps(sensitive, ensure_ascii=False, default=str).encode("utf-8"), None)
    return {"v": v, "iter": iters,
            "salt": base64.b64encode(salt).decode(),
            "iv": base64.b64encode(iv).decode(),
            "ct": base64.b64encode(ct).decode()}


def inject_fib(agg: dict, fib_payload: dict, pw: str) -> dict:
    """Put the freshly-built radar inside the aggregate's `sensitive_enc`, leaving a public stub.

    Read-modify-write, and it ABORTS if the read fails. `sensitive_enc` also carries the family
    book (totals, ledger, held positions, trade lab); composing a replacement blob from scratch
    because we could not open the old one would silently delete all of it, on a public repo, with
    no local copy to restore from.
    """
    blob = (agg or {}).get("sensitive_enc")
    if not blob:
        raise FibRefreshAbort(
            "aggregate has no sensitive_enc — refusing to create one: this job owns ONE key inside "
            "that block, not the block itself (the vault emit builds it).")
    if not pw:
        raise FibRefreshAbort("no DESK_UNLOCK_PW — cannot open the book, so cannot re-seal it")
    try:
        sensitive = decrypt_sensitive(blob, pw)
    except Exception as e:
        raise FibRefreshAbort(
            "sensitive_enc did not decrypt (%s: %s) — wrong DESK_UNLOCK_PW or a re-keyed block. "
            "ABORTING with the aggregate untouched." % (type(e).__name__, e))
    sensitive["fib_confluences"] = fib_payload
    agg["sensitive_enc"] = encrypt_sensitive(
        sensitive, pw, v=int(blob.get("v") or 2), iters=int(blob.get("iter") or _DEFAULT_ITERS))
    agg["fib_confluences"] = fib_public_stub(fib_payload)
    return agg


# ── 4. orchestration ───────────────────────────────────────────────────────
# Local fallbacks are a LAPTOP-dry-run convenience only. In Actions the env always wins, so a stale
# file on someone's disk can never quietly sign the cloud's output with the wrong key.
VAULT_INPUTS = Path(r"C:/Users/user/Desktop/CLAUDE PLAY/TRADER/00_SYSTEM/GENERATORS/_inputs")


def _secret(env_name: str, *files) -> str:
    val = os.environ.get(env_name)
    if val and val.strip():
        return val.strip()
    for cand in files:
        if cand.exists():
            return cand.read_text(encoding="utf-8").strip()
    raise FibRefreshAbort(f"no ${env_name} in env and no local fallback file — cannot proceed")


def load_levels_key() -> str:
    """The MACHINE key for the levels bundle (Actions secret LEVELS_BANK_KEY)."""
    return _secret("LEVELS_BANK_KEY", HERE / "_inputs" / ".levels_bank_key",
                   VAULT_INPUTS / ".levels_bank_key")


def load_desk_pw() -> str:
    """The operator password that seals `sensitive_enc` (Actions secret DESK_UNLOCK_PW)."""
    return _secret("DESK_UNLOCK_PW", HERE / "_inputs" / ".dashboard_pw",
                   VAULT_INPUTS / ".dashboard_pw")


def build_payload(bank_dir: Path, hist_csv: Path, daily_csv: Path, obs_json: Path) -> dict:
    """Run the two ported engines against the materialised bank + price caches.

    ci_screener_emit's convention: import the byte-identical modules and REPOINT their path
    constants (which resolve to nonsense inside the repo) at runtime. No forked scoring — whatever
    the laptop computes from a given bank, the cloud computes.
    """
    import box_engine as BE
    import fib_confluence_feed as FF
    BE.BANK_DIR, BE.HIST_CSV, BE.DAILY_CSV, BE.OUT_JSON = bank_dir, hist_csv, daily_csv, obs_json
    obs = BE.build_all(write=True)
    FF.OBS, FF.BANK_DIR, FF.HIST_CSV, FF.DAILY_CSV = obs_json, bank_dir, hist_csv, daily_csv
    return FF.build(obs=obs, write=False)


def _sanity(payload: dict) -> None:
    """Refuse to publish a degenerate radar over a good one.

    A silent empty block is the failure mode this feature exists to end: the card would keep
    rendering, dated today, saying nothing — indistinguishable from a healthy quiet day. Leaving
    yesterday's honest block in place lets the staleness contract do its job.
    """
    if not payload.get("n_names"):
        raise FibRefreshAbort("rebuilt radar has 0 names — refusing to publish over the prior block")
    if not payload.get("price_as_of"):
        raise FibRefreshAbort("rebuilt radar has no price_as_of — an undateable block is a broken one")


def build_radar_block(payload: dict) -> dict:
    """The public Radar block (fires / watch / board), via the ONE shared projection.

    `fib_confluence_source` is a byte-identical copy of the vault module (test_copy_parity), so the
    gate — at-confluence + STRONG + >=3 verified sources + setup/trend + R:R >= 1.5 — is not
    re-implemented here. Its VAULT-rooted `BANK` constant is never reached: we pass the in-memory
    payload we just built, which is the same object the laptop reads from EDGE/fib_confluences.json.
    """
    import fib_confluence_source as FCS
    return FCS.build_radar(payload)


def _sanity_radar(radar: dict, payload: dict) -> None:
    """Refuse to publish a radar that cannot be judged, on the same principle as _sanity.

    A board with rows but no `price_as_of` is the F260721-FIBPROV bug (an undatable block cannot be
    proven stale). A basis that disagrees with the payload we just sealed would mean the two halves
    of this job scored different closes — publishing that pair would put a fresh-looking board next
    to a differently-dated analysis, which is the 30-Jul defect in miniature.
    """
    if not radar.get("board"):
        raise FibRefreshAbort("rebuilt radar has an EMPTY board — refusing to publish over the "
                              "prior block (a dated, empty board reads as a healthy quiet day)")
    if not radar.get("price_as_of"):
        raise FibRefreshAbort("rebuilt radar has no price_as_of — an undateable block is broken")
    if radar["price_as_of"] != payload.get("price_as_of"):
        raise FibRefreshAbort(
            "radar price_as_of %s != sealed payload %s — the two halves of this job scored "
            "different closes" % (radar["price_as_of"], payload.get("price_as_of")))


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0 Safari/537.36")
FETCH_TIMEOUT = 20

# The vault's phantom-bar predicate, carried over verbatim from cache_integrity (F260812).
# Copied rather than imported: cache_integrity is an 821-line module rooted in vault-only data
# (corporate_actions.csv, venue_exceptions.csv, the levels dir) and none of that exists in Actions.
# What IS needed here is one predicate, and it is reproduced with its reasoning attached so the
# next reader can see it is a decision and not a stub.
VOLUMELESS_FRAC = 0.10        # zero volume only carries information where it is EXCEPTIONAL for
VOLUMELESS_MIN_SAMPLE = 30    # the instrument; FX/yields/index levels are 100% zero-volume.


def _is_phantom(row, prior):
    """A carried-forward write: zero volume AND a close identical to the one before it.

    The conjunction is the whole point. A genuinely illiquid name that did not print on a real
    session has volume 0 and a DIFFERENT close; a name that closed unchanged on real volume traded.
    Neither is a phantom, and deleting on either signal alone takes real bars with it.
    """
    if prior is None:
        return False
    return row[4] == 0 and abs(row[1] - prior[1]) < 1e-9


def drop_phantom_bars(rows):
    """Refuse the bars Yahoo invents for days NSE never traded.

    F260812-PHANTOMBARS, ported. Yahoo emits a bar for Indian market holidays -- zero volume, close
    repeating the previous session -- and does it for essentially the whole universe at once
    (verified 2026-08-14 on 2026-05-01, 2026-05-28 and 2026-06-26). The vault has guarded its own
    write boundary against this since August. stocks/engine/fetch_historical.py is a stale fork
    that never received the guard, so the fetch running while the operator is away was the one
    without it. A phantom bar has high == low == close: a zero-range day, dragging down the very
    median that IS the stop noise floor this file now exists to feed.

    Instruments whose volume field carries no information are exempt -- for them "zero volume and
    unchanged" is an ordinary observation rather than a signature. The exemption is derived from the
    fetched series itself, so there is no list to maintain and no list to go stale. Below the sample
    floor the fraction is noise (two zero-volume rows out of three reads as 67%), so the guard
    applies: a wrongly-dropped bar is refetchable, a wrongly-kept phantom silently corrupts every
    window that spans it.
    """
    if not rows:
        return rows
    zero_frac = sum(1 for r in rows if r[4] == 0) / len(rows)
    if len(rows) >= VOLUMELESS_MIN_SAMPLE and zero_frac >= VOLUMELESS_FRAC:
        return list(rows)                      # volume is not a signal for this instrument
    kept = []
    for row in rows:
        if _is_phantom(row, kept[-1] if kept else None):
            continue
        kept.append(row)
    return kept


def chart_ohlc(yahoo_sym: str, range_: str = "1y"):
    """Daily bars for one Yahoo symbol as [(date, close, high, low, volume)], ascending.

    F260907-NOFLOOR. The predecessor of this function read the same response and kept only the
    close. Yahoo hands back high and low in the same quote block; discarding them is what left the
    cloud unable to measure a single name's own daily range, which switched off every stop's noise
    floor without a word. This is the same one request, parsed less wastefully.

    A bar missing ANY of close/high/low is skipped rather than back-filled from its neighbours: a
    fabricated range is worse than a shorter series, because the median it feeds cannot tell the
    two apart.
    """
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=1d"
           % (yahoo_sym, range_))
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
        data = json.loads(r.read().decode("utf-8"))
    chart = data.get("chart") or {}
    if chart.get("error"):
        raise ValueError(chart["error"])
    result = chart.get("result") or []
    if not result:
        return []
    res = result[0]
    stamps = res.get("timestamp") or []
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    closes, highs = quote.get("close") or [], quote.get("high") or []
    lows, vols = quote.get("low") or [], quote.get("volume") or []
    out = []
    for i, ts in enumerate(stamps):
        c = closes[i] if i < len(closes) else None
        h = highs[i] if i < len(highs) else None
        lo = lows[i] if i < len(lows) else None
        if c is None or h is None or lo is None:
            continue
        v = vols[i] if i < len(vols) and vols[i] is not None else 0
        # fromtimestamp(..., utc) not utcfromtimestamp(): the latter is deprecated and
        # scheduled for removal, and this job has to keep running with nobody watching.
        # Same value either way -- an NSE daily bar is stamped at 03:45 UTC, so the UTC
        # date and the IST date are the same day.
        out.append((datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d"),
                    float(c), float(h), float(lo), int(v)))
    return sorted(out, key=lambda x: x[0])


def fetch_ohlc_series(tickers, range_="1y", now_ist=None, sleep_s=0.6) -> dict:
    """Daily OHLC per ticker from Yahoo -- SETTLED bars only, phantom holidays removed.

    Symbol resolution still goes through fetch_historical.yahoo_symbols, so the cloud keeps
    inheriting the repo's existing fallbacks (BLUESTAR -> BLUESTARCO.NS, PARAS -> PARASDEFEN.NS,
    ...) rather than assuming the default '.NS' rule and silently losing every name that does not
    follow it. Only the PARSE changed: the range is kept, and the guard runs at this boundary --
    the same place the vault runs it -- so nothing downstream has to know a phantom existed.
    """
    import time
    import fetch_historical as FH
    out, failed = {}, []
    for t in tickers:
        rows, status = [], "404 (all fallbacks exhausted)"
        for sym in FH.yahoo_symbols(t):
            try:
                rows = chart_ohlc(sym, range_)
            except urllib.error.HTTPError as e:
                status = "http-%s" % e.code
                continue
            except Exception as e:                           # noqa: BLE001 -- one bad name
                status = "%s: %s" % (type(e).__name__, e)
                continue
            if rows:
                status = "ok"
                break
        if rows:
            out[t] = drop_forming_bar(drop_phantom_bars(rows), now_ist)
        else:
            failed.append((t, status))
        if sleep_s:
            time.sleep(sleep_s)
    if failed:
        print("[fib-cloud] no history for %d name(s): %s" % (len(failed), failed), file=sys.stderr)
    return out


def main(argv=None) -> int:
    import argparse
    import tempfile
    ap = argparse.ArgumentParser(description="Cloud fib-confluence radar refresh")
    ap.add_argument("--dry-run", action="store_true",
                    help="build + report, write nothing to the aggregate")
    # F260907-WINDOW. Was 1y, and that made the cloud radar BLIND rather than merely
    # shallower. The vault scores levels against historical_ohlc.csv, which holds 1,240
    # daily bars back to 2021; the cloud saw 247. Re-running this build over the same bank
    # with only the window changed: ALLCARGO support 12.45 at 1y, 9.32 at 5y -- and 9.32 is
    # the one its own banked study calls THE SPINE. The cloud was picking the best level it
    # could see inside twelve months and firing on it, which put a 10x reward:risk at the
    # top of the live board. Matched to the vault so both desks derive one bank's levels
    # against one history; the window is part of the level, not a fetch detail.
    ap.add_argument("--range", default="5y",
                    help="history window to fetch (default 5y, matching the vault cache)")
    ap.add_argument("--agg", default=None)
    a = ap.parse_args(argv)

    import levels_bank_publish as LB
    if not LEVELS_ENC.exists():
        print(f"[fib-cloud] ABORT: {LEVELS_ENC} missing — run levels_bank_publish.py on the "
              f"laptop first (the bank has never been shipped)", file=sys.stderr)
        return 2
    try:
        bundle = LB.decrypt_bundle(json.loads(LEVELS_ENC.read_text(encoding="utf-8")),
                                   load_levels_key())
    except FibRefreshAbort as e:
        print(f"[fib-cloud] ABORT: {e}", file=sys.stderr)
        return 2
    except Exception as e:                                       # noqa: BLE001
        print(f"[fib-cloud] ABORT: levels bundle did not decrypt ({type(e).__name__}: {e})",
              file=sys.stderr)
        return 2
    print("[fib-cloud] bank: %s ticker(s) · newest study %s · bundled %s"
          % (bundle.get("n_tickers"), bundle.get("newest_studied_date"),
             bundle.get("generated_at_utc")))

    with tempfile.TemporaryDirectory(prefix="fibcloud-") as td:
        work = Path(td)
        bank = work / "LEVELS"
        n = materialise_bank(bundle, bank)
        hist, daily = work / "historical_ohlc.csv", work / "daily_prices.csv"
        obs_json = work / "box_observations.json"
        series = fetch_ohlc_series(sorted(p.stem for p in bank.glob("*.json")), a.range)
        print("[fib-cloud] materialised %d bank(s); fetched history for %d" % (n, len(series)))
        write_price_caches(series, hist, daily)
        try:
            payload = build_payload(bank, hist, daily, obs_json)
            _sanity(payload)
        except FibRefreshAbort as e:
            print(f"[fib-cloud] ABORT: {e}", file=sys.stderr)
            return 3
        print("[fib-cloud] radar: %d names · %d points · %d AT · basis=%s"
              % (payload["n_names"], payload["n_points"], payload["n_at"], payload["basis"]))
        for nm in payload["names"][:5]:
            print("   %-12s %9.2f  score %3d  %-20s %s"
                  % (nm["ticker"], nm["current_px"], nm["score"], nm["setup"], nm["grade"]))
        if a.dry_run:
            print("[fib-cloud] --dry-run: aggregate NOT written")
            return 0

        agg_path = Path(a.agg) if a.agg else AGG
        if not agg_path.exists():
            print(f"[fib-cloud] ABORT: {agg_path} missing", file=sys.stderr)
            return 2
        agg = json.loads(agg_path.read_text(encoding="utf-8"))
        try:
            agg = inject_fib(agg, payload, load_desk_pw())
        except FibRefreshAbort as e:
            print(f"[fib-cloud] ABORT: {e}", file=sys.stderr)
            return 4
        # F260727-RADARSUBSTRATE — the public Radar block, from the SAME projection the laptop emit
        # calls. Built from the payload we just sealed, so the two can never disagree about which
        # bank or which close they scored. inject_fib comes first on purpose: if the book cannot be
        # opened this job aborts before touching anything, and a radar written next to a book we
        # failed to re-seal would be a half-applied update.
        radar = build_radar_block(payload)
        _sanity_radar(radar, payload)
        agg["fib_radar"] = radar
        from atomic_io import atomic_write_json
        atomic_write_json(agg_path, agg, indent=1)
        c = radar["counts"]
        print("[fib-cloud] aggregate updated — fib analysis LOCKED into sensitive_enc; public stub "
              "(price_as_of=%s) left for provenance" % payload["price_as_of"])
        print("[fib-cloud] fib_radar rebuilt on the CLOUD substrate: %d fire · %d watch · %d "
              "exclude · board %d" % (c["fire"], c["watch"], c["exclude"], len(radar["board"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
