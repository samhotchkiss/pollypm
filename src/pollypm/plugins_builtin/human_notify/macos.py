"""macOS Notification Center adapter via ``osascript``.

Default-on for Darwin hosts. No config required — the binary is
part of the base OS. Notifications land in the notification center
and show as a banner regardless of whether the cockpit is open or
focused, which is the whole point.

Implementation note: we shell out to ``osascript`` rather than
linking against ``pyobjc`` to keep PollyPM's dependency surface
stdlib-only. ``osascript`` is synchronous and returns immediately
once the notification is queued with the daemon, so the 2-second
timeout below is defensive — a hung ``osascript`` call would be a
genuine system problem, not a flake.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys

logger = logging.getLogger(__name__)


class MacOsNotifyAdapter:
    """``HumanNotifyAdapter`` — macOS Notification Center via ``osascript``."""

    name = "macos"

    def is_available(self) -> bool:
        """True iff running on Darwin and ``osascript`` resolves on PATH.

        On non-macOS hosts this short-circuits to False so Linux /
        Windows installs don't pay for an always-failing adapter.
        """
        if sys.platform != "darwin":
            return False
        return shutil.which("osascript") is not None

    def notify(
        self,
        *,
        title: str,
        body: str,
        task_id: str,
        project: str,
    ) -> None:
        """Display a notification with ``title`` + ``body``.

        Uses AppleScript's ``display notification`` — the notification
        shows as a banner + lands in Notification Center. The OS
        handles focus / DND / clearing; we don't track delivery state.

        ``task_id`` / ``project`` are appended to the body when the
        caller-provided body doesn't already reference them, so the
        user can act on the notification without opening the cockpit.
        """
        del task_id, project  # caller includes them in ``body`` already
        script = (
            'display notification "' + _escape(body) + '" '
            'with title "' + _escape(title) + '" '
            'sound name "Glass"'
        )
        try:
            subprocess.run(
                ["osascript", "-e", script],
                check=False,
                timeout=2.0,
                capture_output=True,
            )
        except subprocess.TimeoutExpired:
            logger.warning("human_notify[macos]: osascript timed out")
        except Exception as exc:  # noqa: BLE001
            logger.warning("human_notify[macos]: osascript failed: %s", exc)


def _escape(raw: str) -> str:
    """Escape double quotes + backslashes for AppleScript string literals.

    AppleScript strings use ``"``-delimited double quotes and accept
    ``\\"`` / ``\\\\`` escapes. We leave everything else verbatim —
    control characters are fine in notification bodies, and newlines
    are rendered as paragraph breaks in the banner.
    """
    return raw.replace("\\", "\\\\").replace('"', '\\"')


__all__ = ["MacOsNotifyAdapter"]
