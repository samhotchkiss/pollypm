"""Structured ``kind`` taxonomy for inbox items (#1565).

Before this taxonomy, every inbox row collapsed to ``type=notify``
+ ``tier=immediate`` + ``priority=normal`` — the three "what is this?"
fields said the same thing for every emit site, so nothing was
distinguishable. The :class:`InboxItemKind` enum is the structured
input the canonical ``awaits_user`` predicate (#1566) needs, and the
field every emit site retrains onto in #1567.

Stored as the string value on ``messages.kind`` and ``work_tasks.kind``.
Existing pre-migration rows read as ``LEGACY`` until the backfill in
#1570 reclassifies them; the predicate treats ``LEGACY`` as
"awaits user" so nothing is silently hidden during the migration
window.
"""

from __future__ import annotations

import enum


class InboxItemKind(enum.Enum):
    """Discriminator for what an inbox row actually is.

    Values match the on-disk column verbatim so a round-trip is a
    plain ``InboxItemKind(row["kind"])`` / ``kind.value`` pair. New
    members are append-only — existing values must never change
    spelling because legacy rows hold the literal string.
    """

    PLAN_REVIEW_PENDING = "plan_review_pending"
    APPROVAL_REQUEST = "approval_request"
    PM_QUESTION_UNANSWERED = "pm_question_unanswered"
    WATCHDOG_OPERATOR_DISPATCH = "watchdog_operator_dispatch"
    MANUAL_DECISION = "manual_decision"
    COMPLETION_FYI = "completion_fyi"
    SELF_BUG_REPORT = "self_bug_report"
    ACTIVITY_EVENT = "activity_event"
    INFO = "info"
    LEGACY = "legacy"


def coerce_kind(value: object) -> InboxItemKind:
    """Map a stored value back to :class:`InboxItemKind`.

    ``None`` / unknown strings fall back to :attr:`InboxItemKind.LEGACY`
    — a row written before #1565 landed (or one whose producer hasn't
    been retrained yet) is treated identically to a pre-migration
    row, which keeps the predicate's "fail-open" contract intact.
    """
    if isinstance(value, InboxItemKind):
        return value
    if value is None:
        return InboxItemKind.LEGACY
    try:
        return InboxItemKind(str(value))
    except ValueError:
        return InboxItemKind.LEGACY
