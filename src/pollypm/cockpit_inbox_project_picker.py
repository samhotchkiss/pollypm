"""Inbox project-picker modal (extracted from ``cockpit_ui``).

Contract:
- Inputs: a list of project keys plus the currently-active filter key.
- Outputs: a ``ModalScreen`` returning the selected key (string) or
  ``None`` when dismissed via Esc; selecting the active project returns
  an empty string as the "clear" gesture.
- Side effects: none beyond standard Textual modal push/dismiss.
- Invariants: this module owns one widget class only; shared cockpit
  helpers stay in their original homes.
- Allowed dependencies: Textual primitives.
- Private: ``_InboxProjectPickerModal`` is underscore-prefixed and only
  re-exported via ``cockpit_ui`` for back-compat.

First wedge of the cockpit_ui.py god-module split tracked by #1354.
"""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import ListItem, ListView, Static


class _InboxProjectPickerModal(ModalScreen[str | None]):
    """Tiny modal listing project keys for the ``p`` filter chip.

    Returns the selected key (string) or ``None`` when dismissed via
    Esc. Selecting the currently-active project clears the chip.
    """

    CSS = """
    _InboxProjectPickerModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.45);
    }
    #ipp-dialog {
        width: 48;
        max-width: 90%;
        height: auto;
        max-height: 18;
        padding: 1 1 0 1;
        background: #141a20;
        border: round #2a3340;
    }
    #ipp-title {
        height: 1;
        padding: 0 1;
        color: #97a6b2;
    }
    #ipp-list {
        height: auto;
        max-height: 14;
        background: #141a20;
        border: none;
        margin-top: 1;
        padding: 0;
    }
    #ipp-list > .ipp-row {
        height: 1;
        padding: 0 1;
        color: #d6dee5;
        background: transparent;
    }
    #ipp-list > .ipp-row.-highlight {
        background: #1e2730;
    }
    #ipp-hint {
        height: 1;
        padding: 0 1;
        color: #3e4c5a;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Close"),
        Binding("down,j", "cursor_down", "Down", show=False),
        Binding("up,k", "cursor_up", "Up", show=False),
        Binding("enter", "select", "Pick", show=False),
    ]

    def __init__(self, keys: list[str], current: str | None) -> None:
        super().__init__()
        self._keys = list(keys)
        self._current = current
        self.list_view = ListView(id="ipp-list")
        self.title_bar = Static(
            "[b]Filter by project[/b]", id="ipp-title", markup=True,
        )
        self.hint = Static(
            "[dim]↵ select  ·  esc cancel  ·  pick the active "
            "project to clear[/dim]",
            id="ipp-hint", markup=True,
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="ipp-dialog"):
            yield self.title_bar
            yield self.list_view
            yield self.hint

    def on_mount(self) -> None:
        for key in self._keys:
            label = key
            if key == self._current:
                label = f"● {key}"
            self.list_view.append(
                ListItem(Static(label, markup=False), classes="ipp-row")
            )
        self.list_view.index = 0
        self.list_view.focus()

    def action_cursor_down(self) -> None:
        self.list_view.action_cursor_down()

    def action_cursor_up(self) -> None:
        self.list_view.action_cursor_up()

    def action_select(self) -> None:
        idx = self.list_view.index or 0
        if 0 <= idx < len(self._keys):
            picked = self._keys[idx]
            # Picking the current chip again is a "clear" gesture.
            if picked == self._current:
                self.dismiss("")
            else:
                self.dismiss(picked)
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(ListView.Selected, "#ipp-list")
    def _on_row_selected(self, _event: ListView.Selected) -> None:
        self.action_select()


__all__ = ["_InboxProjectPickerModal"]
