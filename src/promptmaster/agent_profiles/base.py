from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from promptmaster.models import AccountConfig, SessionConfig

if TYPE_CHECKING:
    from promptmaster.config import PromptMasterConfig


@dataclass(slots=True)
class AgentProfileContext:
    config: "PromptMasterConfig"
    session: SessionConfig
    account: AccountConfig
    metadata: dict[str, object] = field(default_factory=dict)


class AgentProfile(Protocol):
    name: str

    def build_prompt(self, context: AgentProfileContext) -> str | None: ...
