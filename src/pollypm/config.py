from __future__ import annotations

import logging
import tomllib
from pathlib import Path

from pollypm.models import (
    AccountConfig,
    EventsRetentionSettings,
    LoggingSettings,
    MemorySettings,
    ModelAssignment,
    KnownProject,
    PlannerSettings,
    PluginSettings,
    ProjectKind,
    ProjectSettings,
    PollyPMConfig,
    PollyPMSettings,
    ProviderKind,
    RailSettings,
    RuntimeKind,
    SessionConfig,
    StorageSettings,
)


GLOBAL_CONFIG_DIR = Path.home() / ".pollypm"
DEFAULT_CONFIG_PATH = GLOBAL_CONFIG_DIR / "pollypm.toml"
PROJECT_CONFIG_DIRNAME = ".pollypm/config"
PROJECT_CONFIG_FILENAME = "project.toml"

_VALID_RELEASE_CHANNELS = ("stable", "beta")
_GLOBAL_ROLE_ASSIGNMENT_KEYS = (
    "operator_pm",
    "architect",
    "worker",
    "reviewer",
)
_PROJECT_ROLE_ASSIGNMENT_KEYS = (
    "architect",
    "worker",
    "reviewer",
)

_log = logging.getLogger(__name__)


def _validate_release_channel(value: object) -> str:
    """Coerce a raw config value into one of the valid release channels.

    Invariant: any unknown or non-string value falls back to ``"stable"``
    with a warning — a fat-fingered channel must never brick the CLI.
    See issue #713.
    """
    if isinstance(value, str) and value in _VALID_RELEASE_CHANNELS:
        return value
    if value is not None:
        _log.warning(
            "Invalid release_channel %r in pollypm.toml; falling back to 'stable'.",
            value,
        )
    return "stable"


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
        return _heartbeat_prompt()
    if session_name == "operator" and "You are Polly, the PollyPM project manager, in session 1." in prompt:
        return _polly_prompt()
    return prompt


def _heartbeat_prompt() -> str:
    from pollypm.plugins_builtin.core_agent_profiles.profiles import heartbeat_prompt

    return heartbeat_prompt()


def _polly_prompt() -> str:
    from pollypm.plugins_builtin.core_agent_profiles.profiles import polly_prompt

    return polly_prompt()


def _resolve_path(base: Path, raw_path: str) -> Path:
    # Global configs live in ``~/.pollypm/pollypm.toml``. Older renders
    # wrote sibling paths as ``.pollypm/...`` relative to that same
    # directory, which reloads as a bogus nested ``~/.pollypm/.pollypm``.
    if base.name == GLOBAL_CONFIG_DIR.name:
        rel = Path(raw_path)
        if not rel.is_absolute() and rel.parts[:1] == (GLOBAL_CONFIG_DIR.name,):
            raw_path = str(Path(*rel.parts[1:])) if len(rel.parts) > 1 else "."
    path = Path(raw_path)
    if path.is_absolute():
        return path
    resolved = (base / path).resolve()
    # Block path traversal: relative paths must stay within base
    try:
        resolved.relative_to(base.resolve())
    except ValueError:
        raise ValueError(f"Path '{raw_path}' escapes base directory '{base}'")
    return resolved


def _toml_str(value: str) -> str:
    """Escape a string for TOML double-quoted format."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _clean_optional_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _parse_role_assignments(
    raw_roles: object,
    *,
    allowed_roles: tuple[str, ...],
    scope_label: str,
) -> dict[str, ModelAssignment]:
    if not isinstance(raw_roles, dict):
        return {}
    assignments: dict[str, ModelAssignment] = {}
    allowed = set(allowed_roles)
    for role_name, role_raw in raw_roles.items():
        if not isinstance(role_name, str):
            _log.warning(
                "Dropping invalid role assignment under %s: role key %r is not a string.",
                scope_label,
                role_name,
            )
            continue
        if role_name not in allowed:
            if role_name == "operator_pm" and "project" in scope_label:
                reason = "operator_pm is global-only"
            else:
                reason = "unknown role"
            _log.warning(
                "Dropping invalid role assignment for %s.%s: %s.",
                scope_label,
                role_name,
                reason,
            )
            continue
        if not isinstance(role_raw, dict):
            _log.warning(
                "Dropping invalid role assignment for %s.%s: entry must be a table.",
                scope_label,
                role_name,
            )
            continue
        alias = _clean_optional_str(role_raw.get("alias"))
        provider = _clean_optional_str(role_raw.get("provider"))
        model = _clean_optional_str(role_raw.get("model"))
        if alias is not None and (provider is not None or model is not None):
            _log.warning(
                "Dropping invalid role assignment for %s.%s: alias and provider/model cannot both be set.",
                scope_label,
                role_name,
            )
            continue
        if alias is None and provider is None and model is None:
            _log.warning(
                "Dropping invalid role assignment for %s.%s: empty assignment.",
                scope_label,
                role_name,
            )
            continue
        if provider is None or model is None:
            if alias is None:
                _log.warning(
                    "Dropping invalid role assignment for %s.%s: provider and model must both be set.",
                    scope_label,
                    role_name,
                )
                continue
        try:
            assignments[role_name] = ModelAssignment(
                alias=alias,
                provider=provider,
                model=model,
            )
        except ValueError:
            _log.warning(
                "Dropping invalid role assignment for %s.%s: assignment must be alias-only or provider/model-only.",
                scope_label,
                role_name,
            )
    return assignments


def _format_path(path: Path, root: Path) -> str:
    if root.resolve().name == GLOBAL_CONFIG_DIR.name:
        legacy_nested_root = root.resolve() / GLOBAL_CONFIG_DIR.name
        try:
            return str(path.resolve().relative_to(legacy_nested_root))
        except ValueError:
            pass
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def project_config_path(project_root: Path) -> Path:
    return project_root / PROJECT_CONFIG_DIRNAME / PROJECT_CONFIG_FILENAME


def resolve_config_path(path: Path = DEFAULT_CONFIG_PATH) -> Path:
    """Resolve the effective config path.

    Always uses the global config at ~/.pollypm/pollypm.toml.
    Project-specific overrides are loaded separately via
    _merge_project_local_config (reads .pollypm/config/project.toml
    from each registered project directory).
    """
    if path != DEFAULT_CONFIG_PATH:
        return path.resolve()
    return DEFAULT_CONFIG_PATH.resolve()


def _load_raw_toml(path: Path) -> dict[str, object]:
    try:
        # TOML is UTF-8 by spec; pin the read encoding to match the
        # write side (``write_config``) so a non-UTF-8 default locale
        # can't mangle non-ASCII project names / personas on read.
        return tomllib.loads(path.read_text(encoding="utf-8"))
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
    # #763: detect per-project configs at ``<root>/.pollypm/config/project.toml``
    # and default base_dir + root_dir into the project's own ``.pollypm/``
    # rather than leaking into the shared ``~/.pollypm/``. Global configs
    # (``~/.pollypm/pollypm.toml``) keep the pre-existing behavior.
    if base.name == "config" and base.parent.name == ".pollypm":
        default_base_dir = base.parent
        root_dir = base.parent.parent
    else:
        default_base_dir = GLOBAL_CONFIG_DIR
        root_dir = base
    base_dir = _resolve_path(base, str(project_raw.get("base_dir", str(default_base_dir))))
    return ProjectSettings(
        name=_normalize_project_display_name("pollypm", project_raw.get("name")) or "PollyPM",
        root_dir=root_dir,
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
        release_channel=_validate_release_channel(pollypm_raw.get("release_channel")),
        role_assignments=_parse_role_assignments(
            pollypm_raw.get("roles", {}),
            allowed_roles=_GLOBAL_ROLE_ASSIGNMENT_KEYS,
            scope_label="pollypm.roles",
        ),
    )


def _parse_memory_settings(raw: dict[str, object]) -> MemorySettings:
    memory_raw = raw.get("memory", {})
    if not isinstance(memory_raw, dict):
        memory_raw = {}
    return MemorySettings(backend=str(memory_raw.get("backend", "file")))


def _parse_rail_settings(raw: dict[str, object]) -> RailSettings:
    """Parse the ``[rail]`` TOML section.

    See docs/extensible-rail-spec.md §6. Missing / malformed sections
    fall back to defaults — an invalid entry type never aborts config
    load.
    """
    rail_raw = raw.get("rail", {})
    if not isinstance(rail_raw, dict):
        return RailSettings()

    def _as_str_tuple(value: object) -> tuple[str, ...]:
        if not isinstance(value, list):
            return ()
        out: list[str] = []
        for entry in value:
            if isinstance(entry, str) and entry.strip():
                out.append(entry.strip())
        return tuple(dict.fromkeys(out))  # preserve order, de-dupe

    return RailSettings(
        hidden_items=_as_str_tuple(rail_raw.get("hidden_items", [])),
        collapsed_sections=_as_str_tuple(rail_raw.get("collapsed_sections", [])),
    )


def _parse_planner_settings(raw: dict[str, object]) -> PlannerSettings:
    """Parse the ``[planner]`` TOML section.

    Recognised keys:

    * ``auto_on_project_created`` — issue #255.
    * ``enforce_plan`` — issue #273. Gate-wide kill switch for the
      plan-presence sweep gate.
    * ``plan_dir`` — issue #273. Per-project relative path to the
      directory hosting ``plan.md``.

    Other nested tables under ``[planner]`` — for example
    ``[planner.budgets]`` — are consumed directly out of raw TOML by
    their own modules and ignored here. Unknown keys pass through
    silently for forward-compat.
    """
    planner_raw = raw.get("planner", {})
    if not isinstance(planner_raw, dict):
        return PlannerSettings()
    auto = planner_raw.get("auto_on_project_created", True)
    # Accept only real bools — anything else falls back to the default so
    # a fat-fingered config never silently disables the planner.
    if not isinstance(auto, bool):
        auto = True
    enforce = planner_raw.get("enforce_plan", True)
    if not isinstance(enforce, bool):
        enforce = True
    plan_dir_raw = planner_raw.get("plan_dir", "docs/plan")
    if not isinstance(plan_dir_raw, str) or not plan_dir_raw.strip():
        plan_dir_raw = "docs/plan"
    auto_claim_raw = planner_raw.get("auto_claim", True)
    auto_claim = bool(auto_claim_raw) if isinstance(auto_claim_raw, bool) else True
    max_conc_raw = planner_raw.get("max_concurrent_per_project", 2)
    max_conc = (
        int(max_conc_raw)
        if isinstance(max_conc_raw, int) and max_conc_raw > 0
        else 2
    )
    return PlannerSettings(
        auto_on_project_created=auto,
        enforce_plan=enforce,
        plan_dir=plan_dir_raw.strip(),
        auto_claim=auto_claim,
        max_concurrent_per_project=max_conc,
    )


def _parse_logging_settings(raw: dict[str, object]) -> LoggingSettings:
    """Parse the ``[logging]`` TOML section for log-rotation tuning.

    Fat-fingered values (non-int, negative) fall back to defaults so a
    malformed config never disables rotation silently — the handler
    still rotates on the 20 MB / 3-keep defaults. Missing section also
    yields defaults. See LoggingSettings docstring.
    """
    logging_raw = raw.get("logging", {})
    if not isinstance(logging_raw, dict):
        return LoggingSettings()
    size_raw = logging_raw.get("rotate_size_mb", 20)
    try:
        size_mb = int(size_raw)
    except (TypeError, ValueError):
        size_mb = 20
    if size_mb < 1:
        size_mb = 20
    keep_raw = logging_raw.get("rotate_keep", 3)
    try:
        keep = int(keep_raw)
    except (TypeError, ValueError):
        keep = 3
    if keep < 0:
        keep = 3
    return LoggingSettings(rotate_size_mb=size_mb, rotate_keep=keep)


def _parse_events_retention_settings(
    raw: dict[str, object],
) -> EventsRetentionSettings:
    """Parse the ``[events]`` TOML section.

    Recognised keys (all ints, measured in days):

    * ``audit_retention_days`` — default 365.
    * ``operational_retention_days`` — default 30.
    * ``high_volume_retention_days`` — default 7.
    * ``default_retention_days`` — default 30.

    Non-positive values or non-int types are silently coerced to the
    defaults — a fat-fingered config must never accidentally delete
    everything. See ``pollypm.storage.events_retention`` for tier
    membership and the ``events.retention_sweep`` handler for cadence.
    """
    events_raw = raw.get("events", {})
    if not isinstance(events_raw, dict):
        return EventsRetentionSettings()

    defaults = EventsRetentionSettings()

    def _as_positive_int(value: object, fallback: int) -> int:
        if isinstance(value, bool):
            # ``bool`` is a subclass of ``int`` — reject explicitly.
            return fallback
        if isinstance(value, int) and value > 0:
            return value
        return fallback

    return EventsRetentionSettings(
        audit_retention_days=_as_positive_int(
            events_raw.get("audit_retention_days"),
            defaults.audit_retention_days,
        ),
        operational_retention_days=_as_positive_int(
            events_raw.get("operational_retention_days"),
            defaults.operational_retention_days,
        ),
        high_volume_retention_days=_as_positive_int(
            events_raw.get("high_volume_retention_days"),
            defaults.high_volume_retention_days,
        ),
        default_retention_days=_as_positive_int(
            events_raw.get("default_retention_days"),
            defaults.default_retention_days,
        ),
    )


def _parse_storage_settings(
    raw: dict[str, object],
    *,
    project: ProjectSettings,
) -> StorageSettings:
    """Parse the ``[storage]`` TOML section (issue #343).

    Recognised keys:

    * ``backend`` — entry-point name under ``pollypm.store_backend``.
      Defaults to ``"sqlite"`` (registered by this package).
    * ``url`` — SQLAlchemy URL. When empty/missing, the resolver derives
      ``sqlite:///<project.state_db>`` so first-run users don't have to
      think about connection strings.

    Missing section yields defaults. Fat-fingered value types (non-str)
    silently fall back to defaults — a broken ``[storage]`` block must
    never brick the CLI on a config the user can fix with ``pm repair``.
    """
    storage_raw = raw.get("storage", {})
    if not isinstance(storage_raw, dict):
        storage_raw = {}
    backend_raw = storage_raw.get("backend", "sqlite")
    if not isinstance(backend_raw, str) or not backend_raw.strip():
        backend_raw = "sqlite"
    url_raw = storage_raw.get("url", "")
    if not isinstance(url_raw, str):
        url_raw = ""
    url_stripped = url_raw.strip()
    if not url_stripped:
        # Default-derive from the already-resolved state_db path. The
        # resolver downstream uses this URL verbatim, so emit an absolute
        # path that survives chdir.
        url_stripped = f"sqlite:///{project.state_db.resolve()}"
    return StorageSettings(
        backend=backend_raw.strip(),
        url=url_stripped,
    )


def _parse_plugin_settings(raw: dict[str, object]) -> PluginSettings:
    """Parse the ``[plugins]`` TOML section.

    Only ``disabled`` is recognised for now (per
    docs/plugin-discovery-spec.md §8). Unknown keys are ignored with a
    silent pass — future compat.
    """
    plugins_raw = raw.get("plugins", {})
    if not isinstance(plugins_raw, dict):
        return PluginSettings()
    disabled_raw = plugins_raw.get("disabled", [])
    if not isinstance(disabled_raw, list):
        return PluginSettings()
    disabled: list[str] = []
    for entry in disabled_raw:
        if isinstance(entry, str) and entry.strip():
            disabled.append(entry.strip())
    # Preserve declaration order but de-dupe.
    deduped = tuple(dict.fromkeys(disabled))
    return PluginSettings(disabled=deduped)


def _parse_known_projects(raw: dict[str, object], *, base: Path) -> dict[str, KnownProject]:
    projects: dict[str, KnownProject] = {}
    projects_raw = raw.get("projects", {})
    if not isinstance(projects_raw, dict):
        return projects
    for project_key, item_raw in projects_raw.items():
        if not isinstance(item_raw, dict):
            continue
        auto_claim_raw = item_raw.get("auto_claim")
        max_concurrent_raw = item_raw.get("max_concurrent_workers")
        projects[project_key] = KnownProject(
            key=project_key,
            path=_resolve_path(base, item_raw["path"]),
            name=_normalize_project_display_name(project_key, item_raw.get("name")),
            persona_name=item_raw.get("persona_name") if isinstance(item_raw.get("persona_name"), str) else None,
            kind=ProjectKind(item_raw.get("kind", "folder")),
            tracked=bool(item_raw.get("tracked", False)),
            role_assignments=_parse_role_assignments(
                item_raw.get("roles", {}),
                allowed_roles=_PROJECT_ROLE_ASSIGNMENT_KEYS,
                scope_label=f"projects.{project_key}.roles",
            ),
            auto_claim=bool(auto_claim_raw) if isinstance(auto_claim_raw, bool) else None,
            max_concurrent_workers=(
                int(max_concurrent_raw)
                if isinstance(max_concurrent_raw, int) and max_concurrent_raw > 0
                else None
            ),
        )
    return projects


def _merge_project_local_config(
    sessions: dict[str, SessionConfig],
    projects: dict[str, KnownProject],
    plugins: PluginSettings | None = None,
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
        # ``[planner].enforce_plan`` per-project override. Reading at the
        # project-local layer (rather than threading new TOML keys through
        # ``[projects.<key>]`` in the global config) keeps the bypass
        # discoverable in the project's own pollypm.toml.
        planner_raw = raw.get("planner", {})
        if isinstance(planner_raw, dict) and "enforce_plan" in planner_raw:
            project.enforce_plan = bool(planner_raw["enforce_plan"])
        _PROTECTED_SESSIONS = {"heartbeat", "operator"}
        for session_name, session in _parse_sessions(raw, base=project.path, default_project=project_key).items():
            if session_name in _PROTECTED_SESSIONS:
                continue  # Never allow project-local configs to override control sessions
            session.project = project_key
            sessions[session_name] = session
        # Compose plugin-disabled lists: project can disable more, not
        # re-enable user-disabled. See docs/plugin-discovery-spec.md §8.
        if plugins is not None:
            local_plugins = _parse_plugin_settings(raw)
            if local_plugins.disabled:
                merged = list(plugins.disabled)
                for name in local_plugins.disabled:
                    if name not in merged:
                        merged.append(name)
                plugins.disabled = tuple(merged)


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
    if pollypm.controller_account and pollypm.controller_account not in accounts:
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


_config_cache: dict[
    Path,
    tuple[float, "PollyPMConfig", dict[Path, float | None]],
] = {}


def _project_local_mtimes(
    config: "PollyPMConfig",
) -> dict[Path, float | None]:
    """Collect mtimes of every registered project's local config.

    The cache validity check below stats these paths on every
    ``load_config`` call so a project-local edit (e.g. flipping
    ``[planner].enforce_plan`` in ``<root>/.pollypm/config/project.toml``)
    invalidates the cached merge — without this, long-running processes
    (cockpit, sweeper) would silently keep serving the stale config
    until the global ``pollypm.toml`` is touched.

    Missing files map to ``None`` so a newly-created (or newly-deleted)
    local config still triggers a reload.
    """
    snapshot: dict[Path, float | None] = {}
    for project in config.projects.values():
        local_path = project_config_path(project.path)
        try:
            snapshot[local_path] = local_path.stat().st_mtime
        except OSError:
            snapshot[local_path] = None
    return snapshot


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> PollyPMConfig:
    config_path = path.resolve()
    try:
        mtime = config_path.stat().st_mtime
        cached = _config_cache.get(config_path)
        if (
            cached is not None
            and cached[0] == mtime
            and _project_local_mtimes(cached[1]) == cached[2]
        ):
            return cached[1]
    except OSError:
        pass
    base = config_path.parent
    raw = _load_raw_toml(config_path)
    project = _parse_project_settings(raw, base=base)
    accounts = _parse_accounts(raw, base=base)
    sessions = _parse_sessions(raw, base=base, workspace_root=project.workspace_root)
    pollypm = _parse_pollypm_settings(raw, sessions)
    memory = _parse_memory_settings(raw)
    plugins = _parse_plugin_settings(raw)
    rail = _parse_rail_settings(raw)
    planner = _parse_planner_settings(raw)
    logging_settings = _parse_logging_settings(raw)
    events = _parse_events_retention_settings(raw)
    storage = _parse_storage_settings(raw, project=project)
    projects = _parse_known_projects(raw, base=base)
    _merge_project_local_config(sessions, projects, plugins)
    _validate_cross_references(accounts=accounts, sessions=sessions, pollypm=pollypm)

    config = PollyPMConfig(
        project=project,
        pollypm=pollypm,
        accounts=accounts,
        sessions=sessions,
        projects=projects,
        memory=memory,
        plugins=plugins,
        rail=rail,
        planner=planner,
        logging=logging_settings,
        events=events,
        storage=storage,
    )
    try:
        _config_cache[config_path] = (
            config_path.stat().st_mtime,
            config,
            _project_local_mtimes(config),
        )
    except OSError:
        pass
    return config


def _ordered_role_assignments(
    assignments: dict[str, ModelAssignment],
    allowed_roles: tuple[str, ...],
) -> list[tuple[str, ModelAssignment]]:
    return [
        (role_name, assignments[role_name])
        for role_name in allowed_roles
        if role_name in assignments
    ]


def _append_role_assignment_tables(
    lines: list[str],
    *,
    table_prefix: str,
    assignments: dict[str, ModelAssignment],
    allowed_roles: tuple[str, ...],
) -> None:
    for role_name, assignment in _ordered_role_assignments(assignments, allowed_roles):
        lines.append(f"[{table_prefix}.{role_name}]")
        if assignment.alias is not None:
            lines.append(f'alias = "{_toml_str(assignment.alias)}"')
        else:
            lines.append(f'provider = "{_toml_str(assignment.provider or "")}"')
            lines.append(f'model = "{_toml_str(assignment.model or "")}"')
        lines.append("")


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
        f'release_channel = "{config.pollypm.release_channel}"',
    ]
    if config.pollypm.timezone:
        lines.append(f'timezone = "{config.pollypm.timezone}"')
    if config.pollypm.failover_accounts:
        items = ", ".join(f'"{name}"' for name in config.pollypm.failover_accounts)
        lines.append(f"failover_accounts = [{items}]")
    lines.append("")
    _append_role_assignment_tables(
        lines,
        table_prefix="pollypm.roles",
        assignments=config.pollypm.role_assignments,
        allowed_roles=_GLOBAL_ROLE_ASSIGNMENT_KEYS,
    )

    lines.extend(
        [
            "[memory]",
            f'backend = "{config.memory.backend}"',
            "",
        ]
    )

    if config.plugins.disabled:
        items = ", ".join(f'"{_toml_str(name)}"' for name in config.plugins.disabled)
        lines.extend(
            [
                "[plugins]",
                f"disabled = [{items}]",
                "",
            ]
        )

    # Emit [planner] only when the user has deviated from the default.
    # Default (all fields at their factory values) round-trips as an
    # absent section so existing configs don't churn on rewrite.
    planner_overrides: list[str] = []
    if not config.planner.auto_on_project_created:
        planner_overrides.append("auto_on_project_created = false")
    if not config.planner.enforce_plan:
        planner_overrides.append("enforce_plan = false")
    if config.planner.plan_dir != "docs/plan":
        planner_overrides.append(f'plan_dir = "{_toml_str(config.planner.plan_dir)}"')
    if planner_overrides:
        lines.append("[planner]")
        lines.extend(planner_overrides)
        lines.append("")

    # Emit [logging] only when the user has deviated from the defaults.
    logging_overrides: list[str] = []
    if config.logging.rotate_size_mb != 20:
        logging_overrides.append(f"rotate_size_mb = {config.logging.rotate_size_mb}")
    if config.logging.rotate_keep != 3:
        logging_overrides.append(f"rotate_keep = {config.logging.rotate_keep}")
    if logging_overrides:
        lines.append("[logging]")
        lines.extend(logging_overrides)
        lines.append("")

    for account_name, account in config.accounts.items():
        lines.extend(
            [
                f"[accounts.{account_name}]",
                f'provider = "{account.provider.value}"',
            ]
        )
        if account.email:
            lines.append(f'email = "{_toml_str(account.email)}"')
        if account.runtime is not RuntimeKind.LOCAL:
            lines.append(f'runtime = "{account.runtime.value}"')
        if account.home is not None:
            lines.append(f'home = "{_format_path(account.home, root)}"')
        if account.docker_image:
            lines.append(f'docker_image = "{_toml_str(account.docker_image)}"')
        if account.docker_extra_args:
            items = ", ".join(f'"{_toml_str(arg)}"' for arg in account.docker_extra_args)
            lines.append(f"docker_extra_args = [{items}]")
        if account.env:
            lines.append("[accounts.%s.env]" % account_name)
            for key, value in account.env.items():
                lines.append(f'{_toml_str(key)} = "{_toml_str(value)}"')
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
        _append_role_assignment_tables(
            lines,
            table_prefix=f"projects.{project_key}.roles",
            assignments=project.role_assignments,
            allowed_roles=_PROJECT_ROLE_ASSIGNMENT_KEYS,
        )

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
    base_dir = root / ".pollypm"
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
                prompt=_heartbeat_prompt(),
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
                prompt=_polly_prompt(),
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
    # Always UTF-8 — TOML is UTF-8 by spec, and a non-UTF-8 default
    # locale (Windows CP-1252, ``LC_ALL=C``) would mangle non-ASCII
    # project names or personas (``café``, ``プロジェクト``) that
    # round-trip through the cockpit settings UI.
    path.write_text(_render_global_config(config), encoding="utf-8")
    for project_key, project in config.projects.items():
        local_path = project_config_path(project.path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(
            _render_project_local_config(config, project_key),
            encoding="utf-8",
        )
    return path
