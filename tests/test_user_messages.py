"""Tests for the structured user-message contract (#760).

Every user-facing notification, CLI refusal, and inbox action card
goes through one shared shape: ``summary`` / ``why_it_matters`` /
``next_action`` / ``details``. This test file pins:

* The dataclass round-trips through ``as_dict``.
* CLI rendering matches the issue's "joy-maximizing" example
  (summary first, blank line, why, blank, ``Next:`` with the
  command, blank, collapsible details).
* Inbox-action-card rendering maps the four-field shape onto the
  keys the dashboard renderer reads.
* The hand-authored ``KNOWN_ERROR_CLASSES`` registry covers the
  classes the issue called out (migration gate, account expired,
  task lookup failure, plan rejected).
"""

from __future__ import annotations

from pollypm.user_messages import (
    KNOWN_ERROR_CLASSES,
    StructuredMessage,
    known_error,
    render_cli_message,
    render_inbox_action_card,
)


def test_structured_message_round_trips_through_as_dict() -> None:
    msg = StructuredMessage(
        summary="Cannot start — pending schema migrations on state.db.",
        why_it_matters="Your PollyPM was upgraded since last boot.",
        next_action="run   pm migrate --apply",
        details="schema diff: ...",
    )
    assert msg.as_dict() == {
        "summary": "Cannot start — pending schema migrations on state.db.",
        "why_it_matters": "Your PollyPM was upgraded since last boot.",
        "next_action": "run   pm migrate --apply",
        "details": "schema diff: ...",
    }


def test_render_cli_message_matches_joy_maximizing_layout() -> None:
    """Layout from the issue body — summary, why, Next, > details."""
    msg = StructuredMessage(
        summary="Cannot start — pending schema migrations on state.db.",
        why_it_matters="Database needs one small update before sessions can connect.",
        next_action="run   pm migrate --apply",
        details="schema diff (v5 → v6)\nDB path: ~/.pollypm/state.db",
    )
    rendered = render_cli_message(msg)
    lines = rendered.splitlines()
    # Summary is the first non-empty line, prefixed.
    assert lines[0] == "✗ Cannot start — pending schema migrations on state.db."
    # Why_it_matters appears between blanks.
    assert lines[1] == ""
    assert "small update" in lines[2]
    # Next action prefixed with "Next:".
    assert any(line.startswith("Next: run") for line in lines)
    # Details collapsed under "> details".
    assert "> details" in rendered
    assert "schema diff" in rendered


def test_render_cli_message_without_optional_fields_is_just_summary() -> None:
    msg = StructuredMessage(summary="Polly is fine.")
    assert render_cli_message(msg) == "✗ Polly is fine."


def test_render_inbox_action_card_maps_to_dashboard_keys() -> None:
    """The dashboard renders ``plain_prompt`` + ``unblock_steps`` +
    ``decision_question``. The mapping ensures the same four-field
    content lands on the inbox card."""
    msg = StructuredMessage(
        summary="A plan is ready for review.",
        why_it_matters="Workers can't claim until you approve.",
        next_action="Press A to approve, or D to discuss.",
        details="plan path: docs/project-plan.md",
    )
    card = render_inbox_action_card(msg)
    assert card["plain_prompt"] == "A plan is ready for review."
    assert card["why_it_matters"] == "Workers can't claim until you approve."
    assert card["unblock_steps"] == ["Press A to approve, or D to discuss."]
    assert card["details"] == "plan path: docs/project-plan.md"


def test_known_error_classes_cover_top_repeating_errors() -> None:
    """#760 lists migration gate, account expired, task lookup
    failure, plan rejected as the top recurring classes that need
    hand-authored copy. Pin them here so a future change can't drop
    them silently."""
    for name in (
        "migration_pending",
        "account_expired",
        "task_not_found",
        "plan_rejected",
    ):
        msg = known_error(name)
        assert msg is not None, f"missing top-N entry: {name}"
        assert msg.summary, f"{name} must have a non-empty summary"
        # Top-N entries must include the why + next action so the
        # CLI rendering is complete out of the box.
        assert msg.why_it_matters, f"{name} missing why_it_matters"
        assert msg.next_action, f"{name} missing next_action"


def test_known_error_lookup_returns_none_for_unknown_class() -> None:
    assert known_error("not-a-real-error-class") is None


def test_top_n_classes_render_through_cli_helper() -> None:
    """Every registered class must render cleanly through
    ``render_cli_message`` — no exceptions, summary in line 1."""
    for name, msg in KNOWN_ERROR_CLASSES.items():
        rendered = render_cli_message(msg)
        first_line = rendered.splitlines()[0]
        assert msg.summary in first_line, f"{name}: summary missing from CLI output"
