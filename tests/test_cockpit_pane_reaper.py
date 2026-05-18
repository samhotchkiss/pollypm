"""Tests for the cockpit-pane orphan reaper (#1590).

The reaper walks ``ps`` output for ``pollypm cockpit-pane`` cmdlines
and SIGTERMs (with SIGKILL fallback) any matches. The tests use
synthetic ``ps`` output plus a real sleep-forever sentinel subprocess
so we exercise the signal path without booting a real cockpit pane.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Iterator

import pytest

from pollypm.cockpit_pane_reaper import (
    ReapedCockpitPane,
    _extract_pane_kind,
    _PaneProcess,
    _parse_etime,
    reap_orphan_cockpit_panes,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spawn_sentinel() -> subprocess.Popen:
    """Spawn a sleep-forever child so we have a real PID to signal.

    The child installs a SIGTERM handler that exits cleanly so the
    grace-window path returns ``"SIGTERM"`` rather than escalating.
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


def _ps_line(
    pid: int,
    etime: str,
    cmdline: str,
    *,
    uid: int | None = None,
) -> str:
    """Format a synthetic ``ps -o pid,uid,etime,command`` line.

    Defaults ``uid`` to :func:`os.geteuid` so existing tests get a
    matching ownership row without the ownership guard rejecting them
    (#1644). Pass an explicit ``uid`` to model another user's row.
    """
    if uid is None:
        uid = os.geteuid()
    return f"  {pid} {uid} {etime} {cmdline}"


def _make_ps_runner(lines: list[str]):
    """Return a callable that produces canned ``ps`` output."""

    def _runner() -> str:
        return "\n".join(lines) + "\n"

    return _runner


# ---------------------------------------------------------------------------
# Pure-function unit tests (no signals, no subprocesses)
# ---------------------------------------------------------------------------


class TestParseEtime:
    def test_seconds_only(self) -> None:
        assert _parse_etime("00:42") == 42

    def test_minutes_seconds(self) -> None:
        assert _parse_etime("05:30") == 5 * 60 + 30

    def test_hours_minutes_seconds(self) -> None:
        assert _parse_etime("01:02:03") == 1 * 3600 + 2 * 60 + 3

    def test_days_hours_minutes_seconds(self) -> None:
        assert _parse_etime("4-00:00:00") == 4 * 86400

    def test_unparseable_returns_none(self) -> None:
        assert _parse_etime("garbage") is None
        assert _parse_etime("") is None


class TestExtractPaneKind:
    def test_simple_kind(self) -> None:
        cmd = "/usr/bin/python -m pollypm cockpit-pane activity"
        assert _extract_pane_kind(cmd) == "activity"

    def test_kind_with_trailing_args(self) -> None:
        cmd = "python -m pollypm cockpit-pane inbox --project foo"
        assert _extract_pane_kind(cmd) == "inbox"

    def test_kind_with_project_route(self) -> None:
        cmd = "python -m pollypm cockpit-pane project Health-Coach"
        assert _extract_pane_kind(cmd) == "project"

    def test_missing_kind_returns_none(self) -> None:
        cmd = "python -m pollypm cockpit-pane"
        assert _extract_pane_kind(cmd) is None

    def test_unbalanced_quotes_falls_back_to_regex(self) -> None:
        cmd = 'python -m pollypm cockpit-pane activity "unbalanced'
        assert _extract_pane_kind(cmd) == "activity"


# ---------------------------------------------------------------------------
# Integration: synthetic ps output + real subprocess targets
# ---------------------------------------------------------------------------


@pytest.fixture
def sentinel() -> Iterator[subprocess.Popen]:
    """A live subprocess we can target with the reaper."""
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


def test_reaps_orphan_cockpit_pane(sentinel: subprocess.Popen) -> None:
    """An orphan ``pollypm cockpit-pane`` row is SIGTERMed."""
    ps_runner = _make_ps_runner(
        [
            "  PID ELAPSED COMMAND",
            _ps_line(
                sentinel.pid,
                "4-00:00:00",
                "python -m pollypm cockpit-pane activity",
            ),
        ]
    )

    reaped = reap_orphan_cockpit_panes(ps_runner=ps_runner)

    assert len(reaped) == 1
    entry = reaped[0]
    assert isinstance(entry, ReapedCockpitPane)
    assert entry.pid == sentinel.pid
    assert entry.pane_kind == "activity"
    assert entry.age_s == 4 * 86400
    assert entry.signal_used in {"SIGTERM", "SIGKILL"}

    # Sentinel's SIGTERM handler should fire cleanly within grace.
    sentinel.wait(timeout=5)
    assert sentinel.returncode is not None


def test_idempotent_no_op_when_no_orphans() -> None:
    """Second call with empty ``ps`` output returns []. Models the
    repeated ``pm reset --force`` case."""
    ps_runner = _make_ps_runner(["  PID ELAPSED COMMAND"])

    first = reap_orphan_cockpit_panes(ps_runner=ps_runner)
    second = reap_orphan_cockpit_panes(ps_runner=ps_runner)

    assert first == []
    assert second == []


def test_idempotent_when_no_matching_rows() -> None:
    """Rows that don't match the cmdline shape are skipped."""
    ps_runner = _make_ps_runner(
        [
            _ps_line(99999, "00:01:00", "python -m pollypm.rail_daemon"),
            _ps_line(99998, "00:02:00", "/usr/bin/tmux new-session"),
            _ps_line(99997, "00:00:30", "bash"),
        ]
    )

    reaped = reap_orphan_cockpit_panes(ps_runner=ps_runner)
    assert reaped == []


def test_skips_self_pid() -> None:
    """``os.getpid()`` is filtered out so the reset CLI doesn't
    accidentally signal itself if it ever shared the needle shape."""
    ps_runner = _make_ps_runner(
        [
            _ps_line(
                os.getpid(),
                "00:01:00",
                "python -m pollypm cockpit-pane activity",
            ),
        ]
    )

    reaped = reap_orphan_cockpit_panes(ps_runner=ps_runner)
    assert reaped == []


def test_skips_rows_missing_pollypm_token() -> None:
    """A row with ``cockpit-pane`` but not ``pollypm`` is left alone.

    (Defends against a third-party tool that happens to use the
    ``cockpit-pane`` literal — we only target our own children.)
    """
    ps_runner = _make_ps_runner(
        [
            _ps_line(99996, "00:00:30", "some-other-app cockpit-pane activity"),
        ]
    )

    reaped = reap_orphan_cockpit_panes(ps_runner=ps_runner)
    assert reaped == []


def test_handles_ps_failure_gracefully() -> None:
    """A subprocess error from ``ps`` returns [] rather than raising —
    ``pm reset`` must never fail on the reap path."""

    def _failing_runner() -> str:
        raise OSError("synthetic failure")

    reaped = reap_orphan_cockpit_panes(ps_runner=_failing_runner)
    assert reaped == []


def test_already_gone_pid_not_claimed() -> None:
    """A ``ps`` row pointing at a dead PID returns ``"already_gone"``
    from terminate_with_grace and is NOT included in the reaped list."""
    proc = _spawn_sentinel()
    pid = proc.pid
    proc.kill()
    proc.wait(timeout=5)

    # Give the kernel a moment to fully reap before re-querying.
    time.sleep(0.1)

    ps_runner = _make_ps_runner(
        [
            _ps_line(pid, "00:00:05", "python -m pollypm cockpit-pane activity"),
        ]
    )

    reaped = reap_orphan_cockpit_panes(ps_runner=ps_runner)
    assert reaped == []


def test_reaps_multiple_orphans() -> None:
    """Multiple orphan rows all get signalled and reported."""
    sentinels = [_spawn_sentinel() for _ in range(3)]
    try:
        ps_runner = _make_ps_runner(
            [
                _ps_line(
                    s.pid,
                    "00:00:05",
                    f"python -m pollypm cockpit-pane {kind}",
                )
                for s, kind in zip(sentinels, ["activity", "inbox", "operator"])
            ]
        )

        reaped = reap_orphan_cockpit_panes(ps_runner=ps_runner)

        assert len(reaped) == 3
        assert {entry.pid for entry in reaped} == {s.pid for s in sentinels}
        assert {entry.pane_kind for entry in reaped} == {
            "activity",
            "inbox",
            "operator",
        }
        for s in sentinels:
            s.wait(timeout=5)
    finally:
        for s in sentinels:
            if s.poll() is None:
                try:
                    s.kill()
                except ProcessLookupError:
                    pass
            try:
                s.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


def test_skips_rows_owned_by_other_uid(sentinel: subprocess.Popen) -> None:
    """A ``ps`` row whose ``uid`` column does not match the current
    user is filtered out before any signal is sent (#1644).

    Multi-user safety guard: even if the kernel would permit the
    signal (e.g. root-owned reset, or a same-group race), the reaper
    must not target processes outside the invoking user's UID.
    """
    foreign_uid = os.geteuid() + 1000  # Synthetic non-matching uid.
    ps_runner = _make_ps_runner(
        [
            _ps_line(
                sentinel.pid,
                "00:01:00",
                "python -m pollypm cockpit-pane activity",
                uid=foreign_uid,
            ),
        ]
    )

    reaped = reap_orphan_cockpit_panes(ps_runner=ps_runner)

    assert reaped == []
    # Sentinel must NOT have been signalled — still running.
    assert sentinel.poll() is None


def test_only_signals_current_user_rows_with_mixed_owners() -> None:
    """Mixed-owner ``ps`` output reaps only the current-user rows.

    Models a shared host with multiple logins running pollypm: the
    reaper sees foreign-uid rows but must not signal them.
    """
    own = _spawn_sentinel()
    foreign = _spawn_sentinel()  # Real PID, but we'll label it as foreign uid.
    try:
        foreign_uid = os.geteuid() + 1000
        ps_runner = _make_ps_runner(
            [
                _ps_line(
                    own.pid,
                    "00:00:05",
                    "python -m pollypm cockpit-pane activity",
                ),
                _ps_line(
                    foreign.pid,
                    "00:00:05",
                    "python -m pollypm cockpit-pane inbox",
                    uid=foreign_uid,
                ),
            ]
        )

        reaped = reap_orphan_cockpit_panes(ps_runner=ps_runner)

        assert len(reaped) == 1
        assert reaped[0].pid == own.pid
        assert reaped[0].pane_kind == "activity"

        own.wait(timeout=5)
        # Foreign-uid PID must still be alive — the reaper never
        # signalled it.
        assert foreign.poll() is None
    finally:
        for proc in (own, foreign):
            if proc.poll() is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


def test_current_uid_override_filters_to_supplied_uid() -> None:
    """The ``current_uid`` kwarg is honoured (covers callers / tests
    that want to assert without depending on the real geteuid)."""
    ps_runner = _make_ps_runner(
        [
            _ps_line(
                99990,
                "00:00:05",
                "python -m pollypm cockpit-pane activity",
                uid=4242,
            ),
            _ps_line(
                99991,
                "00:00:05",
                "python -m pollypm cockpit-pane inbox",
                uid=9999,
            ),
        ]
    )

    # current_uid=4242 → only the first row is even considered for
    # signalling. Neither real PID exists, so both should fall through
    # the ``already_gone`` path and the result is [], but the key
    # assertion is that this does NOT raise (i.e. the foreign row
    # never reaches the signal code).
    reaped = reap_orphan_cockpit_panes(
        ps_runner=ps_runner,
        current_uid=4242,
    )
    assert reaped == []


def test_malformed_uid_column_skips_row() -> None:
    """A row with a non-integer ``uid`` column (e.g. the literal
    ``ps`` header) is dropped — we never signal a row we can't
    confirm ownership for."""
    ps_runner = _make_ps_runner(
        [
            "  PID   UID     ELAPSED COMMAND",
            "  123  not_an_int  00:00:05  python -m pollypm cockpit-pane activity",
        ]
    )

    reaped = reap_orphan_cockpit_panes(ps_runner=ps_runner)
    assert reaped == []


def test_pane_process_dataclass_is_frozen() -> None:
    """``_PaneProcess`` is the parsed-row snapshot — verify shape so
    future fields don't silently break the parser contract."""
    proc = _PaneProcess(
        pid=1234,
        uid=501,
        age_s=42,
        cmdline="python -m pollypm cockpit-pane activity",
        pane_kind="activity",
    )
    assert proc.pid == 1234
    assert proc.uid == 501
    assert proc.pane_kind == "activity"
    with pytest.raises(Exception):  # frozen dataclass → FrozenInstanceError
        proc.pid = 9999  # type: ignore[misc]
