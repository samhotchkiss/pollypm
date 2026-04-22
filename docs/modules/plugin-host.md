**Last Verified:** 2026-04-22

## Summary

The plugin host discovers plugin manifests on disk (and via entry points), imports their `plugin.py` modules, and exposes a `PluginAPI` + registries (rail, roster, job handler, content) that plugins register against. All replaceable seams in PollyPM — providers, runtimes, session services, heartbeat/scheduler backends, agent profiles, launch planner, recovery policy, task / memory / doc backends, rail items, roster entries, job handlers, and content (magic skills, flow templates) — flow through this host.

`ExtensionHost` is the concrete host. `extension_host_for_root(root_dir)` is the cached factory; processes typically hold one host per root. `CoreRail.get_plugin_host()` returns the process-wide host.

Touch this module when changing discovery rules, capability kinds, hook context shape, or registry semantics. Do **not** reach into `plugin.py` internals — always call through the public registries.

## Core Contracts

```python
# src/pollypm/plugin_api/v1.py
PLUGIN_API_VERSION = "1"

KNOWN_CAPABILITY_KINDS = frozenset({
    "provider", "runtime", "session_service", "heartbeat", "scheduler",
    "agent_profile", "task_backend", "memory_backend", "doc_backend",
    "sync_adapter", "transcript_source", "recovery_policy", "launch_planner",
    "job_handler", "roster_entry", "hook", "roster",
})

@dataclass(slots=True)
class HookContext:
    hook_name: str
    payload: Any
    root_dir: Path
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass(slots=True)
class HookFilterResult:
    action: str = "allow"
    payload: Any = None
    reason: str | None = None

@dataclass
class Capability:
    kind: str
    name: str
    # ... plus opt-in fields for content declarations

@dataclass
class PollyPMPlugin:
    name: str
    version: str = "0.0.0"
    description: str = ""
    capabilities: tuple[Capability, ...] = ()
    # Factory / registration fields (any subset):
    initialize: PluginInitializer | None = None
    providers: dict[str, ProviderFactory] | None = None
    runtimes: dict[str, RuntimeFactory] | None = None
    session_services: dict[str, SessionServiceFactory] | None = None
    heartbeat_backends: dict[str, HeartbeatBackendFactory] | None = None
    scheduler_backends: dict[str, SchedulerBackendFactory] | None = None
    agent_profiles: dict[str, AgentProfileFactory] | None = None
    recovery_policies: dict[str, RecoveryPolicyFactory] | None = None
    launch_planners: dict[str, LaunchPlannerFactory] | None = None
    register_roster: RosterRegistrar | None = None
    register_handlers: JobHandlerRegistrar | None = None
    # Rail registration is done inside initialize(api) via api.rail.register_item(...)

class PluginAPI:
    config: PollyPMConfig | None
    rail: RailAPI
    roster: RosterAPI
    job_handlers: JobHandlerAPI
    def content_paths(self, *, kind: str) -> list[Path]: ...
    def emit_event(self, name: str, payload: dict | None = None) -> None: ...

class RailAPI:
    def register_item(
        self, *, section, index, label, handler, key,
        icon=None, state_provider=None, badge_provider=None, label_provider=None,
        rows_provider=None, help_text=None,
    ) -> None: ...

class RosterAPI:
    def register_recurring(
        self, schedule_expr: str, handler_name: str, payload: dict,
        *, dedupe_key: str | None = None,
    ) -> None: ...

class JobHandlerAPI:
    def register_handler(
        self, name: str, handler: JobHandlerCallable,
        *, max_attempts: int = 3, timeout_seconds: float = 60.0,
    ) -> None: ...

def check_requires_api(expression: str | None, current_api_version: str) -> bool: ...
```

```python
# src/pollypm/plugin_host.py
PLUGIN_MANIFEST = "pollypm-plugin.toml"
PLUGIN_API_VERSION = "1"

@dataclass(frozen=True, slots=True)
class PluginManifest: ...

class ExtensionHost:
    def plugins(self) -> list[PollyPMPlugin]: ...
    def initialize_plugins(self, config) -> None: ...
    def run_observers(self, hook_name: str, payload, *, metadata=None) -> None: ...
    def run_filters(self, hook_name: str, payload, *, metadata=None) -> HookFilterResult: ...
    def rail_registry(self) -> RailRegistry: ...
    def job_handler_registry(self) -> JobHandlerRegistry: ...
    def roster(self) -> Roster: ...
    def content_paths(self, *, kind: str) -> list[Path]: ...

def extension_host_for_root(root_dir: str) -> ExtensionHost: ...
```

## File Structure

- `src/pollypm/plugin_api/v1.py` — the public registration surface.
- `src/pollypm/plugin_api/__init__.py` — re-exports from `v1`.
- `src/pollypm/plugin_host.py` — `ExtensionHost`, `PluginManifest`, discovery, dedupe, lifecycle event emission.
- `src/pollypm/plugin_trust.py` — third-party trust warning (`warn_third_party_extension_trust_once`).
- `src/pollypm/plugin_validate.py` — manifest validation helpers.
- `src/pollypm/plugin_cli.py` — `pm plugins` command family.
- `src/pollypm/plugins_builtin/<name>/pollypm-plugin.toml` — per-plugin manifest.
- `src/pollypm/plugins_builtin/<name>/plugin.py` — per-plugin `plugin = PollyPMPlugin(...)`.

## Implementation Details

- **Discovery order.** (1) built-in plugins under `pollypm/plugins_builtin/`. (2) user-global under `~/.pollypm/plugins/`. (3) project-local under `<project>/.pollypm/plugins/`. (4) Python entry points in the `pollypm.plugins` group. Duplicate plugin names are deduped — the *later* source wins so projects can replace a built-in, with a logged warning.
- **Manifest shape.** `pollypm-plugin.toml` declares the plugin's `name`, `description`, `version`, `requires_api`, `capabilities`, and optional `[content]` blocks. `requires_api` is a major-version expression checked via `check_requires_api`; incompatible manifests are refused at load with a log line.
- **Name rules.** Plugin names must match `^[a-z][a-z0-9_-]*$` (`PLUGIN_NAME_PATTERN`). Path-like or ambiguous names are rejected at the host boundary.
- **Capability kinds.** `KNOWN_CAPABILITY_KINDS` is a permissive set. Unknown kinds do *not* fail load — they are preserved as-is with a logged warning so a forward-compatible plugin keeps working on an older rail.
- **Initialize.** `PollyPMPlugin.initialize(api: PluginAPI)` is called once per process after registries are attached. Plugins that want to register rail items do it here via `api.rail.register_item(...)`. Roster and job-handler registration happen via dedicated `register_roster` / `register_handlers` callbacks so the rail can snapshot the roster before ticking.
- **Hooks.** `run_observers(hook_name, payload)` fires every subscriber; `run_filters(hook_name, payload)` runs an allow/deny chain and returns the final `HookFilterResult`. Filters short-circuit on the first non-allow result.
- **Lifecycle events.** On successful plugin load the host records a `plugin_loaded` event via the state store (if available); on error, `plugin_errored`. The Activity Feed plugin surfaces these in the cockpit.
- **Trust.** Third-party plugins (anything outside `plugins_builtin/`) trigger a one-time trust warning via `plugin_trust.warn_third_party_extension_trust_once`.

## Related Docs

- [modules/core-rail.md](core-rail.md) — holds the process-wide `ExtensionHost`.
- [modules/heartbeat.md](heartbeat.md) — consumes `Roster` + `JobHandlerRegistry` from the host.
- [features/cockpit-rail.md](../features/cockpit-rail.md) — consumes `RailRegistry`.
- [plugins/core-recurring.md](../plugins/core-recurring.md) — canonical example of roster + handler registration.
