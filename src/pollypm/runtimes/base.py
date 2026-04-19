from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pollypm.models import AccountConfig, ProjectSettings
from pollypm.providers.base import LaunchCommand


@dataclass(slots=True)
class WrappedRuntimeCommand:
    """Structured runtime invocation for non-shell execution paths."""

    argv: list[str]
    env: dict[str, str] | None = None
    cwd: Path | None = None


class RuntimeAdapter(Protocol):
    def wrap_command(
        self,
        command: LaunchCommand,
        account: AccountConfig,
        project: ProjectSettings,
    ) -> str: ...

    def wrap_command_exec(
        self,
        command: LaunchCommand,
        account: AccountConfig,
        project: ProjectSettings,
    ) -> WrappedRuntimeCommand: ...
