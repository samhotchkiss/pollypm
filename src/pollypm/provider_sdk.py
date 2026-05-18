from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pollypm.models import AccountConfig, SessionConfig

if TYPE_CHECKING:
    from pollypm.tmux.client import TmuxClient


@dataclass(slots=True)
class TranscriptSource:
    root: Path
    pattern: str = "*.jsonl"
    description: str = ""


@dataclass(slots=True)
class LaunchCommand:
    argv: list[str]
    env: dict[str, str]
    cwd: Path
    resume_argv: list[str] | None = None
    resume_marker: Path | None = None
    initial_input: str | None = None
    fresh_launch_marker: Path | None = None


@dataclass(slots=True)
class ProviderUsageSnapshot:
    health: str = "unknown"
    summary: str = "usage unavailable"
    raw_text: str = ""
    plan: str = "unknown"
    used_pct: int | None = None
    remaining_pct: int | None = None
    reset_at: str | None = None
    period_label: str | None = None
    available_at: str | None = None
    access_expires_at: str | None = None
    updated_at: str | None = None


class ProviderAdapterBase(ABC):
    name: str
    binary: str

    def is_available(self) -> bool:
        import shutil

        return shutil.which(self.binary) is not None

    @abstractmethod
    def build_launch_command(self, session: SessionConfig, account: AccountConfig) -> LaunchCommand: ...

    def build_resume_command(
        self,
        session: SessionConfig,
        account: AccountConfig,
    ) -> LaunchCommand | None:
        return None

    def transcript_sources(
        self,
        account: AccountConfig,
        session: SessionConfig | None = None,
    ) -> tuple[TranscriptSource, ...]:
        return ()

    def collect_usage_snapshot(
        self,
        tmux: "TmuxClient",
        target: str,
        *,
        account: AccountConfig,
        session: SessionConfig,
    ) -> ProviderUsageSnapshot:
        raise NotImplementedError(f"{self.name} does not implement usage collection")
