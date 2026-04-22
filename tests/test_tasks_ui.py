"""Focused UI tests for :class:`pollypm.cockpit_tasks.PollyTasksApp`."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Input, Static

from pollypm.work.models import (
    ActorType,
    Artifact,
    ArtifactKind,
    ContextEntry,
    Decision,
    ExecutionStatus,
    FlowNode,
    FlowNodeExecution,
    FlowTemplate,
    NodeType,
    OutputType,
    Priority,
    Task,
    TaskType,
    WorkOutput,
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
    priority: Priority = Priority.HIGH,
    updated_at: datetime | None = None,
    executions: list[FlowNodeExecution] | None = None,
    external_refs: dict[str, str] | None = None,
    acceptance_criteria: str | None = None,
    constraints: str | None = None,
    relevant_files: list[str] | None = None,
    labels: list[str] | None = None,
    flow_template_id: str = "plan_project",
    description: str = "Make the live task detail useful.",
) -> Task:
    return Task(
        project="demo",
        task_number=task_number,
        title=title,
        type=TaskType.TASK,
        labels=list(labels or []),
        work_status=status,
        flow_template_id=flow_template_id,
        flow_template_version=1,
        current_node_id=node_id,
        assignee="architect_demo",
        priority=priority,
        description=description,
        acceptance_criteria=acceptance_criteria,
        constraints=constraints,
        relevant_files=list(relevant_files or []),
        roles={"architect": "architect_demo", "operator": "polly"},
        external_refs=dict(external_refs or {}),
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
        context_by_id: dict[tuple[str, str | None], list] | None = None,
    ) -> None:
        self._tasks_factory = tasks_factory
        self._flow = flow
        self._worker_sessions = worker_sessions or {}
        self._owner_by_id = owner_by_id or {}
        self._context_by_id = context_by_id or {}
        self.approve_calls: list[tuple[str, str, str | None]] = []
        self.reject_calls: list[tuple[str, str, str]] = []

    def list_tasks(self, *, project: str):
        assert project == "demo"
        return deepcopy(self._tasks_factory())

    def get(self, task_id: str) -> Task:
        for task in self._tasks_factory():
            if task.task_id == task_id:
                return deepcopy(task)
        raise AssertionError(task_id)

    def get_context(
        self,
        task_id: str,
        limit: int = 15,
        entry_type: str | None = None,
    ):
        assert task_id.startswith("demo/")
        return list(self._context_by_id.get((task_id, entry_type), []))

    def get_execution(self, task_id: str):
        return list(self.get(task_id).executions)

    def derive_owner(self, task: Task) -> str:
        return self._owner_by_id.get(task.task_id, "architect_demo")

    def approve(
        self, task_id: str, actor: str, reason: str | None = None, skip_gates: bool = False,
    ) -> Task:
        assert actor == "user"
        self.approve_calls.append((task_id, actor, reason))
        return self.get(task_id)

    def reject(self, task_id: str, actor: str, reason: str) -> Task:
        assert actor == "user"
        self.reject_calls.append((task_id, actor, reason))
        return self.get(task_id)

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


def test_task_review_tab_shows_resubmission_diff_and_confidence_chip(
    env, monkeypatch,
) -> None:
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_tasks import PollyTasksApp

    plan_path = env["project_path"] / "docs" / "project-plan.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# Notesy Plan\n\nReview the resubmission diff.\n")

    task = _task(
        node_id="critic_panel",
        status=WorkStatus.REVIEW,
        executions=[
            FlowNodeExecution(
                task_id="demo/1",
                node_id="implement",
                visit=1,
                status=ExecutionStatus.COMPLETED,
                work_output=WorkOutput(
                    type=OutputType.CODE_CHANGE,
                    summary="Implemented the feature",
                    artifacts=[
                        Artifact(
                            kind=ArtifactKind.COMMIT,
                            description="feat: add notes",
                            ref="abc123",
                        )
                    ],
                ),
                completed_at=datetime(2026, 4, 20, 17, 0, tzinfo=UTC),
            ),
            FlowNodeExecution(
                task_id="demo/1",
                node_id="implement",
                visit=2,
                status=ExecutionStatus.COMPLETED,
                work_output=WorkOutput(
                    type=OutputType.CODE_CHANGE,
                    summary="Implemented the feature with review fixes",
                    artifacts=[
                        Artifact(
                            kind=ArtifactKind.COMMIT,
                            description="feat: add notes with review fixes",
                            ref="def456",
                        )
                    ],
                ),
                completed_at=datetime(2026, 4, 20, 17, 20, tzinfo=UTC),
            ),
            FlowNodeExecution(
                task_id="demo/1",
                node_id="critic_panel",
                visit=2,
                status=ExecutionStatus.COMPLETED,
                started_at=datetime(2026, 4, 20, 17, 22, tzinfo=UTC),
                decision=Decision.REJECTED,
                decision_reason="Confidence: 8/10 — tighten the review evidence",
            ),
        ],
        acceptance_criteria="Show the latest review evidence.",
        constraints="Keep the tab readable.",
        relevant_files=["src/pollypm/cockpit_tasks.py"],
    )
    fake_svc = _FakeSvc(tasks_factory=lambda: [task], flow=_flow())

    monkeypatch.setattr("pollypm.cockpit_tasks.create_tmux_client", lambda: _FakeTmux([]))

    app = PollyTasksApp(env["config_path"], "demo")
    app._get_svc = lambda: fake_svc  # type: ignore[method-assign]

    async def body() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            review = str(app.query_one("#task-review", Static).render())
            confidence = str(app.query_one("#task-review-confidence-chip", Static).render())
            toggle = app.query_one("#task-review-diff-toggle", Button)
            diff = str(app.query_one("#task-review-diff", Static).render())

            assert "Review Artifact" in review
            assert "Russell: 8/10" in confidence
            assert toggle.display
            assert "last rejected attempt" in diff

            await pilot.press("d")
            await pilot.pause()
            diff = str(app.query_one("#task-review-diff", Static).render())

            assert "Diff Since Rejection: On" in str(toggle.label)
            assert "Resubmission Diff" in diff
            assert "-Summary: Implemented the feature" in diff
            assert "+Summary: Implemented the feature with review fixes" in diff

    _run(body())


def test_task_review_confidence_chip_requires_explicit_score(env, monkeypatch) -> None:
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_tasks import PollyTasksApp

    review_task = _task(
        node_id="critic_panel",
        status=WorkStatus.REVIEW,
        executions=[
            FlowNodeExecution(
                task_id="demo/1",
                node_id="critic_panel",
                visit=1,
                status=ExecutionStatus.COMPLETED,
                decision=Decision.APPROVED,
                decision_reason="Looks good to me.",
                completed_at=datetime(2026, 4, 20, 17, 22, tzinfo=UTC),
            ),
        ],
    )
    fake_svc = _FakeSvc(tasks_factory=lambda: [review_task], flow=_flow())

    monkeypatch.setattr("pollypm.cockpit_tasks.create_tmux_client", lambda: _FakeTmux([]))

    app = PollyTasksApp(env["config_path"], "demo")
    app._get_svc = lambda: fake_svc  # type: ignore[method-assign]

    async def body() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            confidence = app.query_one("#task-review-confidence-chip", Static)

            assert not confidence.display
            assert str(confidence.render()) == ""

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
            assert rows[0][2] == "🟠 Done already"

    _run(body())


def test_task_app_surfaces_priority_glyphs_and_sorts_critical_first(env, monkeypatch) -> None:
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_tasks import PollyTasksApp

    critical = _task(
        task_number=2,
        node_id="research",
        title="Critical fix",
        priority=Priority.CRITICAL,
        updated_at=datetime(2026, 4, 20, 17, 5, tzinfo=UTC),
    )
    low = _task(
        task_number=1,
        node_id="research",
        title="Low follow-up",
        priority=Priority.LOW,
        updated_at=datetime(2026, 4, 20, 17, 20, tzinfo=UTC),
    )
    fake_svc = _FakeSvc(
        tasks_factory=lambda: [low, critical],
        flow=_flow(),
    )

    monkeypatch.setattr("pollypm.cockpit_tasks.create_tmux_client", lambda: _FakeTmux([]))

    app = PollyTasksApp(env["config_path"], "demo")
    app._get_svc = lambda: fake_svc  # type: ignore[method-assign]

    async def body() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            rows = _table_rows(app.query_one("#tasks-table", DataTable))
            overview = str(app.query_one("#task-detail", Static).render())
            assert rows[0][2] == "🔴 Critical fix"
            assert rows[1][2] == "🟢 Low follow-up"
            assert "Priority   🔴 critical" in overview

    _run(body())


def test_task_app_surfaces_unread_rejection_feedback(env, monkeypatch) -> None:
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_tasks import PollyTasksApp

    work_task = _task(node_id="synthesize", title="Ship Notesy visibility")
    feedback_task = _task(
        task_number=99,
        node_id="chat",
        title="Rejected demo/1 — Ship Notesy visibility",
        flow_template_id="chat",
        labels=["review_feedback", "task:demo/1", "project:demo"],
        description="Need better rollback coverage.\n\nReturned for rework.",
    )
    fake_svc = _FakeSvc(
        tasks_factory=lambda: [work_task, feedback_task],
        flow=_flow(),
    )

    monkeypatch.setattr("pollypm.cockpit_tasks.create_tmux_client", lambda: _FakeTmux([]))

    app = PollyTasksApp(env["config_path"], "demo")
    app._get_svc = lambda: fake_svc  # type: ignore[method-assign]

    async def body() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            rows = _table_rows(app.query_one("#tasks-table", DataTable))
            overview = str(app.query_one("#task-detail", Static).render())

            assert len(rows) == 1
            assert rows[0][1] == "in_progress · feedback"
            assert rows[0][2] == "🔄 🟠 Ship Notesy visibility"
            assert rows[0][4] == "synthesize · Rejected"
            assert "Inbox Feedback" in overview
            assert "Need better rollback coverage." in overview

    _run(body())


def test_task_app_clears_rejection_feedback_after_inbox_open(env, monkeypatch) -> None:
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_tasks import PollyTasksApp

    work_task = _task(node_id="synthesize", title="Ship Notesy visibility")
    feedback_task = _task(
        task_number=99,
        node_id="chat",
        title="Rejected demo/1 — Ship Notesy visibility",
        flow_template_id="chat",
        labels=["review_feedback", "task:demo/1", "project:demo"],
        description="Need better rollback coverage.\n\nReturned for rework.",
    )
    read_marker = ContextEntry(
        actor="user",
        timestamp=datetime(2026, 4, 20, 17, 30, tzinfo=UTC),
        text="opened in cockpit inbox",
        entry_type="read",
    )
    fake_svc = _FakeSvc(
        tasks_factory=lambda: [work_task, feedback_task],
        flow=_flow(),
        context_by_id={("demo/99", "read"): [read_marker]},
    )

    monkeypatch.setattr("pollypm.cockpit_tasks.create_tmux_client", lambda: _FakeTmux([]))

    app = PollyTasksApp(env["config_path"], "demo")
    app._get_svc = lambda: fake_svc  # type: ignore[method-assign]

    async def body() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            rows = _table_rows(app.query_one("#tasks-table", DataTable))
            overview = str(app.query_one("#task-detail", Static).render())

            assert len(rows) == 1
            assert rows[0][1] == "in_progress"
            assert rows[0][2] == "🟠 Ship Notesy visibility"
            assert "Inbox Feedback" not in overview

    _run(body())

def test_task_bulk_selection_and_batch_review(env, monkeypatch) -> None:
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_tasks import PollyTasksApp

    first = _task(
        node_id="critic_panel",
        status=WorkStatus.REVIEW,
        title="Docs: tidy links",
    )
    second = _task(
        task_number=2,
        node_id="critic_panel",
        status=WorkStatus.REVIEW,
        title="Bump a dependency",
    )
    fake_svc = _FakeSvc(tasks_factory=lambda: [first, second], flow=_flow())

    monkeypatch.setattr("pollypm.cockpit_tasks.create_tmux_client", lambda: _FakeTmux([]))
    monkeypatch.setattr("pollypm.cockpit_tasks._PENDING_UNDO_SECONDS", 0.2)

    app = PollyTasksApp(env["config_path"], "demo")
    app._get_svc = lambda: fake_svc  # type: ignore[method-assign]

    async def body() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            await pilot.press("A")
            await pilot.pause()
            assert fake_svc.approve_calls == []

            await pilot.press("space")
            await pilot.pause()
            table = app.query_one("#tasks-table", DataTable)
            assert table.get_row_at(0)[0] == "◉ #1"

            table.move_cursor(row=1, column=0, animate=False)
            await pilot.pause()
            await pilot.press("space")
            await pilot.pause()
            rows = _table_rows(table)
            assert rows[0][0] == "◉ #1"
            assert rows[1][0] == "◉ #2"

            await pilot.press("A")
            await pilot.pause()
            banner = str(app.query_one("#tasks-banner", Static).render())
            assert "Approve 2 tasks" in banner
            assert fake_svc.approve_calls == []

            await asyncio.sleep(0.3)
            await pilot.pause()
            assert fake_svc.approve_calls == [
                ("demo/1", "user", "Approved from task cockpit"),
                ("demo/2", "user", "Approved from task cockpit"),
            ]

            await pilot.press("space")
            await pilot.pause()
            await pilot.press("X")
            await asyncio.sleep(0.3)
            await pilot.pause()
            assert fake_svc.reject_calls == [
                ("demo/2", "user", "Bulk rejected from task cockpit"),
            ]

    _run(body())


def test_task_app_approve_undo_banner_and_auto_merge(env, monkeypatch) -> None:
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_tasks import PollyTasksApp

    task = _task(
        node_id="critic_panel",
        status=WorkStatus.REVIEW,
        external_refs={"github_pr": "123"},
    )
    fake_svc = _FakeSvc(tasks_factory=lambda: [task], flow=_flow())
    merge_calls: list[list[str]] = []
    notification_calls: list[str] = []

    monkeypatch.setattr("pollypm.cockpit_tasks.create_tmux_client", lambda: _FakeTmux([]))
    monkeypatch.setattr("pollypm.cockpit_tasks._PENDING_UNDO_SECONDS", 0.2)
    monkeypatch.setattr(
        "pollypm.cockpit_tasks.notify_task_approved",
        lambda task, *, notify: notification_calls.append(task.task_id),
    )
    monkeypatch.setattr(
        "pollypm.cockpit_tasks.subprocess.run",
        lambda cmd, **kwargs: merge_calls.append(list(cmd)) or type(
            "Proc",
            (),
            {"returncode": 0, "stdout": "", "stderr": ""},
        )(),
    )

    app = PollyTasksApp(env["config_path"], "demo")
    app._get_svc = lambda: fake_svc  # type: ignore[method-assign]

    async def body() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()
            banner = str(app.query_one("#tasks-banner", Static).render())
            assert "[Z] Undo" in banner
            assert fake_svc.approve_calls == []

            await pilot.press("z")
            await pilot.pause()
            assert fake_svc.approve_calls == []

            await pilot.press("a")
            await asyncio.sleep(0.3)
            await pilot.pause()
            assert fake_svc.approve_calls == [
                ("demo/1", "user", "Approved from task cockpit")
            ]
            assert notification_calls == ["demo/1"]
            assert merge_calls and merge_calls[0][:4] == ["gh", "pr", "merge", "123"]

    _run(body())


def test_task_reject_modal_quick_pick_and_follow_up_reason(env, monkeypatch) -> None:
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_tasks import PollyTasksApp

    task = _task(node_id="critic_panel", status=WorkStatus.REVIEW)
    fake_svc = _FakeSvc(tasks_factory=lambda: [task], flow=_flow())

    monkeypatch.setattr("pollypm.cockpit_tasks.create_tmux_client", lambda: _FakeTmux([]))
    monkeypatch.setattr("pollypm.cockpit_tasks._PENDING_UNDO_SECONDS", 60.0)

    app = PollyTasksApp(env["config_path"], "demo")
    app._get_svc = lambda: fake_svc  # type: ignore[method-assign]

    async def body() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            await pilot.press("x")
            await pilot.pause()
            app.screen.query_one("#task-reject-tests", Button).press()
            await pilot.pause()
            assert app._pending_review_action is not None
            assert app._pending_review_action.reason == "Tests missing or broken"
            assert fake_svc.reject_calls == []

            app.action_undo_pending_review()
            await pilot.pause()

            await pilot.press("x")
            await pilot.pause()
            app.screen.query_one("#task-reject-fix", Button).press()
            await pilot.pause()
            fix_input = app.screen.query_one("#task-reject-fix-reason", Input)
            fix_input.value = "Include regression tests and a repro case"
            app.screen.query_one("#task-reject-fix-submit", Button).press()
            await pilot.pause()
            assert app._pending_review_action is not None
            assert (
                app._pending_review_action.reason
                == "Include regression tests and a repro case"
            )

            app._commit_pending_review_action()
            assert fake_svc.reject_calls[-1] == (
                "demo/1",
                "user",
                "Include regression tests and a repro case",
            )

    _run(body())


def test_task_live_scroll_pauses_on_scroll_up_and_resumes(env, monkeypatch) -> None:
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_tasks import PollyTasksApp, _TaskLiveScroll
    from textual.widgets import Static as TextualStatic

    task = _task(
        node_id="synthesize",
        status=WorkStatus.IN_PROGRESS,
        executions=[
            FlowNodeExecution(
                task_id="demo/1",
                node_id="synthesize",
                visit=1,
                status=ExecutionStatus.ACTIVE,
                started_at=datetime(2026, 4, 20, 17, 14, tzinfo=UTC),
            )
        ],
    )
    fake_svc = _FakeSvc(
        tasks_factory=lambda: [task],
        flow=_flow(),
        worker_sessions={
            "demo/1": WorkerSessionRecord(
                task_project="demo",
                task_number=1,
                agent_name="architect_polly_remote",
                pane_id="%42",
                worktree_path="/tmp/worktree",
                branch_name="demo-architect",
                started_at="2026-04-20T17:14:00+00:00",
            )
        },
    )
    monkeypatch.setattr(
        "pollypm.cockpit_tasks.create_tmux_client",
        lambda: _FakeTmux([f"line {idx}" for idx in range(60)]),
    )
    monkeypatch.setattr(
        "pollypm.cockpit_tasks._render_live",
        lambda task, active_session: "Live session\n"
        + "\n".join(f"stream line {idx}" for idx in range(120)),
    )

    app = PollyTasksApp(env["config_path"], "demo")
    app._get_svc = lambda: fake_svc  # type: ignore[method-assign]

    async def body() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            live_scroll = app.query_one("#task-live-scroll", _TaskLiveScroll)
            assert live_scroll.scroll_y == live_scroll.max_scroll_y
            assert not app._live_tail_paused

            live_scroll.action_scroll_up()
            await pilot.pause()
            assert app._live_tail_paused
            assert app.query_one("#task-live-end-pill", TextualStatic).display

            app.action_resume_live_tail()
            await pilot.pause()
            assert not app._live_tail_paused
            assert live_scroll.scroll_y == live_scroll.max_scroll_y

    _run(body())


def test_task_filter_chips_reflect_active_filters_and_clear_them(env, monkeypatch) -> None:
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_tasks import PollyTasksApp

    review_task = _task(
        node_id="critic_panel",
        title="Review me",
        status=WorkStatus.REVIEW,
    )
    done_task = _task(
        task_number=2,
        node_id="research",
        title="Done already",
        status=WorkStatus.DONE,
    )
    fake_svc = _FakeSvc(tasks_factory=lambda: [review_task, done_task], flow=_flow())

    monkeypatch.setattr("pollypm.cockpit_tasks.create_tmux_client", lambda: _FakeTmux([]))

    app = PollyTasksApp(env["config_path"], "demo")
    app._get_svc = lambda: fake_svc  # type: ignore[method-assign]

    async def body() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            chips = app.query_one("#tasks-filter-chips", Horizontal)
            status_chip = app.query_one("#tasks-chip-status", Button)
            search_chip = app.query_one("#tasks-chip-search", Button)
            clear_chip = app.query_one("#tasks-chip-clear-all", Button)
            assert chips.display
            assert "Status: Active ×" in str(status_chip.render())
            assert not search_chip.display

            app.search_input.value = "review"
            app._render_table(select_first=True)
            await pilot.pause()
            assert chips.display
            assert search_chip.display
            assert "Search: review ×" in str(search_chip.render())

            clear_chip.press()
            await pilot.pause()
            assert app._status_filter == "all"
            assert app._search_query == ""
            assert not chips.display

    _run(body())
