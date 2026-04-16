from __future__ import annotations

import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from pollypm.plugins_builtin.core_agent_profiles.profiles import heartbeat_prompt, polly_prompt
from pollypm.config import DEFAULT_CONFIG_PATH, load_config, write_config
from pollypm.models import (
    AccountConfig,
    KnownProject,
    ProjectKind,
    ProjectSettings,
    PollyPMConfig,
    PollyPMSettings,
    ProviderKind,
    SessionConfig,
)
from pollypm.projects import (
    DEFAULT_WORKSPACE_ROOT,
    discover_recent_git_repositories,
    ensure_project_scaffold,
    make_project_key,
)
from pollypm.runtime_env import provider_profile_env_for_provider
from typing import TYPE_CHECKING

from pollypm.session_services import create_tmux_client

if TYPE_CHECKING:
    from pollypm.tmux.client import TmuxClient


@dataclass(slots=True)
class ConnectedAccount:
    provider: ProviderKind
    email: str
    account_name: str
    home: Path


@dataclass(slots=True)
class CliAvailability:
    provider: ProviderKind
    label: str
    binary: str
    installed: bool


@dataclass(slots=True)
class ProviderChoice:
    key: str
    label: str
    provider: ProviderKind | None


@dataclass(slots=True)
class LoginPreferences:
    codex_headless: bool = False


@dataclass(slots=True)
class OnboardingResult:
    config_path: Path
    launch_requested: bool = False


class LoginCancelled(Exception):
    pass


_CLAUDE_OPERATOR_TOOLS = "Read,Glob,Grep,LS,Bash,WebFetch,WebSearch,TodoWrite,Task"
_CLAUDE_HEARTBEAT_TOOLS = "Read,Glob,Grep,LS,WebFetch,WebSearch,TodoWrite,Task"
_CLAUDE_NO_WRITE_TOOLS = "Edit,Write,MultiEdit,NotebookEdit"
_CLAUDE_OPERATOR_DISALLOWED = "Agent,Edit,Write,MultiEdit,NotebookEdit"  # PM delegates — never writes files


def default_session_args(
    provider: ProviderKind,
    *,
    open_permissions: bool = True,
    role: str = "",
) -> list[str]:
    args: list[str] = []
    if provider is ProviderKind.CLAUDE:
        if open_permissions and role not in {"heartbeat-supervisor", "operator-pm"}:
            args.append("--dangerously-skip-permissions")
        if role == "heartbeat-supervisor":
            args.extend(["--allowedTools", _CLAUDE_HEARTBEAT_TOOLS])
            args.extend(["--disallowedTools", _CLAUDE_NO_WRITE_TOOLS])
        elif role == "operator-pm":
            args.extend(["--allowedTools", _CLAUDE_OPERATOR_TOOLS])
            args.extend(["--disallowedTools", _CLAUDE_OPERATOR_DISALLOWED])
        return args
    if provider is ProviderKind.CODEX:
        if role in {"heartbeat-supervisor", "operator-pm"}:
            return ["--sandbox", "read-only", "--ask-for-approval", "never"]
        if role == "worker":
            return ["--sandbox", "workspace-write", "--ask-for-approval", "never"]
        if open_permissions:
            return ["--dangerously-bypass-approvals-and-sandbox"]
    return args


def default_control_args(
    provider: ProviderKind,
    *,
    open_permissions: bool = True,
    role: str = "",
) -> list[str]:
    return default_session_args(provider, open_permissions=open_permissions, role=role)


def _detected_claude_version() -> str:
    try:
        result = subprocess.run(
            ["claude", "--version"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return "2.1.92"
    match = re.search(r"(\d+\.\d+\.\d+)", result.stdout)
    if match:
        return match.group(1)
    return "2.1.92"


def _prime_claude_home(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)

    # Claude Code reads .claude.json from INSIDE CLAUDE_CONFIG_DIR (home/.claude/)
    state_path = claude_dir / ".claude.json"
    data: dict[str, object] = {}
    if state_path.exists():
        try:
            data = json.loads(state_path.read_text())
        except json.JSONDecodeError:
            data = {}

    if "firstStartTime" not in data:
        data["firstStartTime"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    if not isinstance(data.get("numStartups"), int):
        data["numStartups"] = 0
    data["hasCompletedOnboarding"] = True
    data["lastOnboardingVersion"] = str(data.get("lastOnboardingVersion") or _detected_claude_version())

    state_path.write_text(json.dumps(data, indent=2) + "\n")

    # Ensure settings.json has the flags needed for unattended operation:
    # - skipDangerousModePermissionPrompt: skip the "are you sure?" dialog
    # - bypassWorkspaceTrust: skip the "is this a project you trust?" dialog
    # - permissions.dangerouslySkipPermissions: match the --dangerously-skip-permissions flag
    settings_path = claude_dir / "settings.json"
    settings: dict[str, object] = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            settings = {}
    settings["skipDangerousModePermissionPrompt"] = True
    settings["bypassWorkspaceTrust"] = True
    if not isinstance(settings.get("permissions"), dict):
        settings["permissions"] = {}
    settings["permissions"]["dangerouslySkipPermissions"] = True  # type: ignore[index]
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")


def _available_clis() -> list[CliAvailability]:
    return [
        CliAvailability(
            provider=ProviderKind.CLAUDE,
            label="Claude CLI",
            binary="claude",
            installed=shutil.which("claude") is not None,
        ),
        CliAvailability(
            provider=ProviderKind.CODEX,
            label="Codex CLI",
            binary="codex",
            installed=shutil.which("codex") is not None,
        ),
    ]


def _render_intro(statuses: list[CliAvailability]) -> None:
    console = Console()
    hero_text = Text(justify="left")
    hero_text.append("PollyPM\n", style="bold bright_white")
    hero_text.append("Set up your CLI agents, detect active projects, and bring the control room online.", style="white")
    hero = Panel(
        hero_text,
        border_style="grey70",
        box=box.ROUNDED,
        padding=(1, 2),
    )
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column("tool", style="bold")
    table.add_column("status")
    for item in statuses:
        state = "[green]Ready[/green]" if item.installed else "[red]Missing[/red]"
        table.add_row(item.label, state)
    table.add_row("tmux", "[green]Ready[/green]" if shutil.which("tmux") else "[red]Missing[/red]")
    machine_panel = Panel(table, title="Machine Check", border_style="grey50", box=box.ROUNDED)

    flight_plan = Table(show_header=False, box=None, pad_edge=False)
    flight_plan.add_column("step", style="bold cyan", width=4)
    flight_plan.add_column("detail")
    flight_plan.add_row("01", "Connect your first agent account")
    flight_plan.add_row("02", "Detect projects with recent activity")
    flight_plan.add_row("03", "Launch your control room")
    plan_panel = Panel(
        flight_plan,
        title="Setup Flow",
        subtitle="You can add more accounts later",
        border_style="grey50",
        box=box.ROUNDED,
    )

    console.print(hero)
    console.print(Columns([machine_panel, plan_panel], equal=True, expand=True))
    console.print()


def _starting_provider_message(installed: list[CliAvailability]) -> str:
    if len(installed) == 1:
        return f"First, let's connect {installed[0].label}."
    return "First, let's connect your agent accounts."


def _provider_choices(installed: list[CliAvailability], accounts: dict[str, ConnectedAccount]) -> list[ProviderChoice]:
    connected_by_provider = {item.provider: 0 for item in installed}
    for account in accounts.values():
        connected_by_provider[account.provider] = connected_by_provider.get(account.provider, 0) + 1

    choices: list[ProviderChoice] = []
    for index, item in enumerate(installed, start=1):
        count = connected_by_provider.get(item.provider, 0)
        suffix = f" ({count} connected)" if count else ""
        choices.append(ProviderChoice(str(index), f"{item.label}{suffix}", item.provider))
    if accounts:
        choices.append(ProviderChoice(str(len(choices) + 1), "Continue", None))
    return choices


def _render_account_step_intro(installed: list[CliAvailability], accounts: dict[str, ConnectedAccount]) -> None:
    console = Console()
    body = Text()
    body.append(_starting_provider_message(installed), style="bold white")
    body.append("\n")
    body.append(
        "PollyPM opens a real login window, detects the account automatically, and saves it as a reusable profile.",
        style="dim",
    )
    if not accounts:
        body.append("\n\n")
        body.append("Press Return to connect your first account.", style="bold bright_green")
    else:
        body.append("\n\n")
        body.append("You can connect more now, or later anytime from the Accounts tab.", style="yellow")
    console.print(Panel(body, title="Account Setup", border_style="grey50", box=box.ROUNDED))


def _render_connected_account(account: ConnectedAccount, total_accounts: int) -> None:
    console = Console()
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column("field", style="bold")
    table.add_column("value")
    table.add_row("Connected", account.email)
    table.add_row("Provider", account.provider.value)
    table.add_row("Profile", account.account_name)
    table.add_row("Accounts ready", str(total_accounts))
    console.print(Panel(table, title="Account Ready", border_style="green", box=box.ROUNDED))


def _provider_description(provider: ProviderKind) -> str:
    if provider is ProviderKind.CLAUDE:
        return "Strong for planning, review, and longer strategic loops."
    if provider is ProviderKind.CODEX:
        return "Strong for implementation, shell-heavy work, and fast coding loops."
    return ""


def _render_provider_choices(choices: list[ProviderChoice]) -> None:
    console = Console()
    table = Table(box=box.SIMPLE_HEAVY, expand=True, show_header=True, header_style="bold white")
    table.add_column("Option", width=8, style="bold cyan")
    table.add_column("Provider", width=18, style="bold")
    table.add_column("Best For")
    for item in choices:
        if item.provider is None:
            table.add_row(item.key, "Continue", "Move on to controller and project setup.")
            continue
        table.add_row(item.key, item.label, _provider_description(item.provider))
    console.print(Panel(table, title="Choose a Provider", border_style="grey50", box=box.ROUNDED))


def _select_provider_to_connect(installed: list[CliAvailability], accounts: dict[str, ConnectedAccount]) -> ProviderKind | None:
    if len(installed) == 1:
        if not accounts:
            _render_account_step_intro(installed, accounts)
            typer.prompt("Press Return to start", default="", show_default=False)
        return installed[0].provider

    _render_account_step_intro(installed, accounts)
    choices = _provider_choices(installed, accounts)
    _render_provider_choices(choices)
    choice = typer.prompt("Choose", default="1")
    for item in choices:
        if choice == item.key:
            return item.provider
    return None


def _next_account_index(accounts: dict[str, ConnectedAccount], provider: ProviderKind) -> int:
    return len([account for account in accounts.values() if account.provider is provider]) + 1


def _connect_accounts_interactively(
    tmux: TmuxClient,
    *,
    root_dir: Path,
    accounts: dict[str, ConnectedAccount],
    available: list[CliAvailability],
) -> dict[str, ConnectedAccount]:
    installed = [item for item in available if item.installed]
    while True:
        provider = _select_provider_to_connect(installed, accounts)
        if provider is None:
            break
        account = _connect_account_via_tmux(
            tmux,
            root_dir=root_dir,
            provider=provider,
            index=_next_account_index(accounts, provider),
        )
        if account.account_name in accounts:
            raise typer.BadParameter(
                f"Duplicate connected account detected for {account.email}. "
                "Each connected account email must be unique."
            )
        accounts[account.account_name] = account
        typer.echo("")
        _render_connected_account(account, len(accounts))
        typer.echo("")
        if not typer.confirm("Connect another account?", default=True):
            break
    return accounts


def _scan_recent_projects(config_path: Path) -> list[KnownProject]:
    discovered = discover_recent_project_candidates(config_path)
    if not discovered:
        typer.echo("No recently active git repos were found in your home folder.")
        return []

    typer.echo("")
    typer.echo("PollyPM found recently active repositories:")
    selected: list[Path] = []
    for repo_path in discovered:
        if typer.confirm(f"Add project {repo_path.name} at {repo_path}?", default=True):
            selected.append(repo_path)
    return add_selected_projects(config_path, selected)


def discover_recent_project_candidates(config_path: Path) -> list[Path]:
    config = load_config(config_path)
    known_paths = {project.path.resolve() for project in config.projects.values()}
    return discover_recent_git_repositories(Path.home(), known_paths=known_paths, recent_days=14)


def add_selected_projects(config_path: Path, selected_paths: list[Path]) -> list[KnownProject]:
    if not selected_paths:
        return []
    config = load_config(config_path)
    added: list[KnownProject] = []
    for repo_path in selected_paths:
        normalized = repo_path.resolve()
        if any(project.path.resolve() == normalized for project in config.projects.values()):
            continue
        project = KnownProject(
            key=make_project_key(normalized, set(config.projects) | {item.key for item in added}),
            path=normalized,
            name=normalized.name,
            kind=ProjectKind.GIT,
        )
        config.projects[project.key] = project
        ensure_project_scaffold(normalized)
        added.append(project)
    if added:
        write_config(config, path=config_path, force=True)
    return added


def _slugify_email(provider: ProviderKind, email: str) -> str:
    normalized = email.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
    return f"{provider.value}_{slug}"


def _display_label(account: ConnectedAccount) -> str:
    provider = "Claude" if account.provider is ProviderKind.CLAUDE else "Codex"
    return f"{provider} · {account.email}"


def _choose_account(accounts: dict[str, ConnectedAccount], prompt: str) -> str:
    ordered_names = list(accounts)
    typer.echo("")
    typer.echo("Available accounts:")
    for index, name in enumerate(ordered_names, start=1):
        typer.echo(f"{index}. {_display_label(accounts[name])}")
    choice = typer.prompt(prompt, type=int, default=1)
    if choice < 1 or choice > len(ordered_names):
        raise typer.BadParameter(f"Choose a number between 1 and {len(ordered_names)}.")
    return ordered_names[choice - 1]


def _default_failover_accounts(accounts: dict[str, ConnectedAccount], controller_account: str) -> list[str]:
    remaining = [name for name in accounts if name != controller_account]
    if not remaining:
        return []
    typer.echo("")
    typer.echo("PollyPM failover order:")
    for name in remaining:
        typer.echo(f"- {_display_label(accounts[name])}")
    return remaining


def _runtime_home(root_dir: Path, provider: ProviderKind, index: int) -> Path:
    return root_dir / "homes" / f"onboarding_{provider.value}_{index}"


def _login_command(
    provider: ProviderKind,
    *,
    interactive: bool = False,
    preferences: LoginPreferences | None = None,
) -> str:
    if provider is ProviderKind.CODEX:
        if preferences is not None and preferences.codex_headless:
            return "codex login --device-auth"
        return "codex login"
    if provider is ProviderKind.CLAUDE:
        if interactive:
            return "claude"
        return "claude auth login --claudeai"
    raise ValueError(f"Unsupported provider: {provider.value}")


def _build_login_shell(
    provider: ProviderKind,
    home: Path,
    *,
    return_to_caller: bool = False,
    interactive: bool = False,
    force_fresh_auth: bool = False,
    preferences: LoginPreferences | None = None,
) -> str:
    env = provider_profile_env_for_provider(provider, home)
    parts = [f"mkdir -p {shlex.quote(str(home))}"]
    if "CODEX_HOME" in env:
        parts.append(f"mkdir -p {shlex.quote(env['CODEX_HOME'])}")
    if "CLAUDE_CONFIG_DIR" in env:
        parts.append(f"mkdir -p {shlex.quote(env['CLAUDE_CONFIG_DIR'])}")
    for key, value in env.items():
        parts.append(f"export {key}={shlex.quote(value)}")
    if force_fresh_auth:
        if provider is ProviderKind.CLAUDE:
            parts.append("claude auth logout || true")
        elif provider is ProviderKind.CODEX:
            parts.append("codex logout || true")
    parts.append(_login_command(provider, interactive=interactive, preferences=preferences))
    if return_to_caller:
        parts.append('printf "\\nPollyPM: login window complete. Returning to onboarding...\\n"')
        parts.append("sleep 1")
    else:
        parts.append('printf "\\nPollyPM: login window complete. Detach with Ctrl-b d to continue onboarding.\\n"')
        parts.append('exec "${SHELL:-/bin/zsh}" -l')
    return "sh -lc " + shlex.quote(" && ".join(parts))


def _decode_jwt_payload(token: str) -> dict[str, object]:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def _detect_codex_email(home: Path) -> str | None:
    auth_path = home / ".codex" / "auth.json"
    if not auth_path.exists():
        return None
    try:
        data = json.loads(auth_path.read_text())
        id_token = data.get("tokens", {}).get("id_token")
        if not isinstance(id_token, str) or id_token.count(".") < 2:
            return None
        payload = _decode_jwt_payload(id_token)
        email = payload.get("email")
        return str(email).lower() if isinstance(email, str) and email else None
    except Exception:  # noqa: BLE001
        return None


def _isolated_env(provider: ProviderKind, home: Path) -> dict[str, str]:
    return provider_profile_env_for_provider(provider, home, base_env=os.environ)


def _detect_claude_email(home: Path) -> str | None:
    env = _isolated_env(ProviderKind.CLAUDE, home)
    json_result = subprocess.run(
        ["claude", "auth", "status", "--json"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if json_result.returncode == 0:
        try:
            data = json.loads(json_result.stdout)
            if not data.get("loggedIn"):
                return None
            email = data.get("email")
            if isinstance(email, str) and email:
                return email.lower()
        except Exception:  # noqa: BLE001
            pass

    text_result = subprocess.run(
        ["claude", "auth", "status", "--text"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if text_result.returncode == 0:
        match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text_result.stdout)
        if match:
            return match.group(0).lower()
    return None


def _detect_account_email(provider: ProviderKind, home: Path) -> str | None:
    if provider is ProviderKind.CODEX:
        return _detect_codex_email(home)
    if provider is ProviderKind.CLAUDE:
        return _detect_claude_email(home)
    raise ValueError(f"Unsupported provider: {provider.value}")


def _detect_email_from_pane(provider: ProviderKind, pane_text: str) -> str | None:
    if provider is ProviderKind.CODEX:
        match = re.search(r"Account:\s+([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})", pane_text)
        if match:
            return match.group(1).lower()
    return None


def _claude_prompt_ready(pane_text: str) -> bool:
    lowered = pane_text.lower()
    if "select login method:" in lowered or "please run /login" in lowered:
        return False
    if "choose the text style that looks best with your terminal" in lowered:
        return False
    return "❯" in pane_text and ("welcome back" in lowered or "claude code v" in lowered)


def _login_completion_marker_seen(pane_text: str) -> bool:
    return "PollyPM: login window complete." in pane_text


def _wait_for_login_completion(
    tmux: TmuxClient,
    *,
    target: str,
    provider: ProviderKind,
    home: Path,
    allow_existing_auth_shortcut: bool = True,
    timeout_seconds: int = 300,
    poll_interval: float = 1.0,
) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout_seconds
    last_pane = ""
    while time.monotonic() < deadline:
        try:
            last_pane = tmux.capture_pane(target, lines=200)
        except Exception:  # noqa: BLE001
            last_pane = ""

        if _login_completion_marker_seen(last_pane):
            return True, last_pane

        if _detect_email_from_pane(provider, last_pane):
            return True, last_pane
        if allow_existing_auth_shortcut and _detect_account_email(provider, home):
            return True, last_pane

        time.sleep(poll_interval)

    return False, last_pane


def _final_account_home(root_dir: Path, account_name: str) -> Path:
    return root_dir / "homes" / account_name


def _promote_onboarding_home(temp_home: Path, final_home: Path) -> Path:
    if temp_home.resolve() == final_home.resolve():
        return final_home

    final_home.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if final_home.exists():
        shutil.rmtree(temp_home, ignore_errors=True)
        return final_home
    shutil.move(str(temp_home), str(final_home))
    return final_home


def _provider_from_home_name(name: str) -> ProviderKind | None:
    if name.startswith("codex_") or name.startswith("onboarding_codex_"):
        return ProviderKind.CODEX
    if name.startswith("claude_") or name.startswith("onboarding_claude_"):
        return ProviderKind.CLAUDE
    return None


def _recover_existing_accounts(root_dir: Path) -> dict[str, ConnectedAccount]:
    homes_dir = root_dir / "homes"
    if not homes_dir.exists():
        return {}

    connected: dict[str, ConnectedAccount] = {}
    for entry in sorted(homes_dir.iterdir()):
        if not entry.is_dir():
            continue
        provider = _provider_from_home_name(entry.name)
        if provider is None:
            continue

        email = _detect_account_email(provider, entry)
        if email is None:
            continue

        account_name = _slugify_email(provider, email)
        final_home = _final_account_home(root_dir, account_name)
        actual_home = entry
        if entry.name.startswith("onboarding_"):
            actual_home = _promote_onboarding_home(entry, final_home)
        if provider is ProviderKind.CLAUDE:
            _prime_claude_home(actual_home if actual_home.exists() else final_home)
        connected[account_name] = ConnectedAccount(
            provider=provider,
            email=email,
            account_name=account_name,
            home=actual_home if actual_home.exists() else final_home,
        )

    return connected


def _run_login_window(
    tmux: TmuxClient,
    *,
    provider: ProviderKind,
    home: Path,
    window_label: str,
    quiet: bool = False,
    allow_existing_auth_shortcut: bool = True,
    force_fresh_auth: bool = False,
    preferences: LoginPreferences | None = None,
) -> str:
    current_tmux = tmux.current_session_name()
    temp_session = f"pollypm-login-{window_label}"

    if current_tmux:
        current_window = tmux.current_window_index()
        tmux.run("kill-window", "-t", f"{current_tmux}:{window_label}", check=False)
        tmux.create_window(
            current_tmux,
            window_label,
            _build_login_shell(
                provider,
                home,
                interactive=(provider is ProviderKind.CLAUDE),
                force_fresh_auth=force_fresh_auth,
                preferences=preferences,
            ),
        )
        tmux.select_window(f"{current_tmux}:{window_label}")
        if not quiet:
            typer.echo(
                f"Finish login in window `{window_label}`. PollyPM will switch you back automatically "
                "when the login completes."
            )
        completed, pane_text = _wait_for_login_completion(
            tmux,
            target=f"{current_tmux}:{window_label}",
            provider=provider,
            home=home,
            allow_existing_auth_shortcut=allow_existing_auth_shortcut,
        )
        if current_window is not None:
            tmux.select_window(f"{current_tmux}:{current_window}")
        if not completed:
            if not quiet:
                typer.echo("PollyPM did not detect completion automatically.")
                typer.echo(f"Finish login in window `{window_label}`, then press Enter here to continue.")
                typer.prompt("Press Enter after login is complete", default="", show_default=False)
            pane_text = tmux.capture_pane(f"{current_tmux}:{window_label}", lines=200)
        elif not quiet:
            typer.echo("Login completed. Returning to onboarding.")
        tmux.run("kill-window", "-t", f"{current_tmux}:{window_label}", check=False)
        return pane_text

    if tmux.has_session(temp_session):
        tmux.kill_session(temp_session)
    if not quiet:
        typer.echo("Complete login in that tmux session. PollyPM will return here automatically.")
    tmux.create_session(
        temp_session,
        "login",
        _build_login_shell(
            provider,
            home,
            interactive=(provider is ProviderKind.CLAUDE),
            force_fresh_auth=force_fresh_auth,
            preferences=preferences,
        ),
        remain_on_exit=False,
    )
    watch_result: dict[str, str | bool] = {"pane_text": "", "completed": False}

    def _watch_for_completion() -> None:
        completed, pane_text = _wait_for_login_completion(
            tmux,
            target=f"{temp_session}:0",
            provider=provider,
            home=home,
            allow_existing_auth_shortcut=allow_existing_auth_shortcut,
        )
        watch_result["completed"] = completed
        watch_result["pane_text"] = pane_text
        if completed and tmux.has_session(temp_session):
            tmux.kill_session(temp_session)

    watcher = threading.Thread(target=_watch_for_completion, daemon=True)
    watcher.start()
    raise_code = tmux.attach_session(temp_session)
    watcher.join(timeout=1.0)
    if raise_code != 0:
        if tmux.has_session(temp_session):
            tmux.kill_session(temp_session)
        raise LoginCancelled("Login cancelled. Returned to onboarding.")

    pane_text = str(watch_result.get("pane_text") or "")
    if tmux.has_session(temp_session):
        if not pane_text:
            pane_text = tmux.capture_pane(f"{temp_session}:0", lines=200)
        tmux.kill_session(temp_session)
    return pane_text


def _connect_account_via_tmux(
    tmux: TmuxClient,
    *,
    root_dir: Path,
    provider: ProviderKind,
    index: int,
    quiet: bool = False,
    preferences: LoginPreferences | None = None,
) -> ConnectedAccount:
    label = "Codex" if provider is ProviderKind.CODEX else "Claude"
    home = _runtime_home(root_dir, provider, index)
    temp_window = f"onboard-{provider.value}-{index}"

    if not quiet:
        typer.echo("")
        typer.echo(f"Opening a temporary login window for {label} account #{index}.")
    pane_text = _run_login_window(
        tmux,
        provider=provider,
        home=home,
        window_label=temp_window,
        quiet=quiet,
        preferences=preferences,
    )

    email = _detect_email_from_pane(provider, pane_text) or _detect_account_email(provider, home)
    if provider is ProviderKind.CLAUDE and email is None:
        message = (
            "Claude login finished, but the managed PollyPM profile is still not authenticated. "
            "This usually means Claude completed browser auth without persisting credentials into the "
            "managed CLAUDE_CONFIG_DIR profile."
        )
        if quiet:
            raise typer.BadParameter(message)
        raise typer.BadParameter(message)
    if email is None:
        if quiet:
            raise typer.BadParameter(
                "PollyPM could not auto-detect the connected account email. "
                "Try running the login again and, for Codex, open `/status` once login completes."
            )
        typer.echo("")
        typer.echo("PollyPM could not auto-detect the account email from the completed login.")
        email = typer.prompt(f"Email address for this {label} account").strip().lower()

    if provider is ProviderKind.CLAUDE:
        # Claude auth lives in the macOS Keychain, keyed to the CLAUDE_CONFIG_DIR path hash.
        # Renaming the home directory would invalidate the keychain entry, so keep it in place.
        final_home = home
        _prime_claude_home(final_home)
    else:
        final_home = _promote_onboarding_home(
            home,
            _final_account_home(root_dir, _slugify_email(provider, email)),
        )

    return ConnectedAccount(
        provider=provider,
        email=email,
        account_name=_slugify_email(provider, email),
        home=final_home,
    )


def _resolve_account_identifier(config: PollyPMConfig, identifier: str) -> tuple[str, AccountConfig]:
    if identifier in config.accounts:
        return identifier, config.accounts[identifier]

    lowered = identifier.strip().lower()
    matches = [
        (name, account)
        for name, account in config.accounts.items()
        if account.email and account.email.lower() == lowered
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise typer.BadParameter(f"Multiple accounts matched {identifier}; use the internal account key instead.")
    raise typer.BadParameter(f"Unknown account: {identifier}")


def build_onboarded_config(
    *,
    root_dir: Path,
    accounts: dict[str, ConnectedAccount],
    controller_account: str,
    open_permissions_by_default: bool = True,
    failover_enabled: bool,
    failover_accounts: list[str],
    projects: dict[str, KnownProject] | None = None,
) -> PollyPMConfig:
    if controller_account not in accounts:
        raise ValueError(f"Unknown controller account: {controller_account}")

    controller = accounts[controller_account]
    base_dir = root_dir / ".pollypm-state"

    config_accounts = {
        name: AccountConfig(
            name=account.account_name,
            provider=account.provider,
            email=account.email,
            home=account.home,
        )
        for name, account in accounts.items()
    }

    return PollyPMConfig(
        project=ProjectSettings(
            name="PollyPM",
            root_dir=root_dir,
            tmux_session="pollypm",
            workspace_root=DEFAULT_WORKSPACE_ROOT,
            base_dir=base_dir,
            logs_dir=base_dir / "logs",
            snapshots_dir=base_dir / "snapshots",
            state_db=base_dir / "state.db",
        ),
        pollypm=PollyPMSettings(
            controller_account=controller_account,
            open_permissions_by_default=open_permissions_by_default,
            failover_enabled=failover_enabled,
            failover_accounts=failover_accounts,
        ),
        accounts=config_accounts,
        sessions={
            "heartbeat": SessionConfig(
                name="heartbeat",
                role="heartbeat-supervisor",
                provider=controller.provider,
                account=controller.account_name,
                cwd=root_dir,
                project="pollypm",
                window_name="pm-heartbeat",
                prompt=heartbeat_prompt(),
                agent_profile="heartbeat",
                args=default_control_args(controller.provider, open_permissions=open_permissions_by_default, role="heartbeat-supervisor"),
            ),
            "operator": SessionConfig(
                name="operator",
                role="operator-pm",
                provider=controller.provider,
                account=controller.account_name,
                cwd=root_dir,
                project="pollypm",
                window_name="pm-operator",
                prompt=polly_prompt(),
                agent_profile="polly",
                args=default_control_args(controller.provider, open_permissions=open_permissions_by_default, role="operator-pm"),
            ),
        },
        projects=dict(projects or {}),
    )


def run_onboarding(config_path: Path = DEFAULT_CONFIG_PATH, force: bool = False) -> OnboardingResult:
    from pollypm.onboarding_tui import run_onboarding_app

    return run_onboarding_app(config_path=config_path, force=force)


def relogin_account(config_path: Path, identifier: str) -> tuple[str, str]:
    config = load_config(config_path)
    account_name, account = _resolve_account_identifier(config, identifier)
    if account.home is None:
        raise typer.BadParameter(f"Account {account_name} does not have an isolated home configured.")

    tmux = create_tmux_client()
    typer.echo(f"Re-launching login for {account.email or account_name} [{account.provider.value}]")
    pane_text = _run_login_window(
        tmux,
        provider=account.provider,
        home=account.home,
        window_label=f"relogin-{account_name}",
    )

    detected_email = _detect_email_from_pane(account.provider, pane_text) or _detect_account_email(
        account.provider,
        account.home,
    )
    if detected_email and detected_email != (account.email or "").lower():
        config.accounts[account_name].email = detected_email
        write_config(config, path=config_path, force=True)
        return account_name, detected_email

    return account_name, account.email or detected_email or account_name
