"""Cockpit inbox + worker roster panels (#405).

Contract:
- Inputs: a cockpit config (plus work-service handles for per-project
  inbox scans) and, for the roster, an optional live ``Supervisor`` so we
  can read heartbeats + state-drift events.
- Outputs: plain-text renderers (``_render_inbox_panel``,
  ``_render_worker_roster_panel``) that ``build_cockpit_detail`` in
  :mod:`pollypm.cockpit` can return when the right pane hasn't launched
  the Textual app yet, plus the data-only helpers
  (``_inbox_db_sources``, ``_count_inbox_tasks_for_label``,
  ``_gather_worker_roster``, …) that both the rail badges and the
  Textual screens consume.
- Side effects: opens (and closes) SQLite connections for every tracked
  project; best-effort tmux queries for the roster. Nothing writes.
- Invariants: any DB / tmux failure degrades a single row rather than
  raising — the inbox and roster panels must always produce useful
  output even on a partially broken workspace.

Extracted from ``cockpit.py`` so the monolithic module can focus on
orchestration. Re-exports live on ``pollypm.cockpit`` for legacy
callers + the test suite.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pollypm.config import load_config
from pollypm.cockpit_sections.base import _STATUS_ICONS


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


@dataclass(slots=True, frozen=True)
class InboxThreadRow:
    """One visible row in the inbox thread tree.

    The inbox UI renders root task rows plus optional inline reply rows
    underneath expanded tasks. This shape stays data-only so the Textual
    app can consume it without embedding tree math in widget callbacks.
    """

    key: str
    kind: Literal["task", "reply"]
    task_id: str
    task: object
    reply: object | None = None
    reply_index: int | None = None
    reply_count: int = 0
    expanded: bool = False
    depth: int = 0

    @property
    def is_task(self) -> bool:
        return self.kind == "task"

    @property
    def is_reply(self) -> bool:
        return self.kind == "reply"

    @property
    def has_children(self) -> bool:
        return self.reply_count > 0


def build_inbox_thread_rows(
    tasks: list[object],
    replies_by_task: dict[str, list[object]],
    expanded_task_ids: set[str],
) -> list[InboxThreadRow]:
    """Flatten task threads into the visible list rows for the inbox UI."""

    rows: list[InboxThreadRow] = []
    for task in tasks:
        task_id = getattr(task, "task_id", "")
        replies = list(replies_by_task.get(task_id, ()))
        expanded = task_id in expanded_task_ids and bool(replies)
        rows.append(
            InboxThreadRow(
                key=f"task:{task_id}",
                kind="task",
                task_id=task_id,
                task=task,
                reply_count=len(replies),
                expanded=expanded,
            )
        )
        if not expanded:
            continue
        for reply_index, reply in enumerate(replies):
            rows.append(
                InboxThreadRow(
                    key=f"reply:{task_id}:{reply_index}",
                    kind="reply",
                    task_id=task_id,
                    task=task,
                    reply=reply,
                    reply_index=reply_index,
                    reply_count=len(replies),
                    depth=1,
                )
            )
    return rows


def inbox_thread_right_action(
    rows: list[InboxThreadRow],
    selected_index: int | None,
) -> tuple[Literal["noop", "expand", "select_child"], int | None]:
    """Return the tree-navigation action for ``Right`` on ``selected_index``."""

    if selected_index is None or selected_index < 0 or selected_index >= len(rows):
        return "noop", None
    row = rows[selected_index]
    if row.is_reply or not row.has_children:
        return "noop", None
    if not row.expanded:
        return "expand", None
    child_index = selected_index + 1
    if child_index < len(rows) and rows[child_index].is_reply and rows[child_index].task_id == row.task_id:
        return "select_child", child_index
    return "noop", None


def inbox_thread_left_action(
    rows: list[InboxThreadRow],
    selected_index: int | None,
) -> tuple[Literal["noop", "collapse", "select_parent"], int | None]:
    """Return the tree-navigation action for ``Left`` on ``selected_index``."""

    if selected_index is None or selected_index < 0 or selected_index >= len(rows):
        return "noop", None
    row = rows[selected_index]
    if row.is_reply:
        for index in range(selected_index - 1, -1, -1):
            candidate = rows[index]
            if candidate.is_task and candidate.task_id == row.task_id:
                return "select_parent", index
        return "noop", None
    if row.expanded and row.has_children:
        return "collapse", None
    return "noop", None


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
