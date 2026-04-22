**Last Verified:** 2026-04-22

## Summary

Config is TOML, loaded from `~/.pollypm/pollypm.toml` by default, with project-local overrides merged from `<project>/.pollypm/config/project.toml`. `pollypm.config.load_config(path)` parses and validates the file, resolves path fields, applies safe fallbacks for mis-typed values (e.g. an unknown `release_channel` silently becomes `"stable"`), and returns a `PollyPMConfig` dataclass tree defined in `pollypm.models`.

Global config lives under `~/.pollypm/`; project config lives under `<project>/.pollypm/config/`. Agent-driven preference patches are applied via `config_patches.apply_preference_patch` — the user chats with Polly about what they want different, Polly patches the override file, and the next load picks it up. Built-in defaults are never modified.

Touch this module when adding a new config section or dataclass. Do not read TOML directly in downstream modules — route through `load_config`.

## Core Contracts

```python
# src/pollypm/config.py
GLOBAL_CONFIG_DIR = Path.home() / ".pollypm"
DEFAULT_CONFIG_PATH = GLOBAL_CONFIG_DIR / "pollypm.toml"
PROJECT_CONFIG_DIRNAME = ".pollypm/config"
PROJECT_CONFIG_FILENAME = "project.toml"

def resolve_config_path(path: Path = DEFAULT_CONFIG_PATH) -> Path: ...
def load_config(path: Path) -> PollyPMConfig: ...
def write_config(path: Path, config: PollyPMConfig) -> None: ...
def render_example_config() -> str: ...
def write_example_config(path: Path) -> None: ...
def project_config_path(project_root: Path) -> Path: ...

# src/pollypm/config_patches.py
def detect_preference_patch(project_key: str, text: str) -> PreferencePatch | None: ...
def apply_preference_patch(project_key: str, text: str) -> PreferencePatch: ...
def list_project_overrides(project_key: str) -> list[str]: ...
```

Top-level dataclass tree (see `src/pollypm/models.py`):

```python
@dataclass
class PollyPMConfig:
    project: ProjectSettings
    projects: dict[str, KnownProject]
    accounts: dict[str, AccountConfig]
    sessions: dict[str, SessionConfig]
    pollypm: PollyPMSettings
    memory: MemorySettings
    plugins: PluginSettings
    rail: RailSettings
    logging: LoggingSettings
    planner: PlannerSettings
    storage: StorageSettings
    events: EventsRetentionSettings
```

## File Structure

- `src/pollypm/config.py` — loader, writer, path resolution, example rendering, fallback logic.
- `src/pollypm/config_patches.py` — detect + apply agent-driven preference patches.
- `src/pollypm/models.py` — all config dataclasses + `ProviderKind`, `RuntimeKind`, `ProjectKind` enums.
- `pollypm.dev.toml` — the example config checked into the repo root (used for local development).
- `src/pollypm/defaults/` — shipped templates (`docs/`, `magic/`, `rules/`, demo data).

## Implementation Details

- **Global vs project.** `resolve_config_path` returns `DEFAULT_CONFIG_PATH` unless a caller explicitly passes a different path. Project-local overrides are merged separately at load time via `_merge_project_local_config` — reading `.pollypm/config/project.toml` from each registered project.
- **Safe fallbacks.** `release_channel` and similar typed fields use `_validate_*` helpers that log a warning and return the safe default for unknown values. A typo in the TOML must never brick the CLI (issue #713).
- **Path traversal.** `_resolve_path(base, raw_path)` rejects relative paths that escape `base` after resolution. This protects against `../../etc/passwd`-style config entries.
- **Prompt substitution.** `_normalize_session_prompt` recognizes the legacy full-text heartbeat / operator prompts and rewrites them to call `heartbeat_prompt()` / `polly_prompt()` from the built-in persona plugin. This keeps old configs working after the prompt source moved into `core_agent_profiles`.
- **Tmux session name.** `_normalize_tmux_session_name` normalizes to `"pollypm"` by default; users can override per-project.
- **Preference patches.** `detect_preference_patch(project_key, text)` sniffs free-form user text for a recognized intent ("use beta", "open permissions off", etc.) and returns a diff. `apply_preference_patch` writes the override to the project's `project.toml`. Built-in defaults are never modified — patches create overrides.

## Related Docs

- [modules/state-store.md](state-store.md) — `ProjectSettings.state_db` points at the per-project DB.
- [features/cli.md](../features/cli.md) — most commands take `--config` and resolve through this module.
- [features/service-api.md](../features/service-api.md) — `PollyPMService(config_path)` binds here.
