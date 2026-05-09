"""Tests for :mod:`pollypm.rail_daemon_supervisor`.

The supervisor is the layer-2 watcher that sits above the rail
daemon's in-thread tick retry. Its job is to detect a dead/stuck
daemon (``rail_daemon.pid`` missing, PID dead, or no heartbeat tick
in N seconds) and respawn it. Tests focus on:

* The diagnosis function being pure: same inputs → same outputs.
* The throttle preventing respawn loops.
* The revival path actually invoking the spawn callback exactly once
  per qualifying decision.
* Permission-denied paths bailing without fanning out.

We never spawn a real ``pollypm.rail_daemon`` process here — the
spawn callable is always injected.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pollypm.rail_daemon_supervisor import (
    DEFAULT_REVIVAL_THROTTLE_SECONDS,
    DEFAULT_STALE_TICK_SECONDS,
    RevivalDecision,
    check_and_revive_rail_daemon,
    diagnose_rail_daemon,
    should_revive,
)


# ---------------------------------------------------------------------------
# diagnose_rail_daemon — pure logic
# ---------------------------------------------------------------------------


@pytest.fixture()
def now_utc() -> datetime:
    """Fixed reference time for deterministic age math."""
    return datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC)


def _iso_seconds_ago(now: datetime, seconds: float) -> str:
    return (now - timedelta(seconds=seconds)).isoformat()


def test_missing_pid_file_triggers_revival(tmp_path: Path, now_utc: datetime):
    pid_path = tmp_path / "rail_daemon.pid"
    decision = diagnose_rail_daemon(
        pid_path=pid_path,
        last_tick_iso=None,
        now=now_utc,
    )
    assert decision.state == "missing_pid"
    assert decision.needs_revival is True
    assert "no rail_daemon.pid" in decision.reason


def test_dead_pid_triggers_revival(tmp_path: Path, now_utc: datetime):
    """A PID that no longer exists must be classified as ``dead_process``."""
    pid_path = tmp_path / "rail_daemon.pid"
    # 999999 is overwhelmingly unlikely to be live on any real system.
    pid_path.write_text("999999")
    decision = diagnose_rail_daemon(
        pid_path=pid_path,
        last_tick_iso=None,
        now=now_utc,
    )
    assert decision.state == "dead_process"
    assert decision.needs_revival is True


def test_alive_pid_with_recent_tick_is_alive(tmp_path: Path, now_utc: datetime):
    """Self-PID (live) plus a 30s-ago tick → ``alive``."""
    pid_path = tmp_path / "rail_daemon.pid"
    pid_path.write_text(str(os.getpid()))
    decision = diagnose_rail_daemon(
        pid_path=pid_path,
        last_tick_iso=_iso_seconds_ago(now_utc, 30),
        now=now_utc,
    )
    assert decision.state == "alive"
    assert decision.needs_revival is False
    assert decision.last_tick_age_seconds == pytest.approx(30, abs=1)


def test_alive_pid_with_stale_tick_is_stuck(tmp_path: Path, now_utc: datetime):
    """A live PID that hasn't ticked in > threshold is ``stuck_no_tick``."""
    pid_path = tmp_path / "rail_daemon.pid"
    pid_path.write_text(str(os.getpid()))
    stale = DEFAULT_STALE_TICK_SECONDS + 30
    decision = diagnose_rail_daemon(
        pid_path=pid_path,
        last_tick_iso=_iso_seconds_ago(now_utc, stale),
        now=now_utc,
    )
    assert decision.state == "stuck_no_tick"
    assert decision.needs_revival is True
    assert decision.last_tick_age_seconds is not None
    assert decision.last_tick_age_seconds > DEFAULT_STALE_TICK_SECONDS


def test_alive_pid_no_tick_history_is_alive(tmp_path: Path, now_utc: datetime):
    """Fresh boot with no tick history yet must NOT trigger revival.

    The DB is empty when the daemon just started; we shouldn't
    misclassify "haven't gotten the first tick written yet" as stuck.
    """
    pid_path = tmp_path / "rail_daemon.pid"
    pid_path.write_text(str(os.getpid()))
    decision = diagnose_rail_daemon(
        pid_path=pid_path,
        last_tick_iso=None,
        now=now_utc,
    )
    assert decision.state == "alive"
    assert decision.needs_revival is False


def test_throttle_prevents_immediate_respawn(tmp_path: Path, now_utc: datetime):
    """A revival 10s ago suppresses any new revival regardless of state."""
    pid_path = tmp_path / "rail_daemon.pid"
    # Process is dead AND tick is stale — both signals fire.
    pid_path.write_text("999999")
    last_revival = now_utc - timedelta(seconds=10)
    decision = diagnose_rail_daemon(
        pid_path=pid_path,
        last_tick_iso=_iso_seconds_ago(now_utc, 600),
        last_revival_at=last_revival,
        now=now_utc,
    )
    assert decision.state == "throttled"
    assert decision.needs_revival is False
    assert "throttle" in decision.reason


def test_throttle_expires_after_window(tmp_path: Path, now_utc: datetime):
    """A revival > throttle_seconds ago does NOT block a new one."""
    pid_path = tmp_path / "rail_daemon.pid"
    pid_path.write_text("999999")
    last_revival = now_utc - timedelta(seconds=DEFAULT_REVIVAL_THROTTLE_SECONDS + 5)
    decision = diagnose_rail_daemon(
        pid_path=pid_path,
        last_tick_iso=None,
        last_revival_at=last_revival,
        now=now_utc,
    )
    assert decision.state == "dead_process"
    assert decision.needs_revival is True


def test_pid_file_garbage_treated_as_missing(tmp_path: Path, now_utc: datetime):
    """A PID file with non-integer contents is treated like an absent file."""
    pid_path = tmp_path / "rail_daemon.pid"
    pid_path.write_text("not-a-pid-at-all")
    decision = diagnose_rail_daemon(
        pid_path=pid_path,
        last_tick_iso=None,
        now=now_utc,
    )
    assert decision.state == "missing_pid"


def test_pid_file_zero_treated_as_missing(tmp_path: Path, now_utc: datetime):
    """PID 0 is invalid — treat as no daemon at all."""
    pid_path = tmp_path / "rail_daemon.pid"
    pid_path.write_text("0")
    decision = diagnose_rail_daemon(
        pid_path=pid_path,
        last_tick_iso=None,
        now=now_utc,
    )
    assert decision.state == "missing_pid"


def test_iso_timestamp_with_z_suffix_parses(tmp_path: Path, now_utc: datetime):
    """Older ``messages.created_at`` rows use ``Z`` instead of ``+00:00``."""
    pid_path = tmp_path / "rail_daemon.pid"
    pid_path.write_text(str(os.getpid()))
    iso_z = (now_utc - timedelta(seconds=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
    decision = diagnose_rail_daemon(
        pid_path=pid_path,
        last_tick_iso=iso_z,
        now=now_utc,
    )
    assert decision.state == "alive"
    assert decision.last_tick_age_seconds == pytest.approx(15, abs=1)


def test_iso_timestamp_unparseable_treated_as_no_history(
    tmp_path: Path, now_utc: datetime,
):
    """Garbled ``last_tick_iso`` must NOT raise — treat as no history."""
    pid_path = tmp_path / "rail_daemon.pid"
    pid_path.write_text(str(os.getpid()))
    decision = diagnose_rail_daemon(
        pid_path=pid_path,
        last_tick_iso="not a real timestamp",
        now=now_utc,
    )
    assert decision.state == "alive"
    assert decision.last_tick_age_seconds is None


def test_should_revive_helper(tmp_path: Path, now_utc: datetime):
    pid_path = tmp_path / "rail_daemon.pid"
    pid_path.write_text("999999")
    decision = diagnose_rail_daemon(
        pid_path=pid_path,
        last_tick_iso=None,
        now=now_utc,
    )
    assert should_revive(decision) is True

    pid_path.write_text(str(os.getpid()))
    healthy = diagnose_rail_daemon(
        pid_path=pid_path,
        last_tick_iso=_iso_seconds_ago(now_utc, 5),
        now=now_utc,
    )
    assert should_revive(healthy) is False


# ---------------------------------------------------------------------------
# check_and_revive_rail_daemon — orchestration
# ---------------------------------------------------------------------------


class _SpawnRecorder:
    """Test double for ``cli._spawn_rail_daemon``.

    Records every config_path it was invoked with so tests can assert
    not just "did revival happen" but "did revival happen at all" /
    "with the right config".
    """

    def __init__(self, raise_on_call: bool = False) -> None:
        self.calls: list[Path] = []
        self.raise_on_call = raise_on_call

    def __call__(self, config_path: Path) -> None:
        self.calls.append(config_path)
        if self.raise_on_call:
            raise RuntimeError("simulated spawn failure")


def test_revive_calls_spawner_when_pid_missing(tmp_path: Path):
    config_path = tmp_path / "pollypm.toml"
    pid_path = tmp_path / "rail_daemon.pid"
    spawner = _SpawnRecorder()

    result = check_and_revive_rail_daemon(
        config_path=config_path,
        pid_path=pid_path,
        last_tick_iso=None,
        last_revival_at=None,
        spawn_fn=spawner,
        emit_audit=False,
    )

    assert result.revived is True
    assert result.spawn_error is None
    assert spawner.calls == [config_path]
    assert result.decision.state == "missing_pid"


def test_revive_skips_when_alive(tmp_path: Path):
    config_path = tmp_path / "pollypm.toml"
    pid_path = tmp_path / "rail_daemon.pid"
    pid_path.write_text(str(os.getpid()))
    spawner = _SpawnRecorder()
    last_tick_iso = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()

    result = check_and_revive_rail_daemon(
        config_path=config_path,
        pid_path=pid_path,
        last_tick_iso=last_tick_iso,
        last_revival_at=None,
        spawn_fn=spawner,
        emit_audit=False,
    )

    assert result.revived is False
    assert spawner.calls == []
    assert result.decision.state == "alive"


def test_revive_skips_when_throttled(tmp_path: Path):
    config_path = tmp_path / "pollypm.toml"
    pid_path = tmp_path / "rail_daemon.pid"
    pid_path.write_text("999999")
    spawner = _SpawnRecorder()
    last_revival = datetime.now(UTC) - timedelta(seconds=5)

    result = check_and_revive_rail_daemon(
        config_path=config_path,
        pid_path=pid_path,
        last_tick_iso=None,
        last_revival_at=last_revival,
        spawn_fn=spawner,
        emit_audit=False,
    )

    assert result.revived is False
    assert result.decision.state == "throttled"
    assert spawner.calls == []


def test_revive_unlinks_stale_pid_file(tmp_path: Path):
    """``stuck_no_tick`` must clear the PID file before respawning."""
    config_path = tmp_path / "pollypm.toml"
    pid_path = tmp_path / "rail_daemon.pid"
    # Use our own PID so the supervisor sees "alive but stuck".
    pid_path.write_text(str(os.getpid()))
    spawner = _SpawnRecorder()
    stale_tick = (datetime.now(UTC) - timedelta(seconds=DEFAULT_STALE_TICK_SECONDS + 60)).isoformat()

    # Inject a no-op terminator (we can't actually SIGKILL ourselves
    # in a unit test). The supervisor's signal helper still fires
    # SIGTERM at our PID — so we patch out the actual kill via a
    # custom sleep_fn that immediately reports the PID as gone.
    import pollypm.rail_daemon_supervisor as _mod

    # We call _terminate_with_grace via the supervisor; intercept the
    # os.kill so we don't actually signal pytest.
    real_kill = _mod.os.kill
    def fake_kill(pid: int, sig: int) -> None:
        # Pretend SIGTERM took: subsequent ``os.kill(pid, 0)`` should
        # raise ProcessLookupError. Easiest way: track that we sent
        # the signal and forward 0-checks to the live PID.
        if sig == 0:
            return real_kill(pid, sig)
        # Don't actually kill; treat as accepted.
        return None

    _mod.os.kill = fake_kill
    try:
        result = check_and_revive_rail_daemon(
            config_path=config_path,
            pid_path=pid_path,
            last_tick_iso=stale_tick,
            last_revival_at=None,
            spawn_fn=spawner,
            sleep_fn=lambda _s: None,
            emit_audit=False,
        )
    finally:
        _mod.os.kill = real_kill

    # The pid file is unlinked even though our fake_kill didn't really
    # kill the process — the unlink is unconditional after the term step.
    assert not pid_path.exists()
    assert spawner.calls == [config_path]


def test_spawn_failure_is_reported(tmp_path: Path):
    config_path = tmp_path / "pollypm.toml"
    pid_path = tmp_path / "rail_daemon.pid"
    spawner = _SpawnRecorder(raise_on_call=True)

    result = check_and_revive_rail_daemon(
        config_path=config_path,
        pid_path=pid_path,
        last_tick_iso=None,
        last_revival_at=None,
        spawn_fn=spawner,
        emit_audit=False,
    )

    assert result.revived is False
    assert result.spawn_error is not None
    assert "RuntimeError" in result.spawn_error


def test_revive_idempotent_under_throttle(tmp_path: Path):
    """Two back-to-back calls only spawn once because of the throttle."""
    config_path = tmp_path / "pollypm.toml"
    pid_path = tmp_path / "rail_daemon.pid"
    spawner = _SpawnRecorder()

    first = check_and_revive_rail_daemon(
        config_path=config_path,
        pid_path=pid_path,
        last_tick_iso=None,
        last_revival_at=None,
        spawn_fn=spawner,
        emit_audit=False,
    )
    assert first.revived is True

    # Simulate what the cockpit/cli does: persist last_revival_at and
    # call again with no time passed.
    second = check_and_revive_rail_daemon(
        config_path=config_path,
        pid_path=pid_path,
        last_tick_iso=None,
        last_revival_at=datetime.now(UTC),
        spawn_fn=spawner,
        emit_audit=False,
    )
    assert second.revived is False
    assert second.decision.state == "throttled"
    assert len(spawner.calls) == 1
