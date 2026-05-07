"""Gather yesterday's cross-project activity for the morning briefing.

Returns a :class:`YesterdaySnapshot` summarizing:

* ``commits_by_project`` — per-project git-log headlines for the local
  calendar day preceding ``now_local``.
* ``task_transitions`` — work-service state transitions that occurred in
  the same window.
* ``advisor_insights`` — entries from ``.pollypm/advisor-log.jsonl``
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
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pollypm.models import KnownProject


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Signal filter
# ---------------------------------------------------------------------------
#
# A tracked project is only worth surfacing in the briefing when it has
# *something* — a transition, a creation, an inbox arrival, an error.
# Without this gate, ``tracked=true`` becoming the default for all new
# projects (parallel UX change) would balloon the briefing into a wall
# of empty headers for every dormant project the user has ever touched.
#
# We use the audit log as the cheap "did anything happen here" probe:
# every task-lifecycle, marker, and inbox mutation already lands there
# (see :mod:`pollypm.audit`). Reading one JSONL file per project is far
# cheaper than the existing per-project SQL + git-log probes — and lets
# us short-circuit the latter entirely when the project sat idle.
#
# The bar is intentionally low: ANY non-heartbeat event in the window
# qualifies. Heartbeat ticks alone (the audit-watchdog liveness pulse)
# are background noise and do not count. When in doubt — missing log,
# unparseable line, anything funky — we INCLUDE the project. Better an
# occasional quiet header than silently dropping a project the user
# actually cares about.

# Events that, on their own, do not constitute "valuable signal" for
# the briefing. Anything else in the audit log is treated as signal.
_BACKGROUND_AUDIT_EVENTS: frozenset[str] = frozenset({
    "heartbeat.tick",
})

# Operator override: setting this env var to a truthy value forces the
# briefing to include every tracked project, bypassing the signal gate.
# Useful for debugging the gate itself or for users who want a fully
# exhaustive briefing regardless of activity.
_INCLUDE_ALL_ENV = "POLLYPM_BRIEFING_INCLUDE_ALL"


def _truthy(value: str | None) -> bool:
    if not value:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def project_has_signal(
    project: KnownProject,
    *,
    since_iso: str,
) -> bool:
    """Return ``True`` when ``project`` has briefing-worthy activity since ``since_iso``.

    "Signal" is any audit-log event other than the heartbeat-watchdog
    liveness pulse (``heartbeat.tick``). Specifically counted:

    * ``task.created`` / ``task.status_changed`` / ``task.deleted``
    * ``marker.*`` (worker lifecycle: created, released, leaked, failed)
    * ``work_table.cleared`` (forensic — savethenovel-class incidents)
    * ``work_db.opened`` (DB stamp events; rare, but real)
    * Any future ``noun.verb`` audit event.

    Behavior on missing data is conservative: if the audit log is
    absent or unreadable we return ``False`` (skip). New / brand-new
    projects with no audit history don't have signal *because* nothing
    has happened yet — by definition. The briefing's job is to flag
    today's activity, not to advertise project rosters.

    Errors during the read default to ``True`` (include) so a transient
    permission glitch doesn't silently amputate the briefing.
    """
    try:
        from pollypm.audit import read_events
    except Exception as exc:  # noqa: BLE001 — never crash the briefing
        logger.debug("briefing: audit import failed, including %s: %s", project.key, exc)
        return True

    try:
        events = read_events(
            project.key,
            since=since_iso,
            project_path=project.path,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "briefing: read_events failed for %s, including conservatively: %s",
            project.key, exc,
        )
        return True

    for ev in events:
        if ev.event not in _BACKGROUND_AUDIT_EVENTS:
            return True
    return False


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


def project_state_db_paths(project: KnownProject, config=None) -> list[Path]:
    """Return existing state.db paths to read for ``project``.

    Post-#339 the canonical layout collapsed per-project ``state.db``
    files into a workspace-scoped store at
    ``<workspace_root>/.pollypm/state.db``. Briefing reads were left on
    the per-project path and silently failed-open on every install with
    #339 shipped (#1036).

    The dual-path resolution mirrors :func:`pollypm.cockpit_inbox._inbox_db_sources`:

    1. Try ``<project_path>/.pollypm/state.db`` first (legacy /
       per-project layout still in use on some installs).
    2. Fall back to ``<workspace_root>/.pollypm/state.db`` when the
       per-project DB is absent.

    Both are returned (deduped by resolved path) when both exist, so a
    workspace whose project path equals the workspace root scans once,
    and a project sitting under a workspace with a shared DB still
    surfaces its rows.
    """
    candidates: list[Path] = []
    seen: set[Path] = set()

    per_project = project.path / ".pollypm" / "state.db"
    if per_project.exists():
        resolved = per_project.resolve()
        if resolved not in seen:
            seen.add(resolved)
            candidates.append(per_project)

    workspace_root = None
    if config is not None:
        workspace_root = getattr(getattr(config, "project", None), "workspace_root", None)
    if workspace_root is not None:
        ws_db = Path(workspace_root) / ".pollypm" / "state.db"
        if ws_db.exists():
            resolved = ws_db.resolve()
            if resolved not in seen:
                seen.add(resolved)
                candidates.append(ws_db)

    return candidates


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


def _gather_transitions_for_project(
    project: KnownProject,
    *,
    since_iso: str,
    until_iso: str,
    config=None,
) -> list[TransitionRecord]:
    """Read work_transitions from the project's state.db in the window.

    Tries per-project then workspace-root (post-#339) DBs. Rows are
    filtered by ``task_project = project.key`` because a shared
    workspace DB holds rows for many projects.
    """
    from pollypm.storage.morning_briefing_queries import transition_rows

    rows = transition_rows(
        project_state_db_paths(project, config),
        project_key=project.key,
        since_iso=since_iso,
        until_iso=until_iso,
    )
    return [
        TransitionRecord(
            project=str(row["project"] or ""),
            task_id=f"{row['project']}/{row['task_number']}",
            task_title=str(row["title"] or ""),
            from_state=str(row["from_state"] or ""),
            to_state=str(row["to_state"] or ""),
            actor=str(row["actor"] or ""),
            timestamp=str(row["created_at"] or ""),
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Advisor log
# ---------------------------------------------------------------------------


def _gather_advisor_insights(
    project_root: Path,
    *,
    since_iso: str,
    until_iso: str,
) -> list[AdvisorInsightSummary]:
    """Read ``.pollypm/advisor-log.jsonl`` within the window.

    Schema (best-effort — missing fields degrade gracefully):
    each line is a JSON object with keys: ``timestamp`` (UTC ISO),
    ``emit`` (bool), ``project`` (str), ``kind`` (str), ``title``,
    ``body``. Only rows with ``emit == True`` are surfaced.

    Missing file / corrupt lines / wrong type → ignored.
    """
    log_path = project_root / ".pollypm" / "advisor-log.jsonl"
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
    config=None,
) -> list[DowntimeArtifactSummary]:
    """Return downtime tasks that reached ``awaiting_approval`` yesterday.

    We find these by scanning ``work_transitions`` for rows whose
    ``to_state = 'awaiting_approval'`` in the window, filtered to tasks
    labelled with ``downtime`` in ``work_tasks.labels``. The label
    column stores JSON, so we match substring — good enough for the
    briefing's purposes.

    Tries per-project then workspace-root state.db (post-#339), filtering
    by ``task_project = project.key`` so a shared workspace DB only
    surfaces this project's rows.
    """
    from pollypm.storage.morning_briefing_queries import downtime_artifact_rows

    rows = downtime_artifact_rows(
        project_state_db_paths(project, config),
        project_key=project.key,
        since_iso=since_iso,
        until_iso=until_iso,
    )
    return [
        DowntimeArtifactSummary(
            project=str(row["project"] or ""),
            task_id=f"{row['project']}/{row['task_number']}",
            title=str(row["title"] or ""),
            reached_at=str(row["created_at"] or ""),
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def gather_yesterday(
    config,
    *,
    now_local: datetime,
    project_root: Path | None = None,
    include_quiet_projects: bool | None = None,
) -> YesterdaySnapshot:
    """Build a :class:`YesterdaySnapshot` for the day before ``now_local``.

    ``config`` is a :class:`PollyPMConfig`. ``project_root`` defaults to
    ``config.project.root_dir`` — it's where the advisor log lives.

    ``include_quiet_projects`` controls the audit-log signal gate:

    * ``None`` (default) — honour the ``POLLYPM_BRIEFING_INCLUDE_ALL``
      env var; when unset, gate by :func:`project_has_signal`.
    * ``True`` — bypass the gate; iterate every tracked project (legacy
      behaviour). Useful for tests and the operator-debug knob.
    * ``False`` — force the gate on regardless of the env var.

    When the gate trims a project, the briefing skips its commits,
    transitions, downtime artifacts entirely. The advisor-log read
    is unconditional — it lives at the workspace root, not per
    project, and the heuristic doesn't apply.
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

    if include_quiet_projects is None:
        include_quiet_projects = _truthy(os.environ.get(_INCLUDE_ALL_ENV))

    projects = iter_tracked_projects(config)
    if not include_quiet_projects:
        filtered: list[KnownProject] = []
        for project in projects:
            try:
                has_signal = project_has_signal(project, since_iso=since_iso)
            except Exception as exc:  # noqa: BLE001 — conservative include
                logger.debug(
                    "briefing: signal probe crashed for %s, including: %s",
                    project.key, exc,
                )
                has_signal = True
            if has_signal:
                filtered.append(project)
            else:
                logger.debug(
                    "briefing: skipping %s — no audit signal in window",
                    project.key,
                )
        projects = filtered

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
                    config=config,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("briefing: transitions failed for %s: %s", project.key, exc)

        try:
            snapshot.downtime_artifacts.extend(
                _gather_downtime_artifacts(
                    project, since_iso=since_iso, until_iso=until_iso,
                    config=config,
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
