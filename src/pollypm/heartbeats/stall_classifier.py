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
        # Architects alternate emit/wait. An idle pane is the default
        # state between approvals. If the user hasn't approved yet, or
        # if there's nothing queued for the architect to do, silence is
        # expected.
        if not ctx.has_pending_work:
            return "legitimate_idle"
        # Pending work + idle could be a real stall — treat as such so
        # we at least get the remediation ladder, not silence.
        return "unrecoverable_stall"
    if role == "worker":
        if ctx.awaiting_user_action:
            return "legitimate_idle"
        if not ctx.has_pending_work:
            return "legitimate_idle"
        return "unrecoverable_stall"

    # Unknown role — stay conservative.
    return "legitimate_idle"


__all__ = ["StallContext", "StallClass", "classify_stall"]
