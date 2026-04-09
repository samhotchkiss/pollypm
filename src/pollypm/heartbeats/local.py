from __future__ import annotations

from pollypm.heartbeats.base import HeartbeatBackend
from pollypm.storage.state import AlertRecord


class LocalHeartbeatBackend(HeartbeatBackend):
    name = "local"

    def run(self, supervisor, *, snapshot_lines: int = 200) -> list[AlertRecord]:
        return supervisor._run_heartbeat_local(snapshot_lines=snapshot_lines)
