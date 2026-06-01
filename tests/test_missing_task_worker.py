"""Tests for #1070 ``missing_task_worker`` alert emission.

Detection-only sweep: enumerates ``in_progress`` / ``rework`` tasks
with a ``worker`` role and asserts the corresponding
``task-<project>-<N>`` tmux window exists in the storage-closet. If
not (worker died, ``pm reset --force`` blew it away, host reboot,
etc.), the sweep emits a warn-level alert pointing the operator at
the manual recovery flow. Recovery itself is intentionally out of
scope — the alert exists so the gap stops being invisible.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pollypm.plugins_builtin.task_assignment_notify.handlers import sweep as sweep_mod
from pollypm.plugins_builtin.task_assignment_notify.handlers.sweep import (
    MISSING_TASK_WORKER_ALERT_TYPE,
    _missing_task_worker_session_name,
    _sweep_missing_task_workers,
)
from pollypm.plugins_builtin.task_assignment_notify.resolver import _RuntimeServices
from pollypm.work.models import WorkStatus


class _FakeProject:
    def __init__(self, *, key: str, path: Path = Path("/tmp/proj")) -> None:
        self.key = key
        self.path = path
        # Required for compatibility with sweep-body helpers; the
        # missing-task-worker pass doesn't read these but the harness
        # walks the same registered-project list as the rest of the
        # sweep.
        self.auto_claim = None
        self.max_concurrent_workers = None


class _FakeWindow:
    def __init__(self, name: str, *, pane_dead: bool = False) -> None:
        self.name = name
        self.pane_dead = pane_dead


class _FakeTmux:
    def __init__(self, windows: list[_FakeWindow]) -> None:
        self._windows = windows
        self.calls: list[str] = []

    def list_windows(self, session_name: str) -> list[_FakeWindow]:
        self.calls.append(session_name)
        return list(self._windows)


class _FakeSessionService:
    def __init__(self, windows: list[_FakeWindow]) -> None:
        self.tmux = _FakeTmux(windows)

    def storage_closet_session_name(self) -> str:
        return "pollypm-storage-closet"


class _FakeStore:
    def __init__(self) -> None:
        self.upserts: list[tuple[str, str, str, str]] = []
        self.clears: list[tuple[str, str]] = []

    def upsert_alert(
        self, session_name: str, alert_type: str, severity: str, message: str,
    ) -> None:
        self.upserts.append((session_name, alert_type, severity, message))

    def clear_alert(self, session_name: str, alert_type: str) -> None:
        self.clears.append((session_name, alert_type))


def _make_task(
    *,
    project: str,
    task_number: int,
    work_status: str,
    roles: dict[str, Any] | None = None,
) -> SimpleNamespace:
    """Build a minimal task double with the fields the sweep reads."""
    return SimpleNamespace(
        project=project,
        task_number=task_number,
        task_id=f"{project}/{task_number}",
        work_status=work_status,
        roles=roles if roles is not None else {"worker": "claude"},
    )


class _FakeWorkService:
    """Returns canned active-worker tasks for the missing-task sweep.

    Behaves both as a workspace DB (filters by ``project=...``) and a
    per-project DB (returns everything when ``project`` is None).
    ``work_status`` filtering matches the sweep's
    ``_ACTIVE_WORKER_STATUSES`` enumeration.
    """

    def __init__(
        self,
        tasks: list[SimpleNamespace],
        *,
        is_workspace: bool = False,
    ) -> None:
        self._tasks = tasks
        self._is_workspace = is_workspace
        self.closed = False

    def list_tasks(
        self, *, project: str | None = None, work_status: str = "",
    ) -> list[SimpleNamespace]:
        out: list[SimpleNamespace] = []
        for task in self._tasks:
            if work_status and task.work_status != work_status:
                continue
            if project is not None and task.project != project:
                continue
            if project is None and self._is_workspace:
                # Workspace DB requires an explicit project filter — the
                # sweep always passes one for workspace queries.
                continue
            out.append(task)
        return out

    def close(self) -> None:
        self.closed = True


def _build_services(
    *,
    windows: list[_FakeWindow],
    known_projects: tuple[_FakeProject, ...],
    store: _FakeStore | None = None,
    workspace_tasks: list[SimpleNamespace] | None = None,
) -> _RuntimeServices:
    return _RuntimeServices(
        session_service=_FakeSessionService(windows),
        state_store=store,
        msg_store=store,
        work_service=_FakeWorkService(workspace_tasks or [], is_workspace=True),
        project_root=Path("/tmp"),
        known_projects=known_projects,
    )


def _patch_project_db(
    monkeypatch: pytest.MonkeyPatch,
    tasks_by_project: dict[str, list[SimpleNamespace]],
) -> None:
    """Stub ``_open_project_work_service`` to return a per-project fake."""

    def _opener(project: _FakeProject, _services: Any) -> _FakeWorkService:
        rows = list(tasks_by_project.get(project.key, []))
        return _FakeWorkService(rows, is_workspace=False)

    monkeypatch.setattr(sweep_mod, "_open_project_work_service", _opener)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_session_name_is_namespaced_per_task() -> None:
    assert (
        _missing_task_worker_session_name("polly_remote/13")
        == "missing_task_worker-polly_remote/13"
    )


# ---------------------------------------------------------------------------
# Detection — fires when worker window is missing
# ---------------------------------------------------------------------------


def test_emits_alert_when_in_progress_task_has_no_worker_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug from #1070: in_progress task, no per-task tmux window."""
    project = _FakeProject(key="polly_remote")
    store = _FakeStore()
    services = _build_services(
        # Storage closet has the parent worker window but NOT the
        # per-task task-* window — this is the post-`pm reset --force`
        # state where supervisors restarted but per-task workers
        # didn't respawn.
        windows=[
            _FakeWindow("worker_polly_remote"),
            _FakeWindow("pm-heartbeat"),
        ],
        known_projects=(project,),
        store=store,
    )
    _patch_project_db(monkeypatch, {
        "polly_remote": [
            _make_task(
                project="polly_remote",
                task_number=14,
                work_status=WorkStatus.IN_PROGRESS.value,
            ),
        ],
    })

    summary = _sweep_missing_task_workers(services)

    assert summary["emitted"] == 1
    assert summary["cleared"] == 0
    assert summary["considered"] == 1
    assert len(store.upserts) == 1
    scope, alert_type, severity, message = store.upserts[0]
    assert scope == "missing_task_worker-polly_remote/14"
    assert alert_type == MISSING_TASK_WORKER_ALERT_TYPE
    assert severity == "warn"
    # Wording must point the operator at concrete recovery steps and
    # the per-task log directory so they can investigate.
    assert "polly_remote/14" in message
    assert "task-polly_remote-14" in message
    assert "pm task hold polly_remote/14" in message
    assert "pm task claim polly_remote/14" in message
    assert "logs/task_polly_remote_14" in message


def test_emits_alert_for_rework_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rework state mirrors in_progress — both block on a live worker."""
    project = _FakeProject(key="polly_remote")
    store = _FakeStore()
    services = _build_services(
        windows=[],
        known_projects=(project,),
        store=store,
    )
    _patch_project_db(monkeypatch, {
        "polly_remote": [
            _make_task(
                project="polly_remote",
                task_number=13,
                work_status=WorkStatus.REWORK.value,
            ),
        ],
    })

    summary = _sweep_missing_task_workers(services)

    assert summary["emitted"] == 1
    assert any(
        u[0] == "missing_task_worker-polly_remote/13" for u in store.upserts
    )


# ---------------------------------------------------------------------------
# Auto-clear — fires when the worker window comes back / task resolves
# ---------------------------------------------------------------------------


def test_clears_alert_when_worker_window_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-task window present → clear stale alert, no emission."""
    project = _FakeProject(key="polly_remote")
    store = _FakeStore()
    services = _build_services(
        windows=[_FakeWindow("task-polly_remote-14")],
        known_projects=(project,),
        store=store,
    )
    _patch_project_db(monkeypatch, {
        "polly_remote": [
            _make_task(
                project="polly_remote",
                task_number=14,
                work_status=WorkStatus.IN_PROGRESS.value,
            ),
        ],
    })

    summary = _sweep_missing_task_workers(services)

    assert summary["emitted"] == 0
    assert summary["cleared"] == 1
    assert store.upserts == []
    assert store.clears == [
        ("missing_task_worker-polly_remote/14", MISSING_TASK_WORKER_ALERT_TYPE),
    ]


# ---------------------------------------------------------------------------
# Filtering — only worker-role tasks, only active statuses
# ---------------------------------------------------------------------------


def test_skips_tasks_without_worker_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tasks whose roles dict has no ``worker`` are out of scope."""
    project = _FakeProject(key="polly_remote")
    store = _FakeStore()
    services = _build_services(
        windows=[],
        known_projects=(project,),
        store=store,
    )
    _patch_project_db(monkeypatch, {
        "polly_remote": [
            _make_task(
                project="polly_remote",
                task_number=14,
                work_status=WorkStatus.IN_PROGRESS.value,
                roles={"reviewer": "human"},
            ),
        ],
    })

    summary = _sweep_missing_task_workers(services)

    assert summary == {"emitted": 0, "cleared": 0, "considered": 0}
    assert store.upserts == []


def test_skips_terminal_status_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Done / cancelled / queued tasks aren't considered."""
    project = _FakeProject(key="polly_remote")
    store = _FakeStore()
    services = _build_services(
        windows=[],
        known_projects=(project,),
        store=store,
    )
    _patch_project_db(monkeypatch, {
        "polly_remote": [
            _make_task(
                project="polly_remote",
                task_number=10,
                work_status=WorkStatus.DONE.value,
            ),
            _make_task(
                project="polly_remote",
                task_number=11,
                work_status=WorkStatus.QUEUED.value,
            ),
            _make_task(
                project="polly_remote",
                task_number=12,
                work_status=WorkStatus.CANCELLED.value,
            ),
        ],
    })

    summary = _sweep_missing_task_workers(services)

    assert summary == {"emitted": 0, "cleared": 0, "considered": 0}
    assert store.upserts == []


# ---------------------------------------------------------------------------
# Bail conditions — no false positives when we can't enumerate
# ---------------------------------------------------------------------------


def test_skip_when_session_service_cannot_enumerate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No session service → bail without emitting false-positives."""
    project = _FakeProject(key="polly_remote")
    store = _FakeStore()
    services = _RuntimeServices(
        session_service=None,
        state_store=store,
        msg_store=store,
        work_service=_FakeWorkService([], is_workspace=True),
        project_root=Path("/tmp"),
        known_projects=(project,),
    )
    _patch_project_db(monkeypatch, {
        "polly_remote": [
            _make_task(
                project="polly_remote",
                task_number=14,
                work_status=WorkStatus.IN_PROGRESS.value,
            ),
        ],
    })

    summary = _sweep_missing_task_workers(services)

    assert summary == {"emitted": 0, "cleared": 0, "considered": 0}
    assert store.upserts == []
    assert store.clears == []


# ---------------------------------------------------------------------------
# Multi-task — each gap surfaces independently
# ---------------------------------------------------------------------------


def test_multiple_missing_workers_each_get_their_own_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact #1070 repro: polly_remote/13 (rework) + /14 (in_progress)."""
    project = _FakeProject(key="polly_remote")
    store = _FakeStore()
    services = _build_services(
        windows=[_FakeWindow("worker_polly_remote")],
        known_projects=(project,),
        store=store,
    )
    _patch_project_db(monkeypatch, {
        "polly_remote": [
            _make_task(
                project="polly_remote",
                task_number=13,
                work_status=WorkStatus.REWORK.value,
            ),
            _make_task(
                project="polly_remote",
                task_number=14,
                work_status=WorkStatus.IN_PROGRESS.value,
            ),
        ],
    })

    summary = _sweep_missing_task_workers(services)

    assert summary["emitted"] == 2
    assert summary["considered"] == 2
    scopes = {u[0] for u in store.upserts}
    assert scopes == {
        "missing_task_worker-polly_remote/13",
        "missing_task_worker-polly_remote/14",
    }


def test_liveness_gate_skips_dormant_project_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2530: dormant projects are not scanned for missing workers."""
    live_project = _FakeProject(key="polly_remote")
    dormant_project = _FakeProject(key="pm_test_01wave_1779715196")
    store = _FakeStore()
    services = _build_services(
        windows=[],
        known_projects=(live_project, dormant_project),
        store=store,
    )
    opened: list[str] = []

    def _opener(project: _FakeProject, _services: Any) -> _FakeWorkService:
        opened.append(project.key)
        return _FakeWorkService(
            [
                _make_task(
                    project=project.key,
                    task_number=1,
                    work_status=WorkStatus.IN_PROGRESS.value,
                ),
            ],
            is_workspace=False,
        )

    monkeypatch.setattr(sweep_mod, "_open_project_work_service", _opener)

    summary = _sweep_missing_task_workers(
        services,
        recent_real_work_projects=frozenset({"polly_remote"}),
    )

    assert opened == ["polly_remote"]
    assert summary["emitted"] == 1
    assert summary["considered"] == 1
    assert {row[0] for row in store.upserts} == {
        "missing_task_worker-polly_remote/1",
    }
