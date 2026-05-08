"""Tests for the rail_daemon process reaper (#1432).

The reaper walks ``ps`` output, classifies each ``pollypm.rail_daemon``
row as keep / reap, and SIGTERMs (with SIGKILL fallback) the reapable
ones. The tests use synthetic ``ps`` output and a tiny long-lived
sentinel subprocess so we exercise the real signal path without needing
to actually boot a daemon.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Iterator

import pytest

from pollypm.rail_daemon_reaper import (
    ReapedDaemon,
    _classify,
    _DaemonProcess,
    _extract_config_path,
    _is_temp_config_path,
    _parse_etime,
    reap_stale_rail_daemons,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spawn_sentinel() -> subprocess.Popen:
    """Spawn a sleep-forever child so we have a real PID to signal.

    The child writes nothing and waits for SIGTERM/SIGKILL. Tests
    must call ``proc.wait()`` (or rely on the fixture cleanup) to
    avoid leaking a zombie.
    """
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, time; signal.signal(signal.SIGTERM, lambda *a: __import__('sys').exit(0)); time.sleep(60)",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _ps_line(pid: int, etime: str, cmdline: str) -> str:
    """Format a synthetic ``ps -o pid,etime,command`` line."""
    return f"  {pid} {etime} {cmdline}"


def _make_ps_runner(lines: list[str]):
    """Return a callable that produces canned ``ps`` output."""
    def _runner() -> str:
        return "\n".join(lines) + "\n"
    return _runner


# ---------------------------------------------------------------------------
# Pure-function unit tests (no signals, no subprocesses)
# ---------------------------------------------------------------------------


class TestParseEtime:
    """``ps etime`` field comes in three shapes; the parser handles all."""

    def test_seconds_only(self) -> None:
        assert _parse_etime("00:42") == 42

    def test_minutes_seconds(self) -> None:
        assert _parse_etime("05:30") == 5 * 60 + 30

    def test_hours_minutes_seconds(self) -> None:
        assert _parse_etime("01:02:03") == 1 * 3600 + 2 * 60 + 3

    def test_days_hours_minutes_seconds(self) -> None:
        assert _parse_etime("2-03:04:05") == 2 * 86400 + 3 * 3600 + 4 * 60 + 5

    def test_unparseable_returns_none(self) -> None:
        assert _parse_etime("garbage") is None
        assert _parse_etime("") is None


class TestExtractConfigPath:
    """``--config <path>`` and ``--config=<path>`` both supported."""

    def test_space_separated(self) -> None:
        cmd = "/usr/bin/python -m pollypm.rail_daemon --config /home/u/.pollypm/pollypm.toml"
        assert _extract_config_path(cmd) == "/home/u/.pollypm/pollypm.toml"

    def test_equals_form(self) -> None:
        cmd = "python -m pollypm.rail_daemon --config=/tmp/polly-abc/pollypm.toml"
        assert _extract_config_path(cmd) == "/tmp/polly-abc/pollypm.toml"

    def test_missing_returns_none(self) -> None:
        cmd = "python -m pollypm.rail_daemon"
        assert _extract_config_path(cmd) is None

    def test_unbalanced_quotes_falls_back_to_regex(self) -> None:
        # shlex.split raises on unbalanced quotes; the regex fallback
        # still pulls the path out so a pathological cmdline can't
        # blind the reaper.
        cmd = 'python -m pollypm.rail_daemon --config /opt/foo "unbalanced'
        assert _extract_config_path(cmd) == "/opt/foo"


class TestIsTempConfigPath:
    """Temp-dir detection covers the macOS + Linux + pytest cases."""

    def test_tmp_path(self) -> None:
        assert _is_temp_config_path("/tmp/polly-abc/pollypm.toml") is True

    def test_private_tmp_path_macos(self) -> None:
        assert _is_temp_config_path("/private/tmp/polly-abc/pollypm.toml") is True

    def test_var_folders_macos(self) -> None:
        assert (
            _is_temp_config_path("/var/folders/xx/abc/T/pytest-of-x/pollypm.toml")
            is True
        )

    def test_workspace_path_is_not_temp(self) -> None:
        assert _is_temp_config_path("/Users/sam/.pollypm/pollypm.toml") is False
        assert _is_temp_config_path("/home/u/.pollypm/pollypm.toml") is False

    def test_none_is_not_temp(self) -> None:
        assert _is_temp_config_path(None) is False
        assert _is_temp_config_path("") is False


class TestClassify:
    """``_classify`` is the staleness oracle — keep, reap-sibling, reap-temp."""

    def _proc(self, *, pid: int, config_path: str | None) -> _DaemonProcess:
        return _DaemonProcess(
            pid=pid, age_s=42, cmdline="python -m pollypm.rail_daemon",
            config_path=config_path,
        )

    def test_pid_file_owner_preserved(self) -> None:
        """Even with a matching workspace config, the PID-file owner is kept."""
        proc = self._proc(pid=4242, config_path="/home/u/.pollypm/pollypm.toml")
        assert _classify(
            proc,
            current_config_path="/home/u/.pollypm/pollypm.toml",
            pid_file_owner=4242,
        ) is None

    def test_sibling_in_current_workspace_reaped(self) -> None:
        proc = self._proc(pid=9999, config_path="/home/u/.pollypm/pollypm.toml")
        reason = _classify(
            proc,
            current_config_path="/home/u/.pollypm/pollypm.toml",
            pid_file_owner=4242,  # different PID
        )
        assert reason is not None
        assert "sibling daemon" in reason

    def test_temp_config_always_reaped(self) -> None:
        proc = self._proc(pid=8888, config_path="/tmp/polly-xyz/pollypm.toml")
        reason = _classify(
            proc,
            current_config_path="/home/u/.pollypm/pollypm.toml",
            pid_file_owner=None,
        )
        assert reason is not None
        assert "test-run leftover" in reason

    def test_foreign_workspace_left_alone(self) -> None:
        """A daemon under another user's home is not ours to reap."""
        proc = self._proc(pid=7777, config_path="/home/other/.pollypm/pollypm.toml")
        assert _classify(
            proc,
            current_config_path="/home/u/.pollypm/pollypm.toml",
            pid_file_owner=None,
        ) is None

    def test_no_config_arg_left_alone(self) -> None:
        proc = self._proc(pid=6666, config_path=None)
        assert _classify(
            proc,
            current_config_path="/home/u/.pollypm/pollypm.toml",
            pid_file_owner=None,
        ) is None


# ---------------------------------------------------------------------------
# Integration: synthetic ps output + real subprocess targets
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Path]:
    """A fake workspace with a config file path the reaper can resolve."""
    base = tmp_path / "ws"
    base.mkdir()
    cfg = base / "pollypm.toml"
    cfg.write_text("# fake")
    yield cfg


@pytest.fixture
def sentinel() -> Iterator[subprocess.Popen]:
    """A live subprocess we can target with the reaper.

    Cleans itself up: if the test reaped it, the wait() returns
    quickly; if not, we SIGKILL on teardown so the test runner
    doesn't leak processes.
    """
    proc = _spawn_sentinel()
    try:
        yield proc
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def test_reaps_synthetic_stale_daemon_for_current_workspace(
    workspace: Path, sentinel: subprocess.Popen,
) -> None:
    """A daemon row matching the current workspace + not the PID-file
    owner is SIGTERMed."""
    ps_runner = _make_ps_runner([
        "  PID ELAPSED COMMAND",
        _ps_line(
            sentinel.pid,
            "00:00:05",
            f"python -m pollypm.rail_daemon --config {workspace}",
        ),
    ])

    reaped = reap_stale_rail_daemons(
        current_config_path=workspace,
        pid_file_path=workspace.parent / "rail_daemon.pid",  # absent → no owner
        ps_runner=ps_runner,
    )

    assert len(reaped) == 1
    entry = reaped[0]
    assert isinstance(entry, ReapedDaemon)
    assert entry.pid == sentinel.pid
    assert entry.age_s == 5
    assert entry.signal_used in {"SIGTERM", "SIGKILL", "already_gone"}
    assert "sibling daemon" in entry.reason

    # The sentinel installs a SIGTERM handler that exits cleanly, so it
    # should be gone within the grace window.
    sentinel.wait(timeout=5)
    assert sentinel.returncode is not None


def test_preserves_live_pid_file_owner(
    workspace: Path, sentinel: subprocess.Popen,
) -> None:
    """The daemon whose PID is in ``rail_daemon.pid`` is left running."""
    pid_file = workspace.parent / "rail_daemon.pid"
    pid_file.write_text(str(sentinel.pid))

    ps_runner = _make_ps_runner([
        _ps_line(
            sentinel.pid,
            "00:00:10",
            f"python -m pollypm.rail_daemon --config {workspace}",
        ),
    ])

    reaped = reap_stale_rail_daemons(
        current_config_path=workspace,
        pid_file_path=pid_file,
        ps_runner=ps_runner,
    )

    assert reaped == []
    # Sentinel should still be alive — give the SIGTERM handler some
    # time to fire if anything went wrong.
    time.sleep(0.2)
    assert sentinel.poll() is None, "live daemon was killed!"


def test_reaps_temp_config_daemon_regardless_of_workspace(
    workspace: Path, sentinel: subprocess.Popen,
) -> None:
    """A daemon pinned to ``/tmp/polly-*`` is reaped even if its config
    doesn't match the current workspace."""
    ps_runner = _make_ps_runner([
        _ps_line(
            sentinel.pid,
            "1-02:03:04",
            "python -m pollypm.rail_daemon --config /tmp/polly-xyz/pollypm.toml",
        ),
    ])

    reaped = reap_stale_rail_daemons(
        current_config_path=workspace,
        pid_file_path=workspace.parent / "rail_daemon.pid",
        ps_runner=ps_runner,
    )

    assert len(reaped) == 1
    assert reaped[0].pid == sentinel.pid
    # 1d 2h 3m 4s → 93784s
    assert reaped[0].age_s == 1 * 86400 + 2 * 3600 + 3 * 60 + 4
    assert "test-run leftover" in reaped[0].reason

    sentinel.wait(timeout=5)


def test_idempotent_no_op_when_only_live_owner_present(
    workspace: Path, sentinel: subprocess.Popen,
) -> None:
    """Running the reaper twice on the same ``ps`` output doesn't kill
    the live daemon. Models the 'bootstrap fires twice' edge case."""
    pid_file = workspace.parent / "rail_daemon.pid"
    pid_file.write_text(str(sentinel.pid))

    ps_lines = [
        _ps_line(
            sentinel.pid,
            "00:00:30",
            f"python -m pollypm.rail_daemon --config {workspace}",
        ),
    ]

    first = reap_stale_rail_daemons(
        current_config_path=workspace,
        pid_file_path=pid_file,
        ps_runner=_make_ps_runner(ps_lines),
    )
    second = reap_stale_rail_daemons(
        current_config_path=workspace,
        pid_file_path=pid_file,
        ps_runner=_make_ps_runner(ps_lines),
    )

    assert first == []
    assert second == []
    assert sentinel.poll() is None


def test_skips_self_pid(workspace: Path) -> None:
    """``os.getpid()`` is filtered out so a future ``pm`` subcommand
    that reuses this module can't reap itself."""
    ps_runner = _make_ps_runner([
        _ps_line(
            os.getpid(),
            "00:01:00",
            f"python -m pollypm.rail_daemon --config {workspace}",
        ),
    ])

    reaped = reap_stale_rail_daemons(
        current_config_path=workspace,
        pid_file_path=workspace.parent / "rail_daemon.pid",
        ps_runner=ps_runner,
    )

    assert reaped == []


def test_handles_ps_failure_gracefully(workspace: Path) -> None:
    """A subprocess error from ``ps`` returns an empty list rather than
    raising — bootstrap must never fail on the reaper path."""
    def _failing_runner() -> str:
        raise OSError("synthetic failure")

    reaped = reap_stale_rail_daemons(
        current_config_path=workspace,
        pid_file_path=workspace.parent / "rail_daemon.pid",
        ps_runner=_failing_runner,
    )
    assert reaped == []


def test_emits_daemon_reaped_audit_event(
    workspace: Path,
    sentinel: subprocess.Popen,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each reap produces a ``daemon.reaped`` line in the central audit tail."""
    audit_home = tmp_path / "audit-home"
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))

    ps_runner = _make_ps_runner([
        _ps_line(
            sentinel.pid,
            "00:00:07",
            f"python -m pollypm.rail_daemon --config {workspace}",
        ),
    ])

    reaped = reap_stale_rail_daemons(
        current_config_path=workspace,
        pid_file_path=workspace.parent / "rail_daemon.pid",
        ps_runner=ps_runner,
    )
    assert reaped, "expected stale daemon to be reaped"

    from pollypm.audit.log import read_events

    events = read_events("_workspace")
    daemon_events = [e for e in events if e.event == "daemon.reaped"]
    assert daemon_events, (
        f"expected daemon.reaped event, got {[e.event for e in events]}"
    )
    last = daemon_events[-1]
    assert last.metadata.get("role") == "rail"
    assert last.metadata.get("pid") == sentinel.pid
    assert last.metadata.get("age_s") == 7
    assert last.metadata.get("reason") and "sibling daemon" in last.metadata["reason"]
    assert last.actor == "system"

    sentinel.wait(timeout=5)


def test_sigkill_fallback_when_sigterm_ignored(workspace: Path) -> None:
    """A daemon that ignores SIGTERM is escalated to SIGKILL.

    We spawn a child that installs an empty SIGTERM handler so the
    signal doesn't kill it. The reaper should fall through to SIGKILL
    after the grace window.
    """
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import signal, time; "
                "signal.signal(signal.SIGTERM, lambda *a: None); "
                "time.sleep(60)"
            ),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        ps_runner = _make_ps_runner([
            _ps_line(
                proc.pid,
                "00:00:15",
                f"python -m pollypm.rail_daemon --config {workspace}",
            ),
        ])

        # Use a tiny grace window so the test doesn't take 3s; the
        # default sleep_fn is fine because we're capping the grace
        # via a faster sleep.
        # We can't override grace_s through the public API yet; the
        # default 3.0s window is acceptable for a single test.
        reaped = reap_stale_rail_daemons(
            current_config_path=workspace,
            pid_file_path=workspace.parent / "rail_daemon.pid",
            ps_runner=ps_runner,
        )
        assert len(reaped) == 1
        # The fallback should fire — SIGTERM was ignored at the handler
        # level, so the reaper's grace window expires and SIGKILL is
        # sent. We assert on the reaper's recorded signal rather than
        # ``proc.returncode`` because Python's signal-restart semantics
        # mean the child can still report ``-SIGTERM`` as its exit
        # signal even after we've escalated to SIGKILL.
        assert reaped[0].signal_used == "SIGKILL"
        proc.wait(timeout=5)
        assert proc.returncode is not None
        assert proc.returncode != 0  # any non-zero is fine
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
