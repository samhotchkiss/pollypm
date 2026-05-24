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
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Common
# ---------------------------------------------------------------------------


class ErrorBody(BaseModel):
    code: str
    message: str
    hint: str | None = None
    retry_after_seconds: int | None = None


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
    auth_mode: Literal["tailnet_trust", "bearer_only"]
    tailnet_trust_enabled: bool


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
    claimed_by_session: str | None = None
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


class TaskListPartialFailureWarning(BaseModel):
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


class TaskListFilteredWarning(BaseModel):
    """Notice that the list omitted untracked task rows by design."""

    code: Literal["untracked_filtered"]
    dropped_count: int = Field(ge=1)
    reason: Literal["untracked_projects"]


TaskListWarning: TypeAlias = (
    TaskListPartialFailureWarning | TaskListFilteredWarning
)


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

    ``actor`` is the session-level claimant identity recorded on the
    response/task as ``claimed_by_session``. The work-service still
    derives the resulting ``assignee`` from the task's flow + roles.
    A separate ``/reassign`` endpoint covers "change owner" semantics.

    ``extra="forbid"`` (Codex round-12, #2064): without it Pydantic
    silently drops unsupported keys (e.g. an ``assignee`` field a
    client sends thinking ``/claim`` accepts the spec §5.3 shape),
    and the request is processed as if the field were never sent.
    Forbidding extras turns those typos / unsupported fields into a
    ``422 Unprocessable Entity`` from FastAPI's request validator so
    the client sees the contract mismatch immediately. Mirrors
    ``TaskPatchRequest`` below.
    """

    model_config = {"extra": "forbid"}

    actor: str = Field(
        min_length=1,
        description=(
            "Session-level claimant identity recorded as "
            "`claimed_by_session`; does not override the role-derived "
            "`assignee`."
        ),
    )


class TaskCancelRequest(BaseModel):
    """Body for ``POST /tasks/{project}/{n}/cancel``.

    ``reason`` is optional per spec §5.3; absent reasons resolve to
    ``"cancelled via API"`` in the audit row so grep stays meaningful.

    ``extra="forbid"`` (Codex round-12, #2064): see ``TaskClaimRequest``
    above — keep the request surface tight so misspelled / unsupported
    fields surface as 422 instead of being silently dropped.
    """

    model_config = {"extra": "forbid"}

    reason: str | None = None


class TaskReopenRequest(BaseModel):
    """Body for ``POST /tasks/{project}/{n}/reopen``."""

    model_config = {"extra": "forbid"}

    reason: str | None = None


class TaskReassignRequest(BaseModel):
    """Body for ``POST /tasks/{project}/{n}/reassign``.

    Sets the task's ``assignee`` to ``actor``. ``null`` is not yet
    supported (spec leaves "null ⇒ unassign" open; we'd need a second
    column setter for that and the use-case is rare today).

    ``extra="forbid"`` (Codex round-12, #2064): see ``TaskClaimRequest``
    above — keep the request surface tight so misspelled / unsupported
    fields surface as 422 instead of being silently dropped.
    """

    model_config = {"extra": "forbid"}

    actor: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Lifecycle REST verbs (#2137) — done / approve / hold / rework / block /
# review / in_progress. Each mirrors the existing claim/cancel pattern:
# ``actor`` for the operator identity; optional ``reason`` recorded on the
# transition row; ``extra='forbid'`` so misspelled keys surface as 422
# instead of being silently dropped.
# ---------------------------------------------------------------------------


class TaskDoneRequest(BaseModel):
    """Body for ``POST /tasks/{project}/{n}/done``.

    Operator "force done" gesture. Maps to
    :meth:`PgWorkService.mark_done` — moves the task directly to
    ``done`` without requiring a flow ``work_output``. The CLI path
    (``pm task done``) uses :meth:`node_done` which advances via the
    flow and requires an output payload; the Web UI exposes the
    simpler bypass for operators.
    """

    model_config = {"extra": "forbid"}

    actor: str = Field(min_length=1)


class TaskApproveRequest(BaseModel):
    """Body for ``POST /tasks/{project}/{n}/approve``."""

    model_config = {"extra": "forbid"}

    actor: str = Field(min_length=1)
    reason: str | None = None


class TaskHoldRequest(BaseModel):
    """Body for ``POST /tasks/{project}/{n}/hold``."""

    model_config = {"extra": "forbid"}

    actor: str = Field(min_length=1)
    reason: str | None = None


class TaskReworkRequest(BaseModel):
    """Body for ``POST /tasks/{project}/{n}/rework``.

    Maps to :meth:`PgWorkService.reject` — bounces a review-state task
    back to ``rework``. The work-service requires a non-empty
    ``reason``; we enforce ``min_length=1`` on the wire so a missing
    reason surfaces as 422 from the request validator instead of as a
    work-service ``ValidationError``.
    """

    model_config = {"extra": "forbid"}

    actor: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class TaskBlockRequest(BaseModel):
    """Body for ``POST /tasks/{project}/{n}/block``.

    ``blocker_task_id`` names the task this one is blocked on
    (``project/n`` format). The work-service inserts a ``blocks``
    dependency edge and flips the task's status to ``blocked``.
    """

    model_config = {"extra": "forbid"}

    actor: str = Field(min_length=1)
    blocker_task_id: str = Field(min_length=1)


class TaskReviewRequest(BaseModel):
    """Body for ``POST /tasks/{project}/{n}/review``.

    Forces an ``in_progress`` task into ``review`` without going
    through ``node_done``. Useful when the operator wants to mark work
    ready for review from the Web UI without supplying a flow
    ``work_output`` payload.
    """

    model_config = {"extra": "forbid"}

    actor: str = Field(min_length=1)


class TaskInProgressRequest(BaseModel):
    """Body for ``POST /tasks/{project}/{n}/in_progress``.

    Forces a task into ``in_progress`` from a non-terminal state.
    Maps to :meth:`PgWorkService.resume` when the source is
    ``on_hold``; otherwise a direct transition via the work-service's
    simple-transition helper.
    """

    model_config = {"extra": "forbid"}

    actor: str = Field(min_length=1)


class TaskPatchRequest(BaseModel):
    """Body for ``PATCH /tasks/{project}/{n}``.

    Per spec §5.4 — selective field updates. Lists replace (not
    merge). Unrecognised statuses raise 422; statuses that aren't
    reachable via the work-service's direct setters (e.g.
    ``in_progress``, ``review``) also raise 422 with a hint pointing
    to the dedicated transition endpoint.

    ``extra="forbid"`` (Codex round-6, #2064): without it Pydantic
    silently drops misspelled keys (e.g. ``metdata``) and forwards an
    all-``None`` body, so the route returns ``200 ok`` even though
    nothing changed. Forbidding extras turns typos / unsupported
    fields like ``priority`` into a ``422 Unprocessable Entity`` from
    FastAPI's request validator, giving the client an actionable
    error instead of a silent no-op.
    """

    model_config = {"extra": "forbid"}

    labels: list[str] | None = None
    status: str | None = None
    metadata: dict[str, str] | None = None


class TaskActionResult(BaseModel):
    """Wrapper envelope for task mutations — ``{ok, message, task, warnings}``.

    Spec §5.3 specifies this exact shape so the client can refresh
    its UI without a follow-up ``GET``. ``message`` is informational
    only (operator-facing); clients should route on ``task.work_status``
    instead of parsing the string.

    ``warnings`` is a (possibly empty) list of operator-facing strings.
    Today the claim path uses it to surface ``last_provision_error``
    when the DB transition committed but the per-task worker session
    failed to provision — the task is ``in_progress`` with no live
    agent lane, and the operator needs to recover manually. This
    mirrors the CLI's stderr warning at
    ``src/pollypm/work/cli.py:912-929`` so the API and ``pm task
    claim`` give the same recovery story (#2064 round-10).
    """

    ok: bool
    message: str | None = None
    task: TaskDetail
    warnings: list[str] = Field(
        default_factory=list,
        description=(
            "Operator-facing warnings about side effects of the "
            "transition that did not fail the request — e.g. the "
            "claim path surfaces ``last_provision_error`` here when "
            "the DB claim committed but the worker session did not "
            "provision."
        ),
    )


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
    "TaskApproveRequest",
    "TaskBlockRequest",
    "TaskCancelRequest",
    "TaskClaimRequest",
    "TaskDetail",
    "TaskDoneRequest",
    "TaskHoldRequest",
    "TaskInProgressRequest",
    "TaskListFilteredWarning",
    "TaskListPartialFailureWarning",
    "TaskListResponse",
    "TaskListWarning",
    "TaskPatchRequest",
    "TaskReassignRequest",
    "TaskReopenRequest",
    "TaskRelationships",
    "TaskReviewRequest",
    "TaskReworkRequest",
    "TaskSummary",
    "Transition",
    "ValidationErrorDetail",
    "ValidationErrorResponse",
    "WorkOutput",
]
