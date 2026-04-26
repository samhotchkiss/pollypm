"""Atomic file write utilities to prevent data loss on crash."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def atomic_write_json(path: Path, data: object, *, indent: int = 2) -> None:
    """Write JSON to a file atomically using write-to-temp + rename.

    If the process crashes mid-write, the original file is untouched.
    On POSIX, os.rename() is atomic within the same filesystem.

    Always writes UTF-8 — a non-UTF-8 default locale (legacy Linux
    install, Windows CP-1252) would otherwise mangle emojis in commit
    messages, notify subjects, or any non-ASCII project name. JSON
    readers downstream (``json.loads(path.read_text())`` everywhere)
    expect UTF-8.
    """
    content = json.dumps(data, indent=indent) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp_path, path)
    except BaseException:
        # Clean up temp file on any failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, content: str) -> None:
    """Write text to a file atomically.

    Always writes UTF-8 — see ``atomic_write_json`` for rationale.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
