"""Project-scoped state.db row teardown helpers (#1676).

The ``pm project remove --purge-state`` flow needs to count and bulk-
delete every row in ``state.db`` tied to a project key. Doing that work
inside the plugin CLI re-introduced direct ``sqlite3`` imports in
``plugins_builtin/`` and tripped the boundary test that #1376 set up
(``test_work_task_query_callers_do_not_open_sqlite_directly``).

This module is the storage-layer facade the CLI calls instead. It owns
the schema table list, the transactional bulk DELETE, and the
read-only count probes. The CLI keeps audit-tail file teardown
(non-SQLite) and resolves the DB path; everything that touches SQLite
lives here.

Mirrors the ``storage/work_session_queries.py`` pattern from #1617:
read-only probes open ``file:<path>?mode=ro`` URIs via stdlib
``sqlite3``; bulk writes open a normal connection inside an explicit
``BEGIN IMMEDIATE`` so a mid-sweep crash leaves the DB consistent.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


# Tables that carry project-scoped rows. Listed in delete order so any
# child-row foreign-key chains (work_tasks references in dependencies /
# executions / context / transitions / sessions / sync) are pruned BEFORE
# the parent ``work_tasks`` rows themselves. SQLite enforces FKs only
# when ``PRAGMA foreign_keys=ON`` is set on the connection — the work
# service opens connections without it, but we still respect the order
# so a future enable doesn't bite us.
#
# Each entry: ``(table, where_clause, params_kind)`` where ``params_kind``
# is ``"single"`` (binds ``project_key`` once) or ``"pair"`` (binds it
# twice — used by ``work_task_dependencies`` whose WHERE matches
# from-project OR to-project).
class ProjectStatePurgeError(RuntimeError):
    """Raised when the bulk state-db purge cannot commit cleanly.

    Signals a hard failure (locked DB, corrupt file, unwritable path,
    aborted transaction) that left the rows in place. The CLI catches
    this and aborts before touching ``pollypm.toml`` so the config
    doesn't drift relative to the still-present rows (see issue #1673).
    """


_STATE_PURGE_TABLES: tuple[tuple[str, str, str], ...] = (
    # Work-service children of work_tasks (FK to work_tasks(project,
    # task_number)) — these MUST go first or a future FK-enabled
    # connection would refuse the parent delete.
    ("work_task_dependencies",
     "from_project = ? OR to_project = ?", "pair"),
    ("work_node_executions", "task_project = ?", "single"),
    ("work_context_entries", "task_project = ?", "single"),
    ("work_transitions", "task_project = ?", "single"),
    ("work_sessions", "task_project = ?", "single"),
    ("work_sync_state", "task_project = ?", "single"),
    # Parent work_tasks row. The delete trigger fires
    # ``work_task_delete_audit_outbox`` rows; we drop those next so the
    # outbox doesn't dangle once the project is gone.
    ("work_tasks", "project = ?", "single"),
    ("work_task_delete_audit_outbox", "project = ?", "single"),
    # Other work-service / notification rows keyed off project.
    ("notification_staging", "project = ?", "single"),
    # state.db core tables that scope by project key.
    ("messages", "scope = ?", "single"),
    ("worktrees", "project_key = ?", "single"),
    ("architect_resume_tokens", "project_key = ?", "single"),
    ("token_samples", "project_key = ?", "single"),
    ("token_usage_hourly", "project_key = ?", "single"),
)


def _params_for(project_key: str, kind: str) -> tuple[str, ...]:
    return (project_key, project_key) if kind == "pair" else (project_key,)


def count_project_state_rows(
    db_path: Path | None,
    project_key: str,
) -> dict[str, int]:
    """Return per-table row counts for ``project_key`` in ``db_path``.

    Best-effort: a missing DB, missing table, or read failure yields
    zero for that table. The returned dict has one key per entry in
    :data:`_STATE_PURGE_TABLES`. Callers that also track non-SQLite
    artefacts (e.g. the central audit-tail JSONL) merge their own keys
    onto the result.
    """
    counts: dict[str, int] = {table: 0 for table, _w, _p in _STATE_PURGE_TABLES}

    if db_path is None or not db_path.exists():
        return counts

    # #1674: percent-encode so URI metacharacters (``#``/``?``) in the
    # workspace path don't get parsed as fragment/query and silently
    # produce a no-rows result.
    from pollypm.storage.sqlite_pragmas import readonly_uri

    try:
        conn = sqlite3.connect(readonly_uri(db_path), uri=True)
    except sqlite3.Error as exc:
        logger.debug(
            "project_state_purge: read-only connect failed for %s: %s",
            db_path, exc,
        )
        return counts
    try:
        for table, where, ptype in _STATE_PURGE_TABLES:
            params = _params_for(project_key, ptype)
            try:
                cur = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {where}",
                    params,
                )
                row = cur.fetchone()
                counts[table] = int(row[0]) if row else 0
            except sqlite3.Error:
                # Table doesn't exist yet (fresh DB, pre-migration, or
                # schema drift). Counts as zero — there's nothing to
                # delete.
                counts[table] = 0
    finally:
        conn.close()

    return counts


def purge_project_state_rows(
    db_path: Path | None,
    project_key: str,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Delete every project-scoped row from ``db_path``.

    Returns a ``{table: removed_count}`` dict (one entry per
    :data:`_STATE_PURGE_TABLES` table). In ``dry_run`` mode no
    mutations happen; the returned counts reflect what WOULD be
    deleted (the same numbers :func:`count_project_state_rows`
    returns).

    Per-table best-effort: a missing table (fresh DB, schema drift)
    is swallowed so it never aborts the rest of the sweep; the count
    for that table falls through as ``0``. The deletes run in a
    single ``BEGIN IMMEDIATE`` transaction so a mid-sweep crash
    leaves the DB consistent.

    A connection-level failure (locked DB, corrupt file, unwritable
    path) or transaction-level failure (BEGIN/COMMIT error) raises
    :class:`ProjectStatePurgeError` so the caller can abort before
    any downstream config mutation (issue #1673 — best-effort-on-
    everything silently desynced ``pollypm.toml`` from the orphaned
    rows).

    Why a single bulk SQL sweep instead of routing through
    ``work_service.delete_task``: per-task deletes would fire
    cascade-aware audit emits + sync hooks for every row, which is
    exactly the noise the user is trying to clear. Bulk DELETE
    silences the side-channels and matches the operator's mental
    model ("tear it all down, fast").
    """
    counts = count_project_state_rows(db_path, project_key)
    if dry_run:
        return counts

    if db_path is None or not db_path.exists():
        return counts

    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error as exc:
        # Can't even open the DB. Surface so the caller aborts before
        # touching downstream config.
        raise ProjectStatePurgeError(
            f"could not open state.db at {db_path}: {exc}"
        ) from exc
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
            for table, where, ptype in _STATE_PURGE_TABLES:
                params = _params_for(project_key, ptype)
                try:
                    cur = conn.execute(
                        f"DELETE FROM {table} WHERE {where}", params,
                    )
                except sqlite3.Error:
                    # Missing table — skip silently. We already counted
                    # 0 for it above so the summary line stays
                    # accurate.
                    continue
                counts[table] = int(cur.rowcount or 0)
            conn.commit()
        except sqlite3.Error as exc:
            # BEGIN failed (locked DB) or COMMIT failed (disk full,
            # corrupt). Roll back so the partial-state risk is zero,
            # then signal hard failure to the caller.
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            logger.debug(
                "project_state_purge: bulk DELETE failed for %s: %s",
                db_path, exc,
            )
            raise ProjectStatePurgeError(
                f"state.db purge failed for '{project_key}': {exc}"
            ) from exc
    finally:
        conn.close()

    return counts


__all__ = [
    "ProjectStatePurgeError",
    "count_project_state_rows",
    "purge_project_state_rows",
]
