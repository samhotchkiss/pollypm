from __future__ import annotations

from pathlib import Path

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Static, TabbedContent, TabPane

from pollypm.cockpit_task_review import (
    load_task_review_artifact,
    render_task_review_artifact,
)
from pollypm.config import load_config
from pollypm.session_services import create_tmux_client
from pollypm.tz import format_time as _fmt_time

_TASK_STATUS_ORDER = {
    "in_progress": 0,
    "review": 1,
    "queued": 2,
    "blocked": 3,
    "on_hold": 4,
    "draft": 5,
    "done": 6,
    "cancelled": 7,
}

_FILTER_LABELS = {
    "all": "All",
    "active": "Active",
    "review": "Review",
    "blocked": "Blocked",
    "done": "Done",
}


def _format_relative_age(value) -> str:
    """Render a human age from a datetime or ISO string."""
    if not value:
        return ""
    from datetime import datetime as _dt

    iso_str = value.isoformat() if isinstance(value, _dt) else str(value)
    try:
        from pollypm.tz import format_relative

        return format_relative(iso_str)
    except Exception:  # noqa: BLE001
        return iso_str[:16]


def _format_event_time(value) -> str:
    """Stable local timestamp formatting for task detail rows."""
    if not value:
        return ""
    iso = value.isoformat() if hasattr(value, "isoformat") else str(value)
    return _fmt_time(iso)


def _timestamp_sort_value(value) -> float:
    """Sortable timestamp helper tolerant of missing / malformed inputs."""
    if not value:
        return 0.0
    try:
        if hasattr(value, "timestamp"):
            return float(value.timestamp())
        from datetime import datetime as _dt

        return float(_dt.fromisoformat(str(value)).timestamp())
    except Exception:  # noqa: BLE001
        return 0.0


def _task_sort_key(task) -> tuple[int, float, int]:
    """Sort tasks by status priority, then newest activity, then task number."""
    status = getattr(getattr(task, "work_status", None), "value", None) or str(
        getattr(task, "work_status", "") or ""
    )
    updated = getattr(task, "updated_at", None) or getattr(task, "created_at", None)
    return (
        _TASK_STATUS_ORDER.get(status, 99),
        -_timestamp_sort_value(updated),
        int(getattr(task, "task_number", 0) or 0),
    )


def _format_stage_label(task, flow) -> str:
    node_id = getattr(task, "current_node_id", None)
    if not node_id:
        return "—"
    if flow is None:
        return str(node_id)
    node = getattr(flow, "nodes", {}).get(node_id)
    if node is None:
        return str(node_id)
    parts = [str(node_id)]
    node_type = getattr(getattr(node, "type", None), "value", None) or getattr(
        node, "type", None
    )
    if node_type:
        parts.append(str(node_type))
    actor = (
        getattr(node, "actor_role", None)
        or getattr(node, "agent_name", None)
        or getattr(getattr(node, "actor_type", None), "value", None)
        or getattr(node, "actor_type", None)
    )
    if actor:
        parts.append(str(actor))
    return " · ".join(parts)


def _peek_session_tail(pane_id: str | None) -> list[str]:
    if not pane_id:
        return []
    try:
        pane_text = create_tmux_client().capture_pane(pane_id, lines=12)
    except Exception:  # noqa: BLE001
        return []
    lines = [line.rstrip() for line in pane_text.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    return lines[-8:]


def _task_counts(tasks: list) -> dict[str, int]:
    counts: dict[str, int] = {}
    for task in tasks:
        status = getattr(getattr(task, "work_status", None), "value", None) or str(
            getattr(task, "work_status", "") or ""
        )
        counts[status] = counts.get(status, 0) + 1
    return counts


def _task_matches_status(task, status_filter: str) -> bool:
    status = getattr(getattr(task, "work_status", None), "value", None) or str(
        getattr(task, "work_status", "") or ""
    )
    if status_filter == "all":
        return True
    if status_filter == "active":
        return status not in {"done", "cancelled"}
    if status_filter == "review":
        return status == "review"
    if status_filter == "blocked":
        return status in {"blocked", "on_hold"}
    if status_filter == "done":
        return status in {"done", "cancelled"}
    return True


def _task_matches_query(task, owner: str | None, query: str) -> bool:
    query = (query or "").strip().lower()
    if not query:
        return True
    haystack = " ".join(
        [
            getattr(task, "task_id", "") or "",
            getattr(task, "title", "") or "",
            getattr(task, "description", "") or "",
            getattr(task, "assignee", "") or "",
            owner or "",
            getattr(task, "current_node_id", "") or "",
            getattr(task, "flow_template_id", "") or "",
        ]
    ).lower()
    return query in haystack


def _render_overview(task, *, owner: str | None, flow, active_session) -> str:
    icon = PollyTasksApp._STATUS_ICONS.get(task.work_status.value, "·")
    lines = [
        f"{icon} #{task.task_number} {task.title}",
        "",
        f"Status     {task.work_status.value}",
        f"Priority   {task.priority.value}",
        f"Flow       {task.flow_template_id}",
        f"Stage      {_format_stage_label(task, flow)}",
        f"Owner      {owner or '—'}",
    ]
    if task.assignee:
        lines.append(f"Assignee   {task.assignee}")
    if task.roles:
        roles = ", ".join(f"{key}={value}" for key, value in task.roles.items())
        lines.append(f"Roles      {roles}")
    tokens_in = getattr(task, "total_input_tokens", 0) or 0
    tokens_out = getattr(task, "total_output_tokens", 0) or 0
    session_count = getattr(task, "session_count", 0) or 0
    if tokens_in or tokens_out or session_count:
        lines.append(
            f"Tokens     in={tokens_in}  out={tokens_out}  sessions={session_count}"
        )
    updated = _format_event_time(getattr(task, "updated_at", None))
    updated_rel = _format_relative_age(getattr(task, "updated_at", None))
    if updated:
        line = f"Updated    {updated}"
        if updated_rel:
            line += f" · {updated_rel}"
        lines.append(line)
    if active_session is not None:
        lines.append(f"Session    {active_session.agent_name}")
    if task.description:
        lines.extend(["", "Description", "", task.description])
    if task.acceptance_criteria:
        lines.extend(["", "Acceptance Criteria", "", task.acceptance_criteria])
    if task.constraints:
        lines.extend(["", "Constraints", "", task.constraints])
    if task.relevant_files:
        lines.extend(["", "Relevant Files", ""])
        lines.extend(f"- {path}" for path in task.relevant_files)
    return "\n".join(lines)


def _render_context(task) -> str:
    if not getattr(task, "context", None):
        return "No context entries yet."
    lines: list[str] = []
    for entry in task.context:
        ts = _format_event_time(getattr(entry, "timestamp", None))
        age = _format_relative_age(getattr(entry, "timestamp", None))
        meta = [getattr(entry, "actor", "") or "system"]
        if ts:
            meta.append(ts)
        if age:
            meta.append(age)
        lines.append(" · ".join(meta))
        text = getattr(entry, "text", "") or ""
        lines.append(text if text else "(empty)")
        lines.append("")
    return "\n".join(lines).rstrip()


def _render_live(task, active_session) -> str:
    if active_session is None:
        return "No active worker session is currently attached to this task."
    lines = [f"Session    {active_session.agent_name}"]
    if active_session.branch_name:
        lines.append(f"Branch     {active_session.branch_name}")
    if active_session.worktree_path:
        lines.append(f"Worktree   {active_session.worktree_path}")
    started = _format_event_time(active_session.started_at)
    started_rel = _format_relative_age(active_session.started_at)
    if started:
        line = f"Started    {started}"
        if started_rel:
            line += f" · {started_rel}"
        lines.append(line)
    peek_lines = _peek_session_tail(active_session.pane_id)
    lines.extend(["", "Peek", ""])
    if peek_lines:
        lines.extend(peek_lines)
    else:
        lines.append("unavailable")
    transcript_root = Path.home() / ".pollypm"
    transcript_dir = transcript_root / "transcripts" / "tasks" / task.task_id
    if transcript_dir.exists():
        lines.extend(["", "Transcript", "", str(transcript_dir)])
    return "\n".join(lines)


class _TaskRejectReasonModal(ModalScreen[str | None]):
    """Small modal that captures a rejection reason for review tasks."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]
    CSS = """
    Screen {
        align: center middle;
        background: rgba(8, 12, 15, 0.65);
    }
    #task-reject-modal {
        width: 72;
        height: auto;
        padding: 1 2;
        border: round #394754;
        background: #111920;
    }
    #task-reject-actions {
        height: auto;
        padding-top: 1;
    }
    #task-reject-actions Button {
        margin-right: 1;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.reason_input = Input(
            placeholder="Why is this being rejected?",
            id="task-reject-reason",
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="task-reject-modal"):
            yield Static("Reject Review Task", id="task-reject-title")
            yield Static(
                "Record a concrete reason so the next pass has a clear target.",
                id="task-reject-copy",
            )
            yield self.reason_input
            with Horizontal(id="task-reject-actions"):
                yield Button("Reject", id="task-reject-submit", variant="warning")
                yield Button("Cancel", id="task-reject-cancel")

    def on_mount(self) -> None:
        self.reason_input.focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted, "#task-reject-reason")
    def _on_submit(self, event: Input.Submitted) -> None:
        reason = (event.value or "").strip()
        self.dismiss(reason or None)

    @on(Button.Pressed, "#task-reject-submit")
    def _on_press_submit(self) -> None:
        reason = (self.reason_input.value or "").strip()
        self.dismiss(reason or None)

    @on(Button.Pressed, "#task-reject-cancel")
    def _on_press_cancel(self) -> None:
        self.dismiss(None)


class PollyTasksApp(App[None]):
    """Interactive task board for a single project.

    Contract:
    - Input: a Polly config path and one project key.
    - Data source: one project-local WorkService rooted at that project's
      `.pollypm/state.db`.
    - Output: a split-pane Textual UI with replaceable modules:
      task filtering/table, overview rendering, timeline rendering, context
      rendering, and live-session rendering.
    """

    TITLE = "PollyPM"
    SUB_TITLE = "Tasks"
    BINDINGS = [
        Binding("slash", "focus_search", "Search", show=False),
        Binding("r", "refresh", "Refresh"),
        Binding("a", "approve_task", "Approve"),
        Binding("x", "reject_task", "Reject"),
        Binding("o", "refresh_live", "Refresh Live", show=False),
        Binding("escape", "back", "Back"),
    ]
    CSS = """
    Screen { background: #10161b; color: #eef2f4; }
    #tasks-root { height: 1fr; }
    #tasks-toolbar {
        height: auto;
        padding: 1 1 0 1;
        background: #0d1419;
    }
    #tasks-heading {
        width: 18;
        padding: 1 1 0 0;
        color: #8db7ff;
        text-style: bold;
    }
    #tasks-search {
        width: 1fr;
        margin: 0 1 0 0;
    }
    #tasks-filters {
        width: auto;
        height: auto;
        padding-top: 0;
    }
    #tasks-filters Button {
        margin-left: 1;
        min-width: 8;
    }
    #tasks-body { height: 1fr; }
    #tasks-list-pane {
        width: 46;
        min-width: 38;
        padding: 0 1;
        border-right: tall #24303b;
    }
    #tasks-summary {
        height: auto;
        color: #97a6b2;
        padding: 1 0;
    }
    #tasks-table {
        height: 1fr;
    }
    #tasks-status {
        height: auto;
        color: #6b7a88;
        padding: 1 0;
    }
    #tasks-detail-pane {
        width: 1fr;
        padding: 0 1;
    }
    #task-header {
        height: auto;
        padding: 1 0 0 0;
        color: #eef6ff;
    }
    #task-actions {
        height: auto;
        padding: 1 0;
    }
    #task-actions Button {
        margin-right: 1;
    }
    #task-tabs { height: 1fr; }
    #task-detail-scroll,
    #task-review-scroll,
    #task-context-scroll,
    #task-live-scroll {
        height: 1fr;
        overflow-y: auto;
    }
    #task-detail,
    #task-review,
    #task-context,
    #task-live {
        padding: 0 1 1 1;
    }
    #task-timeline {
        height: 1fr;
    }
    """

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

    def __init__(self, config_path: Path, project_key: str) -> None:
        super().__init__()
        self.config_path = config_path
        self.project_key = project_key
        self._tasks: list = []
        self._owner_by_task_id: dict[str, str | None] = {}
        self._selected_task_id: str | None = None
        self._status_filter = "active"
        self._search_query = ""
        self.search_input = Input(
            placeholder="Search task, owner, stage, assignee…",
            id="tasks-search",
        )
        self.summary = Static("", id="tasks-summary")
        self.status = Static("", id="tasks-status")
        self.task_table = DataTable(id="tasks-table", zebra_stripes=True)
        self.detail_header = Static("", id="task-header")
        self.detail_overview = Static("", id="task-detail")
        self.detail_review = Static("", id="task-review")
        self.detail_context = Static("", id="task-context")
        self.detail_live = Static("", id="task-live")
        self.timeline = DataTable(id="task-timeline", zebra_stripes=True)
        self.filter_buttons = {
            key: Button(label, id=f"tasks-filter-{key}")
            for key, label in _FILTER_LABELS.items()
        }
        self.approve_button = Button("Approve", id="task-approve", variant="success")
        self.reject_button = Button("Reject", id="task-reject", variant="warning")
        self.refresh_live_button = Button("Refresh Live", id="task-refresh-live")

    def compose(self) -> ComposeResult:
        with Vertical(id="tasks-root"):
            with Horizontal(id="tasks-toolbar"):
                yield Static(f"Tasks · {self.project_key}", id="tasks-heading")
                yield self.search_input
                with Horizontal(id="tasks-filters"):
                    for button in self.filter_buttons.values():
                        yield button
            with Horizontal(id="tasks-body"):
                with Vertical(id="tasks-list-pane"):
                    yield self.summary
                    yield self.task_table
                    yield self.status
                with Vertical(id="tasks-detail-pane"):
                    yield self.detail_header
                    with Horizontal(id="task-actions"):
                        yield self.approve_button
                        yield self.reject_button
                        yield self.refresh_live_button
                    with TabbedContent(initial="task-tab-overview", id="task-tabs"):
                        with TabPane("Overview", id="task-tab-overview"):
                            with VerticalScroll(id="task-detail-scroll"):
                                yield self.detail_overview
                        with TabPane("Review", id="task-tab-review"):
                            with VerticalScroll(id="task-review-scroll"):
                                yield self.detail_review
                        with TabPane("Timeline", id="task-tab-timeline"):
                            yield self.timeline
                        with TabPane("Context", id="task-tab-context"):
                            with VerticalScroll(id="task-context-scroll"):
                                yield self.detail_context
                        with TabPane("Live", id="task-tab-live"):
                            with VerticalScroll(id="task-live-scroll"):
                                yield self.detail_live

    def on_mount(self) -> None:
        self._configure_tables()
        self._sync_filter_buttons()
        self._set_detail_empty("No task selected.")
        self._refresh_list(select_first=True)
        self.set_interval(8, self._background_refresh)
        self.task_table.focus()

    def _configure_tables(self) -> None:
        self.task_table.cursor_type = "row"
        self.task_table.add_columns("ID", "Status", "Title", "Owner", "Stage", "Updated")
        self.timeline.cursor_type = "row"
        self.timeline.add_columns("Stage", "State", "Started", "Completed", "Details")

    def _get_svc(self):
        from pollypm.work.sqlite_service import SQLiteWorkService

        config = load_config(self.config_path)
        project = config.projects.get(self.project_key)
        if not project:
            return None
        db_path = project.path / ".pollypm" / "state.db"
        if not db_path.exists():
            return None
        return SQLiteWorkService(db_path=db_path, project_path=project.path)

    def _load_tasks(self) -> tuple[list, dict[str, str | None]]:
        svc = self._get_svc()
        if svc is None:
            return [], {}
        try:
            tasks = list(svc.list_tasks(project=self.project_key))
            owners = {task.task_id: svc.derive_owner(task) for task in tasks}
        finally:
            svc.close()
        tasks.sort(key=_task_sort_key)
        return tasks, owners

    def _filtered_tasks(self) -> list:
        visible: list = []
        for task in self._tasks:
            if not _task_matches_status(task, self._status_filter):
                continue
            owner = self._owner_by_task_id.get(task.task_id)
            if not _task_matches_query(task, owner, self._search_query):
                continue
            visible.append(task)
        return visible

    def _summary_text(self, visible: list) -> str:
        counts = _task_counts(self._tasks)
        parts: list[str] = []
        for status in ("in_progress", "review", "queued", "blocked", "on_hold", "done"):
            count = counts.get(status, 0)
            if count:
                icon = self._STATUS_ICONS.get(status, "·")
                parts.append(f"{icon} {count} {status.replace('_', ' ')}")
        total = len(self._tasks)
        shown = len(visible)
        prefix = f"{shown} of {total} shown" if shown != total else f"{total} tasks"
        return prefix if not parts else prefix + "  ·  " + "  ·  ".join(parts)

    def _current_task_key(self) -> str | None:
        if self.task_table.row_count == 0 or self.task_table.cursor_row < 0:
            return None
        try:
            row_key = self.task_table.coordinate_to_cell_key(
                (self.task_table.cursor_row, 0)
            ).row_key
        except Exception:  # noqa: BLE001
            return None
        return str(row_key.value) if row_key is not None else None

    def _sync_filter_buttons(self) -> None:
        for key, button in self.filter_buttons.items():
            button.variant = "primary" if key == self._status_filter else "default"

    def _set_detail_empty(self, message: str) -> None:
        self._selected_task_id = None
        self.detail_header.update(message)
        self.detail_overview.update("")
        self.detail_review.update("")
        self.detail_context.update("")
        self.detail_live.update("")
        self.timeline.clear()
        self.approve_button.disabled = True
        self.reject_button.disabled = True
        self.refresh_live_button.disabled = True

    def _render_table(self, *, select_first: bool) -> None:
        visible = self._filtered_tasks()
        self.summary.update(self._summary_text(visible))
        self.status.update(
            f"Filter: {_FILTER_LABELS.get(self._status_filter, self._status_filter)}"
            + (
                f"  ·  Search: {self._search_query}"
                if self._search_query.strip()
                else "  ·  / search · a approve · x reject"
            )
        )
        previous = self._selected_task_id
        self.task_table.clear()
        if not visible:
            self._set_detail_empty("No tasks match the current filter.")
            return
        for task in visible:
            owner = self._owner_by_task_id.get(task.task_id) or "—"
            updated = _format_event_time(
                getattr(task, "updated_at", None) or getattr(task, "created_at", None)
            )
            self.task_table.add_row(
                f"#{task.task_number}",
                task.work_status.value,
                task.title,
                owner,
                task.current_node_id or "—",
                updated or "—",
                key=task.task_id,
            )
        target_id = previous if any(t.task_id == previous for t in visible) else None
        if target_id is None and (select_first or previous is None):
            target_id = visible[0].task_id
        if target_id is None:
            target_id = visible[0].task_id
        row_index = next(
            (index for index, task in enumerate(visible) if task.task_id == target_id),
            0,
        )
        self.task_table.move_cursor(row=row_index, column=0, animate=False, scroll=True)
        self._show_detail(target_id)

    def _refresh_list(self, *, select_first: bool = False) -> None:
        self._tasks, self._owner_by_task_id = self._load_tasks()
        self._render_table(select_first=select_first)

    def _render_timeline(self, executions: list) -> None:
        self.timeline.clear()
        if not executions:
            return
        for execution in executions:
            status = getattr(getattr(execution, "status", None), "value", None) or str(
                getattr(execution, "status", "") or ""
            )
            if status == "active":
                marker = "⟳"
            elif getattr(execution, "decision", None) is not None:
                decision_value = getattr(
                    getattr(execution, "decision", None),
                    "value",
                    getattr(execution, "decision", None),
                )
                marker = "✓" if decision_value == "approved" else "✗"
            else:
                marker = "●"
            started = _format_event_time(getattr(execution, "started_at", None)) or "—"
            completed = _format_event_time(getattr(execution, "completed_at", None)) or "—"
            details: list[str] = []
            age = _format_relative_age(
                getattr(execution, "completed_at", None)
                or getattr(execution, "started_at", None)
            )
            if age:
                details.append(age)
            decision = getattr(execution, "decision", None)
            if decision is not None:
                decision_value = getattr(decision, "value", decision)
                details.append(str(decision_value))
            if getattr(execution, "decision_reason", None):
                details.append(str(execution.decision_reason))
            output = getattr(execution, "work_output", None)
            if output is not None and getattr(output, "summary", None):
                details.append(str(output.summary))
            self.timeline.add_row(
                f"{marker} {execution.node_id}",
                status,
                started,
                completed,
                " · ".join(details) if details else "—",
                key=f"{execution.node_id}:{execution.visit}",
            )

    def _render_selected_task(self, task, *, owner: str | None, flow, active_session) -> None:
        icon = self._STATUS_ICONS.get(task.work_status.value, "·")
        header_bits = [
            f"{icon} #{task.task_number} {task.title}",
            f"{task.work_status.value} · {_format_relative_age(task.updated_at or task.created_at)}",
        ]
        self.detail_header.update("\n".join(header_bits))
        self.detail_overview.update(
            _render_overview(
                task,
                owner=owner,
                flow=flow,
                active_session=active_session,
            )
        )
        self.detail_context.update(_render_context(task))
        self.detail_live.update(_render_live(task, active_session))
        self._render_timeline(list(getattr(task, "executions", None) or []))
        in_review = task.work_status.value == "review"
        self.approve_button.disabled = not in_review
        self.reject_button.disabled = not in_review
        self.refresh_live_button.disabled = active_session is None

    def _project_path(self) -> Path | None:
        try:
            config = load_config(self.config_path)
        except Exception:  # noqa: BLE001
            return None
        project = getattr(config, "projects", {}).get(self.project_key)
        if project is None:
            return None
        return getattr(project, "path", None)

    def _show_detail(self, task_id: str) -> None:
        previous_task_id = self._selected_task_id
        svc = self._get_svc()
        if svc is None:
            self._set_detail_empty("Could not open the project work service.")
            return
        try:
            task = svc.get(task_id)
            task.context = svc.get_context(task_id, limit=15)
            task.executions = svc.get_execution(task_id)
            owner = svc.derive_owner(task)
            try:
                flow = svc.get_flow(task.flow_template_id, project=task.project)
            except Exception:  # noqa: BLE001
                flow = None
            try:
                active_session = svc.get_worker_session(
                    task_project=task.project,
                    task_number=task.task_number,
                    active_only=True,
                )
            except Exception:  # noqa: BLE001
                active_session = None
        except Exception as exc:  # noqa: BLE001
            self._set_detail_empty(f"Error loading task: {exc}")
            return
        finally:
            try:
                svc.close()
            except Exception:  # noqa: BLE001
                pass
        self._selected_task_id = task_id
        self._owner_by_task_id[task.task_id] = owner
        review_artifact = load_task_review_artifact(task, self._project_path())
        self._render_selected_task(
            task,
            owner=owner,
            flow=flow,
            active_session=active_session,
        )
        self.detail_review.update(render_task_review_artifact(review_artifact))
        tabs = self.query_one("#task-tabs", TabbedContent)
        if task_id != previous_task_id:
            tabs.active = (
                "task-tab-review"
                if task.work_status.value == "review" and review_artifact is not None
                else "task-tab-overview"
            )

    def _background_refresh(self) -> None:
        try:
            self._refresh_list(select_first=False)
        except Exception:  # noqa: BLE001
            pass

    def action_focus_search(self) -> None:
        self.search_input.focus()
        self.search_input.cursor_position = len(self.search_input.value)

    def action_back(self) -> None:
        if self.search_input.has_focus:
            self.task_table.focus()
            return
        self.exit()

    def action_refresh(self) -> None:
        self._refresh_list(select_first=False)

    def action_refresh_live(self) -> None:
        if not self._selected_task_id:
            return
        tabs = self.query_one("#task-tabs", TabbedContent)
        tabs.active = "task-tab-live"
        self._show_detail(self._selected_task_id)

    def _review_task(self, task_id: str, *, decision: str, reason: str | None = None) -> None:
        svc = self._get_svc()
        if svc is None:
            self.notify("Could not open project database.", severity="error")
            return
        try:
            task = svc.get(task_id)
            if task.work_status.value != "review":
                self.notify("Task is not in review state.", severity="warning")
                return
            if decision == "approve":
                svc.approve(task_id, "user", reason or "Approved from task cockpit")
                self.notify(f"Approved {task_id}", severity="information")
            else:
                svc.reject(task_id, "user", reason or "Rejected from task cockpit")
                self.notify(f"Rejected {task_id}", severity="information")
        except Exception as exc:  # noqa: BLE001
            action = "Approve" if decision == "approve" else "Reject"
            self.notify(f"{action} failed: {exc}", severity="error")
        finally:
            try:
                svc.close()
            except Exception:  # noqa: BLE001
                pass
        self._refresh_list(select_first=False)

    def action_approve_task(self) -> None:
        if not self._selected_task_id:
            return
        self._review_task(self._selected_task_id, decision="approve")

    def action_reject_task(self) -> None:
        if not self._selected_task_id:
            return

        def _after(reason: str | None) -> None:
            if reason is None:
                return
            self._review_task(
                self._selected_task_id or "",
                decision="reject",
                reason=reason,
            )

        self.push_screen(_TaskRejectReasonModal(), _after)

    @on(Input.Changed, "#tasks-search")
    def _on_search_changed(self, event: Input.Changed) -> None:
        self._search_query = event.value or ""
        self._render_table(select_first=True)

    @on(Input.Submitted, "#tasks-search")
    def _on_search_submitted(self) -> None:
        self.task_table.focus()

    @on(Button.Pressed, "#tasks-filter-all")
    def _on_filter_all(self) -> None:
        self._status_filter = "all"
        self._sync_filter_buttons()
        self._render_table(select_first=True)

    @on(Button.Pressed, "#tasks-filter-active")
    def _on_filter_active(self) -> None:
        self._status_filter = "active"
        self._sync_filter_buttons()
        self._render_table(select_first=True)

    @on(Button.Pressed, "#tasks-filter-review")
    def _on_filter_review(self) -> None:
        self._status_filter = "review"
        self._sync_filter_buttons()
        self._render_table(select_first=True)

    @on(Button.Pressed, "#tasks-filter-blocked")
    def _on_filter_blocked(self) -> None:
        self._status_filter = "blocked"
        self._sync_filter_buttons()
        self._render_table(select_first=True)

    @on(Button.Pressed, "#tasks-filter-done")
    def _on_filter_done(self) -> None:
        self._status_filter = "done"
        self._sync_filter_buttons()
        self._render_table(select_first=True)

    @on(Button.Pressed, "#task-approve")
    def _on_press_approve(self) -> None:
        self.action_approve_task()

    @on(Button.Pressed, "#task-reject")
    def _on_press_reject(self) -> None:
        self.action_reject_task()

    @on(Button.Pressed, "#task-refresh-live")
    def _on_press_refresh_live(self) -> None:
        self.action_refresh_live()

    @on(DataTable.RowHighlighted, "#tasks-table")
    def _on_task_highlighted(self) -> None:
        task_id = self._current_task_key()
        if task_id is None or task_id == self._selected_task_id:
            return
        self._show_detail(task_id)

    @on(DataTable.RowSelected, "#tasks-table")
    def _on_task_selected(self) -> None:
        task_id = self._current_task_key()
        if task_id is None:
            return
        self._show_detail(task_id)
