"""Postgres serial-sequence alignment helpers.

These helpers inspect sequences owned by tables in the active schema and
repair only the unsafe case: the sequence's next value is at or below the
table's current max id. They never lower a sequence that is already ahead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable

from psycopg import sql

if TYPE_CHECKING:
    from psycopg import Connection
    from psycopg_pool import ConnectionPool

    from pollypm.models import PollyPMConfig


@dataclass(frozen=True, slots=True)
class PgSequenceStatus:
    table_schema: str
    table_name: str
    column_name: str
    sequence_schema: str
    sequence_name: str
    qualified_sequence_name: str
    max_value: int
    last_value: int
    is_called: bool

    @property
    def next_value(self) -> int:
        return self.last_value + 1 if self.is_called else self.last_value

    @property
    def skewed(self) -> bool:
        return self.max_value > 0 and self.next_value <= self.max_value

    def as_dict(self) -> dict[str, object]:
        return {
            "table": self.table_name,
            "column": self.column_name,
            "sequence": self.qualified_sequence_name,
            "max_value": self.max_value,
            "last_value": self.last_value,
            "is_called": self.is_called,
            "next_value": self.next_value,
            "skewed": self.skewed,
        }


@dataclass(frozen=True, slots=True)
class _SequenceMetadata:
    table_schema: str
    table_name: str
    column_name: str
    sequence_schema: str
    sequence_name: str
    qualified_sequence_name: str


def _pool_or_default(
    pool: "ConnectionPool | None",
    config: "PollyPMConfig | None",
) -> "ConnectionPool":
    if pool is not None:
        return pool
    from pollypm.storage.pg_pool import get_rw_pool

    return get_rw_pool(config)


def _owned_sequence_metadata(conn: "Connection") -> list[_SequenceMetadata]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                table_ns.nspname AS table_schema,
                table_rel.relname AS table_name,
                col.attname AS column_name,
                seq_ns.nspname AS sequence_schema,
                seq_rel.relname AS sequence_name,
                format('%I.%I', seq_ns.nspname, seq_rel.relname) AS qualified_sequence_name
            FROM pg_class AS seq_rel
            JOIN pg_namespace AS seq_ns
                ON seq_ns.oid = seq_rel.relnamespace
            JOIN pg_depend AS dep
                ON dep.objid = seq_rel.oid
                AND dep.deptype IN ('a', 'i')
            JOIN pg_class AS table_rel
                ON table_rel.oid = dep.refobjid
            JOIN pg_namespace AS table_ns
                ON table_ns.oid = table_rel.relnamespace
            JOIN pg_attribute AS col
                ON col.attrelid = table_rel.oid
                AND col.attnum = dep.refobjsubid
            WHERE seq_rel.relkind = 'S'
                AND table_rel.relkind IN ('r', 'p')
                AND table_ns.nspname = current_schema()
                AND seq_ns.nspname = current_schema()
            ORDER BY table_rel.relname, col.attname
            """
        )
        rows = cur.fetchall()
    return [
        _SequenceMetadata(
            table_schema=str(row[0]),
            table_name=str(row[1]),
            column_name=str(row[2]),
            sequence_schema=str(row[3]),
            sequence_name=str(row[4]),
            qualified_sequence_name=str(row[5]),
        )
        for row in rows
    ]


def _status_for_sequence(
    conn: "Connection",
    meta: _SequenceMetadata,
    *,
    lock_table: bool = False,
) -> PgSequenceStatus:
    table_ident = sql.Identifier(meta.table_schema, meta.table_name)
    column_ident = sql.Identifier(meta.column_name)
    sequence_ident = sql.Identifier(meta.sequence_schema, meta.sequence_name)
    with conn.cursor() as cur:
        if lock_table:
            cur.execute(
                sql.SQL("LOCK TABLE {} IN SHARE ROW EXCLUSIVE MODE").format(
                    table_ident
                )
            )
        cur.execute(
            sql.SQL("SELECT COALESCE(MAX({}), 0) FROM {}").format(
                column_ident,
                table_ident,
            )
        )
        max_row = cur.fetchone()
        cur.execute(
            sql.SQL("SELECT last_value, is_called FROM {}").format(
                sequence_ident
            )
        )
        sequence_row = cur.fetchone()
    return PgSequenceStatus(
        table_schema=meta.table_schema,
        table_name=meta.table_name,
        column_name=meta.column_name,
        sequence_schema=meta.sequence_schema,
        sequence_name=meta.sequence_name,
        qualified_sequence_name=meta.qualified_sequence_name,
        max_value=int(max_row[0]) if max_row and max_row[0] is not None else 0,
        last_value=int(sequence_row[0]),
        is_called=bool(sequence_row[1]),
    )


def collect_owned_sequence_statuses(
    *,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> list[PgSequenceStatus]:
    """Return serial-sequence alignment status for the active pg schema."""
    resolved_pool = _pool_or_default(pool, config)
    with resolved_pool.connection() as conn:
        metadata = _owned_sequence_metadata(conn)
        return [_status_for_sequence(conn, meta) for meta in metadata]


def skewed_owned_sequence_statuses(
    *,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> list[PgSequenceStatus]:
    return [
        status
        for status in collect_owned_sequence_statuses(pool=pool, config=config)
        if status.skewed
    ]


def repair_owned_sequences_in_connection(
    conn: "Connection",
    *,
    only: Iterable[tuple[str, str]] | None = None,
) -> list[PgSequenceStatus]:
    """Advance skewed owned sequences in ``conn``'s active schema.

    ``only`` matches ``(table_name, column_name)`` pairs. Repaired status
    objects describe the skew observed before ``setval``.
    """
    wanted = set(only or ())
    repaired: list[PgSequenceStatus] = []
    metadata = _owned_sequence_metadata(conn)
    with conn.cursor() as cur:
        for meta in metadata:
            if wanted and (meta.table_name, meta.column_name) not in wanted:
                continue
            status = _status_for_sequence(conn, meta, lock_table=True)
            if not status.skewed:
                continue
            target = max(status.max_value, status.last_value)
            cur.execute(
                "SELECT setval(%s::regclass, %s, true)",
                (status.qualified_sequence_name, target),
            )
            repaired.append(status)
    return repaired


def repair_owned_sequences(
    *,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
    only: Iterable[tuple[str, str]] | None = None,
) -> list[PgSequenceStatus]:
    """Advance every skewed owned sequence in the active pg schema."""
    resolved_pool = _pool_or_default(pool, config)
    with resolved_pool.connection() as conn:
        return repair_owned_sequences_in_connection(conn, only=only)


__all__ = [
    "PgSequenceStatus",
    "collect_owned_sequence_statuses",
    "repair_owned_sequences",
    "repair_owned_sequences_in_connection",
    "skewed_owned_sequence_statuses",
]
