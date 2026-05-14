"""Unit tests for ``pollypm.supervision.alert_task_lookup`` (#1545).

Pins the contract that the supervisor's terminal-task alert sweep
(``Supervisor._sweep_stale_alerts``, #1545 extension) extracts
candidate task IDs correctly across every alert reference shape:

* ``stuck_on_task:<project>/<num>`` — task id in the alert_type suffix.
* ``no_session_for_assignment:<project>/<num>`` — same shape.
* ``review-<project>-<num>`` — task number in the session_name scope.
* ``plan_missing`` (scope: ``plan_gate-<project>``) — task id in the
  message body.
* ``no_session`` and other family alerts that don't reference a task —
  must yield no candidates so the sweep doesn't false-clear.
"""

from __future__ import annotations

from pollypm.supervision.alert_task_lookup import (
    extract_task_ids,
    project_key_from_task_id,
)


# ---------------------------------------------------------------------------
# Alert-type-suffix family
# ---------------------------------------------------------------------------


def test_extract_stuck_on_task_alert_type_returns_task_id() -> None:
    candidates = extract_task_ids(
        alert_type="stuck_on_task:coffeeboardnm/1",
        session_name="worker_coffeeboardnm",
    )
    assert candidates == ["coffeeboardnm/1"]


def test_extract_no_session_for_assignment_returns_task_id() -> None:
    candidates = extract_task_ids(
        alert_type="no_session_for_assignment:demo/42",
        session_name="worker-demo",
    )
    assert candidates == ["demo/42"]


def test_extract_handles_hyphenated_project_in_alert_type_suffix() -> None:
    """The alert_type carries the storage-name form of the project, which
    can include hyphens (``health-coach``) for projects whose display
    name and slug differ."""
    candidates = extract_task_ids(
        alert_type="stuck_on_task:health-coach/3",
        session_name="worker_health_coach",
    )
    assert candidates == ["health-coach/3"]


# ---------------------------------------------------------------------------
# session_name review scope
# ---------------------------------------------------------------------------


def test_extract_review_scope_returns_task_id() -> None:
    candidates = extract_task_ids(
        alert_type="review_pending",
        session_name="review-demo-7",
    )
    assert candidates == ["demo/7"]


def test_extract_review_scope_handles_hyphenated_project() -> None:
    candidates = extract_task_ids(
        alert_type="review_pending",
        session_name="review-health-coach-3",
    )
    # The regex is greedy on the project segment up to the trailing
    # ``-<num>`` — ``health-coach`` keeps both hyphens.
    assert candidates == ["health-coach/3"]


# ---------------------------------------------------------------------------
# plan_missing family — message-body parsing
# ---------------------------------------------------------------------------


def test_extract_plan_missing_pulls_task_from_message_body() -> None:
    """The plan_missing alert's emit copy embeds ``queued task <id> is
    waiting`` in the message body. The extractor pulls that task id
    so the sweep can detect terminal-status transitions even when the
    alert_type / session_name don't carry the task reference.
    """
    candidates = extract_task_ids(
        alert_type="plan_missing",
        session_name="plan_gate-coffeeboardnm",
        message=(
            "Project 'coffeeboardnm' has no approved plan yet — "
            "queued task coffeeboardnm/1 is waiting. "
            "Run `pm project plan coffeeboardnm` to queue planning."
        ),
    )
    assert candidates == ["coffeeboardnm/1"]


def test_extract_plan_missing_only_parses_message_for_known_types() -> None:
    """The message body is only parsed for alert types that are known
    to embed a task id there (currently just ``plan_missing``). An
    unrelated alert type with a task id in its message should NOT
    yield candidates — that protects against false clears when
    operators write freeform alert copy mentioning a task as context.
    """
    candidates = extract_task_ids(
        alert_type="some_other_alert",
        session_name="some-session",
        message="See task coffeeboardnm/1 for context.",
    )
    assert candidates == []


# ---------------------------------------------------------------------------
# Negative cases — alerts without a task reference
# ---------------------------------------------------------------------------


def test_extract_no_session_alert_yields_no_candidates() -> None:
    """``no_session`` alerts are project-keyed (``worker-<project>`` /
    ``architect-<project>`` scope) but don't carry a task id — the
    queued-task sweep is what re-emits the per-task variant. The
    extractor must NOT invent a candidate from the project segment.
    """
    candidates = extract_task_ids(
        alert_type="no_session",
        session_name="worker-coffeeboardnm",
    )
    assert candidates == []


def test_extract_returns_empty_for_unrecognized_shape() -> None:
    candidates = extract_task_ids(
        alert_type="recovery_limit",
        session_name="worker_demo",
    )
    assert candidates == []


def test_extract_handles_empty_inputs() -> None:
    assert extract_task_ids(alert_type="", session_name="") == []
    assert extract_task_ids(
        alert_type="plan_missing", session_name="plan_gate-demo", message="",
    ) == []


# ---------------------------------------------------------------------------
# Idempotence + dedupe
# ---------------------------------------------------------------------------


def test_extract_dedupes_when_task_appears_in_multiple_locations() -> None:
    """If the alert message body re-mentions the same task id that
    appears in the alert_type suffix, the extractor returns the id
    once. Idempotence: repeat calls return the same ordered list."""
    candidates_a = extract_task_ids(
        alert_type="stuck_on_task:demo/3",
        session_name="worker-demo",
        message="Task demo/3 has been stuck for 30 minutes.",
    )
    candidates_b = extract_task_ids(
        alert_type="stuck_on_task:demo/3",
        session_name="worker-demo",
        message="Task demo/3 has been stuck for 30 minutes.",
    )
    # Same call returns same ordered list.
    assert candidates_a == candidates_b
    # Body parsing is gated on alert type, so the body's mention of
    # demo/3 doesn't add a duplicate (it isn't even read for
    # stuck_on_task) — the result is just the alert_type suffix id.
    assert candidates_a == ["demo/3"]


# ---------------------------------------------------------------------------
# Punctuation robustness — alert message bodies often wrap the id in
# trailing punctuation.
# ---------------------------------------------------------------------------


def test_extract_strips_trailing_punctuation_in_message_body() -> None:
    candidates = extract_task_ids(
        alert_type="plan_missing",
        session_name="plan_gate-demo",
        message="queued task demo/5, is waiting.",
    )
    assert candidates == ["demo/5"]


# ---------------------------------------------------------------------------
# project_key_from_task_id
# ---------------------------------------------------------------------------


def test_project_key_from_task_id_simple() -> None:
    assert project_key_from_task_id("coffeeboardnm/1") == "coffeeboardnm"


def test_project_key_from_task_id_handles_hyphens() -> None:
    assert project_key_from_task_id("health-coach/3") == "health-coach"


def test_project_key_from_task_id_returns_empty_for_invalid() -> None:
    assert project_key_from_task_id("") == ""
    assert project_key_from_task_id("nopath") == ""
