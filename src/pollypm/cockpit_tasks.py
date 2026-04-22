from __future__ import annotations

import difflib
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Static, TabbedContent, TabPane

from pollypm.approval_notifications import notify_task_approved
from pollypm.cockpit_task_priority import (
    priority_glyph,
    priority_label,
    priority_rank,
)
from pollypm.cockpit_task_review import (
    extract_confidence_score,
    load_task_review_artifact,
    render_task_review_artifact,
)
from pollypm.cockpit_formatting import format_event_time
from pollypm.cockpit_formatting import format_relative_age as _format_relative_age
from pollypm.config import load_config, project_config_path
from pollypm.rejection_feedback import (
    RejectionFeedbackNotice,
    is_rejection_feedback_task,
    unread_rejection_feedback,
)
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

_PENDING_UNDO_SECONDS = 5.0


@dataclass(slots=True)
class _PendingReviewAction:
    task_ids: tuple[str, ...]
    task_numbers: tuple[int, ...]
    decision: str
    reason: str | None
    deadline: float


class _TaskLiveScroll(VerticalScroll):
    """Live-pane scroll container that pauses tailing when the user scrolls up."""

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        app = getattr(self, "app", None)
        if not isinstance(app, PollyTasksApp):
            return
        if new_value >= self.max_scroll_y:
            app._set_live_tail_paused(False)
        elif new_value < old_value:
            app._set_live_tail_paused(True)

    def action_scroll_up(self) -> None:
        self.scroll_up(animate=False, force=True)
        app = getattr(self, "app", None)
        if isinstance(app, PollyTasksApp):
            app._set_live_tail_paused(True)

    @on(events.MouseScrollUp)
    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        event.stop()
        self.action_scroll_up()


def _execution_completed_output_lines(execution) -> list[str]:
    work_output = getattr(execution, "work_output", None)
    if work_output is None:
        return []
    lines = [f"Summary: {getattr(work_output, 'summary', '') or '(empty)'}"]
    for artifact in getattr(work_output, "artifacts", []) or []:
        kind = getattr(getattr(artifact, "kind", None), "value", None) or getattr(
            artifact, "kind", None
        )
        bits = [str(kind or "artifact")]
        description = getattr(artifact, "description", None)
        if description:
            bits.append(str(description))
        ref = getattr(artifact, "ref", None)
        if ref:
            bits.append(f"ref={ref}")
        path = getattr(artifact, "path", None)
        if path:
            bits.append(f"path={path}")
        external_ref = getattr(artifact, "external_ref", None)
        if external_ref:
            bits.append(f"external={external_ref}")
        lines.append(" | ".join(bits))
    return lines


def _execution_snapshot_label(execution) -> str:
    node_id = getattr(execution, "node_id", "submission")
    visit = getattr(execution, "visit", "?")
    completed_at = getattr(execution, "completed_at", None) or getattr(
        execution, "started_at", None
    )
    if completed_at is None:
        return f"{node_id} v{visit}"
    return f"{node_id} v{visit} · {_format_event_time(completed_at)}"


def _review_submission_executions(task) -> list:
    submissions: list = []
    for execution in list(getattr(task, "executions", None) or []):
        if getattr(execution, "work_output", None) is not None:
            submissions.append(execution)
    submissions.sort(
        key=lambda execution: (
            _timestamp_sort_value(
                getattr(execution, "completed_at", None)
                or getattr(execution, "started_at", None)
            ),
            int(getattr(execution, "visit", 0) or 0),
        )
    )
    return submissions


def _render_review_submission_diff(task) -> str:
    submissions = _review_submission_executions(task)
    if len(submissions) < 2:
        return "No resubmission diff yet."
    before = submissions[-2]
    after = submissions[-1]
    before_lines = _execution_completed_output_lines(before)
    after_lines = _execution_completed_output_lines(after)
    diff_lines = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=_execution_snapshot_label(before),
            tofile=_execution_snapshot_label(after),
            lineterm="",
        )
    )
    if not diff_lines:
        return "No changes between the latest two submissions."
    rendered = ["[b]Resubmission Diff[/b]"]
    for line in diff_lines:
        if line.startswith(("---", "+++")):
            rendered.append(f"[dim]{line}[/dim]")
        elif line.startswith("@@"):
            rendered.append(f"[yellow]{line}[/yellow]")
        elif line.startswith("+"):
            rendered.append(f"[green]{line}[/green]")
        elif line.startswith("-"):
            rendered.append(f"[red]{line}[/red]")
        else:
            rendered.append(line)
    return "\n".join(rendered)


def _task_has_resubmission_diff(task) -> bool:
    submissions = _review_submission_executions(task)
    if len(submissions) < 2:
        return False
    return any(
        getattr(getattr(execution, "decision", None), "value", None) == "rejected"
        for execution in getattr(task, "executions", None) or []
    )


def _review_confidence_score(task, review_artifact) -> int | None:
    if review_artifact is not None and getattr(review_artifact, "confidence", None) is not None:
        return int(review_artifact.confidence)
    executions = list(getattr(task, "executions", None) or [])
    executions.sort(
        key=lambda execution: (
            _timestamp_sort_value(
                getattr(execution, "completed_at", None)
                or getattr(execution, "started_at", None)
            ),
            int(getattr(execution, "visit", 0) or 0),
        ),
        reverse=True,
    )
    for execution in executions:
        score = extract_confidence_score(getattr(execution, "decision_reason", None))
        if score is not None:
            return score
        work_output = getattr(execution, "work_output", None)
        if work_output is None:
            continue
        score = extract_confidence_score(getattr(work_output, "summary", None))
        if score is not None:
            return score
        for artifact in getattr(work_output, "artifacts", []) or []:
            score = extract_confidence_score(getattr(artifact, "description", None))
            if score is not None:
                return score
    return None


def _review_confidence_markup(score: int | None) -> str:
    if score is None:
        return ""
    if score >= 8:
        style = "black on #2f9e44"
    elif score >= 5:
        style = "black on #f4b942"
    else:
        style = "black on #d94841"
    return f"[{style}] Russell: {score}/10 [/] "


def _format_event_time(value) -> str:
    return format_event_time(value, formatter=_fmt_time)


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


def _task_sort_key(task) -> tuple[int, int, float, int]:
    """Sort tasks by status, then task priority, then newest activity."""
    status = getattr(getattr(task, "work_status", None), "value", None) or str(
        getattr(task, "work_status", "") or ""
    )
    updated = getattr(task, "updated_at", None) or getattr(task, "created_at", None)
    return (
        _TASK_STATUS_ORDER.get(status, 99),
        priority_rank(task),
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


def _project_auto_merge_on_approve_enabled(project_path: Path | None) -> bool:
    """Read the per-project auto-merge flag, defaulting on for review flow."""

    if project_path is None:
        return True
    config_file = project_config_path(project_path)
    if not config_file.exists():
        return True
    try:
        raw = tomllib.loads(config_file.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return True
    for section_name in ("project", "cockpit", "task_ui"):
        section = raw.get(section_name)
        if isinstance(section, dict) and "auto_merge_on_approve" in section:
            return bool(section["auto_merge_on_approve"])
    return True


def _task_pr_number(task) -> str | None:
    refs = getattr(task, "external_refs", {}) or {}
    for key in ("github_pr", "github_pr_number", "pull_request", "pr_number"):
        value = refs.get(key)
        if value in (None, ""):
            continue
        text = str(value).strip()
        if not text:
            continue
        if text.startswith("http"):
            text = text.rstrip("/").rsplit("/", 1)[-1]
        if text.isdigit():
            return text
    return None


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


def _render_overview(
    task,
    *,
    owner: str | None,
    flow,
    active_session,
    rejection_feedback: RejectionFeedbackNotice | None = None,
) -> str:
    icon = PollyTasksApp._STATUS_ICONS.get(task.work_status.value, "·")
    lines = [
        f"{icon} #{task.task_number} {priority_glyph(task)} {task.title}",
        "",
        f"Status     {task.work_status.value}",
        f"Priority   {priority_label(task)}",
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
    if rejection_feedback is not None:
        lines.extend(
            [
                "",
                "Inbox Feedback",
                "",
                f"Status     Rejected — feedback in inbox ({rejection_feedback.inbox_task_id})",
                f"Preview    {rejection_feedback.preview}",
            ]
        )
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


class _TaskRejectFixModal(ModalScreen[str | None]):
    """Follow-up modal for the "write fix instructions" reject path."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]
    CSS = """
    Screen {
        align: center middle;
        background: rgba(8, 12, 15, 0.65);
    }
    #task-reject-fix-modal {
        width: 72;
        height: auto;
        padding: 1 2;
        border: round #394754;
        background: #111920;
    }
    #task-reject-fix-actions {
        height: auto;
        padding-top: 1;
    }
    #task-reject-fix-actions Button {
        margin-right: 1;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.reason_input = Input(
            placeholder="Write the fix instructions...",
            id="task-reject-fix-reason",
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="task-reject-fix-modal"):
            yield Static("Write Fix Instructions", id="task-reject-fix-title")
            yield Static(
                "Be specific enough that the next pass can move immediately.",
                id="task-reject-fix-copy",
            )
            yield self.reason_input
            with Horizontal(id="task-reject-fix-actions"):
                yield Button("Save", id="task-reject-fix-submit", variant="warning")
                yield Button("Cancel", id="task-reject-fix-cancel")

    def on_mount(self) -> None:
        self.reason_input.focus()

    def on_key(self, event: events.Key) -> None:
        key = event.key
        if key == "1":
            event.stop()
            self.action_pick_tests()
        elif key == "2":
            event.stop()
            self.action_pick_scope()
        elif key == "3":
            event.stop()
            self.action_pick_fix()
        elif key == "4":
            event.stop()
            self.action_pick_other()

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted, "#task-reject-fix-reason")
    def _on_submit(self, event: Input.Submitted) -> None:
        reason = (event.value or "").strip()
        self.dismiss(reason or None)

    @on(Button.Pressed, "#task-reject-fix-submit")
    def _on_press_submit(self) -> None:
        reason = (self.reason_input.value or "").strip()
        self.dismiss(reason or None)

    @on(Button.Pressed, "#task-reject-fix-cancel")
    def _on_press_cancel(self) -> None:
        self.dismiss(None)


class _TaskRejectReasonModal(ModalScreen[str | None]):
    """Quick-pick reject modal with a free-text escape hatch."""

    BINDINGS = [
        Binding("1", "pick_tests", "Tests missing or broken", show=False, priority=True),
        Binding("2", "pick_scope", "Scope drift", show=False, priority=True),
        Binding("3", "pick_fix", "Write fix instructions", show=False, priority=True),
        Binding("4", "pick_other", "Other", show=False, priority=True),
        Binding("escape", "cancel", "Cancel", show=False),
    ]
    CSS = """
    Screen {
        align: center middle;
        background: rgba(8, 12, 15, 0.65);
    }
    #task-reject-modal {
        width: 78;
        height: auto;
        padding: 1 2;
        border: round #394754;
        background: #111920;
    }
    #task-reject-reasons {
        height: auto;
        padding: 1 0;
        color: #b7c4d1;
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
            placeholder="Type a custom rejection reason, then press Enter",
            id="task-reject-reason",
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="task-reject-modal"):
            yield Static("Reject Review Task", id="task-reject-title")
            yield Static(
                "[1] Tests missing or broken\n"
                "[2] Scope drift\n"
                "[3] Write fix instructions\n"
                "[4] Other (free text)",
                id="task-reject-reasons",
            )
            yield self.reason_input
            with Horizontal(id="task-reject-actions"):
                yield Button("1 Tests", id="task-reject-tests", variant="default")
                yield Button("2 Scope", id="task-reject-scope", variant="default")
                yield Button("3 Fix", id="task-reject-fix", variant="default")
                yield Button("4 Other", id="task-reject-other", variant="default")

    def on_mount(self) -> None:
        self.reason_input.focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_pick_tests(self) -> None:
        self.dismiss("Tests missing or broken")

    def action_pick_scope(self) -> None:
        self.dismiss("Scope drift")

    def action_pick_fix(self) -> None:
        def _after(reason: str | None) -> None:
            self.dismiss(reason or None)

        self.app.push_screen(_TaskRejectFixModal(), _after)

    def action_pick_other(self) -> None:
        reason = (self.reason_input.value or "").strip()
        if reason:
            self.dismiss(reason)
            return
        self.reason_input.focus()

    @on(Input.Submitted, "#task-reject-reason")
    def _on_submit(self, event: Input.Submitted) -> None:
        reason = (event.value or "").strip()
        self.dismiss(reason or None)

    @on(Button.Pressed, "#task-reject-tests")
    def _on_press_tests(self) -> None:
        self.action_pick_tests()

    @on(Button.Pressed, "#task-reject-scope")
    def _on_press_scope(self) -> None:
        self.action_pick_scope()

    @on(Button.Pressed, "#task-reject-fix")
    def _on_press_fix(self) -> None:
        self.action_pick_fix()

    @on(Button.Pressed, "#task-reject-other")
    def _on_press_other(self) -> None:
        self.action_pick_other()


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
        Binding("space", "toggle_task_selection", "Select", show=False),
        Binding("A", "bulk_approve_selected", "Approve Selected", show=False),
        Binding("X", "bulk_reject_selected", "Reject Selected", show=False),
        Binding("d", "toggle_resubmission_diff", "Review Diff", show=False),
        Binding("z", "undo_pending_review", "Undo", show=False),
        Binding("G", "resume_live_tail", "Resume Live", show=False),
        Binding("end", "resume_live_tail", "Resume Live", show=False),
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
    #tasks-filter-chips {
        height: auto;
        padding: 0 1 1 1;
        background: #0d1419;
    }
    #tasks-filter-chips Button {
        margin-right: 1;
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
    #task-review-panel {
        height: 1fr;
    }
    #task-review-confidence-chip {
        height: auto;
        padding: 0 1;
        margin: 0 0 1 0;
        border: round #3d4d60;
        background: #18232d;
        color: #cfe1ff;
    }
    #task-review-diff {
        height: auto;
        margin-top: 1;
        padding: 1 0 0 0;
    }
    #tasks-banner {
        height: auto;
        padding: 0 1 1 1;
        color: #b7c4d1;
        background: #0d1419;
    }
    #task-tabs { height: 1fr; }
    #task-detail-scroll,
    #task-review-scroll,
    #task-context-scroll,
    #task-live-scroll {
        height: 1fr;
        overflow-y: auto;
    }
    #task-live-header {
        height: auto;
        padding: 0 0 1 0;
    }
    #task-live-header-fill {
        width: 1fr;
    }
    #task-live-end-pill {
        width: auto;
        padding: 0 1;
        border: round #3d4d60;
        background: #18232d;
        color: #cfe1ff;
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
        self._rejection_feedback_by_task_id: dict[str, RejectionFeedbackNotice] = {}
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
        self.review_confidence = Static("", id="task-review-confidence-chip")
        self.review_diff_toggle = Button(
            "Diff Since Rejection: Off [D]",
            id="task-review-diff-toggle",
            variant="default",
        )
        self.review_diff = Static("", id="task-review-diff")
        self.detail_context = Static("", id="task-context")
        self.detail_live = Static("", id="task-live")
        self.timeline = DataTable(id="task-timeline", zebra_stripes=True)
        self.filter_chips_status = Button("", id="tasks-chip-status")
        self.filter_chips_search = Button("", id="tasks-chip-search")
        self.filter_chips_clear = Button("×clear", id="tasks-chip-clear-all", variant="default")
        self.filter_buttons = {
            key: Button(label, id=f"tasks-filter-{key}")
            for key, label in _FILTER_LABELS.items()
        }
        self.approve_button = Button("Approve", id="task-approve", variant="success")
        self.reject_button = Button("Reject", id="task-reject", variant="warning")
        self.bulk_approve_button = Button(
            "Approve Selected", id="task-bulk-approve", variant="success"
        )
        self.refresh_live_button = Button("Refresh Live", id="task-refresh-live")
        self.live_tail_fill = Static("", id="task-live-header-fill")
        self.live_tail_pill = Static("[End]", id="task-live-end-pill")
        self.banner = Static("", id="tasks-banner", markup=False)
        self._pending_review_action: _PendingReviewAction | None = None
        self._pending_commit_timer = None
        self._pending_countdown_timer = None
        self._live_tail_paused = False
        self._active_reject_modal: _TaskRejectReasonModal | None = None
        self._selected_task_ids: set[str] = set()
        self._show_resubmission_diff = False

    def compose(self) -> ComposeResult:
        with Vertical(id="tasks-root"):
            with Horizontal(id="tasks-toolbar"):
                yield Static(f"Tasks · {self.project_key}", id="tasks-heading")
                yield self.search_input
                with Horizontal(id="tasks-filters"):
                    for button in self.filter_buttons.values():
                        yield button
            with Horizontal(id="tasks-filter-chips"):
                yield self.filter_chips_status
                yield self.filter_chips_search
                yield self.filter_chips_clear
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
                        yield self.bulk_approve_button
                        yield self.refresh_live_button
                    with TabbedContent(initial="task-tab-overview", id="task-tabs"):
                        with TabPane("Overview", id="task-tab-overview"):
                            with VerticalScroll(id="task-detail-scroll"):
                                yield self.detail_overview
                        with TabPane("Review", id="task-tab-review"):
                            with Vertical(id="task-review-panel"):
                                yield self.review_confidence
                                yield self.review_diff_toggle
                                with VerticalScroll(id="task-review-scroll"):
                                    yield self.detail_review
                                    yield self.review_diff
                        with TabPane("Timeline", id="task-tab-timeline"):
                            yield self.timeline
                        with TabPane("Context", id="task-tab-context"):
                            with VerticalScroll(id="task-context-scroll"):
                                yield self.detail_context
                        with TabPane("Live", id="task-tab-live"):
                            with Vertical(id="task-live-panel"):
                                with Horizontal(id="task-live-header"):
                                    yield self.live_tail_fill
                                    yield self.live_tail_pill
                                with _TaskLiveScroll(id="task-live-scroll"):
                                    yield self.detail_live
            yield self.banner

    def on_mount(self) -> None:
        self._configure_tables()
        self._sync_filter_buttons()
        self._sync_filter_chips()
        self._sync_live_tail_indicator()
        self._sync_banner()
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

    def _load_tasks(
        self,
    ) -> tuple[list, dict[str, str | None], dict[str, RejectionFeedbackNotice]]:
        svc = self._get_svc()
        if svc is None:
            return [], {}, {}
        try:
            tasks = [
                task
                for task in svc.list_tasks(project=self.project_key)
                if not is_rejection_feedback_task(task)
            ]
            owners = {task.task_id: svc.derive_owner(task) for task in tasks}
            feedback = unread_rejection_feedback(svc, project=self.project_key)
        finally:
            svc.close()
        tasks.sort(key=_task_sort_key)
        return tasks, owners, feedback

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

    def _sync_filter_chips(self) -> None:
        chips: list[tuple[Button, str, bool]] = [
            (
                self.filter_chips_status,
                f"Status: {_FILTER_LABELS.get(self._status_filter, self._status_filter)} ×",
                self._status_filter != "all",
            ),
            (
                self.filter_chips_search,
                f"Search: {self._search_query.strip()} ×",
                bool(self._search_query.strip()),
            ),
        ]
        any_visible = False
        for button, label, visible in chips:
            button.label = label
            button.display = visible
            any_visible = any_visible or visible
        self.filter_chips_clear.display = any_visible
        try:
            chips_row = self.query_one("#tasks-filter-chips", Horizontal)
        except Exception:  # noqa: BLE001
            return
        chips_row.display = any_visible

    def _sync_live_tail_indicator(self) -> None:
        self.live_tail_pill.display = self._live_tail_paused
        try:
            header_row = self.query_one("#task-live-header", Horizontal)
        except Exception:  # noqa: BLE001
            return
        header_row.display = self._live_tail_paused

    def _sync_banner(self) -> None:
        banner = ""
        if self._pending_review_action is not None:
            seconds_left = max(
                0,
                int(self._pending_review_action.deadline - monotonic() + 0.999),
            )
            verb = (
                "Approve"
                if self._pending_review_action.decision == "approve"
                else "Reject"
            )
            if len(self._pending_review_action.task_numbers) == 1:
                target = f"#{self._pending_review_action.task_numbers[0]}"
            else:
                target = f"{len(self._pending_review_action.task_numbers)} tasks"
            banner = f"{verb} {target} — [Z] Undo ({seconds_left}s)"
        self.banner.update(banner)
        self.banner.display = bool(banner)

    def _set_detail_empty(self, message: str) -> None:
        self._selected_task_id = None
        self.detail_header.update(message)
        self.detail_overview.update("")
        self.detail_review.update("")
        self.review_confidence.update("")
        self.review_confidence.display = False
        self.review_diff_toggle.display = False
        self.review_diff.update("")
        self.review_diff.display = False
        self.detail_context.update("")
        self.detail_live.update("")
        self.timeline.clear()
        self.approve_button.disabled = True
        self.reject_button.disabled = True
        self.bulk_approve_button.disabled = True
        self.refresh_live_button.disabled = True
        self._set_live_tail_paused(False)

    def _render_table(self, *, select_first: bool) -> None:
        visible = self._filtered_tasks()
        visible_ids = {task.task_id for task in visible}
        self._selected_task_ids.intersection_update(visible_ids)
        self.summary.update(self._summary_text(visible))
        self.status.update(
            f"Filter: {_FILTER_LABELS.get(self._status_filter, self._status_filter)}"
            + (
                f"  ·  Search: {self._search_query}"
                if self._search_query.strip()
                else "  ·  / search · space select · a approve · x reject"
            )
        )
        self._sync_filter_chips()
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
            feedback = self._rejection_feedback_by_task_id.get(task.task_id)
            status = task.work_status.value
            title = f"{priority_glyph(task)} {task.title}"
            stage = task.current_node_id or "—"
            task_number = f"#{task.task_number}"
            if task.task_id in self._selected_task_ids:
                task_number = f"◉ {task_number}"
            if feedback is not None:
                status = f"{status} · feedback"
                title = f"🔄 {title}"
                stage = f"{stage} · Rejected"
            self.task_table.add_row(
                task_number,
                status,
                title,
                owner,
                stage,
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
        selected_review_count = len(self._selected_review_task_ids(visible=visible))
        self.bulk_approve_button.label = (
            f"Approve Selected ({selected_review_count})"
            if selected_review_count
            else "Approve Selected"
        )
        self.bulk_approve_button.disabled = selected_review_count == 0
        self._show_detail(target_id)

    def _selected_review_task_ids(self, *, visible: list | None = None) -> list[str]:
        tasks = visible if visible is not None else self._filtered_tasks()
        return [
            task.task_id
            for task in tasks
            if task.task_id in self._selected_task_ids
            and getattr(getattr(task, "work_status", None), "value", None) == "review"
        ]

    def _refresh_list(self, *, select_first: bool = False) -> None:
        self._tasks, self._owner_by_task_id, self._rejection_feedback_by_task_id = (
            self._load_tasks()
        )
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
        feedback = self._rejection_feedback_by_task_id.get(task.task_id)
        header_bits = [
            f"{'🔄 ' if feedback is not None else ''}{icon} #{task.task_number} {priority_glyph(task)} {task.title}",
            f"{task.work_status.value} · {_format_relative_age(task.updated_at or task.created_at)}",
        ]
        if feedback is not None:
            header_bits.append("Rejected — feedback in inbox")
        self.detail_header.update("\n".join(header_bits))
        self.detail_overview.update(
            _render_overview(
                task,
                owner=owner,
                flow=flow,
                active_session=active_session,
                rejection_feedback=feedback,
            )
        )
        self.detail_context.update(_render_context(task))
        self.detail_live.update(_render_live(task, active_session))
        self._render_timeline(list(getattr(task, "executions", None) or []))
        in_review = task.work_status.value == "review"
        self.approve_button.disabled = not in_review
        self.reject_button.disabled = not in_review
        self.bulk_approve_button.disabled = len(self._selected_review_task_ids()) == 0
        self.refresh_live_button.disabled = active_session is None
        if active_session is None:
            self._set_live_tail_paused(False)
        self._sync_live_tail_indicator()
        if active_session is not None and not self._live_tail_paused:
            self.call_after_refresh(self._tail_live_scroll_to_end)

    def _sync_review_panel(self, task, review_artifact) -> None:
        self.detail_review.update(render_task_review_artifact(review_artifact))
        score = _review_confidence_score(task, review_artifact)
        if score is None:
            self.review_confidence.update("")
            self.review_confidence.display = False
        else:
            self.review_confidence.update(_review_confidence_markup(score))
            self.review_confidence.display = True
        diff_available = _task_has_resubmission_diff(task)
        self.review_diff_toggle.display = diff_available
        self.review_diff_toggle.disabled = not diff_available
        if not diff_available:
            self._show_resubmission_diff = False
            self.review_diff.update("")
            self.review_diff.display = False
            return
        self.review_diff_toggle.label = (
            "Diff Since Rejection: On [D]"
            if self._show_resubmission_diff
            else "Diff Since Rejection: Off [D]"
        )
        if self._show_resubmission_diff:
            self.review_diff.update(_render_review_submission_diff(task))
        else:
            self.review_diff.update(
                "[dim]Press [D] to compare this submission against the last rejected attempt.[/dim]"
            )
        self.review_diff.display = True

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
        if task_id != previous_task_id:
            self._set_live_tail_paused(False)
            self._show_resubmission_diff = False
        self._render_selected_task(
            task,
            owner=owner,
            flow=flow,
            active_session=active_session,
        )
        self._sync_review_panel(task, review_artifact)
        tabs = self.query_one("#task-tabs", TabbedContent)
        if task_id != previous_task_id:
            tabs.active = (
                "task-tab-review"
                if task.work_status.value == "review" and review_artifact is not None
                else "task-tab-overview"
            )
        if active_session is not None and not self._live_tail_paused:
            self.call_after_refresh(self._tail_live_scroll_to_end)

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

    def action_resume_live_tail(self) -> None:
        self._set_live_tail_paused(False)
        self._sync_live_tail_indicator()
        if self._selected_task_id:
            self.call_after_refresh(self._tail_live_scroll_to_end)

    def action_undo_pending_review(self) -> None:
        if self._pending_review_action is None:
            return
        self._clear_pending_review_action()

    def _set_live_tail_paused(self, paused: bool) -> None:
        if self._live_tail_paused == paused:
            return
        self._live_tail_paused = paused
        self._sync_live_tail_indicator()

    def _tail_live_scroll_to_end(self) -> None:
        if self._live_tail_paused:
            return
        try:
            live_scroll = self.query_one("#task-live-scroll", _TaskLiveScroll)
        except Exception:  # noqa: BLE001
            return
        live_scroll.scroll_end(animate=False, force=True)

    def _clear_pending_review_action(self) -> None:
        if self._pending_commit_timer is not None:
            self._pending_commit_timer.stop()
            self._pending_commit_timer = None
        if self._pending_countdown_timer is not None:
            self._pending_countdown_timer.stop()
            self._pending_countdown_timer = None
        self._pending_review_action = None
        self._sync_banner()

    def _start_pending_review_action(
        self,
        *,
        task_ids: tuple[str, ...],
        task_numbers: tuple[int, ...],
        decision: str,
        reason: str | None = None,
    ) -> None:
        if self._pending_review_action is not None:
            self._commit_pending_review_action()
        self._pending_review_action = _PendingReviewAction(
            task_ids=task_ids,
            task_numbers=task_numbers,
            decision=decision,
            reason=reason,
            deadline=monotonic() + _PENDING_UNDO_SECONDS,
        )
        self._pending_commit_timer = self.set_timer(
            _PENDING_UNDO_SECONDS,
            self._commit_pending_review_action,
        )
        self._pending_countdown_timer = self.set_interval(1, self._sync_banner)
        self._sync_banner()

    def _merge_task_pr(self, task) -> None:
        pr_number = _task_pr_number(task)
        if pr_number is None:
            return
        if not _project_auto_merge_on_approve_enabled(self._project_path()):
            return
        project_path = self._project_path()
        if project_path is None:
            return
        try:
            subprocess.run(
                ["gh", "pr", "merge", pr_number, "--squash"],
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=45,
                check=True,
            )
            self.notify(f"Merged PR #{pr_number}", severity="information")
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Auto-merge failed: {exc}", severity="error")

    def _commit_pending_review_action(self) -> None:
        pending = self._pending_review_action
        if pending is None:
            return
        self._clear_pending_review_action()
        svc = self._get_svc()
        if svc is None:
            self.notify("Could not open project database.", severity="error")
            return
        try:
            processed_ids: list[str] = []
            approve_count = 0
            reject_count = 0
            for task_id in pending.task_ids:
                task = svc.get(task_id)
                if task.work_status.value != "review":
                    self.notify(f"{task_id} is not in review state.", severity="warning")
                    continue
                if pending.decision == "approve":
                    approved = svc.approve(
                        task_id,
                        "user",
                        pending.reason or "Approved from task cockpit",
                    )
                    notify_task_approved(approved, notify=self.notify)
                    self._merge_task_pr(approved)
                    approve_count += 1
                else:
                    svc.reject(
                        task_id,
                        "user",
                        pending.reason or "Rejected from task cockpit",
                    )
                    reject_count += 1
                processed_ids.append(task_id)
            if len(processed_ids) > 1:
                if approve_count:
                    self.notify(
                        f"Approved {approve_count} selected task"
                        + ("s" if approve_count != 1 else ""),
                        severity="information",
                    )
                if reject_count:
                    self.notify(
                        f"Rejected {reject_count} selected task"
                        + ("s" if reject_count != 1 else ""),
                        severity="information",
                    )
            self._selected_task_ids.difference_update(processed_ids)
        except Exception as exc:  # noqa: BLE001
            action = "Approve" if pending.decision == "approve" else "Reject"
            self.notify(f"{action} failed: {exc}", severity="error")
        finally:
            try:
                svc.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            self._refresh_list(select_first=False)
        except Exception:  # noqa: BLE001
            pass

    def _review_task(self, task_id: str, *, decision: str, reason: str | None = None) -> None:
        task = None
        svc = self._get_svc()
        if svc is None:
            self.notify("Could not open project database.", severity="error")
            return
        try:
            task = svc.get(task_id)
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Review failed: {exc}", severity="error")
        finally:
            try:
                svc.close()
            except Exception:  # noqa: BLE001
                pass
        if task is None:
            return
        if task.work_status.value != "review":
            self.notify("Task is not in review state.", severity="warning")
            return
        self._start_pending_review_action(
            task_ids=(task_id,),
            task_numbers=(task.task_number,),
            decision=decision,
            reason=reason,
        )

    def action_approve_task(self) -> None:
        if not self._selected_task_id:
            return
        self._review_task(self._selected_task_id, decision="approve")

    def action_toggle_task_selection(self) -> None:
        task_id = self._current_task_key()
        if task_id is None:
            return
        if task_id in self._selected_task_ids:
            self._selected_task_ids.remove(task_id)
        else:
            self._selected_task_ids.add(task_id)
        self._render_table(select_first=False)

    def _review_selected_tasks(self, *, decision: str, reason: str | None = None) -> None:
        selected_tasks = [
            task
            for task in self._filtered_tasks()
            if task.task_id in self._selected_task_ids and task.work_status.value == "review"
        ]
        if not selected_tasks:
            return
        self._start_pending_review_action(
            task_ids=tuple(task.task_id for task in selected_tasks),
            task_numbers=tuple(task.task_number for task in selected_tasks),
            decision=decision,
            reason=reason,
        )

    def action_bulk_approve_selected(self) -> None:
        self._review_selected_tasks(decision="approve")

    def action_bulk_reject_selected(self) -> None:
        self._review_selected_tasks(
            decision="reject",
            reason="Bulk rejected from task cockpit",
        )

    def action_reject_task(self) -> None:
        if not self._selected_task_id:
            return
        task_id = self._selected_task_id
        modal = _TaskRejectReasonModal()
        self._active_reject_modal = modal

        def _after(reason: str | None) -> None:
            self._active_reject_modal = None
            if reason is None:
                return
            self._review_task(task_id, decision="reject", reason=reason)

        self.push_screen(modal, _after)

    def action_toggle_resubmission_diff(self) -> None:
        if not self._selected_task_id:
            return
        svc = self._get_svc()
        if svc is None:
            return
        try:
            task = svc.get(self._selected_task_id)
            task.executions = svc.get_execution(self._selected_task_id)
        except Exception:  # noqa: BLE001
            return
        finally:
            try:
                svc.close()
            except Exception:  # noqa: BLE001
                pass
        if not _task_has_resubmission_diff(task):
            return
        self._show_resubmission_diff = not self._show_resubmission_diff
        self._show_detail(self._selected_task_id)

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

    @on(Button.Pressed, "#tasks-chip-status")
    def _on_press_status_chip(self) -> None:
        self._status_filter = "all"
        self._sync_filter_buttons()
        self._render_table(select_first=True)

    @on(Button.Pressed, "#tasks-chip-search")
    def _on_press_search_chip(self) -> None:
        self._search_query = ""
        self.search_input.value = ""
        self._render_table(select_first=True)

    @on(Button.Pressed, "#tasks-chip-clear-all")
    def _on_press_clear_all_chips(self) -> None:
        self._status_filter = "all"
        self._search_query = ""
        self.search_input.value = ""
        self._sync_filter_buttons()
        self._render_table(select_first=True)

    @on(Button.Pressed, "#task-approve")
    def _on_press_approve(self) -> None:
        self.action_approve_task()

    @on(Button.Pressed, "#task-reject")
    def _on_press_reject(self) -> None:
        self.action_reject_task()

    @on(Button.Pressed, "#task-bulk-approve")
    def _on_press_bulk_approve(self) -> None:
        self.action_bulk_approve_selected()

    @on(Button.Pressed, "#task-review-diff-toggle")
    def _on_press_review_diff_toggle(self) -> None:
        self.action_toggle_resubmission_diff()

    @on(Button.Pressed, "#task-refresh-live")
    def _on_press_refresh_live(self) -> None:
        self.action_refresh_live()

    def on_key(self, event: events.Key) -> None:
        modal = self._active_reject_modal
        if modal is None:
            return
        if event.key == "1":
            event.stop()
            modal.action_pick_tests()
        elif event.key == "2":
            event.stop()
            modal.action_pick_scope()
        elif event.key == "3":
            event.stop()
            modal.action_pick_fix()
        elif event.key == "4":
            event.stop()
            modal.action_pick_other()

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
