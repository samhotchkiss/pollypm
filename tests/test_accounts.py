from pathlib import Path

import pytest

from pollypm.accounts import add_account_via_login
from pollypm.config import write_config
from pollypm.models import (
    AccountConfig,
    PollyPMConfig,
    PollyPMSettings,
    ProjectSettings,
    ProviderKind,
)


def _config(tmp_path: Path) -> PollyPMConfig:
    return PollyPMConfig(
        project=ProjectSettings(
            root_dir=tmp_path,
            base_dir=tmp_path / ".pollypm",
            logs_dir=tmp_path / ".pollypm/logs",
            snapshots_dir=tmp_path / ".pollypm/snapshots",
            state_db=tmp_path / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(controller_account="", failover_enabled=False),
        accounts={},
        sessions={},
        projects={},
    )


def test_add_account_reuses_orphaned_home_with_same_email(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    write_config(_config(tmp_path), config_path)
    orphan_home = tmp_path / ".pollypm" / "homes" / "claude_s_example_com"
    orphan_home.mkdir(parents=True, exist_ok=True)
    (orphan_home / "stale.txt").write_text("keep me")

    # Agent homes now live at ~/.pollypm/agent_homes/<provider>_<n>
    agent_homes = tmp_path / ".pollypm" / "agent_homes"
    monkeypatch.setattr("pollypm.accounts.Path.home", lambda: tmp_path)

    def fake_login_window(_tmux, provider, home, window_label):  # noqa: ANN001
        home.mkdir(parents=True, exist_ok=True)
        (home / "fresh.txt").write_text("fresh")
        return "done"

    def fake_detect(provider, home):  # noqa: ANN001
        # The ad-hoc home is now named claude_1 under agent_homes
        return "s@example.com"

    monkeypatch.setattr("pollypm.accounts._run_login_window", fake_login_window)
    monkeypatch.setattr("pollypm.accounts._detect_account_email", fake_detect)
    monkeypatch.setattr("pollypm.accounts._prime_claude_home", lambda home: None)

    key, email = add_account_via_login(config_path, ProviderKind.CLAUDE)

    assert key == "claude_s_example_com"
    assert email == "s@example.com"
    # Claude keeps the ad-hoc home in place (keychain auth is tied to the path)
    assert (agent_homes / "claude_1").exists()


def test_add_account_replaces_orphaned_home_when_stale(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    write_config(_config(tmp_path), config_path)
    orphan_home = tmp_path / ".pollypm" / "homes" / "claude_s_example_com"
    orphan_home.mkdir(parents=True, exist_ok=True)
    (orphan_home / "stale.txt").write_text("stale")

    # Agent homes now live at ~/.pollypm/agent_homes/<provider>_<n>
    agent_homes = tmp_path / ".pollypm" / "agent_homes"
    monkeypatch.setattr("pollypm.accounts.Path.home", lambda: tmp_path)

    def fake_login_window(_tmux, provider, home, window_label):  # noqa: ANN001
        home.mkdir(parents=True, exist_ok=True)
        (home / "fresh.txt").write_text("fresh")
        return "done"

    def fake_detect(provider, home):  # noqa: ANN001
        return "s@example.com"

    monkeypatch.setattr("pollypm.accounts._run_login_window", fake_login_window)
    monkeypatch.setattr("pollypm.accounts._detect_account_email", fake_detect)
    monkeypatch.setattr("pollypm.accounts._prime_claude_home", lambda home: None)

    key, _email = add_account_via_login(config_path, ProviderKind.CLAUDE)

    assert key == "claude_s_example_com"
    # Claude keeps the ad-hoc home in place (keychain auth is tied to the path)
    assert (agent_homes / "claude_1").exists()
    assert (agent_homes / "claude_1" / "fresh.txt").exists()


def test_add_account_rejects_duplicate_configured_account(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    config = _config(tmp_path)
    config.accounts["claude_s_example_com"] = AccountConfig(
        name="claude_s_example_com",
        provider=ProviderKind.CLAUDE,
        email="s@example.com",
        home=tmp_path / ".pollypm" / "homes" / "claude_s_example_com",
    )
    write_config(config, config_path)

    def fake_login_window(_tmux, provider, home, window_label):  # noqa: ANN001
        home.mkdir(parents=True, exist_ok=True)
        return "done"

    monkeypatch.setattr("pollypm.accounts._run_login_window", fake_login_window)
    monkeypatch.setattr("pollypm.accounts._detect_account_email", lambda provider, home: "s@example.com")

    with pytest.raises(Exception, match="already exists"):
        add_account_via_login(config_path, ProviderKind.CLAUDE)
