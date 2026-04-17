from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


def _normalize_fts_query(query: str) -> str:
    """Convert a free-text query into an FTS5-safe MATCH expression.

    FTS5's query syntax treats a fistful of characters as operators
    (``" ( ) : . * + -``) and rejects malformed input with
    ``sqlite3.OperationalError: fts5: syntax error``. We take the belt-
    and-braces approach: split the input on anything that isn't a word
    character, drop empty fragments, wrap each surviving token in
    double quotes (FTS5 treats a quoted token as a literal phrase — no
    operators), and join with implicit-AND. Tokens shorter than two
    characters are dropped because FTS5's unicode61 tokenizer will
    silently skip them anyway, and empty queries fall back to an
    all-columns wildcard so the MATCH always matches nothing (we
    surface that to the caller by returning a sentinel which the
    caller can treat as "no results" — see note below).

    Returns a string safe to pass as the right-hand side of a
    ``col MATCH ?`` parameterized query.
    """
    # Extract alphanumeric runs; underscore is included so identifiers
    # like ``state_store`` stay whole.
    tokens = re.findall(r"[\w]+", query.lower(), flags=re.UNICODE)
    # Drop single-char tokens — FTS5 unicode61 filters them anyway and
    # keeping them just bloats the query.
    tokens = [t for t in tokens if len(t) >= 2]
    if not tokens:
        # Fall back to a query that matches nothing. FTS5 requires *some*
        # valid term, so we use an obviously-nonsense token. Callers that
        # want "all entries" should pass empty string to recall() which
        # short-circuits before reaching this helper.
        return '"__pollypm_no_match_sentinel__"'
    # OR the tokens so a multi-word query like "testing strategy" still
    # surfaces entries that match either word (bm25 ranks entries that
    # match both higher, which is what we want). Each token is quoted to
    # neutralise any operator characters that slip through (the regex
    # already strips them, but quoting is cheap insurance). The porter
    # tokenizer on the FTS table handles stemming so ``testing`` matches
    # entries containing ``tests`` or ``tested`` without any prefix-``*``
    # gymnastics.
    return " OR ".join(f'"{t}"' for t in tokens)


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

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_name TEXT NOT NULL,
    event_type TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);

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

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_name TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_open
ON alerts(session_name, alert_type)
WHERE status = 'open';

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

CREATE TABLE IF NOT EXISTS inbox_messages (
    id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    owner TEXT NOT NULL DEFAULT 'polly',
    sender TEXT NOT NULL,
    project TEXT NOT NULL DEFAULT '',
    message_count INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_inbox_status ON inbox_messages(status);
CREATE INDEX IF NOT EXISTS idx_inbox_owner ON inbox_messages(owner, status);

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
    ttl_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_memory_entries_scope
ON memory_entries(scope, id DESC);
-- idx_memory_entries_type is created by migration 8 after the ``type``
-- column is back-filled onto pre-M01 databases.

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


@dataclass(slots=True)
class MemorySummaryRecord:
    summary_id: int
    scope: str
    summary_text: str
    summary_path: str
    entry_count: int
    created_at: str


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

    def _deduplicate_alerts(self) -> None:
        """Remove duplicate alerts, keeping the most recently updated row."""
        try:
            self._conn.execute("""
                DELETE FROM alerts WHERE rowid NOT IN (
                    SELECT MAX(rowid) FROM alerts
                    GROUP BY session_name, alert_type
                )
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
        (1, "Rebuild alerts unique index", [
            "DROP INDEX IF EXISTS idx_alerts_open",
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_open
               ON alerts(session_name, alert_type) WHERE status = 'open'""",
        ]),
        (2, "Add project column to sessions", [
            # Column-existence check is handled by _safe_add_column below.
        ]),
        (3, "Add snapshot_hash to heartbeats", []),
        (4, "Add cache_read_tokens to token_usage_hourly", []),
        (5, "Add indexes on events and heartbeats for heartbeat sweep performance", [
            "CREATE INDEX IF NOT EXISTS idx_events_session_type ON events(session_name, event_type, id DESC)",
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
        # The ``inbox_messages`` table is left in place for one release
        # cycle in case a rollback is needed. Nothing writes to it after
        # this point. A follow-up release will drop the table.
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
                UPDATE alerts
                SET status = 'cleared', updated_at = ?
                WHERE status = 'open' AND session_name NOT IN ({placeholders})
                """,
                (now, *sorted(valid_session_names)),
            )
        else:
            self.execute("DELETE FROM sessions")
            self.execute("DELETE FROM leases")
            self.execute(
                """
                UPDATE alerts
                SET status = 'cleared', updated_at = ?
                WHERE status = 'open'
                """,
                (now,),
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
            INSERT INTO events (session_name, event_type, message, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (session_name, event_type, message, self._now()),
        )
        self.commit()

    def last_event_at(self, session_name: str, event_type: str) -> str | None:
        """Return the ISO timestamp of the most recent event of this type, or None."""
        row = self.execute(
            "SELECT created_at FROM events WHERE session_name = ? AND event_type = ? ORDER BY id DESC LIMIT 1",
            (session_name, event_type),
        ).fetchone()
        return row[0] if row else None

    def last_heartbeat_at(self) -> str | None:
        """Return the ISO timestamp of the most recent heartbeat sweep, or None."""
        row = self.execute(
            "SELECT created_at FROM events WHERE event_type = 'heartbeat' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None

    def recent_events(self, limit: int = 20) -> list[EventRecord]:
        rows = self.execute(
            """
            SELECT session_name, event_type, message, created_at
            FROM events
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
            e_cursor = self._conn.execute("DELETE FROM events WHERE created_at < ?", (event_cutoff,))
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
                SELECT id, message, severity
                FROM alerts
                WHERE session_name = ? AND alert_type = ? AND status = 'open'
                """,
                (session_name, alert_type),
            ).fetchone()
            if existing is None:
                try:
                    self._conn.execute(
                        """
                        INSERT INTO alerts (session_name, alert_type, severity, message, status, created_at, updated_at)
                        VALUES (?, ?, ?, ?, 'open', ?, ?)
                        """,
                        (session_name, alert_type, severity, message, now, now),
                    )
                except sqlite3.IntegrityError:
                    pass  # Another process inserted first — that's fine
            else:
                self._conn.execute(
                    """
                    UPDATE alerts
                    SET severity = ?, message = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (severity, message, now, existing[0]),
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
            UPDATE alerts
            SET status = 'cleared', updated_at = ?
            WHERE session_name = ? AND alert_type = ? AND status = 'open'
            """,
            (self._now(), session_name, alert_type),
        )
        self.commit()

    def open_alerts(self) -> list[AlertRecord]:
        rows = self.execute(
            """
            SELECT id, session_name, alert_type, severity, message, status, created_at, updated_at
            FROM alerts
            WHERE status = 'open'
            ORDER BY updated_at DESC
            """
        ).fetchall()
        return [
            AlertRecord(
                session_name=row[1],
                alert_type=row[2],
                severity=row[3],
                message=row[4],
                status=row[5],
                created_at=row[6],
                updated_at=row[7],
                alert_id=int(row[0]),
            )
            for row in rows
        ]

    def get_alert(self, alert_id: int) -> AlertRecord | None:
        row = self.execute(
            """
            SELECT id, session_name, alert_type, severity, message, status, created_at, updated_at
            FROM alerts
            WHERE id = ?
            """,
            (alert_id,),
        ).fetchone()
        if row is None:
            return None
        return AlertRecord(
            session_name=row[1],
            alert_type=row[2],
            severity=row[3],
            message=row[4],
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
            UPDATE alerts
            SET status = 'cleared', updated_at = ?
            WHERE id = ? AND status = 'open'
            """,
            (self._now(), alert_id),
        )
        self.commit()
        return self.get_alert(alert_id)

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
    ) -> None:
        self.execute(
            """
            INSERT INTO account_usage (
                account_name, provider, plan, health, usage_summary, raw_text, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_name) DO UPDATE SET
                provider = excluded.provider,
                plan = excluded.plan,
                health = excluded.health,
                usage_summary = excluded.usage_summary,
                raw_text = excluded.raw_text,
                updated_at = excluded.updated_at
            """,
            (account_name, provider, plan, health, usage_summary, raw_text, self._now()),
        )
        self.commit()

    def get_account_usage(self, account_name: str) -> AccountUsageRecord | None:
        row = self.execute(
            """
            SELECT account_name, provider, plan, health, usage_summary, raw_text, updated_at
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

    # ── Inbox messages (v2) ──

    def upsert_inbox_message(
        self,
        *,
        id: str,
        subject: str,
        status: str = "open",
        owner: str = "polly",
        sender: str = "",
        project: str = "",
        message_count: int = 1,
        created_at: str = "",
        updated_at: str = "",
    ) -> None:
        now = self._now()
        self.execute(
            """
            INSERT INTO inbox_messages (id, subject, status, owner, sender, project, message_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status = excluded.status,
                owner = excluded.owner,
                message_count = excluded.message_count,
                updated_at = excluded.updated_at,
                closed_at = CASE WHEN excluded.status = 'closed' THEN ? ELSE closed_at END
            """,
            (id, subject, status, owner, sender, project, message_count,
             created_at or now, updated_at or now, now),
        )
        self.commit()

    def list_inbox_messages(self, *, status: str = "open") -> list[dict]:
        if status == "all":
            rows = self.execute(
                "SELECT id, subject, status, owner, sender, project, message_count, created_at, updated_at FROM inbox_messages ORDER BY updated_at DESC"
            ).fetchall()
        else:
            rows = self.execute(
                "SELECT id, subject, status, owner, sender, project, message_count, created_at, updated_at FROM inbox_messages WHERE status = ? ORDER BY updated_at DESC",
                (status,),
            ).fetchall()
        return [
            {"id": r[0], "subject": r[1], "status": r[2], "owner": r[3], "sender": r[4],
             "project": r[5], "message_count": r[6], "created_at": r[7], "updated_at": r[8]}
            for r in rows
        ]

    def inbox_messages_awaiting(self, owner: str, *, max_age_seconds: int | None = None) -> list[dict]:
        """Find open messages owned by a specific actor, optionally older than N seconds."""
        rows = self.execute(
            "SELECT id, subject, status, owner, sender, project, message_count, created_at, updated_at FROM inbox_messages WHERE status = 'open' AND owner = ? ORDER BY updated_at DESC",
            (owner,),
        ).fetchall()
        results = []
        for r in rows:
            entry = {"id": r[0], "subject": r[1], "status": r[2], "owner": r[3], "sender": r[4],
                     "project": r[5], "message_count": r[6], "created_at": r[7], "updated_at": r[8]}
            if max_age_seconds is not None:
                from datetime import UTC, datetime
                age = (datetime.now(UTC) - datetime.fromisoformat(entry["updated_at"])).total_seconds()
                if age < max_age_seconds:
                    continue
            results.append(entry)
        return results

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
    ) -> MemoryEntryRecord:
        now = self._now()
        tags_json = json.dumps([str(tag) for tag in tags], ensure_ascii=True)
        cursor = self.execute(
            """
            INSERT INTO memory_entries (
                scope, kind, title, body, tags, source, file_path, summary_path, created_at, updated_at,
                type, importance, superseded_by, ttl_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scope, kind, title, body, tags_json, source, file_path, summary_path, now, now,
                type, int(importance), superseded_by, ttl_at,
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
        )

    def get_memory_entry(self, entry_id: int) -> MemoryEntryRecord | None:
        row = self.execute(
            """
            SELECT id, scope, kind, title, body, tags, source, file_path, summary_path, created_at, updated_at,
                   type, importance, superseded_by, ttl_at
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
            tags=tuple(json.loads(row[5] or "[]")),
            source=row[6],
            file_path=row[7],
            summary_path=row[8],
            created_at=row[9],
            updated_at=row[10],
            type=row[11] if row[11] is not None else "project",
            importance=int(row[12]) if row[12] is not None else 3,
            superseded_by=int(row[13]) if row[13] is not None else None,
            ttl_at=row[14],
        )

    def list_memory_entries(
        self,
        *,
        scope: str | None = None,
        kind: str | None = None,
        type: str | None = None,
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
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.execute(
            f"""
            SELECT id, scope, kind, title, body, tags, source, file_path, summary_path, created_at, updated_at,
                   type, importance, superseded_by, ttl_at
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
                tags=tuple(json.loads(row[5] or "[]")),
                source=row[6],
                file_path=row[7],
                summary_path=row[8],
                created_at=row[9],
                updated_at=row[10],
                type=row[11] if row[11] is not None else "project",
                importance=int(row[12]) if row[12] is not None else 3,
                superseded_by=int(row[13]) if row[13] is not None else None,
                ttl_at=row[14],
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
        """
        clauses: list[str] = []
        params: list[object] = []
        # Filter superseded entries out of recall by default — they remain
        # readable by id but shouldn't surface in relevance search. Matches
        # the spec's "superseded entries preserved for audit but not returned
        # by default" contract (§3.7).
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
                   me.type, me.importance, me.superseded_by, me.ttl_at,
                   bm25(memory_entries_fts) AS bm25_score
            FROM memory_entries_fts
            JOIN memory_entries me ON me.id = memory_entries_fts.rowid
            WHERE memory_entries_fts MATCH ?
              AND {where}
            ORDER BY bm25_score ASC
            LIMIT ?
            """
            fts_query = _normalize_fts_query(query_text)
            rows = self.execute(sql, (fts_query, *params, fetch_limit)).fetchall()
        else:
            where = " AND ".join(clauses)
            sql = f"""
            SELECT me.id, me.scope, me.kind, me.title, me.body, me.tags, me.source,
                   me.file_path, me.summary_path, me.created_at, me.updated_at,
                   me.type, me.importance, me.superseded_by, me.ttl_at,
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
                tags=tuple(json.loads(row[5] or "[]")),
                source=row[6],
                file_path=row[7],
                summary_path=row[8],
                created_at=row[9],
                updated_at=row[10],
                type=row[11] if row[11] is not None else "project",
                importance=int(row[12]) if row[12] is not None else 3,
                superseded_by=int(row[13]) if row[13] is not None else None,
                ttl_at=row[14],
            )
            bm25_score = float(row[15]) if row[15] is not None else None
            results.append((record, bm25_score))
        return results

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
