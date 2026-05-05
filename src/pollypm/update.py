"""``pm update`` — fetch origin/main, fast-forward local source, reinstall (#1079).

Closes the activation gap between "fix is on origin/main" and "fix is
running on the user's machine." The default user CLI surface (``pm up``,
``pm reset --force``, ``pm upgrade``) had no path that ran the
equivalent of ``git pull && uv tool install --reinstall``, so subagent-
shipped fixes piled up on origin without ever activating.

This module provides the smallest viable backbone:

* :func:`update` — the orchestrated flow (in_progress refusal → fetch →
  reset → reinstall) with an emit hook + subprocess seams for tests.
* :func:`count_in_progress_tasks` — best-effort count of tasks in
  ``in_progress`` state across the workspace; used to refuse a hard
  reset that would yank source out from under a live worker.
* :func:`pending_commits` — the ``origin/main`` vs ``HEAD`` SHA range
  and commit subjects, surfaced in both ``--check-only`` and the full
  flow.

The CLI shim lives in :mod:`pollypm.cli_features.update`. The cockpit
keystroke (call this from a button) is a deliberately separate follow-
up — this PR only ships the CLI surface.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


def _repo_root() -> Path:
    """Return the repo root containing ``pyproject.toml``.

    Walks up from this file's location, matching the strategy used by
    :func:`pollypm.doctor._pyproject_path`. Falls back to ``Path.cwd()``
    if no ``pyproject.toml`` is found upstream — callers that need a
    real git checkout should validate via :func:`is_git_checkout`.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    return Path.cwd()


def is_git_checkout(repo_root: Path) -> bool:
    """True iff ``repo_root`` is a git working tree (or a worktree)."""
    return (repo_root / ".git").exists()


def _is_claude_agent_worktree(repo_root: Path) -> bool:
    """True iff ``repo_root`` points at or inside a Claude agent worktree."""
    parts = repo_root.parts
    for index in range(len(parts) - 2):
        if (
            parts[index] == ".claude"
            and parts[index + 1] == "worktrees"
            and parts[index + 2].startswith("agent-")
        ):
            return True
    return False


@dataclass(slots=True)
class CommitInfo:
    sha: str
    subject: str


@dataclass(slots=True)
class PendingCommits:
    """Commits on ``origin/main`` that are ahead of ``HEAD``."""

    head_sha: str
    target_sha: str
    commits: list[CommitInfo] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.commits)

    @property
    def up_to_date(self) -> bool:
        return self.count == 0


@dataclass(slots=True)
class UpdateResult:
    ok: bool
    check_only: bool
    refused: bool
    old_sha: str
    new_sha: str
    commits: list[CommitInfo]
    message: str
    stderr: str = ""

    @property
    def count(self) -> int:
        return len(self.commits)


class _Step:
    """Tiny stdout-marker helper, mirrors ``upgrade.Step``."""

    def __init__(self, emit: Callable[[str], None]) -> None:
        self._emit = emit

    def __call__(self, text: str) -> None:
        self._emit(f"[step] {text}")


def _run_git(
    repo_root: Path,
    args: list[str],
    *,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    """Run a git command in ``repo_root`` and return the completed process."""
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _git_fetch(repo_root: Path) -> tuple[bool, str]:
    """Fetch origin. Returns ``(ok, stderr)``."""
    try:
        result = _run_git(repo_root, ["fetch", "origin"], timeout=120.0)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return (False, str(exc))
    if result.returncode != 0:
        return (False, result.stderr.strip() or result.stdout.strip())
    return (True, "")


def _git_rev_parse(repo_root: Path, ref: str) -> str:
    """Return the resolved SHA for ``ref``, or empty string on failure."""
    try:
        result = _run_git(repo_root, ["rev-parse", ref], timeout=10.0)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _git_log_range(repo_root: Path, base: str, head: str) -> list[CommitInfo]:
    """Return ``base..head`` commits as oldest-first ``CommitInfo`` rows.

    Empty list when ``base`` and ``head`` resolve to the same SHA, when
    git rejects the range (e.g. unrelated histories), or when either
    end can't be resolved. Best-effort — this drives the user-facing
    summary, never a control-flow decision.
    """
    if not base or not head or base == head:
        return []
    try:
        result = _run_git(
            repo_root,
            ["log", "--reverse", "--format=%H %s", f"{base}..{head}"],
            timeout=10.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    rows: list[CommitInfo] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        sha, _, subject = line.partition(" ")
        rows.append(CommitInfo(sha=sha, subject=subject))
    return rows


def pending_commits(repo_root: Path) -> PendingCommits:
    """Resolve ``origin/main..HEAD``-equivalent commit list.

    Returns a :class:`PendingCommits` describing the gap between local
    ``HEAD`` and ``origin/main``. ``head_sha`` / ``target_sha`` are
    populated even when the range is empty so callers can render
    "you are at <sha>".
    """
    head_sha = _git_rev_parse(repo_root, "HEAD")
    target_sha = _git_rev_parse(repo_root, "origin/main")
    commits = _git_log_range(repo_root, head_sha, target_sha)
    return PendingCommits(head_sha=head_sha, target_sha=target_sha, commits=commits)


def count_in_progress_tasks() -> int:
    """Return the number of work-service tasks currently ``in_progress``.

    Best-effort: a missing config / DB / service-import failure returns
    ``0`` (caller treats "couldn't tell" as "nothing to refuse over"
    — the reinstall itself is reversible, the SHA history is preserved
    in the dropped HEAD's reflog, so a false negative is recoverable.)
    A *false positive* would block a user who has nothing in flight,
    which is the worse failure mode.
    """
    try:
        from pollypm.config import DEFAULT_CONFIG_PATH
        from pollypm.work.db_resolver import resolve_work_db_path
        from pollypm.work.sqlite_service import SQLiteWorkService
    except Exception:  # noqa: BLE001
        return 0
    try:
        db_path = resolve_work_db_path()
    except Exception:  # noqa: BLE001
        return 0
    if not db_path.exists():
        return 0
    # Touch DEFAULT_CONFIG_PATH so a misconfigured install (config
    # missing entirely) returns 0 without raising — the resolver
    # already does the right thing, but the import-time import of
    # ``DEFAULT_CONFIG_PATH`` makes the dependency obvious for
    # readers and keeps the import-graph audit happy.
    _ = DEFAULT_CONFIG_PATH
    try:
        svc = SQLiteWorkService(db_path=db_path)
        tasks = svc.list_tasks(work_status="in_progress")
    except Exception:  # noqa: BLE001
        return 0
    return len(tasks)


def _reinstall_command(repo_root: Path) -> list[str]:
    """Return the argv that reinstalls PollyPM from ``repo_root``."""
    return ["uv", "tool", "install", "--reinstall", str(repo_root)]


def update(
    *,
    check_only: bool = False,
    repo_root: Path | None = None,
    emit: Callable[[str], None] | None = None,
    in_progress_count: int | None = None,
    fetcher: Callable[[Path], tuple[bool, str]] | None = None,
    resolver: Callable[[Path], PendingCommits] | None = None,
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
    reset_runner: Callable[[Path, str], tuple[bool, str]] | None = None,
) -> UpdateResult:
    """Fetch origin/main, fast-forward, and reinstall PollyPM.

    Steps:

    1. Refuse if work-service tasks are ``in_progress`` (override via
       the ``in_progress_count=0`` test seam).
    2. Verify ``repo_root`` is a real git checkout — if not, abort
       with a message pointing the user at ``pm upgrade`` (the
       package-manager-installed flow).
    3. ``git fetch origin``.
    4. Resolve ``origin/main`` vs ``HEAD``.
    5. If ``check_only``, report the gap and exit.
    6. ``git reset --hard origin/main``.
    7. ``uv tool install --reinstall <repo_root>``.

    All side-effecting steps go through small seams so tests can stub
    them without spawning real shells:

    * ``fetcher(repo) -> (ok, stderr)``
    * ``resolver(repo) -> PendingCommits``
    * ``runner(argv) -> CompletedProcess`` (used for the ``uv`` call)
    * ``reset_runner(repo, target) -> (ok, stderr)``
    """
    step = _Step(emit or print)
    repo = (repo_root or _repo_root()).resolve()
    do_fetch = fetcher or _git_fetch
    resolve = resolver or pending_commits
    do_run = runner or (
        lambda argv: subprocess.run(
            argv, check=False, capture_output=True, text=True, timeout=600.0,
        )
    )
    do_reset = reset_runner or (
        lambda r, target: _do_reset_hard(r, target)
    )

    if _is_claude_agent_worktree(repo):
        msg = (
            f"refusing to update: repo_root {repo} looks like a Claude "
            "agent worktree, not the canonical install path. Run "
            "`pm update` from the canonical install directory."
        )
        step(msg)
        return UpdateResult(
            ok=False,
            check_only=check_only,
            refused=True,
            old_sha="",
            new_sha="",
            commits=[],
            message=msg,
        )

    # Refuse on in_progress work — yanking source out from under a live
    # worker corrupts logs and confuses the supervisor. The user can
    # ``pm task hold`` / ``pm task done`` first.
    count = in_progress_count if in_progress_count is not None else count_in_progress_tasks()
    if count > 0:
        msg = (
            f"refusing to update: {count} task(s) in_progress. "
            "Finish, hold, or cancel them first "
            "(`pm task list --status in_progress`)."
        )
        step(msg)
        return UpdateResult(
            ok=False,
            check_only=check_only,
            refused=True,
            old_sha="",
            new_sha="",
            commits=[],
            message=msg,
        )

    if not is_git_checkout(repo):
        msg = (
            f"repo_root {repo} is not a git checkout. "
            "Use `pm upgrade` for package-manager installs."
        )
        step(msg)
        return UpdateResult(
            ok=False,
            check_only=check_only,
            refused=False,
            old_sha="",
            new_sha="",
            commits=[],
            message=msg,
        )

    step(f"repo_root={repo}")
    step("fetching origin")
    fetch_ok, fetch_err = do_fetch(repo)
    if not fetch_ok:
        msg = "git fetch failed — check network / remote auth"
        step(msg)
        return UpdateResult(
            ok=False,
            check_only=check_only,
            refused=False,
            old_sha="",
            new_sha="",
            commits=[],
            message=msg,
            stderr=fetch_err,
        )

    pending = resolve(repo)
    head_sha = pending.head_sha
    target_sha = pending.target_sha
    if not head_sha or not target_sha:
        msg = "could not resolve HEAD / origin/main — is the remote configured?"
        step(msg)
        return UpdateResult(
            ok=False,
            check_only=check_only,
            refused=False,
            old_sha=head_sha,
            new_sha=target_sha,
            commits=[],
            message=msg,
        )

    if pending.up_to_date:
        step(f"already up to date at {head_sha[:12]}")
        return UpdateResult(
            ok=True,
            check_only=check_only,
            refused=False,
            old_sha=head_sha,
            new_sha=head_sha,
            commits=[],
            message=f"already up to date at {head_sha[:12]}",
        )

    step(
        f"{pending.count} commit(s) to apply: "
        f"{head_sha[:12]} → {target_sha[:12]}"
    )

    if check_only:
        return UpdateResult(
            ok=True,
            check_only=True,
            refused=False,
            old_sha=head_sha,
            new_sha=target_sha,
            commits=list(pending.commits),
            message=(
                f"check-only: {pending.count} commit(s) behind "
                f"({head_sha[:12]} → {target_sha[:12]})"
            ),
        )

    step("git reset --hard origin/main")
    reset_ok, reset_err = do_reset(repo, "origin/main")
    if not reset_ok:
        msg = "git reset --hard failed"
        step(msg)
        return UpdateResult(
            ok=False,
            check_only=False,
            refused=False,
            old_sha=head_sha,
            new_sha=head_sha,
            commits=list(pending.commits),
            message=msg,
            stderr=reset_err,
        )

    step("uv tool install --reinstall <repo>")
    cmd = _reinstall_command(repo)
    try:
        result = do_run(cmd)
    except FileNotFoundError as exc:
        msg = f"uv binary not on PATH: {exc}"
        step(msg)
        return UpdateResult(
            ok=False,
            check_only=False,
            refused=False,
            old_sha=head_sha,
            new_sha=target_sha,
            commits=list(pending.commits),
            message=msg,
            stderr=str(exc),
        )
    except subprocess.TimeoutExpired:
        msg = "uv tool install timed out (>10m)"
        step(msg)
        return UpdateResult(
            ok=False,
            check_only=False,
            refused=False,
            old_sha=head_sha,
            new_sha=target_sha,
            commits=list(pending.commits),
            message=msg,
            stderr="timeout",
        )
    if result.returncode != 0:
        msg = f"uv tool install failed (exit {result.returncode})"
        step(msg)
        return UpdateResult(
            ok=False,
            check_only=False,
            refused=False,
            old_sha=head_sha,
            new_sha=target_sha,
            commits=list(pending.commits),
            message=msg,
            stderr=(result.stderr or "").strip(),
        )

    msg = (
        f"updated {head_sha[:12]} → {target_sha[:12]} "
        f"({pending.count} commit(s))"
    )
    step(msg)
    return UpdateResult(
        ok=True,
        check_only=False,
        refused=False,
        old_sha=head_sha,
        new_sha=target_sha,
        commits=list(pending.commits),
        message=msg,
    )


def _do_reset_hard(repo_root: Path, target: str) -> tuple[bool, str]:
    """Run ``git reset --hard <target>``. Returns ``(ok, stderr)``."""
    try:
        result = _run_git(
            repo_root, ["reset", "--hard", target], timeout=30.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return (False, str(exc))
    if result.returncode != 0:
        return (False, (result.stderr or result.stdout).strip())
    return (True, "")


__all__ = [
    "CommitInfo",
    "PendingCommits",
    "UpdateResult",
    "count_in_progress_tasks",
    "is_git_checkout",
    "pending_commits",
    "update",
]
