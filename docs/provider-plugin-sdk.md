# Provider Plugin SDK

PollyPM exposes a stable provider SDK in `promptmaster.provider_sdk` for authors who want to add a new CLI provider without editing core orchestration code.

## What The SDK Covers

- launch commands
- optional resume commands through the launch command contract
- transcript discovery roots
- usage snapshot collection
- provider-specific health parsing

## Core Types

Import these from `promptmaster.provider_sdk`:

- `ProviderAdapterBase`
- `ProviderUsageSnapshot`
- `TranscriptSource`

## Provider Contract

Subclass `ProviderAdapterBase` and implement:

- `build_launch_command(session, account)`
- `collect_usage_snapshot(tmux, target, account=..., session=...)`

Optional hooks:

- `build_resume_command(session, account)`
- `transcript_sources(account, session=None)`

The launch command still carries the provider-specific resume args/marker so PollyPM can restart controller sessions in place.

## Example

```python
from promptmaster.models import AccountConfig, SessionConfig
from promptmaster.provider_sdk import ProviderAdapterBase, ProviderUsageSnapshot, TranscriptSource
from promptmaster.providers.base import LaunchCommand


class GeminiAdapter(ProviderAdapterBase):
    name = "gemini"
    binary = "gemini"

    def build_launch_command(self, session: SessionConfig, account: AccountConfig) -> LaunchCommand:
        argv = [self.binary, *session.args]
        if session.prompt:
            argv.append(session.prompt)
        return LaunchCommand(argv=argv, env=dict(account.env), cwd=session.cwd)

    def transcript_sources(self, account: AccountConfig, session: SessionConfig | None = None):
        if account.home is None:
            return ()
        return (TranscriptSource(root=account.home / ".gemini" / "sessions", pattern="**/*.jsonl"),)

    def collect_usage_snapshot(self, tmux, target: str, *, account: AccountConfig, session: SessionConfig):
        text = tmux.capture_pane(target, lines=320)
        return ProviderUsageSnapshot(health="healthy", summary="usage available", raw_text=text)
```

## Built-In Providers

Claude and Codex now implement the SDK directly. That keeps third-party providers aligned with the same launch, transcript, and usage hooks PollyPM uses internally.
