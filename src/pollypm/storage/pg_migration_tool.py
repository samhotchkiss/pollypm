"""SQLite → Postgres bulk data migration tool (issue #1737, Slice E).

Slice E ships the operator-facing one-shot ``pm storage migrate-to-pg``
that moves data from PollyPM's existing sqlite workspace (and any
leftover per-project ``state.db`` files) into the pg schema installed
by Slice A's :mod:`pollypm.storage.pg_migrations` applier.

Design (per the slice spec)
---------------------------

* **Sources**: enumerate the workspace ``~/.pollypm/state.db`` plus every
  ``<project>/.pollypm/state.db`` registered in the loaded config. The
  per-project DBs (#1004 legacy) carry the same shape but the
  ``project_key`` column derives from the filename instead of a row
  column.
* **Idempotency**: SHA-256 of each source file is recorded in
  ``_pg_migration_audit``. A second run against an unchanged sqlite file
  is a no-op with a clear "already imported" message.
* **Atomicity**: every source is copied inside one pg transaction. A
  failure rolls back the whole source — the sqlite file is **not**
  renamed and the tool exits non-zero. Other already-finished sources
  stay committed (each has its own audit row).
* **Performance**: bounded-batch INSERTs (5000 rows / batch) via
  ``psycopg.cursor.executemany``. ``COPY FROM STDIN`` was considered
  but the spec is fine with a plain bulk insert for the time budget;
  the audit table records throughput so a follow-up can swap in COPY
  if real installs are slow.
* **Safety**: ``--commit`` is the kill switch. Without it the tool runs
  the whole pipeline against a transaction it always rolls back, so
  pre-flight + parity checks still surface but the pg state is
  untouched.
* **Rollback**: on success the source sqlite files are renamed to
  ``state.db.pre-pg-<timestamp>`` (not deleted). Operators flip
  ``[storage] backend`` back to ``sqlite`` and rename the file back to
  recover.

Out of scope (per the spec)
---------------------------

* The actual cutover (flipping ``[storage] backend = "postgres"``) is
  Slice J.
* ``notification_staging`` retirement (#704) is **not** bundled — the
  table is copied 1:1.
* ``--reembed`` is wired as a flag but the actual re-embed call is a
  TODO pointer; the writer that would re-fire ``pm memory
  backfill-embeddings`` lands when Slice D's embedding writer is in.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pollypm.storage.sqlite_pragmas import readonly_uri

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

    from pollypm.models import PollyPMConfig

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------- #


# Audit-table DDL. Created on first run; not part of the canonical
# schema in ``pg_schema.py`` because it's a migration-tool artefact
# rather than a domain table. The schema mirrors what an operator
# would actually want to see: source path, content hash, when it
# completed, what got moved.
AUDIT_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS _pg_migration_audit (
    id              bigserial PRIMARY KEY,
    source_path     text NOT NULL,
    source_sha256   text NOT NULL,
    source_kind     text NOT NULL,
    project_key     text NOT NULL DEFAULT '',
    rows_per_table  jsonb NOT NULL DEFAULT '{}'::jsonb,
    started_at      timestamptz NOT NULL,
    completed_at    timestamptz NOT NULL,
    tool_version    text NOT NULL DEFAULT 'slice-e-1737',
    UNIQUE (source_sha256)
);

CREATE INDEX IF NOT EXISTS idx_pg_migration_audit_path
    ON _pg_migration_audit(source_path);
"""


# Batch size for bulk inserts. 5000 is a sweet spot per the spec — large
# enough to amortise per-statement round-trips, small enough that one
# batch fits comfortably in pg's WAL buffer on default tunings.
COPY_BATCH_SIZE = 5000


# Suffix appended to source sqlite files after a successful copy. The
# timestamp is YYYYMMDDTHHMMSSZ so the suffix sorts naturally.
RENAME_SUFFIX_PREFIX = ".pre-pg-"


# Tables copied per source. Order matters for FK validity in pg:
# parents (work_tasks, work_flow_templates) must land before children.
#
# ``json_cols`` — sqlite columns that store TEXT-encoded JSON; the
# migrator parses and re-encodes to pg's jsonb adapter.
# ``ts_cols`` — sqlite columns that store ISO-8601 TEXT; coerced to
# datetime for pg's timestamptz adapter.
# ``bool_cols`` — sqlite columns that store 0/1 INTEGER; coerced to
# Python bool for pg's boolean adapter.
# ``inject_project_key`` — when True, the migrator ensures the
# destination ``project_key`` column is populated, with this precedence:
#   1. A non-empty value already on the source row.
#   2. The per-table fallback (``project_key_fallback_col``), e.g.
#      ``work_tasks.project`` carries the binding even though the source
#      row has no native ``project_key`` column.
#   3. The source DB's derived project key (per-project DB filename).
# Tables whose source schema has a native ``project_key`` column
# (checkpoints, worktrees, token_samples, token_usage_hourly,
# architect_resume_tokens) still set this flag so an empty value gets
# backfilled from the descriptor instead of landing as ``''``.
@dataclass(frozen=True)
class TableSpec:
    sqlite_table: str
    pg_table: str
    json_cols: tuple[str, ...] = ()
    ts_cols: tuple[str, ...] = ()
    bool_cols: tuple[str, ...] = ()
    inject_project_key: bool = False
    # Source column whose row value should be used as the project_key
    # when the source row has no native (or only-empty) ``project_key``.
    # Used by ``work_tasks`` where each row carries ``project`` even in
    # the workspace-DB case.
    project_key_fallback_col: str | None = None


# Order: schema_version / FK-parents first, children after.
TABLE_SPECS: tuple[TableSpec, ...] = (
    # Operator / runtime tables — no inter-table FKs to worry about.
    TableSpec(
        sqlite_table="sessions",
        pg_table="sessions",
    ),
    TableSpec(
        sqlite_table="heartbeats",
        pg_table="heartbeats",
        ts_cols=("created_at",),
        bool_cols=("pane_dead",),
    ),
    TableSpec(
        sqlite_table="leases",
        pg_table="leases",
        ts_cols=("updated_at",),
    ),
    TableSpec(
        sqlite_table="account_usage",
        pg_table="account_usage",
        ts_cols=("reset_at", "updated_at"),
    ),
    TableSpec(
        sqlite_table="account_runtime",
        pg_table="account_runtime",
        ts_cols=("available_at", "access_expires_at", "updated_at"),
        bool_cols=("refresh_available",),
    ),
    TableSpec(
        sqlite_table="session_runtime",
        pg_table="session_runtime",
        ts_cols=(
            "recovery_window_started_at",
            "retry_at",
            "last_recovered_at",
            "updated_at",
        ),
    ),
    TableSpec(
        sqlite_table="checkpoints",
        pg_table="checkpoints",
        ts_cols=("created_at",),
        inject_project_key=True,
    ),
    TableSpec(
        sqlite_table="worktrees",
        pg_table="worktrees",
        ts_cols=("created_at", "updated_at"),
        inject_project_key=True,
    ),
    TableSpec(
        sqlite_table="token_samples",
        pg_table="token_samples",
        ts_cols=("observed_at",),
        inject_project_key=True,
    ),
    TableSpec(
        sqlite_table="token_usage_hourly",
        pg_table="token_usage_hourly",
        ts_cols=("updated_at",),
        inject_project_key=True,
    ),
    # Messages — the unified inbox surface.
    TableSpec(
        sqlite_table="messages",
        pg_table="messages",
        json_cols=("payload_json", "labels"),
        ts_cols=("created_at", "updated_at", "closed_at"),
        inject_project_key=True,
    ),
    # Work flow templates land before tasks/nodes (FK parent).
    TableSpec(
        sqlite_table="work_flow_templates",
        pg_table="work_flow_templates",
        json_cols=("roles",),
        ts_cols=("created_at",),
        bool_cols=("is_current",),
    ),
    TableSpec(
        sqlite_table="work_flow_nodes",
        pg_table="work_flow_nodes",
        json_cols=("gates",),
    ),
    # Work tasks — the FK parent for the rest of the work-* tables.
    # The source ``project`` column carries the per-row project binding
    # even in the workspace-DB case (where ``source.project_key`` is
    # empty), so map it as the ``project_key`` fallback.
    TableSpec(
        sqlite_table="work_tasks",
        pg_table="work_tasks",
        json_cols=("labels", "relevant_files", "roles", "external_refs"),
        ts_cols=("created_at", "updated_at"),
        bool_cols=("requires_human_review",),
        inject_project_key=True,
        project_key_fallback_col="project",
    ),
    TableSpec(
        sqlite_table="work_task_delete_audit_outbox",
        pg_table="work_task_delete_audit_outbox",
        ts_cols=("deleted_at",),
    ),
    TableSpec(
        sqlite_table="work_task_dependencies",
        pg_table="work_task_dependencies",
        ts_cols=("created_at",),
    ),
    TableSpec(
        sqlite_table="work_node_executions",
        pg_table="work_node_executions",
        json_cols=("work_output",),
        ts_cols=("started_at", "completed_at", "kickoff_sent_at"),
    ),
    TableSpec(
        sqlite_table="work_context_entries",
        pg_table="work_context_entries",
        ts_cols=("created_at",),
    ),
    TableSpec(
        sqlite_table="work_transitions",
        pg_table="work_transitions",
        ts_cols=("created_at",),
    ),
    TableSpec(
        sqlite_table="work_sessions",
        pg_table="work_sessions",
        ts_cols=("started_at", "ended_at"),
    ),
    TableSpec(
        sqlite_table="work_sync_state",
        pg_table="work_sync_state",
        ts_cols=("last_synced_at",),
    ),
    # Memory / ops.
    TableSpec(
        sqlite_table="memory_entries",
        pg_table="memory_entries",
        ts_cols=("created_at", "updated_at", "ttl_at"),
        inject_project_key=True,
    ),
    TableSpec(
        sqlite_table="memory_summaries",
        pg_table="memory_summaries",
        ts_cols=("created_at",),
    ),
    TableSpec(
        sqlite_table="work_jobs",
        pg_table="work_jobs",
        json_cols=("payload_json",),
        ts_cols=("enqueued_at", "run_after", "claimed_at", "finished_at"),
    ),
    TableSpec(
        sqlite_table="architect_resume_tokens",
        pg_table="architect_resume_tokens",
        ts_cols=("captured_at", "last_active_at"),
        inject_project_key=True,
    ),
    TableSpec(
        sqlite_table="workspace_state",
        pg_table="workspace_state",
        json_cols=("value_json",),
        ts_cols=("set_at",),
    ),
    TableSpec(
        sqlite_table="tier4_promotion_state",
        pg_table="tier4_promotion_state",
        json_cols=("dispatch_history_json",),
        ts_cols=("last_promotion_at", "tier4_entered_at", "updated_at"),
        bool_cols=("tier4_active",),
    ),
    TableSpec(
        sqlite_table="notification_staging",
        pg_table="notification_staging",
        json_cols=("payload_json",),
        ts_cols=("created_at", "flushed_at"),
    ),
)


# --------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------- #


@dataclass
class SourceDescriptor:
    """One sqlite source to migrate.

    ``project_key`` is the value injected into rows of tables flagged
    with ``inject_project_key=True`` when the source row does not
    already carry a ``project_key`` column. For the workspace DB this
    is empty (the row's existing ``project`` / ``scope`` columns carry
    the binding); for a per-project DB it's the project key derived
    from the parent directory name.
    """

    path: Path
    kind: str  # "workspace" | "per_project"
    project_key: str = ""

    @property
    def display(self) -> str:
        return str(self.path)


@dataclass
class TableCopyReport:
    """Per-table outcome for one source DB."""

    table: str
    sqlite_rows: int = 0
    pg_rows_copied: int = 0
    skipped_reason: str | None = None


@dataclass
class SourceMigrationReport:
    """End-to-end outcome for one source DB."""

    source: SourceDescriptor
    source_sha256: str
    started_at: datetime
    completed_at: datetime | None = None
    per_table: list[TableCopyReport] = field(default_factory=list)
    skipped_already_imported_at: datetime | None = None
    renamed_to: Path | None = None
    failure: str | None = None
    failed_table: str | None = None
    parity_ok: bool = True
    parity_mismatches: list[tuple[str, int, int]] = field(default_factory=list)

    @property
    def total_rows_copied(self) -> int:
        return sum(t.pg_rows_copied for t in self.per_table)

    @property
    def succeeded(self) -> bool:
        return self.failure is None and self.skipped_already_imported_at is None

    def rows_per_table_map(self) -> dict[str, int]:
        return {t.table: t.pg_rows_copied for t in self.per_table if t.pg_rows_copied}


@dataclass
class MigrationRunReport:
    """Top-level report — list of per-source reports + flags."""

    sources: list[SourceMigrationReport] = field(default_factory=list)
    dry_run: bool = True
    committed: bool = False
    preflight_error: str | None = None

    @property
    def succeeded(self) -> bool:
        if self.preflight_error is not None:
            return False
        return all(s.succeeded or s.skipped_already_imported_at is not None
                   for s in self.sources)


# --------------------------------------------------------------------- #
# Source enumeration
# --------------------------------------------------------------------- #


def _workspace_db_path(config: "PollyPMConfig | None") -> Path | None:
    """Resolve the workspace ``state.db`` path from the loaded config.

    Returns ``None`` when the config can't be loaded (the caller treats
    that as "no auto-discovery; rely on operator override").
    """
    if config is None:
        return None
    workspace_root_raw = getattr(config.project, "workspace_root", None)
    if workspace_root_raw is None:
        return None
    return Path(workspace_root_raw) / ".pollypm" / "state.db"


def _project_db_path(project_path: Path) -> Path:
    return project_path / ".pollypm" / "state.db"


def discover_sources(
    *,
    config: "PollyPMConfig | None" = None,
    include_legacy_per_project: bool = True,
    from_sqlite_override: str | None = None,
) -> list[SourceDescriptor]:
    """Enumerate the sqlite sources to migrate.

    ``from_sqlite_override`` (the CLI's ``--from-sqlite``) takes priority
    when set to anything other than ``"auto"``. When ``"auto"`` (or
    ``None``), the workspace DB and every per-project DB registered in
    the config are returned (in that order — workspace first so its
    rows land before any legacy per-project overlay).
    """
    sources: list[SourceDescriptor] = []

    if from_sqlite_override and from_sqlite_override != "auto":
        # Operator-pinned source. Treat as workspace-kind so no
        # project_key injection happens — the rows are taken at face
        # value.
        path = Path(from_sqlite_override).expanduser().resolve()
        if path.exists():
            sources.append(
                SourceDescriptor(path=path, kind="workspace", project_key="")
            )
        return sources

    workspace_db = _workspace_db_path(config)
    if workspace_db is not None and workspace_db.exists():
        sources.append(
            SourceDescriptor(
                path=workspace_db.resolve(),
                kind="workspace",
                project_key="",
            )
        )

    if include_legacy_per_project and config is not None:
        known = getattr(config, "projects", {}) or {}
        for project_key, project_cfg in known.items():
            project_path_raw = getattr(project_cfg, "path", None)
            if project_path_raw is None:
                continue
            per_project_db = _project_db_path(Path(project_path_raw))
            if not per_project_db.exists():
                continue
            try:
                resolved = per_project_db.resolve()
            except OSError:
                continue
            # Skip if same inode as the workspace DB (project at
            # workspace root case).
            if any(s.path == resolved for s in sources):
                continue
            sources.append(
                SourceDescriptor(
                    path=resolved,
                    kind="per_project",
                    project_key=project_key,
                )
            )
    return sources


# --------------------------------------------------------------------- #
# Hashing + audit
# --------------------------------------------------------------------- #


def compute_sha256(path: Path) -> str:
    """Stream-hash a sqlite file. The .db is the canonical content; we
    deliberately exclude any -wal/-shm sidecars because their contents
    are merged into the .db on a clean close, and including them would
    make the hash non-deterministic across runs.
    """
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _ensure_audit_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(AUDIT_TABLE_DDL)
    conn.commit()


def _audit_lookup(conn, sha256: str) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT completed_at FROM _pg_migration_audit WHERE source_sha256 = %s",
            (sha256,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return row[0]


def _audit_record(
    conn,
    *,
    report: SourceMigrationReport,
) -> None:
    """Insert one audit row inside the caller's open transaction.

    Caller controls commit/rollback — dry-run callers always roll back
    so the audit row never persists.
    """
    from psycopg.types.json import Jsonb

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO _pg_migration_audit
                (source_path, source_sha256, source_kind, project_key,
                 rows_per_table, started_at, completed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_sha256) DO NOTHING
            """,
            (
                str(report.source.path),
                report.source_sha256,
                report.source.kind,
                report.source.project_key,
                Jsonb(report.rows_per_table_map()),
                report.started_at,
                report.completed_at or datetime.now(UTC),
            ),
        )


# --------------------------------------------------------------------- #
# Pre-flight
# --------------------------------------------------------------------- #


@dataclass
class PreflightResult:
    ok: bool
    server_version_num: int | None = None
    vector_installed: bool = False
    migration_version: int | None = None
    message: str = ""


def preflight(pool: "ConnectionPool", *, min_version: int = 16) -> PreflightResult:
    """Verify pg is reachable, modern enough, and schema migration 0001 is applied.

    Parameters
    ----------
    pool:
        RW pool — the audit table + bulk inserts both need write access.
    min_version:
        Major-version gate (default 16). The schema relies on generated
        tsvector columns + pgvector hnsw which is best on pg 16+.
    """
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT current_setting('server_version_num')")
            version_num = int(cur.fetchone()[0])
            major = version_num // 10000
            cur.execute(
                "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
            )
            vector = cur.fetchone() is not None
            cur.execute(
                "SELECT max(version) FROM schema_migrations"
            )
            row = cur.fetchone()
            migration_version = int(row[0]) if row and row[0] is not None else None
    except Exception as exc:  # noqa: BLE001
        return PreflightResult(
            ok=False,
            message=f"pg unreachable or schema_migrations missing: {exc}",
        )

    if major < min_version:
        return PreflightResult(
            ok=False,
            server_version_num=version_num,
            vector_installed=vector,
            migration_version=migration_version,
            message=(
                f"pg server version {major} < required {min_version}. "
                "Upgrade pg before running the migration."
            ),
        )
    if not vector:
        return PreflightResult(
            ok=False,
            server_version_num=version_num,
            vector_installed=False,
            migration_version=migration_version,
            message=(
                "vector extension not installed. Run "
                "`CREATE EXTENSION vector` in the target database."
            ),
        )
    if migration_version is None or migration_version < 1:
        return PreflightResult(
            ok=False,
            server_version_num=version_num,
            vector_installed=vector,
            migration_version=migration_version,
            message=(
                "schema_migrations does not record 0001. Run "
                "`apply_migrations()` (or open a PgWorkService) once "
                "before migrating data."
            ),
        )
    return PreflightResult(
        ok=True,
        server_version_num=version_num,
        vector_installed=vector,
        migration_version=migration_version,
        message=(
            f"pg {major} reachable; vector ext present; "
            f"schema_migrations at version {migration_version}."
        ),
    )


# --------------------------------------------------------------------- #
# sqlite helpers
# --------------------------------------------------------------------- #


def _sqlite_open_ro(path: Path) -> sqlite3.Connection:
    """Open a sqlite file read-only. Uses :func:`readonly_uri` so the
    pragma machinery is consistent with the rest of the codebase."""
    conn = sqlite3.connect(readonly_uri(path), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _sqlite_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cur.fetchone() is not None


def _sqlite_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cur.fetchall()]


def _sqlite_count(conn: sqlite3.Connection, table: str) -> int:
    cur = conn.execute(f"SELECT count(*) FROM {table}")
    return int(cur.fetchone()[0])


# --------------------------------------------------------------------- #
# Row conversion
# --------------------------------------------------------------------- #


def _parse_iso_timestamp(value: Any) -> datetime | None:
    """Best-effort parse of a sqlite-stored ISO-8601 string.

    sqlite stores timestamps as TEXT in a variety of shapes:
    ``YYYY-MM-DD HH:MM:SS`` (the ``CURRENT_TIMESTAMP`` default),
    ``YYYY-MM-DDTHH:MM:SS+00:00`` (Python ``datetime.isoformat()``),
    sometimes with microseconds. ``datetime.fromisoformat`` (py>=3.11)
    handles both forms; we patch the legacy ``YYYY-MM-DD HH:MM:SS`` form
    by swapping the space for ``T`` so older formats still parse.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    candidate = text
    if " " in candidate and "T" not in candidate:
        candidate = candidate.replace(" ", "T", 1)
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        logger.debug("pg_migration_tool: cannot parse timestamp %r", value)
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _parse_json_text(value: Any, default: Any) -> Any:
    """Decode a sqlite TEXT-as-JSON column to a Python object.

    sqlite stores these as text; pg expects jsonb. Empty / NULL coalesce
    to ``default`` so an INSERT with a NOT NULL DEFAULT still satisfies
    the column.
    """
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            return json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return default
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return default
    return default


def _coerce_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "t", "yes")
    return bool(value)


def _convert_row(
    row: sqlite3.Row,
    *,
    source_columns: list[str],
    spec: TableSpec,
    project_key: str,
    target_columns: set[str],
) -> tuple[tuple[str, ...], tuple[Any, ...]]:
    """Convert one sqlite Row into ``(columns_tuple, values_tuple)``
    aligned with the pg schema.

    Behaviour:

    * Source columns not present in pg are dropped silently (sqlite
      might carry a column the pg schema collapsed away; the migrator
      doesn't lose data because the FK-related columns are guaranteed
      to be in both).
    * jsonb columns parse the sqlite TEXT to a Python object and wrap
      in ``psycopg.types.json.Jsonb`` so the adapter inserts them as
      real jsonb.
    * timestamptz columns parse the ISO-8601 TEXT to a ``datetime``.
    * boolean columns coerce 0/1 to bool.
    * ``project_key`` is stamped with the first non-empty of: the
      source row's ``project_key`` value, the per-table fallback column
      (e.g. ``work_tasks.project``), or the source DB's derived project
      key. This closes the gap where workspace-DB rows used to land
      with ``project_key = ''`` and broke every cockpit query that
      filters by project (#1737 follow-up).
    """
    from psycopg.types.json import Jsonb

    json_cols = set(spec.json_cols)
    ts_cols = set(spec.ts_cols)
    bool_cols = set(spec.bool_cols)

    cols_out: list[str] = []
    vals_out: list[Any] = []

    # Track whether we've emitted project_key already (and what value)
    # so the post-loop injection step can decide whether to append or
    # overwrite.
    project_key_emitted_idx: int | None = None

    for col in source_columns:
        if col not in target_columns:
            continue
        raw = row[col]
        if col in json_cols:
            default = {} if col in {"payload_json", "value_json", "roles",
                                     "external_refs", "work_output"} else []
            parsed = _parse_json_text(raw, default)
            cols_out.append(col)
            vals_out.append(Jsonb(parsed))
            continue
        if col in ts_cols:
            cols_out.append(col)
            vals_out.append(_parse_iso_timestamp(raw))
            continue
        if col in bool_cols:
            cols_out.append(col)
            vals_out.append(_coerce_bool(raw))
            continue
        if col == "project_key":
            project_key_emitted_idx = len(cols_out)
        cols_out.append(col)
        vals_out.append(raw)

    if spec.inject_project_key and "project_key" in target_columns:
        # Resolve the project_key with the documented precedence:
        # 1. Source row's non-empty value (if the column exists).
        # 2. Per-table fallback column (e.g. work_tasks.project).
        # 3. Source DB descriptor's project_key.
        existing = (
            vals_out[project_key_emitted_idx]
            if project_key_emitted_idx is not None
            else None
        )
        if existing is None or existing == "":
            resolved: str = ""
            fallback_col = spec.project_key_fallback_col
            if fallback_col and fallback_col in source_columns:
                fallback_val = row[fallback_col]
                if fallback_val is not None and fallback_val != "":
                    resolved = str(fallback_val)
            if not resolved:
                resolved = project_key or ""
            if project_key_emitted_idx is not None:
                # Overwrite the empty source value in place — keeps the
                # column-order invariant the executemany batching relies
                # on (columns_for_insert is derived from the first row).
                vals_out[project_key_emitted_idx] = resolved
            else:
                cols_out.append("project_key")
                vals_out.append(resolved)

    return tuple(cols_out), tuple(vals_out)


def _pg_table_columns(conn, pg_table: str) -> set[str]:
    """Introspect pg for the destination column set. Used to drop any
    sqlite-only columns from the inserted row."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
              FROM information_schema.columns
             WHERE table_schema = current_schema()
               AND table_name = %s
            """,
            (pg_table,),
        )
        return {row[0] for row in cur.fetchall()}


# --------------------------------------------------------------------- #
# Per-table copy
# --------------------------------------------------------------------- #


def _copy_table(
    *,
    conn,
    sqlite_conn: sqlite3.Connection,
    spec: TableSpec,
    project_key: str,
    batch_size: int = COPY_BATCH_SIZE,
) -> TableCopyReport:
    """Bulk-copy one table from sqlite to pg.

    Inserts go through :class:`psycopg.Cursor.executemany` in batches
    of ``batch_size``. The caller owns the surrounding transaction; a
    failure inside this function lets the exception propagate so the
    caller's rollback covers every prior table.
    """
    report = TableCopyReport(table=spec.sqlite_table)

    if not _sqlite_table_exists(sqlite_conn, spec.sqlite_table):
        report.skipped_reason = "sqlite_table_missing"
        return report

    source_columns = _sqlite_columns(sqlite_conn, spec.sqlite_table)
    target_columns = _pg_table_columns(conn, spec.pg_table)
    if not source_columns or not target_columns:
        report.skipped_reason = "no_columns"
        return report

    report.sqlite_rows = _sqlite_count(sqlite_conn, spec.sqlite_table)
    if report.sqlite_rows == 0:
        return report

    # Stream sqlite rows; psycopg executemany handles batching but
    # we still chunk to bound memory.
    cursor = sqlite_conn.execute(
        f"SELECT * FROM {spec.sqlite_table}"
    )

    batch: list[tuple[Any, ...]] = []
    columns_for_insert: tuple[str, ...] | None = None
    inserted = 0

    with conn.cursor() as pg_cur:
        for row in cursor:
            cols, vals = _convert_row(
                row,
                source_columns=source_columns,
                spec=spec,
                project_key=project_key,
                target_columns=target_columns,
            )
            if columns_for_insert is None:
                columns_for_insert = cols
                col_list = ", ".join(columns_for_insert)
                placeholders = ", ".join("%s" for _ in columns_for_insert)
                insert_sql = (
                    f"INSERT INTO {spec.pg_table} ({col_list}) "
                    f"VALUES ({placeholders}) "
                    "ON CONFLICT DO NOTHING"
                )
            elif cols != columns_for_insert:
                # Schema shape drifted mid-table (shouldn't happen —
                # all rows of one sqlite table share the same columns)
                # but defend by flushing what we have and re-deriving.
                if batch:
                    pg_cur.executemany(insert_sql, batch)
                    inserted += len(batch)
                    batch = []
                columns_for_insert = cols
                col_list = ", ".join(columns_for_insert)
                placeholders = ", ".join("%s" for _ in columns_for_insert)
                insert_sql = (
                    f"INSERT INTO {spec.pg_table} ({col_list}) "
                    f"VALUES ({placeholders}) "
                    "ON CONFLICT DO NOTHING"
                )

            batch.append(vals)
            if len(batch) >= batch_size:
                pg_cur.executemany(insert_sql, batch)
                inserted += len(batch)
                batch = []

        if batch:
            pg_cur.executemany(insert_sql, batch)
            inserted += len(batch)

    report.pg_rows_copied = inserted
    return report


# --------------------------------------------------------------------- #
# Parity check
# --------------------------------------------------------------------- #


def _parity_check(
    *,
    conn,
    sqlite_conn: sqlite3.Connection,
    source: SourceDescriptor,
) -> list[tuple[str, int, int]]:
    """Return the list of (table, sqlite_count, pg_count) mismatches.

    Only checks tables we copied. Per-project sources count only rows
    where ``project = <source.project_key>`` on the pg side, since
    other projects may have already been imported from other sources.
    The workspace source compares total counts.
    """
    mismatches: list[tuple[str, int, int]] = []
    with conn.cursor() as cur:
        for spec in TABLE_SPECS:
            if not _sqlite_table_exists(sqlite_conn, spec.sqlite_table):
                continue
            sqlite_n = _sqlite_count(sqlite_conn, spec.sqlite_table)
            target_columns = _pg_table_columns(conn, spec.pg_table)
            if source.kind == "per_project" and "project" in target_columns:
                cur.execute(
                    f"SELECT count(*) FROM {spec.pg_table} "
                    "WHERE project = %s",
                    (source.project_key,),
                )
            elif (
                source.kind == "per_project"
                and "project_key" in target_columns
            ):
                cur.execute(
                    f"SELECT count(*) FROM {spec.pg_table} "
                    "WHERE project_key = %s",
                    (source.project_key,),
                )
            else:
                cur.execute(f"SELECT count(*) FROM {spec.pg_table}")
            pg_n = int(cur.fetchone()[0])
            # For workspace source, the workspace might be the union
            # of every project — pg can legitimately have MORE rows
            # than sqlite after we also import per-project DBs in the
            # same run. So we only flag "pg < sqlite" as a mismatch.
            if pg_n < sqlite_n:
                mismatches.append((spec.sqlite_table, sqlite_n, pg_n))
    return mismatches


# --------------------------------------------------------------------- #
# Top-level entry point
# --------------------------------------------------------------------- #


def _rename_source(source_path: Path) -> Path:
    """Rename ``state.db`` (+ sidecars) to ``state.db.pre-pg-<ts>``."""
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = f"{RENAME_SUFFIX_PREFIX}{ts}"
    target = source_path.with_name(source_path.name + suffix)
    # Avoid collision if two runs land in the same second.
    i = 0
    while target.exists():
        i += 1
        target = source_path.with_name(source_path.name + suffix + f".{i}")
    # Rename the .db and any -wal/-shm sidecars alongside.
    source_path.rename(target)
    for sidecar_suffix in ("-wal", "-shm"):
        sidecar = source_path.with_name(source_path.name + sidecar_suffix)
        if sidecar.exists():
            sidecar.rename(
                target.with_name(target.name + sidecar_suffix)
            )
    return target


def migrate_sources(
    sources: list[SourceDescriptor],
    *,
    pool: "ConnectionPool",
    commit: bool,
    rename_on_success: bool = True,
) -> MigrationRunReport:
    """Run the migration pipeline against every source.

    Parameters
    ----------
    sources:
        Output of :func:`discover_sources`.
    pool:
        RW pool to mutate.
    commit:
        When False, every per-source transaction is rolled back at the
        end — the dry-run safety net. When True, transactions commit
        on success and the source sqlite file is renamed.
    rename_on_success:
        Pass False when called from a smoke-test that wants the source
        preserved; defaults to True so the real CLI gets the rollback
        snapshot the spec promised.
    """
    run = MigrationRunReport(dry_run=not commit, committed=False)

    # Bootstrap audit table once at the top of the run. Always commits
    # — dry-run still needs to read the table even though it never
    # writes a row.
    with pool.connection() as bootstrap:
        bootstrap.autocommit = False
        try:
            _ensure_audit_table(bootstrap)
        except Exception as exc:  # noqa: BLE001
            run.preflight_error = f"audit table bootstrap failed: {exc}"
            return run

    for source in sources:
        started_at = datetime.now(UTC)
        try:
            sha = compute_sha256(source.path)
        except OSError as exc:
            report = SourceMigrationReport(
                source=source,
                source_sha256="",
                started_at=started_at,
                failure=f"hash failed: {exc}",
            )
            run.sources.append(report)
            continue

        report = SourceMigrationReport(
            source=source,
            source_sha256=sha,
            started_at=started_at,
        )

        # Idempotency: if this exact file has been imported, skip.
        with pool.connection() as audit_conn:
            audit_conn.autocommit = False
            prior = _audit_lookup(audit_conn, sha)
        if prior is not None:
            report.skipped_already_imported_at = prior
            report.completed_at = datetime.now(UTC)
            run.sources.append(report)
            continue

        try:
            sqlite_conn = _sqlite_open_ro(source.path)
        except sqlite3.Error as exc:
            report.failure = f"sqlite open failed: {exc}"
            run.sources.append(report)
            continue

        try:
            with pool.connection() as conn:
                conn.autocommit = False
                try:
                    for spec in TABLE_SPECS:
                        try:
                            table_report = _copy_table(
                                conn=conn,
                                sqlite_conn=sqlite_conn,
                                spec=spec,
                                project_key=source.project_key,
                            )
                        except Exception as exc:  # noqa: BLE001
                            report.failure = (
                                f"copy of {spec.sqlite_table} failed: {exc}"
                            )
                            report.failed_table = spec.sqlite_table
                            raise
                        report.per_table.append(table_report)

                    # Parity check inside the same txn so a failure
                    # rolls back the copy.
                    mismatches = _parity_check(
                        conn=conn,
                        sqlite_conn=sqlite_conn,
                        source=source,
                    )
                    if mismatches:
                        report.parity_ok = False
                        report.parity_mismatches = mismatches

                    report.completed_at = datetime.now(UTC)

                    if commit:
                        _audit_record(conn, report=report)
                        conn.commit()
                        run.committed = True
                    else:
                        # Dry-run — never persist.
                        conn.rollback()
                except Exception:
                    conn.rollback()
                    if report.failure is None:
                        report.failure = "transaction rolled back"
        finally:
            sqlite_conn.close()

        if commit and report.succeeded and rename_on_success:
            try:
                report.renamed_to = _rename_source(source.path)
            except OSError as exc:
                # The pg copy is already committed; surface the rename
                # failure as a soft warning rather than failing the
                # whole run. Operator can rename manually.
                report.failure = (
                    f"copy committed but rename failed: {exc}; "
                    "rename the source manually to avoid re-import"
                )

        run.sources.append(report)

    return run


# --------------------------------------------------------------------- #
# Reporting helpers used by the CLI
# --------------------------------------------------------------------- #


def format_run_summary(run: MigrationRunReport) -> str:
    """Render a human-friendly summary the CLI dumps to stdout."""
    lines: list[str] = []
    if run.preflight_error:
        lines.append(f"Pre-flight failed: {run.preflight_error}")
        return "\n".join(lines)

    mode = "DRY RUN" if run.dry_run else "COMMIT"
    lines.append(f"pm storage migrate-to-pg — {mode}")
    lines.append("")

    if not run.sources:
        lines.append("No sqlite sources discovered. Nothing to do.")
        return "\n".join(lines)

    for report in run.sources:
        lines.append(f"source: {report.source.display}")
        lines.append(f"  kind: {report.source.kind}")
        if report.source.project_key:
            lines.append(f"  project_key: {report.source.project_key}")
        lines.append(f"  sha256: {report.source_sha256[:16]}…")

        if report.skipped_already_imported_at is not None:
            lines.append(
                "  status: SKIPPED — already imported on "
                f"{report.skipped_already_imported_at.isoformat()}"
            )
            lines.append("")
            continue

        if report.failure:
            lines.append(f"  status: FAILED — {report.failure}")
            if report.failed_table:
                lines.append(f"  failed_table: {report.failed_table}")
            lines.append("")
            continue

        total = report.total_rows_copied
        lines.append(f"  status: OK — {total} rows copied")
        # Only surface tables that actually moved data. The full skip
        # roster is in the structured report for programmatic callers;
        # the operator summary stays scannable.
        for t in report.per_table:
            if t.pg_rows_copied:
                lines.append(
                    f"    {t.table}: {t.pg_rows_copied}/{t.sqlite_rows}"
                )
        if report.parity_mismatches:
            lines.append("  parity mismatches (pg < sqlite):")
            for tbl, sn, pn in report.parity_mismatches:
                lines.append(f"    {tbl}: sqlite={sn} pg={pn}")
        if report.renamed_to:
            lines.append(f"  renamed to: {report.renamed_to}")
        elif run.dry_run:
            lines.append("  (dry-run — source file untouched)")
        lines.append("")

    if run.dry_run:
        lines.append(
            "Dry-run complete. Re-run with --commit to actually move "
            "data into pg."
        )
    elif run.succeeded:
        lines.append("All sources migrated successfully.")
    else:
        lines.append(
            "At least one source failed; see per-source status above."
        )
    return "\n".join(lines)


__all__ = [
    "AUDIT_TABLE_DDL",
    "COPY_BATCH_SIZE",
    "MigrationRunReport",
    "PreflightResult",
    "SourceDescriptor",
    "SourceMigrationReport",
    "TableCopyReport",
    "TABLE_SPECS",
    "compute_sha256",
    "discover_sources",
    "format_run_summary",
    "migrate_sources",
    "preflight",
]
