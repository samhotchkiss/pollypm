from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal


class ProviderKind(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"


class RuntimeKind(StrEnum):
    LOCAL = "local"
    DOCKER = "docker"


class ProjectKind(StrEnum):
    GIT = "git"
    FOLDER = "folder"


# Canonical set of "control-plane" session roles — they coordinate the
# workspace (operator, reviewer, heartbeat supervisor, triage) rather
# than execute project work (architect, worker). Historically this set
# was redeclared in ~6 modules; exporting it once here avoids drift
# when a new control role lands. See #763 / #765 follow-ups.
CONTROL_ROLES: frozenset[str] = frozenset({
    "heartbeat-supervisor",
    "operator-pm",
    "reviewer",
    "triage",
})


@dataclass(slots=True)
class ProjectSettings:
    name: str = "PollyPM"
    root_dir: Path = Path(".")
    tmux_session: str = "pollypm"
    workspace_root: Path = Path.home() / "dev"
    base_dir: Path = Path(".pollypm")
    logs_dir: Path = Path(".pollypm/logs")
    snapshots_dir: Path = Path(".pollypm/snapshots")
    state_db: Path = Path(".pollypm/state.db")


@dataclass(slots=True)
class ModelAssignment:
    alias: str | None = None
    provider: str | None = None
    model: str | None = None

    def __post_init__(self) -> None:
        has_alias = isinstance(self.alias, str) and bool(self.alias.strip())
        has_pair = (
            isinstance(self.provider, str)
            and bool(self.provider.strip())
            and isinstance(self.model, str)
            and bool(self.model.strip())
        )
        if has_alias == has_pair:
            raise ValueError(
                "ModelAssignment requires exactly one of alias or provider/model."
            )


@dataclass(slots=True)
class KnownProject:
    key: str
    path: Path
    name: str | None = None
    persona_name: str | None = None
    kind: ProjectKind = ProjectKind.FOLDER
    tracked: bool = False
    role_assignments: dict[str, ModelAssignment] = field(default_factory=dict)
    # #768 auto-claim overrides. ``None`` means "defer to planner defaults".
    auto_claim: bool | None = None
    max_concurrent_workers: int | None = None
    # #1737 per-task parallel worker cap. When set, ``provision_worker``
    # refuses to spawn a new per-task worker once the project already has
    # this many active worker sessions. ``None`` defers to the default
    # (``DEFAULT_MAX_PARALLEL_WORKERS``, currently 5). This is distinct
    # from ``max_concurrent_workers`` (which the auto-claim sweep uses to
    # rate-limit DB-level claims): the cap here is the hard ceiling on
    # concurrently-spawned worker tmux windows per project, regardless of
    # how the claim was initiated.
    max_parallel_workers: int | None = None
    # Per-project override of ``[planner].enforce_plan``. ``None`` defers
    # to the global setting; ``False`` bypasses the plan-presence gate
    # for this project (single-task / cleanup / one-off work that doesn't
    # warrant a multi-stage plan); ``True`` re-enables the gate even
    # when the global default has been turned off.
    enforce_plan: bool | None = None

    def display_label(self) -> str:
        return self.name or self.key


ProjectConfig = KnownProject


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
    release_channel: Literal["stable", "beta"] = "stable"
    role_assignments: dict[str, ModelAssignment] = field(default_factory=dict)


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
class LoggingSettings:
    """Log-file hygiene configuration from the ``[logging]`` TOML section.

    Controls the hourly ``log.rotate`` recurring handler that keeps
    ``<logs_dir>/*.log`` files from growing unbounded. Writers (tmux
    ``pipe-pane``) keep appending to ``<name>.log``; the handler renames
    the file when it crosses the threshold, gzips the rotation, and
    truncates the original back to empty so the writer's next append
    starts at offset 0.

    ``rotate_size_mb`` — threshold in megabytes above which a log is
    rotated. Files at or below this size are left alone. Default 20 MB.
    ``rotate_keep`` — number of gzipped rotations to retain per base log
    name. Older ``.log.<ts>.gz`` files beyond this count are deleted.
    Default 3.
    """

    rotate_size_mb: int = 20
    rotate_keep: int = 3


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

    ``enforce_plan`` — when ``True`` (default), the ``task_assignment.sweep``
    handler refuses to delegate implementation tasks for projects that
    don't have an approved plan. Set to ``False`` to disable the gate
    globally (tests / migration). See issue #273.

    ``plan_dir`` — directory under each project root where the canonical
    forward plan lives. Defaults to ``"docs/plan"`` — the gate looks for
    ``<project>/<plan_dir>/plan.md``. Absolute paths are honoured as-is;
    relative paths resolve against the project root. See issue #273.

    Note: this section is distinct from ``[planner.budgets]`` which is
    consumed directly out of raw TOML by ``budgets.py``; the two live
    under the same top-level key but serve different layers.
    """

    auto_on_project_created: bool = True
    enforce_plan: bool = True
    plan_dir: str = "docs/plan"
    # #768: when True (default), the task_assignment sweep auto-claims
    # queued worker-role tasks for any project with an approved plan,
    # up to ``max_concurrent_per_project`` workers per project. Set to
    # False to require manual ``pm task claim``. Per-project opt-out is
    # also available via ``[projects.<key>].auto_claim = false``.
    auto_claim: bool = True
    # Default worker-concurrency cap per project. Bounds the number of
    # parallel Claude sessions a single project can burn overnight.
    # Per-project override via ``[projects.<key>].max_concurrent_workers``.
    max_concurrent_per_project: int = 2


@dataclass(slots=True)
class EventsRetentionSettings:
    """Tiered retention windows for the ``events`` table.

    Each tier's retention window is measured in days. The actual
    event_type → tier mapping lives in
    ``pollypm.storage.events_retention`` and is authoritative — this
    dataclass only tunes the windows. See issue context in
    ``core_recurring/plugin.py::events_retention_sweep_handler``.
    """

    audit_retention_days: int = 365
    operational_retention_days: int = 30
    high_volume_retention_days: int = 7
    default_retention_days: int = 30


@dataclass(slots=True)
class PgStorageSettings:
    """``[storage.pg]`` knobs — pool sizing + DSN override (issue #1737).

    Slice A wires these as config-driven defaults; the
    :mod:`pollypm.storage.pg_pool` module reads them when constructing
    the process-wide pool. ``dsn`` is overridden by the
    ``POLLYPM_PG_DSN`` env var (operator-level escape valve).

    ``min_version`` is consulted by ``pm doctor pg-connection`` to
    refuse to proceed against an older pg instance; pgvector's HNSW
    behaviour is robust from pg 16 onward.
    """

    pool_min: int = 1
    pool_max: int = 10
    min_version: str = "16.0"
    dsn: str = ""


@dataclass(slots=True)
class EmbeddingScoreWeights:
    """Hybrid-recall scoring knobs (#1737, Slice D).

    The pg memory recall layer blends four components into one
    final score:

    * ``semantic`` — cosine similarity from pgvector
    * ``fts`` — Postgres ``ts_rank_cd`` against the generated
      ``tsvector`` columns
    * ``importance`` — ``memory_entries.importance`` normalised to
      ``[0, 1]``
    * ``recency`` — exponential decay over ``created_at`` age

    Weights are non-negative; the recall query normalises them to sum
    to 1 before blending so an operator can leave one knob alone.
    Defaults mirror the design doc at #1737 §3.4.
    """

    semantic: float = 0.5
    fts: float = 0.25
    importance: float = 0.15
    recency: float = 0.1


@dataclass(slots=True)
class EmbeddingSettings:
    """``[storage.embedding]`` knobs — embedding provider for pgvector.

    Slice A laid the dataclass shape; Slice D wires the writer +
    recall path. Knobs:

    ``model`` — provider-namespaced model name (``openai:text-embedding-3-small``).
    ``provider`` — short id used by the resolver (``openai``, ``ollama``, ...).
    ``api_key_env`` — name of the env var that holds the provider's
        API key. Defaults to ``OPENAI_API_KEY``.
    ``score_weights`` — hybrid-recall blend weights (#1737 Slice D).
    """

    model: str = "openai:text-embedding-3-small"
    provider: str = "openai"
    api_key_env: str = "OPENAI_API_KEY"
    score_weights: EmbeddingScoreWeights = field(
        default_factory=EmbeddingScoreWeights,
    )


@dataclass(slots=True)
class StorageSettings:
    """Storage-backend selection from the ``[storage]`` TOML section.

    PollyPM loads its persistent-state backend via the
    ``pollypm.store_backend`` entry-point group (issue #343). This
    dataclass holds the config values the resolver feeds into
    :func:`pollypm.store.registry.get_store`.

    ``backend`` — entry-point name. ``"postgres"`` is the canonical
    default after the #1737 cutover (issue #1939); ``"sqlite"`` remains
    registered for explicit opt-in (tests, legacy migration). Third-party
    packages can register additional names.
    ``url`` — SQLAlchemy URL passed to the backend constructor. Empty
    string means "use the backend's own default" — for ``postgres`` that
    is the DSN resolved by :func:`pollypm.storage.pg_pool.resolve_dsn`;
    for ``sqlite`` it derives ``sqlite:///<resolved-state-db-path>``.

    ``pg`` / ``embedding`` — sub-section knobs for the Postgres backend
    (#1737, Slice A). Defaults are safe to read regardless of backend
    selection — they're only consulted when the backend resolves to
    postgres or the embedding writer fires.
    """

    backend: str = "postgres"
    url: str = ""
    pg: PgStorageSettings = field(default_factory=PgStorageSettings)
    embedding: EmbeddingSettings = field(default_factory=EmbeddingSettings)


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
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    events: EventsRetentionSettings = field(
        default_factory=EventsRetentionSettings,
    )
    storage: StorageSettings = field(default_factory=StorageSettings)


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
