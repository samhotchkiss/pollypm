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
        svc._auto_merge_approved_task_branch = lambda _task, **_kwargs: None
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


# ---------------------------------------------------------------------------
# #920 regression — config-key vs work-DB project-name mismatch
# ---------------------------------------------------------------------------


def _write_split_key_config(
    project_path: Path, config_path: Path,
) -> None:
    """Write a config whose TOML key (``blackjack_trainer``) differs from
    the project ``name`` (``blackjack-trainer``) — the same shape the
    real CLI emits when ``pm project register`` ingests a hyphenated
    repo dir.
    """
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


def test_dashboard_finds_tasks_when_config_key_and_project_name_differ(
    tmp_path: Path,
) -> None:
    """#920 — dashboard must report ``in_progress`` tasks even when the
    config key is the slugified form (``blackjack_trainer``) and the
    work DB stores tasks under the display name (``blackjack-trainer``).

    Pre-fix: ``state_counts(project=...)`` did an exact match, so the
    dashboard passed the underscore key and got zero rows back. The
    counts and pipeline both lied: "○ idle" / "No tasks yet."
    """
    project_path = tmp_path / "blackjack-trainer"
    project_path.mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_split_key_config(project_path, config_path)

    workspace_db = tmp_path / ".pollypm" / "state.db"
    workspace_db.parent.mkdir(parents=True, exist_ok=True)
    with SQLiteWorkService(db_path=workspace_db, project_path=tmp_path) as svc:
        task = svc.create(
            title="Build Hi-Lo card counting trainer",
            description="seed",
            type="task",
            project="blackjack-trainer",  # hyphen — display-name form
            flow_template="standard",
            roles={"worker": "worker", "reviewer": "reviewer"},
            priority="normal",
            created_by="polly",
        )
        svc.queue(task.task_id, "polly")
        svc.claim(task.task_id, "worker")

    from pollypm import cockpit_ui as _cockpit_ui

    _cockpit_ui._PROJECT_DASHBOARD_TASK_CACHE.clear()
    # Dashboard receives the slugified config key, NOT the display name.
    data = _cockpit_ui._gather_project_dashboard(
        config_path, "blackjack_trainer",
    )

    assert data is not None
    # Counts must surface the in_progress row. Pre-fix this was 0.
    assert data.task_counts.get("in_progress") == 1
    assert [row["title"] for row in data.task_buckets.get("in_progress", [])] == [
        "Build Hi-Lo card counting trainer"
    ]
    # Status pill must reflect the in-flight task (yellow, "in progress")
    # — not "○ idle" as in the bug report.
    assert data.status_label == "in progress"


def test_dashboard_pipeline_body_does_not_say_no_tasks_when_in_progress(
    tmp_path: Path,
) -> None:
    """#920 — pipeline body must not render "No tasks yet" when the
    project has at least one task in any non-terminal status, even
    when the config key and DB project name disagree.
    """
    project_path = tmp_path / "blackjack-trainer"
    project_path.mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_split_key_config(project_path, config_path)

    workspace_db = tmp_path / ".pollypm" / "state.db"
    workspace_db.parent.mkdir(parents=True, exist_ok=True)
    with SQLiteWorkService(db_path=workspace_db, project_path=tmp_path) as svc:
        task = svc.create(
            title="Active feature",
            description="seed",
            type="task",
            project="blackjack-trainer",
            flow_template="standard",
            roles={"worker": "worker", "reviewer": "reviewer"},
            priority="normal",
            created_by="polly",
        )
        svc.queue(task.task_id, "polly")
        svc.claim(task.task_id, "worker")

    from pollypm import cockpit_ui as _cockpit_ui

    _cockpit_ui._PROJECT_DASHBOARD_TASK_CACHE.clear()
    data = _cockpit_ui._gather_project_dashboard(
        config_path, "blackjack_trainer",
    )

    assert data is not None
    # Bypass the App so we can call the renderer without a full Pilot.
    rendered = _cockpit_ui.PollyProjectDashboardApp._render_pipeline_body(
        None, data,  # type: ignore[arg-type]
    )
    assert "No tasks yet" not in rendered
    assert "in progress" in rendered.lower()


def test_dashboard_now_body_acknowledges_in_progress_without_heartbeat(
    tmp_path: Path,
) -> None:
    """#920 — a claimed task with no live heartbeat must still surface
    in the "Current activity" pane, not fall through to "Idle. No tasks
    in flight and no user action needed."
    """
    project_path = tmp_path / "blackjack-trainer"
    project_path.mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_split_key_config(project_path, config_path)

    workspace_db = tmp_path / ".pollypm" / "state.db"
    workspace_db.parent.mkdir(parents=True, exist_ok=True)
    with SQLiteWorkService(db_path=workspace_db, project_path=tmp_path) as svc:
        task = svc.create(
            title="Active feature",
            description="seed",
            type="task",
            project="blackjack-trainer",
            flow_template="standard",
            roles={"worker": "worker", "reviewer": "reviewer"},
            priority="normal",
            created_by="polly",
        )
        svc.queue(task.task_id, "polly")
        svc.claim(task.task_id, "worker")

    from pollypm import cockpit_ui as _cockpit_ui

    _cockpit_ui._PROJECT_DASHBOARD_TASK_CACHE.clear()
    data = _cockpit_ui._gather_project_dashboard(
        config_path, "blackjack_trainer",
    )
    assert data is not None
    # Active worker may be None (no heartbeat yet) — that is precisely
    # the case the bug reproduced under.
    rendered = _cockpit_ui.PollyProjectDashboardApp._render_now_body(
        None, data,  # type: ignore[arg-type]
    )
    assert "Idle" not in rendered
    assert "No tasks in flight" not in rendered
    assert "In flight" in rendered


def test_dashboard_storage_aliases_round_trip() -> None:
    """``_project_storage_aliases`` must yield every form the work-DB
    might have stored a project under.
    """
    from pollypm.cockpit_ui import _project_storage_aliases
    from pollypm.models import KnownProject

    cfg = type("_C", (), {})()
    cfg.projects = {
        "blackjack_trainer": KnownProject(
            key="blackjack_trainer",
            path=Path("/tmp/blackjack-trainer"),
            name="blackjack-trainer",
        ),
    }
    aliases = _project_storage_aliases(cfg, "blackjack_trainer")
    assert "blackjack_trainer" in aliases
    assert "blackjack-trainer" in aliases
    # Order: config key first, then display name, then path basename,
    # then explicit hyphen/underscore swaps.
    assert aliases[0] == "blackjack_trainer"


# ---------------------------------------------------------------------------
# #915 regression — queued task on the canonical ``pollypm`` project must
# surface even when the per-project ``display_name`` override clobbers
# the canonical pre-override name (``PollyPM``) that legacy task rows may
# still be stored under.
# ---------------------------------------------------------------------------


def _write_pollypm_config_with_local_override(
    project_path: Path, config_path: Path,
) -> None:
    """Mirror the production layout where the global config declares
    ``name = "PollyPM"`` but the project-local ``project.toml`` overrides
    ``display_name = "pollypm"``. The override pipes through
    ``_merge_project_local_config`` and clobbers ``project.name``, leaving
    only the lowercase form on the in-memory ``KnownProject``.
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "[project]\n"
        'tmux_session = "pollypm-test"\n'
        f'workspace_root = "{project_path.parent}"\n'
        "\n"
        "[projects.pollypm]\n"
        'name = "PollyPM"\n'
        f'path = "{project_path}"\n'
    )
    local_dir = project_path / ".pollypm" / "config"
    local_dir.mkdir(parents=True, exist_ok=True)
    (local_dir / "project.toml").write_text(
        "[project]\n"
        'display_name = "pollypm"\n'
    )


def test_dashboard_storage_aliases_include_canonical_pollypm() -> None:
    """#915 — ``_project_storage_aliases`` for the ``pollypm`` key must
    include the canonical ``PollyPM`` form even when the in-memory
    ``KnownProject.name`` has been overwritten with ``"pollypm"`` by the
    project-local ``display_name`` override. Otherwise legacy task rows
    stored under ``PollyPM`` are silently invisible.
    """
    from pollypm.cockpit_ui import _project_storage_aliases
    from pollypm.models import KnownProject

    cfg = type("_C", (), {})()
    cfg.projects = {
        "pollypm": KnownProject(
            key="pollypm",
            path=Path("/tmp/pollypm"),
            name="pollypm",  # post-override lowercase
        ),
    }
    aliases = _project_storage_aliases(cfg, "pollypm")
    assert "pollypm" in aliases
    assert "PollyPM" in aliases  # canonical pre-override form


def test_dashboard_finds_queued_task_on_pollypm_project(tmp_path: Path) -> None:
    """#915 — a queued task on the canonical ``pollypm`` project must
    appear in ``_dashboard_gather_tasks``'s output, the ``queued`` count
    must increment, and the pipeline body must NOT render "No tasks yet."

    Reproduces the exact bug shape reported in #915: Polly creates and
    queues ``pollypm/1``, ``pm task counts --project pollypm`` confirms
    it, but the project Dashboard's pipeline panel claims the project
    is empty.
    """
    project_path = tmp_path / "pollypm"
    project_path.mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_pollypm_config_with_local_override(project_path, config_path)

    db_path = project_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with SQLiteWorkService(db_path=db_path, project_path=project_path) as svc:
        task = svc.create(
            title="Fix #914: 'Open PollyPM' on Step 4 drops user into bash",
            description="seed",
            type="task",
            project="pollypm",
            flow_template="standard",
            roles={"worker": "worker", "reviewer": "reviewer"},
            priority="normal",
            created_by="polly",
        )
        svc.queue(task.task_id, "polly")

    from pollypm import cockpit_ui as _cockpit_ui

    _cockpit_ui._PROJECT_DASHBOARD_TASK_CACHE.clear()
    data = _cockpit_ui._gather_project_dashboard(config_path, "pollypm")

    assert data is not None
    # Counts must increment.
    assert data.task_counts.get("queued") == 1
    # "In Pipeline" / queued bucket must surface the task.
    queued_titles = [r["title"] for r in data.task_buckets.get("queued", [])]
    assert any("Fix #914" in t for t in queued_titles)

    # Pipeline body must NOT degrade to "No tasks yet."
    rendered = _cockpit_ui.PollyProjectDashboardApp._render_pipeline_body(
        None, data,  # type: ignore[arg-type]
    )
    assert "No tasks yet" not in rendered
    assert "queued" in rendered.lower()
    # "Recent activity" / now body must mention the queued task too.
    now_rendered = _cockpit_ui.PollyProjectDashboardApp._render_now_body(
        None, data,  # type: ignore[arg-type]
    )
    assert "Idle. No tasks in flight" not in now_rendered
    assert "Next queued task" in now_rendered


def test_dashboard_finds_legacy_capitalcased_task_on_pollypm_project(
    tmp_path: Path,
) -> None:
    """#915 — a task stored under the canonical ``PollyPM`` label (created
    BEFORE the local ``display_name = "pollypm"`` override clobbered the
    in-memory display name) must still surface on the dashboard. The
    alias resolver must include the canonical pre-override form so legacy
    rows aren't silently orphaned.
    """
    project_path = tmp_path / "pollypm"
    project_path.mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_pollypm_config_with_local_override(project_path, config_path)

    db_path = project_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with SQLiteWorkService(db_path=db_path, project_path=project_path) as svc:
        task = svc.create(
            title="Legacy task created when name was PollyPM",
            description="seed",
            type="task",
            project="PollyPM",  # canonical pre-override form
            flow_template="standard",
            roles={"worker": "worker", "reviewer": "reviewer"},
            priority="normal",
            created_by="polly",
        )
        svc.queue(task.task_id, "polly")

    from pollypm import cockpit_ui as _cockpit_ui

    _cockpit_ui._PROJECT_DASHBOARD_TASK_CACHE.clear()
    data = _cockpit_ui._gather_project_dashboard(config_path, "pollypm")

    assert data is not None
    assert data.task_counts.get("queued") == 1
    assert [r["title"] for r in data.task_buckets.get("queued", [])] == [
        "Legacy task created when name was PollyPM"
    ]


def test_dashboard_db_alias_discovery_finds_uncommon_casing(tmp_path: Path) -> None:
    """#915 — ``_dashboard_discover_db_aliases`` must surface DB labels
    that case-insensitively (or via slugify) match a known alias. Catches
    drift from operator-typed casing variants (``-p Pollypm``,
    ``-p POLLYPM``, etc.).
    """
    project_path = tmp_path / "pollypm"
    project_path.mkdir()

    db_path = project_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with SQLiteWorkService(db_path=db_path, project_path=project_path) as svc:
        for label in ("PollyPM", "pollypm", "POLLYPM"):
            t = svc.create(
                title=f"task-{label}",
                description="seed",
                type="task",
                project=label,
                flow_template="standard",
                roles={"worker": "w", "reviewer": "r"},
                priority="normal",
                created_by="polly",
            )
            svc.queue(t.task_id, "polly")

    from pollypm.cockpit_ui import _dashboard_discover_db_aliases

    discovered = _dashboard_discover_db_aliases(db_path, ["pollypm"])
    # ``POLLYPM`` is neither in the static alias list nor produced by
    # ``_normalize_project_display_name`` — it must come back from the
    # DB-discovery scan.
    assert "POLLYPM" in discovered


def test_dashboard_reads_workspace_root_tasks_when_project_db_missing(tmp_path: Path) -> None:
    """Tasks created through the default CLI DB still appear on the project dashboard."""
    project_path = tmp_path / "demo"
    project_path.mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_config(project_path, config_path)

    workspace_db = tmp_path / ".pollypm" / "state.db"
    workspace_db.parent.mkdir(parents=True, exist_ok=True)
    with SQLiteWorkService(db_path=workspace_db, project_path=tmp_path) as svc:
        task = svc.create(
            title="Workspace queued task",
            description="Created from the workspace-root task DB.",
            type="task",
            project="demo",
            flow_template="standard",
            roles={"worker": "worker", "reviewer": "reviewer"},
            priority="normal",
            created_by="polly",
        )
        svc.queue(task.task_id, "polly")

    from pollypm import cockpit_ui as _cockpit_ui
    from pollypm.cockpit_ui import _gather_project_dashboard

    _cockpit_ui._PROJECT_DASHBOARD_TASK_CACHE.clear()
    data = _gather_project_dashboard(config_path, "demo")

    assert data is not None
    assert data.task_counts.get("queued") == 1
    assert [row["title"] for row in data.task_buckets.get("queued", [])] == [
        "Workspace queued task"
    ]


def _run(coro) -> None:
    asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. Top bar — project name + PM persona
# ---------------------------------------------------------------------------


def test_topbar_renders_name_and_default_pm(dashboard_env, dashboard_app) -> None:
    """With no persona configured the top bar uses a neutral PM label."""
    async def body() -> None:
        async with dashboard_app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            topbar_text = str(dashboard_app.topbar.render())
            assert "Demo" in topbar_text
            assert "PM: Project PM" in topbar_text
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


def test_project_pm_labels_match_architect_session_persona(tmp_path: Path) -> None:
    """When PM Chat routes to an architect session, adjacent content
    surfaces should name Archie instead of the project's worker persona.
    """
    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        "[project]\n"
        'tmux_session = "pollypm-test"\n'
        f'workspace_root = "{tmp_path}"\n'
        "\n"
        "[accounts.controller]\n"
        'provider = "claude"\n'
        "\n"
        "[sessions.architect_demo]\n"
        'role = "architect"\n'
        'provider = "claude"\n'
        'account = "controller"\n'
        f'cwd = "{project_path}"\n'
        'project = "demo"\n'
        'window_name = "architect-demo"\n'
        "\n"
        "[projects.demo]\n"
        'key = "demo"\n'
        'name = "Demo"\n'
        f'path = "{project_path}"\n'
        'persona_name = "Bea"\n',
        encoding="utf-8",
    )
    _seed_tasks(project_path)
    if not _load_config_compatible(config_path):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")

    from pollypm.cockpit_ui import _gather_project_dashboard, _resolve_pm_target

    dashboard = _gather_project_dashboard(config_path, "demo")
    assert dashboard is not None
    assert dashboard.pm_label == "PM: Archie"
    assert _resolve_pm_target(config_path, "demo") == (
        "project:demo:session",
        "Archie",
    )


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
    """In_progress task with no live worker → ``in progress`` (yellow).

    The fixture seeds one ``in_progress`` task. Before #920, this fell
    through to ``idle`` because the status pill only checked for a
    heartbeat-alive worker. The fix surfaces ``in_progress`` count so
    the pill, banner, and counts strip agree.
    """
    async def body() -> None:
        async with dashboard_app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            assert dashboard_app.data is not None
            # Only non-inbox tasks were seeded; inbox should be zero.
            assert dashboard_app.data.inbox_count == 0
            assert dashboard_app.data.active_worker is None
            assert dashboard_app.data.status_label == "in progress"
    _run(body())


def test_plan_gate_alert_counts_as_project_attention(
    dashboard_env, monkeypatch,
) -> None:
    """Synthetic plan_gate alerts should make the project dashboard yellow."""
    from types import SimpleNamespace

    from pollypm.cockpit_ui import _dashboard_active_worker

    class FakeStore:
        def open_alerts(self) -> list[object]:
            return [
                SimpleNamespace(
                    session_name="plan_gate-demo",
                    alert_type="plan_missing",
                )
            ]

    class FakeSupervisor:
        store = FakeStore()

        def plan_launches(self) -> list[object]:
            return []

    class FakeService:
        def __init__(self, _config_path: Path) -> None:
            pass

        def load_supervisor(self) -> FakeSupervisor:
            return FakeSupervisor()

    monkeypatch.setattr("pollypm.service_api.PollyPMService", FakeService)

    worker, alert_count = _dashboard_active_worker(
        dashboard_env["config_path"], "demo",
    )

    assert worker is None
    assert alert_count == 1


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
            # Banner promotes on_hold to the lead — "Paused: 1 task is
            # on hold" — so the user sees the user-attention need
            # instead of a misleading "Moving now" framing. Either
            # singular/plural shape satisfies the intent (visible
            # on-hold count in the banner).
            action_bar_text = str(dashboard_app.action_bar.render())
            assert (
                "1 task is on hold" in action_bar_text
                or "1 on hold" in action_bar_text
            )
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
            # Inbox content is split across inbox_lead, per-card
            # Statics, and the trailing inbox_body so each card's
            # response controls can sit directly under it. Use the
            # combined helper for content assertions.
            rendered = dashboard_app._inbox_section_text()
            assert "To move this project forward" in rendered
            assert "Action Required" not in rendered
            assert "Details" not in rendered
            assert "What you need to set up" in rendered
            assert "Set up the Fly.io app for this project" in rendered
            assert "Give Polly deployment access" in rendered
            assert "Decision" in rendered
            assert "approve the code work now" in rendered
            assert dashboard_app.action_primary_buttons[0].label.plain == "1 Approve it anyway"
            assert (
                dashboard_app.action_secondary_buttons[0].label.plain
                == "2 Wait until environment is set"
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
            rendered = dashboard_app._inbox_section_text()
            assert "A full project plan is ready for your review" in rendered
            assert "Open the plan review surface" in rendered
            assert "Waiting on you:" in str(dashboard_app.action_bar.render())
            assert dashboard_app.action_primary_buttons[0].label.plain == "1 Review plan"
            assert dashboard_app.action_secondary_buttons[0].label.plain == "2 Open task"
            assert "1 primary" in str(dashboard_app.hint.render())

            # Audit fix: the "Review plan" primary button now routes
            # directly to the underlying plan_project task instead of
            # dropping into the bare inbox. The task ref comes from the
            # ``plan_task:<ref>`` label on the message — here, demo/3.
            routed_tasks: list[str] = []
            routed_inbox: list[str] = []
            dashboard_app.action_jump_inbox = lambda: routed_inbox.append("inbox")  # type: ignore[method-assign]
            dashboard_app._route_to_task = lambda task_id: routed_tasks.append(task_id)  # type: ignore[method-assign]
            await pilot.press("1")
            await pilot.pause()
            assert routed_tasks == ["demo/3"]
            assert routed_inbox == []

            await pilot.press("3")
            await pilot.pause()
            assert dashboard_app.action_other_inputs[0].has_focus

    _run(body())


def test_inbox_remainder_shows_also_on_hold_summary_under_action_card(
    dashboard_app,
) -> None:
    """When the dashboard renders an Action Needed card AND the project
    has on_hold tasks, the inbox remainder surfaces a compact ``Also
    on hold: #N <title>`` line right under the action card.

    Booktalk (live, 2026-04-26): banner read ``Waiting on you: A full
    project plan is ready for your review. · 1 on hold`` but the
    dashboard gave no detail on what was on hold until the user
    scrolled to Task pipeline. The summary line closes that gap.
    """
    from types import SimpleNamespace

    fake_data = SimpleNamespace(
        inbox_count=1,
        task_counts={"on_hold": 1},
        task_buckets={
            "on_hold": [
                {
                    "task_number": 2,
                    "title": "Phase 2: Scraper + SQLite infrastructure",
                },
            ],
        },
        action_items=[
            {
                "task_id": "msg:demo:1",
                "primary_ref": "demo/3",
                "title": "Plan ready",
                "plain_prompt": "A full project plan is ready.",
                "decision_question": "Approve?",
                "unblock_steps": ["Open the plan."],
                "needs_action": True,
            },
        ],
        inbox_top=[],
    )
    body = dashboard_app._render_inbox_remainder(fake_data)
    assert "Also on hold:" in body
    assert "#2" in body
    assert "Phase 2" in body


def test_inbox_remainder_drops_also_on_hold_when_action_card_already_covers_it(
    dashboard_app,
) -> None:
    """Don't double-list the same task: if the on_hold task IS the
    action card's primary_ref, the summary line is skipped (the card
    already covers it)."""
    from types import SimpleNamespace

    fake_data = SimpleNamespace(
        inbox_count=1,
        task_counts={"on_hold": 1},
        task_buckets={
            "on_hold": [
                {"task_number": 3, "title": "Plan project demo"},
            ],
        },
        action_items=[
            {
                "task_id": "msg:demo:1",
                "primary_ref": "demo/3",  # same as the on_hold task
                "title": "Plan ready",
                "plain_prompt": "A full project plan is ready.",
                "decision_question": "Approve?",
                "unblock_steps": ["Open the plan."],
                "needs_action": True,
            },
        ],
        inbox_top=[],
    )
    body = dashboard_app._render_inbox_remainder(fake_data)
    assert "Also on hold:" not in body


def test_inbox_remainder_skips_also_on_hold_when_no_action_card(
    dashboard_app,
) -> None:
    """The ``Also on hold`` summary is a sibling-to-action-card affordance.
    Without action_items, the existing ``On hold`` inbox section
    renders the full detail — no need for the compact summary."""
    from types import SimpleNamespace

    fake_data = SimpleNamespace(
        inbox_count=0,
        task_counts={"on_hold": 1},
        task_buckets={
            "on_hold": [
                {"task_number": 2, "title": "Phase 2"},
            ],
        },
        action_items=[],
        inbox_top=[],
    )
    body = dashboard_app._render_inbox_remainder(fake_data)
    assert "Also on hold:" not in body
    # Original on_hold inbox section should still render.
    assert "On hold" in body


def test_action_open_plan_with_enforce_plan_false_says_bypass(
    dashboard_app,
) -> None:
    """``p`` (open plan view) on a no-plan project with enforce_plan=false
    surfaces the explicit bypass instead of nudging the user about
    a missing plan. Same shape as the dashboard Plan section copy
    (cycle 6).
    """
    from types import SimpleNamespace

    dashboard_app.data = SimpleNamespace(
        plan_path=None,
        enforce_plan=False,
    )
    notifications: list[tuple[str, str]] = []
    dashboard_app.notify = lambda msg, **kw: notifications.append(  # type: ignore[method-assign]
        (kw.get("severity", "information"), msg)
    )
    dashboard_app.action_open_plan()
    assert len(notifications) == 1
    severity, msg = notifications[0]
    assert "Plan not required" in msg
    assert "enforce_plan = false" in msg
    assert severity == "information"


def test_action_open_plan_with_enforce_plan_true_warns_about_missing(
    dashboard_app,
) -> None:
    """Default enforce_plan=true keeps the original 'no plan yet' warning."""
    from types import SimpleNamespace

    dashboard_app.data = SimpleNamespace(
        plan_path=None,
        enforce_plan=True,
    )
    notifications: list[tuple[str, str]] = []
    dashboard_app.notify = lambda msg, **kw: notifications.append(  # type: ignore[method-assign]
        (kw.get("severity", "information"), msg)
    )
    dashboard_app.action_open_plan()
    assert len(notifications) == 1
    severity, msg = notifications[0]
    assert "No plan file yet" in msg
    assert severity == "warning"


def test_action_open_editor_with_enforce_plan_false_says_bypass(
    dashboard_app,
) -> None:
    """``e`` (open in editor) honors the bypass identically."""
    from types import SimpleNamespace

    dashboard_app.data = SimpleNamespace(
        plan_path=None,
        enforce_plan=False,
    )
    notifications: list[tuple[str, str]] = []
    dashboard_app.notify = lambda msg, **kw: notifications.append(  # type: ignore[method-assign]
        (kw.get("severity", "information"), msg)
    )
    dashboard_app.action_open_editor()
    assert len(notifications) == 1
    severity, msg = notifications[0]
    assert "Plan not required" in msg
    assert "enforce_plan = false" in msg
    assert severity == "information"


def test_record_action_response_distinguishes_resume_from_no_op(
    dashboard_app, monkeypatch,
) -> None:
    """When 'Approve' resumes an on_hold task that lands somewhere other
    than ``review``, the toast says ``Resumed task (was on_hold, now
    queued)`` instead of the misleading ``task stayed at 'queued'``.

    Original copy lumped two distinct cases together: (1) click did
    nothing because the task was never approvable, and (2) click
    resumed the task but stopped short of approval. The user reading
    "stayed at 'queued'" after a real on_hold → queued transition
    couldn't tell their click had moved anything. Now case (1) keeps
    the warning toast and case (2) gets an information toast that
    names the transition.
    """
    from types import SimpleNamespace

    # Pin the action item to a real-shape task ref so the early bail-
    # out gates (action_item_at, primary_ref regex) all pass.
    fake_action_item = {
        "task_id": "demo/1",
        "primary_ref": "demo/1",
        "primary_response": "Approve it anyway.",
    }
    dashboard_app._action_item_at = lambda idx: fake_action_item  # type: ignore[method-assign]

    # ProjectDashboardData stub — needs project_path so the service
    # path resolution doesn't bail.
    dashboard_app.data = SimpleNamespace(
        project_path=Path("/tmp/dashboard-test-fake"),
    )

    # Replay the on_hold → queued transition. ``svc.get`` is called
    # twice (initial_status + final_status) and once between resume
    # checks, so the iterator drives all four calls.
    statuses = iter([
        SimpleNamespace(work_status=SimpleNamespace(value="on_hold")),  # initial
        SimpleNamespace(work_status=SimpleNamespace(value="on_hold")),  # before resume check
        SimpleNamespace(work_status=SimpleNamespace(value="queued")),   # after resume
        SimpleNamespace(work_status=SimpleNamespace(value="queued")),   # final
    ])

    class _FakeSvc:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, _task_id):
            return next(statuses)
        def add_reply(self, _task_id, _response, *, actor): pass
        def resume(self, _task_id, _actor):
            return SimpleNamespace(work_status=SimpleNamespace(value="queued"))
        def approve(self, _task_id, _actor, _response):  # not called
            raise AssertionError("approve should not fire when status != review")

    monkeypatch.setattr(
        "pollypm.work.sqlite_service.SQLiteWorkService", _FakeSvc,
    )

    notifications: list[tuple[str, str]] = []
    dashboard_app.notify = lambda msg, **kw: notifications.append(  # type: ignore[method-assign]
        (kw.get("severity", "information"), msg)
    )
    # Prevent _refresh from doing real I/O.
    dashboard_app._refresh = lambda: None  # type: ignore[method-assign]

    dashboard_app._record_action_response(
        0, "Approve it anyway.", approve_if_possible=True,
    )

    assert len(notifications) == 1
    severity, msg = notifications[0]
    assert "Resumed task" in msg
    assert "on_hold" in msg
    assert "queued" in msg
    # Resume is real progress, not a warning.
    assert severity == "information"
    # Old "stayed at" copy must not appear for a state that did change.
    assert "stayed at" not in msg


def test_record_action_response_warns_when_state_truly_unchanged(
    dashboard_app, monkeypatch,
) -> None:
    """If 'Approve' fires on a state that PollyPM can't transition AND
    the status doesn't change, keep the warning toast — the user's
    click really did just save a reply."""
    from types import SimpleNamespace

    fake_action_item = {
        "task_id": "demo/1",
        "primary_ref": "demo/1",
        "primary_response": "Approve it anyway.",
    }
    dashboard_app._action_item_at = lambda idx: fake_action_item  # type: ignore[method-assign]
    dashboard_app.data = SimpleNamespace(
        project_path=Path("/tmp/dashboard-test-fake"),
    )

    statuses = iter([
        SimpleNamespace(work_status=SimpleNamespace(value="blocked")),  # initial
        SimpleNamespace(work_status=SimpleNamespace(value="blocked")),  # before resume check (not on_hold, no resume)
        SimpleNamespace(work_status=SimpleNamespace(value="blocked")),  # final
    ])

    class _FakeSvc:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, _task_id):
            return next(statuses)
        def add_reply(self, _task_id, _response, *, actor): pass
        def resume(self, *a):
            raise AssertionError("resume should not fire on blocked")
        def approve(self, *a):
            raise AssertionError("approve should not fire on blocked")

    monkeypatch.setattr(
        "pollypm.work.sqlite_service.SQLiteWorkService", _FakeSvc,
    )

    notifications: list[tuple[str, str]] = []
    dashboard_app.notify = lambda msg, **kw: notifications.append(  # type: ignore[method-assign]
        (kw.get("severity", "information"), msg)
    )
    dashboard_app._refresh = lambda: None  # type: ignore[method-assign]

    dashboard_app._record_action_response(
        0, "Approve it anyway.", approve_if_possible=True,
    )

    assert len(notifications) == 1
    severity, msg = notifications[0]
    assert "stayed at 'blocked'" in msg
    assert severity == "warning"


def test_review_plan_falls_back_to_inbox_without_task_ref(dashboard_app) -> None:
    """When a ``review_plan`` action has no resolvable task ref (older
    messages, malformed primary_ref), the button still lands the user
    somewhere useful — the inbox — rather than no-oping silently.
    """
    routed_inbox: list[str] = []
    routed_tasks: list[str] = []
    dashboard_app.action_jump_inbox = lambda: routed_inbox.append("inbox")  # type: ignore[method-assign]
    dashboard_app._route_to_task = lambda task_id: routed_tasks.append(task_id)  # type: ignore[method-assign]

    fake_action_item = {
        "task_id": "msg:demo:1",
        "primary_ref": "blocker-summary:42",  # NOT a project/N task ref
        "primary_action": {"kind": "review_plan", "label": "Review plan"},
    }
    dashboard_app._action_item_at = lambda idx: fake_action_item  # type: ignore[method-assign]

    dashboard_app._perform_dashboard_action(0, "primary")
    assert routed_inbox == ["inbox"]
    assert routed_tasks == []


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
            rendered = dashboard_app._inbox_section_text()
            assert "needs your API key" in rendered
            assert "Add the Bookshop API key" in rendered
            assert "Internal details" not in rendered
            assert "Waiting on you:" in str(dashboard_app.action_bar.render())
            assert dashboard_app.action_primary_buttons[0].label.plain == "1 Open task"
            assert dashboard_app.action_secondary_buttons[0].label.plain == "2 Discuss"

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
            # The session name "architect_demo" is just role + project
            # context — both already implicit on this dashboard — so
            # the row collapses to just the role label.
            assert "architect" in rendered
            assert "architect_demo" not in rendered
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


def test_current_activity_keeps_session_name_when_distinct_from_role(
    dashboard_env, dashboard_app, monkeypatch,
) -> None:
    """When the session name carries information beyond the role
    (e.g. ``task-demo-7``), keep showing both — the session name
    isn't a redundant restatement of the role and may help the
    operator identify which task the worker is on."""
    from datetime import UTC, datetime

    fake_worker = {
        "session_name": "task-demo-7",
        "role": "worker",
        "last_heartbeat": datetime.now(UTC).isoformat(),
    }

    def _fake_active_worker(config_path, project_key, *, action_items=None):
        return fake_worker, 0

    from pollypm import cockpit_ui as _cockpit_ui
    monkeypatch.setattr(
        _cockpit_ui, "_dashboard_active_worker", _fake_active_worker,
    )

    async def body() -> None:
        async with dashboard_app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            rendered = str(dashboard_app.now_body.render())
            # Both bits of identity survive when they're distinct.
            assert "task-demo-7" in rendered
            assert "worker" in rendered

    _run(body())


def test_inbox_section_keeps_press_i_hint_when_inbox_has_spillover(
    dashboard_env, dashboard_app,
) -> None:
    """The 'Press i to jump to the inbox' CTA must stay visible when
    there are more inbox items than what the dashboard surfaces —
    that's the case where the user needs to know there's more to
    find. Suppressing it then would hide work."""
    db_path = dashboard_env["project_path"] / ".pollypm" / "state.db"
    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        # Three distinct action-needed notifications. The dashboard
        # only renders the first two as full cards, so the third
        # creates spillover where the user needs the "press i" hint
        # to find it.
        for i, suffix in enumerate(("alpha", "beta", "gamma"), start=1):
            store.enqueue_message(
                type="notify",
                tier="immediate",
                recipient="user",
                sender="architect",
                subject=f"Decide on {suffix}",
                body=f"Decide on {suffix}.",
                scope="demo",
                labels=["project:demo"],
                payload={
                    "actor": "architect",
                    "project": "demo",
                    "task_id": f"demo/{i + 100}",
                    "user_prompt": {
                        "summary": f"Need a call on {suffix}.",
                        "steps": ["Look"],
                        "question": "Approve?",
                        "actions": [
                            {"label": "Approve", "kind": "approve_task",
                             "task_id": f"demo/{i + 100}"},
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
            rendered = str(dashboard_app.inbox_body.render())
            # Spillover present → the inline CTA stays so the user
            # knows where to look for the rest.
            assert "Press i to jump to the inbox" in rendered

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
            # And the "Press i to jump to the inbox" CTA is also
            # redundant when there's no inbox spillover beyond what
            # the cards already show — the screen footer already
            # exposes the ``i`` keybinding.
            assert "Press i to jump to the inbox" not in rendered, (
                "redundant 'press i' hint printed when no inbox spillover: "
                f"{rendered!r}"
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


def test_banner_review_count_drops_action_overlap() -> None:
    """Booktalk's banner read::

        Waiting on you: A full project plan is ready for your review.
            · 1 on hold · 1 approval

    The "1 approval" was the very review task the lede named —
    counting it separately double-states the same work. Drop any
    review task from the count when its id is the primary_ref of an
    action card.
    """
    from pollypm.cockpit_ui import _banner_review_count_after_action_overlap

    review_bucket = [{"task_id": "booktalk/3"}]
    action_items = [{"primary_ref": "booktalk/3"}]
    assert (
        _banner_review_count_after_action_overlap(1, action_items, review_bucket)
        == 0
    )


def test_banner_review_count_keeps_unrelated_reviews() -> None:
    """Review tasks whose id is *not* the subject of any action card
    must still be counted — those are genuine extra approvals
    waiting beyond what the prompt names."""
    from pollypm.cockpit_ui import _banner_review_count_after_action_overlap

    review_bucket = [
        {"task_id": "booktalk/3"},
        {"task_id": "booktalk/7"},
    ]
    action_items = [{"primary_ref": "booktalk/3"}]
    assert (
        _banner_review_count_after_action_overlap(2, action_items, review_bucket)
        == 1
    )


def test_banner_review_count_no_action_items_passes_through() -> None:
    from pollypm.cockpit_ui import _banner_review_count_after_action_overlap

    assert _banner_review_count_after_action_overlap(3, [], []) == 3


def test_banner_on_hold_count_drops_action_overlap() -> None:
    """Polly_remote (live, 2026-04-26) had 2 action cards whose
    primary_refs were the same 2 on_hold tasks. The banner suffix
    still read ``· 2 on hold`` — the user couldn't tell whether
    there were 4 things waiting (2 cards + 2 on_hold) or 2 things
    double-named. Same overlap reduction the review-count helper
    does, applied to on_hold via the generic
    ``_banner_count_after_action_overlap``.
    """
    from pollypm.cockpit_ui import _banner_count_after_action_overlap

    on_hold_bucket = [
        {"task_id": "polly_remote/3"},
        {"task_id": "polly_remote/12"},
    ]
    action_items = [
        {"primary_ref": "polly_remote/12"},
        {"primary_ref": "polly_remote/3"},
    ]
    # Both on_hold tasks are covered by action cards → suffix shows 0.
    assert (
        _banner_count_after_action_overlap(2, action_items, on_hold_bucket)
        == 0
    )


def test_banner_on_hold_count_keeps_extras() -> None:
    """When SOME on_hold tasks are uncovered by action cards, those
    extras still count — only overlap subtracts."""
    from pollypm.cockpit_ui import _banner_count_after_action_overlap

    on_hold_bucket = [
        {"task_id": "polly_remote/3"},   # covered
        {"task_id": "polly_remote/99"},  # NOT covered
    ]
    action_items = [{"primary_ref": "polly_remote/3"}]
    assert (
        _banner_count_after_action_overlap(2, action_items, on_hold_bucket)
        == 1
    )


def test_format_blocked_dep_attaches_title_when_known() -> None:
    """Blocked tasks listed dependencies as bare task IDs
    (``"polly_remote/6, polly_remote/9"``). The user has no idea
    what those tasks are without drilling in. Surface the title
    inline so the dashboard answers "what am I waiting on?"
    without an extra hop.
    """
    from pollypm.cockpit_ui import _format_blocked_dep

    title_map = {
        "polly_remote/6": "Implement N4: notify-api",
        "polly_remote/9": "Implement N7: pollypm-http-shim",
    }
    assert (
        _format_blocked_dep("polly_remote/6", title_map)
        == "polly_remote/6 (Implement N4: notify-api)"
    )


def test_format_blocked_dep_falls_back_to_bare_ref_when_unknown() -> None:
    """Cross-project deps or archived tasks may not appear in the
    title map for the current project. Render the bare ref instead
    of a misleading "(Untitled)" — the user can still navigate to
    it by ID.
    """
    from pollypm.cockpit_ui import _format_blocked_dep

    assert _format_blocked_dep("other_proj/12", {}) == "other_proj/12"
    assert (
        _format_blocked_dep("polly_remote/99", {"polly_remote/6": "x"})
        == "polly_remote/99"
    )


def test_format_blocked_dep_rewrites_in_project_refs_to_hash_form() -> None:
    """When the dependency lives in the same project as the dashboard
    we're rendering, the ``project_key/N`` prefix is jargon — the
    rest of the dashboard already uses ``#N`` form. Drop the project
    prefix for in-project refs so the "waiting on:" line speaks the
    same task-number language as the row above.
    """
    from pollypm.cockpit_ui import _format_blocked_dep

    title_map = {
        "polly_remote/6": "Implement N4: notify-api",
        "polly_remote/9": "Implement N7: pollypm-http-shim",
    }
    assert (
        _format_blocked_dep(
            "polly_remote/6", title_map, current_project="polly_remote",
        )
        == "#6 (Implement N4: notify-api)"
    )


def test_format_blocked_dep_keeps_full_ref_for_cross_project_deps() -> None:
    """Cross-project deps must keep the full ``other_proj/N`` form so
    the operator can tell the dep lives in a different project —
    bare ``#N`` would be ambiguous with the rendering project's own
    task numbers.
    """
    from pollypm.cockpit_ui import _format_blocked_dep

    title_map = {
        "other_proj/3": "Cross-project gating work",
    }
    assert (
        _format_blocked_dep(
            "other_proj/3", title_map, current_project="polly_remote",
        )
        == "other_proj/3 (Cross-project gating work)"
    )


def test_format_blocked_dep_legacy_call_keeps_project_prefix() -> None:
    """Calls without ``current_project`` must keep the existing
    ``project/N (Title)`` rendering — that's the form the older tests
    and direct callers expect."""
    from pollypm.cockpit_ui import _format_blocked_dep

    title_map = {"polly_remote/6": "Implement N4: notify-api"}
    assert (
        _format_blocked_dep("polly_remote/6", title_map)
        == "polly_remote/6 (Implement N4: notify-api)"
    )


def test_format_blocked_dep_truncates_long_titles() -> None:
    """Some plans produce verbose task titles. Three deps × verbose
    titles can blow past the dashboard pane width; truncate the
    title (not the ref) so the line stays scannable.
    """
    from pollypm.cockpit_ui import _format_blocked_dep

    long_title = "A really long descriptive task title that keeps going"
    rendered = _format_blocked_dep(
        "polly_remote/12", {"polly_remote/12": long_title}
    )
    assert rendered.startswith("polly_remote/12 (")
    assert rendered.endswith("…)")
    # The rendered fragment must be much shorter than the raw input.
    assert len(rendered) < len(long_title) + len("polly_remote/12 ()")


def test_render_user_prompt_block_uses_decision_colon_label() -> None:
    """Inline ``[b]Decision[/b]`` without a colon read as the start of
    a sentence rather than a label. Adding the colon turns it into a
    parseable inline label so the operator's eye finds the question
    quickly without having to mentally insert punctuation.
    """
    from pollypm.cockpit_ui import _render_user_prompt_block

    payload = {
        "user_prompt": {
            "summary": "A full project plan is ready.",
            "steps": ["Open the plan."],
            "question": "Approve?",
        }
    }
    rendered = _render_user_prompt_block(payload)
    assert rendered is not None
    assert "[b]Decision:[/b]" in rendered
    # Pre-fix form must be gone.
    assert "[b]Decision[/b] Approve" not in rendered


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
    ) == "Use 1/2/3 for these actions, or click this card to open the source task."

    # Single thread-backed card.
    assert _action_card_click_hint(
        [{"primary_ref": "blocker-summary:42"}]
    ) == "Use 1/2/3 for these actions, or click this card to open the inbox thread."

    # Two task-backed cards: pluralise.
    assert _action_card_click_hint(
        [
            {"primary_ref": "polly_remote/12"},
            {"primary_ref": "polly_remote/9"},
        ]
    ) == "Use 1-3 for the first card and 4-6 for the second, or click any card to open its source task."

    # Mixed: hedge.
    assert _action_card_click_hint(
        [
            {"primary_ref": "polly_remote/12"},
            {"primary_ref": "blocker-summary:42"},
        ]
    ) == "Use 1-3 for the first card and 4-6 for the second, or click any card to open its source task or inbox thread."


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


def test_clean_hold_reason_removes_internal_provisioning_failure() -> None:
    from pollypm.cockpit_ui import _clean_hold_reason

    reason = (
        "Awaiting user Phase A approval. Heartbeat re-assigns in a loop "
        "because tmux session 'pollypm-storage-closet' (task-media-1) "
        "cannot be provisioned, but worker_media session is alive and "
        "the work is intact at b4a7e2a. Resume with: pm task resume "
        "once user replies in the inbox."
    )

    cleaned = _clean_hold_reason(reason)

    assert "Awaiting user Phase A approval" in cleaned
    assert "Resume with: pm task resume" in cleaned
    assert "pollypm-storage-closet" not in cleaned
    assert "cannot be provisioned" not in cleaned


def test_clean_hold_reason_rewrites_known_task_refs_to_hash_form() -> None:
    """Architects and operator-pms sometimes write hold reasons that
    name an upstream task by full ``project_key/N`` ref:

        ``Waiting on operator: polly_remote/12 — same scope escalation
        pattern as polly_remote/3``

    For a non-technical operator that reads as a path, not a task
    number. The rest of the project dashboard already refers to
    in-project tasks as ``#12``/``#3``, so leaving the raw form here
    is a jarring jargon mismatch. With a title_map provided, the
    helper rewrites known refs to ``#N (Title)`` form.
    """
    from pollypm.cockpit_ui import _clean_hold_reason

    title_map = {
        "polly_remote/12": "Implement N-RC1: pollypm-reachability",
        "polly_remote/3": "Implement N1: deploy-bootstrap",
    }
    out = _clean_hold_reason(
        "Waiting on operator: polly_remote/12 — same scope escalation "
        "pattern as polly_remote/3",
        title_map,
    )
    assert "polly_remote/12" not in out
    assert "polly_remote/3" not in out
    # Titles longer than 28 chars are truncated to keep prose readable.
    assert "#12 (Implement N-RC1: pollypm-re" in out
    assert "#3 (Implement N1: deploy-bootst" in out


def test_clean_hold_reason_leaves_unknown_refs_untouched() -> None:
    """Refs absent from the title_map (cross-project, archived, or
    just unrecognised) must not be silently rewritten — collapsing
    ``other_project/3`` to ``#3`` would mislead the operator into
    thinking the rest of *this* dashboard's #3 is the upstream."""
    from pollypm.cockpit_ui import _clean_hold_reason

    out = _clean_hold_reason(
        "Waiting on cross-project work: other_project/9 to land",
        {"polly_remote/12": "Some title"},
    )
    assert "other_project/9" in out


def test_clean_hold_reason_handles_mixed_case_project_keys() -> None:
    """Cycle 90: the rewrite regex was lowercase-only — a project
    keyed ``MyProject`` or ``proj-x`` slipped through unrewritten,
    leaving ``MyProject/12`` raw in the hold reason. Align with
    ``_PROJECT_TASK_REF_RE`` (case-aware, allows hyphens).
    """
    from pollypm.cockpit_ui import _clean_hold_reason

    out = _clean_hold_reason(
        "Waiting on operator: MyProject/7 — see proj-x/3 for context",
        {
            "MyProject/7": "Mixed-case title",
            "proj-x/3": "Hyphen-keyed title",
        },
    )
    assert "MyProject/7" not in out
    assert "proj-x/3" not in out
    assert "#7 (Mixed-case title)" in out
    assert "#3 (Hyphen-keyed title)" in out


def test_clean_hold_reason_elides_self_reference_in_held_row() -> None:
    """When the held task's hold reason names the held task itself,
    repeating ``#N (Title)`` on the row that already shows
    ``#N <title>`` is tautological — strip the self-reference and
    clean up the dangling colon so prose reads naturally.
    """
    from pollypm.cockpit_ui import _clean_hold_reason

    title_map = {
        "polly_remote/12": "Implement N-RC1: pollypm-reachability",
        "polly_remote/3": "Implement N1: relay-core",
    }
    out = _clean_hold_reason(
        "Waiting on operator: polly_remote/12 — same scope escalation "
        "pattern as polly_remote/3",
        title_map,
        self_task_id="polly_remote/12",
    )
    # Self-reference must be gone, both raw and rewritten forms.
    assert "polly_remote/12" not in out
    assert "#12" not in out
    # Cross-references still rewrite normally.
    assert "#3 (Implement N1: relay-core)" in out
    # Dangling "operator:  —" gets cleaned to "operator —".
    assert "operator: " not in out
    assert out.startswith("Waiting on operator")


def test_clean_hold_reason_self_ref_elision_keeps_other_refs_intact() -> None:
    """Self-ref elision must only drop the held-task's own ref; other
    in-project refs in the hold reason still get the standard
    ``#N (Title)`` rewrite."""
    from pollypm.cockpit_ui import _clean_hold_reason

    title_map = {
        "polly_remote/3": "Implement N1: relay-core",
        "polly_remote/9": "Implement N7: pollypm-http-shim",
    }
    out = _clean_hold_reason(
        "Blocked by polly_remote/9 and polly_remote/3",
        title_map,
        self_task_id="polly_remote/3",
    )
    assert "polly_remote/3" not in out
    assert "polly_remote/9" not in out
    # Title is truncated past 28 chars per the existing rewrite contract.
    assert "#9 (Implement N7: pollypm-http-" in out
    # The self-ref was dropped; the surrounding conjunction may collapse.
    assert "#3" not in out


def test_clean_hold_reason_without_title_map_keeps_legacy_behavior() -> None:
    """Callers that pass no title_map must still get the existing
    [Action]-stripping behaviour with no surprise rewrites."""
    from pollypm.cockpit_ui import _clean_hold_reason

    assert _clean_hold_reason(
        "Waiting on operator: polly_remote/12 to land"
    ) == "Waiting on operator: polly_remote/12 to land"


def test_strip_action_subject_prefix_also_strips_alert_routing_tag() -> None:
    """Alerts emit subjects with an ``[Alert]`` tier prefix from the
    supervisor — same routing-tag pattern as ``[Action]``. The strip
    helper must drop both so the activity feed Message column doesn't
    render ``[Alert] Additional work remains —`` with the bracketed
    routing label leading the prose.
    """
    from pollypm.cockpit_ui import _strip_action_subject_prefix

    assert _strip_action_subject_prefix(
        "[Alert] Additional work remains — open inbox to triage"
    ) == "Additional work remains — open inbox to triage"
    assert _strip_action_subject_prefix(
        "[ALERT] disk filling"
    ) == "disk filling"
    # No tag → unchanged.
    assert _strip_action_subject_prefix(
        "Calculator CLI E2E complete"
    ) == "Calculator CLI E2E complete"


def test_strip_action_subject_prefix_drops_routing_tag() -> None:
    """The ``[Action]`` prefix is a tier/recipient routing label added
    by the notify CLI; it must not survive into user-facing subject
    rendering. The inbox list rail already strips it for action-bucket
    rows, and the detail pane mirrors the same strip — exercise the
    helper directly so both call sites stay in sync.
    """
    from pollypm.cockpit_ui import _strip_action_subject_prefix

    # Standard form: "[Action] <subject>"
    assert _strip_action_subject_prefix(
        "[Action] N-RC1 review (polly_remote/12): code solid"
    ) == "N-RC1 review (polly_remote/12): code solid"

    # Case-insensitive prefix match.
    assert _strip_action_subject_prefix(
        "[ACTION] hold this thing"
    ) == "hold this thing"

    # Strips trailing punctuation/separators glued to the prefix so
    # the rendered subject doesn't lead with stray ":" or "-".
    assert _strip_action_subject_prefix(
        "[Action]: do the thing"
    ) == "do the thing"
    assert _strip_action_subject_prefix(
        "[Action] — escalation"
    ) == "escalation"

    # No prefix → unchanged.
    assert _strip_action_subject_prefix(
        "Plan ready for review: booktalk"
    ) == "Plan ready for review: booktalk"

    # Empty / falsy → unchanged.
    assert _strip_action_subject_prefix("") == ""


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
            # In-project deps render as #N (Title) — the project_key
            # prefix is dropped because we're already on that project's
            # dashboard, matching the rest of the dashboard's task
            # numbering. Cross-project deps would keep the full ref.
            _, _, upstream_n = upstream.task_id.partition("/")
            assert f"#{upstream_n}" in rendered

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


def test_waiting_on_you_banner_surfaces_extra_actions(
    dashboard_env, dashboard_app,
) -> None:
    """When more than one user-facing action is waiting, the banner
    only shows the first prompt — surface the rest as a "+N more
    action(s)" tag so the user doesn't read the banner, take action
    on the first item, and miss the others."""
    db_path = dashboard_env["project_path"] / ".pollypm" / "state.db"
    in_progress_id = dashboard_env["task_ids"]["in_progress"]
    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        for label in ("alpha", "beta"):
            store.enqueue_message(
                type="notify",
                tier="immediate",
                recipient="user",
                sender="architect",
                subject=f"Need decision on {label}",
                body=f"Decide {label}.",
                scope="demo",
                labels=["project:demo"],
                payload={
                    "actor": "architect",
                    "project": "demo",
                    "task_id": f"demo/{1000 if label == 'alpha' else 2000}",
                    "user_prompt": {
                        "summary": f"Need a call on {label}.",
                        "steps": ["Review"],
                        "question": "Approve?",
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

    async def body() -> None:
        async with dashboard_app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            assert dashboard_app.data is not None
            assert dashboard_app.data.inbox_count >= 2
            rendered = str(dashboard_app.action_bar.render())
            assert "Waiting on you:" in rendered
            # The "+N more action" tag must surface so the user knows
            # there's more than what the banner sentence shows.
            assert "+1 more action" in rendered, (
                f"banner missing extras tag: {rendered!r}"
            )

    _run(body())


def test_waiting_on_you_banner_omits_extras_tag_for_single_action(
    dashboard_env, dashboard_app,
) -> None:
    """With exactly one action item the banner already says
    everything — no '+0 more' tag should appear."""
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
            body="Decide.",
            scope="demo",
            labels=["project:demo"],
            payload={
                "actor": "architect",
                "project": "demo",
                "task_id": in_progress_id,
                "user_prompt": {
                    "summary": "Just one.",
                    "steps": ["Review"],
                    "question": "Approve?",
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

    async def body() -> None:
        async with dashboard_app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            rendered = str(dashboard_app.action_bar.render())
            assert "Waiting on you:" in rendered
            assert "more action" not in rendered, (
                f"banner printed extras tag for single action: {rendered!r}"
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


def test_now_body_calls_out_on_hold_when_no_worker_active() -> None:
    """When a project has only on_hold tasks and no worker, the
    "Now" section used to fall through to ``Idle. No tasks in flight``
    — misleading, because on_hold tasks ARE in flight, just paused.
    Surface the on-hold state explicitly so the operator knows where
    to look (the Task pipeline carries the hold reason).
    """
    from types import SimpleNamespace
    from pollypm.cockpit_ui import PollyProjectDashboardApp

    app = PollyProjectDashboardApp.__new__(PollyProjectDashboardApp)
    fake_data = SimpleNamespace(
        active_worker=None,
        action_items=[],
        task_buckets={
            "queued": [], "in_progress": [], "review": [],
            "blocked": [], "on_hold": [], "done": [],
        },
        task_counts={"on_hold": 2},
    )
    rendered = app._render_now_body(fake_data)
    assert "Idle" not in rendered
    assert "on hold" in rendered
    assert "Task pipeline" in rendered


def test_now_body_idle_when_no_tasks_at_all() -> None:
    """Confirmation: a truly empty project still hits the Idle line —
    no on-hold/review/blocked/queued counts → Idle is the right
    message.
    """
    from types import SimpleNamespace
    from pollypm.cockpit_ui import PollyProjectDashboardApp

    app = PollyProjectDashboardApp.__new__(PollyProjectDashboardApp)
    fake_data = SimpleNamespace(
        active_worker=None,
        action_items=[],
        task_buckets={
            "queued": [], "in_progress": [], "review": [],
            "blocked": [], "on_hold": [], "done": [],
        },
        task_counts={},
    )
    rendered = app._render_now_body(fake_data)
    assert "Idle" in rendered
    assert "no user action needed" in rendered


# ---------------------------------------------------------------------------
# #990 — Dashboard activity classifier: heartbeat-alive does NOT mean the
# session is doing work. The bikepath repro had an architect with a fresh
# heartbeat self-reporting "standing by" while the dashboard banner read
# "architect is in action" — factually wrong. The classifier returns
# ``working / idle / awaiting_user`` and the banner / now-section / pill
# read it.
# ---------------------------------------------------------------------------


def test_now_body_says_standing_by_for_idle_worker() -> None:
    """An alive but idle worker (no claimed task, no pane movement)
    must NOT be rendered as ``in action``. The dashboard renders the
    standby state explicitly so the user knows the session is alive
    but not progressing work (#990).
    """
    from types import SimpleNamespace
    from pollypm.cockpit_ui import PollyProjectDashboardApp

    app = PollyProjectDashboardApp.__new__(PollyProjectDashboardApp)
    app.project_key = "bikepath"
    fake_data = SimpleNamespace(
        active_worker={
            "session_name": "architect_bikepath",
            "role": "architect",
            "last_heartbeat": "2026-04-29T22:30:00+00:00",
            "activity": "idle",
        },
        action_items=[],
        task_buckets={
            "queued": [], "in_progress": [], "review": [],
            "blocked": [], "on_hold": [], "done": [],
        },
        task_counts={},
    )
    rendered = app._render_now_body(fake_data)
    assert "standing by" in rendered
    # Yellow/grey dot, NOT the green ● that signals "active".
    assert "[#3ddc84]●[/#3ddc84]" not in rendered
    assert "not progressing" in rendered


def test_now_body_says_waiting_on_input_for_permission_prompt() -> None:
    """An alive worker blocked on a permission prompt is rendered as
    ``waiting on input``, not as active work. Closes the loop with
    the ``pane:permission_prompt`` alert flow (#989, #990)."""
    from types import SimpleNamespace
    from pollypm.cockpit_ui import PollyProjectDashboardApp

    app = PollyProjectDashboardApp.__new__(PollyProjectDashboardApp)
    app.project_key = "bikepath"
    fake_data = SimpleNamespace(
        active_worker={
            "session_name": "architect_bikepath",
            "role": "architect",
            "last_heartbeat": "2026-04-29T22:30:00+00:00",
            "activity": "awaiting_user",
        },
        action_items=[],
        task_buckets={
            "queued": [], "in_progress": [], "review": [],
            "blocked": [], "on_hold": [], "done": [],
        },
        task_counts={},
    )
    rendered = app._render_now_body(fake_data)
    assert "waiting on input" in rendered
    assert "permission prompt" in rendered


def test_now_body_keeps_in_action_for_working_worker() -> None:
    """A worker classified as ``working`` (claimed task + pane moving)
    keeps the green ● and the in-flight task line. Regression guard
    so the #990 fix doesn't over-correct and erase the genuine
    activity signal."""
    from types import SimpleNamespace
    from pollypm.cockpit_ui import PollyProjectDashboardApp

    app = PollyProjectDashboardApp.__new__(PollyProjectDashboardApp)
    app.project_key = "demo"
    fake_data = SimpleNamespace(
        active_worker={
            "session_name": "worker_demo",
            "role": "worker",
            "last_heartbeat": "2026-04-29T22:30:00+00:00",
            "activity": "working",
        },
        action_items=[],
        task_buckets={
            "queued": [],
            "in_progress": [
                {"task_number": 7, "title": "Add foo", "current_node_id": None}
            ],
            "review": [], "blocked": [], "on_hold": [], "done": [],
        },
        task_counts={"in_progress": 1},
    )
    rendered = app._render_now_body(fake_data)
    assert "[#3ddc84]●[/#3ddc84]" in rendered
    assert "Add foo" in rendered
    assert "standing by" not in rendered


def test_status_pill_uses_activity_classification() -> None:
    """The pill colour / label reads the classifier, not just liveness.

    * ``working`` → green ● ``active``
    * ``idle`` → grey ○ ``standing by``
    * ``awaiting_user`` → yellow ◆ ``waiting on input``

    A naive "heartbeat alive ⇒ green active" pill is exactly what
    #990 reproduced on bikepath.
    """
    from pollypm.cockpit_ui import _dashboard_status

    working = {"session_name": "w", "role": "worker", "activity": "working"}
    idle = {"session_name": "w", "role": "architect", "activity": "idle"}
    blocked = {
        "session_name": "w", "role": "architect", "activity": "awaiting_user",
    }

    dot, _, label = _dashboard_status(working, 0, 0, None)
    assert label == "active"
    assert dot == "●"

    dot, _, label = _dashboard_status(idle, 0, 0, None)
    assert label == "standing by"
    assert dot == "○"

    dot, _, label = _dashboard_status(blocked, 0, 0, None)
    assert label == "waiting on input"
    assert dot == "◆"


def test_banner_does_not_claim_in_action_for_idle_worker() -> None:
    """The banner under "Moving now" must not claim an idle worker
    is active. #990 banner read ``Moving now: architect_bikepath
    (architect) is active`` while the architect was self-reporting
    "standing by." The fix routes ``idle`` to a standby banner and
    falls through to category banners (queued/blocked/review) when
    other work is waiting.
    """
    from types import SimpleNamespace

    from pollypm.cockpit_ui import PollyProjectDashboardApp

    app = PollyProjectDashboardApp.__new__(PollyProjectDashboardApp)
    fake_data = SimpleNamespace(
        action_items=[],
        alert_count=0,
        active_worker={
            "session_name": "architect_bikepath",
            "role": "architect",
            "activity": "idle",
        },
        task_counts={},
        task_buckets={"on_hold": []},
        inbox_count=0,
    )
    banner = app._render_project_state_banner(fake_data, "▸ Clear")
    assert "Moving now" not in banner
    assert "is active" not in banner
    assert "standing by" in banner


def test_banner_says_waiting_on_you_for_permission_prompt() -> None:
    """A worker at a permission prompt elevates the banner to
    ``Waiting on you``, putting the operator-required action front
    and centre instead of pretending the agent is making progress.
    """
    from types import SimpleNamespace

    from pollypm.cockpit_ui import PollyProjectDashboardApp

    app = PollyProjectDashboardApp.__new__(PollyProjectDashboardApp)
    fake_data = SimpleNamespace(
        action_items=[],
        alert_count=0,
        active_worker={
            "session_name": "architect_bikepath",
            "role": "architect",
            "activity": "awaiting_user",
        },
        task_counts={},
        task_buckets={"on_hold": []},
        inbox_count=0,
    )
    banner = app._render_project_state_banner(fake_data, "▸ Clear")
    assert "Waiting on you" in banner
    assert "permission prompt" in banner


def test_classify_worker_activity_idle_when_pane_unchanged_and_no_task(
    tmp_path,
) -> None:
    """Direct unit test of the classifier. With matching snapshot
    hashes (pane unchanged) and no claimed task, the classifier
    returns ``idle`` — exactly the bikepath state on #990 repro.
    """
    from types import SimpleNamespace
    from pollypm.cockpit_ui import _classify_worker_activity

    class _FakeStore:
        def recent_heartbeats(self, name, *, limit=2):
            # Same hash twice → pane has not moved.
            return [
                SimpleNamespace(snapshot_hash="abc"),
                SimpleNamespace(snapshot_hash="abc"),
            ]

    sup = SimpleNamespace(store=_FakeStore())
    state = _classify_worker_activity(
        sup,
        session_name="architect_bikepath",
        role="architect",
        project_aliases={"bikepath"},
        project_path=None,
        has_pane_permission_alert=False,
    )
    assert state == "idle"


def test_classify_worker_activity_awaiting_user_short_circuits() -> None:
    """A live ``pane:permission_prompt`` alert wins over any pane /
    task signal — the agent is blocked on the operator regardless of
    whether the pane has moved."""
    from types import SimpleNamespace
    from pollypm.cockpit_ui import _classify_worker_activity

    class _FakeStore:
        def recent_heartbeats(self, name, *, limit=2):
            # Even with the pane changing, a permission prompt blocks
            # the session — classifier should still return awaiting_user.
            return [
                SimpleNamespace(snapshot_hash="abc"),
                SimpleNamespace(snapshot_hash="def"),
            ]

    sup = SimpleNamespace(store=_FakeStore())
    state = _classify_worker_activity(
        sup,
        session_name="worker_demo",
        role="worker",
        project_aliases={"demo"},
        project_path=None,
        has_pane_permission_alert=True,
    )
    assert state == "awaiting_user"


def test_classify_worker_activity_working_when_architect_pane_moves() -> None:
    """An architect with no claimed task but a moving pane is still
    ``working`` — architects don't claim tasks the way workers do, so
    pane movement on its own is the activity signal for them.
    """
    from types import SimpleNamespace
    from pollypm.cockpit_ui import _classify_worker_activity

    class _FakeStore:
        def recent_heartbeats(self, name, *, limit=2):
            return [
                SimpleNamespace(snapshot_hash="hash1"),
                SimpleNamespace(snapshot_hash="hash2"),
            ]

    sup = SimpleNamespace(store=_FakeStore())
    state = _classify_worker_activity(
        sup,
        session_name="architect_demo",
        role="architect",
        project_aliases={"demo"},
        project_path=None,
        has_pane_permission_alert=False,
    )
    assert state == "working"


def test_recent_activity_elides_self_reference_in_transition_reason(
    dashboard_env, dashboard_app, monkeypatch,
) -> None:
    """When the projector's transition row says ``task #N: ... → ...``
    and the parenthesised reason then names the same task again
    (``Waiting on operator: #N — ...``), the row reads as
    ``task #N ... #N ...`` — the inner ``#N`` is tautological since
    the row's prefix already names it. Drop the leading inner ``#N``
    so the reason reads as standalone prose. Mirrors cycle 5's
    hold-reason elision applied at the projector summary layer.
    """
    fake_entries = [
        {
            "timestamp": "2026-04-24T10:00:00+00:00",
            "actor": "pm",
            "verb": "review->on_hold",
            "summary": (
                "task demo/12: review → on_hold "
                "(Waiting on operator: demo/12 — same scope as demo/3)"
            ),
            "kind": "task_transition",
        },
    ]

    def _fake_activity(config_path, project_key, *, limit=10):
        return fake_entries[:limit]

    from pollypm import cockpit_ui
    monkeypatch.setattr(cockpit_ui, "_dashboard_activity", _fake_activity)

    async def body() -> None:
        async with dashboard_app.run_test(size=(160, 60)) as pilot:
            await pilot.pause()
            rendered = str(dashboard_app.activity_body.render())
            # The leading ``task #12:`` survives.
            assert "task #12: review → on_hold" in rendered
            # The inner self-reference (``: #12 — ``) is gone.
            assert "Waiting on operator —" in rendered
            # And the cross-project reference (#3) still survives.
            assert "#3" in rendered

    _run(body())


def test_recent_activity_strips_in_project_task_prefix_and_action_tag(
    dashboard_env, dashboard_app, monkeypatch,
) -> None:
    """Recent activity rows on the per-project dashboard inherit the
    global feed's ``task <project>/<N>: <from> → <to> (<reason>)``
    summary. On the project's own dashboard, the ``<project>/`` prefix
    is implicit and ``[Action]`` tags that leaked into the reason are
    routing noise. Strip both so the row reads as clean prose.
    """
    fake_entries = [
        {
            "timestamp": "2026-04-24T10:00:00+00:00",
            "actor": "pm",
            "verb": "review->on_hold",
            "summary": (
                "task demo/12: review → on_hold "
                "(Waiting on operator: [Action] demo/12 — same scope as demo/3)"
            ),
            "kind": "task_transition",
        },
    ]

    def _fake_activity(config_path, project_key, *, limit=10):
        return fake_entries[:limit]

    from pollypm import cockpit_ui
    monkeypatch.setattr(cockpit_ui, "_dashboard_activity", _fake_activity)

    async def body() -> None:
        async with dashboard_app.run_test(size=(160, 60)) as pilot:
            await pilot.pause()
            rendered = str(dashboard_app.activity_body.render())
            # In-project refs collapse to #N form.
            assert "task #12: review → on_hold" in rendered
            assert "demo/12" not in rendered
            # Cross-references in the reason also collapse.
            assert "#3" in rendered
            assert "demo/3" not in rendered
            # [Action] routing tag stripped from the reason.
            assert "[Action]" not in rendered

    _run(body())


def test_recent_activity_drops_verb_when_summary_already_carries_transition() -> None:
    """Task-transition rows came in with verb=``review->done`` and
    summary=``task polly_remote/17: review → done`` — the same
    transition encoded twice. Drop the verb prefix on
    ``task_transition`` rows so the line reads as one statement of
    fact instead of two near-duplicate ones.
    """
    from types import SimpleNamespace
    from pollypm.cockpit_ui import PollyProjectDashboardApp

    app = PollyProjectDashboardApp.__new__(PollyProjectDashboardApp)
    fake_data = SimpleNamespace(
        activity_entries=[
            {
                "timestamp": "2026-04-24T10:00:00+00:00",
                "actor": "reviewer",
                "verb": "review->done",
                "summary": "task polly_remote/17: review → done",
                "kind": "task_transition",
            },
            {
                "timestamp": "2026-04-24T09:00:00+00:00",
                "actor": "alerter",
                "verb": "alerted",
                "summary": "Disk full on host-A",
                "kind": "alert",
            },
        ],
    )
    rendered = app._render_activity_body(fake_data)
    # Task-transition row shows the summary only — no verb prefix.
    assert "task polly_remote/17: review → done" in rendered
    assert "[b]review->done[/b]" not in rendered
    # Non-transition rows keep the bold verb prefix.
    assert "[b]alerted[/b] Disk full on host-A" in rendered


def test_pipeline_strip_uses_distinct_glyph_for_blocked_vs_in_progress() -> None:
    """Regression: the pipeline strip rendered both ``in_progress`` and
    ``blocked`` with the ◆ glyph, distinguished only by colour. The
    shape duplication made the strip ambiguous in low-colour terminals
    and snapshots — switch ``blocked`` to ▣ so the count strip carries
    six distinct glyphs.
    """
    from types import SimpleNamespace
    from pollypm.cockpit_ui import PollyProjectDashboardApp

    app = PollyProjectDashboardApp.__new__(PollyProjectDashboardApp)
    fake_data = SimpleNamespace(
        exists_on_disk=True,
        task_counts={
            "queued": 0,
            "in_progress": 1,
            "review": 0,
            "blocked": 5,
            "on_hold": 0,
            "done": 3,
        },
        task_buckets={
            "queued": [], "in_progress": [], "review": [],
            "blocked": [], "on_hold": [], "done": [],
        },
    )
    rendered = app._render_pipeline_body(fake_data)
    # in_progress keeps ◆ (filled diamond) — visually says "active".
    assert "◆[/] [b]1[/b] [dim]in progress[/dim]" in rendered
    # blocked now uses ▣ (squared inner square) — visually says
    # "wall / dependency" without overlapping the in_progress glyph.
    assert "▣[/] [b]5[/b] [dim]blocked[/dim]" in rendered
    # Specifically: blocked must NOT use ◆ anymore.
    assert "◆[/] [b]5[/b] [dim]blocked[/dim]" not in rendered


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
            # #920 — the nudge spells out *which pane* to press c in
            # so the user can act on it from the rail-focused state
            # without having to discover the pane focus rule.
            assert "Press" in rendered and "c" in rendered
            assert "in this pane" in rendered
            assert "ask for a plan" in rendered
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
                    "project:demo:session",
                    're: project/demo "dashboard discussion"',
                    "Project PM",
                )
            assert calls, "expected _perform_pm_dispatch to be called"
            cockpit_key, context_line = calls[-1]
            # No persona configured on the fixture, but the project still has
            # its own PM Chat route.
            assert cockpit_key == "project:demo:session"
            assert "project/demo" in context_line
    _run(body())


def test_pm_dispatch_reattaches_recent_duplicate_context(
    dashboard_app, monkeypatch,
) -> None:
    """Re-entering the same PM Chat routes back without re-sending primer."""
    sent: list[tuple[str, str]] = []
    routed: list[str] = []
    notices: list[str] = []
    now = 100.0

    def fake_dispatch(cockpit_key: str, context_line: str) -> None:
        sent.append((cockpit_key, context_line))

    def fake_route(cockpit_key: str):
        routed.append(cockpit_key)
        return (None, "", None)

    def fake_call_from_thread(fn, *args, **kwargs):
        fn(*args, **kwargs)

    monkeypatch.setattr(dashboard_app, "_perform_pm_dispatch", fake_dispatch)
    monkeypatch.setattr(dashboard_app, "_route_pm_target", fake_route)
    monkeypatch.setattr(
        dashboard_app, "_pm_dispatch_now", lambda: now,
    )
    monkeypatch.setattr(dashboard_app, "call_from_thread", fake_call_from_thread)
    monkeypatch.setattr(
        dashboard_app,
        "notify",
        lambda message, **_kw: notices.append(message),
    )

    context_line = 're: project/demo "dashboard discussion"'
    dashboard_app._dispatch_to_pm_sync(
        "project:demo:session", context_line, "Project PM",
    )
    now += 30.0
    dashboard_app._dispatch_to_pm_sync(
        "project:demo:session", context_line, "Project PM",
    )
    now += dashboard_app._PM_CONTEXT_REATTACH_WINDOW_SECONDS + 1.0
    dashboard_app._dispatch_to_pm_sync(
        "project:demo:session", context_line, "Project PM",
    )

    assert sent == [
        ("project:demo:session", context_line),
        ("project:demo:session", context_line),
    ]
    assert routed == ["project:demo:session"]
    assert any("Re-attached to existing PM Chat" in note for note in notices)


# ---------------------------------------------------------------------------
# 7. Keybinding — `q` routes home without exiting the pane
# ---------------------------------------------------------------------------


def test_q_keybinding_routes_to_home(dashboard_env, dashboard_app) -> None:
    """Pressing ``q`` returns to the global dashboard instead of exiting."""
    async def body() -> None:
        calls: list[bool] = []

        def fake_route_to_home(self) -> None:
            calls.append(True)

        from pollypm.cockpit_ui import PollyProjectDashboardApp
        PollyProjectDashboardApp._route_to_home = fake_route_to_home  # type: ignore[assignment]

        async with dashboard_app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
            await pilot.pause()
            if not calls:
                dashboard_app._route_to_home_sync()
            assert calls, "expected _route_to_home to be called"

    _run(body())


# ---------------------------------------------------------------------------
# 8. Keybinding — `i` routes to the inbox
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
# 9. Recent activity — shows the 10 most recent events for the project
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


# ---------------------------------------------------------------------------
# #1015 — "Blocked, but summary missing" predicate refinements
# ---------------------------------------------------------------------------


def test_existing_blocker_context_returns_none_with_no_signal() -> None:
    """No on-hold reason, no explicit blocked note, no inbox summary →
    nag is correct."""
    from types import SimpleNamespace
    from pollypm.cockpit_ui import _existing_blocker_context

    data = SimpleNamespace(
        project_key="demo",
        task_buckets={
            "blocked": [
                {
                    "task_id": "demo/3",
                    "task_number": 3,
                    "summary": "Waiting on blocker.",
                    "blocker_explicit": False,  # description fallback only
                    "hold_reason": "",
                },
            ],
            "on_hold": [],
        },
        inbox_top=[],
    )
    assert _existing_blocker_context(data) is None


def test_existing_blocker_context_picks_on_hold_reason() -> None:
    """An on-hold task with a populated ``hold_reason`` is the bikepath
    case the user repro'd: the answer is on screen, suppress the nag."""
    from types import SimpleNamespace
    from pollypm.cockpit_ui import _existing_blocker_context

    data = SimpleNamespace(
        project_key="bikepath",
        task_buckets={
            "blocked": [],
            "on_hold": [
                {
                    "task_id": "bikepath/8",
                    "task_number": 8,
                    "hold_reason": "Waiting on operator: code review passed.",
                },
            ],
        },
        inbox_top=[],
    )
    ctx = _existing_blocker_context(data)
    assert ctx is not None
    assert ctx["kind"] == "on_hold"
    assert ctx["task_number"] == 8
    assert ctx["task_id"] == "bikepath/8"


def test_existing_blocker_context_picks_inbox_blocker_summary() -> None:
    """A project-level ``blocker_summary`` inbox item is the canonical
    artefact — pressing ``c`` should route there, not recompose."""
    from types import SimpleNamespace
    from pollypm.cockpit_ui import _existing_blocker_context

    data = SimpleNamespace(
        project_key="demo",
        task_buckets={"blocked": [], "on_hold": []},
        inbox_top=[
            {
                "task_id": "blocker-summary:42",
                "primary_ref": "demo/3",
                "source": "blocker_summary",
                "summary": "Plan needs Phase A approval.",
            },
        ],
    )
    ctx = _existing_blocker_context(data)
    assert ctx is not None
    assert ctx["kind"] == "blocker_summary"
    assert ctx["primary_ref"] == "demo/3"


def test_existing_blocker_context_picks_explicit_blocked_note() -> None:
    """A blocked task whose blocker text came from a real context entry
    (not a description fallback) counts as a summary."""
    from types import SimpleNamespace
    from pollypm.cockpit_ui import _existing_blocker_context

    data = SimpleNamespace(
        project_key="demo",
        task_buckets={
            "blocked": [
                {
                    "task_id": "demo/9",
                    "task_number": 9,
                    "summary": "Blocked: vendor API down — awaiting reply.",
                    "blocker_explicit": True,
                    "hold_reason": "",
                },
            ],
            "on_hold": [],
        },
        inbox_top=[],
    )
    ctx = _existing_blocker_context(data)
    assert ctx is not None
    assert ctx["kind"] == "blocked_note"
    assert ctx["task_number"] == 9


def test_inbox_remainder_suppresses_nag_when_on_hold_reason_exists(
    dashboard_app,
) -> None:
    """Bikepath repro: 6 blocked + 1 on hold with a populated reason.
    The Inbox panel should not claim "summary missing" — it should
    point the user at the on-hold task in Task pipeline below."""
    from types import SimpleNamespace

    fake_data = SimpleNamespace(
        project_key="bikepath",
        inbox_count=0,
        task_counts={"blocked": 6, "on_hold": 1},
        task_buckets={
            "blocked": [],
            "on_hold": [
                {
                    "task_id": "bikepath/8",
                    "task_number": 8,
                    "title": "Implement module 1",
                    "hold_reason": (
                        "Waiting on operator: code review passed, but pm "
                        "task approve cannot auto-merge because project "
                        "root has uncommitted .gitignore."
                    ),
                },
            ],
        },
        action_items=[],
        inbox_top=[],
    )
    body = dashboard_app._render_inbox_remainder(fake_data)
    assert "summary missing" not in body.lower()
    assert "Blocked" in body
    assert "task #8" in body


def test_inbox_remainder_suppresses_nag_when_blocker_summary_inbox_item() -> None:
    """A ``project.blocker_summary`` inbox item routes the user to it
    rather than firing the nag."""
    from types import SimpleNamespace
    from pollypm.cockpit_ui import PollyProjectDashboardApp

    # Use SimpleNamespace + the unbound method so we don't need a
    # mounted app instance.
    fake_data = SimpleNamespace(
        project_key="demo",
        inbox_count=1,
        task_counts={"blocked": 2, "on_hold": 0},
        task_buckets={"blocked": [], "on_hold": []},
        action_items=[],
        inbox_top=[
            {
                "task_id": "blocker-summary:42",
                "primary_ref": "demo/3",
                "source": "blocker_summary",
                "summary": "Plan needs Phase A approval.",
                "needs_action": False,
                "updated_at": "2026-05-01T17:00:00",
            },
        ],
    )

    class _FakeApp:
        project_key = "demo"
        _action_click_task_ids: list[str] = []

    body = PollyProjectDashboardApp._render_inbox_remainder(
        _FakeApp(), fake_data,
    )
    assert "summary missing" not in body.lower()
    assert "Polly's blocker summary is in the inbox below" in body


def test_inbox_remainder_keeps_nag_with_no_blocker_signal(
    dashboard_app,
) -> None:
    """When nothing carries a blocker note, the nag is still correct."""
    from types import SimpleNamespace

    fake_data = SimpleNamespace(
        project_key="demo",
        inbox_count=0,
        task_counts={"blocked": 1, "on_hold": 0},
        task_buckets={
            "blocked": [
                {
                    "task_id": "demo/3",
                    "task_number": 3,
                    "summary": "",
                    "blocker_explicit": False,
                    "hold_reason": "",
                },
            ],
            "on_hold": [],
        },
        action_items=[],
        inbox_top=[],
    )
    body = dashboard_app._render_inbox_remainder(fake_data)
    assert "summary missing" in body.lower()
    assert "ask the PM for a blocker summary" in body


def test_action_chat_pm_routes_to_existing_on_hold_task(
    dashboard_app,
) -> None:
    """#1015 concern #2: pressing ``c`` when an on-hold task already
    carries a populated reason should route to the task, not open a
    fresh PM chat that wastes tokens."""
    from types import SimpleNamespace

    dashboard_app.data = SimpleNamespace(
        project_key="bikepath",
        plan_path=object(),  # idle_project_needs_plan → False
        exists_on_disk=True,
        enforce_plan=True,
        task_counts={"blocked": 6, "on_hold": 1},
        task_buckets={
            "blocked": [],
            "on_hold": [
                {
                    "task_id": "bikepath/8",
                    "task_number": 8,
                    "hold_reason": "Waiting on operator: dirty project root.",
                },
            ],
        },
        inbox_top=[],
    )
    routed: list[str] = []
    dashboard_app._route_to_task = lambda task_id: routed.append(task_id)  # type: ignore[method-assign]
    notifications: list[str] = []
    dashboard_app.notify = lambda msg, **_kw: notifications.append(msg)  # type: ignore[method-assign]
    dispatched: list[tuple] = []
    dashboard_app._dispatch_to_pm_sync = lambda *a, **kw: dispatched.append((a, kw))  # type: ignore[method-assign]

    dashboard_app.action_chat_pm()

    assert routed == ["bikepath/8"], (
        "expected route to on-hold task, not PM dispatch"
    )
    assert dispatched == [], "PM should not be invoked when context exists"
    assert any("on-hold task" in msg for msg in notifications)


def test_action_chat_pm_falls_through_when_no_blocker_context(
    dashboard_app,
) -> None:
    """When nothing carries blocker context (or the project isn't even
    blocked), ``c`` falls through to the regular PM dispatch path."""
    from types import SimpleNamespace

    dashboard_app.data = SimpleNamespace(
        project_key="demo",
        plan_path=object(),
        exists_on_disk=True,
        enforce_plan=True,
        task_counts={"blocked": 0, "on_hold": 0},
        task_buckets={"blocked": [], "on_hold": []},
        inbox_top=[],
    )
    routed: list[str] = []
    dashboard_app._route_to_task = lambda task_id: routed.append(task_id)  # type: ignore[method-assign]

    fake_dispatch_calls: list[tuple] = []

    def fake_run_worker(fn, **_kw):
        # Execute synchronously so the test stays deterministic.
        fn()

    dashboard_app.run_worker = fake_run_worker  # type: ignore[method-assign]
    dashboard_app._dispatch_to_pm_sync = (  # type: ignore[method-assign]
        lambda *a, **kw: fake_dispatch_calls.append((a, kw))
    )

    dashboard_app.action_chat_pm()

    assert routed == [], "no blocker context → no task route"
    assert fake_dispatch_calls, "fall-through to PM dispatch expected"


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
            rendered = dashboard_app._inbox_section_text()
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


def test_inbox_remainder_no_contradiction_with_on_hold(dashboard_app) -> None:
    """#794: when the project has no inbox messages but does have
    on-hold tasks, the dashboard rendered both an "On hold" block
    and "No project inbox items are open." right after it, so the
    same panel said yes-and-no on adjacent lines. Suppress the
    no-items reassurance whenever the on-hold/blocked branch
    already rendered something.
    """
    from types import SimpleNamespace

    fake_data = SimpleNamespace(
        inbox_count=0,
        task_counts={"on_hold": 1},
        task_buckets={
            "on_hold": [
                {
                    "task_number": 1,
                    "title": "Library cleanup",
                    "summary": "Awaiting Phase A approval",
                    "steps": [],
                }
            ]
        },
        action_items=[],
        inbox_top=[],
    )
    rendered = dashboard_app._render_inbox_body(fake_data)
    assert "On hold" in rendered
    assert "No project inbox items are open" not in rendered


def test_banner_promotes_on_hold_over_active_worker(dashboard_app) -> None:
    """When a project has on_hold tasks AND an active worker, the banner
    leads with ``Paused`` instead of ``Moving now``.

    Sam's media project (2026-04-26) had a worker active in the
    background while task #1 was on_hold awaiting user Phase A
    approval. The pill correctly read "needs attention" but the
    banner contradicted that with "Moving now: worker_media is
    active · 1 on hold" — the actionable hold reason was buried as a
    tail count. Banner now matches pill priority.
    """
    from types import SimpleNamespace

    fake_data = SimpleNamespace(
        action_items=[],
        alert_count=0,
        active_worker={"role": "worker", "session_name": "worker_media"},
        task_counts={"on_hold": 1},
        task_buckets={"on_hold": []},
        inbox_count=0,
    )
    counts = "▸ 1 on hold"
    banner = dashboard_app._render_project_state_banner(fake_data, counts)
    assert banner.startswith("Paused"), (
        f"banner should lead with 'Paused', got: {banner!r}"
    )
    assert "Moving now" not in banner
    # The active worker info is preserved as background context.
    assert "worker_media" in banner
    assert "active in background" in banner
    # No "1 on hold" duplicate in the suffix — overlap stripped.
    assert banner.count("on hold") == 1


def test_banner_elevates_internal_hold_provisioning_failure(dashboard_app) -> None:
    from types import SimpleNamespace

    fake_data = SimpleNamespace(
        action_items=[],
        alert_count=0,
        active_worker={"session_name": "worker_media", "role": "worker"},
        task_counts={"on_hold": 1},
        task_buckets={
            "on_hold": [
                {
                    "task_number": 1,
                    "hold_reason": (
                        "Awaiting user Phase A approval. Heartbeat re-assigns "
                        "in a loop because tmux session 'pollypm-storage-closet' "
                        "(task-media-1) cannot be provisioned."
                    ),
                }
            ],
        },
        inbox_count=0,
    )

    banner = dashboard_app._render_project_state_banner(fake_data, "▸ 1 on hold")

    assert banner.startswith("Needs repair:")
    assert "task #1 worker pane could not be provisioned" in banner
    assert "pollypm-storage-closet" not in banner


def test_banner_paused_drops_overlap_when_only_on_hold(dashboard_app) -> None:
    """A pure on_hold project (no other categories) reads cleanly."""
    from types import SimpleNamespace

    fake_data = SimpleNamespace(
        action_items=[],
        alert_count=0,
        active_worker=None,
        task_counts={"on_hold": 2},
        task_buckets={"on_hold": []},
        inbox_count=0,
    )
    banner = dashboard_app._render_project_state_banner(fake_data, "▸ 2 on hold")
    assert banner == "Paused: 2 tasks are on hold"


def test_plan_body_with_enforce_plan_false_says_plan_not_required(dashboard_app) -> None:
    """When the project has ``enforce_plan = false`` and no plan file,
    the Plan section reads ``Plan not required`` instead of nudging the
    user to draft one. Sam's media project (2026-04-26) shipped the
    bypass but the dashboard kept showing ``Press c to ask the PM to
    plan it now`` — contradicting the operator's explicit choice.
    """
    from types import SimpleNamespace

    fake_data = SimpleNamespace(
        exists_on_disk=True,
        plan_path=None,
        plan_sections=[],
        plan_aux_files=[],
        plan_explainer=None,
        enforce_plan=False,
    )
    body = dashboard_app._render_plan_body(fake_data)
    assert "Plan not required" in body
    assert "enforce_plan = false" in body
    assert "ask the PM to plan it now" not in body


def test_plan_body_with_enforce_plan_true_uses_default_nudge(dashboard_app) -> None:
    """Default ``enforce_plan = true`` keeps the original draft-prompt copy."""
    from types import SimpleNamespace

    fake_data = SimpleNamespace(
        exists_on_disk=True,
        plan_path=None,
        plan_sections=[],
        plan_aux_files=[],
        plan_explainer=None,
        enforce_plan=True,
    )
    body = dashboard_app._render_plan_body(fake_data)
    # #920 — copy now spells out which pane to press c in.
    assert "in this pane" in body
    assert "ask for a plan" in body
    assert "Plan not required" not in body


def test_action_hint_uses_per_card_form_when_two_cards_visible() -> None:
    """Footer hint must match the live key bindings.

    With two action cards visible (polly_remote, live 2026-04-26),
    bindings 1-3 drive card 1 and 4-6 drive card 2. The original
    hint always read ``1 primary · 2 secondary · 3 reply`` — but
    pressing 2 actually fired the second card's primary action,
    not the first card's secondary. Mismatched help.
    """
    from pollypm.cockpit_ui import PollyProjectDashboardApp

    assert PollyProjectDashboardApp._ACTION_HINT == (
        "1 primary · 2 secondary · 3 reply · c chat · i inbox · q home"
    )
    assert PollyProjectDashboardApp._ACTION_HINT_TWO_CARDS == (
        "1-3 first card · 4-6 second card · c chat · i inbox · q home"
    )
    # Two distinct hints.
    assert (
        PollyProjectDashboardApp._ACTION_HINT
        != PollyProjectDashboardApp._ACTION_HINT_TWO_CARDS
    )


def test_action_card_steps_compact_when_two_cards_visible(dashboard_app) -> None:
    """When two action cards are visible, each one shrinks its step list.

    Sam's perf review (2026-04-26) flagged the dual-card stack as
    "too tall/noisy" — polly_remote routinely renders two cards with
    five setup steps each, blowing past a single screen. The renderer
    now caps steps at 2 per card with a ``+N more`` tail when more
    than one card is in view; a single solo card keeps the full
    five-step display so context isn't lost when there's only one
    thing to do.
    """
    long_steps = [
        "Step one setup task.",
        "Step two setup task.",
        "Step three setup task.",
        "Step four setup task.",
        "Step five setup task.",
    ]

    # One card visible — expect full 5 steps, no truncation tail.
    solo = dashboard_app._render_action_card_body(
        {
            "plain_prompt": "Do the thing.",
            "decision_question": "Approve?",
            "unblock_steps": long_steps,
            "steps_heading": "What you need to set up",
        },
        compact=False,
    )
    assert "[dim]5.[/dim] Step five setup task." in solo
    assert "more — click card" not in solo

    # Two cards visible — expect 2 steps + "+3 more" tail.
    compact = dashboard_app._render_action_card_body(
        {
            "plain_prompt": "Do the thing.",
            "decision_question": "Approve?",
            "unblock_steps": long_steps,
            "steps_heading": "What you need to set up",
        },
        compact=True,
    )
    assert "[dim]1.[/dim] Step one setup task." in compact
    assert "[dim]2.[/dim] Step two setup task." in compact
    assert "[dim]3.[/dim] Step three setup task." not in compact
    assert "(+3 more — click card to see all)" in compact
    # Decision still always renders.
    assert "[b]Decision:[/b] Approve?" in compact


def test_inbox_preview_splits_action_vs_info_items(dashboard_app) -> None:
    """Regression: when no Action Needed cards render but the inbox
    preview holds a mix of ``needs_action`` and informational items,
    the action items must come first and the informational items must
    sit under an explicit ``Other open items`` subhead.

    Before: both got listed under the ``2 need action`` count header,
    so a user reading the dashboard saw three rows under "2 need
    action" and had to mentally re-derive which two were actionable.
    After: action items are listed first, then ``Other open items``
    cleanly separates the FYI rows.
    """
    from types import SimpleNamespace

    fake_data = SimpleNamespace(
        inbox_count=2,
        task_counts={},
        task_buckets={},
        action_items=[],
        inbox_top=[
            {
                "task_id": "demo/1",
                "primary_ref": "demo/1",
                "title": "Approve scoped delivery",
                "updated_at": "",
                "triage_label": "needs unblock",
                "source": "task",
                "needs_action": True,
            },
            {
                "task_id": "demo/2",
                "primary_ref": "demo/2",
                "title": "Decide review feedback",
                "updated_at": "",
                "triage_label": "needs review",
                "source": "task",
                "needs_action": True,
            },
            {
                "task_id": "demo/3",
                "primary_ref": "demo/3",
                "title": "Architect status update",
                "updated_at": "",
                "triage_label": "fyi",
                "source": "message",
                "needs_action": False,
            },
        ],
    )
    rendered = dashboard_app._render_inbox_body(fake_data)
    assert "Other open items" in rendered
    head, _, tail = rendered.partition("Other open items")
    # Action-needed items appear above the subhead; FYI item below.
    assert "Approve scoped delivery" in head
    assert "Decide review feedback" in head
    assert "Architect status update" not in head
    assert "Architect status update" in tail


def test_action_response_controls_sit_under_their_own_card(
    dashboard_env, dashboard_app,
) -> None:
    """Issue #2 — when two action cards render, each card's response
    controls (Approve / Wait / Other) must sit directly under that
    card, not stack at the bottom of the inbox section.

    Asserts: (1) each visible card has its body Static and its
    control row in the same Vertical group; (2) groups mount in
    order so card 0's controls precede card 1's body in the DOM.
    """
    db_path = dashboard_env["project_path"] / ".pollypm" / "state.db"
    in_progress_id = dashboard_env["task_ids"]["in_progress"]
    queued_id = dashboard_env["task_ids"]["queued"]
    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        for task_id, subject in (
            (in_progress_id, "First decision needed"),
            (queued_id, "Second decision needed"),
        ):
            store.enqueue_message(
                type="notify",
                tier="immediate",
                recipient="user",
                sender="architect",
                subject=subject,
                body="Please choose.",
                scope="demo",
                labels=["project:demo"],
                payload={
                    "actor": "architect",
                    "project": "demo",
                    "task_id": task_id,
                    "user_prompt": {
                        "summary": f"{subject} — body",
                        "steps": ["Step one"],
                        "question": "Approve?",
                        "actions": [
                            {
                                "label": "Approve",
                                "kind": "approve_task",
                                "task_id": task_id,
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
        async with dashboard_app.run_test(size=(160, 70)) as pilot:
            await pilot.pause()
            assert dashboard_app.data is not None
            assert len(dashboard_app.data.action_items) >= 2

            # Both card groups visible; trailing inbox_body is the
            # remainder Static.
            for idx in range(2):
                group = dashboard_app.action_card_groups[idx]
                assert "-hidden" not in group.classes, (
                    f"action card group {idx} unexpectedly hidden"
                )
                # The control row must be a child of the same group
                # as the card body (interleaved layout, not pooled
                # at the bottom of the inbox section).
                row = dashboard_app.action_control_rows[idx]
                card = dashboard_app.action_card_bodies[idx]
                assert row.parent is group, (
                    f"control row {idx} parent is {row.parent!r}, "
                    f"expected group {group!r}"
                )
                assert card.parent is group, (
                    f"card body {idx} parent is {card.parent!r}, "
                    f"expected group {group!r}"
                )
                # And the row must be visible (-hidden cleared by
                # _sync_action_controls when the slot is filled).
                assert "-hidden" not in row.classes, (
                    f"control row {idx} unexpectedly hidden"
                )

            # Mount order: title → lead → group 0 → group 1 → inbox_body.
            inbox_section = dashboard_app.query_one("#proj-inbox-section")
            children = list(inbox_section.children)
            ordered_ids = [c.id for c in children]
            i0 = ordered_ids.index("proj-action-0-group")
            i1 = ordered_ids.index("proj-action-1-group")
            assert i0 < i1
            # The trailing inbox_body Static is mounted last in the
            # section. (No explicit id, so identify by widget identity.)
            assert children[-1] is dashboard_app.inbox_body

            # Each card body holds only its own card text (the other
            # card's body is in a different Static).
            card0 = str(dashboard_app.action_card_bodies[0].render())
            card1 = str(dashboard_app.action_card_bodies[1].render())
            assert "Decision" in card0
            assert "Decision" in card1
            # The "To move..." lead is in inbox_lead, not in the
            # card bodies.
            assert "To move this project forward" not in card0
            assert "To move this project forward" not in card1
            assert "To move this project forward" in str(
                dashboard_app.inbox_lead.render()
            )

    _run(body())


def test_inbox_section_hides_action_groups_when_no_action_items(
    dashboard_env, dashboard_app,
) -> None:
    """Without action items, the per-card groups + lead must be
    hidden so they don't reserve empty vertical space in the inbox
    section."""

    async def body() -> None:
        async with dashboard_app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            assert dashboard_app.data is not None
            assert not dashboard_app.data.action_items
            assert "-hidden" in dashboard_app.inbox_lead.classes
            for group in dashboard_app.action_card_groups:
                assert "-hidden" in group.classes

    _run(body())


def test_gather_project_dashboard_completes_under_budget(
    dashboard_env, monkeypatch,
) -> None:
    """Cold ``_gather_project_dashboard`` must finish under 500ms (#1290).

    Regression for #1290: the cockpit drilldown felt sluggish (3.6–8.0s)
    because ``_dashboard_active_worker`` called
    ``supervisor.plan_launches()``, which resolves provider profiles,
    scans the rules catalog, and writes session manifests for every
    enabled session — all to surface the worker name + role on the
    drilldown banner. Profiling pinned ~380ms of the 510ms budget on
    that one call. Now we enumerate ``config.sessions`` directly and
    skip the launch-plan pipeline entirely.

    The fixture is intentionally scaled: 30 dummy sessions are written
    to the config so the *old* code path would have done 30x the
    profile / catalog work. The new path skips that pipeline so the
    session count barely moves the needle. Threshold is 500ms — well
    under the 1000ms acceptance bar — so a regression that re-introduces
    plan_launches-style work shows up clearly.
    """
    import time as _time

    from pollypm.cockpit_ui import _gather_project_dashboard

    # Re-write the config with many enabled sessions on the project so
    # the worker-enumeration path has real work to do.
    project_path = dashboard_env["project_path"]
    config_path = dashboard_env["config_path"]
    sessions_block = "\n".join(
        f"[sessions.session_{i}]\n"
        f'role = "worker"\n'
        f'project = "demo"\n'
        f'provider = "claude"\n'
        f'account = "primary"\n'
        f'cwd = "{project_path}"\n'
        f"enabled = true\n"
        for i in range(30)
    )
    config_path.write_text(
        "[project]\n"
        f'tmux_session = "pollypm-test"\n'
        f'workspace_root = "{project_path.parent}"\n'
        "\n"
        "[accounts.primary]\n"
        'provider = "claude"\n'
        "\n"
        f'[projects.demo]\n'
        f'key = "demo"\n'
        f'name = "Demo"\n'
        f'path = "{project_path}"\n'
        "\n"
        f"{sessions_block}"
    )

    # Drop every dashboard cache so the call exercises the cold path.
    from pollypm import cockpit_ui as _cockpit_ui
    for cache_name in (
        "_PROJECT_DASHBOARD_TASK_CACHE",
        "_DASHBOARD_INBOX_CACHE",
        "_DASHBOARD_ACTIVITY_CACHE",
        "_PLAN_STALENESS_CACHE",
    ):
        cache = getattr(_cockpit_ui, cache_name, None)
        if isinstance(cache, dict):
            cache.clear()

    # Warm every module that the function imports lazily so the timed
    # call measures *work*, not first-import overhead.
    _gather_project_dashboard(config_path, "demo")
    for cache_name in (
        "_PROJECT_DASHBOARD_TASK_CACHE",
        "_DASHBOARD_INBOX_CACHE",
        "_DASHBOARD_ACTIVITY_CACHE",
        "_PLAN_STALENESS_CACHE",
    ):
        cache = getattr(_cockpit_ui, cache_name, None)
        if isinstance(cache, dict):
            cache.clear()

    t0 = _time.perf_counter()
    data = _gather_project_dashboard(config_path, "demo")
    elapsed_ms = (_time.perf_counter() - t0) * 1000

    assert data is not None
    assert elapsed_ms < 500.0, (
        f"_gather_project_dashboard took {elapsed_ms:.0f}ms with 30 "
        f"sessions configured — exceeds 500ms budget. Likely regression "
        f"of #1290 (worker enumeration re-running the launch pipeline)."
    )


# ---------------------------------------------------------------------------
# #1290 follow-up — zero-tasks empty-state affordance
# ---------------------------------------------------------------------------
#
# The savethenovel project landed in a state with zero ``work_tasks`` rows
# (registered, briefly had a task, then deleted). The drilldown showed a
# bare ``○`` glyph and no affordances — the operator watched it for 2.5+
# hours with nothing to act on. The empty-state panel must surface the
# moment the user navigates into a project with zero tasks, regardless of
# tracked-state, and must NOT appear when there are real tasks.


def _empty_project_env(tmp_path: Path):
    """Fresh project with the config wired up but zero ``work_tasks`` rows."""
    project_path = tmp_path / "savethenovel"
    project_path.mkdir()
    _init_git_repo(project_path)
    config_path = tmp_path / "pollypm.toml"
    _write_config(project_path, config_path)
    return {
        "config_path": config_path,
        "project_path": project_path,
    }


def test_empty_state_appears_when_zero_tasks(tmp_path: Path) -> None:
    """Zero ``work_tasks`` rows → drilldown surfaces the affordance copy
    naming both the planner and manual-create commands."""
    env = _empty_project_env(tmp_path)
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyProjectDashboardApp
    from pollypm import cockpit_ui as _cockpit_ui
    _cockpit_ui._PROJECT_DASHBOARD_TASK_CACHE.clear()
    app = PollyProjectDashboardApp(env["config_path"], "demo")

    async def body() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            assert app.data is not None
            # Sanity: the fixture really has zero tasks.
            total = sum(int(v or 0) for v in (app.data.task_counts or {}).values())
            assert total == 0
            # Empty-state panel must be visible (not -hidden) and contain
            # the affordance copy.
            assert "-hidden" not in app.empty_state.classes
            rendered = str(app.empty_state.render())
            assert "No tasks for" in rendered
            assert "pm project plan" in rendered
            assert "pm task create" in rendered
    _run(body())


def test_empty_state_hidden_when_tasks_exist(dashboard_env, dashboard_app) -> None:
    """Real tasks present → the empty-state panel must NOT render. The
    fixture's seeded queued/in_progress/done tasks should suppress it."""
    async def body() -> None:
        async with dashboard_app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            assert dashboard_app.data is not None
            total = sum(
                int(v or 0)
                for v in (dashboard_app.data.task_counts or {}).values()
            )
            assert total > 0
            assert "-hidden" in dashboard_app.empty_state.classes
            assert str(dashboard_app.empty_state.render()).strip() == ""
    _run(body())


def test_empty_state_renders_at_narrow_width(tmp_path: Path) -> None:
    """Narrow cockpit (80 cols) must still render the affordance — copy
    is short enough to fit on one wrapped line."""
    env = _empty_project_env(tmp_path)
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyProjectDashboardApp
    from pollypm import cockpit_ui as _cockpit_ui
    _cockpit_ui._PROJECT_DASHBOARD_TASK_CACHE.clear()
    app = PollyProjectDashboardApp(env["config_path"], "demo")

    async def body() -> None:
        async with app.run_test(size=(80, 50)) as pilot:
            await pilot.pause()
            assert "-hidden" not in app.empty_state.classes
            rendered = str(app.empty_state.render())
            assert "No tasks for" in rendered
            # The pipeline section must still render (no layout break)
            assert app.pipeline_body is not None
    _run(body())


def test_empty_state_pure_renderer_returns_affordance(tmp_path: Path) -> None:
    """Unit-level: ``_render_empty_state`` returns the affordance string
    when ``data.task_counts`` and ``data.task_buckets`` are both empty,
    and returns ``""`` when any task is present."""
    from pollypm.cockpit_ui import (
        PollyProjectDashboardApp,
        ProjectDashboardData,
    )

    base_kwargs = dict(
        project_key="savethenovel",
        project_name="Save The Novel",
        project_path=tmp_path,
        persona_name=None,
        pm_label="PM: polly",
        exists_on_disk=True,
        status_dot="○",
        status_color="#4a5568",
        status_label="idle",
        active_worker=None,
        architect=None,
        plan_path=None,
        plan_sections=[],
        plan_explainer=None,
        plan_text=None,
        plan_aux_files=[],
        plan_mtime=None,
        plan_stale_reason=None,
        activity_entries=[],
        inbox_count=0,
        inbox_top=[],
        action_items=[],
        alert_count=0,
    )

    empty = ProjectDashboardData(
        task_counts={},
        task_buckets={},
        **base_kwargs,
    )
    rendered_empty = PollyProjectDashboardApp._render_empty_state(
        None, empty,  # type: ignore[arg-type]
    )
    assert "No tasks for savethenovel" in rendered_empty
    assert "pm project plan savethenovel" in rendered_empty
    assert "pm task create" in rendered_empty

    populated = ProjectDashboardData(
        task_counts={"queued": 1},
        task_buckets={
            "queued": [{"task_id": "t1", "title": "Real task"}],
        },
        **base_kwargs,
    )
    assert PollyProjectDashboardApp._render_empty_state(
        None, populated,  # type: ignore[arg-type]
    ) == ""


def test_empty_state_suppressed_when_path_missing(tmp_path: Path) -> None:
    """When the project path does not exist on disk, the topbar already
    surfaces a louder warning — the empty-state panel should defer."""
    from pollypm.cockpit_ui import (
        PollyProjectDashboardApp,
        ProjectDashboardData,
    )

    data = ProjectDashboardData(
        project_key="missing",
        project_name="missing",
        project_path=None,
        persona_name=None,
        pm_label="PM: polly",
        exists_on_disk=False,
        status_dot="○",
        status_color="#4a5568",
        status_label="idle",
        active_worker=None,
        architect=None,
        task_counts={},
        task_buckets={},
        plan_path=None,
        plan_sections=[],
        plan_explainer=None,
        plan_text=None,
        plan_aux_files=[],
        plan_mtime=None,
        plan_stale_reason=None,
        activity_entries=[],
        inbox_count=0,
        inbox_top=[],
        action_items=[],
        alert_count=0,
    )
    assert PollyProjectDashboardApp._render_empty_state(
        None, data,  # type: ignore[arg-type]
    ) == ""


# ---------------------------------------------------------------------------
# Drafts-pending affordance — surface unpromoted drafts in the drilldown
# ---------------------------------------------------------------------------
#
# Forensic audit found 108 drafts stuck system-wide (oldest 16 days). The
# pipeline-bucket renderer historically showed queued / in_progress /
# review / blocked / on_hold / done — but NOT draft, so drafts piled up
# invisibly. PR #1340's empty-state only fires when total tasks == 0; a
# project with 1 cancelled + 1 draft (the savethenovel repro) has
# total>0 but no live work, so neither affordance fired and the only
# live task — the unpromoted draft — was hidden.


def _draft_data(
    tmp_path: Path,
    *,
    draft_count: int = 1,
    extra_counts: dict[str, int] | None = None,
    extra_buckets: dict[str, list[dict]] | None = None,
):
    """Build a ``ProjectDashboardData`` with N drafts and optional
    other-status mix. Keeps the unit tests away from the work-service
    bootstrap."""
    from pollypm.cockpit_ui import ProjectDashboardData

    counts: dict[str, int] = {"draft": draft_count}
    counts.update(extra_counts or {})
    buckets: dict[str, list[dict]] = {
        "draft": [
            {
                "task_id": f"savethenovel/{i}",
                "task_number": i,
                "title": f"Draft {i}",
                "updated_at": "",
            }
            for i in range(1, draft_count + 1)
        ]
    }
    buckets.update(extra_buckets or {})
    return ProjectDashboardData(
        project_key="savethenovel",
        project_name="Save The Novel",
        project_path=tmp_path,
        persona_name=None,
        pm_label="PM: polly",
        exists_on_disk=True,
        status_dot="○",
        status_color="#4a5568",
        status_label="idle",
        active_worker=None,
        architect=None,
        task_counts=counts,
        task_buckets=buckets,
        plan_path=None,
        plan_sections=[],
        plan_explainer=None,
        plan_text=None,
        plan_aux_files=[],
        plan_mtime=None,
        plan_stale_reason=None,
        activity_entries=[],
        inbox_count=0,
        inbox_top=[],
        action_items=[],
        alert_count=0,
    )


def test_drafts_pending_renderer_fires_when_only_drafts(tmp_path: Path) -> None:
    """Drafts present, no live work → ``_render_drafts_pending`` returns
    the affordance copy with the count, the queue command, and the
    project-qualified draft refs."""
    from pollypm.cockpit_ui import PollyProjectDashboardApp

    data = _draft_data(tmp_path, draft_count=3)
    rendered = PollyProjectDashboardApp._render_drafts_pending(
        None, data,  # type: ignore[arg-type]
    )
    assert "3 drafts pending" in rendered
    assert "pm task queue" in rendered
    # Refs use project/N form so the operator can copy-paste
    # ``pm task queue savethenovel/1``.
    assert "savethenovel/1" in rendered
    assert "savethenovel/2" in rendered
    assert "savethenovel/3" in rendered


def test_drafts_pending_renderer_singular_copy(tmp_path: Path) -> None:
    """Single draft → ``1 draft pending`` (singular noun)."""
    from pollypm.cockpit_ui import PollyProjectDashboardApp

    data = _draft_data(tmp_path, draft_count=1)
    rendered = PollyProjectDashboardApp._render_drafts_pending(
        None, data,  # type: ignore[arg-type]
    )
    assert "1 draft pending" in rendered
    assert "drafts pending" not in rendered


def test_drafts_pending_renderer_truncates_long_lists(tmp_path: Path) -> None:
    """More than three drafts → first three refs + ``(+N more)`` so the
    line stays readable on narrow terminals."""
    from pollypm.cockpit_ui import PollyProjectDashboardApp

    data = _draft_data(tmp_path, draft_count=7)
    rendered = PollyProjectDashboardApp._render_drafts_pending(
        None, data,  # type: ignore[arg-type]
    )
    assert "7 drafts pending" in rendered
    assert "savethenovel/1" in rendered
    assert "savethenovel/3" in rendered
    # Refs 4..7 collapse into "+N more"
    assert "+4 more" in rendered
    assert "savethenovel/7" not in rendered


def test_drafts_pending_renderer_silent_when_live_work_exists(
    tmp_path: Path,
) -> None:
    """Drafts coexist with live work (queued / in_progress / review) →
    the panel stays hidden so it doesn't shout. The pipeline-strip
    ``draft`` bucket carries the surface in that case."""
    from pollypm.cockpit_ui import PollyProjectDashboardApp

    for live_status in ("queued", "in_progress", "review"):
        data = _draft_data(
            tmp_path,
            draft_count=2,
            extra_counts={live_status: 1},
            extra_buckets={
                live_status: [
                    {
                        "task_id": f"savethenovel/9",
                        "task_number": 9,
                        "title": "Live work",
                        "updated_at": "",
                    }
                ],
            },
        )
        rendered = PollyProjectDashboardApp._render_drafts_pending(
            None, data,  # type: ignore[arg-type]
        )
        assert rendered == "", (
            f"drafts panel must hide when {live_status!r} task exists "
            f"(got {rendered!r})"
        )


def test_drafts_pending_renderer_silent_when_no_drafts(tmp_path: Path) -> None:
    """Zero drafts → no false affordance, even with terminal-state
    tasks (cancelled / done) on the project."""
    from pollypm.cockpit_ui import (
        PollyProjectDashboardApp,
        ProjectDashboardData,
    )

    data = ProjectDashboardData(
        project_key="savethenovel",
        project_name="Save The Novel",
        project_path=tmp_path,
        persona_name=None,
        pm_label="PM: polly",
        exists_on_disk=True,
        status_dot="○",
        status_color="#4a5568",
        status_label="idle",
        active_worker=None,
        architect=None,
        task_counts={"done": 1},
        task_buckets={
            "done": [
                {
                    "task_id": "savethenovel/1",
                    "task_number": 1,
                    "title": "Shipped",
                    "updated_at": "",
                }
            ]
        },
        plan_path=None,
        plan_sections=[],
        plan_explainer=None,
        plan_text=None,
        plan_aux_files=[],
        plan_mtime=None,
        plan_stale_reason=None,
        activity_entries=[],
        inbox_count=0,
        inbox_top=[],
        action_items=[],
        alert_count=0,
    )
    assert PollyProjectDashboardApp._render_drafts_pending(
        None, data,  # type: ignore[arg-type]
    ) == ""


def test_drafts_pending_renderer_silent_when_path_missing(
    tmp_path: Path,
) -> None:
    """Project path missing on disk → topbar surfaces a louder warning
    already; the drafts panel defers (matches empty-state behavior)."""
    from pollypm.cockpit_ui import (
        PollyProjectDashboardApp,
        ProjectDashboardData,
    )

    data = ProjectDashboardData(
        project_key="savethenovel",
        project_name="Save The Novel",
        project_path=None,
        persona_name=None,
        pm_label="PM: polly",
        exists_on_disk=False,
        status_dot="○",
        status_color="#4a5568",
        status_label="idle",
        active_worker=None,
        architect=None,
        task_counts={"draft": 5},
        task_buckets={
            "draft": [
                {
                    "task_id": "savethenovel/1",
                    "task_number": 1,
                    "title": "Draft 1",
                    "updated_at": "",
                }
            ]
        },
        plan_path=None,
        plan_sections=[],
        plan_explainer=None,
        plan_text=None,
        plan_aux_files=[],
        plan_mtime=None,
        plan_stale_reason=None,
        activity_entries=[],
        inbox_count=0,
        inbox_top=[],
        action_items=[],
        alert_count=0,
    )
    assert PollyProjectDashboardApp._render_drafts_pending(
        None, data,  # type: ignore[arg-type]
    ) == ""


def test_drafts_pending_does_not_block_empty_state_at_zero_total(
    tmp_path: Path,
) -> None:
    """Trigger conditions are disjoint: empty-state fires at total==0,
    drafts-pending fires at total>0 with draft>0 and live==0. Don't
    regress the empty-state's own trigger when drafts==0 too."""
    from pollypm.cockpit_ui import (
        PollyProjectDashboardApp,
        ProjectDashboardData,
    )

    zero = ProjectDashboardData(
        project_key="savethenovel",
        project_name="Save The Novel",
        project_path=tmp_path,
        persona_name=None,
        pm_label="PM: polly",
        exists_on_disk=True,
        status_dot="○",
        status_color="#4a5568",
        status_label="idle",
        active_worker=None,
        architect=None,
        task_counts={},
        task_buckets={},
        plan_path=None,
        plan_sections=[],
        plan_explainer=None,
        plan_text=None,
        plan_aux_files=[],
        plan_mtime=None,
        plan_stale_reason=None,
        activity_entries=[],
        inbox_count=0,
        inbox_top=[],
        action_items=[],
        alert_count=0,
    )
    assert PollyProjectDashboardApp._render_drafts_pending(
        None, zero,  # type: ignore[arg-type]
    ) == ""
    # Empty-state still fires at total==0 — guard the disjoint trigger.
    assert "No tasks for savethenovel" in (
        PollyProjectDashboardApp._render_empty_state(
            None, zero,  # type: ignore[arg-type]
        )
    )


def test_pipeline_strip_includes_draft_bucket(tmp_path: Path) -> None:
    """A project with only drafts must still render the pipeline strip
    with a ``draft`` glyph — the historical strip omitted it, which is
    why drafts piled up invisibly."""
    from pollypm.cockpit_ui import PollyProjectDashboardApp

    data = _draft_data(tmp_path, draft_count=2)
    rendered = PollyProjectDashboardApp._render_pipeline_body(
        None, data,  # type: ignore[arg-type]
    )
    assert "No tasks yet" not in rendered
    assert "draft" in rendered.lower()
    # Bucket header + at least one draft row by number
    assert "Draft" in rendered  # status header (.title())
    assert "#1" in rendered or "Draft 1" in rendered


def test_drafts_pending_real_db_only_drafts_no_live_work(tmp_path: Path) -> None:
    """Integration: a real work-DB with only a draft row → the
    gathered ``ProjectDashboardData`` carries the draft count and the
    drafts-pending renderer fires while the empty-state stays hidden.

    Bypasses the App pilot so this doesn't race the first-refresh
    worker thread; the renderer is still the one the App calls."""
    # ``_write_config`` writes ``[projects.demo]`` with the project
    # path pointing at the directory we create — keep the project
    # dir name aligned so the work-service stores tasks under the
    # canonical project alias the dashboard looks up.
    project_path = tmp_path / "demo"
    project_path.mkdir()
    _init_git_repo(project_path)
    config_path = tmp_path / "pollypm.toml"
    _write_config(project_path, config_path)

    if not _load_config_compatible(config_path):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")

    project_db = project_path / ".pollypm" / "state.db"
    project_db.parent.mkdir(parents=True, exist_ok=True)
    with SQLiteWorkService(db_path=project_db, project_path=project_path) as svc:
        svc.create(
            title="Unpromoted draft",
            description="seed",
            type="task",
            project="demo",
            flow_template="standard",
            roles={"worker": "worker", "reviewer": "reviewer"},
            priority="normal",
            created_by="polly",
        )
        # Note: do NOT call svc.queue() — the draft must stay in DRAFT.

    from pollypm import cockpit_ui as _cockpit_ui
    from pollypm.cockpit_ui import PollyProjectDashboardApp

    _cockpit_ui._PROJECT_DASHBOARD_TASK_CACHE.clear()
    data = _cockpit_ui._gather_project_dashboard(config_path, "demo")
    assert data is not None
    # Sanity: exactly one draft, no live work, total > 0 (empty-state
    # would have fired at total==0 instead).
    assert data.task_counts.get("draft") == 1
    assert not any(
        int(data.task_counts.get(s, 0))
        for s in ("queued", "in_progress", "review")
    )
    total = sum(int(v or 0) for v in (data.task_counts or {}).values())
    assert total > 0

    drafts_rendered = PollyProjectDashboardApp._render_drafts_pending(
        None, data,  # type: ignore[arg-type]
    )
    assert "1 draft pending" in drafts_rendered
    assert "pm task queue" in drafts_rendered

    # Empty-state defers because total > 0.
    empty_rendered = PollyProjectDashboardApp._render_empty_state(
        None, data,  # type: ignore[arg-type]
    )
    assert empty_rendered == ""

    # Pipeline body now contains the draft bucket too — verifying the
    # historical "draft omitted" gap is closed.
    pipeline_rendered = PollyProjectDashboardApp._render_pipeline_body(
        None, data,  # type: ignore[arg-type]
    )
    assert "No tasks yet" not in pipeline_rendered
    assert "draft" in pipeline_rendered.lower()


def test_drafts_pending_panel_mounts_in_compose(tmp_path: Path) -> None:
    """Confirm the drafts-pending widget participates in ``compose``
    (mounted under ``proj-body`` between empty-state and inbox-section)
    and starts hidden when the App is constructed."""
    from pollypm.cockpit_ui import PollyProjectDashboardApp

    project_path = tmp_path / "demo"
    project_path.mkdir()
    _init_git_repo(project_path)
    config_path = tmp_path / "pollypm.toml"
    _write_config(project_path, config_path)

    app = PollyProjectDashboardApp(config_path, "demo")
    assert app.drafts_pending is not None
    assert app.drafts_pending.id == "proj-drafts-pending"
    # Hidden by default (CSS class). The render path toggles it.
    assert "-hidden" in app.drafts_pending.classes
