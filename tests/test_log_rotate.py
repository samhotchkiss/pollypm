"""Tests for the hourly ``log.rotate`` recurring handler.

The handler keeps ``<logs_dir>/*.log`` from growing unbounded. Writers
(tmux ``pipe-pane``) keep appending to ``<name>.log``; when the file
crosses the threshold the handler renames it, gzips the rotation, and
truncates the original back to empty.

Behaviour under test:

* Files at or below the threshold are skipped — size unchanged.
* Oversize files are rotated to ``<name>.log.<ts>.gz`` and the original
  is truncated to 0 bytes.
* Retention keeps only the most recent N rotations per base name.
* Running twice on a clean state is a no-op.
* Missing ``logs_dir`` returns zeroed counters without error.
* Non-``.log`` files (JSON state blobs etc.) are never touched.
"""

from __future__ import annotations

import gzip
import os
import time
from pathlib import Path

from pollypm.plugins_builtin.core_recurring.plugin import log_rotate_handler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_bytes(path: Path, n_bytes: int) -> None:
    """Create ``path`` with exactly ``n_bytes`` bytes of filler content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write in a single shot; these test fixtures are well under 100 MB.
    path.write_bytes(b"x" * n_bytes)


def _make_gz_rotation(logs_dir: Path, base: str, ts: int) -> Path:
    """Create a pre-existing ``<base>.log.<ts>.gz`` rotation file."""
    path = logs_dir / f"{base}.log.{ts}.gz"
    with gzip.open(path, "wb") as fh:
        fh.write(b"archived rotation\n")
    return path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_under_threshold_skipped(tmp_path: Path) -> None:
    """A log file below the threshold is left untouched."""
    logs = tmp_path / "logs"
    logs.mkdir()
    small = logs / "small.log"
    _write_bytes(small, 4 * 1024 * 1024)  # 4 MB, under 20 MB default

    result = log_rotate_handler({"logs_dir": str(logs)})

    assert result == {"rotated": 0, "deleted": 0, "errors": 0}
    assert small.stat().st_size == 4 * 1024 * 1024
    # No .gz siblings were born.
    assert not list(logs.glob("*.gz"))


def test_over_threshold_rotated_and_gzipped(tmp_path: Path) -> None:
    """A log over the threshold is rotated to .gz + original truncated."""
    logs = tmp_path / "logs"
    logs.mkdir()
    big = logs / "big.log"
    # 2 MB file with a 1 MB threshold triggers rotation.
    _write_bytes(big, 2 * 1024 * 1024)

    result = log_rotate_handler(
        {"logs_dir": str(logs), "rotate_size_mb": 1, "rotate_keep": 3},
    )

    assert result["rotated"] == 1
    assert result["errors"] == 0
    # Original now exists and is empty.
    assert big.exists()
    assert big.stat().st_size == 0
    # Exactly one gzipped rotation produced.
    rotations = sorted(logs.glob("big.log.*.gz"))
    assert len(rotations) == 1
    # And it decompresses back to the original bytes count.
    with gzip.open(rotations[0], "rb") as fh:
        data = fh.read()
    assert len(data) == 2 * 1024 * 1024
    # No stray uncompressed rotation was left behind — every file
    # matching ``big.log.*`` must end in ``.gz``.
    stray = [p for p in logs.glob("big.log.*") if not p.name.endswith(".gz")]
    assert stray == []


def test_retention_keeps_only_n_most_recent(tmp_path: Path) -> None:
    """Only the ``rotate_keep`` most recent rotations survive."""
    logs = tmp_path / "logs"
    logs.mkdir()
    # Five prior rotations at ascending timestamps. Keep=2 → 3 deleted.
    _make_gz_rotation(logs, "pm-operator", 1000)
    _make_gz_rotation(logs, "pm-operator", 2000)
    _make_gz_rotation(logs, "pm-operator", 3000)
    _make_gz_rotation(logs, "pm-operator", 4000)
    _make_gz_rotation(logs, "pm-operator", 5000)

    # No active .log file above threshold — rotation step does nothing,
    # but the retention sweep still runs.
    result = log_rotate_handler(
        {"logs_dir": str(logs), "rotate_size_mb": 1, "rotate_keep": 2},
    )

    assert result["rotated"] == 0
    assert result["deleted"] == 3
    assert result["errors"] == 0
    surviving = sorted(p.name for p in logs.glob("pm-operator.log.*.gz"))
    assert surviving == [
        "pm-operator.log.4000.gz",
        "pm-operator.log.5000.gz",
    ]


def test_idempotent_on_clean_state(tmp_path: Path) -> None:
    """Second pass after full rotation is a no-op."""
    logs = tmp_path / "logs"
    logs.mkdir()
    big = logs / "ops.log"
    _write_bytes(big, 2 * 1024 * 1024)

    first = log_rotate_handler(
        {"logs_dir": str(logs), "rotate_size_mb": 1, "rotate_keep": 3},
    )
    assert first["rotated"] == 1

    second = log_rotate_handler(
        {"logs_dir": str(logs), "rotate_size_mb": 1, "rotate_keep": 3},
    )
    # ops.log is empty now, nothing to rotate; existing gz is within
    # retention, nothing to delete.
    assert second == {"rotated": 0, "deleted": 0, "errors": 0}


def test_missing_logs_dir_returns_zeros(tmp_path: Path) -> None:
    """A logs_dir that doesn't exist yields zero counters without error."""
    logs = tmp_path / "does-not-exist"
    result = log_rotate_handler({"logs_dir": str(logs)})
    assert result == {"rotated": 0, "deleted": 0, "errors": 0}


def test_non_log_files_are_not_rotated(tmp_path: Path) -> None:
    """Files without a ``.log`` suffix are ignored even when large."""
    logs = tmp_path / "logs"
    logs.mkdir()
    # Oversize non-.log file — must not be rotated.
    state_json = logs / "sessions.json"
    _write_bytes(state_json, 3 * 1024 * 1024)
    # An oversize .log file next to it — should rotate.
    log = logs / "worker.log"
    _write_bytes(log, 3 * 1024 * 1024)

    result = log_rotate_handler(
        {"logs_dir": str(logs), "rotate_size_mb": 1, "rotate_keep": 3},
    )

    assert result["rotated"] == 1
    assert result["errors"] == 0
    # The JSON file is untouched, same size, no gz spawned for it.
    assert state_json.exists()
    assert state_json.stat().st_size == 3 * 1024 * 1024
    assert not list(logs.glob("sessions.json.*.gz"))
    assert not list(logs.glob("sessions.*.gz"))
    # The log file rotated as expected.
    assert log.stat().st_size == 0
    assert len(list(logs.glob("worker.log.*.gz"))) == 1


def test_multiple_rotations_then_retention(tmp_path: Path) -> None:
    """Rotating several times over time honours retention on each pass."""
    logs = tmp_path / "logs"
    logs.mkdir()
    big = logs / "roll.log"

    # First rotation.
    _write_bytes(big, 2 * 1024 * 1024)
    log_rotate_handler(
        {"logs_dir": str(logs), "rotate_size_mb": 1, "rotate_keep": 2},
    )

    # Back-date the first rotation so the next timestamp is distinct
    # and so the sort order is deterministic (epoch seconds can collide
    # within a test run).
    for p in logs.glob("roll.log.*.gz"):
        stamp = time.time() - 3 * 3600
        os.utime(p, (stamp, stamp))
        # Also rename with an older ts so the numeric sort places it
        # before the next rotation.
        renamed = p.with_name("roll.log.1000.gz")
        p.rename(renamed)

    # Second rotation.
    _write_bytes(big, 2 * 1024 * 1024)
    log_rotate_handler(
        {"logs_dir": str(logs), "rotate_size_mb": 1, "rotate_keep": 2},
    )

    for p in logs.glob("roll.log.*.gz"):
        if p.name == "roll.log.1000.gz":
            continue
        p.rename(p.with_name("roll.log.2000.gz"))

    # Third rotation — at this point we should have ts=1000, ts=2000,
    # and a fresh ts from this pass; retention=2 drops the oldest.
    _write_bytes(big, 2 * 1024 * 1024)
    result = log_rotate_handler(
        {"logs_dir": str(logs), "rotate_size_mb": 1, "rotate_keep": 2},
    )

    assert result["rotated"] == 1
    # At least the ts=1000 rotation was deleted to honour keep=2.
    assert result["deleted"] >= 1
    remaining = sorted(p.name for p in logs.glob("roll.log.*.gz"))
    assert len(remaining) == 2
    assert "roll.log.1000.gz" not in remaining
