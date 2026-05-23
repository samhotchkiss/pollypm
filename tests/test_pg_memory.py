"""pg-parity tests for the ``pg_memory`` facade (#1737, Slice K-state-port phase 2d).

Covers cluster J: ``memory_entries`` + ``memory_summaries`` tables.
"""

from __future__ import annotations


def _apply_initial_migrations(pg_schema_pool) -> None:
    from pollypm.storage.pg_migrations import apply_migrations

    apply_migrations(pg_schema_pool)


def test_pg_memory_record_get_roundtrip(pg_schema_pool):
    """record_memory_entry writes; get_memory_entry reads."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_memory import get_memory_entry, record_memory_entry

    record = record_memory_entry(
        scope="alpha",
        kind="note",
        title="Test Title",
        body="Test body content",
        tags=["foo", "bar"],
        source="manual",
        file_path="/tmp/note.md",
        summary_path="",
        importance=4,
        scope_tier="project",
        pool=pg_schema_pool,
    )
    assert record.entry_id > 0
    assert record.scope == "alpha"
    assert record.tags == ("foo", "bar")

    fetched = get_memory_entry(record.entry_id, pool=pg_schema_pool)
    assert fetched is not None
    assert fetched.title == "Test Title"
    assert fetched.body == "Test body content"
    assert fetched.tags == ("foo", "bar")
    assert fetched.importance == 4


def test_pg_memory_record_repairs_skewed_id_sequence(pg_schema_pool):
    """A dumped explicit id should not wedge future memory writes."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_memory import record_memory_entry

    with pg_schema_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO memory_entries (
                id, scope, kind, title, body, tags, source, file_path,
                summary_path, created_at, updated_at
            )
            VALUES
                (1, 'alpha', 'note', 'dumped-1', '', '[]', 'restore', '', '', now(), now()),
                (2, 'alpha', 'note', 'dumped-2', '', '[]', 'restore', '', '', now(), now())
            """
        )

    record = record_memory_entry(
        scope="alpha",
        kind="note",
        title="after skew",
        body="",
        tags=[],
        source="manual",
        file_path="",
        summary_path="",
        pool=pg_schema_pool,
    )

    assert record.entry_id == 3


def test_pg_memory_list_filters(pg_schema_pool):
    """list_memory_entries filters by scope/kind/type/scope_tier."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_memory import list_memory_entries, record_memory_entry

    for scope, kind, tier in [
        ("alpha", "note", "project"),
        ("alpha", "lesson", "project"),
        ("beta", "note", "project"),
        ("alpha", "note", "session"),
    ]:
        record_memory_entry(
            scope=scope,
            kind=kind,
            title=f"{scope}-{kind}",
            body="",
            tags=[],
            source="manual",
            file_path="",
            summary_path="",
            scope_tier=tier,
            pool=pg_schema_pool,
        )

    by_scope = list_memory_entries(scope="alpha", pool=pg_schema_pool)
    assert {r.title for r in by_scope} == {"alpha-note", "alpha-lesson"}

    by_kind = list_memory_entries(kind="note", pool=pg_schema_pool)
    assert {r.title for r in by_kind} == {"alpha-note", "beta-note"}

    by_tier = list_memory_entries(scope_tier="session", pool=pg_schema_pool)
    assert len(by_tier) == 1
    assert by_tier[0].scope_tier == "session"


def test_pg_memory_update_entry(pg_schema_pool):
    """update_memory_entry patches subset of fields and bumps updated_at."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_memory import (
        get_memory_entry,
        record_memory_entry,
        update_memory_entry,
    )

    record = record_memory_entry(
        scope="alpha",
        kind="note",
        title="t",
        body="original",
        tags=["a"],
        source="manual",
        file_path="",
        summary_path="",
        importance=2,
        pool=pg_schema_pool,
    )

    # No-op update returns False.
    assert update_memory_entry(record.entry_id, pool=pg_schema_pool) is False

    assert (
        update_memory_entry(
            record.entry_id,
            body="updated",
            importance=4,
            tags=["a", "b"],
            pool=pg_schema_pool,
        )
        is True
    )
    after = get_memory_entry(record.entry_id, pool=pg_schema_pool)
    assert after is not None
    assert after.body == "updated"
    assert after.importance == 4
    assert after.tags == ("a", "b")


def test_pg_memory_update_invalid_importance(pg_schema_pool):
    """update_memory_entry rejects importance outside [1, 5]."""
    _apply_initial_migrations(pg_schema_pool)
    import pytest

    from pollypm.storage.pg_memory import record_memory_entry, update_memory_entry

    record = record_memory_entry(
        scope="alpha",
        kind="note",
        title="t",
        body="",
        tags=[],
        source="m",
        file_path="",
        summary_path="",
        pool=pg_schema_pool,
    )
    with pytest.raises(ValueError):
        update_memory_entry(record.entry_id, importance=99, pool=pg_schema_pool)


def test_pg_memory_delete(pg_schema_pool):
    """delete_memory_entry removes a row, returns True/False per row presence."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_memory import (
        delete_memory_entry,
        get_memory_entry,
        record_memory_entry,
    )

    record = record_memory_entry(
        scope="alpha",
        kind="note",
        title="t",
        body="",
        tags=[],
        source="m",
        file_path="",
        summary_path="",
        pool=pg_schema_pool,
    )
    assert delete_memory_entry(record.entry_id, pool=pg_schema_pool) is True
    assert get_memory_entry(record.entry_id, pool=pg_schema_pool) is None
    assert delete_memory_entry(record.entry_id, pool=pg_schema_pool) is False


def test_pg_memory_purge_session_scope(pg_schema_pool):
    """purge_session_scope removes only session-tier rows for that session."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_memory import (
        list_memory_entries,
        purge_session_scope,
        record_memory_entry,
    )

    # Plant: 2 session-tier rows for s1, 1 for s2, 1 project-tier row.
    for scope, tier in [
        ("s1", "session"),
        ("s1", "session"),
        ("s2", "session"),
        ("s1", "project"),
    ]:
        record_memory_entry(
            scope=scope,
            kind="note",
            title=f"{scope}-{tier}",
            body="",
            tags=[],
            source="m",
            file_path="",
            summary_path="",
            scope_tier=tier,
            pool=pg_schema_pool,
        )

    removed = purge_session_scope("s1", pool=pg_schema_pool)
    assert removed == 2

    remaining = list_memory_entries(pool=pg_schema_pool)
    # s2 session + s1 project survive.
    assert len(remaining) == 2


def test_pg_memory_expire_task_scope(pg_schema_pool):
    """expire_task_scope stamps a TTL on task-tier entries with the given scope."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_memory import (
        expire_task_scope,
        get_memory_entry,
        record_memory_entry,
    )

    record = record_memory_entry(
        scope="T123",
        kind="note",
        title="t",
        body="",
        tags=[],
        source="m",
        file_path="",
        summary_path="",
        scope_tier="task",
        pool=pg_schema_pool,
    )
    updated = expire_task_scope(
        "T123",
        terminal_at="2026-05-19T00:00:00+00:00",
        ttl_days=30,
        pool=pg_schema_pool,
    )
    assert updated == 1
    after = get_memory_entry(record.entry_id, pool=pg_schema_pool)
    assert after is not None
    assert after.ttl_at is not None
    # ttl_at lands 30 days after terminal_at.
    assert "2026-06" in after.ttl_at


def test_pg_memory_sweep_expired(pg_schema_pool):
    """sweep_expired_memory_entries drops only rows with elapsed ttl_at."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_memory import (
        list_memory_entries,
        record_memory_entry,
        sweep_expired_memory_entries,
    )

    record_memory_entry(
        scope="alpha",
        kind="note",
        title="elapsed",
        body="",
        tags=[],
        source="m",
        file_path="",
        summary_path="",
        ttl_at="2000-01-01T00:00:00+00:00",
        pool=pg_schema_pool,
    )
    record_memory_entry(
        scope="alpha",
        kind="note",
        title="future",
        body="",
        tags=[],
        source="m",
        file_path="",
        summary_path="",
        ttl_at="2099-01-01T00:00:00+00:00",
        pool=pg_schema_pool,
    )
    record_memory_entry(
        scope="alpha",
        kind="note",
        title="no-ttl",
        body="",
        tags=[],
        source="m",
        file_path="",
        summary_path="",
        pool=pg_schema_pool,
    )

    removed = sweep_expired_memory_entries(pool=pg_schema_pool)
    assert removed == 1

    surviving = {r.title for r in list_memory_entries(pool=pg_schema_pool)}
    assert surviving == {"future", "no-ttl"}


def test_pg_memory_recall_keyword(pg_schema_pool):
    """recall_memory_entries runs a tsvector match; empty query falls back to id DESC."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_memory import (
        recall_memory_entries,
        record_memory_entry,
    )

    record_memory_entry(
        scope="alpha",
        kind="note",
        title="Heartbeat cascade",
        body="The heartbeat tier owns mechanical recovery.",
        tags=[],
        source="m",
        file_path="",
        summary_path="",
        pool=pg_schema_pool,
    )
    record_memory_entry(
        scope="alpha",
        kind="note",
        title="Polly is the operator",
        body="Polly mediates between PM and user.",
        tags=[],
        source="m",
        file_path="",
        summary_path="",
        pool=pg_schema_pool,
    )

    results = recall_memory_entries(query="heartbeat", limit=5, pool=pg_schema_pool)
    assert len(results) == 1
    record, score = results[0]
    assert record.title == "Heartbeat cascade"
    assert score is not None and score > 0

    # Empty query falls back to id DESC ordering with score=None.
    results = recall_memory_entries(query="", limit=5, pool=pg_schema_pool)
    assert len(results) == 2
    assert all(score is None for _, score in results)
    # Newest first — second insert ranks first.
    assert results[0][0].title == "Polly is the operator"


def test_pg_memory_recall_filters_superseded_and_expired(pg_schema_pool):
    """recall hides superseded + expired rows by default."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_memory import (
        recall_memory_entries,
        record_memory_entry,
        update_memory_entry,
    )

    keeper = record_memory_entry(
        scope="alpha",
        kind="note",
        title="keeper heartbeat",
        body="",
        tags=[],
        source="m",
        file_path="",
        summary_path="",
        pool=pg_schema_pool,
    )
    superseded = record_memory_entry(
        scope="alpha",
        kind="note",
        title="old heartbeat",
        body="",
        tags=[],
        source="m",
        file_path="",
        summary_path="",
        pool=pg_schema_pool,
    )
    update_memory_entry(
        superseded.entry_id,
        superseded_by=keeper.entry_id,
        pool=pg_schema_pool,
    )

    results = recall_memory_entries(query="heartbeat", limit=5, pool=pg_schema_pool)
    titles = {r.title for r, _ in results}
    assert "keeper heartbeat" in titles
    assert "old heartbeat" not in titles

    # include_superseded brings it back.
    results = recall_memory_entries(
        query="heartbeat",
        limit=5,
        include_superseded=True,
        pool=pg_schema_pool,
    )
    titles = {r.title for r, _ in results}
    assert {"keeper heartbeat", "old heartbeat"} <= titles


def test_pg_memory_summaries_roundtrip(pg_schema_pool):
    """record_memory_summary + latest_memory_summary roundtrip."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_memory import (
        latest_memory_summary,
        record_memory_summary,
    )

    assert latest_memory_summary("alpha", pool=pg_schema_pool) is None

    first = record_memory_summary(
        scope="alpha",
        summary_text="first summary",
        summary_path="/tmp/s1.md",
        entry_count=5,
        pool=pg_schema_pool,
    )
    second = record_memory_summary(
        scope="alpha",
        summary_text="second summary",
        summary_path="/tmp/s2.md",
        entry_count=10,
        pool=pg_schema_pool,
    )
    assert second.summary_id > first.summary_id

    latest = latest_memory_summary("alpha", pool=pg_schema_pool)
    assert latest is not None
    assert latest.summary_id == second.summary_id
    assert latest.summary_text == "second summary"
    assert latest.entry_count == 10

    # Different scope is isolated.
    assert latest_memory_summary("beta", pool=pg_schema_pool) is None
