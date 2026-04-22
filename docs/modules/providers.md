**Last Verified:** 2026-04-22

## Summary

A `ProviderAdapter` builds the launch command and environment for a provider CLI, reports whether the binary is available, declares its transcript sources, and collects a live usage snapshot via a probe pane. The two shipped providers are **Claude** (the `claude` CLI) and **Codex** (`codex`). Aider, Gemini, and OpenCode are future work.

`ProviderAdapter` is intentionally minimal. It does not manage accounts, pick runtimes, place windows, or send input. Those are the Supervisor / session-service / account layer's jobs. The adapter's job is: *given this account, return a `LaunchCommand` I can execute, and point me at the transcripts you produce.*

Touch this module when adding a new provider, or when a provider's CLI changes (flags, env vars, transcript layout). Do not add policy here.

## Core Contracts

```python
# src/pollypm/providers/base.py
@dataclass(slots=True)
class LaunchCommand:
    argv: list[str]
    env: dict[str, str]
    cwd: Path
    resume_argv: list[str] | None = None
    resume_marker: Path | None = None
    initial_input: str | None = None
    fresh_launch_marker: Path | None = None

class ProviderAdapter(Protocol):
    name: str
    binary: str

    def is_available(self) -> bool: ...
    def build_launch_command(
        self, session: SessionConfig, account: AccountConfig,
    ) -> LaunchCommand: ...
    def build_resume_command(
        self, session: SessionConfig, account: AccountConfig,
    ) -> LaunchCommand | None: ...
    def transcript_sources(
        self, account: AccountConfig, session: SessionConfig | None = None,
    ) -> tuple[TranscriptSource, ...]: ...
    def collect_usage_snapshot(
        self, tmux, target, *, account, session,
    ) -> ProviderUsageSnapshot: ...
```

Registry:

```python
# src/pollypm/providers/__init__.py
def get_provider(name: str) -> ProviderAdapter: ...
```

## File Structure

- `src/pollypm/providers/base.py` — `ProviderAdapter` protocol and `LaunchCommand`.
- `src/pollypm/providers/args.py` — shared argument helpers.
- `src/pollypm/providers/__init__.py` — registry lookup.
- `src/pollypm/providers/claude/` — Claude adapter, prompt injection, resume logic, transcript source.
- `src/pollypm/providers/codex/` — Codex adapter and transcript source.
- `src/pollypm/provider_sdk.py` — `TranscriptSource` and `ProviderUsageSnapshot` types.
- `src/pollypm/plugins_builtin/claude/plugin.py` — registers `ClaudeAdapter` as provider `"claude"`.
- `src/pollypm/plugins_builtin/codex/plugin.py` — registers `CodexAdapter` as provider `"codex"`.

## Implementation Details

- **Claude resume.** `providers/claude/resume.py` exposes `recorded_session_id` and `session_ids` helpers. The Supervisor uses these to stitch a fresh launch to a prior Claude session id when the user wants to continue.
- **Env and home priming.** `AccountConfig.home` points to an isolated account home (`~/.pollypm/homes/<account>/` by convention). The adapter returns env vars (e.g. `ANTHROPIC_*`, `CLAUDE_*`) that tell the CLI to use that home instead of `~/`.
- **Account runtime status.** Adapters do *not* decide whether an account is healthy. The account manager (`pollypm.accounts`) owns login detection, cached usage, `auth-broken`, `exhausted`, `provider_outage`, and `blocked` classifications. Providers only *expose* usage via `collect_usage_snapshot`.
- **Transcript sources.** Each adapter lists one or more `TranscriptSource` objects that point at the JSONL files the provider CLI writes. `transcript_ingest` tails these and rewrites them into the standardized PollyPM archive (`<project>/.pollypm/transcripts/`).
- **Availability.** `is_available` runs `shutil.which(binary)` and a cheap version check. `pm doctor` surfaces the result.

## Related Docs

- [modules/runtimes.md](runtimes.md) — wraps `LaunchCommand` for local vs. Docker execution.
- [modules/accounts.md](accounts.md) — owns login state and cached usage.
- [modules/transcript-ingest.md](transcript-ingest.md) — consumes `transcript_sources`.
- [modules/supervisor.md](supervisor.md) — orchestrates launches via the planner + session service.
