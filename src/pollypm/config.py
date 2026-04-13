from __future__ import annotations

import tomllib
from pathlib import Path

from pollypm.agent_profiles.builtin import heartbeat_prompt, polly_prompt
from pollypm.models import (
    AccountConfig,
    MemorySettings,
    KnownProject,
    ProjectKind,
    ProjectSettings,
    PollyPMConfig,
    PollyPMSettings,
    ProviderKind,
    RuntimeKind,
    SessionConfig,
)


GLOBAL_CONFIG_DIR = Path.home() / ".pollypm"
DEFAULT_CONFIG_PATH = GLOBAL_CONFIG_DIR / "pollypm.toml"
PROJECT_CONFIG_DIRNAME = ".pollypm/config"
PROJECT_CONFIG_FILENAME = "project.toml"


def _normalize_project_display_name(key: str, name: str | None) -> str | None:
    if key == "pollypm" and (name is None or name.strip().casefold() == "pollypm"):
        return "PollyPM"
    return name


def _normalize_tmux_session_name(name: str | None) -> str:
    if name is None:
        return "pollypm"
    normalized = name.strip()
    if normalized in {"", "pollypm"}:
        return "pollypm"
    return normalized


def _normalize_session_prompt(session_name: str, prompt: str | None) -> str | None:
    if prompt is None:
        return None
    if session_name == "heartbeat" and "You are PollyPM session 0," in prompt:
        return heartbeat_prompt()
    if session_name == "operator" and "You are Polly, the PollyPM project manager, in session 1." in prompt:
        return polly_prompt()
    return prompt


def _resolve_path(base: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return base / path


def _format_path(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def project_config_path(project_root: Path) -> Path:
    return project_root / PROJECT_CONFIG_DIRNAME / PROJECT_CONFIG_FILENAME


def _load_raw_toml(path: Path) -> dict[str, object]:
    try:
        return tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise
    except Exception as exc:
        raise RuntimeError(
            f"Failed to parse config at {path}: {exc}\n"
            f"The config file may be corrupted. Try `pm repair` to regenerate defaults, "
            f"or check the file manually for syntax errors."
        ) from exc


def _parse_project_settings(raw: dict[str, object], *, base: Path) -> ProjectSettings:
    project_raw = raw.get("project", {})
    if not isinstance(project_raw, dict):
        project_raw = {}
    base_dir = _resolve_path(base, str(project_raw.get("base_dir", str(GLOBAL_CONFIG_DIR))))
    return ProjectSettings(
        name=_normalize_project_display_name("pollypm", project_raw.get("name")) or "PollyPM",
        root_dir=base,
        tmux_session=_normalize_tmux_session_name(project_raw.get("tmux_session")),
        workspace_root=_resolve_path(base, str(project_raw.get("workspace_root", str(Path.home() / "dev")))),
        base_dir=base_dir,
        logs_dir=_resolve_path(base, str(project_raw.get("logs_dir", str(base_dir / "logs")))),
        snapshots_dir=_resolve_path(base, str(project_raw.get("snapshots_dir", str(base_dir / "snapshots")))),
        state_db=_resolve_path(base, str(project_raw.get("state_db", str(base_dir / "state.db")))),
    )


def _parse_accounts(raw: dict[str, object], *, base: Path) -> dict[str, AccountConfig]:
    accounts: dict[str, AccountConfig] = {}
    accounts_raw = raw.get("accounts", {})
    if not isinstance(accounts_raw, dict):
        return accounts
    for account_name, account_raw in accounts_raw.items():
        if not isinstance(account_raw, dict):
            continue
        accounts[account_name] = AccountConfig(
            name=account_name,
            provider=ProviderKind(account_raw["provider"]),
            email=account_raw.get("email"),
            runtime=RuntimeKind(account_raw.get("runtime", "local")),
            home=_resolve_path(base, account_raw["home"]) if account_raw.get("home") else None,
            env={str(key): str(value) for key, value in account_raw.get("env", {}).items()},
            docker_image=account_raw.get("docker_image"),
            docker_extra_args=[str(arg) for arg in account_raw.get("docker_extra_args", [])],
        )
    return accounts


def _parse_sessions(
    raw: dict[str, object],
    *,
    base: Path,
    default_project: str = "pollypm",
    workspace_root: Path | None = None,
) -> dict[str, SessionConfig]:
    _CONTROL_ROLES = {"heartbeat-supervisor", "operator-pm", "triage"}
    sessions: dict[str, SessionConfig] = {}
    sessions_raw = raw.get("sessions", {})
    if not isinstance(sessions_raw, dict):
        return sessions
    for session_name, session_raw in sessions_raw.items():
        if not isinstance(session_raw, dict):
            continue
        role = session_raw["role"]
        raw_cwd = session_raw.get("cwd", ".")
        # Control sessions (heartbeat, operator) with cwd="." should resolve
        # to the workspace root so they can see project files and issue
        # trackers, rather than being stuck in the config base directory.
        if raw_cwd == "." and role in _CONTROL_ROLES and workspace_root is not None:
            cwd = workspace_root
        else:
            cwd = _resolve_path(base, raw_cwd)
        sessions[session_name] = SessionConfig(
            name=session_name,
            role=role,
            provider=ProviderKind(session_raw["provider"]),
            account=session_raw["account"],
            cwd=cwd,
            project=session_raw.get("project", default_project),
            prompt=_normalize_session_prompt(session_name, session_raw.get("prompt")),
            agent_profile=session_raw.get("agent_profile"),
            args=[str(arg) for arg in session_raw.get("args", [])],
            enabled=bool(session_raw.get("enabled", True)),
            window_name=session_raw.get("window_name"),
        )
    return sessions


def _validate_timezone(tz: str) -> str:
    """Validate timezone is a valid IANA name. Returns empty string if invalid."""
    if not tz:
        return ""
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz)
        return tz
    except (KeyError, Exception):
        return ""  # Invalid timezone, fall back to auto-detect


def _parse_pollypm_settings(raw: dict[str, object], sessions: dict[str, SessionConfig]) -> PollyPMSettings:
    pollypm_raw = raw.get("pollypm", {})
    if not isinstance(pollypm_raw, dict):
        pollypm_raw = {}
    controller_account = pollypm_raw.get("controller_account")
    if controller_account is None:
        operator = sessions.get("operator")
        heartbeat = sessions.get("heartbeat")
        controller_account = operator.account if operator else (heartbeat.account if heartbeat else "")

    failover_accounts = [str(item) for item in pollypm_raw.get("failover_accounts", [])]
    failover_enabled = bool(pollypm_raw.get("failover_enabled", bool(failover_accounts)))
    open_permissions_by_default = bool(pollypm_raw.get("open_permissions_by_default", True))
    return PollyPMSettings(
        controller_account=controller_account,
        open_permissions_by_default=open_permissions_by_default,
        failover_enabled=failover_enabled,
        failover_accounts=failover_accounts,
        heartbeat_backend=str(pollypm_raw.get("heartbeat_backend", "local")),
        scheduler_backend=str(pollypm_raw.get("scheduler_backend", "inline")),
        lease_timeout_minutes=max(1, int(pollypm_raw.get("lease_timeout_minutes", 30))),
        timezone=_validate_timezone(str(pollypm_raw.get("timezone", ""))),
    )


def _parse_memory_settings(raw: dict[str, object]) -> MemorySettings:
    memory_raw = raw.get("memory", {})
    if not isinstance(memory_raw, dict):
        memory_raw = {}
    return MemorySettings(backend=str(memory_raw.get("backend", "file")))


def _parse_known_projects(raw: dict[str, object], *, base: Path) -> dict[str, KnownProject]:
    projects: dict[str, KnownProject] = {}
    projects_raw = raw.get("projects", {})
    if not isinstance(projects_raw, dict):
        return projects
    for project_key, item_raw in projects_raw.items():
        if not isinstance(item_raw, dict):
            continue
        projects[project_key] = KnownProject(
            key=project_key,
            path=_resolve_path(base, item_raw["path"]),
            name=_normalize_project_display_name(project_key, item_raw.get("name")),
            persona_name=item_raw.get("persona_name") if isinstance(item_raw.get("persona_name"), str) else None,
            kind=ProjectKind(item_raw.get("kind", "folder")),
            tracked=bool(item_raw.get("tracked", False)),
        )
    return projects


def _merge_project_local_config(
    sessions: dict[str, SessionConfig],
    projects: dict[str, KnownProject],
) -> None:
    for project_key, project in projects.items():
        local_path = project_config_path(project.path)
        if not local_path.exists():
            continue
        raw = _load_raw_toml(local_path)
        project_raw = raw.get("project", {})
        if isinstance(project_raw, dict):
            display_name = project_raw.get("display_name") or project_raw.get("name")
            if isinstance(display_name, str) and display_name.strip():
                project.name = display_name.strip()
            persona_name = project_raw.get("persona_name")
            if isinstance(persona_name, str) and persona_name.strip():
                project.persona_name = persona_name.strip()
        for session_name, session in _parse_sessions(raw, base=project.path, default_project=project_key).items():
            session.project = project_key
            # Project-local session config overrides global config for the same name
            sessions[session_name] = session


def _validate_cross_references(
    *,
    accounts: dict[str, AccountConfig],
    sessions: dict[str, SessionConfig],
    pollypm: PollyPMSettings,
) -> None:
    for session_name, session in sessions.items():
        if session.account and session.account not in accounts:
            raise ValueError(
                f"Session '{session_name}' references unknown account '{session.account}'. "
                f"Known accounts: {', '.join(accounts) or 'none'}"
            )
    if pollypm.controller_account and pollypm.controller_account not in accounts and accounts:
        raise ValueError(
            f"Controller account '{pollypm.controller_account}' not found. "
            f"Known accounts: {', '.join(accounts)}"
        )
    for account_name in pollypm.failover_accounts:
        if account_name not in accounts:
            raise ValueError(
                f"Failover account '{account_name}' not found. "
                f"Known accounts: {', '.join(accounts)}"
            )


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> PollyPMConfig:
    config_path = path.resolve()
    base = config_path.parent
    raw = _load_raw_toml(config_path)
    project = _parse_project_settings(raw, base=base)
    accounts = _parse_accounts(raw, base=base)
    sessions = _parse_sessions(raw, base=base, workspace_root=project.workspace_root)
    pollypm = _parse_pollypm_settings(raw, sessions)
    memory = _parse_memory_settings(raw)
    projects = _parse_known_projects(raw, base=base)
    _merge_project_local_config(sessions, projects)
    _validate_cross_references(accounts=accounts, sessions=sessions, pollypm=pollypm)

    return PollyPMConfig(
        project=project,
        pollypm=pollypm,
        accounts=accounts,
        sessions=sessions,
        projects=projects,
        memory=memory,
    )


def _render_global_config(config: PollyPMConfig) -> str:
    root = config.project.root_dir
    lines = [
        "[project]",
        f'name = "{config.project.name}"',
        f'tmux_session = "{config.project.tmux_session}"',
        f'workspace_root = "{_format_path(config.project.workspace_root, root)}"',
        f'base_dir = "{_format_path(config.project.base_dir, root)}"',
        f'logs_dir = "{_format_path(config.project.logs_dir, root)}"',
        f'snapshots_dir = "{_format_path(config.project.snapshots_dir, root)}"',
        f'state_db = "{_format_path(config.project.state_db, root)}"',
        "",
        "[pollypm]",
        f'controller_account = "{config.pollypm.controller_account}"',
        f"open_permissions_by_default = {'true' if config.pollypm.open_permissions_by_default else 'false'}",
        f"failover_enabled = {'true' if config.pollypm.failover_enabled else 'false'}",
        f'heartbeat_backend = "{config.pollypm.heartbeat_backend}"',
        f'scheduler_backend = "{config.pollypm.scheduler_backend}"',
        f"lease_timeout_minutes = {config.pollypm.lease_timeout_minutes}",
    ]
    if config.pollypm.timezone:
        lines.append(f'timezone = "{config.pollypm.timezone}"')
    if config.pollypm.failover_accounts:
        items = ", ".join(f'"{name}"' for name in config.pollypm.failover_accounts)
        lines.append(f"failover_accounts = [{items}]")
    lines.append("")

    lines.extend(
        [
            "[memory]",
            f'backend = "{config.memory.backend}"',
            "",
        ]
    )

    for account_name, account in config.accounts.items():
        lines.extend(
            [
                f"[accounts.{account_name}]",
                f'provider = "{account.provider.value}"',
            ]
        )
        if account.email:
            lines.append(f'email = "{account.email}"')
        if account.runtime is not RuntimeKind.LOCAL:
            lines.append(f'runtime = "{account.runtime.value}"')
        if account.home is not None:
            lines.append(f'home = "{_format_path(account.home, root)}"')
        if account.docker_image:
            lines.append(f'docker_image = "{account.docker_image}"')
        if account.docker_extra_args:
            items = ", ".join(f'"{arg}"' for arg in account.docker_extra_args)
            lines.append(f"docker_extra_args = [{items}]")
        if account.env:
            lines.append("[accounts.%s.env]" % account_name)
            for key, value in account.env.items():
                lines.append(f'{key} = "{value}"')
        lines.append("")

    for session_name, session in config.sessions.items():
        if session.role == "worker" and session.project != "pollypm":
            continue
        lines.extend(
            [
                f"[sessions.{session_name}]",
                f'role = "{session.role}"',
                f'provider = "{session.provider.value}"',
                f'account = "{session.account}"',
                f'cwd = "{_format_path(session.cwd, root)}"',
            ]
        )
        if session.project != "pollypm":
            lines.append(f'project = "{session.project}"')
        if session.window_name:
            lines.append(f'window_name = "{session.window_name}"')
        if session.prompt:
            if "\n" in session.prompt:
                escaped = session.prompt.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
                lines.append(f'prompt = """{escaped}"""')
            else:
                escaped = session.prompt.replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'prompt = "{escaped}"')
        if session.agent_profile:
            lines.append(f'agent_profile = "{session.agent_profile}"')
        if session.args:
            items = ", ".join(f'"{arg}"' for arg in session.args)
            lines.append(f"args = [{items}]")
        if not session.enabled:
            lines.append("enabled = false")
        lines.append("")

    for project_key, project in config.projects.items():
        lines.extend(
            [
                f"[projects.{project_key}]",
                f'path = "{_format_path(project.path, root)}"',
            ]
        )
        if project.name:
            lines.append(f'name = "{project.name}"')
        if project.persona_name:
            lines.append(f'persona_name = "{project.persona_name}"')
        if project.kind is not ProjectKind.FOLDER:
            lines.append(f'kind = "{project.kind.value}"')
        if project.tracked:
            lines.append("tracked = true")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _render_project_local_config(config: PollyPMConfig, project_key: str) -> str:
    project = config.projects[project_key]
    lines = [
        "[project]",
        f'display_name = "{project.name or project.key}"',
        f'persona_name = "{project.persona_name}"' if project.persona_name else None,
        "",
    ]
    lines = [line for line in lines if line is not None]

    for session_name, session in config.sessions.items():
        if session.role != "worker" or session.project != project_key:
            continue
        lines.extend(
            [
                f"[sessions.{session_name}]",
                f'role = "{session.role}"',
                f'provider = "{session.provider.value}"',
                f'account = "{session.account}"',
                f'cwd = "{_format_path(session.cwd, project.path)}"',
            ]
        )
        if session.window_name:
            lines.append(f'window_name = "{session.window_name}"')
        if session.prompt:
            if "\n" in session.prompt:
                escaped = session.prompt.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
                lines.append(f'prompt = """{escaped}"""')
            else:
                escaped = session.prompt.replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'prompt = "{escaped}"')
        if session.agent_profile:
            lines.append(f'agent_profile = "{session.agent_profile}"')
        if session.args:
            items = ", ".join(f'"{arg}"' for arg in session.args)
            lines.append(f"args = [{items}]")
        if not session.enabled:
            lines.append("enabled = false")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_config(config: PollyPMConfig) -> str:
    return _render_global_config(config)


def render_example_config() -> str:
    root = Path.cwd()
    return render_config(_build_example_config(root))


def _build_example_config(root: Path) -> PollyPMConfig:
    base_dir = root / ".pollypm-state"
    return PollyPMConfig(
        project=ProjectSettings(
            name="PollyPM",
            root_dir=root,
            tmux_session="pollypm",
            workspace_root=Path.home() / "dev",
            base_dir=base_dir,
            logs_dir=base_dir / "logs",
            snapshots_dir=base_dir / "snapshots",
            state_db=base_dir / "state.db",
        ),
        pollypm=PollyPMSettings(
            controller_account="codex_primary",
            open_permissions_by_default=True,
            failover_enabled=True,
            failover_accounts=["claude_primary"],
            heartbeat_backend="local",
            scheduler_backend="inline",
            lease_timeout_minutes=30,
        ),
        accounts={
            "codex_primary": AccountConfig(
                name="codex_primary",
                provider=ProviderKind.CODEX,
                email="codex@example.com",
                runtime=RuntimeKind.LOCAL,
                home=base_dir / "homes" / "codex_primary",
            ),
            "claude_primary": AccountConfig(
                name="claude_primary",
                provider=ProviderKind.CLAUDE,
                email="claude@example.com",
                runtime=RuntimeKind.LOCAL,
                home=base_dir / "homes" / "claude_primary",
            ),
        },
        sessions={
            "heartbeat": SessionConfig(
                name="heartbeat",
                role="heartbeat-supervisor",
                provider=ProviderKind.CODEX,
                account="codex_primary",
                cwd=root,
                project="pollypm",
                window_name="pm-heartbeat",
                prompt=heartbeat_prompt(),
                agent_profile="heartbeat",
            ),
            "operator": SessionConfig(
                name="operator",
                role="operator-pm",
                provider=ProviderKind.CODEX,
                account="codex_primary",
                cwd=root,
                project="pollypm",
                window_name="pm-operator",
                prompt=polly_prompt(),
                agent_profile="polly",
            ),
            "worker_demo": SessionConfig(
                name="worker_demo",
                role="worker",
                provider=ProviderKind.CLAUDE,
                account="claude_primary",
                cwd=root,
                project="demo-project",
                window_name="worker-demo",
                prompt="Inspect the repository and propose the next high-leverage implementation step.",
            ),
        },
        projects={
            "pollypm": KnownProject(
                key="pollypm",
                path=root,
                name="PollyPM",
                kind=ProjectKind.GIT if (root / ".git").exists() else ProjectKind.FOLDER,
            ),
        },
        memory=MemorySettings(backend="file"),
    )


def write_example_config(path: Path = DEFAULT_CONFIG_PATH, force: bool = False) -> Path:
    if path.exists() and not force:
        raise FileExistsError(f"Config already exists: {path}")
    root = path.resolve().parent
    return write_config(_build_example_config(root), path, force=True)


def write_config(config: PollyPMConfig, path: Path = DEFAULT_CONFIG_PATH, force: bool = False) -> Path:
    if path.exists() and not force:
        raise FileExistsError(f"Config already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_global_config(config))
    for project_key, project in config.projects.items():
        local_path = project_config_path(project.path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(_render_project_local_config(config, project_key))
    return path
