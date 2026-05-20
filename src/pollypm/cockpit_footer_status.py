"""Unified footer status-bar formatter for the cockpit.

Per the 2026-05-20 tmux UI audit, the cockpit footer today is a 4-line
hodgepodge: ``⚙ Settings`` row, event ticker, key hint, and a wrapping
heartbeat-offline warning. The audit recommended collapsing those into
one coherent status line of the shape::

    12 projects · 38 agents · 23 inbox  |  ⚠ heartbeat 26h offline

This module ships the pure formatter for that string with explicit
truncation rules + per-state color cues. It is intentionally a leaf
module — no Textual / Rich primitive imports, no I/O. Call sites pass
in already-resolved counts and (optionally) an alert string; the
formatter returns Rich-markup ready text the caller can hand to a
``Static`` widget or the rail's row builder.

Wiring is intentionally NOT included in this PR — the audit doc flagged
the rail's footer rendering loop as the higher-risk surface to touch
last. Landing the formatter + its tests independently lets future
follow-ups adopt it from any of the candidate sites
(``PollyCockpitApp._update_hint``, ``cockpit_rail._format_event_ticker``,
the headless ``pm rail``) without re-deciding the format.

Contract:
- Inputs: ``project_count`` / ``agent_count`` / ``inbox_count`` ints,
  an optional ``alert`` string, and a ``width`` budget in chars.
- Outputs: a Rich markup ``str`` ready for inline rendering.
- Side effects: none.
- Allowed dependencies: stdlib + ``pollypm.cockpit_theme``.
"""

from __future__ import annotations

from pollypm.cockpit_theme import Glyph, State


# Bracket form used between the count chunk and the alert chunk so the
# alert reads as a distinct second column instead of "yet another · chip".
_SEPARATOR_MAJOR = "  |  "

# Inter-chunk separator inside the counts section. Matches the existing
# event-ticker affordance (`events · heartbeat error`) so the operator's
# eye already trains to "·" as a chunk break.
_SEPARATOR_MINOR = " · "  # " · "


def _format_count_chunk(value: int, label_singular: str, label_plural: str) -> str:
    """Compose a single ``<N> <noun>`` chunk."""
    noun = label_singular if value == 1 else label_plural
    return f"{value} {noun}"


def _color_for_inbox(inbox_count: int) -> str:
    """Inbox count gets the WAITING amber when non-empty, MUTED at rest.

    The whole point of the inbox count is to signal whether the operator
    has unread work; coloring it amber the moment ``inbox_count > 0``
    folds the rail's "(N)" badge affordance into the footer without
    needing a second glyph.
    """
    return State.WAITING if inbox_count > 0 else State.MUTED


def _wrap(markup: str, color: str) -> str:
    """Wrap ``markup`` in Rich-markup ``[color]…[/color]``.

    Centralised so the consumers (and tests) don't have to repeat the
    ``f"[{color}]{value}[/{color}]"`` pattern eight times.
    """
    return f"[{color}]{markup}[/{color}]"


def _truncate_alert(alert: str, budget: int) -> str:
    """Trim an alert string to fit ``budget`` chars, ellipsis on overflow.

    Returns ``""`` when ``budget <= 0`` so the caller can drop the alert
    chunk entirely on a very narrow rail. The output is guaranteed to
    satisfy ``len(result) <= budget`` — when overflow would force the
    ellipsis to consume the whole budget alone (``budget < 2``), the
    function returns ``""`` instead of a single ``…``.
    """
    if budget <= 0:
        return ""
    flat = " ".join(alert.split()).strip()
    if not flat:
        return ""
    if len(flat) <= budget:
        return flat
    # Overflow path: need ``head + "…"`` to fit in ``budget``.
    # That requires at least 1 char of body + 1 char of ellipsis.
    if budget < 2:
        return ""
    return flat[: budget - 1].rstrip() + "…"


def render_footer_status(
    *,
    project_count: int,
    agent_count: int,
    inbox_count: int,
    alert: str | None = None,
    width: int = 80,
) -> str:
    """Compose the unified footer status string with per-state color cues.

    Layout::

        <projects> · <agents> · <inbox>  |  <alert>

    Truncation rules (least to most aggressive):

    1. **Full**: width >= projects + agents + inbox + alert. Render all
       four chunks with separators + color.
    2. **Drop labels**: width below the full budget but enough to keep
       all four counts. Counts collapse to ``"12·38·23"`` (no spaces).
    3. **Drop alert truncation**: alert string gets ellipsis-trimmed so
       the count column always survives, since the heartbeat alert can
       be very long and counts are the more frequently-useful signal.
    4. **Drop counts**: only the alert renders, since it is the
       higher-priority signal. Falls out for very small ``width``.
    5. **Empty**: width < 1 or no chunks to render.

    Per-state colors (sourced from :mod:`pollypm.cockpit_theme`):

    - counts: :attr:`State.MUTED` (chrome / metadata)
    - inbox count, when non-zero: :attr:`State.WAITING` (amber)
    - alert glyph (``⚠``) + alert text: :attr:`State.BLOCKED` (red)
    - separators: :attr:`State.IDLE` (dim slate)
    """
    project_chunk = _format_count_chunk(project_count, "project", "projects")
    agent_chunk = _format_count_chunk(agent_count, "agent", "agents")
    inbox_chunk = _format_count_chunk(inbox_count, "inbox", "inbox")
    inbox_color = _color_for_inbox(inbox_count)

    alert_text = (alert or "").strip()
    has_alert = bool(alert_text)

    # ---- Pre-compute candidate renderings (plain text, sans markup) ----
    plain_full_counts = _SEPARATOR_MINOR.join(
        (project_chunk, agent_chunk, inbox_chunk)
    )
    plain_compact_counts = "·".join(  # "·" no spaces
        (str(project_count), str(agent_count), str(inbox_count))
    )
    plain_alert_prefix = f"{Glyph.STUCK} " if has_alert else ""

    # ---- Pick a layout based on width budget ----
    if width <= 0:
        return ""

    # Pick Layout 1 (full labels) when the full count chunk plus the
    # full alert (no truncation) fits inside ``width`` outright. As soon
    # as the alert would need to truncate to fit, switch to the compact
    # count form so the alert text stays as readable as possible.
    plain_alert_full = (
        plain_alert_prefix + " ".join(alert_text.split()) if has_alert else ""
    )
    full_layout_plain_len = (
        len(plain_full_counts)
        + (len(_SEPARATOR_MAJOR) + len(plain_alert_full) if has_alert else 0)
    )

    counts_markup: str
    if not has_alert or full_layout_plain_len <= width:
        # Layout 1 — full counts (with labels) fit alongside the full alert.
        counts_plain = plain_full_counts
        counts_markup = (
            _wrap(project_chunk, State.MUTED)
            + _wrap(_SEPARATOR_MINOR, State.IDLE)
            + _wrap(agent_chunk, State.MUTED)
            + _wrap(_SEPARATOR_MINOR, State.IDLE)
            + _wrap(inbox_chunk, inbox_color)
        )
    else:
        # Layout 2 — compact counts (drop labels) so the alert gets more
        # room. Uses sparing-of-spaces "·" join.
        counts_plain = plain_compact_counts
        counts_markup = (
            _wrap(str(project_count), State.MUTED)
            + _wrap("·", State.IDLE)
            + _wrap(str(agent_count), State.MUTED)
            + _wrap("·", State.IDLE)
            + _wrap(str(inbox_count), inbox_color)
        )

    if has_alert:
        # Reserve at least 4 chars of alert body so the alert isn't
        # truncated past the point of usefulness. Below that threshold
        # the operator gets more value from a clear "⚠ <first word>…"
        # alert-only line than from a "999·999·999  |  ⚠ he…" stub.
        _MIN_ALERT_BODY = 4
        remaining = width - len(counts_plain) - len(_SEPARATOR_MAJOR) - len(plain_alert_prefix)
        if remaining >= _MIN_ALERT_BODY:
            truncated_alert = _truncate_alert(alert_text, remaining)
            if truncated_alert:
                alert_markup = _wrap(
                    f"{Glyph.STUCK} {truncated_alert}",
                    State.BLOCKED,
                )
                return (
                    counts_markup
                    + _wrap(_SEPARATOR_MAJOR, State.IDLE)
                    + alert_markup
                )
        # Alert can't fit usefully alongside counts — promote to
        # alert-only so the more-actionable signal survives.
        alert_only_budget = max(0, width - len(plain_alert_prefix))
        truncated_alert = _truncate_alert(alert_text, alert_only_budget)
        if truncated_alert:
            return _wrap(f"{Glyph.STUCK} {truncated_alert}", State.BLOCKED)
        # Alert can't render at all (width too narrow even for the
        # glyph) — fall through to counts-only.

    if len(counts_plain) <= width:
        return counts_markup
    return ""


__all__ = ["render_footer_status"]
