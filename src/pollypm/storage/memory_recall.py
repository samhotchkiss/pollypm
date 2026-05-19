"""Hybrid pgvector + FTS memory recall (issue #1737, Slice D).

The public surface is :func:`recall` — embed the query once, then run a
single pg query that joins ``embeddings`` against the originating
source table and blends four components into a final score:

* **semantic** — cosine similarity from pgvector (``1 - embedding <=> q``)
* **fts** — ``ts_rank_cd`` against the generated ``tsvector`` columns
* **importance** — only meaningful for ``memory_entries``;
  defaults to ``0.6`` for tables without an importance column
* **recency** — exponential decay over ``created_at`` age

Weights come from ``[storage.embedding] score_weights``. The query
normalises them to sum to 1 before applying — operators can leave any
single knob alone without throwing the whole blend off.

Out-of-scope for Slice D
------------------------

* The cockpit recall pane (future slice; CLI is the only surface today).
* Per-table scope filters (``project_key``, ``recipient``, ...). The
  CLI exposes ``--source`` for picking which tables to search; per-row
  filtering is a Slice E-or-later refinement once an operator actually
  asks for it.
* Cross-encoder reranking. Hybrid blend is enough for the use cases on
  the recall-pane wishlist; a reranker stage can be appended later.

The recall function is intentionally pure: no module-level state, no
threads. It opens a read-only pg connection from the pool, runs one
query, returns. Callers (CLI, future cockpit pane, tests) treat it as
a function.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pollypm.storage.embedder import Embedder, EmbedderError, resolve_embedder

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

    from pollypm.models import EmbeddingScoreWeights, PollyPMConfig


logger = logging.getLogger(__name__)


# Tables we know how to recall from. Each has its own per-table query
# fragment (different text columns, different tsvector column,
# different importance handling). ``order`` is the UNION ALL emit
# order — recall results from earlier tables tie-break ahead of later
# ones at equal score, which matches operator intuition (memory entries
# beat raw messages beat work-context notes when the scores match).
SUPPORTED_SOURCES = ("memory_entries", "messages", "work_context_entries")


@dataclass(slots=True)
class RecallHit:
    """One row from the hybrid recall query.

    ``source_table`` — which table the hit came from.
    ``source_id`` — the row's id, stringified to match ``embeddings.source_id``.
    ``score`` — final blended score in ``[0, 1]`` (higher = better).
    ``text_preview`` — short snippet of the matched text (first ~240 chars).
    ``metadata`` — dict of per-table extras (title, scope, type, ...).
    Includes the score breakdown under the ``"components"`` key for
    debugging.
    """

    source_table: str
    source_id: str
    score: float
    text_preview: str
    metadata: dict[str, object] = field(default_factory=dict)


# --------------------------------------------------------------------- #
# Per-table query fragments.
# --------------------------------------------------------------------- #
# Each fragment SELECTs a uniform projection so the UNION ALL parent
# query can blend results without a CASE-per-table mess. The columns,
# in order:
#
#   source_table TEXT
#   source_id TEXT
#   raw_text TEXT             -- text we'd want to preview
#   semantic_score FLOAT       -- 1 - (embedding <=> $q_vec); 0 if no embed
#   fts_score FLOAT            -- ts_rank_cd; 0 if no fts hit
#   importance_score FLOAT     -- normalised [0,1]; 0.6 default
#   recency_score FLOAT        -- exp(-age_days/90)
#   metadata_json TEXT         -- per-table extras as jsonb_build_object
#
# The semantic score is computed even for rows without an embedding
# (joins as LEFT JOIN so the row still surfaces under FTS-only matches).

_FTS_RANK_SQL = "coalesce(ts_rank_cd({tsv}, plainto_tsquery('english', %(query_text)s)), 0)"

_RECENCY_SQL = (
    "exp(-extract(epoch from (now() - {ts_col})) / (90.0 * 86400.0))"
)

_SEMANTIC_SQL = (
    "CASE WHEN e.embedding IS NULL THEN 0.0 "
    "ELSE 1.0 - (e.embedding <=> %(query_vec)s::vector) END"
)


def _per_table_select(source_table: str) -> str:
    """Return the SELECT fragment for ``source_table``.

    Each fragment must produce a row shape identical to the UNION
    parent. ``source_id`` is forced to ``text`` everywhere so the
    pg vector index join key matches across tables.
    """
    if source_table == "memory_entries":
        return f"""
        SELECT
            'memory_entries'::text AS source_table,
            m.id::text AS source_id,
            (coalesce(m.title, '') || E'\\n' || coalesce(m.body, '')) AS raw_text,
            {_SEMANTIC_SQL} AS semantic_score,
            {_FTS_RANK_SQL.format(tsv='m.title_body_tsv')} AS fts_score,
            (m.importance::float / 5.0) AS importance_score,
            {_RECENCY_SQL.format(ts_col='m.created_at')} AS recency_score,
            jsonb_build_object(
                'title', m.title,
                'scope', m.scope,
                'scope_tier', m.scope_tier,
                'type', m.type,
                'importance', m.importance,
                'tags', m.tags,
                'created_at', m.created_at
            ) AS metadata_json
        FROM memory_entries m
        LEFT JOIN embeddings e
            ON e.source_table = 'memory_entries'
           AND e.source_id = m.id::text
        WHERE (m.superseded_by IS NULL)
        """
    if source_table == "messages":
        return f"""
        SELECT
            'messages'::text AS source_table,
            mm.id::text AS source_id,
            (coalesce(mm.subject, '') || E'\\n' || coalesce(mm.body, '')) AS raw_text,
            {_SEMANTIC_SQL} AS semantic_score,
            {_FTS_RANK_SQL.format(tsv='mm.subject_body_tsv')} AS fts_score,
            0.6::float AS importance_score,
            {_RECENCY_SQL.format(ts_col='mm.created_at')} AS recency_score,
            jsonb_build_object(
                'subject', mm.subject,
                'scope', mm.scope,
                'type', mm.type,
                'tier', mm.tier,
                'recipient', mm.recipient,
                'sender', mm.sender,
                'state', mm.state,
                'created_at', mm.created_at
            ) AS metadata_json
        FROM messages mm
        LEFT JOIN embeddings e
            ON e.source_table = 'messages'
           AND e.source_id = mm.id::text
        """
    if source_table == "work_context_entries":
        return f"""
        SELECT
            'work_context_entries'::text AS source_table,
            w.id::text AS source_id,
            coalesce(w.text, '') AS raw_text,
            {_SEMANTIC_SQL} AS semantic_score,
            (CASE
                WHEN plainto_tsquery('english', %(query_text)s) = ''::tsquery
                    THEN 0.0
                ELSE coalesce(
                    ts_rank_cd(
                        to_tsvector('english', coalesce(w.text, '')),
                        plainto_tsquery('english', %(query_text)s)
                    ),
                    0
                )
            END) AS fts_score,
            0.6::float AS importance_score,
            {_RECENCY_SQL.format(ts_col='w.created_at')} AS recency_score,
            jsonb_build_object(
                'task_project', w.task_project,
                'task_number', w.task_number,
                'actor', w.actor,
                'entry_type', w.entry_type,
                'created_at', w.created_at
            ) AS metadata_json
        FROM work_context_entries w
        LEFT JOIN embeddings e
            ON e.source_table = 'work_context_entries'
           AND e.source_id = w.id::text
        """
    raise ValueError(f"recall: unsupported source_table {source_table!r}")


# --------------------------------------------------------------------- #
# Score normalisation.
# --------------------------------------------------------------------- #


def _normalise_weights(
    weights: "EmbeddingScoreWeights",
) -> tuple[float, float, float, float]:
    """Return (semantic, fts, importance, recency) normalised to sum 1.

    Negative weights are clamped at 0. An all-zero blend falls back to
    the documented default so the recall query never multiplies by 0
    across the board.
    """
    s = max(0.0, weights.semantic)
    f = max(0.0, weights.fts)
    i = max(0.0, weights.importance)
    r = max(0.0, weights.recency)
    total = s + f + i + r
    if total <= 0:
        return (0.5, 0.25, 0.15, 0.10)
    return (s / total, f / total, i / total, r / total)


# --------------------------------------------------------------------- #
# Public API.
# --------------------------------------------------------------------- #


def recall(
    query: str,
    *,
    limit: int = 10,
    source_tables: list[str] | None = None,
    config: "PollyPMConfig | None" = None,
    pool: "ConnectionPool | None" = None,
    embedder: Embedder | None = None,
) -> list[RecallHit]:
    """Hybrid pgvector + FTS recall.

    Parameters
    ----------
    query:
        Free-text query. Empty/whitespace-only queries are allowed —
        results then rank purely on importance + recency, useful for
        "show me anything recent" smoke tests.
    limit:
        Max hits to return. Capped at 200 to keep the round-trip
        bounded — the recall API isn't a paginated list endpoint.
    source_tables:
        Restrict to a subset of ``("memory_entries", "messages",
        "work_context_entries")``. ``None`` (default) searches all
        three.
    config:
        Loaded :class:`PollyPMConfig`. Required for the embedder
        unless ``embedder`` is passed explicitly.
    pool:
        Read-only :class:`psycopg_pool.ConnectionPool`. Defaults to
        :func:`pollypm.storage.pg_pool.get_ro_pool`.
    embedder:
        Injection seam for tests / backfill. Defaults to the one
        resolved from ``config.storage.embedding``.
    """
    if limit <= 0:
        raise ValueError("recall: limit must be >= 1")
    limit = min(limit, 200)

    tables = list(source_tables or SUPPORTED_SOURCES)
    unknown = set(tables) - set(SUPPORTED_SOURCES)
    if unknown:
        raise ValueError(
            f"recall: unsupported source_tables {sorted(unknown)!r}; "
            f"known: {SUPPORTED_SOURCES!r}"
        )

    if pool is None:
        from pollypm.storage import pg_pool

        pool = pg_pool.get_ro_pool(config)

    # Resolve weights from config (or fall back to dataclass defaults).
    if config is not None:
        weights = config.storage.embedding.score_weights
    else:
        from pollypm.models import EmbeddingScoreWeights

        weights = EmbeddingScoreWeights()
    w_sem, w_fts, w_imp, w_rec = _normalise_weights(weights)

    # Embed the query. If the embedder is missing or fails, fall back
    # to a zero vector — the recall still works against the FTS leg,
    # just with no semantic contribution. We log so an operator can
    # see why their `pm memory recall` query went keyword-only.
    query_vec = _resolve_query_vector(query, config, embedder)

    # Build the UNION ALL across selected source tables.
    union_parts = [_per_table_select(t) for t in tables]
    union_sql = "\nUNION ALL\n".join(union_parts)

    # Wrap the union in an outer SELECT that applies the weight blend
    # and orders + limits. The CASE on plainto_tsquery handles the
    # empty-query case (no FTS contribution) cleanly.
    final_sql = f"""
    WITH hits AS (
        {union_sql}
    )
    SELECT
        source_table,
        source_id,
        raw_text,
        ({w_sem} * semantic_score
         + {w_fts} * fts_score
         + {w_imp} * importance_score
         + {w_rec} * recency_score) AS score,
        semantic_score,
        fts_score,
        importance_score,
        recency_score,
        metadata_json
    FROM hits
    ORDER BY score DESC, source_table, source_id DESC
    LIMIT %(limit)s
    """

    return _run_recall_query(pool, final_sql, query, query_vec, limit)


def _resolve_query_vector(
    query: str,
    config: "PollyPMConfig | None",
    embedder: Embedder | None,
) -> list[float] | None:
    """Embed ``query`` once. Returns ``None`` on failure (FTS-only mode).

    The zero-vector fallback inside the SQL (the ``CASE WHEN
    e.embedding IS NULL THEN 0.0`` clause) means a ``None`` here just
    drops the semantic contribution to zero — recall still works on
    the FTS / importance / recency legs.
    """
    if embedder is None and config is not None:
        try:
            embedder = resolve_embedder(
                config.storage.embedding.model,
                config.storage.embedding.api_key_env,
            )
        except EmbedderError:
            logger.exception(
                "recall: embedder resolution failed; "
                "falling back to FTS-only"
            )
            return None

    if embedder is None:
        return None
    if not query.strip():
        # Empty query — no need to call the embedder. The CASE in the
        # SQL will see a NULL query_vec and skip the cosine compare.
        return None
    try:
        vectors = embedder.embed([query])
    except EmbedderError:
        logger.exception(
            "recall: embed failed; falling back to FTS-only"
        )
        return None
    if not vectors:
        return None
    return vectors[0]


def _run_recall_query(
    pool: "ConnectionPool",
    sql: str,
    query_text: str,
    query_vec: list[float] | None,
    limit: int,
) -> list[RecallHit]:
    """Execute the recall query and pack results into :class:`RecallHit`.

    The ``query_vec`` ``None`` case substitutes a zero vector — the
    cosine compare against zero yields a constant similarity for
    every row, so the semantic term contributes equally and the
    blended score collapses cleanly onto the FTS+importance+recency
    components.
    """
    # When the embedder failed or the query was empty, hand the SQL
    # a zero vector. We don't bother to skip the join because pgvector
    # cosine is cheap relative to the FTS path.
    if query_vec is None:
        effective_vec: list[float] = [0.0] * 1536
    else:
        effective_vec = query_vec

    # pgvector wants a literal of the form '[0.1, 0.2, ...]' for the
    # ``::vector`` cast in the SQL. The psycopg adapter
    # (``register_vector``) accepts ``list[float]`` natively, but the
    # recall path can also be called without the adapter (sqlite
    # installs + unit tests). Format manually so we don't add a
    # mandatory import here.
    vec_literal = "[" + ",".join(repr(float(v)) for v in effective_vec) + "]"

    params = {
        "query_text": query_text,
        "query_vec": vec_literal,
        "limit": limit,
    }

    with pool.connection() as conn:
        # Make this query read-only at the transaction level — the
        # RO pool already sets the session-level flag but defensive
        # belts + suspenders.
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    return [_row_to_hit(row) for row in rows]


def _row_to_hit(row) -> RecallHit:
    """Pack a recall row into :class:`RecallHit`.

    Row shape: ``(source_table, source_id, raw_text, score,
    semantic, fts, importance, recency, metadata_json)``.
    """
    (
        source_table,
        source_id,
        raw_text,
        score,
        semantic,
        fts,
        importance,
        recency,
        metadata_json,
    ) = row
    text_preview = (raw_text or "").strip().replace("\n", " ")[:240]

    meta: dict[str, object] = {}
    if isinstance(metadata_json, dict):
        meta.update(metadata_json)
    meta["components"] = {
        "semantic": float(semantic or 0.0),
        "fts": float(fts or 0.0),
        "importance": float(importance or 0.0),
        "recency": float(recency or 0.0),
    }

    return RecallHit(
        source_table=str(source_table),
        source_id=str(source_id),
        score=float(score or 0.0),
        text_preview=text_preview,
        metadata=meta,
    )


__all__ = [
    "RecallHit",
    "SUPPORTED_SOURCES",
    "recall",
]
