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

The plugin re-exports these names from ``feed_panel`` so existing callers
keep working without churn.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable


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


__all__ = [
    "ActivityFeedEntry",
    "format_relative_time",
    "render_entry_detail",
]
