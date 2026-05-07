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
    #: ``True`` when the session has been nudged in the last cycle.
    #: Suppresses re-nudging while the previous nudge is still being
    #: digested — gives the model one more tick before the heartbeat
    #: escalates to ``unrecoverable_stall`` (#765).
    recently_nudged: bool = False
    #: ``True`` when the session is mid-turn (model is thinking /
    #: streaming) per the live transcript / pane heuristics. A stable
    #: snapshot during an active turn is normal — just a long thinking
    #: pause — so it gets the ``transient`` bucket instead of
    #: ``unrecoverable_stall``.
    turn_in_flight: bool = False
    #: ``True`` when the pane is showing a known idle-placeholder UI
    #: state — Codex's rotating ``› <suggestion>`` hint, or Claude's
    #: empty ``❯`` prompt. Same-snapshot detection on a placeholder
    #: pane is the alive-but-idle case (#1010): the agent is waiting
    #: for input, not stuck. Always classifies as ``legitimate_idle``,
    #: regardless of ``has_pending_work`` — work is queued behind the
    #: agent's input cycle, not behind a pane that needs unsticking.
    pane_is_idle_placeholder: bool = False


def classify_stall(ctx: StallContext) -> StallClass:
    """Classify a same-snapshot detection for ``ctx``.

    Three-tier policy (#765):

    - ``legitimate_idle`` — the session is supposed to be quiet.
      Event-driven roles (heartbeat / operator / reviewer), explicit
      control sessions, idle architects, workers with no queued work
      or with an awaiting-user gate. No alert; no remediation.
    - ``transient`` — the session is probably still working but the
      snapshot happens to look stable right now (the heartbeat just
      nudged it last cycle, the model is mid-turn). Defer remediation
      one tick before deciding. The heartbeat MUST NOT raise
      ``suspected_loop`` for transient classifications.
    - ``unrecoverable_stall`` — the session looks actually stuck.
      Worker with pending work, no awaiting-user gate, no in-flight
      turn, no recent nudge. The heartbeat starts the remediation
      ladder (nudge → triage → restart) without escalating to a toast
      until the ladder is exhausted.

    Conservative by design: when a case is ambiguous it errs toward
    ``legitimate_idle`` rather than ``unrecoverable_stall``. A missed
    real stall is a single delayed detection on the next heartbeat;
    a false-positive stall toast is user-trust damage that persists.
    """
    role = (ctx.role or "").strip()
    session_name = (ctx.session_name or "").strip()

    # #1010 — placeholder-showing pane is alive-but-idle. The Codex
    # rotating-suggestion hint and the Claude empty-``❯`` prompt are
    # both "agent is waiting for input" signals, not stalls. Same-
    # snapshot ticks on these panes are baseline (the placeholder
    # text doesn't always rotate within a 3-tick window), so promote
    # to ``legitimate_idle`` before the role / pending-work ladder
    # can fire ``unrecoverable_stall``.
    if ctx.pane_is_idle_placeholder:
        return "legitimate_idle"

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
        return "legitimate_idle"
    if role == "worker":
        if ctx.awaiting_user_action:
            return "legitimate_idle"
        if not ctx.has_pending_work:
            return "legitimate_idle"
        # Worker has pending work and we'd otherwise classify this as
        # a stall. Two transient cases (#765):
        # 1. The heartbeat just nudged this session — give the model
        #    a tick to act on the nudge before declaring it stalled.
        # 2. The session is mid-turn — a stable snapshot during an
        #    active turn is just a long thinking pause.
        if ctx.recently_nudged or ctx.turn_in_flight:
            return "transient"
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
            from pollypm.work import create_work_service

            db_path = project_path / ".pollypm" / "state.db"
            if db_path.exists():
                with create_work_service(
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
