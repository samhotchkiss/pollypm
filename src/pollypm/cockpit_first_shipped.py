"""First-shipped celebration modal (extracted from ``cockpit_ui``).

Contract:
- Inputs: the host ``App`` instance (for ``_celebrate_first_shipped``)
  used only to ``notify`` and ``push_screen`` the modal.
- Outputs: a short-lived ``ModalScreen`` that animates a confetti frame
  and auto-dismisses after ~2 seconds.
- Side effects: emits a single ``notify`` toast and optionally pushes
  the modal; honours ``POLLY_NO_CONFETTI=1`` to suppress the modal
  while keeping the toast (used by smoke / no-TTY environments).
- Invariants: this module owns one widget class and one helper only;
  shared cockpit state stays in its original homes.
- Allowed dependencies: Textual primitives, ``os`` for the env switch.
- Private: ``_FirstShippedCelebrationModal`` and
  ``_celebrate_first_shipped`` are underscore-prefixed and re-exported
  via ``cockpit_ui`` for back-compat (see #1354).

Wedge of the cockpit_ui.py god-module split tracked by #1354.
"""

from __future__ import annotations

import os

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static


_FIRST_SHIPPED_FRAMES = (
    "  ✨   🎉   ✨\n🎊  First PR shipped  🎊\n  ✨   🎉   ✨",
    "🎉   ✨   🎊   ✨\n  First PR shipped\n✨   🎊   ✨   🎉",
    "  🎊   ✨   🎉\n🎉  First PR shipped  🎉\n  ✨   🎊   ✨",
)


class _FirstShippedCelebrationModal(ModalScreen[None]):
    """Short-lived modal that celebrates the first shipped task."""

    DEFAULT_CSS = """
    #first-shipped-modal {
        width: 60;
        padding: 1 2;
        border: round #6fcf97;
        background: #102019;
        color: #effaf3;
    }
    #first-shipped-title {
        text-align: center;
        margin-bottom: 1;
    }
    #first-shipped-confetti {
        text-align: center;
        color: #ffd166;
        height: auto;
    }
    #first-shipped-hint {
        text-align: center;
        color: #93a7b3;
        margin-top: 1;
    }
    """

    BINDINGS = [Binding("escape", "dismiss", "Dismiss", show=False)]

    def __init__(self) -> None:
        super().__init__()
        self._frame_index = 0

    def compose(self) -> ComposeResult:  # pragma: no cover - Textual harness
        with Vertical(id="first-shipped-modal"):
            yield Static("First PR shipped", id="first-shipped-title")
            yield Static(_FIRST_SHIPPED_FRAMES[0], id="first-shipped-confetti", markup=True)
            yield Static(
                "Recorded once and pinned in Activity.",
                id="first-shipped-hint",
            )

    def on_mount(self) -> None:  # pragma: no cover - Textual harness
        try:
            self.set_interval(0.16, self._advance_frame)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.set_timer(2.0, self.dismiss)
        except Exception:  # noqa: BLE001
            pass

    def _advance_frame(self) -> None:
        self._frame_index = (self._frame_index + 1) % len(_FIRST_SHIPPED_FRAMES)
        try:
            self.query_one("#first-shipped-confetti", Static).update(
                _FIRST_SHIPPED_FRAMES[self._frame_index],
            )
        except Exception:  # noqa: BLE001
            pass


def _celebrate_first_shipped(app) -> None:
    """Announce the one-time shipped milestone in whichever cockpit view approved it."""
    app.notify("🎉 First PR shipped. Nicely done.", severity="information", timeout=2.0)
    if os.getenv("POLLY_NO_CONFETTI") == "1":
        return
    try:
        app.push_screen(_FirstShippedCelebrationModal())
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "_FIRST_SHIPPED_FRAMES",
    "_FirstShippedCelebrationModal",
    "_celebrate_first_shipped",
]
