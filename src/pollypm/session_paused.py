"""Shared pause-marker reader for ``<base_dir>/paused-sessions.json``.

The POST ``/api/v1/sessions/{name}/pause`` route writes the marker file
(see :mod:`pollypm.web_api.routes.sessions_admin`); this module is the
single read-side helper that the supervisor / recovery / dispatch loops
consult so the marker is honoured uniformly. Until this module landed
the marker was informational only — wiring it into every loop is tracked
under issue #2068.

The on-disk shape (a JSON list of session names) is owned by
``sessions_admin`` — this module only reads. The reader is best-effort:
a missing file, malformed JSON, or unreadable bytes collapse to "no
sessions paused" so a paused marker on a different project / partial
write can never crash the loops it gates.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_PAUSE_MARKER_FILENAME = "paused-sessions.json"


def _pause_marker_path(config: Any) -> Path | None:
    """Return ``<base_dir>/paused-sessions.json`` or ``None``.

    ``None`` when the supplied ``config`` does not carry a ``project.base_dir``
    — e.g. a partially-loaded config double in tests, or a recovery
    sweep tick that ran before config bootstrap finished. Callers must
    treat ``None`` the same as "no marker" so we never accidentally turn
    a misconfigured base_dir into an over-broad pause.
    """
    project = getattr(config, "project", None)
    base_dir = getattr(project, "base_dir", None)
    if base_dir is None:
        return None
    return Path(base_dir) / _PAUSE_MARKER_FILENAME


def load_paused_names(config: Any) -> set[str]:
    """Read the pause marker; empty set on missing / malformed file.

    Mirrors the reader in :mod:`pollypm.web_api.routes.sessions_admin`
    (which was the original home of this helper before issue #2068
    pulled it out so the loops could share a single source of truth).
    Best-effort: filesystem / JSON errors degrade to an empty set with
    a debug log line — the goal is to never break a recovery loop on a
    flaky read.
    """
    path = _pause_marker_path(config)
    if path is None or not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        logger.debug("pause marker unreadable: %s", path, exc_info=True)
        return set()
    if not isinstance(data, list):
        return set()
    return {str(name) for name in data if isinstance(name, str)}


def is_paused(config: Any, session_name: str) -> bool:
    """Return True iff ``session_name`` appears in the pause marker."""
    if not session_name:
        return False
    return session_name in load_paused_names(config)


# Audit-event constants. The supervisor / recovery loops emit
# ``session.pause.skip`` via the unified messages store whenever the
# guard fires, so the cockpit can show "the loop honored the pause"
# without operators having to grep logs.
PAUSE_SKIP_EVENT_TYPE = "session.pause.skip"


def skip_if_paused(
    config: Any,
    session_name: str,
    *,
    store: Any | None = None,
    loop: str = "",
    reason: str = "",
) -> bool:
    """Return True (and emit an audit event) when ``session_name`` is paused.

    ``store`` — when supplied, the helper emits a ``session.pause.skip``
    event via ``store.record_event(scope, sender, subject)`` so the
    cockpit / audit log records that the loop honoured the marker
    rather than silently dropping the call. Best-effort: any store
    error is swallowed so a flaky event-write can't unblock the guard.

    ``loop`` / ``reason`` — free-form context used to build the audit
    subject. ``loop`` is the calling site (e.g. ``"supervisor.maybe_recover_session"``,
    ``"no_session_spawn.auto_recover"``) so an operator can correlate
    the skip with the loop that yielded.

    Callers that don't want audit emission (e.g. read-side helpers) can
    omit ``store`` and use this purely as a boolean guard.
    """
    if not is_paused(config, session_name):
        return False
    if store is not None:
        _emit_pause_skip(store, session_name, loop=loop, reason=reason)
    return True


def _emit_pause_skip(
    store: Any, session_name: str, *, loop: str = "", reason: str = "",
) -> None:
    """Best-effort emit of the ``session.pause.skip`` audit event.

    Tolerant of both store shapes the recovery loops see in practice:

    * Unified ``Store`` — ``record_event(scope=..., sender=..., subject=..., payload=...)``
      (the supervisor / messages-store path).
    * Legacy ``StateStore`` — ``record_event(session_name, event_type, message)``
      (positional, used by :mod:`pollypm.recovery.no_session_spawn`'s
      attempt-history events).

    We try the unified shape first (it carries richer metadata) and
    fall back to the positional shape if the keyword call raises. Any
    failure is swallowed: the guard side is the contract, the audit
    event is observability only.
    """
    record = getattr(store, "record_event", None)
    if not callable(record):
        return
    subject_loop = loop or "unknown_loop"
    subject = (
        f"skipped {session_name} — {subject_loop} honored pause marker"
        + (f" ({reason})" if reason else "")
    )
    payload = {
        "session_name": session_name,
        "loop": subject_loop,
        "reason": reason,
    }
    try:
        record(
            scope=session_name,
            sender=PAUSE_SKIP_EVENT_TYPE,
            subject=subject,
            payload=payload,
        )
        return
    except TypeError:
        # Older / legacy ``record_event(session_name, event_type, message)``
        # positional shape — fall through.
        pass
    except Exception:  # noqa: BLE001
        logger.debug(
            "session.pause.skip emit (kw) failed for %s", session_name,
            exc_info=True,
        )
        return
    try:
        record(session_name, PAUSE_SKIP_EVENT_TYPE, subject)
    except Exception:  # noqa: BLE001
        logger.debug(
            "session.pause.skip emit (positional) failed for %s",
            session_name, exc_info=True,
        )


__all__ = [
    "PAUSE_SKIP_EVENT_TYPE",
    "is_paused",
    "load_paused_names",
    "skip_if_paused",
]
