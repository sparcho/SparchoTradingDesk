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
