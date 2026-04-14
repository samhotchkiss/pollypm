from __future__ import annotations

import subprocess
from pathlib import Path

import typer

from pollypm.config import load_config
from pollypm.projects import (
    ensure_project_scaffold,
    ensure_session_lock,
    project_worktrees_dir,
    release_session_lock,
    session_scoped_dir,
)
from pollypm.storage.state import StateStore, WorktreeRecord


def ensure_worktree(
    config_path: Path,
    *,
    project_key: str,
    lane_kind: str,
    lane_key: str,
    session_name: str | None = None,
    issue_key: str | None = None,
) -> WorktreeRecord | None:
    import re
    # Validate lane parameters to prevent path traversal
    for param_name, param_value in [("lane_kind", lane_kind), ("lane_key", lane_key)]:
        if not re.match(r"^[a-zA-Z0-9_-]+$", param_value):
            raise typer.BadParameter(f"{param_name} contains invalid characters: {param_value}")

    config = load_config(config_path)
    project = config.projects.get(project_key)
    if project is None:
        raise typer.BadParameter(f"Unknown project: {project_key}")

    ensure_project_scaffold(project.path)
    session_id = session_name or f"{lane_kind}-{lane_key}"
    worktree_root = session_scoped_dir(project_worktrees_dir(project.path), session_id)
    ensure_session_lock(worktree_root, session_id)
    if not (project.path / ".git").exists():
        return None

    store = StateStore(config.project.state_db)
    existing = _active_worktree(store, project_key, lane_kind, lane_key)
    if existing is not None and Path(existing.path).exists():
        return existing

    path = worktree_root / f"{project_key}-{lane_kind}-{lane_key}"
    branch = f"pollypm/{project_key}/{lane_kind}/{lane_key}"
    if not path.exists():
        result = subprocess.run(
            ["git", "-C", str(project.path), "worktree", "add", "-B", branch, str(path), "HEAD"],
            check=False,
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            # Release the session lock so future calls don't get blocked
            release_session_lock(worktree_root, session_id)
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "git worktree add failed")

    store.upsert_worktree(
        project_key=project_key,
        lane_kind=lane_kind,
        lane_key=lane_key,
        session_name=session_name,
        issue_key=issue_key,
        path=str(path),
        branch=branch,
        status="active",
    )
    return _active_worktree(store, project_key, lane_kind, lane_key)


def cleanup_worktree(
    config_path: Path,
    *,
    project_key: str,
    lane_kind: str,
    lane_key: str,
    force: bool = False,
) -> Path:
    config = load_config(config_path)
    project = config.projects.get(project_key)
    if project is None:
        raise typer.BadParameter(f"Unknown project: {project_key}")
    store = StateStore(config.project.state_db)
    record = _active_worktree(store, project_key, lane_kind, lane_key)
    if record is None:
        raise typer.BadParameter(f"No active worktree for {project_key}:{lane_kind}:{lane_key}")
    path = Path(record.path)
    if path.exists() and not force:
        status = subprocess.run(
            ["git", "-C", str(path), "status", "--short"],
            check=False,
            text=True,
            capture_output=True,
        )
        if status.stdout.strip():
            raise typer.BadParameter(f"Worktree {path} has uncommitted changes; use force to clean it up.")
    remove_cmd = ["git", "-C", str(project.path), "worktree", "remove", str(path)]
    if force:
        remove_cmd.append("--force")
    subprocess.run(remove_cmd, check=False, text=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(project.path), "worktree", "prune"],
        check=False,
        text=True,
        capture_output=True,
    )
    release_session_lock(path.parent, record.session_name)
    store.update_worktree_status(project_key, lane_kind, lane_key, "closed")
    return path


def list_worktrees(config_path: Path, project_key: str | None = None) -> list[WorktreeRecord]:
    config = load_config(config_path)
    store = StateStore(config.project.state_db)
    return store.list_worktrees(project_key)


def _active_worktree(store: StateStore, project_key: str, lane_kind: str, lane_key: str) -> WorktreeRecord | None:
    for item in store.list_worktrees(project_key):
        if item.lane_kind == lane_kind and item.lane_key == lane_key and item.status == "active":
            return item
    return None
