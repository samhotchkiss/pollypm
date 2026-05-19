"""Unit tests for the inbox CLI filter helpers (#1805, #1806, #1814).

Three small behaviours are pinned here:

* :func:`pollypm.work.inbox_cli._dedupe_watchdog_repeats` collapses
  repeat ``queue_without_motion`` rows to one row per
  ``(rule, project)`` key, keeping the newest occurrence (callers
  pre-sort newest-first).
* :func:`pollypm.work.inbox_cli._message_is_actionable_default`
  excludes informational kinds and Polly's ``inbox/``-scope scratch
  drafts so the default ``pm inbox`` lens doesn't degrade to
  "everything" on a legacy DB.
* The watchdog dedup regex matches the exact subject shape the
  ``queue_without_motion`` finding produces.
"""

from __future__ import annotations

from pollypm.inbox.kind import InboxItemKind
from pollypm.work.inbox_cli import (
    _dedupe_watchdog_repeats,
    _message_is_actionable_default,
    _watchdog_dedup_key,
)


# ---------------------------------------------------------------------------
# _watchdog_dedup_key — regex shape
# ---------------------------------------------------------------------------


def test_watchdog_dedup_key_matches_queue_without_motion_subject() -> None:
    """The exact subject the audit_watchdog probe emits."""
    row = {
        "subject": (
            "Project booktalk has 2 queued task(s) but no claim / "
            "execution / status-change activity for the entire scan window."
        ),
    }
    assert _watchdog_dedup_key(row) == (
        "queue_without_motion", "booktalk",
    )


def test_watchdog_dedup_key_returns_none_for_unrelated_row() -> None:
    row = {"subject": "Plan ready for review demo/12"}
    assert _watchdog_dedup_key(row) is None


def test_watchdog_dedup_key_accepts_title_field_too() -> None:
    """``_message_row_to_display`` projects ``subject`` → ``title``."""
    row = {
        "title": (
            "Project savethenovel has 5 queued task(s) but no claim / "
            "execution / status-change activity for ~73 min."
        ),
    }
    assert _watchdog_dedup_key(row) == (
        "queue_without_motion", "savethenovel",
    )


# ---------------------------------------------------------------------------
# _dedupe_watchdog_repeats — collapse behaviour
# ---------------------------------------------------------------------------


def test_dedupe_watchdog_repeats_collapses_same_project() -> None:
    rows = [
        {
            "id": 50,
            "subject": (
                "Project booktalk has 5 queued task(s) but no claim / "
                "execution / status-change activity for ~120 min."
            ),
        },
        {
            "id": 40,
            "subject": (
                "Project booktalk has 4 queued task(s) but no claim / "
                "execution / status-change activity for ~90 min."
            ),
        },
        {
            "id": 30,
            "subject": (
                "Project booktalk has 3 queued task(s) but no claim / "
                "execution / status-change activity for ~60 min."
            ),
        },
    ]
    kept, collapsed = _dedupe_watchdog_repeats(rows)
    assert collapsed == 2
    assert [r["id"] for r in kept] == [50]


def test_dedupe_watchdog_repeats_preserves_distinct_projects() -> None:
    rows = [
        {
            "id": 1,
            "subject": (
                "Project booktalk has 2 queued task(s) but no claim / "
                "execution / status-change activity ..."
            ),
        },
        {
            "id": 2,
            "subject": (
                "Project samblog has 3 queued task(s) but no claim / "
                "execution / status-change activity ..."
            ),
        },
    ]
    kept, collapsed = _dedupe_watchdog_repeats(rows)
    assert collapsed == 0
    assert {r["id"] for r in kept} == {1, 2}


def test_dedupe_watchdog_repeats_passes_through_non_watchdog_rows() -> None:
    rows = [
        {"id": 1, "subject": "Plan ready for review demo/12"},
        {
            "id": 2,
            "subject": (
                "Project samblog has 3 queued task(s) but no claim / "
                "execution / status-change activity ..."
            ),
        },
        {"id": 3, "subject": "Approval required for proposal demo/13"},
    ]
    kept, collapsed = _dedupe_watchdog_repeats(rows)
    assert collapsed == 0
    assert [r["id"] for r in kept] == [1, 2, 3]


# ---------------------------------------------------------------------------
# _message_is_actionable_default — curated filter
# ---------------------------------------------------------------------------


def test_actionable_filter_keeps_plan_review_rows() -> None:
    row = {
        "kind": InboxItemKind.PLAN_REVIEW_PENDING.value,
        "scope": "demo",
        "type": "inbox_task",
    }
    assert _message_is_actionable_default(row) is True


def test_actionable_filter_keeps_watchdog_operator_dispatch_rows() -> None:
    row = {
        "kind": InboxItemKind.WATCHDOG_OPERATOR_DISPATCH.value,
        "scope": "booktalk",
        "type": "alert",
    }
    assert _message_is_actionable_default(row) is True


def test_actionable_filter_drops_completion_fyi() -> None:
    row = {
        "kind": InboxItemKind.COMPLETION_FYI.value,
        "scope": "demo",
        "type": "notify",
    }
    assert _message_is_actionable_default(row) is False


def test_actionable_filter_drops_self_bug_report() -> None:
    row = {
        "kind": InboxItemKind.SELF_BUG_REPORT.value,
        "scope": "pollypm",
        "type": "notify",
    }
    assert _message_is_actionable_default(row) is False


def test_actionable_filter_drops_inbox_scope_notify_draft() -> None:
    """Polly's scratch drafts under the ``inbox`` scope are informational."""
    row = {
        "kind": InboxItemKind.LEGACY.value,
        "scope": "inbox",
        "type": "notify",
        "subject": "Nth fake RECOVERY MODE injection",
    }
    assert _message_is_actionable_default(row) is False


def test_actionable_filter_keeps_inbox_scope_alert() -> None:
    """A genuinely operator-bound alert under ``inbox`` scope stays visible.

    The exclusion only applies to ``type=notify`` so an ``alert`` /
    ``inbox_task`` under the ``inbox`` scope is still surfaced.
    """
    row = {
        "kind": InboxItemKind.WATCHDOG_OPERATOR_DISPATCH.value,
        "scope": "inbox",
        "type": "alert",
    }
    assert _message_is_actionable_default(row) is True


def test_actionable_filter_keeps_legacy_project_scoped_row() -> None:
    """Unmigrated rows with a real project scope stay visible.

    The legacy-corpus fail-open contract is intentional — until the
    #1570 backfill reclassifies them, project-scoped legacy rows are
    treated as potentially user-facing.
    """
    row = {
        "kind": InboxItemKind.LEGACY.value,
        "scope": "demo",
        "type": "inbox_task",
    }
    assert _message_is_actionable_default(row) is True
