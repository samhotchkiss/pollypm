"""Legacy :class:`StateStore` — domain tables that haven't moved yet.

#342 retired the event/alert/message-shaped surfaces that this module
used to own (now on :class:`pollypm.store.SQLAlchemyStore`). What's
left are the domain tables we didn't have time to port to SQLAlchemy
Core Table defs in a single PR:

* ``sessions`` / ``account_usage`` / ``account_runtime`` /
  ``session_runtime`` — the operator session model.
* ``work_jobs`` — background job queue.
* ``worktrees`` — the shared worktree ledger.
* ``memory_entries`` + ``memory_summaries`` FTS — knowledge recall.
* ``heartbeats`` / ``leases`` / ``checkpoints`` / ``token_*`` — ops
  tables.

TODO(#342-followup): port each of the tables above to Core ``Table()``
definitions in :mod:`pollypm.store.schema` and move the corresponding
writers off :class:`StateStore`. Then delete this module entirely. The
unified ``messages`` writers already live on :class:`Store`; what's
left is domain-data migration.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pollypm.storage.fts_query import normalize_fts_query


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    description TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    name TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    project TEXT NOT NULL,
    provider TEXT NOT NULL,
    account TEXT NOT NULL,
    cwd TEXT NOT NULL,
    window_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    type TEXT NOT NULL,
    tier TEXT NOT NULL DEFAULT 'immediate',
    recipient TEXT NOT NULL,
    sender TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open',
    parent_id INTEGER,
    subject TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    labels TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_recipient_state
ON messages(recipient, state);

CREATE INDEX IF NOT EXISTS idx_messages_type_tier
ON messages(type, tier);

CREATE INDEX IF NOT EXISTS idx_messages_scope_created
ON messages(scope, created_at);

CREATE TABLE IF NOT EXISTS heartbeats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_name TEXT NOT NULL,
    tmux_window TEXT NOT NULL,
    pane_id TEXT NOT NULL,
    pane_command TEXT NOT NULL,
    pane_dead INTEGER NOT NULL,
    log_bytes INTEGER NOT NULL,
    snapshot_path TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leases (
    session_name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    note TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_usage (
    account_name TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    plan TEXT NOT NULL,
    health TEXT NOT NULL,
    usage_summary TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    used_pct INTEGER,
    remaining_pct INTEGER,
    reset_at TEXT,
    period_label TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_runtime (
    account_name TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    available_at TEXT,
    access_expires_at TEXT,
    refresh_available INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_runtime (
    session_name TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'healthy',
    effective_account TEXT,
    effective_provider TEXT,
    recovery_attempts INTEGER NOT NULL DEFAULT 0,
    recovery_window_started_at TEXT,
    last_failure_type TEXT,
    last_failure_message TEXT,
    last_checkpoint_path TEXT,
    retry_at TEXT,
    last_recovered_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_name TEXT NOT NULL,
    project_key TEXT NOT NULL,
    level TEXT NOT NULL,
    json_path TEXT NOT NULL,
    summary_path TEXT NOT NULL,
    snapshot_path TEXT NOT NULL,
    summary_text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS worktrees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_key TEXT NOT NULL,
    lane_kind TEXT NOT NULL,
    lane_key TEXT NOT NULL,
    session_name TEXT,
    issue_key TEXT,
    path TEXT NOT NULL,
    branch TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_worktrees_active
ON worktrees(project_key, lane_kind, lane_key, status);

CREATE TABLE IF NOT EXISTS token_samples (
    session_name TEXT PRIMARY KEY,
    account_name TEXT NOT NULL,
    provider TEXT NOT NULL,
    model_name TEXT NOT NULL,
    project_key TEXT NOT NULL,
    cumulative_tokens INTEGER NOT NULL,
    observed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS token_usage_hourly (
    hour_bucket TEXT NOT NULL,
    account_name TEXT NOT NULL,
    provider TEXT NOT NULL,
    model_name TEXT NOT NULL,
    project_key TEXT NOT NULL,
    tokens_used INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (hour_bucket, account_name, provider, model_name, project_key)
);

CREATE TABLE IF NOT EXISTS memory_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    tags TEXT NOT NULL,
    source TEXT NOT NULL,
    file_path TEXT NOT NULL,
    summary_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'project',
    importance INTEGER NOT NULL DEFAULT 3,
    superseded_by INTEGER,
    ttl_at TEXT,
    scope_tier TEXT NOT NULL DEFAULT 'project'
);

CREATE INDEX IF NOT EXISTS idx_memory_entries_scope
ON memory_entries(scope, id DESC);
-- idx_memory_entries_type is created by migration 8 after the ``type``
-- column is back-filled onto pre-M01 databases.
-- idx_memory_entries_tier is created by migration 10 after the
-- ``scope_tier`` column is back-filled onto pre-M03 databases.

-- FTS5 keyword index over memory_entries.{title, body, tags}. The entries
-- table is the source of truth; this contentless-FTS mirror (content=…) is
-- kept in sync by triggers below. ``rowid`` mirrors ``memory_entries.id`` so
-- joins are trivial (#231 / M02).
-- Tokenizer: ``porter unicode61 remove_diacritics 2`` — Porter stemmer
-- chained on top of unicode61 folds "testing"/"tests"/"tested" to a
-- common stem so a query for "testing" surfaces all three. Accent
-- folding keeps the index forgiving for non-ASCII content.
CREATE VIRTUAL TABLE IF NOT EXISTS memory_entries_fts
USING fts5(
    title,
    body,
    tags,
    content='memory_entries',
    content_rowid='id',
    tokenize='porter unicode61 remove_diacritics 2'
);

-- Triggers: keep the FTS index in lock-step with writes. A contentless FTS
-- table stores no rows by itself; after_delete uses the 'delete' command
-- form ``INSERT INTO fts(fts, rowid, …) VALUES('delete', …)`` so the index
-- matches before/after consistently. See SQLite FTS5 docs §4.4.
CREATE TRIGGER IF NOT EXISTS memory_entries_fts_ai
AFTER INSERT ON memory_entries BEGIN
    INSERT INTO memory_entries_fts(rowid, title, body, tags)
    VALUES (new.id, new.title, new.body, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS memory_entries_fts_ad
AFTER DELETE ON memory_entries BEGIN
    INSERT INTO memory_entries_fts(memory_entries_fts, rowid, title, body, tags)
    VALUES ('delete', old.id, old.title, old.body, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS memory_entries_fts_au
AFTER UPDATE ON memory_entries BEGIN
    INSERT INTO memory_entries_fts(memory_entries_fts, rowid, title, body, tags)
    VALUES ('delete', old.id, old.title, old.body, old.tags);
    INSERT INTO memory_entries_fts(rowid, title, body, tags)
    VALUES (new.id, new.title, new.body, new.tags);
END;

CREATE TABLE IF NOT EXISTS memory_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    summary_text TEXT NOT NULL,
    summary_path TEXT NOT NULL,
    entry_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS work_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handler_name TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    dedupe_key TEXT,
    enqueued_at TEXT NOT NULL,
    run_after TEXT NOT NULL,
    claimed_at TEXT,
    claimed_by TEXT,
    finished_at TEXT,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_work_jobs_claim
ON work_jobs(status, run_after, id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_work_jobs_dedupe_queued
ON work_jobs(dedupe_key)
WHERE dedupe_key IS NOT NULL AND status IN ('queued', 'claimed');

CREATE TABLE IF NOT EXISTS architect_resume_tokens (
    project_key TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    session_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    last_active_at TEXT NOT NULL
);
"""


@dataclass(slots=True)
class SessionRecord:
    name: str
    role: str
    project: str
    provider: str
    account: str
    cwd: str
    window_name: str


@dataclass(slots=True)
class EventRecord:
    session_name: str
    event_type: str
    message: str
    created_at: str


@dataclass(slots=True)
class HeartbeatRecord:
    session_name: str
    tmux_window: str
    pane_id: str
    pane_command: str
    pane_dead: bool
    log_bytes: int
    snapshot_path: str
    snapshot_hash: str
    created_at: str


@dataclass(slots=True)
class AlertRecord:
    session_name: str
    alert_type: str
    severity: str
    message: str
    status: str
    created_at: str
    updated_at: str
    alert_id: int | None = None


@dataclass(slots=True)
class LeaseRecord:
    session_name: str
    owner: str
    note: str
    updated_at: str


@dataclass(slots=True)
class AccountUsageRecord:
    account_name: str
    provider: str
    plan: str
    health: str
    usage_summary: str
    raw_text: str
    updated_at: str
    used_pct: int | None = None
    remaining_pct: int | None = None
    reset_at: str | None = None
    period_label: str | None = None


@dataclass(slots=True)
class AccountRuntimeRecord:
    account_name: str
    provider: str
    status: str
    reason: str
    available_at: str | None
    access_expires_at: str | None
    refresh_available: bool
    updated_at: str


@dataclass(slots=True)
class SessionRuntimeRecord:
    session_name: str
    status: str
    effective_account: str | None
    effective_provider: str | None
    recovery_attempts: int
    recovery_window_started_at: str | None
    last_failure_type: str | None
    last_failure_message: str | None
    last_checkpoint_path: str | None
    retry_at: str | None
    last_recovered_at: str | None
    updated_at: str


@dataclass(slots=True)
class CheckpointRecord:
    session_name: str
    project_key: str
    level: str
    json_path: str
    summary_path: str
    snapshot_path: str
    summary_text: str
    created_at: str


@dataclass(slots=True)
class WorktreeRecord:
    project_key: str
    lane_kind: str
    lane_key: str
    session_name: str | None
    issue_key: str | None
    path: str
    branch: str
    status: str
    created_at: str
    updated_at: str


@dataclass(slots=True)
class TokenSampleRecord:
    session_name: str
    account_name: str
    provider: str
    model_name: str
    project_key: str
    cumulative_tokens: int
    observed_at: str


@dataclass(slots=True)
class TokenUsageHourlyRecord:
    hour_bucket: str
    account_name: str
    provider: str
    model_name: str
    project_key: str
    tokens_used: int
    updated_at: str


@dataclass(slots=True)
class MemoryEntryRecord:
    entry_id: int
    scope: str
    kind: str
    title: str
    body: str
    tags: tuple[str, ...]
    source: str
    file_path: str
    summary_path: str
    created_at: str
    updated_at: str
    # M01 typed-schema columns — defaults match the schema DEFAULTs so
    # legacy construction paths remain valid.
    type: str = "project"
    importance: int = 3
    superseded_by: int | None = None
    ttl_at: str | None = None
    # M03 tiered-scope column. Default matches the schema DEFAULT so
    # pre-M03 records and tests that predate the column keep working.
    scope_tier: str = "project"


@dataclass(slots=True)
class MemorySummaryRecord:
    summary_id: int
    scope: str
    summary_text: str
    summary_path: str
    entry_count: int
    created_at: str


@dataclass(slots=True)
class ArchitectResumeRecord:
    """Persisted resume token for an idled-out architect session.

    When an architect-* session has been project-idle for 2h+, the
    supervisor captures the provider's session UUID, kills the tmux
    window, and persists this record. On next demand the architect
    relaunches via ``provider.resume_launch_cmd(session_id, ...)``.
    """
    project_key: str
    provider: str
    session_id: str
    captured_at: str
    last_active_at: str


def _strip_alert_subject(subject: str) -> str:
    if subject.startswith("[Alert] "):
        return subject[len("[Alert] "):]
    if subject.startswith("[Alert]"):
        return subject[len("[Alert]"):].lstrip()
    return subject


def _safe_payload(raw: object) -> dict:
    """Decode a ``payload_json`` column into a dict, defensively.

    Producers always write dict payloads, but a hand-edited or legacy
    DB row could parse to a list/null/string — downstream callers do
    ``payload.get(...)`` and would AttributeError. Coerce non-dict
    shapes to ``{}`` so a corrupt row degrades gracefully. Mirrors the
    cycle 107 fix in ``SQLAlchemyStore.query_messages``.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_tags(raw: object) -> tuple:
    """Decode a ``tags`` JSON column into a tuple, defensively.

    Companion to ``_safe_payload`` for ``memory_entries.tags``: producers
    always serialise a list, but a hand-edited or legacy row could land
    a dict / string / null. ``tuple(dict)`` would return the keys and
    ``tuple(str)`` the characters — subtle data-corruption bugs in
    consumers iterating ``record.tags``. Coerce non-list shapes to ``()``.
    """
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    return tuple(parsed) if isinstance(parsed, list) else ()


class StateStore:
    def __init__(self, path: Path, *, readonly: bool = False) -> None:
        self.path = path
        self.readonly = readonly
        # Always ensure the parent directory exists — SQLite can't create the
        # database file if the directory is missing, even in read-only mode.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        use_readonly_uri = readonly and path.exists()
        db_target = f"file:{path}?mode=ro&immutable=1" if use_readonly_uri else str(path)
        self._conn = sqlite3.connect(db_target, check_same_thread=False, uri=use_readonly_uri)
        with self._lock:
            self.execute("PRAGMA busy_timeout=30000")
            if not use_readonly_uri:
                # Apply schema for new or writable databases — even in
                # "readonly" mode we may have just created an empty DB
                # (when the file didn't exist before connect).
                self.execute("PRAGMA journal_mode=WAL")
                # auto_vacuum=INCREMENTAL lets us reclaim freelist space on
                # demand via ``PRAGMA incremental_vacuum``. This pragma must
                # run BEFORE any tables are created to take effect on a
                # fresh DB — on existing DBs it's a no-op and the one-shot
                # VACUUM below (gated on the current mode) actually flips
                # the page format.
                self.execute("PRAGMA auto_vacuum=INCREMENTAL")
                try:
                    self._conn.executescript(SCHEMA)
                except sqlite3.IntegrityError:
                    # Duplicates exist that conflict with a UNIQUE index.
                    # Deduplicate and retry.
                    self._deduplicate_alerts()
                    self._conn.executescript(SCHEMA)
                try:
                    self._migrate()
                except Exception:
                    self._conn.rollback()
                    raise
                self.commit()
                # One-shot migration to flip existing NONE-mode DBs into
                # INCREMENTAL mode. Must run OUTSIDE any transaction —
                # VACUUM cannot run mid-tx, so we gate it behind a pragma
                # read and skip if already in the right mode. New DBs hit
                # this path with auto_vacuum already set, so the VACUUM is
                # skipped for them (the initial pragma above sufficed).
                self._ensure_incremental_auto_vacuum()

    def _ensure_incremental_auto_vacuum(self) -> None:
        """Flip pre-existing DBs into ``auto_vacuum=INCREMENTAL`` mode.

        SQLite's ``auto_vacuum`` mode is baked into the page format, so the
        only way to change it on an existing DB is to set the pragma and
        then run a full ``VACUUM``. New DBs already pick up the setting
        from the pragma issued before schema creation, so the VACUUM below
        is skipped for them.

        Must run outside any open transaction — callers are responsible
        for committing first. ``PRAGMA auto_vacuum`` returns:
            0 = NONE, 1 = FULL, 2 = INCREMENTAL.
        """
        try:
            row = self._conn.execute("PRAGMA auto_vacuum").fetchone()
        except sqlite3.DatabaseError:
            return
        mode = int(row[0]) if row else 0
        if mode == 2:
            return
        # Fall back to a one-shot VACUUM to rewrite the page format. Use
        # the raw connection so we bypass Python's implicit-tx layer; the
        # connect path uses the default isolation_level ("") which
        # auto-begins transactions on DML. For VACUUM we temporarily flip
        # isolation_level to None so SQLite accepts the statement, then
        # restore the original so downstream DML keeps its tx semantics.
        previous = self._conn.isolation_level
        try:
            self._conn.isolation_level = None
            self._conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            self._conn.execute("VACUUM")
        except sqlite3.DatabaseError:
            # VACUUM can fail if another connection holds the DB — that's
            # OK, we'll retry on the next StateStore open. Better to log
            # and keep going than to block the cockpit boot on DB hygiene.
            import logging
            logging.getLogger(__name__).warning(
                "StateStore: auto_vacuum=INCREMENTAL migration failed; will retry on next open",
                exc_info=True,
            )
        finally:
            self._conn.isolation_level = previous

    def incremental_vacuum(self) -> int:
        """Reclaim freelist pages via ``PRAGMA incremental_vacuum``.

        Returns the number of bytes reclaimed (0 if no-op). Must run on
        the shared connection so it coordinates with the existing write
        lock — concurrent writers will block on the busy_timeout and pick
        up once the vacuum is done. Safe to call any time after the
        INCREMENTAL mode migration has run.
        """
        with self._lock:
            try:
                page_size_row = self._conn.execute("PRAGMA page_size").fetchone()
                freelist_before_row = self._conn.execute("PRAGMA freelist_count").fetchone()
            except sqlite3.DatabaseError:
                return 0
            page_size = int(page_size_row[0]) if page_size_row else 0
            freelist_before = int(freelist_before_row[0]) if freelist_before_row else 0
            if freelist_before == 0 or page_size == 0:
                return 0
            previous = self._conn.isolation_level
            try:
                # incremental_vacuum cannot run inside a transaction — use
                # the same isolation_level=None trick as the one-shot
                # migration above.
                self._conn.isolation_level = None
                self._conn.execute("PRAGMA incremental_vacuum")
            finally:
                self._conn.isolation_level = previous
            freelist_after_row = self._conn.execute("PRAGMA freelist_count").fetchone()
            freelist_after = int(freelist_after_row[0]) if freelist_after_row else 0
            reclaimed_pages = max(freelist_before - freelist_after, 0)
            return reclaimed_pages * page_size

    def sweep_expired_memory_entries(self) -> int:
        """Drop memory_entries whose ``ttl_at`` has elapsed.

        Only touches entries that EXPLICITLY set a TTL — ``ttl_at IS NULL``
        rows are left alone. Returns the number of rows deleted. Uses
        ``datetime('now')`` so comparisons happen in SQL space against the
        ISO-8601 strings already stored on the table.
        """
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM memory_entries "
                "WHERE ttl_at IS NOT NULL AND ttl_at < datetime('now')"
            )
            deleted = int(cursor.rowcount or 0)
            self._conn.commit()
        # Bump epoch outside the lock — matches commit()'s contract.
        try:
            from pollypm.state_epoch import bump
            bump()
        except Exception:  # noqa: BLE001
            pass
        return deleted

    def _deduplicate_alerts(self) -> None:
        """Remove duplicate alerts, keeping the most recently updated row."""
        try:
            self._conn.execute("""
                DELETE FROM messages WHERE rowid NOT IN (
                    SELECT MAX(rowid) FROM messages
                    WHERE type = 'alert' AND state = 'open'
                    GROUP BY scope, sender
                )
                AND type = 'alert' AND state = 'open'
            """)
            self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                import logging
                logging.getLogger(__name__).debug("Error closing StateStore", exc_info=True)

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Thread-safe execute."""
        with self._lock:
            return self._conn.execute(sql, params)

    def commit(self) -> None:
        """Thread-safe commit. Bumps the state epoch so subscribers know to refresh."""
        with self._lock:
            self._conn.commit()
        # Bump epoch outside the lock — commit is already durable and
        # the bump only touches a sentinel file's mtime.
        try:
            from pollypm.state_epoch import bump
            bump()
        except Exception:  # noqa: BLE001
            pass

    def _now(self) -> str:
        return datetime.now(UTC).isoformat()

    # ------------------------------------------------------------------
    # Schema migrations — append-only list.  Each entry is
    # (version, description, sql_statements).  The runner applies any
    # migration whose version is greater than the current DB version.
    # Existing migrations are idempotent (IF NOT EXISTS / column checks)
    # so they are safe to replay on databases created before versioning.
    # ------------------------------------------------------------------
    _MIGRATIONS: list[tuple[int, str, list[str]]] = [
        (1, "Rebuild alerts unique index", []),
        (2, "Add project column to sessions", [
            # Column-existence check is handled by _safe_add_column below.
        ]),
        (3, "Add snapshot_hash to heartbeats", []),
        (4, "Add cache_read_tokens to token_usage_hourly", []),
        (5, "Add indexes on events and heartbeats for heartbeat sweep performance", [
            "CREATE INDEX IF NOT EXISTS idx_heartbeats_session ON heartbeats(session_name, id DESC)",
        ]),
        (6, "Add work_jobs table for durable job queue", [
            """CREATE TABLE IF NOT EXISTS work_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                handler_name TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                attempt INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                dedupe_key TEXT,
                enqueued_at TEXT NOT NULL,
                run_after TEXT NOT NULL,
                claimed_at TEXT,
                claimed_by TEXT,
                finished_at TEXT,
                last_error TEXT
            )""",
            """CREATE INDEX IF NOT EXISTS idx_work_jobs_claim
               ON work_jobs(status, run_after, id)""",
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_work_jobs_dedupe_queued
               ON work_jobs(dedupe_key)
               WHERE dedupe_key IS NOT NULL AND status IN ('queued', 'claimed')""",
        ]),
        # --- Migration 7 -----------------------------------------------
        # Inbox subsystem retired (iv04 / issue #191).
        # Left as a no-op marker so the migration number sequence
        # stays stable; the actual drop happens in migration 13.
        (7, "Deprecate legacy inbox_messages table (iv04)", []),
        (8, "Typed memory schema (type/importance/superseded_by/ttl_at)", [
            # Pre-statements left empty — column additions happen in the
            # dispatch block below (feature-detected via _safe_add_column),
            # and the index is created afterwards, once the column exists.
        ]),
        (9, "FTS5 index for memory recall (title/body/tags)", [
            # Table + triggers are declared in SCHEMA with IF NOT EXISTS,
            # so they already exist on fresh databases by the time this
            # migration runs. On upgraded databases we just need to
            # back-fill the FTS rows for entries that existed before the
            # triggers landed (dispatch block below handles that).
        ]),
        # --- Migration 10 ----------------------------------------------
        # Tiered scope model (#232 / M03). Adds ``scope_tier`` to
        # memory_entries — one of session/task/project/user. Pre-M03
        # rows back-fill to ``'project'`` via the column DEFAULT so
        # existing behaviour is preserved. The index supports the common
        # "recall across tier X" and "purge tier=session scope=S" paths.
        (10, "Tiered scope model (scope_tier column + index)", [
            # Column addition handled in the dispatch block below via
            # _safe_add_column (idempotent on fresh DBs that already have
            # it from SCHEMA). The index is created afterwards.
        ]),
        # --- Migration 11 ----------------------------------------------
        # Task-assignment notification dedupe table (#244). Records
        # every ping sent to a session about a given task so the notify
        # handler can throttle re-sends to once per 30 minutes and the
        # ``pm task pickup-log`` CLI can surface delivery history.
        (11, "Task-assignment notifications dedupe table (#244)", []),
        # --- Migration 12 ----------------------------------------------
        # Reject-bounce retry fix (#279). The dedupe for task-assignment
        # pings was keyed on ``(session_name, task_id)`` only, which
        # meant a rejected worker couldn't be re-pinged when the task
        # bounced back to ``implement`` — the 30-minute dedupe window
        # kept the retry ping suppressed. We add an ``execution_version``
        # column (the ``work_node_executions.visit`` counter for the
        # task's current node at ping time) so the dedupe key becomes
        # ``(session_name, task_id, execution_version)``. A reject that
        # opens a fresh ``(node, visit=N+1)`` execution counts as a new
        # ping opportunity; identical-state pings within the window are
        # still suppressed. Existing rows default to ``0`` (column
        # DEFAULT) — the same default a freshly-built event carries when
        # the work service can't compute a visit — so pre-migration
        # dedupe semantics survive the upgrade.
        (12, "Dedupe includes execution_version (#279)", [
            # Column addition + index creation handled via the dispatch
            # block below (_safe_add_column + explicit CREATE INDEX) so
            # the column is guaranteed to exist before the index touches
            # it, and a fresh DB re-running the migration is idempotent.
        ]),
        # --- Migration 13 ----------------------------------------------
        # Actually drop the ``inbox_messages`` table that migration 7
        # deprecated but left in place. No live callers remain after
        # #342, the table is empty on every DB in the wild, and
        # shipped release notes don't promise an offline rollback.
        # ``DROP … IF EXISTS`` is idempotent so re-running is safe.
        (13, "Drop legacy inbox_messages table (promised follow-up to migration 7)", [
            "DROP INDEX IF EXISTS idx_inbox_status",
            "DROP INDEX IF EXISTS idx_inbox_owner",
            "DROP TABLE IF EXISTS inbox_messages",
        ]),
        (14, "Structured account_usage fields for sampler snapshots", [
            # Column additions handled in the dispatch block below.
        ]),
        # --- Migration 15 ----------------------------------------------
        # Move the remaining message-shaped rows into the unified
        # ``messages`` table, then retire the legacy tables outright.
        (15, "Retire legacy events / alerts / task_notifications tables", []),
    ]

    def _migrate(self) -> None:
        # Determine current version.  The schema_version table may not
        # exist on databases created before versioning was introduced —
        # CREATE TABLE IF NOT EXISTS in the SCHEMA constant handles that.
        try:
            row = self.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_version"
            ).fetchone()
            current = row[0] if row else 0
        except Exception:  # noqa: BLE001
            current = 0

        for version, description, stmts in self._MIGRATIONS:
            if version <= current:
                continue
            for sql in stmts:
                self.execute(sql)
            # Migrations 2-4 are column additions — run them via helper
            if version == 2:
                self._safe_add_column("sessions", "project", "TEXT NOT NULL DEFAULT 'pollypm'")
            elif version == 3:
                self._safe_add_column("heartbeats", "snapshot_hash", "TEXT NOT NULL DEFAULT ''")
            elif version == 4:
                self._safe_add_column("token_usage_hourly", "cache_read_tokens", "INTEGER NOT NULL DEFAULT 0")
            elif version == 8:
                # Back-fill typed-schema columns on pre-existing memory_entries
                # rows. Each _safe_add_column call is a no-op if the column
                # already exists (fresh DBs already have them from SCHEMA).
                # Existing rows get type='project', importance=3 via column
                # DEFAULTs — the spec's migration contract (§3.2, #230).
                self._safe_add_column("memory_entries", "type", "TEXT NOT NULL DEFAULT 'project'")
                self._safe_add_column("memory_entries", "importance", "INTEGER NOT NULL DEFAULT 3")
                self._safe_add_column("memory_entries", "superseded_by", "INTEGER")
                self._safe_add_column("memory_entries", "ttl_at", "TEXT")
                # Index created last, once the column is guaranteed to exist.
                self.execute(
                    "CREATE INDEX IF NOT EXISTS idx_memory_entries_type "
                    "ON memory_entries(type, id DESC)"
                )
            elif version == 9:
                # Back-fill the FTS5 index for rows that existed before the
                # after_insert trigger was installed. Use the 'rebuild'
                # command form so SQLite repopulates the index from the
                # content table atomically — cheaper and safer than a
                # row-by-row insert loop. This is a no-op on a fresh DB
                # (the table is empty) and idempotent otherwise.
                self.execute(
                    "INSERT INTO memory_entries_fts(memory_entries_fts) VALUES('rebuild')"
                )
            elif version == 10:
                # Tiered scope model. Add ``scope_tier`` as NOT NULL with
                # DEFAULT 'project' so any pre-M03 row is treated as
                # project-tier memory — matching the acceptance contract
                # that existing entries survive the upgrade with the
                # "never auto-expire" lifecycle. Fresh DBs already have
                # the column from SCHEMA; _safe_add_column is a no-op.
                self._safe_add_column(
                    "memory_entries",
                    "scope_tier",
                    "TEXT NOT NULL DEFAULT 'project'",
                )
                # Composite (tier, scope) index supports the two hot
                # paths introduced by M03: "recall across tier X" and
                # "purge session-tier entries with scope=S".
                self.execute(
                    "CREATE INDEX IF NOT EXISTS idx_memory_entries_tier "
                    "ON memory_entries(scope_tier, scope, id DESC)"
                )
            elif version == 12:
                # Reject-bounce retry fix (#279). The dedupe key now
                # includes the current node's execution version (the
                # ``work_node_executions.visit`` counter at ping time).
                # Existing rows back-fill to ``0`` via the column
                # DEFAULT, which matches the default version emitted by
                # freshly-rebuilt events whose work service can't
                # compute a visit — so pre-migration dedupe semantics
                # survive the upgrade intact.
                if self.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='task_notifications'"
                ).fetchone():
                    self._safe_add_column(
                        "task_notifications",
                        "execution_version",
                        "INTEGER NOT NULL DEFAULT 0",
                    )
                    # Composite index supports the new dedupe query path —
                    # ``WHERE session = ? AND task = ? AND version = ? AND
                    # notified_at >= ?``. The original per-session-task
                    # index (migration 11) stays in place for pickup-log
                    # filters that don't care about version.
                    self.execute(
                        "CREATE INDEX IF NOT EXISTS "
                        "idx_task_notifications_session_task_version "
                        "ON task_notifications("
                        "session_name, task_id, execution_version, "
                        "notified_at DESC)"
                    )
            elif version == 14:
                self._safe_add_column("account_usage", "used_pct", "INTEGER")
                self._safe_add_column("account_usage", "remaining_pct", "INTEGER")
                self._safe_add_column("account_usage", "reset_at", "TEXT")
                self._safe_add_column("account_usage", "period_label", "TEXT")
            elif version == 15:
                if self.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='events'"
                ).fetchone():
                    rows = self.execute(
                        "SELECT session_name, event_type, message, created_at FROM events ORDER BY id"
                    ).fetchall()
                    for session_name, event_type, message, created_at in rows:
                        self.execute(
                            """
                            INSERT INTO messages (
                                scope, type, tier, recipient, sender, state,
                                subject, body, payload_json, labels,
                                created_at, updated_at
                            )
                            VALUES (?, 'event', 'immediate', '*', ?, 'open', ?, ?, ?, '[]', ?, ?)
                            """,
                            (
                                session_name,
                                session_name,
                                event_type,
                                message,
                                json.dumps(
                                    {
                                        "session_name": session_name,
                                        "event_type": event_type,
                                        "message": message,
                                    }
                                ),
                                created_at,
                                created_at,
                            ),
                        )
                if self.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='alerts'"
                ).fetchone():
                    rows = self.execute(
                        "SELECT session_name, alert_type, severity, message, status, created_at, updated_at FROM alerts ORDER BY id"
                    ).fetchall()
                    for session_name, alert_type, severity, message, status, created_at, updated_at in rows:
                        state = "open" if status == "open" else "closed"
                        closed_at = None if state == "open" else updated_at
                        self.execute(
                            """
                            INSERT INTO messages (
                                scope, type, tier, recipient, sender, state,
                                subject, body, payload_json, labels,
                                created_at, updated_at, closed_at
                            )
                            VALUES (?, 'alert', 'immediate', 'user', ?, ?, ?, '', ?, '[]', ?, ?, ?)
                            """,
                            (
                                session_name,
                                alert_type,
                                state,
                                f"[Alert] {message}",
                                json.dumps(
                                    {
                                        "severity": severity,
                                        "session_name": session_name,
                                    }
                                ),
                                created_at,
                                updated_at,
                                closed_at,
                            ),
                        )
                if self.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='task_notifications'"
                ).fetchone():
                    cols = {
                        row[1]
                        for row in self.execute("PRAGMA table_info(task_notifications)").fetchall()
                    }
                    execution_sql = (
                        "COALESCE(execution_version, 0)"
                        if "execution_version" in cols
                        else "0"
                    )
                    rows = self.execute(
                        f"SELECT session_name, task_id, project, notified_at, "
                        f"delivery_status, message, {execution_sql} "
                        f"FROM task_notifications ORDER BY id"
                    ).fetchall()
                    for session_name, task_id, project, notified_at, delivery_status, message, execution_version in rows:
                        self.execute(
                            """
                            INSERT INTO messages (
                                scope, type, tier, recipient, sender, state,
                                subject, body, payload_json, labels,
                                created_at, updated_at
                            )
                            VALUES (?, 'task_notification', 'immediate', ?, ?, 'open', ?, ?, ?, '[]', ?, ?)
                            """,
                            (
                                session_name,
                                project,
                                task_id,
                                task_id,
                                message,
                                json.dumps(
                                    {
                                        "project": project,
                                        "delivery_status": delivery_status,
                                        "execution_version": int(execution_version or 0),
                                    }
                                ),
                                notified_at,
                                notified_at,
                            ),
                        )
                self.execute("DROP INDEX IF EXISTS idx_task_notifications_session_task_version")
                self.execute("DROP INDEX IF EXISTS idx_task_notifications_recent")
                self.execute("DROP INDEX IF EXISTS idx_task_notifications_session_task")
                self.execute("DROP INDEX IF EXISTS idx_alerts_open")
                self.execute("DROP INDEX IF EXISTS idx_events_session_type")
                self.execute("DROP TABLE IF EXISTS task_notifications")
                self.execute("DROP TABLE IF EXISTS alerts")
                self.execute("DROP TABLE IF EXISTS events")
            self.execute(
                "INSERT INTO schema_version (version, description, applied_at) VALUES (?, ?, ?)",
                (version, description, datetime.now(UTC).isoformat()),
            )

    @staticmethod
    def _column_exists(cursor, table: str, column: str) -> bool:
        cols = {row[1] for row in cursor.execute(f"PRAGMA table_info({table})").fetchall()}
        return column in cols

    def _safe_add_column(self, table: str, column: str, typedef: str) -> None:
        if not self._column_exists(self._conn, table, column):
            self.execute(f"ALTER TABLE {table} ADD COLUMN {column} {typedef}")

    def upsert_session(
        self,
        *,
        name: str,
        role: str,
        project: str,
        provider: str,
        account: str,
        cwd: str,
        window_name: str,
    ) -> None:
        self.execute(
            """
            INSERT INTO sessions (name, role, project, provider, account, cwd, window_name)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                role = excluded.role,
                project = excluded.project,
                provider = excluded.provider,
                account = excluded.account,
                cwd = excluded.cwd,
                window_name = excluded.window_name
            """,
            (name, role, project, provider, account, cwd, window_name),
        )
        self.commit()

    def list_sessions(self) -> list[SessionRecord]:
        rows = self.execute(
            "SELECT name, role, project, provider, account, cwd, window_name FROM sessions"
        ).fetchall()
        return [
            SessionRecord(
                name=r[0], role=r[1], project=r[2], provider=r[3],
                account=r[4], cwd=r[5], window_name=r[6],
            )
            for r in rows
        ]

    def prune_sessions(self, valid_session_names: set[str]) -> None:
        now = self._now()
        if valid_session_names:
            placeholders = ", ".join("?" for _ in valid_session_names)
            params = (*sorted(valid_session_names),)
            self.execute(
                f"DELETE FROM sessions WHERE name NOT IN ({placeholders})",
                params,
            )
            self.execute(
                f"DELETE FROM leases WHERE session_name NOT IN ({placeholders})",
                params,
            )
            self.execute(
                f"""
                UPDATE messages
                SET state = 'closed', closed_at = ?, updated_at = ?
                WHERE type = 'alert'
                  AND state = 'open'
                  AND scope NOT IN ({placeholders})
                """,
                (now, now, *sorted(valid_session_names)),
            )
            # M03 (#232): session-tier memory entries auto-purge when
            # their session ends. The supervisor's session-reconciliation
            # path calls ``prune_sessions`` whenever it rebuilds the set
            # of live sessions, so piggy-backing here gives us the
            # "session ended → session memory gone" contract without
            # needing a new observer. Only session-tier rows are touched;
            # project/user/task memory is untouched.
            self.execute(
                f"""
                DELETE FROM memory_entries
                WHERE scope_tier = 'session'
                  AND scope NOT IN ({placeholders})
                """,
                params,
            )
        else:
            self.execute("DELETE FROM sessions")
            self.execute("DELETE FROM leases")
            self.execute(
                """
                UPDATE messages
                SET state = 'closed', closed_at = ?, updated_at = ?
                WHERE type = 'alert' AND state = 'open'
                """,
                (now, now),
            )
            # No live sessions ⇒ all session-tier memory is orphaned.
            self.execute(
                "DELETE FROM memory_entries WHERE scope_tier = 'session'"
            )
        self.commit()

    def get_session_window(self, session_name: str) -> str | None:
        row = self.execute(
            "SELECT window_name FROM sessions WHERE name = ?",
            (session_name,),
        ).fetchone()
        return None if row is None else str(row[0])

    def record_event(self, session_name: str, event_type: str, message: str) -> None:
        self.execute(
            """
            INSERT INTO messages (
                scope, type, tier, recipient, sender, state,
                subject, body, payload_json, labels, created_at, updated_at
            )
            VALUES (?, 'event', 'immediate', '*', ?, 'open', ?, ?, ?, '[]', ?, ?)
            """,
            (
                session_name,
                session_name,
                event_type,
                message,
                json.dumps(
                    {
                        "session_name": session_name,
                        "event_type": event_type,
                        "message": message,
                    }
                ),
                self._now(),
                self._now(),
            ),
        )
        self.commit()

    def last_event_at(self, session_name: str, event_type: str) -> str | None:
        """Return the ISO timestamp of the most recent event of this type, or None."""
        row = self.execute(
            "SELECT created_at FROM messages "
            "WHERE type = 'event' AND scope = ? AND subject = ? "
            "ORDER BY id DESC LIMIT 1",
            (session_name, event_type),
        ).fetchone()
        return row[0] if row else None

    def last_heartbeat_at(self) -> str | None:
        """Return the ISO timestamp of the most recent heartbeat sweep, or None.

        Reads from the unified ``messages`` table where heartbeat
        events have landed since #349. Migration 15 copies any
        surviving legacy rows into ``messages`` before dropping the
        physical ``events`` table, so this path must stay table-agnostic.
        """
        row = self.execute(
            "SELECT created_at FROM messages "
            "WHERE type = 'event' AND scope = 'heartbeat' AND subject = 'heartbeat' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None

    def recent_events(self, limit: int = 20) -> list[EventRecord]:
        rows = self.execute(
            """
            SELECT
                scope AS session_name,
                COALESCE(json_extract(payload_json, '$.event_type'), subject) AS event_type,
                CASE
                    WHEN body != '' THEN body
                    WHEN json_extract(payload_json, '$.message') IS NOT NULL
                        THEN json_extract(payload_json, '$.message')
                    WHEN payload_json != '{}' THEN payload_json
                    ELSE subject
                END AS message,
                created_at
            FROM messages
            WHERE type = 'event'
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [EventRecord(*row) for row in rows]

    def prune_old_data(self, *, event_days: int = 7, heartbeat_hours: int = 24) -> dict[str, int]:
        """Remove old events and heartbeat observations. Returns counts of pruned rows."""
        from datetime import timedelta
        now = datetime.now(UTC)
        event_cutoff = (now - timedelta(days=event_days)).isoformat()
        heartbeat_cutoff = (now - timedelta(hours=heartbeat_hours)).isoformat()
        with self._lock:
            e_cursor = self._conn.execute(
                "DELETE FROM messages "
                "WHERE type = 'event' AND created_at < ? "
                "AND COALESCE(json_extract(payload_json, '$.pinned'), 0) != 1",
                (event_cutoff,),
            )
            events_pruned = e_cursor.rowcount
            h_cursor = self._conn.execute("DELETE FROM heartbeats WHERE created_at < ?", (heartbeat_cutoff,))
            heartbeats_pruned = h_cursor.rowcount
            # WAL checkpoint to reclaim disk space
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.commit()
        return {"events": events_pruned, "heartbeats": heartbeats_pruned}

    def record_heartbeat(
        self,
        *,
        session_name: str,
        tmux_window: str,
        pane_id: str,
        pane_command: str,
        pane_dead: bool,
        log_bytes: int,
        snapshot_path: str,
        snapshot_hash: str,
    ) -> None:
        self.execute(
            """
            INSERT INTO heartbeats (
                session_name, tmux_window, pane_id, pane_command, pane_dead, log_bytes, snapshot_path, snapshot_hash, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_name,
                tmux_window,
                pane_id,
                pane_command,
                1 if pane_dead else 0,
                log_bytes,
                snapshot_path,
                snapshot_hash,
                self._now(),
            ),
        )
        self.commit()

    def latest_heartbeat(self, session_name: str) -> HeartbeatRecord | None:
        row = self.execute(
            """
            SELECT session_name, tmux_window, pane_id, pane_command, pane_dead, log_bytes, snapshot_path, snapshot_hash, created_at
            FROM heartbeats
            WHERE session_name = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (session_name,),
        ).fetchone()
        if row is None:
            return None
        session, tmux_window, pane_id, pane_command, pane_dead, log_bytes, snapshot_path, snapshot_hash, created_at = row
        return HeartbeatRecord(
            session_name=session,
            tmux_window=tmux_window,
            pane_id=pane_id,
            pane_command=pane_command,
            pane_dead=bool(pane_dead),
            log_bytes=int(log_bytes),
            snapshot_path=snapshot_path,
            snapshot_hash=snapshot_hash,
            created_at=created_at,
        )

    def recent_heartbeats(self, session_name: str, limit: int = 3) -> list[HeartbeatRecord]:
        rows = self.execute(
            """
            SELECT session_name, tmux_window, pane_id, pane_command, pane_dead, log_bytes, snapshot_path, snapshot_hash, created_at
            FROM heartbeats
            WHERE session_name = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (session_name, limit),
        ).fetchall()
        return [
            HeartbeatRecord(
                session_name=row[0],
                tmux_window=row[1],
                pane_id=row[2],
                pane_command=row[3],
                pane_dead=bool(row[4]),
                log_bytes=int(row[5]),
                snapshot_path=row[6],
                snapshot_hash=row[7],
                created_at=row[8],
            )
            for row in rows
        ]

    def upsert_alert(self, session_name: str, alert_type: str, severity: str, message: str) -> None:
        now = self._now()
        # Use INSERT OR IGNORE + UPDATE to avoid check-then-act race
        with self._lock:
            existing = self._conn.execute(
                """
                SELECT id
                FROM messages
                WHERE type = 'alert'
                  AND scope = ?
                  AND sender = ?
                  AND state = 'open'
                """,
                (session_name, alert_type),
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    """
                    INSERT INTO messages (
                        scope, type, tier, recipient, sender, state,
                        subject, body, payload_json, labels, created_at, updated_at
                    )
                    VALUES (?, 'alert', 'immediate', 'user', ?, 'open', ?, '', ?, '[]', ?, ?)
                    """,
                    (
                        session_name,
                        alert_type,
                        f"[Alert] {message}",
                        json.dumps(
                            {
                                "severity": severity,
                                "session_name": session_name,
                            }
                        ),
                        now,
                        now,
                    ),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE messages
                    SET subject = ?, payload_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        f"[Alert] {message}",
                        json.dumps(
                            {
                                "severity": severity,
                                "session_name": session_name,
                            }
                        ),
                        now,
                        existing[0],
                    ),
                )
            self._conn.commit()
            try:
                from pollypm.state_epoch import bump
                bump()
            except Exception:  # noqa: BLE001
                pass

    def clear_alert(self, session_name: str, alert_type: str) -> None:
        self.execute(
            """
            UPDATE messages
            SET state = 'closed', closed_at = ?, updated_at = ?
            WHERE type = 'alert'
              AND scope = ?
              AND sender = ?
              AND state = 'open'
            """,
            (self._now(), self._now(), session_name, alert_type),
        )
        self.commit()

    def open_alerts(self) -> list[AlertRecord]:
        rows = self.execute(
            """
            SELECT id, scope, sender, payload_json, subject, state, created_at, updated_at
            FROM messages
            WHERE type = 'alert' AND state = 'open'
            ORDER BY updated_at DESC
            """
        ).fetchall()
        out: list[AlertRecord] = []
        for row in rows:
            payload = _safe_payload(row[3])
            out.append(
                AlertRecord(
                    session_name=row[1],
                    alert_type=row[2],
                    severity=str(payload.get("severity") or ""),
                    message=_strip_alert_subject(str(row[4] or "")),
                    status=row[5],
                    created_at=row[6],
                    updated_at=row[7],
                    alert_id=int(row[0]),
                )
            )
        return out

    def get_alert(self, alert_id: int) -> AlertRecord | None:
        row = self.execute(
            """
            SELECT id, scope, sender, payload_json, subject, state, created_at, updated_at
            FROM messages
            WHERE id = ?
            """,
            (alert_id,),
        ).fetchone()
        if row is None:
            return None
        payload = _safe_payload(row[3])
        return AlertRecord(
            session_name=row[1],
            alert_type=row[2],
            severity=str(payload.get("severity") or ""),
            message=_strip_alert_subject(str(row[4] or "")),
            status=row[5],
            created_at=row[6],
            updated_at=row[7],
            alert_id=int(row[0]),
        )

    def clear_alert_by_id(self, alert_id: int) -> AlertRecord | None:
        alert = self.get_alert(alert_id)
        if alert is None:
            return None
        self.execute(
            """
            UPDATE messages
            SET state = 'closed', closed_at = ?, updated_at = ?
            WHERE id = ? AND state = 'open'
            """,
            (self._now(), self._now(), alert_id),
        )
        self.commit()
        return self.get_alert(alert_id)

    # ------------------------------------------------------------------
    # Task-assignment notification dedupe (#244)
    # ------------------------------------------------------------------

    def record_notification(
        self,
        *,
        session_name: str,
        task_id: str,
        project: str = "",
        message: str = "",
        delivery_status: str = "sent",
        execution_version: int = 0,
    ) -> None:
        """Record a task-assignment ping sent to ``session_name``.

        The caller looks up ``was_notified_within`` beforehand to enforce
        the 30-minute throttle; this method just appends a row.

        ``execution_version`` (#279) captures the ``visit`` counter of
        the task's current node execution at ping time. A rejection that
        bounces the task back to an earlier node spawns a fresh
        execution with a higher visit — which the dedupe query below
        correctly treats as a new ping opportunity instead of a
        duplicate within the 30-minute window.
        """
        self.execute(
            """
            INSERT INTO messages (
                scope, type, tier, recipient, sender, state,
                subject, body, payload_json, labels, created_at, updated_at
            )
            VALUES (?, 'task_notification', 'immediate', ?, ?, 'open', ?, ?, ?, '[]', ?, ?)
            """,
            (
                session_name,
                project,
                task_id,
                task_id,
                message,
                json.dumps(
                    {
                        "project": project,
                        "delivery_status": delivery_status,
                        "execution_version": int(execution_version),
                    }
                ),
                self._now(),
                self._now(),
            ),
        )
        self.commit()

    def claim_notification_slot(
        self,
        *,
        session_name: str,
        task_id: str,
        window_seconds: int,
        execution_version: int = 0,
        project: str = "",
        message: str = "",
    ) -> int | None:
        """Atomically claim a dedupe slot for a ``(session, task, version)`` ping.

        Closes the TOCTOU between :meth:`was_notified_within` and
        :meth:`record_notification` (#952). The bug: the sweeper used to
        check ``was_notified_within`` (read), then call ``session.send``,
        then ``record_notification`` (write). Concurrent sweep ticks
        could all see "not yet sent" and each fire a duplicate ping
        before any of them committed the row.

        This method does the check + insert under the connection lock in
        one atomic step:

        * If a row already exists within ``window_seconds`` for the
          ``(session, task, execution_version)`` tuple, returns ``None``
          (caller should treat as ``deduped``).
        * Otherwise inserts a placeholder row with
          ``delivery_status="pending"`` and returns its rowid. The
          caller then attempts the send and updates the row's
          ``delivery_status`` via :meth:`update_notification_status`.

        Concurrent callers are serialised by the StateStore connection
        lock, so the second caller's check sees the first caller's
        pending row and returns ``None``. The pending row counts as a
        "sent" entry for dedupe purposes — if the send ultimately fails
        the next sweep tick (5 min later) will re-fire as it would have
        with the legacy sent-row.
        """
        from datetime import timedelta

        with self._lock:
            cutoff = (
                datetime.now(UTC) - timedelta(seconds=window_seconds)
            ).isoformat()
            existing = self._conn.execute(
                """
                SELECT 1 FROM messages
                WHERE type = 'task_notification'
                  AND scope = ?
                  AND sender = ?
                  AND COALESCE(json_extract(payload_json, '$.execution_version'), 0) = ?
                  AND created_at >= ?
                LIMIT 1
                """,
                (
                    session_name,
                    task_id,
                    int(execution_version),
                    cutoff,
                ),
            ).fetchone()
            if existing is not None:
                return None
            now = datetime.now(UTC).isoformat()
            cursor = self._conn.execute(
                """
                INSERT INTO messages (
                    scope, type, tier, recipient, sender, state,
                    subject, body, payload_json, labels, created_at, updated_at
                )
                VALUES (?, 'task_notification', 'immediate', ?, ?, 'open', ?, ?, ?, '[]', ?, ?)
                """,
                (
                    session_name,
                    project,
                    task_id,
                    task_id,
                    message,
                    json.dumps(
                        {
                            "project": project,
                            "delivery_status": "pending",
                            "execution_version": int(execution_version),
                        }
                    ),
                    now,
                    now,
                ),
            )
            self._conn.commit()
            rowid = int(cursor.lastrowid or 0)
        # Bump epoch outside the lock — same pattern as :meth:`commit`.
        try:
            from pollypm.state_epoch import bump
            bump()
        except Exception:  # noqa: BLE001
            pass
        return rowid or None

    def update_notification_status(
        self,
        notification_id: int,
        *,
        delivery_status: str,
        message: str | None = None,
    ) -> None:
        """Update the delivery status / body of a previously-claimed slot.

        Companion to :meth:`claim_notification_slot` — once the send has
        been attempted, the caller stamps the resulting status (``sent``
        or ``failed: …``) on the placeholder row. If ``message`` is
        provided, the body is also updated to the canonical payload.
        """
        if not notification_id:
            return
        # Patch the JSON status in place. ``json_set`` keeps the rest of
        # the payload (project, execution_version) untouched.
        if message is None:
            self.execute(
                """
                UPDATE messages
                SET payload_json = json_set(
                        payload_json,
                        '$.delivery_status', ?
                    ),
                    updated_at = ?
                WHERE id = ? AND type = 'task_notification'
                """,
                (delivery_status, self._now(), int(notification_id)),
            )
        else:
            self.execute(
                """
                UPDATE messages
                SET payload_json = json_set(
                        payload_json,
                        '$.delivery_status', ?
                    ),
                    body = ?,
                    updated_at = ?
                WHERE id = ? AND type = 'task_notification'
                """,
                (delivery_status, message, self._now(), int(notification_id)),
            )
        self.commit()

    def was_notified_within(
        self,
        session_name: str,
        task_id: str,
        window_seconds: int,
        execution_version: int = 0,
    ) -> bool:
        """Return ``True`` if ``(session, task, version)`` was pinged inside the window.

        ``window_seconds`` is the dedupe horizon — 30 min (``1800``) for
        the primary throttle, 5 min (``300``) for the sweeper's
        re-enqueue-avoidance cursor.

        ``execution_version`` (#279) is matched exactly. A reject-bounce
        that advances the task's current node execution to a fresh
        ``visit`` yields a different version, so the dedupe returns
        ``False`` and the retry ping gets through. Pre-#279 notification
        rows back-fill to ``0`` via the column DEFAULT; events built
        when the work service can't compute a visit also emit ``0``, so
        identical-state pings still dedupe correctly across the
        migration boundary.
        """
        from datetime import timedelta
        cutoff = (datetime.now(UTC) - timedelta(seconds=window_seconds)).isoformat()
        row = self.execute(
            """
            SELECT 1 FROM messages
            WHERE type = 'task_notification'
              AND scope = ?
              AND sender = ?
              AND COALESCE(json_extract(payload_json, '$.execution_version'), 0) = ?
              AND created_at >= ?
            LIMIT 1
            """,
            (session_name, task_id, int(execution_version), cutoff),
        ).fetchone()
        return row is not None

    def recent_notifications(
        self,
        *,
        since_seconds: int | None = None,
        project: str | None = None,
        task_id: str | None = None,
        limit: int = 500,
    ) -> list[dict]:
        """Return a reverse-chronological slice of pickup notifications.

        Backs ``pm task pickup-log``. Filters compose (AND).
        """
        clauses: list[str] = []
        params: list[object] = []
        if since_seconds is not None:
            from datetime import timedelta
            cutoff = (datetime.now(UTC) - timedelta(seconds=since_seconds)).isoformat()
            clauses.append("created_at >= ?")
            params.append(cutoff)
        if project is not None:
            clauses.append("recipient = ?")
            params.append(project)
        if task_id is not None:
            clauses.append("sender = ?")
            params.append(task_id)
        where = " WHERE type = 'task_notification'"
        if clauses:
            where += " AND " + " AND ".join(clauses)
        sql = (
            "SELECT scope, sender, recipient, created_at, body, payload_json "
            "FROM messages"
            f"{where} ORDER BY created_at DESC LIMIT ?"
        )
        params.append(int(limit))
        rows = self.execute(sql, tuple(params)).fetchall()
        out: list[dict] = []
        for row in rows:
            payload = _safe_payload(row[5])
            out.append(
                {
                    "session_name": row[0],
                    "task_id": row[1],
                    "project": row[2],
                    "notified_at": row[3],
                    "delivery_status": str(payload.get("delivery_status") or ""),
                    "message": row[4],
                    "execution_version": int(payload.get("execution_version") or 0),
                }
            )
        return out

    def set_lease(self, session_name: str, owner: str, note: str = "") -> None:
        now = self._now()
        self.execute(
            """
            INSERT INTO leases (session_name, owner, note, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(session_name) DO UPDATE SET
                owner = excluded.owner,
                note = excluded.note,
                updated_at = excluded.updated_at
            """,
            (session_name, owner, note, now),
        )
        self.commit()

    def clear_lease(self, session_name: str) -> None:
        self.execute("DELETE FROM leases WHERE session_name = ?", (session_name,))
        self.commit()

    def get_lease(self, session_name: str) -> LeaseRecord | None:
        row = self.execute(
            """
            SELECT session_name, owner, note, updated_at
            FROM leases
            WHERE session_name = ?
            """,
            (session_name,),
        ).fetchone()
        if row is None:
            return None
        return LeaseRecord(*row)

    def list_leases(self) -> list[LeaseRecord]:
        rows = self.execute(
            """
            SELECT session_name, owner, note, updated_at
            FROM leases
            ORDER BY session_name
            """
        ).fetchall()
        return [LeaseRecord(*row) for row in rows]

    def upsert_account_usage(
        self,
        *,
        account_name: str,
        provider: str,
        plan: str,
        health: str,
        usage_summary: str,
        raw_text: str,
        used_pct: int | None = None,
        remaining_pct: int | None = None,
        reset_at: str | None = None,
        period_label: str | None = None,
    ) -> None:
        self.execute(
            """
            INSERT INTO account_usage (
                account_name, provider, plan, health, usage_summary, raw_text,
                used_pct, remaining_pct, reset_at, period_label, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_name) DO UPDATE SET
                provider = excluded.provider,
                plan = excluded.plan,
                health = excluded.health,
                usage_summary = excluded.usage_summary,
                raw_text = excluded.raw_text,
                used_pct = excluded.used_pct,
                remaining_pct = excluded.remaining_pct,
                reset_at = excluded.reset_at,
                period_label = excluded.period_label,
                updated_at = excluded.updated_at
            """,
            (
                account_name,
                provider,
                plan,
                health,
                usage_summary,
                raw_text,
                used_pct,
                remaining_pct,
                reset_at,
                period_label,
                self._now(),
            ),
        )
        self.commit()

    def get_account_usage(self, account_name: str) -> AccountUsageRecord | None:
        row = self.execute(
            """
            SELECT account_name, provider, plan, health, usage_summary, raw_text,
                   updated_at, used_pct, remaining_pct, reset_at, period_label
            FROM account_usage
            WHERE account_name = ?
            """,
            (account_name,),
        ).fetchone()
        if row is None:
            return None
        return AccountUsageRecord(*row)

    def upsert_account_runtime(
        self,
        *,
        account_name: str,
        provider: str,
        status: str,
        reason: str,
        available_at: str | None = None,
        access_expires_at: str | None = None,
        refresh_available: bool = False,
    ) -> None:
        self.execute(
            """
            INSERT INTO account_runtime (
                account_name, provider, status, reason, available_at, access_expires_at, refresh_available, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_name) DO UPDATE SET
                provider = excluded.provider,
                status = excluded.status,
                reason = excluded.reason,
                available_at = excluded.available_at,
                access_expires_at = excluded.access_expires_at,
                refresh_available = excluded.refresh_available,
                updated_at = excluded.updated_at
            """,
            (
                account_name,
                provider,
                status,
                reason,
                available_at,
                access_expires_at,
                1 if refresh_available else 0,
                self._now(),
            ),
        )
        self.commit()

    def get_account_runtime(self, account_name: str) -> AccountRuntimeRecord | None:
        row = self.execute(
            """
            SELECT account_name, provider, status, reason, available_at, access_expires_at, refresh_available, updated_at
            FROM account_runtime
            WHERE account_name = ?
            """,
            (account_name,),
        ).fetchone()
        if row is None:
            return None
        return AccountRuntimeRecord(
            account_name=row[0],
            provider=row[1],
            status=row[2],
            reason=row[3],
            available_at=row[4],
            access_expires_at=row[5],
            refresh_available=bool(row[6]),
            updated_at=row[7],
        )

    _UNSET = object()  # sentinel: "not provided" (keeps existing value)

    def upsert_session_runtime(
        self,
        *,
        session_name: str,
        status: str,
        effective_account: str | None | object = _UNSET,
        effective_provider: str | None | object = _UNSET,
        recovery_attempts: int | None | object = _UNSET,
        recovery_window_started_at: str | None | object = _UNSET,
        last_failure_type: str | None | object = _UNSET,
        last_failure_message: str | None | object = _UNSET,
        last_checkpoint_path: str | None | object = _UNSET,
        retry_at: str | None | object = _UNSET,
        last_recovered_at: str | None | object = _UNSET,
    ) -> None:
        _U = self._UNSET
        current = self.get_session_runtime(session_name)

        def _resolve(new, old_val, default=None):
            if new is not _U:
                return new  # explicitly provided (including None → NULL)
            return old_val if current else default

        self.execute(
            """
            INSERT INTO session_runtime (
                session_name, status, effective_account, effective_provider, recovery_attempts,
                recovery_window_started_at, last_failure_type, last_failure_message, last_checkpoint_path,
                retry_at, last_recovered_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_name) DO UPDATE SET
                status = excluded.status,
                effective_account = excluded.effective_account,
                effective_provider = excluded.effective_provider,
                recovery_attempts = excluded.recovery_attempts,
                recovery_window_started_at = excluded.recovery_window_started_at,
                last_failure_type = excluded.last_failure_type,
                last_failure_message = excluded.last_failure_message,
                last_checkpoint_path = excluded.last_checkpoint_path,
                retry_at = excluded.retry_at,
                last_recovered_at = excluded.last_recovered_at,
                updated_at = excluded.updated_at
            """,
            (
                session_name,
                status,
                _resolve(effective_account, current.effective_account if current else None),
                _resolve(effective_provider, current.effective_provider if current else None),
                _resolve(recovery_attempts, current.recovery_attempts if current else 0, default=0),
                _resolve(recovery_window_started_at, current.recovery_window_started_at if current else None),
                _resolve(last_failure_type, current.last_failure_type if current else None),
                _resolve(last_failure_message, current.last_failure_message if current else None),
                _resolve(last_checkpoint_path, current.last_checkpoint_path if current else None),
                _resolve(retry_at, current.retry_at if current else None),
                _resolve(last_recovered_at, current.last_recovered_at if current else None),
                self._now(),
            ),
        )
        self.commit()

    def get_session_runtime(self, session_name: str) -> SessionRuntimeRecord | None:
        row = self.execute(
            """
            SELECT session_name, status, effective_account, effective_provider, recovery_attempts,
                   recovery_window_started_at, last_failure_type, last_failure_message, last_checkpoint_path,
                   retry_at, last_recovered_at, updated_at
            FROM session_runtime
            WHERE session_name = ?
            """,
            (session_name,),
        ).fetchone()
        if row is None:
            return None
        return SessionRuntimeRecord(
            session_name=row[0],
            status=row[1],
            effective_account=row[2],
            effective_provider=row[3],
            recovery_attempts=int(row[4]),
            recovery_window_started_at=row[5],
            last_failure_type=row[6],
            last_failure_message=row[7],
            last_checkpoint_path=row[8],
            retry_at=row[9],
            last_recovered_at=row[10],
            updated_at=row[11],
        )

    def list_session_runtimes(self) -> list[SessionRuntimeRecord]:
        rows = self.execute(
            """
            SELECT session_name, status, effective_account, effective_provider, recovery_attempts,
                   recovery_window_started_at, last_failure_type, last_failure_message, last_checkpoint_path,
                   retry_at, last_recovered_at, updated_at
            FROM session_runtime
            """
        ).fetchall()
        return [
            SessionRuntimeRecord(
                session_name=row[0], status=row[1], effective_account=row[2],
                effective_provider=row[3], recovery_attempts=int(row[4]),
                recovery_window_started_at=row[5], last_failure_type=row[6],
                last_failure_message=row[7], last_checkpoint_path=row[8],
                retry_at=row[9], last_recovered_at=row[10], updated_at=row[11],
            )
            for row in rows
        ]

    def upsert_architect_resume_token(
        self,
        *,
        project_key: str,
        provider: str,
        session_id: str,
        last_active_at: str,
    ) -> None:
        now = self._now()
        self.execute(
            """
            INSERT INTO architect_resume_tokens (
                project_key, provider, session_id, captured_at, last_active_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(project_key) DO UPDATE SET
                provider = excluded.provider,
                session_id = excluded.session_id,
                captured_at = excluded.captured_at,
                last_active_at = excluded.last_active_at
            """,
            (project_key, provider, session_id, now, last_active_at),
        )
        self.commit()

    def get_architect_resume_token(self, project_key: str) -> ArchitectResumeRecord | None:
        row = self.execute(
            """
            SELECT project_key, provider, session_id, captured_at, last_active_at
            FROM architect_resume_tokens WHERE project_key = ?
            """,
            (project_key,),
        ).fetchone()
        if row is None:
            return None
        return ArchitectResumeRecord(
            project_key=row[0], provider=row[1], session_id=row[2],
            captured_at=row[3], last_active_at=row[4],
        )

    def clear_architect_resume_token(self, project_key: str) -> None:
        self.execute(
            "DELETE FROM architect_resume_tokens WHERE project_key = ?",
            (project_key,),
        )
        self.commit()

    def list_architect_resume_tokens(self) -> list[ArchitectResumeRecord]:
        rows = self.execute(
            """
            SELECT project_key, provider, session_id, captured_at, last_active_at
            FROM architect_resume_tokens
            """
        ).fetchall()
        return [
            ArchitectResumeRecord(
                project_key=row[0], provider=row[1], session_id=row[2],
                captured_at=row[3], last_active_at=row[4],
            )
            for row in rows
        ]

    def record_checkpoint(
        self,
        *,
        session_name: str,
        project_key: str,
        level: str,
        json_path: str,
        summary_path: str,
        snapshot_path: str,
        summary_text: str,
    ) -> None:
        self.execute(
            """
            INSERT INTO checkpoints (
                session_name, project_key, level, json_path, summary_path, snapshot_path, summary_text, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (session_name, project_key, level, json_path, summary_path, snapshot_path, summary_text, self._now()),
        )
        self.commit()

    def latest_checkpoint(self, session_name: str) -> CheckpointRecord | None:
        row = self.execute(
            """
            SELECT session_name, project_key, level, json_path, summary_path, snapshot_path, summary_text, created_at
            FROM checkpoints
            WHERE session_name = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (session_name,),
        ).fetchone()
        if row is None:
            return None
        return CheckpointRecord(*row)

    def upsert_worktree(
        self,
        *,
        project_key: str,
        lane_kind: str,
        lane_key: str,
        session_name: str | None,
        issue_key: str | None,
        path: str,
        branch: str,
        status: str,
    ) -> None:
        now = self._now()
        existing = self.execute(
            """
            SELECT id, created_at
            FROM worktrees
            WHERE project_key = ? AND lane_kind = ? AND lane_key = ? AND status = ?
            """,
            (project_key, lane_kind, lane_key, status),
        ).fetchone()
        if existing is None:
            self.execute(
                """
                INSERT INTO worktrees (
                    project_key, lane_kind, lane_key, session_name, issue_key, path, branch, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (project_key, lane_kind, lane_key, session_name, issue_key, path, branch, status, now, now),
            )
        else:
            self.execute(
                """
                UPDATE worktrees
                SET session_name = ?, issue_key = ?, path = ?, branch = ?, updated_at = ?
                WHERE id = ?
                """,
                (session_name, issue_key, path, branch, now, existing[0]),
            )
        self.commit()

    def get_token_sample(self, session_name: str) -> TokenSampleRecord | None:
        row = self.execute(
            """
            SELECT session_name, account_name, provider, model_name, project_key, cumulative_tokens, observed_at
            FROM token_samples
            WHERE session_name = ?
            """,
            (session_name,),
        ).fetchone()
        if row is None:
            return None
        return TokenSampleRecord(
            session_name=row[0],
            account_name=row[1],
            provider=row[2],
            model_name=row[3],
            project_key=row[4],
            cumulative_tokens=int(row[5]),
            observed_at=row[6],
        )

    def record_token_sample(
        self,
        *,
        session_name: str,
        account_name: str,
        provider: str,
        model_name: str,
        project_key: str,
        cumulative_tokens: int,
        observed_at: str | None = None,
    ) -> int:
        now = observed_at or self._now()
        previous = self.get_token_sample(session_name)
        delta = 0
        if previous is not None:
            if (
                previous.account_name == account_name
                and previous.provider == provider
                and previous.model_name == model_name
                and previous.project_key == project_key
                and cumulative_tokens >= previous.cumulative_tokens
            ):
                delta = cumulative_tokens - previous.cumulative_tokens
            else:
                delta = 0

        self.execute(
            """
            INSERT INTO token_samples (
                session_name, account_name, provider, model_name, project_key, cumulative_tokens, observed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_name) DO UPDATE SET
                account_name = excluded.account_name,
                provider = excluded.provider,
                model_name = excluded.model_name,
                project_key = excluded.project_key,
                cumulative_tokens = excluded.cumulative_tokens,
                observed_at = excluded.observed_at
            """,
            (session_name, account_name, provider, model_name, project_key, cumulative_tokens, now),
        )

        if delta > 0:
            hour_bucket = now[:13] + ":00:00+00:00"
            self.execute(
                """
                INSERT INTO token_usage_hourly (
                    hour_bucket, account_name, provider, model_name, project_key, tokens_used, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(hour_bucket, account_name, provider, model_name, project_key) DO UPDATE SET
                    tokens_used = token_usage_hourly.tokens_used + excluded.tokens_used,
                    updated_at = excluded.updated_at
                """,
                (hour_bucket, account_name, provider, model_name, project_key, delta, now),
            )
        self.commit()
        return delta

    def upsert_token_sample(
        self,
        *,
        session_name: str,
        account_name: str,
        provider: str,
        model_name: str,
        project_key: str,
        cumulative_tokens: int,
        observed_at: str,
    ) -> None:
        self.execute(
            """
            INSERT INTO token_samples (
                session_name, account_name, provider, model_name, project_key, cumulative_tokens, observed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_name) DO UPDATE SET
                account_name = excluded.account_name,
                provider = excluded.provider,
                model_name = excluded.model_name,
                project_key = excluded.project_key,
                cumulative_tokens = excluded.cumulative_tokens,
                observed_at = excluded.observed_at
            """,
            (session_name, account_name, provider, model_name, project_key, cumulative_tokens, observed_at),
        )
        self.commit()

    def replace_token_usage_hourly(
        self,
        rows: list[TokenUsageHourlyRecord],
        *,
        account_names: list[str] | None = None,
    ) -> None:
        if account_names:
            placeholders = ", ".join("?" for _ in account_names)
            self.execute(
                f"DELETE FROM token_usage_hourly WHERE account_name IN ({placeholders})",
                tuple(account_names),
            )
        else:
            self.execute("DELETE FROM token_usage_hourly")
        for row in rows:
            self.execute(
                """
                INSERT INTO token_usage_hourly (
                    hour_bucket, account_name, provider, model_name, project_key, tokens_used, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.hour_bucket,
                    row.account_name,
                    row.provider,
                    row.model_name,
                    row.project_key,
                    row.tokens_used,
                    row.updated_at,
                ),
            )
        self.commit()

    def recent_token_usage(self, limit: int = 24) -> list[TokenUsageHourlyRecord]:
        rows = self.execute(
            """
            SELECT hour_bucket, account_name, provider, model_name, project_key, tokens_used, updated_at
            FROM token_usage_hourly
            ORDER BY hour_bucket DESC, tokens_used DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            TokenUsageHourlyRecord(
                hour_bucket=row[0],
                account_name=row[1],
                provider=row[2],
                model_name=row[3],
                project_key=row[4],
                tokens_used=int(row[5]),
                updated_at=row[6],
            )
            for row in rows
        ]

    def daily_token_usage(self, days: int = 30) -> list[tuple[str, int]]:
        """Return (date_str, total_tokens) for the last N days."""
        rows = self.execute(
            """
            SELECT substr(hour_bucket, 1, 10) AS day, SUM(tokens_used)
            FROM token_usage_hourly
            GROUP BY day
            ORDER BY day DESC
            LIMIT ?
            """,
            (days,),
        ).fetchall()
        return [(row[0], int(row[1])) for row in reversed(rows)]

    def record_memory_entry(
        self,
        *,
        scope: str,
        kind: str,
        title: str,
        body: str,
        tags: list[str],
        source: str,
        file_path: str,
        summary_path: str,
        type: str = "project",
        importance: int = 3,
        superseded_by: int | None = None,
        ttl_at: str | None = None,
        scope_tier: str = "project",
    ) -> MemoryEntryRecord:
        now = self._now()
        tags_json = json.dumps([str(tag) for tag in tags], ensure_ascii=True)
        cursor = self.execute(
            """
            INSERT INTO memory_entries (
                scope, kind, title, body, tags, source, file_path, summary_path, created_at, updated_at,
                type, importance, superseded_by, ttl_at, scope_tier
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scope, kind, title, body, tags_json, source, file_path, summary_path, now, now,
                type, int(importance), superseded_by, ttl_at, scope_tier,
            ),
        )
        self.commit()
        return MemoryEntryRecord(
            entry_id=int(cursor.lastrowid),
            scope=scope,
            kind=kind,
            title=title,
            body=body,
            tags=tuple(tags),
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

    def get_memory_entry(self, entry_id: int) -> MemoryEntryRecord | None:
        row = self.execute(
            """
            SELECT id, scope, kind, title, body, tags, source, file_path, summary_path, created_at, updated_at,
                   type, importance, superseded_by, ttl_at, scope_tier
            FROM memory_entries
            WHERE id = ?
            """,
            (entry_id,),
        ).fetchone()
        if row is None:
            return None
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
            created_at=row[9],
            updated_at=row[10],
            type=row[11] if row[11] is not None else "project",
            importance=int(row[12]) if row[12] is not None else 3,
            superseded_by=int(row[13]) if row[13] is not None else None,
            ttl_at=row[14],
            scope_tier=row[15] if row[15] is not None else "project",
        )

    def list_memory_entries(
        self,
        *,
        scope: str | None = None,
        kind: str | None = None,
        type: str | None = None,
        scope_tier: str | None = None,
        limit: int = 50,
    ) -> list[MemoryEntryRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if scope is not None:
            clauses.append("scope = ?")
            params.append(scope)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if type is not None:
            clauses.append("type = ?")
            params.append(type)
        if scope_tier is not None:
            clauses.append("scope_tier = ?")
            params.append(scope_tier)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.execute(
            f"""
            SELECT id, scope, kind, title, body, tags, source, file_path, summary_path, created_at, updated_at,
                   type, importance, superseded_by, ttl_at, scope_tier
            FROM memory_entries
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        return [
            MemoryEntryRecord(
                entry_id=int(row[0]),
                scope=row[1],
                kind=row[2],
                title=row[3],
                body=row[4],
                tags=_safe_tags(row[5]),
                source=row[6],
                file_path=row[7],
                summary_path=row[8],
                created_at=row[9],
                updated_at=row[10],
                type=row[11] if row[11] is not None else "project",
                importance=int(row[12]) if row[12] is not None else 3,
                superseded_by=int(row[13]) if row[13] is not None else None,
                ttl_at=row[14],
                scope_tier=row[15] if row[15] is not None else "project",
            )
            for row in rows
        ]

    def recall_memory_entries(
        self,
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
    ) -> list[tuple[MemoryEntryRecord, float | None]]:
        """Return (record, bm25_score_or_None) pairs ranked by FTS5.

        When ``query`` is empty or whitespace-only, skips the FTS MATCH and
        returns rows filtered by the remaining predicates, ordered by id
        DESC. The caller (``FileMemoryBackend.recall``) is responsible for
        combining bm25 with importance + recency into the final score.

        ``candidate_multiplier`` widens the SQL fetch so the ranker has
        headroom to apply the importance/recency weighting without the
        top-K being clipped by SQL's bm25-only ordering. Defaults to 5×
        which is generous but bounded (still O(limit)).

        ``scope_tiers`` restricts to rows whose ``scope_tier`` is in the
        list. ``tier_scope_pairs`` matches rows whose
        ``(scope_tier, scope)`` pair is in the list — this lets callers
        compose cross-tier recall ("task=T123 AND project=foo") in one
        query without a Python-side merge (M03 / #232).
        """
        clauses: list[str] = []
        params: list[object] = []
        # Filter superseded entries out of recall by default — they remain
        # readable by id but shouldn't surface in relevance search. Matches
        # the spec's "superseded entries preserved for audit but not returned
        # by default" contract (§3.7). Callers opt back in with
        # ``include_superseded=True``.
        if not include_superseded:
            clauses.append("me.superseded_by IS NULL")
        # Filter expired entries (ttl_at in the past). ISO-8601 sorts
        # lexicographically so a string comparison works for UTC timestamps.
        now_iso = self._now()
        clauses.append("(me.ttl_at IS NULL OR me.ttl_at > ?)")
        params.append(now_iso)
        if scopes:
            placeholders = ", ".join("?" for _ in scopes)
            clauses.append(f"me.scope IN ({placeholders})")
            params.extend(scopes)
        if types:
            placeholders = ", ".join("?" for _ in types)
            clauses.append(f"me.type IN ({placeholders})")
            params.extend(types)
        if scope_tiers:
            placeholders = ", ".join("?" for _ in scope_tiers)
            clauses.append(f"me.scope_tier IN ({placeholders})")
            params.extend(scope_tiers)
        if tier_scope_pairs:
            # Match ``(scope_tier, scope)`` pairs via OR-of-equalities —
            # SQLite handles small OR sets efficiently with the
            # idx_memory_entries_tier index, and this keeps the plan
            # deterministic without needing a VALUES() clause.
            pair_clauses: list[str] = []
            for tier, scope_id in tier_scope_pairs:
                pair_clauses.append("(me.scope_tier = ? AND me.scope = ?)")
                params.extend([tier, scope_id])
            if pair_clauses:
                clauses.append("(" + " OR ".join(pair_clauses) + ")")
        if importance_min > 1:
            clauses.append("me.importance >= ?")
            params.append(int(importance_min))

        query_text = (query or "").strip()
        fetch_limit = max(limit * candidate_multiplier, limit)

        if query_text:
            # FTS5 MATCH — negative bm25 so that higher = better (matches
            # the shape of every other relevance score in the codebase and
            # lets callers ignore the "lower is better" quirk).
            where = " AND ".join(clauses)
            sql = f"""
            SELECT me.id, me.scope, me.kind, me.title, me.body, me.tags, me.source,
                   me.file_path, me.summary_path, me.created_at, me.updated_at,
                   me.type, me.importance, me.superseded_by, me.ttl_at, me.scope_tier,
                   bm25(memory_entries_fts) AS bm25_score
            FROM memory_entries_fts
            JOIN memory_entries me ON me.id = memory_entries_fts.rowid
            WHERE memory_entries_fts MATCH ?
              AND {where}
            ORDER BY bm25_score ASC
            LIMIT ?
            """
            fts_query = normalize_fts_query(query_text)
            rows = self.execute(sql, (fts_query, *params, fetch_limit)).fetchall()
        else:
            where = " AND ".join(clauses)
            sql = f"""
            SELECT me.id, me.scope, me.kind, me.title, me.body, me.tags, me.source,
                   me.file_path, me.summary_path, me.created_at, me.updated_at,
                   me.type, me.importance, me.superseded_by, me.ttl_at, me.scope_tier,
                   NULL AS bm25_score
            FROM memory_entries me
            WHERE {where}
            ORDER BY me.id DESC
            LIMIT ?
            """
            rows = self.execute(sql, (*params, fetch_limit)).fetchall()

        results: list[tuple[MemoryEntryRecord, float | None]] = []
        for row in rows:
            record = MemoryEntryRecord(
                entry_id=int(row[0]),
                scope=row[1],
                kind=row[2],
                title=row[3],
                body=row[4],
                tags=_safe_tags(row[5]),
                source=row[6],
                file_path=row[7],
                summary_path=row[8],
                created_at=row[9],
                updated_at=row[10],
                type=row[11] if row[11] is not None else "project",
                importance=int(row[12]) if row[12] is not None else 3,
                superseded_by=int(row[13]) if row[13] is not None else None,
                ttl_at=row[14],
                scope_tier=row[15] if row[15] is not None else "project",
            )
            bm25_score = float(row[16]) if row[16] is not None else None
            results.append((record, bm25_score))
        return results

    # ------------------------------------------------------------------
    # Tiered-scope lifecycle helpers (M03 / #232)
    #
    # These methods implement the per-tier lifecycle contract from
    # docs/memory-system-review.md §3.1:
    #
    #   * session  — auto-purge on session end (purge_session_scope)
    #   * task     — auto-TTL 30 days after terminal (expire_task_scope)
    #   * project  — never auto-expire
    #   * user     — never auto-expire
    #
    # They operate on the state store alone — on-disk memory files are
    # left behind by ``purge_session_scope`` (cheap "tombstones" for
    # post-mortem inspection) since the recall path filters rows out of
    # the SQLite view the moment they're deleted from memory_entries.
    # The file-backend-owned artifact directory cleanup is a later
    # enhancement; the DB is the source of truth for what ``recall``
    # sees.
    # ------------------------------------------------------------------

    def purge_session_scope(self, session_id: str) -> int:
        """Delete all session-tier memory entries with scope=``session_id``.

        Returns the number of rows removed. Idempotent — calling twice
        removes zero on the second call. FTS triggers keep the FTS
        virtual table in sync automatically (see memory_entries_fts_ad).
        """
        cursor = self.execute(
            "DELETE FROM memory_entries WHERE scope_tier = 'session' AND scope = ?",
            (session_id,),
        )
        removed = int(cursor.rowcount) if cursor.rowcount is not None else 0
        if removed:
            self.commit()
        return removed

    def expire_task_scope(
        self,
        task_id: str,
        *,
        terminal_at: str | None = None,
        ttl_days: int = 30,
    ) -> int:
        """Set a ttl_at 30 days out for task-tier entries with scope=``task_id``.

        ``terminal_at`` defaults to ``now`` in UTC; tests pass an explicit
        value. ``ttl_days`` is the lifetime after terminal transition —
        30 per the spec, exposed as an argument for tests. Entries that
        already carry a ``ttl_at`` keep the earlier of the two so a
        caller-set TTL is never extended.
        """
        from datetime import datetime as _dt, timedelta as _td, UTC as _UTC
        if terminal_at is None:
            now = _dt.now(_UTC)
        else:
            now = _dt.fromisoformat(terminal_at)
            if now.tzinfo is None:
                now = now.replace(tzinfo=_UTC)
        ttl_at = (now + _td(days=int(ttl_days))).isoformat()
        # Use MIN() so an explicit earlier ttl (e.g. an author pinned a
        # 7-day experiment note) isn't extended by the terminal-transition
        # update. ``COALESCE`` fills the null side so MIN() works when
        # ttl_at was previously NULL.
        cursor = self.execute(
            """
            UPDATE memory_entries
            SET ttl_at = MIN(COALESCE(ttl_at, ?), ?),
                updated_at = ?
            WHERE scope_tier = 'task' AND scope = ?
            """,
            (ttl_at, ttl_at, now.isoformat(), task_id),
        )
        updated = int(cursor.rowcount) if cursor.rowcount is not None else 0
        if updated:
            self.commit()
        return updated

    def delete_memory_entry(self, entry_id: int) -> bool:
        """Hard-delete a single memory row. Returns True when a row was removed.

        Used by the ``pm memory forget`` CLI and the curator's TTL
        sweep. FTS triggers keep the virtual index consistent
        automatically. The on-disk markdown file is left untouched —
        callers that care about the file removing it belongs to a
        separate concern (the file backend's ``forget`` wrapper).
        """
        cursor = self.execute(
            "DELETE FROM memory_entries WHERE id = ?",
            (int(entry_id),),
        )
        removed = int(cursor.rowcount) if cursor.rowcount is not None else 0
        if removed:
            self.commit()
        return removed > 0

    def update_memory_entry(
        self,
        entry_id: int,
        *,
        body: str | None = None,
        importance: int | None = None,
        tags: list[str] | None = None,
        superseded_by: int | None = None,
        clear_superseded: bool = False,
    ) -> bool:
        """Mutate a subset of fields on a memory row. Returns True on change.

        ``body`` / ``importance`` / ``tags`` / ``superseded_by`` are all
        optional — fields left at ``None`` keep their current values.
        Pass ``clear_superseded=True`` to explicitly null the
        ``superseded_by`` column (useful for reviewer rollback).

        ``updated_at`` is bumped whenever any field changes.
        """
        sets: list[str] = []
        params: list[object] = []
        if body is not None:
            sets.append("body = ?")
            params.append(str(body))
        if importance is not None:
            if not (1 <= int(importance) <= 5):
                raise ValueError(
                    f"importance must be between 1 and 5 (got {importance})"
                )
            sets.append("importance = ?")
            params.append(int(importance))
        if tags is not None:
            sets.append("tags = ?")
            params.append(json.dumps([str(tag) for tag in tags], ensure_ascii=True))
        if clear_superseded:
            sets.append("superseded_by = NULL")
        elif superseded_by is not None:
            sets.append("superseded_by = ?")
            params.append(int(superseded_by))
        if not sets:
            return False
        now = self._now()
        sets.append("updated_at = ?")
        params.append(now)
        params.append(int(entry_id))
        cursor = self.execute(
            f"UPDATE memory_entries SET {', '.join(sets)} WHERE id = ?",
            tuple(params),
        )
        changed = int(cursor.rowcount) if cursor.rowcount is not None else 0
        if changed:
            self.commit()
        return changed > 0

    def record_memory_summary(
        self,
        *,
        scope: str,
        summary_text: str,
        summary_path: str,
        entry_count: int,
    ) -> MemorySummaryRecord:
        now = self._now()
        cursor = self.execute(
            """
            INSERT INTO memory_summaries (scope, summary_text, summary_path, entry_count, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (scope, summary_text, summary_path, entry_count, now),
        )
        self.commit()
        return MemorySummaryRecord(
            summary_id=int(cursor.lastrowid),
            scope=scope,
            summary_text=summary_text,
            summary_path=summary_path,
            entry_count=entry_count,
            created_at=now,
        )

    def latest_memory_summary(self, scope: str) -> MemorySummaryRecord | None:
        row = self.execute(
            """
            SELECT id, scope, summary_text, summary_path, entry_count, created_at
            FROM memory_summaries
            WHERE scope = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (scope,),
        ).fetchone()
        if row is None:
            return None
        return MemorySummaryRecord(
            summary_id=int(row[0]),
            scope=row[1],
            summary_text=row[2],
            summary_path=row[3],
            entry_count=int(row[4]),
            created_at=row[5],
        )

    def update_worktree_status(self, project_key: str, lane_kind: str, lane_key: str, status: str) -> None:
        self.execute(
            """
            UPDATE worktrees
            SET status = ?, updated_at = ?
            WHERE project_key = ? AND lane_kind = ? AND lane_key = ? AND status = 'active'
            """,
            (status, self._now(), project_key, lane_kind, lane_key),
        )
        self.commit()

    def list_worktrees(self, project_key: str | None = None) -> list[WorktreeRecord]:
        if project_key is None:
            rows = self.execute(
                """
                SELECT project_key, lane_kind, lane_key, session_name, issue_key, path, branch, status, created_at, updated_at
                FROM worktrees
                ORDER BY updated_at DESC
                """
            ).fetchall()
        else:
            rows = self.execute(
                """
                SELECT project_key, lane_kind, lane_key, session_name, issue_key, path, branch, status, created_at, updated_at
                FROM worktrees
                WHERE project_key = ?
                ORDER BY updated_at DESC
                """,
                (project_key,),
            ).fetchall()
        return [WorktreeRecord(*row) for row in rows]
