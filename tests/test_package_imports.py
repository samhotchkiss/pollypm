from __future__ import annotations

import importlib


def test_first_party_heartbeat_and_inbox_sweep_imports_resolve() -> None:
    heartbeat = importlib.import_module("pollypm.heartbeat")
    heartbeat_boot = importlib.import_module("pollypm.heartbeat.boot")
    inbox_sweep = importlib.import_module("pollypm.inbox_sweep")

    assert hasattr(heartbeat, "Roster")
    assert hasattr(heartbeat_boot, "HeartbeatRail")
    assert hasattr(inbox_sweep, "sweep_stale_notifies")
