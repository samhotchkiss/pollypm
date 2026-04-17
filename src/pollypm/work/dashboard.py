"""TUI widgets for the work service dashboard.

Provides TaskListWidget, TaskDetailWidget, SessionListWidget, and
WorkDashboard — all Textual widgets that consume the WorkService protocol
for rendering project dashboards, task details, and session lists.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text

from textual.app import ComposeResult
from textual.containers import Container, Horizontal
from textual.message import Message
from textual.widgets import Static

from pollypm.work.models import (
    FlowNodeExecution,
    Task,
    WorkOutput,
    WorkStatus,
    TERMINAL_STATUSES,
)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

_STATUS_ICONS: dict[str, str] = {
    "in_progress": "⟳",
    "review": "◉",
    "queued": "○",
    "draft": "◌",
    "blocked": "⊘",
    "on_hold": "⏸",
    "done": "✓",
    "cancelled": "✗",
}

_PRIORITY_LABELS: dict[str, str] = {
    "critical": "‼ critical",
    "high": "! high",
    "normal": "normal",
    "low": "↓ low",
}

_PRIORITY_COLORS: dict[str, str] = {
    "critical": "bold red",
    "high": "yellow",
    "normal": "default",
    "low": "dim",
}


def format_status_icon(status: str) -> str:
    """Map work_status value to an icon character."""
    return _STATUS_ICONS.get(status, "?")


def format_priority(priority: str) -> str:
    """Format priority with a visual indicator."""
    return _PRIORITY_LABELS.get(priority, priority)


def format_task_row(task: Task) -> str:
    """Format a task as a single-line row for the list."""
    icon = format_status_icon(task.work_status.value)
    number = f"#{task.task_number}"
    title = task.title
    assignee = task.assignee or ""
    priority = format_priority(task.priority.value)
    parts = [icon, number, title]
    if assignee:
        parts.append(f"[{assignee}]")
    parts.append(f"({priority})")
    return " ".join(parts)


def format_execution_history(executions: list[FlowNodeExecution]) -> str:
    """Format execution records as a readable history."""
    if not executions:
        return "(no execution history)"
    lines: list[str] = []
    for exe in executions:
        status_str = exe.status.value
        line = f"  {exe.node_id} (visit {exe.visit}) — {status_str}"
        if exe.work_output:
            line += f" — {format_work_output_summary(exe.work_output)}"
        if exe.decision:
            line += f" [{exe.decision.value}]"
            if exe.decision_reason:
                line += f": {exe.decision_reason}"
        lines.append(line)
    return "\n".join(lines)


def format_work_output_summary(output: WorkOutput) -> str:
    """One-line summary of what was produced."""
    parts: list[str] = [output.summary] if output.summary else []
    for artifact in output.artifacts:
        if artifact.ref:
            parts.append(artifact.ref)
        elif artifact.description:
            parts.append(artifact.description)
    return " · ".join(parts) if parts else f"({output.type.value})"


def _format_counts_bar(counts: dict[str, int]) -> str:
    """Build a summary bar like '3 queued · 2 in progress · 1 review · 14 done'."""
    order = ["queued", "in_progress", "blocked", "on_hold", "review", "done", "cancelled", "draft"]
    parts: list[str] = []
    for status in order:
        n = counts.get(status, 0)
        if n > 0:
            label = status.replace("_", " ")
            parts.append(f"{n} {label}")
    return " · ".join(parts) if parts else "no tasks"


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


class TaskSelected(Message):
    """Posted when a task is selected in the list."""

    def __init__(self, task: Task) -> None:
        super().__init__()
        self.task = task


# ---------------------------------------------------------------------------
# TaskListWidget
# ---------------------------------------------------------------------------

_NON_TERMINAL = frozenset(
    s for s in WorkStatus if s not in TERMINAL_STATUSES
)


class TaskListWidget(Static):
    """Displays tasks grouped by status with counts."""

    DEFAULT_CSS = """
    TaskListWidget {
        height: auto;
        padding: 0 1;
    }
    """

    def __init__(
        self,
        tasks: list[Task] | None = None,
        counts: dict[str, int] | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)
        self._tasks: list[Task] = list(tasks or [])
        self._counts: dict[str, int] = dict(counts or {})

    def set_data(self, tasks: list[Task], counts: dict[str, int]) -> None:
        """Update the widget with new data and re-render."""
        self._tasks = list(tasks)
        self._counts = dict(counts)
        self.update(self._render_content())

    def on_mount(self) -> None:
        self.update(self._render_content())

    def _render_content(self) -> str:
        lines: list[str] = []

        # Summary bar
        lines.append(_format_counts_bar(self._counts))
        lines.append("")

        # Active (non-terminal) tasks
        active = [t for t in self._tasks if t.work_status not in TERMINAL_STATUSES]
        active.sort(key=lambda t: (
            list(WorkStatus).index(t.work_status),
            t.task_number,
        ))

        if active:
            for task in active:
                lines.append(format_task_row(task))
        else:
            lines.append("(no active tasks)")

        # Recently completed (done/cancelled)
        terminal = [t for t in self._tasks if t.work_status in TERMINAL_STATUSES]
        terminal.sort(key=lambda t: t.updated_at or t.created_at or t.updated_at, reverse=True)
        recent = terminal[:10]

        if recent:
            lines.append("")
            lines.append("Recently completed")
            for task in recent:
                lines.append(format_task_row(task))

        return "\n".join(lines)

    def on_click(self) -> None:
        """Post a TaskSelected message for the first active task (placeholder).

        Real click-targeting will use coordinates; for now this is a stub.
        """
        active = [t for t in self._tasks if t.work_status not in TERMINAL_STATUSES]
        if active:
            self.post_message(TaskSelected(active[0]))


# ---------------------------------------------------------------------------
# TaskDetailWidget
# ---------------------------------------------------------------------------


class TaskDetailWidget(Static):
    """Displays detailed view of a single task."""

    DEFAULT_CSS = """
    TaskDetailWidget {
        height: auto;
        padding: 0 1;
    }
    """

    def __init__(self, task: Task | None = None, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._task: Task | None = task

    def set_task(self, task: Task) -> None:
        """Update the widget with a new task and re-render."""
        self._task = task
        self.update(self._render_content())

    def on_mount(self) -> None:
        self.update(self._render_content())

    def _render_content(self) -> str:
        task = self._task
        if task is None:
            return "(select a task)"

        lines: list[str] = []

        # Header
        icon = format_status_icon(task.work_status.value)
        pri = format_priority(task.priority.value)
        lines.append(f"{icon} #{task.task_number} {task.title}  [{task.work_status.value}] ({pri})")
        lines.append("")

        # Flow info
        if task.flow_template_id:
            node_str = task.current_node_id or "(none)"
            lines.append(f"Flow: {task.flow_template_id}  Node: {node_str}")

        # Roles
        if task.roles:
            role_parts = [f"{k}={v}" for k, v in task.roles.items()]
            lines.append(f"Roles: {', '.join(role_parts)}")

        # Description
        if task.description:
            lines.append("")
            lines.append("Description:")
            lines.append(task.description)

        # Acceptance criteria
        if task.acceptance_criteria:
            lines.append("")
            lines.append("Acceptance criteria:")
            lines.append(task.acceptance_criteria)

        # Execution history
        if task.executions:
            lines.append("")
            lines.append("Execution history:")
            lines.append(format_execution_history(task.executions))

        # Context log (last 5)
        if task.context:
            lines.append("")
            lines.append("Context log:")
            recent = task.context[-5:]
            for entry in recent:
                ts = entry.timestamp.strftime("%Y-%m-%d %H:%M")
                lines.append(f"  [{ts}] {entry.actor}: {entry.text}")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# SessionListWidget
# ---------------------------------------------------------------------------


class SessionListWidget(Static):
    """Lists active worker sessions for a project."""

    DEFAULT_CSS = """
    SessionListWidget {
        height: auto;
        padding: 0 1;
    }
    """

    def __init__(
        self,
        sessions: list[Task] | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)
        self._sessions: list[Task] = list(sessions or [])

    def set_sessions(self, sessions: list[Task]) -> None:
        """Update the widget with new session data and re-render."""
        self._sessions = list(sessions)
        self.update(self._render_content())

    def on_mount(self) -> None:
        self.update(self._render_content())

    def _render_content(self) -> str:
        if not self._sessions:
            return "(no active sessions)"

        lines: list[str] = []
        for task in self._sessions:
            icon = format_status_icon(task.work_status.value)
            agent = task.assignee or task.roles.get("worker", "")
            lines.append(f"{icon} #{task.task_number} {task.title}  [{agent}]")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# WorkDashboard (composable container)
# ---------------------------------------------------------------------------


class WorkDashboard(Container):
    """Project work dashboard — task list on left, detail on right."""

    DEFAULT_CSS = """
    WorkDashboard {
        layout: horizontal;
        height: 1fr;
    }
    WorkDashboard > TaskListWidget {
        width: 1fr;
        border-right: solid $accent;
    }
    WorkDashboard > TaskDetailWidget {
        width: 2fr;
    }
    """

    def __init__(
        self,
        tasks: list[Task] | None = None,
        counts: dict[str, int] | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)
        self._tasks = tasks
        self._counts = counts

    def compose(self) -> ComposeResult:
        yield TaskListWidget(tasks=self._tasks, counts=self._counts)
        yield TaskDetailWidget()

    def on_task_list_widget_task_selected(self, message: TaskSelected) -> None:
        """When a task is selected in the list, show its detail."""
        # Note: TaskSelected bubbles from TaskListWidget
        pass

    def on_task_selected(self, message: TaskSelected) -> None:
        """Update the detail pane when a task is selected."""
        detail = self.query_one(TaskDetailWidget)
        detail.set_task(message.task)
