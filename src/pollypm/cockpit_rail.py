from __future__ import annotations

import os
import select
import shutil
import sys
import termios
import time
import tty
from dataclasses import dataclass, field
from pathlib import Path

from pollypm.cockpit import CockpitItem, CockpitRouter


# ── Color palette ────────────────────────────────────────────────────────────

_C = tuple[int, int, int]

PALETTE: dict[str, _C] = {
    "bg":              (15, 19, 23),
    "wordmark_hi":     (91, 138, 255),
    "wordmark_lo":     (55, 95, 195),
    "slogan":          (92, 106, 119),
    "slogan_fade":     (42, 52, 64),
    "section_rule":    (30, 39, 48),
    "section_label":   (74, 85, 104),
    "item_normal":     (184, 196, 207),
    "item_muted":      (92, 106, 119),
    "sel_bg":          (26, 58, 92),
    "sel_text":        (238, 246, 255),
    "sel_accent":      (91, 138, 255),
    "active_bg":       (22, 42, 64),
    "live_bg":         (19, 42, 30),
    "live_text":       (126, 232, 164),
    "live_indicator":  (61, 220, 132),
    "alert_bg":        (53, 26, 29),
    "alert_text":      (242, 184, 188),
    "alert_indicator": (255, 95, 109),
    "inbox_has":       (240, 196, 90),
    "inbox_empty":     (74, 85, 104),
    "idle":            (74, 85, 104),
    "dead":            (255, 95, 109),
    "hint":            (52, 64, 77),
}

ARC_SPINNER = ("◜", "◝", "◞", "◟")

ASCII_POLLY = (
    "█▀█ █▀█ █   █   █▄█",
    "█▀▀ █▄█ █▄▄ █▄▄  █ ",
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

GUTTER = 2


# ── Render row ───────────────────────────────────────────────────────────────

@dataclass(slots=True)
class RenderRow:
    text: str
    fg: _C = field(default_factory=lambda: PALETTE["item_normal"])
    bg: _C = field(default_factory=lambda: PALETTE["bg"])
    bold: bool = False


def _sgr(row: RenderRow) -> str:
    fr, fg, fb = row.fg
    br, bg, bb = row.bg
    bold = "1;" if row.bold else ""
    return f"\x1b[{bold}38;2;{fr};{fg};{fb};48;2;{br};{bg};{bb}m"


# ── Rail renderer ────────────────────────────────────────────────────────────

class PollyCockpitRail:
    def __init__(self, config_path: Path) -> None:
        self.router = CockpitRouter(config_path)
        self.selected_key = self.router.selected_key()
        self.spinner_index = 0
        self.slogan_started_at = time.time()
        self._last_items: list[CockpitItem] = []
        self._slogan_phase = 0

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
                self._tick_slogan()
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

    # ── Navigation ───────────────────────────────────────────────────────

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

    # ── Slogan rotation ─────────────────────────────────────────────────

    def _tick_slogan(self) -> None:
        elapsed = time.time() - self.slogan_started_at
        cycle = 60
        pos = elapsed % cycle
        if pos >= cycle - 1:
            self._slogan_phase = -1  # fade out
        elif pos < 1:
            self._slogan_phase = 1   # fade in
        else:
            self._slogan_phase = 0   # normal

    def _current_slogan(self) -> tuple[str, str]:
        index = int((time.time() - self.slogan_started_at) // 60) % len(POLLY_SLOGANS)
        return POLLY_SLOGANS[index]

    def _slogan_color(self) -> _C:
        if self._slogan_phase != 0:
            return PALETTE["slogan_fade"]
        return PALETTE["slogan"]

    # ── Rendering ────────────────────────────────────────────────────────

    def _render(self, items: list[CockpitItem]) -> None:
        size = shutil.get_terminal_size((30, 24))
        width = max(16, size.columns)
        height = max(8, size.lines)
        lines: list[RenderRow] = []
        pad = " " * GUTTER

        # ── Wordmark (centered)
        lines.append(RenderRow(""))
        wm0 = ASCII_POLLY[0]
        wm1 = ASCII_POLLY[1]
        lines.append(RenderRow(wm0.center(width)[:width], fg=PALETTE["wordmark_hi"], bold=True))
        lines.append(RenderRow(wm1.center(width)[:width], fg=PALETTE["wordmark_lo"]))
        lines.append(RenderRow(""))

        # ── Slogan (centered)
        slogan = self._current_slogan()
        sc = self._slogan_color()
        lines.append(RenderRow(slogan[0].center(width)[:width], fg=sc))
        lines.append(RenderRow(slogan[1].center(width)[:width], fg=sc))
        lines.append(RenderRow(""))

        # ── Section: navigation
        settings_item = next((item for item in items if item.key == "settings"), None)
        body_items = [item for item in items if item.key != "settings"]
        active_view = self.router.selected_key()

        first_project = True
        for item in body_items:
            if item.key.startswith("project:") and first_project:
                lines.append(RenderRow(""))
                lines.append(self._section_header("projects", width))
                first_project = False
            lines.append(self._item_row(item, width, active_view))

        # ── Spacer + settings at bottom
        if settings_item is not None:
            target_lines = len(lines) + 4  # section header + blank + item + hint
            while len(lines) < height - target_lines + len(lines):
                if len(lines) >= height - 4:
                    break
                lines.append(RenderRow(""))
            lines.append(RenderRow(""))
            lines.append(self._section_header("system", width))
            lines.append(self._item_row(settings_item, width, active_view))

        # ── Hint line
        while len(lines) < height - 2:
            lines.append(RenderRow(""))
        lines.append(RenderRow(""))
        hint = f"{pad}j/k move \u00b7 \u21b5 open \u00b7 n new"
        lines.append(RenderRow(hint[:width], fg=PALETTE["hint"]))

        # ── Flush
        bg = PALETTE["bg"]
        clear_line = f"\x1b[48;2;{bg[0]};{bg[1]};{bg[2]}m" + " " * width + "\x1b[0m"
        self._write("\x1b[H")
        for i in range(height):
            if i < len(lines):
                row = lines[i]
                if isinstance(row, _RawRow):
                    self._write(_sgr(row) + row.text + "\x1b[0m\r\n")
                else:
                    self._write(_sgr(row) + row.text.ljust(width)[:width] + "\x1b[0m\r\n")
            else:
                self._write(clear_line + "\r\n")

    def _section_header(self, label: str, width: int) -> RenderRow:
        pad = " " * GUTTER
        prefix = f"{pad}\u2500\u2500 {label.upper()} "
        remaining = width - len(prefix)
        rule = "\u2500" * max(0, remaining)
        return RenderRow((prefix + rule)[:width], fg=PALETTE["section_label"])

    def _item_row(self, item: CockpitItem, width: int, active_view: str) -> RenderRow:
        is_selected = item.key == self.selected_key
        is_active = item.key == active_view
        indicator, ind_color = self._indicator(item)

        # Build the text with gutter (2-char prefix for alignment)
        bar = "\u258c " if is_selected else "  "
        label = item.label
        max_label = width - 6  # 2 bar + 1 indicator + 1 space + 2 margin
        if len(label) > max_label and max_label > 3:
            label = label[: max_label - 1] + "\u2026"
        text = f" {bar}{indicator} {label}"
        text = text[:width]

        # Determine colors
        fg = PALETTE["item_normal"]
        bg = PALETTE["bg"]
        bold = False

        if item.state.startswith("!"):
            fg = PALETTE["alert_text"]
            bg = PALETTE["alert_bg"]
        elif item.state.endswith("live") and not is_selected:
            fg = PALETTE["live_text"]
            bg = PALETTE["live_bg"]

        if is_selected:
            fg = PALETTE["sel_text"]
            bg = PALETTE["sel_bg"]
            bold = True
        elif is_active and not is_selected:
            fg = PALETTE["sel_text"]
            bg = PALETTE["active_bg"]

        row = RenderRow(text, fg=fg, bg=bg, bold=bold)

        # Apply indicator color inline via ANSI if indicator is present
        if ind_color and indicator.strip():
            # Rebuild text with colored indicator
            bar_sgr = ""
            if is_selected:
                bar_sgr = f"\x1b[38;2;{PALETTE['sel_accent'][0]};{PALETTE['sel_accent'][1]};{PALETTE['sel_accent'][2]}m"
            label_part = f" {item.label}"
            ind_sgr = f"\x1b[38;2;{ind_color[0]};{ind_color[1]};{ind_color[2]}m"
            text_sgr = _sgr(row)
            row.text = f" {bar_sgr}{bar}\x1b[0m{text_sgr}{ind_sgr}{indicator} \x1b[0m{text_sgr}{label_part}"
            # Pad manually since we have inline escapes
            visible_len = 1 + 1 + len(indicator) + 1 + len(item.label)
            if visible_len < width:
                row.text += " " * (width - visible_len)
            row.text = row.text  # already formatted
            # Return a special row that writes raw (skip _sgr in render)
            return _RawRow(row.text, fg=fg, bg=bg, bold=bold)

        return row

    def _indicator(self, item: CockpitItem) -> tuple[str, _C | None]:
        if item.state.endswith("working"):
            char = ARC_SPINNER[self.spinner_index]
            return char, PALETTE["live_indicator"]
        if item.state.endswith("live"):
            return "\u25cf", PALETTE["live_indicator"]
        if item.state.startswith("!"):
            return "\u25b2", PALETTE["alert_indicator"]
        if item.state == "dead":
            return "\u2715", PALETTE["dead"]
        if item.key == "inbox":
            has_items = "(" in item.label and not item.label.endswith("(0)")
            if has_items:
                return "\u25c6", PALETTE["inbox_has"]
            return "\u25c7", PALETTE["inbox_empty"]
        if item.key == "polly":
            return "\u2022", PALETTE["sel_accent"]
        if item.key == "settings":
            return "\u2699", PALETTE["item_muted"]
        if item.key.startswith("project:"):
            return "\u25cb", PALETTE["idle"]
        return " ", None

    def _write(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()


class _RawRow(RenderRow):
    """A row whose .text already contains ANSI escapes -- render without wrapping in _sgr()."""
    pass


def run_cockpit_rail(config_path: Path) -> None:
    PollyCockpitRail(config_path).run()
