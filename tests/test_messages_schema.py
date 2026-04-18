"""Tests for :mod:`pollypm.store.schema` — unified ``messages`` table + FTS5.

Run under an isolated HOME so the suite never leaks into ``~/.pollypm/``:

    HOME=/tmp/pytest-store-messages uv run pytest tests/test_messages_schema.py -x

Coverage (per issue #338 acceptance):

1. Schema creates cleanly on an empty SQLite DB — all columns + indexes
   present after bootstrap.
2. FTS5 shadow table is created alongside the main table.
3. Insert into ``messages`` is mirrored into ``messages_fts`` so
   ``MATCH`` queries return the expected rowid.
4. Update to ``messages`` keeps ``messages_fts`` in sync — old term no
   longer matches, new term does.
5. Delete from ``messages`` removes the row from ``messages_fts``.
6. Bootstrap is idempotent — a second ``SQLAlchemyStore`` on the same
   DB path neither raises nor duplicates rows/indexes.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect, text

from pollypm.store import SQLAlchemyStore
from pollypm.store.schema import FTS_DDL_STATEMENTS, messages


def _db_url(tmp_path: Path, name: str = "store.db") -> str:
    return f"sqlite:///{tmp_path / name}"


# --------------------------------------------------------------------------
# Schema shape
# --------------------------------------------------------------------------


def test_messages_table_has_expected_columns(tmp_path: Path) -> None:
    store = SQLAlchemyStore(_db_url(tmp_path))
    try:
        inspector = inspect(store.write_engine)
        cols = {c["name"] for c in inspector.get_columns("messages")}
        # All columns from the #338 spec must land.
        expected = {
            "id",
            "scope",
            "type",
            "tier",
            "recipient",
            "sender",
            "state",
            "parent_id",
            "subject",
            "body",
            "payload_json",
            "labels",
            "created_at",
            "updated_at",
            "closed_at",
        }
        assert expected <= cols, f"missing columns: {expected - cols}"
    finally:
        store.close()


def test_messages_indexes_exist(tmp_path: Path) -> None:
    store = SQLAlchemyStore(_db_url(tmp_path))
    try:
        inspector = inspect(store.write_engine)
        index_names = {idx["name"] for idx in inspector.get_indexes("messages")}
        assert "idx_messages_recipient_state" in index_names
        assert "idx_messages_type_tier" in index_names
        assert "idx_messages_scope_created" in index_names
    finally:
        store.close()


def test_fts_virtual_table_exists(tmp_path: Path) -> None:
    store = SQLAlchemyStore(_db_url(tmp_path))
    try:
        with store.read_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='messages_fts'"
                )
            ).scalar()
        assert row == "messages_fts"
    finally:
        store.close()


def test_fts_ddl_statements_are_populated() -> None:
    # Guards against a regression where someone empties the list while
    # refactoring. The schema bootstrap depends on this being non-empty.
    assert len(FTS_DDL_STATEMENTS) >= 4


# --------------------------------------------------------------------------
# FTS sync triggers
# --------------------------------------------------------------------------


def _insert_row(
    store: SQLAlchemyStore,
    subject: str,
    body: str = "",
    labels: str = "[]",
) -> int:
    with store.transaction() as conn:
        result = conn.execute(
            messages.insert(),
            {
                "scope": "root",
                "type": "event",
                "tier": "immediate",
                "recipient": "*",
                "sender": "test",
                "state": "open",
                "subject": subject,
                "body": body,
                "payload_json": "{}",
                "labels": labels,
            },
        )
        return int(result.inserted_primary_key[0])


def _fts_rowids(store: SQLAlchemyStore, match: str) -> list[int]:
    with store.read_engine.connect() as conn:
        return [
            int(r[0])
            for r in conn.execute(
                text("SELECT rowid FROM messages_fts WHERE messages_fts MATCH :q"),
                {"q": match},
            ).all()
        ]


def test_insert_triggers_sync_into_fts(tmp_path: Path) -> None:
    store = SQLAlchemyStore(_db_url(tmp_path))
    try:
        row_id = _insert_row(store, subject="unique-marlin-kite")
        hits = _fts_rowids(store, "marlin")
        assert row_id in hits
    finally:
        store.close()


def test_update_triggers_resync_fts(tmp_path: Path) -> None:
    store = SQLAlchemyStore(_db_url(tmp_path))
    try:
        row_id = _insert_row(store, subject="alpha-token", body="hello")
        assert _fts_rowids(store, "alpha") == [row_id]

        with store.transaction() as conn:
            conn.execute(
                messages.update()
                .where(messages.c.id == row_id)
                .values(subject="beta-token", body="world")
            )

        # Old term no longer matches, new term does.
        assert _fts_rowids(store, "alpha") == []
        assert row_id in _fts_rowids(store, "beta")
    finally:
        store.close()


def test_delete_removes_row_from_fts(tmp_path: Path) -> None:
    store = SQLAlchemyStore(_db_url(tmp_path))
    try:
        row_id = _insert_row(store, subject="gamma-sentinel")
        assert row_id in _fts_rowids(store, "gamma")

        with store.transaction() as conn:
            conn.execute(messages.delete().where(messages.c.id == row_id))

        assert _fts_rowids(store, "gamma") == []
    finally:
        store.close()


# --------------------------------------------------------------------------
# Idempotent bootstrap
# --------------------------------------------------------------------------


def test_bootstrap_is_idempotent(tmp_path: Path) -> None:
    url = _db_url(tmp_path)
    first = SQLAlchemyStore(url)
    row_id = _insert_row(first, subject="persist-me")
    first.close()

    # Second construction against the same DB must not raise or lose data.
    second = SQLAlchemyStore(url)
    try:
        with second.read_engine.connect() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM messages")).scalar()
        assert count == 1

        hits = _fts_rowids(second, "persist")
        assert row_id in hits
    finally:
        second.close()
