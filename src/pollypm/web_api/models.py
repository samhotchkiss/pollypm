"""Pydantic response models for the Web API.

Each model corresponds to a schema in ``docs/api/openapi.yaml``. The
field names + types mirror the YAML exactly; the OpenAPI conformance
test (`tests/web_api/test_openapi_conformance.py`) verifies that the
implementation's emitted document matches the on-disk contract.

Phase 1 only needs the read shapes plus the error envelope; write-side
models (``ApproveRequest``, ``RejectRequest``, etc.) ship in Phase 2.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Common
# ---------------------------------------------------------------------------


class ErrorBody(BaseModel):
    code: str
    message: str
    hint: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class ValidationErrorDetail(BaseModel):
    field: str
    message: str


class ValidationErrorResponse(ErrorResponse):
    details: list[ValidationErrorDetail] | None = None


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    schema_version: int
    started_at: datetime


# ---------------------------------------------------------------------------
# Action responses (write endpoints)
# ---------------------------------------------------------------------------


class ActionResult(BaseModel):
    """Generic ``{ok, message?}`` envelope for write endpoints (spec §6).

    Used by ``approveTask`` / ``rejectTask`` / ``queueTask`` and the
    inbox reply / archive routes. ``message`` is operator-facing and
    surfaces a short summary of the transition (e.g.
    ``"queued myproj/3"``); clients should not parse it.
    """

    ok: bool
    message: str | None = None


# ---------------------------------------------------------------------------
# Doctor (declared so the OpenAPI document carries the schema; route
# itself is implemented in Phase 3).
# ---------------------------------------------------------------------------


class DoctorCheck(BaseModel):
    name: str
    status: Literal["ok", "warn", "fail"]
    message: str
    hint: str | None = None


class DoctorResponse(BaseModel):
    overall: Literal["ok", "warn", "fail"]
    checks: list[DoctorCheck]


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


# Per spec §7 / OpenAPI: ``ProjectKind`` is open-ended; clients should
# treat unknown values as ``unknown``. Pydantic ``str`` keeps the door
# open without forcing a brittle Literal.
ProjectKindStr = str
ProjectGlyphStr = str


class Project(BaseModel):
    key: str
    name: str
    path: str = Field(description="Filesystem path (server-local).")
    tracked: bool
    kind: ProjectKindStr
    persona_name: str | None = None
    state: str | None = None
    glyph: ProjectGlyphStr
    task_counts: dict[str, int] = Field(default_factory=dict)
    open_inbox_count: int
    pending_plan_review: bool


class ProjectListResponse(BaseModel):
    items: list[Project]


class ProjectActivityEntry(BaseModel):
    ts: datetime
    event: str
    subject: str
    actor: str
    status: str | None = None
    summary: str | None = None


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


TaskStatusStr = Literal[
    "draft",
    "queued",
    "in_progress",
    "rework",
    "blocked",
    "on_hold",
    "review",
    "done",
    "cancelled",
]

TaskTypeStr = Literal["epic", "task", "subtask", "bug", "spike"]
TaskPriorityStr = Literal["critical", "high", "normal", "low"]


class TaskSummary(BaseModel):
    task_id: str
    project: str
    task_number: int
    title: str
    work_status: TaskStatusStr
    type: TaskTypeStr
    priority: TaskPriorityStr
    assignee: str | None = None
    current_node_id: str | None = None
    plan_version: int | None = None
    updated_at: datetime | None = None


class Transition(BaseModel):
    from_state: str
    to_state: str
    actor: str
    timestamp: datetime
    reason: str | None = None


class Artifact(BaseModel):
    kind: Literal["commit", "file_change", "action", "note"]
    description: str
    ref: str | None = None
    path: str | None = None
    external_ref: str | None = None


class WorkOutput(BaseModel):
    type: Literal["code_change", "action", "document", "mixed"]
    summary: str
    artifacts: list[Artifact] | None = None


class FlowNodeExecution(BaseModel):
    task_id: str
    node_id: str
    visit: int
    status: Literal["pending", "active", "blocked", "completed", "abandoned"]
    decision: Literal["approved", "rejected"] | None = None
    decision_reason: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    work_output: WorkOutput | None = None


class ContextEntry(BaseModel):
    actor: str
    timestamp: datetime
    text: str
    # ``entry_type`` is open-ended on the work-service side: writers
    # set free-form labels like ``human_review_approved``,
    # ``readiness_warning``, ``rollup_item``, ``proposal_accepted``,
    # ``plan_review_approved``, etc. We don't constrain it on the API
    # surface — clients should treat unknown values like ``"note"``.
    # Known values produced today (non-exhaustive): ``note``, ``reply``,
    # ``read``, ``human_review_approved``, ``plan_approved``,
    # ``plan_review_approved``, ``plan_review_denied``,
    # ``proposal_accepted``, ``proposal_rejected``,
    # ``readiness_warning``, ``rollup_item``.
    entry_type: str = "note"


class TaskRelationships(BaseModel):
    parent: str | None = None
    children: list[str] | None = None
    blocks: list[str] | None = None
    blocked_by: list[str] | None = None
    relates_to: list[str] | None = None
    supersedes: str | None = None
    superseded_by: str | None = None


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


class PlanJudgmentCall(BaseModel):
    point: str


class Plan(BaseModel):
    task_id: str
    version: int = Field(ge=1)
    predecessor_task_id: str | None = None
    summary: str
    judgment_calls: list[PlanJudgmentCall]
    body: str
    critic_synthesis: str | None = None
    created_at: datetime


class TaskDetail(TaskSummary):
    description: str
    acceptance_criteria: str | None = None
    constraints: str | None = None
    labels: list[str] | None = None
    relevant_files: list[str] | None = None
    relationships: TaskRelationships
    flow_template_id: str | None = None
    flow_template_version: int | None = None
    requires_human_review: bool | None = None
    predecessor_task_id: str | None = None
    transitions: list[Transition]
    executions: list[FlowNodeExecution]
    context: list[ContextEntry] | None = None
    external_refs: dict[str, str] | None = None
    total_input_tokens: int | None = None
    total_output_tokens: int | None = None
    session_count: int | None = None
    created_at: datetime | None = None
    created_by: str | None = None
    plan: Plan | None = None


class TaskListWarning(BaseModel):
    """Per-project partial-failure notice on the cross-project task list.

    Round 1 of Codex review on PR #2067 flagged that silently skipping
    a project on backing-store failure makes the flat list look
    complete when it is not (operator can't tell that pg outage on one
    project is hiding live work behind 200). The aggregator now
    surfaces every skipped project here so clients can render the
    partial-failure surface explicitly.
    """

    project: str
    error: str  # short code, e.g. "service_unavailable"


class TaskListResponse(BaseModel):
    items: list[TaskSummary]
    next_cursor: str | None = None
    # Empty/absent means every project read succeeded. One entry per
    # project whose backing store raised during this request — see
    # ``TaskListWarning`` for the rationale.
    warnings: list[TaskListWarning] | None = None


# ---------------------------------------------------------------------------
# Task transition / edit request + response shapes (Phase 2 — #1548)
# ---------------------------------------------------------------------------


class TaskClaimRequest(BaseModel):
    """Body for ``POST /tasks/{project}/{n}/claim``.

    Spec §5.3 documents ``{assignee, actor}``; this PR keeps the
    surface tight to ``actor`` (the assigning user) — the
    work-service derives the resulting ``assignee`` from the task's
    flow + roles. A separate ``/reassign`` endpoint covers "change
    owner" semantics.
    """

    actor: str = Field(min_length=1)


class TaskCancelRequest(BaseModel):
    """Body for ``POST /tasks/{project}/{n}/cancel``.

    ``reason`` is optional per spec §5.3; absent reasons resolve to
    ``"cancelled via API"`` in the audit row so grep stays meaningful.
    """

    reason: str | None = None


class TaskReassignRequest(BaseModel):
    """Body for ``POST /tasks/{project}/{n}/reassign``.

    Sets the task's ``assignee`` to ``actor``. ``null`` is not yet
    supported (spec leaves "null ⇒ unassign" open; we'd need a second
    column setter for that and the use-case is rare today).
    """

    actor: str = Field(min_length=1)


class TaskPatchRequest(BaseModel):
    """Body for ``PATCH /tasks/{project}/{n}``.

    Per spec §5.4 — selective field updates. Lists replace (not
    merge). Unrecognised statuses raise 422; statuses that aren't
    reachable via the work-service's direct setters (e.g.
    ``in_progress``, ``review``) also raise 422 with a hint pointing
    to the dedicated transition endpoint.
    """

    labels: list[str] | None = None
    status: str | None = None
    metadata: dict[str, str] | None = None


class TaskActionResult(BaseModel):
    """Wrapper envelope for task mutations — ``{ok, message, task}``.

    Spec §5.3 specifies this exact shape so the client can refresh
    its UI without a follow-up ``GET``. ``message`` is informational
    only (operator-facing); clients should route on ``task.work_status``
    instead of parsing the string.
    """

    ok: bool
    message: str | None = None
    task: TaskDetail


class ProjectDrilldown(Project):
    recent_activity: list[ProjectActivityEntry]
    top_tasks: list[TaskSummary]
    plan_review: Plan | None = None


# ---------------------------------------------------------------------------
# Inbox
# ---------------------------------------------------------------------------


InboxItemTypeStr = str  # open enum per spec §7
InboxItemStateStr = Literal[
    "open", "threaded", "waiting-on-pa", "waiting-on-pm", "resolved", "closed"
]
InboxOwnerStr = Literal["pm", "pa", "worker", "operator"]


class InboxItem(BaseModel):
    id: str
    project: str
    type: InboxItemTypeStr
    state: InboxItemStateStr
    subject: str
    preview: str | None = None
    owner: InboxOwnerStr
    thread_id: str | None = None
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any] | None = None


class InboxMessage(BaseModel):
    id: str
    sender: str
    timestamp: datetime
    body: str


class InboxItemDetail(InboxItem):
    messages: list[InboxMessage]


class InboxListResponse(BaseModel):
    items: list[InboxItem]
    next_cursor: str | None = None


# ---------------------------------------------------------------------------
# Inbox write requests (Phase 2 — #1548, spec §4.1)
# ---------------------------------------------------------------------------


class InboxArchiveRequest(BaseModel):
    """Body for ``POST /inbox/{id}/archive``.

    ``reason`` is optional metadata recorded in the audit trail so the
    operator can later answer "why did this disappear?". The cockpit
    archive action doesn't require one.
    """

    reason: str | None = None


class InboxSnoozeRequest(BaseModel):
    """Body for ``POST /inbox/{id}/snooze``.

    Exactly one of ``duration_seconds`` or ``until`` must be supplied.
    ``duration_seconds`` is the simpler shape for clients ("snooze for
    1h"); ``until`` lets the caller pin a wall-clock wake time.
    """

    duration_seconds: int | None = Field(default=None, ge=1)
    until: datetime | None = None
    reason: str | None = None


class InboxPromoteRequest(BaseModel):
    """Body for ``POST /inbox/{id}/promote-to-task``.

    ``project`` overrides the destination project (default: same as
    source). ``prompt`` becomes the new task's description; if omitted
    we fall back to the source item's subject + preview.
    """

    project: str | None = None
    prompt: str | None = None
    title: str | None = None


class InboxMarkReadRequest(BaseModel):
    """Body for ``POST /inbox/{id}/mark-read`` (empty body allowed)."""

    actor: str | None = None


class InboxReplyRequest(BaseModel):
    """Body for ``POST /inbox/{id}/reply``.

    Mirrors the spec's snake_case shape; ``body`` carries the reply
    text. ``owner`` is open metadata so the cockpit can tag who's
    talking (defaults to ``operator``).
    """

    body: str = Field(min_length=1)
    owner: InboxOwnerStr | None = None


# ---------------------------------------------------------------------------
# Events (SSE payload)
# ---------------------------------------------------------------------------


class Event(BaseModel):
    schema_: int = Field(default=1, alias="schema")
    ts: datetime
    project: str
    event: str
    subject: str
    actor: str
    status: str
    metadata: dict[str, Any] | None = None

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Storage (Phase 2 §12) — disk-usage report for ~/.pollypm/.
# ---------------------------------------------------------------------------


class StorageEntry(BaseModel):
    """One row of the storage report — totals for a single subdir.

    Mirrors :class:`pollypm.cli_features.storage.DirScan` on the wire.
    ``cap_hit=True`` means the scan stopped early at the
    ``_SCAN_FILE_CAP`` guard (snapshots/ only today) and ``files`` /
    ``bytes`` are lower bounds; NOTES carries the human-facing flag.
    """

    name: str
    files: int = 0
    bytes: int = 0
    oldest_mtime: datetime | None = None
    newest_mtime: datetime | None = None
    cap_hit: bool = False
    note: str = ""


class StorageConfigFiles(BaseModel):
    """Top-level ``~/.pollypm/*`` config files (TOML, state.db, pid, …).

    Separated from subdirs on the report so the bytes column doesn't
    distort the sort order — mirrors the CLI's ``config files`` row.
    """

    files: int = 0
    bytes: int = 0
    newest_mtime: datetime | None = None


class StorageReport(BaseModel):
    """Whole ``~/.pollypm/`` storage report.

    Mirrors ``pm storage report --json`` (#2040). Phase 2 §12 keeps
    prune off-API; this surface is read-only.
    """

    home: str
    generated_at: datetime
    total_files: int = 0
    total_bytes: int = 0
    subdirs: list[StorageEntry] = Field(default_factory=list)
    config_files: StorageConfigFiles = Field(default_factory=StorageConfigFiles)


__all__ = [
    "ActionResult",
    "Artifact",
    "ContextEntry",
    "DoctorCheck",
    "DoctorResponse",
    "ErrorBody",
    "ErrorResponse",
    "Event",
    "FlowNodeExecution",
    "HealthResponse",
    "InboxArchiveRequest",
    "InboxItem",
    "InboxItemDetail",
    "InboxListResponse",
    "InboxMarkReadRequest",
    "InboxMessage",
    "InboxPromoteRequest",
    "InboxReplyRequest",
    "InboxSnoozeRequest",
    "Plan",
    "PlanJudgmentCall",
    "Project",
    "ProjectActivityEntry",
    "ProjectDrilldown",
    "ProjectListResponse",
    "StorageConfigFiles",
    "StorageEntry",
    "StorageReport",
    "TaskActionResult",
    "TaskCancelRequest",
    "TaskClaimRequest",
    "TaskDetail",
    "TaskListResponse",
    "TaskListWarning",
    "TaskPatchRequest",
    "TaskReassignRequest",
    "TaskRelationships",
    "TaskSummary",
    "Transition",
    "ValidationErrorDetail",
    "ValidationErrorResponse",
    "WorkOutput",
]
