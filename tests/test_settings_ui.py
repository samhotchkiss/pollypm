"""Textual UI tests for the rebuilt :class:`PollySettingsPaneApp`.

The settings screen is the cockpit panel Sam lands on when he clicks
Settings in the left rail. These tests drive the rebuilt sections-based
screen via ``Pilot`` so we can assert every section renders, keyboard
nav between sections works, the ``/`` search filter narrows visible
rows, ``R`` refreshes live data, disabled projects dim, and the mount
path stays under a reasonable wall-clock budget for a synthetic
20-project / 5-account workspace.

Run with::

    HOME=/tmp/pytest-agent-settings uv run pytest \\
        tests/test_settings_ui.py -q
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from textual.widgets import Button

from pollypm.models import ProviderKind


# ---------------------------------------------------------------------------
# Fake data builders — avoid booting a real config loader / subprocess path
# ---------------------------------------------------------------------------


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


class _FakeProject:
    def __init__(self, key: str, *, tracked: bool = True, name: str | None = None) -> None:
        self.key = key
        self.name = name or key.replace("_", " ").title()
        self.path = Path("/tmp") / "projects" / key
        self.persona_name = None
        self.tracked = tracked

    def display_label(self) -> str:
        return self.name or self.key


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


class _FakeService:
    def __init__(self, statuses: list) -> None:
        self._statuses = statuses
        self.permissions_calls: list[bool] = []
        self.tracked_calls: list[tuple[str, bool]] = []
        self.controller_calls: list[str] = []
        self.add_account_calls: list[ProviderKind] = []
        self.remove_account_calls: list[tuple[str, bool]] = []
        self.refresh_usage_calls: list[str] = []
        self.cached_calls = 0
        self.live_calls = 0

    def list_account_statuses(self):
        self.live_calls += 1
        # Return a copy each call so a test can mutate the source list
        # between refreshes to exercise the R keybinding reload path.
        return list(self._statuses)

    def list_cached_account_statuses(self):
        self.cached_calls += 1
        # Return a copy each call so a test can mutate the source list
        # between refreshes to exercise the R keybinding reload path.
        return list(self._statuses)

    def set_open_permissions_default(self, enabled: bool) -> bool:
        self.permissions_calls.append(enabled)
        return enabled

    def set_project_tracked(self, key: str, enabled: bool) -> None:
        self.tracked_calls.append((key, enabled))

    def set_controller_account(self, key: str) -> None:
        self.controller_calls.append(key)

    def add_account(self, provider: ProviderKind) -> tuple[str, str]:
        self.add_account_calls.append(provider)
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

    def remove_account(self, key: str, *, delete_home: bool = False) -> tuple[str, str]:
        self.remove_account_calls.append((key, delete_home))
        self._statuses = [status for status in self._statuses if status.key != key]
        return key, "removed"


# ---------------------------------------------------------------------------
# Fixture: build a settings app with fully-faked config + service
# ---------------------------------------------------------------------------


@pytest.fixture
def settings_env(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "pollypm.toml"
    # pollypm.toml must exist so ``config_path.parent.exists()``
    # (the disk-usage probe) behaves like a real workspace.
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
            "codex_demo", provider=ProviderKind.CODEX,
            email="codex@example.com", health="capacity-low",
            usage="20% left",
        ),
        _fake_status(
            "claude_stale", email="stale@example.com",
            health="auth-broken", logged_in=False, usage="offline",
        ),
    ]
    fake_config = _FakeConfig(tmp_path, projects=projects)
    monkeypatch.setattr(
        "pollypm.cockpit_ui.load_config", lambda _p: fake_config,
    )
    service = _FakeService(statuses)

    from pollypm.cockpit_ui import PollySettingsPaneApp
    app = PollySettingsPaneApp(config_path)
    app.service = service  # type: ignore[assignment]
    return {
        "config_path": config_path,
        "app": app,
        "service": service,
        "statuses": statuses,
        "projects": projects,
    }


def _run(coro) -> None:
    asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. Settings screen mounts and renders all sections
# ---------------------------------------------------------------------------


def test_settings_mounts_all_sections(settings_env) -> None:
    app = settings_env["app"]

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            assert app.data is not None
            # Top bar mentions the controller + account count.
            topbar = str(app.topbar.render())
            assert "claude_demo" in topbar
            assert "accounts" in topbar
            # All seven section widgets are mounted.
            assert set(app._nav_widgets.keys()) == {
                "accounts", "projects", "heartbeat",
                "plugins", "planner", "inbox", "about",
            }
            # Default section renders the accounts table.
            assert app.accounts.display
            assert app.accounts.row_count == 3

    _run(body())


# ---------------------------------------------------------------------------
# 2. Accounts section lists configured accounts with correct fields
# ---------------------------------------------------------------------------


def test_accounts_section_lists_configured_accounts(settings_env) -> None:
    app = settings_env["app"]

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            assert app.accounts.row_count == 3
            detail_text = str(app.detail.render())
            # The first-row default selection is the healthy claude.
            assert "claude_demo" in detail_text
            assert "demo@example.com" in detail_text
            assert "healthy" in detail_text
            # The underlying data includes the capacity-low + auth-broken
            # accounts too — even though the detail only shows one.
            keys = [a["key"] for a in app.data.accounts]
            assert keys == ["claude_demo", "codex_demo", "claude_stale"]

    _run(body())


def test_accounts_detail_shows_cached_usage_snapshot_fields(settings_env) -> None:
    app = settings_env["app"]

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            detail_text = str(app.detail.render())
            assert "Remaining:" in detail_text
            assert "80%" in detail_text
            assert "Used:" in detail_text
            assert "Window:" in detail_text
            assert "current week" in detail_text
            assert "Resets:" in detail_text
            assert "Sampled:" in detail_text

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


def test_add_account_actions_refresh_the_accounts_snapshot(settings_env) -> None:
    app = settings_env["app"]
    service = settings_env["service"]

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app.action_add_codex_account()
            await pilot.pause()
            keys = [a["key"] for a in app.data.accounts]
            assert service.add_account_calls == [ProviderKind.CODEX]
            assert "codex_new" in keys

    _run(body())


def test_refresh_usage_action_updates_cached_account_detail(settings_env) -> None:
    app = settings_env["app"]
    service = settings_env["service"]

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app.action_refresh_selected_account_usage()
            await pilot.pause()
            assert service.refresh_usage_calls == ["claude_demo"]
            detail_text = str(app.detail.render())
            assert "92%" in detail_text
            assert "Sampled:" in detail_text

    _run(body())


def test_remove_account_action_confirms_and_refreshes_snapshot(settings_env, monkeypatch) -> None:
    app = settings_env["app"]
    service = settings_env["service"]

    def _auto_confirm(_screen, callback):
        callback(True)

    monkeypatch.setattr(app, "push_screen", _auto_confirm)

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app.action_remove_selected_account()
            await pilot.pause()
            keys = [a["key"] for a in app.data.accounts]
            assert service.remove_account_calls == [("claude_demo", False)]
            assert "claude_demo" not in keys

    _run(body())


# ---------------------------------------------------------------------------
# 3. Projects section lists registered projects with task counts
# ---------------------------------------------------------------------------


def test_projects_section_lists_registered_projects(settings_env) -> None:
    app = settings_env["app"]

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            # Step into Projects via Tab.
            await pilot.press("tab")
            await pilot.pause()
            assert app._active_section == "projects"
            assert app.projects_table.display
            # All three fixtures show up.
            keys = [p["key"] for p in app.data.projects]
            assert set(keys) == {"alpha", "bravo", "charlie"}
            assert app.projects_table.row_count == 3

    _run(body())


# ---------------------------------------------------------------------------
# 4. Keyboard nav between sections works (Tab + Enter + j/k)
# ---------------------------------------------------------------------------


def test_tab_moves_between_sections(settings_env) -> None:
    app = settings_env["app"]

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            assert app._active_section == "accounts"

            await pilot.press("tab")
            await pilot.pause()
            assert app._active_section == "projects"

            await pilot.press("tab")
            await pilot.pause()
            assert app._active_section == "heartbeat"

            await pilot.press("shift+tab")
            await pilot.pause()
            assert app._active_section == "projects"

    _run(body())


def test_jk_moves_nav_cursor_before_entering_table(settings_env) -> None:
    app = settings_env["app"]

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            # Nav cursor starts on index 0 (accounts).
            assert app._nav_cursor == 0
            await pilot.press("j")
            await pilot.pause()
            assert app._nav_cursor == 1
            await pilot.press("j")
            await pilot.pause()
            assert app._nav_cursor == 2
            await pilot.press("k")
            await pilot.pause()
            assert app._nav_cursor == 1

    _run(body())


# ---------------------------------------------------------------------------
# 5. Search filter `/` narrows visible rows
# ---------------------------------------------------------------------------


def test_search_filters_accounts(settings_env) -> None:
    app = settings_env["app"]

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            # Full list first.
            assert app.accounts.row_count == 3
            # Drive the filter by setting the query directly — the
            # Input.Changed handler re-renders the section in-place
            # (simulating the user typing after pressing `/`).
            app._search_query = "codex"
            app._render_section("accounts")
            await pilot.pause()
            assert app.accounts.row_count == 1

            # Clear the filter via the real back-or-cancel path.
            app.search_input.add_class("-active")
            app.action_back_or_cancel()
            await pilot.pause()
            assert app._search_query == ""
            assert app.accounts.row_count == 3

    _run(body())


# ---------------------------------------------------------------------------
# 6. R refresh reloads live data (service.list_account_statuses called
#    again)
# ---------------------------------------------------------------------------


def test_r_refresh_reloads_account_data(settings_env) -> None:
    app = settings_env["app"]
    service = settings_env["service"]

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            # Mutate the fake service's status list; a refresh should
            # surface the new row.
            service._statuses.append(
                _fake_status("claude_new", email="new@example.com"),
            )
            await pilot.press("r")
            await pilot.pause()
            keys = [a["key"] for a in app.data.accounts]
            assert "claude_new" in keys
            assert app.accounts.row_count == 4

    _run(body())


def test_settings_prefers_cached_account_status_snapshot(settings_env) -> None:
    app = settings_env["app"]
    service = settings_env["service"]

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            assert service.cached_calls >= 1
            assert service.live_calls == 0

    _run(body())


def test_settings_mount_skips_disk_usage_probe(settings_env, monkeypatch) -> None:
    app = settings_env["app"]

    def _fail(_path: Path) -> int:
        raise AssertionError("disk usage probe should not run during mount")

    monkeypatch.setattr("pollypm.cockpit_ui._settings_dir_size", _fail)

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            assert app.data is not None
            assert app._active_section == "accounts"

    _run(body())


def test_settings_reload_button_uses_cockpit_shell_reload(settings_env) -> None:
    app = settings_env["app"]
    calls: list[tuple[str, str | None, str | None]] = []

    class FakeRouter:
        def reload_cockpit_shell(
            self,
            *,
            kind: str = "settings",
            project_key: str | None = None,
            selected_key: str | None = None,
        ) -> None:
            calls.append((kind, project_key, selected_key))

    app._router = FakeRouter()  # type: ignore[assignment]

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            assert app.query_one("#settings-reload-cockpit") is not None
            app.action_reload_cockpit()
            await pilot.pause()
            assert calls == [("settings", None, "settings")]

    _run(body())


# ---------------------------------------------------------------------------
# 7. Disabled projects (tracked=False) render dimmed
# ---------------------------------------------------------------------------


def test_disabled_projects_render_dimmed(settings_env) -> None:
    app = settings_env["app"]

    async def body() -> None:
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("tab")  # step into Projects
            await pilot.pause()
            # Our fixtures mark Charlie as untracked.
            by_key = {p["key"]: p for p in app.data.projects}
            assert by_key["alpha"]["tracked"] is True
            assert by_key["charlie"]["tracked"] is False

            # Move the cursor to the Charlie row and verify the detail
            # reflects the paused state with the `t` hint.
            keys_in_order = [p["key"] for p in app.data.projects]
            charlie_idx = keys_in_order.index("charlie")
            app.projects_table.move_cursor(row=charlie_idx)
            app._sync_selection()
            await pilot.pause()
            detail_text = str(app.detail.render())
            assert "paused" in detail_text.lower()
            assert "charlie" in detail_text.lower()

    _run(body())


# ---------------------------------------------------------------------------
# 8. Mount completes within a reasonable budget for a synthetic config
#    with 20 projects + 5 accounts
# ---------------------------------------------------------------------------


def test_mount_perf_budget_for_20_projects_5_accounts(tmp_path: Path, monkeypatch) -> None:
    """Assert cold mount stays under 1.5s for a synthetic workspace.

    The settings screen used to lag because each render fired live
    subprocess calls. Our rebuild gathers everything once; this test is
    a regression floor. The budget is generous (1.5s) to tolerate
    slow CI hardware — the real target is "subjectively instant".
    """
    config_path = tmp_path / "pollypm.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("# stubbed\n")

    projects = {
        f"proj_{i:02d}": _FakeProject(
            f"proj_{i:02d}", tracked=(i % 3 != 0),
            name=f"Project {i:02d}",
        )
        for i in range(20)
    }
    statuses = [
        _fake_status(f"acct_{i}", email=f"a{i}@example.com")
        for i in range(5)
    ]
    fake_config = _FakeConfig(tmp_path, projects=projects)
    monkeypatch.setattr(
        "pollypm.cockpit_ui.load_config", lambda _p: fake_config,
    )
    from pollypm.cockpit_ui import PollySettingsPaneApp
    app = PollySettingsPaneApp(config_path)
    app.service = _FakeService(statuses)  # type: ignore[assignment]

    async def body() -> None:
        start = time.perf_counter()
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            elapsed = time.perf_counter() - start
            assert app.data is not None
            assert len(app.data.projects) == 20
            assert len(app.data.accounts) == 5
            assert elapsed < 1.5, (
                f"Settings mount took {elapsed:.3f}s; budget is 1.5s"
            )

    _run(body())


# ---------------------------------------------------------------------------
# Bonus: `b` toggle still fires permissions action even after rebuild
# (explicit regression of the legacy contract).
# ---------------------------------------------------------------------------


def test_b_key_toggles_permissions(settings_env) -> None:
    app = settings_env["app"]
    service = settings_env["service"]

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press("b")
            await pilot.pause()
            assert service.permissions_calls == [False]

    _run(body())


# ---------------------------------------------------------------------------
# Section-switch rendering sanity — kv sections show key/value pairs
# ---------------------------------------------------------------------------


def test_about_section_lists_version_and_disk(settings_env) -> None:
    app = settings_env["app"]

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            # Jump directly to About via repeated Tab.
            for _ in range(6):
                await pilot.press("tab")
                await pilot.pause()
            assert app._active_section == "about"
            kv_text = str(app.kv_static.render())
            assert "PollyPM version" in kv_text
            assert "Python" in kv_text
            assert "Disk usage" in kv_text

    _run(body())


def test_about_section_loads_disk_usage_lazily(settings_env, monkeypatch) -> None:
    app = settings_env["app"]
    monkeypatch.setattr("pollypm.cockpit_ui._settings_dir_size", lambda _path: 2048)

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            for _ in range(6):
                await pilot.press("tab")
                await pilot.pause()
            assert app._active_section == "about"
            kv_text = str(app.kv_static.render())
            assert "Disk usage" in kv_text
            assert "2.0 KB" in kv_text

    _run(body())
