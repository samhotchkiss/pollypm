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
    base_dir: Path = Path(".pollypm-state")
    logs_dir: Path = Path(".pollypm-state/logs")
    snapshots_dir: Path = Path(".pollypm-state/snapshots")
    state_db: Path = Path(".pollypm-state/state.db")


@dataclass(slots=True)
class KnownProject:
    key: str
    path: Path
    name: str | None = None
    persona_name: str | None = None
    kind: ProjectKind = ProjectKind.FOLDER
    tracked: bool = False

    def display_label(self) -> str:
        return self.name or self.key


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
    project: str = "pollypm"
    prompt: str | None = None
    agent_profile: str | None = None
    args: list[str] = field(default_factory=list)
    enabled: bool = True
    window_name: str | None = None


@dataclass(slots=True)
class PollyPMSettings:
    controller_account: str
    open_permissions_by_default: bool = True
    failover_enabled: bool = False
    failover_accounts: list[str] = field(default_factory=list)
    heartbeat_backend: str = "local"
    scheduler_backend: str = "inline"
    lease_timeout_minutes: int = 30
    timezone: str = ""  # IANA timezone (e.g. "America/Denver"). Empty = auto-detect.


@dataclass(slots=True)
class MemorySettings:
    backend: str = "file"


@dataclass(slots=True)
class PluginSettings:
    """Plugin-level configuration from the ``[plugins]`` TOML section.

    ``disabled`` — names of plugins that should be discovered but not
    loaded. User-global and project-local disables compose (project can
    disable more, not re-enable a user-disabled plugin). See
    docs/plugin-discovery-spec.md §8.
    """

    disabled: tuple[str, ...] = ()


@dataclass(slots=True)
class RailSettings:
    """User-level cockpit-rail customisation from the ``[rail]`` TOML
    section.

    ``hidden_items`` — list of ``"section.label"`` keys that should be
    skipped by the rail renderer (e.g. ``"tools.activity"``). Matched
    case-sensitively against each registration's ``item_key``.
    ``collapsed_sections`` — section names that start collapsed; the
    user can still expand them live during a cockpit session.

    See docs/extensible-rail-spec.md §6 and issue #224.
    """

    hidden_items: tuple[str, ...] = ()
    collapsed_sections: tuple[str, ...] = ()


@dataclass(slots=True)
class PlannerSettings:
    """Project-planner plugin configuration from the ``[planner]`` TOML
    section.

    ``auto_on_project_created`` — when ``True`` (default), the
    project_planning plugin's ``project.created`` observer auto-creates
    a ``plan_project`` flow task for every newly-registered project.
    Setting this to ``False`` suppresses auto-fire globally; the user
    can still run ``pm project plan <name>`` manually. Per-invocation
    opt-out is also available via ``--skip-plan`` on both
    ``pm add-project`` and ``pm project new``. See issue #255.

    Note: this section is distinct from ``[planner.budgets]`` which is
    consumed directly out of raw TOML by ``budgets.py``; the two live
    under the same top-level key but serve different layers.
    """

    auto_on_project_created: bool = True


@dataclass(slots=True)
class PollyPMConfig:
    project: ProjectSettings
    pollypm: PollyPMSettings
    accounts: dict[str, AccountConfig]
    sessions: dict[str, SessionConfig]
    projects: dict[str, KnownProject] = field(default_factory=dict)
    memory: MemorySettings = field(default_factory=MemorySettings)
    plugins: PluginSettings = field(default_factory=PluginSettings)
    rail: RailSettings = field(default_factory=RailSettings)
    planner: PlannerSettings = field(default_factory=PlannerSettings)


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
