# Provider Plugin SDK

PollyPM has two shipped provider contracts today.

- The runtime provider adapter is what launches sessions, declares transcript locations, and parses live usage from a pane. The real contract is the `ProviderAdapter` Protocol in [`src/pollypm/providers/base.py`](../src/pollypm/providers/base.py). Most provider-plugin authors start here.
- The account/onboarding provider is what powers `pm accounts`, onboarding, `pm doctor`, login/logout helpers, and architect resume. That contract lives in [`src/pollypm/acct/protocol.py`](../src/pollypm/acct/protocol.py) and is registered through the `pollypm.provider` entry-point group.

If you only ship the runtime adapter, PollyPM can launch your provider in work sessions, but the account-management commands will not know about it. If you only ship the `pollypm.provider` entry point, `pm accounts` can see you, but the plugin-host launch path will not.

## Choose The Surface

| Goal | Implement | Register it as | In-tree examples |
| --- | --- | --- | --- |
| Launch worker/control sessions, resume them, discover transcripts, parse live usage | `ProviderAdapterBase` from [`src/pollypm/provider_sdk.py`](../src/pollypm/provider_sdk.py), validated against [`src/pollypm/providers/base.py`](../src/pollypm/providers/base.py) | `PollyPMPlugin.providers` in a plugin | [`src/pollypm/providers/codex/adapter.py`](../src/pollypm/providers/codex/adapter.py), [`src/pollypm/providers/claude/adapter.py`](../src/pollypm/providers/claude/adapter.py) |
| Integrate with `pm accounts`, onboarding, doctor, login/logout, provider registry | `ProviderAdapter` in [`src/pollypm/acct/protocol.py`](../src/pollypm/acct/protocol.py) | `[project.entry-points."pollypm.provider"]` | [`src/pollypm/providers/codex/provider.py`](../src/pollypm/providers/codex/provider.py), [`src/pollypm/providers/claude/provider.py`](../src/pollypm/providers/claude/provider.py) |

For a complete external provider package, expect to ship both.

## Runtime Provider Contract

`ProviderAdapterBase` is the convenience ABC. The launch path is typed against the Protocol in [`src/pollypm/providers/base.py`](../src/pollypm/providers/base.py), so that file is the source of truth.

Import these core types:

- From `pollypm.provider_sdk`: `ProviderAdapterBase`, `ProviderUsageSnapshot`, `TranscriptSource`
- From `pollypm.providers.base`: `LaunchCommand`

Your runtime adapter exposes:

- `name`: stable provider id such as `"codex"` or `"claude"`
- `binary`: CLI binary name used by the default `is_available()` check
- `build_launch_command(session, account) -> LaunchCommand`: required
- `build_resume_command(session, account) -> LaunchCommand | None`: optional
- `transcript_sources(account, session=None) -> tuple[TranscriptSource, ...]`: optional
- `collect_usage_snapshot(tmux, target, *, account, session) -> ProviderUsageSnapshot`: optional at the base-class level, but required in practice if you want live usage/health

### `LaunchCommand` fields

| Field | Meaning in PollyPM |
| --- | --- |
| `argv` | Command PollyPM executes for a fresh launch |
| `env` | Provider-specific env vars merged into the runtime env |
| `cwd` | Working directory for the launched process |
| `resume_argv` | Alternate argv PollyPM uses when the `resume_marker` exists |
| `resume_marker` | Marker file PollyPM writes/reads to decide whether resume is valid |
| `initial_input` | Prompt text PollyPM sends after a fresh launch |
| `fresh_launch_marker` | Marker PollyPM touches on a fresh launch so it only sends `initial_input` once |

The marker fields matter for control sessions. The built-in Codex and Claude adapters both use them so operator and supervisor windows resume in place instead of starting a new conversation every time.

### `ProviderUsageSnapshot`

`ProviderUsageSnapshot` is intentionally soft-typed. Built-ins populate:

- `health`, `summary`, `raw_text`
- usage percentages such as `used_pct` and `remaining_pct`
- reset/access timestamps when the provider exposes them

On parse misses, the shipped adapters prefer returning a partial snapshot with `raw_text` populated over raising an exception.

## Worked Runtime Example

[`src/pollypm/providers/codex/adapter.py`](../src/pollypm/providers/codex/adapter.py) is the smallest complete built-in example. A third-party adapter usually looks like this:

```python
from pollypm.models import AccountConfig, SessionConfig
from pollypm.provider_sdk import (
    ProviderAdapterBase,
    ProviderUsageSnapshot,
    TranscriptSource,
)
from pollypm.providers.base import LaunchCommand


class ExampleAdapter(ProviderAdapterBase):
    name = "example"
    binary = "example-cli"

    def build_launch_command(
        self,
        session: SessionConfig,
        account: AccountConfig,
    ) -> LaunchCommand:
        argv = [self.binary, *session.args]
        resume_argv = None
        resume_marker = None
        fresh_marker = None
        if account.home is not None:
            fresh_marker = (
                account.home / ".pollypm" / "session-markers" / f"{session.name}.fresh"
            )
        if session.role in {"heartbeat-supervisor", "operator-pm"} and account.home is not None:
            resume_argv = [self.binary, "resume", "--last", *session.args]
            resume_marker = (
                account.home / ".pollypm" / "session-markers" / f"{session.name}.resume"
            )
        return LaunchCommand(
            argv=argv,
            env=dict(account.env),
            cwd=session.cwd,
            resume_argv=resume_argv,
            resume_marker=resume_marker,
            initial_input=session.prompt,
            fresh_launch_marker=fresh_marker,
        )

    def transcript_sources(
        self,
        account: AccountConfig,
        session: SessionConfig | None = None,
    ) -> tuple[TranscriptSource, ...]:
        if account.home is None:
            return ()
        return (
            TranscriptSource(
                root=account.home / ".example-cli" / "sessions",
                pattern="**/*.jsonl",
                description="Example CLI transcript JSONL",
            ),
        )

    def collect_usage_snapshot(
        self,
        tmux,
        target: str,
        *,
        account: AccountConfig,
        session: SessionConfig,
    ) -> ProviderUsageSnapshot:
        text = tmux.capture_pane(target, lines=320)
        if "% left" not in text:
            return ProviderUsageSnapshot(raw_text=text)
        return ProviderUsageSnapshot(
            health="healthy",
            summary="usage available",
            raw_text=text,
        )
```

Important details from the built-ins:

- `build_launch_command()` is deterministic and side-effect-free. Do not create directories, write config files, or mutate auth state there.
- `transcript_sources()` is best-effort. Return `()` when `account.home` is missing.
- `collect_usage_snapshot()` owns prompt-driving in the tmux pane and should tolerate unparseable output.

## Register The Runtime Adapter

Runtime adapters are discovered through the plugin host, not the `pollypm.provider` entry-point registry.

Directory-style plugin:

```toml
# ~/.pollypm/plugins/example-provider/pollypm-plugin.toml
api_version = "1"
name = "example-provider"
kind = "provider"
version = "0.1.0"
entrypoint = "plugin.py:plugin"

[[capabilities]]
kind = "provider"
name = "example"
```

```python
# ~/.pollypm/plugins/example-provider/plugin.py
from pollypm.plugin_api.v1 import Capability, PollyPMPlugin

# ExampleAdapter defined above. Keep it in this file for a directory-style
# plugin, or package the plugin and use the pollypm.plugins entry-point path
# below.

plugin = PollyPMPlugin(
    name="example-provider",
    version="0.1.0",
    capabilities=(Capability(kind="provider", name="example"),),
    providers={"example": ExampleAdapter},
)
```

Entry-point plugin package:

```toml
[project.entry-points."pollypm.plugins"]
example-provider = "example_provider.plugin:plugin"
```

The plugin-host side is exercised in [`tests/test_plugins.py`](../tests/test_plugins.py).

## Full Account Integration

If you also want PollyPM's account-management flows, add a second class that implements the Protocol in [`src/pollypm/acct/protocol.py`](../src/pollypm/acct/protocol.py) and register it under `pollypm.provider`.

That contract covers:

- auth detection and email detection
- interactive login/logout helpers
- account usage probing
- worker launch argv for the account layer
- isolated home env wiring
- latest-session lookup and resume argv
- home priming and login completion detection

`CodexProvider` in [`src/pollypm/providers/codex/provider.py`](../src/pollypm/providers/codex/provider.py) is the better current example if you need the whole surface. `ClaudeProvider` still carries compatibility stubs for parts of the Phase-B account refactor.

Register the account-layer provider in your package metadata:

```toml
[project.entry-points."pollypm.provider"]
example = "example_provider.provider:ExampleProvider"
```

PollyPM's account registry and `pm doctor` walk this entry-point group directly. The registry behavior is covered by [`tests/test_acct_substrate.py`](../tests/test_acct_substrate.py) and the fuller provider tests under [`tests/providers/`](../tests/providers).

## Error-Handling Expectations

- `is_available()` should stay cheap and side-effect-free. The base implementation uses `shutil.which(self.binary)`.
- `build_launch_command()` and `build_resume_command()` may raise on truly invalid configuration, but they should not do runtime probing or filesystem mutation.
- `transcript_sources()` should degrade to `()` when the provider has no usable home or transcript root.
- `collect_usage_snapshot()` should return a conservative `ProviderUsageSnapshot` for ordinary parse failures. Reserve exceptions for genuinely unsupported probe paths, not transient provider output drift.
- If you implement the account-layer provider, follow the same style as the built-ins: when something is unsupported tonight, raise a descriptive error that explains what happened, why it matters, and how to fix or route around it.

## Testing Pattern

You do not need a live tmux session or a real provider login to test most of this.

- Runtime adapter unit tests: build `AccountConfig` and `SessionConfig`, feed a fake tmux object with `capture_pane()` and `send_keys()`, and assert on `LaunchCommand`, transcript roots, and `ProviderUsageSnapshot`. See [`tests/test_provider_sdk.py`](../tests/test_provider_sdk.py) and [`tests/test_runtime.py`](../tests/test_runtime.py).
- Plugin registration tests: create a temporary plugin directory or entry-point-backed plugin object and load it through `ExtensionHost(tmp_path)`. See [`tests/test_plugins.py`](../tests/test_plugins.py).
- Account-layer provider tests: instantiate the provider directly, monkeypatch its helper modules, and separately verify the `pollypm.provider` registry path. See [`tests/providers/test_codex_provider.py`](../tests/providers/test_codex_provider.py) and [`tests/providers/test_claude_provider.py`](../tests/providers/test_claude_provider.py).

For plugin packaging details beyond the provider lane, see [`plugin-authoring.md`](plugin-authoring.md).
