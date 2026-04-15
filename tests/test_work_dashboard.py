"""Tests for work dashboard TUI widgets and formatting helpers."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pollypm.work.models import (
    Artifact,
    ArtifactKind,
    ContextEntry,
    Decision,
    ExecutionStatus,
    FlowNodeExecution,
    OutputType,
    Priority,
    Task,
    TaskType,
    WorkOutput,
    WorkStatus,
)
from pollypm.work.dashboard import (
    TaskDetailWidget,
    TaskListWidget,
    SessionListWidget,
    WorkDashboard,
    format_execution_history,
    format_priority,
    format_status_icon,
    format_task_row,
    format_work_output_summary,
)


# ---------------------------------------------------------------------------
# Test data helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 4, 14, 12, 0, 0, tzinfo=timezone.utc)


def _make_task(
    number: int = 42,
    title: str = "Fix auth cookies",
    status: WorkStatus = WorkStatus.IN_PROGRESS,
    priority: Priority = Priority.NORMAL,
    assignee: str | None = "pete",
    project: str = "myproj",
    **kwargs: object,
) -> Task:
    defaults = dict(
        project=project,
        task_number=number,
        title=title,
        type=TaskType.TASK,
        work_status=status,
        priority=priority,
        assignee=assignee,
        flow_template_id="standard",
        current_node_id="work" if status == WorkStatus.IN_PROGRESS else None,
        description="Fix the auth cookie handling for SSO",
        roles={"worker": "pete", "reviewer": "polly"},
        created_at=_NOW,
        updated_at=_NOW,
    )
    defaults.update(kwargs)
    return Task(**defaults)


def _make_execution(
    task_id: str = "myproj/42",
    node_id: str = "work",
    visit: int = 1,
    status: ExecutionStatus = ExecutionStatus.COMPLETED,
    work_output: WorkOutput | None = None,
    decision: Decision | None = None,
    decision_reason: str | None = None,
) -> FlowNodeExecution:
    return FlowNodeExecution(
        task_id=task_id,
        node_id=node_id,
        visit=visit,
        status=status,
        work_output=work_output,
        decision=decision,
        decision_reason=decision_reason,
        started_at=_NOW,
        completed_at=_NOW,
    )


# ---------------------------------------------------------------------------
# Formatting helper tests
# ---------------------------------------------------------------------------


class TestFormatStatusIcon:
    def test_all_statuses_have_distinct_icons(self):
        statuses = [
            "in_progress", "review", "queued", "draft",
            "blocked", "on_hold", "done", "cancelled",
        ]
        icons = [format_status_icon(s) for s in statuses]
        # Every status maps to something
        assert all(icon != "?" for icon in icons)
        # All icons are distinct
        assert len(set(icons)) == len(icons)

    def test_unknown_status_returns_fallback(self):
        assert format_status_icon("nonexistent") == "?"


class TestFormatTaskRow:
    def test_contains_number_title_assignee(self):
        task = _make_task()
        row = format_task_row(task)
        assert "#42" in row
        assert "Fix auth cookies" in row
        assert "pete" in row

    def test_no_assignee(self):
        task = _make_task(assignee=None)
        row = format_task_row(task)
        assert "#42" in row
        assert "Fix auth cookies" in row
        # No bracket for assignee
        assert "[" not in row or "normal" in row

    def test_status_icon_present(self):
        task = _make_task(status=WorkStatus.REVIEW)
        row = format_task_row(task)
        assert "◉" in row


class TestFormatPriority:
    def test_critical(self):
        assert "critical" in format_priority("critical")

    def test_high(self):
        assert "high" in format_priority("high")

    def test_normal(self):
        assert "normal" in format_priority("normal")

    def test_low(self):
        assert "low" in format_priority("low")

    def test_unknown(self):
        result = format_priority("ultra")
        assert result == "ultra"


class TestFormatExecutionHistory:
    def test_empty(self):
        result = format_execution_history([])
        assert "no execution history" in result

    def test_single_work_node(self):
        exe = _make_execution(
            node_id="work",
            visit=1,
            work_output=WorkOutput(
                type=OutputType.CODE_CHANGE,
                summary="Fixed cookie handling",
                artifacts=[
                    Artifact(kind=ArtifactKind.COMMIT, description="commit", ref="abc123"),
                ],
            ),
        )
        result = format_execution_history([exe])
        assert "work" in result
        assert "visit 1" in result
        assert "Fixed cookie handling" in result

    def test_review_with_decision(self):
        exe = _make_execution(
            node_id="review",
            visit=1,
            status=ExecutionStatus.COMPLETED,
            decision=Decision.APPROVED,
            decision_reason="Looks good",
        )
        result = format_execution_history([exe])
        assert "review" in result
        assert "approved" in result
        assert "Looks good" in result

    def test_multiple_visits(self):
        exes = [
            _make_execution(node_id="work", visit=1),
            _make_execution(node_id="review", visit=1, decision=Decision.REJECTED, decision_reason="Needs changes"),
            _make_execution(node_id="work", visit=2),
        ]
        result = format_execution_history(exes)
        assert "visit 1" in result
        assert "visit 2" in result


class TestFormatWorkOutputSummary:
    def test_code_change_with_commit_ref(self):
        output = WorkOutput(
            type=OutputType.CODE_CHANGE,
            summary="Fixed cookie handling",
            artifacts=[
                Artifact(kind=ArtifactKind.COMMIT, description="commit", ref="abc123def"),
            ],
        )
        result = format_work_output_summary(output)
        assert "abc123def" in result
        assert "Fixed cookie handling" in result

    def test_action_output(self):
        output = WorkOutput(
            type=OutputType.ACTION,
            summary="Deployed to staging",
            artifacts=[
                Artifact(kind=ArtifactKind.ACTION, description="deploy staging"),
            ],
        )
        result = format_work_output_summary(output)
        assert "Deployed to staging" in result
        assert "deploy staging" in result

    def test_empty_output(self):
        output = WorkOutput(type=OutputType.CODE_CHANGE, summary="")
        result = format_work_output_summary(output)
        assert "code_change" in result


# ---------------------------------------------------------------------------
# Widget instantiation tests (no Textual headless runtime required)
# ---------------------------------------------------------------------------


class TestTaskListWidgetInstantiation:
    def test_creates_with_data(self):
        tasks = [_make_task(number=1), _make_task(number=2)]
        counts = {"in_progress": 2}
        widget = TaskListWidget(tasks=tasks, counts=counts)
        assert widget._tasks == tasks
        assert widget._counts == counts

    def test_creates_empty(self):
        widget = TaskListWidget()
        assert widget._tasks == []
        assert widget._counts == {}

    def test_render_content_contains_tasks(self):
        tasks = [
            _make_task(number=1, title="Task one"),
            _make_task(number=2, title="Task two"),
        ]
        counts = {"in_progress": 2, "queued": 1}
        widget = TaskListWidget(tasks=tasks, counts=counts)
        content = widget._render_content()
        assert "#1" in content
        assert "#2" in content
        assert "Task one" in content
        assert "2 in progress" in content
        assert "1 queued" in content

    def test_render_content_shows_recently_completed(self):
        tasks = [
            _make_task(number=1, status=WorkStatus.IN_PROGRESS),
            _make_task(number=2, status=WorkStatus.DONE, title="Done task"),
        ]
        counts = {"in_progress": 1, "done": 1}
        widget = TaskListWidget(tasks=tasks, counts=counts)
        content = widget._render_content()
        assert "Recently completed" in content
        assert "Done task" in content

    def test_render_content_no_active_tasks(self):
        widget = TaskListWidget(tasks=[], counts={})
        content = widget._render_content()
        assert "no active tasks" in content


class TestTaskDetailWidgetInstantiation:
    def test_creates_with_task(self):
        task = _make_task()
        widget = TaskDetailWidget(task=task)
        assert widget._task is task

    def test_creates_empty(self):
        widget = TaskDetailWidget()
        assert widget._task is None

    def test_render_content_shows_details(self):
        task = _make_task(
            description="Fix the auth cookie handling for SSO",
            acceptance_criteria="Cookies work with SSO",
        )
        task.executions = [
            _make_execution(
                node_id="work",
                visit=1,
                work_output=WorkOutput(
                    type=OutputType.CODE_CHANGE,
                    summary="Fixed it",
                ),
            ),
        ]
        task.context = [
            ContextEntry(actor="pete", timestamp=_NOW, text="Started work"),
        ]
        widget = TaskDetailWidget(task=task)
        content = widget._render_content()
        assert "#42" in content
        assert "Fix auth cookies" in content
        assert "in_progress" in content
        assert "standard" in content
        assert "worker=pete" in content
        assert "Fix the auth cookie handling" in content
        assert "Cookies work with SSO" in content
        assert "Execution history" in content
        assert "work" in content
        assert "Context log" in content
        assert "Started work" in content

    def test_render_content_no_task(self):
        widget = TaskDetailWidget()
        content = widget._render_content()
        assert "select a task" in content


class TestSessionListWidgetInstantiation:
    def test_creates_with_sessions(self):
        sessions = [_make_task(number=1), _make_task(number=2)]
        widget = SessionListWidget(sessions=sessions)
        assert len(widget._sessions) == 2

    def test_render_content_shows_sessions(self):
        sessions = [
            _make_task(number=1, title="Task one", assignee="agent-1"),
            _make_task(number=2, title="Task two", assignee="agent-2"),
        ]
        widget = SessionListWidget(sessions=sessions)
        content = widget._render_content()
        assert "#1" in content
        assert "Task one" in content
        assert "agent-1" in content
        assert "#2" in content

    def test_render_content_empty(self):
        widget = SessionListWidget()
        content = widget._render_content()
        assert "no active sessions" in content


class TestWorkDashboardInstantiation:
    def test_creates_with_data(self):
        tasks = [_make_task()]
        counts = {"in_progress": 1}
        dashboard = WorkDashboard(tasks=tasks, counts=counts)
        assert dashboard._tasks == tasks
        assert dashboard._counts == counts


# ---------------------------------------------------------------------------
# Widget rendering tests (synchronous — no pytest-asyncio needed)
#
# These test the widget classes directly via _render_content() rather than
# running a full Textual headless app, which would require pytest-asyncio.
# The instantiation tests above already exercise the data flow; these add
# targeted checks matching the test names from the spec.
# ---------------------------------------------------------------------------


def test_task_list_widget_renders():
    tasks = [_make_task(number=42, title="Fix auth cookies")]
    counts = {"in_progress": 1}
    widget = TaskListWidget(tasks=tasks, counts=counts)
    content = widget._render_content()
    assert "#42" in content
    assert "Fix auth cookies" in content


def test_task_list_widget_shows_counts():
    tasks = [
        _make_task(number=1, status=WorkStatus.IN_PROGRESS),
        _make_task(number=2, status=WorkStatus.QUEUED),
    ]
    counts = {"in_progress": 1, "queued": 1}
    widget = TaskListWidget(tasks=tasks, counts=counts)
    content = widget._render_content()
    assert "1 in progress" in content
    assert "1 queued" in content


def test_task_detail_widget_renders():
    task = _make_task()
    task.executions = [_make_execution()]
    widget = TaskDetailWidget(task=task)
    content = widget._render_content()
    assert "#42" in content
    assert "Fix auth cookies" in content
    assert "Execution history" in content


def test_session_list_widget_renders():
    sessions = [_make_task(number=1, title="Task one", assignee="agent-1")]
    widget = SessionListWidget(sessions=sessions)
    content = widget._render_content()
    assert "#1" in content
    assert "agent-1" in content


def test_dashboard_composes_widgets():
    """Verify WorkDashboard stores both sub-widget data and can be instantiated."""
    tasks = [_make_task()]
    counts = {"in_progress": 1}
    dashboard = WorkDashboard(tasks=tasks, counts=counts)
    assert dashboard._tasks is not None
    assert dashboard._counts is not None
    # Verify compose yields both widget types
    children = list(dashboard.compose())
    types = {type(c).__name__ for c in children}
    assert "TaskListWidget" in types
    assert "TaskDetailWidget" in types
