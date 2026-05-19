"""Forward-only Postgres migration applier (issue #1737, Slice A).

The applier reads :data:`pollypm.storage.pg_schema.MIGRATIONS` and applies
any not yet recorded in ``schema_migrations``. Each migration runs inside
its own transaction so a partial apply rolls back cleanly.

Slice A ships exactly one migration: ``0001_initial`` (the full schema
port). Slices B-H append entries to ``MIGRATIONS``; the applier picks
them up transparently. The applier is **idempotent**: running it twice
against a fully-applied DB is a no-op (the ``schema_migrations`` row
already exists, so the version is skipped).

The bookkeeping table itself is bootstrapped before the migration walk
so the very first run can record its own application.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pollypm.storage.pg_schema import (
    EXTENSIONS,
    MIGRATIONS,
    SCHEMA_MIGRATIONS_TABLE,
)

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MigrationResult:
    """Summary of an ``apply_migrations`` run.

    ``applied`` lists ``(version, label)`` tuples for migrations that
    actually ran this invocation. ``already_applied`` mirrors the same
    shape for migrations the run found pre-recorded. Tests use both
    lists to assert idempotency.
    """

    applied: list[tuple[int, str]]
    already_applied: list[tuple[int, str]]

    @property
    def did_anything(self) -> bool:
        return bool(self.applied)


def _ensure_bookkeeping(conn) -> None:
    """Create the ``schema_migrations`` table + extensions if missing.

    Runs outside the per-migration transaction so a bare-empty pg can
    record migration 0001 in the same transaction it's applied with.
    The extension bootstrap also has to live here (not inside the
    per-version DDL) because pg parses each multi-statement DDL pack
    as a unit, and would refuse to parse ``vector(1536)`` if the type
    weren't already registered at session scope.
    """
    with conn.cursor() as cur:
        cur.execute(EXTENSIONS)
        cur.execute(SCHEMA_MIGRATIONS_TABLE)
    conn.commit()


def _applied_versions(conn) -> set[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_migrations")
        return {int(row[0]) for row in cur.fetchall()}


def _validate_migration_order() -> None:
    """Fail loudly if :data:`MIGRATIONS` is mis-ordered or has gaps.

    A typo'd version (``(3, ...)`` after ``(1, ...)``) would silently
    skip migration 2 — a class of bug that's catastrophic for forward-
    only DDL. The applier crashes here before touching pg.
    """
    versions = [v for v, _, _ in MIGRATIONS]
    if not versions:
        return
    seen: set[int] = set()
    for idx, version in enumerate(versions):
        if version in seen:
            raise RuntimeError(
                f"pg_migrations: duplicate migration version {version!r} "
                f"in MIGRATIONS at index {idx}."
            )
        seen.add(version)
    sorted_versions = sorted(versions)
    if versions != sorted_versions:
        raise RuntimeError(
            "pg_migrations: MIGRATIONS list is not sorted by version; "
            f"got {versions!r}."
        )
    # Gap detection — versions must be 1, 2, 3, ... with no holes.
    expected = list(range(sorted_versions[0], sorted_versions[-1] + 1))
    if sorted_versions != expected:
        missing = sorted(set(expected) - set(sorted_versions))
        raise RuntimeError(
            f"pg_migrations: missing migration versions {missing!r}."
        )


def apply_migrations(pool: "ConnectionPool") -> MigrationResult:
    """Apply every unapplied migration in :data:`MIGRATIONS`.

    Parameters
    ----------
    pool:
        A read-write :class:`psycopg_pool.ConnectionPool`. Typically
        obtained via :func:`pollypm.storage.pg_pool.get_rw_pool`. The
        applier acquires one connection from the pool for the whole
        run; nothing else should write while migrations are in flight
        (but other readers are fine — pg's MVCC handles the overlap).

    Returns
    -------
    MigrationResult
        Records which versions ran versus which were skipped.
    """
    _validate_migration_order()

    with pool.connection() as conn:
        # psycopg 3 defaults to autocommit=False; we control transactions
        # explicitly per migration. Make sure autocommit is off so the
        # ``commit()`` calls below are meaningful.
        conn.autocommit = False
        _ensure_bookkeeping(conn)

        applied = _applied_versions(conn)
        ran: list[tuple[int, str]] = []
        skipped: list[tuple[int, str]] = []

        for version, label, ddl in MIGRATIONS:
            if version in applied:
                skipped.append((version, label))
                continue
            logger.info(
                "pg_migrations: applying version=%d label=%s", version, label,
            )
            try:
                with conn.cursor() as cur:
                    cur.execute(ddl)
                    cur.execute(
                        "INSERT INTO schema_migrations (version, label) "
                        "VALUES (%s, %s)",
                        (version, label),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                logger.exception(
                    "pg_migrations: version=%d label=%s failed; rolled back",
                    version,
                    label,
                )
                raise
            ran.append((version, label))

        return MigrationResult(applied=ran, already_applied=skipped)


__all__ = [
    "MigrationResult",
    "apply_migrations",
]
