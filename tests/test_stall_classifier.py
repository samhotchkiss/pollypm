"""Tests for the #765 stall classifier.

The classifier decides which same-snapshot detections from the
heartbeat earn a user-facing alert. The policy: anything the user
can't act on must stay silent — only a genuine, actionable stall
becomes an alert.

Regression targets tonight's reports:
- The Notesy architect pane sitting after emit, awaiting plan
  approval, should NOT trigger a stall alert.
- A worker with no queued work should NOT trigger an alert.
- A worker with queued work and no user-gate IS an actionable stall.
"""

from __future__ import annotations

from pollypm.heartbeats.stall_classifier import StallContext, classify_stall


def _ctx(**kwargs) -> StallContext:
    defaults = dict(
        role="worker",
        session_name="sess",
        has_pending_work=False,
        awaiting_user_action=False,
    )
    defaults.update(kwargs)
    return StallContext(**defaults)


def test_event_driven_roles_are_always_legitimate_idle() -> None:
    for role in ("heartbeat-supervisor", "operator-pm", "reviewer"):
        assert classify_stall(_ctx(role=role, has_pending_work=True)) == "legitimate_idle"


def test_control_session_names_are_always_legitimate_idle() -> None:
    assert (
        classify_stall(_ctx(role="worker", session_name="worker_pollypm", has_pending_work=True))
        == "legitimate_idle"
    )


def test_architect_with_no_pending_work_is_legitimate_idle() -> None:
    """The Notesy regression: architect just emitted the plan, is
    waiting for user approval. Pane sits quiet — must not toast."""
    assert classify_stall(_ctx(role="architect", has_pending_work=False)) == "legitimate_idle"


def test_architect_is_always_legitimate_idle_regardless_of_queue() -> None:
    """Architects are event-driven: emit, then wait for the user. The
    project having downstream queued tasks is normal (the architect's
    own output) — it doesn't mean the architect is stalled. Morning-
    after #765 refinement: stop toasting the user on architect idle
    just because the worker queue is non-empty."""
    assert (
        classify_stall(_ctx(role="architect", has_pending_work=True))
        == "legitimate_idle"
    )
    assert (
        classify_stall(_ctx(role="architect", has_pending_work=False))
        == "legitimate_idle"
    )


def test_worker_without_pending_work_is_legitimate_idle() -> None:
    assert classify_stall(_ctx(role="worker", has_pending_work=False)) == "legitimate_idle"


def test_worker_awaiting_user_action_is_legitimate_idle() -> None:
    """A worker parked at a review gate is waiting on us, not stalled."""
    assert (
        classify_stall(_ctx(role="worker", has_pending_work=True, awaiting_user_action=True))
        == "legitimate_idle"
    )


def test_worker_with_pending_work_and_no_user_gate_is_unrecoverable_stall() -> None:
    assert (
        classify_stall(_ctx(role="worker", has_pending_work=True, awaiting_user_action=False))
        == "unrecoverable_stall"
    )


def test_unknown_role_defaults_to_legitimate_idle() -> None:
    """Conservative: unknown roles get silence, not alerts. Rather
    miss a stall than train the user to ignore warnings."""
    assert classify_stall(_ctx(role="polyglot", has_pending_work=True)) == "legitimate_idle"
