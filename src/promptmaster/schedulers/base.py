from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from promptmaster.supervisor import Supervisor


@dataclass(slots=True)
class ScheduledJob:
    job_id: str
    kind: str
    run_at: datetime
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    interval_seconds: int | None = None
    last_error: str | None = None


class SchedulerBackend(Protocol):
    name: str

    def schedule(
        self,
        supervisor: "Supervisor",
        *,
        kind: str,
        run_at: datetime,
        payload: dict[str, Any] | None = None,
        interval_seconds: int | None = None,
    ) -> ScheduledJob: ...

    def list_jobs(self, supervisor: "Supervisor") -> list[ScheduledJob]: ...

    def run_due(self, supervisor: "Supervisor", *, now: datetime | None = None) -> list[ScheduledJob]: ...
