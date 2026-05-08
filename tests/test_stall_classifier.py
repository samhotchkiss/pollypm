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


def test_recently_nudged_worker_is_transient_not_stalled() -> None:
    """#765 — when the heartbeat just nudged a worker, the next stable
    snapshot is the model digesting the nudge. Defer remediation one
    tick instead of escalating to ``suspected_loop`` immediately."""
    assert (
        classify_stall(_ctx(role="worker", has_pending_work=True, recently_nudged=True))
        == "transient"
    )


def test_turn_in_flight_worker_is_transient_not_stalled() -> None:
    """A long thinking pause looks like a stable snapshot — a stall
    detection during an active turn is a false positive."""
    assert (
        classify_stall(_ctx(role="worker", has_pending_work=True, turn_in_flight=True))
        == "transient"
    )


def test_transient_signals_dont_apply_when_user_gate_holds() -> None:
    """``awaiting_user_action`` wins over the transient signals — we're
    waiting on the user, period; the worker is legitimately idle."""
    assert (
        classify_stall(_ctx(
            role="worker", has_pending_work=True,
            awaiting_user_action=True, recently_nudged=True,
        ))
        == "legitimate_idle"
    )


def test_awaiting_operator_question_promotes_to_awaiting_operator() -> None:
    """When the agent's last turn ends with a question to the operator
    (pane sitting at empty ``❯`` prompt), classify_stall must return
    ``awaiting_operator`` so the heartbeat surfaces the question to
    the operator's inbox — instead of letting the placeholder short-
    circuit silently file the pane as ``legitimate_idle``.
    """
    ctx = _ctx(
        role="worker",
        has_pending_work=True,
        pane_is_idle_placeholder=True,
        awaiting_operator_question=(
            "Ready to proceed when you give the nod — "
            "anything you want to steer first?"
        ),
    )
    assert classify_stall(ctx) == "awaiting_operator"


def test_awaiting_operator_takes_priority_over_legitimate_idle_paths() -> None:
    """Even an architect or a control-name session with a pending
    question must surface — the question signal beats the role-based
    legitimate_idle promotion. Architects asking the user a question
    is exactly the kind of thing the operator needs to see.
    """
    architect_ctx = _ctx(
        role="architect",
        has_pending_work=True,
        pane_is_idle_placeholder=True,
        awaiting_operator_question="Should I include venue X in scope?",
    )
    assert classify_stall(architect_ctx) == "awaiting_operator"

    pollypm_worker_ctx = _ctx(
        role="worker",
        session_name="worker_pollypm",
        has_pending_work=True,
        pane_is_idle_placeholder=True,
        awaiting_operator_question="Ready to proceed?",
    )
    assert classify_stall(pollypm_worker_ctx) == "awaiting_operator"


def test_no_question_preserves_legitimate_idle_behavior() -> None:
    """Regression guard: placeholder + no question text must still
    classify as ``legitimate_idle``. The new awaiting_operator branch
    fires only when the question text is present.
    """
    ctx = _ctx(
        role="worker",
        has_pending_work=True,
        pane_is_idle_placeholder=True,
        awaiting_operator_question=None,
    )
    assert classify_stall(ctx) == "legitimate_idle"


def test_pane_ends_with_unanswered_question_detects_coffeeboardnm_pattern() -> None:
    """The exact regression: coffeeboardnm worker emitted a "Ready to
    proceed when you give the nod — anything you want to steer first?"
    turn and sat at the empty ``❯`` prompt. Detector must return the
    question text so the heartbeat can surface it.
    """
    from pollypm.idle_placeholders import pane_ends_with_unanswered_question

    pane_text = "\n".join([
        "⏺ Hi — settled in.",
        "",
        "  Where I am: worktree coffeeboardnm-1, task implement node.",
        "",
        "  Plan, roughly: read SKILL → research NM venues → outline → render.",
        "",
        "  Ready to proceed when you give the nod — anything you want "
        "to steer first (scope, depth, visual style, \"skip section X\")?",
        "",
        "✻ Cogitated for 1m 34s",
        "",
        "──────────────────────────────────────────────────────",
        "❯ ",
        "──────────────────────────────────────────────────────",
        "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
    ])

    result = pane_ends_with_unanswered_question(pane_text)
    assert result is not None
    assert "ready to proceed" in result.lower()
    assert "?" in result


def test_pane_ends_with_unanswered_question_none_for_normal_idle() -> None:
    """An idle empty prompt with no recent question pattern (e.g., the
    worker just finished a task and is awaiting next-task input)
    must NOT trigger awaiting_operator.
    """
    from pollypm.idle_placeholders import pane_ends_with_unanswered_question

    pane_text = "\n".join([
        "⏺ Task coffeeboardnm/1 complete. Deployed to coffeeboardnm.itsalive.co.",
        "",
        "✻ Cooked for 3m 12s",
        "",
        "──────────────────────────────────────────────────────",
        "❯ ",
        "──────────────────────────────────────────────────────",
        "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
    ])

    assert pane_ends_with_unanswered_question(pane_text) is None


def test_pane_ends_with_unanswered_question_skips_when_already_answered() -> None:
    """If a user-input line (``❯ <text>``) sits between the question
    and the prompt, the operator already answered — don't re-surface.
    """
    from pollypm.idle_placeholders import pane_ends_with_unanswered_question

    pane_text = "\n".join([
        "⏺ Should I proceed?",
        "",
        "✻ Cogitated for 12s",
        "",
        "❯ go ahead",
        "",
        "──────────────────────────────────────────────────────",
        "❯ ",
        "──────────────────────────────────────────────────────",
        "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
    ])

    assert pane_ends_with_unanswered_question(pane_text) is None


def test_pane_ends_with_unanswered_question_none_when_pane_is_mid_turn() -> None:
    """If the pane shows a status spinner / no empty prompt at the
    tail, the agent is mid-turn — not awaiting an answer.
    """
    from pollypm.idle_placeholders import pane_ends_with_unanswered_question

    pane_text = "\n".join([
        "⏺ Should I proceed?",
        "",
        "⏺ Bash(pm task get coffeeboardnm/1)",
        "  ⎿  ID: coffeeboardnm/1 — POC plan",
        "",
        "✶ Cooking… (12s · ↓ 1.2k tokens · still thinking)",
    ])

    assert pane_ends_with_unanswered_question(pane_text) is None


def test_alert_channel_classifies_three_tiers() -> None:
    """#765 — public alert-channel policy. Operational alerts never
    earn a toast; informational alerts don't either; only
    action-required does. The toast renderer routes through this so
    every emitter goes through one classifier."""
    from pollypm.cockpit_alerts import (
        AlertChannel,
        alert_channel,
        alert_should_toast,
    )

    # Operational: heartbeat-internal noise.
    assert alert_channel("suspected_loop") is AlertChannel.OPERATIONAL
    assert alert_channel("missing_window") is AlertChannel.OPERATIONAL
    assert not alert_should_toast("suspected_loop")

    # Action-required: anything not in the operational/informational
    # buckets. ``auth_broken`` is a real user-actionable failure.
    assert alert_channel("auth_broken") is AlertChannel.ACTION_REQUIRED
    assert alert_should_toast("auth_broken")
