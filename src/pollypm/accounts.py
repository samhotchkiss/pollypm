from __future__ import annotations

import json
import os
import platform
import re
import shutil
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import typer

from pollypm.plugins_builtin.core_agent_profiles.profiles import heartbeat_prompt, polly_prompt
from pollypm.config import load_config, write_config
from pollypm.models import AccountConfig, PollyPMConfig, ProviderKind
from pollypm.onboarding import (
    _decode_jwt_payload,
    _detect_account_email,
    _prime_claude_home,
    _resolve_account_identifier,
    _run_login_window,
    _slugify_email,
    default_control_args,
)
from pollypm.runtime_env import claude_config_dir, codex_home_dir, provider_profile_env
from pollypm.session_services import create_tmux_client
from pollypm.storage.state import StateStore


@dataclass(slots=True)
class AccountStatus:
    key: str
    provider: ProviderKind
    email: str
    home: Path | None
    logged_in: bool
    plan: str
    health: str
    usage_summary: str
    reason: str = ""
    available_at: str | None = None
    access_expires_at: str | None = None
    usage_updated_at: str | None = None
    usage_raw_text: str = ""
    used_pct: int | None = None
    remaining_pct: int | None = None
    reset_at: str | None = None
    period_label: str | None = None
    isolation_status: str = "unknown"
    isolation_summary: str = "unknown"
    isolation_recommendation: str = ""
    auth_storage: str = "unknown"
    profile_root: str | None = None


def _provider_account_count(accounts: dict[str, AccountConfig], provider: ProviderKind) -> int:
    return sum(1 for account in accounts.values() if account.provider is provider)


def _account_usage_summary(account: AccountConfig) -> tuple[str, str, str]:
    if account.home is None:
        return ("unknown", "missing-home", "not configured")

    if account.provider is ProviderKind.CLAUDE:
        env = provider_profile_env(account, base_env=os.environ)
        import subprocess

        try:
            result = subprocess.run(
                ["claude", "auth", "status", "--json"],
                check=False,
                capture_output=True,
                text=True,
                env=env,
                timeout=5,
            )
        except subprocess.TimeoutExpired:
            return ("unknown", "timeout", "status check timed out")
        except FileNotFoundError:
            return ("unknown", "binary-missing", "claude CLI missing")
        if result.returncode != 0:
            return ("unknown", "auth-broken", "status unavailable")
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return ("unknown", "auth-broken", "status unreadable")
        if not data.get("loggedIn"):
            return ("signed-out", "signed-out", "signed out")
        plan = str(data.get("subscriptionType") or "logged in").lower()
        org_name = str(data.get("orgName") or "").strip()
        summary = plan
        if org_name:
            summary = f"{plan} via {org_name}"
        return (plan, "healthy", summary)

    if account.provider is ProviderKind.CODEX:
        auth_path = account.home / ".codex" / "auth.json"
        if not auth_path.exists():
            return ("signed-out", "signed-out", "signed out")
        try:
            data = json.loads(auth_path.read_text())
            id_token = data.get("tokens", {}).get("id_token")
            payload = _decode_jwt_payload(id_token) if isinstance(id_token, str) and id_token.count(".") >= 2 else {}
            auth_data = payload.get("https://api.openai.com/auth", {})
            if not isinstance(auth_data, dict):
                auth_data = {}
            plan = str(auth_data.get("chatgpt_plan_type") or "logged in").lower()
            active_until = auth_data.get("chatgpt_subscription_active_until")
            if isinstance(active_until, str) and active_until:
                try:
                    end_date = datetime.fromisoformat(active_until.replace("Z", "+00:00")).date().isoformat()
                    return (plan, "healthy", f"{plan} until {end_date}")
                except ValueError:
                    return (plan, "healthy", plan)
            return (plan, "healthy", plan)
        except Exception:  # noqa: BLE001
            return ("unknown", "auth-broken", "status unreadable")

    return ("unknown", "unknown", "unknown")


def _cached_account_usage_summary(account: AccountConfig) -> tuple[str, str, str]:
    """Return a fast local/default status summary without provider probes."""
    if account.home is None:
        return ("unknown", "missing-home", "not configured")

    if account.provider is ProviderKind.CLAUDE:
        credentials_path = claude_config_dir(account.home) / ".credentials.json"
        if credentials_path.exists():
            return ("unknown", "healthy", "logged in")
        return ("unknown", "unknown", "status unavailable")

    if account.provider is ProviderKind.CODEX:
        auth_path = account.home / ".codex" / "auth.json"
        if not auth_path.exists():
            return ("signed-out", "signed-out", "signed out")
        try:
            data = json.loads(auth_path.read_text())
            id_token = data.get("tokens", {}).get("id_token")
            payload = (
                _decode_jwt_payload(id_token)
                if isinstance(id_token, str) and id_token.count(".") >= 2
                else {}
            )
            auth_data = payload.get("https://api.openai.com/auth", {})
            if not isinstance(auth_data, dict):
                auth_data = {}
            plan = str(auth_data.get("chatgpt_plan_type") or "logged in").lower()
            return (plan, "healthy", plan)
        except Exception:  # noqa: BLE001
            return ("unknown", "auth-broken", "status unreadable")

    return ("unknown", "unknown", "status unavailable")


def _effective_logged_in(
    account: AccountConfig,
    *,
    cached_health: str | None = None,
    runtime_status: str | None = None,
    probe_live: bool = True,
) -> bool:
    if runtime_status in {"auth-broken", "signed-out"}:
        return False
    if cached_health in {"auth-broken", "signed-out"}:
        return False
    if not probe_live:
        if cached_health:
            return True
        if runtime_status:
            return True
        if account.home is None:
            return False
        if account.provider is ProviderKind.CLAUDE:
            return bool(account.email)
        if account.provider is ProviderKind.CODEX:
            return (account.home / ".codex" / "auth.json").exists()
        return False

    from pollypm.acct import detect_logged_in

    return detect_logged_in(account)


def _codex_credentials_store(home: Path) -> str:
    config_path = codex_home_dir(home) / "config.toml"
    if not config_path.exists():
        return "auto"
    try:
        data = tomllib.loads(config_path.read_text())
    except Exception:  # noqa: BLE001
        return "auto"
    value = data.get("cli_auth_credentials_store")
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    auth = data.get("auth")
    if isinstance(auth, dict):
        nested = auth.get("credentials_store")
        if isinstance(nested, str) and nested.strip():
            return nested.strip().lower()
    return "auto"


def inspect_account_isolation(
    account: AccountConfig,
    *,
    provider_account_count: int | None = None,
) -> tuple[str, str, str, str, str | None]:
    if account.runtime.value == "docker":
        return (
            "isolated-runtime",
            "Account runs in a Docker-isolated runtime with a mounted shared workspace.",
            "",
            "runtime-isolated",
            None,
        )

    if account.home is None:
        remediation = ""
        if provider_account_count is None or provider_account_count > 1:
            remediation = "Configure an isolated home/profile root or switch this account to Docker."
        return (
            "default-profile",
            "Account uses your default ~/.claude/ (or ~/.codex/) login - no isolation.",
            remediation,
            "default-profile",
            None,
        )

    system = platform.system()
    if account.provider is ProviderKind.CLAUDE:
        profile_root = claude_config_dir(account.home)
        credentials_path = profile_root / ".credentials.json"
        if system == "Darwin":
            return (
                "host-profile-keyring",
                "Claude uses CLAUDE_CONFIG_DIR under the isolated home, but macOS auth may still rely on Keychain.",
                "Use Docker runtime for the strongest multi-account guarantees on macOS.",
                "keychain",
                str(profile_root),
            )
        if credentials_path.exists():
            return (
                "host-profile",
                "Claude uses CLAUDE_CONFIG_DIR under the isolated home with file-backed credentials detected.",
                "",
                "file",
                str(profile_root),
            )
        return (
            "host-profile",
            "Claude uses CLAUDE_CONFIG_DIR under the isolated home.",
            "",
            "unknown",
            str(profile_root),
        )

    if account.provider is ProviderKind.CODEX:
        profile_root = codex_home_dir(account.home)
        auth_path = profile_root / "auth.json"
        if auth_path.exists():
            return (
                "host-profile",
                "Codex uses CODEX_HOME under the isolated home with file-backed auth detected.",
                "",
                "file",
                str(profile_root),
            )
        credentials_store = _codex_credentials_store(account.home)
        if system == "Darwin" and credentials_store in {"auto", "keyring"}:
            return (
                "host-profile-keyring",
                f"Codex uses CODEX_HOME under the isolated home, but auth storage is {credentials_store} on macOS.",
                "Switch Codex to file-backed auth or use Docker runtime for stronger account separation.",
                credentials_store,
                str(profile_root),
            )
        return (
            "host-profile",
            f"Codex uses CODEX_HOME under the isolated home with credential store `{credentials_store}`.",
            "",
            credentials_store,
            str(profile_root),
        )

    return ("unknown", "Unknown provider isolation status.", "", "unknown", None)


def probe_account_usage(config_path: Path, identifier: str) -> AccountStatus:
    from pollypm.account_usage_sampler import refresh_account_usage

    config = load_config(config_path)
    account_name, account = _resolve_account_identifier(config, identifier)
    if account.home is None:
        raise typer.BadParameter(f"Account {account_name} does not have an isolated home configured.")

    refresh_account_usage(config_path, account_name)
    with StateStore(config.project.state_db) as store:
        cached = store.get_account_usage(account_name)
        runtime = store.get_account_runtime(account_name)
    default_plan = cached.plan if cached is not None else "unknown"
    default_health = cached.health if cached is not None else "unknown"
    default_summary = cached.usage_summary if cached is not None else "usage unavailable"
    isolation_status, isolation_summary, isolation_recommendation, auth_storage, profile_root = inspect_account_isolation(
        account,
        provider_account_count=_provider_account_count(config.accounts, account.provider),
    )
    return AccountStatus(
        key=account_name,
        provider=account.provider,
        email=account.email or account_name,
        home=account.home,
        logged_in=_effective_logged_in(
            account,
            cached_health=(cached.health if cached else None),
            runtime_status=(runtime.status if runtime else None),
        ),
        plan=default_plan,
        health=(runtime.status if runtime and runtime.status != "healthy" else (cached.health if cached else default_health)),
        usage_summary=(cached.usage_summary if cached else default_summary),
        reason=runtime.reason if runtime else "",
        available_at=runtime.available_at if runtime else None,
        access_expires_at=runtime.access_expires_at if runtime else None,
        usage_updated_at=(cached.updated_at if cached else None),
        usage_raw_text=(cached.raw_text if cached else ""),
        used_pct=(cached.used_pct if cached else None),
        remaining_pct=(cached.remaining_pct if cached else None),
        reset_at=(cached.reset_at if cached else None),
        period_label=(cached.period_label if cached else None),
        isolation_status=isolation_status,
        isolation_summary=isolation_summary,
        isolation_recommendation=isolation_recommendation,
        auth_storage=auth_storage,
        profile_root=profile_root,
    )


def list_account_statuses(config_path: Path) -> list[AccountStatus]:
    config = load_config(config_path)
    items: list[AccountStatus] = []
    with StateStore(config.project.state_db) as store:
        for key, account in config.accounts.items():
            plan, health, usage_summary = _account_usage_summary(account)
            cached = store.get_account_usage(key)
            runtime = store.get_account_runtime(key)
            logged_in = _effective_logged_in(
                account,
                cached_health=(cached.health if cached else None),
                runtime_status=(runtime.status if runtime else None),
            )
            isolation_status, isolation_summary, isolation_recommendation, auth_storage, profile_root = inspect_account_isolation(
                account,
                provider_account_count=_provider_account_count(config.accounts, account.provider),
            )
            items.append(
                AccountStatus(
                    key=key,
                    provider=account.provider,
                    email=account.email or key,
                    home=account.home,
                    logged_in=logged_in,
                    plan=cached.plan if cached else plan,
                    health=runtime.status if runtime and runtime.status != "healthy" else (cached.health if cached else health),
                    usage_summary=cached.usage_summary if cached else usage_summary,
                    reason=runtime.reason if runtime else "",
                    available_at=runtime.available_at if runtime else None,
                    access_expires_at=runtime.access_expires_at if runtime else None,
                    usage_updated_at=cached.updated_at if cached else None,
                    usage_raw_text=cached.raw_text if cached else "",
                    used_pct=cached.used_pct if cached else None,
                    remaining_pct=cached.remaining_pct if cached else None,
                    reset_at=cached.reset_at if cached else None,
                    period_label=cached.period_label if cached else None,
                    isolation_status=isolation_status,
                    isolation_summary=isolation_summary,
                    isolation_recommendation=isolation_recommendation,
                    auth_storage=auth_storage,
                    profile_root=profile_root,
                )
            )
    return items


def list_cached_account_statuses(config_path: Path) -> list[AccountStatus]:
    """Return a fast cached status snapshot suitable for interactive UIs.

    This path does not shell out to provider CLIs. It reads the state-store
    cache plus local profile files so cockpit screens can render immediately
    even when a provider auth-status probe is slow or wedged.
    """
    config = load_config(config_path)
    items: list[AccountStatus] = []
    with StateStore(config.project.state_db) as store:
        for key, account in config.accounts.items():
            plan, default_health, default_summary = _cached_account_usage_summary(
                account
            )
            cached = store.get_account_usage(key)
            runtime = store.get_account_runtime(key)
            logged_in = _effective_logged_in(
                account,
                cached_health=(cached.health if cached else None),
                runtime_status=(runtime.status if runtime else None),
                probe_live=False,
            )
            isolation_status, isolation_summary, isolation_recommendation, auth_storage, profile_root = inspect_account_isolation(
                account,
                provider_account_count=_provider_account_count(config.accounts, account.provider),
            )
            items.append(
                AccountStatus(
                    key=key,
                    provider=account.provider,
                    email=account.email or key,
                    home=account.home,
                    logged_in=logged_in,
                    plan=cached.plan if cached else plan,
                    health=runtime.status if runtime and runtime.status != "healthy" else (cached.health if cached else default_health),
                    usage_summary=cached.usage_summary if cached else default_summary,
                    reason=runtime.reason if runtime else "",
                    available_at=runtime.available_at if runtime else None,
                    access_expires_at=runtime.access_expires_at if runtime else None,
                    usage_updated_at=cached.updated_at if cached else None,
                    usage_raw_text=cached.raw_text if cached else "",
                    used_pct=cached.used_pct if cached else None,
                    remaining_pct=cached.remaining_pct if cached else None,
                    reset_at=cached.reset_at if cached else None,
                    period_label=cached.period_label if cached else None,
                    isolation_status=isolation_status,
                    isolation_summary=isolation_summary,
                    isolation_recommendation=isolation_recommendation,
                    auth_storage=auth_storage,
                    profile_root=profile_root,
                )
            )
    return items


def add_account_via_login(config_path: Path, provider: ProviderKind) -> tuple[str, str]:
    config = load_config(config_path)
    root_dir = config.project.root_dir
    tmux = create_tmux_client()

    existing = [account for account in config.accounts.values() if account.provider is provider]
    next_index = len(existing) + 1
    agent_homes = Path.home() / ".pollypm" / "agent_homes"
    agent_homes.mkdir(parents=True, exist_ok=True)
    home = agent_homes / f"{provider.value}_{next_index}"
    pane_text = _run_login_window(
        tmux,
        provider=provider,
        home=home,
        window_label=f"add-{provider.value}-{next_index}",
    )

    email = _detect_account_email(provider, home)
    if email is None:
        raise typer.BadParameter(f"Could not detect the logged-in email for the new {provider.value} account.")

    key = _slugify_email(provider, email)
    if key in config.accounts:
        if home.exists() and home != config.accounts[key].home:
            shutil.rmtree(home, ignore_errors=True)
        raise typer.BadParameter(f"Account {email} already exists.")

    if provider is ProviderKind.CLAUDE:
        # Claude auth lives in macOS Keychain, keyed to the CLAUDE_CONFIG_DIR path hash.
        # Renaming the directory would invalidate the keychain entry, so keep it in place.
        final_home = home
    else:
        final_home = config.project.base_dir / "homes" / key
        if home.exists() and home != final_home:
            final_home.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if final_home.exists():
                existing_email = _detect_account_email(provider, final_home)
                if existing_email == email:
                    shutil.rmtree(home, ignore_errors=True)
                else:
                    shutil.rmtree(final_home, ignore_errors=True)
                    home.rename(final_home)
            else:
                home.rename(final_home)
        else:
            final_home = home
    if provider is ProviderKind.CLAUDE:
        _prime_claude_home(final_home)

    config.accounts[key] = AccountConfig(
        name=key,
        provider=provider,
        email=email,
        home=final_home,
    )
    write_config(config, config_path, force=True)
    return key, email


def relogin_account(config_path: Path, identifier: str) -> tuple[str, str]:
    config = load_config(config_path)
    account_name, account = _resolve_account_identifier(config, identifier)
    if account.home is None:
        raise typer.BadParameter(f"Account {account_name} does not have an isolated home configured.")

    tmux = create_tmux_client()
    typer.echo(f"Re-launching login for {account.email or account_name} [{account.provider.value}]")
    _run_login_window(
        tmux,
        provider=account.provider,
        home=account.home,
        window_label=f"relogin-{account_name}",
        allow_existing_auth_shortcut=False,
        force_fresh_auth=True,
    )

    detected_email = _detect_account_email(account.provider, account.home)
    if detected_email:
        if account.provider is ProviderKind.CLAUDE:
            _prime_claude_home(account.home)
        config.accounts[account_name].email = detected_email
        write_config(config, path=config_path, force=True)
        return account_name, detected_email

    raise typer.BadParameter(
        f"PollyPM could not verify an authenticated {account.provider.value} session in the managed profile "
        f"for {account.email or account_name}."
    )


def remove_account(config_path: Path, identifier: str, *, delete_home: bool = False) -> tuple[str, str]:
    config = load_config(config_path)
    account_name, account = _resolve_account_identifier(config, identifier)
    _validate_account_removal(config, account_name)

    del config.accounts[account_name]
    if config.pollypm.controller_account == account_name:
        config.pollypm.controller_account = next(iter(config.accounts), "")
    config.pollypm.failover_accounts = [
        name for name in config.pollypm.failover_accounts if name != account_name
    ]
    write_config(config, path=config_path, force=True)

    if delete_home and account.home and account.home.exists():
        import shutil

        shutil.rmtree(account.home, ignore_errors=True)

    return account_name, account.email or account_name


def set_controller_account(config_path: Path, identifier: str) -> tuple[str, str]:
    config = load_config(config_path)
    account_name, account = _resolve_account_identifier(config, identifier)
    previous = config.pollypm.controller_account
    config.pollypm.controller_account = account_name
    config.sessions["heartbeat"].account = account_name
    config.sessions["heartbeat"].provider = account.provider
    config.sessions["heartbeat"].prompt = heartbeat_prompt()
    config.sessions["heartbeat"].agent_profile = "heartbeat"
    config.sessions["heartbeat"].args = default_control_args(
        account.provider,
        open_permissions=config.pollypm.open_permissions_by_default,
    )
    config.sessions["operator"].account = account_name
    config.sessions["operator"].provider = account.provider
    config.sessions["operator"].prompt = polly_prompt()
    config.sessions["operator"].agent_profile = "polly"
    config.sessions["operator"].args = default_control_args(
        account.provider,
        open_permissions=config.pollypm.open_permissions_by_default,
    )

    failover = [name for name in config.pollypm.failover_accounts if name != account_name]
    if previous and previous != account_name and previous in config.accounts and previous not in failover:
        failover.insert(0, previous)
    config.pollypm.failover_accounts = failover
    config.pollypm.failover_enabled = bool(failover)
    write_config(config, path=config_path, force=True)
    return account_name, account.email or account_name


def set_open_permissions_default(config_path: Path, enabled: bool) -> bool:
    config = load_config(config_path)
    config.pollypm.open_permissions_by_default = enabled

    for session_name in ("heartbeat", "operator"):
        session = config.sessions.get(session_name)
        if session is None:
            continue
        session.args = default_control_args(session.provider, open_permissions=enabled)

    write_config(config, path=config_path, force=True)
    return enabled


def toggle_failover_account(config_path: Path, identifier: str) -> tuple[str, bool]:
    config = load_config(config_path)
    account_name, _account = _resolve_account_identifier(config, identifier)
    if account_name == config.pollypm.controller_account:
        raise typer.BadParameter("The controller account cannot also be in the failover list.")

    failover = list(config.pollypm.failover_accounts)
    enabled: bool
    if account_name in failover:
        failover = [name for name in failover if name != account_name]
        enabled = False
    else:
        failover.append(account_name)
        enabled = True

    config.pollypm.failover_accounts = failover
    config.pollypm.failover_enabled = bool(failover)
    write_config(config, path=config_path, force=True)
    return account_name, enabled


def _validate_account_removal(config: PollyPMConfig, account_name: str) -> None:
    if account_name == config.pollypm.controller_account:
        raise typer.BadParameter(
            f"Account {account_name} is the PollyPM controller account. Reassign it before removal."
        )
    session_refs = [session.name for session in config.sessions.values() if session.account == account_name]
    if session_refs:
        raise typer.BadParameter(
            f"Account {account_name} is still used by sessions: {', '.join(session_refs)}"
        )
