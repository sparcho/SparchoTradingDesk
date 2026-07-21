#!/usr/bin/env python3
"""levels_bank_publish.py — ship the operator's banked fib reads to the cloud, encrypted.

WHY THIS EXISTS (F260721-LEVELSCLOUD). The fib-confluence radar is produced by `box_engine.py`
+ `fib_confluence_feed.py`, and both read the operator's banked fib zones from
`00_SYSTEM/EDGE/LEVELS/` (58 tickers). The cloud has none of that, so the radar could only
ever be as fresh as the laptop — the exact surface the operator asked to keep working while
they are away. Shipping the bank is the precondition for moving that compute to the cloud.

The bank is the operator's proprietary analysis and the desk repo is PUBLIC, so it travels as
AES-256-GCM ciphertext under a MACHINE key held in a GitHub Actions secret.

Deliberately NOT the human dashboard password: that one is short and typed, and it also
unlocks the family book in-browser. Using a separate machine key means compromising one does
not yield the other, and the machine key can be long and rotated without the operator having
to remember anything.

No password-derivation here on purpose — a raw 32-byte random key needs no KDF, and adding one
would only invite a weak human secret back into the design.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path(__file__).resolve().parents[2]
BANK_DIR = VAULT / "00_SYSTEM" / "EDGE" / "LEVELS"
KEY_ENV = "LEVELS_BANK_KEY"


def new_key() -> str:
    """A fresh base64 32-byte machine key."""
    return base64.b64encode(os.urandom(32)).decode()


def _key_bytes(key_b64: str) -> bytes:
    raw = base64.b64decode(key_b64)
    if len(raw) != 32:
        raise ValueError("LEVELS_BANK_KEY must decode to exactly 32 bytes (got %d)" % len(raw))
    return raw


def encrypt_bundle(bundle: dict, key_b64: str) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    iv = os.urandom(12)
    payload = json.dumps(bundle, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ct = AESGCM(_key_bytes(key_b64)).encrypt(iv, payload, None)
    return {"v": 1, "alg": "AES-256-GCM",
            "iv": base64.b64encode(iv).decode(),
            "ct": base64.b64encode(ct).decode()}


def decrypt_bundle(blob: dict, key_b64: str) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    iv = base64.b64decode(blob["iv"])
    ct = base64.b64decode(blob["ct"])
    return json.loads(AESGCM(_key_bytes(key_b64)).decrypt(iv, ct, None).decode("utf-8"))


def build_bundle(bank_dir: Path = None) -> dict:
    """Collect every banked ticker into ONE bundle, stamped with its own provenance.

    The stamp is not decoration: a block that cannot be dated cannot be proven stale, which is
    exactly how the fib radar sat two sessions old while the desk looked fresh (F260721-FIBPROV).
    Whatever consumes this bundle must be able to say how old it is.
    """
    d = Path(bank_dir) if bank_dir else BANK_DIR
    banks, newest = {}, None
    for f in sorted(d.glob("*.json")):
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:                      # a corrupt bank file must be VISIBLE
            banks[f.stem] = {"_error": "unreadable: %s: %s" % (type(e).__name__, e)}
            continue
        banks[f.stem] = j
        sd = str(j.get("studied_date") or "")[:10]
        if len(sd) == 10 and (newest is None or sd > newest):
            newest = sd
    return {
        "schema": "v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "newest_studied_date": newest,
        "n_tickers": len(banks),
        "banks": banks,
    }


def publish(out_path, key_b64: str, bank_dir=None) -> dict:
    """Write the encrypted bundle, and RETURN the plaintext provenance sidecar.

    The sidecar carries dates and counts only — never levels, never tickers — so the cloud job
    and the doctor can judge the bank's age without holding the key. Encrypting a block must
    not make it unfalsifiable.
    """
    bundle = build_bundle(bank_dir)
    blob = encrypt_bundle(bundle, key_b64)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(blob, indent=1), encoding="utf-8")
    os.replace(tmp, out)                                   # atomic (§0.2d)
    return {"schema": bundle["schema"],
            "generated_at_utc": bundle["generated_at_utc"],
            "newest_studied_date": bundle["newest_studied_date"],
            "n_tickers": bundle["n_tickers"]}


def _load_key() -> str:
    key = os.environ.get(KEY_ENV)
    if key:
        return key.strip()
    kf = VAULT / "00_SYSTEM" / "GENERATORS" / "_inputs" / ".levels_bank_key"
    if kf.exists():
        return kf.read_text(encoding="utf-8").strip()
    raise SystemExit("no key: set $%s or create _inputs/.levels_bank_key (see --new-key)" % KEY_ENV)


def main() -> int:
    ap = argparse.ArgumentParser(description="Publish the banked fib levels to the cloud, encrypted.")
    ap.add_argument("--out", help="path to write the encrypted blob")
    ap.add_argument("--meta-out", help="path to write the plaintext provenance sidecar")
    ap.add_argument("--new-key", action="store_true", help="print a fresh machine key and exit")
    a = ap.parse_args()
    if a.new_key:
        print(new_key())
        return 0
    if not a.out:
        ap.error("--out is required")
    meta = publish(a.out, _load_key())
    if a.meta_out:
        Path(a.meta_out).write_text(json.dumps(meta, indent=1), encoding="utf-8")
    print("[levels-bank] %d ticker(s) | newest study %s | -> %s"
          % (meta["n_tickers"], meta["newest_studied_date"], a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
