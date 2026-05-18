"""Per-heuristic unit tests for ``classify_legacy`` (#1570).

Locks in the spec order so a refactor cannot silently shuffle
priorities. Each heuristic gets a positive case (the canonical
shape) and a negative case (a row that looks similar but should
not match), plus a top-level test that an unmatched row returns
``None`` so the CLI's "leave it legacy" branch keeps working.
"""

from __future__ import annotations

import pytest

from pollypm.inbox.backfill_heuristics import (
    Classification,
    classify_legacy,
)
from pollypm.inbox.kind import InboxItemKind


# ---------------------------------------------------------------------------
# 1. completion_fyi — title contains complete / done / resubmitted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Tier-4 cascade Complete",
        "Web API phase 1 complete",
        "Done: morning test sweep",
        "PR resubmitted after rebase",
        "RESUBMITTED to fix conflict",
    ],
)
def test_completion_titles_classify_completion_fyi(title: str) -> None:
    out = classify_legacy(title=title, sender="polly", project="demo")
    assert out is not None
    assert out.kind is InboxItemKind.COMPLETION_FYI
    assert "title_contains:" in out.heuristic


def test_completion_token_must_be_present() -> None:
    """A title without completion tokens should not hit the rule."""
    out = classify_legacy(
        title="Please review the new spec draft",
        sender="polly",
        project="demo",
    )
    assert out is None or out.kind is not InboxItemKind.COMPLETION_FYI


# ---------------------------------------------------------------------------
# 2. self_bug_report — title contains misrouted / bogus / proj/1 / repeated stale
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Misrouted Polly notification",
        "Bogus task surfaced again",
        "Heartbeat fired against proj/1 (no such project)",
        "Repeated stale review ping (3rd this hour)",
    ],
)
def test_self_bug_report_titles_classify_self_bug_report(title: str) -> None:
    out = classify_legacy(title=title, sender="polly", project="demo")
    assert out is not None
    assert out.kind is InboxItemKind.SELF_BUG_REPORT


def test_self_bug_report_does_not_swallow_unrelated_titles() -> None:
    out = classify_legacy(
        title="New approval request from architect",
        sender="polly",
        project="demo",
    )
    assert out is None or out.kind is not InboxItemKind.SELF_BUG_REPORT


# ---------------------------------------------------------------------------
# 3. plan_review_pending — title starts with "Plan ready for review"
# ---------------------------------------------------------------------------


def test_plan_ready_for_review_prefix_classifies_plan_review_pending() -> None:
    out = classify_legacy(
        title="Plan ready for review — demo/12",
        sender="architect",
        project="demo",
    )
    assert out is not None
    assert out.kind is InboxItemKind.PLAN_REVIEW_PENDING


def test_plan_ready_for_review_must_be_a_prefix() -> None:
    """A row that merely mentions plan-review mid-string shouldn't match."""
    out = classify_legacy(
        title="Reminder: plan ready for review was sent yesterday",
        sender="polly",
        project="demo",
    )
    assert out is None or out.kind is not InboxItemKind.PLAN_REVIEW_PENDING


# ---------------------------------------------------------------------------
# 4. manual_decision — Polly-authored digest into the inbox project
# ---------------------------------------------------------------------------


def test_polly_digest_in_inbox_classifies_manual_decision() -> None:
    out = classify_legacy(
        title="Digest: 7 open findings need your call",
        sender="polly",
        project="inbox",
    )
    assert out is not None
    assert out.kind is InboxItemKind.MANUAL_DECISION


def test_polly_digest_must_be_in_inbox_project() -> None:
    """Same title + sender but a real project key — not a manual_decision."""
    out = classify_legacy(
        title="Digest: 7 open findings need your call",
        sender="polly",
        project="savethenovel",
    )
    assert out is None or out.kind is not InboxItemKind.MANUAL_DECISION


def test_polly_digest_must_come_from_polly() -> None:
    out = classify_legacy(
        title="Digest: 7 open findings need your call",
        sender="audit_watchdog",
        project="inbox",
    )
    assert out is None or out.kind is not InboxItemKind.MANUAL_DECISION


# ---------------------------------------------------------------------------
# 5. watchdog_operator_dispatch — audit_watchdog sender + Action in title
# ---------------------------------------------------------------------------


def test_audit_watchdog_action_classifies_watchdog_operator_dispatch() -> None:
    out = classify_legacy(
        title="[Action] queue_without_motion needs review",
        sender="audit_watchdog",
        project="demo",
    )
    assert out is not None
    assert out.kind is InboxItemKind.WATCHDOG_OPERATOR_DISPATCH


def test_audit_watchdog_without_action_does_not_classify() -> None:
    out = classify_legacy(
        title="Heartbeat tick at 12:30",
        sender="audit_watchdog",
        project="demo",
    )
    assert out is None


def test_action_title_without_audit_watchdog_sender_does_not_classify() -> None:
    """Other senders writing 'Action' must not be misattributed."""
    out = classify_legacy(
        title="Action item: refresh credentials",
        sender="polly",
        project="demo",
    )
    # Polly's "Action" message must not be tagged as watchdog dispatch.
    if out is not None:
        assert out.kind is not InboxItemKind.WATCHDOG_OPERATOR_DISPATCH


# ---------------------------------------------------------------------------
# Unmatched rows
# ---------------------------------------------------------------------------


def test_unmatched_row_returns_none() -> None:
    out = classify_legacy(
        title="Random one-off note from a worker",
        sender="worker-7",
        project="demo",
    )
    assert out is None


def test_empty_inputs_return_none() -> None:
    assert classify_legacy(title="", sender="", project="") is None


# ---------------------------------------------------------------------------
# Spec order — first matching rule wins
# ---------------------------------------------------------------------------


def test_completion_wins_over_self_bug_report_when_both_match() -> None:
    """Order matters — completion wins because it precedes self-bug-report."""
    out = classify_legacy(
        title="proj/1 task complete (bogus row)",
        sender="polly",
        project="demo",
    )
    assert out is not None
    assert out.kind is InboxItemKind.COMPLETION_FYI


def test_self_bug_report_wins_over_plan_review_prefix() -> None:
    """A bogus-tagged Plan-ready row should still be flagged as bug noise."""
    out = classify_legacy(
        title="bogus Plan ready for review (mis-routed)",
        sender="architect",
        project="demo",
    )
    assert out is not None
    assert out.kind is InboxItemKind.SELF_BUG_REPORT


# ---------------------------------------------------------------------------
# Return shape
# ---------------------------------------------------------------------------


def test_classification_carries_heuristic_label() -> None:
    out = classify_legacy(
        title="Plan ready for review — alpha/3",
        sender="architect",
        project="alpha",
    )
    assert isinstance(out, Classification)
    assert out.heuristic
    assert out.kind is InboxItemKind.PLAN_REVIEW_PENDING
