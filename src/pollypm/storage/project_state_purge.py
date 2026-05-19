"""Project-scoped state-row teardown helpers (Postgres-only, #1676/#1737).

The ``pm project remove --purge-state`` flow needs to count and bulk-
delete every row tied to a project key. This module is the storage-layer
facade the CLI calls instead of poking storage directly. Following Slice
K-state-callers-port (#1737), only the Postgres path remains.

The CLI keeps audit-tail file teardown (non-DB) and resolves the DB
path; everything that touches the DB lives here.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pollypm.models import PollyPMConfig

logger = logging.getLogger(__name__)


class ProjectStatePurgeError(RuntimeError):
    """Raised when the bulk state purge cannot commit cleanly.

    Signals a hard failure (unreachable pool, aborted transaction) that
    left the rows in place. The CLI catches this and aborts before
    touching ``pollypm.toml`` so the config doesn't drift relative to
    the still-present rows (see issue #1673).
    """


# Tables that carry project-scoped rows. Listed in delete order so any
# child-row references (work_tasks dependencies / executions / context /
# transitions / sessions / sync) are pruned BEFORE the parent
# ``work_tasks`` rows themselves.
#
# Each entry: ``(table, where_clause, params_kind)`` where ``params_kind``
# is ``"single"`` (binds ``project_key`` once) or ``"pair"`` (binds it
# twice — used by ``work_task_dependencies`` whose WHERE matches
# from-project OR to-project). WHERE clauses use ``%s`` placeholders for
# psycopg.
_STATE_PURGE_TABLES: tuple[tuple[str, str, str], ...] = (
    # Work-service children of work_tasks (FK to work_tasks(project,
    # task_number)) — these MUST go first or a FK-enabled connection
    # would refuse the parent delete.
    ("work_task_dependencies",
     "from_project = %s OR to_project = %s", "pair"),
    ("work_node_executions", "task_project = %s", "single"),
    ("work_context_entries", "task_project = %s", "single"),
    ("work_transitions", "task_project = %s", "single"),
    ("work_sessions", "task_project = %s", "single"),
    ("work_sync_state", "task_project = %s", "single"),
    # Parent work_tasks row. The delete trigger fires
    # ``work_task_delete_audit_outbox`` rows; we drop those next so the
    # outbox doesn't dangle once the project is gone.
    ("work_tasks", "project = %s", "single"),
    ("work_task_delete_audit_outbox", "project = %s", "single"),
    # Other work-service / notification rows keyed off project.
    ("notification_staging", "project = %s", "single"),
    # core tables that scope by project key.
    ("messages", "scope = %s", "single"),
    ("worktrees", "project_key = %s", "single"),
    ("architect_resume_tokens", "project_key = %s", "single"),
    ("token_samples", "project_key = %s", "single"),
    ("token_usage_hourly", "project_key = %s", "single"),
)


def _params_for(project_key: str, kind: str) -> tuple[str, ...]:
    return (project_key, project_key) if kind == "pair" else (project_key,)


def count_project_state_rows(
    db_path: Path | None,
    project_key: str,
    *,
    config: "PollyPMConfig | None" = None,
) -> dict[str, int]:
    """Return per-table row counts for ``project_key``.

    Best-effort: a missing table or pool failure yields zero for that
    table. The returned dict has one key per entry in
    :data:`_STATE_PURGE_TABLES`. Callers that also track non-DB
    artefacts (e.g. the central audit-tail JSONL) merge their own keys
    onto the result. ``db_path`` is unused on the pg backend.
    """
    del db_path  # unused on pg backend
    counts: dict[str, int] = {table: 0 for table, _w, _p in _STATE_PURGE_TABLES}
    try:
        from pollypm.storage.pg_pool import get_ro_pool
    except Exception as exc:  # noqa: BLE001
        logger.debug("project_state_purge: pg_pool import failed: %s", exc)
        return counts
    try:
        pool = get_ro_pool(config)
    except Exception as exc:  # noqa: BLE001
        logger.debug("project_state_purge: get_ro_pool failed: %s", exc)
        return counts
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            for table, where, ptype in _STATE_PURGE_TABLES:
                params = _params_for(project_key, ptype)
                try:
                    cur.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE {where}",
                        params,
                    )
                    row = cur.fetchone()
                    counts[table] = int(row[0]) if row else 0
                except Exception:  # noqa: BLE001
                    # Rollback per-statement so the next iteration's
                    # execute succeeds; psycopg aborts the whole
                    # transaction on any error otherwise.
                    try:
                        conn.rollback()
                    except Exception:  # noqa: BLE001
                        pass
                    counts[table] = 0
    except Exception as exc:  # noqa: BLE001
        logger.debug("project_state_purge: pg count failed: %s", exc)
    return counts


def purge_project_state_rows(
    db_path: Path | None,
    project_key: str,
    *,
    dry_run: bool = False,
    config: "PollyPMConfig | None" = None,
) -> dict[str, int]:
    """Delete every project-scoped row from the pg state schema.

    Returns a ``{table: removed_count}`` dict (one entry per
    :data:`_STATE_PURGE_TABLES` table). In ``dry_run`` mode no mutations
    happen; the returned counts reflect what WOULD be deleted (the same
    numbers :func:`count_project_state_rows` returns).

    The deletes run in a single transaction so a mid-sweep crash leaves
    the DB consistent. Any pool / transaction failure raises
    :class:`ProjectStatePurgeError` so the caller can abort before
    downstream config mutation (issue #1673 — best-effort-on-everything
    silently desynced ``pollypm.toml`` from the orphaned rows).
    """
    counts = count_project_state_rows(db_path, project_key, config=config)
    if dry_run:
        return counts

    try:
        from pollypm.storage.pg_pool import get_rw_pool
    except Exception as exc:  # noqa: BLE001
        raise ProjectStatePurgeError(
            f"pg_pool import failed during purge of '{project_key}': {exc}"
        ) from exc
    try:
        pool = get_rw_pool(config)
    except Exception as exc:  # noqa: BLE001
        raise ProjectStatePurgeError(
            f"could not open pg pool during purge of '{project_key}': {exc}"
        ) from exc
    try:
        with pool.connection() as conn:
            conn.autocommit = False
            try:
                with conn.cursor() as cur:
                    for table, where, ptype in _STATE_PURGE_TABLES:
                        params = _params_for(project_key, ptype)
                        try:
                            cur.execute(
                                f"DELETE FROM {table} WHERE {where}",
                                params,
                            )
                            counts[table] = int(cur.rowcount or 0)
                        except Exception:  # noqa: BLE001
                            # Surface so the caller sees a consistent
                            # partial-rollback signal rather than a
                            # silent skip.
                            raise
                conn.commit()
            except Exception as exc:  # noqa: BLE001
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
                raise ProjectStatePurgeError(
                    f"pg state purge failed for '{project_key}': {exc}"
                ) from exc
    except ProjectStatePurgeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ProjectStatePurgeError(
            f"pg state purge failed for '{project_key}': {exc}"
        ) from exc
    return counts


__all__ = [
    "ProjectStatePurgeError",
    "count_project_state_rows",
    "purge_project_state_rows",
]
