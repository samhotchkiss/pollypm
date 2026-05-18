from __future__ import annotations

from typing import Protocol

from pollypm.models import AccountConfig, SessionConfig
from pollypm.provider_sdk import (
    LaunchCommand,
    ProviderUsageSnapshot,
    TranscriptSource,
)

# ``LaunchCommand`` is defined in :mod:`pollypm.provider_sdk` so that
# ``provider_sdk`` no longer needs a (formerly-cyclic) TYPE_CHECKING import
# from this module. Re-exported here for backwards compatibility — many
# call sites still ``from pollypm.providers.base import LaunchCommand``.
__all__ = [
    "LaunchCommand",
    "ProviderAdapter",
    "ProviderUsageSnapshot",
    "TranscriptSource",
]


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
