from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from pollypm.atomic_io import atomic_write_json
from pollypm.config import load_config
from pollypm.providers import get_provider
from pollypm.projects import ensure_project_scaffold
from pollypm.runtimes import get_runtime
from pollypm.service_api import PollyPMService
from pollypm.task_backends import get_task_backend
from pollypm.session_services import create_tmux_client
from pollypm.worktrees import list_worktrees


_STATUS_ICONS = {
    "draft": "◌",
    "queued": "○",
    "in_progress": "⟳",
    "blocked": "⊘",
    "on_hold": "⏸",
    "review": "◉",
    "done": "✓",
    "cancelled": "✗",
}


def _render_work_service_issues(project: object) -> str:
    """Render tasks from the work service for a project."""
    from pollypm.work.sqlite_service import SQLiteWorkService

    db_path = project.path / ".pollypm" / "state.db"
    if not db_path.exists():
        raise FileNotFoundError(db_path)

    with SQLiteWorkService(db_path=db_path, project_path=project.path) as svc:
        counts = svc.state_counts(project=getattr(project, "key", None))
        tasks = svc.list_tasks(project=getattr(project, "key", None))

    name = getattr(project, "name", None) or getattr(project, "key", "Project")
    lines = [f"{name} · Tasks", ""]

    # Summary bar
    count_parts = []
    for status in ("queued", "in_progress", "review", "blocked", "on_hold", "draft"):
        n = counts.get(status, 0)
        if n:
            icon = _STATUS_ICONS.get(status, "·")
            count_parts.append(f"{icon} {n} {status.replace('_', ' ')}")
    if count_parts:
        lines.append(" · ".join(count_parts))
        lines.append("")

    # Active tasks (non-terminal) — sorted by status priority
    _so = {"in_progress": 0, "review": 1, "queued": 2, "blocked": 3, "on_hold": 4, "draft": 5}
    active = [t for t in tasks if t.work_status.value not in ("done", "cancelled")]
    active.sort(key=lambda t: _so.get(t.work_status.value, 9))
    if active:
        for t in active:
            icon = _STATUS_ICONS.get(t.work_status.value, "·")
            assignee = f" [{t.assignee}]" if t.assignee else ""
            lines.append(f"  {icon} #{t.task_number} {t.title}{assignee}")
        lines.append("")

    # Recently completed (last 10)
    completed = [t for t in tasks if t.work_status.value in ("done", "cancelled")]
    completed.sort(key=lambda t: t.updated_at or "", reverse=True)
    if completed:
        lines.append(f"─── completed ({len(completed)}) ───")
        for t in completed[:10]:
            icon = _STATUS_ICONS.get(t.work_status.value, "·")
            lines.append(f"  {icon} #{t.task_number} {t.title}")
        lines.append("")

    if not tasks:
        lines.append("No tasks found.")

    return "\n".join(lines)


def _inbox_db_sources(config) -> list[tuple[str | None, Path, Path]]:
    """Return ``(project_key, db_path, project_path)`` for every inbox source.

    Includes the per-project ``.pollypm/state.db`` for every registered
    project **and** the workspace-root ``<workspace_root>/.pollypm/state.db``
    — the latter is where ``pm notify`` (with defaults) lands items that
    don't belong to any one project (#271). Duplicates are dropped so a
    project whose path happens to equal the workspace root is scanned once.

    ``project_key`` is ``None`` for the workspace-root source; callers that
    need a project filter treat ``None`` as "no filter".
    """
    sources: list[tuple[str | None, Path, Path]] = []
    seen: set[Path] = set()
    for project_key, project in getattr(config, "projects", {}).items():
        project_path = Path(project.path)
        db_path = project_path / ".pollypm" / "state.db"
        resolved = db_path.resolve() if db_path.exists() else db_path
        if resolved in seen:
            continue
        seen.add(resolved)
        sources.append((project_key, db_path, project_path))

    workspace_root = getattr(getattr(config, "project", None), "workspace_root", None)
    if workspace_root is not None:
        ws_path = Path(workspace_root)
        ws_db = ws_path / ".pollypm" / "state.db"
        resolved = ws_db.resolve() if ws_db.exists() else ws_db
        if resolved not in seen:
            seen.add(resolved)
            sources.append((None, ws_db, ws_path))
    return sources


def _count_inbox_tasks_for_label(config) -> int:
    """Sum of inbox tasks across all tracked projects + workspace-root.

    Used by the cockpit rail label and the dashboard summary line; the
    aggregate must match what :func:`_render_inbox_panel` would render.
    Dedupes tasks by ``task_id`` so a task that somehow appears in more
    than one DB counts exactly once.
    """
    try:
        from pollypm.work.inbox_view import inbox_tasks
        from pollypm.work.sqlite_service import SQLiteWorkService
    except Exception:  # noqa: BLE001
        return 0
    seen_task_ids: set[str] = set()
    for project_key, db_path, project_path in _inbox_db_sources(config):
        if not db_path.exists():
            continue
        try:
            with SQLiteWorkService(
                db_path=db_path, project_path=project_path,
            ) as svc:
                for task in inbox_tasks(svc, project=project_key):
                    seen_task_ids.add(task.task_id)
        except Exception:  # noqa: BLE001
            continue
    return len(seen_task_ids)


def render_inbox_panel(service, projects: list[object] | None = None) -> str:
    """Render the cockpit inbox panel from a WorkService.

    Groups tasks by project (when multiple project roots are tracked) and
    lists each one on two lines — subject-like header + status/when subline
    — matching the visual shape of the legacy inbox panel.

    ``projects`` is an optional list of project objects (each with ``.key``
    and ``.name``) used solely to resolve display names; when omitted the
    raw project key is shown.
    """
    from pollypm.work.inbox_view import inbox_tasks

    project_names: dict[str, str] = {}
    for proj in projects or ():
        key = getattr(proj, "key", None)
        if key:
            project_names[key] = getattr(proj, "name", None) or key

    tasks = inbox_tasks(service)
    assigned = len(tasks)

    lines = ["Inbox"]
    if not tasks:
        lines.extend(
            [
                "",
                "No tasks waiting for you.",
                "",
                "This panel shows non-terminal tasks whose current node is",
                "assigned to you (actor_type: human) or whose roles include",
                "the 'user' role.",
            ]
        )
        lines.extend(
            [
                "",
                f"Assigned: {assigned}",
                "List: pm inbox",
                "Show: pm inbox show <task_id>",
            ]
        )
        return "\n".join(lines)

    lines.extend(["", f"Assigned ({assigned}):"])
    for task in tasks[:10]:
        prio = task.priority.value
        prefix = ""
        if prio == "critical":
            prefix = "▲ "
        elif prio == "high":
            prefix = "◆ "
        subject = task.title[:60]
        lines.append(f"  {prefix}{subject}")

        updated = task.updated_at
        if updated is not None and hasattr(updated, "isoformat"):
            when = updated.isoformat()[:16]
        else:
            when = (str(updated) if updated else "")[:16]
        project_label = project_names.get(task.project, task.project)
        node_part = f" · @{task.current_node_id}" if task.current_node_id else ""
        lines.append(
            f"    {task.task_id} · {project_label} · {task.work_status.value}"
            f"{node_part} · {when}"
        )

        preview = (task.description or "").strip().split("\n", 1)[0][:70]
        if preview:
            lines.append(f"    {preview}")
        lines.append("")

    if assigned > 10:
        lines.append(f"… and {assigned - 10} more")
        lines.append("")

    lines.extend(
        [
            "List: pm inbox",
            "Show: pm inbox show <task_id>",
        ]
    )
    return "\n".join(lines)


def _render_inbox_panel(config) -> str:
    """Render the inbox panel for the active cockpit config.

    Opens every tracked project's work-service DB, queries the inbox from
    each, and renders the combined view. Projects with no DB are silently
    skipped so a fresh install stays usable.
    """
    from pollypm.work.sqlite_service import SQLiteWorkService

    # Aggregate inbox tasks across all tracked projects. Each project has its
    # own SQLite db; we open each, query, then close.
    class _AggregateService:
        """Adapter exposing the subset of WorkService used by inbox_view."""

        def __init__(self) -> None:
            self._tasks: list = []
            self._flow_cache: dict = {}
            self._flows_by_svc: list = []

        def list_tasks(self, *, project=None, **_ignored):
            if project is None:
                return list(self._tasks)
            return [t for t in self._tasks if t.project == project]

        def get_flow(self, name: str, project=None):
            key = (name, project)
            if key in self._flow_cache:
                return self._flow_cache[key]
            for svc in self._flows_by_svc:
                try:
                    flow = svc.get_flow(name, project=project)
                except Exception:  # noqa: BLE001
                    continue
                self._flow_cache[key] = flow
                return flow
            raise KeyError(f"flow {name!r} not found")

    agg = _AggregateService()
    opened: list[SQLiteWorkService] = []
    seen_task_ids: set[str] = set()
    try:
        for project_key, db_path, project_path in _inbox_db_sources(config):
            if not db_path.exists():
                continue
            try:
                svc = SQLiteWorkService(
                    db_path=db_path, project_path=project_path,
                )
            except Exception:  # noqa: BLE001
                continue
            opened.append(svc)
            agg._flows_by_svc.append(svc)
            try:
                for task in svc.list_tasks(project=project_key):
                    if task.task_id in seen_task_ids:
                        continue
                    seen_task_ids.add(task.task_id)
                    agg._tasks.append(task)
            except Exception:  # noqa: BLE001
                pass

        projects_iter = list(getattr(config, "projects", {}).values())
        return render_inbox_panel(agg, projects=projects_iter)
    finally:
        for svc in opened:
            try:
                svc.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Per-project dashboard (#245) — info-dense, section-based layout.
#
# Each ``_section_*`` helper renders one block of the dashboard. All helpers
# degrade gracefully: when their data source is missing or malformed they
# return an empty string (omitted section) or a "(none)" line rather than
# raising. The top-level ``_render_project_dashboard`` threads a single
# gathered ``_ProjectSnapshot`` through them to keep SQLite opens to one per
# render.
# ---------------------------------------------------------------------------

_DASHBOARD_DIVIDER_WIDTH = 72
_DASHBOARD_BULLET = "  "  # two-space indent for every row


def _dashboard_divider(title: str = "") -> str:
    """Return a section divider line ``─── title ──────``."""
    if not title:
        return _DASHBOARD_BULLET + "─" * (_DASHBOARD_DIVIDER_WIDTH - 2)
    prefix = f"─── {title} "
    remaining = max(3, _DASHBOARD_DIVIDER_WIDTH - 2 - len(prefix))
    return _DASHBOARD_BULLET + prefix + "─" * remaining


def _format_tokens(n: int) -> str:
    """Human-readable token count: ``1234`` → ``1.2k``, ``2_100_000`` → ``2.1M``."""
    if n < 1000:
        return str(int(n))
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.1f}M"


def _iso_to_dt(value: object):
    """Best-effort ISO-string → aware datetime. Returns ``None`` on failure."""
    from datetime import UTC, datetime

    if value is None:
        return None
    if hasattr(value, "tzinfo"):
        dt = value  # already a datetime
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _age_from_dt(dt, now=None) -> str:
    """Relative age: '5m ago', '2h ago'. Empty string on None."""
    from datetime import UTC, datetime

    if dt is None:
        return ""
    now = now or datetime.now(UTC)
    secs = (now - dt).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _format_clock(dt) -> str:
    """Render ``HH:MM`` from a datetime for activity timeline rows."""
    from datetime import UTC, datetime

    if dt is None:
        return "     "
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.strftime("%H:%M")


def _find_commit_sha(task) -> str | None:
    """Pull a commit SHA from a task's most recent completed execution.

    Walks the task's executions newest-first and looks for an artifact
    with ``kind == ArtifactKind.COMMIT``. Returns the 7-char short SHA
    so the row stays narrow, or ``None`` if no commit was produced.
    """
    try:
        from pollypm.work.models import ArtifactKind
    except Exception:  # noqa: BLE001
        return None
    executions = getattr(task, "executions", None) or []
    for execution in reversed(executions):
        output = getattr(execution, "work_output", None)
        if output is None:
            continue
        for artifact in getattr(output, "artifacts", None) or []:
            if getattr(artifact, "kind", None) == ArtifactKind.COMMIT:
                ref = getattr(artifact, "ref", None)
                if ref:
                    return str(ref)[:7]
    return None


def _task_cycle_minutes(task) -> int | None:
    """Minutes between first in_progress transition and the terminal one.

    Falls back to ``None`` when transitions are missing or dates can't be
    parsed — keeps the rendering tolerant of partial state on old tasks.
    """
    transitions = getattr(task, "transitions", None) or []
    start = None
    end = None
    for tr in transitions:
        ts = getattr(tr, "timestamp", None)
        to_state = getattr(tr, "to_state", "")
        if to_state == "in_progress" and start is None:
            start = ts
        if to_state in ("done", "cancelled"):
            end = ts
    if start is None or end is None:
        return None
    try:
        return max(0, int((end - start).total_seconds() // 60))
    except (TypeError, ValueError):
        return None


def _aggregate_project_tokens(
    db_path: Path, project_key: str,
) -> tuple[int, int] | None:
    """SUM(total_input_tokens), SUM(total_output_tokens) for ``project_key``.

    Queries ``work_sessions`` directly — when #86 lands its aggregate
    helper we can swap this out for a single method call. Returns
    ``None`` when the table is missing (old DB) or the query fails, so
    the Tokens line degrades to "(n/a)" rather than breaking the render.
    """
    import sqlite3

    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(total_input_tokens), 0), "
                "       COALESCE(SUM(total_output_tokens), 0) "
                "FROM work_sessions WHERE task_project = ?",
                (project_key,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if row is None:
        return 0, 0
    return int(row[0] or 0), int(row[1] or 0)


def _worker_presence(supervisor, project_key: str) -> str:
    """Render the header right-gutter: ``● worker alive`` / ``○ worker idle`` / ``– none``.

    A worker is "alive" when at least one planned session for this project
    has a recent heartbeat. "idle" means we know about the session but the
    heartbeat is stale / absent. "none" means no worker session is planned
    for this project at all.
    """
    from datetime import UTC, datetime, timedelta

    try:
        launches = list(supervisor.plan_launches())
    except Exception:  # noqa: BLE001
        return "– no supervisor"

    session_names = [
        l.session.name for l in launches
        if getattr(l.session, "project", None) == project_key
        and getattr(l.session, "role", "") != "operator-pm"
    ]
    if not session_names:
        return "– no worker"

    alive_cutoff = datetime.now(UTC) - timedelta(minutes=5)
    for name in session_names:
        try:
            hb = supervisor.store.latest_heartbeat(name)
        except Exception:  # noqa: BLE001
            continue
        if hb is None:
            continue
        dt = _iso_to_dt(hb.created_at)
        if dt is not None and dt > alive_cutoff and not hb.pane_dead:
            return "● worker alive"
    return "○ worker idle"


def _section_header(name: str, presence: str) -> str:
    """Project-name header with worker presence in the right gutter."""
    gutter = presence or ""
    # Pad the name so ``presence`` sits at the right edge of the divider.
    total_width = _DASHBOARD_DIVIDER_WIDTH
    # account for 2-space indent; name on the left, presence on the right
    pad = max(1, total_width - len(name) - len(gutter) - 2)
    return f"{_DASHBOARD_BULLET}{name}{' ' * pad}{gutter}"


def _section_summary(counts: dict[str, int]) -> str:
    """Counts bar: ``⟳ 1 in progress · ◉ 0 review · ○ 2 queued · ✓ 1 done``."""
    parts: list[str] = []
    for status in ("in_progress", "review", "queued", "blocked", "on_hold", "draft"):
        n = counts.get(status, 0)
        if n:
            icon = _STATUS_ICONS.get(status, "·")
            parts.append(f"{icon} {n} {status.replace('_', ' ')}")
    done = counts.get("done", 0)
    if done:
        parts.append(f"✓ {done} done")
    if not parts:
        return f"{_DASHBOARD_BULLET}No tasks yet."
    return _DASHBOARD_BULLET + " · ".join(parts)


def _section_velocity(tasks: list, tokens: tuple[int, int] | None) -> list[str]:
    """Velocity + cycle-time sparklines and token aggregation."""
    from datetime import UTC, datetime, timedelta

    lines: list[str] = []
    now = datetime.now(UTC)

    # Weekly velocity over the last 7 weeks: count of tasks that hit a
    # terminal state in each week. The sparkline reads left-to-right
    # oldest → newest.
    weekly: list[int] = [0] * 7
    for t in tasks:
        if getattr(t, "work_status", None) is None:
            continue
        if t.work_status.value not in ("done", "cancelled"):
            continue
        dt = _iso_to_dt(t.updated_at)
        if dt is None:
            continue
        age_days = (now - dt).days
        if age_days < 0 or age_days >= 49:
            continue
        week_idx = 6 - (age_days // 7)
        if 0 <= week_idx < 7:
            weekly[week_idx] += 1
    if any(weekly):
        per_week = weekly[-1]
        trend = (
            "trending up" if weekly[-1] > weekly[0] + 1 else
            "trending down" if weekly[-1] + 1 < weekly[0] else
            "steady"
        )
        lines.append(
            f"{_DASHBOARD_BULLET}Velocity    {_spark_bar(weekly):<8}    "
            f"{per_week} tasks/wk, {trend}"
        )

    # Cycle time sparkline: median minutes for each of the last 7 completed tasks.
    cycles: list[int] = []
    completed = [
        t for t in tasks if getattr(t, "work_status", None) is not None
        and t.work_status.value == "done"
    ]
    completed.sort(
        key=lambda t: _iso_to_dt(t.updated_at)
        or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    for t in completed[:7]:
        m = _task_cycle_minutes(t)
        if m is not None:
            cycles.append(m)
    if cycles:
        avg_min = sum(cycles) // len(cycles)
        cycles_asc = list(reversed(cycles))  # oldest-left, newest-right
        lines.append(
            f"{_DASHBOARD_BULLET}Cycle time  {_spark_bar(cycles_asc):<8}    "
            f"{avg_min}m avg"
        )

    # Token aggregation — drops the line entirely when unavailable.
    if tokens is not None:
        tin, tout = tokens
        if tin or tout:
            lines.append(
                f"{_DASHBOARD_BULLET}Tokens      "
                f"{_format_tokens(tin)} in · {_format_tokens(tout)} out"
            )
    return lines


def _section_you_need_to(
    review_tasks: list,
    alerts: list,
    insights_pending: int,
) -> list[str]:
    """Attention-first block — approvals, alerts, advisor insights."""
    lines = [_dashboard_divider("You need to"), ""]
    if not review_tasks and not alerts and not insights_pending:
        lines.append(f"{_DASHBOARD_BULLET}Nothing pending.")
        lines.append("")
        return lines
    for t in review_tasks[:5]:
        lines.append(
            f"{_DASHBOARD_BULLET}◉ approve #{t.task_number} {t.title}"
        )
    for a in alerts[:3]:
        sess = getattr(a, "session_name", "")
        kind = getattr(a, "alert_type", "")
        msg = (getattr(a, "message", "") or "")[:60]
        lines.append(f"{_DASHBOARD_BULLET}▲ {sess}: {kind} — {msg}")
    if insights_pending:
        lines.append(
            f"{_DASHBOARD_BULLET}✦ {insights_pending} advisor "
            f"insight{'s' if insights_pending != 1 else ''} to review"
        )
    lines.append("")
    return lines


def _section_in_flight(in_progress: list) -> list[str]:
    """Tasks currently being worked on."""
    lines = [_dashboard_divider("In flight"), ""]
    if not in_progress:
        lines.extend([f"{_DASHBOARD_BULLET}(none)", ""])
        return lines
    for t in in_progress:
        icon = _STATUS_ICONS.get(t.work_status.value, "⟳")
        assignee = f" [{t.assignee}]" if getattr(t, "assignee", None) else ""
        node = (
            f" @ {t.current_node_id}"
            if getattr(t, "current_node_id", None)
            else ""
        )
        age = _age_from_dt(_iso_to_dt(getattr(t, "updated_at", None)))
        age_part = f" · {age}" if age else ""
        lines.append(
            f"{_DASHBOARD_BULLET}{icon} #{t.task_number} {t.title}"
            f"{assignee}{node}{age_part}"
        )
    lines.append("")
    return lines


def _section_recent(completed: list) -> list[str]:
    """Most recently finished task with commit SHA, cycle time, approver."""
    lines = [_dashboard_divider("Recent"), ""]
    if not completed:
        lines.extend([f"{_DASHBOARD_BULLET}(none)", ""])
        return lines
    t = completed[0]
    dt = _iso_to_dt(getattr(t, "updated_at", None))
    clock = _format_clock(dt)
    icon = _STATUS_ICONS.get(t.work_status.value, "✓")

    # Approver = the actor on the last done/approve transition.
    approver = ""
    for tr in reversed(getattr(t, "transitions", None) or []):
        if tr.to_state in ("done",) and tr.actor:
            approver = f"approved by {tr.actor}"
            break
    if not approver:
        approver = ""

    title = t.title[:40]
    right = f"  {approver}" if approver else ""
    lines.append(
        f"{_DASHBOARD_BULLET}{clock}  {icon} #{t.task_number} {title}{right}"
    )

    # Sub-line: commit SHA + cycle time.
    sub_parts: list[str] = []
    sha = _find_commit_sha(t)
    if sha:
        sub_parts.append(f"commit {sha}")
    cycle = _task_cycle_minutes(t)
    if cycle is not None:
        sub_parts.append(f"{cycle}m cycle")
    if sub_parts:
        lines.append(f"{_DASHBOARD_BULLET}       └ " + " · ".join(sub_parts))
    lines.append("")
    return lines


def _section_activity(
    tasks: list,
    system_events: list,
    *,
    now=None,
    limit: int = 10,
) -> list[str]:
    """Unified timeline: task transitions + system events from last 24h."""
    from datetime import UTC, datetime, timedelta

    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=24)

    entries: list[tuple[object, str]] = []  # (datetime, rendered row)

    for t in tasks:
        for tr in getattr(t, "transitions", None) or []:
            dt = getattr(tr, "timestamp", None)
            if dt is None:
                continue
            if getattr(dt, "tzinfo", None) is None:
                dt = dt.replace(tzinfo=UTC)
            if dt < cutoff:
                continue
            clock = _format_clock(dt)
            actor = tr.actor or "?"
            entries.append(
                (
                    dt,
                    f"{_DASHBOARD_BULLET}{clock}  task/{t.task_number} "
                    f"→ {tr.to_state:<15}({actor})",
                )
            )

    for ev in system_events:
        dt = _iso_to_dt(getattr(ev, "created_at", None))
        if dt is None or dt < cutoff:
            continue
        etype = getattr(ev, "event_type", "")
        if etype in ("heartbeat", "token_ledger", "polly_followup"):
            continue
        sess = getattr(ev, "session_name", "") or "system"
        msg = (getattr(ev, "message", "") or "")[:50]
        clock = _format_clock(dt)
        entries.append(
            (
                dt,
                f"{_DASHBOARD_BULLET}{clock}  {etype} {msg} ({sess})",
            )
        )

    entries.sort(key=lambda row: row[0], reverse=True)

    lines = [_dashboard_divider("Activity (last 24h)"), ""]
    if not entries:
        lines.extend([f"{_DASHBOARD_BULLET}(none)", ""])
        return lines
    for _dt, row in entries[:limit]:
        lines.append(row)
    lines.append("")
    return lines


def _section_insights(project_path: Path, project_key: str) -> list[str]:
    """Last 7 days of advisor insights (emit=true) for this project."""
    from datetime import UTC, datetime, timedelta

    lines = [_dashboard_divider("Insights"), ""]
    log_path = project_path / ".pollypm" / "advisor-log.jsonl"
    if not log_path.exists():
        lines.extend(
            [f"{_DASHBOARD_BULLET}(no advisor insights in last 7 days)", ""]
        )
        return lines

    cutoff = datetime.now(UTC) - timedelta(days=7)
    kept: list[tuple[object, str, str]] = []
    try:
        raw = log_path.read_text(encoding="utf-8")
    except OSError:
        raw = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("project") != project_key:
            continue
        if data.get("decision") != "emit":
            continue
        dt = _iso_to_dt(data.get("timestamp"))
        if dt is None or dt < cutoff:
            continue
        summary = str(data.get("summary") or "")[:60]
        severity = str(data.get("severity") or "")
        kept.append((dt, severity, summary))
    if not kept:
        lines.extend(
            [f"{_DASHBOARD_BULLET}(no advisor insights in last 7 days)", ""]
        )
        return lines
    kept.sort(key=lambda row: row[0], reverse=True)
    for dt, severity, summary in kept[:5]:
        age = _age_from_dt(dt)
        sev = f"[{severity}] " if severity else ""
        lines.append(f"{_DASHBOARD_BULLET}✦ {sev}{summary} ({age})")
    lines.append("")
    return lines


def _section_downtime(project_path: Path) -> list[str]:
    """First 5 items from ``docs/downtime-backlog.md`` if present."""
    lines = [_dashboard_divider("Downtime backlog"), ""]
    backlog_path = project_path / "docs" / "downtime-backlog.md"
    if not backlog_path.exists():
        lines.extend([f"{_DASHBOARD_BULLET}(none queued)", ""])
        return lines
    try:
        raw = backlog_path.read_text(encoding="utf-8")
    except OSError:
        raw = ""
    entries: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        # Accept markdown bullets only — skip headings and prose.
        if stripped.startswith(("- ", "* ", "+ ")):
            entries.append(stripped[2:].strip())
        if len(entries) >= 5:
            break
    if not entries:
        lines.extend([f"{_DASHBOARD_BULLET}(none queued)", ""])
        return lines
    for e in entries:
        lines.append(f"{_DASHBOARD_BULLET}• {e[:60]}")
    lines.append("")
    return lines


def _section_quick_actions() -> list[str]:
    """Row of keyboard hotkey hints."""
    return [
        _dashboard_divider("Quick actions"),
        "",
        f"{_DASHBOARD_BULLET}n  new task    w  start worker    r  replan    "
        f"i  inbox    c  chat",
    ]


def _render_project_dashboard(
    project: object,
    project_key: str,
    config_path,
    supervisor,
) -> str | None:
    """Info-dense per-project dashboard (spec: #245).

    Sections (top to bottom): header, summary bar, velocity/cycle/tokens,
    "you need to" (approvals + alerts + pending insights), in-flight
    tasks, most-recent completion, 24h activity timeline, advisor
    insights (7d), downtime backlog, quick-action hotkeys.

    Each section is rendered by a dedicated ``_section_*`` helper that
    degrades gracefully on missing data so a fresh project with empty
    state still produces a readable surface.
    """
    from pollypm.work.sqlite_service import SQLiteWorkService

    db_path = project.path / ".pollypm" / "state.db"
    if not db_path.exists():
        return None

    # Single SQLite open per render — every downstream section reuses
    # this hydrated task list and the counts map.
    with SQLiteWorkService(db_path=db_path, project_path=project.path) as svc:
        counts = svc.state_counts(project=project_key)
        tasks = svc.list_tasks(project=project_key)

    tokens = _aggregate_project_tokens(db_path, project_key)

    name = getattr(project, "name", None) or project_key

    # Partition tasks for downstream sections.
    in_progress = [
        t for t in tasks if t.work_status.value == "in_progress"
    ]
    review = [t for t in tasks if t.work_status.value == "review"]
    completed = [t for t in tasks if t.work_status.value == "done"]
    completed.sort(
        key=lambda t: _iso_to_dt(t.updated_at) or 0,
        reverse=True,
    )

    # Project-scoped alerts, filtered the same way the legacy renderer did.
    project_alerts: list = []
    try:
        project_alerts = [
            a for a in supervisor.store.open_alerts()
            if any(
                l.session.project == project_key
                and l.session.name == a.session_name
                for l in supervisor.plan_launches()
            )
            and a.alert_type
            not in ("suspected_loop", "stabilize_failed", "needs_followup")
        ]
    except Exception:  # noqa: BLE001
        project_alerts = []

    try:
        system_events = supervisor.store.recent_events(limit=200)
    except Exception:  # noqa: BLE001
        system_events = []
    system_events = [
        e for e in system_events
        if any(
            l.session.project == project_key
            and l.session.name == getattr(e, "session_name", None)
            for l in (
                supervisor.plan_launches()
                if hasattr(supervisor, "plan_launches")
                else []
            )
        )
    ] if system_events else []

    presence = _worker_presence(supervisor, project_key)

    out: list[str] = [
        _section_header(name, presence),
        _DASHBOARD_BULLET + "─" * (_DASHBOARD_DIVIDER_WIDTH - 2),
        _section_summary(counts),
        "",
    ]
    velocity_lines = _section_velocity(tasks, tokens)
    if velocity_lines:
        out.extend(velocity_lines)
        out.append("")

    out.extend(_section_you_need_to(review, project_alerts, 0))
    out.extend(_section_in_flight(in_progress))
    out.extend(_section_recent(completed))
    out.extend(_section_activity(tasks, system_events))
    out.extend(_section_insights(project.path, project_key))
    out.extend(_section_downtime(project.path))
    out.extend(_section_quick_actions())

    return "\n".join(out)


@dataclass(slots=True)
class CockpitItem:
    key: str
    label: str
    state: str
    selectable: bool = True


def _selected_project_key(selected: object) -> str | None:
    """Extract the project key from the ``selected`` cockpit state."""
    if not isinstance(selected, str) or not selected.startswith("project:"):
        return None
    parts = selected.split(":", 2)
    if len(parts) < 2:
        return None
    return parts[1] or None


def _hidden_rail_items(config: object) -> frozenset[str]:
    """Return user-configured hidden rail item keys (``section.label``)."""
    rail_cfg = getattr(config, "rail", None)
    hidden = getattr(rail_cfg, "hidden_items", None) if rail_cfg is not None else None
    if not hidden:
        return frozenset()
    return frozenset(str(item) for item in hidden)


def _collapsed_rail_sections(config: object) -> frozenset[str]:
    """Return user-configured collapsed section names."""
    rail_cfg = getattr(config, "rail", None)
    collapsed = (
        getattr(rail_cfg, "collapsed_sections", None) if rail_cfg is not None else None
    )
    if not collapsed:
        return frozenset()
    return frozenset(str(item) for item in collapsed)


def _visibility_passes(reg, ctx) -> bool:
    """Evaluate the registration's visibility predicate.

    * ``"always"`` — always visible.
    * ``"has_feature"`` — visible only if ``ctx.extras["features"]``
      (a set/frozenset of capability names) includes
      ``reg.feature_name`` (or ``reg.item_key`` as fallback).
    * ``Callable`` — invoked; exceptions treat as hidden-and-logged.
    """
    import logging

    visibility = reg.visibility
    if visibility == "always":
        return True
    if visibility == "has_feature":
        features = ctx.extras.get("features") or frozenset()
        if not isinstance(features, (set, frozenset)):
            try:
                features = frozenset(features)
            except TypeError:
                return False
        target = reg.feature_name or reg.item_key
        return target in features
    if callable(visibility):
        try:
            return bool(visibility(ctx))
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception(
                "Rail item %s visibility predicate raised — hiding item",
                reg.item_key,
            )
            return False
    return True


def _rows_for_registration(reg, ctx) -> list:
    """Produce the list of :class:`RailRow` a registration renders to.

    Default: one row using the (possibly dynamic) label, icon, and
    state. When ``rows_provider`` is set we defer to the plugin —
    handy for sections like ``projects`` where one registration fans
    out into N rows.
    """
    import logging
    from pollypm.plugin_api.v1 import RailRow

    logger = logging.getLogger(__name__)

    if reg.rows_provider is not None:
        try:
            rows = reg.rows_provider(ctx)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Rail item %s rows_provider raised — skipping", reg.item_key,
            )
            return []
        return [r for r in rows if isinstance(r, RailRow)]

    label = reg.label
    if reg.label_provider is not None:
        try:
            dynamic_label = reg.label_provider(ctx)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Rail item %s label_provider raised — falling back to static",
                reg.item_key,
            )
            dynamic_label = None
        if isinstance(dynamic_label, str) and dynamic_label:
            label = dynamic_label

    state = "idle"
    if reg.state_provider is not None:
        try:
            dynamic_state = reg.state_provider(ctx)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Rail item %s state_provider raised — falling back to idle",
                reg.item_key,
            )
            dynamic_state = None
        if isinstance(dynamic_state, str) and dynamic_state:
            state = dynamic_state

    # Badge appended to label if provider returns a non-null value. The
    # badge-rendering tick is cheap; provider exceptions fall back to no
    # badge per er03 acceptance.
    if reg.badge_provider is not None:
        try:
            badge = reg.badge_provider(ctx)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Rail item %s badge_provider raised — rendering without badge",
                reg.item_key,
            )
            badge = None
        if badge not in (None, 0, ""):
            # Only append a badge when the label_provider hasn't already
            # baked the count in (e.g. "Inbox (3)" from core_rail_items).
            if f"({badge})" not in label:
                label = f"{label} ({badge})"

    key = reg.selection_key
    return [RailRow(key=key, label=label, state=state, selectable=True)]


class CockpitRouter:
    _STATE_FILE = "cockpit_state.json"
    _COCKPIT_WINDOW = "PollyPM"
    _LEFT_PANE_WIDTH = 30  # default; actual value persisted in cockpit state.

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.service = PollyPMService(config_path)
        self.tmux = create_tmux_client()
        self._supervisor = None
        # Per-project activity cache keyed by project key.
        # value: (db_mtime, git_mtime, is_active, has_working_task)
        # Skips re-opening SQLite on every 0.8s cockpit tick when nothing changed.
        self._project_activity_cache: dict[str, tuple[float, float, bool, bool]] = {}

    def _load_supervisor(self, *, fresh: bool = False):
        # Reload config if the file changed (picks up new projects, sessions, etc.)
        if not fresh and self._supervisor is not None:
            try:
                config_mtime = self.config_path.stat().st_mtime
                if not hasattr(self, "_config_mtime") or config_mtime != self._config_mtime:
                    fresh = True
                    self._config_mtime = config_mtime
            except OSError:
                pass
        if fresh or self._supervisor is None:
            if self._supervisor is not None:
                self._supervisor.store.close()
            self._supervisor = self.service.load_supervisor()
            self._supervisor.ensure_layout()
            try:
                self._config_mtime = self.config_path.stat().st_mtime
            except OSError:
                pass
            # Bump epoch so the cockpit TUI refreshes on next tick
            try:
                from pollypm.state_epoch import bump
                bump()
            except Exception:  # noqa: BLE001
                pass
        return self._supervisor

    def _state_path(self) -> Path:
        config = load_config(self.config_path)
        config.project.base_dir.mkdir(parents=True, exist_ok=True)
        return config.project.base_dir / self._STATE_FILE

    def selected_key(self) -> str:
        self._validate_state()
        data = self._load_state()
        value = data.get("selected")
        return str(value) if isinstance(value, str) and value else "polly"

    def set_selected_key(self, key: str) -> None:
        self._validate_state()
        data = self._load_state()
        data["selected"] = key
        self._write_state(data)

    def _load_state(self) -> dict[str, object]:
        path = self._state_path()
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text())
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_state(self, data: dict[str, object]) -> None:
        atomic_write_json(self._state_path(), data)

    def rail_width(self) -> int:
        """Return the persisted rail width, falling back to the default."""
        data = self._load_state()
        value = data.get("rail_width")
        if isinstance(value, int) and 20 <= value <= 120:
            return value
        return self._LEFT_PANE_WIDTH

    def set_rail_width(self, width: int) -> None:
        """Persist the rail width so subsequent launches and layout checks use it."""
        if not isinstance(width, int) or width < 20 or width > 120:
            return
        data = self._load_state()
        if data.get("rail_width") == width:
            return
        data["rail_width"] = width
        self._write_state(data)

    def _validate_state(self, *, panes: list | None = None, target: str | None = None) -> list:
        """Clear stale entries from cockpit_state.json.

        Checks that right_pane_id points to a real pane and that
        mounted_session is actually alive. Prevents stale state from
        blocking heartbeat recovery or causing wrong session mounts.

        Returns the list of panes fetched (or the list passed in) so
        callers can avoid re-issuing ``list_panes`` — see #175.
        """
        state = self._load_state()
        dirty = False
        if target is None:
            config = load_config(self.config_path)
            target = f"{config.project.tmux_session}:{self._COCKPIT_WINDOW}"
        if panes is None:
            panes = self._safe_list_panes(target)

        right_pane_id = state.get("right_pane_id")
        right_pane = None
        if isinstance(right_pane_id, str) and right_pane_id:
            right_pane = next((pane for pane in panes if pane.pane_id == right_pane_id), None)
            if right_pane is None or getattr(right_pane, "pane_dead", False):
                state.pop("right_pane_id", None)
                state.pop("mounted_session", None)
                dirty = True
                right_pane = None

        mounted = state.get("mounted_session")
        if isinstance(mounted, str) and mounted:
            release_lease = False
            try:
                supervisor = self._load_supervisor()
                launches = supervisor.plan_launches()
                launch = next((l for l in launches if l.session.name == mounted), None)
                if launch is None or not self._mounted_session_matches_pane(launch, right_pane):
                    state.pop("mounted_session", None)
                    dirty = True
                    release_lease = True
            except Exception:  # noqa: BLE001
                state.pop("mounted_session", None)
                dirty = True
                release_lease = True
            if release_lease:
                self._release_cockpit_lease(supervisor if "supervisor" in locals() else None, mounted)

        if dirty:
            self._write_state(state)
        return panes

    def _safe_list_panes(self, target: str) -> list:
        try:
            return self.tmux.list_panes(target)
        except Exception:  # noqa: BLE001
            return []

    def _mounted_session_matches_pane(self, launch, pane) -> bool:
        if pane is None or getattr(pane, "pane_dead", False):
            return False
        if not self._is_live_provider_pane(pane):
            return False
        # A live provider pane is running — trust the state rather than
        # trying to match CWD.  CWD matching is unreliable because the
        # agent may cd elsewhere during its turn.  If state says this
        # session is mounted and the pane is alive, believe it.
        return True

    def _release_cockpit_lease(self, supervisor, session_name: str) -> None:
        if supervisor is None:
            try:
                supervisor = self._load_supervisor()
            except Exception:  # noqa: BLE001
                return
        try:
            supervisor.release_lease(session_name, expected_owner="cockpit")
        except Exception:  # noqa: BLE001
            pass

    def _ui_initialized_sessions(self) -> set[str]:
        data = self._load_state()
        value = data.get("ui_initialized_sessions")
        if not isinstance(value, list):
            return set()
        return {item for item in value if isinstance(item, str) and item}

    def _mark_ui_initialized(self, session_name: str) -> None:
        data = self._load_state()
        current = data.get("ui_initialized_sessions")
        items = [item for item in current if isinstance(item, str) and item] if isinstance(current, list) else []
        if session_name not in items:
            items.append(session_name)
        data["ui_initialized_sessions"] = items
        self._write_state(data)

    def build_items(self, *, spinner_index: int = 0) -> list[CockpitItem]:
        """Build rail rows by walking the plugin-host rail registry.

        Rows are gathered in section order (``top`` → ``projects`` →
        ``workflows`` → ``tools`` → ``system``), then within each
        section by ``(index, plugin_name)``. Items that declare a
        ``rows_provider`` expand into N rows; others collapse into a
        single :class:`CockpitItem` built from the static label plus
        optional ``label_provider`` / ``state_provider`` callables.

        Pre-er02 behaviour is preserved by the built-in
        ``core_rail_items`` plugin, which registers every rail entry
        that used to be hardcoded here.
        """
        from pollypm.plugin_api.v1 import RailContext, RAIL_SECTIONS

        supervisor = self._load_supervisor()
        config = supervisor.config
        launches, windows, alerts, _leases, _errors = supervisor.status()

        cockpit_state = self._load_state()
        ctx = RailContext(
            selected_project=_selected_project_key(cockpit_state.get("selected")),
            cockpit_state=dict(cockpit_state),
            extras={
                "router": self,
                "supervisor": supervisor,
                "config": config,
                "launches": launches,
                "windows": windows,
                "alerts": alerts,
                "spinner_index": spinner_index,
            },
        )

        registry = self._rail_registry()

        # Hidden items + visibility predicates land in er03 / er04. The
        # renderer here runs them every tick — badge providers likewise
        # (a crash falls back to no-badge).
        hidden_keys = _hidden_rail_items(config)
        collapsed_sections = _collapsed_rail_sections(config)

        grouped: dict[str, list[CockpitItem]] = {name: [] for name in RAIL_SECTIONS}

        for reg in registry.items():
            if reg.item_key in hidden_keys:
                continue
            if not _visibility_passes(reg, ctx):
                continue
            rows = _rows_for_registration(reg, ctx)
            for row in rows:
                item = CockpitItem(
                    key=row.key,
                    label=row.label,
                    state=row.state,
                    selectable=row.selectable,
                )
                grouped.setdefault(reg.section, []).append(item)

        items: list[CockpitItem] = []
        for section in RAIL_SECTIONS:
            rows = grouped.get(section) or []
            if not rows:
                continue
            if section in collapsed_sections:
                # Collapsed sections render as a disabled header row so
                # the user can still see the section exists. Expansion
                # is a runtime concept tracked via set_selected_key.
                items.append(
                    CockpitItem(
                        key=f"_section:{section}",
                        label=f"{section.upper()} (collapsed)",
                        state="separator",
                        selectable=False,
                    )
                )
                continue
            items.extend(rows)

        return items

    def _rail_registry(self):
        """Return the plugin-host rail registry for the active root dir."""
        from pollypm.plugin_host import extension_host_for_root

        config = load_config(self.config_path)
        host = extension_host_for_root(str(config.project.root_dir.resolve()))
        # Initialize plugins so the rail registry is populated. Safe to
        # call repeatedly — it tracks which plugins have been init'd.
        try:
            host.initialize_plugins(config=config)
        except Exception:  # noqa: BLE001
            # Plugin init failures surface via degraded_plugins; don't
            # block the rail rendering.
            pass
        registry = host.rail_registry()
        # Worker roster top-rail entry. Registered here (not in
        # ``core_rail_items``) so the cockpit router + roster Textual
        # app land in one feature drop — the rail row, the route handler
        # (``route_selected("workers")``), and the panel renderer all
        # live alongside each other. The registration is gated on
        # ``core_rail_items`` being active so disabling the core plugin
        # still yields an empty rail (see
        # ``test_removing_core_rail_items_yields_empty_rail``).
        try:
            core_enabled = "core_rail_items" in host.plugins()
        except Exception:  # noqa: BLE001
            core_enabled = True
        if core_enabled:
            _register_worker_roster_rail_item(registry, self)
            _register_metrics_rail_item(registry, self)
        return registry

    def _project_session_map(self, launches) -> dict[str, str]:
        project_session_map: dict[str, str] = {}
        for launch in launches:
            if launch.session.role in {"operator-pm", "heartbeat-supervisor", "triage", "reviewer"}:
                continue
            project_session_map.setdefault(launch.session.project, launch.session.name)
        return project_session_map

    # Alert types that are informational / auto-managed — don't show red triangle
    _SILENT_ALERT_TYPES = frozenset({
        "suspected_loop",      # auto-clears when snapshot changes
        "stabilize_failed",    # stale after successful recovery
        "needs_followup",      # informational, handled by heartbeat
    })

    def _session_state(self, session_name: str, launches, windows, alerts, spinner_index: int) -> str:
        actionable = [
            a for a in alerts
            if a.session_name == session_name and a.alert_type not in self._SILENT_ALERT_TYPES
        ]
        if actionable:
            # Include a short reason so the user knows what's wrong
            top = actionable[0]
            short_reason = top.alert_type.replace("_", " ")
            return f"! {short_reason}"
        launch = next((item for item in launches if item.session.name == session_name), None)
        if launch is None:
            return "idle"
        window_map = {window.name: window for window in windows}
        window = window_map.get(launch.window_name)
        # If the session is mounted in the cockpit, its storage window is gone.
        # Check the cockpit right pane instead.
        if window is None:
            state = self._load_state()
            if state.get("mounted_session") == session_name:
                window = self._mounted_window_proxy(launch, windows)
        if window is None:
            return "idle"
        if window.pane_dead:
            return "dead"
        spinners = ["\u25dc", "\u25dd", "\u25de", "\u25df"]
        if launch.session.role in ("worker", "operator-pm", "reviewer"):
            working = self._is_pane_working(window, launch.session.provider)
            if working:
                return spinners[spinner_index % 4] + " working"
            if launch.session.role == "worker":
                return "\u25cf live"
            return "ready"
        if launch.session.role == "heartbeat-supervisor":
            return "watch"
        if launch.session.role == "triage":
            return "triage"
        return "live"

    def _mounted_window_proxy(self, launch, windows):
        """Return a window-like object for a session mounted in the cockpit pane."""
        cockpit_windows = [w for w in windows if w.name == self._COCKPIT_WINDOW]
        if not cockpit_windows:
            return None
        # The cockpit window has multiple panes; the right pane is the mounted session.
        try:
            supervisor = self._load_supervisor()
            target = f"{supervisor.config.project.tmux_session}:{self._COCKPIT_WINDOW}"
            panes = self.tmux.list_panes(target)
            if len(panes) < 2:
                return None
            right_pane = max(panes, key=self._pane_left)
            # Return the cockpit window but with the right pane's info
            cockpit_win = cockpit_windows[0]
            from dataclasses import replace as dc_replace
            return dc_replace(
                cockpit_win,
                pane_id=right_pane.pane_id,
                pane_current_command=right_pane.pane_current_command,
                pane_dead=right_pane.pane_dead,
            )
        except Exception:  # noqa: BLE001
            return None

    def _is_pane_working(self, window, provider) -> bool:
        """Check if a session pane has an active turn (agent is working, not idle at prompt)."""
        try:
            pane_text = self.tmux.capture_pane(window.pane_id, lines=15)
        except Exception:  # noqa: BLE001
            return False
        stripped = pane_text.rstrip()
        if not stripped:
            return False
        tail = stripped[-200:]
        lowered = tail.lower()
        # Universal working indicator — both Claude and Codex show this during active turns
        if "esc to interrupt" in lowered:
            return True
        # Universal idle indicators — if any are present, the session is idle regardless of provider
        idle_markers = (
            "bypass permissions", "new task?", "/clear to save", "shift+tab to cycle",  # Claude
            "press enter to confirm", "% left",  # Codex
        )
        if any(marker in lowered for marker in idle_markers):
            return False
        # Provider-specific prompt detection
        provider_value = provider.value if hasattr(provider, "value") else str(provider)
        if provider_value == "claude":
            return "\u276f" not in tail
        if provider_value == "codex":
            # Codex idle: › prompt
            if "\u203a" in tail:
                return False
            return bool(stripped)
        return False

    def _cleanup_duplicate_windows(self, storage_session: str) -> None:
        """Kill duplicate windows in the storage closet, keeping only the first of each name."""
        try:
            windows = self.tmux.list_windows(storage_session)
        except Exception:  # noqa: BLE001
            return
        seen: dict[str, int] = {}  # name -> first index
        for window in windows:
            if window.name in seen:
                try:
                    self.tmux.kill_window(f"{storage_session}:{window.index}")
                except Exception:  # noqa: BLE001
                    pass
            else:
                seen[window.name] = window.index

    def ensure_cockpit_layout(self) -> None:
        config = load_config(self.config_path)
        target = f"{config.project.tmux_session}:{self._COCKPIT_WINDOW}"
        # Single list-panes baseline shared with ``_validate_state``. See
        # #175: subsequent list-panes calls only run after a mutation that
        # invalidates the cached view (split/kill); pure swaps preserve
        # pane IDs so we update order locally.
        panes = self._safe_list_panes(target)
        self._validate_state(panes=panes, target=target)
        # Clean up duplicate windows in the storage closet before layout setup
        try:
            supervisor = self._load_supervisor()
            self._cleanup_duplicate_windows(supervisor.storage_closet_session_name())
        except Exception:  # noqa: BLE001
            pass
        state = self._load_state()
        right_pane_id = state.get("right_pane_id")
        right_pane_present = isinstance(right_pane_id, str) and any(pane.pane_id == right_pane_id for pane in panes)
        if len(panes) == 1 and right_pane_present and panes[0].pane_id == right_pane_id:
            # The rail (left) pane died, only the worker (right) pane survived.
            # Park the worker back to storage and clear stale state so the
            # split below creates a fresh right pane from the cockpit pane.
            supervisor = self._load_supervisor()
            mounted = state.get("mounted_session")
            if isinstance(mounted, str) and mounted:
                launch = next(
                    (item for item in supervisor.plan_launches() if item.session.name == mounted),
                    None,
                )
                storage_session = supervisor.storage_closet_session_name()
                if launch is not None and self.tmux.has_session(storage_session):
                    try:
                        self.tmux.break_pane(panes[0].pane_id, storage_session, launch.window_name)
                    except Exception:  # noqa: BLE001
                        pass
            state.pop("right_pane_id", None)
            state.pop("mounted_session", None)
            self._write_state(state)
            try:
                panes = self.tmux.list_panes(target)  # structural change (break_pane)
            except Exception:  # noqa: BLE001
                panes = []
            right_pane_id = None
            right_pane_present = False
        if len(panes) < 2:
            # Calculate right pane size so the rail starts at exactly rail_width
            # columns — avoids the visible flash of a 50/50 split followed by resize.
            window_width = panes[0].pane_width if panes else 200
            right_size = max(window_width - self.rail_width() - 1, 40)
            right_pane_id = self.tmux.split_window(
                target,
                self._right_pane_command("polly"),
                horizontal=True,
                detached=True,
                size=right_size,
            )
            state["right_pane_id"] = right_pane_id
            self._write_state(state)
            panes = self.tmux.list_panes(target)  # split added a pane
        elif len(panes) > 2:
            for pane in panes:
                if pane.pane_id == panes[0].pane_id:
                    continue
                try:
                    self.tmux.kill_pane(pane.pane_id)
                except Exception:  # noqa: BLE001
                    pass
            panes = self.tmux.list_panes(target)  # kill_pane removed panes
        if len(panes) >= 2:
            # ``_normalize_layout`` may swap pane positions but never changes
            # pane IDs or count — so we can reason about the post-swap left/
            # right locally without another ``list-panes`` round-trip.
            self._normalize_layout(target, panes)
            left_pane, right_pane = self._post_normalize_lr(panes)
            state["right_pane_id"] = right_pane.pane_id
            self._write_state(state)
            self._try_resize_rail(left_pane.pane_id)

    def _post_normalize_lr(self, panes):
        """Return ``(left, right)`` for the panes after ``_normalize_layout``.

        ``_normalize_layout`` guarantees the ``uv`` (rail) pane ends up on
        the left. When neither pane is ``uv`` (edge case), fall back to the
        pre-swap ``pane_left`` ordering — the same answer the old code would
        have computed via a second ``list-panes`` + ``min/max(pane_left)``.
        """
        if len(panes) != 2:
            left = min(panes, key=self._pane_left)
            right = max(panes, key=self._pane_left)
            return left, right
        a, b = panes
        a_cmd = getattr(a, "pane_current_command", "")
        b_cmd = getattr(b, "pane_current_command", "")
        if a_cmd == "uv":
            return a, b
        if b_cmd == "uv":
            return b, a
        left = min(panes, key=self._pane_left)
        right = max(panes, key=self._pane_left)
        return left, right

    def _pane_left(self, pane) -> int:
        return int(getattr(pane, "pane_left", 0))

    def _try_resize_rail(self, pane_id: str) -> None:
        """Best-effort resize of the rail pane. Never raises."""
        try:
            self.tmux.resize_pane_width(pane_id, self.rail_width())
        except Exception:  # noqa: BLE001
            pass

    def _right_pane_size(self, window_target: str) -> int | None:
        """Calculate the exact right-pane size so the rail starts at rail_width columns."""
        try:
            panes = self.tmux.list_panes(window_target)
            if panes:
                window_width = max(p.pane_width for p in panes)
                return max(window_width - self.rail_width() - 1, 40)
        except Exception:  # noqa: BLE001
            pass
        return None

    def _normalize_layout(self, target: str, panes) -> None:
        if len(panes) != 2:
            return
        left_pane = min(panes, key=self._pane_left)
        right_pane = max(panes, key=self._pane_left)
        left_command = getattr(left_pane, "pane_current_command", "")
        right_command = getattr(right_pane, "pane_current_command", "")
        if left_command == "uv":
            return
        if right_command == "uv":
            self.tmux.swap_pane(right_pane.pane_id, left_pane.pane_id)

    def _right_pane_id(self, target: str) -> str | None:
        panes = self.tmux.list_panes(target)
        if len(panes) < 2:
            return None
        return max(panes, key=self._pane_left).pane_id

    def _left_pane_id(self, target: str) -> str | None:
        panes = self.tmux.list_panes(target)
        if not panes:
            return None
        return min(panes, key=self._pane_left).pane_id

    def route_selected(self, key: str) -> None:
        supervisor = self._load_supervisor()
        window_target = f"{supervisor.config.project.tmux_session}:{self._COCKPIT_WINDOW}"
        self.ensure_cockpit_layout()
        right_pane = self._right_pane_id(window_target)
        if right_pane is None:
            raise RuntimeError("Cockpit right pane is not available.")

        self.set_selected_key(key)
        if key == "polly":
            # Launch operator session if not running
            launches = supervisor.plan_launches()
            storage_session = supervisor.storage_closet_session_name()
            op_launch = next((l for l in launches if l.session.name == "operator"), None)
            if op_launch is not None:
                storage_windows = {w.name for w in self.tmux.list_windows(storage_session)}
                if op_launch.window_name not in storage_windows:
                    try:
                        supervisor.launch_session("operator")
                    except Exception:
                        pass
            try:
                self._show_live_session(supervisor, "operator", window_target)
            except Exception:
                self._show_static_view(supervisor, window_target, "polly")
            return
        if key == "russell":
            # Launch reviewer session if not running
            launches = supervisor.plan_launches()
            storage_session = supervisor.storage_closet_session_name()
            rev_launch = next((l for l in launches if l.session.name == "reviewer"), None)
            if rev_launch is not None:
                storage_windows = {w.name for w in self.tmux.list_windows(storage_session)}
                if rev_launch.window_name not in storage_windows:
                    try:
                        supervisor.launch_session("reviewer")
                    except Exception:
                        pass
            try:
                self._show_live_session(supervisor, "reviewer", window_target)
            except Exception:
                self._show_static_view(supervisor, window_target, "polly")
            return
        if key == "inbox":
            self._show_static_view(supervisor, window_target, "inbox")
            return
        if key == "workers":
            self._show_static_view(supervisor, window_target, "workers")
            return
        if key == "metrics":
            self._show_static_view(supervisor, window_target, "metrics")
            return
        if key == "settings":
            self._show_static_view(supervisor, window_target, "settings")
            return
        if key == "activity" or key.startswith("activity:"):
            # Live activity feed — served through the standard static-view
            # plumbing. ``activity:<project_key>`` preloads the per-project
            # filter so the dashboard's ``l`` keybinding lands the user
            # exactly where they were looking.
            project_key = None
            if key.startswith("activity:"):
                _, _, project_key = key.partition(":")
                project_key = project_key or None
            self._show_static_view(
                supervisor, window_target, "activity", project_key,
            )
            return
        if key.startswith("project:"):
            parts = key.split(":")
            project_key = parts[1]
            sub_view = parts[2] if len(parts) > 2 else None
            if sub_view is None or sub_view == "dashboard":
                # Clicking project parent or Dashboard — show dashboard,
                # and set selection to the dashboard sub-item
                self.set_selected_key(f"project:{project_key}:dashboard")
                self._show_static_view(supervisor, window_target, "project", project_key)
                return
            if sub_view in ("settings", "issues"):
                self._show_static_view(supervisor, window_target, sub_view, project_key)
                return
            if sub_view == "task" and len(parts) > 3:
                # Per-task worker session — mount it from storage closet
                task_num = parts[3]
                window_name = f"task-{project_key}-{task_num}"
                storage = supervisor.storage_closet_session_name()
                try:
                    storage_windows = self.tmux.list_windows(storage)
                    target_win = next((w for w in storage_windows if w.name == window_name), None)
                    if target_win is not None:
                        self._park_mounted_session(supervisor, window_target)
                        self._cleanup_extra_panes(window_target)
                        left_pane = self._left_pane_id(window_target)
                        right_pane_id = self._right_pane_id(window_target)
                        if right_pane_id is not None:
                            self.tmux.kill_pane(right_pane_id)
                        source = f"{storage}:{target_win.index}.0"
                        self.tmux.join_pane(source, left_pane, horizontal=True)
                        panes = self.tmux.list_panes(window_target)
                        left_p = min(panes, key=self._pane_left)
                        self._try_resize_rail(left_p.pane_id)
                        right_p = max(panes, key=self._pane_left)
                        self.tmux.set_pane_history_limit(right_p.pane_id, 200)
                        state = self._load_state()
                        state["mounted_session"] = window_name
                        state["right_pane_id"] = right_p.pane_id
                        self._write_state(state)
                        return
                except Exception:  # noqa: BLE001
                    pass
                # Fallback to dashboard
                self.set_selected_key(f"project:{project_key}:dashboard")
                self._show_static_view(supervisor, window_target, "project", project_key)
                return
            if sub_view == "session":
                # PM Chat — mount live session, spawning if needed
                launches = supervisor.plan_launches()
                session_name = self._project_session_map(launches).get(project_key)
                if session_name is not None:
                    if not self._session_available_for_mount(supervisor, session_name, window_target):
                        # Session exists in config but not running — launch it
                        try:
                            supervisor.launch_session(session_name)
                        except Exception:
                            pass
                    try:
                        self._show_live_session(supervisor, session_name, window_target)
                    except Exception:
                        # Mount failed (e.g. duplicate windows) — fall back to dashboard
                        self.set_selected_key(f"project:{project_key}:dashboard")
                        self._show_static_view(supervisor, window_target, "project", project_key)
                else:
                    # No session configured — try to create one
                    try:
                        self.create_worker_and_route(project_key)
                    except Exception:
                        # Creation failed (e.g. no git commits) — fall back to dashboard
                        self.set_selected_key(f"project:{project_key}:dashboard")
                        self._show_static_view(supervisor, window_target, "project", project_key)
                return
            # Unknown sub_view — fall back to dashboard
            self.set_selected_key(f"project:{project_key}:dashboard")
            self._show_static_view(supervisor, window_target, "project", project_key)
            return
        raise RuntimeError(f"Unknown cockpit item: {key}")

    def focus_right_pane(self) -> None:
        config = load_config(self.config_path)
        window_target = f"{config.project.tmux_session}:{self._COCKPIT_WINDOW}"
        self.ensure_cockpit_layout()
        right_pane = self._right_pane_id(window_target)
        if right_pane is not None:
            self.tmux.select_pane(right_pane)

    def create_worker_and_route(
        self,
        project_key: str,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        supervisor = self._load_supervisor()
        launches = supervisor.plan_launches()
        session_name = self._project_session_map(launches).get(project_key)
        storage_session = supervisor.storage_closet_session_name()
        storage_windows = {w.name for w in self.tmux.list_windows(storage_session)}

        target: str | None = None
        if session_name is not None:
            launch = next(l for l in launches if l.session.name == session_name)
            if launch.window_name not in storage_windows:
                _launch, target = supervisor.create_session_window(session_name, on_status=on_status)
        else:
            prompt = self.service.suggest_worker_prompt(project_key=project_key)
            self.service.create_and_launch_worker(
                project_key=project_key, prompt=prompt, on_status=on_status, skip_stabilize=True,
            )
            # Re-read launches to pick up the newly created session
            supervisor = self._load_supervisor(fresh=True)
            launches = supervisor.plan_launches()
            session_name = self._project_session_map(launches).get(project_key)
            if session_name is not None:
                launch = next(l for l in launches if l.session.name == session_name)
                tmux_session = supervisor.tmux_session_for_launch(launch)
                window_map = supervisor.window_map()
                if launch.window_name in window_map:
                    target_key = f"{tmux_session}:{launch.window_name}"
                    # Window exists but hasn't been stabilized yet
                    target = target_key

        # Route immediately so the user sees the session booting live
        self.route_selected(f"project:{project_key}")

        # Stabilize in the background (dismisses prompts, waits for ready)
        if target is not None and session_name is not None:
            launch = next(l for l in supervisor.plan_launches() if l.session.name == session_name)
            supervisor.stabilize_launch(launch, target, on_status=on_status)

    def _show_live_session(self, supervisor, session_name: str, window_target: str) -> None:
        mounted_session = self._mounted_session_name(supervisor, window_target)
        launch = next(item for item in supervisor.plan_launches() if item.session.name == session_name)
        if isinstance(mounted_session, str) and mounted_session == session_name:
            right_pane_id = self._right_pane_id(window_target)
            if right_pane_id is not None:
                panes = self.tmux.list_panes(window_target)
                right_pane = max(panes, key=self._pane_left)
                if self._is_live_provider_pane(right_pane):
                    return
        self._park_mounted_session(supervisor, window_target)
        self._cleanup_extra_panes(window_target)
        left_pane_id = self._left_pane_id(window_target)
        if left_pane_id is None:
            raise RuntimeError("Cockpit left pane is not available.")
        right_pane_id = self._right_pane_id(window_target)
        storage_session = supervisor.storage_closet_session_name()
        storage_windows = {window.name for window in self.tmux.list_windows(storage_session)}
        if launch.window_name not in storage_windows:
            # Control sessions (operator, reviewer) should be respawned
            # when missing — the user clicking on Polly expects to talk to
            # Polly, not see a placeholder.
            if launch.session.role in {"operator-pm", "reviewer"}:
                try:
                    supervisor.launch_session(session_name)
                    storage_windows = {w.name for w in self.tmux.list_windows(storage_session)}
                    if launch.window_name in storage_windows:
                        if right_pane_id is not None:
                            self.tmux.kill_pane(right_pane_id)
                        source = f"{storage_session}:{launch.window_name}.0"
                        self.tmux.join_pane(source, left_pane_id, horizontal=True)
                        panes = self.tmux.list_panes(window_target)
                        left_pane = min(panes, key=self._pane_left)
                        self._try_resize_rail(left_pane.pane_id)
                        right_pane = max(panes, key=self._pane_left)
                        self.tmux.set_pane_history_limit(right_pane.pane_id, 200)
                        state = self._load_state()
                        state["mounted_session"] = session_name
                        state["right_pane_id"] = right_pane.pane_id
                        self._write_state(state)
                        return
                except Exception:  # noqa: BLE001
                    pass  # Fall through to static view if relaunch fails
            # Non-control sessions or failed relaunch — show static view
            fallback_kind = "polly" if session_name == "operator" else "project"
            fallback_target = launch.session.project if fallback_kind == "project" else None
            if right_pane_id is None:
                right_size = self._right_pane_size(window_target)
                right_pane_id = self.tmux.split_window(
                    left_pane_id,
                    self._right_pane_command(fallback_kind, fallback_target),
                    horizontal=True,
                    detached=True,
                    size=right_size,
                )
            else:
                self.tmux.respawn_pane(right_pane_id, self._right_pane_command(fallback_kind, fallback_target))
            state = self._load_state()
            state.pop("mounted_session", None)
            state["right_pane_id"] = self._right_pane_id(window_target)
            self._write_state(state)
            return
        if right_pane_id is not None:
            self.tmux.kill_pane(right_pane_id)
        # Use window index to avoid ambiguity with duplicate window names
        storage_windows = self.tmux.list_windows(storage_session)
        target_window = next(
            (w for w in storage_windows if w.name == launch.window_name),
            None,
        )
        if target_window is None:
            self._show_static_view(supervisor, window_target, "polly" if session_name == "operator" else "project")
            return
        source = f"{storage_session}:{target_window.index}.0"
        self.tmux.join_pane(source, left_pane_id, horizontal=True)
        panes = self.tmux.list_panes(window_target)
        left_pane = min(panes, key=self._pane_left)
        self._try_resize_rail(left_pane.pane_id)
        right_pane = max(panes, key=self._pane_left)
        self.tmux.set_pane_history_limit(right_pane.pane_id, 200)
        state = self._load_state()
        state["mounted_session"] = session_name
        state["right_pane_id"] = right_pane.pane_id
        self._write_state(state)
        # Auto-claim a cockpit lease so the heartbeat won't send
        # nudges while a human is viewing/typing in this session.
        try:
            supervisor.claim_lease(session_name, "cockpit", "mounted in cockpit")
        except Exception:  # noqa: BLE001
            pass  # Lease may conflict — best effort

    def _should_boot_visible(self, launch) -> bool:
        if launch.session.name in self._ui_initialized_sessions():
            return False
        return launch.session.provider.value in {"claude", "codex"}

    def _launch_visible_session(self, supervisor, launch, window_target: str, left_pane_id: str, right_pane_id: str | None):
        storage_session = supervisor.storage_closet_session_name()
        for window in self.tmux.list_windows(storage_session):
            if window.name == launch.window_name:
                self.tmux.kill_window(f"{storage_session}:{window.index}")
                break
        visible_launch = launch
        if launch.session.provider.value == "codex" and launch.initial_input:
            provider = get_provider(launch.session.provider, root_dir=supervisor.config.project.root_dir)
            runtime = get_runtime(launch.account.runtime, root_dir=supervisor.config.project.root_dir)
            visible_command = provider.build_launch_command(launch.session, launch.account)
            visible_launch = replace(
                launch,
                command=runtime.wrap_command(visible_command, launch.account, supervisor.config.project),
                initial_input=visible_command.initial_input,
                resume_marker=visible_command.resume_marker,
                fresh_launch_marker=visible_command.fresh_launch_marker,
            )
        if right_pane_id is not None:
            self.tmux.kill_pane(right_pane_id)
        right_size = self._right_pane_size(window_target)
        right_pane_id = self.tmux.split_window(
            left_pane_id,
            visible_launch.command,
            horizontal=True,
            detached=False,
            size=right_size,
        )
        self.tmux.set_pane_history_limit(right_pane_id, 200)
        self.tmux.pipe_pane(right_pane_id, visible_launch.log_path)
        supervisor.stabilize_launch(visible_launch, right_pane_id)
        return max(self.tmux.list_panes(window_target), key=self._pane_left)

    def _park_mounted_session(self, supervisor, window_target: str) -> None:
        state = self._load_state()
        mounted_session = self._mounted_session_name(supervisor, window_target)
        if not isinstance(mounted_session, str) or not mounted_session:
            return
        right_pane_id = self._right_pane_id(window_target)
        if right_pane_id is None:
            state.pop("mounted_session", None)
            self._write_state(state)
            self._release_cockpit_lease(supervisor, mounted_session)
            return
        right_pane = max(self.tmux.list_panes(window_target), key=self._pane_left)
        if not self._is_live_provider_pane(right_pane):
            state.pop("mounted_session", None)
            self._write_state(state)
            self._release_cockpit_lease(supervisor, mounted_session)
            return
        launch = next(item for item in supervisor.plan_launches() if item.session.name == mounted_session)
        storage_session = supervisor.storage_closet_session_name()
        before = {(window.index, window.name) for window in self.tmux.list_windows(storage_session)}
        self.tmux.break_pane(right_pane_id, storage_session, launch.window_name)
        after = self.tmux.list_windows(storage_session)
        created = [window for window in after if (window.index, window.name) not in before]
        if created:
            self.tmux.rename_window(f"{storage_session}:{created[-1].index}", launch.window_name)
        else:
            for window in after:
                if window.name == self._COCKPIT_WINDOW:
                    self.tmux.rename_window(f"{storage_session}:{window.index}", launch.window_name)
                    break
        state.pop("mounted_session", None)
        self._write_state(state)
        self._release_cockpit_lease(supervisor, mounted_session)

    # Roles that should NEVER be auto-detected as mounted via CWD fallback.
    # These are background roles — if the user is looking at a pane, it's
    # not the heartbeat.  Guessing wrong here causes cascading mis-parks.
    _NEVER_MOUNT_ROLES = frozenset({"heartbeat-supervisor", "triage"})

    # When CWD is ambiguous (multiple sessions share the same cwd), prefer
    # the session the user is most likely interacting with.
    _MOUNT_PRIORITY = {"operator-pm": 0, "reviewer": 1, "worker": 2}

    def _mounted_session_name(self, supervisor, window_target: str) -> str | None:
        state = self._load_state()
        mounted_session = state.get("mounted_session")
        if isinstance(mounted_session, str) and mounted_session:
            return mounted_session
        if not hasattr(self.tmux, "list_panes"):
            return None
        right_pane_id = self._right_pane_id(window_target)
        if right_pane_id is None:
            return None
        panes = self.tmux.list_panes(window_target)
        if len(panes) < 2:
            return None
        right_pane = max(panes, key=self._pane_left)
        if not self._is_live_provider_pane(right_pane):
            return None
        # CWD fallback: find the best matching session, but NEVER guess
        # heartbeat or triage — those are background roles that should
        # never be mounted in the cockpit.  When multiple sessions share
        # a CWD, prefer operator > reviewer > worker.
        pane_path = str(Path(right_pane.pane_current_path).resolve())
        best_match: tuple[int, str] | None = None  # (priority, session_name)
        for launch in supervisor.plan_launches():
            if launch.session.role in self._NEVER_MOUNT_ROLES:
                continue
            session_cwd = str(Path(launch.session.cwd).resolve())
            if pane_path == session_cwd:
                priority = self._MOUNT_PRIORITY.get(launch.session.role, 5)
                if best_match is None or priority < best_match[0]:
                    best_match = (priority, launch.session.name)
        if best_match is not None:
            state["mounted_session"] = best_match[1]
            state["right_pane_id"] = right_pane.pane_id
            self._write_state(state)
            return best_match[1]
        return None

    def _is_live_provider_pane(self, pane) -> bool:
        cmd = getattr(pane, "pane_current_command", "")
        # Claude Code may report the version string (e.g. "2.1.98") as the
        # current command instead of "claude" or "node".
        if cmd in {"node", "claude", "codex"}:
            return True
        # Treat any version-like string (digits and dots) as a live Claude pane.
        if cmd and all(c.isdigit() or c == "." for c in cmd):
            return True
        return False

    def _session_available_for_mount(self, supervisor, session_name: str, window_target: str) -> bool:
        """Return True only if the session is already running (mounted or in storage)."""
        mounted = self._mounted_session_name(supervisor, window_target)
        if mounted == session_name:
            return True
        launch = next((item for item in supervisor.plan_launches() if item.session.name == session_name), None)
        if launch is None:
            return False
        storage_session = supervisor.storage_closet_session_name()
        storage_windows = {window.name for window in self.tmux.list_windows(storage_session)}
        return launch.window_name in storage_windows

    def _show_static_view(
        self,
        supervisor,
        window_target: str,
        kind: str,
        project_key: str | None = None,
    ) -> None:
        self._park_mounted_session(supervisor, window_target)
        self._cleanup_extra_panes(window_target)
        left_pane_id = self._left_pane_id(window_target)
        if left_pane_id is None:
            raise RuntimeError("Cockpit left pane is not available.")
        right_pane_id = self._right_pane_id(window_target)
        if right_pane_id is None:
            right_pane_id = self.tmux.split_window(
                left_pane_id,
                self._right_pane_command(kind, project_key),
                horizontal=True,
                detached=True,
                size=self._right_pane_size(window_target),
            )
        else:
            self.tmux.respawn_pane(right_pane_id, self._right_pane_command(kind, project_key))
        state = self._load_state()
        state.pop("mounted_session", None)
        state["right_pane_id"] = self._right_pane_id(window_target)
        self._write_state(state)

    def _cleanup_extra_panes(self, window_target: str) -> None:
        """Kill any extra panes beyond the expected 2 (rail + right)."""
        try:
            panes = self.tmux.list_panes(window_target)
        except Exception:  # noqa: BLE001
            return
        if len(panes) <= 2:
            return
        left_pane = min(panes, key=self._pane_left)
        # Keep the leftmost (rail) and rightmost (content) panes, kill the rest
        right_pane = max(panes, key=self._pane_left)
        for pane in panes:
            if pane.pane_id not in {left_pane.pane_id, right_pane.pane_id}:
                try:
                    self.tmux.kill_pane(pane.pane_id)
                except Exception:  # noqa: BLE001
                    pass

    def _right_pane_command(self, kind: str, project_key: str | None = None) -> str:
        root = shlex.quote(str(self.config_path.parent.resolve()))
        import shutil
        pm_cmd = "pm" if shutil.which("pm") else "uv run pm"
        args = [pm_cmd, "cockpit-pane", shlex.quote(kind)]
        if project_key is not None:
            args.append(shlex.quote(project_key))
        joined = " ".join(args)
        return f"sh -lc 'cd {root} && {joined}'"


def _spark_bar(values: list[int], width: int = 30) -> str:
    """Render a mini spark-line bar chart using Unicode block characters."""
    if not values:
        return ""
    max_val = max(values) or 1
    blocks = " ▁▂▃▄▅▆▇█"
    return "".join(blocks[min(8, int(v / max_val * 8))] for v in values)


_DASHBOARD_PROJECT_CACHE: dict[str, tuple[float, dict[str, list], dict[str, int]]] = {}


def _dashboard_project_tasks(
    project_key: str, project_path: Path,
) -> tuple[dict[str, list], dict[str, int]]:
    """Return ({status -> [tasks]}, state_counts) for a project, cached by db_mtime.

    At scale (100+ projects) this is the top hot path inside _build_dashboard:
    previously every render opened SQLiteWorkService per project and hydrated
    the full task list. Projects that haven't changed since last render reuse
    the cached partition, so the dashboard's cost scales with changed projects,
    not total projects.
    """
    db_path = project_path / ".pollypm" / "state.db"
    if not db_path.exists():
        return {}, {}
    try:
        db_mtime = db_path.stat().st_mtime
    except OSError:
        return {}, {}
    cached = _DASHBOARD_PROJECT_CACHE.get(project_key)
    if cached is not None and cached[0] == db_mtime:
        return cached[1], cached[2]

    from pollypm.work.sqlite_service import SQLiteWorkService
    partitioned: dict[str, list] = {
        "in_progress": [], "review": [], "queued": [], "blocked": [], "done": [],
    }
    counts: dict[str, int] = {}
    try:
        with SQLiteWorkService(db_path=db_path, project_path=project_path) as svc:
            tasks = svc.list_tasks(project=project_key)
            counts = svc.state_counts(project=project_key)
            for t in tasks:
                sv = t.work_status.value
                if sv in partitioned:
                    partitioned[sv].append(t)
    except Exception:  # noqa: BLE001
        return {}, {}

    _DASHBOARD_PROJECT_CACHE[project_key] = (db_mtime, partitioned, counts)
    return partitioned, counts


def _build_dashboard(supervisor, config) -> str:
    from datetime import UTC, datetime, timedelta

    lines: list[str] = []
    now = datetime.now(UTC)

    def _age(ts_str: str) -> str:
        try:
            dt = datetime.fromisoformat(ts_str)
            secs = (now - dt).total_seconds()
            if secs < 60:
                return "just now"
            if secs < 3600:
                return f"{int(secs // 60)}m ago"
            if secs < 86400:
                return f"{int(secs // 3600)}h ago"
            return f"{int(secs // 86400)}d ago"
        except (ValueError, TypeError):
            return ""

    # ── Gather task data across all projects ──
    # Partition + counts are cached per project by state.db mtime; unchanged
    # projects skip SQLite on each render.
    all_active: list[tuple[str, object]] = []  # in_progress
    all_review: list[tuple[str, object]] = []  # waiting for review
    all_queued: list[tuple[str, object]] = []  # ready for pickup
    all_blocked: list[tuple[str, object]] = []
    all_done: list[tuple[str, object]] = []
    total_counts: dict[str, int] = {}
    live_keys: set[str] = set()
    for pk, proj in config.projects.items():
        live_keys.add(pk)
        partitioned, counts = _dashboard_project_tasks(pk, proj.path)
        for s, n in counts.items():
            total_counts[s] = total_counts.get(s, 0) + n
        for t in partitioned.get("in_progress", ()):
            all_active.append((pk, t))
        for t in partitioned.get("review", ()):
            all_review.append((pk, t))
        for t in partitioned.get("queued", ()):
            all_queued.append((pk, t))
        for t in partitioned.get("blocked", ()):
            all_blocked.append((pk, t))
        for t in partitioned.get("done", ()):
            all_done.append((pk, t))
    # Evict cache entries for projects no longer in config.
    for stale_key in list(_DASHBOARD_PROJECT_CACHE.keys()):
        if stale_key not in live_keys:
            _DASHBOARD_PROJECT_CACHE.pop(stale_key, None)
    all_done.sort(key=lambda x: x[1].updated_at or "", reverse=True)

    # ── Gather system data ──
    open_alerts = supervisor.store.open_alerts()
    user_inbox = _count_inbox_tasks_for_label(config)
    actionable_alerts = [a for a in open_alerts if a.alert_type not in (
        "suspected_loop", "stabilize_failed", "needs_followup",
    )]
    recent = supervisor.store.recent_events(limit=300)
    cutoff_24h = (now - timedelta(hours=24)).isoformat()
    day_events = [e for e in recent if e.created_at >= cutoff_24h]

    # ── Header ──
    lines.append("  PollyPM")
    lines.append("")

    # Status line: what needs YOUR attention right now (actionable items only)
    attention: list[str] = []
    if all_review:
        attention.append(f"◉ {len(all_review)} awaiting review")
    if user_inbox:
        attention.append(f"✉ {user_inbox} inbox")
    if actionable_alerts:
        attention.append(f"▲ {len(actionable_alerts)} alert{'s' if len(actionable_alerts) != 1 else ''}")
    if attention:
        lines.append("  " + "  ·  ".join(attention))
        lines.append("")

    # Task count summary
    count_parts = []
    for status in ("in_progress", "review", "queued", "blocked"):
        n = total_counts.get(status, 0)
        if n:
            icon = _STATUS_ICONS.get(status, "·")
            count_parts.append(f"{icon} {n} {status.replace('_', ' ')}")
    done_n = total_counts.get("done", 0)
    if done_n:
        count_parts.append(f"✓ {done_n} done")
    if count_parts:
        lines.append("  " + " · ".join(count_parts))
    lines.append("")

    # ── What's happening right now ──
    if all_active or all_review:
        lines.append("  ─── Now ───────────────────────────────────────────")
        lines.append("")
        for pk, t in all_active:
            project = config.projects.get(pk)
            proj_label = project.display_label() if project else pk
            assignee = f" [{t.assignee}]" if t.assignee else ""
            node = t.current_node_id or ""
            age = _age(t.updated_at) if t.updated_at else ""
            lines.append(f"  ⟳ {t.title}")
            lines.append(f"    {proj_label}{assignee} · {node} · {age}")
            lines.append("")
        for pk, t in all_review:
            project = config.projects.get(pk)
            proj_label = project.display_label() if project else pk
            age = _age(t.updated_at) if t.updated_at else ""
            lines.append(f"  ◉ {t.title}")
            lines.append(f"    {proj_label} · waiting for Russell · {age}")
            lines.append("")

    # ── Queued (ready for pickup) ──
    if all_queued:
        lines.append("  ─── Ready ─────────────────────────────────────────")
        lines.append("")
        for pk, t in all_queued[:5]:
            project = config.projects.get(pk)
            proj_label = project.display_label() if project else pk
            lines.append(f"  ○ {t.title}  ({proj_label})")
        if len(all_queued) > 5:
            lines.append(f"    + {len(all_queued) - 5} more queued")
        lines.append("")

    # ── Recently completed ──
    if all_done:
        lines.append("  ─── Done ──────────────────────────────────────────")
        lines.append("")
        for pk, t in all_done[:8]:
            project = config.projects.get(pk)
            proj_label = project.display_label() if project else pk
            age = _age(t.updated_at) if t.updated_at else ""
            lines.append(f"  ✓ {t.title}  ({proj_label})  {age}")
        if len(all_done) > 8:
            lines.append(f"    + {len(all_done) - 8} more completed")
        lines.append("")

    # ── System activity ──
    lines.append("  ─── Activity ──────────────────────────────────────")
    lines.append("")
    commits = [e for e in day_events if "commit" in e.message.lower()]
    recoveries = [e for e in day_events if e.event_type in ("recover", "recovery", "stabilize_failed")]
    sends = [e for e in day_events if e.event_type == "send_input"]
    task_events = [e for e in day_events if e.event_type in ("launch", "send_input", "auth_sync")]
    activity_parts = []
    if commits:
        activity_parts.append(f"{len(commits)} commits")
    if sends:
        activity_parts.append(f"{len(sends)} messages")
    if recoveries:
        activity_parts.append(f"{len(recoveries)} recoveries")
    if activity_parts:
        lines.append("  Today: " + " · ".join(activity_parts))
    else:
        lines.append("  No notable activity today.")
    lines.append("")

    # Show last few notable events with timestamps
    notable = [e for e in day_events if e.event_type not in ("heartbeat", "token_ledger", "polly_followup")][:6]
    for event in notable:
        age = _age(event.created_at)
        session = event.session_name
        msg = event.message[:55]
        lines.append(f"  {age:>8}  {session}: {msg}")
    if notable:
        lines.append("")

    # ── Alerts (if any) ──
    if actionable_alerts:
        lines.append("  ─── Alerts ────────────────────────────────────────")
        lines.append("")
        for alert in actionable_alerts[:5]:
            lines.append(f"  ▲ {alert.session_name}: {alert.message[:55]}")
        lines.append("")

    # ── Footer ──
    project_count = len(config.projects)
    lines.append(f"  {project_count} projects  ·  j/k navigate  ·  S settings")

    return "\n".join(lines)


def build_cockpit_detail(config_path: Path, kind: str, target: str | None = None) -> str:
    try:
        return _build_cockpit_detail_inner(config_path, kind, target)
    except Exception as exc:  # noqa: BLE001
        return f"Error loading {kind} view: {exc}"


def _build_cockpit_detail_inner(config_path: Path, kind: str, target: str | None = None) -> str:
    supervisor = PollyPMService(config_path).load_supervisor()
    try:
        supervisor.ensure_layout()
        return _build_cockpit_detail_dispatch(supervisor, config_path, kind, target)
    finally:
        try:
            supervisor.store.close()
        except Exception:  # noqa: BLE001
            pass


def _build_cockpit_detail_dispatch(supervisor, config_path: Path, kind: str, target: str | None = None) -> str:
    config = supervisor.config
    if kind in ("polly", "dashboard"):
        return _build_dashboard(supervisor, config)

    if kind == "inbox":
        return _render_inbox_panel(config)

    if kind == "workers":
        return _render_worker_roster_panel(config_path)

    if kind == "metrics":
        return _render_metrics_panel(config_path)

    if kind == "activity":
        from pollypm.plugins_builtin.activity_feed.cockpit.feed_panel import (
            render_activity_feed_text,
        )

        return render_activity_feed_text(config)

    if kind == "settings":
        recent_usage = supervisor.store.recent_token_usage(limit=5)
        lines = [
            "Settings",
            "",
            f"Workspace root: {config.project.workspace_root}",
            f"Control account: {config.pollypm.controller_account}",
            f"Failover order: {', '.join(config.pollypm.failover_accounts) or 'none'}",
            f"Open permissions by default: {'on' if config.pollypm.open_permissions_by_default else 'off'}",
            "",
            "This pane is read-only for now.",
            "Use Polly or the legacy `pm ui` surface for deeper account/runtime changes.",
        ]
        if recent_usage:
            lines.extend(["", "Recent token usage:"])
            for row in recent_usage[:4]:
                lines.append(
                    f"- {row.project_key} · {row.account_name} · {row.model_name} · {row.tokens_used} tokens"
                )
        return "\n".join(lines)

    if kind == "project" and target:
        project = config.projects.get(target)
        if project is None:
            return f"Project '{target}' not found in config.\n\nIt may not have been saved. Try `pm add-project <path>` or check ~/.pollypm/pollypm.toml."
        ensure_project_scaffold(project.path)

        # Try work service dashboard first
        try:
            dashboard = _render_project_dashboard(project, target, config_path, supervisor)
            if dashboard:
                return dashboard
        except Exception:
            pass

        # Fallback to basic project info
        task_backend = get_task_backend(project.path)
        issues_root = task_backend.issues_root()
        state_counts = task_backend.state_counts() if task_backend.exists() else {}
        worktrees = [item for item in list_worktrees(config_path, target) if item.status == "active"]
        lines = [
            f"{project.name or project.key}",
            "",
            f"Path: {project.path}",
            f"Kind: {project.kind.value}",
            f"Tracked: {'yes' if project.tracked else 'no'}",
            f"Issue tracker: {issues_root if task_backend.exists() else 'not initialized'}",
            f"Active worktrees: {len(worktrees)}",
            "",
            "No active live lane is running for this project.",
            "Select the project in the left rail and press N to start a worker lane.",
        ]
        # Show alerts for this project's sessions
        project_alerts = [
            a for a in supervisor.store.open_alerts()
            if any(
                l.session.project == target and l.session.name == a.session_name
                for l in supervisor.plan_launches()
            ) and a.alert_type not in ("suspected_loop", "stabilize_failed", "needs_followup")
        ]
        if project_alerts:
            lines.extend(["", "⚠ Alerts:"])
            for a in project_alerts:
                lines.append(f"  {a.severity} {a.alert_type}: {a.message}")
            lines.append("")
        if state_counts:
            lines.extend(["Task states:"])
            for state, count in state_counts.items():
                if count:
                    lines.append(f"- {state}: {count}")
        return "\n".join(lines)

    if kind == "issues" and target:
        project = config.projects.get(target)
        if not project:
            return f"Project '{target}' not found."
        # Try the work service first, fall back to file-based backend
        try:
            return _render_work_service_issues(project)
        except Exception:
            pass
        task_backend = get_task_backend(project.path)
        if not task_backend.exists():
            return f"{project.name or project.key} · Issues\n\nNo issue tracker initialized.\nUse `pm init-tracker {target}` to create one."
        state_counts = task_backend.state_counts()
        lines = [f"{project.name or project.key} · Issues", ""]
        for state_name in ["01-ready", "02-in-progress", "03-needs-review", "04-in-review", "05-completed"]:
            count = state_counts.get(state_name, 0)
            if count:
                tasks = task_backend.list_tasks(states=[state_name])
                lines.append(f"─── {state_name} ({count}) ───")
                for task in tasks[:8]:
                    lines.append(f"  {task.task_id}: {task.title}")
                lines.append("")
        if not any(state_counts.values()):
            lines.append("No issues found.")
        return "\n".join(lines)

    return "PollyPM\n\nSelect Polly, Inbox, a project, or Settings from the left rail."


# ---------------------------------------------------------------------------
# Worker roster — a live mission-control view that spans every project.
# ``_gather_worker_roster`` walks the config, opens each project's
# ``.pollypm/state.db`` to find active task assignments, cross-references
# tmux windows + supervisor state, and produces one ``WorkerRosterRow``
# per worker session. The row is the stable shape every renderer + test
# consumes — keep it data-only (no Textual imports).
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class WorkerRosterRow:
    """One worker session, flattened for the roster table.

    The row carries both display-ready bits (``status``, ``turn_label``)
    and the raw data the cockpit needs to route a jump (``tmux_window``,
    ``session_name``). ``status`` is one of ``working``/``idle``/``stuck``/
    ``offline`` so the renderer can pick the dot colour + sort bucket
    without re-inspecting the underlying signals.
    """

    project_key: str
    project_name: str
    session_name: str
    status: str  # "working" | "idle" | "stuck" | "offline"
    task_id: str | None
    task_number: int | None
    task_title: str
    current_node: str | None
    turn_label: str
    last_commit_label: str
    tmux_window: str | None  # e.g. "task-<project>-<number>"
    last_heartbeat: str | None
    worktree_path: str | None
    branch_name: str | None


# Order used by the renderer + sort helpers. Lower = earlier in the table.
_WORKER_ROSTER_STATUS_ORDER: dict[str, int] = {
    "stuck": 0,
    "working": 1,
    "idle": 2,
    "offline": 3,
}


def _worker_roster_sort_key(row: "WorkerRosterRow") -> tuple[int, str]:
    return (
        _WORKER_ROSTER_STATUS_ORDER.get(row.status, 9),
        (row.project_name or row.project_key or "").lower(),
    )


def _format_worker_turn_label(
    *, last_heartbeat_iso: str | None, is_turn_active: bool,
) -> str:
    """Return ``active 2m`` / ``idle 12m`` / ``—`` from heartbeat state."""
    if not last_heartbeat_iso:
        return "\u2014"
    from datetime import UTC, datetime
    try:
        dt = datetime.fromisoformat(last_heartbeat_iso)
    except (TypeError, ValueError):
        return "\u2014"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = datetime.now(UTC) - dt
    total_s = max(0, int(delta.total_seconds()))
    mins = total_s // 60 if total_s >= 60 else 0
    if mins < 1:
        unit = "now"
    elif mins < 60:
        unit = f"{mins}m"
    elif mins < 60 * 24:
        unit = f"{mins // 60}h"
    else:
        unit = f"{mins // (60 * 24)}d"
    prefix = "active" if is_turn_active else "idle"
    if unit == "now":
        return f"{prefix} now"
    return f"{prefix} {unit}"


def _last_commit_age(project_path: Path, branch_name: str | None) -> str:
    """Return a short relative-age string for the worker's branch tip.

    Best-effort: we only run ``git log -1 --format=%ct`` so a missing
    branch / non-repo path falls through to the empty-age dash.
    """
    if not branch_name:
        return "\u2014"
    try:
        import subprocess as _sp
        result = _sp.run(
            ["git", "-C", str(project_path), "log", "-1", "--format=%ct", branch_name],
            capture_output=True, text=True, check=False, timeout=2,
        )
    except Exception:  # noqa: BLE001
        return "\u2014"
    if result.returncode != 0:
        return "\u2014"
    ts_raw = (result.stdout or "").strip()
    if not ts_raw:
        return "\u2014"
    try:
        ts = int(ts_raw)
    except ValueError:
        return "\u2014"
    from datetime import UTC, datetime
    dt = datetime.fromtimestamp(ts, tz=UTC)
    delta = datetime.now(UTC) - dt
    total_s = max(0, int(delta.total_seconds()))
    if total_s < 60:
        return "just now"
    if total_s < 3600:
        return f"{total_s // 60}m ago"
    if total_s < 3600 * 24:
        return f"{total_s // 3600}h ago"
    return f"{total_s // (3600 * 24)}d ago"


def _gather_worker_roster(config) -> list[WorkerRosterRow]:
    """Walk every tracked project's work-service + tmux state.

    Returns one ``WorkerRosterRow`` per active worker session. Offline
    sessions (worker record exists but tmux window is gone) are included
    so Sam can see workers that have crashed.

    Always best-effort: a bad DB or missing tmux session degrades one
    project's worth of rows, never the whole roster.
    """
    from pollypm.work.sqlite_service import SQLiteWorkService

    projects = getattr(config, "projects", {}) or {}
    if not projects:
        return []

    # Supervisor state is optional — the roster is valuable even without
    # a running tmux server (e.g. headless CI / tests). We degrade to
    # offline + empty turn labels when either one is unavailable.
    supervisor = _try_load_supervisor_for_config(config)

    tmux = None
    storage_windows: dict[str, object] = {}
    try:
        from pollypm.session_services import create_tmux_client
        tmux = create_tmux_client()
        storage_session = f"{config.project.tmux_session}-storage-closet"
        try:
            storage_window_list = tmux.list_windows(storage_session)
        except Exception:  # noqa: BLE001
            storage_window_list = []
        storage_windows = {w.name: w for w in storage_window_list}
    except Exception:  # noqa: BLE001
        tmux = None
        storage_windows = {}

    rows: list[WorkerRosterRow] = []

    for project_key, project in projects.items():
        project_path = getattr(project, "path", None)
        project_name = (
            project.display_label() if hasattr(project, "display_label")
            else (getattr(project, "name", None) or project_key)
        )
        if project_path is None or not isinstance(project_path, Path):
            continue
        db_path = project_path / ".pollypm" / "state.db"
        if not db_path.exists():
            continue
        try:
            svc = SQLiteWorkService(db_path=db_path, project_path=project_path)
        except Exception:  # noqa: BLE001
            continue
        try:
            try:
                worker_sessions = svc.list_worker_sessions(
                    project=project_key, active_only=True,
                )
            except Exception:  # noqa: BLE001
                worker_sessions = []
            task_lookup: dict[int, object] = {}
            try:
                for t in svc.list_tasks(project=project_key):
                    tn = getattr(t, "task_number", None)
                    if isinstance(tn, int):
                        task_lookup[tn] = t
            except Exception:  # noqa: BLE001
                task_lookup = {}
        finally:
            try:
                svc.close()
            except Exception:  # noqa: BLE001
                pass

        for ws in worker_sessions:
            task = task_lookup.get(ws.task_number)
            task_title_full = (
                getattr(task, "title", "") or "" if task is not None else ""
            )
            task_title = task_title_full[:40]
            task_id = getattr(task, "task_id", None) if task is not None else None
            current_node = (
                getattr(task, "current_node_id", None)
                if task is not None else None
            )
            window_name = f"task-{project_key}-{ws.task_number}"
            window = storage_windows.get(window_name)
            has_window = window is not None and not getattr(
                window, "pane_dead", False,
            )
            session_name = ws.agent_name or window_name

            last_heartbeat_iso: str | None = None
            is_turn_active = False
            if has_window and tmux is not None:
                try:
                    pane_text = tmux.capture_pane(window.pane_id, lines=15)
                except Exception:  # noqa: BLE001
                    pane_text = ""
                lowered = pane_text.lower()
                if "\u23fa" in pane_text or "working" in lowered:
                    is_turn_active = True
                if (
                    "working (" in lowered
                    and "esc to interrupt" in lowered
                ):
                    is_turn_active = True

            stuck = False
            if supervisor is not None:
                try:
                    drift_at = supervisor.store.last_event_at(
                        session_name, "state_drift",
                    )
                except Exception:  # noqa: BLE001
                    drift_at = None
                if drift_at:
                    from datetime import UTC, datetime, timedelta
                    try:
                        drift_dt = datetime.fromisoformat(drift_at)
                    except (TypeError, ValueError):
                        drift_dt = None
                    if drift_dt is not None:
                        if drift_dt.tzinfo is None:
                            drift_dt = drift_dt.replace(tzinfo=UTC)
                        if drift_dt >= datetime.now(UTC) - timedelta(minutes=30):
                            stuck = True
                try:
                    hb = supervisor.store.latest_heartbeat(session_name)
                except Exception:  # noqa: BLE001
                    hb = None
                if hb is not None:
                    last_heartbeat_iso = getattr(hb, "created_at", None)

            if not has_window:
                status = "offline"
            elif stuck:
                status = "stuck"
            elif is_turn_active:
                status = "working"
            else:
                status = "idle"

            turn_label = _format_worker_turn_label(
                last_heartbeat_iso=last_heartbeat_iso,
                is_turn_active=is_turn_active,
            )
            last_commit_label = _last_commit_age(project_path, ws.branch_name)

            rows.append(
                WorkerRosterRow(
                    project_key=project_key,
                    project_name=str(project_name),
                    session_name=session_name,
                    status=status,
                    task_id=task_id,
                    task_number=ws.task_number,
                    task_title=task_title,
                    current_node=current_node,
                    turn_label=turn_label,
                    last_commit_label=last_commit_label,
                    tmux_window=window_name,
                    last_heartbeat=last_heartbeat_iso,
                    worktree_path=ws.worktree_path,
                    branch_name=ws.branch_name,
                )
            )

    if supervisor is not None:
        try:
            supervisor.store.close()
        except Exception:  # noqa: BLE001
            pass

    rows.sort(key=_worker_roster_sort_key)
    return rows


def _try_load_supervisor_for_config(config):
    """Best-effort supervisor load from a ``PollyPMConfig`` — returns
    ``None`` if we can't find a canonical config file path to feed into
    :class:`PollyPMService`. The roster still renders useful data without
    a supervisor (no drift detection, no heartbeat ages), so this is not
    fatal.
    """
    try:
        cfg_path = getattr(config.project, "config_file", None) or getattr(
            config.project, "config_path", None,
        )
    except Exception:  # noqa: BLE001
        cfg_path = None
    if cfg_path is None:
        try:
            root = getattr(config.project, "root_dir", None)
            if root is not None:
                candidate = root / "pollypm.toml"
                cfg_path = candidate if candidate.exists() else None
        except Exception:  # noqa: BLE001
            cfg_path = None
    if cfg_path is None:
        return None
    try:
        from pollypm.service_api import PollyPMService
        return PollyPMService(cfg_path).load_supervisor()
    except Exception:  # noqa: BLE001
        return None


def _render_worker_roster_panel(config_path: Path) -> str:
    """Fallback text-only roster — used by ``build_cockpit_detail`` when
    the right pane hasn't launched the Textual app yet (e.g. in
    automated tests that poke the static-view plumbing).

    The production path is the ``PollyWorkerRosterApp`` Textual screen
    launched via ``pm cockpit-pane workers``.
    """
    try:
        config = load_config(config_path)
    except Exception as exc:  # noqa: BLE001
        return f"Workers\n\nError loading config: {exc}"
    rows = _gather_worker_roster(config)
    if not rows:
        return (
            "Workers\n\n"
            "No active worker sessions.\n\n"
            "Start one with `pm cockpit-pane project <key>` and press `w`."
        )
    lines = ["Workers", ""]
    dot_for = {
        "working": "\u25cf", "idle": "\u25cb",
        "stuck": "\u25b2", "offline": "\u25cf",
    }
    for row in rows:
        dot = dot_for.get(row.status, "\u25cb")
        task_part = (
            f"#{row.task_number} {row.task_title}"
            if row.task_number is not None else "(none)"
        )
        lines.append(
            f"  {dot} {row.status:<7}  {row.project_name:<18}  "
            f"{row.session_name:<22}  {task_part}  @{row.current_node or '-'}  "
            f"{row.turn_label}  {row.last_commit_label}"
        )
    return "\n".join(lines)


def _gather_activity_feed(
    config,
    *,
    project: str | None = None,
    limit: int = 200,
):
    """Project the live activity feed for the cockpit's full-screen view.

    Returns a list of :class:`FeedEntry` records (newest first). Filters
    by ``project`` server-side when provided so the loaded window is
    already scoped — client-side filters in the Textual app then apply
    on the in-memory rows without another DB hit.

    Best-effort: if the projector or any DB read fails, returns an
    empty list rather than propagating the error. The Textual screen
    surfaces the empty state as a friendly placeholder.
    """
    try:
        from pollypm.plugins_builtin.activity_feed.plugin import build_projector
    except Exception:  # noqa: BLE001
        return []
    projector = build_projector(config)
    if projector is None:
        return []
    try:
        return projector.project(
            limit=limit,
            projects=[project] if project else None,
        )
    except Exception:  # noqa: BLE001
        return []


def _register_worker_roster_rail_item(registry, router) -> None:
    """Add the ``top.Workers`` rail row if not already registered.

    Kept out of ``core_rail_items`` so the roster's rail row, its route
    dispatch (``route_selected("workers")``) and its Textual app land in
    one patch rather than being spread across the plugin boundary. The
    registry dedupes on ``(plugin_name, section, label)``, so calling
    this every tick is safe.
    """
    try:
        from pollypm.plugin_api.v1 import RailItemRegistration, PanelSpec
    except Exception:  # noqa: BLE001
        return

    def _label(ctx) -> str:
        extras = getattr(ctx, "extras", {}) or {}
        cfg = extras.get("config")
        try:
            count = len(_gather_worker_roster(cfg)) if cfg is not None else 0
        except Exception:  # noqa: BLE001
            count = 0
        return f"Workers ({count})" if count else "Workers"

    def _state(ctx) -> str:
        extras = getattr(ctx, "extras", {}) or {}
        cfg = extras.get("config")
        try:
            rows = _gather_worker_roster(cfg) if cfg is not None else []
        except Exception:  # noqa: BLE001
            rows = []
        if any(r.status == "stuck" for r in rows):
            return "! stuck"
        if any(r.status == "working" for r in rows):
            return "\u25c6 working"
        return "idle"

    def _handler(ctx):
        try:
            router.route_selected("workers")
        except Exception:  # noqa: BLE001
            pass
        return PanelSpec(widget=None, focus_hint="workers")

    reg = RailItemRegistration(
        plugin_name="cockpit_worker_roster",
        section="top",
        index=25,  # after Inbox (20), before Projects
        label="Workers",
        handler=_handler,
        key="workers",
        state_provider=_state,
        label_provider=_label,
    )
    try:
        registry.add(reg)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Observability metrics snapshot — the data source behind the fifth cockpit
# surface (``PollyMetricsApp``). Kept in ``cockpit.py`` alongside the worker
# roster gather so the metrics screen can reuse existing helpers
# (``_gather_worker_roster``, ``_dashboard_project_tasks``,
# ``_count_inbox_tasks_for_label``) without duplication. The snapshot is a
# plain dataclass so tests + renderer consume the same shape.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MetricsSection:
    """One of the five metrics sections the screen renders.

    ``rows`` is a list of ``(label, value, tone)`` triples. ``tone`` is
    one of ``"ok"``, ``"warn"``, ``"alert"``, ``"muted"`` — the renderer
    maps each to a Rich colour consistent with the cockpit palette.
    """

    key: str     # "fleet" | "resources" | "throughput" | "failures" | "schedulers"
    title: str
    rows: list[tuple[str, str, str]]


@dataclass(slots=True)
class MetricsSnapshot:
    """Immutable snapshot of system health numbers.

    Built by :func:`_gather_metrics_snapshot` and rendered by the Textual
    ``PollyMetricsApp`` screen. Every field is best-effort — a failing
    subsystem lands as ``"?"`` in the row rather than propagating a
    crash. The snapshot also carries the ``captured_at`` ISO timestamp so
    the renderer can show "last refreshed" info without re-reading a
    clock.
    """

    captured_at: str
    fleet: MetricsSection
    resources: MetricsSection
    throughput: MetricsSection
    failures: MetricsSection
    schedulers: MetricsSection

    def sections(self) -> list[MetricsSection]:
        return [self.fleet, self.resources, self.throughput, self.failures, self.schedulers]


def _humanize_bytes(num: int | float) -> str:
    """Render a byte count in KB/MB/GB form with one decimal.

    Lives alongside the metrics gather because every resource row
    wants the same short-and-readable width.
    """
    try:
        value = float(num)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024.0:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} PB"


def _dir_size_bytes(path: Path) -> int:
    """Walk ``path`` once and return the sum of ``st_size`` for files.

    Best-effort — symlinks are not followed, read errors are silently
    skipped. Missing paths return 0 so the metrics screen can still
    display a useful row.
    """
    total = 0
    if not path.exists():
        return 0
    try:
        for child in path.rglob("*"):
            try:
                if child.is_file():
                    total += child.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def _rss_bytes_for_pid(pid: int) -> int | None:
    """Resident-set-size for a PID in bytes, or ``None`` when unavailable.

    Shells out to ``ps -p <pid> -o rss=`` — portable across macOS + Linux
    and doesn't need psutil. The ``ps`` output is in kilobytes so we
    multiply back to bytes for the consumer.
    """
    try:
        import subprocess as _sp
        result = _sp.run(
            ["ps", "-p", str(pid), "-o", "rss="],
            capture_output=True, text=True, check=False, timeout=2,
        )
    except Exception:  # noqa: BLE001
        return None
    if result.returncode != 0:
        return None
    raw = (result.stdout or "").strip()
    if not raw:
        return None
    try:
        return int(raw) * 1024
    except ValueError:
        return None


def _metrics_24h_events(store, now=None) -> list:
    """Return the subset of ``recent_events`` within the last 24 hours.

    Reads up to 2 000 rows via ``recent_events`` and filters client-side.
    That's a bounded read and keeps the snapshot self-contained without
    adding a new StateStore method. Returns an empty list if the store
    read fails.
    """
    from datetime import UTC, datetime, timedelta
    if now is None:
        now = datetime.now(UTC)
    cutoff_iso = (now - timedelta(hours=24)).isoformat()
    try:
        recent = store.recent_events(limit=2000)
    except Exception:  # noqa: BLE001
        return []
    return [e for e in recent if getattr(e, "created_at", "") >= cutoff_iso]


def _fleet_section(config, roster_rows: list, task_counts: dict[str, int],
                   inbox_breakdown: dict[str, int]) -> MetricsSection:
    """Section 1 — Workers / Tasks in flight / Inbox rollup."""
    n_working = sum(1 for r in roster_rows if r.status == "working")
    n_idle = sum(1 for r in roster_rows if r.status == "idle")
    n_stuck = sum(1 for r in roster_rows if r.status == "stuck")
    n_offline = sum(1 for r in roster_rows if r.status == "offline")

    rows: list[tuple[str, str, str]] = []
    worker_tone = "alert" if n_stuck else ("ok" if n_working else "muted")
    rows.append(
        ("Workers",
         f"{n_working} working · {n_idle} idle · {n_stuck} stuck · {n_offline} offline",
         worker_tone),
    )

    queued = int(task_counts.get("queued", 0))
    in_progress = int(task_counts.get("in_progress", 0))
    review = int(task_counts.get("review", 0))
    blocked = int(task_counts.get("blocked", 0))
    flight_tone = "alert" if blocked else ("ok" if in_progress else "muted")
    rows.append(
        ("Tasks in flight",
         f"{queued} queued · {in_progress} in_progress · {review} review · {blocked} blocked",
         flight_tone),
    )

    unread = inbox_breakdown.get("unread", 0)
    plan_review = inbox_breakdown.get("plan_review", 0)
    blocking = inbox_breakdown.get("blocking_question", 0)
    inbox_tone = "warn" if (unread or plan_review or blocking) else "ok"
    rows.append(
        ("Inbox",
         f"{unread} unread · {plan_review} plan_review · {blocking} blocking_question",
         inbox_tone),
    )
    return MetricsSection(key="fleet", title="Fleet", rows=rows)


def _inbox_breakdown(config) -> dict[str, int]:
    """Return ``{"unread": N, "plan_review": N, "blocking_question": N}``.

    Uses the same scan as :func:`_count_inbox_tasks_for_label` so the
    numbers line up with the rail badge. Best-effort: returns zero-value
    dict on any error.
    """
    out = {"unread": 0, "plan_review": 0, "blocking_question": 0}
    try:
        from pollypm.work.inbox_view import inbox_tasks
        from pollypm.work.sqlite_service import SQLiteWorkService
    except Exception:  # noqa: BLE001
        return out
    seen: set[str] = set()
    for project_key, db_path, project_path in _inbox_db_sources(config):
        if not db_path.exists():
            continue
        try:
            with SQLiteWorkService(
                db_path=db_path, project_path=project_path,
            ) as svc:
                for task in inbox_tasks(svc, project=project_key):
                    if task.task_id in seen:
                        continue
                    seen.add(task.task_id)
                    labels = list(task.labels or [])
                    if "plan_review" in labels:
                        out["plan_review"] += 1
                    if "blocking_question" in labels:
                        out["blocking_question"] += 1
                    out["unread"] += 1
        except Exception:  # noqa: BLE001
            continue
    return out


def _resource_section(config) -> MetricsSection:
    """Section 2 — state.db size, worktrees, logs, session RSS."""
    rows: list[tuple[str, str, str]] = []

    # state.db size + freelist ratio for the workspace-root DB.
    state_db = getattr(getattr(config, "project", None), "state_db", None)
    if state_db is not None:
        state_db = Path(state_db)
    if state_db and state_db.exists():
        try:
            db_size = state_db.stat().st_size
        except OSError:
            db_size = 0
        freelist_ratio = 0.0
        try:
            import sqlite3
            conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
            try:
                page_size = int(
                    conn.execute("PRAGMA page_size").fetchone()[0] or 0,
                )
                freelist = int(
                    conn.execute("PRAGMA freelist_count").fetchone()[0] or 0,
                )
                if page_size and db_size:
                    freelist_ratio = (page_size * freelist) / db_size
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            pass
        tone = "alert" if db_size > 500 * 1024 * 1024 else (
            "warn" if db_size > 100 * 1024 * 1024 else "ok"
        )
        rows.append(
            ("state.db",
             f"{_humanize_bytes(db_size)} · freelist {freelist_ratio*100:.1f}%",
             tone),
        )
    else:
        rows.append(("state.db", "(missing)", "muted"))

    # Agent worktrees — count + disk usage.
    workspace_root = getattr(getattr(config, "project", None), "workspace_root", None)
    wt_root = Path(workspace_root) / ".claude" / "worktrees" if workspace_root else None
    if wt_root and wt_root.exists():
        try:
            wt_count = sum(1 for p in wt_root.iterdir() if p.is_dir())
        except OSError:
            wt_count = 0
        wt_size = _dir_size_bytes(wt_root)
        tone = "alert" if wt_size > 10 * 1024**3 else (
            "warn" if wt_size > 2 * 1024**3 else "ok"
        )
        rows.append(
            (".claude/worktrees",
             f"{wt_count} worktree{'s' if wt_count != 1 else ''} · {_humanize_bytes(wt_size)}",
             tone),
        )
    else:
        rows.append((".claude/worktrees", "(none)", "muted"))

    # Log directory size under ~/.pollypm/logs/
    logs_dir = Path.home() / ".pollypm" / "logs"
    if logs_dir.exists():
        log_bytes = 0
        try:
            for f in logs_dir.glob("*.log*"):
                try:
                    log_bytes += f.stat().st_size
                except OSError:
                    continue
        except OSError:
            pass
        tone = "warn" if log_bytes > 500 * 1024 * 1024 else "ok"
        rows.append(("logs", _humanize_bytes(log_bytes), tone))
    else:
        rows.append(("logs", "(none)", "muted"))

    # Memory footprint per live session — sourced from tmux-tracked sessions.
    try:
        from pollypm.service_api import PollyPMService
        cfg_path = getattr(
            getattr(config, "project", None), "config_file", None,
        ) or getattr(
            getattr(config, "project", None), "config_path", None,
        )
        supervisor = None
        if cfg_path is not None:
            supervisor = PollyPMService(cfg_path).load_supervisor(readonly_state=True)
    except Exception:  # noqa: BLE001
        supervisor = None

    total_rss = 0
    live_count = 0
    if supervisor is not None:
        try:
            launches, windows, _alerts, _leases, _errors = supervisor.status()
        except Exception:  # noqa: BLE001
            launches, windows = [], []
        window_map = {w.name: w for w in windows}
        for launch in launches:
            window = window_map.get(launch.window_name)
            if window is None or getattr(window, "pane_dead", False):
                continue
            try:
                pid = int(getattr(window, "pane_pid", 0) or 0)
            except (TypeError, ValueError):
                pid = 0
            if pid <= 0:
                continue
            rss = _rss_bytes_for_pid(pid)
            if rss is None:
                continue
            total_rss += rss
            live_count += 1
        try:
            supervisor.store.close()
        except Exception:  # noqa: BLE001
            pass
    mem_tone = "alert" if total_rss > 8 * 1024**3 else (
        "warn" if total_rss > 2 * 1024**3 else "ok"
    )
    if live_count:
        rows.append(
            ("Session RSS",
             f"{live_count} session{'s' if live_count != 1 else ''} · {_humanize_bytes(total_rss)}",
             mem_tone),
        )
    else:
        rows.append(("Session RSS", "(no live sessions)", "muted"))

    return MetricsSection(key="resources", title="Resources", rows=rows)


def _throughput_section(day_events: list) -> MetricsSection:
    """Section 3 — commits / approvals / completions in the last 24h."""
    rows: list[tuple[str, str, str]] = []

    def _count(pred) -> int:
        return sum(1 for e in day_events if pred(getattr(e, "event_type", "")))

    completed = _count(lambda k: k in ("task_done", "task.done"))
    rejected = _count(lambda k: "reject" in (k or "").lower())
    approvals = _count(
        lambda k: k in ("plan_approved", "plan.approved", "task.approved", "approve"),
    )
    commits = _count(lambda k: "commit" in (k or "").lower() or k == "ran")
    pr_reviews = _count(
        lambda k: "pr_reviewed" in (k or "") or k == "review_completed",
    )

    rows.append(("Tasks completed",
                 str(completed),
                 "ok" if completed else "muted"))
    rows.append(("Tasks rejected",
                 str(rejected),
                 "warn" if rejected else "muted"))
    rows.append(("PRs reviewed",
                 str(pr_reviews),
                 "ok" if pr_reviews else "muted"))
    rows.append(("Commits (worker events)",
                 str(commits),
                 "ok" if commits else "muted"))
    rows.append(("Plan approvals",
                 str(approvals),
                 "ok" if approvals else "muted"))
    return MetricsSection(key="throughput", title="Throughput (24h)", rows=rows)


def _failure_section(day_events: list) -> MetricsSection:
    """Section 4 — failure counts over the last 24h."""
    def _count(pred) -> int:
        return sum(1 for e in day_events if pred(getattr(e, "event_type", "")))

    state_drift = _count(lambda k: k == "state_drift")
    persona_swap = _count(lambda k: k == "persona_swap_detected" or "persona_swap" in (k or ""))
    reprompts = _count(lambda k: k in ("worker_reprompt", "reprompt", "worker_turn_end_reprompt"))
    no_session = _count(lambda k: "no_session" in (k or ""))
    probe_fail = _count(lambda k: "provider_probe" in (k or "") and "fail" in (k or ""))

    rows: list[tuple[str, str, str]] = []
    rows.append(("state_drift", str(state_drift), "alert" if state_drift else "ok"))
    rows.append(("persona_swap_detected", str(persona_swap), "alert" if persona_swap else "ok"))
    rows.append(("worker reprompts", str(reprompts), "warn" if reprompts else "ok"))
    rows.append(("no_session alerts", str(no_session), "alert" if no_session else "ok"))
    rows.append(("Provider probe failures", str(probe_fail), "warn" if probe_fail else "ok"))
    return MetricsSection(key="failures", title="Failures (24h)", rows=rows)


def _scheduler_section(store) -> MetricsSection:
    """Section 5 — last fire-at + staleness flag per scheduled handler.

    Pulls the last 500 events from the store and groups them by the
    scheduled-job "subject" (captured in the summary payload via the
    inline scheduler). We treat each distinct ``kind`` as a handler
    row. Staleness is a 2× cadence rule: if more than 2h elapsed on
    something expected hourly, flag it red. Cadence is inferred from
    the gap between the most recent two firings of the same kind.
    """
    from datetime import UTC, datetime
    rows: list[tuple[str, str, str]] = []
    try:
        events = store.recent_events(limit=500)
    except Exception:  # noqa: BLE001
        events = []

    # Bucket scheduler events by subject (kind). We read the subject
    # out of the activity-summary JSON emitted by InlineSchedulerBackend
    # when available; fall back to a regex on the message text so tests
    # and older rows still group.
    import json as _json
    import re as _re
    bucket: dict[str, list[str]] = {}
    for e in events:
        if getattr(e, "session_name", "") != "scheduler":
            continue
        if getattr(e, "event_type", "") != "ran":
            continue
        message = getattr(e, "message", "") or ""
        subject: str | None = None
        try:
            payload = _json.loads(message)
            if isinstance(payload, dict):
                subject = payload.get("subject") or payload.get("kind")
        except (ValueError, TypeError):
            pass
        if not subject:
            match = _re.search(r"Ran scheduled job ([A-Za-z0-9_.:-]+)", message)
            if match:
                subject = match.group(1)
        if not subject:
            continue
        bucket.setdefault(subject, []).append(getattr(e, "created_at", "") or "")

    if not bucket:
        rows.append(("(no scheduled runs recorded)", "—", "muted"))
        return MetricsSection(key="schedulers", title="Schedulers", rows=rows)

    now = datetime.now(UTC)
    for kind, timestamps in sorted(bucket.items()):
        timestamps = [t for t in timestamps if t]
        if not timestamps:
            continue
        timestamps.sort(reverse=True)
        latest_raw = timestamps[0]
        try:
            latest = datetime.fromisoformat(latest_raw)
            if latest.tzinfo is None:
                from datetime import UTC as _UTC
                latest = latest.replace(tzinfo=_UTC)
            age_s = max(0, int((now - latest).total_seconds()))
        except (TypeError, ValueError):
            age_s = 0
        # Cadence guess from the gap between the two most recent fires.
        cadence_s = None
        if len(timestamps) >= 2:
            try:
                prev = datetime.fromisoformat(timestamps[1])
                if prev.tzinfo is None:
                    from datetime import UTC as _UTC
                    prev = prev.replace(tzinfo=_UTC)
                cadence_s = max(0, int((latest - prev).total_seconds()))
            except (TypeError, ValueError):
                cadence_s = None
        if age_s < 60:
            age_label = "just now"
        elif age_s < 3600:
            age_label = f"{age_s // 60}m ago"
        elif age_s < 86400:
            age_label = f"{age_s // 3600}h ago"
        else:
            age_label = f"{age_s // 86400}d ago"
        # Stale if 2× cadence — or >2h idle when cadence is unknown.
        threshold = cadence_s * 2 if cadence_s else 2 * 3600
        tone = "alert" if age_s > threshold else "ok"
        rows.append((kind, age_label, tone))
    return MetricsSection(key="schedulers", title="Schedulers", rows=rows)


def _gather_metrics_snapshot(config) -> MetricsSnapshot:
    """Build a :class:`MetricsSnapshot` from the live system state.

    Best-effort everywhere — one subsystem failure leaves the other
    sections intact. Safe to call on the UI thread for small configs
    (< 50 projects); callers that render on an interval should hop to a
    background thread (the ``PollyMetricsApp`` screen does).
    """
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    captured_at = now.isoformat()

    # Fleet — workers + task counts across every project.
    try:
        roster_rows = _gather_worker_roster(config)
    except Exception:  # noqa: BLE001
        roster_rows = []
    total_counts: dict[str, int] = {}
    for project_key, project in getattr(config, "projects", {}).items():
        try:
            _partitioned, counts = _dashboard_project_tasks(
                project_key, project.path,
            )
        except Exception:  # noqa: BLE001
            counts = {}
        for status, n in counts.items():
            total_counts[status] = total_counts.get(status, 0) + int(n)
    try:
        inbox_breakdown = _inbox_breakdown(config)
    except Exception:  # noqa: BLE001
        inbox_breakdown = {"unread": 0, "plan_review": 0, "blocking_question": 0}
    fleet = _fleet_section(config, roster_rows, total_counts, inbox_breakdown)

    # Resources.
    try:
        resources = _resource_section(config)
    except Exception:  # noqa: BLE001
        resources = MetricsSection(
            key="resources", title="Resources",
            rows=[("error", "resources unavailable", "alert")],
        )

    # Throughput + failures share a single 24h-event read.
    store = None
    try:
        state_db = getattr(getattr(config, "project", None), "state_db", None)
        if state_db is not None:
            from pollypm.storage.state import StateStore
            store = StateStore(Path(state_db), readonly=True)
    except Exception:  # noqa: BLE001
        store = None

    if store is None:
        throughput = MetricsSection(
            key="throughput", title="Throughput (24h)",
            rows=[("error", "state store unavailable", "alert")],
        )
        failures = MetricsSection(
            key="failures", title="Failures (24h)", rows=[],
        )
        schedulers = MetricsSection(
            key="schedulers", title="Schedulers", rows=[],
        )
    else:
        try:
            day_events = _metrics_24h_events(store, now=now)
        except Exception:  # noqa: BLE001
            day_events = []
        try:
            throughput = _throughput_section(day_events)
        except Exception:  # noqa: BLE001
            throughput = MetricsSection(
                key="throughput", title="Throughput (24h)", rows=[],
            )
        try:
            failures = _failure_section(day_events)
        except Exception:  # noqa: BLE001
            failures = MetricsSection(
                key="failures", title="Failures (24h)", rows=[],
            )
        try:
            schedulers = _scheduler_section(store)
        except Exception:  # noqa: BLE001
            schedulers = MetricsSection(
                key="schedulers", title="Schedulers", rows=[],
            )
        try:
            store.close()
        except Exception:  # noqa: BLE001
            pass

    return MetricsSnapshot(
        captured_at=captured_at,
        fleet=fleet,
        resources=resources,
        throughput=throughput,
        failures=failures,
        schedulers=schedulers,
    )


def _register_metrics_rail_item(registry, router) -> None:
    """Add the ``top.Metrics`` rail row if not already registered.

    Kept next to the worker-roster registration so both observability
    rows sit at the top of the rail. Safe to call repeatedly — the
    registry dedupes on ``(plugin_name, section, label)``.
    """
    try:
        from pollypm.plugin_api.v1 import RailItemRegistration, PanelSpec
    except Exception:  # noqa: BLE001
        return

    def _state(_ctx) -> str:
        return "watch"

    def _handler(ctx):
        try:
            router.route_selected("metrics")
        except Exception:  # noqa: BLE001
            pass
        return PanelSpec(widget=None, focus_hint="metrics")

    reg = RailItemRegistration(
        plugin_name="cockpit_metrics",
        section="top",
        index=28,  # after Workers (25), before Projects (30+)
        label="Metrics",
        handler=_handler,
        key="metrics",
        state_provider=_state,
    )
    try:
        registry.add(reg)
    except Exception:  # noqa: BLE001
        pass


def _render_metrics_panel(config_path: Path) -> str:
    """Fallback text rendering of the metrics snapshot — used by
    ``build_cockpit_detail`` when the right pane hasn't launched the
    Textual app yet. Mirrors the worker-roster fallback.
    """
    try:
        config = load_config(config_path)
    except Exception as exc:  # noqa: BLE001
        return f"Metrics\n\nError loading config: {exc}"
    try:
        snap = _gather_metrics_snapshot(config)
    except Exception as exc:  # noqa: BLE001
        return f"Metrics\n\nError gathering metrics: {exc}"
    lines: list[str] = ["Metrics", ""]
    for section in snap.sections():
        lines.append(section.title)
        if not section.rows:
            lines.append("  (no data)")
            lines.append("")
            continue
        for label, value, _tone in section.rows:
            lines.append(f"  {label}: {value}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command palette (``:``) — see issue brief 2026-04-17.
#
# A thin data layer that the Textual ``CommandPaletteModal`` in
# :mod:`pollypm.cockpit_ui` renders. Keeping the registry out of the UI
# layer lets tests exercise the command list (filtering, fuzzy search,
# per-project commands) without having to spin up a full Textual Pilot.
# Every entry is a plain dataclass — the dispatch happens via a ``tag``
# string the host App interprets. This avoids coupling the registry to
# any particular App class.
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class PaletteCommand:
    """A single row in the ``:`` command palette.

    ``tag`` is a dotted string the host :class:`App` interprets in
    :meth:`dispatch_palette_command`. Commands are kept as inert data
    here so the registry can be built+filtered in tests without
    requiring a running Textual app.
    """

    title: str
    subtitle: str
    category: str
    keybind: str | None
    tag: str

    def haystack(self) -> str:
        """Lowercased search string covering title + subtitle + category."""
        return f"{self.title} {self.subtitle} {self.category}".lower()


def _fuzzy_subsequence_score(needle: str, haystack: str) -> int | None:
    """Return a match score if ``needle`` is a subsequence of ``haystack``.

    Lower score is a tighter match. Adjacent characters score better than
    scattered ones — this mirrors VS Code / Raycast's "close letters
    win" feel without any real dependency on a fuzzy library. Returns
    ``None`` when ``needle`` isn't a subsequence at all.
    """
    if not needle:
        return 0
    needle = needle.lower()
    haystack = haystack.lower()
    # Substring match always wins — the earliest exact substring gets
    # the best (lowest) score.
    idx = haystack.find(needle)
    if idx != -1:
        return idx  # closer to start = better
    # Fall back to subsequence: walk both strings in lockstep.
    score = 0
    last_pos = -1
    h_i = 0
    for ch in needle:
        while h_i < len(haystack) and haystack[h_i] != ch:
            h_i += 1
        if h_i >= len(haystack):
            return None
        gap = h_i - last_pos - 1
        score += 1000 + gap  # subsequence baseline > any substring score
        last_pos = h_i
        h_i += 1
    return score


def filter_palette_commands(
    commands: list[PaletteCommand], query: str,
) -> list[PaletteCommand]:
    """Return matching commands ordered by fuzzy score, then title."""
    query = (query or "").strip()
    if not query:
        return list(commands)
    scored: list[tuple[int, str, PaletteCommand]] = []
    for cmd in commands:
        score = _fuzzy_subsequence_score(query, cmd.haystack())
        if score is None:
            continue
        scored.append((score, cmd.title.lower(), cmd))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [cmd for _score, _title, cmd in scored]


def build_palette_commands(
    config_path: Path,
    *,
    current_project: str | None = None,
) -> list[PaletteCommand]:
    """Return the default global command set for the cockpit palette.

    ``current_project`` is the project key the caller is currently
    viewing (if any) — used to prefill project-scoped commands like
    "Create task". Projects are read from the loaded config; if the
    config can't be loaded we still return the static commands so the
    palette never shows an empty list.
    """
    commands: list[PaletteCommand] = []

    # Navigation — the six top-level cockpit views.
    commands.append(PaletteCommand(
        title="Go to Inbox",
        subtitle="Open the cockpit inbox",
        category="Navigation",
        keybind=None,
        tag="nav.inbox",
    ))
    commands.append(PaletteCommand(
        title="Go to Workers",
        subtitle="Open the worker roster",
        category="Navigation",
        keybind=None,
        tag="nav.workers",
    ))
    commands.append(PaletteCommand(
        title="Go to Activity",
        subtitle="Open the activity feed",
        category="Navigation",
        keybind=None,
        tag="nav.activity",
    ))
    commands.append(PaletteCommand(
        title="Go to Metrics",
        subtitle="Open the observability metrics screen",
        category="Navigation",
        keybind=None,
        tag="nav.metrics",
    ))
    commands.append(PaletteCommand(
        title="Go to Settings",
        subtitle="Open cockpit settings",
        category="Navigation",
        keybind="s",
        tag="nav.settings",
    ))
    commands.append(PaletteCommand(
        title="Go to Dashboard",
        subtitle="Jump to the main cockpit rail",
        category="Navigation",
        keybind=None,
        tag="nav.dashboard",
    ))

    # Per-project navigation + task commands.
    try:
        config = load_config(config_path)
        projects = getattr(config, "projects", {}) or {}
    except Exception:  # noqa: BLE001
        projects = {}
    for project_key, project in projects.items():
        name = getattr(project, "name", None) or project_key
        commands.append(PaletteCommand(
            title=f"Go to project: {name}",
            subtitle=f"Open the {name} dashboard",
            category="Navigation",
            keybind=None,
            tag=f"nav.project:{project_key}",
        ))
        commands.append(PaletteCommand(
            title=f"Create task in {name}",
            subtitle="Draft a new task in this project",
            category="Task",
            keybind=None,
            tag=f"task.create:{project_key}",
        ))
        commands.append(PaletteCommand(
            title=f"Queue next task in {name}",
            subtitle=f"Run pm task next --project {project_key}",
            category="Task",
            keybind=None,
            tag=f"task.queue_next:{project_key}",
        ))

    # Inbox commands.
    commands.append(PaletteCommand(
        title="Run pm notify",
        subtitle="Send a notification into the inbox",
        category="Inbox",
        keybind=None,
        tag="inbox.notify",
    ))
    commands.append(PaletteCommand(
        title="Archive all read inbox items",
        subtitle="Mark every already-read message done",
        category="Inbox",
        keybind=None,
        tag="inbox.archive_read",
    ))

    # Session / app-level.
    commands.append(PaletteCommand(
        title="Refresh data",
        subtitle="Re-read state and repaint the current screen",
        category="Session",
        keybind="r",
        tag="session.refresh",
    ))
    commands.append(PaletteCommand(
        title="Restart cockpit",
        subtitle="Exit and reload the cockpit app",
        category="Session",
        keybind=None,
        tag="session.restart",
    ))
    commands.append(PaletteCommand(
        title="Show keyboard shortcuts",
        subtitle="Display the current screen's keybindings",
        category="Session",
        keybind="?",
        tag="session.shortcuts",
    ))

    # System.
    commands.append(PaletteCommand(
        title="Run pm doctor",
        subtitle="Stream doctor checks into the palette",
        category="System",
        keybind=None,
        tag="system.doctor",
    ))
    commands.append(PaletteCommand(
        title="Open pollypm.toml in editor",
        subtitle=str(config_path),
        category="System",
        keybind=None,
        tag="system.edit_config",
    ))

    # Let the caller prioritise current-project commands visually. We
    # keep the list stable (no re-ordering mid-render); a current-project
    # hint just means the "Create task in <current>" entry sits above
    # its siblings.
    if current_project is not None:
        preferred: list[PaletteCommand] = []
        rest: list[PaletteCommand] = []
        for cmd in commands:
            if cmd.tag.endswith(f":{current_project}"):
                preferred.append(cmd)
            else:
                rest.append(cmd)
        commands = preferred + rest

    return commands
