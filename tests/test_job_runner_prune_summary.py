"""Plural agreement for the gc_maintenance prune-state summary.

Cycle 104: ``_run_prune_state`` always wrote "Pruned N events,
N heartbeat records" — when the count was 1 the message read
"Pruned 1 events, 1 heartbeat records". The summary lands in the
activity feed where users see it; agree the noun with the count.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from pollypm.job_runner import _BUILTIN_EXECUTORS


class _FakeStore:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def prune_old_data(self) -> dict[str, int]:
        return self._prune_result

    def append_event(self, *, scope: str, sender: str, subject: str, payload: dict) -> None:
        self.events.append(
            {"scope": scope, "sender": sender, "subject": subject, "payload": payload}
        )


def _summary_for(events: int, heartbeats: int) -> str:
    store = _FakeStore()
    store._prune_result = {"events": events, "heartbeats": heartbeats}
    supervisor = SimpleNamespace(store=store, msg_store=store)
    _BUILTIN_EXECUTORS["prune_state"](supervisor, {})
    assert store.events, "expected an activity event when totals > 0"
    summary_blob = store.events[0]["payload"]["message"]
    decoded = json.loads(summary_blob)
    return decoded["summary"]


def test_prune_summary_uses_singular_for_one_event() -> None:
    """Cycle 104 — ``Pruned 1 events`` was the bug; ``Pruned 1 event`` is the fix."""
    summary = _summary_for(events=1, heartbeats=5)
    assert "1 event," in summary
    assert "1 events" not in summary


def test_prune_summary_uses_singular_for_one_heartbeat() -> None:
    summary = _summary_for(events=10, heartbeats=1)
    assert "1 heartbeat record" in summary
    assert "1 heartbeat records" not in summary


def test_prune_summary_uses_plural_for_many() -> None:
    """The plural form must still apply for n != 1."""
    summary = _summary_for(events=4, heartbeats=12)
    assert "4 events" in summary
    assert "12 heartbeat records" in summary


def test_prune_summary_skipped_when_nothing_pruned() -> None:
    """No event when both counts are zero — avoid log spam."""
    store = _FakeStore()
    store._prune_result = {"events": 0, "heartbeats": 0}
    supervisor = SimpleNamespace(store=store, msg_store=store)
    _BUILTIN_EXECUTORS["prune_state"](supervisor, {})
    assert store.events == []
