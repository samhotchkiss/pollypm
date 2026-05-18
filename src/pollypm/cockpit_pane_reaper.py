"""Reaper for orphaned ``pm cockpit-pane <kind>`` child processes (#1590).

Background
----------
``CockpitRouter`` spawns right-pane apps as ``python -m pollypm cockpit-pane
<kind> [...]`` inside tmux panes. The expectation is that ``pm reset
--force``'s ``supervisor.shutdown_tmux`` cascade tears down those children
along with the tmux session that hosts them.

In practice the children survive because:

* tmux's ``kill-session`` (and ``kill-pane``) delivers SIGHUP via the
  pseudo-terminal — Python ignores SIGHUP by default, so the child keeps
  running.
* Many panes do not stay attached to the original tmux pane process
  group; the kernel re-parents them to PID 1 / the tmux daemon when
  their immediate parent exits. The pane-kill signal then has no
  pollypm-side target to find.

Field discovery (#1590): one ``cockpit-pane activity`` PID had been
alive for 4 days, accumulating 96 CPU-hours at 98% utilization, despite
many intervening ``pm reset --force`` cycles. Each orphan keeps a
SQLite connection open (contributing to the lock contention diagnosed
in #1587), burns RSS, and makes ``pm reset --force`` a misleading
"clean state" promise.

Fix
---
After the tmux teardown and rail-daemon stop in ``pm reset``, walk
``ps -A`` for any ``pollypm cockpit-pane`` cmdline owned by the current
user and SIGTERM-then-SIGKILL it. The current-user check
(``effective_uid == os.geteuid()``) is the multi-user safety guard —
we never signal another login's processes.

Idempotency
-----------
A second invocation with no orphans returns an empty list. The reaper
is safe to call from any ``pm reset`` path (including ``--force``
without a confirmation prompt).

Mirrors :mod:`pollypm.rail_daemon_reaper` for the ``ps``-walking and
SIGTERM-with-SIGKILL-grace mechanics. Reuses ``EVENT_DAEMON_REAPED``
for audit emission (``role="cockpit_pane"``) so existing audit
consumers pick the events up without schema changes.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)


# Match the cmdline shape ``CockpitRouter`` writes: ``python -m
# pollypm cockpit-pane <kind> ...``. The needle is the contiguous
# token pair, which is unique enough that no other PollyPM cmdline
# matches it (the literal "cockpit-pane" only ever appears as the
# subcommand token).
_COCKPIT_PANE_NEEDLE = "cockpit-pane"
_POLLYPM_NEEDLE = "pollypm"


# SIGTERM grace window before falling back to SIGKILL. Cockpit panes
# are Textual apps that exit promptly on SIGTERM (their event loop
# observes the signal), so a short window is fine.
_SIGTERM_GRACE_S = 2.0


@dataclass(frozen=True, slots=True)
class ReapedCockpitPane:
    """Record of a cockpit-pane process that was killed by the reaper.

    Surfaces enough context for log lines / audit events: the PID,
    its observed age in seconds (parsed from ``ps etime``), the pane
    kind extracted from the cmdline (``activity``/``inbox``/etc.),
    and which signal succeeded.
    """

    pid: int
    pane_kind: str | None
    age_s: int | None
    signal_used: str  # ``"SIGTERM"`` / ``"SIGKILL"`` / ``"already_gone"``


@dataclass(frozen=True, slots=True)
class _PaneProcess:
    """Internal: parsed snapshot of a ``ps`` row pointing at a pane."""

    pid: int
    age_s: int | None
    cmdline: str
    pane_kind: str | None


# Regex tolerant of the macOS ``ps -o etime`` format ``[[DD-]HH:]MM:SS``.
_ETIME_RE = re.compile(
    r"^(?:(?P<days>\d+)-)?(?:(?P<hours>\d+):)?(?P<minutes>\d+):(?P<seconds>\d+)$"
)


def _parse_etime(value: str) -> int | None:
    """Parse the ``ps`` ``etime`` field into seconds.

    Returns ``None`` when the field is unparseable. Mirrors
    :mod:`pollypm.rail_daemon_reaper._parse_etime`.
    """
    match = _ETIME_RE.match(value.strip())
    if match is None:
        return None
    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def _extract_pane_kind(cmdline: str) -> str | None:
    """Return the pane kind argument (``activity``, ``inbox``, ...).

    The router spawns panes as ``... cockpit-pane <kind> [args]``;
    we pull the token immediately after ``cockpit-pane``. Returns
    ``None`` when the cmdline is malformed.
    """
    try:
        tokens = shlex.split(cmdline)
    except ValueError:
        # Unbalanced quoting — fall back to a regex sweep so a
        # pathologically-formatted cmdline doesn't blind the reaper.
        match = re.search(r"cockpit-pane\s+(\S+)", cmdline)
        return match.group(1) if match else None
    for index, token in enumerate(tokens):
        if token == _COCKPIT_PANE_NEEDLE and index + 1 < len(tokens):
            return tokens[index + 1]
    return None


def _list_cockpit_pane_procs(
    *,
    ps_runner: "PsRunner | None" = None,
) -> list[_PaneProcess]:
    """Run ``ps`` and parse out every ``pollypm cockpit-pane`` process.

    The cmdline must contain BOTH ``pollypm`` and ``cockpit-pane`` so a
    user typing ``pm cockpit-pane --help`` in a shell doesn't get picked
    up as a target (their ``ps`` row would still show ``pm`` as the
    binary, not ``pollypm``).
    """
    runner = ps_runner or _default_ps_runner
    try:
        raw = runner()
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("cockpit_pane_reaper: ps invocation failed: %s", exc)
        return []
    out: list[_PaneProcess] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if _POLLYPM_NEEDLE not in line or _COCKPIT_PANE_NEEDLE not in line:
            continue
        # Two splits: pid, etime, then cmdline-as-rest.
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid_text, etime_text, cmdline = parts
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        # Skip ourselves — defensive even though the reset CLI is not
        # itself a cockpit-pane process.
        if pid == os.getpid():
            continue
        # Also skip our parent shell if it happens to match (paranoid:
        # nothing in the cli reset path matches the needle, but if a
        # future caller did this guard prevents self-immolation).
        try:
            if pid == os.getppid():
                continue
        except OSError:
            pass
        out.append(
            _PaneProcess(
                pid=pid,
                age_s=_parse_etime(etime_text),
                cmdline=cmdline,
                pane_kind=_extract_pane_kind(cmdline),
            )
        )
    return out


def _default_ps_runner() -> str:
    """Invoke ``ps`` and return its decoded stdout.

    ``-A`` lists every process; ``-o pid,etime,command`` keeps the
    output narrow and stable across BSD/Linux. Mirrors
    :func:`pollypm.rail_daemon_reaper._default_ps_runner`.
    """
    completed = subprocess.run(
        ["ps", "-A", "-o", "pid,etime,command"],
        capture_output=True,
        text=True,
        timeout=5.0,
        check=False,
    )
    return completed.stdout


PsRunner = Callable[[], str]


def _pid_alive(pid: int) -> bool:
    """Return True iff ``pid`` is still a live process.

    ``EPERM`` (PermissionError) means the PID exists but isn't ours —
    treat as alive but the caller's signal will fail with ``denied``
    (so we won't claim a reap).
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _terminate_with_grace(
    pid: int,
    *,
    grace_s: float = _SIGTERM_GRACE_S,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> str:
    """SIGTERM, wait up to ``grace_s``, SIGKILL if still alive.

    Returns the signal label that ultimately took (or
    ``"already_gone"`` if the process exited before we sent
    anything; ``"denied"`` if we lack permission to signal it).
    """
    if not _pid_alive(pid):
        return "already_gone"
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "already_gone"
    except PermissionError:
        logger.warning(
            "cockpit_pane_reaper: SIGTERM denied for pid=%d (not our process)",
            pid,
        )
        return "denied"

    deadline = time.monotonic() + grace_s
    poll_interval = 0.1
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return "SIGTERM"
        sleep_fn(poll_interval)

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return "SIGTERM"
    except PermissionError:
        return "denied"
    return "SIGKILL"


def _emit_audit(
    *,
    pid: int,
    age_s: int | None,
    pane_kind: str | None,
    signal_used: str,
) -> None:
    """Best-effort ``daemon.reaped`` audit emit.

    Reuses ``EVENT_DAEMON_REAPED`` with ``role="cockpit_pane"`` so
    existing audit consumers (the rail-daemon reap path emits the same
    event with ``role="rail"``) pick the events up without a schema
    change. Mirrors :func:`pollypm.rail_daemon_reaper._emit_audit`.
    """
    try:
        from pollypm.audit import emit as _audit_emit
        from pollypm.audit.log import EVENT_DAEMON_REAPED
    except Exception:  # noqa: BLE001
        return
    try:
        _audit_emit(
            event=EVENT_DAEMON_REAPED,
            project="_workspace",
            subject=f"cockpit_pane/{pid}",
            actor="system",
            status="warn",
            metadata={
                "role": "cockpit_pane",
                "pid": pid,
                "age_s": age_s,
                "pane_kind": pane_kind,
                "reason": "pm_reset_orphan",
                "signal": signal_used,
            },
        )
    except Exception:  # noqa: BLE001
        pass


def reap_orphan_cockpit_panes(
    *,
    ps_runner: PsRunner | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> list[ReapedCockpitPane]:
    """Sweep ``ps`` for orphan ``pollypm cockpit-pane`` processes and kill them.

    Designed to be called from ``pm reset`` AFTER
    ``supervisor.shutdown_tmux`` — by that point every pollypm-owned
    tmux session is dead, so any surviving cockpit-pane cmdline is by
    definition an orphan (re-parented to tmux daemon or PID 1).

    Args:
        ps_runner: callable returning ``ps`` output. Tests inject a
            fake; production uses :func:`_default_ps_runner`.
        sleep_fn: injectable sleep for the SIGTERM grace window.

    Returns:
        The list of :class:`ReapedCockpitPane` records for processes
        that were signalled, so the caller can log / count.
    """
    procs = _list_cockpit_pane_procs(ps_runner=ps_runner)
    reaped: list[ReapedCockpitPane] = []
    for proc in procs:
        signal_used = _terminate_with_grace(proc.pid, sleep_fn=sleep_fn)
        if signal_used in {"denied", "already_gone"}:
            # ``denied`` — not ours to kill (different user / privileged).
            # ``already_gone`` — race with shutdown; nothing to claim.
            continue
        logger.warning(
            "cockpit_pane_reaper: reaped pid=%d age_s=%s pane_kind=%s signal=%s",
            proc.pid,
            proc.age_s,
            proc.pane_kind,
            signal_used,
        )
        _emit_audit(
            pid=proc.pid,
            age_s=proc.age_s,
            pane_kind=proc.pane_kind,
            signal_used=signal_used,
        )
        reaped.append(
            ReapedCockpitPane(
                pid=proc.pid,
                pane_kind=proc.pane_kind,
                age_s=proc.age_s,
                signal_used=signal_used,
            )
        )
    return reaped


__all__ = [
    "ReapedCockpitPane",
    "reap_orphan_cockpit_panes",
]
