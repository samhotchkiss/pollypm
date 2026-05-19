"""Backend-agnostic dataclass records for storage-domain rows.

These ``@dataclass(slots=True)`` shapes are the canonical row
representation across both the legacy sqlite-backed
:mod:`pollypm.storage.state` and the postgres facades in
:mod:`pollypm.storage.work_session_queries`,
:mod:`pollypm.storage.work_task_state`,
:mod:`pollypm.storage.memory_recall`, etc. Keeping them in their own
module lets callers depend on the data shape without dragging a
``sqlite3`` import along.

Slice K-state-port (issue #1737) — extracted out of ``state.py`` so
``state.py`` can be deleted once all ``StateStore`` consumers have
migrated to per-table pg facades.
"""

from __future__ import annotations

from dataclasses import dataclass


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
