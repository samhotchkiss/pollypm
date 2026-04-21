"""Helpers for reusing recent heartbeat pane snapshots across the UI."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from pollypm.storage.state import HeartbeatRecord

RECENT_HEARTBEAT_MAX_AGE_SECONDS = 30


def _heartbeat_created_at(record: HeartbeatRecord | None) -> datetime | None:
    if record is None:
        return None
    try:
        created_at = datetime.fromisoformat(record.created_at)
    except (TypeError, ValueError):
        return None
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return created_at


def read_recent_heartbeat_snapshot(
    record: HeartbeatRecord | None,
    *,
    max_age_seconds: int = RECENT_HEARTBEAT_MAX_AGE_SECONDS,
) -> str | None:
    """Return a recent heartbeat snapshot, or ``None`` when it is stale."""
    if record is None or not record.snapshot_path:
        return None
    created_at = _heartbeat_created_at(record)
    if created_at is None:
        return None
    if datetime.now(UTC) - created_at > timedelta(seconds=max_age_seconds):
        return None
    try:
        return Path(record.snapshot_path).read_text(errors="ignore")
    except (FileNotFoundError, OSError):
        return None
