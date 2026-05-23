"""Email-input modal used by cockpit settings to identify a new account.

Contract:
- Inputs: ``title`` and ``prompt`` strings; optional ``confirm_label``.
- Outputs: a ``ModalScreen[str | None]`` that resolves to the lowercased
  trimmed email string on confirm, or ``None`` on cancel/escape.
- Side effects: pushes/dismisses a Textual modal; no I/O of its own.
- Invariants: this module owns one widget class and nothing else; it has
  no dependency on cockpit state or services.
- Allowed dependencies: Textual primitives only.

Used by the cockpit settings "Add Claude" flow. Claude Max plans on CLI
2.x report ``loggedIn: true`` with ``email: null``, so we cannot detect
the email automatically — the operator must supply it.

Wedge of the cockpit_ui.py god-module split tracked by #1354.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static


class _SettingsEmailPromptModal(ModalScreen[str | None]):
    CSS = """
    Screen {
        align: center middle;
    }
    #settings-email-prompt {
        width: 72;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: heavy $primary;
    }
    #settings-email-prompt-title {
        padding-bottom: 1;
        text-style: bold;
    }
    #settings-email-prompt-input {
        margin-top: 1;
        margin-bottom: 1;
    }
    #settings-email-prompt-buttons {
        height: auto;
        align-horizontal: right;
        padding-top: 1;
    }
    #settings-email-prompt-buttons Button {
        margin-left: 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self,
        *,
        title: str,
        prompt: str,
        confirm_label: str = "Add",
        cancel_label: str = "Cancel",
        placeholder: str = "you@example.com",
    ) -> None:
        super().__init__()
        self._title = title
        self._prompt = prompt
        self._confirm_label = confirm_label
        self._cancel_label = cancel_label
        self._placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-email-prompt"):
            yield Static(self._title, id="settings-email-prompt-title")
            yield Static(self._prompt)
            yield Input(
                placeholder=self._placeholder,
                id="settings-email-prompt-input",
            )
            with Horizontal(id="settings-email-prompt-buttons"):
                yield Button(self._cancel_label, id="cancel")
                yield Button(
                    self._confirm_label, variant="primary", id="confirm"
                )

    def on_mount(self) -> None:
        try:
            self.query_one("#settings-email-prompt-input", Input).focus()
        except Exception:  # noqa: BLE001
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            self._submit()
        else:
            self.dismiss(None)

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        self._submit()

    def _submit(self) -> None:
        try:
            value = self.query_one("#settings-email-prompt-input", Input).value
        except Exception:  # noqa: BLE001
            value = ""
        cleaned = (value or "").strip().lower()
        if not cleaned or "@" not in cleaned:
            # Invalid input — leave the modal open so the operator can fix.
            # No error styling yet; could add a Static error label later.
            return
        self.dismiss(cleaned)

    def action_cancel(self) -> None:
        self.dismiss(None)


__all__ = ["_SettingsEmailPromptModal"]
