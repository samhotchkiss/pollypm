"""Log rotation helpers for ``~/.pollypm/*.log`` files.

Closes #1366. The hourly ``log.rotate`` recurring handler under
``plugins_builtin/core_recurring/maintenance.py`` only walks the
workspace ``logs_dir`` (i.e. ``.tmp-pm/logs``), which means every log
file written under ``~/.pollypm/`` grows unbounded. On the reporter's
machine ``phantom_client.log`` had reached 275 MB.

This module covers the two writer styles that target ``~/.pollypm/``:

* **Python logging-based writers** (``error_log.errors.log`` and
  ``cli_features.ui.cockpit_debug.log``) get a Python
  :class:`~logging.handlers.RotatingFileHandler` with a configurable
  byte cap and backup count. Use :func:`make_rotating_file_handler` —
  it mirrors the stdlib API but pulls defaults from
  :class:`~pollypm.models.LoggingSettings` so the rotation thresholds
  stay in sync with the existing ``log.rotate`` plugin.

* **Shell-piped writers** (``rail_daemon.log``, ``phantom_client.log``
  written via ``open(path, "a")`` then handed to ``Popen`` as
  stdout/stderr) cannot pick up Python ``RotatingFileHandler`` rollover
  — the file descriptor is held by the spawned process. For those we
  call :func:`bootstrap_truncate_if_too_big` from the spawn path: if
  the existing log already exceeds the cap, archive it once into a
  ``.log.<ts>.gz`` sibling (best-effort) and start fresh. The newly
  spawned process appends to a freshly empty file, so further growth
  is bounded by however long the process lives between restarts. Daily
  ``pm up`` cycles are enough to keep these from running away — the
  phantom client gets respawned every cockpit boot, the rail daemon
  every ``pm up``.

Tests exercise both paths in ``tests/test_log_rotation.py``.
"""

from __future__ import annotations

import gzip
import logging
import os
import shutil
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

logger = logging.getLogger(__name__)

# Default cap per individual log file before rotation kicks in. 50 MB
# keeps enough scrollback for incident debugging while staying well
# below the runaway sizes from #1366. Operators who want tighter or
# looser bounds set ``[logging] rotate_size_mb`` in the config (the
# plugin handler reads the same field).
DEFAULT_MAX_BYTES = 50 * 1024 * 1024

# Default number of rolled backups to keep alongside the live file.
# Five backups at 50 MB each = 300 MB ceiling per rotated log, which is
# acceptable for a workstation tool. Operators who need less tune via
# ``[logging] rotate_keep``.
DEFAULT_BACKUP_COUNT = 5


def _resolve_caps(
    max_bytes: int | None,
    backup_count: int | None,
) -> tuple[int, int]:
    """Resolve rotation caps from explicit args, config, or defaults.

    Caller-supplied values win. Otherwise we lazy-import
    :mod:`pollypm.config` and try to read the live ``[logging]``
    section so the rotation thresholds stay aligned with the existing
    ``log.rotate`` plugin handler. If config loading fails for any
    reason we fall back to module defaults — log rotation is a hygiene
    feature, not something to crash boot over.
    """
    if max_bytes is not None and backup_count is not None:
        return max(1, int(max_bytes)), max(0, int(backup_count))
    cfg_size_mb: int | None = None
    cfg_keep: int | None = None
    try:
        from pollypm.config import DEFAULT_CONFIG_PATH, load_config

        config = load_config(Path(DEFAULT_CONFIG_PATH))
    except Exception:  # noqa: BLE001
        config = None
    if config is not None:
        try:
            cfg_size_mb = int(config.logging.rotate_size_mb)
            cfg_keep = int(config.logging.rotate_keep)
        except Exception:  # noqa: BLE001
            cfg_size_mb = None
            cfg_keep = None
    resolved_max = (
        int(max_bytes) if max_bytes is not None
        else (cfg_size_mb * 1024 * 1024 if cfg_size_mb else DEFAULT_MAX_BYTES)
    )
    resolved_keep = (
        int(backup_count) if backup_count is not None
        else (cfg_keep if cfg_keep is not None else DEFAULT_BACKUP_COUNT)
    )
    return max(1, resolved_max), max(0, resolved_keep)


def make_rotating_file_handler(
    path: Path,
    *,
    max_bytes: int | None = None,
    backup_count: int | None = None,
    encoding: str = "utf-8",
    mode: str = "a",
) -> RotatingFileHandler:
    """Build a stdlib :class:`RotatingFileHandler` with PollyPM defaults.

    ``max_bytes`` and ``backup_count`` default to the values from the
    user's ``[logging]`` section (or the module constants if the
    config can't be loaded). Pass explicit values to override per
    call site (e.g. tests pinning a smaller threshold).

    The handler's parent directory is created on demand so callers
    don't have to mkdir. Returns the handler ready to attach with
    ``logger.addHandler(...)``.
    """
    resolved_max, resolved_keep = _resolve_caps(max_bytes, backup_count)
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        path,
        mode=mode,
        maxBytes=resolved_max,
        backupCount=resolved_keep,
        encoding=encoding,
    )
    return handler


def bootstrap_truncate_if_too_big(
    path: Path,
    *,
    max_bytes: int | None = None,
    backup_count: int | None = None,
) -> bool:
    """Archive + truncate ``path`` if it already exceeds the size cap.

    Designed for shell-piped writers (``rail_daemon.log``,
    ``phantom_client.log``) where the file descriptor lives in a
    detached subprocess and Python's :class:`RotatingFileHandler`
    can't see writes. Call this *before* the next spawn so the new
    process starts at offset 0 in a fresh file.

    Behaviour:

    * If ``path`` doesn't exist or is under the cap, no-op (returns
      ``False``).
    * Otherwise rename to ``<path>.<ts>.gz`` (archived in place),
      gzip the contents, drop the uncompressed rename, and prune
      older ``<path>.<ts>.gz`` siblings beyond ``backup_count``.
    * Returns ``True`` when a rotation actually happened. Errors are
      swallowed and logged at DEBUG — log hygiene must never block
      ``pm up``.

    Note this is *not* per-write rotation; it caps the *previous*
    process's tail at the next restart. Combined with the fact that
    ``pm up`` / cockpit reboots happen frequently in normal use, that
    bounds shell-piped logs to roughly one process-lifetime of
    growth — far better than unbounded.
    """
    resolved_max, resolved_keep = _resolve_caps(max_bytes, backup_count)
    try:
        if not path.exists() or not path.is_file():
            return False
        size = path.stat().st_size
    except OSError:
        return False
    if size <= resolved_max:
        return False
    ts = int(time.time())
    archived = path.with_suffix(path.suffix + f".{ts}")
    bump = 0
    while archived.exists():
        bump += 1
        archived = path.with_suffix(path.suffix + f".{ts}.{bump}")
    try:
        os.rename(path, archived)
    except OSError:
        logger.debug(
            "log_rotation: rename failed for %s", path, exc_info=True,
        )
        return False
    gz_path = archived.with_suffix(archived.suffix + ".gz")
    rotated = False
    try:
        with open(archived, "rb") as src, gzip.open(gz_path, "wb") as dst:
            shutil.copyfileobj(src, dst)
        archived.unlink()
        rotated = True
    except OSError:
        logger.debug(
            "log_rotation: gzip failed for %s", archived, exc_info=True,
        )
        # Best-effort: leave the uncompressed rename in place rather
        # than losing the data. Future runs will still keep logs
        # bounded because the live file is now empty.
    finally:
        # Recreate an empty live file so the spawned process's append
        # opens cleanly without depending on creation semantics.
        try:
            path.touch()
        except OSError:
            logger.debug(
                "log_rotation: touch failed for %s", path, exc_info=True,
            )
    _prune_old_archives(path, resolved_keep)
    return rotated


def _prune_old_archives(path: Path, backup_count: int) -> None:
    """Delete archived rotations beyond the retention count.

    Matches files of the shape ``<path>.<ts>[.bump].gz``. Sorting is
    by mtime (descending) so newest survives even if timestamp
    parsing trips on a malformed name.
    """
    if backup_count < 0:
        return
    parent = path.parent
    prefix = path.name + "."
    candidates: list[tuple[float, Path]] = []
    try:
        for sibling in parent.iterdir():
            if not sibling.is_file():
                continue
            if not sibling.name.startswith(prefix):
                continue
            if not sibling.name.endswith(".gz"):
                continue
            try:
                mtime = sibling.stat().st_mtime
            except OSError:
                continue
            candidates.append((mtime, sibling))
    except OSError:
        return
    candidates.sort(key=lambda item: item[0], reverse=True)
    for _mtime, stale in candidates[backup_count:]:
        try:
            stale.unlink()
        except OSError:
            logger.debug(
                "log_rotation: prune failed for %s", stale, exc_info=True,
            )


__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_BACKUP_COUNT",
    "make_rotating_file_handler",
    "bootstrap_truncate_if_too_big",
]
