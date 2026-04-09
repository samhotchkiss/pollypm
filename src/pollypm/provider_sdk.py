from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pollypm.models import AccountConfig, SessionConfig

if TYPE_CHECKING:
    from pollypm.providers.base import LaunchCommand
    from pollypm.tmux.client import TmuxClient


@dataclass(slots=True)
class TranscriptSource:
    root: Path
    pattern: str = "*.jsonl"
    description: str = ""


@dataclass(slots=True)
class ProviderUsageSnapshot:
    health: str = "unknown"
    summary: str = "usage unavailable"
    raw_text: str = ""
    plan: str = "unknown"
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
    def build_launch_command(self, session: SessionConfig, account: AccountConfig) -> "LaunchCommand": ...

    def build_resume_command(
        self,
        session: SessionConfig,
        account: AccountConfig,
    ) -> "LaunchCommand | None":
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
