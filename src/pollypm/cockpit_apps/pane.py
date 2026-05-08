"""Fallback cockpit pane Textual app."""

from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Static

from pollypm.cockpit import build_cockpit_detail


class PollyCockpitPaneApp(App[None]):
    TITLE = "PollyPM"
    SUB_TITLE = "Pane"
    CSS = """
    Screen {
        background: #10161b;
        color: #eef2f4;
        padding: 1;
    }
    #body {
        border: round #253140;
        background: #111820;
        padding: 1 2;
    }
    """

    def __init__(self, config_path: Path, kind: str, target: str | None = None) -> None:
        super().__init__()
        self.config_path = config_path
        self.kind = kind
        self.target = target
        self.body = Static("", id="body")

    def compose(self) -> ComposeResult:
        yield self.body

    def on_mount(self) -> None:
        self._refresh()
        self.set_interval(5, self._refresh)
        # #1109 follow-up: TTY-less keystroke bridge.
        try:
            from pollypm.cockpit_input_bridge import start_input_bridge

            bridge_kind = f"pane-{self.kind}"
            self._input_bridge_handle = start_input_bridge(
                self,
                kind=bridge_kind,
                config_path=self.config_path,
            )
        except Exception:  # noqa: BLE001
            self._input_bridge_handle = None

    def on_unmount(self) -> None:
        bridge = getattr(self, "_input_bridge_handle", None)
        if bridge is not None:
            try:
                bridge.stop()
            except Exception:  # noqa: BLE001
                pass

    def _refresh(self) -> None:
        self.body.update(build_cockpit_detail(self.config_path, self.kind, self.target))
