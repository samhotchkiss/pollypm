"""Operator dashboard view + ASCII render tests (#1572).

End-to-end coverage of the view-model loader against a populated
SQLite workspace + the ASCII renderer used by the cockpit text-pane
fallback (and the PR-body screenshot).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pollypm.dashboard import ProjectState
from pollypm.dashboard.operator_view import (
    load_operator_view,
    project_state_map_from_config,
    view_as_ascii,
)
from pollypm.inbox.kind import InboxItemKind
from pollypm.work.sqlite_service import SQLiteWorkService


def _write_config(workspace_root: Path, config_path: Path, projects: dict[str, dict]) -> None:
    lines = [
        "[project]\n",
        'tmux_session = "pollypm-test"\n',
        f'workspace_root = "{workspace_root}"\n',
        "\n",
    ]
    for key, spec in projects.items():
        lines.append(f"[projects.{key}]\n")
        lines.append(f'key = "{key}"\n')
        lines.append(f'name = "{spec.get("name", key.title())}"\n')
        lines.append(f'path = "{spec["path"]}"\n')
        if "tracked" in spec:
            lines.append(f'tracked = {str(spec["tracked"]).lower()}\n')
        lines.append("\n")
    config_path.write_text("".join(lines))


def _seed_task(
    db_path: Path,
    project_path: Path,
    *,
    project: str,
    title: str,
    kind: str,
    status: str | None = None,
) -> str:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    svc = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        task = svc.create(
            title=title,
            description=f"body for {title}",
            type="task",
            project=project,
            flow_template="chat",
            roles={"requester": "user", "operator": "polly"},
            priority="normal",
            created_by="polly",
            kind=kind,
        )
        if status == "in_progress":
            svc.queue(task.task_id, actor="polly", skip_gates=True)
            svc.claim(task.task_id, actor="claude")
        return task.task_id
    finally:
        svc.close()


@pytest.fixture
def workspace(tmp_path: Path):
    workspace_root = tmp_path / "dev"
    workspace_root.mkdir()
    waiting = workspace_root / "waiting_proj"
    waiting.mkdir()
    (waiting / ".git").mkdir()
    working = workspace_root / "working_proj"
    working.mkdir()
    (working / ".git").mkdir()
    idle = workspace_root / "idle_proj"
    idle.mkdir()
    (idle / ".git").mkdir()
    paused = workspace_root / "paused_proj"
    paused.mkdir()
    (paused / ".git").mkdir()

    config_path = tmp_path / "pollypm.toml"
    _write_config(
        workspace_root, config_path,
        {
            "waiting_proj": {"path": str(waiting), "tracked": True},
            "working_proj": {"path": str(working), "tracked": True},
            "idle_proj": {"path": str(idle), "tracked": True},
            "paused_proj": {"path": str(paused), "tracked": False},
        },
    )

    _seed_task(
        waiting / ".pollypm" / "state.db", waiting,
        project="waiting_proj",
        title="approve the plan",
        kind=InboxItemKind.APPROVAL_REQUEST.value,
    )
    _seed_task(
        working / ".pollypm" / "state.db", working,
        project="working_proj",
        title="ship feature",
        kind=InboxItemKind.INFO.value,
        status="in_progress",
    )
    _seed_task(
        idle / ".pollypm" / "state.db", idle,
        project="idle_proj",
        title="idle stub",
        kind=InboxItemKind.INFO.value,
    )

    return {
        "config_path": config_path,
        "workspace_root": workspace_root,
        "projects": {
            "waiting": waiting,
            "working": working,
            "idle": idle,
            "paused": paused,
        },
    }


def test_load_view_partitions_projects(workspace) -> None:
    view = load_operator_view(workspace["config_path"])
    waiting_keys = [r.project_key for r in view.waiting]
    working_keys = [r.project_key for r in view.working]
    idle_keys = [r.project_key for r in view.idle]
    paused_keys = [r.project_key for r in view.paused]

    assert "waiting_proj" in waiting_keys
    assert "working_proj" in working_keys
    assert "idle_proj" in idle_keys
    assert "paused_proj" in paused_keys

    # No project appears twice.
    every = waiting_keys + working_keys + idle_keys + paused_keys
    assert len(every) == len(set(every))


def test_load_view_detail_strings(workspace) -> None:
    view = load_operator_view(workspace["config_path"])
    waiting_row = next(r for r in view.waiting if r.project_key == "waiting_proj")
    assert waiting_row.detail == "Needs your approval"
    assert waiting_row.glyph == "◆"

    working_row = next(r for r in view.working if r.project_key == "working_proj")
    assert "ship feature" in working_row.detail
    assert working_row.glyph == "●"


def test_state_map_agrees_with_view(workspace) -> None:
    """The rail and the dashboard must read the same per-project state."""
    from pollypm.config import load_config

    config = load_config(workspace["config_path"])
    state_map = project_state_map_from_config(config)
    view = load_operator_view(workspace["config_path"])

    expected = {}
    for row in view.waiting:
        expected[row.project_key] = ProjectState.WAITING
    for row in view.working:
        expected[row.project_key] = ProjectState.WORKING
    for row in view.idle:
        expected[row.project_key] = ProjectState.IDLE
    for row in view.paused:
        expected[row.project_key] = ProjectState.PAUSED

    for key, state in expected.items():
        assert state_map[key] is state, (
            f"{key}: dashboard says {state.value}, rail map says {state_map[key].value}"
        )


def test_ascii_render_includes_all_sections(workspace) -> None:
    view = load_operator_view(workspace["config_path"])
    out = view_as_ascii(view)
    assert "Waiting on you" in out
    assert "Working" in out
    assert "Idle" in out
    assert "◆ waiting_proj" in out
    assert "● working_proj" in out
    assert "○ idle_proj" in out
    # Paused section appears when there are paused projects.
    assert "Paused" in out
    assert "⏸ paused_proj" in out


def test_ascii_render_handles_empty_view() -> None:
    from pollypm.dashboard.categorization import OperatorDashboardView

    out = view_as_ascii(OperatorDashboardView())
    assert "Nothing waiting." in out
    assert "Nothing actively working." in out
