from __future__ import annotations

import logging
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TmuxWindow:
    session: str
    index: int
    name: str
    active: bool
    pane_id: str
    pane_current_command: str
    pane_current_path: str
    pane_dead: bool


@dataclass(slots=True)
class TmuxPane:
    session: str
    window_index: int
    window_name: str
    pane_index: int
    pane_id: str
    active: bool
    pane_current_command: str
    pane_current_path: str
    pane_dead: bool
    pane_left: int
    pane_width: int


import re as _re

# Safe characters for tmux session/window names
_NAME_RE = _re.compile(r"^[a-zA-Z0-9_.-]+$")
# Safe characters for tmux targets (session:window.pane or %pane_id)
_TARGET_RE = _re.compile(r"^[a-zA-Z0-9_:.%-]+$")


class DeadPaneError(RuntimeError):
    """Raised when attempting to send keys to a dead pane."""


class TmuxClient:
    @staticmethod
    def _validate_name(name: str, context: str = "name") -> None:
        """Validate a session or window name contains only safe characters."""
        if not name or not _NAME_RE.match(name):
            raise ValueError(f"Invalid tmux {context}: {name!r} (only [a-zA-Z0-9_.-] allowed)")

    def _exact_target(self, target: str) -> str:
        """Prefix bare session names with ``=`` for exact matching.

        tmux 3.6+ does not support ``=session:window`` — the ``=`` prefix
        only works for bare session names.  When the target contains a colon,
        skip the prefix to avoid a lookup failure.
        """
        if not target:
            raise ValueError("tmux target cannot be empty")
        if target.startswith(("=", "%", "@")):
            return target
        if not _TARGET_RE.match(target):
            raise ValueError(f"Invalid tmux target: {target!r}")
        if ":" in target:
            return target
        return f"={target}"

    def _inside_tmux(self) -> bool:
        return bool(os.environ.get("TMUX"))

    _DEFAULT_TIMEOUT = 15  # seconds — prevents indefinite hangs if tmux is unresponsive

    def run(self, *args: str, check: bool = True, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["tmux", *args],
            check=check,
            text=True,
            capture_output=True,
            timeout=timeout or self._DEFAULT_TIMEOUT,
        )

    def has_session(self, name: str) -> bool:
        result = self.run("has-session", "-t", self._exact_target(name), check=False)
        return result.returncode == 0

    def current_session_name(self) -> str | None:
        if not self._inside_tmux():
            return None
        result = self.run("display-message", "-p", "#S", check=False)
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def current_window_index(self) -> str | None:
        if not self._inside_tmux():
            return None
        result = self.run("display-message", "-p", "#I", check=False)
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def current_pane_id(self) -> str | None:
        if not self._inside_tmux():
            return None
        result = self.run("display-message", "-p", "#{pane_id}", check=False)
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def create_session(self, name: str, window_name: str, command: str, *, remain_on_exit: bool = True, history_limit: int | None = 500) -> None:
        """Create a tmux session. No-op if session already exists."""
        self._validate_name(name, "session name")
        self._validate_name(window_name, "window name")
        if self.has_session(name):
            logger.debug("Session %r already exists, skipping create", name)
            return
        self.run("new-session", "-d", "-s", name, "-n", window_name, command)
        self.run("set-option", "-t", self._exact_target(f"{name}:"), "remain-on-exit", "on" if remain_on_exit else "off")
        if history_limit is not None:
            self.run("set-option", "-t", self._exact_target(f"{name}:"), "history-limit", str(history_limit))

    def new_session_attached(self, name: str, window_name: str, command: str) -> int:
        result = subprocess.run(
            ["tmux", "new-session", "-A", "-s", name, "-n", window_name, command],
            check=False,
        )
        return result.returncode

    def create_window(self, name: str, window_name: str, command: str, *, detached: bool = False) -> None:
        """Create a window in a session. No-op if window already exists."""
        self._validate_name(window_name, "window name")
        # Check if window already exists in this session
        if self.has_session(name):
            try:
                windows = self.list_windows(name)
                if any(w.name == window_name for w in windows):
                    logger.debug("Window %r already exists in session %r, skipping create", window_name, name)
                    return
            except subprocess.CalledProcessError:
                pass  # Session may have vanished; proceed with creation attempt
        args = ["new-window", "-t", self._exact_target(name), "-n", window_name]
        if detached:
            args.append("-d")
        args.append(command)
        self.run(*args)

    def split_window(
        self,
        target: str,
        command: str,
        *,
        horizontal: bool = True,
        detached: bool = True,
        percent: int | None = None,
        size: int | None = None,
    ) -> str:
        args = ["split-window", "-P", "-F", "#{pane_id}", "-t", self._exact_target(target)]
        args.append("-h" if horizontal else "-v")
        if detached:
            args.append("-d")
        if size is not None:
            args.extend(["-l", str(size)])
        elif percent is not None:
            args.extend(["-p", str(percent)])
        args.append(command)
        result = self.run(*args)
        return result.stdout.strip()

    def select_window(self, target: str) -> None:
        self.run("select-window", "-t", self._exact_target(target))

    def select_pane(self, target: str) -> None:
        self.run("select-pane", "-t", self._exact_target(target))

    def kill_window(self, target: str) -> None:
        """Kill a window. No-op if it doesn't exist."""
        result = self.run("kill-window", "-t", self._exact_target(target), check=False)
        if result.returncode != 0:
            logger.debug("kill-window %r returned %d (likely already gone)", target, result.returncode)

    def kill_session(self, name: str) -> None:
        """Kill a session. No-op if it doesn't exist."""
        if not self.has_session(name):
            logger.debug("Session %r does not exist, skipping kill", name)
            return
        result = self.run("kill-session", "-t", self._exact_target(name), check=False)
        if result.returncode != 0:
            logger.debug("kill-session %r returned %d (likely already gone)", name, result.returncode)

    def kill_pane(self, target: str) -> None:
        self.run("kill-pane", "-t", self._exact_target(target))

    def resize_pane_width(self, target: str, width: int) -> None:
        self.run("resize-pane", "-t", self._exact_target(target), "-x", str(width))

    def clear_history(self, target: str) -> None:
        self.run("clear-history", "-t", self._exact_target(target), check=False)

    def set_pane_history_limit(self, target: str, limit: int) -> None:
        self.run("set-option", "-p", "-t", self._exact_target(target), "history-limit", str(limit), check=False)

    def break_pane(self, source: str, target_session: str, window_name: str) -> None:
        self.run(
            "break-pane",
            "-d",
            "-s",
            self._exact_target(source),
            "-t",
            self._exact_target(target_session),
            "-n",
            window_name,
        )

    def join_pane(self, source: str, target: str, *, horizontal: bool = True) -> None:
        args = ["join-pane", "-d", "-s", self._exact_target(source), "-t", self._exact_target(target)]
        args.append("-h" if horizontal else "-v")
        self.run(*args)

    def swap_pane(self, source: str, target: str) -> None:
        self.run("swap-pane", "-d", "-s", self._exact_target(source), "-t", self._exact_target(target))

    def pipe_pane(self, target: str, log_path: Path) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.run("pipe-pane", "-o", "-t", self._exact_target(target), f"cat >> {shlex.quote(str(log_path))}")

    def list_windows(self, name: str) -> list[TmuxWindow]:
        fmt = (
            "#{session_name}\t#{window_index}\t#{window_name}\t#{window_active}\t#{pane_id}\t"
            "#{pane_current_command}\t#{pane_current_path}\t#{pane_dead}"
        )
        result = self.run("list-windows", "-t", self._exact_target(name), "-F", fmt)
        windows: list[TmuxWindow] = []
        for line in result.stdout.splitlines():
            parts = line.split("\t", 7)
            if len(parts) < 8:
                continue
            (
                session,
                index,
                window_name,
                active,
                pane_id,
                pane_current_command,
                pane_current_path,
                pane_dead,
            ) = parts
            windows.append(
                TmuxWindow(
                    session=session,
                    index=int(index),
                    name=window_name,
                    active=active == "1",
                    pane_id=pane_id,
                    pane_current_command=pane_current_command,
                    pane_current_path=pane_current_path,
                    pane_dead=pane_dead == "1",
                )
            )
        return windows

    def list_all_windows(self) -> list[TmuxWindow]:
        """List windows across ALL tmux sessions in a single subprocess call."""
        fmt = (
            "#{session_name}\t#{window_index}\t#{window_name}\t#{window_active}\t#{pane_id}\t"
            "#{pane_current_command}\t#{pane_current_path}\t#{pane_dead}"
        )
        result = self.run("list-windows", "-a", "-F", fmt, check=False)
        if result.returncode != 0:
            return []
        windows: list[TmuxWindow] = []
        for line in result.stdout.splitlines():
            parts = line.split("\t", 7)
            if len(parts) < 8:
                continue
            session, index, window_name, active, pane_id, pane_cmd, pane_path, pane_dead = parts
            windows.append(
                TmuxWindow(
                    session=session,
                    index=int(index),
                    name=window_name,
                    active=active == "1",
                    pane_id=pane_id,
                    pane_current_command=pane_cmd,
                    pane_current_path=pane_path,
                    pane_dead=pane_dead == "1",
                )
            )
        return windows

    def list_panes(self, target: str) -> list[TmuxPane]:
        fmt = (
            "#{session_name}\t#{window_index}\t#{window_name}\t#{pane_index}\t#{pane_id}\t#{pane_active}\t"
            "#{pane_current_command}\t#{pane_current_path}\t#{pane_dead}\t#{pane_left}\t#{pane_width}"
        )
        result = self.run("list-panes", "-t", self._exact_target(target), "-F", fmt)
        panes: list[TmuxPane] = []
        for line in result.stdout.splitlines():
            parts = line.split("\t", 10)
            if len(parts) < 11:
                continue
            (
                session,
                window_index,
                window_name,
                pane_index,
                pane_id,
                active,
                pane_current_command,
                pane_current_path,
                pane_dead,
                pane_left,
                pane_width,
            ) = parts
            panes.append(
                TmuxPane(
                    session=session,
                    window_index=int(window_index),
                    window_name=window_name,
                    pane_index=int(pane_index),
                    pane_id=pane_id,
                    active=active == "1",
                    pane_current_command=pane_current_command,
                    pane_current_path=pane_current_path,
                    pane_dead=pane_dead == "1",
                    pane_left=int(pane_left),
                    pane_width=int(pane_width),
                )
            )
        return panes

    def rename_window(self, target: str, new_name: str) -> None:
        self.run("rename-window", "-t", self._exact_target(target), new_name)

    def respawn_pane(self, target: str, command: str) -> None:
        self.run("respawn-pane", "-k", "-t", self._exact_target(target), command)

    def set_option(self, target: str, option: str, value: str) -> None:
        normalized = target if ":" in target or target.startswith(("=", "%", "@")) else f"{target}:"
        self.run("set-option", "-t", self._exact_target(normalized), option, value)

    def set_window_option(self, target: str, option: str, value: str) -> None:
        self.run("set-window-option", "-t", self._exact_target(target), option, value)

    def link_window(self, source: str, target_session: str) -> None:
        self.run("link-window", "-dk", "-s", self._exact_target(source), "-t", self._exact_target(target_session))

    def capture_pane(self, target: str, lines: int = 200) -> str:
        result = self.run("capture-pane", "-p", "-S", f"-{lines}", "-t", self._exact_target(target))
        return result.stdout

    def send_keys(self, target: str, text: str, press_enter: bool = True) -> None:
        """Send keys to a pane. Raises DeadPaneError if the target pane is dead."""
        resolved = self._exact_target(target)
        # If the target looks like a pane id, check liveness first
        if target.startswith("%"):
            if not self.is_pane_alive(target):
                raise DeadPaneError(f"Cannot send keys to dead pane {target!r}")
        self.run("send-keys", "-l", "-t", resolved, text)
        if press_enter:
            self.run("send-keys", "-t", resolved, "Enter")

    def attach_session(self, name: str) -> int:
        result = subprocess.run(["tmux", "attach", "-t", self._exact_target(name)], check=False)
        return result.returncode

    def switch_client(self, name: str) -> int:
        result = subprocess.run(["tmux", "switch-client", "-t", self._exact_target(name)], check=False)
        return result.returncode

    # -- Health check helpers --------------------------------------------------

    def is_session_alive(self, name: str) -> bool:
        """Return True if the session exists and has at least one live (non-dead) pane."""
        if not self.has_session(name):
            return False
        try:
            windows = self.list_windows(name)
        except subprocess.CalledProcessError:
            return False
        return any(not w.pane_dead for w in windows)

    def is_pane_alive(self, pane_id: str) -> bool:
        """Return True if the pane exists and is not dead."""
        fmt = "#{pane_id}\t#{pane_dead}\t#{pane_pid}"
        result = self.run("list-panes", "-a", "-F", fmt, check=False)
        if result.returncode != 0:
            return False
        for line in result.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) >= 2 and parts[0] == pane_id:
                return parts[1] != "1"
        return False

    def get_pane_pid(self, pane_id: str) -> int | None:
        """Get the PID of the process running in a pane, or None if pane not found."""
        fmt = "#{pane_id}\t#{pane_pid}"
        result = self.run("list-panes", "-a", "-F", fmt, check=False)
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2 and parts[0] == pane_id:
                try:
                    return int(parts[1])
                except ValueError:
                    return None
        return None

    # -- Teardown helper -------------------------------------------------------

    def teardown_session(self, name: str, capture_lines: int = 200) -> dict[str, str]:
        """Tear down a session, capturing final pane output for archival.

        Returns a dict mapping pane_id -> captured output.
        No-op (returns empty dict) if the session doesn't exist.
        """
        if not self.has_session(name):
            return {}

        captured: dict[str, str] = {}
        try:
            windows = self.list_windows(name)
            for w in windows:
                try:
                    output = self.capture_pane(w.pane_id, lines=capture_lines)
                    captured[w.pane_id] = output
                except subprocess.CalledProcessError:
                    logger.debug("Failed to capture pane %s during teardown", w.pane_id)
        except subprocess.CalledProcessError:
            logger.debug("Failed to list windows for session %r during teardown", name)

        self.kill_session(name)
        return captured


def recover_session(
    tmux_client: TmuxClient,
    name: str,
    command: str,
    working_dir: str | None = None,
    *,
    window_name: str = "main",
    **kwargs: object,
) -> bool:
    """Restart a dead session with the same parameters.

    Returns True if recovery was needed and performed, False if session was
    already alive.
    """
    if tmux_client.is_session_alive(name):
        return False

    # Session is dead or doesn't exist; clean up if it lingers
    tmux_client.kill_session(name)

    # Build command with working directory if specified
    full_command = command
    if working_dir:
        full_command = f"cd {shlex.quote(working_dir)} && {command}"

    tmux_client.create_session(name, window_name, full_command, **kwargs)  # type: ignore[arg-type]
    logger.info("Recovered session %r", name)
    return True
