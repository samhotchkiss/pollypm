---
## Summary

PollyPM exposes a stable provider SDK for adding new CLI agent providers without editing core orchestration code. Provider authors subclass a single base class, implement two required methods and two optional methods, and PollyPM handles discovery, lifecycle management, health monitoring, and transcript ingestion. Built-in providers (Claude, Codex) implement the same SDK interface, so third-party adapters have identical capabilities and integration depth.

---

# 05. Provider SDK

## Overview

The provider SDK is the primary integration point for adding new CLI agent backends to PollyPM. It is designed around three principles:

1. **SDK, not RPC.** Providers are Python classes loaded in-process, not external services called over a network boundary. This keeps the integration fast, debuggable, and free of serialization overhead.

2. **Same interface for built-ins and third parties.** The Claude and Codex adapters implement `ProviderAdapterBase` exactly as a third-party Gemini or Aider adapter would. There is no privileged internal path.

3. **Minimal surface area.** Two required methods and two optional methods cover the full provider contract. Everything else (session lifecycle, health policy, failover, recovery) is handled by the core.


## Core Types

Import these from `pollypm.provider_sdk`:

| Type | Purpose |
|------|---------|
| `ProviderAdapterBase` | Abstract base class that all providers subclass |
| `ProviderUsageSnapshot` | Structured snapshot of capacity, health, and usage data |
| `TranscriptSource` | Declaration of where a provider writes JSONL session logs |

Import from `pollypm.providers.base`:

| Type | Purpose |
|------|---------|
| `LaunchCommand` | Container for argv, environment variables, and working directory |


## Provider Contract

Subclass `ProviderAdapterBase` and implement the following methods.

### Required Methods

#### `build_launch_command(session, account) -> LaunchCommand`

Construct the shell command to start a fresh provider CLI session.

Parameters:

- `session` (`SessionConfig`): the session declaration including args, prompt, working directory, role, and project binding
- `account` (`AccountConfig`): the account to use, including environment variables, home directory, and credential paths

Returns a `LaunchCommand` containing:

- `argv`: the command and arguments as a list of strings
- `env`: environment variables to set (merged with the runtime environment)
- `cwd`: working directory for the process

This method must be deterministic for the same inputs. Side effects (creating directories, writing config files) should happen in the runtime layer, not here.

#### `collect_usage_snapshot(tmux, target, *, account, session) -> ProviderUsageSnapshot`

Collect a point-in-time snapshot of the provider's capacity and usage state.

Parameters:

- `tmux`: the tmux controller for pane capture and interaction
- `target` (`str`): the tmux target identifier for this session's pane
- `account` (`AccountConfig`): the account bound to this session
- `session` (`SessionConfig`): the session declaration

Returns a `ProviderUsageSnapshot` containing:

- `health`: one of the provider health classifications (see below)
- `summary`: human-readable summary of the usage state
- `raw_text`: the raw pane output or API response used to determine health

This method is called periodically by the heartbeat system. It should be fast and should not block on network calls that might time out.

### Optional Methods

#### `build_resume_command(session, account) -> LaunchCommand`

Construct the shell command to resume or reattach to an existing provider session. If not implemented, PollyPM falls back to `build_launch_command` for recovery.

The resume command typically includes flags that tell the provider CLI to continue an existing conversation rather than starting fresh. For example, Claude's `--continue` flag or Codex's session resume argument.

Parameters and return type are identical to `build_launch_command`.

#### `transcript_sources(account, session) -> tuple[TranscriptSource, ...]`

Declare the filesystem locations where this provider writes JSONL session logs.

Parameters:

- `account` (`AccountConfig`): the account, used to determine home directory
- `session` (`SessionConfig` or `None`): the session, if scoping to a specific session

Returns a tuple of `TranscriptSource` objects, each containing:

- `root`: the base directory where transcript files are written
- `pattern`: a glob pattern for discovering transcript files within the root

If not implemented, the provider has no transcript-based monitoring and relies solely on pane capture for health signals.


## Example: GeminiAdapter

This example shows a minimal but complete third-party provider adapter.

```python
from pollypm.models import AccountConfig, SessionConfig
from pollypm.provider_sdk import (
    ProviderAdapterBase,
    ProviderUsageSnapshot,
    TranscriptSource,
)
from pollypm.providers.base import LaunchCommand


class GeminiAdapter(ProviderAdapterBase):
    name = "gemini"
    binary = "gemini"

    def build_launch_command(
        self, session: SessionConfig, account: AccountConfig
    ) -> LaunchCommand:
        argv = [self.binary, *session.args]
        if session.prompt:
            argv.append(session.prompt)
        return LaunchCommand(
            argv=argv, env=dict(account.env), cwd=session.cwd
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
                root=account.home / ".gemini" / "sessions",
                pattern="**/*.jsonl",
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
        return ProviderUsageSnapshot(
            health="healthy",
            summary="usage available",
            raw_text=text,
        )
```

Key observations:

- The adapter declares `name` and `binary` as class attributes for discovery and binary detection.
- `build_launch_command` constructs a simple argv list. The runtime layer handles environment isolation, home directory setup, and auth mounting.
- `transcript_sources` points to the Gemini-specific JSONL location. PollyPM's transcript ingestion system tails these files for monitoring.
- `collect_usage_snapshot` captures pane output and returns a structured snapshot. A production adapter would parse the output for rate-limit signals and token counts.


## Built-In Providers

Claude and Codex are implemented as built-in providers using the same SDK.

### Claude

- Binary: `claude`
- Transcript location: `<account_home>/.claude/projects/<project>/sessions/`
- Resume support: yes, via `--continue` flag
- Usage extraction: parses pane output for token counts and rate-limit indicators
- Health signals: detects auth failures, rate limiting, and context exhaustion from pane text

### Codex

- Binary: `codex`
- Transcript location: `<account_home>/.codex/sessions/`
- Resume support: yes, via session ID argument
- Usage extraction: parses pane output for usage summaries
- Health signals: detects API errors and auth failures from pane text

Both adapters are registered as built-in plugins with lowest discovery precedence. Per the "opinionated but pluggable" philosophy, these are the opinionated defaults — a project-local (`<project>/.pollypm/plugins/`) or user-local plugin with the same provider name overrides the built-in.


## Provider Health Classification

Every `ProviderUsageSnapshot` includes a health classification. The provider reports its own health based on signals it can observe. The core decides what to do about unhealthy providers (failover, cooldown, alert).

| Health | Meaning |
|--------|---------|
| `healthy` | Provider is operational with available capacity |
| `degraded` | Provider is operational but approaching limits or experiencing latency |
| `exhausted` | Provider has hit its usage or token limit for the current period |
| `throttled` | Provider is rate-limited and requests are being delayed or rejected |
| `auth_broken` | Provider credentials are invalid, expired, or revoked |
| `provider_outage` | Provider backend is unreachable or returning server errors |

Health is provider-reported, not inferred by the core. Each provider adapter knows how to interpret its own error messages, pane output patterns, and API responses. The core treats health as an opaque enum and applies policy (failover thresholds, cooldown durations, alert escalation) without knowing how the classification was made.

### Health Flow

```
Provider pane output / API response
        |
        v
  collect_usage_snapshot()
        |
        v
  ProviderUsageSnapshot(health=...)
        |
        v
  Core: apply health policy
        |
        +---> healthy: no action
        +---> degraded: log, optionally warn operator
        +---> exhausted: cooldown account, failover if alternate available
        +---> throttled: back off, retry after delay
        +---> auth_broken: disable account, alert operator
        +---> provider_outage: failover to different provider if available
```


## Transcript Discovery

Each provider declares where it writes JSONL session logs via the `transcript_sources` method. These declarations enable PollyPM's transcript ingestion system to discover and tail logs without hardcoding paths for each provider.

### Discovery Model

1. When a session launches, PollyPM calls `transcript_sources(account, session)` on the provider adapter.
2. The adapter returns zero or more `TranscriptSource` objects, each specifying a root directory and a glob pattern.
3. The transcript ingestion system watches these locations for new and updated files.
4. New events are parsed into normalized transcript events and fed into the event bus.

### Provider-Specific Locations

| Provider | Transcript Root | Pattern |
|----------|----------------|---------|
| Claude | `<home>/.claude/projects/<project>/sessions/` | `**/*.jsonl` |
| Codex | `<home>/.codex/sessions/` | `**/*.jsonl` |
| Gemini (example) | `<home>/.gemini/sessions/` | `**/*.jsonl` |

### JSONL as Primary Monitoring Source

Transcript JSONL is the primary source for session monitoring. It provides richer and more reliable signals than pane capture alone:

- **Structured events**: tool calls, token usage, and turn boundaries are explicitly logged, not inferred from terminal text
- **Complete history**: JSONL files contain the full conversation, not just the visible pane buffer
- **No parsing ambiguity**: structured JSON avoids the fragility of regex-based pane output parsing
- **Cursor-based tailing**: the transcript system can resume from a cursor position after restarts

Pane capture remains a fallback for providers that do not write JSONL logs, and as a supplementary signal for detecting states (like interactive prompts) that are not always reflected in transcript files.

### PollyPM-Owned Transcript Archive

PollyPM does not rely on provider logs as the source of truth. Provider JSONL is ingested, normalized, and written to PollyPM's own transcript archive at `<project>/.pollypm/transcripts/`. This archive uses standardized JSONL and covers every worker session and every Polly conversation about the project. The archive is the canonical record — provider-specific logs are inputs, not the system of record.

### Ingestion Mechanics

A background thread in the PollyPM process continuously tails provider JSONL files for all active sessions. This is a purely mechanical process — no LLM is involved, just file tailing and format normalization.

The ingestion thread works as follows:

1. For each active session, it calls `transcript_sources()` on the session's provider adapter to discover where the provider writes its JSONL logs.
2. It continuously tails those files, reading new events as they are appended.
3. It handles file rotation — new files appearing, old files being renamed by the provider — by re-evaluating the glob patterns from `TranscriptSource` declarations.
4. Each provider event is normalized into PollyPM's standardized JSONL format, stripping provider-specific structure and producing a uniform event schema.
5. Normalized events are written to `<project>/.pollypm/transcripts/<session-id>/`.

This stage runs continuously and is free in terms of LLM cost. It exists solely to produce a canonical, provider-agnostic transcript archive that downstream systems (documentation extraction, monitoring, audit) can consume without knowing anything about individual provider log formats.


## Registration

Provider plugins are discovered through the standard plugin discovery mechanism described in [04-extensibility-and-plugin-system.md](04-extensibility-and-plugin-system.md).

A provider plugin manifest:

```toml
api_version = "1"
name = "gemini-cli"
kind = "provider"
version = "0.1.0"
entrypoint = "gemini_plugin:GeminiAdapter"
capabilities = ["provider", "transcript_source"]
description = "Provider adapter for the Gemini CLI agent"
author = "PollyPM Contributors"
```

The `entrypoint` points to the adapter class. The extension host instantiates it and registers it as an available provider. The `capabilities` field declares that this plugin provides both provider and transcript source interfaces.


## Error Handling

Provider adapters should handle their own errors gracefully:

- `build_launch_command` should raise a descriptive exception if the provider binary is not found or the session configuration is invalid. The core catches this and reports it as a launch failure.
- `collect_usage_snapshot` should never raise. If pane capture fails or output cannot be parsed, return a snapshot with `health="degraded"` and a descriptive summary. The core relies on this method returning a value, not an exception.
- `transcript_sources` should return an empty tuple if the account home is not set or the transcript location does not exist. Discovery is best-effort.


## Testing

Provider adapters are unit-testable in isolation:

- `build_launch_command` and `build_resume_command` are pure functions of their inputs. Test them by passing mock `SessionConfig` and `AccountConfig` objects and asserting on the returned `LaunchCommand`.
- `collect_usage_snapshot` can be tested by mocking the tmux controller's `capture_pane` method to return known pane output and asserting on the returned health classification.
- `transcript_sources` can be tested by passing accounts with known home directories and asserting on the returned paths.

No running tmux session, provider CLI, or PollyPM core is required for adapter unit tests.


## Resolved Decisions

1. **SDK, not RPC.** Providers are in-process Python classes, not external services. This was chosen over a subprocess or gRPC model for simplicity, debuggability, and performance. Provider adapters are lightweight and do not justify the overhead of inter-process communication.

2. **Providers implement the same interface as built-ins.** Claude and Codex use `ProviderAdapterBase` identically to third-party adapters. There is no internal fast path. This guarantees the SDK is complete enough for real use and prevents drift between built-in and external providers.

3. **Health is provider-reported, not inferred.** Each provider knows its own error messages and failure modes. The core applies policy to health classifications without parsing provider-specific output. This keeps the core provider-agnostic and makes adding new providers straightforward.

4. **Transcript JSONL is the primary monitoring source.** Structured JSONL logs provide richer, more reliable monitoring signals than pane capture. Pane capture is retained as a fallback and supplement, but transcript ingestion is the preferred path for providers that support it. PollyPM owns its own transcript archive at `<project>/.pollypm/transcripts/` and does not rely on provider logs as the system of record.


## Cross-Doc References

- Extensibility architecture and plugin system: [04-extensibility-and-plugin-system.md](04-extensibility-and-plugin-system.md)
- Core architecture and domain model: [01-architecture-and-domain.md](01-architecture-and-domain.md)
- Session lifecycle and tmux integration: [03-session-management-and-tmux.md](03-session-management-and-tmux.md)
