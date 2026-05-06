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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pollypm.config import load_config
from pollypm.cockpit_task_priority import priority_glyph, priority_rank
from pollypm.cockpit_sections.base import _STATUS_ICONS
from pollypm.cockpit_worker_identity import (
    load_worker_color_overrides,
    worker_identity,
)
from pollypm.heartbeats.snapshots import read_recent_heartbeat_snapshot
from pollypm.inbox_message_refs import row_project_refs as _row_project_refs


def _timestamp_sort_value(value) -> float:
    """Best-effort sortable timestamp for task list ordering."""
    if not value:
        return 0.0
    try:
        if hasattr(value, "timestamp"):
            return float(value.timestamp())
        return float(datetime.fromisoformat(str(value)).timestamp())
    except Exception:  # noqa: BLE001
        return 0.0


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
    active.sort(
        key=lambda t: (
            _so.get(t.work_status.value, 9),
            priority_rank(t),
            -_timestamp_sort_value(getattr(t, "updated_at", None)),
            int(getattr(t, "task_number", 0) or 0),
        )
    )
    if active:
        for t in active:
            icon = _STATUS_ICONS.get(t.work_status.value, "·")
            assignee = f" [{t.assignee}]" if t.assignee else ""
            lines.append(
                f"  {icon} {priority_glyph(t)} #{t.task_number} {t.title}{assignee}"
            )
        lines.append("")

    # Recently completed (last 10)
    completed = [t for t in tasks if t.work_status.value in ("done", "cancelled")]
    completed.sort(key=lambda t: t.updated_at or "", reverse=True)
    if completed:
        lines.append(f"─── completed ({len(completed)}) ───")
        for t in completed[:10]:
            icon = _STATUS_ICONS.get(t.work_status.value, "·")
            lines.append(f"  {icon} {priority_glyph(t)} #{t.task_number} {t.title}")
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

    Note: this helper INCLUDES non-tracked projects on purpose — the
    inbox cockpit panel and rail badge surface all registered projects
    so a user can triage messages even before running ``pm init-tracker``.
    The recovery prompt + morning-briefing surfaces filter to tracked
    only; they have a different invariant (cycles 85/86/87).

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


def _row_is_dev_channel(labels_raw: object) -> bool:
    """Return True if the message's labels list contains ``channel:dev``.

    Accepts the raw column value (JSON string or list) since
    ``Store.query_messages`` may surface either depending on the
    engine. Any other channel (or no explicit channel) is treated as
    user-facing. See #754.
    """
    import json as _json
    labels: list[str] = []
    if isinstance(labels_raw, list):
        labels = [str(x) for x in labels_raw]
    elif isinstance(labels_raw, str) and labels_raw:
        try:
            parsed = _json.loads(labels_raw)
            if isinstance(parsed, list):
                labels = [str(x) for x in parsed]
        except ValueError:
            labels = []
    return "channel:dev" in labels


def _count_inbox_tasks_for_label(config) -> int:
    """Count actionable inbox items across tracked projects + workspace-root.

    The cockpit rail is a notification surface, not an activity feed.
    It should badge work that requires the user, while completion
    updates and FYI messages stay discoverable inside the inbox's
    all-messages lens.

    Dedupes:
    - Tasks by ``task_id`` so a task present in more than one DB counts
      once.
    - Messages by their ``(scope, message_id)`` pair — same reason.

    Applies ``_filter_approved_plan_reviews`` so the rail badge tracks
    the inbox list view: phantom ``plan_review`` rows whose underlying
    user_approval is already APPROVED don't inflate the count (#1107).
    Without this filter the rail badge stays stale relative to the list
    by however many phantom rows are pending sweep, which masked the
    success of #1103 for several overnight loop ticks.

    The function name is kept for backward compatibility with existing
    callers; the return value is now an item count, not just a task
    count.
    """
    try:
        from pollypm.work.inbox_view import inbox_tasks
        from pollypm.work.sqlite_service import SQLiteWorkService
        from pollypm.cockpit_inbox_items import (
            _WORKSPACE_DB_KEY,
            _filter_approved_plan_reviews,
            annotate_inbox_entry,
            message_row_to_inbox_entry,
            task_to_inbox_entry,
        )
    except Exception:  # noqa: BLE001
        return 0

    try:
        from pollypm.store import SQLAlchemyStore
    except Exception:  # noqa: BLE001
        SQLAlchemyStore = None  # type: ignore[assignment]

    seen_task_ids: set[str] = set()
    seen_message_keys: set[tuple[str, object]] = set()
    # Collect actionable entries so the plan-review filter can drop
    # phantoms before we count. ``_filter_approved_plan_reviews`` needs
    # a per-project ``(db_path, project_path)`` map to resolve refs.
    task_entries: dict[str, object] = {}
    message_entries: dict[tuple[str, object], object] = {}
    project_db_paths: dict[str, tuple[Path, Path]] = {}
    known_projects = set(getattr(config, "projects", {}).keys())

    for project_key, db_path, project_path in _inbox_db_sources(config):
        if not db_path.exists():
            continue
        if project_key:
            project_db_paths[project_key] = (db_path, project_path)
        else:
            project_db_paths[_WORKSPACE_DB_KEY] = (db_path, project_path)
        try:
            with SQLiteWorkService(
                db_path=db_path, project_path=project_path,
            ) as svc:
                for task in inbox_tasks(svc, project=project_key):
                    item = annotate_inbox_entry(
                        task_to_inbox_entry(task, db_path=db_path),
                        known_projects=known_projects,
                    )
                    if getattr(item, "needs_action", False):
                        seen_task_ids.add(task.task_id)
                        task_entries.setdefault(task.task_id, item)
        except Exception:  # noqa: BLE001
            pass

        if SQLAlchemyStore is None:
            continue
        try:
            store = SQLAlchemyStore(f"sqlite:///{db_path}")
        except Exception:  # noqa: BLE001
            continue
        try:
            filters: dict[str, object] = dict(
                recipient="user",
                state="open",
                type=["notify", "inbox_task", "alert"],
            )
            if project_key:
                filters["scope"] = project_key
            try:
                rows = store.query_messages(**filters)
            except Exception:  # noqa: BLE001
                rows = []
            for row in rows:
                # query_messages returns dicts with 'id' as the primary
                # key; some older call sites surface it as 'message_id'.
                if isinstance(row, dict):
                    row_id = row.get("id") or row.get("message_id")
                    scope = row.get("scope", "") or ""
                    labels_raw = row.get("labels")
                else:
                    row_id = (
                        getattr(row, "id", None)
                        or getattr(row, "message_id", None)
                    )
                    scope = getattr(row, "scope", "") or ""
                    labels_raw = getattr(row, "labels", None)
                if row_id is None:
                    continue
                if scope and scope != "inbox" and scope not in known_projects:
                    continue
                refs = _row_project_refs(row)
                if any(ref not in known_projects for ref in refs):
                    continue
                # #754 — skip dev-channel messages; those are test /
                # debug traffic that should never count against the
                # user-facing inbox badge.
                if _row_is_dev_channel(labels_raw):
                    continue
                item = annotate_inbox_entry(
                    message_row_to_inbox_entry(
                        row,
                        source_key=project_key or "__workspace__",
                        db_path=db_path,
                    ),
                    known_projects=known_projects,
                )
                if getattr(item, "needs_action", False):
                    msg_key = (str(scope), row_id)
                    seen_message_keys.add(msg_key)
                    message_entries.setdefault(msg_key, item)
        finally:
            try:
                store.close()
            except Exception:  # noqa: BLE001
                pass
    # #1107 — drop ``plan_review`` rows whose underlying user_approval
    # has already been APPROVED so the rail badge tracks the inbox
    # list view (which already runs this filter inside
    # ``load_inbox_entries``).
    collected = list(task_entries.values()) + list(message_entries.values())
    if collected and project_db_paths:
        kept = _filter_approved_plan_reviews(
            collected, project_db_paths=project_db_paths,
        )
        return len(kept)
    return len(seen_task_ids) + len(seen_message_keys)


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
    health: str  # "alive" | "idle_warn" | "unresponsive"
    health_tooltip: str
    task_id: str | None
    task_number: int | None
    task_title: str
    current_node: str | None
    turn_label: str
    last_commit_label: str
    token_total: int
    tmux_window: str | None  # e.g. "task-<project>-<number>"
    last_heartbeat: str | None
    worktree_path: str | None
    branch_name: str | None
    just_shipped: bool = False
    shipment_token: str | None = None


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


def _recent_worker_shipment(task) -> tuple[bool, str | None]:
    work_status = getattr(getattr(task, "work_status", None), "value", "")
    updated_at = getattr(task, "updated_at", None)
    task_id = getattr(task, "task_id", None)
    if work_status != "review" or not updated_at or not task_id:
        return False, None
    try:
        updated_dt = datetime.fromisoformat(str(updated_at))
    except (TypeError, ValueError):
        return False, None
    if updated_dt.tzinfo is None:
        updated_dt = updated_dt.replace(tzinfo=UTC)
    if updated_dt < datetime.now(UTC) - timedelta(seconds=20):
        return False, None
    return True, f"{task_id}:{updated_dt.isoformat()}"


def _pane_text_shows_active_turn(pane_text: str) -> bool:
    lowered = pane_text.lower()
    return (
        "\u23fa" in pane_text
        or "working (" in lowered
        or "esc to interrupt" in lowered
    )


def _tmux_window_has_stopped_descendant(tmux, window: object | None) -> bool:
    if tmux is None or window is None or bool(getattr(window, "pane_dead", False)):
        return False
    check = getattr(tmux, "pane_has_stopped_descendant", None)
    if not callable(check):
        return False
    pane_pid = None
    pane_pid_raw = getattr(window, "pane_pid", None)
    if pane_pid_raw is not None:
        try:
            pane_pid = int(pane_pid_raw)
        except (TypeError, ValueError):
            pane_pid = None
    if pane_pid is not None and pane_pid > 0:
        try:
            return bool(check(pane_pid=pane_pid))
        except Exception:  # noqa: BLE001
            pass
    pane_id = getattr(window, "pane_id", None)
    if pane_id:
        try:
            return bool(check(pane_id=str(pane_id)))
        except Exception:  # noqa: BLE001
            return False
    return False


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


def _format_commit_age(total_s: int) -> str:
    if total_s < 60:
        return "just now"
    if total_s < 3600:
        return f"{total_s // 60}m ago"
    if total_s < 3600 * 24:
        return f"{total_s // 3600}h ago"
    return f"{total_s // (3600 * 24)}d ago"


def _last_commit_age_info(
    project_path: Path,
    branch_name: str | None,
) -> tuple[str, int | None]:
    """Return a short relative-age string and seconds since branch tip.

    Best-effort: we only run ``git log -1 --format=%ct`` so a missing
    branch / non-repo path falls through to the empty-age dash and no
    recency signal.

    For control sessions (architect-/worker-/pm-) without a per-task
    branch, fall back to the project's ``HEAD`` so the column reflects
    "what just landed in this project" rather than rendering empty for
    every row (#997).
    """
    revision = branch_name or "HEAD"
    try:
        import subprocess as _sp
        result = _sp.run(
            ["git", "-C", str(project_path), "log", "-1", "--format=%ct", revision],
            capture_output=True, text=True, check=False, timeout=2,
        )
    except Exception:  # noqa: BLE001
        return "\u2014", None
    if result.returncode != 0:
        return "\u2014", None
    ts_raw = (result.stdout or "").strip()
    if not ts_raw:
        return "\u2014", None
    try:
        ts = int(ts_raw)
    except ValueError:
        return "\u2014", None
    from datetime import UTC, datetime
    dt = datetime.fromtimestamp(ts, tz=UTC)
    delta = datetime.now(UTC) - dt
    total_s = max(0, int(delta.total_seconds()))
    return _format_commit_age(total_s), total_s


def _last_commit_age(project_path: Path, branch_name: str | None) -> str:
    """Return a short relative-age string for the worker's branch tip."""
    label, _seconds_ago = _last_commit_age_info(project_path, branch_name)
    return label


def _format_heartbeat_age(last_heartbeat_iso: str | None) -> str:
    """Return a tooltip-friendly relative heartbeat age."""
    if not last_heartbeat_iso:
        return "unknown"
    try:
        dt = datetime.fromisoformat(last_heartbeat_iso)
    except (TypeError, ValueError):
        return "unknown"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = datetime.now(UTC) - dt
    total_s = max(0, int(delta.total_seconds()))
    if total_s < 60:
        return "now"
    if total_s < 3600:
        return f"{total_s // 60}m ago"
    if total_s < 3600 * 24:
        return f"{total_s // 3600}h ago"
    return f"{total_s // (3600 * 24)}d ago"


def _format_token_total(total: int) -> str:
    if total < 1_000:
        return f"{total}"
    if total < 1_000_000:
        return f"{total / 1_000:.1f}K"
    return f"{total / 1_000_000:.1f}M"


_HANDOFF_NODES = frozenset({
    "code_review", "review", "user_approval", "review_handoff",
    # ``human_review`` is the parking node in the user-review flow
    # (``src/pollypm/work/flows/user-review.yaml``). The worker exits
    # there cleanly to wait for the user — same handoff shape as
    # ``user_approval`` in plan_project. Without it the gone session
    # rendered as 🔴 unresponsive instead of ⚪ handed_off.
    "human_review",
    # ``awaiting_approval`` is the human touchpoint in the downtime
    # explore flow (``plugins_builtin/downtime/flows/downtime_explore.yaml``).
    # The explorer hands off there for human review; the session
    # exits cleanly. Same handoff shape as ``human_review`` /
    # ``user_approval`` — without this, the post-explore gone session
    # rendered as 🔴 unresponsive instead of ⚪ handed_off.
    "awaiting_approval",
})


def _worker_health_snapshot(
    *,
    status: str,
    last_heartbeat_iso: str | None,
    token_total: int,
    session_name: str,
    current_node: str | None = None,
    last_commit_seconds_ago: int | None = None,
) -> tuple[str, str]:
    """Map worker state to a health bucket + tooltip.

    Distinguish "offline because the worker handed off cleanly" (task
    parked at a review node, worker session naturally gone) from
    "offline because the worker dropped" — the same red 🔴 glyph for
    both made expected handoffs look like faults.
    """
    if status == "offline" and current_node in _HANDOFF_NODES:
        health = "handed_off"
    elif status in {"stuck", "offline"}:
        health = "unresponsive"
    elif status == "idle":
        try:
            dt = (
                datetime.fromisoformat(last_heartbeat_iso)
                if last_heartbeat_iso
                else None
            )
        except (TypeError, ValueError):
            dt = None
        if dt is not None and dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        idle_long = dt is not None and dt <= datetime.now(UTC) - timedelta(minutes=5)
        recent_commit = (
            last_commit_seconds_ago is not None
            and last_commit_seconds_ago < 24 * 3600
        )
        health = (
            "idle_warn"
            if (dt is None and not recent_commit) or idle_long
            else "alive"
        )
    else:
        health = "alive"
    if health == "handed_off":
        heartbeat_part = (
            f"worker handed off at {current_node} — session ended naturally"
        )
    elif last_heartbeat_iso:
        heartbeat_part = f"last heartbeat {_format_heartbeat_age(last_heartbeat_iso)}"
    elif status in {"working", "idle"}:
        heartbeat_part = f"{status}; heartbeat not recorded"
        if last_commit_seconds_ago is not None and status == "idle":
            heartbeat_part += f"; last commit {_format_commit_age(last_commit_seconds_ago)}"
    else:
        heartbeat_part = "last heartbeat unknown"
    tooltip = (
        f"{heartbeat_part} · {_format_token_total(token_total)} tokens"
        f" · session: {session_name}"
    )
    return health, tooltip


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
            token_total = 0
            if task is not None:
                token_total = int(
                    (getattr(task, "total_input_tokens", 0) or 0)
                    + (getattr(task, "total_output_tokens", 0) or 0)
                )
            just_shipped, shipment_token = _recent_worker_shipment(task)
            window_name = f"task-{project_key}-{ws.task_number}"
            window = storage_windows.get(window_name)
            has_window = window is not None and not getattr(
                window, "pane_dead", False,
            )
            pane_stopped = (
                has_window
                and _tmux_window_has_stopped_descendant(tmux, window)
            )
            session_name = ws.agent_name or window_name

            last_heartbeat_iso: str | None = None
            heartbeat = None
            is_turn_active = False

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
                    heartbeat = supervisor.store.latest_heartbeat(session_name)
                except Exception:  # noqa: BLE001
                    heartbeat = None
                if heartbeat is not None:
                    last_heartbeat_iso = getattr(heartbeat, "created_at", None)

            if has_window and not pane_stopped:
                pane_text = read_recent_heartbeat_snapshot(heartbeat)
                if pane_text is None and tmux is not None:
                    try:
                        pane_text = tmux.capture_pane(window.pane_id, lines=15)
                    except Exception:  # noqa: BLE001
                        pane_text = ""
                if pane_text:
                    is_turn_active = _pane_text_shows_active_turn(pane_text)

            if not has_window:
                status = "offline"
            elif pane_stopped:
                status = "stuck"
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
            last_commit_label, last_commit_seconds_ago = _last_commit_age_info(
                project_path,
                ws.branch_name,
            )
            health, health_tooltip = _worker_health_snapshot(
                status=status,
                last_heartbeat_iso=last_heartbeat_iso,
                token_total=token_total,
                session_name=session_name,
                current_node=current_node,
                last_commit_seconds_ago=last_commit_seconds_ago,
            )

            rows.append(
                WorkerRosterRow(
                    project_key=project_key,
                    project_name=str(project_name),
                    session_name=session_name,
                    status=status,
                    health=health,
                    health_tooltip=health_tooltip,
                    task_id=task_id,
                    task_number=ws.task_number,
                    task_title=task_title,
                    current_node=current_node,
                    turn_label=turn_label,
                    last_commit_label=last_commit_label,
                    token_total=token_total,
                    tmux_window=window_name,
                    last_heartbeat=last_heartbeat_iso,
                    worktree_path=ws.worktree_path,
                    branch_name=ws.branch_name,
                    just_shipped=just_shipped,
                    shipment_token=shipment_token,
                )
            )

    # Surface storage-closet windows that aren't tracked as per-task
    # workers in any project's work-service DB. Two shapes land here:
    #
    # * long-running control sessions — architect-*, worker-*, pm-* —
    #   with no work_sessions row by design (#871). Without this, the
    #   Workers panel could read '0 sessions' while the storage-closet
    #   had nine live windows.
    # * per-task worker windows — task-<project>-<N> — that the live
    #   tmux server has but whose work_sessions row hasn't landed yet
    #   (architect-spawned children, mismatched project paths, race
    #   between window create + DB upsert). Filtering these out left
    #   the user blind to what was actually burning tokens (#996); the
    #   synthetic row below at least shows the window name + parsed
    #   project/task number so action affordances still work.
    if storage_windows:
        already_tracked = {row.tmux_window for row in rows}
        for window_name, window in storage_windows.items():
            if window_name in already_tracked:
                continue
            is_dead = bool(getattr(window, "pane_dead", False))
            if window_name.startswith("task-"):
                # task-<project>-<N> — parse so the row carries a real
                # task_number (the action affordance routes by it).
                suffix = window_name[len("task-") :]
                project_key, sep, task_num_raw = suffix.rpartition("-")
                if not sep or not project_key or not task_num_raw:
                    continue
                try:
                    task_number: int | None = int(task_num_raw)
                except ValueError:
                    continue
                project = projects.get(project_key)
                project_name = (
                    project.display_label() if project is not None and hasattr(project, "display_label")
                    else (getattr(project, "name", None) if project is not None else None)
                    or project_key
                )
                # Best-effort turn detection so the per-task row gets
                # the same working/idle dot the DB-backed rows do.
                pane_text = ""
                pane_id = getattr(window, "pane_id", None)
                pane_stopped = _tmux_window_has_stopped_descendant(tmux, window)
                if not is_dead and not pane_stopped and tmux is not None and pane_id:
                    try:
                        pane_text = tmux.capture_pane(pane_id, lines=15) or ""
                    except Exception:  # noqa: BLE001
                        pane_text = ""
                if is_dead:
                    status = "offline"
                elif pane_stopped:
                    status = "stuck"
                elif pane_text and _pane_text_shows_active_turn(pane_text):
                    status = "working"
                else:
                    status = "idle"
                health, health_tooltip = _worker_health_snapshot(
                    status=status,
                    last_heartbeat_iso=None,
                    token_total=0,
                    session_name=window_name,
                    current_node=None,
                )
                rows.append(
                    WorkerRosterRow(
                        project_key=project_key,
                        project_name=str(project_name),
                        session_name=window_name,
                        status=status,
                        health=health,
                        health_tooltip=health_tooltip,
                        task_id=None,
                        task_number=task_number,
                        task_title="(per-task worker)",
                        current_node=None,
                        turn_label="",
                        last_commit_label="—",
                        token_total=0,
                        tmux_window=window_name,
                        last_heartbeat=None,
                        worktree_path=None,
                        branch_name=None,
                        just_shipped=False,
                        shipment_token=None,
                    )
                )
                continue
            if not (
                window_name.startswith("architect-")
                or window_name.startswith("worker-")
                or window_name.startswith("pm-")
            ):
                continue
            kind, _, label = window_name.partition("-")
            project_key = label or "[workspace]"
            pane_stopped = _tmux_window_has_stopped_descendant(tmux, window)
            if is_dead:
                status = "offline"
            elif pane_stopped:
                status = "stuck"
            else:
                status = "idle"
            # Synthetic control-session rows have no per-task branch.
            # Fall back to the project's HEAD so the "Last commit" column
            # surfaces real activity instead of being empty for every
            # control session (#997). Rows whose label doesn't match a
            # configured project (e.g. workspace-level ``pm-heartbeat``)
            # keep the empty dash.
            synthetic_project = projects.get(project_key)
            synthetic_path = getattr(synthetic_project, "path", None)
            if isinstance(synthetic_path, Path):
                synthetic_last_commit, synthetic_last_commit_seconds_ago = (
                    _last_commit_age_info(synthetic_path, None)
                )
            else:
                synthetic_last_commit = "—"
                synthetic_last_commit_seconds_ago = None
            health, health_tooltip = _worker_health_snapshot(
                status=status,
                last_heartbeat_iso=None,
                token_total=0,
                session_name=window_name,
                current_node=None,
                last_commit_seconds_ago=synthetic_last_commit_seconds_ago,
            )
            rows.append(
                WorkerRosterRow(
                    project_key=project_key,
                    project_name=label or "[workspace]",
                    session_name=window_name,
                    status=status,
                    health=health,
                    health_tooltip=health_tooltip,
                    task_id=None,
                    task_number=None,
                    task_title=f"{kind.title()} session",
                    current_node=None,
                    turn_label="",
                    last_commit_label=synthetic_last_commit,
                    token_total=0,
                    tmux_window=window_name,
                    last_heartbeat=None,
                    worktree_path=None,
                    branch_name=None,
                    just_shipped=False,
                    shipment_token=None,
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
    color_overrides = load_worker_color_overrides(config_path)
    lines = ["Workers", ""]
    dot_for = {
        "working": "\u25cf", "idle": "\u25cb",
        "stuck": "\u25b2", "offline": "\u25cf",
    }
    for row in rows:
        dot = dot_for.get(row.status, "\u25cb")
        identity = worker_identity(row.session_name, color_overrides=color_overrides)
        task_part = (
            f"#{row.task_number} {row.task_title}"
            if row.task_number is not None else "(none)"
        )
        lines.append(
            f"  {dot} {row.status:<7}  {row.project_name:<18}  "
            f"{(identity.avatar + ' ' + row.session_name):<22}  {task_part}  @{row.current_node or '-'}  "
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
