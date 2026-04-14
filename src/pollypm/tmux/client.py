from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path


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
        self._validate_name(name, "session name")
        self._validate_name(window_name, "window name")
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
        self._validate_name(window_name, "window name")
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
        self.run("kill-window", "-t", self._exact_target(target))

    def kill_session(self, name: str) -> None:
        self.run("kill-session", "-t", self._exact_target(name))

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
        self.run("send-keys", "-l", "-t", self._exact_target(target), text)
        if press_enter:
            self.run("send-keys", "-t", self._exact_target(target), "Enter")

    def attach_session(self, name: str) -> int:
        result = subprocess.run(["tmux", "attach", "-t", self._exact_target(name)], check=False)
        return result.returncode

    def switch_client(self, name: str) -> int:
        result = subprocess.run(["tmux", "switch-client", "-t", self._exact_target(name)], check=False)
        return result.returncode
