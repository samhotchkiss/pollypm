"""Semantic palette + glyph vocabulary for the cockpit UI.

Single source of truth for the (color, glyph) pairs used across the
Textual cockpit. Today the same hex strings (``#f0c45a``, ``#3ddc84``,
``#ff5f6d``, ``#5b8aff``, ``#4a5568``, ``#6b7a88``) are duplicated
across ``cockpit_rail_item``, ``cockpit_ui_inbox_format``,
``cockpit_activity``, ``cockpit_alert_detail``, and the dashboard CSS
in ``cockpit_ui`` — silently drift-prone (the inbox plan-review row
ended up at ``#ff6b5b`` instead of the rail's ``#ff5f6d``, an obvious
accidental copy-paste).

This module exposes two namespaces:

- :class:`State` — semantic state → hex color string (Rich markup ready)
- :class:`Glyph` — semantic role → single-character glyph

Each ``State`` constant has a matching ``as_tuple`` form for callers
that drive the raw rail renderer (``cockpit_rail.PALETTE``), which
historically stored ``(r, g, b)`` triples. Bridging both representations
lets consumers migrate one site at a time.

Scope/contract:
- Pure data, no imports beyond ``typing`` / stdlib. Zero side effects.
- Leaf module: importable from any ``cockpit_*`` module without risking
  cycles. Specifically does NOT import from ``cockpit_rail``,
  ``cockpit_ui``, or any Textual primitive.
- Constants are strings/chars — Rich markup callers can use them inline
  via f-strings (``f"[{State.WAITING}]◆[/{State.WAITING}]"``).
- Wedge of the #1354 god-module split: gives downstream modules a small
  shared dependency so the rail-item / inbox-format / activity colour
  duplication can be folded into one audit-able place.
"""

from __future__ import annotations


def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """Convert ``#rrggbb`` to ``(r, g, b)`` ints in [0, 255].

    Used by the rail's raw renderer (``cockpit_rail.PALETTE``) which
    historically stored RGB triples. Letting both representations co-exist
    means the migration off duplicated hex literals can land one consumer
    at a time without rewriting the rail's renderer.
    """
    if not hex_color.startswith("#") or len(hex_color) != 7:
        raise ValueError(f"expected #rrggbb hex color, got {hex_color!r}")
    return (
        int(hex_color[1:3], 16),
        int(hex_color[3:5], 16),
        int(hex_color[5:7], 16),
    )


class State:
    """Semantic state colors.

    Each name describes the operator-visible meaning, not the hue —
    so a hue tweak (say "amber→orange") happens in one place without
    losing semantic intent. Hex strings are the ones already present
    in the cockpit so this is rename-only, not a redesign.
    """

    # Amber — user action required / decision pending / unread inbox.
    # Today used as ``#f0c45a`` across rail-item / inbox-format / activity.
    WAITING = "#f0c45a"

    # Green — agent actively writing / shipped / done. The cockpit
    # currently does not visually differentiate "working green" from
    # "shipped green"; if that distinction is wanted later, split this
    # into ``WORKING`` and ``DONE`` and update consumers.
    WORKING = "#3ddc84"
    DONE = "#3ddc84"

    # Red — stuck / error / dead / blocked. Rail uses ``#ff5f6d``; some
    # surfaces drifted to ``#ff6b5b`` — we standardise on the rail's.
    BLOCKED = "#ff5f6d"

    # Slate — at rest / idle / disabled. Used for ``○`` icons.
    IDLE = "#4a5568"

    # Blue — top-level nav highlight / active-view marker / info.
    INFO = "#5b8aff"

    # Warm gray — metadata, age, project labels, dim subtitles.
    MUTED = "#6b7a88"

    # Orange — project-yellow rollup glyph + needs-decision pill.
    # Today rail uses ``#f0a030`` for the "Waiting on you:" diamond
    # (one step warmer than ``WAITING`` to differentiate "needs your
    # decision" from "needs your attention").
    ATTENTION = "#f0a030"

    # Heading text on selected/highlighted rows (rail "sel_text",
    # inbox "bold subject"). The brightest neutral in the palette.
    HEADING = "#eef2f4"

    # Body / label color for non-selected rail rows. One step dimmer
    # than HEADING.
    LABEL = "#b8c4cf"


class Glyph:
    """Semantic glyph vocabulary.

    Each name describes the operator-visible role, not the shape — so
    a glyph swap (say ``◆`` → ``◈``) happens in one place. Characters
    are the ones already in the cockpit so this is rename-only.
    """

    # ◆ Yellow diamond — "needs attention" / unread / decision-needed.
    # The dashboard's Action Needed banner and the rail's inbox-has-mail
    # row both use this — same shape, same color (``State.WAITING``).
    ATTENTION = "◆"  # ◆

    # • Filled bullet — currently active / live / writing.
    LIVE = "•"  # •

    # ○ Hollow circle — at rest / idle.
    IDLE = "○"  # ○

    # ▲ Red triangle — alert / fault / dead row.
    BLOCKED = "▲"  # ▲

    # ▶ Play arrow — decision pending (plan review, approval).
    DECISION = "▶"  # ▶

    # ◇ Hollow diamond — paused / waiting-on-plan / inbox empty.
    WAITING = "◇"  # ◇

    # ◉ Bullseye — task in review status.
    REVIEW = "◉"  # ◉

    # ♥ / ♡ — tmux session pulse (attached / detached).
    PULSE_ON = "♥"  # ♥
    PULSE_OFF = "♡"  # ♡

    # ⚠ — worker stuck.
    STUCK = "⚠"  # ⚠

    # ✕ — session exited.
    EXITED = "✕"  # ✕

    # ✎ — reviewer writing.
    REVIEWING = "✎"  # ✎

    # Arc spinner — animated 4-frame "thinking" indicator. Tuple, not a
    # single char, so callers index into it with a frame counter.
    SPINNER = ("◜", "◝", "◞", "◟")  # ◜ ◝ ◞ ◟


__all__ = ["State", "Glyph", "hex_to_rgb"]
