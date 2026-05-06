"""Tests for the worker-marker reaper (#1338).

Covers both the bootstrap-time reaper
(:func:`reap_orphan_worker_markers`) and the runtime sweep
(:func:`sweep_worker_markers`). Each test wires up a minimal
``PollyPMConfig`` with a single project and a fake tmux client so the
classification + tmux-kill paths can be exercised without a live tmux
or live cockpit.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from pollypm.models import (
    AccountConfig,
    KnownProject,
    PollyPMConfig,
    PollyPMSettings,
    ProjectKind,
    ProjectSettings,
    ProviderKind,
)
from pollypm.work.worker_marker_reaper import (
    WORKER_MARKER_REAPABLE_STATUSES,
    reap_orphan_worker_markers,
    sweep_worker_markers,
    _resolve_storage_session,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _bootstrap_work_db(db_path: Path) -> None:
    """Create a minimal ``work_tasks`` table at ``db_path``."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE work_tasks (
                project TEXT NOT NULL,
                task_number INTEGER NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                type TEXT NOT NULL DEFAULT 'task',
                labels TEXT NOT NULL DEFAULT '[]',
                work_status TEXT NOT NULL DEFAULT 'draft',
                flow_template_id TEXT NOT NULL DEFAULT 'default',
                flow_template_version INTEGER NOT NULL DEFAULT 1,
                current_node_id TEXT,
                assignee TEXT,
                priority TEXT NOT NULL DEFAULT 'normal',
                requires_human_review INTEGER NOT NULL DEFAULT 0,
                description TEXT NOT NULL DEFAULT '',
                acceptance_criteria TEXT,
                constraints TEXT,
                relevant_files TEXT NOT NULL DEFAULT '[]',
                parent_project TEXT,
                parent_task_number INTEGER,
                supersedes_project TEXT,
                supersedes_task_number INTEGER,
                roles TEXT NOT NULL DEFAULT '{}',
                external_refs TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT '',
                created_by TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (project, task_number)
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _insert_task(db_path: Path, project: str, number: int, status: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO work_tasks (project, task_number, title, "
            "flow_template_id, work_status) VALUES (?, ?, ?, ?, ?)",
            (project, number, f"task-{number}", "default", status),
        )
        conn.commit()
    finally:
        conn.close()


def _make_config(
    *,
    workspace_root: Path,
    project_path: Path,
    project_key: str = "demo",
) -> PollyPMConfig:
    project = KnownProject(
        key=project_key,
        path=project_path,
        name="Demo",
        kind=ProjectKind.FOLDER,
        tracked=True,
    )
    return PollyPMConfig(
        project=ProjectSettings(
            name="Workspace",
            root_dir=workspace_root,
            tmux_session="test-pm",
            workspace_root=workspace_root,
            base_dir=workspace_root / ".pollypm",
        ),
        pollypm=PollyPMSettings(
            controller_account="claude_test",
            failover_accounts=[],
        ),
        accounts={
            "claude_test": AccountConfig(
                name="claude_test",
                provider=ProviderKind.CLAUDE,
                email="t@example.com",
                home=workspace_root / ".pollypm" / "homes" / "claude_test",
            ),
        },
        sessions={},
        projects={project_key: project},
    )


def _write_marker(project_path: Path, window_name: str) -> Path:
    marker_dir = project_path / ".pollypm" / "worker-markers"
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_dir / f"{window_name}.fresh"
    marker.write_text("ts")
    return marker


class FakeTmux:
    """Minimal tmux double — enough to satisfy the reaper's call surface."""

    def __init__(
        self,
        *,
        session_name: str | None = None,
        window_names: list[str] | None = None,
    ) -> None:
        self._session_name = session_name
        self._window_names = list(window_names or [])
        self.killed: list[str] = []

    def has_session(self, name: str) -> bool:
        return name == self._session_name

    def list_windows(self, name: str) -> list[Any]:
        if name != self._session_name:
            return []

        class _W:
            def __init__(self, n: str) -> None:
                self.name = n

        return [_W(n) for n in self._window_names]

    def kill_window(self, target: str) -> None:
        self.killed.append(target)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_resolve_storage_session_uses_supervisor_suffix(tmp_path: Path) -> None:
    """``_resolve_storage_session`` must mirror Supervisor's actual suffix."""
    from pollypm.supervisor import Supervisor

    config = _make_config(
        workspace_root=tmp_path,
        project_path=tmp_path / "project",
    )
    suffix = Supervisor._STORAGE_CLOSET_SESSION_SUFFIX
    expected = f"{config.project.tmux_session}{suffix}"

    assert _resolve_storage_session(config) == expected


# ---------------------------------------------------------------------------
# Bootstrap reaper
# ---------------------------------------------------------------------------


def test_bootstrap_reaps_marker_with_missing_task_row(tmp_path: Path) -> None:
    """Marker present, no work_tasks row → orphan, reap."""
    project_path = tmp_path / "project"
    project_path.mkdir()
    db_path = project_path / ".pollypm" / "state.db"
    _bootstrap_work_db(db_path)

    marker = _write_marker(project_path, "task-demo-7")
    config = _make_config(
        workspace_root=tmp_path, project_path=project_path,
    )

    reaped = reap_orphan_worker_markers(config, tmux=FakeTmux())

    assert not marker.exists()
    assert len(reaped) == 1
    assert reaped[0].window_name == "task-demo-7"
    assert "no work_tasks row" in reaped[0].reason


@pytest.mark.parametrize("status", sorted(WORKER_MARKER_REAPABLE_STATUSES))
def test_bootstrap_reaps_marker_for_terminal_task(
    tmp_path: Path, status: str,
) -> None:
    """Marker present, work_tasks row in done/cancelled/abandoned → reap."""
    project_path = tmp_path / "project"
    project_path.mkdir()
    db_path = project_path / ".pollypm" / "state.db"
    _bootstrap_work_db(db_path)
    _insert_task(db_path, "demo", 12, status)

    marker = _write_marker(project_path, "task-demo-12")
    config = _make_config(workspace_root=tmp_path, project_path=project_path)

    reaped = reap_orphan_worker_markers(config, tmux=FakeTmux())

    assert not marker.exists()
    assert len(reaped) == 1
    assert status in reaped[0].reason


def test_bootstrap_keeps_live_marker(tmp_path: Path) -> None:
    """Marker present, work_tasks row in_progress, tmux window live → keep."""
    project_path = tmp_path / "project"
    project_path.mkdir()
    db_path = project_path / ".pollypm" / "state.db"
    _bootstrap_work_db(db_path)
    _insert_task(db_path, "demo", 4, "in_progress")

    marker = _write_marker(project_path, "task-demo-4")
    config = _make_config(workspace_root=tmp_path, project_path=project_path)
    storage = _resolve_storage_session(config)
    assert storage is not None
    tmux = FakeTmux(session_name=storage, window_names=["task-demo-4"])

    reaped = reap_orphan_worker_markers(config, tmux=tmux)

    assert marker.exists(), "live marker must not be reaped"
    assert reaped == []


def test_bootstrap_reaps_savethenovel_case(tmp_path: Path) -> None:
    """Marker present, work_tasks in_progress, tmux window MISSING → reap.

    The savethenovel orphan that prompted #1338: the work_tasks row
    looks alive, but the tmux window crashed without firing the
    happy-path unlink. The reaper should still pick it up.
    """
    project_path = tmp_path / "project"
    project_path.mkdir()
    db_path = project_path / ".pollypm" / "state.db"
    _bootstrap_work_db(db_path)
    _insert_task(db_path, "demo", 99, "in_progress")

    marker = _write_marker(project_path, "task-demo-99")
    config = _make_config(workspace_root=tmp_path, project_path=project_path)
    # Storage session exists but does not include the task window.
    storage = _resolve_storage_session(config)
    assert storage is not None
    tmux = FakeTmux(session_name=storage, window_names=[])

    reaped = reap_orphan_worker_markers(config, tmux=tmux)

    assert not marker.exists()
    assert len(reaped) == 1
    assert "tmux window missing" in reaped[0].reason


def test_bootstrap_no_op_when_db_missing(tmp_path: Path) -> None:
    """Fresh install: no DB at all → reaper must be a no-op (defensive)."""
    project_path = tmp_path / "project"
    project_path.mkdir()
    # No DB; just a marker.
    marker = _write_marker(project_path, "task-demo-1")
    config = _make_config(workspace_root=tmp_path, project_path=project_path)

    reaped = reap_orphan_worker_markers(config, tmux=FakeTmux())

    assert marker.exists(), "must not reap without evidence of orphan"
    assert reaped == []


def test_bootstrap_skips_unrecognised_marker_names(tmp_path: Path) -> None:
    """Markers not matching ``task-<project>-<N>`` must be left untouched."""
    project_path = tmp_path / "project"
    project_path.mkdir()
    db_path = project_path / ".pollypm" / "state.db"
    _bootstrap_work_db(db_path)

    marker = _write_marker(project_path, "weird-name-without-task-prefix")
    config = _make_config(workspace_root=tmp_path, project_path=project_path)

    reaped = reap_orphan_worker_markers(config, tmux=FakeTmux())

    assert marker.exists()
    assert reaped == []


def test_bootstrap_handles_no_projects(tmp_path: Path) -> None:
    """Empty ``config.projects`` → no-op."""
    config = _make_config(workspace_root=tmp_path, project_path=tmp_path / "p")
    config.projects.clear()

    assert reap_orphan_worker_markers(config, tmux=FakeTmux()) == []


# ---------------------------------------------------------------------------
# Runtime sweep
# ---------------------------------------------------------------------------


def test_runtime_sweep_kills_dead_window_and_unlinks_marker(
    tmp_path: Path,
) -> None:
    """Runtime sweep finds orphan, kills its (mocked-dead) window, unlinks marker.

    The sweep should call ``kill_window`` with the canonical
    ``<storage>:<window>`` target so a stale ``remain-on-exit`` pane
    can't trip the next ``_kill_stale_task_window`` re-claim path.
    """
    project_path = tmp_path / "project"
    project_path.mkdir()
    db_path = project_path / ".pollypm" / "state.db"
    _bootstrap_work_db(db_path)
    # Task row is gone (cancelled/deleted), but marker is still on disk.
    marker = _write_marker(project_path, "task-demo-42")

    config = _make_config(workspace_root=tmp_path, project_path=project_path)
    storage = _resolve_storage_session(config)
    assert storage is not None
    tmux = FakeTmux(session_name=storage, window_names=[])

    reaped = sweep_worker_markers(config, tmux=tmux)

    assert not marker.exists()
    assert len(reaped) == 1
    assert tmux.killed == [f"{storage}:task-demo-42"]


def test_runtime_sweep_handles_kill_failure_silently(tmp_path: Path) -> None:
    """``kill_window`` failure must not stop the marker unlink."""
    project_path = tmp_path / "project"
    project_path.mkdir()
    db_path = project_path / ".pollypm" / "state.db"
    _bootstrap_work_db(db_path)
    marker = _write_marker(project_path, "task-demo-8")

    config = _make_config(workspace_root=tmp_path, project_path=project_path)
    storage = _resolve_storage_session(config)
    assert storage is not None

    class _FlakyTmux(FakeTmux):
        def kill_window(self, target: str) -> None:  # noqa: D401
            raise RuntimeError("tmux unhappy")

    tmux = _FlakyTmux(session_name=storage, window_names=[])

    reaped = sweep_worker_markers(config, tmux=tmux)

    assert not marker.exists(), "marker must be unlinked even if kill fails"
    assert len(reaped) == 1
