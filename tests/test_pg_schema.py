"""Tests for the canonical pg schema + migration applier (issue #1737).

Strategy
--------

Fixtures (``_pg_container`` + ``pg_schema_pool``) live in
``tests/conftest_pg.py`` so multiple ``test_pg_*.py`` modules share
them. Each test runs against a fresh schema namespace inside one
session-scoped pg container so cases don't collide and teardown is
cheap.

Skipped automatically when neither Docker nor a local pg with
``vector`` is available.
"""

from __future__ import annotations

import pytest


# --------------------------------------------------------------------- #
# Schema apply tests.
# --------------------------------------------------------------------- #


def test_initial_migration_applies_cleanly(pg_schema_pool):
    from pollypm.storage.pg_migrations import apply_migrations
    from pollypm.storage.pg_schema import all_table_names

    result = apply_migrations(pg_schema_pool)
    assert result.did_anything
    assert [v for v, _ in result.applied] == [1]

    expected = set(all_table_names())
    with pg_schema_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
        )
        present = {row[0] for row in cur.fetchall()}
    missing = expected - present
    assert not missing, f"missing tables after migrate: {sorted(missing)}"


def test_migration_is_idempotent(pg_schema_pool):
    """Apply twice — the second pass must be a no-op."""
    from pollypm.storage.pg_migrations import apply_migrations

    first = apply_migrations(pg_schema_pool)
    assert first.did_anything
    second = apply_migrations(pg_schema_pool)
    assert not second.did_anything
    assert [v for v, _ in second.already_applied] == [1]


def test_schema_migrations_table_records_label(pg_schema_pool):
    from pollypm.storage.pg_migrations import apply_migrations

    apply_migrations(pg_schema_pool)
    with pg_schema_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT version, label FROM schema_migrations ORDER BY version"
        )
        rows = cur.fetchall()
    assert rows == [(1, "0001_initial")]


def test_vector_extension_installed(pg_schema_pool):
    """The applier must install pgvector before any vector(N) DDL.

    Without the extension the embeddings table DDL would fail and the
    whole migration would roll back; the existence check here is the
    direct positive assertion.
    """
    from pollypm.storage.pg_migrations import apply_migrations

    apply_migrations(pg_schema_pool)
    with pg_schema_pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        assert cur.fetchone() is not None


def test_embeddings_hnsw_index_present(pg_schema_pool):
    from pollypm.storage.pg_migrations import apply_migrations

    apply_migrations(pg_schema_pool)
    with pg_schema_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_indexes "
            "WHERE schemaname = current_schema() "
            "AND tablename = 'embeddings' "
            "AND indexname = 'embeddings_hnsw'"
        )
        assert cur.fetchone() is not None


def test_messages_tsvector_column_generated(pg_schema_pool):
    """The generated tsvector column on messages must be queryable."""
    from pollypm.storage.pg_migrations import apply_migrations

    apply_migrations(pg_schema_pool)
    with pg_schema_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO messages (scope, type, recipient, sender, subject, body) "
            "VALUES ('s', 'event', 'r', 'me', 'hello world', 'goodbye moon')"
        )
        cur.execute(
            "SELECT (subject_body_tsv)::text FROM messages "
            "WHERE subject = 'hello world'"
        )
        row = cur.fetchone()
        assert row is not None
        assert "hello" in row[0]


def test_validate_migration_order_catches_gap(monkeypatch):
    """Sanity: a missing version in MIGRATIONS must fail loud at validate-time."""
    from pollypm.storage import pg_migrations

    bad = [(1, "a", "select 1"), (3, "c", "select 1")]
    monkeypatch.setattr(pg_migrations, "MIGRATIONS", bad)
    with pytest.raises(RuntimeError, match="missing migration"):
        pg_migrations._validate_migration_order()


def test_validate_migration_order_catches_duplicate(monkeypatch):
    from pollypm.storage import pg_migrations

    bad = [(1, "a", "select 1"), (1, "b", "select 1")]
    monkeypatch.setattr(pg_migrations, "MIGRATIONS", bad)
    with pytest.raises(RuntimeError, match="duplicate migration"):
        pg_migrations._validate_migration_order()
