"""Tests for :meth:`SQLAlchemyStore.query_messages_with_legacy_bridge` (#341).

The bridge is the temporary scaffolding that lets readers run off
the unified ``messages`` table while supervisor / heartbeat writers
still hit the legacy ``events`` / ``alerts`` tables (#349 migrates
those later). We test both halves:

* Rows in the new ``messages`` table show up, regardless of whether
  the legacy tables exist.
* Rows in the legacy tables are reshaped into message-dict form and
  returned alongside the new rows when ``type`` matches.
* Filters (``type`` / ``scope`` / ``state`` / ``limit`` / ``since``)
  apply to both halves uniformly.
* ``query_messages`` (without the bridge) ignores legacy rows — that's
  the post-#349 target behaviour.

Run with ``HOME=/tmp/pytest-storage-e uv run pytest -x
tests/test_store_query_bridge.py``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pollypm.store import SQLAlchemyStore


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


def _install_legacy_tables(path: Path) -> None:
    """Create the legacy ``events`` / ``alerts`` tables on ``path``.

    Mirrors the subset of :class:`StateStore` DDL the bridge reads —
    just enough columns to exercise the reshape logic in isolation.
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_name TEXT NOT NULL,
                event_type TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_name TEXT NOT NULL,
                alert_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


class TestBridgeWithNoLegacyTables:
    def test_bridge_returns_messages_when_legacy_tables_missing(
        self, db_path
    ):
        store = SQLAlchemyStore(f"sqlite:///{db_path}")
        try:
            store.enqueue_message(
                type="notify",
                tier="immediate",
                recipient="user",
                sender="polly",
                subject="hello",
                body="world",
                scope="proj",
            )
            rows = store.query_messages_with_legacy_bridge(
                recipient="user", state="open", type=["notify", "alert"],
            )
            assert len(rows) == 1
            assert rows[0]["type"] == "notify"
            assert rows[0]["scope"] == "proj"
        finally:
            store.close()


class TestBridgeWithLegacyAlerts:
    def test_legacy_alert_surfaces_via_bridge(self, db_path):
        # Create legacy tables FIRST so the Store's own CREATE IF NOT EXISTS
        # doesn't collide with our synthetic ones.
        _install_legacy_tables(db_path)
        store = SQLAlchemyStore(f"sqlite:///{db_path}")
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute(
                    "INSERT INTO alerts (session_name, alert_type, severity, "
                    "message, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 'open', ?, ?)",
                    (
                        "worker-foo",
                        "pane_dead",
                        "warn",
                        "Pane is dead",
                        "2026-01-01T00:00:00+00:00",
                        "2026-01-01T00:00:00+00:00",
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            rows = store.query_messages_with_legacy_bridge(
                type="alert", state="open",
            )
            assert len(rows) == 1
            row = rows[0]
            assert row["type"] == "alert"
            assert row["scope"] == "worker-foo"
            assert row["sender"] == "pane_dead"
            assert row["subject"] == "Pane is dead"
            assert row["payload"]["severity"] == "warn"
            assert row["_source"] == "legacy_alerts"

            # query_messages without the bridge must NOT return the
            # legacy row — that's the post-#349 behaviour.
            plain = store.query_messages(type="alert", state="open")
            assert plain == []
        finally:
            store.close()

    def test_cleared_legacy_alert_excluded(self, db_path):
        _install_legacy_tables(db_path)
        store = SQLAlchemyStore(f"sqlite:///{db_path}")
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute(
                    "INSERT INTO alerts (session_name, alert_type, severity, "
                    "message, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 'cleared', ?, ?)",
                    (
                        "s", "t", "warn", "old",
                        "2026-01-01T00:00:00+00:00",
                        "2026-01-01T00:00:00+00:00",
                    ),
                )
                conn.commit()
            finally:
                conn.close()
            rows = store.query_messages_with_legacy_bridge(
                type="alert", state="open",
            )
            assert rows == []
        finally:
            store.close()


class TestBridgeWithLegacyEvents:
    def test_legacy_event_surfaces_via_bridge(self, db_path):
        _install_legacy_tables(db_path)
        store = SQLAlchemyStore(f"sqlite:///{db_path}")
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute(
                    "INSERT INTO events (session_name, event_type, "
                    "message, created_at) VALUES (?, ?, ?, ?)",
                    (
                        "sess-1", "db.vacuum", "reclaimed 1.0MB",
                        "2026-01-01T00:00:00+00:00",
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            rows = store.query_messages_with_legacy_bridge(
                type="event",
            )
            assert len(rows) == 1
            row = rows[0]
            assert row["type"] == "event"
            assert row["subject"] == "db.vacuum"
            assert row["body"] == "reclaimed 1.0MB"
            assert row["_source"] == "legacy_events"
        finally:
            store.close()


class TestBridgeMerge:
    def test_new_and_legacy_rows_merge_and_sort_newest_first(self, db_path):
        _install_legacy_tables(db_path)
        store = SQLAlchemyStore(f"sqlite:///{db_path}")
        try:
            # Legacy alert with old timestamp.
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute(
                    "INSERT INTO alerts (session_name, alert_type, severity, "
                    "message, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 'open', ?, ?)",
                    (
                        "s", "legacy_type", "warn", "legacy alert",
                        "2026-01-01T00:00:00+00:00",
                        "2026-01-01T00:00:00+00:00",
                    ),
                )
                conn.commit()
            finally:
                conn.close()
            # Fresh messages-table alert via upsert_alert.
            store.upsert_alert(
                session_name="s",
                alert_type="new_type",
                severity="critical",
                message="new alert",
            )
            rows = store.query_messages_with_legacy_bridge(
                type="alert", state="open",
            )
            assert len(rows) == 2
            # messages-table row is newer, so it's first.
            assert rows[0]["sender"] == "new_type"
            assert rows[1]["sender"] == "legacy_type"
        finally:
            store.close()

    def test_limit_applies_after_merge(self, db_path):
        _install_legacy_tables(db_path)
        store = SQLAlchemyStore(f"sqlite:///{db_path}")
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                for i in range(3):
                    conn.execute(
                        "INSERT INTO alerts (session_name, alert_type, "
                        "severity, message, status, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, 'open', ?, ?)",
                        (
                            "s", f"t{i}", "warn", f"m{i}",
                            f"2026-01-0{i + 1}T00:00:00+00:00",
                            f"2026-01-0{i + 1}T00:00:00+00:00",
                        ),
                    )
                conn.commit()
            finally:
                conn.close()
            rows = store.query_messages_with_legacy_bridge(
                type="alert", state="open", limit=2,
            )
            assert len(rows) == 2
        finally:
            store.close()


class TestQueryMessagesListFilter:
    def test_type_accepts_list(self, tmp_path):
        store = SQLAlchemyStore(f"sqlite:///{tmp_path}/s.db")
        try:
            store.enqueue_message(
                type="notify",
                tier="immediate",
                recipient="user",
                sender="polly",
                subject="a",
                body="",
                scope="p",
            )
            store.enqueue_message(
                type="inbox_task",
                tier="immediate",
                recipient="user",
                sender="polly",
                subject="b",
                body="",
                scope="p",
            )
            store.enqueue_message(
                type="event",
                tier="immediate",
                recipient="*",
                sender="sys",
                subject="c",
                body="",
                scope="p",
            )
            # List filter UNIONs rows; single-type filter narrows.
            rows = store.query_messages(type=["notify", "inbox_task"])
            subjects = sorted(r["sender"] for r in rows)
            assert len(rows) == 2
            rows = store.query_messages(type="event")
            assert len(rows) == 1
        finally:
            store.close()
