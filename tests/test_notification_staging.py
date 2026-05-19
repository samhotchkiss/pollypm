"""Pg-backed tests for :mod:`pollypm.notification_staging` (Slice K-misc, #1737).

These tests cover the public surface of the standalone module after the
sqlite → psycopg port:

* :func:`stage_notification` / :func:`list_pending` round-trip
* :func:`prune_old_staging` retention semantics

The work-service-facing helpers (``flush_milestone_digest``,
``check_and_flush_on_done``, ``check_regression_on_reopen``) are
covered by the in-memory mocks in
:mod:`tests.test_inbox_kind_emit_sites` — they don't touch a DB
directly, so they're backend-agnostic and don't need a pg fixture.

The legacy ``tests/test_notification_tiering.py`` still exercises the
sqlite path via the CLI ``--db`` flag; that file belongs to the
K-deletion sweep (separate PR) which collapses the sqlite stack
entirely.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest


pytestmark = pytest.mark.usefixtures("pg_schema_pool")


def _row_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM notification_staging")
        return int(cur.fetchone()[0])


def _all_rows(conn) -> list[dict]:
    from psycopg.rows import dict_row

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM notification_staging ORDER BY id")
        return list(cur.fetchall())


# ---------------------------------------------------------------------------
# _ensure_staging_table — idempotent on pg
# ---------------------------------------------------------------------------


def test_ensure_staging_table_creates_pg_shape(pg_schema_pool) -> None:
    from pollypm.notification_staging import _ensure_staging_table

    with pg_schema_pool.connection() as conn:
        _ensure_staging_table(conn)
        # Second call is a no-op — IF NOT EXISTS protects.
        _ensure_staging_table(conn)
        # Column types are pg-native (bigint id, jsonb payload, tz timestamps).
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = 'notification_staging' "
                "ORDER BY ordinal_position"
            )
            cols = dict(cur.fetchall())
    assert cols["id"] == "bigint"
    assert cols["payload_json"] == "jsonb"
    assert cols["created_at"] == "timestamp with time zone"
    assert cols["flushed_at"] == "timestamp with time zone"


# ---------------------------------------------------------------------------
# stage_notification
# ---------------------------------------------------------------------------


def test_stage_notification_inserts_row_and_returns_id(pg_schema_pool) -> None:
    from pollypm.notification_staging import stage_notification

    with pg_schema_pool.connection() as conn:
        row_id = stage_notification(
            conn,
            project="demo",
            subject="Task A done",
            body="merged PR #99",
            actor="polly",
            priority="digest",
            milestone_key="milestones/01-init",
            payload={"pr": "#99"},
        )
        assert row_id > 0

        rows = _all_rows(conn)
    assert len(rows) == 1
    row = rows[0]
    assert row["project"] == "demo"
    assert row["subject"] == "Task A done"
    assert row["priority"] == "digest"
    assert row["milestone_key"] == "milestones/01-init"
    # payload_json comes back as a python dict thanks to jsonb adaptation.
    payload = row["payload_json"]
    if isinstance(payload, str):  # belt-and-braces: psycopg returns dict
        payload = json.loads(payload)
    assert payload["pr"] == "#99"
    # The producer fills in convenience fields on the payload dict.
    assert payload["subject"] == "Task A done"
    assert payload["actor"] == "polly"
    assert payload["project"] == "demo"


def test_stage_notification_rejects_immediate_priority(pg_schema_pool) -> None:
    from pollypm.notification_staging import stage_notification

    with pg_schema_pool.connection() as conn:
        with pytest.raises(AssertionError):
            stage_notification(
                conn,
                project="demo",
                subject="urgent",
                body="",
                actor="polly",
                priority="immediate",  # not stageable
                milestone_key=None,
            )


def test_stage_notification_accepts_silent_priority(pg_schema_pool) -> None:
    from pollypm.notification_staging import stage_notification

    with pg_schema_pool.connection() as conn:
        row_id = stage_notification(
            conn,
            project="demo",
            subject="audit log",
            body="",
            actor="polly",
            priority="silent",
            milestone_key=None,
        )
        assert row_id > 0
        rows = _all_rows(conn)
    assert len(rows) == 1
    assert rows[0]["priority"] == "silent"
    assert rows[0]["milestone_key"] is None


# ---------------------------------------------------------------------------
# list_pending
# ---------------------------------------------------------------------------


def test_list_pending_returns_oldest_first(pg_schema_pool) -> None:
    from pollypm.notification_staging import list_pending, stage_notification

    with pg_schema_pool.connection() as conn:
        for i in range(3):
            stage_notification(
                conn,
                project="demo",
                subject=f"subject-{i}",
                body=f"body-{i}",
                actor="polly",
                priority="digest",
                milestone_key="milestones/01-init",
            )

        rows = list_pending(
            conn, project="demo", milestone_key="milestones/01-init",
        )
    assert [r["subject"] for r in rows] == ["subject-0", "subject-1", "subject-2"]


def test_list_pending_filters_by_milestone_key(pg_schema_pool) -> None:
    from pollypm.notification_staging import list_pending, stage_notification

    with pg_schema_pool.connection() as conn:
        stage_notification(
            conn, project="demo", subject="a", body="", actor="polly",
            priority="digest", milestone_key="milestones/01",
        )
        stage_notification(
            conn, project="demo", subject="b", body="", actor="polly",
            priority="digest", milestone_key="milestones/02",
        )
        stage_notification(
            conn, project="demo", subject="c", body="", actor="polly",
            priority="digest", milestone_key=None,
        )

        only_01 = list_pending(
            conn, project="demo", milestone_key="milestones/01",
        )
        only_null = list_pending(conn, project="demo", milestone_key=None)
    assert [r["subject"] for r in only_01] == ["a"]
    assert [r["subject"] for r in only_null] == ["c"]


def test_list_pending_skips_silent_and_flushed(pg_schema_pool) -> None:
    from pollypm.notification_staging import list_pending, stage_notification

    with pg_schema_pool.connection() as conn:
        # Silent never surfaces.
        stage_notification(
            conn, project="demo", subject="audit", body="", actor="polly",
            priority="silent", milestone_key="milestones/01",
        )
        # Digest pending → surfaces.
        stage_notification(
            conn, project="demo", subject="open", body="", actor="polly",
            priority="digest", milestone_key="milestones/01",
        )
        # Digest already flushed → does not surface.
        stage_notification(
            conn, project="demo", subject="closed", body="", actor="polly",
            priority="digest", milestone_key="milestones/01",
        )
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE notification_staging SET flushed_at = now(), "
                "rollup_task_id = 'demo/1' WHERE subject = 'closed'"
            )
        conn.commit()

        rows = list_pending(
            conn, project="demo", milestone_key="milestones/01",
        )
    assert [r["subject"] for r in rows] == ["open"]


# ---------------------------------------------------------------------------
# prune_old_staging
# ---------------------------------------------------------------------------


def _insert_row(
    conn, *, subject, priority, created_at, flushed_at=None, milestone_key=None,
):
    """Direct INSERT used by the prune tests to fix timestamps in the past."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO notification_staging "
            "(project, subject, body, actor, priority, payload_json, "
            "milestone_key, created_at, flushed_at, rollup_task_id) "
            "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)",
            (
                "demo", subject, "body", "polly", priority, "{}",
                milestone_key, created_at, flushed_at,
                "demo/99" if flushed_at else None,
            ),
        )
    conn.commit()


def test_prune_removes_old_flushed_and_silent_rows(pg_schema_pool) -> None:
    from pollypm.notification_staging import (
        _ensure_staging_table, prune_old_staging,
    )

    with pg_schema_pool.connection() as conn:
        _ensure_staging_table(conn)
        now = datetime.now(UTC)

        # (a) Flushed 40 days ago — should be pruned.
        _insert_row(
            conn,
            subject="old flushed",
            priority="digest",
            milestone_key="milestones/01",
            created_at=now - timedelta(days=50),
            flushed_at=now - timedelta(days=40),
        )
        # (b) Flushed 5 days ago — keep.
        _insert_row(
            conn,
            subject="recent flushed",
            priority="digest",
            milestone_key="milestones/01",
            created_at=now - timedelta(days=10),
            flushed_at=now - timedelta(days=5),
        )
        # (c) Pending digest from 60 days ago — keep (never pruned).
        _insert_row(
            conn,
            subject="old pending",
            priority="digest",
            milestone_key="milestones/02",
            created_at=now - timedelta(days=60),
        )
        # (d) Silent row 40 days ago — prune.
        _insert_row(
            conn,
            subject="old audit",
            priority="silent",
            created_at=now - timedelta(days=40),
        )

        summary = prune_old_staging(conn, retain_days=30)

        rows = _all_rows(conn)
    assert summary == {"flushed_pruned": 1, "silent_pruned": 1}
    remaining = sorted(r["subject"] for r in rows)
    assert remaining == ["old pending", "recent flushed"]


def test_prune_is_noop_on_empty_table(pg_schema_pool) -> None:
    from pollypm.notification_staging import prune_old_staging

    with pg_schema_pool.connection() as conn:
        summary = prune_old_staging(conn, retain_days=30)
        assert _row_count(conn) == 0
    assert summary == {"flushed_pruned": 0, "silent_pruned": 0}


# ---------------------------------------------------------------------------
# Backend-shape regression — sqlite type hints are gone.
# ---------------------------------------------------------------------------


def test_module_does_not_import_sqlite3() -> None:
    """The K-misc port must leave no sqlite3 reference behind."""
    import pollypm.notification_staging as ns

    # sqlite3 was a top-level import on the legacy module; the port
    # dropped it. Re-imports of the module shouldn't bring it back.
    assert getattr(ns, "sqlite3", None) is None
    # And the source file shouldn't carry any sqlite3 textual references.
    from pathlib import Path

    src = Path(ns.__file__).read_text(encoding="utf-8")
    # Permit references inside docstring comparisons ("sqlite3.Row")
    # only if they're explicitly noting the port — but the actual
    # ``import sqlite3`` line must not appear.
    assert "\nimport sqlite3" not in src
    assert "from sqlite3" not in src
