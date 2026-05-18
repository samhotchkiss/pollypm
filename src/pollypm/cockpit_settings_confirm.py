"""Generic confirm/cancel modal used by cockpit settings screens.

Contract:
- Inputs: ``title``, ``prompt``, and optional ``confirm_label`` /
  ``cancel_label`` strings.
- Outputs: a ``ModalScreen[bool]`` that resolves to ``True`` on confirm
  and ``False`` on cancel/escape.
- Side effects: pushes/dismisses a Textual modal; no I/O of its own.
- Invariants: this module owns one widget class and nothing else; it
  has no dependency on cockpit state or services.
- Allowed dependencies: Textual primitives only.
- Private: ``_SettingsConfirmModal`` is underscore-prefixed and
  re-exported via ``cockpit_ui`` for back-compat (see #1354).

Wedge of the cockpit_ui.py god-module split tracked by #1354.

Previously this class was defined identically in both ``cockpit_ui``
and ``cockpit_project_settings``; this module is now the single home.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class _SettingsConfirmModal(ModalScreen[bool]):
    CSS = """
    Screen {
        align: center middle;
    }
    #settings-confirm {
        width: 72;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: heavy $warning;
    }
    #settings-confirm-title {
        padding-bottom: 1;
        text-style: bold;
    }
    #settings-confirm-buttons {
        height: auto;
        align-horizontal: right;
        padding-top: 1;
    }
    #settings-confirm-buttons Button {
        margin-left: 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self,
        *,
        title: str,
        prompt: str,
        confirm_label: str = "Confirm",
        cancel_label: str = "Cancel",
    ) -> None:
        super().__init__()
        self._title = title
        self._prompt = prompt
        self._confirm_label = confirm_label
        self._cancel_label = cancel_label

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-confirm"):
            yield Static(self._title, id="settings-confirm-title")
            yield Static(self._prompt)
            with Horizontal(id="settings-confirm-buttons"):
                yield Button(self._cancel_label, id="cancel")
                yield Button(self._confirm_label, variant="primary", id="confirm")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)


__all__ = ["_SettingsConfirmModal"]
