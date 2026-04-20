"""Unit tests for :mod:`pollypm.rail_daemon`.

These cover the PID-file lifecycle (the interesting piece — the rail
boot itself is exercised by :mod:`pollypm.core.rail` tests). Running
the actual daemon loop is skipped here because it spawns background
threads that make teardown non-trivial in a test environment; that
path is covered by the end-to-end smoke test in the demo runbook.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pollypm.rail_daemon import _claim_pid_file, _pid_alive, _pid_file


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
