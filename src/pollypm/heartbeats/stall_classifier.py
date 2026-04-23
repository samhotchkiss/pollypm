"""Stall-class classifier for the heartbeat loop (#765).

The heartbeat's ``suspected_loop`` detector fires whenever a session's
pane snapshot is identical for three consecutive heartbeats. That
detection is cheap and broad; it catches real stalls, but it also
catches the dominant normal case — a control-plane session, an
architect waiting for plan approval, a worker with an empty queue —
where the pane is *supposed* to be quiet. Surfacing every one of those
to the user as a warning trains them to dismiss the warning, so when a
real stall arrives it's already tuned out.

This module separates detection from classification. The heartbeat
still runs its cheap equality check; on a hit, it hands the context
here. :func:`classify_stall` returns one of three classes:

- ``legitimate_idle``  — the session is doing exactly what the system
  wants it to do (waiting for approval, no queued work, etc.). No
  alert, no log warning, no user-visible signal.
- ``transient``        — the session is probably still working (recently
  nudged, model is known to take long turns, etc.). Give it another
  tick before deciding.
- ``unrecoverable_stall`` — the session looks actually stuck. Remediate
  (nudge / restart) before escalating to the user.

Only ``unrecoverable_stall`` is supposed to become an alert, and even
then via the remediation ladder in the heartbeat itself, not a direct
toast.

The classifier here is deliberately conservative: when a case is
ambiguous, it errs on ``legitimate_idle`` rather than ``unrecoverable_stall``.
A missed real stall is a single delayed detection on the next heartbeat;
a false-positive stall alert is user trust damage that persists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


StallClass = Literal["legitimate_idle", "transient", "unrecoverable_stall"]


# Roles whose tmux panes are expected to sit idle indefinitely. These
# sessions are event-driven — they only produce output when the user
# sends a message or an event fires — so equal snapshots are the
# baseline, not a signal.
_EVENT_DRIVEN_ROLES: frozenset[str] = frozenset({
    "heartbeat-supervisor",
    "operator-pm",
    "reviewer",
})

# Session names historically treated as control-plane (PollyPM's own
# self-dogfood worker). Kept as an explicit list so we can promote or
# demote specific sessions without widening the role matcher.
_CONTROL_SESSION_NAMES: frozenset[str] = frozenset({
    "worker_pollypm",
})


@dataclass(slots=True, frozen=True)
class StallContext:
    """Inputs to :func:`classify_stall`.

    Kept as a small dataclass rather than threading kwargs so the
    heartbeat can build it once per detection and tests can exercise
    the classifier without constructing a real ``HeartbeatSessionContext``.
    """

    role: str
    session_name: str
    has_pending_work: bool
    #: ``True`` when the session has an open inbox item that's awaiting
    #: the user (plan review, approval request, etc.). The architect
    #: case: just emitted a plan and is waiting for the user to approve.
    awaiting_user_action: bool = False


def classify_stall(ctx: StallContext) -> StallClass:
    """Classify a same-snapshot detection for ``ctx``.

    Policy:

    - Event-driven roles (heartbeat / operator / reviewer) → legitimate.
      These sessions legitimately idle when the user isn't interacting.
    - Explicit control session names → legitimate.
    - Architect with no pending work → legitimate. They emit, they
      stop. The next motion is user approval or a plan-replan command.
    - Worker with an open inbox item awaiting the user → legitimate.
      We're waiting on the user, not the session.
    - Worker with no pending work → legitimate. Queue is empty;
      silence is correct.
    - Worker with pending work AND no awaiting-user gate → this is a
      real stall signal. Return ``unrecoverable_stall`` so the heartbeat
      starts the remediation ladder.
    - Anything else → legitimate by default. We'd rather miss one real
      stall than train the user to ignore warnings.
    """
    role = (ctx.role or "").strip()
    session_name = (ctx.session_name or "").strip()

    if role in _EVENT_DRIVEN_ROLES:
        return "legitimate_idle"
    if session_name in _CONTROL_SESSION_NAMES:
        return "legitimate_idle"
    if role == "architect":
        # Architects are event-driven — they emit once, then wait for
        # the user (approval / replan). Downstream implementation tasks
        # being queued / in_progress does NOT mean the architect has
        # work to do; those belong to workers. So an idle architect is
        # always ``legitimate_idle`` from the classifier's perspective.
        # A real "architect stuck mid-plan" case would need richer
        # signals (e.g. the architect's own plan_project task is at a
        # non-terminal node) and doesn't belong in the same-snapshot
        # detector. See morning-after #765 follow-up.
        return "legitimate_idle"
    if role == "worker":
        if ctx.awaiting_user_action:
            return "legitimate_idle"
        if not ctx.has_pending_work:
            return "legitimate_idle"
        return "unrecoverable_stall"

    # Unknown role — stay conservative.
    return "legitimate_idle"


_WORKER_ACTIONABLE_STATUSES: frozenset[str] = frozenset(
    {"queued", "in_progress", "blocked"}
)


def has_pending_work_for_session(config, session_name: str) -> bool:
    """Best-effort check for queued/actionable work on a session's project.

    Used by the stall detector to decide whether an idle pane is
    ``legitimate_idle`` (no queue) or ``unrecoverable_stall`` (queue
    with no output). Mirrors the older helper in
    :mod:`pollypm.heartbeats.local` so both detection paths (the
    heartbeat-backend sweep and the supervisor-boundary sweep) share a
    single definition of "is there work".

    Returns ``True`` when in doubt — a false "yes" only means we run
    the same-snapshot check that next heartbeat; a false "no" would
    silence a real stall.
    """
    try:
        sessions = getattr(config, "sessions", None) or {}
        session = sessions.get(session_name)
        if session is None:
            return False
        project_key = getattr(session, "project", "") or ""
        if not project_key:
            return False
        projects = getattr(config, "projects", None) or {}
        project = projects.get(project_key)
        if project is None:
            return False
        project_path = getattr(project, "path", None)
        if project_path is None:
            return False

        try:
            from pollypm.task_backends import get_task_backend

            backend = get_task_backend(project_path)
            if backend.exists():
                file_tasks = backend.list_tasks(
                    states=["01-ready", "02-in-progress"],
                )
                if file_tasks:
                    return True
        except Exception:  # noqa: BLE001
            pass

        try:
            from pollypm.work.sqlite_service import SQLiteWorkService

            db_path = project_path / ".pollypm" / "state.db"
            if db_path.exists():
                with SQLiteWorkService(
                    db_path=db_path, project_path=project_path,
                ) as svc:
                    for task in svc.list_tasks(project=project_key):
                        status_value = getattr(task.work_status, "value", "")
                        if status_value in _WORKER_ACTIONABLE_STATUSES:
                            return True
        except Exception:  # noqa: BLE001
            pass

        return False
    except Exception:  # noqa: BLE001
        return True


__all__ = [
    "StallContext",
    "StallClass",
    "classify_stall",
    "has_pending_work_for_session",
]
