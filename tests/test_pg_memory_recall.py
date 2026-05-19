"""Tests for hybrid pgvector + FTS recall (issue #1737, Slice D).

Lives next to the other ``test_pg_*`` modules. Runs against the
testcontainer pg fixture (``pg_schema_pool``); skipped automatically
when Docker / local pg isn't available.

A deterministic stub embedder is registered for the duration of each
test so vectors are reproducible without an OpenAI key.

Note: distinct from ``test_memory_recall.py`` which exercises the
legacy ``FileMemoryBackend.recall`` (M02 / #231).
"""

from __future__ import annotations

import hashlib

import pytest


# --------------------------------------------------------------------- #
# Stub embedder — deterministic 1536-dim vectors keyed by text hash.
# --------------------------------------------------------------------- #


class _StubEmbedder:
    """Reproducible embedder: SHA-256-derived 1536-dim float vectors.

    Each text seeds a tiny pseudo-random generator so two embed() calls
    on the same text produce the same vector. Vectors live in [-1, 1].
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    @property
    def info(self):
        from pollypm.storage.embedder import EmbedderInfo

        return EmbedderInfo(model="stub:test-1536", dim=1536)

    @property
    def max_batch_size(self) -> int:
        return 100

    def embed(self, texts):
        self.calls.append(list(texts))
        return [self._vector_for(t) for t in texts]

    @staticmethod
    def _vector_for(text: str) -> list[float]:
        seed = hashlib.sha256(text.encode("utf-8")).digest()
        vec: list[float] = []
        for i in range(1536):
            byte = seed[i % 32] ^ ((i // 32) & 0xFF)
            vec.append((byte / 127.5) - 1.0)
        return vec


@pytest.fixture()
def stub_embedder():
    return _StubEmbedder()


@pytest.fixture()
def memory_pool(pg_schema_pool):
    """Apply the schema migration so the recall tables exist."""
    from pollypm.storage.pg_migrations import apply_migrations

    apply_migrations(pg_schema_pool)
    return pg_schema_pool


# --------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------- #


def _insert_memory_entry(
    pool,
    *,
    title: str,
    body: str,
    scope: str = "project/demo",
    importance: int = 3,
    tags: str = "",
    age_seconds: int = 0,
) -> int:
    sql = """
    INSERT INTO memory_entries (
        scope, project_key, kind, title, body, tags, source,
        file_path, summary_path, created_at, updated_at,
        type, importance, scope_tier
    ) VALUES (
        %s, %s, %s, %s, %s, %s, %s, %s, %s,
        now() - make_interval(secs => %s),
        now() - make_interval(secs => %s),
        %s, %s, %s
    ) RETURNING id
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            sql,
            (
                scope, "demo", "knowledge", title, body, tags, "test",
                "memory.md", "summary.md", age_seconds, age_seconds,
                "project", importance, "project",
            ),
        )
        row = cur.fetchone()
        conn.commit()
    return int(row[0])


def _insert_message(
    pool,
    *,
    subject: str,
    body: str,
    scope: str = "project/demo",
) -> int:
    sql = """
    INSERT INTO messages (scope, type, recipient, sender, subject, body)
    VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            sql, (scope, "note", "operator", "agent", subject, body),
        )
        row = cur.fetchone()
        conn.commit()
    return int(row[0])


def _embed_row(pool, source_table: str, source_id: int):
    """Write a stub-embedder embedding for a row."""
    from pollypm.storage.embedding_writer import EmbeddingWriter

    embedder = _StubEmbedder()
    writer = EmbeddingWriter(pool, embedder)
    writer.enqueue(source_table, str(source_id))
    writer.drain_once()


# --------------------------------------------------------------------- #
# Recall behaviour.
# --------------------------------------------------------------------- #


def test_recall_empty_when_no_rows(memory_pool, stub_embedder):
    from pollypm.storage.memory_recall import recall

    out = recall(
        "anything",
        limit=5,
        pool=memory_pool,
        embedder=stub_embedder,
    )
    assert out == []


def test_recall_fts_matches_without_embedding(memory_pool, stub_embedder):
    """An FTS hit must surface even when the row has no embedding."""
    from pollypm.storage.memory_recall import recall

    _insert_memory_entry(
        memory_pool,
        title="postgres migration playbook",
        body="how to flip the storage backend safely",
    )
    _insert_memory_entry(
        memory_pool,
        title="completely different topic",
        body="kale recipes for the brave",
    )

    hits = recall(
        "postgres migration",
        limit=5,
        source_tables=["memory_entries"],
        pool=memory_pool,
        embedder=stub_embedder,
    )
    assert hits
    assert "postgres migration" in hits[0].text_preview.lower()


def test_recall_semantic_surfaces_when_embedding_present(
    memory_pool, stub_embedder,
):
    """An embedded row scores high on identical-text semantic match.

    The writer embeds ``title + "\\n" + body``; we craft the row so
    that text equals the query — same string → same stub vector → cos
    similarity = 1. Picks a unique nonsense phrase so FTS doesn't
    contribute at all and the assertion targets the semantic leg.
    """
    from pollypm.storage.memory_recall import recall

    embed_text = "unique-phrase-foo-bar-baz\n"  # body is empty
    target_id = _insert_memory_entry(
        memory_pool,
        title="unique-phrase-foo-bar-baz",
        body="",
    )
    _embed_row(memory_pool, "memory_entries", target_id)

    # Recall with the same embedded text so vectors match exactly.
    hits = recall(
        embed_text,
        limit=5,
        source_tables=["memory_entries"],
        pool=memory_pool,
        embedder=stub_embedder,
    )
    assert hits, "expected at least one hit"
    top = hits[0]
    assert top.source_table == "memory_entries"
    assert top.source_id == str(target_id)
    components = top.metadata["components"]
    # Same vector on both sides → cosine sim ≈ 1.
    assert components["semantic"] > 0.99


def test_recall_returns_metadata_and_components(memory_pool, stub_embedder):
    from pollypm.storage.memory_recall import recall

    entry_id = _insert_memory_entry(
        memory_pool,
        title="alpha title",
        body="beta body content",
        importance=5,
        scope="user/test",
        tags="t1,t2",
    )

    hits = recall(
        "alpha",
        limit=5,
        source_tables=["memory_entries"],
        pool=memory_pool,
        embedder=stub_embedder,
    )
    assert hits
    hit = hits[0]
    assert hit.source_id == str(entry_id)
    comp_keys = set(hit.metadata["components"])
    assert comp_keys == {"semantic", "fts", "importance", "recency"}
    # Importance 5 normalises to 1.0.
    assert hit.metadata["components"]["importance"] == pytest.approx(1.0)


def test_recall_source_filter_restricts_tables(memory_pool, stub_embedder):
    from pollypm.storage.memory_recall import recall

    _insert_memory_entry(
        memory_pool, title="lookup keyword", body="memory hit",
    )
    _insert_message(
        memory_pool, subject="lookup keyword", body="message hit",
    )

    only_messages = recall(
        "lookup keyword",
        limit=5,
        source_tables=["messages"],
        pool=memory_pool,
        embedder=stub_embedder,
    )
    assert only_messages
    assert all(h.source_table == "messages" for h in only_messages)

    only_memory = recall(
        "lookup keyword",
        limit=5,
        source_tables=["memory_entries"],
        pool=memory_pool,
        embedder=stub_embedder,
    )
    assert only_memory
    assert all(h.source_table == "memory_entries" for h in only_memory)


def test_recall_limit_caps_results(memory_pool, stub_embedder):
    from pollypm.storage.memory_recall import recall

    for i in range(15):
        _insert_memory_entry(
            memory_pool, title=f"common-token entry {i}", body="x",
        )

    hits = recall(
        "common-token",
        limit=3,
        source_tables=["memory_entries"],
        pool=memory_pool,
        embedder=stub_embedder,
    )
    assert len(hits) == 3


def test_recall_unsupported_source_raises(memory_pool, stub_embedder):
    from pollypm.storage.memory_recall import recall

    with pytest.raises(ValueError, match="unsupported"):
        recall(
            "x",
            source_tables=["definitely_not_a_table"],
            pool=memory_pool,
            embedder=stub_embedder,
        )


def test_recall_zero_limit_raises(memory_pool, stub_embedder):
    from pollypm.storage.memory_recall import recall

    with pytest.raises(ValueError, match=">= 1"):
        recall("x", limit=0, pool=memory_pool, embedder=stub_embedder)


# --------------------------------------------------------------------- #
# Writer + idempotency.
# --------------------------------------------------------------------- #


def test_writer_idempotent_skips_existing(memory_pool, stub_embedder):
    """A second drain on the same row must not re-call the embedder."""
    from pollypm.storage.embedding_writer import EmbeddingWriter

    entry_id = _insert_memory_entry(
        memory_pool, title="idempotency", body="check",
    )

    writer = EmbeddingWriter(memory_pool, stub_embedder)
    writer.enqueue("memory_entries", str(entry_id))
    first = writer.drain_once()
    assert first == 1

    writer.enqueue("memory_entries", str(entry_id))
    second = writer.drain_once()
    assert second == 0
    # Only one call landed on the embedder.
    assert len(stub_embedder.calls) == 1


def test_writer_handles_missing_source_row(memory_pool, stub_embedder):
    """Enqueued rows that no longer exist must drop quietly."""
    from pollypm.storage.embedding_writer import EmbeddingWriter

    writer = EmbeddingWriter(memory_pool, stub_embedder)
    writer.enqueue("memory_entries", "99999999")
    drained = writer.drain_once()
    assert drained == 0
    assert stub_embedder.calls == []


def test_writer_catastrophic_failure_does_not_crash(memory_pool):
    """An embedder error logs + skips; metrics record the failure."""
    from pollypm.storage.embedder import EmbedderError
    from pollypm.storage.embedding_writer import EmbeddingWriter

    class _BoomEmbedder:
        @property
        def info(self):
            from pollypm.storage.embedder import EmbedderInfo

            return EmbedderInfo(model="boom:v0", dim=1536)

        @property
        def max_batch_size(self) -> int:
            return 32

        def embed(self, texts):
            raise EmbedderError("simulated outage")

    entry_id = _insert_memory_entry(
        memory_pool, title="boom", body="boom",
    )
    writer = EmbeddingWriter(memory_pool, _BoomEmbedder())
    writer.enqueue("memory_entries", str(entry_id))
    assert writer.drain_once() == 0
    assert writer.metrics_batch_failures >= 1


def test_writer_unknown_table_logs_and_drops(memory_pool, stub_embedder, caplog):
    from pollypm.storage.embedding_writer import EmbeddingWriter

    writer = EmbeddingWriter(memory_pool, stub_embedder)
    with caplog.at_level("WARNING"):
        writer.enqueue("not_a_real_table", "1")
    assert writer.drain_once() == 0
    assert "unknown source_table" in caplog.text


def test_writer_embeds_messages_and_work_context(memory_pool, stub_embedder):
    """Sanity-check all three source tables can be embedded."""
    from pollypm.storage.embedding_writer import EmbeddingWriter

    # messages row
    msg_id = _insert_message(
        memory_pool, subject="hello", body="world",
    )

    # work_context row — needs a work_task FK, so set the task up first.
    with memory_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO work_flow_templates (name, version, start_node, created_at) "
            "VALUES ('t', 1, 'n1', now())"
        )
        cur.execute(
            "INSERT INTO work_tasks ("
            "  project, task_number, project_key, title, type, "
            "  flow_template_id, created_at, created_by, updated_at"
            ") VALUES ('p', 1, 'p', 't', 'task', 't', now(), 'u', now())"
        )
        cur.execute(
            "INSERT INTO work_context_entries ("
            "  task_project, task_number, actor, text, created_at"
            ") VALUES ('p', 1, 'op', 'sample context', now()) "
            "RETURNING id"
        )
        ctx_id = int(cur.fetchone()[0])
        conn.commit()

    writer = EmbeddingWriter(memory_pool, stub_embedder)
    writer.enqueue("messages", str(msg_id))
    writer.enqueue("work_context_entries", str(ctx_id))
    assert writer.drain_once() == 2

    # Both should now appear in the embeddings table.
    with memory_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT source_table, source_id FROM embeddings "
            "ORDER BY source_table"
        )
        rows = cur.fetchall()
    assert (("messages", str(msg_id)) in rows)
    assert (("work_context_entries", str(ctx_id)) in rows)


# --------------------------------------------------------------------- #
# Score weight normalisation (pure-Python).
# --------------------------------------------------------------------- #


def test_score_weights_normalised():
    from pollypm.models import EmbeddingScoreWeights
    from pollypm.storage.memory_recall import _normalise_weights

    w = EmbeddingScoreWeights(
        semantic=2.0, fts=2.0, importance=0.0, recency=0.0,
    )
    out = _normalise_weights(w)
    assert sum(out) == pytest.approx(1.0)
    assert out[0] == pytest.approx(0.5)
    assert out[1] == pytest.approx(0.5)


def test_score_weights_all_zero_falls_back():
    from pollypm.models import EmbeddingScoreWeights
    from pollypm.storage.memory_recall import _normalise_weights

    w = EmbeddingScoreWeights(
        semantic=0.0, fts=0.0, importance=0.0, recency=0.0,
    )
    assert _normalise_weights(w) == (0.5, 0.25, 0.15, 0.10)


def test_score_weights_negative_clamped():
    from pollypm.models import EmbeddingScoreWeights
    from pollypm.storage.memory_recall import _normalise_weights

    w = EmbeddingScoreWeights(
        semantic=-1.0, fts=1.0, importance=0.0, recency=0.0,
    )
    out = _normalise_weights(w)
    assert out == (0.0, 1.0, 0.0, 0.0)


# --------------------------------------------------------------------- #
# Backfill helper.
# --------------------------------------------------------------------- #


def test_backfill_enqueues_only_missing_rows(memory_pool, stub_embedder):
    """The backfill helper must skip rows that already have embeddings."""
    from pollypm.memory_recall_cli import _enqueue_missing
    from pollypm.storage.embedding_writer import EmbeddingWriter

    # Two memory rows; embed one of them in advance.
    id1 = _insert_memory_entry(memory_pool, title="a", body="alpha")
    id2 = _insert_memory_entry(memory_pool, title="b", body="beta")
    _embed_row(memory_pool, "memory_entries", id1)

    writer = EmbeddingWriter(memory_pool, stub_embedder)
    scanned, queued = _enqueue_missing(
        memory_pool, "memory_entries", writer, limit=0,
    )
    assert scanned == 2
    assert queued == 1
    embedded = writer.drain_once()
    assert embedded == 1

    # Confirm both rows now have embeddings.
    with memory_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM embeddings "
            "WHERE source_table = 'memory_entries' "
            "AND source_id = ANY(%s)",
            ([str(id1), str(id2)],),
        )
        count = int(cur.fetchone()[0])
    assert count == 2
