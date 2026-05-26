"""Headless process that keeps the heartbeat + recovery rail ticking.

Without this, the rail only runs inside the cockpit TUI (see
``cockpit_ui.CockpitRail._start_core_rail``). When the cockpit isn't
open — or it crashes — nothing fires the recovery handlers that
resurrect a dead ``pm-operator``, nothing rotates logs, nothing sweeps
stale alerts. The 2026-04-19 outage (operator down 5hr, no auto
recovery) traces back to exactly this gap.

The daemon is a small ``while True: sleep()`` that:

1. Boots the same ``CoreRail`` the cockpit would.
2. Writes its PID to ``~/.pollypm/rail_daemon.pid`` so ``pm up`` can
   detect an existing daemon and ``pm reset`` can stop it cleanly.
3. Handles ``SIGTERM`` / ``SIGINT`` by calling ``CoreRail.stop()``
   and removing the PID file. A background watchdog thread escalates
   to ``SIGKILL`` self if graceful shutdown doesn't complete within
   ``_SIGTERM_WATCHDOG_GRACE`` seconds (#1591 — the chaos-primitive
   ``pkill -f pollypm.rail_daemon`` is otherwise silently a no-op
   when the ticker thread is holding the GIL inside a sqlite call).

The rail's own ticker thread does the actual work — this process
just keeps the Python interpreter alive so the thread can run.
"""

from __future__ import annotations

import argparse
import atexit
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger("pollypm.rail_daemon")


def _pid_file(home: Path) -> Path:
    return home / "rail_daemon.pid"


def _lock_file(home: Path) -> Path:
    """Path to the lifetime flock guard (``~/.pollypm/rail_daemon.lock``).

    Distinct from the PID file: the PID file can be unlinked at any
    time (``pm reset`` deletes it pre-emptively, the supervisor rewrites
    it during revival), but this lockfile is held by ``fcntl.flock`` for
    the entire lifetime of the daemon process. The OS releases the lock
    when the holder exits (graceful, SIGKILL, or crash), so a second
    ``python -m pollypm.rail_daemon`` cannot run concurrently regardless
    of PID-file state — closing the #1586 hole where ``pm reset --force``
    SIGTERM'd a stuck daemon, unlinked the PID file, but the daemon
    ignored SIGTERM and ``pm up`` spawned a second one with no PID file
    to gate it.
    """
    return home / "rail_daemon.lock"


def _crash_loop_file(home: Path) -> Path:
    return home / "rail_daemon.crash_loop.json"


def _acquire_lifetime_lock(lock_path: Path) -> int | None:
    """Acquire the lifetime ``flock`` and return the open fd, or ``None``.

    The returned fd MUST be kept alive by the caller for the lifetime
    of the daemon; closing it (or letting it be garbage-collected via
    a lost reference) drops the lock and re-opens the duplicate-spawn
    window. We deliberately leak it into a module-global below.

    Returns ``None`` when another live daemon already holds the lock
    or when fcntl is unavailable on this platform (Windows). The
    Windows fallback intentionally lets the existing ``_claim_pid_file``
    O_EXCL guard handle duplicate-spawn prevention — fcntl-less platforms
    were never the failure surface for #1586.
    """
    try:
        import fcntl  # POSIX-only; rail daemon targets macOS / Linux.
    except ImportError:
        return None

    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        # If we cannot even create the parent dir, fall through; the
        # open() below will raise and we'll return None.
        pass

    try:
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as exc:
        logger.warning("rail_daemon: could not open lock file %s: %s", lock_path, exc)
        return None

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Another daemon process holds the lock — close our fd and bail.
        try:
            os.close(fd)
        except OSError:
            pass
        return None
    except OSError as exc:
        logger.warning("rail_daemon: flock failed on %s: %s", lock_path, exc)
        try:
            os.close(fd)
        except OSError:
            pass
        return None

    return fd


# Module-global reference that keeps the lifetime-lock fd from being
# garbage-collected. Closing the fd releases the flock, so we MUST hold
# this reference for as long as the daemon is running.
_LIFETIME_LOCK_FD: int | None = None

# #1591 — SIGTERM watchdog deadline. The signal handler arms this so a
# background thread can SIGKILL the process if graceful shutdown stalls
# (e.g. ``rail.stop()`` blocked behind a long sqlite call that the
# ticker thread is holding the GIL for). Module-global so tests can
# override the grace window via ``_SIGTERM_WATCHDOG_GRACE``.
_SIGTERM_WATCHDOG_GRACE: float = 2.0


def _claim_pid_file(pid_path: Path) -> bool:
    """Atomically write our PID iff no live daemon already holds the file.

    The supervisor's flock at the parent layer is the primary guard
    against duplicate spawns, but it cannot help when two daemons are
    started by completely independent paths (two ``pm up`` invocations,
    cron tick that bypasses the supervisor, launchd KeepAlive racing a
    cockpit revive). Belt-and-suspenders: this claim itself uses
    ``O_EXCL`` so two children that both pass the initial existence
    check still see exactly one winner — the loser gets ``FileExistsError``
    and exits cleanly, leaving the original holder undisturbed.

    A stale PID file (process no longer exists) is overwritten. The
    overwrite path is racy on its own — two callers can both observe
    the stale file, both unlink, both ``O_EXCL`` create — but exactly
    one will win the create, so the duplicate-spawn outcome is
    impossible regardless.

    Returns True on successful claim, False when a live daemon
    already owns the slot.
    """
    def _read_existing_pid() -> int:
        try:
            return int(pid_path.read_text().strip())
        except (ValueError, OSError):
            return 0

    if pid_path.exists():
        existing = _read_existing_pid()
        if existing <= 0:
            # Another daemon may have won O_EXCL and not written its PID
            # bytes yet. Give that tiny critical section a chance to
            # finish before treating garbage/empty contents as stale.
            time.sleep(0.05)
            existing = _read_existing_pid()
        if existing > 0 and _pid_alive(existing):
            return False
        # Stale file — clear it so our O_EXCL create below can succeed.
        # ``missing_ok`` is fine; another concurrent reaper may have
        # already unlinked it.
        try:
            pid_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # Best-effort cleanup; the O_EXCL below will surface any
            # genuine issue as a FileExistsError → False.
            pass
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    # ``O_EXCL | O_CREAT`` guarantees that two simultaneous claimers
    # see exactly one success: the loser hits ``FileExistsError`` and
    # bails (the daemon's caller logs and exits). Without this, two
    # daemons can both observe a missing file, both ``write_text``,
    # and the second write silently overwrites — leaving two live
    # tickers with only one named in the PID file.
    try:
        fd = os.open(
            str(pid_path),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o644,
        )
    except FileExistsError:
        return False
    except OSError:
        return False
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
    finally:
        os.close(fd)
    return True


def _pid_alive(pid: int) -> bool:
    """Return True iff ``pid`` names a currently-live process."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but belongs to another user; treat as
        # live from our POV rather than claim the slot.
        return True
    return True


def _apply_pending_migrations_for_startup(db_path: Path) -> int:
    """Apply pending workspace migrations before the daemon opens services.

    The rail daemon is the long-lived process that owns heartbeat/job
    draining. After an upgrade, launchd or ``pm rail-daemon`` may start
    it directly before the operator has run ``pm migrate --apply``. At
    this point the daemon has not opened the store yet; applying the
    append-only workspace migrations here avoids a silent restart loop
    while preserving the refuse-start gate for failed migrations.

    Returns the number of migrations applied.
    """
    from pollypm.store import migrations as _migrations

    if _migrations.bypass_env_is_set():
        return 0
    try:
        status = _migrations.inspect(db_path)
    except _migrations.UnusableDatabaseError as exc:
        _migrations.exit_unusable_database(exc)
    if status.up_to_date:
        return 0
    logger.warning(
        "rail_daemon: applying %d pending schema migration(s) before startup",
        len(status.pending),
    )
    try:
        outcome = _migrations.apply(db_path)
    except _migrations.UnusableDatabaseError as exc:
        _migrations.exit_unusable_database(exc)
    except Exception:  # noqa: BLE001
        logger.exception("rail_daemon: startup migration apply failed")
        _migrations.require_no_pending_or_exit(db_path)
        return 0
    applied = len(outcome.applied)
    logger.warning(
        "rail_daemon: applied %d schema migration(s) before startup",
        applied,
    )
    _migrations.require_no_pending_or_exit(db_path)
    return applied


def run(config_path: Path, *, poll_interval: float = 60.0) -> int:
    """Run the daemon loop. Blocks until signalled.

    Returns the process exit code; 0 on graceful shutdown, 1 if
    another daemon already holds the PID lock.
    """
    from pollypm.config import load_config, DEFAULT_CONFIG_PATH
    from pollypm.service_api import PollyPMService

    cfg = load_config(config_path)
    pollypm_home = Path(DEFAULT_CONFIG_PATH).parent
    pid_path = _pid_file(pollypm_home)
    lock_path = _lock_file(pollypm_home)

    # #1586 — lifetime flock is the airtight idempotency guard. The PID
    # file alone is insufficient: ``pm reset --force`` SIGTERMs the
    # daemon then unlinks the PID file pre-emptively, so a stuck daemon
    # that ignores SIGTERM leaves no PID-file record for the next
    # ``pm up`` to gate against. The flock survives any PID-file
    # manipulation — only an actual process exit releases it.
    global _LIFETIME_LOCK_FD
    lock_fd = _acquire_lifetime_lock(lock_path)
    if lock_fd is None:
        # Either another daemon holds the lock, or we couldn't open it
        # at all. In both cases the safe outcome is to exit cleanly so
        # ``pm up`` is idempotent. If the cause was a transient open
        # failure, the supervisor will retry on its next tick.
        logger.warning(
            "rail_daemon: another daemon already holds %s (PID file: %s) "
            "— exiting cleanly so this invocation is a no-op",
            lock_path, pid_path,
        )
        return 1
    _LIFETIME_LOCK_FD = lock_fd

    try:
        _apply_pending_migrations_for_startup(cfg.project.state_db)
    except BaseException:
        try:
            os.close(lock_fd)
        except OSError:
            pass
        _LIFETIME_LOCK_FD = None
        raise

    if not _claim_pid_file(pid_path):
        # Lock was acquired but PID file is held by a still-live process.
        # This is rare (would require the lock holder to have crashed
        # between flock acquisition and PID-file write while leaving a
        # second process owning the PID file), but bail rather than risk
        # a mismatch. Releasing the flock by closing the fd lets a
        # retry succeed once the inconsistency clears.
        logger.warning(
            "rail_daemon: another daemon already holds %s — exiting",
            pid_path,
        )
        try:
            os.close(lock_fd)
        except OSError:
            pass
        _LIFETIME_LOCK_FD = None
        return 1

    supervisor = PollyPMService(config_path).load_supervisor()
    rail = getattr(supervisor, "core_rail", None)
    if rail is None:
        logger.error("rail_daemon: supervisor has no core_rail attribute")
        pid_path.unlink(missing_ok=True)
        return 2

    stopping = {"flag": False, "deadline": 0.0}

    def _fast_exit_watchdog() -> None:
        """Backstop: SIGKILL ourselves if graceful shutdown stalls.

        Python signal handlers run only at bytecode checkpoints in the
        main thread, and ``rail.stop()`` itself can block behind a
        long-running sqlite call that the heartbeat ticker thread is
        executing under the GIL. Without this backstop, ``pkill -f
        pollypm.rail_daemon`` (default SIGTERM) is silently a no-op —
        the daemon stays alive at 100%+ CPU until the operator escalates
        to ``-9``. That broke #1591's chaos-test primitive and any
        supervisor path that uses SIGTERM-then-respawn.

        We watch ``stopping['deadline']`` on a tight loop; when the
        signal handler arms it, we sleep until that wall-clock instant
        and then SIGKILL ourselves regardless of where the main thread
        is stuck.
        """
        # Sleep until the signal handler arms a deadline.
        while not stopping["flag"]:
            time.sleep(0.05)
        # Honour any deadline set by the handler, then escalate.
        now = time.monotonic()
        remaining = max(0.0, stopping["deadline"] - now)
        time.sleep(remaining)
        # Last chance — if we got here we exceeded the grace window.
        logger.warning(
            "rail_daemon: graceful shutdown exceeded %.1fs grace — SIGKILL self",
            _SIGTERM_WATCHDOG_GRACE,
        )
        try:
            os.kill(os.getpid(), signal.SIGKILL)
        except OSError:
            # Belt + braces: if SIGKILL self somehow fails, fall through
            # to os._exit so we still leave the process table.
            os._exit(137)

    def _shutdown(signum: int, _frame: object) -> None:
        # Re-entry safe: if a second signal arrives while we're already
        # winding down, just refresh the deadline rather than reset it.
        if not stopping["flag"]:
            logger.info(
                "rail_daemon: received signal %d — shutting down "
                "(SIGKILL backstop in %.1fs)",
                signum, _SIGTERM_WATCHDOG_GRACE,
            )
            stopping["deadline"] = time.monotonic() + _SIGTERM_WATCHDOG_GRACE
            stopping["flag"] = True
        # Best-effort: interrupt any in-flight sqlite call on this
        # process's state-store connection so ``rail.stop()`` can make
        # progress. The heartbeat ticker may be holding the GIL inside
        # a long ``sqlite3_step``; ``Connection.interrupt`` is one of
        # the few sqlite APIs documented as safe to call from another
        # thread / signal handler.
        try:
            store = rail.get_state_store()
            conn = getattr(store, "_conn", None)
            if conn is not None:
                conn.interrupt()
        except Exception:  # noqa: BLE001 — best-effort
            pass

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Start the watchdog before rail.start() so a SIGTERM during boot
    # still gets the SIGKILL backstop. Daemon=True so it never blocks
    # interpreter shutdown.
    threading.Thread(
        target=_fast_exit_watchdog,
        name="rail_daemon-sigterm-watchdog",
        daemon=True,
    ).start()

    def _cleanup() -> None:
        try:
            rail.stop()
        except Exception:  # noqa: BLE001
            pass
        pid_path.unlink(missing_ok=True)
        # Release the lifetime flock so the next ``pm up`` can re-acquire
        # without waiting on kernel cleanup. Process exit would release
        # it anyway, but explicit close keeps tests deterministic.
        global _LIFETIME_LOCK_FD
        if _LIFETIME_LOCK_FD is not None:
            try:
                os.close(_LIFETIME_LOCK_FD)
            except OSError:
                pass
            _LIFETIME_LOCK_FD = None

    atexit.register(_cleanup)

    try:
        rail.start()
    except Exception:  # noqa: BLE001
        logger.exception("rail_daemon: core_rail.start() failed")
        _cleanup()
        return 3
    _crash_loop_file(pollypm_home).unlink(missing_ok=True)

    logger.info(
        "rail_daemon: started (pid=%d, poll=%.1fs) — heartbeat rail live",
        os.getpid(), poll_interval,
    )

    # The rail's internal ticker thread does the work; we just keep
    # the interpreter alive so the thread can run. Short slices (rather
    # than ``time.sleep(poll_interval)``) keep us responsive to signals
    # even if some platform / libc quirk swallows the EINTR wakeup —
    # POSIX *should* interrupt the sleep on SIGTERM, but #1591 showed
    # the daemon can be unkillable in practice, so we don't rely on it.
    deadline = time.monotonic() + poll_interval
    while not stopping["flag"]:
        slice_s = min(0.5, max(0.0, deadline - time.monotonic()))
        if slice_s <= 0:
            deadline = time.monotonic() + poll_interval
            continue
        time.sleep(slice_s)

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pm-rail-daemon")
    parser.add_argument(
        "--config", type=Path, default=None,
        help="PollyPM config path (defaults to ~/.pollypm/pollypm.toml).",
    )
    parser.add_argument(
        "--poll-interval", type=float, default=60.0,
        help="Seconds between idle-loop wakeups (the rail's own ticker "
             "runs independently; this only governs signal-check cadence).",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        help="Logging level (DEBUG/INFO/WARNING/ERROR).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    # Attach the centralized error log before any plugin / rail code
    # runs so boot-time crashes are captured alongside runtime ones.
    from pollypm.error_log import install as _install_error_log
    _install_error_log(process_label="rail_daemon")

    from pollypm.config import DEFAULT_CONFIG_PATH
    config_path = args.config or DEFAULT_CONFIG_PATH
    return run(config_path, poll_interval=args.poll_interval)


if __name__ == "__main__":
    sys.exit(main())
