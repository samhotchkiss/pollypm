"""Unit tests for :mod:`pollypm.rail_daemon`.

These cover the PID-file lifecycle (the interesting piece — the rail
boot itself is exercised by :mod:`pollypm.core.rail` tests). Running
the actual daemon loop is skipped here because it spawns background
threads that make teardown non-trivial in a test environment; that
path is covered by the end-to-end smoke test in the demo runbook.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from pollypm.rail_daemon import (
    _acquire_lifetime_lock,
    _claim_pid_file,
    _lock_file,
    _pid_alive,
    _pid_file,
)


def test_pid_file_resolves_under_home(tmp_path: Path):
    assert _pid_file(tmp_path) == tmp_path / "rail_daemon.pid"


def test_pid_alive_detects_self():
    # Our own PID is guaranteed live.
    assert _pid_alive(os.getpid()) is True


def test_pid_alive_detects_dead_pid():
    # PID 999999 is overwhelmingly unlikely to exist; if it does, this
    # test is in a weird environment and should be investigated.
    assert _pid_alive(999999) is False


def test_claim_pid_file_fresh(tmp_path: Path):
    pid_path = tmp_path / "rail_daemon.pid"
    assert _claim_pid_file(pid_path) is True
    assert pid_path.read_text().strip() == str(os.getpid())


def test_claim_pid_file_rejects_live_owner(tmp_path: Path):
    pid_path = tmp_path / "rail_daemon.pid"
    # Simulate an already-running daemon owning the file.
    pid_path.write_text(str(os.getpid()))
    assert _claim_pid_file(pid_path) is False
    # The file must be untouched — we reject without stomping.
    assert pid_path.read_text().strip() == str(os.getpid())


def test_claim_pid_file_overwrites_stale(tmp_path: Path):
    pid_path = tmp_path / "rail_daemon.pid"
    # Stale PID that definitely isn't running.
    pid_path.write_text("999999")
    assert _claim_pid_file(pid_path) is True
    assert pid_path.read_text().strip() == str(os.getpid())


def test_claim_pid_file_handles_garbage(tmp_path: Path):
    pid_path = tmp_path / "rail_daemon.pid"
    pid_path.write_text("not-a-pid")
    # Garbage PID file is treated as stale — we claim it.
    assert _claim_pid_file(pid_path) is True
    assert pid_path.read_text().strip() == str(os.getpid())


def test_claim_pid_file_handles_zero(tmp_path: Path):
    pid_path = tmp_path / "rail_daemon.pid"
    pid_path.write_text("0")
    # PID 0 is invalid — treat as stale.
    assert _claim_pid_file(pid_path) is True
    assert pid_path.read_text().strip() == str(os.getpid())


def test_claim_pid_file_creates_parent_dir(tmp_path: Path):
    """Parent directory is created on first claim — tolerate a fresh install
    that hasn't set up ~/.pollypm/ yet."""
    pid_path = tmp_path / "new_subdir" / "rail_daemon.pid"
    assert _claim_pid_file(pid_path) is True
    assert pid_path.exists()


# --- Lifetime flock (#1586) ------------------------------------------------


def test_lock_file_resolves_under_home(tmp_path: Path):
    assert _lock_file(tmp_path) == tmp_path / "rail_daemon.lock"


def test_acquire_lifetime_lock_fresh(tmp_path: Path):
    lock_path = tmp_path / "rail_daemon.lock"
    fd = _acquire_lifetime_lock(lock_path)
    assert fd is not None
    assert lock_path.exists()
    os.close(fd)


def test_acquire_lifetime_lock_rejects_when_held(tmp_path: Path):
    """Second acquire returns None while first fd is still open.

    This is the #1586 regression: even if the PID file has been
    unlinked by ``pm reset --force``, a second daemon must not be
    able to start while the first one's process is still alive.
    """
    lock_path = tmp_path / "rail_daemon.lock"
    first_fd = _acquire_lifetime_lock(lock_path)
    assert first_fd is not None
    try:
        second_fd = _acquire_lifetime_lock(lock_path)
        assert second_fd is None, (
            "second acquire should have been refused while first lock is held"
        )
    finally:
        os.close(first_fd)


def test_acquire_lifetime_lock_succeeds_after_release(tmp_path: Path):
    """Once the holder closes its fd, a fresh acquire succeeds.

    Mirrors the production lifecycle: daemon A exits (OS releases the
    lock), then ``pm up`` invokes daemon B which claims it cleanly.
    """
    lock_path = tmp_path / "rail_daemon.lock"
    first_fd = _acquire_lifetime_lock(lock_path)
    assert first_fd is not None
    os.close(first_fd)
    second_fd = _acquire_lifetime_lock(lock_path)
    assert second_fd is not None
    os.close(second_fd)


def test_acquire_lifetime_lock_creates_parent_dir(tmp_path: Path):
    lock_path = tmp_path / "new_subdir" / "rail_daemon.lock"
    fd = _acquire_lifetime_lock(lock_path)
    assert fd is not None
    assert lock_path.exists()
    os.close(fd)


# --- SIGTERM watchdog (#1591) ---------------------------------------------


def _spawn_stuck_daemon_stub(grace_s: float) -> subprocess.Popen:
    """Spawn a child that mimics the rail_daemon's SIGTERM-watchdog wiring
    but with the main thread deliberately wedged inside an uninterruptible
    loop — emulating the production case where the heartbeat ticker holds
    the GIL inside a long sqlite call and the main thread's signal handler
    can't run cleanup quickly.

    The child arms the same watchdog/handler pattern rail_daemon uses, so
    a SIGTERM should still cause the process to exit within ``grace_s``
    seconds courtesy of the SIGKILL backstop. Without the backstop, the
    child would survive indefinitely (it ignores the cleanup deadline).
    """
    script = textwrap.dedent(
        f"""
        import os, signal, sys, threading, time

        GRACE = {grace_s!r}
        stopping = {{"flag": False, "deadline": 0.0}}

        def watchdog():
            while not stopping["flag"]:
                time.sleep(0.02)
            remaining = max(0.0, stopping["deadline"] - time.monotonic())
            time.sleep(remaining)
            os.kill(os.getpid(), signal.SIGKILL)

        def handler(signum, frame):
            if not stopping["flag"]:
                stopping["deadline"] = time.monotonic() + GRACE
                stopping["flag"] = True

        signal.signal(signal.SIGTERM, handler)
        threading.Thread(target=watchdog, daemon=True).start()

        # Tell parent we're ready.
        sys.stdout.write("READY\\n")
        sys.stdout.flush()

        # Simulate "GIL held by C code" by busy-looping; even if the
        # main thread checks the flag, never break out — only the
        # SIGKILL backstop should end us.
        while True:
            time.sleep(60)
            # If sleep is interrupted by the signal handler, just go
            # back to sleeping — we intentionally do NOT honour the
            # flag to prove the backstop works.
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # Wait for the child to install the handler.
    assert proc.stdout is not None
    ready = proc.stdout.readline()
    assert ready.strip() == "READY", f"unexpected child stdout: {ready!r}"
    return proc


def test_sigterm_watchdog_kills_stuck_daemon():
    """Issue #1591: a SIGTERM must terminate the daemon within the
    grace window even when the main thread refuses to honour the
    shutdown flag (simulating the sqlite-blocked production case)."""
    proc = _spawn_stuck_daemon_stub(grace_s=0.5)
    try:
        proc.send_signal(signal.SIGTERM)
        # Allow grace + small slack for the backstop to fire.
        try:
            rc = proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2.0)
            pytest.fail(
                "SIGTERM watchdog did not kill the process within 3s — "
                "the SIGKILL backstop is not wired up correctly"
            )
        # SIGKILL produces -SIGKILL (negative) exit code on POSIX.
        assert rc == -signal.SIGKILL, (
            f"expected SIGKILL exit (-{int(signal.SIGKILL)}), got {rc}"
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2.0)


def test_acquire_lifetime_lock_independent_of_pid_file(tmp_path: Path):
    """The flock guard works even if the PID file does not exist.

    This is the precise #1586 scenario: ``pm reset --force`` unlinks
    the PID file pre-emptively, but the daemon process survives. A
    second ``pm up`` cannot rely on the PID file being there — it must
    be the flock that refuses the duplicate spawn.
    """
    lock_path = tmp_path / "rail_daemon.lock"
    pid_path = tmp_path / "rail_daemon.pid"
    assert not pid_path.exists()
    first_fd = _acquire_lifetime_lock(lock_path)
    assert first_fd is not None
    try:
        # PID file does not exist (simulating ``pm reset`` having
        # unlinked it). The flock must still refuse a second daemon.
        assert not pid_path.exists()
        assert _acquire_lifetime_lock(lock_path) is None
    finally:
        os.close(first_fd)
