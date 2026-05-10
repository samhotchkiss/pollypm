"""Regression tests for the rail rollup's DB resolution (#1542).

Background — pre-#1542 the cockpit rail's per-project rollup
(``CockpitRouter._project_tasks_for_rollup``) only inspected the
legacy per-project DB at ``<project>/.pollypm/state.db``. The dashboard
gather (``cockpit_ui._dashboard_task_db_paths``) had been migrated to
the canonical workspace-root DB first (#1004 / #1519), so a project
whose tasks live exclusively in the workspace-root DB rolled up to
``ProjectRailState.NONE`` on the rail, even though its dashboard
correctly painted ``◆ needs attention`` from the same data.

Witness symptom (#1542): ``media`` had a paused root task in the
workspace-root DB. The dashboard rendered ``◆ needs attention`` with
"Paused: 1 task is on hold". The rail rendered ``○ media`` (the same
quiet glyph as ``Home`` and ``Workers``) — so a user scanning the
rail looking for "what needs me right now" would skip past it. Rail
says quiet, dashboard says please-look-at-me.

These tests pin the contract: ``_project_tasks_for_rollup`` must
read tasks from *both* the canonical workspace-root DB and the
legacy per-project DB, mirroring the dashboard's resolution order,
so a paused-with-work project surfaces the correct ``◆`` glyph on
the rail.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from pollypm.cockpit_project_state import ProjectRailState
from pollypm.cockpit_rail import CockpitRouter
from pollypm.config import load_config
from pollypm.work.sqlite_service import SQLiteWorkService


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _init_git_repo(path: Path) -> None:
    """Init an empty git repo so the work-service auto-merge path is happy."""
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.test"], cwd=str(path), check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=str(path), check=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=str(path), check=True,
    )
    (path / ".gitignore").write_text(".pollypm/\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=str(path), check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=str(path), check=True,
    )


def _write_config(project_path: Path, config_path: Path) -> None:
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
        f"enforce_plan = false\n"
    )


def _seed_held_task_in_db(db_path: Path, project_path: Path) -> None:
    """Create one queued task and immediately put it on_hold."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with SQLiteWorkService(
        db_path=db_path, project_path=project_path,
    ) as svc:
        task = svc.create(
            title="Paused root task",
            description="Paused root that's blocking downstream work.",
            type="task",
            project="demo",
            flow_template="standard",
            roles={"worker": "pete", "reviewer": "russell"},
            priority="normal",
            created_by="polly",
        )
        svc.queue(task.task_id, "polly")
        svc.hold(task.task_id, "polly", "paused by PM")


def _make_router() -> CockpitRouter:
    """Construct a bare ``CockpitRouter`` without booting the cockpit.

    ``_project_tasks_for_rollup`` only reads ``project.path`` and the
    config — the rest of the router wiring is irrelevant to this
    contract, so we bypass ``__init__``.
    """
    return CockpitRouter.__new__(CockpitRouter)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_rollup_reads_workspace_root_db_when_per_project_db_missing(
    tmp_path: Path,
) -> None:
    """A project whose tasks live ONLY in the canonical workspace-root
    DB must still have its on_hold task picked up by the rail rollup,
    so the rail glyph reflects the dashboard's "needs attention" state.
    """
    project_path = tmp_path / "demo"
    project_path.mkdir()
    _init_git_repo(project_path)
    config_path = tmp_path / "pollypm.toml"
    _write_config(project_path, config_path)

    # Seed the held task in the workspace-root DB only — the legacy
    # per-project DB is intentionally absent.
    workspace_db = tmp_path / ".pollypm" / "state.db"
    _seed_held_task_in_db(workspace_db, tmp_path)

    config = load_config(config_path)
    project = config.projects["demo"]
    router = _make_router()
    tasks, _plan_blocked = router._project_tasks_for_rollup(
        "demo", project, config=config,
    )

    statuses = {
        getattr(getattr(t, "work_status", None), "value", None) for t in tasks
    }
    assert "on_hold" in statuses, (
        f"on_hold task in workspace-root DB must surface; got {statuses}"
    )


def test_rollup_yellow_state_for_paused_workspace_root_task(
    tmp_path: Path,
) -> None:
    """End-to-end: a paused task in the workspace-root DB must produce
    ``ProjectRailState.YELLOW`` from ``_project_state_rollups`` so the
    rail glyph for the project reads as ``◆ needs attention`` (matching
    the dashboard banner) instead of the quiet ``○``.
    """
    project_path = tmp_path / "demo"
    project_path.mkdir()
    _init_git_repo(project_path)
    config_path = tmp_path / "pollypm.toml"
    _write_config(project_path, config_path)

    workspace_db = tmp_path / ".pollypm" / "state.db"
    _seed_held_task_in_db(workspace_db, tmp_path)

    config = load_config(config_path)
    router = _make_router()
    rollups = router._project_state_rollups(config, alerts=[])

    rollup = rollups.get("demo")
    assert rollup is not None, (
        "expected a rollup for the registered ``demo`` project"
    )
    assert rollup.state is ProjectRailState.YELLOW, (
        "paused-with-work in the workspace-root DB must roll up to "
        f"YELLOW (◆ needs attention), got {rollup.state}"
    )


def test_rollup_still_reads_legacy_per_project_db_when_canonical_empty(
    tmp_path: Path,
) -> None:
    """Legacy fallback: if the workspace-root DB has no rows for the
    project but the legacy per-project DB does, the rollup must still
    pick those up. Regressing this would silently demote unmigrated
    projects to a quiet rail glyph.
    """
    project_path = tmp_path / "demo"
    project_path.mkdir()
    _init_git_repo(project_path)
    config_path = tmp_path / "pollypm.toml"
    _write_config(project_path, config_path)

    # Per-project legacy DB only — do not seed the workspace-root DB.
    legacy_db = project_path / ".pollypm" / "state.db"
    _seed_held_task_in_db(legacy_db, project_path)

    config = load_config(config_path)
    project = config.projects["demo"]
    router = _make_router()
    tasks, _plan_blocked = router._project_tasks_for_rollup(
        "demo", project, config=config,
    )

    statuses = {
        getattr(getattr(t, "work_status", None), "value", None) for t in tasks
    }
    assert "on_hold" in statuses, (
        f"on_hold task in legacy per-project DB must still surface; got {statuses}"
    )


def test_rollup_first_db_with_rows_wins_no_legacy_padding(
    tmp_path: Path,
) -> None:
    """Codex review of PR #1557 — when both DBs exist, the canonical
    workspace-root DB must "win" once it yields rows. The legacy
    per-project DB MUST NOT pad those rows with stale records.

    Pre-fix the rollup walked every candidate DB and unioned every
    matching row, reintroducing the same split-brain it claimed to
    repair: a clean canonical DB plus a stale legacy DB → wrong glyph.
    Mirrors ``_dashboard_gather_tasks``'s "first DB with matching rows
    wins" contract.
    """
    project_path = tmp_path / "demo"
    project_path.mkdir()
    _init_git_repo(project_path)
    config_path = tmp_path / "pollypm.toml"
    _write_config(project_path, config_path)

    # Canonical workspace-root DB has the live (queued) task.
    workspace_db = tmp_path / ".pollypm" / "state.db"
    workspace_db.parent.mkdir(parents=True, exist_ok=True)
    with SQLiteWorkService(
        db_path=workspace_db, project_path=tmp_path,
    ) as svc:
        live = svc.create(
            title="Active queued task",
            description="Live work in canonical DB.",
            type="task",
            project="demo",
            flow_template="standard",
            roles={"worker": "pete", "reviewer": "russell"},
            priority="normal",
            created_by="polly",
        )
        svc.queue(live.task_id, "polly")

    # Legacy per-project DB still on disk with a stale ``on_hold`` row.
    legacy_db = project_path / ".pollypm" / "state.db"
    _seed_held_task_in_db(legacy_db, project_path)

    config = load_config(config_path)
    project = config.projects["demo"]
    router = _make_router()
    tasks, _plan_blocked = router._project_tasks_for_rollup(
        "demo", project, config=config,
    )

    statuses = [
        getattr(getattr(t, "work_status", None), "value", None) for t in tasks
    ]
    titles = [getattr(t, "title", None) for t in tasks]

    # The canonical DB wins — only the live ``queued`` row surfaces.
    # The stale legacy ``on_hold`` row must be filtered out so the
    # rail glyph reflects canonical state, not legacy split-brain.
    assert "on_hold" not in statuses, (
        "stale on_hold row from legacy per-project DB must NOT pad "
        f"canonical rows; got statuses={statuses} titles={titles}"
    )
    assert "queued" in statuses, (
        f"expected the canonical queued task to surface; got {statuses}"
    )
    assert len(tasks) == 1, (
        f"first-DB-wins contract violated; expected 1 task, got {len(tasks)} "
        f"(statuses={statuses}, titles={titles})"
    )


def test_rollup_returns_empty_when_no_db_exists(tmp_path: Path) -> None:
    """A project with neither DB present yields no tasks (and falls
    through to ``ProjectRailState.NONE``) — same as before #1542.
    """
    project_path = tmp_path / "demo"
    project_path.mkdir()
    _init_git_repo(project_path)
    config_path = tmp_path / "pollypm.toml"
    _write_config(project_path, config_path)

    config = load_config(config_path)
    project = config.projects["demo"]
    router = _make_router()
    tasks, plan_blocked = router._project_tasks_for_rollup(
        "demo", project, config=config,
    )

    assert tasks == []
    assert plan_blocked is False
