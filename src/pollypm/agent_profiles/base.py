from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from pollypm.models import AccountConfig, SessionConfig

if TYPE_CHECKING:
    from pollypm.config import PollyPMConfig


@dataclass(slots=True)
class AgentProfileContext:
    config: "PollyPMConfig"
    session: SessionConfig
    account: AccountConfig
    metadata: dict[str, object] = field(default_factory=dict)


class AgentProfile(Protocol):
    name: str

    def build_prompt(self, context: AgentProfileContext) -> str | None: ...
