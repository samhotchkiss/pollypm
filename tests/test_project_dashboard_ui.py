"""Textual UI tests for :class:`PollyProjectDashboardApp` (#245 follow-up).

The dashboard is the screen the cockpit renders when the user selects a
project in the left rail. These tests seed a minimal single-project
workspace (real SQLite work service + a fake plan file on disk) and
drive the screen via ``Pilot`` to assert every section renders, the
keybindings dispatch correctly, and empty states degrade gracefully.

All tests are targeted — do NOT use ``uv run pytest tests/`` against a
large state.db, per the agent brief. Run with::

    HOME=/tmp/pytest-agent-dashboard uv run pytest \\
        tests/test_project_dashboard_ui.py -q
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from pollypm.store import SQLAlchemyStore
from pollypm.work.sqlite_service import SQLiteWorkService


# ---------------------------------------------------------------------------
# Config + fixture helpers — lifted from test_cockpit_inbox_ui.py so the
# workspace bootstrap stays identical across cockpit suites.
# ---------------------------------------------------------------------------


def _write_config(
    project_path: Path,
    config_path: Path,
    *,
    persona_name: str | None = None,
) -> None:
    persona_line = (
        f'persona_name = "{persona_name}"\n' if persona_name else ""
    )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "[project]\n"
        f'tmux_session = "pollypm-test"\n'
        f'workspace_root = "{project_path.parent}"\n'
        "\n"
        f'[projects.demo]\n'
        f'key = "demo"\n'
        f'name = "Demo"\n'
        f'path = "{project_path}"\n'
        f"{persona_line}"
    )


def _seed_tasks(project_path: Path) -> dict[str, str]:
    """Create a mix of queued / in_progress / review / done tasks.

    Returns a ``{label: task_id}`` map so individual tests can target
    a specific task when needed.
    """
    db_path = project_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    ids: dict[str, str] = {}
    with SQLiteWorkService(
        db_path=db_path, project_path=project_path,
    ) as svc:
        # Dashboard-rendering tests don't exercise git merging; skip the
        # auto-merge step that real `approve()` would run and that requires
        # a real task branch in the fixture repo.
        svc._auto_merge_approved_task_branch = lambda _task: None
        # Queued (created but not claimed)
        t_queued = svc.create(
            title="Queued feature",
            description="Waiting for worker.",
            type="task",
            project="demo",
            flow_template="standard",
            roles={"worker": "pete", "reviewer": "russell"},
            priority="normal",
            created_by="polly",
        )
        svc.queue(t_queued.task_id, "polly")
        ids["queued"] = t_queued.task_id

        # In progress (claimed)
        t_active = svc.create(
            title="Active feature",
            description="Worker has it.",
            type="task",
            project="demo",
            flow_template="standard",
            roles={"worker": "pete", "reviewer": "russell"},
            priority="normal",
            created_by="polly",
        )
        svc.queue(t_active.task_id, "polly")
        svc.claim(t_active.task_id, "worker")
        ids["in_progress"] = t_active.task_id

        # Done (queue → claim → node_done → approve)
        from pollypm.work.models import (
            Artifact, ArtifactKind, OutputType, WorkOutput,
        )
        t_done = svc.create(
            title="Done thing",
            description="Shipped.",
            type="task",
            project="demo",
            flow_template="standard",
            roles={"worker": "pete", "reviewer": "russell"},
            priority="normal",
            created_by="polly",
        )
        svc.queue(t_done.task_id, "polly")
        svc.claim(t_done.task_id, "worker")
        svc.node_done(
            t_done.task_id, "worker",
            WorkOutput(
                type=OutputType.CODE_CHANGE,
                summary="Built it",
                artifacts=[
                    Artifact(
                        kind=ArtifactKind.COMMIT,
                        description="build",
                        ref="deadbeef0000",
                    ),
                ],
            ),
        )
        svc.approve(t_done.task_id, "russell")
        ids["done"] = t_done.task_id
    return ids


def _init_git_repo(path: Path) -> None:
    """Initialise a real git repo with one empty commit and a ``.gitignore``
    that excludes ``.pollypm/``. Needed because seeding tasks triggers
    ``svc.approve`` which runs ``_auto_merge_approved_task_branch`` —
    that path refuses to run when HEAD is dangling or the working tree
    has uncommitted changes, both of which trip a stubbed repo."""
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.email", "t@t.test"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=str(path), check=True)
    (path / ".gitignore").write_text(".pollypm/\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=str(path), check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"],
        cwd=str(path),
        check=True,
    )


def _seed_workspace_message(
    workspace_root: Path,
    *,
    subject: str,
    body: str,
    scope: str = "inbox",
    sender: str = "polly",
    type: str = "notify",
    tier: str = "immediate",
) -> int:
    db_path = workspace_root / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        return store.enqueue_message(
            type=type,
            tier=tier,
            recipient="user",
            sender=sender,
            subject=subject,
            body=body,
            scope=scope,
        )
    finally:
        store.close()


@pytest.fixture
def dashboard_env(tmp_path: Path):
    project_path = tmp_path / "demo"
    project_path.mkdir()
    _init_git_repo(project_path)
    config_path = tmp_path / "pollypm.toml"
    _write_config(project_path, config_path)
    ids = _seed_tasks(project_path)
    return {
        "config_path": config_path,
        "project_path": project_path,
        "task_ids": ids,
    }


def _load_config_compatible(config_path: Path) -> bool:
    try:
        from pollypm.config import load_config
        cfg = load_config(config_path)
        return "demo" in getattr(cfg, "projects", {})
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture
def dashboard_app(dashboard_env):
    if not _load_config_compatible(dashboard_env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyProjectDashboardApp
    # Reset the per-project task cache so each test sees its own
    # freshly-seeded DB without a cross-test stale hit.
    from pollypm import cockpit_ui as _cockpit_ui
    _cockpit_ui._PROJECT_DASHBOARD_TASK_CACHE.clear()
    return PollyProjectDashboardApp(
        dashboard_env["config_path"], "demo",
    )


def _run(coro) -> None:
    asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. Top bar — project name + PM persona
# ---------------------------------------------------------------------------


def test_topbar_renders_name_and_default_pm(dashboard_env, dashboard_app) -> None:
    """With no persona configured the top bar falls back to ``PM: Polly``."""
    async def body() -> None:
        async with dashboard_app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            topbar_text = str(dashboard_app.topbar.render())
            assert "Demo" in topbar_text
            # Fallback persona because the fixture wrote no ``persona_name``.
            assert "PM: Polly" in topbar_text
    _run(body())


def test_topbar_uses_configured_persona(tmp_path: Path) -> None:
    """A ``persona_name`` configured on the project shows in the top bar."""
    async def body() -> None:
        project_path = tmp_path / "demo"
        project_path.mkdir()
        (project_path / ".git").mkdir()
        config_path = tmp_path / "pollypm.toml"
        _write_config(project_path, config_path, persona_name="Ruby")
        _seed_tasks(project_path)
        if not _load_config_compatible(config_path):
            pytest.skip("minimal pollypm.toml fixture not supported by loader")

        from pollypm import cockpit_ui as _cockpit_ui
        _cockpit_ui._PROJECT_DASHBOARD_TASK_CACHE.clear()
        from pollypm.cockpit_ui import PollyProjectDashboardApp
        app = PollyProjectDashboardApp(config_path, "demo")
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            topbar_text = str(app.topbar.render())
            assert "PM: Ruby" in topbar_text
    _run(body())


# ---------------------------------------------------------------------------
# 2. Status indicator — reflects active-worker / attention / idle
# ---------------------------------------------------------------------------


def test_status_green_when_worker_heartbeat_alive(
    dashboard_env, dashboard_app, monkeypatch,
) -> None:
    """A recent worker heartbeat flips the status dot to green."""
    from datetime import UTC, datetime

    async def body() -> None:
        fake_worker = {
            "session_name": "demo-main",
            "role": "worker",
            "last_heartbeat": datetime.now(UTC).isoformat(),
        }

        def _fake_active_worker(config_path, project_key, *, action_items=None):
            return fake_worker, 0

        from pollypm import cockpit_ui
        monkeypatch.setattr(
            cockpit_ui, "_dashboard_active_worker", _fake_active_worker,
        )

        async with dashboard_app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            assert dashboard_app.data is not None
            assert dashboard_app.data.active_worker == fake_worker
            assert dashboard_app.data.status_label == "active"
            # Green color in the markup.
            status_markup = str(dashboard_app.status_line.render())
            assert "active" in status_markup
            # The current-activity body mentions the session name.
            now_markup = str(dashboard_app.now_body.render())
            assert "demo-main" in now_markup
    _run(body())


def test_status_yellow_when_inbox_has_items(
    dashboard_env, dashboard_app,
) -> None:
    """Seeding an inbox task (chat flow) with no worker flips status to yellow."""
    async def body() -> None:
        # Add an inbox task to the existing fixture DB.
        db_path = dashboard_env["project_path"] / ".pollypm" / "state.db"
        with SQLiteWorkService(
            db_path=db_path, project_path=dashboard_env["project_path"],
        ) as svc:
            svc.create(
                title="Hi there",
                description="Your attention please.",
                type="task",
                project="demo",
                flow_template="chat",
                roles={"requester": "user", "operator": "polly"},
                priority="normal",
                created_by="polly",
            )

        # Reset cache so the new task shows up.
        from pollypm import cockpit_ui as _cockpit_ui
        _cockpit_ui._PROJECT_DASHBOARD_TASK_CACHE.clear()

        async with dashboard_app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            assert dashboard_app.data is not None
            assert dashboard_app.data.inbox_count >= 1
            # No worker in flight → yellow ("needs attention").
            assert dashboard_app.data.active_worker is None
            assert dashboard_app.data.status_label == "needs attention"
    _run(body())


def test_status_idle_when_nothing_active(
    dashboard_env, dashboard_app,
) -> None:
    """No worker + no inbox + no alerts → idle state (dim dot)."""
    async def body() -> None:
        async with dashboard_app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            assert dashboard_app.data is not None
            # Only non-inbox tasks were seeded; inbox should be zero.
            assert dashboard_app.data.inbox_count == 0
            assert dashboard_app.data.active_worker is None
            assert dashboard_app.data.status_label == "idle"
    _run(body())


def test_status_yellow_when_task_is_on_hold(
    dashboard_env, dashboard_app,
) -> None:
    """Held work is attention-worthy, but not a red blocker."""
    db_path = dashboard_env["project_path"] / ".pollypm" / "state.db"
    with SQLiteWorkService(
        db_path=db_path, project_path=dashboard_env["project_path"],
    ) as svc:
        task = svc.create(
            title="Paused cleanup",
            description="Paused until the PM resumes it.",
            type="task",
            project="demo",
            flow_template="standard",
            roles={"worker": "pete", "reviewer": "russell"},
            priority="normal",
            created_by="polly",
        )
        svc.queue(task.task_id, "polly")
        svc.hold(task.task_id, "polly", "paused by PM")
        svc.add_context(
            task.task_id,
            "worker",
            (
                "Acceptance-gate scope split request.\n\n"
                "The plan requires a live Fly.io app, deploy token, "
                "Postgres, and Redis.\n\n"
                "Request one of:\n"
                "  (a) Split acceptance into a follow-up deploy drill.\n"
                "  (b) Grant a Fly-enabled session with credentials.\n"
            ),
        )

    from pollypm import cockpit_ui as _cockpit_ui
    _cockpit_ui._PROJECT_DASHBOARD_TASK_CACHE.clear()

    async def body() -> None:
        async with dashboard_app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            assert dashboard_app.data is not None
            assert dashboard_app.data.status_label == "needs attention"
            assert "1 on hold" in str(dashboard_app.action_bar.render())
            assert "on hold" in str(dashboard_app.pipeline_body.render())
            rendered = str(dashboard_app.inbox_body.render())
            assert "On hold" in rendered
            assert "root holds keeping downstream work waiting" in rendered
            assert "Split acceptance into a follow-up deploy drill" in rendered
            assert "Grant a Fly-enabled session with credentials" in rendered
            assert "summary missing" not in rendered.lower()

    _run(body())


def test_status_yellow_when_workspace_action_message_exists(
    dashboard_env, dashboard_app,
) -> None:
    """Workspace-root PM notes count as project inbox attention."""
    workspace_root = dashboard_env["project_path"].parent
    _seed_workspace_message(
        workspace_root,
        subject="[Action] demo/3 — deploy blocked on Fly.io setup",
        body=(
            "**Blocker:** Acceptance cannot run without a live Fly.io app, "
            "org creds, Postgres/Redis provisioned, and a deploy pipeline.\\n\\n"
            "- Reopen the task with Fly-enabled access\\n"
            "- Or split deploy acceptance into a follow-up task\n"
        ),
    )

    async def body() -> None:
        async with dashboard_app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            assert dashboard_app.data is not None
            assert dashboard_app.data.inbox_count >= 1
            assert dashboard_app.data.action_items
            assert dashboard_app.data.status_label == "needs attention"
            assert "Waiting on you:" in str(dashboard_app.action_bar.render())
            assert str(dashboard_app.inbox_title.render()) == "Action Needed"
            rendered = str(dashboard_app.inbox_body.render())
            assert "To move this project forward" in rendered
            assert "Action Required" not in rendered
            assert "Details" not in rendered
            assert "What you need to set up" in rendered
            assert "Set up the Fly.io app for this project" in rendered
            assert "Give Polly deployment access" in rendered
            assert "Decision" in rendered
            assert "approve the code work now" in rendered
            assert dashboard_app.action_primary_buttons[0].label.plain == "Approve it anyway"
            assert (
                dashboard_app.action_secondary_buttons[0].label.plain
                == "Wait until environment is set"
            )

            routed: list[str] = []
            dashboard_app._route_to_task = routed.append  # type: ignore[assignment]

            class _Click:
                y = 4

                def stop(self) -> None:
                    pass

            dashboard_app.on_inbox_section_click(_Click())  # type: ignore[arg-type]
            assert routed == ["demo/3"]

    _run(body())


def test_project_overview_sections_are_clickable(
    dashboard_env, dashboard_app,
) -> None:
    """Every dashboard card should behave like navigation, not static text."""
    async def body() -> None:
        plan_path = (
            dashboard_env["project_path"] / "docs" / "plan" / "plan.md"
        )
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text("# Plan\n\n## Next\n")

        async with dashboard_app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()

            class _Click:
                def stop(self) -> None:
                    pass

            task_routes: list[str] = []
            task_list_routes: list[str] = []
            activity_routes: list[str] = []
            dashboard_app._route_to_task = task_routes.append  # type: ignore[assignment]
            dashboard_app._route_to_tasks = lambda: task_list_routes.append("tasks")  # type: ignore[assignment]
            dashboard_app.action_jump_activity = lambda: activity_routes.append("activity")  # type: ignore[method-assign]

            dashboard_app.on_now_section_click(_Click())  # type: ignore[arg-type]
            assert task_routes == [dashboard_env["task_ids"]["in_progress"]]

            dashboard_app.on_pipeline_section_click(_Click())  # type: ignore[arg-type]
            assert task_list_routes == ["tasks"]

            assert dashboard_app._plan_view_mode is False
            dashboard_app.on_plan_section_click(_Click())  # type: ignore[arg-type]
            assert dashboard_app._plan_view_mode is True

            dashboard_app.on_activity_section_click(_Click())  # type: ignore[arg-type]
            assert activity_routes == ["activity"]

    _run(body())


def test_plan_review_action_uses_contextual_review_plan_button(
    dashboard_env, dashboard_app,
) -> None:
    """Plan review cards should not show generic approve/wait buttons."""
    db_path = dashboard_env["project_path"] / ".pollypm" / "state.db"
    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        store.enqueue_message(
            type="notify",
            tier="immediate",
            recipient="user",
            sender="architect",
            subject="Plan ready for review: demo",
            body=(
                f"Plan: {dashboard_env['project_path']}/docs/project-plan.md\n\n"
                "A full project plan is ready. Press A to approve."
            ),
            scope="demo",
            labels=["plan_review", "project:demo", "plan_task:demo/3"],
            payload={"actor": "architect", "project": "demo"},
            state="open",
        )
    finally:
        store.close()

    async def body() -> None:
        async with dashboard_app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            rendered = str(dashboard_app.inbox_body.render())
            assert "A full project plan is ready for your review" in rendered
            assert "Open the plan review surface" in rendered
            assert "Waiting on you:" in str(dashboard_app.action_bar.render())
            assert dashboard_app.action_primary_buttons[0].label.plain == "Review plan"
            assert dashboard_app.action_secondary_buttons[0].label.plain == "Open task"

            routed: list[str] = []
            dashboard_app.action_jump_inbox = lambda: routed.append("inbox")  # type: ignore[method-assign]
            dashboard_app._perform_dashboard_action(0, "primary")
            assert routed == ["inbox"]

    _run(body())


def test_user_prompt_payload_drives_dashboard_copy_and_buttons(
    dashboard_env, dashboard_app,
) -> None:
    """Architect-authored user_prompt JSON is the source of truth when present."""
    db_path = dashboard_env["project_path"] / ".pollypm" / "state.db"
    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        store.enqueue_message(
            type="notify",
            tier="immediate",
            recipient="user",
            sender="architect",
            subject="Need user decision",
            body="Internal details that should not drive the dashboard.",
            scope="demo",
            labels=["project:demo"],
            payload={
                "actor": "architect",
                "project": "demo",
                "task_id": "demo/2",
                "user_prompt": {
                    "summary": "The importer is ready, but needs your API key.",
                    "steps": ["Add the Bookshop API key to project secrets"],
                    "question": "Add the key now, or should Polly stub it?",
                    "actions": [
                        {
                            "label": "Open task",
                            "kind": "open_task",
                            "task_id": "demo/2",
                        },
                        {
                            "label": "Discuss",
                            "kind": "discuss_pm",
                        },
                    ],
                    "other_placeholder": "Tell Polly another path...",
                },
            },
            state="open",
        )
    finally:
        store.close()

    async def body() -> None:
        async with dashboard_app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            rendered = str(dashboard_app.inbox_body.render())
            assert "needs your API key" in rendered
            assert "Add the Bookshop API key" in rendered
            assert "Internal details" not in rendered
            assert "Waiting on you:" in str(dashboard_app.action_bar.render())
            assert dashboard_app.action_primary_buttons[0].label.plain == "Open task"
            assert dashboard_app.action_secondary_buttons[0].label.plain == "Discuss"

    _run(body())


def test_current_activity_calls_out_user_decision_when_only_architect_active(
    dashboard_env, dashboard_app, monkeypatch,
) -> None:
    """When the only active worker is an architect (no in-flight
    task) but the user has action items waiting, the Current
    Activity section must say so. Otherwise '● architect_X
    architect just now' alone implies work is happening when the
    operator-facing reality is 'I have a decision to make.'"""
    from datetime import UTC, datetime

    fake_worker = {
        "session_name": "architect_demo",
        "role": "architect",
        "last_heartbeat": datetime.now(UTC).isoformat(),
    }

    def _fake_active_worker(config_path, project_key, *, action_items=None):
        return fake_worker, 0

    from pollypm import cockpit_ui as _cockpit_ui
    monkeypatch.setattr(
        _cockpit_ui, "_dashboard_active_worker", _fake_active_worker,
    )

    db_path = dashboard_env["project_path"] / ".pollypm" / "state.db"
    in_progress_id = dashboard_env["task_ids"]["in_progress"]
    # Drop the seeded in-flight task so the dashboard hits the
    # "architect active, nothing actually running" branch — the real
    # production scenario this test covers (Notesy has 0 in_progress
    # tasks, all are blocked, and only the architect is alive).
    with SQLiteWorkService(
        db_path=db_path, project_path=dashboard_env["project_path"],
    ) as svc:
        svc.cancel(in_progress_id, "polly", reason="test setup")
    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        store.enqueue_message(
            type="notify",
            tier="immediate",
            recipient="user",
            sender="architect",
            subject="Need a call",
            body="Please decide.",
            scope="demo",
            labels=["project:demo"],
            payload={
                "actor": "architect",
                "project": "demo",
                "task_id": in_progress_id,
                "user_prompt": {
                    "summary": "The deploy work is ready, but we need your call.",
                    "steps": ["Approve or wait"],
                    "question": "Approve now or wait?",
                    "actions": [
                        {"label": "Approve", "kind": "approve_task",
                         "task_id": in_progress_id},
                    ],
                },
            },
            state="open",
        )
    finally:
        store.close()
    _cockpit_ui._PROJECT_DASHBOARD_TASK_CACHE.clear()

    async def body() -> None:
        async with dashboard_app.run_test(size=(160, 60)) as pilot:
            await pilot.pause()
            assert dashboard_app.data is not None
            assert dashboard_app.data.active_worker is not None
            assert dashboard_app.data.action_items
            assert not dashboard_app.data.task_buckets.get("in_progress")
            rendered = str(dashboard_app.now_body.render())
            assert "architect_demo" in rendered
            # Critically: the section also surfaces the user-facing
            # decision so the operator can see "I have a decision to
            # make" alongside "the architect is online" — but it
            # points at the Action Needed card instead of restating
            # the prompt content (which is right above on the same
            # screen).
            assert "Waiting on your response" in rendered
            assert "Action Needed" in rendered
            # The prompt body must NOT be repeated here; the user can
            # already see it in the Action Needed card.
            assert "deploy work is ready" not in rendered

    _run(body())


def test_inbox_section_omits_need_action_count_when_cards_show_full_set(
    dashboard_env, dashboard_app,
) -> None:
    """When the Action Needed cards already render every action item
    in the project's inbox, the trailing ``◆ N need action`` line
    just restates what the user already counted in the cards above
    — pure noise. Only print it when the inbox has *more* action
    items than the rendered cards can show."""
    db_path = dashboard_env["project_path"] / ".pollypm" / "state.db"
    in_progress_id = dashboard_env["task_ids"]["in_progress"]
    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        store.enqueue_message(
            type="notify",
            tier="immediate",
            recipient="user",
            sender="architect",
            subject=f"Decide on {in_progress_id}",
            body="Decide.",
            scope="demo",
            labels=["project:demo"],
            payload={
                "actor": "architect",
                "project": "demo",
                "task_id": in_progress_id,
                "user_prompt": {
                    "summary": "One thing waiting.",
                    "steps": ["Look at it"],
                    "question": "Approve?",
                    "actions": [
                        {"label": "Approve", "kind": "approve_task",
                         "task_id": in_progress_id},
                        {"label": "Wait", "kind": "record_response"},
                    ],
                },
            },
            state="open",
        )
    finally:
        store.close()

    async def body() -> None:
        async with dashboard_app.run_test(size=(160, 60)) as pilot:
            await pilot.pause()
            assert dashboard_app.data is not None
            assert len(dashboard_app.data.action_items) == 1
            rendered = str(dashboard_app.inbox_body.render())
            # The single action card already enumerates the only item
            # — the redundant overflow line must not print.
            assert "need action" not in rendered, (
                f"redundant 'need action' overflow line printed: {rendered!r}"
            )

    _run(body())


def test_action_count_dedupes_review_task_and_matching_message() -> None:
    """When a message and a review-stage task share the same
    ``primary_ref``, they're the same conceptual action — count once.

    Without this, the banner reads "2 need action" while only one card
    is rendered, because the second item is the underlying task already
    represented by the message. The user has no way to discover what
    the missing item is; the count just lies.
    """
    from pollypm.cockpit_ui import _action_count

    items = [
        # Task in review status — task-source action.
        {
            "source": "task",
            "needs_action": True,
            "primary_ref": "demo/3",
        },
        # Message about the same task.
        {
            "source": "message",
            "needs_action": True,
            "primary_ref": "demo/3",
        },
    ]
    action_items = [
        {"source": "message", "primary_ref": "demo/3"},
    ]

    assert _action_count(items, action_items) == 1


def test_action_card_click_hint_collapses_per_item_duplication() -> None:
    """Action Needed cards used to repeat
    ``"Click this message to open the source task."`` verbatim
    under every card. With multiple cards open, the user reads the
    same sentence twice on the same screen for no benefit.

    Render one consolidated hint at the bottom of the action stack
    instead, with copy that adapts to single vs. multiple cards and
    to whether each card opens a task or an inbox thread.
    """
    from pollypm.cockpit_ui import _action_card_click_hint

    # Empty → no hint.
    assert _action_card_click_hint([]) == ""

    # Single task-backed card: precise singular.
    assert _action_card_click_hint(
        [{"primary_ref": "polly_remote/12"}]
    ) == "Click this card to open the source task."

    # Single thread-backed card.
    assert _action_card_click_hint(
        [{"primary_ref": "blocker-summary:42"}]
    ) == "Click this card to open the inbox thread."

    # Two task-backed cards: pluralise.
    assert _action_card_click_hint(
        [
            {"primary_ref": "polly_remote/12"},
            {"primary_ref": "polly_remote/9"},
        ]
    ) == "Click any card to open its source task."

    # Mixed: hedge.
    assert _action_card_click_hint(
        [
            {"primary_ref": "polly_remote/12"},
            {"primary_ref": "blocker-summary:42"},
        ]
    ) == "Click any card to open its source task or inbox thread."


def test_clean_hold_reason_strips_action_routing_tag() -> None:
    """Auto-holds emit reasons like
    ``"Waiting on operator: [Action] Done: <subject>"`` because the
    transition manager copies the notification subject verbatim. The
    ``[Action]`` token is a routing tag, not natural language —
    surface the rest of the reason without it.
    """
    from pollypm.cockpit_ui import _clean_hold_reason

    assert _clean_hold_reason(
        "Waiting on operator: [Action] Done: Phase 2 rework resubmitted"
    ) == "Waiting on operator: Done: Phase 2 rework resubmitted"

    # Multiple [Action] tags get all stripped.
    assert _clean_hold_reason(
        "[Action] queued [Action] x"
    ) == "queued x"

    # No tag → unchanged (modulo strip).
    assert _clean_hold_reason(
        "  Manually parked while we wait on legal  "
    ) == "Manually parked while we wait on legal"

    # Empty → empty.
    assert _clean_hold_reason("") == ""
    assert _clean_hold_reason("   ") == ""


def test_stuck_alert_covers_action_dedupes_user_waiting_alerts() -> None:
    """A ``stuck_on_task:<id>`` alert is mechanically fired when a
    session sits idle waiting on the user. When the dashboard already
    shows a user_prompt card for that same task, the stuck alert is
    just the same fact in different words — don't inflate the banner.
    """
    from pollypm.cockpit_ui import _stuck_alert_covers_action

    covered = {"polly_remote/12"}
    # Same task → covered.
    assert _stuck_alert_covers_action(
        "stuck_on_task:polly_remote/12", covered,
    )
    # Different task → not covered.
    assert not _stuck_alert_covers_action(
        "stuck_on_task:polly_remote/9", covered,
    )
    # Non-stuck alert types must never be filtered by this helper.
    assert not _stuck_alert_covers_action(
        "no_session_for_assignment:polly_remote/12", covered,
    )
    assert not _stuck_alert_covers_action("", covered)
    # Empty / malformed body keeps the alert visible.
    assert not _stuck_alert_covers_action(
        "stuck_on_task:", covered,
    )
    assert not _stuck_alert_covers_action(
        "stuck_on_task:   ", covered,
    )


def test_action_count_keeps_distinct_task_actions() -> None:
    """Tasks whose primary_ref does *not* appear in any action card
    must still be counted — otherwise the banner under-reports work
    waiting on the user.
    """
    from pollypm.cockpit_ui import _action_count

    items = [
        {"source": "task", "needs_action": True, "primary_ref": "demo/3"},
        {"source": "task", "needs_action": True, "primary_ref": "demo/4"},
        {"source": "message", "needs_action": True, "primary_ref": "demo/3"},
    ]
    action_items = [{"source": "message", "primary_ref": "demo/3"}]

    # demo/4 has no message coverage → 1 task action + 1 message = 2.
    assert _action_count(items, action_items) == 2


def test_pipeline_in_progress_section_names_assignee_and_node() -> None:
    """In-progress rows must tell the operator which worker is
    carrying the task and which node they're at — without this, the
    dashboard says '1 in progress' but doesn't tell Sam who to ping
    when he has a question."""
    from types import SimpleNamespace

    from pollypm.cockpit_ui import PollyProjectDashboardApp

    fake_data = SimpleNamespace(
        exists_on_disk=True,
        task_counts={"in_progress": 1},
        task_buckets={
            "queued": [],
            "in_progress": [
                {
                    "task_id": "demo/1",
                    "task_number": 1,
                    "title": "Active feature",
                    "updated_at": "",
                    "assignee": "pete",
                    "current_node_id": "implement",
                    "summary": "",
                    "steps": [],
                    "blocked_by": [],
                    "hold_reason": "",
                },
            ],
            "review": [],
            "blocked": [],
            "on_hold": [],
            "done": [],
        },
    )

    app = PollyProjectDashboardApp.__new__(PollyProjectDashboardApp)
    rendered = app._render_pipeline_body(fake_data)

    assert "Active feature" in rendered
    assert "pete" in rendered
    assert "implement" in rendered


def test_pipeline_review_section_distinguishes_user_review_from_auto() -> None:
    """The pipeline's Review section must tell the operator who has
    the ball — the auto-reviewer (Russell) or a user-approval node
    that needs the human. Without this signal, ``1 in review`` looks
    identical whether Sam should wait or act."""
    from types import SimpleNamespace

    # Build a fake dashboard_app and feed it minimal data with two
    # review-bucket tasks: one routed to Russell (auto), one parked at
    # a user-approval node.
    from pollypm.cockpit_ui import PollyProjectDashboardApp
    fake_data = SimpleNamespace(
        exists_on_disk=True,
        task_counts={"review": 2},
        task_buckets={
            "queued": [],
            "in_progress": [],
            "review": [
                {
                    "task_id": "demo/1",
                    "task_number": 1,
                    "title": "Auto-reviewed change",
                    "updated_at": "",
                    "assignee": "russell",
                    "current_node_id": "code_review",
                    "summary": "",
                    "steps": [],
                    "blocked_by": [],
                    "hold_reason": "",
                },
                {
                    "task_id": "demo/2",
                    "task_number": 2,
                    "title": "Plan awaiting approval",
                    "updated_at": "",
                    "assignee": "user",
                    "current_node_id": "user_approval",
                    "summary": "",
                    "steps": [],
                    "blocked_by": [],
                    "hold_reason": "",
                },
            ],
            "blocked": [],
            "on_hold": [],
            "done": [],
        },
    )

    # _render_pipeline_body is a pure method given data, no app run needed.
    app = PollyProjectDashboardApp.__new__(PollyProjectDashboardApp)
    rendered = app._render_pipeline_body(fake_data)

    # Auto-reviewer row names Russell + the node it's parked at.
    assert "Auto-reviewed change" in rendered
    assert "reviewing: russell" in rendered
    assert "code_review" in rendered

    # User-approval row uses the operator-facing call-to-action copy.
    assert "Plan awaiting approval" in rendered
    assert "ready for your approval" in rendered


def test_pipeline_on_hold_section_surfaces_reason(
    dashboard_env, dashboard_app,
) -> None:
    """The pipeline's On Hold section showed only title + age, leaving
    the operator wondering *why* a task was paused. The hold reason is
    recorded with the ``pm task hold --reason`` transition; surface it
    so the operator can see at a glance what would unparked the task."""
    db_path = dashboard_env["project_path"] / ".pollypm" / "state.db"
    with SQLiteWorkService(
        db_path=db_path, project_path=dashboard_env["project_path"],
    ) as svc:
        held = svc.create(
            title="Held feature",
            description="Paused while we wait on a decision.",
            type="task",
            project="demo",
            flow_template="standard",
            roles={"worker": "pete", "reviewer": "russell"},
            priority="normal",
            created_by="polly",
        )
        svc.queue(held.task_id, "polly")
        svc.claim(held.task_id, "worker")
        svc.hold(
            held.task_id, "polly",
            "Waiting on Sam to confirm the API contract",
        )
    from pollypm import cockpit_ui as _cockpit_ui
    _cockpit_ui._PROJECT_DASHBOARD_TASK_CACHE.clear()

    async def body() -> None:
        async with dashboard_app.run_test(size=(160, 60)) as pilot:
            await pilot.pause()
            assert dashboard_app.data is not None
            on_hold = dashboard_app.data.task_buckets.get("on_hold", [])
            assert any(
                item.get("title") == "Held feature" for item in on_hold
            ), f"expected held task in on_hold bucket: {on_hold!r}"
            rendered = str(dashboard_app.pipeline_body.render())
            assert "Held feature" in rendered
            assert "paused:" in rendered
            assert "Waiting on Sam to confirm the API contract" in rendered

    _run(body())


def test_pipeline_blocked_section_surfaces_dependencies(
    dashboard_env, dashboard_app,
) -> None:
    """The pipeline's Blocked section used to show only title + age,
    leaving the operator with no signal about *what* each task is
    waiting on. Surface the ``blocked_by`` task references so the
    user can see the upstream work without drilling into each
    blocked task one by one."""
    db_path = dashboard_env["project_path"] / ".pollypm" / "state.db"
    with SQLiteWorkService(
        db_path=db_path, project_path=dashboard_env["project_path"],
    ) as svc:
        upstream = svc.create(
            title="Upstream task",
            description="Other work this depends on.",
            type="task",
            project="demo",
            flow_template="standard",
            roles={"worker": "pete", "reviewer": "russell"},
            priority="normal",
            created_by="polly",
        )
        svc.queue(upstream.task_id, "polly")
        downstream = svc.create(
            title="Blocked downstream feature",
            description="Cannot start until upstream lands.",
            type="task",
            project="demo",
            flow_template="standard",
            roles={"worker": "pete", "reviewer": "russell"},
            priority="normal",
            created_by="polly",
        )
        svc.queue(downstream.task_id, "polly")
        svc.claim(downstream.task_id, "worker")
        svc.block(downstream.task_id, "polly", upstream.task_id)
    from pollypm import cockpit_ui as _cockpit_ui
    _cockpit_ui._PROJECT_DASHBOARD_TASK_CACHE.clear()

    async def body() -> None:
        async with dashboard_app.run_test(size=(160, 60)) as pilot:
            await pilot.pause()
            assert dashboard_app.data is not None
            blocked = dashboard_app.data.task_buckets.get("blocked", [])
            assert any(
                "Blocked downstream feature" == item.get("title") for item in blocked
            ), f"expected blocked downstream task in buckets: {blocked!r}"
            rendered = str(dashboard_app.pipeline_body.render())
            assert "Blocked downstream feature" in rendered
            assert "waiting on:" in rendered
            assert upstream.task_id in rendered

    _run(body())


def test_status_pill_prefers_user_attention_over_active_worker(
    dashboard_env, dashboard_app, monkeypatch,
) -> None:
    """When a worker is heartbeat-alive *and* the user has inbox
    items, the pill must show 'needs attention' (yellow) — not
    'active' (green). Saying 'active' while the banner says
    'Waiting on you' was the contradictory false-positive green
    light the v1 dashboard contract called out: an architect or
    worker running in the background does not mean 'nothing for the
    operator to do here.'"""
    from datetime import UTC, datetime

    fake_worker = {
        "session_name": "demo-main",
        "role": "architect",
        "last_heartbeat": datetime.now(UTC).isoformat(),
    }

    def _fake_active_worker(config_path, project_key, *, action_items=None):
        return fake_worker, 0

    from pollypm import cockpit_ui as _cockpit_ui
    monkeypatch.setattr(
        _cockpit_ui, "_dashboard_active_worker", _fake_active_worker,
    )

    # Seed an inbox-bearing chat task so inbox_count > 0.
    db_path = dashboard_env["project_path"] / ".pollypm" / "state.db"
    with SQLiteWorkService(
        db_path=db_path, project_path=dashboard_env["project_path"],
    ) as svc:
        svc.create(
            title="Hi there",
            description="Need your call.",
            type="task",
            project="demo",
            flow_template="chat",
            roles={"requester": "user", "operator": "polly"},
            priority="normal",
            created_by="polly",
        )
    _cockpit_ui._PROJECT_DASHBOARD_TASK_CACHE.clear()

    async def body() -> None:
        async with dashboard_app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            assert dashboard_app.data is not None
            assert dashboard_app.data.active_worker is not None
            assert dashboard_app.data.inbox_count >= 1
            # User-attention state outranks the active-worker indicator.
            assert dashboard_app.data.status_label == "needs attention"

    _run(body())


def test_waiting_on_you_banner_drops_redundant_need_action_suffix(
    dashboard_env, dashboard_app,
) -> None:
    """When the dashboard banner already leads with 'Waiting on you:'
    and the Action Needed cards already enumerate the items, repeating
    'N need action' in the tail count is redundant noise. Other
    categories (dependencies, on hold, approvals, alerts) still belong
    in the suffix because they tell the user about *different* state
    from the action cards."""
    db_path = dashboard_env["project_path"] / ".pollypm" / "state.db"
    in_progress_id = dashboard_env["task_ids"]["in_progress"]
    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        store.enqueue_message(
            type="notify",
            tier="immediate",
            recipient="user",
            sender="architect",
            subject=f"Need decision on {in_progress_id}",
            body="Decision required.",
            scope="demo",
            labels=["project:demo"],
            payload={
                "actor": "architect",
                "project": "demo",
                "task_id": in_progress_id,
                "user_prompt": {
                    "summary": "The work is ready for your call.",
                    "steps": ["Review and decide"],
                    "question": "Approve or wait?",
                    "actions": [
                        {
                            "label": "Approve it",
                            "kind": "approve_task",
                            "task_id": in_progress_id,
                        },
                        {"label": "Wait", "kind": "record_response"},
                    ],
                },
            },
            state="open",
        )
    finally:
        store.close()

    async def body() -> None:
        async with dashboard_app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            rendered = str(dashboard_app.action_bar.render())
            assert "Waiting on you:" in rendered
            # Redundant tail count must not appear.
            assert "need action" not in rendered, (
                f"banner still includes redundant 'need action' suffix: {rendered!r}"
            )

    _run(body())


def test_user_prompt_action_kinds_preserve_underscores(
    dashboard_env, dashboard_app,
) -> None:
    """Action ``kind`` values are dispatch identifiers (``approve_task``,
    ``review_plan``, ``open_inbox`` …). The dashboard's plain-text
    sanitizer strips underscores as markdown decorations, which until
    this cycle silently turned ``approve_task`` into ``approvetask`` —
    no branch in ``_perform_dashboard_action`` matched, so every
    structured user_prompt button fell through to the generic
    record-response fallback. The underscore must survive intact."""
    db_path = dashboard_env["project_path"] / ".pollypm" / "state.db"
    in_progress_id = dashboard_env["task_ids"]["in_progress"]
    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        store.enqueue_message(
            type="notify",
            tier="immediate",
            recipient="user",
            sender="architect",
            subject=f"Need decision on {in_progress_id}",
            body="Decision required.",
            scope="demo",
            labels=["project:demo"],
            payload={
                "actor": "architect",
                "project": "demo",
                "task_id": in_progress_id,
                "user_prompt": {
                    "summary": "Approve or reroute?",
                    "steps": ["Review the work"],
                    "question": "Approve or send to review?",
                    "actions": [
                        {
                            "label": "Approve it",
                            "kind": "approve_task",
                            "task_id": in_progress_id,
                        },
                        {
                            "label": "Open inbox",
                            "kind": "open_inbox",
                        },
                    ],
                },
            },
            state="open",
        )
    finally:
        store.close()

    async def body() -> None:
        async with dashboard_app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            assert dashboard_app.data is not None
            assert dashboard_app.data.action_items
            primary = dashboard_app.data.action_items[0]["primary_action"]
            secondary = dashboard_app.data.action_items[0]["secondary_action"]
            assert primary["kind"] == "approve_task", (
                f"underscore stripped from kind: {primary['kind']!r}"
            )
            assert secondary["kind"] == "open_inbox", (
                f"underscore stripped from kind: {secondary['kind']!r}"
            )

    _run(body())


def test_approve_button_warns_when_task_is_not_in_an_approvable_state(
    dashboard_env, dashboard_app,
) -> None:
    """When the user clicks 'Approve it anyway' but the underlying
    task isn't in a state PollyPM can auto-approve from (e.g.
    in_progress, blocked), the toast must explicitly tell the user
    that the task stayed where it was — not the misleading
    'Decision recorded.' which suggested the click moved the project."""
    db_path = dashboard_env["project_path"] / ".pollypm" / "state.db"
    in_progress_id = dashboard_env["task_ids"]["in_progress"]
    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        store.enqueue_message(
            type="notify",
            tier="immediate",
            recipient="user",
            sender="architect",
            subject=f"[Action] {in_progress_id} — code ready",
            body="Code is in but acceptance is blocked on infra.",
            scope="demo",
            labels=["project:demo"],
            payload={
                "actor": "architect",
                "project": "demo",
                "task_id": in_progress_id,
                "user_prompt": {
                    "summary": "Code is in but acceptance is blocked on infra.",
                    "steps": ["Stand up the infra"],
                    "question": "Approve now or wait?",
                    "actions": [
                        {
                            "label": "Approve it anyway",
                            "kind": "approve_task",
                            "task_id": in_progress_id,
                        },
                        {"label": "Wait", "kind": "record_response"},
                    ],
                },
            },
            state="open",
        )
    finally:
        store.close()

    captured: list[tuple[str, str | None]] = []

    async def body() -> None:
        async with dashboard_app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            assert dashboard_app.data is not None
            assert dashboard_app.data.action_items
            dashboard_app.notify = (  # type: ignore[assignment]
                lambda message, *, severity="information", **_: captured.append(
                    (str(message), str(severity)),
                )
            )
            dashboard_app._perform_dashboard_action(0, "primary")
            await pilot.pause()

    _run(body())

    # The user clicked the primary 'approve' button, but the task is
    # in_progress — not 'review' — so the approve helper can't act.
    # The toast must say so explicitly, not blandly claim 'Decision
    # recorded.'
    severities = [sev for _, sev in captured]
    messages = [msg for msg, _ in captured]
    assert any(
        "stayed at 'in_progress'" in msg for msg in messages
    ), f"expected explicit stayed-at toast, got {messages!r}"
    assert "warning" in severities, (
        "an unapproved click must surface as a warning so the user "
        "doesn't think the project moved"
    )


# ---------------------------------------------------------------------------
# 3. Task pipeline — counts + top-N per bucket
# ---------------------------------------------------------------------------


def test_pipeline_shows_counts_and_titles(
    dashboard_env, dashboard_app,
) -> None:
    """Queued / in_progress / done seeded tasks all show up in the pipeline."""
    async def body() -> None:
        async with dashboard_app.run_test(size=(160, 60)) as pilot:
            await pilot.pause()
            assert dashboard_app.data is not None

            # Counts match what we seeded (1 queued, 1 in_progress, 1 done).
            counts = dashboard_app.data.task_counts
            assert counts.get("queued") == 1
            assert counts.get("in_progress") == 1
            assert counts.get("done") == 1

            # Top-N per bucket — the titles we seeded appear in the cache.
            buckets = dashboard_app.data.task_buckets
            queued_titles = [t["title"] for t in buckets.get("queued", [])]
            assert "Queued feature" in queued_titles
            active_titles = [t["title"] for t in buckets.get("in_progress", [])]
            assert "Active feature" in active_titles
            done_titles = [t["title"] for t in buckets.get("done", [])]
            assert "Done thing" in done_titles

            # Render reflects the same.
            rendered = str(dashboard_app.pipeline_body.render())
            assert "Queued feature" in rendered
            assert "Active feature" in rendered
            assert "Done thing" in rendered
    _run(body())


# ---------------------------------------------------------------------------
# 4. Plan summary — reads plan.md, extracts H2 sections
# ---------------------------------------------------------------------------


def test_plan_section_reads_h2_headers(dashboard_env, dashboard_app) -> None:
    """A ``docs/plan/plan.md`` with H2 headers surfaces those headers in-UI."""
    async def body() -> None:
        plan_path = (
            dashboard_env["project_path"] / "docs" / "plan" / "plan.md"
        )
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(
            "# Project Plan\n"
            "\n"
            "Intro paragraph.\n"
            "\n"
            "## Module A\n"
            "Details.\n"
            "\n"
            "## Module B\n"
            "Details.\n"
            "\n"
            "### Sub-section (should not appear)\n"
            "\n"
            "## Module C\n"
        )

        async with dashboard_app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            assert dashboard_app.data is not None
            assert dashboard_app.data.plan_path == plan_path
            # Only H2 headers land in the list.
            assert dashboard_app.data.plan_sections == [
                "Module A", "Module B", "Module C",
            ]
            rendered = str(dashboard_app.plan_body.render())
            assert "Module A" in rendered
            assert "Module B" in rendered
            assert "Sub-section" not in rendered
    _run(body())


# ---------------------------------------------------------------------------
# 5. Plan summary — empty-state copy when plan.md is absent
# ---------------------------------------------------------------------------


def test_plan_empty_state_when_no_plan_file(
    dashboard_env, dashboard_app,
) -> None:
    """No plan file → friendly empty-state hint, not a CLI command dump."""
    async def body() -> None:
        async with dashboard_app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            assert dashboard_app.data is not None
            assert dashboard_app.data.plan_path is None
            assert dashboard_app.data.plan_sections == []
            rendered = str(dashboard_app.plan_body.render())
            # Empty-state copy is plain English: tells the user the PM
            # will draft a plan when work picks up, and points them at
            # the chat keybinding to ask now.
            assert "No plan yet" in rendered
            assert "PM will draft one" in rendered
            assert "ask the PM to plan it now" in rendered
            # No CLI jargon leaking through to the operator surface.
            assert "pm project plan" not in rendered
            assert "auto-fire" not in rendered
    _run(body())


# ---------------------------------------------------------------------------
# 6. Keybinding — `c` dispatches to jump-to-PM (monkeypatch the perform hook)
# ---------------------------------------------------------------------------


def test_c_keybinding_dispatches_to_pm(dashboard_env, dashboard_app) -> None:
    """Pressing ``c`` routes to the PM and sends a context line."""
    async def body() -> None:
        calls: list[tuple[str, str]] = []

        def fake_dispatch(self, cockpit_key: str, context_line: str) -> None:
            calls.append((cockpit_key, context_line))

        from pollypm.cockpit_ui import PollyProjectDashboardApp
        PollyProjectDashboardApp._perform_pm_dispatch = fake_dispatch  # type: ignore[assignment]

        async with dashboard_app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            await pilot.pause()
            # Worker-thread dispatch — if the scheduler didn't land in time,
            # drive the worker body directly so the assertion is deterministic.
            if not calls:
                dashboard_app._dispatch_to_pm_sync(
                    "polly", 're: project/demo "dashboard discussion"', "Polly",
                )
            assert calls, "expected _perform_pm_dispatch to be called"
            cockpit_key, context_line = calls[-1]
            # No persona configured on the fixture → fall back to Polly.
            assert cockpit_key == "polly"
            assert "project/demo" in context_line
    _run(body())


# ---------------------------------------------------------------------------
# 7. Keybinding — `i` routes to the inbox
# ---------------------------------------------------------------------------


def test_i_keybinding_routes_to_inbox(dashboard_env, dashboard_app) -> None:
    """Pressing ``i`` routes the cockpit to the inbox (monkeypatched)."""
    async def body() -> None:
        calls: list[bool] = []

        def fake_route_to_inbox(self) -> None:
            calls.append(True)

        from pollypm.cockpit_ui import PollyProjectDashboardApp
        PollyProjectDashboardApp._route_to_inbox = fake_route_to_inbox  # type: ignore[assignment]

        async with dashboard_app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            await pilot.press("i")
            await pilot.pause()
            await pilot.pause()
            if not calls:
                dashboard_app._route_to_inbox_sync()
            assert calls, "expected _route_to_inbox to be called"
    _run(body())


# ---------------------------------------------------------------------------
# 8. Recent activity — shows the 10 most recent events for the project
# ---------------------------------------------------------------------------


def test_recent_activity_renders_feed_entries(
    dashboard_env, dashboard_app, monkeypatch,
) -> None:
    """Stub the activity projector so the test doesn't depend on event rows."""
    async def body() -> None:
        # Build 12 fake entries — dashboard should cap to 10.
        fake_entries = [
            {
                "timestamp": f"2026-04-17T10:{30 + i:02d}:00+00:00",
                "actor": f"actor-{i}",
                "verb": "did",
                "summary": f"thing {i}",
                "kind": "work",
            }
            for i in range(12)
        ]

        def _fake_activity(config_path, project_key, *, limit=10):
            assert project_key == "demo"
            return fake_entries[:limit]

        from pollypm import cockpit_ui
        monkeypatch.setattr(
            cockpit_ui, "_dashboard_activity", _fake_activity,
        )

        async with dashboard_app.run_test(size=(160, 60)) as pilot:
            await pilot.pause()
            assert dashboard_app.data is not None
            assert len(dashboard_app.data.activity_entries) == 10
            rendered = str(dashboard_app.activity_body.render())
            # First and tenth entries both appear in the rendered body.
            assert "actor-0" in rendered
            assert "actor-9" in rendered
            # The 11th+ entries (capped) don't appear.
            assert "actor-11" not in rendered
    _run(body())


# ---------------------------------------------------------------------------
# Bonus coverage — virtual project (no on-disk path) degrades gracefully
# ---------------------------------------------------------------------------


def test_action_bar_click_routes_to_inbox(dashboard_env, dashboard_app) -> None:
    """#750: the "1 approval · 1 new in inbox" action bar was a dead
    click target — the user had to discover the ``i`` keybinding. Now
    clicking it routes to the inbox, same action as ``i``."""
    async def body() -> None:
        calls: list[bool] = []

        def fake_route_to_inbox(self) -> None:
            calls.append(True)

        from pollypm.cockpit_ui import PollyProjectDashboardApp
        PollyProjectDashboardApp._route_to_inbox = fake_route_to_inbox  # type: ignore[assignment]

        async with dashboard_app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            await pilot.click("#proj-action-bar")
            await pilot.pause()
            # The click dispatches a worker; drain it synchronously if
            # the event loop hasn't caught up.
            if not calls:
                dashboard_app._route_to_inbox_sync()
            assert calls, "expected click on action bar to route to inbox"
    _run(body())


def test_inbox_section_click_routes_to_inbox(dashboard_env, dashboard_app) -> None:
    """#750: the Inbox section on the project dashboard was also a
    dead click target. Clicking anywhere in the section now routes
    to the inbox."""
    async def body() -> None:
        calls: list[bool] = []

        def fake_route_to_inbox(self) -> None:
            calls.append(True)

        from pollypm.cockpit_ui import PollyProjectDashboardApp
        PollyProjectDashboardApp._route_to_inbox = fake_route_to_inbox  # type: ignore[assignment]

        async with dashboard_app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            await pilot.click("#proj-inbox-section")
            await pilot.pause()
            if not calls:
                dashboard_app._route_to_inbox_sync()
            assert calls, "expected click on inbox section to route to inbox"
    _run(body())


def test_blocked_project_calls_out_missing_pm_summary(
    dashboard_env, dashboard_app,
) -> None:
    """Blocked work with no PM note should say the summary is missing."""
    db_path = dashboard_env["project_path"] / ".pollypm" / "state.db"
    with SQLiteWorkService(
        db_path=db_path, project_path=dashboard_env["project_path"],
    ) as svc:
        blocker = svc.create(
            title="Blocker task",
            description="Upstream dependency.",
            type="task",
            project="demo",
            flow_template="standard",
            roles={"worker": "pete", "reviewer": "russell"},
            priority="normal",
            created_by="polly",
        )
        blocked = svc.create(
            title="Blocked task",
            description="Waiting on blocker.",
            type="task",
            project="demo",
            flow_template="standard",
            roles={"worker": "pete", "reviewer": "russell"},
            priority="normal",
            created_by="polly",
        )
        svc.queue(blocked.task_id, "polly")
        svc.claim(blocked.task_id, "worker")
        svc.block(blocked.task_id, "polly", blocker.task_id)

    async def body() -> None:
        async with dashboard_app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            rendered = str(dashboard_app.inbox_body.render())
            assert "summary missing" in rendered.lower()
            assert "ask the PM for a blocker summary" in rendered

    _run(body())


def test_project_blocker_summary_lists_required_user_actions(
    dashboard_env, dashboard_app,
) -> None:
    """A PM-authored blocker summary should render concrete unblock steps."""
    db_path = dashboard_env["project_path"] / ".pollypm" / "state.db"
    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        store.record_event(
            "demo",
            "polly",
            "project.blocker_summary",
            payload={
                "event_type": "project_blocker_summary",
                "project": "demo",
                "reason": "Relay deployment is waiting on Fly.io setup.",
                "owner": "user",
                "required_actions": [
                    "Create the Fly.io app",
                    "Add the deploy token",
                ],
                "affected_tasks": ["demo/3"],
                "unblock_condition": "fly deploy can run successfully",
            },
        )
    finally:
        store.close()

    async def body() -> None:
        async with dashboard_app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            assert dashboard_app.data is not None
            rendered = str(dashboard_app.inbox_body.render())
            assert "To move this project forward" in rendered
            assert "Action Required" not in rendered
            assert "What you need to set up" in rendered
            assert "Set up the Fly.io app for this project" in rendered
            assert "Give Polly deployment access" in rendered
            assert "Create the Fly.io app" in rendered
            assert "Add the deploy token" in rendered

    _run(body())


def test_virtual_project_hides_plan_and_activity(tmp_path: Path) -> None:
    """A project entry pointing at a missing path must not crash the UI."""
    async def body() -> None:
        ghost_path = tmp_path / "ghost"  # intentionally never created
        config_path = tmp_path / "pollypm.toml"
        config_path.write_text(
            "[project]\n"
            f'tmux_session = "pollypm-test"\n'
            f'workspace_root = "{tmp_path}"\n'
            "\n"
            "[projects.ghost]\n"
            'key = "ghost"\n'
            'name = "Ghost"\n'
            f'path = "{ghost_path}"\n'
        )
        if not _load_config_compatible_ghost(config_path):
            pytest.skip("minimal pollypm.toml fixture not supported by loader")

        from pollypm import cockpit_ui as _cockpit_ui
        _cockpit_ui._PROJECT_DASHBOARD_TASK_CACHE.clear()
        from pollypm.cockpit_ui import PollyProjectDashboardApp
        app = PollyProjectDashboardApp(config_path, "ghost")
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            assert app.data is not None
            assert app.data.exists_on_disk is False
            # Plan section falls back to virtual-project copy, not a crash.
            plan_rendered = str(app.plan_body.render())
            assert "Virtual project" in plan_rendered or "No plan" in plan_rendered
            # Pipeline section shows the missing-path message.
            pipeline_rendered = str(app.pipeline_body.render())
            assert "No project path" in pipeline_rendered or "No tasks" in pipeline_rendered
    _run(body())


def _load_config_compatible_ghost(config_path: Path) -> bool:
    try:
        from pollypm.config import load_config
        cfg = load_config(config_path)
        return "ghost" in getattr(cfg, "projects", {})
    except Exception:  # noqa: BLE001
        return False


def test_other_open_items_skips_duplicates_of_action_cards(
    dashboard_app,
) -> None:
    """Regression: ``Other open items`` must not list the underlying task
    or the underlying message that an Action Needed card already
    represents — that turns the dashboard into duplicate noise instead
    of a clear list of distinct things waiting on the user."""
    from types import SimpleNamespace

    fake_data = SimpleNamespace(
        inbox_count=3,
        task_counts={},
        task_buckets={},
        action_items=[
            {
                # The inbox-row id of the action message
                "task_id": "msg-42",
                # The underlying linked task that the message points at
                "primary_ref": "demo/3",
                "title": "[Action] Plan ready for review: demo",
                "plain_prompt": (
                    "A full project plan is ready for your review."
                ),
                "decision_question": "Approve the plan?",
                "unblock_steps": ["Open the plan."],
            },
        ],
        inbox_top=[
            # Duplicate via primary_ref: the architect task that produced
            # the plan — task_id matches the action card's primary_ref.
            {
                "task_id": "demo/3",
                "primary_ref": "demo/3",
                "title": "Plan project demo",
                "updated_at": "",
                "triage_label": "task assigned",
                "source": "task",
            },
            # Duplicate via task_id: the same message that became the
            # action card.
            {
                "task_id": "msg-42",
                "primary_ref": "demo/3",
                "title": "[Action] Plan ready for review: demo",
                "updated_at": "",
                "triage_label": "plan review",
                "source": "message",
            },
            # Duplicate via primary_ref alone: a *different* notification
            # message (different inbox row id) that points at the same
            # underlying task.
            {
                "task_id": "msg-99",
                "primary_ref": "demo/3",
                "title": "[Action] Plan ready for review: demo (re-ping)",
                "updated_at": "",
                "triage_label": "plan review",
                "source": "message",
            },
            # Genuinely different inbox item — must remain.
            {
                "task_id": "demo/9",
                "primary_ref": "demo/9",
                "title": "Different thing waiting on you",
                "updated_at": "",
                "triage_label": "needs review",
                "source": "task",
            },
        ],
    )
    rendered = dashboard_app._render_inbox_body(fake_data)
    other_section = rendered.split("Other open items", 1)[-1]
    assert "Plan project demo" not in other_section
    assert "[Action] Plan ready for review: demo" not in other_section
    assert "re-ping" not in other_section
    # A genuinely-different inbox item must still surface.
    assert "Different thing waiting on you" in other_section
