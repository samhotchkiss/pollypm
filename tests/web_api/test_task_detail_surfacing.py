"""Renderer-surfacing tests for the §1.4.5 task-detail gaps.

Covers the four sibling reports (#2335 / #2336 / #2337 / #2338) that
all share one root cause — the REST detail surface had the data, but
the renderers (CLI ``pm task get`` + Web UI ``renderTaskSummary``) and
the detail endpoint itself swallowed it. Each test pins one of the
gaps so the regression bar matches what the user actually sees.

* #2335: ``project_paused`` flag on detail responses for untracked
  / ``tracked=false`` projects.
* #2336: dwell + dead-session warning on stuck in_progress tasks.
* #2337: ``blocked_by`` relationships rendered in the CLI.
* #2338: detail endpoint returns the task (with ``project_paused``)
  instead of 404 when the project drifted out of config.

The test module reuses the ``pg_schema_pool`` fixture for per-test
isolation (same pattern as ``test_tasks_list_endpoint.py``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pollypm.work.factory import create_work_service

from .conftest import make_task
from .test_tasks_list_endpoint import _seed_untracked_project

pytestmark = pytest.mark.usefixtures("pg_schema_pool")


# ---------------------------------------------------------------------------
# REST detail endpoint (#2335 + #2338)
# ---------------------------------------------------------------------------


def test_task_detail_untracked_project_returns_task_with_paused_flag(
    api_config, client, auth_headers, workspace_root
) -> None:
    """#2338: detail must mirror list behaviour on untracked projects.

    The list endpoint already returns rows for an untracked project
    when ``include_untracked=true``; the operator-facing surface was
    misleading because clicking that row used to 404. The detail
    endpoint now returns the task and flags ``project_paused=True``.
    """
    paused_root = _seed_untracked_project(api_config, workspace_root, key="paused")
    db_path = api_config.project.state_db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Open at the *workspace* root so the row is reachable via the
    # workspace work-service (same way the list endpoint surfaces
    # untracked rows). The project's own dir is empty.
    assert paused_root.exists()
    with create_work_service(
        db_path=db_path, project_path=workspace_root
    ) as svc:
        task = make_task(svc, project="paused", title="Untracked task")

    response = client.get(
        f"/api/v1/tasks/paused/{task.task_number}", headers=auth_headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["title"] == "Untracked task"
    assert body["project_paused"] is True


def test_task_detail_tracked_project_reports_paused_false(
    api_config, client, auth_headers, project_root
) -> None:
    """#2335: tracked projects must report ``project_paused=False``.

    Default state — the operator should be able to rely on the flag
    being explicitly absent (None / False) for healthy projects so
    the renderer can branch on it without false positives.
    """
    db_path = api_config.project.state_db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with create_work_service(db_path=db_path, project_path=project_root) as svc:
        task = make_task(svc, project="myproj", title="Healthy")

    response = client.get(
        f"/api/v1/tasks/myproj/{task.task_number}", headers=auth_headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # ``False`` (not ``None``) so the Web UI's truthy check works.
    assert body["project_paused"] is False


def test_task_detail_tracked_false_flips_paused_flag(
    api_config, client, auth_headers, project_root
) -> None:
    """#2335: a registered-but-paused project must also surface paused.

    The config carries ``tracked=False`` after ``pm projects pause``;
    the detail surface must treat that identically to "missing from
    config" so the renderer's branch fires either way.
    """
    db_path = api_config.project.state_db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with create_work_service(db_path=db_path, project_path=project_root) as svc:
        task = make_task(svc, project="myproj", title="Paused but registered")
    api_config.projects["myproj"].tracked = False

    response = client.get(
        f"/api/v1/tasks/myproj/{task.task_number}", headers=auth_headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["project_paused"] is True


# ---------------------------------------------------------------------------
# CLI ``_print_task`` rendering (#2335 / #2336 / #2337)
# ---------------------------------------------------------------------------


class _StubEnum:
    def __init__(self, value: str) -> None:
        self.value = value


class _StubTask:
    """Lightweight ``_print_task`` fixture — covers the attribute surface."""

    def __init__(
        self,
        *,
        task_id: str = "myproj/1",
        project: str = "myproj",
        title: str = "Stub",
        status: str = "in_progress",
        priority: str = "normal",
        type_: str = "task",
        assignee: str | None = None,
        claimed_by_session: str | None = None,
        blocked_by: list[tuple[str, int]] | None = None,
        state_entered_at: datetime | None = None,
    ) -> None:
        self.task_id = task_id
        self.project = project
        self.title = title
        self.work_status = _StubEnum(status)
        self.priority = _StubEnum(priority)
        self.type = _StubEnum(type_)
        self.assignee = assignee
        self.claimed_by_session = claimed_by_session
        self.blocked_by = blocked_by or []
        self.state_entered_at = state_entered_at
        # Default-empty attributes _print_task touches.
        self.current_node_id = None
        self.description = ""
        self.roles = None
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.session_count = 0
        self.executions = []
        self.context = []


def _render(task, monkeypatch, *, paused: bool = False, dead: bool = False) -> str:
    """Invoke ``_print_task`` with config-lookup helpers stubbed."""
    import typer

    from pollypm.work import cli as work_cli

    monkeypatch.setattr(work_cli, "_project_is_paused", lambda _task: paused)
    monkeypatch.setattr(
        work_cli, "_claimed_session_is_dead", lambda _task: dead
    )

    captured: list[str] = []
    monkeypatch.setattr(typer, "echo", lambda text="": captured.append(str(text)))
    work_cli._print_task(task)
    return "\n".join(captured)


def test_print_task_surfaces_dwell_for_non_terminal(monkeypatch) -> None:
    """#2336: non-terminal tasks should render dwell prominently."""
    entered = datetime.now(timezone.utc) - timedelta(hours=40)
    task = _StubTask(status="in_progress", state_entered_at=entered)
    output = _render(task, monkeypatch)
    assert "Dwell:" in output
    # 40h or 1d 16h depending on rounding — both are valid renders.
    assert "h" in output.split("Dwell:")[1].splitlines()[0]


def test_print_task_dwell_skipped_for_terminal(monkeypatch) -> None:
    entered = datetime.now(timezone.utc) - timedelta(hours=2)
    task = _StubTask(status="done", state_entered_at=entered)
    output = _render(task, monkeypatch)
    assert "Dwell:" not in output


def test_print_task_renders_blocked_by(monkeypatch) -> None:
    """#2337: ``relationships.blocked_by`` must surface on the CLI."""
    task = _StubTask(
        status="blocked",
        blocked_by=[("samblog", 3), ("samblog", 29)],
    )
    output = _render(task, monkeypatch)
    assert "Blocked by: samblog/3, samblog/29" in output


def test_print_task_blocked_without_blockers_warns(monkeypatch) -> None:
    """Blocked status with no blockers is a stale-state smell."""
    task = _StubTask(status="blocked")
    output = _render(task, monkeypatch)
    assert "Status=blocked but no blocked_by" in output


def test_print_task_warns_on_paused_project(monkeypatch) -> None:
    """#2335: surface paused-project state inline so the operator sees it."""
    task = _StubTask(status="queued")
    output = _render(task, monkeypatch, paused=True)
    assert "PROJECT PAUSED" in output


def test_print_task_warns_on_dead_claim_session(monkeypatch) -> None:
    """#2336: claim with no fresh heartbeat must warn."""
    entered = datetime.now(timezone.utc) - timedelta(hours=1)
    task = _StubTask(
        status="in_progress",
        assignee="architect",
        claimed_by_session="session-xyz",
        state_entered_at=entered,
    )
    output = _render(task, monkeypatch, dead=True)
    assert "no fresh heartbeat" in output


def test_print_task_warns_when_assignee_has_no_claim(monkeypatch) -> None:
    """#2336: in_progress with assignee but no claim is stranded."""
    task = _StubTask(
        status="in_progress",
        assignee="architect",
        claimed_by_session=None,
    )
    output = _render(task, monkeypatch)
    assert "no live session" in output
