from pathlib import Path

from pollypm.models import KnownProject, ProviderKind
from pollypm.onboarding import CliAvailability, ConnectedAccount
from pollypm.onboarding_tui import (
    OnboardingApp,
    default_controller_account,
    installed_provider_statuses,
    merge_selected_projects,
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

    assert "Scanning your home folder" in first
    assert first != second
