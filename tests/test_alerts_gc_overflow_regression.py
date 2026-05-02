"""Regression tests for #1047 — alerts.gc handler / prune_old_data overflow.

Before the fix, ``alerts_gc_handler`` called
``store.prune_old_data(event_days=10**6)``. Inside ``prune_old_data``,
``datetime.now(UTC) - timedelta(days=10**6)`` lands at year ~-712, which
Python's ``datetime`` cannot represent — every alerts.gc tick raised
``OverflowError: date value out of range`` and the job retired to
``failed`` after 3 attempts.

The fix is two-part:

1. ``prune_old_data`` now treats ``event_days=None`` as "skip the events
   prune" (heartbeats prune still runs).
2. ``alerts_gc_handler`` passes ``event_days=None``; the events table's
   tiered retention is owned by ``events_retention_sweep_handler``.

These tests pin both ends so the bug can't come back.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pollypm.storage.state import StateStore


# ---------------------------------------------------------------------------
# 1. ``prune_old_data`` directly — accepts ``None`` for event_days
# ---------------------------------------------------------------------------


def test_prune_old_data_accepts_none_event_days(tmp_path: Path) -> None:
    """``event_days=None`` must skip the events prune without raising."""
    db_path = tmp_path / "state.db"
    with StateStore(db_path) as store:
        result = store.prune_old_data(event_days=None)
    assert result == {"events": 0, "heartbeats": 0}


def test_prune_old_data_default_event_days_still_prunes(tmp_path: Path) -> None:
    """The default 7-day window still works — ``None`` is opt-in."""
    db_path = tmp_path / "state.db"
    with StateStore(db_path) as store:
        # Default kwargs — no overflow path.
        result = store.prune_old_data()
    assert result == {"events": 0, "heartbeats": 0}


# ---------------------------------------------------------------------------
# 2. ``alerts_gc_handler`` end-to-end — no OverflowError
# ---------------------------------------------------------------------------


class _StubProject:
    def __init__(self, state_db: Path, root_dir: Path) -> None:
        self.state_db = state_db
        self.root_dir = root_dir


class _StubConfig:
    """Just enough for ``alerts_gc_handler`` to construct a Supervisor stub."""

    def __init__(self, state_db: Path) -> None:
        self.project = _StubProject(state_db, state_db.parent)


def test_alerts_gc_handler_does_not_raise_overflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving the whole handler must not raise ``OverflowError`` (#1047).

    Patches ``_load_config_and_store`` to hand the handler a real
    ``StateStore`` against ``tmp_path``, and stubs ``Supervisor`` so we
    don't pull in the full supervisor stack — the bug under test lived
    in the ``store.prune_old_data(...)`` call, not in lease release.
    """
    db_path = tmp_path / "state.db"
    state_store = StateStore(db_path)
    config = _StubConfig(db_path)

    @contextmanager
    def _fake_load(_payload):
        try:
            yield (config, state_store)
        finally:
            # Mirror the real helper's ``finally: store.close()``.
            pass

    class _StubSupervisor:
        def __init__(self, _config) -> None:
            pass

        def release_expired_leases(self):
            return []

    from pollypm.plugins_builtin.core_recurring import plugin as _plug

    with patch.object(_plug, "_load_config_and_store", _fake_load), \
         patch("pollypm.supervisor.Supervisor", _StubSupervisor):
        # Before the fix this raised ``OverflowError: date value out of range``.
        result = _plug.alerts_gc_handler({})

    state_store.close()

    assert result["leases_released"] == 0
    # ``event_days=None`` skips the events prune — count is zero by design.
    assert result["events_pruned"] == 0
    assert result["heartbeats_pruned"] == 0


def test_alerts_gc_handler_passes_none_for_event_days(
    tmp_path: Path,
) -> None:
    """Pin the call shape: handler must pass ``event_days=None``.

    A future refactor that resurrects a large literal like ``10**6``
    would silently reintroduce the overflow on real databases; assert
    the explicit ``None`` instead so the test fails loudly.
    """
    db_path = tmp_path / "state.db"
    config = _StubConfig(db_path)

    captured: dict = {}

    class _CapturingStore:
        def prune_old_data(self, *, event_days=None, heartbeat_hours=24):
            captured["event_days"] = event_days
            captured["heartbeat_hours"] = heartbeat_hours
            return {"events": 0, "heartbeats": 0}

        def close(self) -> None:
            pass

    @contextmanager
    def _fake_load(_payload):
        yield (config, _CapturingStore())

    class _StubSupervisor:
        def __init__(self, _config) -> None:
            pass

        def release_expired_leases(self):
            return []

    from pollypm.plugins_builtin.core_recurring import plugin as _plug

    with patch.object(_plug, "_load_config_and_store", _fake_load), \
         patch("pollypm.supervisor.Supervisor", _StubSupervisor):
        _plug.alerts_gc_handler({})

    assert captured["event_days"] is None, (
        "alerts_gc_handler must pass event_days=None to avoid datetime "
        "overflow (#1047). Found: %r" % (captured.get("event_days"),)
    )
