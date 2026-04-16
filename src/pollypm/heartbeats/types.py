"""Protocol-level dataclasses for the heartbeat plugin family.

These types are the stable surface the ``HeartbeatAPI`` / ``HeartbeatBackend``
protocols are typed against. Keeping them here (rather than in ``storage.state``)
means backends don't need to import any concrete storage module to satisfy the
protocol — the rail is responsible for adapting its concrete records into the
``Alert`` shape when returning them through the API.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Alert:
    """Protocol-level view of an open alert.

    ``alert_id`` is optional for forward compatibility with backends that do
    not assign stable IDs; built-in SQLite storage always populates it.
    """

    session_name: str
    alert_type: str
    severity: str
    message: str
    status: str
    created_at: str
    updated_at: str
    alert_id: int | None = None


__all__ = ["Alert"]
