"""Shared pause-marker reader/writer for ``<base_dir>/paused-sessions.json``.

This module owns the on-disk pause-marker contract end-to-end:

* The filename constant (``_PAUSE_MARKER_FILENAME``) and the
  config-derived path helper (``_pause_marker_path``).
* The read helpers (``load_paused_state`` / ``load_paused_names`` /
  ``is_paused``) consumed by the supervisor / recovery loops AND the
  sessions-admin GET surface.
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

Marker states
-------------

The on-disk shape is a JSON list of session names sitting next to
``state.db`` in the project's ``base_dir``. The reader distinguishes
three states (PR #2081 round 2 — Codex finding 2):

* ``MarkerState.absent`` — no file on disk. No sessions paused.
* ``MarkerState.ok(names)`` — file parses cleanly to a list of names.
* ``MarkerState.unreadable(reason)`` — file exists but cannot be read
  or parsed (corrupt JSON, permission denied, wrong shape).

For recovery / supervisor gating we FAIL CLOSED on ``unreadable``:
:func:`is_paused` and :func:`skip_if_paused` treat the unreadable case
as "every session is paused" rather than restarting a session the
operator intended to keep quiesced. The transition into the unreadable
state emits a throttled ``session.pause.marker_unreadable`` audit
event and a one-time stderr warning so the operator can repair the
marker; the transition back out emits ``session.pause.marker_restored``.

For the read-only sessions-admin GET surface we keep the legacy
"best-effort empty set" behaviour via :func:`load_paused_names` so a
stale marker on disk does not 500 the dashboard; the GET path surfaces
``paused`` purely as a presentational signal.

Audit-event spam (PR #2081 round 2 — Codex finding 1)
-----------------------------------------------------

:func:`skip_if_paused` is invoked from periodic sweep ticks. The
audit-event side of the helper is throttled per ``(session, loop)``
key with :data:`PAUSE_SKIP_THROTTLE_SECONDS` to keep the durable
event stream bounded on a long-running paused session. The boolean
return is NOT throttled — the guard always trips on a paused name.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
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


# How long to suppress repeat ``session.pause.skip`` audit events for
# the same (session, loop) key. 300s (5 min) is short enough that an
# operator inspecting the audit stream sees a fresh confirmation that
# the marker is still in effect within a reasonable window, but long
# enough that a sweep tick running every few seconds does not pile up
# unbounded rows. The shorter ``rail_daemon_supervisor`` revival
# throttle is 60s for a HOT path; this is observability-only, so we
# can afford a wider window. The watchdog-side audit throttles are
# 1800s / 5400s but those are for true escalations — a pause is a
# steady-state condition the operator is actively maintaining, so we
# split the difference.
PAUSE_SKIP_THROTTLE_SECONDS = 300.0

# Same throttle for the unreadable-marker audit event. We emit at
# most one event per process per window so a corrupt marker that
# survives across many sweep ticks does not spam the durable log;
# state transitions (unreadable -> readable, readable -> unreadable)
# bypass the throttle because the operator needs to see them.
PAUSE_MARKER_UNREADABLE_THROTTLE_SECONDS = PAUSE_SKIP_THROTTLE_SECONDS


# Audit-event constants.
#
# ``session.pause.skip`` — emitted by the supervisor / recovery loops
# whenever the guard fires, so the cockpit can show "the loop honored
# the pause" without operators having to grep logs. Throttled per
# (session, loop) by :data:`PAUSE_SKIP_THROTTLE_SECONDS`.
PAUSE_SKIP_EVENT_TYPE = "session.pause.skip"

# ``session.pause.marker_unreadable`` — emitted when the marker file
# exists but cannot be parsed / read. Recovery loops will be failing
# closed (treating every session as paused) until the operator
# repairs the marker, so this event needs to land. Emitted at most
# once per :data:`PAUSE_MARKER_UNREADABLE_THROTTLE_SECONDS` per
# process; transitions back to readable emit
# :data:`PAUSE_MARKER_RESTORED_EVENT_TYPE` and reset the throttle.
PAUSE_MARKER_UNREADABLE_EVENT_TYPE = "session.pause.marker_unreadable"
PAUSE_MARKER_RESTORED_EVENT_TYPE = "session.pause.marker_restored"


# --- Marker state ---------------------------------------------------


@dataclass(frozen=True)
class MarkerState:
    """Discriminated state of the on-disk pause marker.

    ``kind`` is one of ``"absent"``, ``"ok"``, ``"unreadable"``.

    * ``absent`` — no file present; ``names`` is the empty frozenset.
    * ``ok`` — file parsed cleanly; ``names`` is the parsed set.
    * ``unreadable`` — file present but cannot be read/parsed;
      ``names`` is empty and ``reason`` carries the failure detail.
      Recovery callers MUST treat this as "all sessions paused".
    """

    kind: str
    names: frozenset[str] = frozenset()
    reason: str = ""

    @classmethod
    def absent(cls) -> "MarkerState":
        return cls(kind="absent")

    @classmethod
    def ok(cls, names: set[str] | frozenset[str]) -> "MarkerState":
        return cls(kind="ok", names=frozenset(names))

    @classmethod
    def unreadable(cls, reason: str) -> "MarkerState":
        return cls(kind="unreadable", reason=reason)


# Per-process throttle state. Keyed by ``(session, loop)`` for the
# skip event; a single bucket for the unreadable-marker event since
# it's process-wide.
_SKIP_THROTTLE_LOCK = threading.Lock()
_PAUSE_SKIP_LAST_EMITTED: dict[tuple[str, str], float] = {}

_MARKER_STATE_LOCK = threading.Lock()
# Tracks the most-recent known kind ("ok"/"absent"/"unreadable") so we
# can detect transitions and emit a restored event when the marker
# becomes readable again.
_LAST_MARKER_KIND: dict[str, str] = {}
# Last monotonic timestamp at which we emitted the unreadable event
# for a given marker path. ``None`` / missing entry means "never";
# we deliberately avoid 0.0 because :func:`time.monotonic` can return
# small values early in a process lifetime on some platforms (notably
# macOS) which would otherwise look like a recent emit.
_LAST_UNREADABLE_EMITTED: dict[str, float] = {}


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


def load_paused_state(config: Any) -> MarkerState:
    """Read the pause marker and return its discriminated state.

    Distinguishes "no marker" from "marker exists but can't be read",
    so callers that gate destructive recovery (loops 1-3 wired in PR
    ``feat/sessions-pause-marker-wire-loops-1-3``) can fail closed
    rather than restart a session the operator intended to keep
    quiesced (PR #2081 round 2, Codex finding 2).

    * No ``base_dir`` on config → ``MarkerState.absent`` (treated by
      :func:`is_paused` as "nothing paused"). This matches the existing
      ``None`` path in :func:`_pause_marker_path` — a partially-loaded
      config can't have intentionally paused anything.
    * File missing → ``MarkerState.absent``.
    * File parses to a JSON list of strings → ``MarkerState.ok(names)``.
    * Anything else (``OSError`` from read, ``ValueError`` from JSON,
      wrong document shape) → ``MarkerState.unreadable(reason)``. We
      emit a throttled audit event + stderr warning the first time we
      see this so the operator notices.
    """
    path = _pause_marker_path(config)
    if path is None:
        state = MarkerState.absent()
        _record_marker_kind_transition(config, state)
        return state
    if not path.exists():
        state = MarkerState.absent()
        _record_marker_kind_transition(config, state)
        return state
    try:
        raw = path.read_text()
    except OSError as exc:
        reason = f"read failed: {exc!s}"
        logger.debug("pause marker read failed: %s", path, exc_info=True)
        state = MarkerState.unreadable(reason)
        _record_marker_kind_transition(config, state)
        return state
    try:
        data = json.loads(raw)
    except ValueError as exc:
        reason = f"malformed JSON: {exc!s}"
        logger.debug("pause marker parse failed: %s", path, exc_info=True)
        state = MarkerState.unreadable(reason)
        _record_marker_kind_transition(config, state)
        return state
    if not isinstance(data, list):
        reason = f"unexpected document shape: {type(data).__name__}"
        state = MarkerState.unreadable(reason)
        _record_marker_kind_transition(config, state)
        return state
    names = {str(name) for name in data if isinstance(name, str)}
    state = MarkerState.ok(names)
    _record_marker_kind_transition(config, state)
    return state


def load_paused_names(config: Any) -> set[str]:
    """Read the pause marker; empty set on missing / malformed file.

    Best-effort variant retained for the sessions-admin GET surface
    and the route's read-modify-write paths. An unreadable marker
    collapses to an empty set HERE — recovery callers must use
    :func:`load_paused_state` / :func:`is_paused` instead so they
    fail closed (treat unreadable as "everything paused") rather than
    fail open (restart what the operator wanted paused).
    """
    state = load_paused_state(config)
    if state.kind == "ok":
        return set(state.names)
    return set()


def is_paused(config: Any, session_name: str) -> bool:
    """Return True iff ``session_name`` is currently paused.

    Fail-closed semantics (PR #2081 round 2, Codex finding 2):

    * ``MarkerState.absent`` → False.
    * ``MarkerState.ok`` → ``session_name in state.names``.
    * ``MarkerState.unreadable`` → True for ANY session. A corrupt or
      permission-broken marker MUST NOT allow recovery loops to
      restart a session the operator intended to keep paused. The
      reader emits a throttled ``session.pause.marker_unreadable``
      audit event + stderr warning on the first occurrence so the
      operator notices and repairs the marker.
    """
    if not session_name:
        return False
    state = load_paused_state(config)
    if state.kind == "absent":
        return False
    if state.kind == "ok":
        return session_name in state.names
    # ``unreadable`` — fail closed: every session is treated as paused.
    return True


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


def skip_if_paused(
    config: Any,
    session_name: str,
    *,
    store: Any | None = None,
    loop: str = "",
    reason: str = "",
) -> bool:
    """Return True (and conditionally emit an audit event) when ``session_name``
    is paused.

    ``store`` — when supplied, the helper emits a ``session.pause.skip``
    event via ``store.record_event(scope, sender, subject)`` so the
    cockpit / audit log records that the loop honoured the marker
    rather than silently dropping the call. Best-effort: any store
    error is swallowed so a flaky event-write can't unblock the guard.

    ``loop`` / ``reason`` — free-form context used to build the audit
    subject. ``loop`` is the calling site (e.g. ``"supervisor.maybe_recover_session"``,
    ``"no_session_spawn.auto_recover"``) so an operator can correlate
    the skip with the loop that yielded.

    Audit-event throttling (PR #2081 round 2, Codex finding 1) —
    Repeated calls for the same ``(session_name, loop)`` within
    :data:`PAUSE_SKIP_THROTTLE_SECONDS` (5 min) emit AT MOST one
    durable audit event; subsequent calls return True without
    touching the store. The boolean guard return is NOT throttled
    — the recovery loop must still yield on every tick. Without the
    throttle a paused-but-unhealthy session would pile up one audit
    row per sweep tick forever.

    Callers that don't want audit emission (e.g. read-side helpers) can
    omit ``store`` and use this purely as a boolean guard.
    """
    if not is_paused(config, session_name):
        return False
    if store is not None and _should_emit_skip(session_name, loop):
        _emit_pause_skip(store, session_name, loop=loop, reason=reason)
    return True


def _should_emit_skip(session_name: str, loop: str) -> bool:
    """Return True iff we are outside the per-(session, loop) throttle.

    Updates the last-emit timestamp eagerly under a process-wide lock
    so two threads hitting the same key in the same tick can only race
    one emission through. We use ``None`` rather than ``0.0`` as the
    "never emitted" sentinel because :func:`time.monotonic` is allowed
    to start at an arbitrary low value (a fresh Python process on
    macOS often returns < 1.0 from monotonic at startup) which would
    otherwise look like "we just emitted" and silently swallow the
    first call.
    """
    key = (session_name, loop or "unknown_loop")
    now = time.monotonic()
    with _SKIP_THROTTLE_LOCK:
        last = _PAUSE_SKIP_LAST_EMITTED.get(key)
        if last is not None and now - last < PAUSE_SKIP_THROTTLE_SECONDS:
            return False
        _PAUSE_SKIP_LAST_EMITTED[key] = now
    return True


def _reset_skip_throttle_for_tests() -> None:
    """Clear the throttle state — test-only helper.

    Production code MUST NOT call this. The tests for repeated-tick
    behaviour rely on a deterministic starting point.
    """
    with _SKIP_THROTTLE_LOCK:
        _PAUSE_SKIP_LAST_EMITTED.clear()
    with _MARKER_STATE_LOCK:
        _LAST_MARKER_KIND.clear()
        _LAST_UNREADABLE_EMITTED.clear()


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


# --- Marker-state transition diagnostics ----------------------------


def _marker_state_key(config: Any) -> str:
    """Return a per-marker-path key for transition tracking.

    Includes the project's ``base_dir`` so two configs that share a
    process (multi-project daemons) don't cross-contaminate each
    other's "have we already warned" state.
    """
    path = _pause_marker_path(config)
    if path is None:
        return "<no_base_dir>"
    return str(path)


def _record_marker_kind_transition(config: Any, state: MarkerState) -> None:
    """Detect ``unreadable`` transitions and emit one-time diagnostics.

    Called from :func:`load_paused_state` on every read. We track the
    previously-observed kind per marker path:

    * unreadable for the FIRST time, or after being readable → log a
      stderr warning + emit a throttled
      ``session.pause.marker_unreadable`` audit event. The operator
      needs to know the recovery loops are failing closed.
    * unreadable -> readable (ok/absent) → emit
      ``session.pause.marker_restored`` (one-shot, not throttled —
      transitions are rare and informative).
    """
    key = _marker_state_key(config)
    kind = state.kind
    with _MARKER_STATE_LOCK:
        prior = _LAST_MARKER_KIND.get(key)
        _LAST_MARKER_KIND[key] = kind
        if kind == "unreadable":
            last_emit = _LAST_UNREADABLE_EMITTED.get(key)
            now = time.monotonic()
            became_unreadable = prior != "unreadable"
            within_throttle = (
                last_emit is not None
                and now - last_emit < PAUSE_MARKER_UNREADABLE_THROTTLE_SECONDS
            )
            if not became_unreadable and within_throttle:
                return
            _LAST_UNREADABLE_EMITTED[key] = now
            should_emit = True
            should_warn = became_unreadable
        else:
            should_emit = prior == "unreadable"
            should_warn = False
            # Successful read resets the unreadable-emit window so the
            # next failure re-emits even if it lands inside the prior
            # throttle.
            _LAST_UNREADABLE_EMITTED.pop(key, None)
    if should_warn:
        # Stderr is the only place the operator reliably sees this
        # without the cockpit attached. Best-effort: a closed stderr
        # in a test harness must not crash the read.
        try:
            print(
                f"pollypm: pause marker at {key} is unreadable "
                f"({state.reason}); recovery loops will fail CLOSED "
                f"(treat all sessions as paused) until repaired.",
                file=sys.stderr,
            )
        except Exception:  # noqa: BLE001
            logger.debug("pause marker stderr warn failed", exc_info=True)
    if should_emit and kind == "unreadable":
        _emit_marker_diagnostic(
            config,
            event=PAUSE_MARKER_UNREADABLE_EVENT_TYPE,
            subject=(
                f"pause marker unreadable at {key} ({state.reason}); "
                "recovery loops failing closed"
            ),
            status="warn",
            metadata={"path": key, "reason": state.reason},
        )
    elif should_emit:
        _emit_marker_diagnostic(
            config,
            event=PAUSE_MARKER_RESTORED_EVENT_TYPE,
            subject=(
                f"pause marker readable again at {key} "
                f"(kind={kind}); recovery loops resumed normal gating"
            ),
            status="ok",
            metadata={"path": key, "kind": kind},
        )


def _emit_marker_diagnostic(
    config: Any,
    *,
    event: str,
    subject: str,
    status: str,
    metadata: dict[str, Any],
) -> None:
    """Route a marker-state diagnostic through the canonical audit facade.

    Codex PR #2081 round 3 — finding 1: the ad-hoc writer used to
    append a non-canonical ``{ts: float, event_type, subject, payload}``
    record directly to ``<base_dir>/audit.jsonl``, bypassing
    :func:`pollypm.audit.log.emit` and its
    ``{schema, ts (ISO), project, event, subject, actor, status,
    metadata}`` shape. Anything that grepped audit events by the
    canonical ``event`` field never saw these diagnostics.

    The marker reader is called from many code paths (recovery loops,
    GET surface, supervisor) — most of which don't carry a store
    handle, so we go through the audit facade rather than threading
    one in. The facade writes both the per-project log and the
    central tail, picking up rotation / path-resolution / central-
    mirror behaviour for free.

    Best-effort: ``audit.emit`` already swallows its own IO failures,
    and we belt-and-suspender around an unexpected import / config
    failure so a broken audit subsystem can't crash the reader.
    """
    try:
        from pollypm.audit import emit as _audit_emit
    except Exception:  # noqa: BLE001 — never crash the reader on import
        logger.debug(
            "pause marker diagnostic %s import failed", event,
            exc_info=True,
        )
        return

    project_key = ""
    project_path: Path | None = None
    project_obj = getattr(config, "project", None)
    if project_obj is not None:
        project_key = str(getattr(project_obj, "name", "") or "")
        root = getattr(project_obj, "root_dir", None)
        if root is not None:
            project_path = Path(root)

    try:
        _audit_emit(
            event=event,
            project=project_key,
            subject=subject,
            actor="system",
            status=status,
            metadata=metadata,
            project_path=project_path,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "pause marker diagnostic %s emit failed", event,
            exc_info=True,
        )


__all__ = [
    "MarkerState",
    "PAUSE_MARKER_RESTORED_EVENT_TYPE",
    "PAUSE_MARKER_UNREADABLE_EVENT_TYPE",
    "PAUSE_MARKER_UNREADABLE_THROTTLE_SECONDS",
    "PAUSE_SKIP_EVENT_TYPE",
    "PAUSE_SKIP_THROTTLE_SECONDS",
    "is_paused",
    "load_paused_names",
    "load_paused_state",
    "pause_marker_lock",
    "save_paused_names",
    "skip_if_paused",
]
