"""Tests for the per-project sweep fanout — #259.

The ``task_assignment.sweep`` handler used to open only the workspace-
root state DB. Projects registered via ``pm project new`` store their
tasks in ``<project_path>/.pollypm/state.db``, so those tasks were
invisible to the sweep — workers never got their pickup pings.

These tests verify the fixed sweeper:

* iterates every ``config.projects`` entry, opening each per-project DB
  and sweeping tasks inside;
* skips missing per-project state.db files without error;
* emits a ``no_session`` alert for queued tasks whose role has no live
  session, deduped per ``(project, role)`` per sweep cycle;
* preserves the legacy workspace-root behavior (no regression);
* dedupes alerts across repeated sweeps (``upsert_alert`` refresh, not
  duplicate row).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from pollypm.plugins_builtin.task_assignment_notify.handlers.sweep import (
    task_assignment_sweep_handler,
)
from pollypm.plugins_builtin.task_assignment_notify.resolver import _RuntimeServices
from pollypm.storage.state import StateStore
from pollypm.work import task_assignment as bus
from pollypm.work.sqlite_service import SQLiteWorkService


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class FakeHandle:
    name: str


@dataclass
class FakeSessionService:
    handles: list[FakeHandle]
    sent: list[tuple[str, str]] = field(default_factory=list)
    busy: set[str] = field(default_factory=set)

    def list(self) -> list[FakeHandle]:
        return list(self.handles)

    def send(self, name: str, text: str, *, press_enter: bool = True) -> None:
        self.sent.append((name, text))

    def is_turn_active(self, name: str) -> bool:
        return name in self.busy


@dataclass
class FakeKnownProject:
    """Stand-in for ``pollypm.models.KnownProject`` — the sweeper reads
    only ``.key`` and ``.path`` so we don't need the full dataclass."""

    key: str
    path: Path


def _make_project_db(project_path: Path, *, project_key: str) -> Path:
    """Create ``<project_path>/.pollypm/state.db`` with a queued task.

    Returns the resulting DB path. The task lives under ``project_key``
    with a ``worker`` role so the sweep's resolver expects a live
    ``worker-<project_key>`` session (or a ``no_session`` alert).
    """
    db_dir = project_path / ".pollypm"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "state.db"
    bus.clear_listeners()
    work = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        task = work.create(
            title=f"Do something in {project_key}",
            description="Queued work for the per-project sweep",
            type="task",
            project=project_key,
            flow_template="standard",
            roles={"worker": "agent-1", "reviewer": "agent-2"},
            priority="normal",
        )
        work.queue(task.task_id, "pm")
    finally:
        work.close()
    return db_path


def _install_fake_loader(monkeypatch, services_factory):
    """Patch ``load_runtime_services`` used by the sweep handler."""
    monkeypatch.setattr(
        "pollypm.plugins_builtin.task_assignment_notify.handlers.sweep.load_runtime_services",
        lambda *, config_path=None: services_factory(),
    )


# ---------------------------------------------------------------------------
# Core: sweeper iterates per-project DBs
# ---------------------------------------------------------------------------


class TestPerProjectFanout:
    """#259: the sweep must open every registered per-project DB."""

    def test_sweeper_opens_multiple_per_project_dbs(self, tmp_path, monkeypatch):
        """Two projects, each with a queued task — both pickup pings fire."""
        proj_a = tmp_path / "proj_a"
        proj_b = tmp_path / "proj_b"
        proj_a.mkdir()
        proj_b.mkdir()
        _make_project_db(proj_a, project_key="alpha")
        _make_project_db(proj_b, project_key="beta")

        store = StateStore(tmp_path / "workspace_state.db")
        session_svc = FakeSessionService(handles=[
            FakeHandle("worker-alpha"),
            FakeHandle("worker-beta"),
        ])

        def _factory():
            return _RuntimeServices(
                session_service=session_svc,
                state_store=store,
                work_service=None,  # no workspace-level tasks
                project_root=tmp_path,
                known_projects=(
                    FakeKnownProject(key="alpha", path=proj_a),
                    FakeKnownProject(key="beta", path=proj_b),
                ),
                enforce_plan=False,  # #273: #259 tests pre-date the plan gate
            )

        _install_fake_loader(monkeypatch, _factory)

        result = task_assignment_sweep_handler({})

        assert result["outcome"] == "swept"
        assert result["projects_scanned"] == 2
        assert result["projects_skipped"] == 0
        assert result["by_outcome"].get("sent", 0) == 2

        # Both workers got their pickup ping.
        recipients = {name for name, _text in session_svc.sent}
        assert recipients == {"worker-alpha", "worker-beta"}
        assert all("New work" in text for _name, text in session_svc.sent)

        store.close()

    def test_sweeper_skips_missing_per_project_db(self, tmp_path, monkeypatch):
        """A project registered but never touched (no state.db) is skipped
        silently, and the sweep still processes the other projects."""
        proj_real = tmp_path / "proj_real"
        proj_missing = tmp_path / "proj_missing"
        proj_real.mkdir()
        proj_missing.mkdir()
        # Only proj_real has a state.db — proj_missing has an empty dir
        # with no ``.pollypm`` subfolder.
        _make_project_db(proj_real, project_key="real")

        store = StateStore(tmp_path / "workspace_state.db")
        session_svc = FakeSessionService(handles=[FakeHandle("worker-real")])

        def _factory():
            return _RuntimeServices(
                session_service=session_svc,
                state_store=store,
                work_service=None,
                project_root=tmp_path,
                known_projects=(
                    FakeKnownProject(key="real", path=proj_real),
                    FakeKnownProject(key="missing", path=proj_missing),
                ),
                enforce_plan=False,  # #273: pre-dates plan gate
            )

        _install_fake_loader(monkeypatch, _factory)

        result = task_assignment_sweep_handler({})

        # Sweep didn't error on the missing DB — it scanned one and
        # skipped one.
        assert result["outcome"] == "swept"
        assert result["projects_scanned"] == 1
        assert result["projects_skipped"] == 1
        assert result["by_outcome"].get("sent", 0) == 1
        assert session_svc.sent and session_svc.sent[0][0] == "worker-real"

        store.close()


# ---------------------------------------------------------------------------
# no_session alert emission
# ---------------------------------------------------------------------------


class TestNoSessionAlerts:
    """A queued task with no matching worker session must raise an
    operator-visible alert — previously silent (#259)."""

    def test_queued_task_without_worker_raises_no_session_alert(
        self, tmp_path, monkeypatch,
    ):
        proj = tmp_path / "proj"
        proj.mkdir()
        _make_project_db(proj, project_key="ghost")

        store = StateStore(tmp_path / "workspace_state.db")
        # No live sessions — the queued ghost/1 task has no worker.
        session_svc = FakeSessionService(handles=[])

        def _factory():
            return _RuntimeServices(
                session_service=session_svc,
                state_store=store,
                work_service=None,
                project_root=tmp_path,
                known_projects=(FakeKnownProject(key="ghost", path=proj),),
                enforce_plan=False,  # #273: pre-dates plan gate
            )

        _install_fake_loader(monkeypatch, _factory)

        result = task_assignment_sweep_handler({})

        assert result["outcome"] == "swept"
        # The sweep-level aggregate alert fired once for (ghost, worker).
        assert result["no_session_alerts"] == 1
        assert result["by_outcome"].get("no_session", 0) >= 1
        # No pings were sent.
        assert session_svc.sent == []

        alerts = store.open_alerts()
        # One sweep-level (project, role) alert keyed by alert_type="no_session".
        sweep_alerts = [a for a in alerts if a.alert_type == "no_session"]
        assert len(sweep_alerts) == 1
        alert = sweep_alerts[0]
        # Session name is the first role candidate — ``worker-ghost``.
        assert alert.session_name == "worker-ghost"
        assert alert.severity == "warn"
        assert "ghost" in alert.message
        assert "pm worker-start ghost" in alert.message

        store.close()

    def test_repeated_sweeps_do_not_duplicate_alerts(
        self, tmp_path, monkeypatch,
    ):
        """``upsert_alert`` dedupes on ``(session_name, alert_type, open)`` —
        running the sweep twice should leave exactly one open alert row."""
        proj = tmp_path / "proj"
        proj.mkdir()
        _make_project_db(proj, project_key="ghost")

        store = StateStore(tmp_path / "workspace_state.db")
        session_svc = FakeSessionService(handles=[])

        def _factory():
            return _RuntimeServices(
                session_service=session_svc,
                state_store=store,
                work_service=None,
                project_root=tmp_path,
                known_projects=(FakeKnownProject(key="ghost", path=proj),),
                enforce_plan=False,  # #273: pre-dates plan gate
            )

        _install_fake_loader(monkeypatch, _factory)

        # First sweep → alert created.
        task_assignment_sweep_handler({})
        first = [a for a in store.open_alerts() if a.alert_type == "no_session"]
        assert len(first) == 1

        # Second sweep → alert refreshed, not duplicated.
        task_assignment_sweep_handler({})
        second = [a for a in store.open_alerts() if a.alert_type == "no_session"]
        assert len(second) == 1
        # Same row id as the first emission (upsert path, not INSERT).
        assert first[0].alert_id == second[0].alert_id

        store.close()


# ---------------------------------------------------------------------------
# Regression: workspace-root DB behavior preserved
# ---------------------------------------------------------------------------


class TestWorkspaceRootRegression:
    """Pre-#259 behavior: the workspace-root DB still gets swept. With no
    projects registered, the sweep must behave exactly as before."""

    def test_workspace_root_sweep_still_works_with_no_projects(
        self, tmp_path, monkeypatch,
    ):
        bus.clear_listeners()
        # Workspace-root work service with a queued task.
        workspace_db = tmp_path / "workspace_work.db"
        workspace_work = SQLiteWorkService(db_path=workspace_db)
        task = workspace_work.create(
            title="Root-level task",
            description="Lives in the workspace-root DB",
            type="task",
            project="root",
            flow_template="standard",
            roles={"worker": "agent-1", "reviewer": "agent-2"},
            priority="normal",
        )
        workspace_work.queue(task.task_id, "pm")
        # The sweep will re-open + close the connection it receives; we
        # detach our reference so the handler's close() is the only one.
        workspace_work.close()
        workspace_work = SQLiteWorkService(db_path=workspace_db)

        store = StateStore(tmp_path / "workspace_state.db")
        session_svc = FakeSessionService(handles=[FakeHandle("worker-root")])

        def _factory():
            return _RuntimeServices(
                session_service=session_svc,
                state_store=store,
                work_service=workspace_work,
                project_root=tmp_path,
                known_projects=(),  # no registered projects
            )

        _install_fake_loader(monkeypatch, _factory)

        result = task_assignment_sweep_handler({})

        assert result["outcome"] == "swept"
        assert result["projects_scanned"] == 0
        assert result["by_outcome"].get("sent", 0) == 1
        assert session_svc.sent and session_svc.sent[0][0] == "worker-root"

        store.close()
