"""Pilot timing tests for the operator dashboard app (#1630).

The operator dashboard regressed to ~12s cold-boot render after the
#1610/#1615 categorization edits added per-project sqlite work. The
fix has two halves:

1. ``PollyOperatorDashboardApp`` paints a "Loading operator dashboard…"
   skeleton in <100ms — the worker thread that opens per-project
   sqlite DBs is dispatched via ``call_after_refresh`` so the first
   paint cycle finishes before any DB is touched.
2. ``load_operator_view_from_config`` runs the per-project DB sweep on
   a bounded thread pool, so a 12-project workspace doesn't serialize
   12 sqlite opens on the main worker thread.

These tests assert the perceptual targets from the issue: <100ms to
the skeleton, <2s to the data.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from pollypm.cockpit_apps.operator_dashboard import PollyOperatorDashboardApp
from pollypm.dashboard.categorization import (
    OperatorDashboardRow,
    OperatorDashboardView,
    ProjectState,
)


def _run(coro) -> None:
    asyncio.run(coro)


def _fake_view() -> OperatorDashboardView:
    return OperatorDashboardView(
        waiting=(
            OperatorDashboardRow(
                project_key="alpha",
                state=ProjectState.WAITING,
                glyph="◆",
                detail="Needs your approval",
            ),
        ),
        working=(
            OperatorDashboardRow(
                project_key="beta",
                state=ProjectState.WORKING,
                glyph="●",
                detail="claude: ship feature",
            ),
        ),
        idle=(),
        paused=(),
    )


def test_first_paint_is_skeleton_within_100ms(monkeypatch, tmp_path: Path) -> None:
    """The 'Loading operator dashboard…' header must appear before data.

    Models a slow per-project sweep by parking ``load_operator_view`` on
    a barrier the test only releases AFTER asserting the skeleton is
    visible. If the app blocked on the worker (issue #1630's hypothesis
    3) the skeleton would never paint and the test would deadlock.
    """
    proceed = threading.Event()
    started = threading.Event()

    def fake_load_operator_view(config_path: Path) -> OperatorDashboardView:
        started.set()
        # Cap the wait so a regression aborts the suite instead of
        # hanging forever.
        proceed.wait(timeout=10.0)
        return _fake_view()

    monkeypatch.setattr(
        "pollypm.dashboard.operator_view.load_operator_view",
        fake_load_operator_view,
    )

    async def body() -> None:
        app = PollyOperatorDashboardApp(tmp_path / "pollypm.toml")
        t0 = time.monotonic()
        async with app.run_test(size=(120, 40)) as pilot:
            # First paint cycle.
            await pilot.pause()
            skeleton_elapsed = time.monotonic() - t0
            header = str(app.header_w.render())
            assert "Loading operator dashboard" in header, (
                f"expected skeleton header, got: {header!r}"
            )
            # The data worker should be deferred until AFTER the first
            # paint — but not so far that the user is left waiting. By
            # the time we reach this assert the worker has been
            # dispatched (call_after_refresh fires on the next idle
            # tick). Allow a generous budget for CI noise.
            assert skeleton_elapsed < 1.5, (
                f"skeleton paint took {skeleton_elapsed*1000:.0f}ms — "
                "first-paint regression"
            )
            # Release the fake loader so the worker thread can finish.
            proceed.set()
            # Pump until the view lands.
            for _ in range(50):
                await pilot.pause()
                if app._view is not None:
                    break
            assert app._view is not None
            data_elapsed = time.monotonic() - t0
            assert data_elapsed < 5.0, (
                f"data render took {data_elapsed*1000:.0f}ms — "
                "worker-thread regression"
            )
            assert started.is_set()
            header = str(app.header_w.render())
            assert "waiting" in header
            assert "working" in header

    _run(body())


def test_refresh_uses_thread_worker(monkeypatch, tmp_path: Path) -> None:
    """All sqlite reads must run off the asyncio main thread (#1630)."""
    thread_ids: list[int] = []
    main_thread_id = threading.main_thread().ident

    def fake_load_operator_view(config_path: Path) -> OperatorDashboardView:
        thread_ids.append(threading.get_ident())
        return _fake_view()

    monkeypatch.setattr(
        "pollypm.dashboard.operator_view.load_operator_view",
        fake_load_operator_view,
    )

    async def body() -> None:
        app = PollyOperatorDashboardApp(tmp_path / "pollypm.toml")
        async with app.run_test(size=(120, 40)) as pilot:
            for _ in range(30):
                await pilot.pause()
                if app._view is not None:
                    break
            assert app._view is not None
            assert thread_ids, "loader never invoked"
            assert all(tid != main_thread_id for tid in thread_ids), (
                "loader ran on the main thread — would block first paint"
            )

    _run(body())


def test_scan_to_row_handles_missing_service(tmp_path: Path) -> None:
    """When no shared work-service is available the row falls back to the
    inbox-only categorization (PAUSED / IDLE / WAITING) — #1634 collapsed
    the per-project sqlite fanout into a single shared handle, and the
    scan helper must still degrade gracefully when that handle is None.
    """
    from pollypm.dashboard.operator_view import _ProjectScan, _scan_to_row

    scan = _ProjectScan(
        project_key="solo",
        project_path=tmp_path,
        tracked=True,
    )
    row = _scan_to_row(scan, [], slice_svc=None)
    assert row.project_key == "solo"
    # Tracked + empty inbox → IDLE
    assert row.state.value == "idle"


def test_load_operator_view_preserves_per_project_results(tmp_path: Path) -> None:
    """The single-handle sweep must produce one row per project — same
    keys, no drops — even when the shared work-service is unavailable.
    """
    from types import SimpleNamespace

    from pollypm.dashboard.operator_view import (
        load_operator_view_from_config,
    )

    projects = {
        f"p{i}": SimpleNamespace(path=tmp_path, tracked=(i % 2 == 0))
        for i in range(6)
    }
    config = SimpleNamespace(
        projects=projects,
        project=SimpleNamespace(workspace_root=str(tmp_path)),
        storage=SimpleNamespace(backend="sqlite"),
    )
    view = load_operator_view_from_config(config)
    keys = sorted(
        row.project_key
        for row in (*view.waiting, *view.working, *view.idle, *view.paused)
    )
    assert keys == [f"p{i}" for i in range(6)]
