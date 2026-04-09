from pathlib import Path

from pollypm.accounts import (
    _parse_claude_usage_text,
    _parse_codex_status_text,
    inspect_account_isolation,
    probe_account_usage,
)
from pollypm.config import write_config
from pollypm.models import AccountConfig, ProjectSettings, PollyPMConfig, PollyPMSettings, ProviderKind, RuntimeKind


def test_parse_claude_usage_text() -> None:
    health, summary = _parse_claude_usage_text(
        """
        Status   Config   Usage   Stats

        Current week (all models)
        ██████████▌                                        21% used
        Resets Apr 10 at 1am (America/Denver)
        """
    )

    assert health == "healthy"
    assert summary == "79% left this week · resets Apr 10 at 1am (America/Denver)"


def test_parse_codex_status_text() -> None:
    health, summary = _parse_codex_status_text(
        """
        › Implement {feature}

          gpt-5.4 default · 100% left · /Users/sam/dev/pollypm
        """
    )

    assert health == "healthy"
    assert summary == "100% left"


def test_inspect_codex_isolation_detects_file_backed_profile(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text("{}")
    account = AccountConfig(
        name="codex_primary",
        provider=ProviderKind.CODEX,
        runtime=RuntimeKind.LOCAL,
        home=home,
    )

    status, summary, recommendation, auth_storage, profile_root = inspect_account_isolation(account)

    assert status == "host-profile"
    assert "CODEX_HOME" in summary
    assert recommendation == ""
    assert auth_storage == "file"
    assert profile_root == str(home / ".codex")


def test_inspect_codex_isolation_flags_keyring_on_macos(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("pollypm.accounts.platform.system", lambda: "Darwin")
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n')
    account = AccountConfig(
        name="codex_primary",
        provider=ProviderKind.CODEX,
        runtime=RuntimeKind.LOCAL,
        home=home,
    )

    status, summary, recommendation, auth_storage, _ = inspect_account_isolation(account)

    assert status == "host-profile-keyring"
    assert "keyring" in summary
    assert "Docker runtime" in recommendation
    assert auth_storage == "keyring"


def test_inspect_claude_isolation_flags_keychain_on_macos(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("pollypm.accounts.platform.system", lambda: "Darwin")
    home = tmp_path / "home"
    account = AccountConfig(
        name="claude_primary",
        provider=ProviderKind.CLAUDE,
        runtime=RuntimeKind.LOCAL,
        home=home,
    )

    status, summary, recommendation, auth_storage, profile_root = inspect_account_isolation(account)

    assert status == "host-profile-keyring"
    assert "CLAUDE_CONFIG_DIR" in summary
    assert "Docker runtime" in recommendation
    assert auth_storage == "keychain"
    assert profile_root == str(home / ".claude")


def test_inspect_docker_isolation_marks_runtime_isolated(tmp_path: Path) -> None:
    account = AccountConfig(
        name="codex_primary",
        provider=ProviderKind.CODEX,
        runtime=RuntimeKind.DOCKER,
        home=tmp_path / "home",
        docker_image="ghcr.io/example/pollypm-agent:latest",
    )

    status, summary, recommendation, auth_storage, profile_root = inspect_account_isolation(account)

    assert status == "isolated-runtime"
    assert "Docker-isolated" in summary
    assert recommendation == ""
    assert auth_storage == "runtime-isolated"
    assert profile_root is None


def test_probe_account_usage_records_claude_refresh_failure(monkeypatch, tmp_path: Path) -> None:
    config = PollyPMConfig(
        project=ProjectSettings(
            root_dir=tmp_path,
            base_dir=tmp_path / ".pollypm-state",
            logs_dir=tmp_path / ".pollypm-state/logs",
            snapshots_dir=tmp_path / ".pollypm-state/snapshots",
            state_db=tmp_path / ".pollypm-state/state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_primary"),
        accounts={
            "claude_primary": AccountConfig(
                name="claude_primary",
                provider=ProviderKind.CLAUDE,
                email="claude@example.com",
                runtime=RuntimeKind.LOCAL,
                home=tmp_path / ".pollypm-state/homes/claude_primary",
            )
        },
        sessions={},
        projects={},
    )
    config_path = tmp_path / "pollypm.toml"
    write_config(config, config_path)

    monkeypatch.setattr("pollypm.accounts._run_usage_probe", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("Claude probe session is not authenticated.")))

    status = probe_account_usage(config_path, "claude_primary")

    assert status.health == "auth-broken"
    assert status.usage_summary == "usage refresh failed · Claude still opens the login flow"
