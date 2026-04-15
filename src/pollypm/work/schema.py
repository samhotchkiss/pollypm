"""SQLite schema for the work service.

All tables are prefixed with ``work_`` so they coexist with the existing
tables in ``state.db``.  Uses ``CREATE TABLE IF NOT EXISTS`` for idempotency.
"""

from __future__ import annotations

import sqlite3


WORK_SCHEMA = """
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

CREATE INDEX IF NOT EXISTS idx_work_tasks_priority
    ON work_tasks(priority, work_status);

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
    FOREIGN KEY (task_project, task_number)
        REFERENCES work_tasks(project, task_number)
);

CREATE INDEX IF NOT EXISTS idx_work_context_task
    ON work_context_entries(task_project, task_number, id DESC);

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
    """Create all work service tables.  Safe to call multiple times."""
    conn.executescript(WORK_SCHEMA)
