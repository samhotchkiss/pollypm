from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from pollypm.config import load_config
from pollypm.projects import (
    ensure_project_scaffold,
    ensure_session_lock,
    project_worktrees_dir,
    release_session_lock,
    session_scoped_dir,
)
from pollypm.storage._backend_dispatch import is_pg_backend
from pollypm.storage.records import WorktreeRecord

if TYPE_CHECKING:
    from pollypm.models import PollyPMConfig

_SAFE_WORKTREE_KEY_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _list_worktrees_for_backend(
    config: "PollyPMConfig", project_key: str | None
) -> list[WorktreeRecord]:
    """Backend-aware list helper.

    On the pg path we go straight through the new pg_worktrees facade
    (no per-process StateStore lifecycle); on the sqlite path we open
    a short-lived StateStore for back-compat. Both branches return the
    same :class:`WorktreeRecord` shape so callers don't have to care.
    """
    if is_pg_backend(config):
        from pollypm.storage.pg_worktrees import list_worktrees as pg_list

        return pg_list(project_key)
    from pollypm.storage.state import StateStore

    store = StateStore(config.project.state_db)
    try:
        return store.list_worktrees(project_key)
    finally:
        store.close()


def _upsert_worktree_for_backend(
    config: "PollyPMConfig",
    *,
    project_key: str,
    lane_kind: str,
    lane_key: str,
    session_name: str | None,
    issue_key: str | None,
    path: str,
    branch: str,
    status: str,
) -> None:
    """Backend-aware upsert wrapper. See :func:`_list_worktrees_for_backend`."""
    if is_pg_backend(config):
        from pollypm.storage.pg_worktrees import upsert_worktree as pg_upsert

        pg_upsert(
            project_key=project_key,
            lane_kind=lane_kind,
            lane_key=lane_key,
            session_name=session_name,
            issue_key=issue_key,
            path=path,
            branch=branch,
            status=status,
        )
        return
    from pollypm.storage.state import StateStore

    store = StateStore(config.project.state_db)
    try:
        store.upsert_worktree(
            project_key=project_key,
            lane_kind=lane_kind,
            lane_key=lane_key,
            session_name=session_name,
            issue_key=issue_key,
            path=path,
            branch=branch,
            status=status,
        )
    finally:
        store.close()


def _update_worktree_status_for_backend(
    config: "PollyPMConfig",
    project_key: str,
    lane_kind: str,
    lane_key: str,
    status: str,
) -> None:
    """Backend-aware status-promotion wrapper."""
    if is_pg_backend(config):
        from pollypm.storage.pg_worktrees import (
            update_worktree_status as pg_update,
        )

        pg_update(project_key, lane_kind, lane_key, status)
        return
    from pollypm.storage.state import StateStore

    store = StateStore(config.project.state_db)
    try:
        store.update_worktree_status(project_key, lane_kind, lane_key, status)
    finally:
        store.close()


def _validate_worktree_key(param_name: str, param_value: str) -> None:
    if not _SAFE_WORKTREE_KEY_RE.fullmatch(param_value):
        raise typer.BadParameter(f"{param_name} contains invalid characters: {param_value}")


def ensure_worktree(
    config_path: Path,
    *,
    project_key: str,
    lane_kind: str,
    lane_key: str,
    session_name: str | None = None,
    issue_key: str | None = None,
) -> WorktreeRecord | None:
    # Validate branch/path components before they reach git arguments.
    for param_name, param_value in [
        ("project_key", project_key),
        ("lane_kind", lane_kind),
        ("lane_key", lane_key),
    ]:
        _validate_worktree_key(param_name, param_value)

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

    existing = _active_worktree(config, project_key, lane_kind, lane_key)
    if existing is not None and Path(existing.path).exists():
        return existing

    path = worktree_root / f"{project_key}-{lane_kind}-{lane_key}"
    branch = f"pollypm/{project_key}/{lane_kind}/{lane_key}"
    if not path.exists():
        result = subprocess.run(
            ["git", "-C", str(project.path), "worktree", "add", "-B", branch, "--", str(path), "HEAD"],
            check=False,
            text=True,
            capture_output=True,
            timeout=300,
        )
        if result.returncode != 0:
            # Release the session lock so future calls don't get blocked
            release_session_lock(worktree_root, session_id)
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "git worktree add failed")

    _upsert_worktree_for_backend(
        config,
        project_key=project_key,
        lane_kind=lane_kind,
        lane_key=lane_key,
        session_name=session_name,
        issue_key=issue_key,
        path=str(path),
        branch=branch,
        status="active",
    )
    return _active_worktree(config, project_key, lane_kind, lane_key)


def cleanup_worktree(
    config_path: Path,
    *,
    project_key: str,
    lane_kind: str,
    lane_key: str,
    force: bool = False,
) -> Path:
    for param_name, param_value in [
        ("project_key", project_key),
        ("lane_kind", lane_kind),
        ("lane_key", lane_key),
    ]:
        _validate_worktree_key(param_name, param_value)

    config = load_config(config_path)
    project = config.projects.get(project_key)
    if project is None:
        raise typer.BadParameter(f"Unknown project: {project_key}")
    record = _active_worktree(config, project_key, lane_kind, lane_key)
    if record is None:
        raise typer.BadParameter(f"No active worktree for {project_key}:{lane_kind}:{lane_key}")
    path = Path(record.path)
    if path.exists() and not force:
        status = subprocess.run(
            ["git", "-C", str(path), "status", "--short"],
            check=False,
            text=True,
            capture_output=True,
            timeout=60,
        )
        if status.stdout.strip():
            raise typer.BadParameter(f"Worktree {path} has uncommitted changes; use force to clean it up.")
    remove_cmd = ["git", "-C", str(project.path), "worktree", "remove"]
    if force:
        remove_cmd.append("--force")
    remove_cmd.extend(["--", str(path)])
    subprocess.run(remove_cmd, check=False, text=True, capture_output=True, timeout=300)
    subprocess.run(
        ["git", "-C", str(project.path), "worktree", "prune"],
        check=False,
        text=True,
        capture_output=True,
        timeout=60,
    )
    release_session_lock(path.parent, record.session_name)
    _update_worktree_status_for_backend(config, project_key, lane_kind, lane_key, "closed")
    return path


def list_worktrees(config_path: Path, project_key: str | None = None) -> list[WorktreeRecord]:
    config = load_config(config_path)
    return _list_worktrees_for_backend(config, project_key)


def _active_worktree(
    config: "PollyPMConfig", project_key: str, lane_kind: str, lane_key: str
) -> WorktreeRecord | None:
    for item in _list_worktrees_for_backend(config, project_key):
        if item.lane_kind == lane_kind and item.lane_key == lane_key and item.status == "active":
            return item
    return None
