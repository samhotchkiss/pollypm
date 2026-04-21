"""Focused UI tests for :class:`pollypm.cockpit_tasks.PollyTasksApp`."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest
from textual.widgets import DataTable, Static

from pollypm.work.models import (
    ActorType,
    ExecutionStatus,
    FlowNode,
    FlowNodeExecution,
    FlowTemplate,
    NodeType,
    Priority,
    Task,
    TaskType,
    WorkStatus,
    WorkerSessionRecord,
)


def _write_config(project_path: Path, config_path: Path) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "[project]\n"
        f'tmux_session = "pollypm-test"\n'
        f'workspace_root = "{project_path.parent}"\n'
        "\n"
        "[projects.demo]\n"
        'key = "demo"\n'
        'name = "Demo"\n'
        f'path = "{project_path}"\n'
    )


def _load_config_compatible(config_path: Path) -> bool:
    try:
        from pollypm.config import load_config

        cfg = load_config(config_path)
        return "demo" in getattr(cfg, "projects", {})
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture
def env(tmp_path: Path):
    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_config(project_path, config_path)
    return {
        "config_path": config_path,
        "project_path": project_path,
    }


def _run(coro) -> None:
    asyncio.run(coro)


def _task(
    *,
    task_number: int = 1,
    node_id: str,
    title: str = "Ship Notesy visibility",
    status: WorkStatus = WorkStatus.IN_PROGRESS,
    updated_at: datetime | None = None,
    executions: list[FlowNodeExecution] | None = None,
) -> Task:
    return Task(
        project="demo",
        task_number=task_number,
        title=title,
        type=TaskType.TASK,
        work_status=status,
        flow_template_id="plan_project",
        flow_template_version=1,
        current_node_id=node_id,
        assignee="architect_demo",
        priority=Priority.HIGH,
        description="Make the live task detail useful.",
        roles={"architect": "architect_demo", "operator": "polly"},
        created_at=datetime(2026, 4, 20, 16, 0, tzinfo=UTC),
        created_by="polly",
        updated_at=updated_at or datetime(2026, 4, 20, 17, 15, tzinfo=UTC),
        executions=list(executions or []),
        total_input_tokens=120,
        total_output_tokens=48,
        session_count=1,
    )


def _flow() -> FlowTemplate:
    return FlowTemplate(
        name="plan_project",
        description="Demo plan flow",
        start_node="research",
        nodes={
            "research": FlowNode(
                name="Research",
                type=NodeType.WORK,
                actor_type=ActorType.ROLE,
                actor_role="architect",
                next_node_id="synthesize",
            ),
            "synthesize": FlowNode(
                name="Synthesize",
                type=NodeType.WORK,
                actor_type=ActorType.ROLE,
                actor_role="architect",
                next_node_id="critic_panel",
            ),
            "critic_panel": FlowNode(
                name="Critic Panel",
                type=NodeType.REVIEW,
                actor_type=ActorType.ROLE,
                actor_role="reviewer",
            ),
        },
        version=1,
        is_current=True,
    )


class _FakeTmux:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines

    def capture_pane(self, pane_id: str, lines: int = 12) -> str:
        assert pane_id == "%42"
        assert lines == 12
        return "\n".join(self.lines)


class _FakeSvc:
    def __init__(
        self,
        *,
        tasks_factory,
        flow: FlowTemplate,
        worker_sessions: dict[str, WorkerSessionRecord | None] | None = None,
        owner_by_id: dict[str, str] | None = None,
    ) -> None:
        self._tasks_factory = tasks_factory
        self._flow = flow
        self._worker_sessions = worker_sessions or {}
        self._owner_by_id = owner_by_id or {}
        self.approvals: list[tuple[str, str, str]] = []
        self.rejections: list[tuple[str, str, str]] = []

    def list_tasks(self, *, project: str):
        assert project == "demo"
        return deepcopy(self._tasks_factory())

    def get(self, task_id: str) -> Task:
        for task in self._tasks_factory():
            if task.task_id == task_id:
                return deepcopy(task)
        raise AssertionError(task_id)

    def get_context(self, task_id: str, limit: int = 15):
        assert task_id.startswith("demo/")
        assert limit == 15
        return []

    def get_execution(self, task_id: str):
        return list(self.get(task_id).executions)

    def derive_owner(self, task: Task) -> str:
        return self._owner_by_id.get(task.task_id, "architect_demo")

    def get_flow(self, name: str, *, project: str | None = None) -> FlowTemplate:
        assert name == "plan_project"
        assert project == "demo"
        return self._flow

    def get_worker_session(
        self,
        *,
        task_project: str,
        task_number: int,
        active_only: bool = False,
    ) -> WorkerSessionRecord | None:
        assert task_project == "demo"
        assert active_only is True
        return self._worker_sessions.get(f"{task_project}/{task_number}")

    def approve(self, task_id: str, actor: str, reason: str) -> None:
        self.approvals.append((task_id, actor, reason))

    def reject(self, task_id: str, actor: str, reason: str) -> None:
        self.rejections.append((task_id, actor, reason))

    def close(self) -> None:
        return None


def _table_rows(table: DataTable) -> list[list[str]]:
    return [
        [str(cell) for cell in table.get_row_at(row_index)]
        for row_index in range(table.row_count)
    ]


def test_task_app_surfaces_stage_timestamps_and_live_session_tabs(env, monkeypatch) -> None:
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_tasks import PollyTasksApp

    executions = [
        FlowNodeExecution(
            task_id="demo/1",
            node_id="research",
            visit=1,
            status=ExecutionStatus.COMPLETED,
            started_at=datetime(2026, 4, 20, 16, 43, tzinfo=UTC),
            completed_at=datetime(2026, 4, 20, 16, 57, tzinfo=UTC),
        ),
        FlowNodeExecution(
            task_id="demo/1",
            node_id="synthesize",
            visit=1,
            status=ExecutionStatus.ACTIVE,
            started_at=datetime(2026, 4, 20, 17, 14, tzinfo=UTC),
        ),
    ]
    task = _task(node_id="synthesize", executions=executions)
    worker_session = WorkerSessionRecord(
        task_project="demo",
        task_number=1,
        agent_name="architect_polly_remote",
        pane_id="%42",
        worktree_path="/Users/sam/dev/polly-remote/.pollypm/worktrees/architect",
        branch_name="demo-architect",
        started_at="2026-04-20T17:14:00+00:00",
    )
    fake_svc = _FakeSvc(
        tasks_factory=lambda: [task],
        flow=_flow(),
        worker_sessions={"demo/1": worker_session},
    )

    monkeypatch.setattr("pollypm.cockpit_tasks.create_tmux_client", lambda: _FakeTmux([
        "Claude Code",
        "Working through the synthesize stage",
        "Drafting the rollout plan now",
    ]))
    monkeypatch.setattr("pollypm.cockpit_tasks._fmt_time", lambda iso: f"TS[{iso[11:16]}]")
    monkeypatch.setattr(
        "pollypm.cockpit_tasks._format_relative_age",
        lambda value: "moments ago",
    )

    app = PollyTasksApp(env["config_path"], "demo")
    app._get_svc = lambda: fake_svc  # type: ignore[method-assign]

    async def body() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            table = app.query_one("#tasks-table", DataTable)
            assert table.row_count == 1

            overview = str(app.query_one("#task-detail", Static).render())
            live = str(app.query_one("#task-live", Static).render())
            timeline_rows = _table_rows(app.query_one("#task-timeline", DataTable))

            assert "Stage      synthesize · work · architect" in overview
            assert "Session    architect_polly_remote" in overview
            assert "Branch     demo-architect" in live
            assert "Peek" in live
            assert "Working through the synthesize stage" in live
            assert timeline_rows[0][0] == "● research"
            assert timeline_rows[0][3] == "TS[16:57]"
            assert timeline_rows[1][0] == "⟳ synthesize"
            assert timeline_rows[1][2] == "TS[17:14]"

    _run(body())


def test_plan_review_task_surfaces_plan_artifact_and_selects_review_tab(
    env, monkeypatch,
) -> None:
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_tasks import PollyTasksApp
    from textual.widgets import TabbedContent

    plan_path = env["project_path"] / "docs" / "project-plan.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# Notesy Plan\n\nShip the review surface.\n")

    review_task = _task(
        node_id="critic_panel",
        status=WorkStatus.REVIEW,
        title="Review Notesy plan",
    )
    fake_svc = _FakeSvc(
        tasks_factory=lambda: [review_task],
        flow=_flow(),
    )

    monkeypatch.setattr("pollypm.cockpit_tasks.create_tmux_client", lambda: _FakeTmux([]))

    app = PollyTasksApp(env["config_path"], "demo")
    app._get_svc = lambda: fake_svc  # type: ignore[method-assign]

    async def body() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            tabs = app.query_one("#task-tabs", TabbedContent)
            review = str(app.query_one("#task-review", Static).render())
            assert tabs.active == "task-tab-review"
            assert "Review Artifact" in review
            assert "docs/project-plan.md" in review
            assert "Ship the review surface." in review

    _run(body())


def test_task_app_refresh_preserves_selected_task_and_updates_tabs(env, monkeypatch) -> None:
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_tasks import PollyTasksApp

    state = {
        "task": _task(
            node_id="synthesize",
            executions=[
                FlowNodeExecution(
                    task_id="demo/1",
                    node_id="synthesize",
                    visit=1,
                    status=ExecutionStatus.ACTIVE,
                    started_at=datetime(2026, 4, 20, 17, 14, tzinfo=UTC),
                )
            ],
        ),
        "peek": [
            "Initial session output",
            "Still synthesizing",
        ],
    }
    worker_session = WorkerSessionRecord(
        task_project="demo",
        task_number=1,
        agent_name="architect_polly_remote",
        pane_id="%42",
        worktree_path="/Users/sam/dev/polly-remote/.pollypm/worktrees/architect",
        branch_name="demo-architect",
        started_at="2026-04-20T17:14:00+00:00",
    )
    fake_svc = _FakeSvc(
        tasks_factory=lambda: [state["task"]],
        flow=_flow(),
        worker_sessions={"demo/1": worker_session},
    )

    monkeypatch.setattr("pollypm.cockpit_tasks.create_tmux_client", lambda: _FakeTmux(state["peek"]))
    monkeypatch.setattr("pollypm.cockpit_tasks._fmt_time", lambda iso: f"TS[{iso[11:16]}]")
    monkeypatch.setattr(
        "pollypm.cockpit_tasks._format_relative_age",
        lambda value: "moments ago",
    )

    app = PollyTasksApp(env["config_path"], "demo")
    app._get_svc = lambda: fake_svc  # type: ignore[method-assign]

    async def body() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            first = str(app.query_one("#task-detail", Static).render())
            first_live = str(app.query_one("#task-live", Static).render())
            assert "synthesize · work · architect" in first
            assert "Still synthesizing" in first_live

            state["task"] = _task(
                node_id="critic_panel",
                updated_at=datetime(2026, 4, 20, 17, 20, tzinfo=UTC),
                status=WorkStatus.REVIEW,
                executions=[
                    FlowNodeExecution(
                        task_id="demo/1",
                        node_id="synthesize",
                        visit=1,
                        status=ExecutionStatus.COMPLETED,
                        started_at=datetime(2026, 4, 20, 17, 14, tzinfo=UTC),
                        completed_at=datetime(2026, 4, 20, 17, 19, tzinfo=UTC),
                    ),
                    FlowNodeExecution(
                        task_id="demo/1",
                        node_id="critic_panel",
                        visit=1,
                        status=ExecutionStatus.ACTIVE,
                        started_at=datetime(2026, 4, 20, 17, 20, tzinfo=UTC),
                    ),
                ],
            )
            state["peek"] = [
                "Moved into review",
                "Waiting on critic feedback",
            ]

            app._refresh_list()
            await pilot.pause()
            second = str(app.query_one("#task-detail", Static).render())
            second_live = str(app.query_one("#task-live", Static).render())
            timeline_rows = _table_rows(app.query_one("#task-timeline", DataTable))

            assert "critic_panel · review · reviewer" in second
            assert "Waiting on critic feedback" in second_live
            assert timeline_rows[-1][0] == "⟳ critic_panel"
            assert timeline_rows[-1][2] == "TS[17:20]"

    _run(body())


def test_task_app_filters_drive_table_contents(env, monkeypatch) -> None:
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_tasks import PollyTasksApp

    review_task = _task(node_id="critic_panel", title="Review me", status=WorkStatus.REVIEW)
    done_task = _task(
        task_number=2,
        node_id="research",
        title="Done already",
        status=WorkStatus.DONE,
    )
    fake_svc = _FakeSvc(
        tasks_factory=lambda: [review_task, done_task],
        flow=_flow(),
        owner_by_id={"demo/1": "architect_demo", "demo/2": "closer_demo"},
    )

    monkeypatch.setattr("pollypm.cockpit_tasks.create_tmux_client", lambda: _FakeTmux([]))

    app = PollyTasksApp(env["config_path"], "demo")
    app._get_svc = lambda: fake_svc  # type: ignore[method-assign]

    async def body() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            table = app.query_one("#tasks-table", DataTable)
            assert table.row_count == 1  # default active filter hides done rows

            app._status_filter = "all"
            app._sync_filter_buttons()
            app._render_table(select_first=True)
            await pilot.pause()
            assert table.row_count == 2

            app._search_query = "done already"
            app._render_table(select_first=True)
            await pilot.pause()
            rows = _table_rows(table)
            assert table.row_count == 1
            assert rows[0][2] == "Done already"

    _run(body())


def test_task_app_approval_routes_through_shared_notification(env, monkeypatch) -> None:
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_tasks import PollyTasksApp

    review_task = _task(node_id="critic_panel", status=WorkStatus.REVIEW)
    fake_svc = _FakeSvc(
        tasks_factory=lambda: [review_task],
        flow=_flow(),
    )
    calls: list[str] = []

    monkeypatch.setattr(
        "pollypm.cockpit_tasks.notify_task_approved",
        lambda task, *, notify: calls.append(task.task_id),
    )

    app = PollyTasksApp(env["config_path"], "demo")
    app._get_svc = lambda: fake_svc  # type: ignore[method-assign]
    app._refresh_list = lambda select_first=False: None  # type: ignore[method-assign]

    app._review_task("demo/1", decision="approve")

    assert fake_svc.approvals == [
        ("demo/1", "user", "Approved from task cockpit")
    ]
    assert calls == ["demo/1"]
