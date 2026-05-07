"""Reaper for stale ``pollypm.rail_daemon`` processes (#1432).

Background
----------
``cli._spawn_rail_daemon`` launches ``python -m pollypm.rail_daemon
--config <path>`` detached from the cockpit process group. The daemon
writes ``~/.pollypm/rail_daemon.pid`` on boot and removes it on clean
shutdown.

The PID-file dance (``cli._rail_daemon_live`` / ``cli._stop_rail_daemon``)
breaks down in three real-world cases observed on the field machine:

* ``pm reset --force`` SIGKILLs the cockpit process group before the
  daemon's ``atexit`` cleanup runs — the daemon survives but the PID
  file is unlinked, so the next ``pm up`` happily spawns *another*
  daemon while the first keeps running.
* Test runs against a temp config (``/private/tmp/polly-*``) leave a
  daemon pinned to a now-deleted config tree; the live daemon doesn't
  share a PID file with the live workspace so ``_rail_daemon_live``
  returns False, and the next bootstrap spawns a sibling.
* The orchestrator's ``pm up --phantom-client`` cycle re-spawns
  ``rail_daemon`` faster than the previous PID gets cleaned up,
  resulting in 2+ daemons fighting for the same SQLite WAL.

In savethenovel watch tick #5 (#1432) the field machine had three
concurrent ``pollypm.rail_daemon`` processes, one of which (#1431)
was pegging CPU at 96.8%.

This module mirrors :mod:`pollypm.cockpit_socket_reaper` for the
process-tree case: a bootstrap-time sweep that walks every
``pollypm.rail_daemon`` process via ``ps``, classifies each as
"keep" / "reap", SIGTERMs the reapable ones, and SIGKILLs the
holdouts after a short grace window.

Staleness signal
----------------
A daemon process is reaped when EITHER of:

* Its ``--config`` path matches the current workspace AND its PID is
  not the live PID-file owner (a sibling that snuck past the lock).
* Its ``--config`` path lives under ``$TMPDIR`` / ``/private/tmp``
  (a leftover from a test run; the parent test session is gone).

The PID-file owner is preserved unconditionally — even if the file
is stale, we error on the side of leaving the live daemon alone and
will catch it on the next bootstrap if it really is dead.

Why bootstrap-only
------------------
A runtime sweep risks racing with a freshly-spawned daemon during
the same boot cycle. ``_bootstrap_clear_markers`` is the safe sweep
window: it runs once per supervisor boot, BEFORE the launch executor
fires ``START_RAIL_DAEMON``, so any daemon visible at this point is
by definition from a prior boot.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


# Match the cmdline shape ``cli._spawn_rail_daemon`` writes:
# ``python -m pollypm.rail_daemon --config <path>``.
_RAIL_DAEMON_NEEDLE = "pollypm.rail_daemon"


# How long to wait for SIGTERM to take effect before falling back to
# SIGKILL. Mirrors the existing ``rail_daemon`` shutdown handler which
# tears down ``CoreRail`` synchronously and exits within a tick.
_SIGTERM_GRACE_S = 3.0


@dataclass(frozen=True, slots=True)
class ReapedDaemon:
    """Record of a rail_daemon process that was killed by the reaper.

    Surfaces enough context for log lines / audit events: the PID,
    its observed age in seconds (parsed from ``ps etime``), the
    ``--config`` path argument that pinned it to a workspace, the
    classification reason, and which signal succeeded.
    """

    pid: int
    config_path: str | None
    age_s: int | None
    reason: str
    signal_used: str  # ``"SIGTERM"`` or ``"SIGKILL"`` or ``"already_gone"``


@dataclass(frozen=True, slots=True)
class _DaemonProcess:
    """Internal: parsed snapshot of a ``ps`` row pointing at a daemon."""

    pid: int
    age_s: int | None
    cmdline: str
    config_path: str | None


# Regex tolerant of the macOS ``ps -o etime`` format ``[[DD-]HH:]MM:SS``.
_ETIME_RE = re.compile(
    r"^(?:(?P<days>\d+)-)?(?:(?P<hours>\d+):)?(?P<minutes>\d+):(?P<seconds>\d+)$"
)


def _parse_etime(value: str) -> int | None:
    """Parse the ``ps`` ``etime`` field into seconds.

    Returns ``None`` when the field is unparseable (very-young
    processes can show up as empty on some platforms; a missing age
    is fine for the reaper — staleness is decided by the cmdline
    shape, not by age).
    """
    match = _ETIME_RE.match(value.strip())
    if match is None:
        return None
    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def _extract_config_path(cmdline: str) -> str | None:
    """Pull the ``--config <path>`` argument out of a ``ps`` cmdline.

    Tolerates both ``--config /path`` and ``--config=/path`` shapes.
    Returns ``None`` when the flag is absent (the daemon should never
    be launched without it from ``cli._spawn_rail_daemon``, but a
    test harness or operator might invoke it manually).
    """
    try:
        tokens = shlex.split(cmdline)
    except ValueError:
        # Unbalanced quoting — fall back to a regex sweep so a
        # pathologically-formatted cmdline doesn't blind the reaper.
        match = re.search(r"--config[= ]([^\s]+)", cmdline)
        return match.group(1) if match else None
    for index, token in enumerate(tokens):
        if token == "--config" and index + 1 < len(tokens):
            return tokens[index + 1]
        if token.startswith("--config="):
            return token.split("=", 1)[1]
    return None


def _list_rail_daemon_procs(
    *,
    ps_runner: "PsRunner | None" = None,
) -> list[_DaemonProcess]:
    """Run ``ps`` and parse out every ``pollypm.rail_daemon`` process.

    Uses a tab as the separator between PID/ETIME/COMMAND so the
    cmdline (which itself contains spaces) parses cleanly. Falls back
    to splitting on whitespace twice when ``ps`` doesn't honour the
    separator hint (BSD ``ps`` accepts comma-list ``-o`` but always
    delimits with whitespace).
    """
    runner = ps_runner or _default_ps_runner
    try:
        raw = runner()
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("rail_daemon_reaper: ps invocation failed: %s", exc)
        return []
    out: list[_DaemonProcess] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or _RAIL_DAEMON_NEEDLE not in line:
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
        # Skip ourselves — the reaper runs under the supervisor's
        # Python interpreter, but in a future ``pm`` subcommand the
        # importing process could itself match the needle.
        if pid == os.getpid():
            continue
        out.append(
            _DaemonProcess(
                pid=pid,
                age_s=_parse_etime(etime_text),
                cmdline=cmdline,
                config_path=_extract_config_path(cmdline),
            )
        )
    return out


def _default_ps_runner() -> str:
    """Invoke ``ps`` and return its decoded stdout.

    ``-A`` lists every process; ``-o pid,etime,command`` keeps the
    output narrow and stable across BSD/Linux. We do NOT use ``-f``
    because BSD ``ps -f`` re-orders columns. Timeout is generous (5s)
    because a hanging ``ps`` would block bootstrap; we'd rather skip
    the reap than hang ``pm up``.
    """
    completed = subprocess.run(
        ["ps", "-A", "-o", "pid,etime,command"],
        capture_output=True,
        text=True,
        timeout=5.0,
        check=False,
    )
    return completed.stdout


# Type alias for tests that want to inject canned ``ps`` output.
PsRunner = Callable[[], str]


def _is_temp_config_path(config_path: str | None) -> bool:
    """Return True iff ``config_path`` looks like a test-run leftover.

    Matches paths under the system temp dir — covers
    ``/tmp/polly-*``, ``/private/tmp/polly-*`` (macOS resolves
    ``/tmp`` to ``/private/tmp``), and pytest's ``tmp_path`` family.
    A daemon pinned to a temp config is by definition a test leak:
    the parent test session is long gone and the config tree may
    not even exist on disk anymore.
    """
    if not config_path:
        return False
    try:
        resolved = str(Path(config_path).resolve())
    except OSError:
        resolved = config_path
    tmp_roots = {tempfile.gettempdir(), "/tmp", "/private/tmp", "/var/folders"}
    for root in tmp_roots:
        try:
            root_resolved = str(Path(root).resolve())
        except OSError:
            root_resolved = root
        if resolved.startswith(root_resolved + os.sep) or resolved == root_resolved:
            return True
        # Also catch raw, unresolved paths for cases where the config
        # tree was deleted (so ``Path.resolve`` returns the original).
        if config_path.startswith(root.rstrip(os.sep) + os.sep):
            return True
    return False


def _read_pid_file(pid_path: Path) -> int | None:
    """Return the PID stored in ``rail_daemon.pid``, or ``None``."""
    try:
        text = pid_path.read_text().strip()
    except (OSError, ValueError):
        return None
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _classify(
    proc: _DaemonProcess,
    *,
    current_config_path: str,
    pid_file_owner: int | None,
) -> str | None:
    """Decide why ``proc`` should be reaped (or ``None`` to keep it).

    Preservation rules, evaluated in order:

    1. Live PID-file owner is always preserved. Even if the file is
       stale, the PID is the most authoritative "this is the live
       daemon" signal we have.
    2. Otherwise, reap when:
       * ``--config`` matches the current workspace (sibling daemon
         that snuck past the file lock), OR
       * ``--config`` lives under ``$TMPDIR`` (test-run leftover).

    Other rail_daemon processes (e.g. running under a different
    user's config tree on a shared machine) are left alone — we
    don't own them.
    """
    if pid_file_owner is not None and proc.pid == pid_file_owner:
        return None

    config_path = proc.config_path
    if config_path is None:
        # No --config arg → can't classify; leave alone.
        return None

    try:
        same_workspace = (
            Path(config_path).resolve() == Path(current_config_path).resolve()
        )
    except OSError:
        same_workspace = config_path == current_config_path

    if same_workspace:
        return (
            "sibling daemon for current workspace "
            f"(config={config_path!r}, pid_file_owner={pid_file_owner})"
        )
    if _is_temp_config_path(config_path):
        return f"test-run leftover (config={config_path!r})"
    return None


def _emit_audit(
    *,
    pid: int,
    age_s: int | None,
    reason: str,
    config_path: str | None,
    signal_used: str,
) -> None:
    """Best-effort ``daemon.reaped`` audit emit.

    Fires after a successful kill so the central audit log records
    each reap with ``role=rail``, ``pid``, ``age_s``, ``reason``.
    Mirrors :func:`cockpit_socket_reaper._emit_audit` — never raises,
    and uses the ``_workspace`` project key because the rail daemon
    belongs to no single project.
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
            subject=f"rail_daemon/{pid}",
            actor="system",
            status="warn",
            metadata={
                "role": "rail",
                "pid": pid,
                "age_s": age_s,
                "reason": reason,
                "config": config_path,
                "signal": signal_used,
            },
        )
    except Exception:  # noqa: BLE001
        pass


def _pid_alive(pid: int) -> bool:
    """Return True iff ``pid`` is still a live process.

    ``EPERM`` (PermissionError) means the PID exists but isn't ours
    — treat as alive, matching the conservative behaviour in
    :mod:`cockpit_socket_reaper`.
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
    sleep_fn=time.sleep,
) -> str:
    """SIGTERM, wait up to ``grace_s``, SIGKILL if still alive.

    Returns the signal label that ultimately took (or
    ``"already_gone"`` if the process exited before we sent
    anything). The caller logs / audits the result.

    ``sleep_fn`` is injected so tests can drive the grace window
    deterministically without sleeping wall-clock time.
    """
    if not _pid_alive(pid):
        return "already_gone"
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "already_gone"
    except PermissionError:
        # Can't signal it — log and bail. Conservative: we'd rather
        # leave a foreign daemon alone than fight the OS.
        logger.warning(
            "rail_daemon_reaper: SIGTERM denied for pid=%d (not our process)",
            pid,
        )
        return "denied"

    # Poll on a short cadence so a fast-exiting daemon doesn't block
    # bootstrap for the full grace window.
    deadline = time.monotonic() + grace_s
    poll_interval = 0.1
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return "SIGTERM"
        sleep_fn(poll_interval)

    # Holdout — escalate.
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return "SIGTERM"
    except PermissionError:
        return "denied"
    return "SIGKILL"


def reap_stale_rail_daemons(
    *,
    current_config_path: Path | str,
    pid_file_path: Path | str | None = None,
    ps_runner=None,
    sleep_fn=time.sleep,
) -> list[ReapedDaemon]:
    """Bootstrap-time reaper. Walk ``ps``, kill stale rail_daemon procs.

    Designed to be called from ``Supervisor._bootstrap_clear_markers``
    BEFORE the launch executor fires ``START_RAIL_DAEMON``. By that
    point any daemon visible to ``ps`` is from a prior boot.

    Args:
        current_config_path: the workspace's config path — used to
            decide which daemons belong to "this workspace".
        pid_file_path: location of ``rail_daemon.pid`` (defaults to
            ``<config-parent>/rail_daemon.pid``). The PID stored there
            is preserved unconditionally.
        ps_runner: callable returning ``ps`` output. Tests inject a
            fake; production uses :func:`_default_ps_runner`.
        sleep_fn: injectable sleep for the SIGTERM grace window.

    Returns:
        The list of :class:`ReapedDaemon` records for processes that
        were signalled, so callers can log / count.
    """
    config_path_str = str(current_config_path)
    if pid_file_path is None:
        pid_file = Path(config_path_str).parent / "rail_daemon.pid"
    else:
        pid_file = Path(pid_file_path)

    pid_file_owner: int | None = None
    if pid_file.exists():
        owner = _read_pid_file(pid_file)
        if owner is not None and _pid_alive(owner):
            pid_file_owner = owner

    procs = _list_rail_daemon_procs(ps_runner=ps_runner)
    reaped: list[ReapedDaemon] = []
    for proc in procs:
        reason = _classify(
            proc,
            current_config_path=config_path_str,
            pid_file_owner=pid_file_owner,
        )
        if reason is None:
            continue
        signal_used = _terminate_with_grace(proc.pid, sleep_fn=sleep_fn)
        if signal_used == "denied":
            # Couldn't signal — don't claim a reap.
            continue
        logger.warning(
            "rail_daemon_reaper: reaped pid=%d age_s=%s config=%s reason=%s "
            "signal=%s",
            proc.pid,
            proc.age_s,
            proc.config_path,
            reason,
            signal_used,
        )
        _emit_audit(
            pid=proc.pid,
            age_s=proc.age_s,
            reason=reason,
            config_path=proc.config_path,
            signal_used=signal_used,
        )
        reaped.append(
            ReapedDaemon(
                pid=proc.pid,
                config_path=proc.config_path,
                age_s=proc.age_s,
                reason=reason,
                signal_used=signal_used,
            )
        )
    return reaped


__all__ = [
    "ReapedDaemon",
    "reap_stale_rail_daemons",
]
