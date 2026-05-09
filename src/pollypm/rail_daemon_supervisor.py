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
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

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
            sub_state = _classify_state(pid, last_age, stale_tick_seconds)
            return RevivalDecision(
                state="throttled",
                pid=pid,
                last_tick_age_seconds=last_age,
                reason=(
                    f"would-revive ({sub_state}) but last revival was "
                    f"{elapsed:.0f}s ago (< throttle {throttle_seconds:.0f}s)"
                ),
            )

    state = _classify_state(pid, last_age, stale_tick_seconds)
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
) -> str:
    """Map (pid, last_tick_age) → state label (no throttling concerns)."""
    if pid is None:
        return "missing_pid"
    if not _pid_is_alive(pid):
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
) -> str:
    """SIGTERM, wait, SIGKILL if needed. Mirrors ``rail_daemon_reaper``.

    Returns the signal label that ultimately took, or ``"already_gone"``
    if the process was gone before we sent anything, or ``"denied"`` if
    we lacked permission. ``"none"`` if ``pid <= 0``.
    """
    if pid <= 0:
        return "none"
    if not _pid_is_alive(pid):
        return "already_gone"
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

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return "SIGTERM"
    except PermissionError:
        return "denied"
    return "SIGKILL"


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

    logger.warning(
        "rail_daemon_supervisor: reviving heartbeat — %s", decision.reason,
    )

    # Step 1: clean up the dead/stuck process. ``stuck_no_tick`` means
    # the PID is alive but unresponsive; we SIGTERM it so the new
    # daemon can claim the PID file. ``dead_process`` and
    # ``missing_pid`` skip this entirely.
    killed_pid: int | None = None
    kill_signal: str | None = None
    if decision.state == "stuck_no_tick" and decision.pid is not None:
        kill_signal = _terminate_with_grace(decision.pid, sleep_fn=sleep_fn)
        if kill_signal in ("SIGTERM", "SIGKILL", "already_gone"):
            killed_pid = decision.pid
        elif kill_signal == "denied":
            # Couldn't kill it — bail without spawning so we don't end
            # up with two daemons fighting for the same DB.
            logger.warning(
                "rail_daemon_supervisor: cannot signal pid=%d (permission "
                "denied); skipping respawn — manual intervention required",
                decision.pid,
            )
            result = RevivalResult(
                decision=decision,
                revived=False,
                spawn_error="signal_denied",
                killed_pid=None,
                kill_signal=kill_signal,
            )
            if emit_audit:
                _emit_revival_audit(
                    decision=decision,
                    revived=False,
                    spawn_error="signal_denied",
                    killed_pid=None,
                    kill_signal=kill_signal,
                )
            return result

    # Step 2: clear stale PID file. ``rail_daemon._claim_pid_file``
    # already overwrites stale files but only when the stored PID is
    # confirmed dead; for ``stuck_no_tick`` we just killed the PID, so
    # the file we want gone may still claim "alive" between the kill
    # and the new daemon's first os.kill probe. Belt-and-suspenders.
    try:
        if pid_path.exists():
            pid_path.unlink(missing_ok=True)
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
        decision=decision,
        revived=revived,
        spawn_error=spawn_error,
        killed_pid=killed_pid,
        kill_signal=kill_signal,
    )
    if emit_audit:
        _emit_revival_audit(
            decision=decision,
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
