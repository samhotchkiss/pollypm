"""Real change-detection for the advisor tick.

Replaces the ad01 stub. Given a project path and a ``since`` cutoff,
returns a :class:`ChangeReport` summarizing:

* ``commit_shas`` — commit SHAs authored in the project since ``since``.
* ``changed_files`` — files touched by those commits (union of the
  per-commit name-only diffs).
* ``task_transitions`` — work-service transitions in the project's
  ``work_transitions`` table since ``since``.
* ``files_diff_summary`` — short machine-readable summary ("N commits,
  M files changed") for the tick-handler log; the heavyweight per-file
  diff text is packed by ad03's assess.py when a session is launched.

The report is intentionally small — ad02 does detection, not diff text.
Per-file diff text ingest is ad03's ``assess.py`` where the context
pack gets built.

The ``has_changes`` flag lights up when either ≥1 commit OR ≥1 task
transition is visible in the window. The advisor tick short-circuits
on ``has_changes == False`` — the whole project contributes a single
``no-changes`` row and no session is scheduled.

Cache: per-tick cache keyed by ``(project_path, since_iso)``. The tick
handler runs once every 30 minutes; repeated calls for the same project
inside a tick (as defensive re-reads by downstream ad03 helpers) use
the cached report instead of re-shelling out to git.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


DEFAULT_GIT_TIMEOUT = 10.0


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class TaskTransitionRecord:
    """One ``work_transitions`` row for the advisor's delta window."""

    project: str
    task_number: int
    task_title: str
    from_state: str
    to_state: str
    actor: str
    timestamp: str  # ISO-8601 UTC (as stored by the work service)

    @property
    def task_id(self) -> str:
        return f"{self.project}/{self.task_number}"


@dataclass(slots=True)
class ChangeReport:
    """Summary of changes for a project in a given window."""

    project_path: Path
    since: datetime | None
    commit_shas: list[str] = field(default_factory=list)
    changed_files: list[Path] = field(default_factory=list)
    task_transitions: list[TaskTransitionRecord] = field(default_factory=list)
    files_diff_summary: str = ""

    @property
    def has_changes(self) -> bool:
        return bool(self.commit_shas) or bool(self.task_transitions)


# ---------------------------------------------------------------------------
# Per-tick cache
# ---------------------------------------------------------------------------


_TICK_CACHE: dict[tuple[str, str], ChangeReport] = {}


def clear_cache() -> None:
    """Drop the per-tick cache. The tick handler calls this at tick start."""
    _TICK_CACHE.clear()


def _cache_key(project_path: Path, since: datetime | None) -> tuple[str, str]:
    return (str(Path(project_path).resolve()), since.isoformat() if since else "")


# ---------------------------------------------------------------------------
# Git log + diff
# ---------------------------------------------------------------------------


def _run_git(
    project_path: Path,
    args: list[str],
    *,
    timeout: float = DEFAULT_GIT_TIMEOUT,
) -> tuple[int, str]:
    """Run ``git -C <project_path> <args...>``; return (code, stdout).

    Returns ``(1, "")`` on any subprocess failure so callers can treat
    errors identically to "no output." The advisor tick must never
    crash over a stale repo or a missing ``git`` binary.
    """
    cmd = ["git", "-C", str(project_path), *args]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("advisor: git %s failed in %s: %s", args, project_path, exc)
        return 1, ""
    return result.returncode, result.stdout


def _gather_commits(
    project_path: Path,
    since: datetime | None,
    *,
    timeout: float = DEFAULT_GIT_TIMEOUT,
) -> list[str]:
    """Return commit SHAs authored after ``since``.

    When ``since`` is None (first run for the project), we look back at
    most one day so the first tick doesn't flag a week's worth of
    history — the advisor should only ever review "recent" activity.
    """
    git_dir = project_path / ".git"
    if not git_dir.exists():
        return []

    if since is None:
        # First-run bootstrap window: anything within the last 24 hours.
        since_arg = "--since=24.hours.ago"
    else:
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        since_iso = since.strftime("%Y-%m-%dT%H:%M:%S%z")
        since_arg = f"--since={since_iso}"

    code, out = _run_git(
        project_path,
        ["log", since_arg, "--pretty=format:%H", "--no-merges"],
        timeout=timeout,
    )
    if code != 0 or not out.strip():
        return []
    shas = [line.strip() for line in out.splitlines() if line.strip()]
    # ``git log`` lists newest first; reverse to chronological order so
    # downstream consumers see the progression as it happened.
    shas.reverse()
    return shas


def _gather_changed_files(
    project_path: Path,
    commit_shas: list[str],
    *,
    timeout: float = DEFAULT_GIT_TIMEOUT,
) -> list[Path]:
    """Return the union of files touched by ``commit_shas``.

    We use ``git diff <earliest>^..HEAD --name-only`` per the spec. The
    ``^`` on the earliest commit ensures we include the changes
    *introduced by* that commit, not just everything after it.
    Orphan-commit repos (no parent for the earliest) fall back to
    collecting per-commit name-only diffs.
    """
    if not commit_shas:
        return []

    earliest = commit_shas[0]

    # Try the fast path first.
    code, out = _run_git(
        project_path,
        ["diff", f"{earliest}^..HEAD", "--name-only"],
        timeout=timeout,
    )
    if code == 0 and out.strip():
        paths = _parse_unique_paths(out)
        return paths

    # Fallback: per-commit name-only diffs (handles orphan / first commit).
    seen: dict[str, Path] = {}
    for sha in commit_shas:
        code, out = _run_git(
            project_path,
            ["show", sha, "--name-only", "--pretty=format:"],
            timeout=timeout,
        )
        if code != 0:
            continue
        for p in _parse_unique_paths(out):
            seen.setdefault(str(p), p)
    return list(seen.values())


def _parse_unique_paths(raw: str) -> list[Path]:
    seen: dict[str, Path] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        seen.setdefault(line, Path(line))
    return list(seen.values())


# ---------------------------------------------------------------------------
# Work-service transitions.
# ---------------------------------------------------------------------------


def _transitions_db_path(project_path: Path) -> Path | None:
    """Resolve the read-only work-service DB path for a project.

    Routes through :func:`pollypm.work.db_resolver.resolve_work_db_path`
    so the advisor reads the same canonical workspace-root state.db that
    every other engine writes to (post-#1004 layout). The deprecated
    per-project DB is no longer probed — see the advisor.tick spam fix
    note in :mod:`pollypm.plugins_builtin.advisor.handlers.advisor_tick`.
    Returns ``None`` if the resolved path doesn't exist on disk so
    callers can treat that as "no transitions this window."
    """
    from pollypm.work.db_resolver import resolve_work_db_path

    try:
        db_path = resolve_work_db_path()
    except Exception:  # noqa: BLE001
        return None
    if db_path is None or not db_path.exists():
        return None
    return db_path


def _gather_task_transitions(
    project_path: Path,
    project_key: str,
    since: datetime | None,
    *,
    work_service: Any | None = None,
) -> list[TaskTransitionRecord]:
    """Return transitions for ``project_key`` after ``since``.

    Prefers a caller-supplied work-service handle (tests inject an
    in-memory fake). Falls back to a read-only sqlite open of the
    project's ``.pollypm/state.db`` through the storage query facade so
    the advisor doesn't own raw SQLite connection details.
    """
    since_iso = since.isoformat() if since else _default_since_iso()

    # Test-injection path: work_service supplies transitions directly.
    if work_service is not None:
        getter = getattr(work_service, "list_transitions", None)
        if callable(getter):
            try:
                rows = getter(project=project_key, since=since_iso)
            except Exception as exc:  # noqa: BLE001
                logger.debug("advisor: work_service.list_transitions failed: %s", exc)
                rows = []
            return list(rows)

    db_path = _transitions_db_path(project_path)
    if db_path is None or not db_path.exists():
        return []

    from pollypm.storage.work_transition_queries import advisor_transition_rows

    rows = advisor_transition_rows(
        db_path,
        project_key=project_key,
        since_iso=since_iso,
    )

    out: list[TaskTransitionRecord] = []
    for r in rows:
        try:
            num = int(r["task_number"])
        except (ValueError, TypeError):
            continue
        out.append(
            TaskTransitionRecord(
                project=str(r["project"] or project_key),
                task_number=num,
                task_title=str(r["title"] or ""),
                from_state=str(r["from_state"] or ""),
                to_state=str(r["to_state"] or ""),
                actor=str(r["actor"] or ""),
                timestamp=str(r["created_at"] or ""),
            )
        )
    return out


def _default_since_iso() -> str:
    """Lookback ceiling when the advisor has no persisted last_run.

    24 hours matches the git lookback so the two signals agree about
    what counts as "recent" on a first-ever run.
    """
    from datetime import timedelta
    since = datetime.now(UTC) - timedelta(hours=24)
    return since.isoformat()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_changes(
    project_path: Path,
    since: datetime | None,
    *,
    project_key: str | None = None,
    work_service: Any | None = None,
    git_timeout: float = DEFAULT_GIT_TIMEOUT,
) -> ChangeReport:
    """Build a :class:`ChangeReport` for a project since ``since``.

    ``project_key`` defaults to the basename of ``project_path`` when
    not supplied — the advisor tick always passes it explicitly, but
    the default keeps stand-alone testing cheap.

    Results are cached per-tick keyed by ``(project_path, since)`` so
    a tick that makes multiple passes over the same project (e.g.
    ad03's assess.py re-reading the report) doesn't re-shell-out.
    """
    project_path = Path(project_path)
    key_name = project_key or project_path.name

    cache_key = _cache_key(project_path, since)
    cached = _TICK_CACHE.get(cache_key)
    if cached is not None:
        return cached

    commit_shas = _gather_commits(project_path, since, timeout=git_timeout)
    changed_files = _gather_changed_files(project_path, commit_shas, timeout=git_timeout)
    transitions = _gather_task_transitions(
        project_path, key_name, since, work_service=work_service,
    )

    n_commits = len(commit_shas)
    n_files = len(changed_files)
    n_trans = len(transitions)
    commit_word = "commit" if n_commits == 1 else "commits"
    file_word = "file" if n_files == 1 else "files"
    trans_word = "task transition" if n_trans == 1 else "task transitions"
    summary = (
        f"{n_commits} {commit_word}, {n_files} {file_word} changed, "
        f"{n_trans} {trans_word}"
    )

    report = ChangeReport(
        project_path=project_path,
        since=since,
        commit_shas=commit_shas,
        changed_files=changed_files,
        task_transitions=transitions,
        files_diff_summary=summary,
    )
    _TICK_CACHE[cache_key] = report
    return report
