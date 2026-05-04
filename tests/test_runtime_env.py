from pathlib import Path

from pollypm.models import AccountConfig, ProviderKind
from pollypm.runtime_env import (
    container_runtime_env_for_provider,
    launch_context_env,
    provider_profile_env,
)


def test_provider_profile_env_sets_provider_specific_home_variable(tmp_path: Path) -> None:
    account = AccountConfig(
        name="codex_main",
        provider=ProviderKind.CODEX,
        home=tmp_path / "codex-home",
    )

    env = provider_profile_env(account, base_env={"KEEP": "1"})

    assert env["KEEP"] == "1"
    assert env["CODEX_HOME"] == str(tmp_path / "codex-home" / ".codex")
    assert "CLAUDE_CONFIG_DIR" not in env


def test_container_runtime_env_sets_xdg_and_provider_paths(tmp_path: Path) -> None:
    env = container_runtime_env_for_provider(
        ProviderKind.CLAUDE,
        tmp_path / "claude-home",
        base_env={"KEEP": "1"},
    )

    assert env["KEEP"] == "1"
    assert env["HOME"] == str(tmp_path / "claude-home")
    assert env["XDG_CONFIG_HOME"] == str(tmp_path / "claude-home" / ".config")
    assert env["XDG_DATA_HOME"] == str(tmp_path / "claude-home" / ".local/share")
    assert env["XDG_STATE_HOME"] == str(tmp_path / "claude-home" / ".local/state")
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "claude-home" / ".claude")


def test_launch_context_env_carries_round_start_without_overriding() -> None:
    env = launch_context_env(
        base_env={"KEEP": "1"},
        ambient_env={"ROUND_START_ISO_TS": "2026-05-04T15:14:55+00:00"},
    )

    assert env["KEEP"] == "1"
    assert env["ROUND_START_ISO_TS"] == "2026-05-04T15:14:55+00:00"

    explicit = launch_context_env(
        base_env={"ROUND_START_ISO_TS": "explicit"},
        ambient_env={"ROUND_START_ISO_TS": "ambient"},
    )

    assert explicit["ROUND_START_ISO_TS"] == "explicit"
