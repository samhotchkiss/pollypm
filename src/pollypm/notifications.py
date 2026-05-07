"""Send notifications to the user outside the terminal.

Supports macOS native notifications and terminal bell.
"""
from __future__ import annotations

import subprocess
import logging

logger = logging.getLogger(__name__)


def send_notification(title: str, body: str, *, method: str = "macos") -> bool:
    """Send a notification. Returns True if successful."""
    if method == "macos":
        return _macos_notification(title, body)
    if method == "bell":
        return _terminal_bell()
    if method == "none":
        return True
    logger.warning("Unknown notification method: %s", method)
    return False


def _escape_applescript(raw: str) -> str:
    """Escape a Python string for embedding in an AppleScript string literal.

    Order matters: replace backslashes *before* double quotes, otherwise the
    backslashes we introduce while escaping ``"`` would be doubled again.
    Skipping the backslash escape (the pre-#1378 behaviour) lets a caller
    smuggle in ``\\"``, which AppleScript parses as ``\\`` followed by a
    string-terminating ``"`` — opening a path to arbitrary AppleScript /
    ``do shell script`` execution.
    """
    return raw.replace("\\", "\\\\").replace('"', '\\"')


def _macos_notification(title: str, body: str) -> bool:
    """Send a macOS notification via osascript."""
    title_escaped = _escape_applescript(title)
    body_escaped = _escape_applescript(body)
    script = f'display notification "{body_escaped}" with title "{title_escaped}"'
    try:
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, timeout=5,
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _terminal_bell() -> bool:
    """Send a terminal bell character."""
    print("\a", end="", flush=True)
    return True
