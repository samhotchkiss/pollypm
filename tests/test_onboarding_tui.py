import asyncio
from pathlib import Path

from pollypm.models import KnownProject, ProviderKind
from pollypm.onboarding import CliAvailability, ConnectedAccount
from pollypm.onboarding_tui import (
    ONBOARDING_STAGES,
    OnboardingApp,
    default_controller_account,
    installed_provider_statuses,
    merge_selected_projects,
    onboarding_progress_lines,
    onboarding_step_header,
    run_onboarding_app,
)


def test_installed_provider_statuses_filters_missing_clis() -> None:
    statuses = [
        CliAvailability(provider=ProviderKind.CLAUDE, label="Claude CLI", binary="claude", installed=True),
        CliAvailability(provider=ProviderKind.CODEX, label="Codex CLI", binary="codex", installed=False),
    ]

    installed = installed_provider_statuses(statuses)

    assert [status.provider for status in installed] == [ProviderKind.CLAUDE]


def test_default_controller_account_prefers_first_connected_account(tmp_path: Path) -> None:
    accounts = {
        "claude_alpha": ConnectedAccount(
            provider=ProviderKind.CLAUDE,
            email="alpha@example.com",
            account_name="claude_alpha",
            home=tmp_path / "claude_alpha",
        ),
        "codex_beta": ConnectedAccount(
            provider=ProviderKind.CODEX,
            email="beta@example.com",
            account_name="codex_beta",
            home=tmp_path / "codex_beta",
        ),
    }

    assert default_controller_account(accounts) == "claude_alpha"


def test_merge_selected_projects_adds_new_git_projects_without_duplicates(tmp_path: Path) -> None:
    existing_path = tmp_path / "existing"
    new_path = tmp_path / "new-repo"
    existing_path.mkdir()
    new_path.mkdir()

    existing = {
        "existing": KnownProject(
            key="existing",
            path=existing_path,
            name="Existing",
        )
    }

    merged = merge_selected_projects(existing, [existing_path, new_path])

    assert set(merged) != set()
    assert any(project.path == new_path for project in merged.values())
    assert len([project for project in merged.values() if project.path == existing_path]) == 1


def test_run_onboarding_app_runs_textual_app_directly(monkeypatch, tmp_path: Path) -> None:
    class FakeApp:
        def __init__(self, config_path: Path, force: bool = False) -> None:
            self.config_path = config_path
            self.force = force

        def run(self, mouse: bool = False):
            assert mouse is True
            return self.config_path

    monkeypatch.setattr("pollypm.onboarding_tui.OnboardingApp", FakeApp)

    result = run_onboarding_app(tmp_path / "pollypm.toml", force=True)

    assert result == tmp_path / "pollypm.toml"


def test_scan_loading_message_animates(tmp_path: Path) -> None:
    app = OnboardingApp(tmp_path / "pollypm.toml")
    app.scan_loading_widget = type("Widget", (), {"updated": None, "update": lambda self, value: setattr(self, "updated", value)})()
    app.step = "projects"
    app.state.scan_complete = False

    app._update_scan_loading()
    first = app.scan_loading_widget.updated
    app._tick_scan_animation()
    second = app.scan_loading_widget.updated

    assert "Scanning" in first
    assert first != second


def test_codex_login_mode_routes_remote_to_headless(monkeypatch, tmp_path: Path) -> None:
    app = OnboardingApp(tmp_path / "pollypm.toml")
    captured: list[tuple[ProviderKind, bool]] = []
    monkeypatch.setattr(
        app,
        "_connect_provider",
        lambda provider, login_preferences=None: captured.append(
            (provider, bool(login_preferences and login_preferences.codex_headless))
        ),
    )

    app._handle_codex_login_mode("remote")

    assert captured == [(ProviderKind.CODEX, True)]


def test_codex_login_mode_routes_local_to_standard_login(monkeypatch, tmp_path: Path) -> None:
    app = OnboardingApp(tmp_path / "pollypm.toml")
    captured: list[tuple[ProviderKind, bool]] = []
    monkeypatch.setattr(
        app,
        "_connect_provider",
        lambda provider, login_preferences=None: captured.append(
            (provider, bool(login_preferences and login_preferences.codex_headless))
        ),
    )

    app._handle_codex_login_mode("local")

    assert captured == [(ProviderKind.CODEX, False)]


def test_codex_login_modal_is_centered(monkeypatch, tmp_path: Path) -> None:
    async def run() -> None:
        monkeypatch.setattr("pollypm.onboarding_tui._recover_existing_accounts", lambda _root: {})
        monkeypatch.setattr(
            "pollypm.onboarding_tui._available_clis",
            lambda: [
                CliAvailability(provider=ProviderKind.CLAUDE, label="Claude CLI", binary="claude", installed=True),
                CliAvailability(provider=ProviderKind.CODEX, label="Codex CLI", binary="codex", installed=True),
            ],
        )

        app = OnboardingApp(tmp_path / "pollypm.toml")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#connect-codex")
            await pilot.pause()

            dialog = app.screen.query_one("#codex-mode-dialog")
            region = dialog.region
            size = app.screen.size
            expected_x = (size.width - region.width) // 2
            expected_y = (size.height - region.height) // 2

            assert abs(region.x - expected_x) <= 1
            assert abs(region.y - expected_y) <= 1

    asyncio.run(run())


def test_connect_provider_cancel_returns_to_onboarding(monkeypatch, tmp_path: Path) -> None:
    from pollypm.onboarding import LoginCancelled

    app = OnboardingApp(tmp_path / "pollypm.toml")
    app.state = type(
        "State",
        (),
        {
            "accounts": {},
            "login_preferences": None,
            "controller_account": None,
            "failover_enabled": False,
            "open_permissions_by_default": True,
            "known_projects": {},
            "selected_project_paths": [],
        },
    )()
    messages: list[str] = []
    rendered: list[str] = []
    monkeypatch.setattr(app, "_set_message", lambda message="": messages.append(message))
    monkeypatch.setattr(app, "_render_current_step", lambda: rendered.append("rendered"))
    monkeypatch.setattr(app, "refresh", lambda *args, **kwargs: None)

    class _Suspend:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(app, "suspend", lambda: _Suspend())
    monkeypatch.setattr(
        "pollypm.onboarding_tui._connect_account_via_tmux",
        lambda *args, **kwargs: (_ for _ in ()).throw(LoginCancelled("Login cancelled. Returned to onboarding.")),
    )

    app._connect_provider(ProviderKind.CODEX)

    assert rendered == ["rendered"]
    assert messages[-1] == "Login cancelled. Returned to onboarding."


def test_onboarding_progress_header_shows_step_count_and_bar() -> None:
    header = onboarding_step_header("projects")

    assert "Step 3 of 4" in header
    assert "Add projects" in header
    assert "█" in header


def test_onboarding_progress_lines_mark_done_current_and_up_next() -> None:
    lines = onboarding_progress_lines("controller")

    assert f"✓[/] [#3ddc84]{ONBOARDING_STAGES[0][1]}" in lines[0]
    assert f"◉[/] [#5b8aff bold]{ONBOARDING_STAGES[1][1]}" in lines[1]
    assert f"○[/] [#6b7a88]{ONBOARDING_STAGES[2][1]}" in lines[2]
