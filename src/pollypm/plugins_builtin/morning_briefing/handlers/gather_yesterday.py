"""Gather yesterday's cross-project activity for the morning briefing.

Returns a :class:`YesterdaySnapshot` summarizing:

* ``commits_by_project`` — per-project git-log headlines for the local
  calendar day preceding ``now_local``.
* ``task_transitions`` — work-service state transitions that occurred in
  the same window.
* ``advisor_insights`` — entries from ``.pollypm-state/advisor-log.jsonl``
  emitted in the window (only rows with ``emit=true``).
* ``downtime_artifacts`` — downtime tasks that reached
  ``awaiting_approval`` yesterday.

The helper is hot-path daily code: it opens each project's SQLite work
database read-only, runs one SELECT per project (no per-task round
trips), and treats every failure as a silent skip — a broken project
must not kill the briefing.

DST: every time is computed as a timezone-aware datetime, then
converted to UTC ISO before comparison with DB rows (which are stored
in UTC isoformat). This keeps the window correct across spring-forward
and fall-back days.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import date as _date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pollypm.models import KnownProject


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CommitInfo:
    """A single commit headline from ``git log``."""

    sha: str
    timestamp: str   # ISO-8601 with tz (committer date)
    author: str
    subject: str


@dataclass(slots=True)
class TransitionRecord:
    """A ``work_transitions`` row."""

    project: str
    task_id: str            # ``project/task_number`` canonical form
    task_title: str
    from_state: str
    to_state: str
    actor: str
    timestamp: str          # UTC ISO-8601


@dataclass(slots=True)
class AdvisorInsightSummary:
    """A single emitted entry from the advisor log."""

    timestamp: str          # UTC ISO-8601
    project: str
    kind: str
    title: str
    body: str


@dataclass(slots=True)
class DowntimeArtifactSummary:
    """A downtime task that reached awaiting_approval yesterday."""

    project: str
    task_id: str
    title: str
    reached_at: str         # UTC ISO-8601


@dataclass(slots=True)
class YesterdaySnapshot:
    """Aggregated yesterday-window data across all tracked projects."""

    date_local: str                                               # YYYY-MM-DD
    window_start_utc: str
    window_end_utc: str
    commits_by_project: dict[str, list[CommitInfo]] = field(default_factory=dict)
    task_transitions: list[TransitionRecord] = field(default_factory=list)
    advisor_insights: list[AdvisorInsightSummary] = field(default_factory=list)
    downtime_artifacts: list[DowntimeArtifactSummary] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return (
            not any(self.commits_by_project.values())
            and not self.task_transitions
            and not self.advisor_insights
            and not self.downtime_artifacts
        )

    def total_commits(self) -> int:
        return sum(len(lst) for lst in self.commits_by_project.values())


# ---------------------------------------------------------------------------
# Window calculation
# ---------------------------------------------------------------------------


def yesterday_window(now_local: datetime) -> tuple[datetime, datetime]:
    """Return (start, end) of the calendar day preceding ``now_local``.

    Boundaries are tz-aware datetimes in ``now_local.tzinfo`` (so DST is
    honoured). ``end`` is exclusive — the next local midnight after the
    yesterday date. Both are suitable for conversion to UTC.
    """
    if now_local.tzinfo is None:
        raise ValueError("now_local must be timezone-aware")
    today_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start = today_local - timedelta(days=1)
    end = today_local
    return start, end


def _to_utc_iso(dt: datetime) -> str:
    """Convert a tz-aware datetime to ISO-8601 UTC (naive form for DB cmp)."""
    return dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None).isoformat()


# ---------------------------------------------------------------------------
# Project iteration
# ---------------------------------------------------------------------------


def iter_tracked_projects(config) -> list[KnownProject]:
    """Return projects eligible for a briefing snapshot.

    A project is eligible when:

    * Its ``tracked`` flag is true, OR
    * The project is the ambient ``config.project`` (so a single-project
      install without explicit ``[projects.*]`` entries still gets a
      briefing).

    Projects with missing on-disk ``path`` directories are filtered out.
    """
    projects: list[KnownProject] = []
    seen_paths: set[Path] = set()
    for known in config.projects.values():
        if not known.tracked:
            continue
        if not known.path.exists():
            continue
        projects.append(known)
        seen_paths.add(known.path.resolve())

    # Fold in the ambient project root (useful for single-project installs).
    ambient = getattr(config.project, "root_dir", None)
    if ambient is not None and Path(ambient).exists():
        key = Path(ambient).resolve()
        if key not in seen_paths:
            projects.append(
                KnownProject(
                    key=getattr(config.project, "name", "pollypm") or "pollypm",
                    path=Path(ambient),
                    name=getattr(config.project, "name", None),
                    tracked=True,
                )
            )
    return projects


# ---------------------------------------------------------------------------
# Git log gathering
# ---------------------------------------------------------------------------


def _gather_commits_for_project(
    project: KnownProject,
    *,
    since_utc: datetime,
    until_utc: datetime,
    timeout: float = 5.0,
) -> list[CommitInfo]:
    """Return commits authored in the window for a single project.

    The project is skipped (empty list) when:

    * It lacks a ``.git`` directory.
    * ``git`` is not on PATH / fails.
    * ``git log`` times out.

    We feed ``git log`` the UTC ISO boundaries and use ``--date=iso-strict``
    so the output is unambiguous regardless of the local repo's commit
    dates (commit dates may themselves be in any zone).
    """
    git_dir = project.path / ".git"
    if not git_dir.exists():
        return []

    since_arg = since_utc.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    until_arg = until_utc.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    # Format: <sha>\x1f<iso-date>\x1f<author>\x1f<subject>\x1e between commits.
    fmt = "%H%x1f%cI%x1f%an%x1f%s%x1e"
    cmd = [
        "git", "log",
        f"--since={since_arg}",
        f"--until={until_arg}",
        f"--pretty=format:{fmt}",
        "--no-merges",
    ]
    try:
        result = subprocess.run(
            cmd,
            cwd=project.path,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("briefing: git log failed for %s: %s", project.key, exc)
        return []
    if result.returncode != 0:
        return []

    commits: list[CommitInfo] = []
    for raw in result.stdout.split("\x1e"):
        raw = raw.strip()
        if not raw:
            continue
        parts = raw.split("\x1f")
        if len(parts) < 4:
            continue
        sha, iso_date, author, subject = parts[0], parts[1], parts[2], parts[3]
        commits.append(
            CommitInfo(sha=sha.strip(), timestamp=iso_date.strip(),
                       author=author.strip(), subject=subject.strip())
        )
    return commits


# ---------------------------------------------------------------------------
# Work-service transitions (direct SQL — read-only, single query per project)
# ---------------------------------------------------------------------------


_TRANSITION_SQL = (
    "SELECT t.task_project AS project, t.task_number AS task_number, "
    "       COALESCE(w.title, '') AS title, "
    "       t.from_state AS from_state, t.to_state AS to_state, "
    "       t.actor AS actor, t.created_at AS created_at "
    "FROM work_transitions t "
    "LEFT JOIN work_tasks w "
    "  ON w.project = t.task_project AND w.task_number = t.task_number "
    "WHERE t.created_at >= ? AND t.created_at < ? "
    "ORDER BY t.created_at ASC"
)


def _gather_transitions_for_project(
    project: KnownProject,
    *,
    since_iso: str,
    until_iso: str,
) -> list[TransitionRecord]:
    """Read work_transitions from the project's state.db in the window."""
    db_path = project.path / ".pollypm" / "state.db"
    if not db_path.exists():
        return []
    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        logger.debug("briefing: cannot open %s: %s", db_path, exc)
        return []
    try:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(_TRANSITION_SQL, (since_iso, until_iso)).fetchall()
        except sqlite3.Error as exc:
            logger.debug("briefing: transitions SQL failed for %s: %s", project.key, exc)
            return []
    finally:
        conn.close()

    out: list[TransitionRecord] = []
    for r in rows:
        proj = r["project"]
        num = r["task_number"]
        out.append(
            TransitionRecord(
                project=proj,
                task_id=f"{proj}/{num}",
                task_title=r["title"] or "",
                from_state=r["from_state"] or "",
                to_state=r["to_state"] or "",
                actor=r["actor"] or "",
                timestamp=r["created_at"] or "",
            )
        )
    return out


# ---------------------------------------------------------------------------
# Advisor log
# ---------------------------------------------------------------------------


def _gather_advisor_insights(
    project_root: Path,
    *,
    since_iso: str,
    until_iso: str,
) -> list[AdvisorInsightSummary]:
    """Read ``.pollypm-state/advisor-log.jsonl`` within the window.

    Schema (best-effort — missing fields degrade gracefully):
    each line is a JSON object with keys: ``timestamp`` (UTC ISO),
    ``emit`` (bool), ``project`` (str), ``kind`` (str), ``title``,
    ``body``. Only rows with ``emit == True`` are surfaced.

    Missing file / corrupt lines / wrong type → ignored.
    """
    log_path = project_root / ".pollypm-state" / "advisor-log.jsonl"
    if not log_path.exists():
        return []
    out: list[AdvisorInsightSummary] = []
    try:
        content = log_path.read_text()
    except OSError:
        return out
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        if not entry.get("emit", False):
            continue
        ts = str(entry.get("timestamp") or "")
        if not ts:
            continue
        if not (since_iso <= ts < until_iso):
            continue
        out.append(
            AdvisorInsightSummary(
                timestamp=ts,
                project=str(entry.get("project") or ""),
                kind=str(entry.get("kind") or "insight"),
                title=str(entry.get("title") or ""),
                body=str(entry.get("body") or ""),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Downtime artifacts
# ---------------------------------------------------------------------------


def _gather_downtime_artifacts(
    project: KnownProject,
    *,
    since_iso: str,
    until_iso: str,
) -> list[DowntimeArtifactSummary]:
    """Return downtime tasks that reached ``awaiting_approval`` yesterday.

    We find these by scanning ``work_transitions`` for rows whose
    ``to_state = 'awaiting_approval'`` in the window, filtered to tasks
    labelled with ``downtime`` in ``work_tasks.labels``. The label
    column stores JSON, so we match substring — good enough for the
    briefing's purposes.
    """
    db_path = project.path / ".pollypm" / "state.db"
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT t.task_project AS project, t.task_number AS task_number, "
                "       COALESCE(w.title, '') AS title, t.created_at AS created_at, "
                "       COALESCE(w.labels, '[]') AS labels "
                "FROM work_transitions t "
                "LEFT JOIN work_tasks w "
                "  ON w.project = t.task_project AND w.task_number = t.task_number "
                "WHERE t.to_state = 'awaiting_approval' "
                "  AND t.created_at >= ? AND t.created_at < ? "
                "ORDER BY t.created_at ASC",
                (since_iso, until_iso),
            ).fetchall()
        except sqlite3.Error:
            return []
    finally:
        conn.close()
    out: list[DowntimeArtifactSummary] = []
    for r in rows:
        labels_raw = r["labels"] or "[]"
        if "downtime" not in labels_raw.lower():
            continue
        proj = r["project"]
        num = r["task_number"]
        out.append(
            DowntimeArtifactSummary(
                project=proj,
                task_id=f"{proj}/{num}",
                title=r["title"] or "",
                reached_at=r["created_at"] or "",
            )
        )
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def gather_yesterday(
    config,
    *,
    now_local: datetime,
    project_root: Path | None = None,
) -> YesterdaySnapshot:
    """Build a :class:`YesterdaySnapshot` for the day before ``now_local``.

    ``config`` is a :class:`PollyPMConfig`. ``project_root`` defaults to
    ``config.project.root_dir`` — it's where the advisor log lives.
    """
    if now_local.tzinfo is None:
        raise ValueError("now_local must be timezone-aware")
    start_local, end_local = yesterday_window(now_local)
    date_local = start_local.date().isoformat()

    start_utc = start_local.astimezone(ZoneInfo("UTC"))
    end_utc = end_local.astimezone(ZoneInfo("UTC"))
    since_iso = _to_utc_iso(start_local)
    until_iso = _to_utc_iso(end_local)

    snapshot = YesterdaySnapshot(
        date_local=date_local,
        window_start_utc=since_iso,
        window_end_utc=until_iso,
    )

    projects = iter_tracked_projects(config)
    for project in projects:
        try:
            commits = _gather_commits_for_project(
                project, since_utc=start_utc, until_utc=end_utc,
            )
        except Exception as exc:  # noqa: BLE001 — never crash the briefing
            logger.debug("briefing: commits failed for %s: %s", project.key, exc)
            commits = []
        if commits:
            snapshot.commits_by_project[project.key] = commits
        else:
            snapshot.commits_by_project.setdefault(project.key, [])

        try:
            snapshot.task_transitions.extend(
                _gather_transitions_for_project(
                    project, since_iso=since_iso, until_iso=until_iso,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("briefing: transitions failed for %s: %s", project.key, exc)

        try:
            snapshot.downtime_artifacts.extend(
                _gather_downtime_artifacts(
                    project, since_iso=since_iso, until_iso=until_iso,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("briefing: downtime failed for %s: %s", project.key, exc)

    root = Path(project_root) if project_root is not None else Path(config.project.root_dir)
    try:
        snapshot.advisor_insights = _gather_advisor_insights(
            root, since_iso=since_iso, until_iso=until_iso,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("briefing: advisor log read failed: %s", exc)

    return snapshot
