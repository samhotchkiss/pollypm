"""Focused tests for the async Polly dashboard refresh path (#464)."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

from pollypm.cockpit_ui import PollyDashboardApp
from pollypm.dashboard_data import DashboardData, load_dashboard


def _run(coro) -> None:
    asyncio.run(coro)


def _fake_dashboard_data() -> DashboardData:
    return DashboardData(
        active_sessions=[],
        recent_commits=[],
        completed_items=[],
        daily_tokens=[],
        today_tokens=0,
        total_tokens=0,
        sweep_count_24h=0,
        message_count_24h=0,
        recovery_count_24h=0,
        inbox_count=0,
        alert_count=0,
    )


def _fake_config() -> SimpleNamespace:
    return SimpleNamespace(
        projects={"demo": object()},
        sessions={"operator": object()},
    )


def test_load_dashboard_closes_store(monkeypatch, tmp_path: Path) -> None:
    closed: list[bool] = []
    sentinel_config = SimpleNamespace(project=SimpleNamespace(state_db=tmp_path / "state.db"))
    sentinel_data = _fake_dashboard_data()

    class FakeStore:
        def __init__(self, db_path: Path) -> None:
            self.db_path = db_path

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr("pollypm.dashboard_data.load_config", lambda path: sentinel_config)
    monkeypatch.setattr("pollypm.dashboard_data.StateStore", FakeStore)
    monkeypatch.setattr("pollypm.dashboard_data.gather", lambda config, store: sentinel_data)

    config, data = load_dashboard(tmp_path / "pollypm.toml")

    assert config is sentinel_config
    assert data is sentinel_data
    assert closed == [True]


def test_polly_dashboard_refresh_runs_in_worker_thread(monkeypatch, tmp_path: Path) -> None:
    thread_ids: list[int] = []
    main_thread_id = threading.main_thread().ident

    def fake_load_dashboard(config_path: Path):
        thread_ids.append(threading.get_ident())
        return _fake_config(), _fake_dashboard_data()

    monkeypatch.setattr("pollypm.dashboard_data.load_dashboard", fake_load_dashboard)
    monkeypatch.setattr("pollypm.cockpit_ui._setup_alert_notifier", lambda *args, **kwargs: None)

    async def body() -> None:
        app = PollyDashboardApp(tmp_path / "pollypm.toml")
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.pause()
            assert app._dashboard_data is not None
            assert thread_ids
            assert all(tid != main_thread_id for tid in thread_ids)

    _run(body())


def test_polly_dashboard_refresh_error_keeps_cached_snapshot(monkeypatch, tmp_path: Path) -> None:
    app = PollyDashboardApp(tmp_path / "pollypm.toml")
    config = _fake_config()
    data = _fake_dashboard_data()
    rendered: list[tuple[object, object]] = []

    app._finish_dashboard_refresh_success(config, data)
    monkeypatch.setattr(
        app,
        "_render_dashboard",
        lambda cfg, snapshot: rendered.append((cfg, snapshot)),
    )

    app._finish_dashboard_refresh_error("boom")

    assert app._dashboard_config is config
    assert app._dashboard_data is data
    assert app._refresh_error == "boom"
    assert rendered == [(config, data)]
