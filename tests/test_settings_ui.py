"""Textual UI tests for the rebuilt :class:`PollySettingsPaneApp`.

The settings screen is the cockpit panel Sam lands on when he clicks
Settings in the left rail. These tests drive the rebuilt sections-based
screen via ``Pilot`` so we can assert every section renders, keyboard
nav between sections works, the ``/`` search filter narrows visible
rows, ``R`` refreshes live data, disabled projects dim, and the mount
path stays under a reasonable wall-clock budget for a synthetic
20-project / 5-account workspace.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from textual.widgets import Button

from pollypm.models import ProviderKind


def _fake_status(
    key: str,
    *,
    provider: ProviderKind = ProviderKind.CLAUDE,
    email: str | None = None,
    health: str = "healthy",
    usage: str = "80% left",
    plan: str = "max",
    logged_in: bool = True,
    usage_updated_at: str | None = "2026-04-21T16:45:00+00:00",
    used_pct: int | None = 20,
    remaining_pct: int | None = 80,
    reset_at: str | None = "Apr 24 at 1am",
    period_label: str | None = "current week",
) -> object:
    class _Status:
        pass

    s = _Status()
    s.key = key
    s.email = email or f"{key}@example.com"
    s.provider = provider
    s.logged_in = logged_in
    s.plan = plan
    s.health = health
    s.usage_summary = usage
    s.usage_raw_text = "Plan: max\nUsage: 20%"
    s.reason = ""
    s.available_at = None
    s.access_expires_at = None
    s.usage_updated_at = usage_updated_at
    s.isolation_status = "host-profile"
    s.isolation_summary = ""
    s.isolation_recommendation = ""
    s.auth_storage = "file"
    s.profile_root = None
    s.used_pct = used_pct
    s.remaining_pct = remaining_pct
    s.reset_at = reset_at
    s.period_label = period_label
    s.home = Path("/tmp") / key
    return s


class _FakeUsage:
    def __init__(
        self,
        *,
        used_pct: int | None,
        remaining_pct: int | None,
        usage_summary: str,
        updated_at: str,
    ) -> None:
        self.used_pct = used_pct
        self.remaining_pct = remaining_pct
        self.usage_summary = usage_summary
        self.updated_at = updated_at


class _FakeProject:
    def __init__(self, key: str, *, tracked: bool = True, name: str | None = None) -> None:
        self.key = key
        self.name = name or key.replace("_", " ").title()
        self.path = Path("/tmp") / "projects" / key
        self.persona_name = None
        self.tracked = tracked

    def display_label(self) -> str:
        return self.name or self.key


class _FakeAccount:
    def __init__(self, key: str, provider: ProviderKind, email: str | None = None) -> None:
        self.provider = provider
        self.home = Path("/tmp") / key
        self.email = email or f"{key}@example.com"


class _FakePollyPM:
    def __init__(self, controller: str = "claude_demo") -> None:
        self.controller_account = controller
        self.failover_accounts: list[str] = []
        self.failover_enabled = False
        self.open_permissions_by_default = True
        self.lease_timeout_minutes = 30
        self.heartbeat_backend = "local"
        self.scheduler_backend = "inline"
        self.timezone = ""


class _FakeProjectSettings:
    def __init__(self, tmp_path: Path) -> None:
        self.workspace_root = tmp_path
        self.state_db = tmp_path / ".pollypm" / "state.db"
        self.logs_dir = tmp_path / ".pollypm" / "logs"


class _FakePlanner:
    auto_on_project_created = True
    enforce_plan = True
    plan_dir = "docs/plan"


class _FakePlugins:
    disabled = ()


class _FakeConfig:
    def __init__(
        self,
        tmp_path: Path,
        *,
        projects: dict[str, _FakeProject] | None = None,
        controller: str = "claude_demo",
    ) -> None:
        self.pollypm = _FakePollyPM(controller)
        self.project = _FakeProjectSettings(tmp_path)
        self.projects = projects or {}
        self.planner = _FakePlanner()
        self.plugins = _FakePlugins()
        self.accounts: dict[str, object] = {}


class _FakeService:
    def __init__(self, statuses: list, config: object) -> None:
        self._statuses = statuses
        self._config = config
        self.permissions_calls: list[bool] = []
        self.tracked_calls: list[tuple[str, bool]] = []
        self.controller_calls: list[str] = []
        self.failover_calls: list[str] = []
        self.add_calls: list[ProviderKind] = []
        self.remove_calls: list[str] = []
        self.refresh_usage_calls: list[str] = []
        self.cached_calls = 0
        self.live_calls = 0

    def list_account_statuses(self):
        self.live_calls += 1
        return list(self._statuses)

    def list_cached_account_statuses(self):
        self.cached_calls += 1
        return list(self._statuses)

    def set_open_permissions_default(self, enabled: bool) -> bool:
        self.permissions_calls.append(enabled)
        self._config.pollypm.open_permissions_by_default = enabled
        return enabled

    def set_project_tracked(self, key: str, enabled: bool) -> None:
        self.tracked_calls.append((key, enabled))
        self._config.projects[key].tracked = enabled

    def set_controller_account(self, key: str) -> None:
        self.controller_calls.append(key)
        self._config.pollypm.controller_account = key

    def toggle_failover_account(self, key: str) -> tuple[str, bool]:
        self.failover_calls.append(key)
        accounts = list(self._config.pollypm.failover_accounts)
        if key in accounts:
            accounts.remove(key)
            enabled = False
        else:
            accounts.append(key)
            enabled = True
        self._config.pollypm.failover_accounts = accounts
        return key, enabled

    def add_account(self, provider: ProviderKind) -> tuple[str, str]:
        self.add_calls.append(provider)
        key = f"{provider.value}_new"
        email = f"{key}@example.com"
        self._statuses.append(
            _fake_status(
                key,
                provider=provider,
                email=email,
                usage="93% left this week",
                used_pct=7,
                remaining_pct=93,
                reset_at="Apr 28 at 1am",
            )
        )
        self._config.accounts[key] = _FakeAccount(key, provider, email=email)
        return key, email

    def refresh_account_usage(self, key: str):
        self.refresh_usage_calls.append(key)
        for status in self._statuses:
            if status.key == key:
                status.usage_summary = "92% left this week"
                status.used_pct = 8
                status.remaining_pct = 92
                status.reset_at = "Apr 28 at 1am"
                status.period_label = "current week"
                status.usage_updated_at = "2026-04-21T17:00:00+00:00"
                return status
        raise KeyError(key)

    def remove_account(self, identifier: str, *, delete_home: bool = False) -> tuple[str, str]:
        del delete_home
        self.remove_calls.append(identifier)
        self._statuses = [s for s in self._statuses if getattr(s, "key", "") != identifier]
        self._config.accounts.pop(identifier, None)
        return identifier, "removed"


def _collect_project_rows(config, *, format_relative_age) -> list[dict]:
    del format_relative_age
    rows: list[dict] = []
    for key, project in config.projects.items():
        rows.append(
            {
                "key": key,
                "name": project.name,
                "persona": "Polly",
                "path": str(project.path),
                "path_exists": False,
                "tracked": project.tracked,
                "task_total": 3 if key == "alpha" else 1,
                "task_total_label": 3 if key == "alpha" else 1,
                "last_activity": "5m ago" if key == "alpha" else "1h ago",
                "rationale": "Tracked projects stay visible in the cockpit and feed task counts.",
            }
        )
    return rows


@pytest.fixture
def settings_env(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "pollypm.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("# stubbed — tests monkeypatch load_config\n")

    projects = {
        "alpha": _FakeProject("alpha", tracked=True),
        "bravo": _FakeProject("bravo", tracked=True, name="Bravo Build"),
        "charlie": _FakeProject("charlie", tracked=False, name="Charlie Paused"),
    }
    statuses = [
        _fake_status("claude_demo", email="demo@example.com"),
        _fake_status(
            "codex_demo",
            provider=ProviderKind.CODEX,
            email="codex@example.com",
            health="capacity-low",
            usage="20% left",
        ),
        _fake_status(
            "claude_stale",
            email="stale@example.com",
            health="auth-broken",
            logged_in=False,
            usage="offline",
        ),
    ]
    fake_config = _FakeConfig(tmp_path, projects=projects)
    fake_config.accounts = {
        "claude_demo": _FakeAccount("claude_demo", ProviderKind.CLAUDE, "demo@example.com"),
        "codex_demo": _FakeAccount("codex_demo", ProviderKind.CODEX, "codex@example.com"),
        "claude_stale": _FakeAccount("claude_stale", ProviderKind.CLAUDE, "stale@example.com"),
    }
    recent_tasks = {
        "claude_demo": [
            {
                "task_id": "alpha/7",
                "project": "alpha",
                "title": "Tune settings UI",
                "status": "in_progress",
                "updated_at": "2026-04-21T10:15:00",
            },
            {
                "task_id": "bravo/2",
                "project": "bravo",
                "title": "Fix undo hint",
                "status": "queued",
                "updated_at": "2026-04-21T09:30:00",
            },
        ],
        "codex_demo": [
            {
                "task_id": "charlie/4",
                "project": "charlie",
                "title": "Review budget indicator",
                "status": "done",
                "updated_at": "2026-04-21T11:00:00",
            }
        ],
    }
    cached_usage = {
        "claude_demo": _FakeUsage(
            used_pct=19,
            remaining_pct=81,
            usage_summary="81% left this week",
            updated_at="2026-04-21 09:00:00",
        ),
        "codex_demo": _FakeUsage(
            used_pct=78,
            remaining_pct=22,
            usage_summary="22% left this week",
            updated_at="2026-04-21 09:30:00",
        ),
    }
    monkeypatch.setattr("pollypm.cockpit_ui.load_config", lambda _p: fake_config)
    monkeypatch.setattr("pollypm.cockpit_ui.load_cached_account_usage", lambda _p: cached_usage)
    monkeypatch.setattr(
        "pollypm.cockpit_ui._collect_recent_tasks_by_account",
        lambda _config, _statuses, max_per_account=3: recent_tasks,
    )
    monkeypatch.setattr(
        "pollypm.cockpit_ui.collect_settings_projects",
        _collect_project_rows,
    )
    monkeypatch.setattr(
        "pollypm.cockpit_settings_history.Path.home",
        lambda: tmp_path / "home",
    )
    service = _FakeService(statuses, fake_config)

    from pollypm.cockpit_ui import PollySettingsPaneApp

    app = PollySettingsPaneApp(config_path)
    app.service = service  # type: ignore[assignment]
    return {
        "config_path": config_path,
        "app": app,
        "service": service,
        "statuses": statuses,
        "projects": projects,
        "config": fake_config,
    }


def _run(coro) -> None:
    asyncio.run(coro)


def test_settings_mounts_all_sections(settings_env) -> None:
    app = settings_env["app"]

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            assert app.data is not None
            topbar = str(app.topbar.render())
            assert "claude_demo" in topbar
            assert "accounts" in topbar
            assert set(app._nav_widgets.keys()) == {
                "accounts",
                "projects",
                "heartbeat",
                "plugins",
                "planner",
                "inbox",
                "about",
            }
            assert app.accounts.display
            assert app.accounts.row_count == 3

    _run(body())


def test_accounts_section_lists_configured_accounts(settings_env) -> None:
    app = settings_env["app"]

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            assert app.accounts.row_count == 3
            detail_text = str(app.detail.render())
            assert "claude_demo" in detail_text
            assert "demo@example.com" in detail_text
            assert "healthy" in detail_text
            assert "Remaining" in detail_text
            assert "Sampled" in detail_text
            keys = [a["key"] for a in app.data.accounts]
            assert keys == ["claude_demo", "codex_demo", "claude_stale"]
            assert "Budget" in detail_text

    _run(body())


def test_accounts_preview_includes_budget_and_recent_tasks(settings_env) -> None:
    app = settings_env["app"]

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            preview = str(app.preview.render())
            assert "Diff preview" in preview
            assert "Budget indicator" in preview
            assert "Tune settings UI" in preview
            assert "Fix undo hint" in preview

    _run(body())


def test_accounts_section_exposes_account_action_buttons(settings_env) -> None:
    app = settings_env["app"]

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            assert app.query_one("#settings-account-add-claude", Button) is not None
            assert app.query_one("#settings-account-add-codex", Button) is not None
            assert app.query_one("#settings-account-refresh-usage", Button) is not None
            assert app.query_one("#settings-account-remove", Button) is not None

    _run(body())


def test_projects_section_lists_registered_projects_and_preview(settings_env) -> None:
    app = settings_env["app"]

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press("tab")
            await pilot.pause()
            assert app._active_section == "projects"
            assert app.projects_table.display
            keys = [p["key"] for p in app.data.projects]
            assert set(keys) == {"alpha", "bravo", "charlie"}
            assert app.projects_table.row_count == 3
            preview = str(app.preview.render())
            assert "tracked" in preview
            assert "u undo" in preview.lower()

    _run(body())


def test_search_filters_accounts(settings_env) -> None:
    app = settings_env["app"]

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            assert app.accounts.row_count == 3
            app._search_query = "codex"
            app._render_section("accounts")
            await pilot.pause()
            assert app.accounts.row_count == 1

            app.search_input.add_class("-active")
            app.action_back_or_cancel()
            await pilot.pause()
            assert app.accounts.row_count == 3
            assert app._search_query == ""

    _run(body())


def test_r_refresh_reloads_account_data(settings_env) -> None:
    app = settings_env["app"]
    service = settings_env["service"]

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            service._statuses.append(
                _fake_status("claude_new", email="new@example.com"),
            )
            await pilot.press("r")
            await pilot.pause()
            assert app.accounts.row_count == 4
            assert service.cached_calls >= 2

    _run(body())


def test_refresh_usage_action_updates_selected_account(settings_env) -> None:
    app = settings_env["app"]
    service = settings_env["service"]

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app.action_refresh_selected_account_usage()
            await pilot.pause()
            assert service.refresh_usage_calls == ["claude_demo"]
            detail_text = str(app.detail.render())
            assert "92% left this week" in detail_text
            assert "2026-04-21T17:00:00+00:00" in detail_text

    _run(body())


def test_b_key_toggles_permissions_and_can_undo(settings_env) -> None:
    app = settings_env["app"]
    service = settings_env["service"]

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press("b")
            await pilot.pause()
            assert service.permissions_calls == [False]
            assert "undo" in str(app.preview.render()).lower()
            await pilot.press("u")
            await pilot.pause()
            assert service.permissions_calls == [False, True]
            assert service._config.pollypm.open_permissions_by_default is True

    _run(body())


def test_add_and_remove_account_actions(settings_env, monkeypatch) -> None:
    app = settings_env["app"]
    service = settings_env["service"]

    def _auto_confirm(_screen, callback):
        callback(True)

    monkeypatch.setattr(app, "push_screen", _auto_confirm)

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert service.add_calls == [ProviderKind.CLAUDE]
            assert "claude_new" in [a["key"] for a in app.data.accounts]
            app._selected_account_key = "claude_new"
            app._render_section("accounts")
            await pilot.pause()
            await pilot.press("x")
            await pilot.pause()
            assert service.remove_calls[-1] == "claude_new"

    _run(body())


def test_disabled_projects_render_dimmed(settings_env) -> None:
    app = settings_env["app"]

    async def body() -> None:
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("tab")
            await pilot.pause()
            by_key = {p["key"]: p for p in app.data.projects}
            assert by_key["alpha"]["tracked"] is True
            assert by_key["charlie"]["tracked"] is False

            keys_in_order = [p["key"] for p in app.data.projects]
            charlie_idx = keys_in_order.index("charlie")
            app.projects_table.move_cursor(row=charlie_idx)
            app._sync_selection()
            await pilot.pause()
            detail = str(app.detail.render())
            assert "paused" in detail
            assert "Rationale" in detail

    _run(body())


def test_about_section_lists_version_and_disk(settings_env) -> None:
    app = settings_env["app"]

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            for _ in range(6):
                await pilot.press("tab")
                await pilot.pause()
            assert app._active_section == "about"
            about = str(app.kv_static.render())
            assert "PollyPM version" in about
            assert "Config path" in about
            assert "Disk usage" in about

    _run(body())


def test_settings_data_uses_history_rationale(settings_env, monkeypatch, tmp_path: Path) -> None:
    history_home = tmp_path / "home"
    monkeypatch.setattr("pollypm.cockpit_settings_history.Path.home", lambda: history_home)

    from pollypm.cockpit_settings_history import record_settings_history

    record_settings_history(
        "manual_switch",
        "worker-alpha -> codex_demo",
        {
            "session_name": "worker-alpha",
            "from_account": "claude_demo",
            "to_account": "codex_demo",
        },
    )
    record_settings_history(
        "failover",
        "failover claude_demo on",
        {"account": "claude_demo", "enabled": True},
    )

    app = settings_env["app"]

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            switched = next(a for a in app.data.accounts if a["key"] == "codex_demo")
            assert "Recent manual switch" in switched["rationale"]
            failed_over = next(a for a in app.data.accounts if a["key"] == "claude_demo")
            assert "Recent failover enabled for claude_demo" in failed_over["rationale"]
            defaulted = next(a for a in app.data.accounts if a["key"] == "claude_stale")
            assert "Default account from config: claude_demo." in defaulted["rationale"]

    _run(body())


def test_mount_perf_budget_for_20_projects_5_accounts(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "pollypm.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("# stubbed\n")

    projects = {
        f"proj_{i:02d}": _FakeProject(
            f"proj_{i:02d}",
            tracked=(i % 3 != 0),
            name=f"Project {i:02d}",
        )
        for i in range(20)
    }
    statuses = [
        _fake_status(f"acct_{i}", email=f"acct_{i}@example.com")
        for i in range(5)
    ]
    fake_config = _FakeConfig(tmp_path, projects=projects)
    fake_config.accounts = {
        f"acct_{i}": _FakeAccount(f"acct_{i}", ProviderKind.CLAUDE)
        for i in range(5)
    }
    monkeypatch.setattr("pollypm.cockpit_ui.load_config", lambda _p: fake_config)
    monkeypatch.setattr("pollypm.cockpit_ui.load_cached_account_usage", lambda _p: {})
    monkeypatch.setattr(
        "pollypm.cockpit_ui._collect_recent_tasks_by_account",
        lambda _config, _statuses, max_per_account=3: {},
    )
    monkeypatch.setattr(
        "pollypm.cockpit_ui.collect_settings_projects",
        _collect_project_rows,
    )
    from pollypm.cockpit_ui import PollySettingsPaneApp

    app = PollySettingsPaneApp(config_path)
    app.service = _FakeService(statuses, fake_config)  # type: ignore[assignment]

    async def body() -> None:
        start = time.perf_counter()
        async with app.run_test(size=(160, 42)) as pilot:
            await pilot.pause()
        elapsed = time.perf_counter() - start
        assert elapsed < 1.5, f"mount too slow: {elapsed:.3f}s"

    _run(body())
