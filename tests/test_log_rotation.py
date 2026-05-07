"""Tests for ``pollypm.log_rotation`` — the ``~/.pollypm/`` log helpers.

#1366: ``~/.pollypm/*.log`` files had no rotation, so phantom_client.log
hit 275 MB. The helpers in :mod:`pollypm.log_rotation` cover both
writer styles:

* :func:`make_rotating_file_handler` — Python ``RotatingFileHandler``
  for ``errors.log`` and ``cockpit_debug.log``.
* :func:`bootstrap_truncate_if_too_big` — startup-time cleanup for
  shell-piped writers (``rail_daemon.log``, ``phantom_client.log``)
  whose FDs live in detached subprocesses.

Both paths are exercised end-to-end below: handler-driven rollover
producing ``.1`` / ``.2`` siblings, and bootstrap truncate archiving an
oversized file into a ``.gz`` while leaving an empty live file.
"""

from __future__ import annotations

import gzip
import logging
import time
from pathlib import Path

import pytest

from pollypm.log_rotation import (
    DEFAULT_BACKUP_COUNT,
    DEFAULT_MAX_BYTES,
    bootstrap_truncate_if_too_big,
    make_rotating_file_handler,
)


# ---------------------------------------------------------------------------
# make_rotating_file_handler — Python logging path
# ---------------------------------------------------------------------------


def test_handler_rotates_at_threshold(tmp_path: Path) -> None:
    """RotatingFileHandler rolls over once the live file crosses ``maxBytes``."""
    log_path = tmp_path / "errors.log"
    handler = make_rotating_file_handler(
        log_path,
        max_bytes=200,
        backup_count=3,
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.Logger("test_log_rotation_handler_rotates")
    logger.addHandler(handler)
    try:
        # 50 lines of ~20 bytes each = ~1000 bytes total; with a
        # 200-byte cap that's at least 4 rollovers.
        for i in range(50):
            logger.warning("entry-%03d-padding-bytes" % i)
    finally:
        handler.close()
        logger.removeHandler(handler)

    # The live file exists and is under the cap (rollover always
    # truncates it back to empty before the next write).
    assert log_path.exists()
    assert log_path.stat().st_size <= 400  # most recent line(s) only
    # Backups appear with .1, .2, .3 suffixes — never more than
    # backup_count of them.
    backups = sorted(tmp_path.glob("errors.log.*"))
    assert backups, "expected at least one rolled backup"
    assert len(backups) <= 3
    backup_names = {b.name for b in backups}
    assert "errors.log.1" in backup_names


def test_handler_caps_backup_count(tmp_path: Path) -> None:
    """Old backups beyond ``backup_count`` get deleted automatically."""
    log_path = tmp_path / "noisy.log"
    handler = make_rotating_file_handler(
        log_path,
        max_bytes=100,
        backup_count=2,
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.Logger("test_log_rotation_caps_backups")
    logger.addHandler(handler)
    try:
        for i in range(80):
            logger.warning("line-%03d-padding" % i)
    finally:
        handler.close()
        logger.removeHandler(handler)

    backups = sorted(tmp_path.glob("noisy.log.*"))
    # backup_count=2 means at most .1 and .2 are kept; .3 would have
    # been rolled off.
    assert len(backups) <= 2
    for b in backups:
        suffix = b.name.split(".")[-1]
        assert suffix in {"1", "2"}, b.name


def test_handler_creates_parent_dir(tmp_path: Path) -> None:
    """Handler factory mkdirs the parent so callers don't have to."""
    log_path = tmp_path / "nested" / "subdir" / "out.log"
    handler = make_rotating_file_handler(log_path, max_bytes=1024, backup_count=1)
    try:
        handler.emit(
            logging.LogRecord(
                name="t", level=logging.INFO, pathname=__file__,
                lineno=1, msg="hi", args=None, exc_info=None,
            )
        )
        handler.flush()
    finally:
        handler.close()
    assert log_path.exists()
    assert "hi" in log_path.read_text()


def test_handler_respects_explicit_caps(tmp_path: Path) -> None:
    """Explicit args override config / module defaults."""
    log_path = tmp_path / "explicit.log"
    handler = make_rotating_file_handler(
        log_path,
        max_bytes=12345,
        backup_count=7,
    )
    try:
        assert handler.maxBytes == 12345
        assert handler.backupCount == 7
    finally:
        handler.close()


def test_handler_falls_back_to_module_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When config can't be loaded the module defaults apply."""
    # Force the config import inside _resolve_caps to fail by pointing
    # DEFAULT_CONFIG_PATH at a nonexistent location and stubbing
    # load_config to raise.
    import pollypm.log_rotation as mod

    def _boom(_path: object) -> None:  # noqa: D401
        raise RuntimeError("simulated config failure")

    # Patch the lazy import inside _resolve_caps by intercepting
    # pollypm.config.load_config.
    import pollypm.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "load_config", _boom)

    log_path = tmp_path / "defaults.log"
    handler = mod.make_rotating_file_handler(log_path)
    try:
        assert handler.maxBytes == DEFAULT_MAX_BYTES
        assert handler.backupCount == DEFAULT_BACKUP_COUNT
    finally:
        handler.close()


# ---------------------------------------------------------------------------
# bootstrap_truncate_if_too_big — shell-piped log path
# ---------------------------------------------------------------------------


def test_bootstrap_truncate_skips_small_file(tmp_path: Path) -> None:
    """A file under the cap is left alone."""
    log_path = tmp_path / "rail_daemon.log"
    log_path.write_bytes(b"small\n" * 10)
    original_size = log_path.stat().st_size

    rotated = bootstrap_truncate_if_too_big(
        log_path, max_bytes=10_000, backup_count=3,
    )
    assert rotated is False
    assert log_path.stat().st_size == original_size
    assert not list(tmp_path.glob("rail_daemon.log.*"))


def test_bootstrap_truncate_archives_oversized_file(tmp_path: Path) -> None:
    """A file over the cap is gzipped aside and replaced with empty file."""
    log_path = tmp_path / "phantom_client.log"
    payload = b"X" * 4096
    log_path.write_bytes(payload)

    rotated = bootstrap_truncate_if_too_big(
        log_path, max_bytes=1024, backup_count=3,
    )
    assert rotated is True
    # Live file exists and is empty — the next ``open(path, "a")`` in
    # the spawn path will land at offset 0.
    assert log_path.exists()
    assert log_path.stat().st_size == 0
    # Exactly one .gz archive present, and it round-trips to the
    # original bytes.
    archives = sorted(tmp_path.glob("phantom_client.log.*.gz"))
    assert len(archives) == 1
    with gzip.open(archives[0], "rb") as fh:
        assert fh.read() == payload
    # No stray uncompressed rename was left behind.
    leftovers = [
        p for p in tmp_path.glob("phantom_client.log.*")
        if not p.name.endswith(".gz")
    ]
    assert leftovers == []


def test_bootstrap_truncate_prunes_old_archives(tmp_path: Path) -> None:
    """Older ``.gz`` archives beyond ``backup_count`` are deleted."""
    log_path = tmp_path / "rail_daemon.log"
    # Seed three pre-existing archives at distinct mtimes (oldest first).
    base_ts = int(time.time()) - 1000
    seeded = []
    for i in range(3):
        archive = tmp_path / f"rail_daemon.log.{base_ts + i}.gz"
        with gzip.open(archive, "wb") as fh:
            fh.write(f"archived-{i}".encode())
        # Force mtime to match the encoded ts so retention sees a
        # stable order even on fast filesystems.
        import os as _os

        _os.utime(archive, (base_ts + i, base_ts + i))
        seeded.append(archive)

    # Now write an oversized live file and rotate with backup_count=2.
    log_path.write_bytes(b"Y" * 4096)
    rotated = bootstrap_truncate_if_too_big(
        log_path, max_bytes=1024, backup_count=2,
    )
    assert rotated is True

    surviving = sorted(tmp_path.glob("rail_daemon.log.*.gz"))
    # New rotation + at most one of the previous three (backup_count=2
    # total). The very oldest seeded file must have been pruned.
    assert len(surviving) <= 2
    assert seeded[0] not in surviving


def test_bootstrap_truncate_missing_file(tmp_path: Path) -> None:
    """No-op when the target file doesn't exist yet."""
    log_path = tmp_path / "never_written.log"
    rotated = bootstrap_truncate_if_too_big(
        log_path, max_bytes=1024, backup_count=3,
    )
    assert rotated is False
    assert not log_path.exists()


def test_bootstrap_truncate_handles_directory_gracefully(tmp_path: Path) -> None:
    """A directory at the path is rejected, not crashed on."""
    not_a_file = tmp_path / "some_dir.log"
    not_a_file.mkdir()
    rotated = bootstrap_truncate_if_too_big(
        not_a_file, max_bytes=1, backup_count=1,
    )
    assert rotated is False
    # Directory is still there.
    assert not_a_file.is_dir()
