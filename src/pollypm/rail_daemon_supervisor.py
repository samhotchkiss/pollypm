"""Self-recovery for the headless ``pollypm.rail_daemon`` heartbeat.

Background
----------
PollyPM's heartbeat (cadence sweeps for ``session.health_sweep``,
``audit_watchdog.tick``, ``advisor.tick`` etc) lives in the ``HeartbeatRail``
ticker thread inside the ``pollypm.rail_daemon`` Python process.

The rail's internal ``_tick_loop`` already wraps each ``tick()`` in a
try/except so a single bad tick can't take the loop down — but if the
*process* dies (OOM, sandbox kill, segfault in a C extension, ``pm
reset --force`` racing the daemon's ``atexit`` handler, …) nothing
detects the death and respawns it. The cockpit only spawns the daemon
once at boot via ``cli._spawn_rail_daemon`` and then trusts it to stay
alive for the rest of the session. When that trust is broken, queued
tasks pile up unclaimed and the cascade goes silent.

This is the **meta-watchdog problem**: the heartbeat can't watchdog
itself. Self-recovery for the heartbeat process specifically needs an
external supervisor. This module is that supervisor.

Two staleness signals
---------------------
We treat the heartbeat as dead when either is true:

1. **Process-level**: ``rail_daemon.pid`` is missing or names a PID
   that is no longer a live process. The cli already checks the first
   half (``cli._rail_daemon_live``); we add the second.
2. **Tick-level**: the most recent ``messages`` row scoped to
   ``heartbeat`` is older than ``stale_tick_seconds`` (default 180s).
   The ``session.health_sweep`` cadence is ``@every 10s`` and emits a
   heartbeat event every sweep, so 180s = ~18 missed sweeps means the
   daemon is alive in name only — likely wedged on a SQLite lock,
   pinned by a runaway plugin handler, or otherwise stuck in a way
   the in-thread ``_tick_loop`` retry can't escape.

Both signals are checked from outside the daemon's process — we never
ask the daemon to confirm its own health. (That would defeat the
point of an external supervisor.)

Recovery action
---------------
When dead, we:

1. Try to SIGTERM the existing PID (in case it's a zombie holding the
   pid file). Wait briefly. Escalate to SIGKILL only if the process is
   genuinely stuck — the rail's own shutdown handler removes the file
   on graceful termination.
2. Unlink the stale ``rail_daemon.pid`` so ``_claim_pid_file`` won't
   bail when the new daemon boots.
3. Spawn a fresh daemon via the same ``cli._spawn_rail_daemon`` path
   ``pm up`` uses. Best-effort — failures are surfaced but don't raise
   so the calling supervisor (cockpit periodic timer, ``pm heartbeat``
   cron) can still complete its primary job.
4. Emit ``daemon.revived`` audit event so operators can quantify
   how often the field machine accumulates revivals (chronic revivals
   indicate a real OOM / leak / loop, not just transient death).

Throttle
--------
Revival is throttled to once per 60s by default. Without throttling,
a daemon that dies-on-boot (e.g. broken plugin) gets respawned in a
tight loop, burning CPU and filling logs without making progress. The
throttle window is short enough that the user notices recovery within
a minute, but long enough that the next revival has time to actually
boot before another one fires.

Where this is wired
-------------------
* **Cockpit periodic timer** — every 60s the cockpit calls
  :func:`check_and_revive_rail_daemon`. This is the layer-2 watcher;
  it watches the rail daemon (layer 1) which watches the cadence
  handlers (layer 0).
* **``pm heartbeat`` cron path** — every minute when cron fires
  ``pm heartbeat``, the same check runs. This is defense-in-depth:
  the cron path keeps the daemon alive even when the cockpit isn't
  open or has itself crashed.

Layer 3 (launchd KeepAlive watching the cockpit / launching the rail
daemon directly on machine boot) is a separate opt-in surface — see
``cli_features/rail_launchd.py``.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator

logger = logging.getLogger(__name__)


__all__ = [
    "RevivalDecision",
    "RevivalResult",
    "DEFAULT_STALE_TICK_SECONDS",
    "DEFAULT_REVIVAL_THROTTLE_SECONDS",
    "DEFAULT_SIGTERM_GRACE_SECONDS",
    "check_and_revive_rail_daemon",
    "diagnose_rail_daemon",
    "should_revive",
]


#: Tick-level staleness threshold. The cadence-sweep handler runs every
#: 10s, so 180s ≈ 18 missed sweeps. Empirically this is well past the
#: range where a temporarily-busy daemon catches up; anything older is
#: a wedge / dead process.
DEFAULT_STALE_TICK_SECONDS = 180.0

#: Minimum gap between two revival attempts for the same workspace.
#: Prevents a respawn loop when the daemon is dying-on-boot due to a
#: persistent error (e.g. a plugin that crashes during initialise).
DEFAULT_REVIVAL_THROTTLE_SECONDS = 60.0

#: SIGTERM → SIGKILL grace window for a zombie / stuck PID. Mirrors
#: ``rail_daemon_reaper._SIGTERM_GRACE_S`` for consistency. The rail's
#: own SIGTERM handler tears down ``CoreRail`` synchronously and exits
#: in well under this window when it's responsive at all.
DEFAULT_SIGTERM_GRACE_SECONDS = 3.0


# ---------------------------------------------------------------------------
# Decision shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RevivalDecision:
    """Outcome of inspecting the rail daemon — separate from acting on it.

    Splitting "diagnose" from "revive" lets tests assert the diagnosis
    pure-functionally without spawning processes, and lets callers log
    a healthy daemon's status without paying the spawn-side cost.
    """

    # ``"alive"`` (no action), ``"missing_pid"``, ``"dead_process"``,
    # ``"stuck_no_tick"``, or ``"throttled"``.
    state: str
    pid: int | None
    last_tick_age_seconds: float | None
    reason: str

    @property
    def needs_revival(self) -> bool:
        """True iff the caller should attempt to spawn a new daemon."""
        return self.state in {"missing_pid", "dead_process", "stuck_no_tick"}


@dataclass(frozen=True, slots=True)
class RevivalResult:
    """Side-effecting outcome of :func:`check_and_revive_rail_daemon`."""

    decision: RevivalDecision
    revived: bool
    spawn_error: str | None
    killed_pid: int | None
    kill_signal: str | None  # ``"SIGTERM"`` / ``"SIGKILL"`` / ``"already_gone"`` / ``None``


# ---------------------------------------------------------------------------
# Diagnosis (pure)
# ---------------------------------------------------------------------------


def _read_pid(pid_path: Path) -> int | None:
    """Return the int PID stored in ``pid_path`` or ``None`` if unreadable.

    Mirrors :func:`pollypm.cli._rail_daemon_live` parsing — we deliberately
    don't share the helper because the cli copy unlinks stale files as a
    side effect, which is not appropriate for a pure-diagnosis call.
    """
    try:
        text = pid_path.read_text().strip()
    except (FileNotFoundError, OSError):
        return None
    if not text:
        return None
    try:
        pid = int(text)
    except ValueError:
        return None
    return pid if pid > 0 else None


def _pid_is_alive(pid: int) -> bool:
    """``os.kill(pid, 0)`` wrapper that treats ``EPERM`` as alive."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Belongs to another user; treat as alive — we never touch
        # processes we don't own from this supervisor.
        return True
    except OSError:
        return False
    return True


def _read_pid_command_line(pid: int) -> str | None:
    """Return the full command line of ``pid`` as a single string.

    Used by :func:`_pid_matches_daemon` to verify that a recorded PID is
    still our rail daemon process — catching the PID-reuse hazard where
    the original daemon died and the kernel reassigned its PID to an
    unrelated user process. Without this check, the supervisor would
    happily SIGTERM whatever happened to land at that number.

    Strategy:
    1. On Linux, read ``/proc/<pid>/cmdline`` directly. NUL-separated
       argv joined with spaces. Cheapest and avoids forking ``ps``.
    2. On macOS (no ``/proc``) or if the proc read fails, shell out to
       ``ps -p <pid> -o args=`` — portable to both platforms and
       returns the full argv as a single column.
    3. Returns ``None`` on any error (caller treats unknown identity
       as "not our daemon" and skips signaling — fail-safe).
    """
    if pid <= 0:
        return None

    proc_cmdline = Path(f"/proc/{pid}/cmdline")
    try:
        if proc_cmdline.exists():
            raw = proc_cmdline.read_bytes()
            if raw:
                # cmdline is NUL-separated argv; join with space for matching.
                return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
            # Empty cmdline → kernel thread or zombie; not our daemon.
            return None
    except OSError:
        # Fall through to ps fallback.
        pass

    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="],
            capture_output=True, text=True, check=False, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    text = (result.stdout or "").strip()
    return text or None


def _pid_matches_daemon(pid: int, config_path: Path | None) -> bool:
    """True iff ``pid``'s command line looks like our rail daemon.

    Matches ``pollypm.rail_daemon`` (or the path-form ``pollypm/rail_daemon``
    in case ``ps`` reports the script path rather than the ``-m`` form)
    in the argv. When ``config_path`` is provided AND the daemon's argv
    carries an explicit ``--config <path>``, the path must also match —
    a stray daemon for a different workspace must NOT be killed by this
    supervisor.

    A daemon spawned with no ``--config`` flag (using its default) still
    matches when ``config_path`` is the workspace default — we accept
    the absence of an explicit ``--config`` argument as compatible with
    any config_path so old daemons predating the explicit flag still
    register as ours.

    Returns ``False`` on any read error — callers must treat that as
    "not our daemon" and skip signaling. Better to leave a possibly-stale
    PID file alone than to SIGTERM an innocent process.
    """
    cmdline = _read_pid_command_line(pid)
    if not cmdline:
        return False
    # Must look like the rail_daemon entry point. Both forms appear in
    # the wild: ``-m pollypm.rail_daemon`` (our spawn) and the eager
    # ``python /path/to/pollypm/rail_daemon.py`` form some launchers
    # produce on macOS.
    if "pollypm.rail_daemon" not in cmdline and "pollypm/rail_daemon" not in cmdline:
        return False
    if config_path is None:
        return True
    # Only enforce config match when the daemon actually carries a
    # ``--config`` flag. A daemon launched with the implicit default
    # is compatible with any caller pointing at that same default.
    if "--config" not in cmdline:
        return True
    expected = str(config_path)
    # Conservative substring match — argv joined by spaces, paths can
    # contain spaces, so we accept any occurrence of the resolved
    # config path in the cmdline. A mismatched workspace's path won't
    # appear in our daemon's argv.
    return expected in cmdline


def _parse_iso_age_seconds(iso_text: str | None, *, now: datetime | None = None) -> float | None:
    """Return ``now - iso_text`` in seconds, or ``None`` on parse error.

    Tolerates the trailing ``Z`` suffix that older ``messages.created_at``
    rows use as well as the modern ``+00:00`` offset shape.
    """
    if not iso_text:
        return None
    text = iso_text.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    return max(0.0, (reference - ts).total_seconds())


def diagnose_rail_daemon(
    *,
    pid_path: Path,
    last_tick_iso: str | None,
    last_revival_at: datetime | None = None,
    now: datetime | None = None,
    stale_tick_seconds: float = DEFAULT_STALE_TICK_SECONDS,
    throttle_seconds: float = DEFAULT_REVIVAL_THROTTLE_SECONDS,
    config_path: Path | None = None,
) -> RevivalDecision:
    """Decide whether the rail daemon needs reviving — pure function.

    Args:
        pid_path: location of ``rail_daemon.pid`` (typically
            ``~/.pollypm/rail_daemon.pid``).
        last_tick_iso: ISO timestamp of the most recent ``scope=heartbeat``
            event in ``state.db``. Pass ``None`` when the database has
            no rows yet (fresh install — we still revive on the
            process-level signal).
        last_revival_at: when this supervisor last revived the daemon.
            Pass ``None`` on first call. Used to throttle.
        now: clock injection point (default ``datetime.now(UTC)``).
        stale_tick_seconds: age beyond which a tick-level signal flags
            the daemon as wedged. Default :data:`DEFAULT_STALE_TICK_SECONDS`.
        throttle_seconds: minimum gap between revivals. Default
            :data:`DEFAULT_REVIVAL_THROTTLE_SECONDS`.

    Returns:
        :class:`RevivalDecision` describing the daemon's state. The
        caller invokes :func:`check_and_revive_rail_daemon` to act on
        a decision with ``needs_revival == True``.
    """
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)

    pid = _read_pid(pid_path)
    last_age = _parse_iso_age_seconds(last_tick_iso, now=reference)

    # Throttle gate: even when the daemon looks dead, refuse to spawn
    # if we revived it less than ``throttle_seconds`` ago. Always
    # reports as ``"throttled"`` so callers can log the back-off.
    if last_revival_at is not None:
        if last_revival_at.tzinfo is None:
            last_revival_at = last_revival_at.replace(tzinfo=UTC)
        elapsed = (reference - last_revival_at).total_seconds()
        if elapsed < throttle_seconds:
            # Still inspect process state so the decision carries a
            # useful diagnosis, but the state is sticky to throttled.
            sub_state = _classify_state(
                pid, last_age, stale_tick_seconds, config_path=config_path,
            )
            return RevivalDecision(
                state="throttled",
                pid=pid,
                last_tick_age_seconds=last_age,
                reason=(
                    f"would-revive ({sub_state}) but last revival was "
                    f"{elapsed:.0f}s ago (< throttle {throttle_seconds:.0f}s)"
                ),
            )

    state = _classify_state(
        pid, last_age, stale_tick_seconds, config_path=config_path,
    )
    reason = _explain(state, pid, last_age, stale_tick_seconds)
    return RevivalDecision(
        state=state,
        pid=pid,
        last_tick_age_seconds=last_age,
        reason=reason,
    )


def _classify_state(
    pid: int | None,
    last_tick_age: float | None,
    stale_tick_seconds: float,
    *,
    config_path: Path | None = None,
) -> str:
    """Map (pid, last_tick_age) → state label (no throttling concerns).

    PID identity is verified before any "alive but stale" classification
    can promote a PID to a kill candidate: if the live PID's argv does
    not look like our rail daemon (likely PID reuse after the original
    daemon died), we classify it as ``dead_process`` so the upstream
    supervisor SKIPS the SIGTERM step entirely. Killing the wrong PID
    would terminate an unrelated user process — that's worse than
    failing to revive a daemon for one extra cycle.
    """
    if pid is None:
        return "missing_pid"
    if not _pid_is_alive(pid):
        return "dead_process"
    # The PID is alive — but is it OUR daemon? A PID-reuse hazard
    # exists when the original daemon died and the kernel handed the
    # number to an unrelated process. Verify identity via the cmdline.
    if not _pid_matches_daemon(pid, config_path):
        return "dead_process"
    if last_tick_age is not None and last_tick_age > stale_tick_seconds:
        return "stuck_no_tick"
    return "alive"


def _explain(
    state: str,
    pid: int | None,
    last_tick_age: float | None,
    stale_tick_seconds: float,
) -> str:
    if state == "alive":
        if last_tick_age is not None:
            return (
                f"pid={pid} alive, last heartbeat tick {last_tick_age:.0f}s ago"
            )
        return f"pid={pid} alive (no tick history yet — fresh boot)"
    if state == "missing_pid":
        return "no rail_daemon.pid file (cockpit never spawned the daemon, or it crashed before writing)"
    if state == "dead_process":
        return f"pid={pid} from rail_daemon.pid is no longer a live process"
    if state == "stuck_no_tick":
        return (
            f"pid={pid} alive but last heartbeat tick is {last_tick_age:.0f}s "
            f"old (> {stale_tick_seconds:.0f}s threshold) — wedged"
        )
    return state


def should_revive(decision: RevivalDecision) -> bool:
    """Public wrapper around :attr:`RevivalDecision.needs_revival`.

    Exposed as a top-level helper so callers don't have to import the
    dataclass just to branch on the result.
    """
    return decision.needs_revival


# ---------------------------------------------------------------------------
# Action (side-effecting)
# ---------------------------------------------------------------------------


def _terminate_with_grace(
    pid: int,
    *,
    grace_seconds: float = DEFAULT_SIGTERM_GRACE_SECONDS,
    sleep_fn: Callable[[float], None] = time.sleep,
    config_path: Path | None = None,
) -> str:
    """SIGTERM, wait, SIGKILL if needed. Mirrors ``rail_daemon_reaper``.

    Returns the signal label that ultimately took, or ``"already_gone"``
    if the process was gone before we sent anything, ``"denied"`` if
    we lacked permission, ``"identity_mismatch"`` if the PID's cmdline
    does not look like our daemon (PID-reuse hazard — refuse to
    signal), or ``"none"`` if ``pid <= 0``.

    The identity check is a SAFETY GUARD: it's possible for the original
    daemon to die between diagnosis and termination, and for the kernel
    to immediately reassign that PID to an unrelated process. SIGTERMing
    that process would kill a user's editor / shell / whatever — far
    worse than failing to revive the daemon. We re-verify identity here
    in case the diagnose layer was bypassed or raced.
    """
    if pid <= 0:
        return "none"
    if not _pid_is_alive(pid):
        return "already_gone"
    if not _pid_matches_daemon(pid, config_path):
        logger.warning(
            "rail_daemon_supervisor: refusing to signal pid=%d — cmdline "
            "does not match our rail daemon (PID-reuse safety guard)",
            pid,
        )
        return "identity_mismatch"
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "already_gone"
    except PermissionError:
        logger.warning(
            "rail_daemon_supervisor: SIGTERM denied for pid=%d (not our process)",
            pid,
        )
        return "denied"

    deadline = time.monotonic() + grace_seconds
    poll_interval = 0.1
    while time.monotonic() < deadline:
        if not _pid_is_alive(pid):
            return "SIGTERM"
        sleep_fn(poll_interval)

    # Re-verify identity before SIGKILL: it's possible (rare but real)
    # that our daemon SIGTERM-exited and the PID was reused by an
    # unrelated process within the grace window. SIGKILL is unblockable
    # so the cost of a wrong target is permanent.
    if not _pid_matches_daemon(pid, config_path):
        logger.warning(
            "rail_daemon_supervisor: pid=%d no longer matches daemon "
            "cmdline at SIGKILL stage; treating as already_gone",
            pid,
        )
        return "SIGTERM"

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return "SIGTERM"
    except PermissionError:
        return "denied"
    return "SIGKILL"


@contextmanager
def _supervisor_lock(
    pid_path: Path,
    *,
    timeout_seconds: float = 5.0,
) -> Iterator[bool]:
    """Acquire an exclusive cross-process lock around a revival critical section.

    The supervisor's diagnose → kill → unlink → spawn sequence is NOT
    atomic. Without a lock, two callers (cockpit periodic timer + cron
    ``pm heartbeat`` + a manual ``pm up``) can race: both diagnose
    "missing PID", one spawns successfully, and the second proceeds to
    unlink the freshly-written PID file and spawn ANOTHER daemon. The
    field outcome is two daemons running, both ticking the same DB —
    exactly the contention the rail daemon's PID-file lock was meant
    to prevent (but at the spawn-attempt layer, before the new daemon
    even gets to claim the PID file).

    Implementation: ``fcntl.flock`` (BSD-style advisory lock) on a
    sibling lockfile ``<pid_path>.supervisor.lock``. ``flock`` is per-fd,
    so each process gets its own contention point and the kernel
    serializes acquisitions. Timeout via non-blocking acquire + sleep
    polling rather than ``LOCK_EX`` blocking, so a runaway holder can't
    pin the supervisor forever.

    Yields ``True`` when the lock was acquired, ``False`` when the
    timeout expired. Callers MUST treat ``False`` as "another supervisor
    is active; do nothing this cycle" — re-running diagnosis after a
    timeout would invite the same race we're trying to prevent.
    """
    lock_path = pid_path.with_name(pid_path.name + ".supervisor.lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        # If we can't even create the parent, fall through; the open
        # below will raise and we'll yield False.
        pass

    try:
        import fcntl  # POSIX-only; supervisor is intended for macOS / Linux.
    except ImportError:
        # Non-POSIX: fall back to a best-effort O_EXCL lockfile pattern.
        yield from _supervisor_lock_oexcl(lock_path, timeout_seconds)
        return

    fd: int | None = None
    try:
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        logger.debug("supervisor lock open failed: %s", exc)
        yield False
        return

    acquired = False
    deadline = time.monotonic() + timeout_seconds
    poll = 0.05
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(poll)
        yield acquired
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _supervisor_lock_oexcl(
    lock_path: Path,
    timeout_seconds: float,
) -> Iterator[bool]:
    """O_EXCL fallback for platforms without fcntl (mainly Windows).

    Less robust than flock — a crashed holder leaves a stale file that
    must time out — but the supervisor's flock path is the supported
    one. This exists so the import doesn't blow up on hypothetical
    non-POSIX environments.
    """
    deadline = time.monotonic() + timeout_seconds
    fd: int | None = None
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                yield False
                return
            time.sleep(0.05)
    try:
        yield True
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass


def _emit_revival_audit(
    *,
    decision: RevivalDecision,
    revived: bool,
    spawn_error: str | None,
    killed_pid: int | None,
    kill_signal: str | None,
) -> None:
    """Best-effort ``daemon.revived`` audit emit.

    Mirrors :func:`pollypm.rail_daemon_reaper._emit_audit` — never
    raises, uses the ``_workspace`` project key because the rail
    daemon belongs to no single project.
    """
    try:
        from pollypm.audit import emit as _audit_emit
        from pollypm.audit.log import EVENT_DAEMON_REVIVED
    except Exception:  # noqa: BLE001
        return
    try:
        _audit_emit(
            event=EVENT_DAEMON_REVIVED,
            project="_workspace",
            subject=f"rail_daemon/{decision.pid or 'none'}",
            actor="system",
            status="warn" if revived else "ok",
            metadata={
                "role": "rail",
                "state": decision.state,
                "reason": decision.reason,
                "previous_pid": decision.pid,
                "last_tick_age_s": decision.last_tick_age_seconds,
                "revived": revived,
                "spawn_error": spawn_error,
                "killed_pid": killed_pid,
                "kill_signal": kill_signal,
            },
        )
    except Exception:  # noqa: BLE001
        pass


def check_and_revive_rail_daemon(
    *,
    config_path: Path,
    pid_path: Path,
    last_tick_iso: str | None,
    last_revival_at: datetime | None,
    now: datetime | None = None,
    stale_tick_seconds: float = DEFAULT_STALE_TICK_SECONDS,
    throttle_seconds: float = DEFAULT_REVIVAL_THROTTLE_SECONDS,
    spawn_fn: Callable[[Path], None] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    emit_audit: bool = True,
    cron: bool = False,
    lock_timeout_seconds: float = 5.0,
) -> RevivalResult:
    """Diagnose the rail daemon and respawn it if it's dead / stuck.

    The single entry point for layer-2 supervision. Safe to call from
    any process (cockpit periodic timer, ``pm heartbeat`` cron, tests)
    — concurrency between callers is bounded by the daemon's own
    PID-file lock (``rail_daemon._claim_pid_file``), so a duplicate
    spawn won't produce two live daemons.

    Args:
        config_path: ``~/.pollypm/pollypm.toml`` (or the test override).
            Forwarded to :func:`pollypm.cli._spawn_rail_daemon` so the
            new daemon points at the same workspace.
        pid_path: location of ``rail_daemon.pid``.
        last_tick_iso: most recent ``scope=heartbeat`` ISO timestamp.
            Caller queries ``StateStore.last_heartbeat_at()``.
        last_revival_at: when this supervisor last revived. Pass
            ``None`` on first call; the caller persists this across
            calls (e.g. as a Cockpit attribute).
        now: clock injection point.
        stale_tick_seconds, throttle_seconds: see :func:`diagnose_rail_daemon`.
        spawn_fn: spawner callable (default
            :func:`pollypm.cli._spawn_rail_daemon`). Tests inject a
            mock so they don't actually spawn detached subprocesses.
        sleep_fn: sleep injection for SIGTERM grace window.
        emit_audit: when ``False``, skip the ``daemon.revived`` audit
            emit (used by tests that don't want to write to the real
            audit log).
        cron: when ``True``, the caller is the ``pm heartbeat`` cron
            path (NOT the cockpit periodic timer). In that mode, a
            ``missing_pid`` decision is downgraded to a no-op: the
            cockpit may be hosting an in-process ``HeartbeatRail``
            (which leaves no PID file by design), and spawning a
            headless daemon now would create a second concurrent
            ticker. Cron still acts on ``dead_process`` / ``stuck_no_tick``
            — those mean a daemon WAS recorded but isn't running, which
            never happens under cockpit-hosted mode.
        lock_timeout_seconds: how long to wait for the supervisor lock
            before giving up this cycle. The lock prevents a
            concurrent-revival race that would otherwise spawn two
            daemons. Default 5s — covers the SIGTERM grace + spawn
            latency without making the caller block forever.

    Returns:
        :class:`RevivalResult` carrying the diagnosis, whether a spawn
        was attempted, and any signal / spawn error details.
    """
    decision = diagnose_rail_daemon(
        pid_path=pid_path,
        last_tick_iso=last_tick_iso,
        last_revival_at=last_revival_at,
        now=now,
        stale_tick_seconds=stale_tick_seconds,
        throttle_seconds=throttle_seconds,
        config_path=config_path,
    )

    if not decision.needs_revival:
        # No-op for healthy / throttled daemons. We still log at debug
        # so a flood of "alive" decisions is auditable when needed.
        logger.debug(
            "rail_daemon_supervisor: %s — %s", decision.state, decision.reason,
        )
        return RevivalResult(
            decision=decision,
            revived=False,
            spawn_error=None,
            killed_pid=None,
            kill_signal=None,
        )

    # Cron-path guard: a missing PID file from cron MAY mean the cockpit
    # is hosting the rail in-process (no headless daemon ever existed).
    # Spawning a daemon now would race the cockpit's in-thread ticker.
    # Cron only acts on dead_process / stuck_no_tick — those imply a
    # daemon was recorded and lost, which doesn't happen in
    # cockpit-hosted mode (it never writes a PID file).
    if cron and decision.state == "missing_pid":
        logger.debug(
            "rail_daemon_supervisor: cron skipping missing_pid revival "
            "(cockpit may be hosting rail in-process)",
        )
        return RevivalResult(
            decision=decision,
            revived=False,
            spawn_error=None,
            killed_pid=None,
            kill_signal=None,
        )

    logger.warning(
        "rail_daemon_supervisor: reviving heartbeat — %s", decision.reason,
    )

    # Wrap the kill/unlink/spawn critical section in a cross-process
    # lock so two concurrent supervisors (cockpit + cron, or two
    # consecutive cron ticks while a previous spawn is in flight) can't
    # produce duplicate daemons. Without this, both callers would
    # diagnose missing/dead, one spawns, the other unlinks the new
    # PID file and spawns again — leaving two tickers fighting over
    # the same DB.
    with _supervisor_lock(pid_path, timeout_seconds=lock_timeout_seconds) as locked:
        if not locked:
            logger.info(
                "rail_daemon_supervisor: another supervisor holds the lock "
                "(%.1fs timeout); skipping this cycle",
                lock_timeout_seconds,
            )
            return RevivalResult(
                decision=decision,
                revived=False,
                spawn_error="lock_busy",
                killed_pid=None,
                kill_signal=None,
            )

        # Re-check liveness inside the lock. Another supervisor that
        # held the lock when we started waiting may have already
        # spawned a fresh daemon — in that case the PID file now points
        # at a live, healthy process and we must NOT proceed. Without
        # this re-check we'd diagnose-stale + spawn anyway and end up
        # with two daemons.
        recheck = diagnose_rail_daemon(
            pid_path=pid_path,
            last_tick_iso=last_tick_iso,
            # last_revival_at is intentionally None here so the recheck
            # reflects current process state without re-applying the
            # caller's throttle window.
            last_revival_at=None,
            now=now,
            stale_tick_seconds=stale_tick_seconds,
            throttle_seconds=throttle_seconds,
            config_path=config_path,
        )
        if not recheck.needs_revival:
            logger.info(
                "rail_daemon_supervisor: post-lock recheck shows %s — "
                "another supervisor must have already revived; standing down",
                recheck.state,
            )
            return RevivalResult(
                decision=recheck,
                revived=False,
                spawn_error=None,
                killed_pid=None,
                kill_signal=None,
            )

        # Use the recheck's PID (which reflects current state) for any
        # signaling — the original ``decision.pid`` may be stale.
        diagnosed_pid = recheck.pid

        # Step 1: clean up the dead/stuck process. ``stuck_no_tick``
        # means the PID is alive but unresponsive; SIGTERM it so the
        # new daemon can claim the PID file. ``dead_process`` and
        # ``missing_pid`` skip this entirely.
        killed_pid: int | None = None
        kill_signal: str | None = None
        if recheck.state == "stuck_no_tick" and diagnosed_pid is not None:
            kill_signal = _terminate_with_grace(
                diagnosed_pid, sleep_fn=sleep_fn, config_path=config_path,
            )
            if kill_signal in ("SIGTERM", "SIGKILL", "already_gone"):
                killed_pid = diagnosed_pid
            elif kill_signal in ("denied", "identity_mismatch"):
                # Couldn't (or refused to) kill it — bail without
                # spawning so we don't end up with two daemons or
                # corrupt an unrelated user process.
                err_label = (
                    "signal_denied" if kill_signal == "denied"
                    else "identity_mismatch"
                )
                logger.warning(
                    "rail_daemon_supervisor: skipping respawn for pid=%d "
                    "(%s)", diagnosed_pid, err_label,
                )
                result = RevivalResult(
                    decision=recheck,
                    revived=False,
                    spawn_error=err_label,
                    killed_pid=None,
                    kill_signal=kill_signal,
                )
                if emit_audit:
                    _emit_revival_audit(
                        decision=recheck,
                        revived=False,
                        spawn_error=err_label,
                        killed_pid=None,
                        kill_signal=kill_signal,
                    )
                return result

        # Step 2: compare-and-unlink. Only unlink the PID file if it
        # still names the PID we diagnosed (or is unreadable / empty).
        # If a concurrent supervisor already overwrote it with a fresh
        # daemon's PID — which we'd see as a different number — we
        # MUST NOT clobber that file: doing so removes the freshly-
        # spawned daemon's ownership marker and the next caller will
        # spawn ANOTHER daemon.
        try:
            if pid_path.exists():
                current_pid = _read_pid(pid_path)
                if current_pid is None or current_pid == diagnosed_pid:
                    pid_path.unlink(missing_ok=True)
                else:
                    logger.warning(
                        "rail_daemon_supervisor: pid_path now points at "
                        "pid=%d (was %s); a concurrent supervisor must "
                        "have spawned — standing down",
                        current_pid, diagnosed_pid,
                    )
                    return RevivalResult(
                        decision=recheck,
                        revived=False,
                        spawn_error=None,
                        killed_pid=killed_pid,
                        kill_signal=kill_signal,
                    )
        except OSError as exc:
            logger.warning(
                "rail_daemon_supervisor: could not unlink %s: %s — "
                "_claim_pid_file should still recover", pid_path, exc,
            )

        # Step 3: spawn the new daemon. Default to the cli helper that
        # ``pm up`` uses; tests inject a mock.
        spawner = spawn_fn or _default_spawn_fn()
        spawn_error: str | None = None
        try:
            spawner(config_path)
        except Exception as exc:  # noqa: BLE001
            spawn_error = f"{type(exc).__name__}: {exc}"
            logger.exception("rail_daemon_supervisor: spawn failed")

        revived = spawn_error is None
        result = RevivalResult(
            decision=recheck,
            revived=revived,
            spawn_error=spawn_error,
            killed_pid=killed_pid,
            kill_signal=kill_signal,
        )
        if emit_audit:
            _emit_revival_audit(
                decision=recheck,
                revived=revived,
                spawn_error=spawn_error,
                killed_pid=killed_pid,
                kill_signal=kill_signal,
            )
        return result


def _default_spawn_fn() -> Callable[[Path], None]:
    """Resolve the cli's ``_spawn_rail_daemon`` lazily.

    Lazy import keeps ``rail_daemon_supervisor`` importable from CLI
    boot helpers without pulling in the full ``cli`` module's
    typer-decorated surface.
    """
    from pollypm import cli as _cli

    return _cli._spawn_rail_daemon


# ---------------------------------------------------------------------------
# Convenience helpers for callers (cockpit / cli)
# ---------------------------------------------------------------------------


def query_last_heartbeat_iso(state_db_path: Path) -> str | None:
    """Open the StateStore read-only and return ``last_heartbeat_at()``.

    Returns ``None`` when the DB doesn't exist (fresh install) or any
    error occurs — callers must treat ``None`` as "no tick history",
    not "tick stale".
    """
    if not state_db_path.exists():
        return None
    try:
        from pollypm.storage.state import StateStore
    except Exception:  # noqa: BLE001
        return None
    try:
        store = StateStore(state_db_path, readonly=True)
    except Exception:  # noqa: BLE001
        return None
    try:
        return store.last_heartbeat_at()
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            close = getattr(store, "close", None)
            if callable(close):
                close()
        except Exception:  # noqa: BLE001
            pass


def revive_if_needed(
    *,
    config_path: Path,
    state_db_path: Path,
    pid_path: Path,
    last_revival_at: datetime | None,
    **kwargs: Any,
) -> RevivalResult:
    """One-call wrapper that resolves the tick signal from state.db.

    Designed for periodic-timer callers (cockpit, ``pm heartbeat``) so
    they only need to remember the four paths + their persisted
    ``last_revival_at`` between calls.
    """
    last_tick_iso = query_last_heartbeat_iso(state_db_path)
    return check_and_revive_rail_daemon(
        config_path=config_path,
        pid_path=pid_path,
        last_tick_iso=last_tick_iso,
        last_revival_at=last_revival_at,
        **kwargs,
    )
