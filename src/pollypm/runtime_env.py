from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from pollypm.models import AccountConfig, ProviderKind

LAUNCH_CONTEXT_ENV_KEYS = ("ROUND_START_ISO_TS",)


def claude_config_dir(home: Path) -> Path:
    return home / ".claude"


def codex_home_dir(home: Path) -> Path:
    return home / ".codex"


def launch_context_env(
    *,
    base_env: Mapping[str, str] | None = None,
    ambient_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Carry explicit test/orchestration context into tmux-launched agents."""
    env = dict(base_env or {})
    ambient = ambient_env if ambient_env is not None else os.environ
    for key in LAUNCH_CONTEXT_ENV_KEYS:
        if key in env:
            continue
        value = ambient.get(key)
        if value:
            env[key] = value
    return env


def provider_profile_env_for_provider(
    provider: ProviderKind,
    home: Path,
    *,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    env = dict(base_env or {})
    if provider is ProviderKind.CLAUDE:
        env["CLAUDE_CONFIG_DIR"] = str(claude_config_dir(home))
    if provider is ProviderKind.CODEX:
        env["CODEX_HOME"] = str(codex_home_dir(home))
    return env


def provider_profile_env(
    account: AccountConfig,
    *,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    if account.home is None:
        return dict(base_env or {})
    return provider_profile_env_for_provider(account.provider, account.home, base_env=base_env)


def container_runtime_env_for_provider(
    provider: ProviderKind,
    home: Path,
    *,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    env = dict(base_env or {})
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["XDG_DATA_HOME"] = str(home / ".local/share")
    env["XDG_STATE_HOME"] = str(home / ".local/state")
    return provider_profile_env_for_provider(provider, home, base_env=env)
