"""Account-reassignment modal used by the cockpit settings screen.

Contract:
- Inputs: ``source_key`` (the account being removed/replaced),
  ``session_refs`` (list of pinned session dicts), ``target_options``
  (list of ``(label, key)`` tuples for the target Select), an
  ``include_controller`` flag, plus ``title`` / ``prompt`` strings.
- Outputs: a ``ModalScreen[str | None]`` that resolves to the chosen
  target account key, or ``None`` on cancel/escape/no-selection.
- Side effects: pushes/dismisses a Textual modal; no I/O of its own.
- Invariants: this module owns one widget class and nothing else; it
  has no dependency on cockpit state or services.
- Allowed dependencies: Textual primitives only.
- Private: ``_SettingsAccountReassignModal`` is underscore-prefixed and
  re-exported via ``cockpit_ui`` for back-compat (see #1354).

Wedge of the cockpit_ui.py god-module split tracked by #1354.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Select, Static


class _SettingsAccountReassignModal(ModalScreen[str | None]):
    CSS = """
    Screen {
        align: center middle;
    }
    #settings-account-reassign {
        width: 86;
        max-height: 34;
        padding: 1 2;
        background: $panel;
        border: heavy $primary;
    }
    #settings-account-reassign-title {
        padding-bottom: 1;
        text-style: bold;
    }
    #settings-account-reassign-list {
        max-height: 12;
        padding-top: 1;
        padding-bottom: 1;
    }
    #settings-account-reassign-target {
        width: 1fr;
    }
    #settings-account-reassign-buttons {
        height: auto;
        align-horizontal: right;
        padding-top: 1;
    }
    #settings-account-reassign-buttons Button {
        margin-left: 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self,
        *,
        source_key: str,
        session_refs: list[dict[str, object]],
        target_options: list[tuple[str, str]],
        include_controller: bool,
        title: str,
        prompt: str,
    ) -> None:
        super().__init__()
        self._source_key = source_key
        self._session_refs = list(session_refs)
        self._target_options = list(target_options)
        self._include_controller = include_controller
        self._title = title
        self._prompt = prompt

    def compose(self) -> ComposeResult:
        default_value = (
            self._target_options[0][1]
            if self._target_options else Select.NULL
        )
        with Vertical(id="settings-account-reassign"):
            yield Static(self._title, id="settings-account-reassign-title")
            yield Static(self._prompt)
            with VerticalScroll(id="settings-account-reassign-list"):
                if self._include_controller:
                    yield Static(
                        f"Controller account: {self._source_key}",
                    )
                if self._session_refs:
                    yield Static("Pinned sessions:")
                    for ref in self._session_refs:
                        role = str(ref.get("role") or "-")
                        project = str(ref.get("project") or "-")
                        provider = str(ref.get("provider") or "-")
                        yield Static(
                            f"  {ref.get('name') or ''}  {role} / {project} / {provider}",
                        )
                else:
                    yield Static("Pinned sessions: none.")
            yield Static("Target account")
            yield Select(
                self._target_options,
                allow_blank=False,
                value=default_value,
                id="settings-account-reassign-target",
            )
            with Horizontal(id="settings-account-reassign-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Reassign", variant="primary", id="confirm")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "confirm":
            self.dismiss(None)
            return
        try:
            select = self.query_one(
                "#settings-account-reassign-target",
                Select,
            )
            value = select.value
        except Exception:  # noqa: BLE001
            value = None
        if value is Select.NULL or not value:
            self.dismiss(None)
            return
        self.dismiss(str(value))

    def action_cancel(self) -> None:
        self.dismiss(None)


__all__ = ["_SettingsAccountReassignModal"]
