"""Shared activity-feed contracts consumed by core + the activity_feed plugin (#1363).

Core modules (``cockpit_activity``, ``cockpit``) previously had to lazy-import
``pollypm.plugins_builtin.activity_feed.cockpit.feed_panel`` to format relative
timestamps and render entry detail views. That coupling makes the
``activity_feed`` plugin effectively non-optional — disabling it would break
core paths silently.

This module hosts:

* :class:`ActivityFeedEntry` — a ``Protocol`` describing the shape every entry
  rendered by the cockpit needs (id / timestamp / kind / actor / summary /
  payload / …). The plugin's concrete ``FeedEntry`` dataclass satisfies it
  structurally so nothing in the projection path has to change.
* :func:`format_relative_time` — pure string-in/string-out helper. No plugin
  coupling, no projector dependency.
* :func:`render_entry_detail` — entry-detail plain-text renderer. Takes the
  protocol shape, so the cockpit can render any source of feed entries
  without importing from ``plugins_builtin``.
* :func:`format_entry_row` / :func:`compute_project_column_width` /
  :func:`render_entries_as_text` — multi-row plain-text renderers, also pure.
* :func:`render_activity_feed_text` — config-in/text-out shim used by the
  cockpit's static-pane fallback. Resolves the projector through
  :mod:`pollypm.activity_projector_registry` so core never reaches into the
  optional plugin tree.

The plugin re-exports these names from ``feed_panel`` so existing callers
keep working without churn.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, Iterable, Protocol, runtime_checkable


logger = logging.getLogger(__name__)


@runtime_checkable
class ActivityFeedEntry(Protocol):
    """Structural shape of a single activity-feed row.

    Mirrors the fields on the plugin's ``FeedEntry`` dataclass so the
    concrete class satisfies this protocol without modification. Core
    renderers only read these attributes — nothing constructs entries
    through this protocol.
    """

    id: str
    timestamp: str
    project: str | None
    kind: str
    actor: str
    subject: str | None
    verb: str
    summary: str
    severity: str
    payload: dict[str, Any]
    pinned: bool
    source: str


def format_relative_time(
    timestamp: str, *, now: datetime | None = None,
) -> str:
    """Return a relative-time label (``"3m ago"``, ``"2h ago"``).

    ``timestamp`` is an ISO-8601 string (the projector always produces
    these). Unparseable values fall back to the raw string. ``now`` is
    injectable for deterministic tests.
    """
    if not timestamp:
        return "—"
    try:
        when = datetime.fromisoformat(timestamp)
    except ValueError:
        return timestamp
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    anchor = now or datetime.now(UTC)
    delta = anchor - when
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def render_entry_detail(
    entry: ActivityFeedEntry, *, now: datetime | None = None,
) -> str:
    """Render the per-entry detail view as plain text (lf04).

    Shows absolute + relative timestamps, project / actor / verb,
    severity, a pretty-printed payload JSON, and suggested follow-up
    navigation (task / session links) inferred from the payload.
    """
    lines: list[str] = []
    abs_ts = entry.timestamp
    rel_ts = format_relative_time(entry.timestamp, now=now)
    lines.append(f"Activity entry · {entry.id}")
    lines.append("")
    lines.append(f"When: {abs_ts}  ({rel_ts})")
    lines.append(f"Kind: {entry.kind}")
    if entry.project:
        lines.append(f"Project: {entry.project}")
    lines.append(f"Actor: {entry.actor}")
    if entry.subject:
        lines.append(f"Subject: {entry.subject}")
    lines.append(f"Verb: {entry.verb}")
    lines.append(f"Severity: {entry.severity}")
    if entry.pinned:
        lines.append("Pinned: yes")
    lines.append("")
    lines.append("Summary:")
    lines.append(f"  {entry.summary}")
    # Navigation hints — task / session links inferred from the payload.
    task_project = entry.payload.get("task_project")
    task_number = entry.payload.get("task_number")
    if task_project and task_number is not None:
        lines.append("")
        lines.append(
            f"Related task: project:{task_project}:task:{task_number}  "
            f"(use the rail to open)"
        )
    elif entry.source == "work_transitions" and entry.subject:
        lines.append("")
        lines.append(f"Related task: {entry.subject}")
    if entry.actor and entry.source == "events":
        lines.append("")
        lines.append(f"Related session: {entry.actor}")
    # Payload dump (pretty-printed).
    lines.append("")
    lines.append("Payload:")
    try:
        rendered = json.dumps(entry.payload, indent=2, sort_keys=True, default=str)
    except (TypeError, ValueError):
        rendered = repr(entry.payload)
    for line in rendered.splitlines():
        lines.append(f"  {line}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Multi-row plain-text renderers — used by the cockpit's static-pane fallback
# and ``pm activity``. Pure helpers (no plugin / projector dependency).
# ---------------------------------------------------------------------------


def _project_label(entry: ActivityFeedEntry) -> str:
    """Project key as displayed in the feed (``"-"`` for empty).

    Centralised so the auto-fit width math and :func:`format_entry_row`
    agree on the same string. Empty / ``None`` falls back to ``"-"`` so
    an unknown project still aligns in the column.
    """
    return entry.project or "-"


def compute_project_column_width(
    entries: Iterable[ActivityFeedEntry], *, minimum: int = 1,
) -> int:
    """Width of the widest project key in ``entries`` (auto-fit).

    Used by :func:`format_entry_row` so the project column expands to
    fit long keys like ``blackjack-trainer`` while keeping every row
    aligned. Empty input falls back to ``minimum`` so a header-only
    render still produces a sane column. See #929.
    """
    widest = minimum
    for entry in entries:
        widest = max(widest, len(_project_label(entry)))
    return widest


def format_entry_row(
    entry: ActivityFeedEntry,
    *,
    now: datetime | None = None,
    project_width: int | None = None,
) -> str:
    """Render one feed entry as a single plain-text row.

    Layout:: ``[rel] [project] [actor] verb summary``. Severity is not
    encoded here — the Textual renderer applies colour; the plain-text
    renderer prefixes a ``!`` on critical entries so ``pm activity`` can
    stay unstyled but still draw attention.

    ``project_width`` left-pads the project key inside the brackets so
    multi-row renders align even when project keys differ in length
    (auto-fit; see :func:`compute_project_column_width` and #929). When
    ``None`` the column is sized to the project key itself — the
    single-row default that preserves the historical layout.
    """
    rel = format_relative_time(entry.timestamp, now=now)
    project = _project_label(entry)
    actor = entry.actor or "system"
    verb = entry.verb or entry.kind
    # #1033: distinguish create vs clear in the alert lifecycle so a
    # quick scan tells the user which way the row is pointing. Only
    # rewrite the verb when the emitter didn't supply a richer one
    # (e.g. ``activity_summary(verb='alerted', ...)``) so structured
    # events keep their authored verb intact.
    if entry.kind == "alert" and verb in ("alert", entry.kind):
        verb = "alert↑"
    elif entry.kind == "alert.cleared" and verb in ("alert.cleared", entry.kind):
        verb = "alert↓"
    prefix = "!" if entry.severity == "critical" else " "
    pin = "\U0001f4cc " if entry.pinned else ""
    width = max(project_width or len(project), len(project))
    project_cell = f"{project:<{width}}"
    return f"{prefix} {rel:>8}  [{project_cell}]  [{actor}]  {verb}: {pin}{entry.summary}"


def render_entries_as_text(entries: Iterable[ActivityFeedEntry]) -> str:
    """Render a list of feed entries as multi-line plain text.

    Empty input renders a friendly placeholder so the cockpit panel
    doesn't look broken on a brand-new install. The project column
    auto-fits to the widest key in the batch so long keys like
    ``blackjack-trainer`` aren't visually clipped (see #929).
    """
    materialised = list(entries)
    if not materialised:
        return (
            "No activity yet.\n\n"
            "Events accumulate as sessions start, tasks transition, "
            "and heartbeats fire. Check back after the next sweep."
        )
    width = compute_project_column_width(materialised)
    rows = [format_entry_row(e, project_width=width) for e in materialised]
    return "\n".join(rows)


def render_activity_feed_text(config: Any, *, limit: int = 50) -> str:
    """Render the feed as plain text for the cockpit's static pane path.

    Resolves the projector through
    :func:`pollypm.activity_projector_registry.build_activity_projector`
    so core never reaches into the optional ``activity_feed`` plugin.
    Missing config or missing factory yields the same friendly
    placeholder as an empty feed.
    """
    header = "Activity Feed"
    # Import lazily so the protocol module stays usable in environments
    # where the registry hasn't been wired (test harnesses, doctor probes).
    from pollypm.activity_projector_registry import build_activity_projector

    projector = build_activity_projector(config)
    if projector is None:
        return f"{header}\n\nNo state store configured — nothing to show yet."
    try:
        entries = projector.project(limit=limit)
    except Exception:  # noqa: BLE001
        logger.exception("activity_feed: projection failed for text render")
        return f"{header}\n\nFailed to read activity events."
    return f"{header}\n\n{render_entries_as_text(entries)}"


__all__ = [
    "ActivityFeedEntry",
    "compute_project_column_width",
    "format_entry_row",
    "format_relative_time",
    "render_activity_feed_text",
    "render_entries_as_text",
    "render_entry_detail",
]
