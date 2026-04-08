from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from promptmaster.storage.state import AlertRecord

if TYPE_CHECKING:
    from promptmaster.supervisor import Supervisor


class HeartbeatBackend(Protocol):
    name: str

    def run(self, supervisor: "Supervisor", *, snapshot_lines: int = 200) -> list[AlertRecord]: ...
