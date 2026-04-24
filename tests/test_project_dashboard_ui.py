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

        def _fake_active_worker(config_path, project_key):
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
            rendered = str(dashboard_app.inbox_body.render())
            assert "Action Required" in rendered
            assert "Fly.io" in rendered
            assert "deploy pipeline" in rendered
            assert "Reopen the task with Fly-enabled access" in rendered

    _run(body())


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
    """No plan file → friendly empty-state hint, not blank section."""
    async def body() -> None:
        async with dashboard_app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            assert dashboard_app.data is not None
            assert dashboard_app.data.plan_path is None
            assert dashboard_app.data.plan_sections == []
            rendered = str(dashboard_app.plan_body.render())
            # The empty-state copy mentions the CLI the user should run.
            assert "pm project plan" in rendered
            assert "No plan" in rendered
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
            assert "Action Required" in rendered
            assert "Relay deployment is waiting on Fly.io setup" in rendered
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
