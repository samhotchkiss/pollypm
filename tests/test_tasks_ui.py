"""Focused UI tests for :class:`pollypm.cockpit_ui.PollyTasksApp`.

These cover the task-detail visibility upgrade:

* execution timeline rows surface timestamps
* the current stage is rendered with flow-node metadata
* the active session + live pane peek are visible for in-flight tasks
* an open detail view refreshes when the task list refreshes
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest
from textual.widgets import Static

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
    node_id: str,
    title: str = "Ship Notesy visibility",
    updated_at: datetime | None = None,
    executions: list[FlowNodeExecution] | None = None,
) -> Task:
    return Task(
        project="demo",
        task_number=1,
        title=title,
        type=TaskType.TASK,
        work_status=WorkStatus.IN_PROGRESS,
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
        task_factory,
        flow: FlowTemplate,
        worker_session: WorkerSessionRecord | None,
    ) -> None:
        self._task_factory = task_factory
        self._flow = flow
        self._worker_session = worker_session

    def list_tasks(self, *, project: str):
        assert project == "demo"
        return [deepcopy(self._task_factory())]

    def get(self, task_id: str) -> Task:
        assert task_id == "demo/1"
        return deepcopy(self._task_factory())

    def get_context(self, task_id: str, limit: int = 10):
        assert task_id == "demo/1"
        assert limit == 10
        return []

    def get_execution(self, task_id: str):
        assert task_id == "demo/1"
        return list(self._task_factory().executions)

    def derive_owner(self, task: Task) -> str:
        assert task.task_id == "demo/1"
        return "architect_demo"

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
        assert task_number == 1
        assert active_only is True
        return self._worker_session

    def close(self) -> None:
        return None


def test_task_detail_surfaces_stage_timestamps_and_live_session(env, monkeypatch) -> None:
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyTasksApp

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
    fake_svc = _FakeSvc(task_factory=lambda: task, flow=_flow(), worker_session=worker_session)

    monkeypatch.setattr("pollypm.cockpit_ui.create_tmux_client", lambda: _FakeTmux([
        "Claude Code",
        "Working through the synthesize stage",
        "Drafting the rollout plan now",
    ]))
    monkeypatch.setattr("pollypm.cockpit_ui._fmt_time", lambda iso: f"TS[{iso[11:16]}]")
    monkeypatch.setattr("pollypm.cockpit_ui._format_relative_age", lambda value: "moments ago")

    app = PollyTasksApp(env["config_path"], "demo")
    app._get_svc = lambda: fake_svc  # type: ignore[method-assign]

    async def body() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            app._selected_task_id = "demo/1"
            app._show_detail("demo/1")
            await pilot.pause()
            rendered = str(app.query_one("#task-detail", Static).render())
            assert "Stage     synthesize · work · architect" in rendered
            assert "Session   architect_polly_remote" in rendered
            assert "⟳ synthesize — started TS[17:14] · moments ago" in rendered
            assert "● research — TS[16:57] · moments ago" in rendered
            assert "Branch    demo-architect" in rendered
            assert "Peek" in rendered
            assert "Working through the synthesize stage" in rendered

    _run(body())


def test_selected_task_detail_refreshes_when_list_refreshes(env, monkeypatch) -> None:
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyTasksApp

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
    fake_svc = _FakeSvc(task_factory=lambda: state["task"], flow=_flow(), worker_session=worker_session)

    monkeypatch.setattr("pollypm.cockpit_ui.create_tmux_client", lambda: _FakeTmux(state["peek"]))
    monkeypatch.setattr("pollypm.cockpit_ui._fmt_time", lambda iso: f"TS[{iso[11:16]}]")
    monkeypatch.setattr("pollypm.cockpit_ui._format_relative_age", lambda value: "moments ago")

    app = PollyTasksApp(env["config_path"], "demo")
    app._get_svc = lambda: fake_svc  # type: ignore[method-assign]

    async def body() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            app._selected_task_id = "demo/1"
            app._show_detail("demo/1")
            await pilot.pause()
            first = str(app.query_one("#task-detail", Static).render())
            assert "synthesize · work · architect" in first
            assert "Still synthesizing" in first

            state["task"] = _task(
                node_id="critic_panel",
                updated_at=datetime(2026, 4, 20, 17, 20, tzinfo=UTC),
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
            assert "critic_panel · review · reviewer" in second
            assert "Waiting on critic feedback" in second

    _run(body())
