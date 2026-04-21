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


__all__ = [
    "CliAvailability",
    "ConnectedAccount",
    "LoginPreferences",
    "OnboardingResult",
    "ProviderChoice",
]
