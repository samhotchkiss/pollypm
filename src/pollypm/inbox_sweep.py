"""Inbox lifecycle sweeps — auto-archive stale notify items (#1013).

Pre-#1013 the unified ``messages`` store had no age-based archive policy
for ``notify``-type rows. Completion announcements ("X E2E complete",
"Y emitted", …) accumulated in ``pm inbox`` indefinitely — verified
inbox at 27 stale notifies + 12 unrelated meta-reports + 2 actionable
items, signal-to-noise <5%.

This module owns two cleanup paths:

1. :func:`sweep_stale_notifies` — close ``state='open'`` notify rows
   whose ``created_at`` is older than the configured retention window.
   Driven from the supervisor heartbeat tick so it runs on the same
   cadence as the alert sweeps. Default retention is 14 days, override
   via the ``POLLYPM_NOTIFY_RETENTION_DAYS`` env var (test seam) or by
   passing ``older_than`` directly.

2. :func:`sweep_notifies_for_done_task` — close any open notify whose
   structured ``payload.task_id`` references a task that has
   transitioned to ``done`` / ``archived`` (or whose project was
   deregistered). The notify carries ``payload.task_id`` already
   (writer at :mod:`cli_features.session_runtime` stamps it after the
   work-service task is created), so no schema migration is needed.

Both helpers are best-effort — failures are logged and swallowed so a
broken sweep can't break the heartbeat tick for live sessions.

The inbox-pinning predicate is intentionally minimal: a notify is
"pinned" if its ``labels`` contains the literal string ``pinned``.
Anything explicitly pinned by the operator is exempt from the sweep.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


# Default retention window for ``notify``-type messages. 14 days matches
# the issue's suggested default (#1013). Tunable via
# ``POLLYPM_NOTIFY_RETENTION_DAYS`` so operator scripts and tests can
# override without touching code.
DEFAULT_NOTIFY_RETENTION_DAYS = 14

# Label literal that exempts a notify from the age-based sweep. Keeping
# this here rather than threading it through the API so callers don't
# need to know the implementation detail.
_PINNED_LABEL = "pinned"


def _retention_days() -> int:
    """Read the retention window from env, falling back to the default.

    Centralised so callers + tests share one tunable surface.
    """
    raw = os.environ.get("POLLYPM_NOTIFY_RETENTION_DAYS")
    if not raw:
        return DEFAULT_NOTIFY_RETENTION_DAYS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "POLLYPM_NOTIFY_RETENTION_DAYS=%r is not an int; using %d",
            raw, DEFAULT_NOTIFY_RETENTION_DAYS,
        )
        return DEFAULT_NOTIFY_RETENTION_DAYS
    if value <= 0:
        logger.warning(
            "POLLYPM_NOTIFY_RETENTION_DAYS=%d must be > 0; using %d",
            value, DEFAULT_NOTIFY_RETENTION_DAYS,
        )
        return DEFAULT_NOTIFY_RETENTION_DAYS
    return value


def _row_labels(row: dict[str, Any]) -> list[str]:
    """Coerce a messages-row ``labels`` field to a list of strings.

    ``query_messages`` decodes the JSON for us, but a corrupt row could
    surface a non-list — defend against it so the sweep degrades
    gracefully instead of TypeError-ing on a single bad row.
    """
    raw = row.get("labels")
    if isinstance(raw, list):
        return [str(label) for label in raw]
    return []


def _row_created_at(row: dict[str, Any]) -> datetime | None:
    """Best-effort parse of a messages-row ``created_at`` value.

    Returns ``None`` when the value is missing or unparseable; the
    sweep then leaves the row alone (refusing to delete on ambiguous
    timestamps is the safer default).
    """
    value = row.get("created_at")
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def sweep_stale_notifies(
    store,
    *,
    older_than: datetime | None = None,
    now: datetime | None = None,
) -> int:
    """Archive ``state='open'`` notifies older than the retention window.

    ``store`` is any object exposing ``query_messages(**filters)`` and
    ``close_message(id)`` — the production :class:`SQLAlchemyStore`
    plus any test double satisfies the protocol.

    Returns the number of rows archived. Best-effort: per-row failures
    are logged and skipped; the function only re-raises if the initial
    query itself blows up.

    Pinned notifies (``labels`` contains ``"pinned"``) are exempt.
    """
    reference = now or datetime.now(timezone.utc)
    cutoff = older_than or (reference - timedelta(days=_retention_days()))

    try:
        rows = store.query_messages(
            type="notify", state="open", recipient="user",
        )
    except Exception:  # noqa: BLE001 — sweep must not break heartbeat
        logger.warning("notify-sweep query failed", exc_info=True)
        return 0

    archived = 0
    for row in rows:
        labels = _row_labels(row)
        if _PINNED_LABEL in labels:
            continue
        created_at = _row_created_at(row)
        if created_at is None or created_at >= cutoff:
            continue
        msg_id = row.get("id")
        if msg_id is None:
            continue
        try:
            store.close_message(int(msg_id))
            archived += 1
        except Exception:  # noqa: BLE001 — per-row failure is logged
            logger.warning(
                "notify-sweep close_message(%r) failed", msg_id, exc_info=True,
            )
    if archived:
        word = "notify" if archived == 1 else "notifies"
        logger.info("notify-sweep archived %d stale %s", archived, word)
    return archived


def sweep_notifies_for_done_task(
    store,
    task_id: str,
) -> int:
    """Archive any open notify referencing ``task_id`` in its payload.

    Called from the work-service transition path when a task moves to
    ``done`` / ``archived`` — the notify that announced "Plan ready
    for review" or "Done: X complete" no longer represents an actionable
    state, so closing it keeps the inbox aligned with the work surface.

    Returns the number of notifies archived.
    """
    if not task_id:
        return 0
    try:
        rows = store.query_messages(
            type="notify", state="open", recipient="user",
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "related-task notify sweep failed for %s", task_id, exc_info=True,
        )
        return 0

    archived = 0
    for row in rows:
        payload = row.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if payload.get("task_id") != task_id:
            continue
        msg_id = row.get("id")
        if msg_id is None:
            continue
        try:
            store.close_message(int(msg_id))
            archived += 1
        except Exception:  # noqa: BLE001
            logger.warning(
                "related-task notify close_message(%r) failed",
                msg_id, exc_info=True,
            )
    if archived:
        word = "notify" if archived == 1 else "notifies"
        logger.info(
            "related-task notify sweep archived %d %s for %s",
            archived, word, task_id,
        )
    return archived


__all__ = [
    "DEFAULT_NOTIFY_RETENTION_DAYS",
    "sweep_notifies_for_done_task",
    "sweep_stale_notifies",
]
