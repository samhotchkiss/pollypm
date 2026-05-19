"""Renderer for the per-project Advisor settings tab.

Contract:
- Inputs: the advisor ``base_dir`` (resolved from config) and the
  cockpit's current ``project_key``.
- Outputs: a single multi-line string ready to drop into a ``Static``
  widget — newest event first, capped to ``limit`` rows.
- Side effects: none beyond the read performed by the advisor's own
  history facade.

The advisor stores its emit/silent decisions in
``<base_dir>/advisor-log.jsonl`` (one JSON object per line). We reuse
``recent_entries_for_project`` from the advisor plugin so the renderer
is purely a presentation layer — it never opens the file itself, never
mutates the schema, and stays robust to malformed lines (the facade
already skips them).

Each row renders as ``HH:MM:SS  event_type  summary`` where
``event_type`` is ``emit/<topic>``, ``emit``, or ``silent`` — chosen so
the row is short enough to scan without wrapping. Summary falls back to
the silent rationale when ``decision == "silent"``.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pollypm.plugins_builtin.advisor.handlers.history_log import (
    HistoryEntry,
    recent_entries_for_project,
)


_EMPTY_STATE = "No advisor events yet for this project."


def _format_timestamp(raw: str) -> str:
    """Render the entry timestamp as ``HH:MM:SS``.

    Falls back to ``--:--:--`` for malformed values so the column stays
    aligned. The advisor stores timestamps as ISO-8601 UTC strings, but
    we tolerate naive inputs by treating them as UTC unchanged — the
    Advisor tab is a read-only audit view, so timezone fidelity matters
    less than the row staying readable.
    """
    if not raw:
        return "--:--:--"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return "--:--:--"
    return dt.strftime("%H:%M:%S")


def _event_type(entry: HistoryEntry) -> str:
    """Render the entry's decision as a compact event-type token."""
    if entry.decision == "emit":
        if entry.topic:
            return f"emit/{entry.topic}"
        return "emit"
    return "silent"


def _summary_text(entry: HistoryEntry) -> str:
    """Pick the most informative one-liner from the entry."""
    if entry.decision == "emit":
        return entry.summary or "(no summary)"
    return entry.rationale_if_silent or entry.summary or "(no rationale)"


def format_entry_line(entry: HistoryEntry) -> str:
    """One row: ``HH:MM:SS  event_type  summary``."""
    when = _format_timestamp(entry.timestamp)
    return f"{when}  {_event_type(entry)}  {_summary_text(entry)}"


def render_advisor_log_lines(
    *,
    base_dir: Path,
    project_key: str,
    limit: int = 200,
) -> str:
    """Return the formatted advisor log text for ``project_key``.

    Newest event first, capped to ``limit`` rows. Empty result returns
    the empty-state message so the caller can update the widget with
    a single ``.update()`` call regardless of whether the log has
    entries.
    """
    entries = recent_entries_for_project(
        Path(base_dir), project_key, limit=limit,
    )
    if not entries:
        return _EMPTY_STATE
    # ``recent_entries_for_project`` returns chronological order (oldest
    # → newest); reverse for the UI so the most recent decision shows
    # at the top of the scroll.
    lines = [format_entry_line(e) for e in reversed(entries)]
    return "\n".join(lines)


__all__ = [
    "format_entry_line",
    "render_advisor_log_lines",
]
