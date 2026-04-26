"""Plural agreement for the heartbeat sweep completion summary.

Cycle 116: ``LocalHeartbeatBackend.run`` and ``Supervisor.run_heartbeat_sweep``
both record an activity-feed event with the message
``Heartbeat sweep completed with N open alerts``. The noun was hard-
pluralised, so a sweep that left exactly one open alert produced
``with 1 open alerts``. Match the noun to the count.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from pollypm.heartbeats.local import LocalHeartbeatBackend


def _decode_event_summary(events: list[dict]) -> str | None:
    for kind, _scope, message in events:
        if kind != "heartbeat":
            continue
        try:
            decoded = json.loads(message)
        except (TypeError, ValueError):
            continue
        return decoded.get("summary")
    return None


def _make_api(open_alerts: int) -> SimpleNamespace:
    events: list[tuple[str, str, str]] = []

    def _alert(idx: int) -> SimpleNamespace:
        # Realistic enough: ``_process_unmanaged_windows`` reads
        # ``session_name`` and ``alert_type``. Use non-heartbeat scopes
        # so the sweep treats them as plain pre-existing alerts.
        return SimpleNamespace(
            session_name=f"worker_{idx}",
            alert_type="something_weird",
        )

    class _Api:
        def list_sessions(self):
            return []

        def list_unmanaged_windows(self):
            return []

        def open_alerts(self):
            return [_alert(i) for i in range(open_alerts)]

        def record_event(self, scope, kind, message):
            events.append((kind, scope, message))

    api = _Api()
    api.events = events
    return api


def test_local_backend_uses_singular_alert_when_one_open() -> None:
    api = _make_api(open_alerts=1)
    LocalHeartbeatBackend().run(api)
    summary = _decode_event_summary([(k, s, m) for k, s, m in api.events])
    assert summary is not None
    assert "1 open alert" in summary
    assert "1 open alerts" not in summary


def test_local_backend_uses_plural_alerts_when_many_open() -> None:
    api = _make_api(open_alerts=4)
    LocalHeartbeatBackend().run(api)
    summary = _decode_event_summary([(k, s, m) for k, s, m in api.events])
    assert summary is not None
    assert "4 open alerts" in summary


def test_local_backend_uses_plural_alerts_when_zero_open() -> None:
    """Zero is grammatically plural ("no alerts" / "0 alerts")."""
    api = _make_api(open_alerts=0)
    LocalHeartbeatBackend().run(api)
    summary = _decode_event_summary([(k, s, m) for k, s, m in api.events])
    assert summary is not None
    assert "0 open alerts" in summary
