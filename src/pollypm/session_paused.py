"""Shared pause-marker reader/writer for ``<base_dir>/paused-sessions.json``.

This module owns the on-disk pause-marker contract end-to-end:

* The filename constant (``_PAUSE_MARKER_FILENAME``) and the
  config-derived path helper (``_pause_marker_path``).
* The read helpers (``load_paused_names`` / ``is_paused``) consumed by
  the supervisor / recovery loops AND the sessions-admin GET surface.
* The write helpers (``save_paused_names`` / ``pause_marker_lock``)
  consumed by ``POST /api/v1/sessions/{name}/pause`` and ``resume``.

Centralising both sides here is deliberate: the route module used to
keep its own ``_PAUSE_MARKER_FILENAME`` / ``_pause_marker_path`` copies
and the recovery loops keyed off a separate constant, so a rename or a
shape change risked silent drift between the writer and every reader
(Codex PR #2081 round 1 blocker 2 — "two owners of the marker recreate
exactly the drift risk the shared helper is supposed to remove").

Recovery wiring status (#2068)
------------------------------

PR ``feat/sessions-pause-marker-wire-loops-1-3`` wires the marker into
loops 1–3:

* :func:`pollypm.recovery.no_session_spawn.auto_recover_no_session_alerts`
  (loop 1) and :meth:`pollypm.supervisor.Supervisor.maybe_recover_session`
  (loops 2 + 3) BOTH consult ``is_paused`` / ``skip_if_paused`` and
  yield with an audit event when the marker is set.

Remaining dispatch / cockpit / heartbeat loops still treat the marker
as informational and are tracked as the next slice under #2068.

The on-disk shape is a JSON list of session names sitting next to
``state.db`` in the project's ``base_dir``. The reader is best-effort:
a missing file, malformed JSON, or unreadable bytes collapse to "no
sessions paused" so a paused marker on a different project / partial
write can never crash the loops it gates.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Filename of the project-scoped pause marker. One JSON file per
# project; the document is a list of paused session names. Sitting in
# the project's ``base_dir`` keeps it next to ``state.db`` /
# ``audit.jsonl`` so storage hygiene already covers it. Owned here
# (the shared module) — sessions_admin imports rather than redeclaring
# (Codex PR #2081 round 1 blocker 2).
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


def save_paused_names(config: Any, names: set[str] | list[str]) -> None:
    """Atomically write the pause marker. Raises ``OSError`` on failure.

    Used by ``POST /api/v1/sessions/{name}/pause`` and ``resume``. The
    write is a tmp+rename so concurrent readers always see either the
    pre- or post-write document, never a partial JSON blob. Raises
    ``RuntimeError`` if the supplied config has no ``project.base_dir``
    — the route layer translates that to a 503 ``daemon_unavailable``
    response.

    ``names`` may be any iterable of strings; the on-disk shape is a
    sorted JSON list (stable for diff'ing across writes).
    """
    path = _pause_marker_path(config)
    if path is None:
        raise RuntimeError(
            "no base_dir on config; pause marker has nowhere to live",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = sorted(set(names))
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    tmp.replace(path)


@contextmanager
def pause_marker_lock(config: Any):
    """Serialise pause/resume read-modify-write across concurrent callers.

    Takes an ``fcntl.flock`` on a sibling ``.lock`` file in the same
    directory as the marker. Best-effort on platforms without
    ``fcntl`` (Windows): degrades to a no-op lock since the marker
    write is still atomic via tmp+rename — only the read-modify-write
    window is unprotected.

    Yields the marker ``Path`` (or ``None`` when there is no base_dir
    on the config — caller will hit the same "no path" failure from
    :func:`save_paused_names` anyway).
    """
    path = _pause_marker_path(config)
    if path is None:
        yield None
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    try:
        import fcntl  # type: ignore[import-not-found]
    except ImportError:
        yield path
        return
    fh = open(lock_path, "a+")  # noqa: SIM115 — closed in finally
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield path
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


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
    "pause_marker_lock",
    "save_paused_names",
    "skip_if_paused",
]
