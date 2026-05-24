"""Canonical Postgres schema for PollyPM (issue #1737, Slice A).

This module is the single source of truth for the pg DDL. Slice A ports
every table from the sqlite shape (``storage/state.py:SCHEMA``,
``work/schema.py:WORK_SCHEMA``, ``store/schema.py``, plus the
``notification_staging`` bootstrap in ``notification_staging.py``) into
pg-native form. Subsequent slices add migrations on top via the
``schema_migrations`` audit table.

Conventions
-----------

* JSON columns use ``jsonb`` (not ``text``) so payload filters can hit
  GIN indexes once Slice C ports the query helpers.
* ISO-8601 timestamp columns use ``timestamptz`` (not ``text``). Inserts
  send ``CURRENT_TIMESTAMP``; reads coerce to Python ``datetime`` via
  psycopg's default adapter.
* SQLite ``INTEGER`` flag columns become ``boolean`` where the call sites
  treated them as 0/1 (``requires_human_review``, ``pane_dead``,
  ``refresh_available``, ``tier4_active``). Counters stay ``bigint`` /
  ``int``.
* SQLite auto-increment rowid PKs become ``bigserial``. The sequence
  reset step (``setval(pg_get_serial_sequence(...), max(id) + 1)``) is
  Slice E's job — the schema itself just installs the sequences.
* Inferred FKs from the sqlite shape become enforced
  ``ON DELETE CASCADE`` constraints in pg. The design doc flagged this
  as a migration-time validation step; Slice A only installs the
  constraints, Slice E surfaces orphan rows.
* FTS5 mirror tables (``messages_fts``, ``memory_entries_fts``) become
  ``tsvector`` columns plus GIN indexes; Slice D adds the
  pgvector ``embeddings`` table that complements them.
* ``project_key`` (per #1737 §2.4) stays a plain ``text`` column on
  every domain table that currently keys by project — no native
  partitioning, just a btree index.
"""

from __future__ import annotations


# --------------------------------------------------------------------- #
# Migration applier — schema_migrations audit table
# --------------------------------------------------------------------- #

# The applier records every applied DDL pack in this table. Future slices
# append to ``MIGRATIONS`` below — version 0001 carries the full initial
# schema; 0002+ are forward-only deltas.

SCHEMA_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    int PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now(),
    label      text NOT NULL
);
"""


# --------------------------------------------------------------------- #
# Extension bootstrap — runs before any DDL that uses vector(N).
# --------------------------------------------------------------------- #

EXTENSIONS = """
CREATE EXTENSION IF NOT EXISTS vector;
"""


# --------------------------------------------------------------------- #
# Migration 0001 — the full initial schema.
# --------------------------------------------------------------------- #
# Split into table groups so a reviewer can map each ``CREATE TABLE`` back
# to its sqlite origin. The applier runs the whole pack in one
# transaction; ordering within the pack matters for FK validity.

# --- Group A: operator session / accounts / runtime state. --- #
_GROUP_OPERATOR = """
CREATE TABLE IF NOT EXISTS sessions (
    name        text PRIMARY KEY,
    role        text NOT NULL,
    project     text NOT NULL,
    provider    text NOT NULL,
    account     text NOT NULL,
    cwd         text NOT NULL,
    window_name text NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project);

CREATE TABLE IF NOT EXISTS heartbeats (
    id            bigserial PRIMARY KEY,
    session_name  text NOT NULL,
    tmux_window   text NOT NULL,
    pane_id       text NOT NULL,
    pane_command  text NOT NULL,
    pane_dead     boolean NOT NULL,
    log_bytes     bigint NOT NULL,
    snapshot_path text NOT NULL,
    snapshot_hash text NOT NULL DEFAULT '',
    created_at    timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_heartbeats_session
    ON heartbeats(session_name, id DESC);

CREATE TABLE IF NOT EXISTS leases (
    session_name text PRIMARY KEY,
    owner        text NOT NULL,
    note         text NOT NULL,
    updated_at   timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS account_usage (
    account_name  text PRIMARY KEY,
    provider      text NOT NULL,
    plan          text NOT NULL,
    health        text NOT NULL,
    usage_summary text NOT NULL,
    raw_text      text NOT NULL,
    used_pct      int,
    remaining_pct int,
    -- #1842 — reset_at carries provider display strings ("Monday 1am",
    -- "10:09 on 5 May", "Apr 10 at 1am"), not parseable timestamps. It
    -- must be ``text`` to match the sqlite StateStore contract and to
    -- avoid Postgres rejecting/silently corrupting normal provider
    -- output. See tests/test_provider_sdk.py + the pg_accounts facade.
    reset_at      text,
    period_label  text,
    updated_at    timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS account_runtime (
    account_name        text PRIMARY KEY,
    provider            text NOT NULL,
    status              text NOT NULL,
    reason              text NOT NULL,
    -- #1842 — available_at / access_expires_at are display strings
    -- from the account-runtime sampler, not parseable timestamps.
    -- Stored as ``text`` to round-trip the sqlite StateStore contract.
    available_at        text,
    access_expires_at   text,
    refresh_available   boolean NOT NULL DEFAULT false,
    updated_at          timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS session_runtime (
    session_name                 text PRIMARY KEY,
    status                       text NOT NULL DEFAULT 'healthy',
    effective_account            text,
    effective_provider           text,
    recovery_attempts            int NOT NULL DEFAULT 0,
    recovery_window_started_at   timestamptz,
    last_failure_type            text,
    last_failure_message         text,
    last_checkpoint_path         text,
    retry_at                     timestamptz,
    last_recovered_at            timestamptz,
    updated_at                   timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS checkpoints (
    id            bigserial PRIMARY KEY,
    session_name  text NOT NULL,
    project_key   text NOT NULL,
    level         text NOT NULL,
    json_path     text NOT NULL,
    summary_path  text NOT NULL,
    snapshot_path text NOT NULL,
    summary_text  text NOT NULL,
    created_at    timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_checkpoints_project ON checkpoints(project_key);

CREATE TABLE IF NOT EXISTS worktrees (
    id            bigserial PRIMARY KEY,
    project_key   text NOT NULL,
    lane_kind     text NOT NULL,
    lane_key      text NOT NULL,
    session_name  text,
    issue_key     text,
    path          text NOT NULL,
    branch        text NOT NULL,
    status        text NOT NULL,
    created_at    timestamptz NOT NULL,
    updated_at    timestamptz NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_worktrees_active
    ON worktrees(project_key, lane_kind, lane_key, status);

CREATE TABLE IF NOT EXISTS token_samples (
    session_name      text PRIMARY KEY,
    account_name      text NOT NULL,
    provider          text NOT NULL,
    model_name        text NOT NULL,
    project_key       text NOT NULL,
    cumulative_tokens bigint NOT NULL,
    observed_at       timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_token_samples_project ON token_samples(project_key);

CREATE TABLE IF NOT EXISTS token_usage_hourly (
    hour_bucket  text NOT NULL,
    account_name text NOT NULL,
    provider     text NOT NULL,
    model_name   text NOT NULL,
    project_key  text NOT NULL,
    tokens_used  bigint NOT NULL,
    updated_at   timestamptz NOT NULL,
    PRIMARY KEY (hour_bucket, account_name, provider, model_name, project_key)
);
"""


# --- Group B: unified messages / inbox surface. --- #
_GROUP_MESSAGES = """
CREATE TABLE IF NOT EXISTS messages (
    id           bigserial PRIMARY KEY,
    scope        text NOT NULL,
    project_key  text NOT NULL DEFAULT '',
    type         text NOT NULL,
    tier         text NOT NULL DEFAULT 'immediate',
    recipient    text NOT NULL,
    sender       text NOT NULL,
    state        text NOT NULL DEFAULT 'open',
    parent_id    bigint,
    subject      text NOT NULL,
    body         text NOT NULL DEFAULT '',
    payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    labels       jsonb NOT NULL DEFAULT '[]'::jsonb,
    kind         text NOT NULL DEFAULT 'legacy',
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    closed_at    timestamptz
);

CREATE INDEX IF NOT EXISTS idx_messages_recipient_state
    ON messages(recipient, state);
CREATE INDEX IF NOT EXISTS idx_messages_type_tier
    ON messages(type, tier);
CREATE INDEX IF NOT EXISTS idx_messages_scope_created
    ON messages(scope, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_project_key
    ON messages(project_key);

-- Partial unique alert index (mirrors sqlite ``messages_open_alert_uniq``
-- from #1044). At most one open alert per (scope, type='alert', subject).
CREATE UNIQUE INDEX IF NOT EXISTS messages_open_alert_uniq
    ON messages(scope, subject)
    WHERE type = 'alert' AND state = 'open';

-- GIN indexes for jsonb filter paths.
CREATE INDEX IF NOT EXISTS idx_messages_payload_json_gin
    ON messages USING gin (payload_json);
CREATE INDEX IF NOT EXISTS idx_messages_labels_gin
    ON messages USING gin (labels);

-- tsvector keyword search (replaces FTS5 ``messages_fts``). Generated
-- column so writes don't have to maintain a trigger.
ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS subject_body_tsv tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(subject, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(body, '')), 'B')
    ) STORED;
CREATE INDEX IF NOT EXISTS idx_messages_tsv_gin
    ON messages USING gin (subject_body_tsv);
"""


# --- Group C: work-service domain. --- #
# Faithfully ports work/schema.py:WORK_SCHEMA. FKs that were inferred in
# sqlite become real ON DELETE CASCADE here. ``kind`` ports the #1565
# inbox discriminator with the same ``legacy`` default.
_GROUP_WORK = """
CREATE TABLE IF NOT EXISTS work_flow_templates (
    name        text NOT NULL,
    version     int NOT NULL,
    description text NOT NULL DEFAULT '',
    roles       jsonb NOT NULL DEFAULT '{}'::jsonb,
    start_node  text NOT NULL,
    is_current  boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL,
    PRIMARY KEY (name, version)
);

CREATE TABLE IF NOT EXISTS work_flow_nodes (
    flow_template_name    text NOT NULL,
    flow_template_version int NOT NULL,
    node_id               text NOT NULL,
    name                  text NOT NULL,
    type                  text NOT NULL,
    actor_type            text,
    actor_role            text,
    agent_name            text,
    next_node_id          text,
    reject_node_id        text,
    gates                 jsonb NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY (flow_template_name, flow_template_version, node_id),
    FOREIGN KEY (flow_template_name, flow_template_version)
        REFERENCES work_flow_templates(name, version)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS work_tasks (
    project                 text NOT NULL,
    task_number             int NOT NULL,
    project_key             text NOT NULL,
    title                   text NOT NULL,
    type                    text NOT NULL,
    labels                  jsonb NOT NULL DEFAULT '[]'::jsonb,
    work_status             text NOT NULL DEFAULT 'draft',
    flow_template_id        text NOT NULL,
    flow_template_version   int NOT NULL DEFAULT 1,
    current_node_id         text,
    assignee                text,
    claimed_by_session      text,
    priority                text NOT NULL DEFAULT 'normal',
    requires_human_review   boolean NOT NULL DEFAULT false,
    description             text NOT NULL DEFAULT '',
    acceptance_criteria     text,
    constraints             text,
    relevant_files          jsonb NOT NULL DEFAULT '[]'::jsonb,
    parent_project          text,
    parent_task_number      int,
    supersedes_project      text,
    supersedes_task_number  int,
    plan_version            int NOT NULL DEFAULT 1,
    predecessor_task_id     text,
    kind                    text NOT NULL DEFAULT 'legacy',
    roles                   jsonb NOT NULL DEFAULT '{}'::jsonb,
    external_refs           jsonb NOT NULL DEFAULT '{}'::jsonb,
    reap_count              int NOT NULL DEFAULT 0,
    created_at              timestamptz NOT NULL,
    created_by              text NOT NULL,
    updated_at              timestamptz NOT NULL,
    PRIMARY KEY (project, task_number),
    -- Self-FKs for the parent / supersedes / predecessor wires. Made
    -- DEFERRABLE INITIALLY DEFERRED so a parent-child pair created in
    -- one transaction validates at COMMIT, not row insert.
    FOREIGN KEY (parent_project, parent_task_number)
        REFERENCES work_tasks(project, task_number)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX IF NOT EXISTS idx_work_tasks_status ON work_tasks(work_status);
CREATE INDEX IF NOT EXISTS idx_work_tasks_project_status
    ON work_tasks(project, work_status);
CREATE INDEX IF NOT EXISTS idx_work_tasks_project_key
    ON work_tasks(project_key);
CREATE INDEX IF NOT EXISTS idx_work_tasks_assignee
    ON work_tasks(assignee) WHERE assignee IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_work_tasks_active
    ON work_tasks(current_node_id) WHERE current_node_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_work_tasks_priority
    ON work_tasks(priority, work_status);
CREATE INDEX IF NOT EXISTS idx_work_tasks_predecessor
    ON work_tasks(predecessor_task_id) WHERE predecessor_task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_work_tasks_labels_gin
    ON work_tasks USING gin (labels);
CREATE INDEX IF NOT EXISTS idx_work_tasks_roles_gin
    ON work_tasks USING gin (roles);
CREATE INDEX IF NOT EXISTS idx_work_tasks_external_refs_gin
    ON work_tasks USING gin (external_refs);

CREATE TABLE IF NOT EXISTS work_task_delete_audit_outbox (
    id                bigserial PRIMARY KEY,
    project           text NOT NULL,
    task_number       int NOT NULL,
    title             text NOT NULL DEFAULT '',
    flow_template_id  text NOT NULL DEFAULT '',
    previous_status   text NOT NULL DEFAULT '',
    created_by        text NOT NULL DEFAULT '',
    deleted_at        timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_work_task_delete_audit_outbox_project
    ON work_task_delete_audit_outbox(project, task_number);

-- Port of the sqlite AFTER DELETE trigger on work_tasks. Same payload —
-- the JSONL audit flush is unchanged.
CREATE OR REPLACE FUNCTION work_tasks_delete_audit_outbox_fn()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO work_task_delete_audit_outbox (
        project, task_number, title, flow_template_id,
        previous_status, created_by, deleted_at
    ) VALUES (
        OLD.project, OLD.task_number, OLD.title, OLD.flow_template_id,
        OLD.work_status, OLD.created_by, now()
    );
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_work_tasks_delete_audit_outbox ON work_tasks;
CREATE TRIGGER trg_work_tasks_delete_audit_outbox
AFTER DELETE ON work_tasks
FOR EACH ROW EXECUTE FUNCTION work_tasks_delete_audit_outbox_fn();

CREATE TABLE IF NOT EXISTS work_task_dependencies (
    from_project     text NOT NULL,
    from_task_number int NOT NULL,
    to_project       text NOT NULL,
    to_task_number   int NOT NULL,
    kind             text NOT NULL,
    created_at       timestamptz NOT NULL,
    PRIMARY KEY (from_project, from_task_number, to_project, to_task_number, kind),
    FOREIGN KEY (from_project, from_task_number)
        REFERENCES work_tasks(project, task_number) ON DELETE CASCADE,
    FOREIGN KEY (to_project, to_task_number)
        REFERENCES work_tasks(project, task_number) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_work_deps_to
    ON work_task_dependencies(to_project, to_task_number);
CREATE INDEX IF NOT EXISTS idx_work_deps_from
    ON work_task_dependencies(from_project, from_task_number, kind);

CREATE TABLE IF NOT EXISTS work_node_executions (
    id              bigserial PRIMARY KEY,
    task_project    text NOT NULL,
    task_number     int NOT NULL,
    node_id         text NOT NULL,
    visit           int NOT NULL,
    status          text NOT NULL DEFAULT 'pending',
    work_output     jsonb,
    decision        text,
    decision_reason text,
    started_at      timestamptz,
    completed_at    timestamptz,
    kickoff_sent_at timestamptz,
    FOREIGN KEY (task_project, task_number)
        REFERENCES work_tasks(project, task_number) ON DELETE CASCADE,
    UNIQUE (task_project, task_number, node_id, visit)
);

CREATE INDEX IF NOT EXISTS idx_work_exec_task
    ON work_node_executions(task_project, task_number);

CREATE TABLE IF NOT EXISTS work_context_entries (
    id           bigserial PRIMARY KEY,
    task_project text NOT NULL,
    task_number  int NOT NULL,
    actor        text NOT NULL,
    text         text NOT NULL,
    created_at   timestamptz NOT NULL,
    entry_type   text NOT NULL DEFAULT 'note',
    FOREIGN KEY (task_project, task_number)
        REFERENCES work_tasks(project, task_number) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_work_context_task
    ON work_context_entries(task_project, task_number, id DESC);
CREATE INDEX IF NOT EXISTS idx_work_context_entry_type
    ON work_context_entries(task_project, task_number, entry_type);

CREATE TABLE IF NOT EXISTS work_transitions (
    id           bigserial PRIMARY KEY,
    task_project text NOT NULL,
    task_number  int NOT NULL,
    from_state   text NOT NULL,
    to_state     text NOT NULL,
    actor        text NOT NULL,
    reason       text,
    created_at   timestamptz NOT NULL,
    FOREIGN KEY (task_project, task_number)
        REFERENCES work_tasks(project, task_number) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_work_transitions_task
    ON work_transitions(task_project, task_number, id DESC);

CREATE TABLE IF NOT EXISTS work_sessions (
    task_project          text NOT NULL,
    task_number           int NOT NULL,
    agent_name            text NOT NULL,
    pane_id               text,
    worktree_path         text,
    branch_name           text,
    started_at            timestamptz NOT NULL,
    ended_at              timestamptz,
    total_input_tokens    bigint DEFAULT 0,
    total_output_tokens   bigint DEFAULT 0,
    archive_path          text,
    provider              text,
    provider_home         text,
    PRIMARY KEY (task_project, task_number),
    FOREIGN KEY (task_project, task_number)
        REFERENCES work_tasks(project, task_number) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS work_sync_state (
    task_project    text NOT NULL,
    task_number     int NOT NULL,
    adapter_name    text NOT NULL,
    last_synced_at  timestamptz,
    last_error      text,
    attempts        int NOT NULL DEFAULT 0,
    PRIMARY KEY (task_project, task_number, adapter_name),
    FOREIGN KEY (task_project, task_number)
        REFERENCES work_tasks(project, task_number) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_work_sync_state_adapter
    ON work_sync_state(adapter_name);
"""


# --- Group D: ops / job / memory / workspace. --- #
_GROUP_OPS = """
CREATE TABLE IF NOT EXISTS memory_entries (
    id              bigserial PRIMARY KEY,
    scope           text NOT NULL,
    project_key     text NOT NULL DEFAULT '',
    kind            text NOT NULL,
    title           text NOT NULL,
    body            text NOT NULL,
    tags            text NOT NULL,
    source          text NOT NULL,
    file_path       text NOT NULL,
    summary_path    text NOT NULL,
    created_at      timestamptz NOT NULL,
    updated_at      timestamptz NOT NULL,
    type            text NOT NULL DEFAULT 'project',
    importance      int NOT NULL DEFAULT 3,
    superseded_by   bigint,
    ttl_at          timestamptz,
    scope_tier      text NOT NULL DEFAULT 'project'
);

CREATE INDEX IF NOT EXISTS idx_memory_entries_scope
    ON memory_entries(scope, id DESC);
CREATE INDEX IF NOT EXISTS idx_memory_entries_type
    ON memory_entries(type);
CREATE INDEX IF NOT EXISTS idx_memory_entries_tier
    ON memory_entries(scope_tier);
CREATE INDEX IF NOT EXISTS idx_memory_entries_project_key
    ON memory_entries(project_key);

ALTER TABLE memory_entries
    ADD COLUMN IF NOT EXISTS title_body_tsv tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(body, '')), 'B') ||
        setweight(to_tsvector('english', coalesce(tags, '')), 'C')
    ) STORED;
CREATE INDEX IF NOT EXISTS idx_memory_entries_tsv_gin
    ON memory_entries USING gin (title_body_tsv);

CREATE TABLE IF NOT EXISTS memory_summaries (
    id            bigserial PRIMARY KEY,
    scope         text NOT NULL,
    summary_text  text NOT NULL,
    summary_path  text NOT NULL,
    entry_count   int NOT NULL,
    created_at    timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS work_jobs (
    id            bigserial PRIMARY KEY,
    handler_name  text NOT NULL,
    payload_json  jsonb NOT NULL,
    status        text NOT NULL DEFAULT 'queued',
    attempt       int NOT NULL DEFAULT 0,
    max_attempts  int NOT NULL DEFAULT 3,
    dedupe_key    text,
    enqueued_at   timestamptz NOT NULL,
    run_after     timestamptz NOT NULL,
    claimed_at    timestamptz,
    claimed_by    text,
    finished_at   timestamptz,
    last_error    text
);

CREATE INDEX IF NOT EXISTS idx_work_jobs_claim
    ON work_jobs(status, run_after, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_work_jobs_dedupe_queued
    ON work_jobs(dedupe_key)
    WHERE dedupe_key IS NOT NULL AND status IN ('queued', 'claimed');

CREATE TABLE IF NOT EXISTS architect_resume_tokens (
    project_key      text PRIMARY KEY,
    provider         text NOT NULL,
    session_id       text NOT NULL,
    captured_at      timestamptz NOT NULL,
    last_active_at   timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS workspace_state (
    key         text PRIMARY KEY,
    value_json  jsonb NOT NULL,
    set_at      timestamptz NOT NULL,
    set_by      text NOT NULL DEFAULT 'system'
);

-- #1997 — ``terminal_handoff_at`` is the persistent gate that prevents
-- the infinite re-promotion loop on a permanently-stuck finding (see
-- the matching block in pollypm.storage.state for the rationale).
CREATE TABLE IF NOT EXISTS tier4_promotion_state (
    root_cause_hash            text PRIMARY KEY,
    project                    text NOT NULL DEFAULT '',
    rule                       text NOT NULL DEFAULT '',
    dispatch_history_json      jsonb NOT NULL DEFAULT '[]'::jsonb,
    last_promotion_at          timestamptz,
    promotion_path             text,
    tier4_entered_at           timestamptz,
    tier4_active               boolean NOT NULL DEFAULT false,
    last_finding_signature     text NOT NULL DEFAULT '',
    updated_at                 timestamptz NOT NULL DEFAULT now(),
    terminal_handoff_at        timestamptz
);

CREATE INDEX IF NOT EXISTS idx_tier4_promotion_state_active
    ON tier4_promotion_state(tier4_active, tier4_entered_at);

-- Notification staging (#704 retirement is a separate follow-up; port as-is).
CREATE TABLE IF NOT EXISTS notification_staging (
    id              bigserial PRIMARY KEY,
    project         text NOT NULL,
    subject         text NOT NULL,
    body            text NOT NULL,
    actor           text NOT NULL,
    priority        text NOT NULL,
    payload_json    jsonb NOT NULL,
    milestone_key   text,
    created_at      timestamptz NOT NULL,
    flushed_at      timestamptz,
    rollup_task_id  text
);

CREATE INDEX IF NOT EXISTS idx_notification_staging_pending
    ON notification_staging(project, milestone_key, flushed_at);
CREATE INDEX IF NOT EXISTS idx_notification_staging_created
    ON notification_staging(created_at);
"""


# --- Group E: pgvector embeddings (the new memory-recall surface). --- #
# Slice D wires the writer + recall query; Slice A only installs the
# table + HNSW index so the schema is complete on day one.
_GROUP_EMBEDDINGS = """
CREATE TABLE IF NOT EXISTS embeddings (
    source_table  text NOT NULL,
    source_id     text NOT NULL,
    embedding     vector(1536) NOT NULL,
    model         text NOT NULL,
    generated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_table, source_id)
);

CREATE INDEX IF NOT EXISTS embeddings_hnsw
    ON embeddings USING hnsw (embedding vector_cosine_ops);
"""


# Combined DDL for migration 0001. The migration applier bootstraps the
# ``vector`` extension via :data:`EXTENSIONS` BEFORE the per-version
# transaction starts; pg won't parse ``vector(1536)`` ahead of time
# otherwise. Order within this pack: operator/messages (no deps), work
# (FKs reference work_tasks), ops, embeddings last.
INITIAL_SCHEMA_DDL = "\n".join(
    [
        _GROUP_OPERATOR,
        _GROUP_MESSAGES,
        _GROUP_WORK,
        _GROUP_OPS,
        _GROUP_EMBEDDINGS,
    ]
)


# --------------------------------------------------------------------- #
# Migration 0002 — align alert dedupe index with sqlite semantics (#1737).
# --------------------------------------------------------------------- #
# Slice I lands a real :class:`pollypm.store.backends.pg_store.PgStore`,
# whose :meth:`upsert_alert` / :meth:`upsert_message` rely on the partial
# unique index that enforces "one open alert per ``(scope, sender,
# recipient, type)``". The sqlite shadow at
# :data:`pollypm.store.schema.FTS_DDL_STATEMENTS` is keyed on those four
# columns; migration 0001 accidentally keyed the pg index on
# ``(scope, subject)`` instead — which mis-dedupes whenever two
# different alert subjects share a project, and over-dedupes when the
# same alert is re-emitted with a freshly-stamped subject.
#
# Forward fix: drop the wrong index (idempotently — ``IF EXISTS``) and
# recreate it on the canonical tuple. Closed rows are unconstrained
# because their lifecycle has ended; only ``state='open'`` matters.

_MIGRATION_0002_ALERT_DEDUPE = """
DROP INDEX IF EXISTS messages_open_alert_uniq;

CREATE UNIQUE INDEX IF NOT EXISTS messages_open_alert_uniq
    ON messages(scope, sender, recipient, type)
    WHERE state = 'open' AND type = 'alert';
"""


# --------------------------------------------------------------------- #
# 0003 — account_usage.reset_at + account_runtime.available_at /
# access_expires_at converted from ``timestamptz`` to ``text``.
# --------------------------------------------------------------------- #
#
# #1850: PR #1848 fixed the INITIAL_SCHEMA_DDL so these three columns
# are ``text`` on fresh installs (they carry provider display strings
# like "Monday 1am" — never ISO-8601 timestamps). Existing deployed pg
# databases were left on the old ``timestamptz`` columns and silently
# reject every upsert with a parse error.
#
# Forward fix: ``ALTER COLUMN ... TYPE text USING <col>::text``. The
# ``USING`` clause coerces any pre-existing timestamp rows into their
# ISO-8601 text form so no data is lost (best-effort — the columns are
# nullable and most deployed rows are NULL). ``IF EXISTS`` keeps the
# migration idempotent against fresh installs where 0001's schema
# already declared ``text``: pg's ALTER COLUMN is a no-op when the
# target type matches the current type.

_MIGRATION_0003_ACCOUNT_COLUMNS_TEXT = """
ALTER TABLE IF EXISTS account_usage
    ALTER COLUMN reset_at TYPE text USING reset_at::text;

ALTER TABLE IF EXISTS account_runtime
    ALTER COLUMN available_at TYPE text USING available_at::text;

ALTER TABLE IF EXISTS account_runtime
    ALTER COLUMN access_expires_at TYPE text USING access_expires_at::text;
"""


# --------------------------------------------------------------------- #
# 0004 — tier4_promotion_state.terminal_handoff_at gate (#1997).
# --------------------------------------------------------------------- #
#
# Persistent gate against the infinite re-promotion loop described in
# #1997. Without this column, ``tracker.clear(reason='budget_exhausted')``
# on the budget-exhaustion sweep flipped ``tier4_active=0`` and the next
# tick's auto-promote check trivially re-greenlit the same hash — 98
# promotions / 81 budget-exhausts / 0 demotions across 8 projects over
# 50h in the wild. With the column present, ``should_auto_promote``
# refuses to re-promote any hash that has had its terminal handoff fire
# until the finding actually resolves (which NULLs the column).
#
# Column-only migration. Fresh installs already get the column from
# 0001's INITIAL_SCHEMA_DDL; upgraded DBs run the single ALTER below.
# ``IF EXISTS`` + ``IF NOT EXISTS`` keep the statement idempotent.

_MIGRATION_0004_TIER4_TERMINAL_HANDOFF = """
ALTER TABLE IF EXISTS tier4_promotion_state
    ADD COLUMN IF NOT EXISTS terminal_handoff_at timestamptz;
"""


# --------------------------------------------------------------------- #
# 0005 — work_tasks.reap_count counter for #1999.
# --------------------------------------------------------------------- #
#
# #1999: ``worker_marker_reaper`` deletes a stale fresh-launch marker
# whenever the tmux window has vanished for a non-terminal task, but it
# never demoted the task back to ``queued`` or surfaced repeat reaps to
# the operator. Sam-approved policy:
#
# * 1st + 2nd reap on the same task → demote silently
# * 3rd+ reap on the same task → demote AND escalate to the inbox
#
# The counter must persist across cockpit restarts (Sam may restart
# between reaps), so it lives as a column on ``work_tasks``. The
# reaper UPDATE bumps + reads it atomically via RETURNING.
#
# Migration is idempotent: ``ADD COLUMN IF NOT EXISTS`` is a no-op when
# the column is already present (fresh installs pick it up from
# ``INITIAL_SCHEMA_DDL``; deployed installs gain it via this migration).

_MIGRATION_0005_WORK_TASKS_REAP_COUNT = """
ALTER TABLE IF EXISTS work_tasks
    ADD COLUMN IF NOT EXISTS reap_count int NOT NULL DEFAULT 0;
"""


# --------------------------------------------------------------------- #
# 0006 — work_tasks.claimed_by_session breadcrumb for claim identity.
# --------------------------------------------------------------------- #

_MIGRATION_0006_WORK_TASKS_CLAIMED_BY_SESSION = """
ALTER TABLE IF EXISTS work_tasks
    ADD COLUMN IF NOT EXISTS claimed_by_session text;
"""


# --------------------------------------------------------------------- #
# Migration list — forward-only, append-only.
# --------------------------------------------------------------------- #

# Each entry is ``(version, label, ddl)``. Versions strictly ascend; gaps
# are not allowed (the applier crashes loudly on a gap so a missing
# migration file can't silently skip schema state).
MIGRATIONS: list[tuple[int, str, str]] = [
    (1, "0001_initial", INITIAL_SCHEMA_DDL),
    (2, "0002_alert_dedupe_tuple", _MIGRATION_0002_ALERT_DEDUPE),
    (3, "0003_account_columns_text", _MIGRATION_0003_ACCOUNT_COLUMNS_TEXT),
    (4, "0004_tier4_terminal_handoff", _MIGRATION_0004_TIER4_TERMINAL_HANDOFF),
    (5, "0005_work_tasks_reap_count", _MIGRATION_0005_WORK_TASKS_REAP_COUNT),
    (
        6,
        "0006_work_tasks_claimed_by_session",
        _MIGRATION_0006_WORK_TASKS_CLAIMED_BY_SESSION,
    ),
]


def all_table_names() -> tuple[str, ...]:
    """Return the canonical list of table names defined by the schema.

    Used by tests + the doctor sanity probe to verify schema-applied
    state without hard-coding the list at every callsite. Kept in
    declaration order so test diffs are stable.
    """
    return (
        # operator
        "sessions",
        "heartbeats",
        "leases",
        "account_usage",
        "account_runtime",
        "session_runtime",
        "checkpoints",
        "worktrees",
        "token_samples",
        "token_usage_hourly",
        # messages
        "messages",
        # work
        "work_flow_templates",
        "work_flow_nodes",
        "work_tasks",
        "work_task_delete_audit_outbox",
        "work_task_dependencies",
        "work_node_executions",
        "work_context_entries",
        "work_transitions",
        "work_sessions",
        "work_sync_state",
        # ops
        "memory_entries",
        "memory_summaries",
        "work_jobs",
        "architect_resume_tokens",
        "workspace_state",
        "tier4_promotion_state",
        "notification_staging",
        # embeddings
        "embeddings",
        # bookkeeping
        "schema_migrations",
    )


__all__ = [
    "EXTENSIONS",
    "INITIAL_SCHEMA_DDL",
    "MIGRATIONS",
    "SCHEMA_MIGRATIONS_TABLE",
    "all_table_names",
]
