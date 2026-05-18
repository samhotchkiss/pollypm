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
   and removing the PID file.

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
    if pid_path.exists():
        try:
            existing = int(pid_path.read_text().strip())
        except (ValueError, OSError):
            existing = 0
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


def run(config_path: Path, *, poll_interval: float = 60.0) -> int:
    """Run the daemon loop. Blocks until signalled.

    Returns the process exit code; 0 on graceful shutdown, 1 if
    another daemon already holds the PID lock.
    """
    from pollypm.config import load_config, DEFAULT_CONFIG_PATH
    from pollypm.service_api import PollyPMService
    from pollypm.store import migrations as _migrations

    cfg = load_config(config_path)
    # Refuse-start gate (#717): the daemon opens the state store and
    # would silently run migrations otherwise. Exit loudly so the
    # operator runs ``pm migrate --apply`` from a terminal instead.
    _migrations.require_no_pending_or_exit(cfg.project.state_db)
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

    stopping = {"flag": False}

    def _shutdown(signum: int, _frame: object) -> None:
        logger.info("rail_daemon: received signal %d — shutting down", signum)
        stopping["flag"] = True

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

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

    logger.info(
        "rail_daemon: started (pid=%d, poll=%.1fs) — heartbeat rail live",
        os.getpid(), poll_interval,
    )

    # The rail's internal ticker thread does the work; we just keep
    # the interpreter alive so the thread can run.
    while not stopping["flag"]:
        time.sleep(poll_interval)

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
