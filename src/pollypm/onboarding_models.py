from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pollypm.models import ProviderKind


@dataclass(slots=True)
class ConnectedAccount:
    provider: ProviderKind
    email: str
    account_name: str
    home: Path


@dataclass(slots=True)
class CliAvailability:
    provider: ProviderKind
    label: str
    binary: str
    installed: bool


@dataclass(slots=True)
class ProviderChoice:
    key: str
    label: str
    provider: ProviderKind | None


@dataclass(slots=True)
class LoginPreferences:
    codex_headless: bool = False


@dataclass(slots=True)
class OnboardingResult:
    config_path: Path
    launch_requested: bool = False
    seeded_demo_project_key: str | None = None
    seeded_demo_task_id: str | None = None

    @property
    def parent(self) -> Path:
        return self.config_path.parent

    def resolve(self) -> Path:
        return self.config_path.resolve()

    def __str__(self) -> str:
        return str(self.config_path)


__all__ = [
    "CliAvailability",
    "ConnectedAccount",
    "LoginPreferences",
    "OnboardingResult",
    "ProviderChoice",
]
