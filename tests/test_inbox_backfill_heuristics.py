"""Per-heuristic unit tests for ``classify_legacy`` (#1570, #1564 follow-up).

Locks in the spec order so a refactor cannot silently shuffle
priorities. Each heuristic gets a positive case (the canonical
shape) and a negative case (a row that looks similar but should
not match), plus a top-level test that an unmatched row returns
``None`` so the CLI's "leave it legacy" branch keeps working.

Two surfaces:

* :func:`classify_legacy` — messages-shaped rows (#1570). The first
  six sections cover its heuristics.
* :func:`classify_legacy_task` — work_tasks-shaped rows added in the
  #1564 follow-up so the dashboard's "Watchdog escalated: …" task
  rows can be retagged off ``legacy``.
"""

from __future__ import annotations

import pytest

from pollypm.inbox.backfill_heuristics import (
    Classification,
    classify_legacy,
    classify_legacy_task,
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


# ---------------------------------------------------------------------------
# classify_legacy_task — work_tasks-shaped rows (#1564 follow-up)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        # Verbatim watchdog finding shapes observed in the user's
        # 2026-05-17 dashboard (queue_without_motion safety-net probe).
        (
            "Project savethenovel has 2 queued task(s) but no claim / "
            "execution / status-change activity for the entire scan window."
        ),
        (
            "Project coffeeboardnm has 4 queued task(s) but no claim / "
            "execution / status-change activity for ~31 min."
        ),
        (
            "Project pollypm has 1 queued task(s) but no claim / "
            "execution / status-change activity for the entire scan window."
        ),
    ],
)
def test_watchdog_queue_without_motion_task_classifies_dispatch(
    title: str,
) -> None:
    out = classify_legacy_task(title=title, created_by="audit_watchdog")
    assert out is not None
    assert out.kind is InboxItemKind.WATCHDOG_OPERATOR_DISPATCH
    assert out.heuristic == "watchdog_queue_without_motion"


def test_watchdog_escalated_prefix_classifies_dispatch() -> None:
    """Defensive: any future producer using the cockpit's render copy is caught."""
    out = classify_legacy_task(
        title="Watchdog escalated: queue wedged on demo",
        created_by="audit_watchdog",
    )
    assert out is not None
    assert out.kind is InboxItemKind.WATCHDOG_OPERATOR_DISPATCH
    assert out.heuristic == "watchdog_escalated_prefix"


def test_queue_motion_pattern_only_matches_audit_watchdog_creator() -> None:
    """Same title prose from a non-watchdog creator must not be misattributed."""
    out = classify_legacy_task(
        title=(
            "Project demo has 3 queued task(s) but no claim / "
            "execution / status-change activity for ~5 min."
        ),
        created_by="polly",
    )
    assert out is None


def test_plan_ready_for_review_task_classifies_plan_review_pending() -> None:
    """Watchdog-emitted plan-review tasks use the same prefix as messages."""
    out = classify_legacy_task(
        title="Plan ready for review: coffeeboardnm",
        created_by="audit_watchdog",
    )
    assert out is not None
    assert out.kind is InboxItemKind.PLAN_REVIEW_PENDING
    assert out.heuristic == "title_prefix:plan_ready_for_review"


def test_plan_ready_for_review_task_from_architect_classifies_too() -> None:
    """Architect-authored plan-review tasks classify the same way."""
    out = classify_legacy_task(
        title="Plan ready for review: bikepath",
        created_by="architect_bikepath",
    )
    assert out is not None
    assert out.kind is InboxItemKind.PLAN_REVIEW_PENDING


def test_unrelated_task_title_returns_none() -> None:
    """Random task titles must not classify."""
    out = classify_legacy_task(
        title="Refactor session_runtime helper",
        created_by="polly",
    )
    assert out is None


def test_task_classifier_handles_missing_fields() -> None:
    """Empty inputs are safe — return None."""
    assert classify_legacy_task(title="", created_by="") is None


def test_task_classifier_watchdog_pattern_is_case_insensitive() -> None:
    """Capitalisation drift in the finding body shouldn't break the rule."""
    out = classify_legacy_task(
        title=(
            "PROJECT DEMO HAS 5 QUEUED TASK(S) BUT NO CLAIM / "
            "EXECUTION / STATUS-CHANGE ACTIVITY FOR ~5 MIN."
        ),
        created_by="audit_watchdog",
    )
    assert out is not None
    assert out.kind is InboxItemKind.WATCHDOG_OPERATOR_DISPATCH
