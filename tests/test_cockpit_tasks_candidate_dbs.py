"""Regression tests for ``PollyTasksApp._candidate_dbs`` ordering (#1004).

Background — the work-DB resolver collapsed onto the workspace-root
``state.db`` as the single source of truth. The cockpit Tasks pane
still tolerates a per-project ``<project>/.pollypm/state.db`` as a
legacy fallback (some unmigrated projects only have rows there), but
when both DBs exist with rows the workspace must win — otherwise
``_get_svc`` latches onto the stale per-project DB and the live-task
drilldown shows split-brain results vs. the running worker session
("No active worker session is currently attached to this task." while
the heartbeat is plainly attached to a tmux pane).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def _write_config(project_path: Path, config_path: Path, *, workspace_root: Path) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "[project]\n"
        f'tmux_session = "pollypm-test"\n'
        f'workspace_root = "{workspace_root}"\n'
        "\n"
        "[projects.demo]\n"
        'key = "demo"\n'
        'name = "Demo"\n'
        f'path = "{project_path}"\n'
    )


def _create_empty_state_db(db_path: Path) -> None:
    """Touch a SQLite file at ``db_path`` so ``Path.exists()`` is True.

    ``_candidate_dbs`` filters with ``candidate.exists()`` — we don't
    need a real schema to exercise the ordering logic, just a real
    file on disk.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.close()


def test_candidate_dbs_prefers_workspace_when_both_exist(tmp_path: Path) -> None:
    """Workspace-root DB must come first when both DBs exist.

    Pre-#1004 ordering returned the per-project DB first, so
    ``_get_svc`` would short-circuit on the stale per-project rows
    even when the live workspace DB had the in-progress task and its
    work_sessions row.
    """
    from pollypm.cockpit_tasks import PollyTasksApp

    workspace_root = tmp_path
    project_path = workspace_root / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    config_path = workspace_root / "pollypm.toml"
    _write_config(project_path, config_path, workspace_root=workspace_root)

    workspace_db = workspace_root / ".pollypm" / "state.db"
    per_project_db = project_path / ".pollypm" / "state.db"
    _create_empty_state_db(workspace_db)
    _create_empty_state_db(per_project_db)

    app = PollyTasksApp(config_path, "demo")
    candidates = app._candidate_dbs()
    paths = [db for db, _ in candidates]

    assert workspace_db in paths
    assert per_project_db in paths
    assert paths.index(workspace_db) < paths.index(per_project_db), (
        f"workspace DB must precede per-project DB; got order: {paths}"
    )


def test_candidate_dbs_falls_back_to_per_project_when_workspace_missing(
    tmp_path: Path,
) -> None:
    """Legacy fallback: if only the per-project DB exists, return it.

    Some unmigrated projects still have rows only in
    ``<project>/.pollypm/state.db``; we must not regress those.
    """
    from pollypm.cockpit_tasks import PollyTasksApp

    workspace_root = tmp_path
    project_path = workspace_root / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    config_path = workspace_root / "pollypm.toml"
    _write_config(project_path, config_path, workspace_root=workspace_root)

    per_project_db = project_path / ".pollypm" / "state.db"
    _create_empty_state_db(per_project_db)
    # Deliberately do NOT create the workspace_root state.db.

    app = PollyTasksApp(config_path, "demo")
    candidates = app._candidate_dbs()
    paths = [db for db, _ in candidates]

    assert per_project_db in paths, (
        f"per-project DB must remain a legacy fallback; got: {paths}"
    )
