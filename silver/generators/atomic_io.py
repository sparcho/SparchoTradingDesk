#!/usr/bin/env python3
"""
atomic_io.py — crash-safe file writers for TRADER generators.

WHY (codified 2026-06-03 per P1.2 broken-lens incident):
  Several sentinel writers (`last_run.json` in screener_runner.py /
  watchlist_runner.py) and the `positions_unified.json` cache were written
  with a plain `path.write_text(...)`. If the process is interrupted mid-write
  (signal, kill, crash, disk full), the file is left TRUNCATED — e.g. a
  `last_run.json` cut off at `"status": "` — and any reader (system_doctor.py,
  the dashboard emit) crashes on `json.JSONDecodeError`. The 260603 health run
  reported four lenses RED ("Unterminated string ... char 440") from exactly
  this failure mode; positions_unified.json corrupted the same way on 260525
  (see `_cache/positions_unified.json.corrupt-260525T2105`).

FIX: write to a temp file in the SAME directory, fsync, then `os.replace()`
  onto the target. `os.replace` is atomic on POSIX and on Windows (same volume),
  so a reader sees either the complete old file or the complete new file —
  never a half-written one.

USAGE:
    from atomic_io import atomic_write_text, atomic_write_json
    atomic_write_json(path, obj, indent=2)
    atomic_write_text(path, "....")

Keep this dependency-free (stdlib only) so every generator can import it.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Union

_PathLike = Union[str, "os.PathLike[str]", Path]


def atomic_write_text(path: _PathLike, text: str, encoding: str = "utf-8") -> Path:
    """Atomically write `text` to `path`.

    Writes to a temp file in the same directory, flushes + fsyncs, then
    os.replace()s onto the target. On any exception the temp file is removed
    and the original target is left untouched.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # NamedTemporaryFile in the same dir guarantees os.replace stays on one volume.
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)  # atomic
        return path
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def atomic_write_json(path: _PathLike, obj: Any, *, indent: int = 2,
                      ensure_ascii: bool = False, sort_keys: bool = False) -> Path:
    """Atomically serialize `obj` to JSON at `path`.

    Serialization happens fully in memory BEFORE any file is touched, so a
    serialization error can never leave a partial file on disk.
    """
    text = json.dumps(obj, indent=indent, ensure_ascii=ensure_ascii, sort_keys=sort_keys)
    return atomic_write_text(path, text)


# ---------------------------------------------------------------------------
# safe_write — atomic write WITH pre-commit content VERIFICATION (Phase-0
# guardrail, F139 / F260706 truncation root-cause fix, codified 2026-07-06).
#
# WHY (beyond atomic_write_text): atomicity guarantees a reader never sees a
# half-written file, but it does NOT protect against a *garbled payload computed
# in memory* — a generator/tool that produces truncated Python (the recurring
# Edit-tool / Cowork truncation, CLAUDE.md §0.2) or a cut-off JSON cache. If you
# atomically write bad bytes, you have atomically destroyed a good file. The
# 260705/260706 truncation crisis (system_doctor.py, equity/silver emitters,
# positions_unified.json NUL-tailed) was exactly this class. safe_write closes
# it: the payload is VERIFIED well-formed before it is allowed to replace the
# target, and on any failure the original is left byte-for-byte untouched.
# ---------------------------------------------------------------------------
class VerifyError(ValueError):
    """Raised when a safe_write payload fails verification (never overwrites)."""


def _verify_payload(name: str, data: bytes, *, sentinel: Union[str, bytes, None] = None) -> None:
    """Raise VerifyError unless `data` is a well-formed payload for `name`'s type.

    Checks (in order): non-empty · no NUL byte · .py compiles · .json loads ·
    optional trailing `sentinel` present. Type is inferred from the suffix.
    """
    if len(data) == 0:
        raise VerifyError(f"empty payload for {name}")
    if b"\x00" in data:
        raise VerifyError(f"NUL byte in {name}")
    suffix = Path(name).suffix.lower()
    try:
        if suffix == ".py":
            compile(data.decode("utf-8"), str(name), "exec")
        elif suffix == ".json":
            json.loads(data.decode("utf-8"))
    except (SyntaxError, ValueError, UnicodeDecodeError) as e:
        raise VerifyError(f"{name} failed {suffix or 'text'} verify: {type(e).__name__}: {e}") from e
    if sentinel is not None:
        s = sentinel.encode("utf-8") if isinstance(sentinel, str) else sentinel
        if not data.rstrip().endswith(s.rstrip()):
            raise VerifyError(f"{name} missing trailing sentinel {s!r}")


def safe_write(path: _PathLike, content: Union[str, bytes], *, verify: bool = True,
               sentinel: Union[str, bytes, None] = None, encoding: str = "utf-8") -> Path:
    """Atomically write `content` to `path`, VERIFYING the payload before commit.

    Sequence: verify in memory -> write temp in the same dir -> flush + fsync ->
    RE-READ the temp from disk and verify AGAIN (catches a write truncated by a
    kill / disk-full mid-flush) -> os.replace onto the target. On ANY verify or
    IO failure the temp is removed, the exception is RAISED, and the original
    target is left byte-for-byte untouched. Partial writes never overwrite good
    files. Pass verify=False to fall back to a plain atomic write (rare).
    """
    path = Path(path)
    data = content if isinstance(content, (bytes, bytearray)) else content.encode(encoding)
    data = bytes(data)
    if verify:
        _verify_payload(path.name, data, sentinel=sentinel)  # in-memory gate
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if verify:
            _verify_payload(path.name, tmp.read_bytes(), sentinel=sentinel)  # on-disk gate
        os.replace(tmp, path)  # atomic; only reached if both gates passed
        return path
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
