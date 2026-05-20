"""Inbox-kind taxonomy + pg round-trip coverage (#1794).

Replaces the backend-neutral half of the deleted
``tests/test_inbox_kind_schema.py``:

* Enum stability — every on-disk value is fixed so a typo can't orphan
  legacy rows.
* ``coerce_kind`` fallback semantics (None / unknown → LEGACY).
* ``work_tasks.kind`` round-trip through :meth:`PgWorkService.create`.

The deleted module also exercised the SQLite pre-migration backfill
path (raw ``CREATE TABLE`` with the old column shape, then run
``SQLiteWorkService``'s migrator). The pg backend doesn't have a
pre-migration legacy DB shape to backfill from — the migrations
applier provisions ``kind`` with the row creation — so that test
is intentionally not re-added.
"""

from __future__ import annotations

import pytest

from pollypm.inbox.kind import InboxItemKind, coerce_kind


# ---------------------------------------------------------------------------
# Enum stability
# ---------------------------------------------------------------------------


def test_inbox_item_kind_values_are_stable():
    """The on-disk strings must never drift. If you add a member, do
    it here; if you find yourself wanting to rename one, don't — every
    legacy row holds the literal string."""
    assert {k.value for k in InboxItemKind} == {
        "plan_review_pending",
        "approval_request",
        "pm_question_unanswered",
        "watchdog_operator_dispatch",
        "manual_decision",
        "completion_fyi",
        "self_bug_report",
        "activity_event",
        "info",
        "legacy",
    }


def test_coerce_kind_handles_unknown_and_none():
    assert coerce_kind(None) is InboxItemKind.LEGACY
    assert coerce_kind("not-a-real-kind") is InboxItemKind.LEGACY
    assert (
        coerce_kind("plan_review_pending")
        is InboxItemKind.PLAN_REVIEW_PENDING
    )
    assert (
        coerce_kind(InboxItemKind.MANUAL_DECISION)
        is InboxItemKind.MANUAL_DECISION
    )


# ---------------------------------------------------------------------------
# work_tasks.kind — pg round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(InboxItemKind))
def test_work_tasks_kind_round_trip_via_create(pg_work_service, kind):
    """Every enum value survives create() -> get() unchanged on pg."""
    svc = pg_work_service
    task = svc.create(
        title=f"task for {kind.value}",
        description="round-trip",
        type="task",
        project="proj",
        flow_template="standard",
        roles={"worker": "agent-1", "reviewer": "agent-2"},
        created_by="tester",
        kind=kind.value,
    )
    loaded = svc.get(task.task_id)
    assert loaded.kind is kind


def test_work_tasks_kind_defaults_to_legacy(pg_work_service):
    """No explicit ``kind`` argument should default to LEGACY."""
    svc = pg_work_service
    task = svc.create(
        title="no kind specified",
        description="default",
        type="task",
        project="proj",
        flow_template="standard",
        roles={"worker": "agent-1", "reviewer": "agent-2"},
        created_by="tester",
    )
    loaded = svc.get(task.task_id)
    assert loaded.kind is InboxItemKind.LEGACY
