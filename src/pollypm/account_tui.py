from __future__ import annotations

from pathlib import Path

from pollypm.control_tui import PollyPMApp


class AccountsApp(PollyPMApp):
    def __init__(self, config_path: Path) -> None:
        super().__init__(config_path=config_path)
