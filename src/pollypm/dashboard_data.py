"""Gather dashboard data from git, issues, snapshots, and state."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
class DashboardData:
    active_sessions: list[SessionActivity]
    recent_commits: list[CommitInfo]
    completed_items: list[CompletedItem]
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
        return "working"
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

    return DashboardData(
        active_sessions=active,
        recent_commits=commits,
        completed_items=completed,
        daily_tokens=daily,
        today_tokens=today_tokens,
        total_tokens=sum(values),
        sweep_count_24h=sweeps,
        message_count_24h=sum(1 for e in day_events if e.event_type == "send_input"),
        recovery_count_24h=recoveries,
        inbox_count=inbox_count,
        alert_count=len([
            a for a in store.open_alerts()
            if a.alert_type not in ("suspected_loop", "stabilize_failed", "needs_followup")
        ]),
        briefing=briefing,
    )
