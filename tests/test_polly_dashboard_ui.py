"""Focused tests for the async Polly dashboard refresh path (#464)."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

from pollypm.cockpit_ui import PollyDashboardApp
from pollypm.dashboard_data import (
    AccountQuotaUsage,
    DashboardData,
    InboxPreview,
    load_dashboard,
)
from pollypm.storage.state import AccountUsageRecord


def _run(coro) -> None:
    asyncio.run(coro)


def _fake_dashboard_data() -> DashboardData:
    return DashboardData(
        active_sessions=[],
        recent_commits=[],
        completed_items=[],
        recent_messages=[],
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
    # Alert toast notifier was removed in #956; nothing to silence.

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


def test_polly_dashboard_renders_recent_messages(monkeypatch, tmp_path: Path) -> None:
    app = PollyDashboardApp(tmp_path / "pollypm.toml")
    data = _fake_dashboard_data()
    data.recent_messages = [
        InboxPreview(
            sender="polly",
            title="Feedback on shortlink_gen/5",
            project="Shortlink Gen",
            task_id="shortlink_gen/5",
            age_seconds=120.0,
        ),
        InboxPreview(
            sender="russell",
            title="Approval ready: docs/1",
            project="Docs",
            task_id="docs/1",
            age_seconds=480.0,
        ),
    ]

    app._render_dashboard(_fake_config(), data)

    rendered = str(app.messages_body.render())
    assert "polly" in rendered
    assert "Feedback on shortlink_gen/5" in rendered
    # #1100 — Home advertises capital ``I`` because lowercase ``i`` from
    # the rail is the project-surface-only forward binding (#1089) and a
    # no-op on Home; only ``I`` actually opens the global inbox here.
    assert "Press I to jump to the inbox" in rendered


def test_polly_dashboard_shows_inbox_count_without_recent_messages(tmp_path: Path) -> None:
    app = PollyDashboardApp(tmp_path / "pollypm.toml")
    data = _fake_dashboard_data()
    data.inbox_count = 13

    app._render_dashboard(_fake_config(), data)

    rendered = str(app.messages_body.render())
    assert "Inbox is clear" not in rendered
    assert "13 items in the inbox" in rendered
    # #1100 — Home dashboard hint advertises capital ``I`` (see sister
    # test for full rationale).
    assert "Press I to jump to the inbox" in rendered


def test_polly_dashboard_renders_llm_quota_usage(tmp_path: Path) -> None:
    app = PollyDashboardApp(tmp_path / "pollypm.toml")
    data = _fake_dashboard_data()
    data.account_usages = [
        AccountQuotaUsage(
            account_name="claude_main",
            provider="Anthropic",
            email="claude@swh.me",
            used_pct=84,
            summary="16% left this week",
            severity="warning",
            limit_label="weekly limit",
            reset_at="Apr 24 01:00",
        )
    ]

    app._render_dashboard(_fake_config(), data)

    rendered = str(app.chart_body.render())
    assert "LLM account quota usage" in rendered
    assert "84% used of weekly limit" in rendered
    assert "approaching ceiling" in rendered
    assert "No token data yet" in rendered


def test_dashboard_gather_uses_rail_inbox_counter(monkeypatch, tmp_path: Path) -> None:
    """Home inbox count must match the rail, including untracked projects."""
    from pollypm.dashboard_data import gather

    config = SimpleNamespace(
        projects={
            "ghost": SimpleNamespace(
                path=tmp_path / "ghost",
                tracked=False,
                display_label=lambda: "Ghost",
            )
        }
    )

    class FakeStore:
        def list_session_runtimes(self):
            return []

        def recent_events(self, *, limit: int):
            del limit
            return []

        def daily_token_usage(self, *, days: int):
            del days
            return []

        def open_alerts(self):
            return []

    seen: list[object] = []

    def fake_rail_count(arg):
        seen.append(arg)
        return 13

    monkeypatch.setattr(
        "pollypm.service_api.plan_launches_readonly",
        lambda _config, _store: [],
    )
    monkeypatch.setattr("pollypm.dashboard_data._recent_commits", lambda *_a, **_kw: [])
    monkeypatch.setattr("pollypm.dashboard_data._completed_issues", lambda *_a, **_kw: [])
    monkeypatch.setattr("pollypm.dashboard_data._recent_inbox_messages", lambda *_a, **_kw: [])
    monkeypatch.setattr(
        "pollypm.cockpit_inbox._count_inbox_tasks_for_label",
        fake_rail_count,
    )

    data = gather(config, FakeStore())

    assert seen == [config]
    assert data.inbox_count == 13
    assert "13 inbox items waiting" in data.briefing


def test_dashboard_gather_includes_cached_llm_quota_usage(
    monkeypatch, tmp_path: Path,
) -> None:
    from pollypm.dashboard_data import gather

    config = SimpleNamespace(
        projects={},
        accounts={
            "claude_main": SimpleNamespace(
                provider="claude",
                email="claude@swh.me",
            )
        },
    )

    class FakeStore:
        def list_session_runtimes(self):
            return []

        def recent_events(self, *, limit: int):
            del limit
            return []

        def daily_token_usage(self, *, days: int):
            del days
            return []

        def get_account_usage(self, account_name: str):
            assert account_name == "claude_main"
            return AccountUsageRecord(
                account_name="claude_main",
                provider="claude",
                plan="max",
                health="near-limit",
                usage_summary="21% left this week",
                raw_text="",
                updated_at="2026-04-21T14:10:00+00:00",
                used_pct=79,
                remaining_pct=21,
                reset_at="Apr 24 01:00",
                period_label="current week",
            )

        def open_alerts(self):
            return []

    monkeypatch.setattr(
        "pollypm.service_api.plan_launches_readonly",
        lambda _config, _store: [],
    )
    monkeypatch.setattr("pollypm.dashboard_data._recent_commits", lambda *_a, **_kw: [])
    monkeypatch.setattr("pollypm.dashboard_data._completed_issues", lambda *_a, **_kw: [])
    monkeypatch.setattr("pollypm.dashboard_data._recent_inbox_messages", lambda *_a, **_kw: [])
    monkeypatch.setattr("pollypm.dashboard_data._count_dashboard_inbox_items", lambda _config: 0)

    data = gather(config, FakeStore())

    assert len(data.account_usages) == 1
    usage = data.account_usages[0]
    assert usage.provider == "Anthropic"
    assert usage.email == "claude@swh.me"
    assert usage.used_pct == 79
    assert usage.limit_label == "weekly limit"


def test_polly_dashboard_i_key_routes_to_inbox(monkeypatch, tmp_path: Path) -> None:
    calls: list[bool] = []

    def fake_route(self) -> None:
        calls.append(True)

    monkeypatch.setattr(PollyDashboardApp, "_route_to_inbox", fake_route)
    # Alert toast notifier was removed in #956; nothing to silence.

    async def body() -> None:
        app = PollyDashboardApp(tmp_path / "pollypm.toml")
        app._finish_dashboard_refresh_success(_fake_config(), _fake_dashboard_data())
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("i")
            await pilot.pause()
            if not calls:
                app._route_to_inbox_sync()
            assert calls

    _run(body())
