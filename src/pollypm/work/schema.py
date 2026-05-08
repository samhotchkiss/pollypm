"""SQLite schema for the work service.

All tables are prefixed with ``work_`` so they coexist with the existing
tables in ``state.db``.  Uses ``CREATE TABLE IF NOT EXISTS`` for idempotency.
"""

from __future__ import annotations

import sqlite3


WORK_SCHEMA = """
-- -------------------------------------------------------------------
-- Schema versioning
-- -------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS work_schema_version (
    version INTEGER NOT NULL,
    description TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

-- -------------------------------------------------------------------
-- Flow templates and nodes
-- -------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS work_flow_templates (
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    roles TEXT NOT NULL DEFAULT '{}',
    start_node TEXT NOT NULL,
    is_current INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    PRIMARY KEY (name, version)
);

CREATE TABLE IF NOT EXISTS work_flow_nodes (
    flow_template_name TEXT NOT NULL,
    flow_template_version INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    actor_type TEXT,
    actor_role TEXT,
    agent_name TEXT,
    next_node_id TEXT,
    reject_node_id TEXT,
    gates TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (flow_template_name, flow_template_version, node_id),
    FOREIGN KEY (flow_template_name, flow_template_version)
        REFERENCES work_flow_templates(name, version)
);

-- -------------------------------------------------------------------
-- Tasks
-- -------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS work_tasks (
    project TEXT NOT NULL,
    task_number INTEGER NOT NULL,
    title TEXT NOT NULL,
    type TEXT NOT NULL,
    labels TEXT NOT NULL DEFAULT '[]',

    work_status TEXT NOT NULL DEFAULT 'draft',
    flow_template_id TEXT NOT NULL,
    flow_template_version INTEGER NOT NULL DEFAULT 1,
    current_node_id TEXT,
    assignee TEXT,
    priority TEXT NOT NULL DEFAULT 'normal',
    requires_human_review INTEGER NOT NULL DEFAULT 0,

    description TEXT NOT NULL DEFAULT '',
    acceptance_criteria TEXT,
    constraints TEXT,
    relevant_files TEXT NOT NULL DEFAULT '[]',

    parent_project TEXT,
    parent_task_number INTEGER,
    supersedes_project TEXT,
    supersedes_task_number INTEGER,

    -- #1398 — plan task evolution metadata.
    -- ``plan_version`` increments each time a plan task gets refined
    -- in place (same task ID, new revision). Defaults to 1 so legacy
    -- rows (created before this column existed) read as the first
    -- revision without requiring a backfill pass.
    plan_version INTEGER NOT NULL DEFAULT 1,
    -- ``predecessor_task_id`` points to the prior attempt when a
    -- replan creates a new task instead of refining the existing
    -- one. Stored as canonical ``project/task_number`` text so the
    -- foreign key shape matches the rest of the codebase's task-id
    -- convention; NULL means "no predecessor — original attempt".
    predecessor_task_id TEXT,

    roles TEXT NOT NULL DEFAULT '{}',
    external_refs TEXT NOT NULL DEFAULT '{}',

    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,

    PRIMARY KEY (project, task_number)
);

CREATE INDEX IF NOT EXISTS idx_work_tasks_status
    ON work_tasks(work_status);

CREATE INDEX IF NOT EXISTS idx_work_tasks_project_status
    ON work_tasks(project, work_status);

CREATE INDEX IF NOT EXISTS idx_work_tasks_assignee
    ON work_tasks(assignee)
    WHERE assignee IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_work_tasks_active
    ON work_tasks(current_node_id)
    WHERE current_node_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_work_tasks_priority
    ON work_tasks(priority, work_status);
-- idx_work_tasks_predecessor is created by
-- _ensure_work_task_plan_columns after the column is backfilled
-- (migration 8). SQLite won't parse an index that references a
-- column the (pre-migration) legacy work_tasks table doesn't have
-- yet, so it must live in the ensure helper rather than WORK_SCHEMA.

CREATE TABLE IF NOT EXISTS work_task_delete_audit_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    task_number INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    flow_template_id TEXT NOT NULL DEFAULT '',
    previous_status TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    deleted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_work_task_delete_audit_outbox_project
    ON work_task_delete_audit_outbox(project, task_number);

CREATE TRIGGER IF NOT EXISTS trg_work_tasks_delete_audit_outbox
AFTER DELETE ON work_tasks
BEGIN
    INSERT INTO work_task_delete_audit_outbox (
        project,
        task_number,
        title,
        flow_template_id,
        previous_status,
        created_by,
        deleted_at
    )
    VALUES (
        OLD.project,
        OLD.task_number,
        OLD.title,
        OLD.flow_template_id,
        OLD.work_status,
        OLD.created_by,
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    );
END;

-- -------------------------------------------------------------------
-- Task dependencies / relationships
-- -------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS work_task_dependencies (
    from_project TEXT NOT NULL,
    from_task_number INTEGER NOT NULL,
    to_project TEXT NOT NULL,
    to_task_number INTEGER NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (from_project, from_task_number, to_project, to_task_number, kind),
    FOREIGN KEY (from_project, from_task_number)
        REFERENCES work_tasks(project, task_number),
    FOREIGN KEY (to_project, to_task_number)
        REFERENCES work_tasks(project, task_number)
);

CREATE INDEX IF NOT EXISTS idx_work_deps_to
    ON work_task_dependencies(to_project, to_task_number);

CREATE INDEX IF NOT EXISTS idx_work_deps_from
    ON work_task_dependencies(from_project, from_task_number, kind);

-- -------------------------------------------------------------------
-- Flow node executions
-- -------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS work_node_executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_project TEXT NOT NULL,
    task_number INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    visit INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    work_output TEXT,
    decision TEXT,
    decision_reason TEXT,
    started_at TEXT,
    completed_at TEXT,
    -- #922: per-execution kickoff delivery marker. Stamped by
    -- task_assignment notify() after the canonical "Resume work" ping
    -- lands on the worker session. The heartbeat sweep treats NULL as
    -- "kickoff not yet delivered for this visit" and forces the send
    -- past the idle/dedupe gates so the first push survives the
    -- claim → bootstrap race. A reject-bounce that opens a new visit
    -- gets a fresh row with kickoff_sent_at=NULL, which correctly
    -- re-fires the kickoff for the resumed work.
    kickoff_sent_at TEXT,
    FOREIGN KEY (task_project, task_number)
        REFERENCES work_tasks(project, task_number),
    UNIQUE (task_project, task_number, node_id, visit)
);

CREATE INDEX IF NOT EXISTS idx_work_exec_task
    ON work_node_executions(task_project, task_number);

-- -------------------------------------------------------------------
-- Context entries (append-only log per task)
-- -------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS work_context_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_project TEXT NOT NULL,
    task_number INTEGER NOT NULL,
    actor TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    entry_type TEXT NOT NULL DEFAULT 'note',
    FOREIGN KEY (task_project, task_number)
        REFERENCES work_tasks(project, task_number)
);

CREATE INDEX IF NOT EXISTS idx_work_context_task
    ON work_context_entries(task_project, task_number, id DESC);
-- idx_work_context_entry_type is created by _ensure_context_entry_columns
-- after the entry_type column is backfilled (migration 3).

-- -------------------------------------------------------------------
-- Transitions (status change history)
-- -------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS work_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_project TEXT NOT NULL,
    task_number INTEGER NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_project, task_number)
        REFERENCES work_tasks(project, task_number)
);

CREATE INDEX IF NOT EXISTS idx_work_transitions_task
    ON work_transitions(task_project, task_number, id DESC);

-- -------------------------------------------------------------------
-- Worker sessions (task ↔ tmux/worktree bindings)
-- -------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS work_sessions (
    task_project TEXT NOT NULL,
    task_number INTEGER NOT NULL,
    agent_name TEXT NOT NULL,
    pane_id TEXT,
    worktree_path TEXT,
    branch_name TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    total_input_tokens INTEGER DEFAULT 0,
    total_output_tokens INTEGER DEFAULT 0,
    archive_path TEXT,
    PRIMARY KEY (task_project, task_number),
    FOREIGN KEY (task_project, task_number) REFERENCES work_tasks(project, task_number)
);
"""


def create_work_tables(conn: sqlite3.Connection) -> None:
    """Create all work service tables and run pending migrations.

    Safe to call multiple times — schema uses IF NOT EXISTS and migrations
    are tracked in work_schema_version.
    """
    conn.executescript(WORK_SCHEMA)
    _ensure_flow_node_columns(conn)
    _ensure_context_entry_columns(conn)
    _ensure_node_execution_columns(conn)
    _ensure_work_task_plan_columns(conn)
    _run_work_migrations(conn)


def _ensure_flow_node_columns(conn: sqlite3.Connection) -> None:
    """Backfill optional columns on work_flow_nodes for legacy DBs.

    CREATE TABLE IF NOT EXISTS is a no-op on an existing table, so rows
    created before newer columns were added need an explicit
    ALTER TABLE. SQLite lacks IF NOT EXISTS on ADD COLUMN, so we check
    PRAGMA table_info first.
    """
    cols = {
        row[1] for row in conn.execute("PRAGMA table_info(work_flow_nodes)")
    }
    if "agent_name" not in cols:
        conn.execute("ALTER TABLE work_flow_nodes ADD COLUMN agent_name TEXT")


def _ensure_node_execution_columns(conn: sqlite3.Connection) -> None:
    """Backfill optional columns on work_node_executions for legacy DBs.

    SQLite lacks IF NOT EXISTS on ADD COLUMN, and CREATE TABLE IF NOT
    EXISTS is a no-op for already-present tables, so columns added in
    later migrations need an explicit guard. Mirrors the existing
    ``_ensure_flow_node_columns`` / ``_ensure_context_entry_columns``
    pattern.

    The migration list still records a version bump for ``kickoff_sent_at``
    (#922) so a fresh DB tags itself as v7. This helper handles the
    case where a v6 DB has the column added by the WORK_SCHEMA refresh
    *before* migration v7 attempts the ALTER TABLE — that would
    otherwise raise ``duplicate column name``.
    """
    cols = {
        row[1] for row in conn.execute("PRAGMA table_info(work_node_executions)")
    }
    if "kickoff_sent_at" not in cols:
        conn.execute(
            "ALTER TABLE work_node_executions ADD COLUMN kickoff_sent_at TEXT"
        )


def _ensure_work_task_plan_columns(conn: sqlite3.Connection) -> None:
    """Backfill plan_version + predecessor_task_id on work_tasks (#1398).

    Mirrors the pattern used for kickoff_sent_at / entry_type / agent_name:
    SQLite lacks IF NOT EXISTS on ADD COLUMN and CREATE TABLE IF NOT
    EXISTS is a no-op on already-present tables, so legacy DBs that
    pre-date this migration need an explicit guarded ALTER TABLE.
    Migration v8 records the version bump for both fresh and legacy
    rows so the schema_version row stays accurate.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(work_tasks)")}
    if "plan_version" not in cols:
        conn.execute(
            "ALTER TABLE work_tasks "
            "ADD COLUMN plan_version INTEGER NOT NULL DEFAULT 1"
        )
    if "predecessor_task_id" not in cols:
        conn.execute(
            "ALTER TABLE work_tasks ADD COLUMN predecessor_task_id TEXT"
        )
    # Successors lookup index. Created here (not just in WORK_SCHEMA)
    # because legacy DBs that already have a work_tasks table must get
    # the partial index after the column lands.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_work_tasks_predecessor "
        "ON work_tasks(predecessor_task_id) "
        "WHERE predecessor_task_id IS NOT NULL"
    )


def _ensure_context_entry_columns(conn: sqlite3.Connection) -> None:
    """Backfill optional ``entry_type`` column on work_context_entries.

    The column classifies each row (``note`` default, ``reply`` for user
    chat replies, ``read`` for inbox read-markers). Legacy rows keep the
    default ``note`` tag so existing context-log consumers don't change
    shape. The entry_type index is created here too — not in WORK_SCHEMA —
    because SQLite won't parse an index that references a column the
    (pre-migration) legacy table doesn't have.
    """
    cols = {
        row[1] for row in conn.execute("PRAGMA table_info(work_context_entries)")
    }
    if "entry_type" not in cols:
        conn.execute(
            "ALTER TABLE work_context_entries "
            "ADD COLUMN entry_type TEXT NOT NULL DEFAULT 'note'"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_work_context_entry_type "
        "ON work_context_entries(task_project, task_number, entry_type)"
    )


# ------------------------------------------------------------------
# Work service migrations — append-only list.
# ------------------------------------------------------------------
_WORK_MIGRATIONS: list[tuple[int, str, list[str]]] = [
    (1, "Initial schema — baseline version", []),
    (
        2,
        "Add work_sync_state table for per-adapter per-task sync tracking",
        [
            """
            CREATE TABLE IF NOT EXISTS work_sync_state (
                task_project TEXT NOT NULL,
                task_number INTEGER NOT NULL,
                adapter_name TEXT NOT NULL,
                last_synced_at TEXT,
                last_error TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (task_project, task_number, adapter_name),
                FOREIGN KEY (task_project, task_number)
                    REFERENCES work_tasks(project, task_number)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_work_sync_state_adapter
                ON work_sync_state(adapter_name)
            """,
        ],
    ),
    (
        3,
        "Add entry_type column to work_context_entries for inbox reply/read markers",
        [
            # SQLite lacks IF NOT EXISTS on ADD COLUMN — guarded separately in
            # _ensure_context_entry_columns below. This migration row exists
            # so the schema_version bump is recorded for fresh DBs too.
        ],
    ),
    (
        4,
        "Add notification_staging table for pm notify priority tiering / digest rollups",
        [
            """
            CREATE TABLE IF NOT EXISTS notification_staging (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project TEXT NOT NULL,
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                actor TEXT NOT NULL,
                priority TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                milestone_key TEXT,
                created_at TEXT NOT NULL,
                flushed_at TEXT,
                rollup_task_id TEXT
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_notification_staging_pending
                ON notification_staging(project, milestone_key, flushed_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_notification_staging_created
                ON notification_staging(created_at)
            """,
        ],
    ),
    (
        5,
        "Add hot-path indexes for active-task and dependency-from queries",
        [
            """
            CREATE INDEX IF NOT EXISTS idx_work_tasks_active
                ON work_tasks(current_node_id)
                WHERE current_node_id IS NOT NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_work_deps_from
                ON work_task_dependencies(from_project, from_task_number, kind)
            """,
        ],
    ),
    (
        6,
        "Persist worker provider + transcript home so per-task archival "
        "can locate the right Claude/Codex tree at teardown (#809)",
        [
            # Provider name (``claude`` / ``codex`` / future). Tied to
            # ``ProviderKind`` values but stored as text so a new
            # provider doesn't require another migration.
            "ALTER TABLE work_sessions ADD COLUMN provider TEXT",
            # Filesystem root the worker's transcripts/credentials
            # live under (``CLAUDE_CONFIG_DIR`` for Claude,
            # ``CODEX_HOME`` for Codex). NULL means "fall back to the
            # ambient process env" — preserves pre-#809 behaviour.
            "ALTER TABLE work_sessions ADD COLUMN provider_home TEXT",
        ],
    ),
    (
        7,
        "Track per-execution kickoff delivery so the heartbeat sweep can "
        "force the first 'Resume work' ping past the bootstrap race (#922)",
        [
            # The ``kickoff_sent_at`` column is actually added by
            # ``_ensure_node_execution_columns`` (which runs BEFORE this
            # migration list), because SQLite lacks IF NOT EXISTS on
            # ADD COLUMN and the WORK_SCHEMA refresh already places the
            # column on freshly-created tables. This migration entry
            # exists so the schema-version row records the v7 bump for
            # both fresh and pre-v7 databases.
        ],
    ),
    (
        8,
        "Add plan_version + predecessor_task_id to work_tasks for "
        "plan-task evolution (refinement vs replan) — #1398",
        [
            # Same pattern as v7 — the columns are actually added by
            # ``_ensure_work_task_plan_columns`` (runs BEFORE the
            # migration walk) because SQLite lacks IF NOT EXISTS on
            # ADD COLUMN. The migration entry records the v8 bump so
            # ``work_schema_version`` stays accurate for both fresh
            # and legacy DBs.
        ],
    ),
    (
        9,
        "Add work_task_delete_audit_outbox trigger so raw work_tasks "
        "deletes emit task.deleted audit events",
        [
            # The table, index, and trigger are created by WORK_SCHEMA before
            # the migration walk. This row records that the DB has the delete
            # audit guard installed.
        ],
    ),
]


def _run_work_migrations(conn: sqlite3.Connection) -> None:
    from datetime import UTC, datetime

    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM work_schema_version"
        ).fetchone()
        current = row[0] if row else 0
    except Exception:  # noqa: BLE001
        current = 0

    for version, description, stmts in _WORK_MIGRATIONS:
        if version <= current:
            continue
        for sql in stmts:
            conn.execute(sql)
        conn.execute(
            "INSERT INTO work_schema_version (version, description, applied_at) VALUES (?, ?, ?)",
            (version, description, datetime.now(UTC).isoformat()),
        )
    conn.commit()
