"""#1543 — raw rail must surface a scroll affordance when the project
list overflows the current terminal height.

Before this guard, ``PollyCockpitRail._render`` silently truncated rows
that fell past ``shutil.get_terminal_size().lines``. On a short laptop
window that hid 11 of 12 projects with no indicator — the user thought
they only had one project.

The fix scrolls the body within a bounded window, keeps the selected
row in view, and renders a one-line "▼ N more" hint when content is
hidden. PgUp/PgDn pin the offset so the user can browse off-screen
rows without losing their selection; subsequent j/k navigation resumes
auto-scroll.
"""

from __future__ import annotations

import os
from unittest.mock import patch

from pollypm.cockpit_rail import (
    CockpitItem,
    PollyCockpitRail,
    RenderRow,
)


def _make_rail() -> PollyCockpitRail:
    rail = PollyCockpitRail.__new__(PollyCockpitRail)
    rail._scroll_offset = None
    rail._last_body_total = 0
    rail._last_body_visible = 0
    rail.selected_key = "project:alpha"
    return rail


def test_resolve_scroll_offset_auto_keeps_selected_visible() -> None:
    """Auto-scroll lands the selected row near the bottom of the window
    so the user sees both the highlighted item and a hint of rows above.
    """
    rail = _make_rail()
    # Selected at row 15, window of 5, total of 20: auto offset should
    # land at 11 so rows 11..15 are visible.
    offset = rail._resolve_scroll_offset(selected_row=15, total=20, window=5)
    assert offset == 11

    # Selected near the top — no scroll needed.
    offset = rail._resolve_scroll_offset(selected_row=2, total=20, window=5)
    assert offset == 0

    # No body selection (e.g. ``settings`` is active) — pin to top.
    offset = rail._resolve_scroll_offset(selected_row=None, total=20, window=5)
    assert offset == 0


def test_resolve_scroll_offset_honors_pinned_offset() -> None:
    """PgUp/PgDn pins an offset; auto-scroll defers until j/k clears it."""
    rail = _make_rail()
    rail._scroll_offset = 7
    offset = rail._resolve_scroll_offset(selected_row=2, total=20, window=5)
    assert offset == 7  # honors the pin instead of snapping to selection
    # Out-of-range pin clamps to the valid window.
    rail._scroll_offset = 99
    offset = rail._resolve_scroll_offset(selected_row=None, total=20, window=5)
    assert offset == 15  # 20 - 5


def test_overflow_indicator_text_for_both_directions() -> None:
    """The indicator string must call out hidden rows on either side."""
    rail = _make_rail()
    below_only = rail._overflow_indicator_row(above=0, below=11, width=30)
    assert "11 more" in below_only.text
    assert "j to scroll" in below_only.text

    above_only = rail._overflow_indicator_row(above=4, below=0, width=30)
    assert "4 more" in above_only.text
    assert "k to scroll" in above_only.text

    both = rail._overflow_indicator_row(above=2, below=3, width=30)
    assert "2 above" in both.text
    assert "3 below" in both.text


def test_move_resets_pinned_scroll_offset() -> None:
    """j/k selection moves must resume auto-scroll so the selected row
    is always brought back into view; otherwise PgDn → j leaves the
    cursor invisible.
    """
    rail = _make_rail()
    rail._scroll_offset = 5
    rail.router = type("R", (), {"set_selected_key": lambda self, k: None})()
    items = [
        CockpitItem(key="dashboard", label="Home", state="idle"),
        CockpitItem(key="inbox", label="Inbox", state="idle"),
    ]
    rail.selected_key = "dashboard"
    rail._move(1, items)
    assert rail._scroll_offset is None, (
        "j/k navigation must clear the pinned scroll offset so the "
        "selected row is auto-scrolled back into view"
    )


def test_page_scroll_pins_offset_when_body_overflows() -> None:
    """PgDn pages the visible window forward; PgUp pages it back."""
    rail = _make_rail()
    rail._last_body_total = 20
    rail._last_body_visible = 6  # window is 5 (visible - 1 for indicator)
    rail._scroll_offset = 0
    rail._move = lambda *args, **kwargs: None  # type: ignore[assignment]

    rail._page_scroll(1, items=[])
    assert rail._scroll_offset == 5

    rail._page_scroll(1, items=[])
    assert rail._scroll_offset == 10

    rail._page_scroll(-1, items=[])
    assert rail._scroll_offset == 5

    # Clamps at the upper bound.
    rail._page_scroll(1, items=[])
    rail._page_scroll(1, items=[])
    rail._page_scroll(1, items=[])
    assert rail._scroll_offset == 15  # 20 - 5


def test_page_scroll_falls_back_to_move_when_no_overflow() -> None:
    """When the body fits, PgUp/PgDn act as larger jumps for selection."""
    rail = _make_rail()
    rail._last_body_total = 5
    rail._last_body_visible = 10  # fits with room to spare
    rail._scroll_offset = None
    moves: list[int] = []
    rail._move = lambda delta, items: moves.append(delta)  # type: ignore[assignment]
    rail._page_scroll(1, items=[])
    rail._page_scroll(-1, items=[])
    assert moves == [9, -9]  # max(1, visible - 1) = 9


def test_handle_key_routes_pgup_pgdn_to_page_scroll() -> None:
    """The PgUp/PgDn escape sequences (xterm) must reach ``_page_scroll``."""
    rail = _make_rail()
    rail.router = type("R", (), {})()
    rail.selected_key = "project:alpha"
    calls: list[int] = []
    rail._page_scroll = lambda direction, items: calls.append(direction)  # type: ignore[assignment]
    assert rail._handle_key(b"\x1b[6~", items=[]) is True  # PgDn
    assert rail._handle_key(b"\x1b[5~", items=[]) is True  # PgUp
    assert calls == [1, -1]


def test_row_for_selected_skips_separator_rows() -> None:
    """Headers and blank rows shouldn't be mistaken for selectable items."""
    rail = _make_rail()
    rail.selected_key = "project:beta"
    body_items = [
        CockpitItem(key="dashboard", label="Home", state="idle"),
        CockpitItem(key="project:alpha", label="Alpha", state="idle"),
        CockpitItem(key="project:beta", label="Beta", state="idle"),
    ]
    # 3 items with a blank row + separator inserted before project:alpha:
    # rows 0=Home, 1=blank, 2=`-- projects --`, 3=Alpha, 4=Beta
    body_item_index = [0, None, None, 1, 2]
    row = rail._row_for_selected(body_item_index, body_items)
    assert row == 4


def test_render_emits_overflow_indicator_when_short_terminal() -> None:
    """End-to-end: a 12-project rail rendered into a 16-line terminal
    must produce an "N more" hint row instead of silently dropping the
    bottom-most projects (#1543 acceptance).
    """
    rail = _make_rail()
    rail.selected_key = "dashboard"
    rail.spinner_index = 0
    rail.presence = type("P", (), {"should_animate": lambda self: False})()
    rail._slogan_phase = 0
    rail.slogan_started_at = 0.0
    rail._ticker_started_at = 0.0

    # Stub the router so ``_render`` doesn't touch the live supervisor.
    rail.router = type("R", (), {
        "selected_key": lambda self: "dashboard",
    })()
    rail._event_ticker_text = lambda: ""  # type: ignore[assignment]

    items = [CockpitItem(key="dashboard", label="Home", state="idle")]
    items.extend(
        CockpitItem(key=f"project:p{i}", label=f"Proj {i}", state="idle")
        for i in range(12)
    )
    items.append(CockpitItem(key="settings", label="Settings", state="idle"))

    output: list[str] = []
    rail._write = lambda text: output.append(text)  # type: ignore[assignment]

    # 16 rows is well below the ~25 the full rail wants — forces overflow.
    with patch("pollypm.cockpit_rail.shutil.get_terminal_size",
               return_value=os.terminal_size((30, 16))):
        rail._render(items)

    rendered = "".join(output)
    assert "more" in rendered, (
        "rail must emit a 'N more' overflow indicator when the project "
        "list can't fit at the current terminal height (#1543); got:\n"
        + rendered
    )
    assert rail._scroll_offset is not None, (
        "_render must set _scroll_offset when overflow is active so "
        "_page_scroll has a baseline to advance from"
    )


def test_render_no_indicator_when_terminal_is_tall_enough() -> None:
    """No overflow → no indicator row (no false-positive nag)."""
    rail = _make_rail()
    rail.selected_key = "dashboard"
    rail.spinner_index = 0
    rail.presence = type("P", (), {"should_animate": lambda self: False})()
    rail._slogan_phase = 0
    rail.slogan_started_at = 0.0
    rail._ticker_started_at = 0.0
    rail.router = type("R", (), {
        "selected_key": lambda self: "dashboard",
    })()
    rail._event_ticker_text = lambda: ""  # type: ignore[assignment]

    items = [
        CockpitItem(key="dashboard", label="Home", state="idle"),
        CockpitItem(key="settings", label="Settings", state="idle"),
    ]

    output: list[str] = []
    rail._write = lambda text: output.append(text)  # type: ignore[assignment]
    with patch("pollypm.cockpit_rail.shutil.get_terminal_size",
               return_value=os.terminal_size((30, 40))):
        rail._render(items)
    rendered = "".join(output)
    assert "more · j to scroll" not in rendered
    assert "more · k to scroll" not in rendered
    assert rail._scroll_offset is None
