from __future__ import annotations

from typing import Protocol

from pollypm.models import AccountConfig, ProjectSettings
from pollypm.providers.base import LaunchCommand


class RuntimeAdapter(Protocol):
    def wrap_command(
        self,
        command: LaunchCommand,
        account: AccountConfig,
        project: ProjectSettings,
    ) -> str: ...
