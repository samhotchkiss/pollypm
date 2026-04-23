"""Tests for the work-service-backed inbox view.

Covers:
  - `pm inbox` CLI unit test (membership + sort order)
  - Cockpit inbox panel rendering against a fake work service
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pollypm.cockpit import render_inbox_panel
from pollypm.work.cli import task_app
from pollypm.work.inbox_cli import inbox_app
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


runner = CliRunner()


# ---------------------------------------------------------------------------
# Fake WorkService — just enough for inbox_view + render_inbox_panel
# ---------------------------------------------------------------------------


def _task(
    project: str,
    number: int,
    title: str,
    *,
    priority: Priority = Priority.NORMAL,
    status: WorkStatus = WorkStatus.IN_PROGRESS,
    current_node_id: str | None = "implement",
    flow_template_id: str = "standard",
    roles: dict[str, str] | None = None,
    updated_at: datetime | None = None,
    description: str = "",
) -> Task:
    return Task(
        project=project,
        task_number=number,
        title=title,
        type=TaskType.TASK,
        work_status=status,
        priority=priority,
        flow_template_id=flow_template_id,
        current_node_id=current_node_id,
        roles=roles or {},
        updated_at=updated_at or datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc),
        description=description,
    )


def _flow_with_human_review() -> FlowTemplate:
    return FlowTemplate(
        name="user-review",
        description="",
        roles={"worker": {"description": "w"}},
        nodes={
            "implement": FlowNode(
                name="implement",
                type=NodeType.WORK,
                actor_type=ActorType.ROLE,
                actor_role="worker",
                next_node_id="human_review",
            ),
            "human_review": FlowNode(
                name="human_review",
                type=NodeType.REVIEW,
                actor_type=ActorType.HUMAN,
                next_node_id="done",
                reject_node_id="implement",
            ),
            "done": FlowNode(name="done", type=NodeType.TERMINAL),
        },
        start_node="implement",
    )


def _flow_standard() -> FlowTemplate:
    return FlowTemplate(
        name="standard",
        description="",
        roles={
            "worker": {"description": "w"},
            "reviewer": {"description": "r"},
        },
        nodes={
            "implement": FlowNode(
                name="implement",
                type=NodeType.WORK,
                actor_type=ActorType.ROLE,
                actor_role="worker",
                next_node_id="code_review",
            ),
            "code_review": FlowNode(
                name="code_review",
                type=NodeType.REVIEW,
                actor_type=ActorType.ROLE,
                actor_role="reviewer",
                next_node_id="done",
                reject_node_id="implement",
            ),
            "done": FlowNode(name="done", type=NodeType.TERMINAL),
        },
        start_node="implement",
    )


class FakeWorkService:
    def __init__(
        self,
        tasks: list[Task],
        flows: dict[str, FlowTemplate] | None = None,
    ) -> None:
        self._tasks = list(tasks)
        self._flows = flows or {}

    def list_tasks(self, *, project: str | None = None, **_ignored) -> list[Task]:
        if project is None:
            return list(self._tasks)
        return [t for t in self._tasks if t.project == project]

    def get_flow(self, name: str, project: str | None = None) -> FlowTemplate:
        if name not in self._flows:
            raise KeyError(name)
        return self._flows[name]


# ---------------------------------------------------------------------------
# inbox_view.inbox_tasks — the core query
# ---------------------------------------------------------------------------


class TestInboxTasksQuery:
    def test_human_node_included(self):
        # Task at a human-review node — should be in the inbox.
        t = _task(
            "proj", 1, "Needs your review",
            flow_template_id="user-review",
            current_node_id="human_review",
            status=WorkStatus.REVIEW,
        )
        svc = FakeWorkService([t], flows={"user-review": _flow_with_human_review()})
        assert [x.task_id for x in inbox_tasks(svc)] == ["proj/1"]

    def test_role_user_included(self):
        # Task with role "user" assignment — in the inbox even without a
        # human node active.
        t = _task(
            "proj", 2, "User-owned task",
            roles={"user": "sam"},
            flow_template_id="standard",
        )
        svc = FakeWorkService([t], flows={"standard": _flow_standard()})
        assert [x.task_id for x in inbox_tasks(svc)] == ["proj/2"]

    def test_role_value_user_included(self):
        # roles={"requester": "user"} also counts.
        t = _task(
            "proj", 3, "Requested by user",
            roles={"requester": "user"},
            flow_template_id="standard",
        )
        svc = FakeWorkService([t], flows={"standard": _flow_standard()})
        assert [x.task_id for x in inbox_tasks(svc)] == ["proj/3"]

    def test_non_matching_excluded(self):
        # Pure agent-to-agent task with no human node and no 'user' role.
        t = _task(
            "proj", 4, "agent work",
            roles={"worker": "claude", "reviewer": "gpt"},
            flow_template_id="standard",
        )
        svc = FakeWorkService([t], flows={"standard": _flow_standard()})
        assert inbox_tasks(svc) == []

    def test_terminal_excluded(self):
        # Even if the task would otherwise match, terminal tasks are hidden.
        t = _task(
            "proj", 5, "done task",
            roles={"user": "sam"},
            status=WorkStatus.DONE,
            current_node_id=None,
            flow_template_id="standard",
        )
        svc = FakeWorkService([t], flows={"standard": _flow_standard()})
        assert inbox_tasks(svc) == []

    def test_sort_priority_desc_then_updated_desc(self):
        # Three tasks, all matching:
        #   A: normal, older
        #   B: normal, newer
        #   C: critical, oldest
        now = datetime(2026, 4, 10, 9, 0, tzinfo=timezone.utc)
        a = _task(
            "p", 1, "A",
            priority=Priority.NORMAL,
            updated_at=now - timedelta(hours=2),
            roles={"user": "sam"},
        )
        b = _task(
            "p", 2, "B",
            priority=Priority.NORMAL,
            updated_at=now,
            roles={"user": "sam"},
        )
        c = _task(
            "p", 3, "C",
            priority=Priority.CRITICAL,
            updated_at=now - timedelta(days=5),
            roles={"user": "sam"},
        )
        svc = FakeWorkService([a, b, c], flows={"standard": _flow_standard()})
        ordered = [t.task_id for t in inbox_tasks(svc)]
        # Critical beats normal despite being older.
        # Within normal, newer (B) beats older (A).
        assert ordered == ["p/3", "p/2", "p/1"]


# ---------------------------------------------------------------------------
# CLI: pm inbox (unit test via CliRunner + shared db fixture)
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "state.db")


def _create_user_review_task(db_path: str, title: str, priority: str = "normal") -> None:
    """Create a task on the user-review flow so human_review is reachable."""
    result = runner.invoke(
        task_app,
        [
            "create", title,
            "--project", "proj",
            "--flow", "user-review",
            "--priority", priority,
            "--description", f"desc: {title}",
            "--type", "task",
            "--role", "worker=agent-1",
            "--db", db_path,
        ],
    )
    assert result.exit_code == 0, result.output


class TestInboxCLI:
    def test_empty_inbox(self, db_path):
        result = runner.invoke(inbox_app, ["--db", db_path])
        assert result.exit_code == 0, result.output
        # #341 rewrote the header to reflect the messages+tasks UNION.
        assert "Inbox: 0 items" in result.output
        assert "No messages waiting for you." in result.output

    def test_json_output_when_empty(self, db_path):
        result = runner.invoke(inbox_app, ["--db", db_path, "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        # #341: inbox JSON now emits a ``messages`` key alongside ``tasks``
        # (the messages-table side of the UNION).
        assert payload == {
            "assigned_count": 0,
            "messages": [],
            "tasks": [],
        }

    def test_task_at_human_review_appears(self, db_path):
        # Drive a task through the user-review flow until it sits at the
        # human_review node — then it should show up in pm inbox.
        _create_user_review_task(db_path, "Review me", priority="high")

        # queue → claim → node_done (moves the task to human_review)
        assert runner.invoke(
            task_app, ["queue", "proj/1", "--db", db_path],
        ).exit_code == 0
        assert runner.invoke(
            task_app,
            ["claim", "proj/1", "--actor", "agent-1", "--db", db_path],
        ).exit_code == 0
        wo = json.dumps(
            {
                "type": "code_change",
                "summary": "done",
                "artifacts": [
                    {"kind": "commit", "description": "abc123", "ref": "abc123"},
                ],
            }
        )
        done_result = runner.invoke(
            task_app,
            [
                "done", "proj/1",
                "--output", wo,
                "--actor", "agent-1",
                "--db", db_path,
            ],
        )
        assert done_result.exit_code == 0, done_result.output

        # Now the task is at human_review (actor_type: human). Check inbox.
        result = runner.invoke(inbox_app, ["--db", db_path, "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["assigned_count"] == 1
        assert payload["tasks"][0]["task_id"] == "proj/1"
        assert payload["tasks"][0]["current_node_id"] == "human_review"

    def test_show_aliases_to_task_get(self, db_path):
        _create_user_review_task(db_path, "Alias target")
        result = runner.invoke(
            inbox_app, ["show", "proj/1", "--db", db_path],
        )
        assert result.exit_code == 0, result.output
        # Same output shape as `pm task get`.
        assert "ID:       proj/1" in result.output
        assert "Alias target" in result.output

    def test_reply_missing_task_includes_why_and_fix(self, db_path):
        _create_user_review_task(db_path, "Existing task")

        result = runner.invoke(
            inbox_app, ["reply", "proj/9", "hello", "--db", db_path],
        )

        assert result.exit_code == 1
        assert "✗ Task proj/9 not found." in result.output
        assert "Why: project 'proj' does not have task number 9." in result.output
        assert "Fix: run `pm task list --project proj` to see available task ids." in result.output

    # #754 — bulk archive by title glob
    def _seed_notify(self, db_path: str, subject: str) -> int:
        from pollypm.store import SQLAlchemyStore
        store = SQLAlchemyStore(f"sqlite:///{db_path}")
        try:
            return store.enqueue_message(
                type="notify", tier="immediate", scope="inbox",
                sender="polly", recipient="user",
                subject=subject, body="body",
            )
        finally:
            store.close()

    def test_archive_match_bulk_archives_by_glob(self, db_path):
        self._seed_notify(db_path, "loop-test-111")
        self._seed_notify(db_path, "loop-test-222")
        self._seed_notify(db_path, "real-action-please-review")

        result = runner.invoke(
            inbox_app, ["archive", "--match", "*loop-test-*", "--db", db_path],
        )
        assert result.exit_code == 0, result.output
        assert "Archived 2 message(s)" in result.output

        # The non-matching message is still open.
        from pollypm.store import SQLAlchemyStore
        store = SQLAlchemyStore(f"sqlite:///{db_path}")
        try:
            rows = store.query_messages(recipient="user", state="open")
            remaining = [r.get("subject") or "" for r in rows]
        finally:
            store.close()
        assert any("real-action" in s for s in remaining)
        assert not any("loop-test" in s for s in remaining)

    def test_archive_match_dry_run_does_not_change_state(self, db_path):
        self._seed_notify(db_path, "loop-test-aaa")
        self._seed_notify(db_path, "loop-test-bbb")

        result = runner.invoke(
            inbox_app,
            ["archive", "--match", "*loop-test-*", "--dry-run", "--db", db_path],
        )
        assert result.exit_code == 0, result.output
        assert "Would archive 2 message(s)" in result.output
        # State unchanged.
        from pollypm.store import SQLAlchemyStore
        store = SQLAlchemyStore(f"sqlite:///{db_path}")
        try:
            rows = store.query_messages(recipient="user", state="open")
            assert len(rows) == 2
        finally:
            store.close()

    def test_archive_match_empty_result_is_a_clean_no_op(self, db_path):
        self._seed_notify(db_path, "unrelated")
        result = runner.invoke(
            inbox_app, ["archive", "--match", "*nothing-matches*", "--db", db_path],
        )
        assert result.exit_code == 0, result.output
        assert "No open messages matched" in result.output

    def test_archive_single_msg_id(self, db_path):
        mid = self._seed_notify(db_path, "archive me directly")

        result = runner.invoke(
            inbox_app, ["archive", f"msg:{mid}", "--db", db_path],
        )
        assert result.exit_code == 0, result.output
        assert f"msg:{mid} → archived" in result.output

        from pollypm.store import SQLAlchemyStore
        store = SQLAlchemyStore(f"sqlite:///{db_path}")
        try:
            open_rows = store.query_messages(recipient="user", state="open")
            assert len(open_rows) == 0
        finally:
            store.close()

    def test_archive_without_arg_and_without_match_errors(self, db_path):
        result = runner.invoke(inbox_app, ["archive", "--db", db_path])
        assert result.exit_code == 2, result.output
        assert "--match" in result.output


# ---------------------------------------------------------------------------
# Cockpit panel rendering with a fake work service
# ---------------------------------------------------------------------------


class TestCockpitInboxPanel:
    def test_empty_panel(self):
        svc = FakeWorkService([], flows={"standard": _flow_standard()})
        out = render_inbox_panel(svc)
        assert out.startswith("Inbox")
        assert "No tasks waiting for you." in out
        assert "Assigned: 0" in out
        # Footer gives the CLI pointer.
        assert "pm inbox" in out

    def test_panel_lists_assigned_tasks_in_priority_order(self):
        now = datetime(2026, 4, 12, 15, 0, tzinfo=timezone.utc)
        t1 = _task(
            "demo", 7, "Review launch plan",
            priority=Priority.CRITICAL,
            status=WorkStatus.REVIEW,
            flow_template_id="user-review",
            current_node_id="human_review",
            updated_at=now,
            description="Please look at the roadmap doc.",
        )
        t2 = _task(
            "demo", 8, "Answer onboarding question",
            priority=Priority.NORMAL,
            roles={"user": "sam"},
            updated_at=now - timedelta(hours=1),
            flow_template_id="standard",
        )
        svc = FakeWorkService(
            [t2, t1],  # unsorted input; the renderer must sort
            flows={
                "user-review": _flow_with_human_review(),
                "standard": _flow_standard(),
            },
        )

        class _Project:
            key = "demo"
            name = "Demo"

        out = render_inbox_panel(svc, projects=[_Project()])
        assert "Assigned (2):" in out
        # Critical first
        first = out.index("Review launch plan")
        second = out.index("Answer onboarding question")
        assert first < second
        # Project name is rendered (not just the key)
        assert "Demo" in out
        # Work-status badge on the subline
        assert "review" in out
        # Description preview rendered
        assert "Please look at the roadmap doc." in out
