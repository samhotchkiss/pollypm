from __future__ import annotations

from dataclasses import dataclass
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
from pollypm.storage.records import WorktreeRecord

if TYPE_CHECKING:
    from pollypm.models import PollyPMConfig

_SAFE_WORKTREE_KEY_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
ARCHITECT_WORKTREE_STATUS_PATH = Path(".pollypm") / "architect-worktree-status.md"


@dataclass(slots=True)
class ArchitectWorktreeRefreshResult:
    """Outcome of the architect worktree freshness check."""

    status: str
    worktree_path: Path
    base_ref: str | None = None
    base_head: str | None = None
    worktree_head: str | None = None
    marker_path: Path | None = None
    reason: str | None = None


def _list_worktrees_for_backend(
    config: "PollyPMConfig", project_key: str | None
) -> list[WorktreeRecord]:
    """List worktrees through the pg facade."""
    del config  # pg pool is process-wide
    from pollypm.storage.pg_worktrees import list_worktrees as pg_list

    return pg_list(project_key)


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
    """Upsert a worktree row through the pg facade."""
    del config  # pg pool is process-wide
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


def _update_worktree_status_for_backend(
    config: "PollyPMConfig",
    project_key: str,
    lane_kind: str,
    lane_key: str,
    status: str,
) -> None:
    """Update a worktree row's status through the pg facade."""
    del config  # pg pool is process-wide
    from pollypm.storage.pg_worktrees import (
        update_worktree_status as pg_update,
    )

    pg_update(project_key, lane_kind, lane_key, status)


def _validate_worktree_key(param_name: str, param_value: str) -> None:
    if not _SAFE_WORKTREE_KEY_RE.fullmatch(param_value):
        raise typer.BadParameter(f"{param_name} contains invalid characters: {param_value}")


def _git(
    cwd: Path,
    *args: str,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=False,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def _git_ref_exists(project_path: Path, ref: str) -> bool:
    result = _git(project_path, "rev-parse", "--verify", "--quiet", ref)
    return result.returncode == 0


def _architect_base_ref(project_path: Path) -> str:
    for ref in ("refs/heads/main", "refs/heads/master"):
        if _git_ref_exists(project_path, ref):
            return ref
    return "HEAD"


def _git_oid(path: Path, ref: str) -> str | None:
    result = _git(path, "rev-parse", "--verify", ref)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _git_worktree_registered(project_path: Path, worktree_path: Path) -> bool:
    result = _git(project_path, "worktree", "list", "--porcelain")
    if result.returncode != 0:
        return False
    try:
        target = worktree_path.resolve()
    except OSError:
        target = worktree_path
    for line in result.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        registered = Path(line[len("worktree "):].strip())
        try:
            registered = registered.resolve()
        except OSError:
            pass
        if registered == target:
            return True
    return False


def _base_is_ancestor(worktree_path: Path, base_head: str) -> bool:
    result = _git(
        worktree_path,
        "merge-base",
        "--is-ancestor",
        base_head,
        "HEAD",
    )
    return result.returncode == 0


def _worktree_dirty(worktree_path: Path) -> bool:
    result = _git(worktree_path, "status", "--porcelain")
    if result.returncode != 0:
        return True
    return bool(result.stdout.strip())


def _write_architect_stale_marker(
    *,
    worktree_path: Path,
    base_ref: str | None,
    base_head: str | None,
    worktree_head: str | None,
    reason: str,
) -> Path:
    marker_path = worktree_path / ARCHITECT_WORKTREE_STATUS_PATH
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(
        "\n".join(
            [
                "# Architect worktree freshness warning",
                "",
                "PollyPM could not fast-forward this architect worktree "
                "to the current project mainline without risking local work.",
                "",
                f"- Base ref checked: `{base_ref or 'unknown'}`",
                f"- Base HEAD: `{base_head or 'unknown'}`",
                f"- Worktree HEAD: `{worktree_head or 'unknown'}`",
                f"- Reason: {reason}",
                "",
                "Surface this before planning from repository state. Do not "
                "reset, delete, or discard work in this checkout unless the "
                "operator explicitly approves it.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return marker_path


def _clear_architect_stale_marker(worktree_path: Path) -> Path:
    marker_path = worktree_path / ARCHITECT_WORKTREE_STATUS_PATH
    marker_path.unlink(missing_ok=True)
    return marker_path


def refresh_architect_worktree(
    project_path: Path,
    worktree_path: Path,
) -> ArchitectWorktreeRefreshResult:
    """Fast-forward an architect worktree to local main when safe.

    This is intentionally conservative: it never resets, rebases,
    deletes, or force-checkouts. If the checkout is dirty or divergent,
    PollyPM writes a visible status file for the architect instead of
    mutating the worktree.
    """
    marker_path = _clear_architect_stale_marker(worktree_path)
    if not worktree_path.exists():
        return ArchitectWorktreeRefreshResult(
            status="missing",
            worktree_path=worktree_path,
            marker_path=marker_path,
            reason="worktree path does not exist",
        )
    if not (project_path / ".git").exists():
        return ArchitectWorktreeRefreshResult(
            status="not_git",
            worktree_path=worktree_path,
            marker_path=marker_path,
            reason="project path is not a git repository",
        )
    if not _git_worktree_registered(project_path, worktree_path):
        reason = "worktree is not registered with git"
        marker_path = _write_architect_stale_marker(
            worktree_path=worktree_path,
            base_ref=None,
            base_head=None,
            worktree_head=None,
            reason=reason,
        )
        return ArchitectWorktreeRefreshResult(
            status="stale",
            worktree_path=worktree_path,
            marker_path=marker_path,
            reason=reason,
        )

    base_ref = _architect_base_ref(project_path)
    base_head = _git_oid(project_path, base_ref)
    worktree_head = _git_oid(worktree_path, "HEAD")
    if base_head is None or worktree_head is None:
        reason = "could not resolve git HEADs for freshness check"
        marker_path = _write_architect_stale_marker(
            worktree_path=worktree_path,
            base_ref=base_ref,
            base_head=base_head,
            worktree_head=worktree_head,
            reason=reason,
        )
        return ArchitectWorktreeRefreshResult(
            status="stale",
            worktree_path=worktree_path,
            base_ref=base_ref,
            base_head=base_head,
            worktree_head=worktree_head,
            marker_path=marker_path,
            reason=reason,
        )

    if _base_is_ancestor(worktree_path, base_head):
        return ArchitectWorktreeRefreshResult(
            status="current",
            worktree_path=worktree_path,
            base_ref=base_ref,
            base_head=base_head,
            worktree_head=worktree_head,
            marker_path=marker_path,
        )

    if _worktree_dirty(worktree_path):
        reason = "worktree has uncommitted changes"
        marker_path = _write_architect_stale_marker(
            worktree_path=worktree_path,
            base_ref=base_ref,
            base_head=base_head,
            worktree_head=worktree_head,
            reason=reason,
        )
        return ArchitectWorktreeRefreshResult(
            status="stale",
            worktree_path=worktree_path,
            base_ref=base_ref,
            base_head=base_head,
            worktree_head=worktree_head,
            marker_path=marker_path,
            reason=reason,
        )

    result = _git(worktree_path, "merge", "--ff-only", base_ref, timeout=300)
    if result.returncode == 0:
        refreshed_head = _git_oid(worktree_path, "HEAD")
        if refreshed_head is not None and _base_is_ancestor(worktree_path, base_head):
            return ArchitectWorktreeRefreshResult(
                status="updated",
                worktree_path=worktree_path,
                base_ref=base_ref,
                base_head=base_head,
                worktree_head=refreshed_head,
                marker_path=marker_path,
            )

    reason = (
        result.stderr.strip()
        or result.stdout.strip()
        or "worktree branch cannot fast-forward to mainline"
    )
    marker_path = _write_architect_stale_marker(
        worktree_path=worktree_path,
        base_ref=base_ref,
        base_head=base_head,
        worktree_head=worktree_head,
        reason=reason,
    )
    return ArchitectWorktreeRefreshResult(
        status="stale",
        worktree_path=worktree_path,
        base_ref=base_ref,
        base_head=base_head,
        worktree_head=worktree_head,
        marker_path=marker_path,
        reason=reason,
    )


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
        if lane_kind == "architect":
            refresh_architect_worktree(project.path, Path(existing.path))
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
    if lane_kind == "architect":
        refresh_architect_worktree(project.path, path)
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
