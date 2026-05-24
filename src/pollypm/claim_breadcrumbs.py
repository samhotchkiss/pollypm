"""Shared claim breadcrumb vocabulary and render helpers."""

from __future__ import annotations

from datetime import datetime
from typing import Any

CLAIM_WON_BY = "claim.won_by"
CLAIM_ATTEMPTED_BY_LOSER = "claim.attempted_by_loser"
CLAIM_ALREADY_CLAIMED_REASON = "already_claimed"


def _clean(value: object | None) -> str:
    return str(value or "").strip()


def _add_field(parts: list[str], name: str, value: object | None) -> None:
    text = _clean(value)
    if text:
        parts.append(f"{name}={text}")


def build_claim_breadcrumb_text(
    *,
    event_type: str,
    task_id: str,
    actor: str,
    session: str | None,
    reason: str | None = None,
    assignee: str | None = None,
    winner_session: str | None = None,
) -> str:
    """Render a stable, grep-friendly task context breadcrumb."""
    parts = [event_type]
    _add_field(parts, "task_id", task_id)
    _add_field(parts, "actor", actor)
    _add_field(parts, "session", session or actor)
    _add_field(parts, "reason", reason)
    _add_field(parts, "assignee", assignee)
    _add_field(parts, "winner_session", winner_session)
    return " ".join(parts)


def build_claim_breadcrumb_metadata(
    *,
    task_id: str,
    actor: str,
    session: str | None,
    timestamp: datetime | None = None,
    reason: str | None = None,
    assignee: str | None = None,
    winner_session: str | None = None,
) -> dict[str, Any]:
    """Structured counterpart to the context breadcrumb text."""
    metadata: dict[str, Any] = {
        "task_id": task_id,
        "actor": actor,
        "session": session or actor,
    }
    if timestamp is not None:
        metadata["timestamp"] = timestamp.isoformat()
    if reason:
        metadata["reason"] = reason
    if assignee:
        metadata["assignee"] = assignee
    if winner_session:
        metadata["winner_session"] = winner_session
    return metadata


__all__ = [
    "CLAIM_ALREADY_CLAIMED_REASON",
    "CLAIM_ATTEMPTED_BY_LOSER",
    "CLAIM_WON_BY",
    "build_claim_breadcrumb_metadata",
    "build_claim_breadcrumb_text",
]
