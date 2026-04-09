from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pollypm.models import AccountConfig, SessionConfig
from pollypm.provider_sdk import ProviderUsageSnapshot, TranscriptSource


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
        self,
        session: SessionConfig,
        account: AccountConfig,
    ) -> LaunchCommand: ...

    def build_resume_command(
        self,
        session: SessionConfig,
        account: AccountConfig,
    ) -> LaunchCommand | None: ...

    def transcript_sources(
        self,
        account: AccountConfig,
        session: SessionConfig | None = None,
    ) -> tuple[TranscriptSource, ...]: ...

    def collect_usage_snapshot(
        self,
        tmux: object,
        target: str,
        *,
        account: AccountConfig,
        session: SessionConfig,
    ) -> ProviderUsageSnapshot: ...
