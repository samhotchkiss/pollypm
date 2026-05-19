"""Background embedding writer for pgvector recall (issue #1737, Slice D).

When a row lands in ``messages``, ``work_context_entries`` or
``memory_entries`` we want a corresponding row in ``embeddings`` so the
recall query has a vector to compare against. Doing the embed inline on
the write path would (a) couple every domain write to an outbound HTTP
call and (b) block the caller for hundreds of milliseconds. Instead we
queue ``(source_table, source_id)`` pairs on an in-process queue and
drain it from a background thread that batches rows into 32-at-a-time
embed calls.

Catastrophic-failure contract
-----------------------------

The writer thread MUST NOT crash on:

* missing API key (the embedder raises ``EmbedderError`` → log + drop)
* HTTP errors after retries are exhausted (log + drop the batch)
* missing source rows (log + drop; the row may have been deleted
  between enqueue and drain)
* psycopg connection errors (log; retry the batch one more time, then
  drop)

The contract is "embedding is best-effort coverage". A row without an
embedding is still recallable via the FTS leg of the hybrid scorer.

Idempotency
-----------

Before embedding, the writer checks the ``embeddings`` table for an
existing row at ``(source_table, source_id)``. If one is present the
row is skipped — re-enqueueing after a process restart is therefore
free.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from queue import Empty, Queue
from typing import TYPE_CHECKING, Iterable

from pollypm.storage.embedder import Embedder, EmbedderError

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool


logger = logging.getLogger(__name__)


# Per-#1737 spec: batch 32 rows per embed call. Picked to amortise the
# HTTP overhead while keeping per-batch latency under a second on the
# default text-embedding-3-small model.
DEFAULT_EMBED_BATCH = 32

# How long the drain thread waits between empty-queue polls. Tests
# inject a tiny value so the thread exits cheaply; production uses 0.5s.
DEFAULT_POLL_INTERVAL = 0.5

# Tables we know about. Anything else enqueued is rejected with a
# warning so a typo in the writer-call site shows up immediately
# rather than silently never embedding.
KNOWN_SOURCE_TABLES = frozenset({
    "messages",
    "work_context_entries",
    "memory_entries",
})


@dataclass(slots=True, frozen=True)
class EmbedJob:
    """A single ``(source_table, source_id)`` enqueued for embedding."""

    source_table: str
    source_id: str


# --------------------------------------------------------------------- #
# Source-text extractors.
# --------------------------------------------------------------------- #
# Each source table has a different shape — extract the text we
# actually want to embed, falling back to empty string when the row
# is missing or the columns are NULL.


_SOURCE_TEXT_SQL: dict[str, str] = {
    "messages": (
        "SELECT coalesce(subject, '') || E'\\n' || coalesce(body, '') "
        "FROM messages WHERE id::text = %s"
    ),
    "work_context_entries": (
        "SELECT coalesce(text, '') "
        "FROM work_context_entries WHERE id::text = %s"
    ),
    "memory_entries": (
        "SELECT coalesce(title, '') || E'\\n' || coalesce(body, '') "
        "FROM memory_entries WHERE id::text = %s"
    ),
}


def _fetch_source_text(conn, table: str, source_id: str) -> str | None:
    """Look up the text to embed for ``(table, source_id)``.

    Returns ``None`` when the row no longer exists (legitimate race —
    a writer enqueued the row, a curator/delete swept it before the
    drain thread ran). The writer skips ``None`` rows quietly.
    """
    sql = _SOURCE_TEXT_SQL.get(table)
    if sql is None:
        return None
    with conn.cursor() as cur:
        cur.execute(sql, (source_id,))
        row = cur.fetchone()
    if row is None:
        return None
    return row[0] or ""


def _filter_existing(
    conn,
    jobs: list[EmbedJob],
) -> list[EmbedJob]:
    """Return only the jobs that aren't already in ``embeddings``.

    Skipping pre-existing rows is what makes the writer idempotent —
    a backfill + a fresh enqueue race converge instead of double-billing
    the OpenAI account.
    """
    if not jobs:
        return []
    keys = [(j.source_table, j.source_id) for j in jobs]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_table, source_id FROM embeddings "
            "WHERE (source_table, source_id) IN ("
            + ",".join(["(%s, %s)"] * len(keys))
            + ")",
            [v for pair in keys for v in pair],
        )
        existing = {(row[0], row[1]) for row in cur.fetchall()}
    return [j for j in jobs if (j.source_table, j.source_id) not in existing]


def _write_embeddings(
    conn,
    rows: list[tuple[str, str, list[float], str]],
) -> None:
    """Insert (or overwrite) embedding rows in one shot.

    Each ``rows`` entry is ``(source_table, source_id, vector, model)``.
    The ``ON CONFLICT`` clause makes the writer safe to re-run against
    rows that were just inserted by another process.
    """
    if not rows:
        return
    # psycopg's pgvector adapter is opt-in via pgvector.psycopg.register_vector
    # — without it psycopg sends the list as a Postgres array. We always
    # call ``register_vector`` once per connection in :func:`_ensure_vector`.
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO embeddings (source_table, source_id, embedding, model) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (source_table, source_id) DO UPDATE "
            "SET embedding = EXCLUDED.embedding, "
            "    model = EXCLUDED.model, "
            "    generated_at = now()",
            rows,
        )


def _ensure_vector(conn) -> None:
    """Register pgvector's psycopg adapter on this connection.

    Idempotent — the adapter sets a flag on the connection so a second
    call is a no-op. Localised so the writer doesn't import pgvector
    unless it's actually going to embed.
    """
    try:
        from pgvector.psycopg import register_vector
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise EmbedderError(
            "pgvector adapter not installed; the postgres extra "
            "must include `pgvector` for the embedding writer to "
            "speak the vector type"
        ) from exc
    register_vector(conn)


# --------------------------------------------------------------------- #
# Public writer.
# --------------------------------------------------------------------- #


class EmbeddingWriter:
    """Background drain thread that turns ``EmbedJob`` queue items into
    rows in the ``embeddings`` table.

    Public surface:

    * :meth:`enqueue` — non-blocking; called by the write hooks.
    * :meth:`start` — spawn the drain thread.
    * :meth:`stop` — signal shutdown; joins with a bounded timeout.
    * :meth:`drain_once` — synchronous drain pass; tests + the backfill
      CLI use this to embed without spawning a thread.
    """

    def __init__(
        self,
        pool: "ConnectionPool",
        embedder: Embedder,
        *,
        batch_size: int = DEFAULT_EMBED_BATCH,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        self._pool = pool
        self._embedder = embedder
        self._batch_size = max(1, min(batch_size, embedder.max_batch_size))
        self._poll_interval = poll_interval
        self._queue: Queue[EmbedJob] = Queue()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # Tests use this to assert exactly N batches landed without
        # racing the queue.
        self.metrics_batches_processed = 0
        self.metrics_rows_embedded = 0
        self.metrics_rows_skipped = 0
        self.metrics_batch_failures = 0

    # --- enqueue surface ---------------------------------------------- #

    def enqueue(self, source_table: str, source_id: str | int) -> None:
        """Queue ``(source_table, source_id)`` for embedding.

        Non-blocking. Unknown tables log + drop — that's a programming
        error in the caller, not a runtime condition, so we don't want
        it to silently soak up writer time.
        """
        if source_table not in KNOWN_SOURCE_TABLES:
            logger.warning(
                "embedding_writer: unknown source_table=%r; ignoring",
                source_table,
            )
            return
        self._queue.put(EmbedJob(source_table=source_table, source_id=str(source_id)))

    def enqueue_many(self, jobs: Iterable[EmbedJob]) -> None:
        for job in jobs:
            self.enqueue(job.source_table, job.source_id)

    # --- thread lifecycle --------------------------------------------- #

    def start(self) -> None:
        """Spawn the drain thread. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="pollypm-embedding-writer",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        """Signal shutdown and join the thread. Safe to call when stopped."""
        self._stop_event.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=timeout)
        self._thread = None

    def _run(self) -> None:
        """Drain loop: gather a batch, embed, write, repeat."""
        while not self._stop_event.is_set():
            try:
                jobs = self._collect_batch()
                if not jobs:
                    # Sleep on the event so stop() unblocks promptly.
                    self._stop_event.wait(self._poll_interval)
                    continue
                self._process_batch(jobs)
            except Exception:  # noqa: BLE001 — never crash the writer
                logger.exception("embedding_writer: loop iteration crashed")
                self.metrics_batch_failures += 1
                self._stop_event.wait(self._poll_interval)

    def _collect_batch(self) -> list[EmbedJob]:
        """Drain up to ``batch_size`` items from the queue.

        Returns immediately if the queue is empty — the caller sleeps.
        """
        jobs: list[EmbedJob] = []
        deadline_reached = False
        while len(jobs) < self._batch_size and not deadline_reached:
            try:
                job = self._queue.get_nowait()
            except Empty:
                deadline_reached = True
                continue
            jobs.append(job)
        return jobs

    # --- batch processing --------------------------------------------- #

    def drain_once(self, *, max_batches: int = 1024) -> int:
        """Synchronous drain: pull and embed until the queue empties.

        Returns the count of rows successfully embedded. Used by the
        backfill CLI (no thread spawn) and by tests that want
        deterministic timing.
        """
        rows_embedded = 0
        for _ in range(max_batches):
            jobs = self._collect_batch()
            if not jobs:
                break
            rows_embedded += self._process_batch(jobs)
        return rows_embedded

    def _process_batch(self, jobs: list[EmbedJob]) -> int:
        """Embed one batch end-to-end. Returns rows successfully written."""
        self.metrics_batches_processed += 1
        try:
            with self._pool.connection() as conn:
                _ensure_vector(conn)

                # Idempotency: skip rows that already have an embedding.
                pending = _filter_existing(conn, jobs)
                if not pending:
                    self.metrics_rows_skipped += len(jobs)
                    return 0
                self.metrics_rows_skipped += len(jobs) - len(pending)

                # Fetch the source text for each pending row.
                texts: list[str] = []
                resolved: list[EmbedJob] = []
                for job in pending:
                    text = _fetch_source_text(
                        conn, job.source_table, job.source_id,
                    )
                    if text is None:
                        # Source row gone (deleted between enqueue + drain).
                        self.metrics_rows_skipped += 1
                        continue
                    texts.append(text)
                    resolved.append(job)

                if not resolved:
                    return 0

                # Run the embedder OUTSIDE the connection's implicit
                # transaction so a slow HTTP call doesn't hold a pg
                # connection. ``with self._pool.connection()`` will
                # commit + return the connection here; we re-open
                # one for the write below.
        except Exception:  # noqa: BLE001
            logger.exception(
                "embedding_writer: prep failed for %d jobs; dropping batch",
                len(jobs),
            )
            self.metrics_batch_failures += 1
            return 0

        try:
            vectors = self._embedder.embed(texts)
        except EmbedderError as exc:
            logger.warning(
                "embedding_writer: embedder failed: %r; dropping %d rows",
                exc,
                len(resolved),
            )
            self.metrics_batch_failures += 1
            return 0
        except Exception:  # noqa: BLE001
            logger.exception(
                "embedding_writer: embedder crashed; dropping %d rows",
                len(resolved),
            )
            self.metrics_batch_failures += 1
            return 0

        if len(vectors) != len(resolved):
            logger.warning(
                "embedding_writer: embedder returned %d vectors for %d "
                "jobs; dropping batch",
                len(vectors),
                len(resolved),
            )
            self.metrics_batch_failures += 1
            return 0

        model_label = self._embedder.info.model
        write_rows = [
            (j.source_table, j.source_id, vec, model_label)
            for j, vec in zip(resolved, vectors)
        ]
        try:
            with self._pool.connection() as conn:
                _ensure_vector(conn)
                _write_embeddings(conn, write_rows)
                conn.commit()
        except Exception:  # noqa: BLE001
            logger.exception(
                "embedding_writer: write failed for %d rows; dropping",
                len(write_rows),
            )
            self.metrics_batch_failures += 1
            return 0

        self.metrics_rows_embedded += len(write_rows)
        return len(write_rows)


# --------------------------------------------------------------------- #
# Module-level singleton — installed by ``pm cockpit`` startup so
# write-hook callers don't have to thread a writer through every site.
# Slice D ships the wiring; Slice H is the place to flip the write
# hooks on. The singleton is lazily created so tests can wholly skip
# the writer by not calling get_embedding_writer().
# --------------------------------------------------------------------- #


_WRITER_LOCK = threading.Lock()
_WRITER: EmbeddingWriter | None = None


def get_embedding_writer() -> EmbeddingWriter | None:
    """Return the process-wide writer if one has been installed."""
    return _WRITER


def install_embedding_writer(writer: EmbeddingWriter) -> None:
    """Install ``writer`` as the process singleton, replacing any prior."""
    global _WRITER
    with _WRITER_LOCK:
        previous = _WRITER
        _WRITER = writer
    if previous is not None:
        try:
            previous.stop()
        except Exception:  # noqa: BLE001 — never raise on shutdown
            logger.warning("embedding_writer: previous stop failed", exc_info=True)


def shutdown_embedding_writer() -> None:
    """Stop and clear the singleton. Idempotent."""
    global _WRITER
    with _WRITER_LOCK:
        writer = _WRITER
        _WRITER = None
    if writer is not None:
        try:
            writer.stop()
        except Exception:  # noqa: BLE001
            logger.warning("embedding_writer: stop failed", exc_info=True)


def enqueue_embedding(source_table: str, source_id: str | int) -> None:
    """Convenience: enqueue against the singleton if installed.

    Safe to call when no writer is installed (sqlite-backed installs
    or test runs that don't exercise the pg path). The whole call is
    a no-op in that case.
    """
    writer = _WRITER
    if writer is None:
        return
    try:
        writer.enqueue(source_table, source_id)
    except Exception:  # noqa: BLE001
        logger.exception(
            "embedding_writer: enqueue failed for %s/%s",
            source_table,
            source_id,
        )


__all__ = [
    "DEFAULT_EMBED_BATCH",
    "DEFAULT_POLL_INTERVAL",
    "EmbedJob",
    "EmbeddingWriter",
    "KNOWN_SOURCE_TABLES",
    "enqueue_embedding",
    "get_embedding_writer",
    "install_embedding_writer",
    "shutdown_embedding_writer",
]
