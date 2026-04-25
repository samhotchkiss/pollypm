"""Gather dashboard data from git, issues, snapshots, and state."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pollypm.config import load_config
from pollypm.config import PollyPMConfig
from pollypm.storage.state import StateStore


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


def _recent_commits(config: PollyPMConfig, hours: int = 24) -> list[CommitInfo]:
    """Get git commits from the last N hours across all projects."""
    commits: list[CommitInfo] = []
    now = datetime.now(UTC)
    seen: set[str] = set()

    for key, project in config.projects.items():
        git_dir = project.path / ".git"
        if not git_dir.exists():
            continue
        try:
            result = subprocess.run(
                ["git", "log", f"--since={hours} hours ago", "--format=%H\t%s\t%an\t%aI", "--all"],
                cwd=project.path, capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                continue
            for line in result.stdout.strip().splitlines():
                parts = line.split("\t", 3)
                if len(parts) < 4:
                    continue
                h, msg, author, date_str = parts
                if h in seen:
                    continue
                seen.add(h)
                try:
                    age = (now - datetime.fromisoformat(date_str)).total_seconds()
                except (ValueError, TypeError):
                    age = 0
                commits.append(CommitInfo(
                    hash=h[:7], message=msg[:80], author=author,
                    age_seconds=age, project=key,
                ))
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue

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
            # Check for progress indicators first
            import re
            for line in text.strip().splitlines():
                stripped = line.strip()
                # pytest: "312 passed in 24.80s" or "collecting ..."
                if re.search(r"\d+ passed", stripped):
                    return stripped[:70]
                # npm/build progress
                if "building" in stripped.lower() and ("%" in stripped or "/" in stripped):
                    return stripped[:70]
                # Working indicator with time
                m = re.search(r"Working \((\d+[ms]\s?\d*s?)\s*", stripped)
                if m:
                    return f"working ({m.group(1)})"
            # Look for meaningful lines in the snapshot
            for line in reversed(text.strip().splitlines()):
                line = line.strip()
                if not line or len(line) < 10:
                    continue
                # Skip prompt lines and noise
                if line.startswith(("❯", ">", "$", "%", "─", "│", "┌", "└")):
                    continue
                if "gpt-" in line.lower() or "default ·" in line:
                    continue
                return line[:70]
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
    briefing = ""
    if commits or completed or inbox_count or recoveries:
        parts: list[str] = []
        if commits:
            projects_touched = len({c.project for c in commits})
            parts.append(f"{len(commits)} commits across {projects_touched} project(s)")
        if completed:
            parts.append(f"{len(completed)} issue(s) completed")
        if inbox_count:
            parts.append(f"{inbox_count} inbox item(s) waiting for you")
        if recoveries:
            parts.append(f"{recoveries} session recovery(ies)")
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
