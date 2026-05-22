"""Snooze-filter regression for the cockpit/dashboard inbox path (#2060).

Codex round-3 blocker #2 on PR #2060: snooze visibility was split
across surfaces. ``GET /api/v1/inbox`` filtered active snoozes via
``pollypm.web_api.service._active_snoozed_ids``, but the cockpit
inbox panel + dashboard + rail count went through
``pollypm.work.inbox_view.inbox_tasks()`` which never consulted
snooze state. A snoozed item would vanish from the API while still
showing up everywhere the cockpit looked.

These tests pin the cockpit-path filter at the inbox-view layer so
both surfaces agree on what's visible.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pollypm.work.inbox_view import inbox_tasks
from pollypm.work.models import (
    ActorType,
    FlowNode,
    FlowTemplate,
    NodeType,
    Priority,
    Task,
    TaskType,
    WorkStatus,
)


# ---------------------------------------------------------------------------
# Fixtures (mirror tests/test_inbox_review_ordering.py shape)
# ---------------------------------------------------------------------------


def _task(
    number: int,
    *,
    title: str = "Inbox item",
    flow_template_id: str = "chat",
    current_node_id: str = "human_review",
    priority: Priority = Priority.NORMAL,
    roles: dict[str, str] | None = None,
) -> Task:
    return Task(
        project="demo",
        task_number=number,
        title=title,
        type=TaskType.TASK,
        work_status=WorkStatus.IN_PROGRESS,
        priority=priority,
        flow_template_id=flow_template_id,
        current_node_id=current_node_id,
        roles=roles or {"requester": "user"},
        updated_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )


def _chat_flow() -> FlowTemplate:
    return FlowTemplate(
        name="chat",
        description="",
        start_node="human_review",
        nodes={
            "human_review": FlowNode(
                name="human_review",
                type=NodeType.REVIEW,
                actor_type=ActorType.HUMAN,
            ),
        },
    )


class _ContextEntry:
    """Mimics :class:`pollypm.work.models.ContextEntry` for the snooze field."""

    def __init__(self, text: str) -> None:
        self.text = text


class _BulkSnoozeService:
    """Inbox-view stub with the pg-shape bulk snooze helper.

    Mirrors the pg backend (which is what cockpit/dashboard go
    through) — exposes ``list_tasks``, ``get_flow``, and
    ``latest_snoozes_bulk``. The bulk helper returns one entry per
    snoozed task keyed by ``(project, task_number)`` (matching
    :meth:`PgWorkService.latest_snoozes_bulk`).
    """

    def __init__(
        self,
        tasks: list[Task],
        *,
        snoozes: dict[tuple[str, int], str] | None = None,
    ) -> None:
        self.tasks = list(tasks)
        self.snoozes = dict(snoozes or {})
        self.bulk_calls = 0
        self.flows = {"chat": _chat_flow()}

    def list_tasks(self, *, project: str | None = None, work_status=None):
        out = list(self.tasks)
        if project is not None:
            out = [t for t in out if t.project == project]
        if work_status is not None:
            out = [
                t for t in out
                if getattr(t.work_status, "value", str(t.work_status)) == work_status
            ]
        return out

    def get_flow(self, name: str, project: str | None = None) -> FlowTemplate:
        return self.flows[name]

    def latest_snoozes_bulk(self, keys):
        self.bulk_calls += 1
        return {
            key: _ContextEntry(text)
            for key, text in self.snoozes.items()
            if key in set(keys)
        }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_inbox_tasks_excludes_actively_snoozed_row() -> None:
    """A row with a future snooze must NOT appear in cockpit inbox.

    This is the broken-feature case from Codex round-3: snooze hid
    the row from ``GET /api/v1/inbox`` but the cockpit / dashboard
    panel still surfaced it because ``inbox_tasks`` never consulted
    the snooze state.
    """
    now = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
    future = (now + timedelta(hours=2)).isoformat()

    visible = _task(1, title="visible")
    snoozed = _task(2, title="snoozed")

    svc = _BulkSnoozeService(
        [visible, snoozed],
        snoozes={("demo", 2): f"until_iso={future}"},
    )
    result = inbox_tasks(svc, project="demo", now=now)
    ids = [t.task_id for t in result]
    assert "demo/1" in ids
    assert "demo/2" not in ids, (
        "snoozed item still surfaced via inbox_tasks — cockpit / "
        "dashboard would show it even though /api/v1/inbox hides it"
    )


def test_inbox_tasks_re_surfaces_expired_snooze() -> None:
    """A snooze marker whose wake time is in the past must NOT hide the row."""
    now = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
    past = (now - timedelta(hours=1)).isoformat()

    task = _task(1, title="re-emerged")
    svc = _BulkSnoozeService(
        [task],
        snoozes={("demo", 1): f"until_iso={past}"},
    )
    result = inbox_tasks(svc, project="demo", now=now)
    assert [t.task_id for t in result] == ["demo/1"]


def test_inbox_tasks_uses_bulk_helper_when_available() -> None:
    """One bulk call regardless of page size — no N+1 on cockpit refresh."""
    now = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
    future = (now + timedelta(hours=2)).isoformat()

    tasks = [_task(i, title=f"item-{i}") for i in range(1, 6)]
    svc = _BulkSnoozeService(
        tasks,
        snoozes={("demo", 3): f"until_iso={future}"},
    )
    result = inbox_tasks(svc, project="demo", now=now)
    assert svc.bulk_calls == 1, (
        f"cockpit path must call latest_snoozes_bulk once; saw {svc.bulk_calls}"
    )
    ids = sorted(t.task_id for t in result)
    assert ids == ["demo/1", "demo/2", "demo/4", "demo/5"]


def test_inbox_tasks_without_bulk_helper_returns_all_rows() -> None:
    """Backends without bulk OR get_context (legacy mocks) degrade open.

    The snooze filter must not blow up on a service that lacks both
    helpers — that's the shape of the existing fakes in
    ``tests/test_inbox_review_ordering.py``. Without snooze state we
    can't decide; the safe default is "show the row" (matches
    pre-#2060 behaviour everywhere except pg).
    """

    class _LegacyService:
        def list_tasks(self, *, project=None, work_status=None):
            # Only return when filtered to IN_PROGRESS so the per-
            # status fanout in inbox_tasks doesn't dupe the row.
            if work_status not in (None, "in_progress"):
                return []
            return [_task(1)]

        def get_flow(self, name, project=None):
            return _chat_flow()

    result = inbox_tasks(_LegacyService(), project="demo")
    assert [t.task_id for t in result] == ["demo/1"]
