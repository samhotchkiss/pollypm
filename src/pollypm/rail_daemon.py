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
    if not _claim_pid_file(pid_path):
        logger.warning(
            "rail_daemon: another daemon already holds %s — exiting",
            pid_path,
        )
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
