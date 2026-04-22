import asyncio
from pathlib import Path

from pollypm.models import KnownProject, ProviderKind
from pollypm.onboarding import CliAvailability, ConnectedAccount
from pollypm.onboarding_tui import (
    BlockingAutoFix,
    ONBOARDING_STAGES,
    OnboardingApp,
    OnboardingResult,
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
        def __init__(self, config_path: Path, force: bool = False, no_animation: bool = False) -> None:
            self.config_path = config_path
            self.force = force

        def run(self, mouse: bool = False):
            assert mouse is True
            return OnboardingResult(config_path=self.config_path, launch_requested=True)

    monkeypatch.setattr("pollypm.onboarding_tui.OnboardingApp", FakeApp)

    result = run_onboarding_app(tmp_path / "pollypm.toml", force=True)

    assert result == OnboardingResult(config_path=tmp_path / "pollypm.toml", launch_requested=True)


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


def test_onboarding_app_lazily_creates_tmux_client(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "pollypm.onboarding_tui.create_tmux_client",
        lambda: (_ for _ in ()).throw(AssertionError("tmux client should be lazy")),
    )

    app = OnboardingApp(tmp_path / "pollypm.toml")

    assert app.tmux is None


def test_blocking_auto_fixes_exposes_tmux_fix(monkeypatch, tmp_path: Path) -> None:
    app = OnboardingApp(tmp_path / "pollypm.toml")
    monkeypatch.setattr(app, "_tmux_ready", lambda: False)
    monkeypatch.setattr(
        "pollypm.onboarding_tui.check_tmux",
        lambda: type(
            "Result",
            (),
            {
                "status": "tmux missing",
                "auto_fix": type(
                    "Plan",
                    (),
                    {
                        "description": "Install tmux",
                        "command": ["brew", "install", "tmux"],
                        "requires_sudo": False,
                        "platforms": ["macos"],
                    },
                )(),
            },
        )(),
    )

    fixes = app._blocking_auto_fixes()

    assert fixes[0].button_id == "fix-tmux"
    assert fixes[0].label == "Install tmux"


def test_run_machine_fix_refreshes_statuses(monkeypatch, tmp_path: Path) -> None:
    app = OnboardingApp(tmp_path / "pollypm.toml")
    messages: list[str] = []
    renders: list[str] = []
    app.state.statuses = []
    monkeypatch.setattr(
        app,
        "_blocking_auto_fixes",
        lambda: [
            BlockingAutoFix(
                button_id="fix-tmux",
                label="Install tmux",
                summary="tmux missing",
                plan=type(
                    "Plan",
                    (),
                    {
                        "description": "Install tmux",
                        "command": ["brew", "install", "tmux"],
                        "requires_sudo": False,
                        "platforms": ["macos"],
                    },
                )(),
            )
        ],
    )
    monkeypatch.setattr("pollypm.onboarding_tui.run_auto_fix", lambda _plan: (True, "Install tmux completed."))
    monkeypatch.setattr(
        "pollypm.onboarding_tui._available_clis",
        lambda: [CliAvailability(provider=ProviderKind.CLAUDE, label="Claude CLI", binary="claude", installed=True)],
    )
    monkeypatch.setattr(app, "_set_message", lambda message="": messages.append(message))
    monkeypatch.setattr(app, "_render_current_step", lambda: renders.append("rendered"))
    monkeypatch.setattr(app, "refresh", lambda *args, **kwargs: None)

    class _Suspend:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(app, "suspend", lambda: _Suspend())

    app._run_machine_fix("fix-tmux")

    assert renders == ["rendered"]
    assert messages[-1] == "Install tmux completed."
    assert app.state.statuses[0].installed is True


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
        monkeypatch.setattr("pollypm.onboarding_tui._detected_host_account", lambda provider: None)
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


def test_projects_step_can_offer_demo_repo_fallback(monkeypatch, tmp_path: Path) -> None:
    async def run() -> None:
        demo_path = tmp_path / "workspace" / "pollypm-demo"
        monkeypatch.setattr(
            "pollypm.onboarding_tui.demo_project_fallback_destination",
            lambda _config_path: demo_path,
        )
        monkeypatch.setattr(
            "pollypm.onboarding_tui.provision_demo_project_fallback",
            lambda _config_path: demo_path,
        )
        monkeypatch.setattr(
            "pollypm.onboarding.seed_demo_project_task",
            lambda project_path, *, project_key: "demo/1",
        )

        app = OnboardingApp(tmp_path / "pollypm.toml")
        app.step = "projects"
        app.state.scan_started = True
        app.state.scan_complete = True
        app.state.recent_projects = []
        app.state.selected_project_paths = []
        monkeypatch.setattr(
            app,
            "push_screen",
            lambda _screen, callback: callback("keep"),
        )

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.query_one("#projects-use-demo") is not None
            await pilot.click("#projects-use-demo")
            await pilot.pause()

            assert app.state.recent_projects == [demo_path]
            assert app.state.selected_project_paths == [demo_path]
            assert app.state.seeded_demo_task_id == "demo/1"
            assert app.project_selection is not None
            assert list(app.project_selection.selected) == [demo_path]
            assert "Seeded task demo/1" in str(app.message_widget.render())

    asyncio.run(run())


def test_demo_task_choice_keeps_seeded_task(monkeypatch, tmp_path: Path) -> None:
    app = OnboardingApp(tmp_path / "pollypm.toml")
    demo_path = tmp_path / "workspace" / "pollypm-demo"
    app._pending_demo_repo_path = demo_path
    app.state.known_projects = {}

    seeded: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        "pollypm.onboarding.seed_demo_project_task",
        lambda project_path, *, project_key: seeded.append((project_path, project_key)) or "demo/1",
    )
    rendered: list[str] = []
    monkeypatch.setattr(app, "_render_current_step", lambda: rendered.append("rendered"))
    messages: list[str] = []
    monkeypatch.setattr(app, "_set_message", lambda message="": messages.append(message))

    app._handle_demo_task_choice("keep")

    assert seeded == [(demo_path, "pollypm_demo")]
    assert app.state.recent_projects == [demo_path]
    assert app.state.selected_project_paths == [demo_path]
    assert app.state.seeded_demo_task_id == "demo/1"
    assert rendered == ["rendered"]
    assert "Seeded task demo/1" in messages[-1]


def test_demo_task_choice_can_forget_seeded_task(monkeypatch, tmp_path: Path) -> None:
    app = OnboardingApp(tmp_path / "pollypm.toml")
    demo_path = tmp_path / "workspace" / "pollypm-demo"
    app._pending_demo_repo_path = demo_path
    app.state.known_projects = {}
    app.state.seeded_demo_task_id = "demo/1"

    monkeypatch.setattr(
        "pollypm.onboarding.seed_demo_project_task",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not seed")),
    )
    rendered: list[str] = []
    monkeypatch.setattr(app, "_render_current_step", lambda: rendered.append("rendered"))
    messages: list[str] = []
    monkeypatch.setattr(app, "_set_message", lambda message="": messages.append(message))

    app._handle_demo_task_choice("forget")

    assert app.state.recent_projects == [demo_path]
    assert app.state.selected_project_paths == [demo_path]
    assert app.state.seeded_demo_task_id is None
    assert rendered == ["rendered"]
    assert "No seeded task" in messages[-1]
