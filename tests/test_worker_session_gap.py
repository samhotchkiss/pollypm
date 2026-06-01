"""Tests for #1054 ``worker_session_gap`` alert emission.

The post-sweep pass scans registered projects and emits a
``worker_session_gap`` warn alert for any project that has queued
tasks but no ``worker_<project>`` (or ``worker-<project>``) tmux window
in the storage-closet. Auto-clears when the queue drains or a worker
session appears.

The tests below stub the work-service + tmux client so we can pin the
contract without spinning up real tmux sessions or DBs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pollypm.plugins_builtin.task_assignment_notify.handlers import sweep as sweep_mod
from pollypm.plugins_builtin.task_assignment_notify.handlers.sweep import (
    WORKER_SESSION_GAP_ALERT_TYPE,
    _project_has_worker_session,
    _sweep_worker_session_gaps,
    _worker_session_gap_session_name,
)
from pollypm.plugins_builtin.task_assignment_notify.resolver import _RuntimeServices


class _FakeProject:
    def __init__(self, *, key: str, path: Path = Path("/tmp/proj")) -> None:
        self.key = key
        self.path = path
        # Required for _auto_claim_enabled_for_project compatibility,
        # though the gap pass doesn't read these.
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


class _FakeWorkService:
    """Minimal work-service double — returns canned queued-task lists.

    Behaves as both a per-project DB (queries without ``project`` filter
    return everything in ``_queued``) and a workspace DB (queries with
    ``project=...`` return that project's slice).
    """

    def __init__(
        self,
        queued_by_project: dict[str, int],
        *,
        is_workspace: bool = False,
    ) -> None:
        self._queued = queued_by_project
        self._is_workspace = is_workspace
        self.closed = False

    def list_tasks(self, *, project: str | None = None, work_status: str = ""):
        if work_status != "queued":
            return []
        if project is None:
            # Per-project DB pass — return all rows the fake holds.
            total = sum(self._queued.values())
            return [object() for _ in range(total)]
        # Workspace DB pass — return rows filtered to the named project.
        # By default workspace doubles hold zero rows so per-project counts
        # don't double-count.
        if not self._is_workspace:
            return []
        n = self._queued.get(project, 0)
        return [object() for _ in range(n)]

    def close(self) -> None:
        self.closed = True


def _build_services(
    *,
    windows: list[_FakeWindow],
    known_projects: tuple[_FakeProject, ...],
    store: _FakeStore | None = None,
    workspace_queued: dict[str, int] | None = None,
) -> _RuntimeServices:
    return _RuntimeServices(
        session_service=_FakeSessionService(windows),
        state_store=store,
        msg_store=store,
        work_service=_FakeWorkService(workspace_queued or {}),
        project_root=Path("/tmp"),
        known_projects=known_projects,
    )


# ---------------------------------------------------------------------------
# Helpers — naming-convention coverage
# ---------------------------------------------------------------------------


def test_project_has_worker_session_underscore_form() -> None:
    """``worker_<project>`` (the shipping default) counts."""
    assert _project_has_worker_session("polly_remote", {"worker_polly_remote"})


def test_project_has_worker_session_hyphen_form() -> None:
    """``worker-<project>`` (legacy) also counts."""
    assert _project_has_worker_session("polly_remote", {"worker-polly_remote"})


def test_project_has_worker_session_per_task_form() -> None:
    """A per-task ``task-<project>-<N>`` window also closes the gap."""
    assert _project_has_worker_session(
        "polly_remote", {"task-polly_remote-5"},
    )


def test_project_has_worker_session_unrelated_window_ignored() -> None:
    """Sibling-project worker windows must not close the gap."""
    assert not _project_has_worker_session(
        "polly_remote",
        {"worker-bikepath", "worker_booktalk", "task-other-3"},
    )


def test_project_has_worker_session_empty_window_set() -> None:
    assert not _project_has_worker_session("polly_remote", set())


def test_session_name_is_namespaced() -> None:
    assert (
        _worker_session_gap_session_name("polly_remote")
        == "worker_session_gap-polly_remote"
    )


# ---------------------------------------------------------------------------
# Sweep pass — emit / clear behaviour
# ---------------------------------------------------------------------------


def _patch_project_db(
    monkeypatch: pytest.MonkeyPatch,
    queued_by_project: dict[str, int],
) -> None:
    """Stub ``_open_project_work_service`` to return a per-project fake."""

    def _opener(project: _FakeProject, _services: Any) -> _FakeWorkService:
        return _FakeWorkService({project.key: queued_by_project.get(project.key, 0)})

    monkeypatch.setattr(sweep_mod, "_open_project_work_service", _opener)


def test_emits_alert_when_queued_tasks_and_no_worker_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug from #1054: queued tasks + no worker session → warn alert."""
    project = _FakeProject(key="polly_remote")
    store = _FakeStore()
    services = _build_services(
        windows=[
            _FakeWindow("worker-bikepath"),
            _FakeWindow("worker_booktalk"),
            _FakeWindow("pm-heartbeat"),
        ],
        known_projects=(project,),
        store=store,
    )
    _patch_project_db(monkeypatch, {"polly_remote": 4})

    summary = _sweep_worker_session_gaps(services)

    assert summary == {"emitted": 1, "cleared": 0}
    assert len(store.upserts) == 1
    session_name, alert_type, severity, message = store.upserts[0]
    assert session_name == "worker_session_gap-polly_remote"
    assert alert_type == WORKER_SESSION_GAP_ALERT_TYPE
    assert severity == "warn"
    assert "polly_remote" in message
    assert "4 queued tasks" in message
    # #1059 — hint must point at per-task claim flow, not the deprecated
    # ``pm worker-start <project>`` (which defaults to ``--role worker``).
    assert "pm task next -p polly_remote" in message
    assert "pm task claim" in message
    assert "pm worker-start" not in message
    assert "pm task hold" in message


def test_no_alert_when_worker_session_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker session present → no alert (and existing alert auto-clears)."""
    project = _FakeProject(key="polly_remote")
    store = _FakeStore()
    services = _build_services(
        windows=[_FakeWindow("worker_polly_remote")],
        known_projects=(project,),
        store=store,
    )
    _patch_project_db(monkeypatch, {"polly_remote": 4})

    summary = _sweep_worker_session_gaps(services)

    assert summary == {"emitted": 0, "cleared": 1}
    assert store.upserts == []
    assert store.clears == [
        ("worker_session_gap-polly_remote", WORKER_SESSION_GAP_ALERT_TYPE),
    ]


def test_no_alert_when_no_queued_tasks_and_no_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty queue → no alert regardless of worker presence (and clears stale)."""
    project = _FakeProject(key="polly_remote")
    store = _FakeStore()
    services = _build_services(
        windows=[_FakeWindow("worker-bikepath")],
        known_projects=(project,),
        store=store,
    )
    _patch_project_db(monkeypatch, {"polly_remote": 0})

    summary = _sweep_worker_session_gaps(services)

    assert summary == {"emitted": 0, "cleared": 1}
    assert store.upserts == []
    assert store.clears == [
        ("worker_session_gap-polly_remote", WORKER_SESSION_GAP_ALERT_TYPE),
    ]


def test_no_alert_when_no_queued_tasks_with_worker_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quiet, healthy state — no emission, idempotent clear."""
    project = _FakeProject(key="polly_remote")
    store = _FakeStore()
    services = _build_services(
        windows=[_FakeWindow("worker_polly_remote")],
        known_projects=(project,),
        store=store,
    )
    _patch_project_db(monkeypatch, {"polly_remote": 0})

    summary = _sweep_worker_session_gaps(services)

    assert summary == {"emitted": 0, "cleared": 1}
    assert store.upserts == []


def test_multiple_projects_independent_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each project's gap is evaluated independently."""
    p1 = _FakeProject(key="polly_remote")
    p2 = _FakeProject(key="bikepath")
    p3 = _FakeProject(key="booktalk")
    store = _FakeStore()
    services = _build_services(
        windows=[
            _FakeWindow("worker-bikepath"),       # bikepath: ok
            _FakeWindow("worker_booktalk"),       # booktalk: ok (queue empty anyway)
            # polly_remote: missing
        ],
        known_projects=(p1, p2, p3),
        store=store,
    )
    _patch_project_db(monkeypatch, {
        "polly_remote": 4,
        "bikepath": 2,
        "booktalk": 0,
    })

    summary = _sweep_worker_session_gaps(services)

    assert summary["emitted"] == 1
    assert summary["cleared"] == 2
    upserted_projects = {row[0] for row in store.upserts}
    assert upserted_projects == {"worker_session_gap-polly_remote"}


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
        work_service=_FakeWorkService({}),
        project_root=Path("/tmp"),
        known_projects=(project,),
    )
    _patch_project_db(monkeypatch, {"polly_remote": 4})

    summary = _sweep_worker_session_gaps(services)

    assert summary == {"emitted": 0, "cleared": 0}
    assert store.upserts == []
    assert store.clears == []


def test_dead_pane_window_does_not_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead-pane worker window is treated as no worker."""
    project = _FakeProject(key="polly_remote")
    store = _FakeStore()
    services = _build_services(
        windows=[_FakeWindow("worker_polly_remote", pane_dead=True)],
        known_projects=(project,),
        store=store,
    )
    _patch_project_db(monkeypatch, {"polly_remote": 1})

    summary = _sweep_worker_session_gaps(services)

    assert summary == {"emitted": 1, "cleared": 0}
    assert store.upserts and store.upserts[0][1] == WORKER_SESSION_GAP_ALERT_TYPE


def test_no_known_projects_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty project registry → empty summary, no store interaction."""
    store = _FakeStore()
    services = _build_services(
        windows=[],
        known_projects=(),
        store=store,
    )

    summary = _sweep_worker_session_gaps(services)

    assert summary == {"emitted": 0, "cleared": 0}
    assert store.upserts == []
    assert store.clears == []


def test_liveness_gate_skips_dormant_project_gap_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2530: queued tasks in dormant projects do not refresh gap alerts."""
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
        return _FakeWorkService({project.key: 3})

    monkeypatch.setattr(sweep_mod, "_open_project_work_service", _opener)

    summary = _sweep_worker_session_gaps(
        services,
        recent_real_work_projects=frozenset({"polly_remote"}),
    )

    assert opened == ["polly_remote"]
    assert summary == {"emitted": 1, "cleared": 0}
    assert [row[0] for row in store.upserts] == [
        "worker_session_gap-polly_remote",
    ]
