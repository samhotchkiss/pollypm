import asyncio
from pathlib import Path

from pollypm.control_tui import InputModal, PollyPMApp
from pollypm.models import AccountConfig, ProviderKind, SessionConfig, SessionLaunchSpec


def test_accounts_table_preserves_selection_by_row_key(tmp_path: Path) -> None:
    async def run() -> None:
        app = PollyPMApp(tmp_path / "missing.toml")

        async with app.run_test():
            table = app.accounts_table
            app._replace_table_rows(
                table,
                [
                    (("acct-a", "a@example.com", "claude", "yes", "", "", "healthy"), "acct-a"),
                    (("acct-b", "b@example.com", "codex", "yes", "", "", "healthy"), "acct-b"),
                ],
            )
            table.move_cursor(row=1, animate=False, scroll=False)

            app._replace_table_rows(
                table,
                [
                    (("acct-a", "a@example.com", "claude", "yes", "", "", "79% left"), "acct-a"),
                    (("acct-b", "b@example.com", "codex", "yes", "", "", "100% left"), "acct-b"),
                ],
            )

            assert table.cursor_row == 1
            assert table.get_row_at(table.cursor_row)[0] == "acct-b"

    asyncio.run(run())


def test_control_tui_renders_cockpit_shell(tmp_path: Path) -> None:
    async def run() -> None:
        app = PollyPMApp(tmp_path / "missing.toml")

        async with app.run_test():
            assert app.title == "PollyPM"
            assert app.cockpit_table.id == "cockpit-nav"
            assert app.dashboard.id == "cockpit-body"
            assert app._active_tab() == "dashboard-tab"

    asyncio.run(run())


def test_cockpit_table_contains_polly_inbox_and_settings(tmp_path: Path) -> None:
    async def run() -> None:
        app = PollyPMApp(tmp_path / "missing.toml")

        async with app.run_test():
            rows = [
                (("Polly", "ready"), "polly"),
                (("Inbox (0)", "clear"), "inbox"),
                (("Projects", "browse"), "section:projects"),
                (("Demo Project", "idle"), "project:demo"),
                (("System", "tools"), "section:system"),
                (("Settings", "config"), "settings"),
            ]
            app._replace_table_rows(app.cockpit_table, rows)

            assert tuple(app.cockpit_table.get_row_at(0)) == ("Polly", "ready")
            assert tuple(app.cockpit_table.get_row_at(1)) == ("Inbox (0)", "clear")
            assert tuple(app.cockpit_table.get_row_at(3)) == ("Demo Project", "idle")
            assert tuple(app.cockpit_table.get_row_at(5)) == ("Settings", "config")

    asyncio.run(run())


def test_dashboard_settings_selection_jumps_to_accounts(tmp_path: Path) -> None:
    async def run() -> None:
        app = PollyPMApp(tmp_path / "missing.toml")

        async with app.run_test():
            app._replace_table_rows(
                app.cockpit_table,
                [
                    (("Polly", "ready"), "polly"),
                    (("Inbox (0)", "clear"), "inbox"),
                    (("Projects", "browse"), "section:projects"),
                    (("Demo Project", "idle"), "project:demo"),
                    (("System", "tools"), "section:system"),
                    (("Settings", "config"), "settings"),
                ],
            )
            app.cockpit_table.move_cursor(row=5, animate=False, scroll=False)

            app.action_open_selected_session()

            assert app._active_tab() == "accounts-tab"
            assert app.notice_text == "Jumped to account and runtime controls."

    asyncio.run(run())


def test_new_worker_modal_prefills_default_prompt(tmp_path: Path) -> None:
    async def run() -> None:
        app = PollyPMApp(tmp_path / "missing.toml")
        captured: dict[str, object] = {}

        async with app.run_test():
            app._set_active_tab("projects-tab")
            app.service.suggest_worker_prompt = lambda *, project_key: f"Scoped kickoff for {project_key}"  # type: ignore[method-assign]
            app._replace_table_rows(
                app.projects_table,
                [(("pollypm", "PollyPM", "git", "", str(tmp_path)), "pollypm")],
            )
            app.projects_table.move_cursor(row=0, animate=False, scroll=False)

            def fake_push_screen(screen, callback=None):
                captured["screen"] = screen
                captured["callback"] = callback
                return None

            app.push_screen = fake_push_screen  # type: ignore[method-assign]
            app.action_new_worker()

            screen = captured["screen"]
            assert isinstance(screen, InputModal)
            assert screen.request.value == "Scoped kickoff for pollypm"

    asyncio.run(run())


def test_send_input_modal_prefills_default_text(tmp_path: Path) -> None:
    async def run() -> None:
        app = PollyPMApp(tmp_path / "missing.toml")
        captured: dict[str, object] = {}

        async with app.run_test():
            app._set_active_tab("sessions-tab")
            app._replace_table_rows(
                app.sessions_table,
                [(("worker_pollypm", "worker", "pollypm", "codex_s_swh_me", "running", "", ""), "worker_pollypm")],
            )
            app.sessions_table.move_cursor(row=0, animate=False, scroll=False)

            def fake_push_screen(screen, callback=None):
                captured["screen"] = screen
                captured["callback"] = callback
                return None

            app.push_screen = fake_push_screen  # type: ignore[method-assign]
            app.action_send_input_selected()

            screen = captured["screen"]
            assert isinstance(screen, InputModal)
            assert screen.request.value == "Continue with the next step."

    asyncio.run(run())


def test_session_detail_uses_role_specific_tmux_session(tmp_path: Path) -> None:
    app = PollyPMApp(tmp_path / "missing.toml")
    session = SessionConfig(
        name="heartbeat",
        role="heartbeat-supervisor",
        provider=ProviderKind.CLAUDE,
        account="claude_main",
        cwd=tmp_path,
        window_name="pm-heartbeat",
    )
    account = AccountConfig(name="claude_main", provider=ProviderKind.CLAUDE, email="pearl@swh.me")
    launch = SessionLaunchSpec(
        session=session,
        account=account,
        window_name="pm-heartbeat",
        log_path=tmp_path / "heartbeat.log",
        command="claude",
    )

    class FakeTmux:
        def __init__(self) -> None:
            self.targets: list[str] = []

        def has_session(self, name: str) -> bool:
            return name == "pollypm-storage-closet"

        def capture_pane(self, target: str, lines: int = 200) -> str:
            self.targets.append(target)
            return "heartbeat preview"

    class FakeWindow:
        pane_current_command = "claude"
        pane_current_path = str(tmp_path)

    class FakeSupervisor:
        def __init__(self) -> None:
            self.tmux = FakeTmux()

        def plan_launches(self) -> list[SessionLaunchSpec]:
            return [launch]

        def _tmux_session_for_launch(self, _launch: SessionLaunchSpec) -> str:
            return "pollypm-storage-closet"

        def _window_map(self) -> dict[str, FakeWindow]:
            return {"pm-heartbeat": FakeWindow()}

    supervisor = FakeSupervisor()
    detail = app._session_detail(supervisor, "heartbeat")

    assert "heartbeat preview" in detail
    assert supervisor.tmux.targets == ["pollypm-storage-closet:pm-heartbeat"]


def test_numeric_keys_switch_tabs_even_with_table_focus(tmp_path: Path) -> None:
    async def run() -> None:
        app = PollyPMApp(tmp_path / "missing.toml")

        async with app.run_test() as pilot:
            app._set_active_tab("alerts-tab")
            app.alerts_table.focus()
            await pilot.press("2")
            assert app._active_tab() == "accounts-tab"
            await pilot.press("4")
            assert app._active_tab() == "sessions-tab"

    asyncio.run(run())
