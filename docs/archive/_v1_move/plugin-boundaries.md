# Plugin Boundaries and Interop Contracts

This document defines the boundary contracts between work service plugins. Each plugin is a sealed, interchangeable piece with a defined protocol, clear responsibilities, and explicit dependency rules.

## Plugin Boundary Map

| Plugin | Protocol | Responsibility | Can call | Cannot call |
|--------|----------|----------------|----------|-------------|
| Work service | `WorkService` | Task state, flows, transitions | Storage backend | TUI, tmux, git |
| Session manager | `SessionManager` | Worktree/tmux lifecycle | tmux client, git | Work service internals |
| Flow engine | functions | Load/validate/resolve flows | Filesystem | Storage, tmux |
| Gates | `Gate` | Evaluate preconditions | Task data (read-only) | Storage, transitions |
| Sync adapters | `SyncAdapter` | Project to external systems | External APIs | Work service internals |
| TUI | (consumer) | Render UI | All plugin APIs (read) | Direct storage access |

## Protocols

### WorkService

Defined in `src/pollypm/work/service.py`. The central protocol covering:

- Task lifecycle: create, queue, claim, cancel, hold, resume
- Flow progression: node_done, approve, reject, block
- Context: add_context, get_context
- Relationships: link, unlink, dependents
- Flows: available_flows, get_flow, validate_advance
- Sync: sync_status, trigger_sync
- Queries: state_counts, my_tasks, blocked_tasks

Two implementations exist:
- `SQLiteWorkService` (production, backed by SQLite)
- `MockWorkService` (testing, in-memory dicts)

### Gate

Defined in `src/pollypm/work/gates.py`. A runtime-checkable protocol:

```python
class Gate(Protocol):
    name: str
    gate_type: str  # "hard" or "soft"
    def check(self, task: Task, **kwargs) -> GateResult: ...
```

Gates are discovered from three tiers: built-in, user-global (`~/.pollypm/gates/`), project-local (`<project>/.pollypm/gates/`).

### SyncAdapter

Defined in `src/pollypm/work/sync.py`. A runtime-checkable protocol:

```python
class SyncAdapter(Protocol):
    name: str
    def on_create(self, task: Task) -> None: ...
    def on_transition(self, task: Task, old_status: str, new_status: str) -> None: ...
    def on_update(self, task: Task, changed_fields: list[str]) -> None: ...
```

### SessionManager

Defined in `src/pollypm/work/session_manager.py`. A concrete class (not a protocol) that binds tasks to tmux panes and git worktrees. Requires a TmuxClient and a work service reference.

## Plugin Registry

`src/pollypm/work/plugin_registry.py` provides:

- `PluginRegistry` class with typed slots for each plugin
- `configure_work_plugins()` loader that wires defaults from config
- `PluginNotRegisteredError` raised when accessing unregistered slots

## Configuration

Plugin selection is configured in `pollypm.toml`:

```toml
[work_service]
backend = "builtin"  # or "custom"
# module = "my_company.custom_backend"  # when backend = "custom"

[sync]
adapters = ["file", "github"]

[sync.github]
repo = "owner/repo"

[sync.file]
issues_dir = "issues"
```

The `configure_work_plugins()` function reads this config and instantiates the appropriate plugins.

## Interop Guarantees

1. Any code that accepts `WorkService` works identically with `MockWorkService` or `SQLiteWorkService`
2. Custom gates implementing the `Gate` protocol integrate into the gate registry without modification
3. Custom sync adapters implementing `SyncAdapter` receive events via the `SyncManager`
4. The `PluginRegistry` enforces that all required plugins are registered before use
5. The `configure_work_plugins()` loader handles missing config by selecting built-in defaults

## Rail Plugin References

Rail plugins (providers, runtimes, session services, agent profiles, etc.)
live at a different boundary from the work-service plugins above. This
document is about work-service interop contracts; it is not the canonical home
for rail plugin manifests, discovery precedence, or install guidance.

Use these docs instead:

- [`plugin-authoring.md`](plugin-authoring.md) — current hello-world/how-to guide
- [`plugin-discovery-spec.md`](plugin-discovery-spec.md) — manifest shape and discovery precedence
- [`provider-plugin-sdk.md`](provider-plugin-sdk.md) — provider-specific adapter contract
- [`extensible-rail-spec.md`](extensible-rail-spec.md) — cockpit rail extension surface
