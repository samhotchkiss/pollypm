"""Structured-summary helpers for event emission (lf02).

The event-emission sites across the codebase call
``StateStore.record_event(session_name, event_type, message)``. The
schema's ``message`` column is TEXT, so the ``activity_events`` view
ships whatever is written there. Per the feed spec §4, new emission
sites should pack a JSON blob into ``message`` carrying at least
``summary`` + ``severity``; the projector (see
``handlers/event_projector.py``) decodes that blob back out.

This module centralises the packing so emission sites stay readable::

    from pollypm.plugins_builtin.activity_feed.summaries import activity_summary

    store.record_event(
        session_name,
        "alert",
        activity_summary(
            summary=f"Raised {severity} alert {alert_type}: {message}",
            severity="critical" if severity == "critical" else "recommendation",
            verb="alerted",
            subject=alert_type,
            project=project_key,
        ),
    )

Back-compat: plain-string messages remain valid — the projector falls
back to kind+actor rendering, per spec §4. Nothing in the current data
migration requires rewriting old rows.
"""

from __future__ import annotations

import json
from typing import Any


_KNOWN_SEVERITIES: frozenset[str] = frozenset({"critical", "recommendation", "routine"})


def activity_summary(
    *,
    summary: str,
    severity: str = "routine",
    verb: str | None = None,
    subject: str | None = None,
    project: str | None = None,
    **extra: Any,
) -> str:
    """Serialise a structured activity payload to a JSON string.

    Fields:

    * ``summary`` — one-sentence human-readable description (required).
    * ``severity`` — ``critical``/``recommendation``/``routine``.
      Unknown values are accepted but coerced to ``routine`` by the
      projector.
    * ``verb`` — short past-tense verb (``started``, ``committed``,
      ``blocked``). Optional; the projector falls back to ``event_type``.
    * ``subject`` — the thing the event is about (``task demo/5``,
      ``session worker-foo``). Optional.
    * ``project`` — project key for filtering in the cockpit.
    * ``**extra`` — additional structured fields preserved on the
      feed entry's ``payload`` dict.
    """
    body: dict[str, Any] = {"summary": str(summary)}
    if severity in _KNOWN_SEVERITIES:
        body["severity"] = severity
    else:
        body["severity"] = "routine"
    if verb:
        body["verb"] = str(verb)
    if subject:
        body["subject"] = str(subject)
    if project:
        body["project"] = str(project)
    for key, value in extra.items():
        if value is None:
            continue
        # JSON-serialisable scalars + small containers only. Objects
        # outside that pass through json.dumps' default handling, which
        # raises for non-serialisable values — callers should not pass
        # those in.
        body[key] = value
    return json.dumps(body, separators=(",", ":"))


__all__ = ["activity_summary"]
