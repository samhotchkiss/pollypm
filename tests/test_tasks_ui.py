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


# ---------------------------------------------------------------------------
# #920 regression — config-key vs work-DB project-name mismatch + workspace DB
# ---------------------------------------------------------------------------


def _write_split_key_config(
    project_path: Path, config_path: Path,
) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "[project]\n"
        'tmux_session = "pollypm-test"\n'
        f'workspace_root = "{project_path.parent}"\n'
        "\n"
        "[projects.blackjack_trainer]\n"
        'key = "blackjack_trainer"\n'
        'name = "blackjack-trainer"\n'
        f'path = "{project_path}"\n'
    )


def test_tasks_pane_lists_in_progress_when_config_key_and_name_differ(
    tmp_path: Path,
) -> None:
    """#920 — Tasks pane must include ``in_progress`` rows even when
    the config key (``blackjack_trainer``) and the work-DB project
    name (``blackjack-trainer``) disagree.

    Pre-fix: ``svc.list_tasks(project=self.project_key)`` did an exact
    match against the underscore form and got zero rows back, so the
    table read ``0 tasks / No tasks match the current filter.`` even
    though ``pm task counts`` reported ``in_progress=1``.
    """
    from pollypm.cockpit_tasks import PollyTasksApp
    from pollypm.work.sqlite_service import SQLiteWorkService

    project_path = tmp_path / "blackjack-trainer"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_split_key_config(project_path, config_path)

    workspace_db = tmp_path / ".pollypm" / "state.db"
    workspace_db.parent.mkdir(parents=True, exist_ok=True)
    with SQLiteWorkService(db_path=workspace_db, project_path=tmp_path) as svc:
        task = svc.create(
            title="Build Hi-Lo card counting trainer",
            description="seed",
            type="task",
            project="blackjack-trainer",  # hyphen — what `pm task` writes
            flow_template="standard",
            roles={"worker": "worker", "reviewer": "reviewer"},
            priority="normal",
            created_by="polly",
        )
        svc.queue(task.task_id, "polly")
        svc.claim(task.task_id, "worker")

    app = PollyTasksApp(config_path, "blackjack_trainer")
    tasks, _, _, _, _, _ = app._load_tasks()
    titles = [t.title for t in tasks]
    statuses = [t.work_status.value for t in tasks]
    assert "Build Hi-Lo card counting trainer" in titles
    assert "in_progress" in statuses


def test_tasks_pane_default_active_filter_includes_in_progress(
    tmp_path: Path,
) -> None:
    """#920 — the default ``Active`` filter must surface in_progress
    rows. Either the default changes to ``All`` or ``Active`` includes
    in_progress; both satisfy the user-visible contract.
    """
    from pollypm.cockpit_tasks import PollyTasksApp, _task_matches_status
    from pollypm.work.sqlite_service import SQLiteWorkService

    project_path = tmp_path / "blackjack-trainer"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_split_key_config(project_path, config_path)

    workspace_db = tmp_path / ".pollypm" / "state.db"
    workspace_db.parent.mkdir(parents=True, exist_ok=True)
    with SQLiteWorkService(db_path=workspace_db, project_path=tmp_path) as svc:
        t = svc.create(
            title="Active feature",
            description="seed",
            type="task",
            project="blackjack-trainer",
            flow_template="standard",
            roles={"worker": "worker", "reviewer": "reviewer"},
            priority="normal",
            created_by="polly",
        )
        svc.queue(t.task_id, "polly")
        svc.claim(t.task_id, "worker")

    app = PollyTasksApp(config_path, "blackjack_trainer")
    # Default filter is "active" on construction — invariant for the bug.
    assert app._status_filter in {"active", "all"}
    tasks, _, _, _, _, _ = app._load_tasks()
    assert tasks, "load_tasks should return the in_progress task"
    in_progress_task = next(
        t for t in tasks if t.work_status.value == "in_progress"
    )
    assert _task_matches_status(in_progress_task, app._status_filter)


def test_tasks_pane_header_uses_canonical_display_label(tmp_path: Path) -> None:
    """#920 — Tasks pane header must echo the project's display name
    (``blackjack-trainer``), not the slugified config key
    (``blackjack_trainer``). The rail and CLI both use the hyphen
    form; the cockpit's Tasks header was the only surface that flipped
    to the underscore form.
    """
    from pollypm.cockpit_tasks import _project_label_and_aliases
    from pollypm.config import load_config

    project_path = tmp_path / "blackjack-trainer"
    project_path.mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_split_key_config(project_path, config_path)

    config = load_config(config_path)
    label, aliases = _project_label_and_aliases(config, "blackjack_trainer")
    assert label == "blackjack-trainer"
    # Aliases must include both the slug and the display name so the
    # query layer matches whichever form the DB recorded.
    assert "blackjack_trainer" in aliases
    assert "blackjack-trainer" in aliases


def test_summary_text_pluralises_singular_task_count(env) -> None:
    """Task pane status bar must read ``1 task`` (not ``1 tasks``).

    Cycle 63: the ``{shown} == {total}`` branch printed ``{total}
    tasks`` unconditionally. At ``total=1`` it read ``1 tasks``,
    which is a copy bug at the typical state where one task is
    selected/loaded. Mirrors the inbox status bar (cycle 57) and
    the doctor / dashboard plural cleanup batch.
    """
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_tasks import PollyTasksApp

    app = PollyTasksApp(env["config_path"], "demo")
    one = _task(node_id="research")
    app._tasks = [one]
    summary = app._summary_text([one])
    assert summary.startswith("1 task")
    assert not summary.startswith("1 tasks")

    two = _task(task_number=2, node_id="research", title="Second task")
    app._tasks = [one, two]
    summary = app._summary_text([one, two])
    assert summary.startswith("2 tasks")


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


def test_task_search_keeps_focus_and_suppresses_review_shortcuts(
    env, monkeypatch,
) -> None:
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_tasks import PollyTasksApp

    review_task = _task(
        node_id="critic_panel",
        title="Review xylophone",
        status=WorkStatus.REVIEW,
    )
    fake_svc = _FakeSvc(tasks_factory=lambda: [review_task], flow=_flow())

    monkeypatch.setattr("pollypm.cockpit_tasks.create_tmux_client", lambda: _FakeTmux([]))

    app = PollyTasksApp(env["config_path"], "demo")
    app._get_svc = lambda: fake_svc  # type: ignore[method-assign]

    async def body() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            await pilot.press("/")
            await pilot.press("x")
            await pilot.pause()

            assert app.search_input.has_focus
            assert app.search_input.value == "x"
            assert app._search_query == "x"
            assert app._active_reject_modal is None
            assert fake_svc.approve_calls == []
            assert fake_svc.reject_calls == []

            await pilot.press("backspace")
            await pilot.pause()
            assert app.search_input.has_focus
            assert app.search_input.value == ""
            assert app._search_query == ""

            await pilot.press("a")
            await pilot.pause()
            assert app.search_input.value == "a"
            assert fake_svc.approve_calls == []

            await pilot.press("escape")
            await pilot.pause()
            assert app.search_input.has_focus
            assert app.search_input.value == ""
            assert app._search_query == ""

    _run(body())


def test_task_active_search_filter_blocks_review_shortcuts_when_table_focused(
    env, monkeypatch,
) -> None:
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_tasks import PollyTasksApp

    review_task = _task(
        node_id="critic_panel",
        title="Review xylophone",
        status=WorkStatus.REVIEW,
    )
    fake_svc = _FakeSvc(tasks_factory=lambda: [review_task], flow=_flow())

    monkeypatch.setattr("pollypm.cockpit_tasks.create_tmux_client", lambda: _FakeTmux([]))

    app = PollyTasksApp(env["config_path"], "demo")
    app._get_svc = lambda: fake_svc  # type: ignore[method-assign]

    async def body() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            app._search_query = "q"
            app.search_input.value = "q"
            app._render_table(select_first=True)
            app.task_table.focus()
            await pilot.pause()

            await pilot.press("x")
            await pilot.pause()
            assert app._active_reject_modal is None
            assert fake_svc.reject_calls == []
            assert app.search_input.has_focus
            assert app.search_input.value == "q"

            app.task_table.focus()
            await pilot.press("backspace")
            await pilot.pause()
            assert app.search_input.has_focus
            assert app.search_input.value == ""
            assert app._search_query == ""

            app._status_filter = "review"
            app._search_query = "q"
            app.search_input.value = "q"
            app._sync_filter_buttons()
            app._render_table(select_first=True)
            app.task_table.focus()
            await pilot.press("c")
            await pilot.pause()
            assert app._status_filter == "all"
            assert app.search_input.value == ""
            assert app._search_query == ""
            assert fake_svc.approve_calls == []

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
            inline_diff = app.query_one("#task-review-inline-diff", Static)
            toggle = app.query_one("#task-review-diff-toggle", Button)
            diff = str(app.query_one("#task-review-diff", Static).render())

            assert "Review Artifact" in review
            assert "Russell: 8/10" in confidence
            assert not inline_diff.display
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


def test_task_review_diff_prefers_artifact_over_git_fallback(tmp_path, monkeypatch) -> None:
    from pollypm.cockpit_task_review import (
        ReviewArtifact,
        ReviewSection,
        load_task_review_diff,
    )

    artifact = ReviewArtifact(
        title="Review Artifact",
        summary="Review the change before approving it.",
        sections=[
            ReviewSection(
                title="Project Plan",
                path=Path("docs/project-plan.md"),
                body=(
                    "Plan notes.\n\n"
                    "```diff\n"
                    "--- a/src/app.py\n"
                    "+++ b/src/app.py\n"
                    "@@ -1 +1 @@\n"
                    "-print('old')\n"
                    "+print('new')\n"
                    "```\n"
                ),
            )
        ],
    )
    task = _task(
        node_id="critic_panel",
        status=WorkStatus.REVIEW,
        flow_template_id="chat",
        external_refs={
            "github_pr": "123",
            "base_branch": "origin/main",
            "head_branch": "issue-663-inline-review-diff",
        },
    )

    def _unexpected_git_diff(*args, **kwargs):
        raise AssertionError("captured artifact diff should win before git fallback")

    monkeypatch.setattr(
        "pollypm.cockpit_task_review.subprocess.run",
        _unexpected_git_diff,
    )

    bundle = load_task_review_diff(task, tmp_path, review_artifact=artifact)
    assert bundle is not None
    assert bundle.source_label == "Review Artifact Diff"
    assert len(bundle.files) == 1
    assert bundle.files[0].path == "src/app.py"
    assert "-print('old')" in bundle.files[0].patch
    assert "+print('new')" in bundle.files[0].patch


def test_task_review_diff_falls_back_to_git_for_pr_linked_tasks(tmp_path, monkeypatch) -> None:
    from pollypm.cockpit_task_review import load_task_review_diff

    task = _task(
        node_id="critic_panel",
        status=WorkStatus.REVIEW,
        flow_template_id="chat",
        external_refs={"github_pr": "123"},
    )

    def fake_run(cmd, *, capture_output, text, check):  # noqa: ANN001
        assert cmd[-1] == "origin/main..feature/notesy"
        assert capture_output is True
        assert text is True
        assert check is False
        return type(
            "Proc",
            (),
            {
                "returncode": 0,
                "stdout": (
                    "diff --git a/src/app.py b/src/app.py\n"
                    "--- a/src/app.py\n"
                    "+++ b/src/app.py\n"
                    "@@ -1 +1 @@\n"
                    "-print('old')\n"
                    "+print('new')\n"
                ),
                "stderr": "",
            },
        )()

    monkeypatch.setattr("pollypm.cockpit_task_review.subprocess.run", fake_run)

    bundle = load_task_review_diff(
        task,
        tmp_path,
        review_artifact=None,
        active_branch="feature/notesy",
    )

    assert bundle is not None
    assert bundle.source_label == "git diff origin/main..feature/notesy"
    assert len(bundle.files) == 1
    assert bundle.files[0].path == "src/app.py"
    assert "+print('new')" in bundle.files[0].patch


def test_task_review_tab_shows_inline_review_diff_without_resubmission_toggle(
    env, monkeypatch,
) -> None:
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_tasks import PollyTasksApp
    from rich.syntax import Syntax
    from textual.widgets import TabbedContent

    plan_path = env["project_path"] / "docs" / "project-plan.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        "# Notesy Plan\n\n"
        "Review the embedded diff inline.\n\n"
        "```diff\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1 +1 @@\n"
        "-print('old')\n"
        "+print('new')\n"
        "```\n"
    )

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
            inline_diff_header = app.query_one("#task-review-inline-diff-header", Static)
            inline_diff_expand = app.query_one("#task-review-inline-diff-expand", Button)
            inline_diff = app.query_one("#task-review-inline-diff", Static)
            toggle = app.query_one("#task-review-diff-toggle", Button)
            renderable = inline_diff.content

            assert tabs.active == "task-tab-review"
            assert "Review Artifact" in review
            assert inline_diff_header.display
            assert "Code Diff" in str(inline_diff_header.render())
            assert "src/app.py" in str(inline_diff_header.render())
            assert inline_diff.display
            assert isinstance(renderable, Syntax)
            assert renderable.line_numbers
            assert "-print('old')" in renderable.code
            assert "+print('new')" in renderable.code
            assert not inline_diff_expand.display
            assert not toggle.display

    _run(body())


def test_task_review_tab_uses_git_diff_navigation_and_expand(env, monkeypatch) -> None:
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_tasks import PollyTasksApp
    from rich.syntax import Syntax
    from textual.widgets import TabbedContent

    git_calls: list[list[str]] = []
    large_lines = "\n".join(f"+big line {idx}" for idx in range(520))
    git_diff = (
        "diff --git a/src/app.py b/src/app.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1 +1 @@\n"
        "-print('old')\n"
        "+print('new')\n"
        "diff --git a/src/big.py b/src/big.py\n"
        "index 3333333..4444444 100644\n"
        "--- a/src/big.py\n"
        "+++ b/src/big.py\n"
        "@@ -0,0 +1,520 @@\n"
        f"{large_lines}\n"
    )

    def _fake_git_diff(cmd, **kwargs):
        git_calls.append(list(cmd))
        return type(
            "Proc",
            (),
            {"returncode": 0, "stdout": git_diff, "stderr": ""},
        )()

    review_task = _task(
        node_id="critic_panel",
        status=WorkStatus.REVIEW,
        title="Review git-backed diff",
        flow_template_id="chat",
        external_refs={
            "github_pr": "123",
            "base_branch": "origin/main",
            "head_branch": "issue-663-inline-review-diff",
        },
    )
    fake_svc = _FakeSvc(tasks_factory=lambda: [review_task], flow=_flow())

    monkeypatch.setattr("pollypm.cockpit_tasks.create_tmux_client", lambda: _FakeTmux([]))
    monkeypatch.setattr("pollypm.cockpit_task_review.subprocess.run", _fake_git_diff)

    app = PollyTasksApp(env["config_path"], "demo")
    app._get_svc = lambda: fake_svc  # type: ignore[method-assign]

    async def body() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            tabs = app.query_one("#task-tabs", TabbedContent)
            inline_diff_header = app.query_one("#task-review-inline-diff-header", Static)
            inline_diff_expand = app.query_one("#task-review-inline-diff-expand", Button)
            inline_diff = app.query_one("#task-review-inline-diff", Static)

            assert tabs.active == "task-tab-review"
            assert git_calls
            assert git_calls[0][-1] == "origin/main..issue-663-inline-review-diff"
            assert "src/app.py" in str(inline_diff_header.render())

            renderable = inline_diff.content
            assert isinstance(renderable, Syntax)
            assert renderable.line_numbers
            assert "+print('new')" in renderable.code
            assert not inline_diff_expand.display

            await pilot.press("]")
            await pilot.press("f")
            await pilot.pause()

            header_text = str(inline_diff_header.render())
            assert "git diff origin/main..issue-663-inline-review-diff" in header_text
            assert "src/big.py" in header_text
            assert inline_diff_expand.display
            assert "Expand (+" in str(inline_diff_expand.label)

            renderable = inline_diff.content
            assert isinstance(renderable, Syntax)
            assert "+big line 494" in renderable.code
            assert "+big line 519" not in renderable.code

            inline_diff_expand.press()
            await pilot.pause()
            renderable = inline_diff.content
            assert isinstance(renderable, Syntax)
            assert "+big line 519" in renderable.code
            assert str(inline_diff_expand.label) == "Collapse"

            await pilot.press("[")
            await pilot.press("f")
            await pilot.pause()
            assert "src/app.py" in str(inline_diff_header.render())

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
        context_by_id={("demo/1", None): work_task.context},
    )

    monkeypatch.setattr("pollypm.cockpit_tasks.create_tmux_client", lambda: _FakeTmux([]))

    app = PollyTasksApp(env["config_path"], "demo")
    app._get_svc = lambda: fake_svc  # type: ignore[method-assign]

    async def body() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            rows = _table_rows(app.query_one("#tasks-table", DataTable))
            header = str(app.query_one("#task-header", Static).render())
            overview = str(app.query_one("#task-detail", Static).render())

            assert len(rows) == 1
            assert rows[0][1] == "in_progress"
            assert rows[0][2] == "🔄 🟠 Ship Notesy visibility"
            assert rows[0][4] == "synthesize"
            assert "in_progress" in header
            assert "Unread rejection feedback in inbox" in header
            assert overview.index("In plain English") < overview.index("Status")
            assert "This task has unread reviewer feedback in the inbox." in overview
            assert "The current owner may still be reworking it." in overview
            assert "Inbox Feedback" in overview
            assert "Artifact   Unread rejection feedback (demo/99)" in overview
            assert "Need better rollback coverage." in overview

    _run(body())


def test_task_app_filters_pm_notify_inbox_tasks(env, monkeypatch) -> None:
    """``pm notify --priority immediate`` materialises a chat-flow task
    so the cockpit inbox can render the architect's plan_review handoff
    with ``v open · d discuss · A approve`` — but those rows have no
    node-level transition affordance, so they MUST stay out of the
    Tasks pane (#1003). Otherwise users see ``<project>/7 "Plan ready
    for review: <project>"`` as a draft row that nothing in the Tasks
    UI can act on.
    """
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_tasks import PollyTasksApp

    work_task = _task(node_id="synthesize", title="Plan project demo")
    notify_task = _task(
        task_number=7,
        node_id=None,  # chat-flow notify rows park outside the flow graph
        title="Plan ready for review: demo",
        status=WorkStatus.DRAFT,
        flow_template_id="chat",
        labels=[
            "plan_review",
            "project:demo",
            "plan_task:demo/1",
            "explainer:/tmp/demo-plan-review.html",
            "notify",
            "notify_message:42",
        ],
        description=(
            "Plan: docs/project-plan.md\nExplainer: /tmp/demo-plan-review.html\n"
            "Press v to open, d to discuss, A to approve."
        ),
    )
    fake_svc = _FakeSvc(
        tasks_factory=lambda: [work_task, notify_task],
        flow=_flow(),
        context_by_id={("demo/1", None): work_task.context},
    )

    monkeypatch.setattr(
        "pollypm.cockpit_tasks.create_tmux_client", lambda: _FakeTmux([]),
    )

    app = PollyTasksApp(env["config_path"], "demo")
    app._get_svc = lambda: fake_svc  # type: ignore[method-assign]

    async def body() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            rows = _table_rows(app.query_one("#tasks-table", DataTable))

            # Only the real plan_project work task is visible — the
            # ``pm notify``-backed plan_review row stays inbox-only.
            assert len(rows) == 1
            row_text = " ".join(rows[0])
            assert "Plan project demo" in row_text
            assert "Plan ready for review" not in row_text

    _run(body())


def test_task_app_leads_user_review_with_plain_language_summary(
    env, monkeypatch,
) -> None:
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_tasks import PollyTasksApp

    review_task = _task(
        node_id="critic_panel",
        title="Review Notesy plan",
        status=WorkStatus.REVIEW,
    )
    fake_svc = _FakeSvc(
        tasks_factory=lambda: [review_task],
        flow=_flow(),
        owner_by_id={"demo/1": "human"},
    )

    monkeypatch.setattr("pollypm.cockpit_tasks.create_tmux_client", lambda: _FakeTmux([]))

    app = PollyTasksApp(env["config_path"], "demo")
    app._get_svc = lambda: fake_svc  # type: ignore[method-assign]

    async def body() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            header = str(app.query_one("#task-header", Static).render())
            overview = str(app.query_one("#task-detail", Static).render())

            assert "user-review" in header
            assert overview.index("In plain English") < overview.index("Status")
            assert "Review needed: the project plan is waiting" in overview
            assert "approve it or reject with the changes you need" in overview

    _run(body())


def test_task_app_leads_on_hold_feedback_with_paused_review_summary(
    env, monkeypatch,
) -> None:
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_tasks import PollyTasksApp

    work_task = _task(
        node_id="code_review",
        title="Scraper infrastructure",
        status=WorkStatus.ON_HOLD,
        executions=[
            FlowNodeExecution(
                task_id="demo/1",
                node_id="code_review",
                visit=1,
                status=ExecutionStatus.COMPLETED,
                decision=Decision.REJECTED,
                decision_reason=(
                    "Confidence: 6/10 — core is correct, but two gaps "
                    "block approval:\n\n"
                    "1. needs_real_browser not demonstrated via CLI. Fix: "
                    "run python -m scraper.run --venue <id> through CDP and "
                    "show scrape_runs status=ok.\n\n"
                    "2. eventbrite_embed strategy not implemented. Fix: "
                    "implement it or record explicit scope acknowledgment "
                    "to defer it."
                ),
            ),
        ],
    )
    work_task.context = [
        ContextEntry(
            actor="review_summary",
            timestamp=datetime(2026, 4, 20, 17, 20, tzinfo=UTC),
            text=(
                "You're being asked to approve two deviations from the original plan.\n"
                "1. Browser requirement: strike browser use from v1 because the "
                "current path meets the goal without it.\n"
                "2. Eventbrite: move it to v2 so v1 can get to testing.\n"
                "Approve if those tradeoffs are acceptable for this version; "
                "reject if either requirement still needs to be met now."
            ),
            entry_type="plain_summary",
        )
    ]
    feedback_task = _task(
        task_number=99,
        node_id="chat",
        title="Rejected demo/1 — Scraper infrastructure",
        flow_template_id="chat",
        labels=["review_feedback", "task:demo/1", "project:demo"],
        description=(
            "Confidence: 6/10 — core is correct, but two gaps block approval."
        ),
    )
    fake_svc = _FakeSvc(
        tasks_factory=lambda: [work_task, feedback_task],
        flow=_flow(),
        context_by_id={("demo/1", None): work_task.context},
    )

    monkeypatch.setattr("pollypm.cockpit_tasks.create_tmux_client", lambda: _FakeTmux([]))

    app = PollyTasksApp(env["config_path"], "demo")
    app._get_svc = lambda: fake_svc  # type: ignore[method-assign]

    async def body() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            overview = str(app.query_one("#task-detail", Static).render())

            assert overview.index("In plain English") < overview.index("Status")
            assert (
                "You're being asked to approve two deviations from the original plan."
            ) in overview
            assert "Browser requirement: strike browser use from v1" in overview
            assert "Eventbrite: move it to v2 so v1 can get to testing" in overview
            assert "Confidence: 6/10" in overview

    _run(body())


def test_task_app_keeps_autoreview_state_distinct_from_feedback_inbox(
    env, monkeypatch,
) -> None:
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_tasks import PollyTasksApp

    review_task = _task(
        node_id="critic_panel",
        title="Review Notesy visibility",
        status=WorkStatus.REVIEW,
    )
    feedback_task = _task(
        task_number=99,
        node_id="chat",
        title="Rejected demo/1 — Review Notesy visibility",
        flow_template_id="chat",
        labels=["review_feedback", "task:demo/1", "project:demo"],
        description="Need stronger rollout coverage before approval.",
    )
    fake_svc = _FakeSvc(
        tasks_factory=lambda: [review_task, feedback_task],
        flow=_flow(),
        owner_by_id={"demo/1": "russell"},
    )

    monkeypatch.setattr("pollypm.cockpit_tasks.create_tmux_client", lambda: _FakeTmux([]))

    app = PollyTasksApp(env["config_path"], "demo")
    app._get_svc = lambda: fake_svc  # type: ignore[method-assign]

    async def body() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            rows = _table_rows(app.query_one("#tasks-table", DataTable))
            header = str(app.query_one("#task-header", Static).render())
            overview = str(app.query_one("#task-detail", Static).render())

            assert len(rows) == 1
            assert rows[0][1] == "autoreview"
            assert rows[0][2] == "🔄 🟠 Review Notesy visibility"
            assert rows[0][4] == "critic_panel"
            assert "autoreview" in header
            assert "Unread rejection feedback in inbox" in header
            assert "Status     autoreview" in overview
            assert "Artifact   Unread rejection feedback (demo/99)" in overview
            assert "Need stronger rollout coverage before approval." in overview

    _run(body())


def test_task_app_sorts_user_review_before_autoreview(
    env, monkeypatch,
) -> None:
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_tasks import PollyTasksApp

    human_low = _task(
        task_number=1,
        node_id="critic_panel",
        title="Human low-priority review",
        status=WorkStatus.REVIEW,
        priority=Priority.LOW,
        updated_at=datetime(2026, 4, 20, 16, 0, tzinfo=UTC),
    )
    auto_critical = _task(
        task_number=2,
        node_id="critic_panel",
        title="Russell critical autoreview",
        status=WorkStatus.REVIEW,
        priority=Priority.CRITICAL,
        updated_at=datetime(2026, 4, 20, 18, 0, tzinfo=UTC),
    )
    human_high = _task(
        task_number=3,
        node_id="critic_panel",
        title="Human high-priority review",
        status=WorkStatus.REVIEW,
        priority=Priority.HIGH,
        updated_at=datetime(2026, 4, 20, 17, 0, tzinfo=UTC),
    )
    fake_svc = _FakeSvc(
        tasks_factory=lambda: [auto_critical, human_low, human_high],
        flow=_flow(),
        owner_by_id={
            "demo/1": "human",
            "demo/2": "russell",
            "demo/3": "human",
        },
    )

    monkeypatch.setattr("pollypm.cockpit_tasks.create_tmux_client", lambda: _FakeTmux([]))

    app = PollyTasksApp(env["config_path"], "demo")
    app._get_svc = lambda: fake_svc  # type: ignore[method-assign]

    async def body() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            rows = _table_rows(app.query_one("#tasks-table", DataTable))

            assert [row[1] for row in rows] == [
                "user-review",
                "user-review",
                "autoreview",
            ]
            assert [row[2] for row in rows] == [
                "🟠 Human high-priority review",
                "🟢 Human low-priority review",
                "🔴 Russell critical autoreview",
            ]

    _run(body())


def test_task_app_surfaces_plan_blocked_queued_tasks(env, monkeypatch) -> None:
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_tasks import PollyTasksApp

    work_task = _task(
        node_id="implement",
        title="Implement planned child",
        status=WorkStatus.QUEUED,
        flow_template_id="standard",
    )
    plan_task = _task(
        task_number=2,
        node_id="user_approval",
        title="Approve project plan",
        status=WorkStatus.QUEUED,
        flow_template_id="plan_project",
    )
    fake_svc = _FakeSvc(
        tasks_factory=lambda: [work_task, plan_task],
        flow=_flow(),
    )

    monkeypatch.setattr("pollypm.cockpit_tasks.create_tmux_client", lambda: _FakeTmux([]))

    app = PollyTasksApp(env["config_path"], "demo")
    app._get_svc = lambda: fake_svc  # type: ignore[method-assign]

    async def body() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            rows = _table_rows(app.query_one("#tasks-table", DataTable))
            header = str(app.query_one("#task-header", Static).render())
            overview = str(app.query_one("#task-detail", Static).render())

            assert rows[0][1] == "waiting_on_plan"
            assert rows[0][2] == "🟠 Implement planned child"
            assert "waiting_on_plan" in header
            assert "Plan Gate" in overview
            assert "Approve    demo/2 · plan_project/user_approval" in overview

    _run(body())


def test_task_app_honours_per_project_enforce_plan_false(env, monkeypatch) -> None:
    """Per-project ``enforce_plan=false`` keeps the task list out of waiting_on_plan.

    Sam's media project sets ``[planner].enforce_plan = false`` in its
    project-local pollypm.toml because there's no project-wide plan to
    write — it's a one-off cleanup task. Three gate sites used to need
    fixing for this to take effect end-to-end: rail rollup display,
    sweeper worker pickup, and (this one) the task list view in
    cockpit_tasks.PollyTasksApp.
    """
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_tasks import PollyTasksApp

    # Flip the per-project bypass on the demo fixture.
    local_cfg_dir = env["project_path"] / ".pollypm" / "config"
    local_cfg_dir.mkdir(parents=True)
    (local_cfg_dir / "project.toml").write_text(
        '[project]\ndisplay_name = "Demo"\n\n'
        "[planner]\nenforce_plan = false\n"
    )

    work_task = _task(
        node_id="implement",
        title="Implement planned child",
        status=WorkStatus.QUEUED,
        flow_template_id="standard",
    )
    fake_svc = _FakeSvc(tasks_factory=lambda: [work_task], flow=_flow())

    monkeypatch.setattr("pollypm.cockpit_tasks.create_tmux_client", lambda: _FakeTmux([]))

    app = PollyTasksApp(env["config_path"], "demo")
    app._get_svc = lambda: fake_svc  # type: ignore[method-assign]

    async def body() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            rows = _table_rows(app.query_one("#tasks-table", DataTable))
            # Without bypass this would render "waiting_on_plan" because
            # the project has no plan.md / approved plan_project task.
            assert rows[0][1] == "queued", (
                f"expected 'queued' with per-project bypass, got {rows[0][1]!r}"
            )

    _run(body())


def test_task_app_surfaces_recent_sweeper_ping(env, monkeypatch) -> None:
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_tasks import PollyTasksApp
    from pollypm.plugins_builtin.task_assignment_notify.handlers.sweep import (
        SWEEPER_PING_CONTEXT_ENTRY_TYPE,
    )

    task = _task(node_id="synthesize", title="Recently swept")
    task.context = [
        ContextEntry(
            actor="sweeper",
            timestamp=datetime.now(UTC),
            text="task_assignment.sweep:deduped",
            entry_type=SWEEPER_PING_CONTEXT_ENTRY_TYPE,
        )
    ]
    fake_svc = _FakeSvc(tasks_factory=lambda: [task], flow=_flow())

    monkeypatch.setattr("pollypm.cockpit_tasks.create_tmux_client", lambda: _FakeTmux([]))

    app = PollyTasksApp(env["config_path"], "demo")
    app._get_svc = lambda: fake_svc  # type: ignore[method-assign]

    async def body() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            rows = _table_rows(app.query_one("#tasks-table", DataTable))
            header = str(app.query_one("#task-header", Static).render())
            overview = str(app.query_one("#task-detail", Static).render())

            assert rows[0][2] == "↻ 🟠 Recently swept"
            assert "Sweeper pinged in the last minute" in header
            assert "Sweeper    pinged in the last minute" in overview

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
    # #955 — keep the undo window generous enough that a busy full-suite
    # ordering can't race the banner-render against the auto-commit timer
    # (the 0.2s window was tight enough that the banner could clear before
    # the test's `pause()` returned). We trigger the commit explicitly
    # below, so a long window does not slow the test down.
    monkeypatch.setattr("pollypm.cockpit_tasks._PENDING_UNDO_SECONDS", 5.0)

    app = PollyTasksApp(env["config_path"], "demo")
    app._get_svc = lambda: fake_svc  # type: ignore[method-assign]

    async def body() -> None:
        from time import monotonic

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
            # #955 — poll for the banner text instead of relying on a
            # single ``pilot.pause()`` to cover the render. The previous
            # fixed-pause shape flaked under busy full-suite orderings.
            deadline = monotonic() + 10.0
            banner = ""
            while monotonic() < deadline:
                await pilot.pause()
                banner = str(app.query_one("#tasks-banner", Static).render())
                if "APPROVED" in banner:
                    break
            # #767 — banner now has a bolder, more visible shape:
            # "APPROVED · 2 tasks   Undo press Z (Ns)".
            assert "APPROVED" in banner
            assert "2 tasks" in banner
            assert "Undo" in banner
            assert fake_svc.approve_calls == []

            # #955 — drive the commit deterministically rather than
            # waiting on the (now generous) undo timer to fire.
            app._commit_pending_review_action()
            await pilot.pause()
            assert fake_svc.approve_calls == [
                ("demo/1", "user", "Approved from task cockpit"),
                ("demo/2", "user", "Approved from task cockpit"),
            ]

            await pilot.press("space")
            await pilot.pause()
            await pilot.press("X")
            await pilot.pause()
            # #955 — same deterministic commit for the reject path.
            app._commit_pending_review_action()
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
    # #955 — this test checks the banner/undo state explicitly, so keep
    # the undo window long and commit manually. A 0.2s timer can clear the
    # banner before a busy full-suite ``pilot.pause()`` returns.
    monkeypatch.setattr("pollypm.cockpit_tasks._PENDING_UNDO_SECONDS", 5.0)
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
        from time import monotonic

        async def wait_for_banner(expected: str) -> str:
            deadline = monotonic() + 10.0
            banner = ""
            while monotonic() < deadline:
                await pilot.pause()
                banner = str(app.query_one("#tasks-banner", Static).render())
                if expected in banner:
                    return banner
            return banner

        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            # #881 — single-letter `a` arms the destructive action on
            # first press. The second press within 3s confirms.
            await pilot.press("a")
            await pilot.press("a")
            banner = await wait_for_banner("APPROVED")
            # #767 — banner reads "APPROVED · #1   Undo press Z (Ns)".
            assert "APPROVED" in banner
            assert "Undo" in banner
            assert fake_svc.approve_calls == []
            # #767 — in-place button morph: the Approve button itself
            # now reads ``Undo (Ns)`` so the click target is at the
            # user's gaze. Reject button disables to prevent
            # double-firing while the pending action is in flight.
            assert "Undo" in str(app.approve_button.label)
            assert app.approve_button.has_class("-undo")
            assert app.reject_button.disabled is True

            await pilot.press("z")
            await pilot.pause()
            assert fake_svc.approve_calls == []
            # After undo, buttons restore to their default labels.
            assert str(app.approve_button.label) == "Approve"
            assert not app.approve_button.has_class("-undo")
            assert app.reject_button.disabled is False

            await pilot.press("a")
            await pilot.press("a")
            banner = await wait_for_banner("APPROVED")
            assert "APPROVED" in banner
            app._commit_pending_review_action()
            await pilot.pause()
            assert fake_svc.approve_calls == [
                ("demo/1", "user", "Approved from task cockpit")
            ]
            assert notification_calls == ["demo/1"]
            assert any(
                call[:4] == ["gh", "pr", "merge", "123"]
                for call in merge_calls
            )

    _run(body())


def test_task_app_banner_includes_progress_bar_and_success_state(env, monkeypatch) -> None:
    """#767: the redesigned banner renders a visible progress bar
    underneath the APPROVED line, uses the success (green) CSS class
    by default, and is tall enough (multi-line) to be noticeable."""
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_tasks import PollyTasksApp

    task = _task(node_id="critic_panel", status=WorkStatus.REVIEW)
    fake_svc = _FakeSvc(tasks_factory=lambda: [task], flow=_flow())

    monkeypatch.setattr("pollypm.cockpit_tasks.create_tmux_client", lambda: _FakeTmux([]))
    # Long window so we can inspect the banner mid-countdown without
    # racing the auto-commit.
    monkeypatch.setattr("pollypm.cockpit_tasks._PENDING_UNDO_SECONDS", 60.0)

    app = PollyTasksApp(env["config_path"], "demo")
    app._get_svc = lambda: fake_svc  # type: ignore[method-assign]

    async def body() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            # #881 — arm + confirm.
            await pilot.press("a")
            await pilot.press("a")
            await pilot.pause()

            banner_widget = app.query_one("#tasks-banner", Static)
            rendered = str(banner_widget.render())
            # Multi-line: APPROVED line + progress-bar line.
            assert "\n" in rendered, (
                f"banner should span multiple lines for visibility; got {rendered!r}"
            )
            # Progress bar uses full / empty block glyphs.
            assert "█" in rendered or "░" in rendered, (
                f"banner should render a progress bar; got {rendered!r}"
            )
            # Approve decision → success class, NOT reject class.
            assert not banner_widget.has_class("-reject")

    _run(body())


def test_task_approve_button_fires_on_single_click(env, monkeypatch) -> None:
    """#900 — visible Approve button is explicit intent and must NOT
    inherit the #881 keyboard arming. One click opens the undo
    banner; the existing 5-second pending-review window is the
    canonical safety net for accidental clicks."""
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
            # Single click must open the undo banner — not arm.
            app.approve_button.press()
            await pilot.pause()
            assert app._pending_review_action is not None
            banner = str(app.query_one("#tasks-banner", Static).render())
            assert "APPROVED" in banner
            assert "Undo" in banner

    _run(body())


def test_task_reject_button_fires_on_single_click(env, monkeypatch) -> None:
    """#900 — visible Reject button is explicit intent. One click
    opens the reject-reason modal; no arming."""
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_tasks import PollyTasksApp

    task = _task(node_id="critic_panel", status=WorkStatus.REVIEW)
    fake_svc = _FakeSvc(tasks_factory=lambda: [task], flow=_flow())

    monkeypatch.setattr("pollypm.cockpit_tasks.create_tmux_client", lambda: _FakeTmux([]))

    app = PollyTasksApp(env["config_path"], "demo")
    app._get_svc = lambda: fake_svc  # type: ignore[method-assign]

    async def body() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            app.reject_button.press()
            await pilot.pause()
            # Single click pushes the reject-reason modal — the
            # arming gate would have refused on first press.
            assert app._active_reject_modal is not None

    _run(body())


def test_task_app_reject_banner_uses_reject_class(env, monkeypatch) -> None:
    """Rejecting paints the banner red (distinct color class) so the
    user immediately reads the decision visually."""
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_tasks import PollyTasksApp
    from pollypm.cockpit_tasks import _PendingReviewAction
    from time import monotonic

    task = _task(node_id="critic_panel", status=WorkStatus.REVIEW)
    fake_svc = _FakeSvc(tasks_factory=lambda: [task], flow=_flow())

    monkeypatch.setattr("pollypm.cockpit_tasks.create_tmux_client", lambda: _FakeTmux([]))
    monkeypatch.setattr("pollypm.cockpit_tasks._PENDING_UNDO_SECONDS", 60.0)

    app = PollyTasksApp(env["config_path"], "demo")
    app._get_svc = lambda: fake_svc  # type: ignore[method-assign]

    async def body() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            # Drive _sync_banner with a reject-decision pending action.
            app._pending_review_action = _PendingReviewAction(
                task_ids=("demo/1",),
                task_numbers=(1,),
                decision="reject",
                reason="needs work",
                deadline=monotonic() + 60.0,
            )
            app._sync_banner()
            await pilot.pause()

            banner_widget = app.query_one("#tasks-banner", Static)
            rendered = str(banner_widget.render())
            assert "REJECTED" in rendered
            assert banner_widget.has_class("-reject")

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
            # #881 — single-letter `x` arms; second press confirms.
            await pilot.press("x")
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


# ---------------------------------------------------------------------------
# #1017 — within-pane click responsiveness (parallel of #974 rail responsiveness)
# ---------------------------------------------------------------------------


def test_task_click_paints_placeholder_immediately_when_load_is_slow(
    env, monkeypatch,
) -> None:
    """#1017 — clicking a task row must update the highlight + paint a
    "Loading…" placeholder *before* the slow DB / review-artifact load
    completes.

    Pre-fix: ``_show_detail`` did every ``svc.get`` / context / flow /
    worker-session / review-artifact / git-diff fetch synchronously on
    the UI thread. A typical click stalled 60ms-1s; the cold first
    click was ~1s thanks to gate-plugin discovery in the work-service
    constructor. Post-fix: the click handler bumps a request id, sets
    ``_selected_task_id``, paints a placeholder header, and dispatches
    the load to a worker thread.

    This test simulates a ~250ms slow svc by blocking ``_FakeSvc.get``,
    then asserts the click is acknowledged in the UI before the load
    finishes.
    """

    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_tasks import PollyTasksApp

    one = _task(node_id="research", title="First click target")
    two = _task(task_number=2, node_id="research", title="Slow click target")

    import threading
    import time as _time

    release = threading.Event()
    get_started = threading.Event()

    class _SlowSvc(_FakeSvc):
        def get(self, task_id):  # type: ignore[override]
            # Only stall the *second* task so the first click can fall
            # through to a normal render and we can deterministically
            # observe a pending second click before its load completes.
            if task_id == two.task_id:
                get_started.set()
                # Bound the wait so a runaway test still terminates.
                if not release.wait(timeout=5.0):
                    raise AssertionError("test never released slow svc.get")
            return super().get(task_id)

    fake_svc = _SlowSvc(tasks_factory=lambda: [one, two], flow=_flow())

    monkeypatch.setattr("pollypm.cockpit_tasks.create_tmux_client", lambda: _FakeTmux([]))

    app = PollyTasksApp(env["config_path"], "demo")
    app._get_svc = lambda: fake_svc  # type: ignore[method-assign]

    async def body() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            table = app.query_one("#tasks-table", DataTable)
            assert table.row_count == 2
            # First row was auto-selected on mount; its detail should
            # already be loaded (no slow path hit). Sanity check.
            assert app._selected_task_id == one.task_id
            seq_before = app._detail_request_seq

            # Move cursor to the second row — this is the within-pane
            # "click" the regression in #1017 covers. The row select
            # event triggers ``_show_detail``.
            t0 = _time.perf_counter()
            table.move_cursor(row=1, column=0, animate=False)
            # ``move_cursor`` posts a RowHighlighted message; pump it.
            await pilot.pause()
            ack_elapsed_ms = (_time.perf_counter() - t0) * 1000

            # The placeholder must appear *before* the slow load
            # finishes — assert the worker hasn't returned yet.
            assert get_started.wait(timeout=2.0), (
                "_get_svc/get should have been dispatched to a worker"
            )

            # Click acknowledgement: highlight tracks the new row,
            # the request id has been bumped, and the detail header
            # carries a "Loading…" hint so the user sees their click
            # land. Allow a generous bound — the assertion is not
            # "render finished in 100ms" but "click was acknowledged
            # before the slow data load completed".
            assert app._selected_task_id == two.task_id
            assert app._detail_request_seq == seq_before + 1
            header_now = str(app.detail_header.render())
            assert "Loading" in header_now, header_now
            # The header should reference the clicked task, not the
            # previously-selected one.
            assert "Slow click target" in header_now
            # And, critically, the click hand-off itself must be
            # responsive: the event-loop pump should return well under
            # the simulated svc latency.
            assert ack_elapsed_ms < 250, (
                f"click acknowledgement took {ack_elapsed_ms:.0f}ms, "
                "should be well under the simulated load latency"
            )

            # Release the slow load and let the real detail render.
            release.set()
            # Wait for the worker to finish and apply the result.
            deadline = _time.perf_counter() + 5.0
            while _time.perf_counter() < deadline:
                await pilot.pause()
                if "Slow click target" in str(app.detail_header.render()) and (
                    "Loading" not in str(app.detail_header.render())
                ):
                    break
            assert "Loading" not in str(app.detail_header.render())
            assert "Slow click target" in str(app.detail_header.render())

    _run(body())


def test_rapid_task_clicks_drop_stale_loads(env, monkeypatch) -> None:
    """#1017 — a click during an in-flight slow load must not be
    clobbered by the older load completing.

    Mirrors the rail-click ``_route_click_seq`` guard from #974 / #967:
    every click bumps ``_detail_request_seq`` synchronously, and the
    worker-thread completion checks the seq before applying the result.
    A stale (superseded) load is silently dropped.
    """

    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_tasks import PollyTasksApp

    one = _task(node_id="research", title="First click target")
    two = _task(task_number=2, node_id="research", title="Second click target")
    three = _task(task_number=3, node_id="research", title="Final click target")

    import threading

    release_first = threading.Event()
    release_second = threading.Event()
    second_started = threading.Event()

    class _GatedSvc(_FakeSvc):
        def get(self, task_id):  # type: ignore[override]
            if task_id == two.task_id:
                second_started.set()
                if not release_first.wait(timeout=5.0):
                    raise AssertionError("test never released first slow get")
            elif task_id == three.task_id:
                if not release_second.wait(timeout=5.0):
                    raise AssertionError("test never released second slow get")
            return super().get(task_id)

    fake_svc = _GatedSvc(tasks_factory=lambda: [one, two, three], flow=_flow())

    monkeypatch.setattr("pollypm.cockpit_tasks.create_tmux_client", lambda: _FakeTmux([]))

    app = PollyTasksApp(env["config_path"], "demo")
    app._get_svc = lambda: fake_svc  # type: ignore[method-assign]

    async def body() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            table = app.query_one("#tasks-table", DataTable)
            assert table.row_count == 3

            # Click row 2 (slow), then immediately click row 3 (also
            # slow) before row 2's load returns. Row 3 is the user's
            # intended final destination.
            table.move_cursor(row=1, column=0, animate=False)
            await pilot.pause()
            assert second_started.wait(timeout=2.0)

            table.move_cursor(row=2, column=0, animate=False)
            await pilot.pause()

            # Highlight already tracks row 3.
            assert app._selected_task_id == three.task_id

            # Now release row 2's load. Its `_apply_detail_result`
            # must be dropped because the seq has moved on — so the
            # selected_task_id and header must continue tracking row 3.
            release_first.set()
            # Pump events; the dropped result should produce no
            # observable header swap.
            for _ in range(5):
                await pilot.pause()
            assert app._selected_task_id == three.task_id
            assert "Final click target" in str(app.detail_header.render())

            # Now release row 3's load; it should win.
            release_second.set()
            from time import perf_counter as _pc

            deadline = _pc() + 5.0
            while _pc() < deadline:
                await pilot.pause()
                header_text = str(app.detail_header.render())
                if (
                    "Final click target" in header_text
                    and "Loading" not in header_text
                ):
                    break
            header_text = str(app.detail_header.render())
            assert "Final click target" in header_text
            assert "Loading" not in header_text

    _run(body())
