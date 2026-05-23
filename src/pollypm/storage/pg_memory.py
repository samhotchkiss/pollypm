"""Postgres facade for ``memory_entries`` + ``memory_summaries`` (#1737).

This module owns the read/write path for cluster J of the StateStore
port plan: the knowledge / memory tier that backs the
:class:`pollypm.memory_backends.file.FileMemoryBackend` and the
``pm memory`` CLI surface.

Public API:

Entry CRUD:

* :func:`record_memory_entry` — INSERT a new entry, return the record
* :func:`get_memory_entry` — SELECT a single entry by id
* :func:`list_memory_entries` — SELECT with optional filters
* :func:`delete_memory_entry` — DELETE by id, return bool
* :func:`update_memory_entry` — patch a subset of columns

Recall:

* :func:`recall_memory_entries` — keyword-only recall using pg
  ``tsvector`` (the generated ``title_body_tsv`` column). The hybrid
  pgvector + FTS recall path lives separately in
  :mod:`pollypm.storage.memory_recall` and is the path the CLI's
  ``pm memory recall`` command uses; this function is the StateStore
  parity surface used by :class:`FileMemoryBackend`.

Lifecycle:

* :func:`purge_session_scope` — delete every session-tier entry for a
  given session id
* :func:`expire_task_scope` — stamp a 30-day TTL on task-tier entries
* :func:`sweep_expired_memory_entries` — drop rows whose TTL has elapsed

Summaries:

* :func:`record_memory_summary` — INSERT a summary row
* :func:`latest_memory_summary` — SELECT the most recent summary for
  a given scope

All functions accept an optional ``pool`` kwarg. Default is
:func:`~pollypm.storage.pg_pool.get_rw_pool` for the mutators and
:func:`~pollypm.storage.pg_pool.get_ro_pool` for the readers; passing a
custom pool is the test-harness seam.

Slice K-state-port phase 2d — port of StateStore cluster J.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from psycopg import errors as pg_errors

from pollypm.storage.records import MemoryEntryRecord, MemorySummaryRecord

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

    from pollypm.models import PollyPMConfig

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


def _stamp_str(value: object) -> str:
    """Return ``value`` as an ISO-8601 string (pg datetime → sqlite-style str)."""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _opt_stamp_str(value: object) -> str | None:
    """Return ``None`` for NULL, otherwise an ISO-8601 string."""
    if value is None:
        return None
    return _stamp_str(value)


def _is_memory_entries_pkey_violation(exc: pg_errors.UniqueViolation) -> bool:
    diag = getattr(exc, "diag", None)
    constraint = getattr(diag, "constraint_name", None)
    return constraint == "memory_entries_pkey" or "memory_entries_pkey" in str(exc)


def _safe_tags(raw: object) -> tuple[str, ...]:
    """Decode a stored ``tags`` value into a tuple.

    The ``memory_entries.tags`` column is stored as JSON text on both
    backends. Producers always write a list, but historical / malformed
    rows could carry dict / string / NULL — coerce non-list shapes to
    ``()`` so consumers iterating ``record.tags`` never see surprise
    types. Mirrors :func:`pollypm.storage.state._safe_tags`.
    """
    if not raw:
        return ()
    if isinstance(raw, (list, tuple)):
        return tuple(str(item) for item in raw)
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return ()
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return ()
        if isinstance(parsed, list):
            return tuple(str(item) for item in parsed)
    return ()


def _row_to_entry(row) -> MemoryEntryRecord:
    """Pack a SELECT row into :class:`MemoryEntryRecord`.

    Row shape: ``(id, scope, kind, title, body, tags, source, file_path,
    summary_path, created_at, updated_at, type, importance,
    superseded_by, ttl_at, scope_tier)``.
    """
    return MemoryEntryRecord(
        entry_id=int(row[0]),
        scope=row[1],
        kind=row[2],
        title=row[3],
        body=row[4],
        tags=_safe_tags(row[5]),
        source=row[6],
        file_path=row[7],
        summary_path=row[8],
        created_at=_stamp_str(row[9]),
        updated_at=_stamp_str(row[10]),
        type=row[11] if row[11] is not None else "project",
        importance=int(row[12]) if row[12] is not None else 3,
        superseded_by=int(row[13]) if row[13] is not None else None,
        ttl_at=_opt_stamp_str(row[14]),
        scope_tier=row[15] if row[15] is not None else "project",
    )


# --------------------------------------------------------------------- #
# memory_entries — CRUD
# --------------------------------------------------------------------- #


def record_memory_entry(
    *,
    scope: str,
    kind: str,
    title: str,
    body: str,
    tags: list[str],
    source: str,
    file_path: str,
    summary_path: str,
    type: str = "project",  # noqa: A002 — match StateStore signature
    importance: int = 3,
    superseded_by: int | None = None,
    ttl_at: str | None = None,
    scope_tier: str = "project",
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> MemoryEntryRecord:
    """Append a memory entry and return the populated record.

    Mirrors :meth:`StateStore.record_memory_entry`: tags are stored as
    a JSON-encoded list (``json.dumps([...])``); the
    ``title_body_tsv`` generated column updates automatically.
    """
    if pool is None:
        from pollypm.storage.pg_pool import get_rw_pool

        pool = get_rw_pool(config)
    now = _now_iso()
    tags_json = json.dumps([str(tag) for tag in tags], ensure_ascii=True)
    def _insert_once() -> int:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO memory_entries (
                    scope, kind, title, body, tags, source, file_path, summary_path,
                    created_at, updated_at, type, importance, superseded_by, ttl_at, scope_tier
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    scope,
                    kind,
                    title,
                    body,
                    tags_json,
                    source,
                    file_path,
                    summary_path,
                    now,
                    now,
                    type,
                    int(importance),
                    superseded_by,
                    ttl_at,
                    scope_tier,
                ),
            )
            row = cur.fetchone()
            return int(row[0])

    try:
        entry_id = _insert_once()
    except pg_errors.UniqueViolation as exc:
        if not _is_memory_entries_pkey_violation(exc):
            raise
        from pollypm.storage.pg_sequence_health import repair_owned_sequences

        repaired = repair_owned_sequences(
            pool=pool,
            only={("memory_entries", "id")},
        )
        if repaired:
            logger.warning(
                "memory_entries id sequence was behind table max; repaired %s and retrying insert",
                repaired[0].qualified_sequence_name,
            )
        else:
            logger.warning(
                "memory_entries primary-key insert collided; sequence is no longer skewed after nextval, retrying insert"
            )
        entry_id = _insert_once()
    return MemoryEntryRecord(
        entry_id=entry_id,
        scope=scope,
        kind=kind,
        title=title,
        body=body,
        tags=tuple(str(t) for t in tags),
        source=source,
        file_path=file_path,
        summary_path=summary_path,
        created_at=now,
        updated_at=now,
        type=type,
        importance=int(importance),
        superseded_by=superseded_by,
        ttl_at=ttl_at,
        scope_tier=scope_tier,
    )


def get_memory_entry(
    entry_id: int,
    *,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> MemoryEntryRecord | None:
    """Return a single memory entry by id, or ``None`` if absent."""
    if pool is None:
        from pollypm.storage.pg_pool import get_ro_pool

        pool = get_ro_pool(config)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, scope, kind, title, body, tags, source, file_path, summary_path,
                   created_at, updated_at, type, importance, superseded_by, ttl_at, scope_tier
            FROM memory_entries
            WHERE id = %s
            """,
            (int(entry_id),),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return _row_to_entry(row)


def list_memory_entries(
    *,
    scope: str | None = None,
    kind: str | None = None,
    type: str | None = None,  # noqa: A002 — match StateStore signature
    scope_tier: str | None = None,
    limit: int = 50,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> list[MemoryEntryRecord]:
    """List memory entries with optional filters, newest first.

    Mirrors :meth:`StateStore.list_memory_entries`: filters compose as
    ANDed equality predicates; ``None`` skips that filter entirely.
    """
    clauses: list[str] = []
    params: list[object] = []
    if scope is not None:
        clauses.append("scope = %s")
        params.append(scope)
    if kind is not None:
        clauses.append("kind = %s")
        params.append(kind)
    if type is not None:
        clauses.append("type = %s")
        params.append(type)
    if scope_tier is not None:
        clauses.append("scope_tier = %s")
        params.append(scope_tier)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    if pool is None:
        from pollypm.storage.pg_pool import get_ro_pool

        pool = get_ro_pool(config)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, scope, kind, title, body, tags, source, file_path, summary_path,
                   created_at, updated_at, type, importance, superseded_by, ttl_at, scope_tier
            FROM memory_entries
            {where}
            ORDER BY id DESC
            LIMIT %s
            """,
            (*params, int(limit)),
        )
        rows = cur.fetchall()
    return [_row_to_entry(row) for row in rows]


def delete_memory_entry(
    entry_id: int,
    *,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> bool:
    """Hard-delete a memory entry by id. Returns True when a row was removed."""
    if pool is None:
        from pollypm.storage.pg_pool import get_rw_pool

        pool = get_rw_pool(config)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM memory_entries WHERE id = %s",
            (int(entry_id),),
        )
        removed = cur.rowcount or 0
    return removed > 0


def update_memory_entry(
    entry_id: int,
    *,
    body: str | None = None,
    importance: int | None = None,
    tags: list[str] | None = None,
    superseded_by: int | None = None,
    clear_superseded: bool = False,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> bool:
    """Patch a subset of fields on a memory row. Returns True on change.

    Mirrors :meth:`StateStore.update_memory_entry`. Fields left at
    ``None`` keep their current values; pass ``clear_superseded=True``
    to explicitly null the ``superseded_by`` column.
    """
    sets: list[str] = []
    params: list[object] = []
    if body is not None:
        sets.append("body = %s")
        params.append(str(body))
    if importance is not None:
        if not (1 <= int(importance) <= 5):
            raise ValueError(
                f"importance must be between 1 and 5 (got {importance})"
            )
        sets.append("importance = %s")
        params.append(int(importance))
    if tags is not None:
        sets.append("tags = %s")
        params.append(json.dumps([str(tag) for tag in tags], ensure_ascii=True))
    if clear_superseded:
        sets.append("superseded_by = NULL")
    elif superseded_by is not None:
        sets.append("superseded_by = %s")
        params.append(int(superseded_by))
    if not sets:
        return False
    now = _now_iso()
    sets.append("updated_at = %s")
    params.append(now)
    params.append(int(entry_id))
    if pool is None:
        from pollypm.storage.pg_pool import get_rw_pool

        pool = get_rw_pool(config)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"UPDATE memory_entries SET {', '.join(sets)} WHERE id = %s",
            tuple(params),
        )
        changed = cur.rowcount or 0
    return changed > 0


def recall_memory_entries(
    *,
    query: str,
    scopes: list[str] | None = None,
    types: list[str] | None = None,
    importance_min: int = 1,
    limit: int = 10,
    candidate_multiplier: int = 5,
    scope_tiers: list[str] | None = None,
    tier_scope_pairs: list[tuple[str, str]] | None = None,
    include_superseded: bool = False,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> list[tuple[MemoryEntryRecord, float | None]]:
    """Keyword-only recall against ``memory_entries.title_body_tsv``.

    Returns ``(record, ts_rank_score_or_None)`` pairs ordered by
    descending rank. Mirrors :meth:`StateStore.recall_memory_entries`:

    * empty ``query`` skips the tsvector match and orders rows by
      ``id DESC`` for "show me recent" callers.
    * ``include_superseded=False`` (default) filters out rows whose
      ``superseded_by`` is set — matches the M01 contract.
    * ``ttl_at`` in the past is filtered out so expired rows don't
      surface even when the sweep hasn't yet run.

    The hybrid pgvector + ts_rank recall lives in
    :mod:`pollypm.storage.memory_recall`; this surface is the StateStore
    parity path used by :class:`FileMemoryBackend`. The "bm25_score"
    return slot from the sqlite path becomes ts_rank_cd on pg — the
    file backend's blender in
    :mod:`pollypm.memory_backends.file` treats it as an opaque
    "higher = better" score so the change is transparent.
    """
    clauses: list[str] = []
    params: list[object] = []
    if not include_superseded:
        clauses.append("me.superseded_by IS NULL")
    now_iso = _now_iso()
    clauses.append("(me.ttl_at IS NULL OR me.ttl_at > %s)")
    params.append(now_iso)
    if scopes:
        clauses.append("me.scope = ANY(%s)")
        params.append(list(scopes))
    if types:
        clauses.append("me.type = ANY(%s)")
        params.append(list(types))
    if scope_tiers:
        clauses.append("me.scope_tier = ANY(%s)")
        params.append(list(scope_tiers))
    if tier_scope_pairs:
        pair_clauses: list[str] = []
        for tier, scope_id in tier_scope_pairs:
            pair_clauses.append("(me.scope_tier = %s AND me.scope = %s)")
            params.extend([tier, scope_id])
        if pair_clauses:
            clauses.append("(" + " OR ".join(pair_clauses) + ")")
    if importance_min > 1:
        clauses.append("me.importance >= %s")
        params.append(int(importance_min))

    query_text = (query or "").strip()
    fetch_limit = max(int(limit) * int(candidate_multiplier), int(limit))

    if pool is None:
        from pollypm.storage.pg_pool import get_ro_pool

        pool = get_ro_pool(config)

    where = " AND ".join(clauses)
    if query_text:
        # Use plainto_tsquery against the generated title_body_tsv column.
        # ts_rank_cd returns "higher = better" which matches the
        # negative-bm25 contract the file backend's recall blender
        # expects.
        sql = f"""
        SELECT me.id, me.scope, me.kind, me.title, me.body, me.tags, me.source,
               me.file_path, me.summary_path, me.created_at, me.updated_at,
               me.type, me.importance, me.superseded_by, me.ttl_at, me.scope_tier,
               ts_rank_cd(me.title_body_tsv, plainto_tsquery('english', %s)) AS score
        FROM memory_entries me
        WHERE me.title_body_tsv @@ plainto_tsquery('english', %s)
          AND {where}
        ORDER BY score DESC
        LIMIT %s
        """
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (query_text, query_text, *params, fetch_limit))
            rows = cur.fetchall()
    else:
        sql = f"""
        SELECT me.id, me.scope, me.kind, me.title, me.body, me.tags, me.source,
               me.file_path, me.summary_path, me.created_at, me.updated_at,
               me.type, me.importance, me.superseded_by, me.ttl_at, me.scope_tier,
               NULL::float AS score
        FROM memory_entries me
        WHERE {where}
        ORDER BY me.id DESC
        LIMIT %s
        """
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (*params, fetch_limit))
            rows = cur.fetchall()

    results: list[tuple[MemoryEntryRecord, float | None]] = []
    for row in rows:
        record = _row_to_entry(row[:16])
        score = float(row[16]) if row[16] is not None else None
        results.append((record, score))
    return results


# --------------------------------------------------------------------- #
# Tiered-scope lifecycle (M03 / #232)
# --------------------------------------------------------------------- #


def purge_session_scope(
    session_id: str,
    *,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> int:
    """Delete every session-tier memory entry with ``scope = session_id``.

    Returns the number of rows removed. Idempotent — calling twice
    removes zero on the second call.
    """
    if pool is None:
        from pollypm.storage.pg_pool import get_rw_pool

        pool = get_rw_pool(config)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM memory_entries WHERE scope_tier = 'session' AND scope = %s",
            (session_id,),
        )
        return cur.rowcount or 0


def expire_task_scope(
    task_id: str,
    *,
    terminal_at: str | None = None,
    ttl_days: int = 30,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> int:
    """Stamp a 30-day TTL on task-tier entries with ``scope = task_id``.

    Mirrors :meth:`StateStore.expire_task_scope`: uses LEAST() so an
    explicit earlier TTL is never extended. Returns the row count
    actually modified.
    """
    if terminal_at is None:
        now = datetime.now(UTC)
    else:
        now = datetime.fromisoformat(terminal_at)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
    ttl_at = (now + timedelta(days=int(ttl_days))).isoformat()
    if pool is None:
        from pollypm.storage.pg_pool import get_rw_pool

        pool = get_rw_pool(config)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE memory_entries
            SET ttl_at = LEAST(COALESCE(ttl_at, %s::timestamptz), %s::timestamptz),
                updated_at = %s
            WHERE scope_tier = 'task' AND scope = %s
            """,
            (ttl_at, ttl_at, now.isoformat(), task_id),
        )
        return cur.rowcount or 0


def sweep_expired_memory_entries(
    *,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> int:
    """Drop ``memory_entries`` whose ``ttl_at`` has elapsed.

    Only touches rows with a non-NULL ``ttl_at``. Returns the number
    of rows deleted. Mirrors :meth:`StateStore.sweep_expired_memory_entries`.
    """
    if pool is None:
        from pollypm.storage.pg_pool import get_rw_pool

        pool = get_rw_pool(config)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM memory_entries WHERE ttl_at IS NOT NULL AND ttl_at < now()"
        )
        return cur.rowcount or 0


# --------------------------------------------------------------------- #
# memory_summaries
# --------------------------------------------------------------------- #


def record_memory_summary(
    *,
    scope: str,
    summary_text: str,
    summary_path: str,
    entry_count: int,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> MemorySummaryRecord:
    """Append a memory_summary row and return the populated record."""
    if pool is None:
        from pollypm.storage.pg_pool import get_rw_pool

        pool = get_rw_pool(config)
    now = _now_iso()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO memory_summaries (scope, summary_text, summary_path, entry_count, created_at)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (scope, summary_text, summary_path, int(entry_count), now),
        )
        row = cur.fetchone()
        summary_id = int(row[0])
    return MemorySummaryRecord(
        summary_id=summary_id,
        scope=scope,
        summary_text=summary_text,
        summary_path=summary_path,
        entry_count=int(entry_count),
        created_at=now,
    )


def latest_memory_summary(
    scope: str,
    *,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> MemorySummaryRecord | None:
    """Return the most-recently inserted summary for ``scope`` or ``None``."""
    if pool is None:
        from pollypm.storage.pg_pool import get_ro_pool

        pool = get_ro_pool(config)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, scope, summary_text, summary_path, entry_count, created_at
            FROM memory_summaries
            WHERE scope = %s
            ORDER BY id DESC
            LIMIT 1
            """,
            (scope,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return MemorySummaryRecord(
        summary_id=int(row[0]),
        scope=row[1],
        summary_text=row[2],
        summary_path=row[3],
        entry_count=int(row[4]),
        created_at=_stamp_str(row[5]),
    )


class PgMemoryStore:
    """Lightweight adapter exposing StateStore-shaped memory methods on pg.

    :class:`pollypm.memory_backends.file.FileMemoryBackend` was written
    against :class:`pollypm.storage.state.StateStore` and calls a
    handful of memory_* methods on it. This adapter offers the same
    method names but routes each call through the module-level pg
    functions above. Backend selection happens at
    :func:`pollypm.memory_backends.get_memory_backend` time.

    The instance is stateless (no pool kept on ``self``) so it is
    cheap to construct and safe to share across threads — each call
    pulls a connection from the process-wide pool.
    """

    def record_memory_entry(self, **kwargs) -> MemoryEntryRecord:
        return record_memory_entry(**kwargs)

    def get_memory_entry(self, entry_id: int) -> MemoryEntryRecord | None:
        return get_memory_entry(entry_id)

    def list_memory_entries(self, **kwargs) -> list[MemoryEntryRecord]:
        return list_memory_entries(**kwargs)

    def recall_memory_entries(self, **kwargs):
        return recall_memory_entries(**kwargs)

    def purge_session_scope(self, session_id: str) -> int:
        return purge_session_scope(session_id)

    def expire_task_scope(self, task_id: str, **kwargs) -> int:
        return expire_task_scope(task_id, **kwargs)

    def delete_memory_entry(self, entry_id: int) -> bool:
        return delete_memory_entry(entry_id)

    def update_memory_entry(self, entry_id: int, **kwargs) -> bool:
        return update_memory_entry(entry_id, **kwargs)

    def record_memory_summary(self, **kwargs) -> MemorySummaryRecord:
        return record_memory_summary(**kwargs)

    def latest_memory_summary(self, scope: str) -> MemorySummaryRecord | None:
        return latest_memory_summary(scope)

    def sweep_expired_memory_entries(self) -> int:
        return sweep_expired_memory_entries()

    def close(self) -> None:
        """No-op — the pg adapter doesn't own a connection lifetime."""


__all__ = [
    "PgMemoryStore",
    "delete_memory_entry",
    "expire_task_scope",
    "get_memory_entry",
    "latest_memory_summary",
    "list_memory_entries",
    "purge_session_scope",
    "recall_memory_entries",
    "record_memory_entry",
    "record_memory_summary",
    "sweep_expired_memory_entries",
    "update_memory_entry",
]
