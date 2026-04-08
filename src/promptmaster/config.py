from __future__ import annotations

import tomllib
from pathlib import Path

from promptmaster.agent_profiles.builtin import heartbeat_prompt, polly_prompt
from promptmaster.models import (
    AccountConfig,
    MemorySettings,
    KnownProject,
    ProjectKind,
    ProjectSettings,
    PromptMasterConfig,
    PromptMasterSettings,
    ProviderKind,
    RuntimeKind,
    SessionConfig,
)


DEFAULT_CONFIG_PATH = Path("promptmaster.toml")


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


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> PromptMasterConfig:
    config_path = path.resolve()
    base = config_path.parent
    raw = tomllib.loads(config_path.read_text())

    project_raw = raw.get("project", {})
    base_dir = _resolve_path(base, project_raw.get("base_dir", ".promptmaster"))
    project = ProjectSettings(
        name=project_raw.get("name", "promptmaster"),
        root_dir=base,
        tmux_session=project_raw.get("tmux_session", "promptmaster"),
        workspace_root=_resolve_path(base, project_raw.get("workspace_root", str(Path.home() / "dev"))),
        base_dir=base_dir,
        logs_dir=_resolve_path(base, project_raw.get("logs_dir", str(base_dir / "logs"))),
        snapshots_dir=_resolve_path(base, project_raw.get("snapshots_dir", str(base_dir / "snapshots"))),
        state_db=_resolve_path(base, project_raw.get("state_db", str(base_dir / "state.db"))),
    )

    accounts: dict[str, AccountConfig] = {}
    for account_name, account_raw in raw.get("accounts", {}).items():
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

    sessions: dict[str, SessionConfig] = {}
    for session_name, session_raw in raw.get("sessions", {}).items():
        sessions[session_name] = SessionConfig(
            name=session_name,
            role=session_raw["role"],
            provider=ProviderKind(session_raw["provider"]),
            account=session_raw["account"],
            cwd=_resolve_path(base, session_raw.get("cwd", ".")),
            project=session_raw.get("project", "promptmaster"),
            prompt=session_raw.get("prompt"),
            agent_profile=session_raw.get("agent_profile"),
            args=[str(arg) for arg in session_raw.get("args", [])],
            enabled=bool(session_raw.get("enabled", True)),
            window_name=session_raw.get("window_name"),
        )

    promptmaster_raw = raw.get("promptmaster", {})
    controller_account = promptmaster_raw.get("controller_account")
    if controller_account is None:
        operator = sessions.get("operator")
        heartbeat = sessions.get("heartbeat")
        controller_account = operator.account if operator else (heartbeat.account if heartbeat else "")

    failover_accounts = [str(item) for item in promptmaster_raw.get("failover_accounts", [])]
    failover_enabled = bool(promptmaster_raw.get("failover_enabled", bool(failover_accounts)))
    open_permissions_by_default = bool(promptmaster_raw.get("open_permissions_by_default", True))
    promptmaster = PromptMasterSettings(
        controller_account=controller_account,
        open_permissions_by_default=open_permissions_by_default,
        failover_enabled=failover_enabled,
        failover_accounts=failover_accounts,
        heartbeat_backend=str(promptmaster_raw.get("heartbeat_backend", "local")),
        scheduler_backend=str(promptmaster_raw.get("scheduler_backend", "inline")),
    )

    memory_raw = raw.get("memory", {})
    memory = MemorySettings(backend=str(memory_raw.get("backend", "file")))

    projects: dict[str, KnownProject] = {}
    for project_key, item_raw in raw.get("projects", {}).items():
        projects[project_key] = KnownProject(
            key=project_key,
            path=_resolve_path(base, item_raw["path"]),
            name=item_raw.get("name"),
            kind=ProjectKind(item_raw.get("kind", "folder")),
            tracked=bool(item_raw.get("tracked", False)),
        )

    return PromptMasterConfig(
        project=project,
        promptmaster=promptmaster,
        accounts=accounts,
        sessions=sessions,
        projects=projects,
        memory=memory,
    )


def render_config(config: PromptMasterConfig) -> str:
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
        "[promptmaster]",
        f'controller_account = "{config.promptmaster.controller_account}"',
        f"open_permissions_by_default = {'true' if config.promptmaster.open_permissions_by_default else 'false'}",
        f"failover_enabled = {'true' if config.promptmaster.failover_enabled else 'false'}",
        f'heartbeat_backend = "{config.promptmaster.heartbeat_backend}"',
        f'scheduler_backend = "{config.promptmaster.scheduler_backend}"',
    ]
    if config.promptmaster.failover_accounts:
        items = ", ".join(f'"{name}"' for name in config.promptmaster.failover_accounts)
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
        lines.extend(
            [
                f"[sessions.{session_name}]",
                f'role = "{session.role}"',
                f'provider = "{session.provider.value}"',
                f'account = "{session.account}"',
                f'cwd = "{_format_path(session.cwd, root)}"',
            ]
        )
        if session.project != "promptmaster":
            lines.append(f'project = "{session.project}"')
        if session.window_name:
            lines.append(f'window_name = "{session.window_name}"')
        if session.prompt:
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
        if project.kind is not ProjectKind.FOLDER:
            lines.append(f'kind = "{project.kind.value}"')
        if project.tracked:
            lines.append("tracked = true")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_example_config() -> str:
    root = Path.cwd()
    base_dir = root / ".promptmaster"
    config = PromptMasterConfig(
        project=ProjectSettings(
            name="promptmaster",
            root_dir=root,
            tmux_session="promptmaster",
            workspace_root=Path.home() / "dev",
            base_dir=base_dir,
            logs_dir=base_dir / "logs",
            snapshots_dir=base_dir / "snapshots",
            state_db=base_dir / "state.db",
        ),
        promptmaster=PromptMasterSettings(
            controller_account="codex_primary",
            open_permissions_by_default=True,
            failover_enabled=True,
            failover_accounts=["claude_primary"],
            heartbeat_backend="local",
            scheduler_backend="inline",
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
                project="promptmaster",
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
                project="promptmaster",
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
            "promptmaster": KnownProject(
                key="promptmaster",
                path=root,
                name="Prompt Master",
                kind=ProjectKind.GIT if (root / ".git").exists() else ProjectKind.FOLDER,
            ),
        },
        memory=MemorySettings(backend="file"),
    )
    return render_config(config)


def write_example_config(path: Path = DEFAULT_CONFIG_PATH, force: bool = False) -> Path:
    if path.exists() and not force:
        raise FileExistsError(f"Config already exists: {path}")
    path.write_text(render_example_config())
    return path


def write_config(config: PromptMasterConfig, path: Path = DEFAULT_CONFIG_PATH, force: bool = False) -> Path:
    if path.exists() and not force:
        raise FileExistsError(f"Config already exists: {path}")
    path.write_text(render_config(config))
    return path
