from __future__ import annotations

import os
import select
import shutil
import sys
import termios
import time
import tty
from dataclasses import dataclass
from pathlib import Path

from promptmaster.cockpit import CockpitItem, CockpitRouter


ASCII_POLLY = (
    "┏━ POLLY ━┓",
    "┗━━━━━━━━━━┛",
)

POLLY_SLOGANS = [
    ("Plans first.", "Chaos later."),
    ("Inbox clear.", "Projects moving."),
    ("Small steps.", "Sharp turns."),
    ("Less thrash.", "More shipped."),
    ("Watch the drift.", "Trim the waste."),
    ("Keep it modular.", "Keep it moving."),
    ("Fewer heroics.", "More progress."),
    ("Big picture.", "Tight loops."),
    ("Plan clean.", "Land faster."),
    ("Break it down.", "Ship it right."),
    ("Stay useful.", "Stay honest."),
    ("No mystery.", "Just momentum."),
    ("Steady lanes.", "Clean handoffs."),
    ("Less panic.", "More process."),
    ("Trim the scope.", "Raise the bar."),
    ("One project.", "Many good turns."),
]


@dataclass(slots=True)
class RenderRow:
    text: str
    fg: str = "37"
    bg: str | None = None
    bold: bool = False


class PollyCockpitRail:
    def __init__(self, config_path: Path) -> None:
        self.router = CockpitRouter(config_path)
        self.selected_key = self.router.selected_key()
        self.spinner_index = 0
        self.slogan_started_at = time.time()
        self._last_items: list[CockpitItem] = []

    def run(self) -> None:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            self._write("\x1b[?25l")
            while True:
                self.router.ensure_cockpit_layout()
                items = self.router.build_items(spinner_index=self.spinner_index)
                self._last_items = items
                self._clamp_selection(items)
                self._render(items)
                self.spinner_index = (self.spinner_index + 1) % 4
                ready, _, _ = select.select([sys.stdin], [], [], 1.0)
                if not ready:
                    continue
                key = os.read(fd, 32)
                if not key:
                    continue
                if not self._handle_key(key, items):
                    break
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
            self._write("\x1b[0m\x1b[?25h")

    def _handle_key(self, key: bytes, items: list[CockpitItem]) -> bool:
        if key in {b"q", b"\x03"}:
            return False
        if key in {b"j", b"\x1b[B"}:
            self._move(1, items)
            return True
        if key in {b"k", b"\x1b[A"}:
            self._move(-1, items)
            return True
        if key in {b"g", b"\x1b[H"}:
            self._select_first(items)
            return True
        if key in {b"G", b"\x1b[F"}:
            self._select_last(items)
            return True
        if key in {b"\r", b"\n"}:
            self.router.route_selected(self.selected_key)
            return True
        if key in {b"n", b"N"} and self.selected_key.startswith("project:"):
            self.router.create_worker_and_route(self.selected_key.split(":", 1)[1])
            return True
        if key in {b"s", b"S"}:
            self.router.route_selected("settings")
            self.selected_key = "settings"
            return True
        if key in {b"i", b"I"}:
            self.router.route_selected("inbox")
            self.selected_key = "inbox"
            return True
        return True

    def _move(self, delta: int, items: list[CockpitItem]) -> None:
        keys = [item.key for item in items if item.selectable]
        if not keys:
            return
        try:
            index = keys.index(self.selected_key)
        except ValueError:
            self.selected_key = keys[0]
            self.router.set_selected_key(self.selected_key)
            return
        self.selected_key = keys[(index + delta) % len(keys)]
        self.router.set_selected_key(self.selected_key)

    def _select_first(self, items: list[CockpitItem]) -> None:
        for item in items:
            if item.selectable:
                self.selected_key = item.key
                self.router.set_selected_key(self.selected_key)
                return

    def _select_last(self, items: list[CockpitItem]) -> None:
        for item in reversed(items):
            if item.selectable:
                self.selected_key = item.key
                self.router.set_selected_key(self.selected_key)
                return

    def _clamp_selection(self, items: list[CockpitItem]) -> None:
        keys = {item.key for item in items if item.selectable}
        if self.selected_key not in keys and keys:
            self.selected_key = next(iter(keys))
            self.router.set_selected_key(self.selected_key)

    def _render(self, items: list[CockpitItem]) -> None:
        size = shutil.get_terminal_size((16, 24))
        width = max(16, size.columns)
        height = max(8, size.lines)
        lines: list[RenderRow] = []
        for wordmark in ASCII_POLLY:
            lines.append(RenderRow(wordmark[:width], fg="255", bold=True))
        lines.append(RenderRow(""))
        slogan = self._current_slogan()
        lines.append(RenderRow(slogan[0][:width], fg="245"))
        lines.append(RenderRow(slogan[1][:width], fg="245"))
        lines.append(RenderRow(""))
        settings_item = next((item for item in items if item.key == "settings"), None)
        body_items = [item for item in items if item.key != "settings"]
        first_project = True
        active_view = self.router.selected_key()
        for item in body_items:
            label = self._item_label(item, width)
            row = RenderRow(label)
            if item.key == self.selected_key or item.key == active_view:
                row.bg = "24"
                row.fg = "255"
                row.bold = True
            if item.state.startswith("!"):
                row.bg = "52" if row.bg is None else row.bg
                row.fg = "224"
            if item.state.endswith("live"):
                row.bg = "22" if row.bg is None else row.bg
                row.fg = "157" if row.bg != "24" else "255"
            if item.key.startswith("project:") and first_project:
                lines.append(RenderRow(""))
                first_project = False
            lines.append(row)
        if settings_item is not None:
            reserved_lines = len(lines) + 2
            while reserved_lines < height - 1:
                lines.append(RenderRow(""))
                reserved_lines += 1
            lines.append(RenderRow(""))
            label = self._item_label(settings_item, width)
            row = RenderRow(label)
            if settings_item.key == self.selected_key or settings_item.key == active_view:
                row.bg = "24"
                row.fg = "255"
                row.bold = True
            lines.append(row)
        self._write("\x1b[H\x1b[2J")
        for row in lines:
            self._write(self._style(row) + row.text.ljust(width)[:width] + "\x1b[0m\r\n")

    def _item_label(self, item: CockpitItem, width: int) -> str:
        indicator = "  "
        if item.state.endswith("live"):
            indicator = item.state.split(" ", 1)[0] + " "
        elif item.state.startswith("!"):
            indicator = "! "
        elif item.key in {"polly", "settings"}:
            indicator = "• "
        text = f"{indicator}{item.label}"
        return text[:width]

    def _current_slogan(self) -> tuple[str, str]:
        index = int((time.time() - self.slogan_started_at) // 60) % len(POLLY_SLOGANS)
        return POLLY_SLOGANS[index]

    def _style(self, row: RenderRow) -> str:
        codes: list[str] = []
        if row.bold:
            codes.append("1")
        codes.append(row.fg)
        if row.bg is not None:
            codes.append(f"48;5;{row.bg}")
        return f"\x1b[{';'.join(codes)}m"

    def _write(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()


def run_cockpit_rail(config_path: Path) -> None:
    PollyCockpitRail(config_path).run()
