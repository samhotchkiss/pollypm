"""Gather dashboard data from git, issues, snapshots, and state."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pollypm.config import load_config
from pollypm.config import PollyPMConfig
from pollypm.storage.state import StateStore


# ANSI CSI/OSC escapes plus C0 control chars that can survive in a
# tmux pane snapshot. ``readyring`` and friends in the cockpit Now
# panel (#792) are produced when an in-flight render's overlapping
# fragments make it into the snapshot text — strip the control bytes
# before parsing the snapshot.
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*\x07?)")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sanitize_snapshot_line(text: str) -> tuple[str, bool]:
    """Strip ANSI/control bytes from a snapshot line.

    Returns ``(cleaned_text, was_dirty)``. ``was_dirty`` is True when
    the source line carried ANSI escapes or control chars — those
    snapshots come from in-flight renders where adjacent fragments
    can fuse on strip (``ready\x1b[Kring`` → ``readyring``), so the
    caller should treat such lines as untrustworthy.
    """
    had_escape = bool(_ANSI_ESCAPE_RE.search(text) or _CONTROL_CHARS_RE.search(text))
    cleaned = _ANSI_ESCAPE_RE.sub("", text)
    cleaned = _CONTROL_CHARS_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned, had_escape


def _truncate_for_now_panel(text: str, *, limit: int = 70) -> str:
    """Truncate snapshot text at a word boundary, append ``…`` (#792).

    The Now panel previously sliced ``[:70]`` directly, so a status
    like ``"… is on hold awaiting your Phase A decision."`` rendered
    as ``"… is on hold awaiting your Phase A decisio"`` — chopped
    mid-word with no ellipsis to signal the cut.
    """
    if len(text) <= limit:
        return text
    # Look back from the limit for the last space; fall back to a hard
    # cut if the word itself is longer than the budget.
    cut = text.rfind(" ", 0, limit)
    if cut <= 0 or cut < limit - 20:
        cut = limit - 1
    return text[:cut].rstrip() + "…"


@dataclass(slots=True)
class CommitInfo:
    hash: str
    message: str
    author: str
    age_seconds: float
    project: str


@dataclass(slots=True)
class SessionActivity:
    name: str
    role: str
    project: str
    project_label: str
    status: str
    description: str  # human-readable "what it's doing"
    age_seconds: float


@dataclass(slots=True)
class CompletedItem:
    title: str
    kind: str  # "issue", "commit", "pr"
    project: str
    age_seconds: float


@dataclass(slots=True)
class InboxPreview:
    sender: str
    title: str
    project: str
    task_id: str
    age_seconds: float


@dataclass(slots=True)
class DashboardData:
    active_sessions: list[SessionActivity]
    recent_commits: list[CommitInfo]
    completed_items: list[CompletedItem]
    recent_messages: list[InboxPreview]
    daily_tokens: list[tuple[str, int]]  # (date, tokens)
    today_tokens: int
    total_tokens: int
    sweep_count_24h: int
    message_count_24h: int
    recovery_count_24h: int
    inbox_count: int
    alert_count: int
    briefing: str = ""  # morning briefing narrative (if user was away)


# Per-project git-log cache: ``(project_path, hours) -> (cached_at, rows)``.
# The polly-dashboard refreshes every 10s and this helper used to spawn
# one ``git log`` subprocess per project per refresh — at 9 projects ×
# (up to) 5s timeout that was 9 forks every tick and a 45s worst-case
# hang on a single slow repo. Cache for 60s: dashboard ticks 6x within
# the window pay zero subprocess cost. Cache key is the project path so
# a config edit that drops a project just stops looking it up.
_COMMIT_CACHE: dict[tuple[str, int], tuple[float, list["_CachedCommitRow"]]] = {}
_COMMIT_CACHE_TTL_SECONDS = 60.0
_COMMIT_PER_PROJECT_TIMEOUT_SECONDS = 2.0


from dataclasses import dataclass as _dataclass


@_dataclass(slots=True, frozen=True)
class _CachedCommitRow:
    """git-log row stored in the cache — converted to CommitInfo on read.

    Stored separately so the cached ``age_seconds`` doesn't drift; the
    consumer recomputes age from ``date_iso`` at the moment of read.
    """
    hash7: str
    message: str
    author: str
    date_iso: str


def _git_log_rows_cached(project_path: Path, hours: int) -> list[_CachedCommitRow]:
    """Return cached git-log rows for ``project_path``, refreshing on TTL."""
    import time as _time

    cache_key = (str(project_path), hours)
    cached = _COMMIT_CACHE.get(cache_key)
    now_mono = _time.monotonic()
    if cached is not None and (now_mono - cached[0]) < _COMMIT_CACHE_TTL_SECONDS:
        return cached[1]

    git_dir = project_path / ".git"
    if not git_dir.exists():
        _COMMIT_CACHE[cache_key] = (now_mono, [])
        return []
    try:
        result = subprocess.run(
            ["git", "log", f"--since={hours} hours ago", "--format=%H\t%s\t%an\t%aI", "--all"],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=_COMMIT_PER_PROJECT_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        # Cache the empty result too — a slow repo shouldn't be retried
        # every refresh tick. The TTL still applies, so the next 60s of
        # ticks return [] instantly instead of re-spawning git.
        _COMMIT_CACHE[cache_key] = (now_mono, [])
        return []
    if result.returncode != 0:
        _COMMIT_CACHE[cache_key] = (now_mono, [])
        return []

    rows: list[_CachedCommitRow] = []
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t", 3)
        if len(parts) < 4:
            continue
        h, msg, author, date_str = parts
        rows.append(
            _CachedCommitRow(hash7=h[:7], message=msg[:80], author=author, date_iso=date_str)
        )
    _COMMIT_CACHE[cache_key] = (now_mono, rows)
    return rows


def _recent_commits(config: PollyPMConfig, hours: int = 24) -> list[CommitInfo]:
    """Get git commits from the last N hours across all projects.

    Backed by a per-project ``git log`` cache (60s TTL) so the
    dashboard's 10s refresh tick doesn't re-spawn a subprocess per
    project on every tick.
    """
    commits: list[CommitInfo] = []
    now = datetime.now(UTC)
    seen: set[str] = set()

    for key, project in config.projects.items():
        for row in _git_log_rows_cached(project.path, hours):
            if row.hash7 in seen:
                continue
            seen.add(row.hash7)
            try:
                age = (now - datetime.fromisoformat(row.date_iso)).total_seconds()
            except (ValueError, TypeError):
                age = 0.0
            commits.append(CommitInfo(
                hash=row.hash7,
                message=row.message,
                author=row.author,
                age_seconds=age,
                project=key,
            ))

    commits.sort(key=lambda c: c.age_seconds)
    return commits


def _completed_issues(config: PollyPMConfig, hours: int = 72) -> list[CompletedItem]:
    """Find recently completed issues across projects."""
    items: list[CompletedItem] = []
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=hours)

    for key, project in config.projects.items():
        completed_dir = project.path / "issues" / "05-completed"
        if not completed_dir.exists():
            continue
        for f in sorted(completed_dir.glob("*.md"), reverse=True):
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=UTC)
                if mtime < cutoff:
                    continue
                # Extract title from filename: 0035-some-title.md -> some title
                stem = f.stem
                parts = stem.split("-", 1)
                title = parts[1].replace("-", " ") if len(parts) > 1 else stem
                items.append(CompletedItem(
                    title=title, kind="issue", project=key,
                    age_seconds=(now - mtime).total_seconds(),
                ))
            except (OSError, ValueError):
                continue

    items.sort(key=lambda i: i.age_seconds)
    return items[:10]


def _session_description(status: str, role: str, snapshot_path: str | None) -> str:
    """Build a human-readable description of what a session is doing."""
    if role == "operator-pm":
        if status == "healthy":
            return "managing projects and reviewing work"
        if status == "waiting_on_user":
            return "waiting for your direction"
        return "supervising"
    if role == "heartbeat-supervisor":
        return "monitoring all sessions"
    # Worker — try to get context from the last snapshot
    if snapshot_path:
        try:
            text = Path(snapshot_path).read_text(errors="ignore")
            # Strip ANSI escapes and control bytes up front. Without
            # this, in-flight Claude renders leak overlapping
            # fragments like ``ready\x1b[Kring…`` that read as
            # ``readyring`` in the panel (#792). When a line had
            # escapes, the cleaned text is unreliable (adjacent
            # fragments may have fused), so we mark those lines and
            # only use them as a last-resort fallback.
            sanitized = [
                _sanitize_snapshot_line(line)
                for line in text.splitlines()
            ]
            cleaned_lines = [text for text, _dirty in sanitized]
            clean_lines = [text for text, dirty in sanitized if not dirty]
            # Check for progress indicators first — only over the
            # trustworthy (escape-free) lines so we don't echo a
            # half-rendered status string.
            for stripped in clean_lines:
                # pytest: "312 passed in 24.80s" or "collecting ..."
                if re.search(r"\d+ passed", stripped):
                    return _truncate_for_now_panel(stripped)
                # npm/build progress
                if "building" in stripped.lower() and ("%" in stripped or "/" in stripped):
                    return _truncate_for_now_panel(stripped)
                # Working indicator with time
                m = re.search(r"Working \((\d+[ms]\s?\d*s?)\s*", stripped)
                if m:
                    return f"working ({m.group(1)})"
            # Look for meaningful lines in the snapshot — restrict to
            # escape-free lines so a fused render (#792) doesn't end
            # up as the displayed status.
            for line in reversed(clean_lines):
                if not line or len(line) < 10:
                    continue
                # Skip prompt lines and noise
                if line.startswith(("❯", ">", "$", "%", "─", "│", "┌", "└")):
                    continue
                if "gpt-" in line.lower() or "default ·" in line:
                    continue
                # Claude TUI bottom-bar boilerplate. ``⏵⏵`` is the
                # bypass-permissions hint; the others are standing
                # keybinding cues that appear on every snapshot when
                # the session is idle at the prompt. Reporting them
                # as "what's happening now" is misleading — the
                # session isn't *doing* the bypass-permissions thing,
                # it's idle waiting for input.
                lower = line.lower()
                if (
                    "bypass permissions on" in lower
                    or "ctrl+t to hide tasks" in lower
                    or "ctrl+t to show tasks" in lower
                    or "shift+tab to cycle" in lower
                    or line.startswith("⏵⏵")
                ):
                    continue
                return _truncate_for_now_panel(line)
        except (FileNotFoundError, OSError):
            pass
    if status == "waiting_on_user":
        return "waiting for your input"
    if status == "healthy":
        # Use ``idle`` instead of ``working`` — the rail spinner
        # activates on any label ending in ``working``, so mapping
        # the catchall healthy case to ``working`` made Polly's
        # spinner spin forever whenever she wasn't mid-turn (2026-04-20
        # desktop screenshot). ``idle`` reads better in the UI and
        # correctly pauses the spinner until Claude Code itself
        # reports a ``Working (Nm)`` line in the pane snapshot
        # (detected above).
        return "idle"
    if status == "needs_followup":
        return "in progress"
    return status


def _count_inbox_tasks(config: PollyPMConfig) -> int:
    """Total inbox tasks across all tracked projects (work-service backed)."""
    try:
        from pollypm.work.inbox_view import inbox_tasks
        from pollypm.work.sqlite_service import SQLiteWorkService
    except Exception:  # noqa: BLE001
        return 0
    total = 0
    for project_key, project in getattr(config, "projects", {}).items():
        # Same invariant as recovery_prompt._pending_inbox_section
        # (cycle 85) and the doctor's sweeper-dbs check: only tracked
        # projects' state.db files are PollyPM-owned. A registered-
        # but-not-tracked project may have a stale .pollypm/state.db
        # left over from a prior tracking run; counting its leftover
        # inbox tasks inflates the morning-briefing count and the
        # doctor's "open inbox items" check.
        if not getattr(project, "tracked", False):
            continue
        db_path = project.path / ".pollypm" / "state.db"
        if not db_path.exists():
            continue
        try:
            with SQLiteWorkService(
                db_path=db_path, project_path=project.path,
            ) as svc:
                total += len(inbox_tasks(svc, project=project_key))
        except Exception:  # noqa: BLE001
            continue
    return total


def _user_waiting_task_ids_across_projects(
    config: PollyPMConfig,
) -> frozenset[str]:
    """Return ``project/N`` ids for every task in a user-waiting state
    across every tracked project.

    Reads each project's ``state.db`` directly (read-only sqlite) so
    we don't pay the work-service hydration cost just to filter
    alerts. Used to suppress ``stuck_on_task:<id>`` alerts that are
    already covered by the project's user-waiting status.
    """
    import sqlite3 as _sqlite3

    out: set[str] = set()
    for project_key, project in getattr(config, "projects", {}).items():
        # Same tracked-only invariant as _count_inbox_tasks (cycle 86)
        # and _pending_inbox_section (cycle 85).
        if not getattr(project, "tracked", False):
            continue
        db_path = project.path / ".pollypm" / "state.db"
        if not db_path.exists():
            continue
        try:
            conn = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                rows = conn.execute(
                    "SELECT task_number FROM work_tasks "
                    "WHERE project = ? "
                    "AND work_status IN ('blocked','on_hold','waiting_on_user')",
                    (project_key,),
                ).fetchall()
            finally:
                conn.close()
        except (_sqlite3.Error, OSError):
            continue
        for (number,) in rows:
            out.add(f"{project_key}/{number}")
    return frozenset(out)


def _stuck_alert_already_user_waiting(
    alert_type: str, user_waiting_task_ids: frozenset[str],
) -> bool:
    """Return True for ``stuck_on_task:<id>`` alerts on a user-
    waiting task. Mirror of the rail-side helper in
    ``cockpit_rail._stuck_alert_already_user_waiting``.
    """
    prefix = "stuck_on_task:"
    if not alert_type or not alert_type.startswith(prefix):
        return False
    task_id = alert_type[len(prefix):].strip()
    return bool(task_id) and task_id in user_waiting_task_ids


def _inbox_sender(task) -> str:
    roles = getattr(task, "roles", {}) or {}
    operator = roles.get("operator")
    if operator and operator != "user":
        return str(operator)
    created_by = getattr(task, "created_by", "")
    if created_by and created_by != "user":
        return str(created_by)
    return "polly"


def _recent_inbox_messages(config: PollyPMConfig, *, limit: int = 3) -> list[InboxPreview]:
    try:
        from pollypm.work.inbox_view import inbox_tasks
        from pollypm.work.sqlite_service import SQLiteWorkService
    except Exception:  # noqa: BLE001
        return []

    now = datetime.now(UTC)
    seen_task_ids: set[str] = set()
    previews: list[InboxPreview] = []
    sources: list[tuple[str | None, str, Path, Path]] = []
    for project_key, project in getattr(config, "projects", {}).items():
        # Same tracked-only invariant as _count_inbox_tasks (cycle 86):
        # a non-tracked project's leftover state.db would leak stale
        # tasks into the polly-dashboard's "Recent messages" preview.
        if not getattr(project, "tracked", False):
            continue
        sources.append((project_key, project.display_label(), project.path / ".pollypm" / "state.db", project.path))
    workspace_root = getattr(getattr(config, "project", None), "workspace_root", None)
    if workspace_root is not None:
        workspace_path = Path(workspace_root)
        sources.append((None, "Workspace", workspace_path / ".pollypm" / "state.db", workspace_path))

    for project_key, project_label, db_path, project_path in sources:
        if not db_path.exists():
            continue
        try:
            with SQLiteWorkService(db_path=db_path, project_path=project_path) as svc:
                for task in inbox_tasks(svc, project=project_key):
                    if task.task_id in seen_task_ids:
                        continue
                    seen_task_ids.add(task.task_id)
                    stamped = getattr(task, "updated_at", None) or getattr(task, "created_at", None)
                    if hasattr(stamped, "timestamp"):
                        age_seconds = max(0.0, now.timestamp() - float(stamped.timestamp()))
                    else:
                        try:
                            age_seconds = max(
                                0.0,
                                (now - datetime.fromisoformat(str(stamped))).total_seconds(),
                            )
                        except (ValueError, TypeError):
                            age_seconds = 0.0
                    previews.append(
                        InboxPreview(
                            sender=_inbox_sender(task),
                            title=(getattr(task, "title", "") or "(untitled)")[:80],
                            project=project_label,
                            task_id=task.task_id,
                            age_seconds=age_seconds,
                        )
                    )
        except Exception:  # noqa: BLE001
            continue

    previews.sort(key=lambda item: item.age_seconds)
    return previews[:limit]


def load_dashboard(config_path: Path) -> tuple[PollyPMConfig, DashboardData]:
    """Load config + state store and gather one blocking dashboard snapshot."""
    config = load_config(config_path)
    store = StateStore(config.project.state_db)
    try:
        data = gather(config, store)
    finally:
        store.close()
    return config, data


def gather(config: PollyPMConfig, store: StateStore) -> DashboardData:
    """Gather all dashboard data."""
    from pollypm.service_api import plan_launches_readonly

    now = datetime.now(UTC)

    # Active sessions
    all_runtimes = store.list_session_runtimes()
    runtime_map = {rt.session_name: rt for rt in all_runtimes}
    launches = plan_launches_readonly(config, store)

    active: list[SessionActivity] = []
    for launch in launches:
        rt = runtime_map.get(launch.session.name)
        status = rt.status if rt else "unknown"
        project = config.projects.get(launch.session.project)
        label = project.display_label() if project else launch.session.project

        # Get last snapshot path for description
        hb = store.latest_heartbeat(launch.session.name)
        snapshot_path = hb.snapshot_path if hb else None

        desc = _session_description(status, launch.session.role, snapshot_path)
        age = 0.0
        if rt and rt.updated_at:
            try:
                age = (now - datetime.fromisoformat(rt.updated_at)).total_seconds()
            except (ValueError, TypeError):
                pass

        active.append(SessionActivity(
            name=launch.session.name, role=launch.session.role,
            project=launch.session.project, project_label=label,
            status=status, description=desc, age_seconds=age,
        ))

    # Events summary
    recent = store.recent_events(limit=300)
    cutoff = (now - timedelta(hours=24)).isoformat()
    day_events = [e for e in recent if e.created_at >= cutoff]

    # Token data
    daily = store.daily_token_usage(days=30)
    values = [t for _, t in daily]
    today_str = now.strftime("%Y-%m-%d")
    today_tokens = next((t for d, t in daily if d == today_str), 0)

    commits = _recent_commits(config, hours=24)
    completed = _completed_issues(config, hours=72)
    inbox_count = _count_inbox_tasks(config)
    recent_messages = _recent_inbox_messages(config)
    sweeps = sum(1 for e in day_events if e.event_type == "heartbeat")
    recoveries = sum(1 for e in day_events if "recover" in e.event_type)

    # Morning briefing: generate if there are overnight results
    def _plural(count: int, singular: str, plural: str | None = None) -> str:
        word = singular if count == 1 else (plural or f"{singular}s")
        return f"{count} {word}"

    briefing = ""
    if commits or completed or inbox_count or recoveries:
        parts: list[str] = []
        if commits:
            projects_touched = len({c.project for c in commits})
            parts.append(
                f"{_plural(len(commits), 'commit')} across "
                f"{_plural(projects_touched, 'project')}"
            )
        if completed:
            parts.append(f"{_plural(len(completed), 'issue')} completed")
        if inbox_count:
            parts.append(
                f"{_plural(inbox_count, 'inbox item')} waiting for you"
            )
        if recoveries:
            parts.append(
                _plural(recoveries, "session recovery", "session recoveries")
            )
        briefing = "While you were away: " + ", ".join(parts) + "."

    # Late import keeps dashboard_data out of the cockpit_alerts import
    # graph (cockpit_alerts → cockpit_palette → cockpit, which pulls
    # dashboard_data in at top level).
    from pollypm.cockpit_alerts import is_operational_alert

    # Drop ``stuck_on_task:<id>`` alerts whose task is already in a
    # user-waiting state — the session sat idle because the user
    # hasn't responded, which is the system doing what it should,
    # not a fault to surface as a separate alert. Mirrors cycles 45
    # / 53 / 55 dedup at the global polly-dashboard count level.
    user_waiting = _user_waiting_task_ids_across_projects(config)
    alert_count = sum(
        1 for a in store.open_alerts()
        if not is_operational_alert(a.alert_type)
        and not _stuck_alert_already_user_waiting(
            a.alert_type, user_waiting,
        )
    )

    return DashboardData(
        active_sessions=active,
        recent_commits=commits,
        completed_items=completed,
        recent_messages=recent_messages,
        daily_tokens=daily,
        today_tokens=today_tokens,
        total_tokens=sum(values),
        sweep_count_24h=sweeps,
        message_count_24h=sum(1 for e in day_events if e.event_type == "send_input"),
        recovery_count_24h=recoveries,
        inbox_count=inbox_count,
        alert_count=alert_count,
        briefing=briefing,
    )
