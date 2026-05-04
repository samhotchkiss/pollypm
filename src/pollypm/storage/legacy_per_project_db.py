"""One-shot migration for legacy per-project ``state.db`` files (#1004).

Pre-#1004 layout allowed a per-project ``<project>/.pollypm/state.db``
to coexist with the workspace-root ``<workspace>/.pollypm/state.db``.
``pollypm.work.db_resolver.resolve_work_db_path`` short-circuited to
the per-project file whenever it existed on disk — even when empty —
while writers (architect notify, project_planning, supervisor) landed
in the workspace DB.

Symptoms:

* ``pm task list --project foo`` returned 0 rows while the workspace
  DB held N tasks for ``foo`` (#1004).
* ``pm task next`` returned ``foo/1`` as available while
  ``pm task get foo/1`` returned "not found" (#1003 adjacency).
* The cockpit Tasks pane disagreed with the rail badge.

Fix: the resolver now always returns the workspace-root DB. This
module reconciles any leftover per-project DB into the workspace DB
and archives the per-project file so the next install does not get
re-bitten.

Usage::

    from pollypm.storage.legacy_per_project_db import (
        migrate_legacy_per_project_dbs,
    )
    report = migrate_legacy_per_project_dbs(config=load_config())

The migration is **idempotent**: rows already present in the
workspace DB (matched by ``(project, task_number)``) are not
overwritten. The per-project DB is renamed to
``state.db.legacy-1004`` only after every row was either copied or
matched. Failures leave the source DB in place for retry.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pollypm.config import PollyPMConfig

logger = logging.getLogger(__name__)

# Tables we migrate when the per-project DB carries rows the workspace
# DB does not. Order matters — children before parents would violate
# foreign keys, but the work-service schema has no enforced FKs across
# these tables, so dependency order is for *logical* consistency only.
_MIGRATABLE_TABLES = (
    "work_tasks",
    "work_transitions",
    "work_node_executions",
    "work_task_dependencies",
    "work_context_entries",
    "work_sessions",
)

# Suffix appended to the per-project DB after a successful migration.
LEGACY_DB_SUFFIX = ".legacy-1004"


@dataclass
class PerProjectMigrationReport:
    """Summary of a per-project DB migration run.

    ``rows_copied`` counts only the rows actually inserted into the
    workspace DB. ``rows_skipped`` counts rows already present (matched
    by ``(project, task_number)`` for ``work_tasks``, by primary key
    for the rest).
    """

    project_key: str
    source_db: Path
    archived_to: Path | None = None
    rows_copied: dict[str, int] = field(default_factory=dict)
    rows_skipped: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    skipped_reason: str | None = None

    @property
    def succeeded(self) -> bool:
        return not self.errors and self.skipped_reason is None


def _open_ro(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        logger.warning("legacy_per_project_db: cannot open %s read-only: %s", path, exc)
        return None


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cur.fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cur.fetchall()]


def _existing_task_keys(conn: sqlite3.Connection) -> set[tuple[str, int]]:
    if not _table_exists(conn, "work_tasks"):
        return set()
    cur = conn.execute("SELECT project, task_number FROM work_tasks")
    return {(row[0], row[1]) for row in cur.fetchall()}


def _copy_work_tasks(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
) -> tuple[int, int, set[tuple[str, int]]]:
    """Copy ``work_tasks`` rows missing in ``dst``. Returns
    ``(copied, skipped, copied_keys)``.

    ``copied_keys`` is the set of (project, task_number) pairs that
    were actually inserted; the caller uses it to gate child-table
    copies (only copy a transition if its parent task was copied).
    """
    src_cols = _columns(src, "work_tasks")
    dst_cols = _columns(dst, "work_tasks")
    shared = [c for c in src_cols if c in dst_cols]
    if not shared:
        return 0, 0, set()
    existing = _existing_task_keys(dst)

    cur = src.execute(f"SELECT {', '.join(shared)} FROM work_tasks")
    rows = cur.fetchall()
    copied = 0
    skipped = 0
    copied_keys: set[tuple[str, int]] = set()
    for row in rows:
        rec = dict(zip(shared, row, strict=False))
        key = (rec.get("project"), rec.get("task_number"))
        if key in existing:
            skipped += 1
            continue
        placeholders = ", ".join("?" for _ in shared)
        dst.execute(
            f"INSERT INTO work_tasks ({', '.join(shared)}) "
            f"VALUES ({placeholders})",
            row,
        )
        copied += 1
        # task_number is bound to int by the schema; defensive cast.
        if isinstance(key[1], int) and isinstance(key[0], str):
            copied_keys.add((key[0], key[1]))
    return copied, skipped, copied_keys


def _copy_child_table(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    table: str,
    *,
    copied_task_keys: set[tuple[str, int]],
) -> tuple[int, int]:
    """Copy rows from a child table for tasks we just inserted.

    Each table carries (project, task_number) so we can restrict to
    rows whose parent task was just copied — keeps us from polluting
    the workspace DB with orphaned children when a stale per-project
    DB happens to share a key with a different live workspace task.

    Tables without (project, task_number) columns (e.g. ad-hoc
    history tables a future schema bump might add) are skipped with
    a debug log.
    """
    if not _table_exists(src, table) or not _table_exists(dst, table):
        return 0, 0
    src_cols = _columns(src, table)
    dst_cols = _columns(dst, table)
    if "project" not in src_cols or "task_number" not in src_cols:
        logger.debug(
            "legacy_per_project_db: %s lacks (project, task_number) — skipping",
            table,
        )
        return 0, 0
    shared = [c for c in src_cols if c in dst_cols]
    if not shared:
        return 0, 0

    cur = src.execute(f"SELECT {', '.join(shared)} FROM {table}")
    rows = cur.fetchall()
    copied = 0
    skipped = 0
    for row in rows:
        rec = dict(zip(shared, row, strict=False))
        key = (rec.get("project"), rec.get("task_number"))
        if not (isinstance(key[0], str) and isinstance(key[1], int)):
            skipped += 1
            continue
        if key not in copied_task_keys:
            skipped += 1
            continue
        placeholders = ", ".join("?" for _ in shared)
        try:
            dst.execute(
                f"INSERT INTO {table} ({', '.join(shared)}) "
                f"VALUES ({placeholders})",
                row,
            )
            copied += 1
        except sqlite3.Error as exc:
            logger.debug(
                "legacy_per_project_db: insert into %s failed for %s: %s",
                table, key, exc,
            )
            skipped += 1
    return copied, skipped


def migrate_one(
    project_key: str,
    project_path: Path,
    workspace_db: Path,
) -> PerProjectMigrationReport:
    """Migrate a single project's legacy ``state.db`` into the workspace DB."""
    src_path = project_path / ".pollypm" / "state.db"
    report = PerProjectMigrationReport(
        project_key=project_key, source_db=src_path,
    )

    if not src_path.exists():
        report.skipped_reason = "no_per_project_db"
        return report

    # Resolve to absolute paths once — same-file detection has to look at
    # the real inode, not whatever string the caller handed us.
    try:
        src_real = src_path.resolve()
        ws_real = workspace_db.resolve()
    except OSError as exc:
        report.errors.append(f"resolve failed: {exc}")
        return report

    if src_real == ws_real:
        # Project path *is* the workspace root. The "legacy" file IS the
        # workspace DB. Nothing to migrate; nothing to archive.
        report.skipped_reason = "is_workspace_db"
        return report

    src = _open_ro(src_path)
    if src is None:
        report.errors.append("cannot open source DB")
        return report

    try:
        dst = sqlite3.connect(workspace_db)
    except sqlite3.Error as exc:
        report.errors.append(f"cannot open workspace DB: {exc}")
        src.close()
        return report

    try:
        with dst:  # transaction
            copied, skipped, copied_keys = _copy_work_tasks(src, dst)
            report.rows_copied["work_tasks"] = copied
            report.rows_skipped["work_tasks"] = skipped

            for table in _MIGRATABLE_TABLES[1:]:
                c, s = _copy_child_table(
                    src, dst, table, copied_task_keys=copied_keys,
                )
                report.rows_copied[table] = c
                report.rows_skipped[table] = s
    except sqlite3.Error as exc:
        report.errors.append(f"copy failed: {exc}")
        return report
    finally:
        src.close()
        dst.close()

    # Archive only after every copy succeeded. A failure leaves the
    # source in place so a re-run can pick up where we left off.
    archive_path = src_path.with_suffix(src_path.suffix + LEGACY_DB_SUFFIX)
    try:
        # If a previous run left an archive, append a numeric suffix
        # rather than overwriting — we never destroy operator data
        # silently.
        i = 0
        final_archive = archive_path
        while final_archive.exists():
            i += 1
            final_archive = src_path.with_suffix(
                src_path.suffix + LEGACY_DB_SUFFIX + f".{i}"
            )
        # Move the WAL/SHM siblings too so a stale read can't reopen
        # the deprecated file.
        for sibling in (src_path, src_path.with_suffix(src_path.suffix + "-wal"),
                        src_path.with_suffix(src_path.suffix + "-shm")):
            if sibling.exists():
                target = sibling.parent / (
                    final_archive.name + sibling.name[len(src_path.name):]
                )
                shutil.move(str(sibling), str(target))
        report.archived_to = final_archive
    except OSError as exc:
        report.errors.append(f"archive failed: {exc}")

    return report


def migrate_legacy_per_project_dbs(
    *,
    config: "PollyPMConfig | None" = None,
) -> list[PerProjectMigrationReport]:
    """Migrate every registered project's legacy per-project DB.

    Skips projects whose per-project DB does not exist, or whose path
    resolves to the workspace DB itself (e.g. a project registered at
    ``workspace_root``). Idempotent — safe to run repeatedly; a re-run
    is a no-op once each project is migrated and archived.
    """
    if config is None:
        try:
            from pollypm.config import load_config

            config = load_config()
        except Exception as exc:  # noqa: BLE001
            logger.warning("legacy_per_project_db: load_config failed: %s", exc)
            return []

    workspace_root_raw = getattr(config.project, "workspace_root", None)
    if workspace_root_raw is None:
        return []
    workspace_db = Path(workspace_root_raw) / ".pollypm" / "state.db"
    workspace_db.parent.mkdir(parents=True, exist_ok=True)

    reports: list[PerProjectMigrationReport] = []
    known: dict[str, Any] = getattr(config, "projects", {}) or {}
    for project_key, project_cfg in known.items():
        project_path_raw = getattr(project_cfg, "path", None)
        if project_path_raw is None:
            continue
        report = migrate_one(
            project_key=project_key,
            project_path=Path(project_path_raw),
            workspace_db=workspace_db,
        )
        reports.append(report)
        if report.succeeded and any(
            v for v in report.rows_copied.values()
        ):
            logger.info(
                "legacy_per_project_db: migrated %s → workspace (%s); "
                "archived to %s",
                project_key,
                {k: v for k, v in report.rows_copied.items() if v},
                report.archived_to,
            )
    return reports


__all__ = [
    "LEGACY_DB_SUFFIX",
    "PerProjectMigrationReport",
    "migrate_legacy_per_project_dbs",
    "migrate_one",
]
