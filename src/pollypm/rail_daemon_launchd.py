"""Layer-3 supervision: macOS LaunchAgent for ``pollypm.rail_daemon``.

Why a third layer
-----------------
PollyPM's heartbeat supervision has three nested layers:

1. **Tick-loop retry (in-thread)** — :class:`HeartbeatRail._tick_loop`
   wraps each tick in try/except so a single bad tick can't take the
   loop down. Handles transient errors like SQLite WAL contention.
2. **External process supervision (rail_daemon_supervisor)** — the
   cockpit periodic timer + ``pm heartbeat`` cron path call
   :func:`pollypm.rail_daemon_supervisor.check_and_revive_rail_daemon`
   to detect a dead/stuck daemon and respawn it. Handles process-
   level death (OOM, segfault, sandbox kill).
3. **OS-level supervision (this module)** — launchd watches the rail
   daemon process and restarts it if it dies, AND launches it
   automatically on machine boot. Handles the cases layer-2 can't:
   the cockpit isn't open, the user just rebooted, or the cron job
   itself isn't installed.

Layer 3 is opt-in because writing to ``~/Library/LaunchAgents/`` is a
user-level action that not every operator wants the CLI to do without
asking. Once installed, layer 3 is the strongest guarantee available
without root: launchd ``KeepAlive=true`` guarantees the daemon stays
running modulo system shutdown / explicit ``launchctl unload``.

Plist shape
-----------
A minimal ``KeepAlive`` LaunchAgent. Key choices:

* ``KeepAlive: true`` — restart the daemon any time it exits, regardless
  of exit code. We *want* aggressive restarts; the rail-daemon side
  refuses to start if another live daemon already holds the PID file
  (``rail_daemon._claim_pid_file``), so a doubled restart is harmless.
* ``RunAtLoad: true`` — boot the daemon as soon as the user logs in,
  not just when something else asks for it.
* ``ThrottleInterval: 30`` — backoff for crash loops. Without this a
  daemon that dies-on-boot due to a config error gets respawned in a
  CPU-pinning tight loop. Mirrors the in-process throttle in
  :mod:`pollypm.rail_daemon_supervisor` (60s) but lets launchd come
  back faster than the in-process supervisor for legitimate restarts.
* ``StandardOutPath`` / ``StandardErrorPath`` → ``~/.pollypm/rail_daemon.log``
  so output lands in the same file ``cli._spawn_rail_daemon`` writes to.
  Operators don't have to learn a second log location.

Tests
-----
``install_launchd_keepalive`` and ``uninstall_launchd_keepalive`` both
take injectable ``launchctl_runner`` + ``plist_dir`` so unit tests can
stub the OS-level call without writing to the user's real
``~/Library/LaunchAgents/`` directory.
"""

from __future__ import annotations

import logging
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


__all__ = [
    "DEFAULT_LABEL",
    "DEFAULT_THROTTLE_INTERVAL_SECONDS",
    "build_plist_dict",
    "install_launchd_keepalive",
    "plist_path_for_label",
    "uninstall_launchd_keepalive",
]


DEFAULT_LABEL = "com.pollypm.rail-daemon"
DEFAULT_THROTTLE_INTERVAL_SECONDS = 30


# ---------------------------------------------------------------------------
# Plist construction
# ---------------------------------------------------------------------------


def plist_path_for_label(label: str, *, plist_dir: Path | None = None) -> Path:
    """Resolve the on-disk plist location for a given LaunchAgent label.

    Defaults to ``~/Library/LaunchAgents/<label>.plist``. ``plist_dir``
    is injectable so tests don't have to write to the real LaunchAgents
    directory.
    """
    base = plist_dir or (Path.home() / "Library" / "LaunchAgents")
    return base / f"{label}.plist"


def build_plist_dict(
    *,
    label: str = DEFAULT_LABEL,
    config_path: Path,
    python_executable: Path | str | None = None,
    log_path: Path | None = None,
    throttle_interval: int = DEFAULT_THROTTLE_INTERVAL_SECONDS,
) -> dict:
    """Build the dict that ``plistlib.dumps`` will serialize.

    Public so tests can assert the shape directly without round-tripping
    through plistlib (which is a separate concern — we're testing what
    we'd ask launchd to do, not whether plistlib can serialize a dict).
    """
    python = str(python_executable or sys.executable)
    log = str(log_path or (Path.home() / ".pollypm" / "rail_daemon.log"))
    program_args = [
        python, "-m", "pollypm.rail_daemon",
        "--config", str(config_path),
    ]
    return {
        "Label": label,
        "ProgramArguments": program_args,
        # KeepAlive=true → restart on any exit. The rail-daemon's own
        # PID-file claim lock guards against doubled instances.
        "KeepAlive": True,
        # Boot at user login, not just when another agent requests it.
        "RunAtLoad": True,
        # Backoff on crash loops; respects launchd's documented minimum
        # of 10s. We pick 30s to match the supervisor module's mental
        # model (process-level liveness checks every 60s; if launchd
        # restarts twice that fast, the supervisor's throttle prevents
        # double-spawning).
        "ThrottleInterval": int(throttle_interval),
        "StandardOutPath": log,
        "StandardErrorPath": log,
        # Set the working directory to the user's home so any relative
        # path the daemon ever uses resolves against a writable dir.
        "WorkingDirectory": str(Path.home()),
        # launchd processes don't inherit the user's shell PATH; the
        # rail daemon shells out to ``ps`` and ``tmux`` indirectly, so
        # we set a sane default. ``cli._spawn_rail_daemon`` already
        # works without this because it inherits the parent shell's
        # PATH; in launchd land we have to set it ourselves.
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(Path.home()),
        },
    }


# ---------------------------------------------------------------------------
# Install / uninstall
# ---------------------------------------------------------------------------


# Type alias for an injectable launchctl runner — tests pass a fake.
LaunchctlRunner = Callable[[list[str]], subprocess.CompletedProcess]


def _default_launchctl_runner(argv: list[str]) -> subprocess.CompletedProcess:
    """Run ``launchctl`` and return the completed process.

    ``check=False`` so callers can inspect ``returncode`` for the
    permissive "already loaded / not loaded" cases without us raising.
    """
    return subprocess.run(
        argv, capture_output=True, text=True, check=False, timeout=10.0,
    )


def install_launchd_keepalive(
    *,
    config_path: Path,
    label: str = DEFAULT_LABEL,
    plist_dir: Path | None = None,
    python_executable: Path | str | None = None,
    log_path: Path | None = None,
    throttle_interval: int = DEFAULT_THROTTLE_INTERVAL_SECONDS,
    load: bool = True,
    launchctl_runner: LaunchctlRunner | None = None,
) -> Path:
    """Write the plist and (optionally) bootstrap it via launchctl.

    Returns the path to the written plist.

    Failures during ``launchctl bootstrap`` are logged but don't raise
    — the file is still on disk and the user can load it manually with
    the printed command. We never raise on the launchctl call because
    "already bootstrapped" is a normal case (re-installing) and the
    exit code mirrors that.

    Args:
        config_path: forwarded as ``--config`` to ``pollypm.rail_daemon``.
        label: LaunchAgent ``Label``. Reverse-DNS conventional.
        plist_dir: where to write the plist (default
            ``~/Library/LaunchAgents``). Injectable for tests.
        python_executable: which Python to invoke. Defaults to
            :attr:`sys.executable` (the interpreter running ``pm``),
            which respects whatever virtualenv the user installed
            PollyPM into.
        log_path: where launchd should redirect stdout/stderr.
            Defaults to ``~/.pollypm/rail_daemon.log`` so it lines up
            with ``cli._spawn_rail_daemon``'s log path.
        throttle_interval: launchd backoff between respawns.
        load: when True, call ``launchctl bootstrap gui/<uid>`` so the
            agent starts immediately. When False, the user has to run
            ``launchctl bootstrap`` themselves — useful for scripted
            installs that want to defer activation.
        launchctl_runner: override the launchctl invocation. Tests
            pass a fake to avoid mutating the user's real launchd state.
    """
    plist_path = plist_path_for_label(label, plist_dir=plist_dir)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_dict = build_plist_dict(
        label=label,
        config_path=config_path,
        python_executable=python_executable,
        log_path=log_path,
        throttle_interval=throttle_interval,
    )
    plist_bytes = plistlib.dumps(plist_dict, fmt=plistlib.FMT_XML)
    plist_path.write_bytes(plist_bytes)

    if load:
        runner = launchctl_runner or _default_launchctl_runner
        if shutil.which("launchctl") is not None or launchctl_runner is not None:
            uid = os.getuid()
            domain = f"gui/{uid}"
            # If a previous version is loaded, bootstrap will fail with
            # "service already loaded". Bootout first, ignore errors.
            runner(["launchctl", "bootout", domain, str(plist_path)])
            result = runner(["launchctl", "bootstrap", domain, str(plist_path)])
            if result.returncode != 0:
                logger.warning(
                    "rail_daemon_launchd: bootstrap returncode=%d stderr=%s",
                    result.returncode, (result.stderr or "").strip(),
                )

    return plist_path


def uninstall_launchd_keepalive(
    *,
    label: str = DEFAULT_LABEL,
    plist_dir: Path | None = None,
    launchctl_runner: LaunchctlRunner | None = None,
) -> bool:
    """Bootout and unlink the plist for ``label``.

    Returns True iff the plist file existed (and was therefore removed).
    Bootout failures are logged and ignored — the goal is to leave the
    user with no plist file. If the file was never installed the call
    is a quiet no-op.
    """
    plist_path = plist_path_for_label(label, plist_dir=plist_dir)
    if not plist_path.exists():
        return False

    runner = launchctl_runner or _default_launchctl_runner
    if shutil.which("launchctl") is not None or launchctl_runner is not None:
        uid = os.getuid()
        domain = f"gui/{uid}"
        result = runner(["launchctl", "bootout", domain, str(plist_path)])
        if result.returncode != 0:
            # Most common case: the agent wasn't loaded. Log at debug.
            logger.debug(
                "rail_daemon_launchd: bootout returncode=%d stderr=%s",
                result.returncode, (result.stderr or "").strip(),
            )

    plist_path.unlink(missing_ok=True)
    return True
