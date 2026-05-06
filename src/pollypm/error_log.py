"""Centralized error log — one file, all processes, all tracebacks.

Every long-running PollyPM process (rail daemon, cockpit TUI,
``pm``-entry-point CLI calls, future workers) attaches this handler
via :func:`install` during its boot path. WARNING-and-above records
from every logger in the process land in
``~/.pollypm/errors.log`` with a header that pins process name +
pid + timestamp + logger + level + message + traceback. Single
file, line-buffered, rotated by the existing ``log.rotate``
roster handler once it crosses the configurable size cap.

Design goals:

- **One place to grep.** Plugin crashes, SQLite lock failures,
  transcript-ingest FD exhaustion, provider CLI errors — all end
  up here. Sam's question "are we logging errors?" answered in a
  single path.
- **Tracebacks included.** ``logger.exception(...)`` lands with
  the full traceback; plain ``logger.error("msg")`` lands with
  just the message. Both are useful; both are captured.
- **No dependency on the scheduler/rail.** The handler is a plain
  stdlib ``logging.FileHandler`` — safe to install before the
  plugin host / rail / event bus exist. A boot-time crash still
  gets logged.
- **Idempotent.** Calling ``install`` twice (e.g. the cockpit
  initializing after ``pm up``) doesn't duplicate lines.

``pm errors`` (see ``cli_features/maintenance.py``) tails the file
for the user. Operators who prefer their own tooling can point
any log aggregator at ``~/.pollypm/errors.log``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from pollypm.error_notifications import CriticalErrorNotificationHandler

DEFAULT_LOG_FILENAME = "errors.log"
DEFAULT_LEVEL = logging.WARNING
_FORMAT = (
    "%(asctime)s %(levelname)s "
    "%(process_tag)s %(name)s: %(message)s"
)


class _ProcessTagFilter(logging.Filter):
    """Inject ``process_tag`` = ``<process_label>/<pid>`` into every record.

    Letting operators grep lines from a single process at a time —
    ``rail_daemon/12345`` vs ``cockpit/67890`` vs ``cli/11111`` —
    without cross-referencing pid mappings.
    """

    def __init__(self, tag: str) -> None:
        super().__init__()
        self._tag = f"{tag}/{os.getpid()}"

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        record.process_tag = self._tag
        return True


def _log_path() -> Path:
    """~/.pollypm/errors.log — mirrors the DEFAULT_CONFIG_PATH convention."""
    override = os.environ.get("POLLYPM_ERROR_LOG_PATH")
    if override:
        return Path(override).expanduser()

    from pollypm.config import DEFAULT_CONFIG_PATH

    return Path(DEFAULT_CONFIG_PATH).parent / DEFAULT_LOG_FILENAME


def install(
    *,
    process_label: str,
    level: int = DEFAULT_LEVEL,
    path: Path | None = None,
) -> None:
    """Attach the centralized error handler to the root logger.

    ``process_label`` appears in every record as part of
    ``process_tag`` so ``rail_daemon`` lines, ``cockpit`` lines, and
    ``cli`` lines are distinguishable at a glance. Safe to call more
    than once per process — the handler is keyed by a module-level
    sentinel so the second call is a no-op.
    """
    root = logging.getLogger()
    # Don't stack handlers — a repeat install (cockpit boot after
    # ``pm up`` already installed for its CLI process) is a no-op.
    if not any(getattr(h, "_pollypm_error_handler", False) for h in root.handlers):
        target = path or _log_path()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(target, encoding="utf-8")
        except Exception:  # noqa: BLE001
            # Can't write the log file — don't take the whole process
            # down over it. Callers still get stderr logging from their
            # own basicConfig.
            return
        handler.setLevel(level)
        handler.setFormatter(logging.Formatter(_FORMAT))
        handler.addFilter(_ProcessTagFilter(process_label))
        handler._pollypm_error_handler = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    if (
        not _env_flag_enabled("POLLYPM_DISABLE_ERROR_NOTIFICATIONS")
        and not any(
            getattr(h, "_pollypm_error_notification_handler", False)
            for h in root.handlers
        )
    ):
        alert_handler = CriticalErrorNotificationHandler()
        alert_handler._pollypm_error_notification_handler = True  # type: ignore[attr-defined]
        root.addHandler(alert_handler)
    if root.level > level:
        root.setLevel(level)


def _env_flag_enabled(name: str) -> bool:
    value = os.environ.get(name, "")
    return value.strip().lower() not in {"", "0", "false", "no", "off"}


def path() -> Path:
    """Return the ``~/.pollypm/errors.log`` path callers should tail."""
    return _log_path()


__all__ = ["DEFAULT_LOG_FILENAME", "DEFAULT_LEVEL", "install", "path"]
