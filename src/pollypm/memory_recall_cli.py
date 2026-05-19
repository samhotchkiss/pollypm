"""CLI surface for pgvector-backed hybrid recall (issue #1737, Slice D).

Adds two commands under the existing ``pm memory`` Typer:

* ``pm memory pg-recall "<query>" [--limit N] [--source messages,...]``
  Runs the hybrid pgvector + FTS recall query against the workspace
  pg backend. Prints results as a table or JSON.
* ``pm memory backfill-embeddings [--source ...] [--limit N]``
  Walks ``messages`` / ``work_context_entries`` / ``memory_entries``
  for rows without a row in ``embeddings`` and embeds them. Idempotent
  and resumable.

These are registered on the existing ``memory_app`` from
:mod:`pollypm.memory_cli` so they appear under ``pm memory ...`` in
``pm --help``.

A separate module (not in-line in ``memory_cli.py``) so the file-backed
memory commands stay readable and the pg-backed surface can grow
without churning the legacy file. The legacy ``pm memory recall``
keeps its FTS+importance+recency behaviour against the FileMemoryBackend;
the pg surface ships under ``pm memory pg-recall``. Once Slice H deletes
the legacy backend the pg-recall command becomes the canonical
``pm memory recall``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable

import typer

from pollypm.config import DEFAULT_CONFIG_PATH, load_config, resolve_config_path
from pollypm.memory_cli import memory_app
from pollypm.storage.memory_recall import (
    RecallHit,
    SUPPORTED_SOURCES,
    recall as run_recall,
)


logger = logging.getLogger(__name__)


# --------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------- #


def _parse_source_list(raw: str | None) -> list[str] | None:
    """Parse the ``--source`` CSV into a clean list.

    Accepts ``--source messages,work_context_entries`` or
    ``--source memory_entries``. Also accepts the short alias
    ``work_context`` as a synonym for ``work_context_entries`` because
    that's what Sam will type. ``None`` means "all supported sources".
    """
    if raw is None:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        return None
    canonical: list[str] = []
    for part in parts:
        if part == "work_context":
            canonical.append("work_context_entries")
            continue
        canonical.append(part)
    unknown = sorted(set(canonical) - set(SUPPORTED_SOURCES))
    if unknown:
        raise typer.BadParameter(
            f"--source has unknown values {unknown!r}; "
            f"choose from {list(SUPPORTED_SOURCES)!r}"
        )
    return canonical


def _hit_to_dict(hit: RecallHit) -> dict[str, object]:
    return {
        "source_table": hit.source_table,
        "source_id": hit.source_id,
        "score": round(hit.score, 4),
        "text_preview": hit.text_preview,
        "metadata": hit.metadata,
    }


def _print_hit_table(hits: Iterable[RecallHit]) -> None:
    """Render hits as a human-readable column table."""
    rows = list(hits)
    if not rows:
        typer.echo("No results.")
        return
    typer.echo(
        f"{'score':>6}  {'source':<22} {'id':>8}  preview"
    )
    typer.echo("-" * 80)
    for hit in rows:
        preview = hit.text_preview[:60]
        typer.echo(
            f"{hit.score:6.3f}  "
            f"{hit.source_table:<22} "
            f"{hit.source_id:>8}  "
            f"{preview}"
        )


# --------------------------------------------------------------------- #
# pg-recall command.
# --------------------------------------------------------------------- #


@memory_app.command("pg-recall")
def pg_recall(
    query: str = typer.Argument(..., help="Free-text query."),
    limit: int = typer.Option(10, "--limit", help="Max hits to return."),
    source: str | None = typer.Option(
        None,
        "--source",
        help=(
            "Comma-separated source tables: messages, work_context_entries, "
            "memory_entries (or the alias 'work_context'). Default: all three."
        ),
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit structured JSON.",
    ),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
) -> None:
    """Hybrid pgvector + FTS recall (Slice D)."""
    if limit <= 0:
        raise typer.BadParameter("--limit must be >= 1")

    resolved = resolve_config_path(config_path)
    if not resolved.exists():
        from pollypm.errors import format_config_not_found_error

        raise typer.BadParameter(format_config_not_found_error(resolved))
    config = load_config(resolved)
    sources = _parse_source_list(source)

    try:
        hits = run_recall(
            query,
            limit=limit,
            source_tables=sources,
            config=config,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to operator
        if json_output:
            typer.echo(json.dumps({"error": str(exc)}))
        else:
            typer.echo(f"recall failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if json_output:
        typer.echo(
            json.dumps(
                [_hit_to_dict(h) for h in hits],
                indent=2,
                sort_keys=True,
                default=str,
            )
        )
        return

    _print_hit_table(hits)


# --------------------------------------------------------------------- #
# backfill-embeddings command.
# --------------------------------------------------------------------- #


@memory_app.command("backfill-embeddings")
def backfill_embeddings(
    source: str = typer.Option(
        "all",
        "--source",
        help=(
            "Single source table to backfill, or 'all'. Choices: "
            "messages, work_context_entries, memory_entries, all."
        ),
    ),
    limit: int = typer.Option(
        0,
        "--limit",
        help="Max rows to process per source (0 = no cap).",
    ),
    batch_size: int = typer.Option(
        32, "--batch-size", help="Rows per embed batch.",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit structured JSON summary.",
    ),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
) -> None:
    """Scan source tables for rows missing embeddings; embed them.

    Idempotent: skips rows that already have an ``embeddings`` row at
    ``(source_table, source_id)``. Resumable: a re-run after a partial
    backfill picks up exactly where it left off.
    """
    if limit < 0:
        raise typer.BadParameter("--limit must be >= 0")
    if batch_size <= 0:
        raise typer.BadParameter("--batch-size must be >= 1")

    if source == "all":
        targets = list(SUPPORTED_SOURCES)
    else:
        targets = _parse_source_list(source) or []
        if not targets:
            raise typer.BadParameter("--source produced an empty list")

    resolved = resolve_config_path(config_path)
    if not resolved.exists():
        from pollypm.errors import format_config_not_found_error

        raise typer.BadParameter(format_config_not_found_error(resolved))
    config = load_config(resolved)

    # Resolve the embedder + pool exactly once so a thousand-row
    # backfill doesn't re-look-up the API key on every batch.
    from pollypm.storage import pg_pool
    from pollypm.storage.embedder import resolve_embedder
    from pollypm.storage.embedding_writer import EmbeddingWriter

    embedder = resolve_embedder(
        config.storage.embedding.model,
        config.storage.embedding.api_key_env,
    )
    pool = pg_pool.get_rw_pool(config)
    writer = EmbeddingWriter(pool, embedder, batch_size=batch_size)

    summary: dict[str, dict[str, int]] = {}
    for table in targets:
        scanned, queued = _enqueue_missing(pool, table, writer, limit)
        embedded = writer.drain_once(max_batches=10_000)
        summary[table] = {
            "scanned": scanned,
            "queued": queued,
            "embedded": embedded,
            "failures": writer.metrics_batch_failures,
        }
        # Reset per-table counters so the next table's summary is clean.
        writer.metrics_batch_failures = 0
        writer.metrics_batches_processed = 0
        writer.metrics_rows_embedded = 0
        writer.metrics_rows_skipped = 0

    if json_output:
        typer.echo(json.dumps(summary, indent=2, sort_keys=True))
        return

    for table, stats in summary.items():
        typer.echo(
            f"{table:<22}  scanned={stats['scanned']:>5}  "
            f"queued={stats['queued']:>5}  "
            f"embedded={stats['embedded']:>5}  "
            f"failures={stats['failures']}"
        )


def _enqueue_missing(
    pool,
    table: str,
    writer,
    limit: int,
) -> tuple[int, int]:
    """Scan ``table`` for rows lacking an ``embeddings`` row; enqueue.

    Returns ``(scanned, queued)``. ``scanned`` is the number of rows
    inspected; ``queued`` is the subset that didn't already have a row
    in ``embeddings``. A ``limit`` of 0 means "no cap" — the backfill
    walks the whole table.
    """
    if table not in SUPPORTED_SOURCES:
        raise ValueError(f"backfill: unsupported source {table!r}")

    sql_lookup = {
        "messages": "SELECT id::text FROM messages ORDER BY id",
        "work_context_entries": (
            "SELECT id::text FROM work_context_entries ORDER BY id"
        ),
        "memory_entries": "SELECT id::text FROM memory_entries ORDER BY id",
    }
    base_sql = sql_lookup[table]
    if limit > 0:
        base_sql = f"{base_sql} LIMIT {int(limit)}"

    scanned = 0
    candidates: list[str] = []
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(base_sql)
        for row in cur:
            scanned += 1
            candidates.append(str(row[0]))

    if not candidates:
        return scanned, 0

    # Single missing-row probe instead of per-row checks — same shape
    # as the writer's _filter_existing, but pulled inline so the
    # backfill CLI can report counts cleanly.
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT source_id FROM embeddings "
            "WHERE source_table = %s AND source_id = ANY(%s)",
            (table, candidates),
        )
        already = {row[0] for row in cur.fetchall()}

    missing = [sid for sid in candidates if sid not in already]
    for sid in missing:
        writer.enqueue(table, sid)
    return scanned, len(missing)


__all__ = [
    "backfill_embeddings",
    "pg_recall",
]
