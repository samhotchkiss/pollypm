"""macOS launchd supervision helpers for ``pm serve``.

The CLI exposes these as ``pm serve install/start/stop/uninstall``.
This module keeps the macOS-specific launchctl calls out of the Web API
startup path and gives tests injection points for plist directories,
binary paths, and subprocess runners.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from xml.sax.saxutils import escape

from pollypm.audit.log import EVENT_DAEMON_SERVE_RESPAWN

logger = logging.getLogger(__name__)

DEFAULT_LABEL = "com.pollypm.serve"
DEFAULT_THROTTLE_INTERVAL_SECONDS = 10
QUIESCED_MARKER_NAME = ".serve-quiesced"
SERVE_PID_FILENAME = "serve.pid"
TEMPLATE_FILENAME = "com.pollypm.serve.plist.template"
DEFAULT_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{{LABEL}}</string>

  <key>ProgramArguments</key>
  <array>
    <string>{{PM_BINARY}}</string>
    <string>serve</string>
  </array>

  <key>KeepAlive</key>
  <true/>

  <key>RunAtLoad</key>
  <true/>

  <key>ThrottleInterval</key>
  <integer>10</integer>

  <key>StandardOutPath</key>
  <string>{{HOME}}/.pollypm/logs/serve.log</string>

  <key>StandardErrorPath</key>
  <string>{{HOME}}/.pollypm/logs/serve.log</string>

  <key>WorkingDirectory</key>
  <string>{{HOME}}</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>{{HOME}}</string>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
</dict>
</plist>
"""

LaunchctlRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]
AuditEmitter = Callable[..., None]


@dataclass(frozen=True)
class LaunchdActionResult:
    """Result of a plist action and its optional launchctl invocation."""

    plist_path: Path
    launchctl_result: subprocess.CompletedProcess[str] | None = None
    removed: bool = False


def pollypm_home(*, home: Path | None = None) -> Path:
    """Return the user-level PollyPM home for launchd-managed serve state."""

    return (home or Path.home()) / ".pollypm"


def logs_dir(*, home: Path | None = None) -> Path:
    return pollypm_home(home=home) / "logs"


def quiesced_marker_path(*, home: Path | None = None) -> Path:
    return pollypm_home(home=home) / QUIESCED_MARKER_NAME


def serve_pid_path(base_dir: Path) -> Path:
    return Path(base_dir) / SERVE_PID_FILENAME


def plist_path_for_label(
    label: str = DEFAULT_LABEL,
    *,
    plist_dir: Path | None = None,
    home: Path | None = None,
) -> Path:
    """Resolve ``~/Library/LaunchAgents/<label>.plist``.

    ``plist_dir`` and ``home`` are injectable so tests never need to
    touch the user's real LaunchAgents directory.
    """

    base = plist_dir or ((home or Path.home()) / "Library" / "LaunchAgents")
    return base / f"{label}.plist"


def default_template_path() -> Path:
    """Return the source-tree plist template path."""

    return Path(__file__).resolve().parents[2] / "scripts" / TEMPLATE_FILENAME


def resolve_pm_binary(pm_binary: Path | str | None = None) -> Path:
    """Resolve the absolute ``pm`` executable path for launchd.

    launchd does not run through the user's interactive shell, so the
    plist must contain an absolute executable path. Prefer the explicit
    test/CLI override, then the current PATH, then ``sys.argv[0]`` if it
    names a resolvable command.
    """

    if pm_binary is not None:
        return Path(pm_binary).expanduser().resolve()

    found = shutil.which("pm")
    if found:
        return Path(found).resolve()

    argv0 = Path(sys.argv[0]).expanduser()
    if argv0.is_absolute() or len(argv0.parts) > 1:
        return argv0.resolve()

    found_argv0 = shutil.which(sys.argv[0])
    if found_argv0:
        return Path(found_argv0).resolve()

    raise RuntimeError(
        "Could not resolve the pm executable path. Ensure `pm` is on PATH "
        "or pass an explicit pm_binary."
    )


def render_plist(
    *,
    home: Path | None = None,
    label: str = DEFAULT_LABEL,
    pm_binary: Path | str | None = None,
    template_path: Path | None = None,
) -> str:
    """Render the launchd plist template with HOME and pm binary path."""

    resolved_home = (home or Path.home()).expanduser().resolve()
    resolved_binary = resolve_pm_binary(pm_binary)
    resolved_template_path = template_path or default_template_path()
    try:
        template = resolved_template_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        template = DEFAULT_PLIST_TEMPLATE
    return (
        template.replace("{{LABEL}}", escape(label))
        .replace("{{HOME}}", escape(str(resolved_home)))
        .replace("{{PM_BINARY}}", escape(str(resolved_binary)))
    )


def write_plist(
    *,
    home: Path | None = None,
    label: str = DEFAULT_LABEL,
    plist_dir: Path | None = None,
    pm_binary: Path | str | None = None,
    template_path: Path | None = None,
) -> Path:
    """Write the rendered LaunchAgent plist and ensure its log dir exists."""

    logs_dir(home=home).mkdir(parents=True, exist_ok=True)
    plist_path = plist_path_for_label(label, plist_dir=plist_dir, home=home)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(
        render_plist(
            home=home,
            label=label,
            pm_binary=pm_binary,
            template_path=template_path,
        ),
        encoding="utf-8",
    )
    return plist_path


def _default_launchctl_runner(
    argv: list[str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=False,
        timeout=10.0,
    )


def _run_launchctl(
    action: str,
    plist_path: Path,
    *,
    launchctl_runner: LaunchctlRunner | None = None,
) -> subprocess.CompletedProcess[str]:
    runner = launchctl_runner or _default_launchctl_runner
    return runner(["launchctl", action, str(plist_path)])


def load_launch_agent(
    plist_path: Path,
    *,
    launchctl_runner: LaunchctlRunner | None = None,
) -> subprocess.CompletedProcess[str]:
    return _run_launchctl("load", plist_path, launchctl_runner=launchctl_runner)


def unload_launch_agent(
    plist_path: Path,
    *,
    launchctl_runner: LaunchctlRunner | None = None,
) -> subprocess.CompletedProcess[str]:
    return _run_launchctl("unload", plist_path, launchctl_runner=launchctl_runner)


def install_launch_agent(
    *,
    home: Path | None = None,
    label: str = DEFAULT_LABEL,
    plist_dir: Path | None = None,
    pm_binary: Path | str | None = None,
    template_path: Path | None = None,
    launchctl_runner: LaunchctlRunner | None = None,
) -> LaunchdActionResult:
    """Render the plist to LaunchAgents and load it via launchctl."""

    plist_path = write_plist(
        home=home,
        label=label,
        plist_dir=plist_dir,
        pm_binary=pm_binary,
        template_path=template_path,
    )
    result = load_launch_agent(plist_path, launchctl_runner=launchctl_runner)
    return LaunchdActionResult(plist_path=plist_path, launchctl_result=result)


def uninstall_launch_agent(
    *,
    home: Path | None = None,
    label: str = DEFAULT_LABEL,
    plist_dir: Path | None = None,
    launchctl_runner: LaunchctlRunner | None = None,
) -> LaunchdActionResult:
    """Unload and remove the serve LaunchAgent plist if present."""

    plist_path = plist_path_for_label(label, plist_dir=plist_dir, home=home)
    result: subprocess.CompletedProcess[str] | None = None
    if plist_path.exists():
        result = unload_launch_agent(plist_path, launchctl_runner=launchctl_runner)
        plist_path.unlink(missing_ok=True)
        return LaunchdActionResult(
            plist_path=plist_path,
            launchctl_result=result,
            removed=True,
        )
    return LaunchdActionResult(plist_path=plist_path, launchctl_result=None)


def start_launch_agent(
    *,
    home: Path | None = None,
    label: str = DEFAULT_LABEL,
    plist_dir: Path | None = None,
    pm_binary: Path | str | None = None,
    template_path: Path | None = None,
    launchctl_runner: LaunchctlRunner | None = None,
) -> LaunchdActionResult:
    """Clear the quiesced marker, render the plist, and load launchd."""

    marker = quiesced_marker_path(home=home)
    marker.unlink(missing_ok=True)
    return install_launch_agent(
        home=home,
        label=label,
        plist_dir=plist_dir,
        pm_binary=pm_binary,
        template_path=template_path,
        launchctl_runner=launchctl_runner,
    )


def stop_launch_agent(
    *,
    home: Path | None = None,
    label: str = DEFAULT_LABEL,
    plist_dir: Path | None = None,
    launchctl_runner: LaunchctlRunner | None = None,
) -> LaunchdActionResult:
    """Write the quiesced marker and unload the LaunchAgent."""

    marker = quiesced_marker_path(home=home)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("quiesced\n", encoding="utf-8")
    plist_path = plist_path_for_label(label, plist_dir=plist_dir, home=home)
    result = unload_launch_agent(plist_path, launchctl_runner=launchctl_runner)
    return LaunchdActionResult(plist_path=plist_path, launchctl_result=result)


def _read_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        pid = int(raw)
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def _default_audit_emit(**kwargs: Any) -> None:
    from pollypm.audit.log import emit

    emit(**kwargs)


def record_serve_startup(
    *,
    base_dir: Path,
    current_pid: int | None = None,
    audit_emit: AuditEmitter | None = None,
) -> None:
    """Record the current ``pm serve`` PID and audit a PID change.

    The audit event is emitted when a prior PID file exists, parses as a
    positive integer, and differs from this process. The write is
    best-effort so serve startup does not fail because a diagnostic PID
    file or audit tail is unavailable.
    """

    pid = int(current_pid or os.getpid())
    path = serve_pid_path(base_dir)
    previous_pid = _read_pid(path) if path.exists() else None
    if previous_pid is not None and previous_pid != pid:
        try:
            emitter = audit_emit or _default_audit_emit
            emitter(
                event=EVENT_DAEMON_SERVE_RESPAWN,
                project="_workspace",
                subject=f"serve/{pid}",
                actor="system",
                status="warn",
                metadata={
                    "role": "serve",
                    "previous_pid": previous_pid,
                    "pid": pid,
                    "pid_file": str(path),
                },
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "daemon.serve.respawn audit emit failed "
                "(previous_pid=%s, pid=%s)",
                previous_pid,
                pid,
                exc_info=True,
            )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{pid}\n", encoding="utf-8")
    except OSError:
        logger.warning("pm serve: failed to write PID file %s", path, exc_info=True)


__all__ = [
    "DEFAULT_LABEL",
    "DEFAULT_THROTTLE_INTERVAL_SECONDS",
    "EVENT_DAEMON_SERVE_RESPAWN",
    "LaunchdActionResult",
    "install_launch_agent",
    "load_launch_agent",
    "logs_dir",
    "plist_path_for_label",
    "pollypm_home",
    "quiesced_marker_path",
    "record_serve_startup",
    "render_plist",
    "resolve_pm_binary",
    "serve_pid_path",
    "start_launch_agent",
    "stop_launch_agent",
    "uninstall_launch_agent",
    "unload_launch_agent",
    "write_plist",
]
