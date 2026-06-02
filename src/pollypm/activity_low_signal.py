"""Shared low-signal activity classification for operator-facing feeds."""

from __future__ import annotations

from typing import Any


LOW_SIGNAL_ACTIVITY_KINDS: frozenset[str] = frozenset(
    {
        "heartbeat",
        "lease",
        "lease_override",
        "scheduled",
        "session.pause.skip",
        "token_ledger",
        "work_db.opened",
    }
)
LOW_SIGNAL_EMPTY_ACTIVITY_KINDS: frozenset[str] = frozenset({"launch"})


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


def activity_summary_has_content(
    *,
    kind: Any = None,
    verb: Any = None,
    actor: Any = None,
    summary: Any = None,
) -> bool:
    summary_text = _lower(summary)
    if not summary_text:
        return False
    kind_text = _lower(kind)
    verb_text = _lower(verb)
    actor_text = _lower(actor)
    return summary_text not in {
        kind_text,
        verb_text,
        actor_text,
        f"{kind_text} on {actor_text}",
    }


def is_low_signal_activity(
    *,
    kind: Any = None,
    verb: Any = None,
    actor: Any = None,
    summary: Any = None,
) -> bool:
    kind_text = _lower(kind)
    verb_text = _lower(verb)
    if (
        kind_text in LOW_SIGNAL_ACTIVITY_KINDS
        or verb_text in LOW_SIGNAL_ACTIVITY_KINDS
    ):
        return True
    actor_text = _lower(actor)
    if (
        actor_text in {"operator", "scheduler"}
        and (
            kind_text in LOW_SIGNAL_EMPTY_ACTIVITY_KINDS
            or verb_text in LOW_SIGNAL_EMPTY_ACTIVITY_KINDS
        )
        and not activity_summary_has_content(
            kind=kind_text,
            verb=verb_text,
            actor=actor_text,
            summary=summary,
        )
    ):
        return True
    return False


def is_noise_type_filter(kind: Any = None) -> bool:
    lowered = _lower(kind)
    return (
        lowered in LOW_SIGNAL_ACTIVITY_KINDS
        or lowered in LOW_SIGNAL_EMPTY_ACTIVITY_KINDS
    )


__all__ = [
    "LOW_SIGNAL_ACTIVITY_KINDS",
    "LOW_SIGNAL_EMPTY_ACTIVITY_KINDS",
    "activity_summary_has_content",
    "is_low_signal_activity",
    "is_noise_type_filter",
]
