"""Cycle 130 — UX audit fix: derive reviewer name from the task.

The polly-dashboard's review section hardcoded ``waiting for Russell``
in the per-task line. Russell is the spec's autoreviewer name but
not every workspace uses it — workspaces with a different reviewer
persona saw a misleading label. Derive the reviewer from the task's
roles or assignee, fall back to a generic ``waiting for review``.
"""

from __future__ import annotations

from types import SimpleNamespace

from pollypm.cockpit_sections.dashboard import _task_reviewer


def _task(*, roles=None, assignee=None) -> SimpleNamespace:
    return SimpleNamespace(roles=roles or {}, assignee=assignee)


def test_reviewer_from_reviewer_role() -> None:
    assert _task_reviewer(_task(roles={"reviewer": "alice"})) == "alice"


def test_reviewer_from_autoreviewer_role() -> None:
    assert _task_reviewer(_task(roles={"autoreviewer": "russell"})) == "russell"


def test_reviewer_falls_back_to_assignee() -> None:
    assert _task_reviewer(_task(assignee="bob")) == "bob"


def test_reviewer_skips_user_role_marker() -> None:
    """``roles["reviewer"] == "user"`` means a human reviews — caller
    should render the generic placeholder, not "waiting for user"."""
    assert _task_reviewer(_task(roles={"reviewer": "user"})) == ""


def test_reviewer_skips_system_assignee() -> None:
    assert _task_reviewer(_task(assignee="system")) == ""
    assert _task_reviewer(_task(assignee="pm")) == ""


def test_reviewer_returns_empty_when_unknown() -> None:
    assert _task_reviewer(_task()) == ""
