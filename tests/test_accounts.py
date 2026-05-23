from pathlib import Path

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

    def fake_login_window(_tmux, provider, home, window_label, **_kwargs):  # noqa: ANN001
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

    def fake_login_window(_tmux, provider, home, window_label, **_kwargs):  # noqa: ANN001
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


def test_add_second_account_same_email_gets_suffix_key(monkeypatch, tmp_path: Path) -> None:
    """Two Claude accounts on the same email land at distinct keys (closes #2088)."""
    config_path = tmp_path / "pollypm.toml"
    config = _config(tmp_path)
    config.accounts["claude_s_example_com"] = AccountConfig(
        name="s@example.com",
        provider=ProviderKind.CLAUDE,
        email="s@example.com",
        home=tmp_path / ".pollypm" / "homes" / "claude_s_example_com",
    )
    write_config(config, config_path)

    def fake_login_window(_tmux, provider, home, window_label, **_kwargs):  # noqa: ANN001
        home.mkdir(parents=True, exist_ok=True)
        return "done"

    monkeypatch.setattr("pollypm.accounts._run_login_window", fake_login_window)
    monkeypatch.setattr("pollypm.accounts._detect_account_email", lambda provider, home: "s@example.com")
    monkeypatch.setattr("pollypm.accounts._prime_claude_home", lambda home: None)

    # Must NOT raise; second account should land under a suffixed key.
    key, email = add_account_via_login(config_path, ProviderKind.CLAUDE)

    assert key == "claude_s_example_com_2"
    assert email == "s@example.com"

    from pollypm.config import load_config

    saved = load_config(config_path)
    assert "claude_s_example_com" in saved.accounts
    assert "claude_s_example_com_2" in saved.accounts
    # name is always reconstructed from the key on load (config.py:_parse_accounts)
    assert saved.accounts["claude_s_example_com_2"].name == "claude_s_example_com_2"
    assert saved.accounts["claude_s_example_com_2"].email == "s@example.com"


def test_add_third_account_same_email_gets_sequential_suffix(monkeypatch, tmp_path: Path) -> None:
    """A third account on the same email gets _3."""
    config_path = tmp_path / "pollypm.toml"
    config = _config(tmp_path)
    for suffix, label in [("", "s@example.com"), ("_2", "s@example.com (#2)")]:
        config.accounts[f"claude_s_example_com{suffix}"] = AccountConfig(
            name=label,
            provider=ProviderKind.CLAUDE,
            email="s@example.com",
            home=tmp_path / ".pollypm" / "homes" / f"claude_s_example_com{suffix}",
        )
    write_config(config, config_path)

    def fake_login_window(_tmux, provider, home, window_label, **_kwargs):  # noqa: ANN001
        home.mkdir(parents=True, exist_ok=True)
        return "done"

    monkeypatch.setattr("pollypm.accounts._run_login_window", fake_login_window)
    monkeypatch.setattr("pollypm.accounts._detect_account_email", lambda provider, home: "s@example.com")
    monkeypatch.setattr("pollypm.accounts._prime_claude_home", lambda home: None)

    key, email = add_account_via_login(config_path, ProviderKind.CLAUDE)

    assert key == "claude_s_example_com_3"
    assert email == "s@example.com"

    from pollypm.config import load_config

    saved = load_config(config_path)
    # name is always reconstructed from the key on load (config.py:_parse_accounts)
    assert saved.accounts["claude_s_example_com_3"].name == "claude_s_example_com_3"
    assert saved.accounts["claude_s_example_com_3"].email == "s@example.com"


def test_add_account_forces_fresh_auth_and_disables_shortcut(
    monkeypatch, tmp_path: Path
) -> None:
    """The add path must force fresh auth so the Keychain shortcut can't mask a real login (closes #2088)."""
    import pytest

    config_path = tmp_path / "pollypm.toml"
    write_config(_config(tmp_path), config_path)
    monkeypatch.setattr("pollypm.accounts.Path.home", lambda: tmp_path)

    captured: dict = {}

    def fake_login_window(_tmux, *, provider, home, window_label, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        captured["window_label"] = window_label
        home.mkdir(parents=True, exist_ok=True)
        return "done"

    monkeypatch.setattr("pollypm.accounts._run_login_window", fake_login_window)
    monkeypatch.setattr(
        "pollypm.accounts._detect_account_email",
        lambda provider, home: "fresh@example.com",
    )
    monkeypatch.setattr("pollypm.accounts._prime_claude_home", lambda home: None)

    key, email = add_account_via_login(config_path, ProviderKind.CLAUDE)

    assert key == "claude_fresh_example_com"
    assert email == "fresh@example.com"
    assert captured.get("allow_existing_auth_shortcut") is False
    assert captured.get("force_fresh_auth") is True


def test_add_account_sentinel_without_hint_raises(
    monkeypatch, tmp_path: Path
) -> None:
    """When detection returns a Max-plan sentinel and no email_hint is supplied, raise."""
    import typer

    config_path = tmp_path / "pollypm.toml"
    write_config(_config(tmp_path), config_path)
    monkeypatch.setattr("pollypm.accounts.Path.home", lambda: tmp_path)

    def fake_login_window(_tmux, *, provider, home, window_label, **_kwargs):  # noqa: ANN001
        home.mkdir(parents=True, exist_ok=True)
        return "done"

    monkeypatch.setattr("pollypm.accounts._run_login_window", fake_login_window)
    # Sentinel: contains ":" but no "@"
    monkeypatch.setattr(
        "pollypm.accounts._detect_account_email",
        lambda provider, home: "claude.ai:max",
    )
    monkeypatch.setattr("pollypm.accounts._prime_claude_home", lambda home: None)

    import pytest

    with pytest.raises(typer.BadParameter) as excinfo:
        add_account_via_login(config_path, ProviderKind.CLAUDE)
    assert "--email" in str(excinfo.value)
    assert "claude.ai:max" in str(excinfo.value)


def test_add_account_with_email_hint_normalizes_and_uses_it(
    monkeypatch, tmp_path: Path
) -> None:
    """email_hint overrides detection and is normalized to lowercase + trimmed."""
    config_path = tmp_path / "pollypm.toml"
    write_config(_config(tmp_path), config_path)
    monkeypatch.setattr("pollypm.accounts.Path.home", lambda: tmp_path)

    def fake_login_window(_tmux, *, provider, home, window_label, **_kwargs):  # noqa: ANN001
        home.mkdir(parents=True, exist_ok=True)
        return "done"

    monkeypatch.setattr("pollypm.accounts._run_login_window", fake_login_window)
    # Detection would return a sentinel, but the hint takes precedence.
    monkeypatch.setattr(
        "pollypm.accounts._detect_account_email",
        lambda provider, home: "claude.ai:max",
    )
    monkeypatch.setattr("pollypm.accounts._prime_claude_home", lambda home: None)

    key, email = add_account_via_login(
        config_path,
        ProviderKind.CLAUDE,
        email_hint="  BACKUP@Example.COM  ",
    )
    assert key == "claude_backup_example_com"
    assert email == "backup@example.com"


def test_add_account_invalid_email_hint_raises(
    monkeypatch, tmp_path: Path
) -> None:
    """An email_hint without '@' is rejected with a clear error."""
    import typer

    config_path = tmp_path / "pollypm.toml"
    write_config(_config(tmp_path), config_path)
    monkeypatch.setattr("pollypm.accounts.Path.home", lambda: tmp_path)

    import pytest

    with pytest.raises(typer.BadParameter) as excinfo:
        add_account_via_login(
            config_path,
            ProviderKind.CLAUDE,
            email_hint="not-an-email",
        )
    assert "email" in str(excinfo.value).lower()
