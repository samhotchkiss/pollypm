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
    entry_type: Literal["note", "reply", "read"]


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


class TaskListResponse(BaseModel):
    items: list[TaskSummary]
    next_cursor: str | None = None


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


__all__ = [
    "Artifact",
    "ContextEntry",
    "DoctorCheck",
    "DoctorResponse",
    "ErrorBody",
    "ErrorResponse",
    "Event",
    "FlowNodeExecution",
    "HealthResponse",
    "InboxItem",
    "InboxItemDetail",
    "InboxListResponse",
    "InboxMessage",
    "Plan",
    "PlanJudgmentCall",
    "Project",
    "ProjectActivityEntry",
    "ProjectDrilldown",
    "ProjectListResponse",
    "TaskDetail",
    "TaskListResponse",
    "TaskRelationships",
    "TaskSummary",
    "Transition",
    "ValidationErrorDetail",
    "ValidationErrorResponse",
    "WorkOutput",
]
