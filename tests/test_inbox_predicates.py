"""Tests for the canonical ``awaits_user`` predicate (#1566).

Parameterised over every :class:`InboxItemKind` value so the
"user-facing vs informational" split is exhaustively pinned. Any new
member added to the enum without updating this table will fail the
``test_predicate_covers_every_kind`` exhaustiveness check.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pollypm.inbox import awaits_user
from pollypm.inbox.kind import InboxItemKind


# Single source of truth for the predicate's intended answer per kind.
# Keep this dict in sync with the enum: missing keys fail the
# exhaustiveness test below, so a future member can't slip in
# undeclared.
_EXPECTED: dict[InboxItemKind, bool] = {
    InboxItemKind.PLAN_REVIEW_PENDING: True,
    InboxItemKind.APPROVAL_REQUEST: True,
    InboxItemKind.PM_QUESTION_UNANSWERED: True,
    InboxItemKind.WATCHDOG_OPERATOR_DISPATCH: True,
    InboxItemKind.MANUAL_DECISION: True,
    InboxItemKind.COMPLETION_FYI: False,
    InboxItemKind.SELF_BUG_REPORT: False,
    InboxItemKind.ACTIVITY_EVENT: False,
    InboxItemKind.INFO: False,
    # ``legacy`` is intentionally True — rows pre-#1565 haven't been
    # classified yet, so the predicate treats them as user-facing
    # until the #1570 backfill reclassifies them. Hiding them would
    # silently drop pending work during the migration window.
    InboxItemKind.LEGACY: True,
}


@pytest.mark.parametrize("kind,expected", list(_EXPECTED.items()))
def test_awaits_user_per_kind(kind, expected):
    item = SimpleNamespace(kind=kind)
    assert awaits_user(item) is expected


def test_predicate_covers_every_kind():
    """Adding a new InboxItemKind without an entry in _EXPECTED is a bug."""
    assert set(_EXPECTED.keys()) == set(InboxItemKind)


def test_awaits_user_accepts_string_kind():
    """Stored rows surface ``kind`` as the on-disk string; the predicate
    must transparently coerce so callers don't have to."""
    assert awaits_user(SimpleNamespace(kind="approval_request")) is True
    assert awaits_user(SimpleNamespace(kind="completion_fyi")) is False


def test_awaits_user_missing_kind_treated_as_legacy():
    """An item with no ``kind`` attribute falls back to legacy semantics
    — True, so a producer that forgets to set the field never silently
    hides work."""
    assert awaits_user(SimpleNamespace()) is True


def test_awaits_user_unknown_kind_string_treated_as_legacy():
    """A typo'd ``kind`` value never crashes; it falls back to legacy."""
    assert awaits_user(SimpleNamespace(kind="not-a-real-kind")) is True
