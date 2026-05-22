"""Shared snooze-marker predicate for inbox surfaces (#2060).

The POST ``/api/v1/inbox/{id}/snooze`` writer persists snooze state as
an ``entry_type='snooze'`` row whose text starts with a structured
``until_iso=<ISO>`` marker. Multiple consumers (the API list path
today, the cockpit inbox panel tomorrow) need to filter "still
snoozed" items out of the default view. Without a shared parser the
two surfaces would drift on what counts as snoozed and a future regex
tweak in one place would silently desync the other.

This module owns the two small helpers both surfaces rely on:

- :func:`parse_snooze_until` — pull a tz-aware wake time out of a
  snooze context entry's text (handles both the canonical
  ``until_iso=`` marker and the older ``snoozed until <iso>`` shape
  so legacy rows still parse).
- :func:`is_snooze_active` — given an entry text and a reference
  ``now``, return whether the wake time is still in the future.

Lives in ``pollypm.work`` rather than ``pollypm.web_api`` so cockpit
code (which must not depend on the web layer) can import it without
pulling FastAPI into its dependency closure.
"""

from __future__ import annotations

from datetime import datetime, timezone


__all__ = ["parse_snooze_until", "is_snooze_active"]


def parse_snooze_until(text: str) -> datetime | None:
    """Pull a tz-aware wake time out of a snooze context-entry text.

    Accepts both the canonical ``until_iso=<ISO>`` marker the snooze
    writer emits and the older ``snoozed until <ISO>`` shape so rows
    written before the structured marker landed still parse. Returns
    ``None`` when no recognisable timestamp is present.
    """
    if not text:
        return None
    candidates: list[str] = []
    for part in text.split(";"):
        chunk = part.strip()
        if chunk.startswith("until_iso="):
            candidates.append(chunk[len("until_iso="):].strip())
        elif chunk.lower().startswith("snoozed until "):
            candidates.append(chunk[len("snoozed until "):].strip())
    for raw in candidates:
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


def is_snooze_active(text: str, *, now: datetime) -> bool:
    """Return True when ``text`` carries a wake time still in the future.

    Convenience wrapper around :func:`parse_snooze_until` for the
    common predicate "should this inbox row be hidden?" — keeps both
    surfaces from re-implementing the wake-time comparison.
    """
    wake = parse_snooze_until(text)
    if wake is None:
        return False
    # ``now`` should already be tz-aware (UTC); be defensive in case a
    # caller passes a naive datetime so the comparison doesn't blow up
    # on a single misuse.
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return wake > now
