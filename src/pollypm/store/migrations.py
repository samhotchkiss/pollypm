"""Schema migration gate — dry-run check, apply, and refuse-start (#717).

PollyPM ships schema migrations in two append-only lists:

* :attr:`pollypm.storage.state.StateStore._MIGRATIONS` — the state-domain
  migrations, tracked in the ``schema_version`` table.
* :attr:`pollypm.work.schema._WORK_MIGRATIONS` — work-service migrations,
  tracked in the ``work_schema_version`` table.

Both tables live in the same workspace-scope ``state.db``. This module
provides a unified façade:

* :func:`inspect` reads those tables read-only and returns the pending /
  applied migration set without touching the DB.
* :func:`check_against_clone` copies the DB to ``~/.pollypm/migration-
  check.db``, opens it read-write, replays every pending migration, and
  reports or rolls back on failure. Never mutates the live DB.
* :func:`apply` runs pending migrations on the real DB by instantiating
  the existing writers (``StateStore`` + ``SQLiteWorkService``) whose
  ``__init__`` already replays migrations idempotently. Recorded in a
  unified ``schema_migrations`` audit table.
* :func:`require_no_pending_or_exit` is the refuse-start gate: probes the
  live DB read-only, prints recovery steps and exits non-zero if any
  migration is pending.

The unified ``schema_migrations`` audit table is additive — it mirrors
the two underlying tables so operators have a single place to inspect
migration history. It does not replace ``schema_version`` or
``work_schema_version``; those stay authoritative for the runners.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path


# Namespace identifiers — must stay stable; persisted in schema_migrations.
NAMESPACE_STATE = "state"
NAMESPACE_WORK = "work"


class UnusableDatabaseError(RuntimeError):
    """Raised when ``state.db`` exists but cannot be read as SQLite."""

    def __init__(self, db_path: Path, detail: str) -> None:
        self.db_path = db_path
        self.detail = detail
        super().__init__(f"{db_path}: {detail}")


@dataclass(frozen=True)
class PendingMigration:
    """One migration that has not yet been applied to a target DB."""

    namespace: str
    version: int
    description: str


@dataclass
class MigrationStatus:
    """Read-only snapshot of migration state for a single DB."""

    db_path: Path
    applied: dict[str, int] = field(default_factory=dict)
    latest: dict[str, int] = field(default_factory=dict)
    pending: list[PendingMigration] = field(default_factory=list)

    @property
    def up_to_date(self) -> bool:
        return not self.pending


@dataclass
class CheckOutcome:
    """Result of a ``pm migrate --check`` dry run against a clone."""

    ok: bool
    applied: list[PendingMigration] = field(default_factory=list)
    tables_changed: list[str] = field(default_factory=list)
    error: str | None = None
    clone_path: Path | None = None


@dataclass
class ApplyOutcome:
    """Result of a ``pm migrate --apply`` on the live DB."""

    applied: list[PendingMigration] = field(default_factory=list)
    already_up_to_date: bool = False


_SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    namespace TEXT NOT NULL,
    version INTEGER NOT NULL,
    description TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    PRIMARY KEY (namespace, version)
)
"""


def _declared_state_migrations() -> list[tuple[int, str]]:
    from pollypm.storage.state import StateStore

    return [(version, desc) for version, desc, _ in StateStore._MIGRATIONS]


def _declared_work_migrations() -> list[tuple[int, str]]:
    from pollypm.work.schema import _WORK_MIGRATIONS

    return [(version, desc) for version, desc, _ in _WORK_MIGRATIONS]


def _readonly_connect(db_path: Path) -> sqlite3.Connection | None:
    """Open a read-only SQLite connection, or ``None`` if the DB is absent."""
    if not db_path.is_file():
        return None
    uri = f"file:{db_path}?mode=ro"
    try:
        return sqlite3.connect(uri, uri=True, timeout=1.0)
    except sqlite3.Error:
        return None


def _looks_like_unusable_database_error(exc: sqlite3.Error) -> bool:
    detail = str(exc).lower()
    return (
        "file is not a database" in detail
        or "database disk image is malformed" in detail
        or "file is encrypted or is not a database" in detail
    )


def _ensure_readable_sqlite(conn: sqlite3.Connection, db_path: Path) -> None:
    """Fail fast when an existing file is not a usable SQLite database."""
    try:
        row = conn.execute("PRAGMA quick_check").fetchone()
    except sqlite3.Error as exc:
        if _looks_like_unusable_database_error(exc):
            raise UnusableDatabaseError(db_path, str(exc)) from exc
        return
    if row and str(row[0]).lower() != "ok":
        raise UnusableDatabaseError(db_path, str(row[0]))


def _applied_version(conn: sqlite3.Connection, table: str) -> int:
    """Return ``MAX(version)`` from a schema-tracking table, 0 if missing."""
    try:
        row = conn.execute(
            f"SELECT COALESCE(MAX(version), 0) FROM {table}"
        ).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row and row[0] is not None else 0


def inspect(db_path: Path) -> MigrationStatus:
    """Compute pending / applied migrations for ``db_path`` without mutating it.

    For a DB that does not yet exist, every declared migration is
    considered pending — the caller is responsible for bootstrapping
    via :func:`apply` (which creates the file on first open).
    """
    state_declared = _declared_state_migrations()
    work_declared = _declared_work_migrations()

    latest = {
        NAMESPACE_STATE: max((v for v, _ in state_declared), default=0),
        NAMESPACE_WORK: max((v for v, _ in work_declared), default=0),
    }

    conn = _readonly_connect(db_path)
    if conn is None:
        # Missing DB — all migrations are pending.
        pending = [
            PendingMigration(NAMESPACE_STATE, v, d) for v, d in state_declared
        ] + [
            PendingMigration(NAMESPACE_WORK, v, d) for v, d in work_declared
        ]
        return MigrationStatus(
            db_path=db_path,
            applied={NAMESPACE_STATE: 0, NAMESPACE_WORK: 0},
            latest=latest,
            pending=pending,
        )

    try:
        _ensure_readable_sqlite(conn, db_path)
        applied = {
            NAMESPACE_STATE: _applied_version(conn, "schema_version"),
            NAMESPACE_WORK: _applied_version(conn, "work_schema_version"),
        }
    finally:
        conn.close()

    pending: list[PendingMigration] = []
    for version, desc in state_declared:
        if version > applied[NAMESPACE_STATE]:
            pending.append(PendingMigration(NAMESPACE_STATE, version, desc))
    for version, desc in work_declared:
        if version > applied[NAMESPACE_WORK]:
            pending.append(PendingMigration(NAMESPACE_WORK, version, desc))

    return MigrationStatus(
        db_path=db_path, applied=applied, latest=latest, pending=pending,
    )


def _table_set(db_path: Path) -> set[str]:
    """Return the set of user table names in ``db_path`` (empty on failure)."""
    if not db_path.is_file():
        return set()
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
    except sqlite3.Error:
        return set()
    # #1018 — read-only probe, but the workspace DB is held by the live
    # writers; ``busy_timeout`` lets us ride out a checkpoint without
    # racing the migration gate's tight 5 s connect window.
    from pollypm.storage.sqlite_pragmas import apply_workspace_pragmas

    apply_workspace_pragmas(conn)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {row[0] for row in rows}
    finally:
        conn.close()


def _default_clone_path() -> Path:
    """Location of the dry-run clone (~/.pollypm/migration-check.db)."""
    home = Path(os.environ.get("POLLYPM_HOME", str(Path.home() / ".pollypm")))
    return home / "migration-check.db"


def check_against_clone(
    db_path: Path,
    clone_path: Path | None = None,
) -> CheckOutcome:
    """Dry-run: clone the DB, replay pending migrations, report or roll back.

    On failure the clone is removed so the next ``--check`` starts from a
    clean slate — stale sidecar DBs from a partial run would confuse
    ``pm doctor``. On success the clone is kept so operators can inspect
    the resulting schema if they want to.
    """
    status = inspect(db_path)
    if not status.pending:
        return CheckOutcome(
            ok=True, applied=[], tables_changed=[], clone_path=None,
        )

    clone = clone_path or _default_clone_path()
    clone.parent.mkdir(parents=True, exist_ok=True)
    # Wipe any stale clone + its WAL / journal sidecars from a prior run.
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(str(clone) + suffix).unlink(missing_ok=True)

    if db_path.is_file():
        shutil.copy2(db_path, clone)

    tables_before = _table_set(clone)

    try:
        _apply_all(clone)
    except Exception as exc:  # noqa: BLE001
        # Roll back by removing the clone — there is no live connection
        # to issue a SQL rollback against (StateStore opens its own).
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(clone) + suffix).unlink(missing_ok=True)
        return CheckOutcome(
            ok=False,
            applied=[],
            tables_changed=[],
            error=str(exc),
            clone_path=None,
        )

    tables_after = _table_set(clone)
    changes = sorted(
        [f"+{name}" for name in tables_after - tables_before]
        + [f"-{name}" for name in tables_before - tables_after]
    )
    return CheckOutcome(
        ok=True,
        applied=list(status.pending),
        tables_changed=changes,
        clone_path=clone,
    )


def _apply_all(db_path: Path) -> None:
    """Replay every migration idempotently by opening the existing writers.

    ``StateStore.__init__`` already replays ``_MIGRATIONS`` against the
    DB; ``SQLiteWorkService.__init__`` (via ``create_work_tables``) does
    the same for ``_WORK_MIGRATIONS``. We also mirror the applied set
    into the unified ``schema_migrations`` audit table so operators have
    a single pane of glass.
    """
    from pollypm.storage.state import StateStore
    from pollypm.work.sqlite_service import SQLiteWorkService

    with StateStore(db_path) as _store:
        pass

    with SQLiteWorkService(db_path) as _svc:
        pass

    _record_schema_migrations(db_path)


def _record_schema_migrations(db_path: Path) -> None:
    """Mirror ``schema_version`` + ``work_schema_version`` into ``schema_migrations``.

    Safe to call repeatedly — the table has a ``(namespace, version)``
    primary key and we ``INSERT OR IGNORE`` so previously-recorded rows
    are left alone. This is the retrofit path for DBs that predate the
    unified audit table: the first call after upgrade back-fills every
    already-applied migration.
    """
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    # #1018 — schema-audit writer; flip into WAL + 5 s busy_timeout so
    # an ``INSERT OR IGNORE`` doesn't race the heartbeat alert upserts
    # holding the writer briefly.
    from pollypm.storage.sqlite_pragmas import apply_workspace_pragmas

    apply_workspace_pragmas(conn)
    try:
        conn.execute(_SCHEMA_MIGRATIONS_DDL)
        now = datetime.now(UTC).isoformat()

        try:
            rows = conn.execute(
                "SELECT version, description, applied_at FROM schema_version"
            ).fetchall()
        except sqlite3.Error:
            rows = []
        for version, description, applied_at in rows:
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations "
                "(namespace, version, description, applied_at) "
                "VALUES (?, ?, ?, ?)",
                (NAMESPACE_STATE, int(version), str(description), applied_at or now),
            )

        try:
            rows = conn.execute(
                "SELECT version, description, applied_at FROM work_schema_version"
            ).fetchall()
        except sqlite3.Error:
            rows = []
        for version, description, applied_at in rows:
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations "
                "(namespace, version, description, applied_at) "
                "VALUES (?, ?, ?, ?)",
                (NAMESPACE_WORK, int(version), str(description), applied_at or now),
            )
        conn.commit()
    finally:
        conn.close()


def apply(db_path: Path) -> ApplyOutcome:
    """Apply pending migrations to the live DB.

    Idempotent: a second call after everything is up to date is a cheap
    no-op that still touches the writers (to retrofit
    ``schema_migrations``) but records nothing new.
    """
    status_before = inspect(db_path)
    if not status_before.pending:
        # Still record the audit table on first upgrade post-#717.
        if db_path.is_file():
            _record_schema_migrations(db_path)
        return ApplyOutcome(applied=[], already_up_to_date=True)

    _apply_all(db_path)
    return ApplyOutcome(applied=list(status_before.pending), already_up_to_date=False)


def format_pending_summary(status: MigrationStatus) -> str:
    """Render pending migrations as a compact multi-line string."""
    if not status.pending:
        return "All migrations up to date."
    n_pending = len(status.pending)
    migration_word = "migration" if n_pending == 1 else "migrations"
    lines = [f"{n_pending} pending {migration_word}:"]
    for item in status.pending:
        lines.append(f"  [{item.namespace}] v{item.version}: {item.description}")
    return "\n".join(lines)


def format_unusable_database_message(error: UnusableDatabaseError) -> str:
    """Render a friendly corruption/recovery message for CLI surfaces."""
    from pollypm.structured_message import StructuredUserMessage

    details = "\n".join([
        f"DB: {error.db_path}",
        f"SQLite error: {error.detail}",
    ])
    msg = StructuredUserMessage(
        summary="Cannot use state.db — the file is not a valid SQLite database.",
        why=(
            "PollyPM could not read its central store. This is data-store "
            "corruption, not a pending schema migration, so `pm migrate` "
            "cannot repair it."
        ),
        next_action=(
            "Run `pm doctor` to confirm the failure, then restore from a "
            "recent backup with `pm restore <snapshot> --confirm` or move "
            "the corrupt state.db aside before reinitializing."
        ),
        suggested_actions=(
            ("Diagnose", "pm doctor"),
            ("List backups", "ls ~/.pollypm/backups"),
            (
                "Restore backup",
                "pm restore ~/.pollypm/backups/<snapshot>.db.gz --confirm",
            ),
        ),
        details=details,
    )
    return msg.render_cli(show_details=True)


def exit_unusable_database(error: UnusableDatabaseError, *, code: int = 2) -> None:
    """Print the corruption message and exit with a non-zero CLI code."""
    sys.stderr.write(format_unusable_database_message(error) + "\n")
    raise SystemExit(code)


# ---------------------------------------------------------------------------
# Refuse-start gate
# ---------------------------------------------------------------------------


# Env var that lets ``pm migrate`` bypass the refuse-start gate so the
# migration CLI can actually open the store to fix the situation. Also
# useful for tests that want to exercise the raw store.
_BYPASS_ENV = "POLLYPM_SKIP_MIGRATION_GATE"


def bypass_env_is_set() -> bool:
    return bool(os.environ.get(_BYPASS_ENV))


def set_bypass(enabled: bool) -> None:
    """Turn the refuse-start gate off (``pm migrate`` uses this)."""
    if enabled:
        os.environ[_BYPASS_ENV] = "1"
    else:
        os.environ.pop(_BYPASS_ENV, None)


def _format_refuse_start_message(status: MigrationStatus) -> str:
    """Render the refuse-start gate's refusal in the four-field
    structured shape (#760). Users see the same layout anywhere
    PollyPM surfaces a user-facing error."""
    from pollypm.structured_message import StructuredUserMessage

    count = len(status.pending)
    plural = "s" if count != 1 else ""
    pending_lines = [
        f"  [{item.namespace}] v{item.version}: {item.description}"
        for item in status.pending
    ]
    details = "\n".join([
        f"DB: {status.db_path}",
        f"{count} pending migration{plural}:",
        *pending_lines,
    ])
    msg = StructuredUserMessage(
        summary=f"Cannot start — {count} pending schema migration{plural} on state.db.",
        why=(
            "Your PollyPM was upgraded since last boot and the database "
            "needs an update before any session can connect. Running "
            "without it risks cross-version data corruption."
        ),
        next_action=(
            f"Apply the pending migration{plural} with pm migrate --apply "
            "(pm is the PollyPM CLI installed alongside pollypm)."
        ),
        # Three escalating options so the user can pick their comfort
        # level: safe apply, dry-run-first, or emergency bypass for
        # non-destructive CLI work. The bypass is last because it
        # leaves the DB on the old schema.
        suggested_actions=(
            ("Apply (recommended)", "pm migrate --apply"),
            ("Dry-run first", "pm migrate --check"),
            (
                "Bypass for this shell only (risky)",
                "export POLLYPM_SKIP_MIGRATION_GATE=1",
            ),
        ),
        details=details,
    )
    return msg.render_cli(show_details=True)


def require_no_pending_or_exit(db_path: Path) -> None:
    """Refuse-start gate: exit non-zero if the DB has pending migrations.

    Skipped when ``POLLYPM_SKIP_MIGRATION_GATE`` is set — ``pm migrate``
    sets this internally so the apply path can itself open the store.

    A missing DB file is NOT a gate violation: it's the legitimate
    first-boot path where ``StateStore``/``SQLiteWorkService`` will
    create the file and apply every migration from scratch. The gate
    protects against the "installed new code, forgot to migrate" case,
    which by definition requires an existing older DB.
    """
    if bypass_env_is_set():
        return
    if not db_path.is_file():
        return
    try:
        status = inspect(db_path)
    except UnusableDatabaseError as exc:
        exit_unusable_database(exc)
    if status.up_to_date:
        return
    sys.stderr.write(_format_refuse_start_message(status) + "\n")
    raise SystemExit(2)


def check_pending(db_path: Path | None = None) -> tuple[bool, str]:
    """Report whether the workspace state.db has any pending migrations.

    Returns ``(ok, detail)`` where ``ok=True`` means the DB is up to date
    (or doesn't exist yet, which is a legitimate first-boot state).
    Used by ``pm upgrade`` to abort before a version bump if the operator
    would land on an incompatible schema.
    """
    if db_path is None:
        try:
            from pollypm.config import DEFAULT_CONFIG_PATH, load_config
        except ImportError:
            return (True, "skipped: config module unavailable")
        if not DEFAULT_CONFIG_PATH.is_file():
            return (True, "skipped: no config present")
        try:
            config = load_config(DEFAULT_CONFIG_PATH)
        except Exception as exc:  # noqa: BLE001
            return (True, f"skipped: config load failed ({type(exc).__name__})")
        db_path = config.project.state_db
    if not db_path.is_file():
        return (True, "skipped: no state.db present")
    try:
        status = inspect(db_path)
    except Exception as exc:  # noqa: BLE001
        return (False, f"migration check raised {type(exc).__name__}: {exc}")
    if status.up_to_date:
        return (True, "ok: migrations up to date")
    return (False, format_pending_summary(status))
