"""Notification staging ownership for the SQLite work service.

Contract:
- Inputs: a ``SQLiteWorkService`` plus typed notification metadata.
- Outputs: staged notification ids, typed digest candidates, and prune
  summaries.
- Side effects: writes the legacy ``notification_staging`` table and
  closes staged rows in the unified ``messages`` table.
- Invariants: callers never need ``svc._conn`` or ad-hoc DB URL
  reconstruction to manage staged digest state.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pollypm.store import SQLAlchemyStore
from pollypm.store.registry import get_store_by_url
from pollypm.work.models import DigestRollupCandidate

if TYPE_CHECKING:
    from pollypm.work.sqlite_service import SQLiteWorkService


def _ensure_staging_table(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS notification_staging (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            actor TEXT NOT NULL,
            priority TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            milestone_key TEXT,
            created_at TEXT NOT NULL,
            flushed_at TEXT,
            rollup_task_id TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_notification_staging_pending
            ON notification_staging(project, milestone_key, flushed_at);
        CREATE INDEX IF NOT EXISTS idx_notification_staging_created
            ON notification_staging(created_at);
        """
    )


def _message_store(service: "SQLiteWorkService") -> SQLAlchemyStore:
    return get_store_by_url(f"sqlite:///{service._db_path}")  # type: ignore[return-value]


def stage_notification_row(
    service: "SQLiteWorkService",
    *,
    project: str,
    subject: str,
    body: str,
    actor: str,
    priority: str,
    milestone_key: str | None,
    payload: dict[str, Any] | None = None,
) -> int:
    assert priority in {"digest", "silent"}, (
        f"stage_notification called with non-stageable priority {priority!r}"
    )
    _ensure_staging_table(service._conn)
    payload_dict = dict(payload or {})
    payload_dict.setdefault("subject", subject)
    payload_dict.setdefault("body", body)
    payload_dict.setdefault("actor", actor)
    payload_dict.setdefault("project", project)
    cur = service._conn.execute(
        "INSERT INTO notification_staging "
        "(project, subject, body, actor, priority, payload_json, "
        "milestone_key, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            project,
            subject,
            body,
            actor,
            priority,
            json.dumps(payload_dict, separators=(",", ":"), default=str),
            milestone_key,
            datetime.now(UTC).isoformat(),
        ),
    )
    service._conn.commit()
    return int(cur.lastrowid or 0)


def list_digest_rollup_candidates(
    service: "SQLiteWorkService",
    *,
    project: str,
    milestone_key: str | None,
) -> list[DigestRollupCandidate]:
    _ensure_staging_table(service._conn)
    if milestone_key is None:
        legacy_rows = service._conn.execute(
            "SELECT * FROM notification_staging "
            "WHERE project = ? AND milestone_key IS NULL "
            "AND flushed_at IS NULL AND priority = 'digest' "
            "ORDER BY created_at, id",
            (project,),
        ).fetchall()
    else:
        legacy_rows = service._conn.execute(
            "SELECT * FROM notification_staging "
            "WHERE project = ? AND milestone_key = ? "
            "AND flushed_at IS NULL AND priority = 'digest' "
            "ORDER BY created_at, id",
            (project, milestone_key),
        ).fetchall()

    merged = [
        DigestRollupCandidate(
            source="legacy",
            row_id=int(row["id"]),
            subject=row["subject"] or "",
            body=row["body"] or "",
            actor=row["actor"] or "polly",
            created_at=row["created_at"] or "",
            payload=json.loads(row["payload_json"] or "{}"),
        )
        for row in legacy_rows
    ]

    store = _message_store(service)
    try:
        message_rows = store.query_messages(
            type="notify",
            tier="digest",
            state="staged",
            scope=project,
        )
    finally:
        store.close()

    for row in message_rows:
        payload = row.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        if payload.get("milestone_key") != milestone_key:
            continue
        merged.append(
            DigestRollupCandidate(
                source="messages",
                row_id=int(row.get("id") or 0),
                subject=row.get("subject") or "",
                body=row.get("body") or "",
                actor=str(payload.get("actor") or row.get("sender") or "polly"),
                created_at=str(row.get("created_at") or ""),
                payload=payload,
            )
        )

    merged.sort(key=lambda row: (row.created_at, row.row_id))
    return merged


def mark_rollup_candidates_flushed(
    service: "SQLiteWorkService",
    candidates: list[DigestRollupCandidate],
    *,
    rollup_task_id: str,
    flushed_at: str,
) -> None:
    legacy_ids = [row.row_id for row in candidates if row.source == "legacy"]
    if legacy_ids:
        placeholders = ",".join("?" for _ in legacy_ids)
        service._conn.execute(
            f"UPDATE notification_staging SET flushed_at = ?, rollup_task_id = ? "
            f"WHERE id IN ({placeholders})",
            [flushed_at, rollup_task_id, *legacy_ids],
        )
        service._conn.commit()

    message_ids = [row.row_id for row in candidates if row.source == "messages"]
    if not message_ids:
        return
    store = _message_store(service)
    try:
        for msg_id in message_ids:
            store.close_message(msg_id)
    finally:
        store.close()


def has_old_pending_digest_rows(
    service: "SQLiteWorkService",
    *,
    project: str,
    milestone_key: str | None,
    min_age_seconds: int,
) -> bool:
    _ensure_staging_table(service._conn)
    cutoff = (datetime.now(UTC) - timedelta(seconds=min_age_seconds)).isoformat()
    if milestone_key is None:
        row = service._conn.execute(
            "SELECT COUNT(*) AS n FROM notification_staging "
            "WHERE project = ? AND milestone_key IS NULL AND flushed_at IS NULL "
            "AND priority = 'digest' AND created_at <= ?",
            (project, cutoff),
        ).fetchone()
    else:
        row = service._conn.execute(
            "SELECT COUNT(*) AS n FROM notification_staging "
            "WHERE project = ? AND milestone_key = ? AND flushed_at IS NULL "
            "AND priority = 'digest' AND created_at <= ?",
            (project, milestone_key, cutoff),
        ).fetchone()
    return bool(row and row[0])


def find_flushed_rollup_milestone(
    service: "SQLiteWorkService",
    *,
    task_id: str,
) -> str | None:
    _ensure_staging_table(service._conn)
    rows = service._conn.execute(
        "SELECT milestone_key, payload_json FROM notification_staging "
        "WHERE flushed_at IS NOT NULL "
        "ORDER BY flushed_at DESC LIMIT 500",
    ).fetchall()
    needle = f'"task_id": "{task_id}"'
    needle_compact = f'"task_id":"{task_id}"'
    for row in rows:
        payload = row[1] or ""
        if needle in payload or needle_compact in payload or task_id in payload:
            return row[0]
    return None


def prune_staged_notifications(
    service: "SQLiteWorkService",
    *,
    retain_days: int = 30,
) -> dict[str, int]:
    _ensure_staging_table(service._conn)
    cutoff = (datetime.now(UTC) - timedelta(days=retain_days)).isoformat()

    cur = service._conn.execute(
        "DELETE FROM notification_staging "
        "WHERE flushed_at IS NOT NULL AND flushed_at <= ?",
        (cutoff,),
    )
    flushed_deleted = cur.rowcount or 0

    cur = service._conn.execute(
        "DELETE FROM notification_staging "
        "WHERE priority = 'silent' AND created_at <= ?",
        (cutoff,),
    )
    silent_deleted = cur.rowcount or 0

    service._conn.commit()
    return {
        "flushed_pruned": int(flushed_deleted),
        "silent_pruned": int(silent_deleted),
    }
