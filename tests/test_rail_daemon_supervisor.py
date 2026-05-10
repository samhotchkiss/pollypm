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
* PID-identity verification preventing accidental kills of unrelated
  user processes whose PID was reused after the daemon died.
* Concurrent-revival lock preventing two callers from spawning two
  daemons via a unlink-then-spawn race.
* Cron path opting out of ``missing_pid`` revival so it doesn't race
  the cockpit's in-process rail.

We never spawn a real ``pollypm.rail_daemon`` process here — the
spawn callable is always injected.
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import pollypm.rail_daemon_supervisor as _supervisor_mod
from pollypm.rail_daemon_supervisor import (
    DEFAULT_REVIVAL_THROTTLE_SECONDS,
    DEFAULT_STALE_TICK_SECONDS,
    RevivalDecision,
    check_and_revive_rail_daemon,
    diagnose_rail_daemon,
    should_revive,
)


@pytest.fixture(autouse=True)
def _stub_pid_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default identity check: any live PID looks like our daemon.

    The supervisor verifies a live PID's argv to avoid signaling an
    unrelated process whose PID was reused. Unit tests use
    ``os.getpid()`` (the pytest process) as a stand-in for "alive PID";
    pytest's argv obviously doesn't match ``pollypm.rail_daemon``, so
    without this override every "alive" test would classify as
    ``dead_process``. Tests that exercise the identity check explicitly
    re-patch this helper.
    """
    monkeypatch.setattr(
        _supervisor_mod, "_pid_matches_daemon", lambda pid, cfg: True,
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


# ---------------------------------------------------------------------------
# CRITICAL-1 — PID identity verification
# ---------------------------------------------------------------------------
#
# The supervisor records a PID and later signals it. Between record and
# signal, the original daemon can die and the kernel can hand the same
# PID number to an unrelated user process (an editor, a shell, anything).
# Without identity verification, ``revive_if_needed`` would SIGTERM that
# innocent process. These tests pin the safety guard.


def test_unrelated_live_pid_classified_as_dead_not_stuck(
    tmp_path: Path, now_utc: datetime, monkeypatch: pytest.MonkeyPatch,
):
    """A live PID whose argv is NOT our daemon must classify as dead_process.

    The autouse stub above forces identity match; this test overrides
    it to simulate the real-world PID-reuse case where the recorded
    PID is now an unrelated process. The supervisor MUST NOT classify
    that as ``stuck_no_tick`` — doing so would queue it for SIGTERM.
    """
    monkeypatch.setattr(
        _supervisor_mod, "_pid_matches_daemon", lambda pid, cfg: False,
    )
    pid_path = tmp_path / "rail_daemon.pid"
    pid_path.write_text(str(os.getpid()))  # live PID, but NOT our daemon
    stale_tick = _iso_seconds_ago(now_utc, DEFAULT_STALE_TICK_SECONDS + 60)
    decision = diagnose_rail_daemon(
        pid_path=pid_path,
        last_tick_iso=stale_tick,
        now=now_utc,
        config_path=tmp_path / "pollypm.toml",
    )
    # Without the identity check this would be ``stuck_no_tick`` and a
    # SIGTERM would follow. With the check, it's ``dead_process`` —
    # safe: the action path skips signaling for dead_process entirely.
    assert decision.state == "dead_process"
    assert decision.needs_revival is True


def test_revive_skips_signaling_when_pid_identity_mismatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Even if classify-state was bypassed, the action path re-checks identity.

    Belt-and-suspenders: if a bug or race promotes a non-daemon PID to
    ``stuck_no_tick``, the terminate helper still verifies identity
    before sending SIGTERM and reports ``identity_mismatch`` instead.
    """
    config_path = tmp_path / "pollypm.toml"
    pid_path = tmp_path / "rail_daemon.pid"
    pid_path.write_text(str(os.getpid()))
    spawner = _SpawnRecorder()

    # Force the classifier to say "stuck_no_tick" even though the PID
    # isn't our daemon. The terminate helper must still refuse to kill.
    monkeypatch.setattr(
        _supervisor_mod, "_pid_matches_daemon", lambda pid, cfg: False,
    )

    sent_signals: list[tuple[int, int]] = []
    real_kill = os.kill

    def tracking_kill(pid: int, sig: int) -> None:
        sent_signals.append((pid, sig))
        if sig == 0:
            return real_kill(pid, sig)
        # Don't actually signal; test cares whether we ATTEMPTED to kill.

    monkeypatch.setattr(_supervisor_mod.os, "kill", tracking_kill)

    # Synthesize the stuck_no_tick path by passing a stale tick — but
    # because identity returns False, classify_state will say
    # dead_process. To test the terminate helper directly, call it.
    result_signal = _supervisor_mod._terminate_with_grace(
        os.getpid(),
        sleep_fn=lambda _s: None,
        config_path=config_path,
    )
    assert result_signal == "identity_mismatch"
    # Only the os.kill(pid, 0) liveness probe is allowed; no SIGTERM.
    real_signals = [sig for (_pid, sig) in sent_signals if sig != 0]
    assert real_signals == [], f"unexpected signals sent: {real_signals!r}"


# Resolve the real (unstubbed) helper once at module load so identity-
# helper tests can call it directly. The autouse fixture replaces the
# attribute on the module; this captured reference dodges that.
_REAL_PID_MATCHES = _supervisor_mod._pid_matches_daemon


def test_pid_matches_daemon_uses_cmdline(monkeypatch: pytest.MonkeyPatch):
    """``_pid_matches_daemon`` must consult cmdline, not just liveness.

    Verifies the helper inspects the recorded process's argv (via
    ``/proc/<pid>/cmdline`` on Linux or ``ps -p <pid> -o args=`` on
    macOS / fallback) and rejects PIDs whose argv lacks the
    ``pollypm.rail_daemon`` marker.
    """
    # Stub the cmdline reader so we can exercise the helper's matching
    # logic on synthetic argv strings without spawning real processes.
    monkeypatch.setattr(
        _supervisor_mod,
        "_read_pid_command_line",
        lambda pid: "/usr/bin/vim /tmp/notes.md",
    )
    assert _REAL_PID_MATCHES(12345, Path("/x/pollypm.toml")) is False

    monkeypatch.setattr(
        _supervisor_mod,
        "_read_pid_command_line",
        lambda pid: "/usr/bin/python -m pollypm.rail_daemon --config /x/pollypm.toml",
    )
    assert _REAL_PID_MATCHES(12345, Path("/x/pollypm.toml")) is True

    # Same daemon, but a DIFFERENT workspace's config — must not match.
    monkeypatch.setattr(
        _supervisor_mod,
        "_read_pid_command_line",
        lambda pid: "/usr/bin/python -m pollypm.rail_daemon --config /y/pollypm.toml",
    )
    assert _REAL_PID_MATCHES(12345, Path("/x/pollypm.toml")) is False

    # Daemon with no explicit --config (legacy default) — accept as ours.
    monkeypatch.setattr(
        _supervisor_mod,
        "_read_pid_command_line",
        lambda pid: "/usr/bin/python -m pollypm.rail_daemon",
    )
    assert _REAL_PID_MATCHES(12345, Path("/x/pollypm.toml")) is True

    # No cmdline available at all (process gone, ps failed) — fail safe.
    monkeypatch.setattr(
        _supervisor_mod, "_read_pid_command_line", lambda pid: None,
    )
    assert _REAL_PID_MATCHES(12345, Path("/x/pollypm.toml")) is False


# ---------------------------------------------------------------------------
# CRITICAL-2 — concurrent revival must produce exactly one spawn
# ---------------------------------------------------------------------------


def test_concurrent_revival_spawns_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Two threads racing into ``check_and_revive_rail_daemon`` → one spawn.

    Without the supervisor lock this race produces TWO spawns: both
    threads diagnose missing_pid, both unlink, both invoke the spawner.
    The fix wraps the kill-unlink-spawn critical section in a
    cross-process flock; this test exercises the in-process equivalent
    (flock is per-fd so one process with two threads still serializes).

    Determinism: each thread blocks on a barrier until both are armed,
    then both call the supervisor. The lock is the only thing
    serializing them — no sleeps required.
    """
    config_path = tmp_path / "pollypm.toml"
    pid_path = tmp_path / "rail_daemon.pid"

    spawn_calls: list[Path] = []
    spawn_lock = threading.Lock()
    barrier = threading.Barrier(2)

    def spawner(cfg: Path) -> None:
        # The first spawn must write a fresh PID file so the SECOND
        # thread (when it eventually gets the lock) sees a healthy
        # daemon and stands down.
        with spawn_lock:
            spawn_calls.append(cfg)
            pid_path.write_text(str(os.getpid()))

    results: list = [None, None]

    def runner(idx: int) -> None:
        barrier.wait()  # release both threads simultaneously
        results[idx] = check_and_revive_rail_daemon(
            config_path=config_path,
            pid_path=pid_path,
            last_tick_iso=None,
            last_revival_at=None,
            spawn_fn=spawner,
            emit_audit=False,
        )

    t1 = threading.Thread(target=runner, args=(0,))
    t2 = threading.Thread(target=runner, args=(1,))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert not t1.is_alive() and not t2.is_alive()

    # CRITICAL: only one of the two callers may have actually spawned.
    assert len(spawn_calls) == 1, (
        f"expected exactly one spawn under lock, got {len(spawn_calls)}"
    )
    revived_count = sum(1 for r in results if r and r.revived)
    assert revived_count == 1
    # The other caller should have observed a healthy / no-need state.
    standby = next(r for r in results if r is not None and not r.revived)
    assert standby.spawn_error in (None, "lock_busy")


def test_post_lock_recheck_stands_down_when_daemon_revived_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """If a sibling supervisor spawned while we waited on the lock, stand down.

    Critical-2 has two layers of defense: the flock serializes callers,
    and the post-lock recheck verifies the world hasn't changed while
    we waited. This test exercises the recheck independently — we
    pretend to acquire the lock cleanly but a fresh PID file appeared
    between the initial diagnose and the body of the critical section.
    """
    config_path = tmp_path / "pollypm.toml"
    pid_path = tmp_path / "rail_daemon.pid"
    # Initial state: dead PID, original diagnose says dead_process.
    pid_path.write_text("999999")
    spawner = _SpawnRecorder()

    # Stage the world: AFTER the initial diagnose runs but BEFORE the
    # post-lock recheck, replace the PID file with a fresh "alive" PID
    # (using our pytest PID + the autouse identity stub which says any
    # live PID matches). The recheck should now classify as ``alive``
    # and bail without calling the spawner or unlinking anything.
    real_diagnose = _supervisor_mod.diagnose_rail_daemon
    diagnose_count = {"n": 0}

    def staged_diagnose(**kw):
        diagnose_count["n"] += 1
        if diagnose_count["n"] == 2:
            # Simulate a concurrent supervisor having just spawned a
            # daemon: replace the PID file before recheck reads it.
            pid_path.write_text(str(os.getpid()))
        return real_diagnose(**kw)

    monkeypatch.setattr(_supervisor_mod, "diagnose_rail_daemon", staged_diagnose)

    result = check_and_revive_rail_daemon(
        config_path=config_path,
        pid_path=pid_path,
        last_tick_iso=None,
        last_revival_at=None,
        spawn_fn=spawner,
        emit_audit=False,
    )

    # Post-lock recheck must detect the fresh daemon and stand down.
    assert spawner.calls == [], (
        "spawner must not run after a sibling supervisor already revived"
    )
    assert result.revived is False
    # The recheck saw an alive process; reflect that in the result.
    assert result.decision.state == "alive"
    # The fresh PID file is preserved — we never reached the unlink step.
    assert pid_path.exists()
    assert pid_path.read_text().strip() == str(os.getpid())


# ---------------------------------------------------------------------------
# HIGH-1 — cron path must not spawn when cockpit hosts in-process
# ---------------------------------------------------------------------------


def test_cron_skips_revival_on_missing_pid(tmp_path: Path):
    """``cron=True`` downgrades ``missing_pid`` to no-op.

    When the cockpit is hosting the rail in-process, no PID file is
    ever written. A cron-driven heartbeat would diagnose ``missing_pid``
    and spawn a headless daemon, producing two concurrent tickers.
    The cron flag prevents that.
    """
    config_path = tmp_path / "pollypm.toml"
    pid_path = tmp_path / "rail_daemon.pid"  # never created
    spawner = _SpawnRecorder()

    result = check_and_revive_rail_daemon(
        config_path=config_path,
        pid_path=pid_path,
        last_tick_iso=None,
        last_revival_at=None,
        spawn_fn=spawner,
        emit_audit=False,
        cron=True,
    )

    assert result.decision.state == "missing_pid"
    assert result.revived is False
    assert spawner.calls == [], (
        "cron must NOT spawn on missing_pid — cockpit may be hosting in-process"
    )


def test_cron_still_revives_dead_process(tmp_path: Path):
    """``cron=True`` must still recover ``dead_process`` daemons.

    A dead_process diagnosis means a daemon WAS recorded (so the
    cockpit isn't hosting in-process) but its PID is no longer alive.
    Cron must revive in that case — that's the failure mode it exists
    to defend against.
    """
    config_path = tmp_path / "pollypm.toml"
    pid_path = tmp_path / "rail_daemon.pid"
    pid_path.write_text("999999")  # dead PID
    spawner = _SpawnRecorder()

    result = check_and_revive_rail_daemon(
        config_path=config_path,
        pid_path=pid_path,
        last_tick_iso=None,
        last_revival_at=None,
        spawn_fn=spawner,
        emit_audit=False,
        cron=True,
    )

    assert result.decision.state == "dead_process"
    assert result.revived is True
    assert spawner.calls == [config_path]


def test_non_cron_caller_still_revives_missing_pid(tmp_path: Path):
    """Default (cockpit) caller still acts on ``missing_pid``.

    The cron guard must NOT regress the cockpit periodic timer's
    ability to spawn a daemon when there's no PID file at all.
    """
    config_path = tmp_path / "pollypm.toml"
    pid_path = tmp_path / "rail_daemon.pid"  # never created
    spawner = _SpawnRecorder()

    result = check_and_revive_rail_daemon(
        config_path=config_path,
        pid_path=pid_path,
        last_tick_iso=None,
        last_revival_at=None,
        spawn_fn=spawner,
        emit_audit=False,
        # cron defaults to False
    )

    assert result.decision.state == "missing_pid"
    assert result.revived is True
    assert spawner.calls == [config_path]
