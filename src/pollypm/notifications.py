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


def _macos_notification(title: str, body: str) -> bool:
    """Send a macOS notification via osascript."""
    # Escape for AppleScript
    title_escaped = title.replace('"', '\\"')
    body_escaped = body.replace('"', '\\"')
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
