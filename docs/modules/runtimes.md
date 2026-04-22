**Last Verified:** 2026-04-22

## Summary

A `RuntimeAdapter` wraps a `LaunchCommand` produced by a provider adapter into the final executable form — a shell string (for tmux's `respawn-window`) and a structured `WrappedRuntimeCommand` (for direct `exec`). The two shipped runtimes are **local** (the default; runs the provider CLI directly under the account's env and home) and **docker** (stub-level; runs the CLI inside a container). Local is daily-driver; Docker is scaffolded.

Runtimes are orthogonal to providers. Any provider can run in any runtime; the `AccountConfig.runtime` field picks which one.

Touch this module when adding a new execution substrate (e.g. firecracker microVMs, a remote exec host). Do not push provider-specific flag handling into runtimes — that belongs in `ProviderAdapter.build_launch_command`.

## Core Contracts

```python
# src/pollypm/runtimes/base.py
@dataclass(slots=True)
class WrappedRuntimeCommand:
    argv: list[str]
    env: dict[str, str] | None = None
    cwd: Path | None = None

class RuntimeAdapter(Protocol):
    def wrap_command(
        self, command: LaunchCommand, account: AccountConfig, project: ProjectSettings,
    ) -> str: ...
    def wrap_command_exec(
        self, command: LaunchCommand, account: AccountConfig, project: ProjectSettings,
    ) -> WrappedRuntimeCommand: ...
```

Registry:

```python
# src/pollypm/runtimes/__init__.py
def get_runtime(name: str) -> RuntimeAdapter: ...
```

## File Structure

- `src/pollypm/runtimes/base.py` — `RuntimeAdapter` protocol + `WrappedRuntimeCommand`.
- `src/pollypm/runtimes/local.py` — `LocalRuntimeAdapter`: direct exec with provider env + account home.
- `src/pollypm/runtimes/docker.py` — `DockerRuntimeAdapter`: `docker run` wrapper.
- `src/pollypm/runtime_env.py` — provider-profile env composition (`provider_profile_env`, `claude_config_dir`, `codex_home_dir`).
- `src/pollypm/runtime_launcher.py` — shared launcher bootstrap script used by `LocalRuntimeAdapter` to prime account homes on first launch.
- `src/pollypm/plugins_builtin/local_runtime/plugin.py` — registers `LocalRuntimeAdapter` as runtime `"local"`.
- `src/pollypm/plugins_builtin/docker_runtime/plugin.py` — registers `DockerRuntimeAdapter` as runtime `"docker"`.

## Implementation Details

- **Local runtime.** Uses the project's `.venv/bin/python` when present; falls back to `sys.executable` when PollyPM is installed globally (e.g. `uv tool install`). It encodes the launch payload as base64 JSON and passes it to a small launcher script (`runtime_launcher.py`) so the tmux `respawn-window` argument stays a single shell word. The launcher restores env, cwd, and argv and execs the provider binary.
- **Account homes.** Account-scoped homes under `~/.pollypm/homes/<account>/` isolate provider config directories. `provider_profile_env(account, base_env)` composes env like `CLAUDE_CONFIG_DIR=<home>/.claude`, `CODEX_HOME=<home>/.codex`, and overrides `HOME` for any CLI that reads dotfiles from `$HOME`.
- **Docker runtime.** Scaffolded but not the daily path. `DockerRuntimeAdapter.wrap_command` constructs a `docker run` invocation with mounts for the project directory and the account home. The image is `AccountConfig.docker_image`; extra args go in `docker_extra_args`. Most v1 validation runs against the local runtime — treat the Docker path as experimental.
- **Why two wrap methods.** `wrap_command` returns a single shell string for tmux; `wrap_command_exec` returns structured argv for code that wants to `subprocess.Popen` directly (e.g. the capacity probe). Keeping both prevents repeated round-trips through a shell.

## Related Docs

- [modules/providers.md](providers.md) — source of `LaunchCommand`.
- [modules/accounts.md](accounts.md) — owns `AccountConfig.home` and account-home priming.
- [modules/session-services.md](session-services.md) — consumes the runtime-wrapped string.
