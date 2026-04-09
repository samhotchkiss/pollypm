from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class ProviderKind(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"


class RuntimeKind(StrEnum):
    LOCAL = "local"
    DOCKER = "docker"


class ProjectKind(StrEnum):
    GIT = "git"
    FOLDER = "folder"


@dataclass(slots=True)
class ProjectSettings:
    name: str = "PollyPM"
    root_dir: Path = Path(".")
    tmux_session: str = "pollypm"
    workspace_root: Path = Path.home() / "dev"
    base_dir: Path = Path(".promptmaster")
    logs_dir: Path = Path(".promptmaster/logs")
    snapshots_dir: Path = Path(".promptmaster/snapshots")
    state_db: Path = Path(".promptmaster/state.db")


@dataclass(slots=True)
class KnownProject:
    key: str
    path: Path
    name: str | None = None
    kind: ProjectKind = ProjectKind.FOLDER
    tracked: bool = False


@dataclass(slots=True)
class AccountConfig:
    name: str
    provider: ProviderKind
    email: str | None = None
    runtime: RuntimeKind = RuntimeKind.LOCAL
    home: Path | None = None
    env: dict[str, str] = field(default_factory=dict)
    docker_image: str | None = None
    docker_extra_args: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SessionConfig:
    name: str
    role: str
    provider: ProviderKind
    account: str
    cwd: Path
    project: str = "promptmaster"
    prompt: str | None = None
    agent_profile: str | None = None
    args: list[str] = field(default_factory=list)
    enabled: bool = True
    window_name: str | None = None


@dataclass(slots=True)
class PromptMasterSettings:
    controller_account: str
    open_permissions_by_default: bool = True
    failover_enabled: bool = False
    failover_accounts: list[str] = field(default_factory=list)
    heartbeat_backend: str = "local"
    scheduler_backend: str = "inline"


@dataclass(slots=True)
class MemorySettings:
    backend: str = "file"


@dataclass(slots=True)
class PromptMasterConfig:
    project: ProjectSettings
    promptmaster: PromptMasterSettings
    accounts: dict[str, AccountConfig]
    sessions: dict[str, SessionConfig]
    projects: dict[str, KnownProject] = field(default_factory=dict)
    memory: MemorySettings = field(default_factory=MemorySettings)


@dataclass(slots=True)
class SessionLaunchSpec:
    session: SessionConfig
    account: AccountConfig
    window_name: str
    log_path: Path
    command: str
    resume_marker: Path | None = None
    initial_input: str | None = None
    fresh_launch_marker: Path | None = None
