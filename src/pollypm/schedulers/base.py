from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(slots=True)
class ScheduledJob:
    job_id: str
    kind: str
    run_at: datetime
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    interval_seconds: int | None = None
    last_error: str | None = None


class SchedulerHost(Protocol):
    """Structural host surface required by scheduler backends.

    ``pollypm.supervisor`` imports the scheduler facade at module load, so
    the scheduler protocol cannot type-import ``Supervisor`` without
    reintroducing the #1367 static import back-edge. Scheduler backends only
    need the config/message-store surface below and an opaque object for job
    executors, so a structural protocol keeps ownership local.
    """

    config: Any
    msg_store: Any


class SchedulerBackend(Protocol):
    name: str

    def schedule(
        self,
        supervisor: SchedulerHost,
        *,
        kind: str,
        run_at: datetime,
        payload: dict[str, Any] | None = None,
        interval_seconds: int | None = None,
    ) -> ScheduledJob: ...

    def list_jobs(self, supervisor: SchedulerHost) -> list[ScheduledJob]: ...

    def run_due(self, supervisor: SchedulerHost, *, now: datetime | None = None) -> list[ScheduledJob]: ...
