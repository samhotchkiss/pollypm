"""Task endpoints.

Phase 1 (#1547) implemented ``GET /api/v1/tasks/{project}/{n}``.

Phase 2 (#1548) is layering in the write surface; ``POST .../queue``
is the wedge — no request body, no kind discriminator, no validation
beyond the existing path params, so it exercises every piece of the
Phase 2 scaffolding (work-service factory, typed errors, ActionResult
envelope, Idempotency-Key plumbing) without dragging in the
plan/code-review state machine. ``approve`` / ``reject`` follow once
the wedge is in.

Phase 2 surface #3 (#1548 spec §5.3 + §5.4) adds the remaining
task-state-mutation verbs: ``/claim``, ``/cancel``, ``/reopen``,
``/reassign`` and the ``PATCH`` edit surface. Each new handler returns
``TaskActionResult = {ok, message, task: TaskDetail}`` so clients
refresh state in one round-trip (spec §5.3 wrapper).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from pollypm.web_api.errors import APIError, invalid_request, not_found
from pollypm.web_api.models import (
    ActionResult,
    TaskActionResult,
    TaskApproveRequest,
    TaskBlockRequest,
    TaskCancelRequest,
    TaskClaimRequest,
    TaskDetail,
    TaskDoneRequest,
    TaskHoldRequest,
    TaskInProgressRequest,
    TaskListResponse,
    TaskPatchRequest,
    TaskReassignRequest,
    TaskReopenRequest,
    TaskReviewRequest,
    TaskReworkRequest,
)
from pollypm.web_api.routes._deps import ConfigDep
from pollypm.web_api.service import (
    StaleCursorError,
    approve_task,
    block_task,
    cancel_task,
    claim_task,
    done_task,
    get_task_detail,
    hold_task,
    in_progress_task,
    list_all_tasks,
    patch_task,
    queue_task,
    reassign_task,
    reopen_task,
    review_task,
    rework_task,
)

router = APIRouter(tags=["Tasks"])


@router.get(
    "/tasks",
    response_model=TaskListResponse,
    summary="Flat list of tasks across all projects",
    operation_id="listTasks",
)
def list_tasks_endpoint(
    config: ConfigDep,
    project: Annotated[str | None, Query(description="Filter to a single project key.")] = None,
    # Repeatable ``status=`` per spec §5.1 — FastAPI maps a list-typed
    # Query into ``?status=draft&status=queued`` (OR semantics on the
    # service side).
    status: Annotated[list[str] | None, Query(description="Filter by work_status (repeatable).")] = None,
    assignee: Annotated[str | None, Query(description="Filter by exact assignee.")] = None,
    since: Annotated[
        str | None,
        Query(description="ISO-8601 lower bound on updated_at (strictly after)."),
    ] = None,
    include_untracked: Annotated[
        bool,
        Query(
            description=(
                "Include task rows for projects outside the tracked-project "
                "set. Defaults to false so existing dashboards stay scoped."
            ),
        ),
    ] = False,
    limit: Annotated[int, Query(ge=1, le=200, description="Page size (capped at 200).")] = 50,
    cursor: Annotated[str | None, Query(description="Opaque cursor from next_cursor.")] = None,
) -> TaskListResponse:
    since_dt: datetime | None = None
    if since is not None:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError as exc:
            # Spec §6: malformed query → 400 invalid_request with a
            # hint about the expected format, not 422. The body shape
            # is fine; the *value* is unparseable, but the
            # invalid_request distinction matches how the other
            # endpoints surface bad timestamps (e.g. /events).
            raise APIError(
                status_code=400,
                code="invalid_request",
                message=f"Invalid `since` value: {since!r}",
                hint="Use ISO-8601 (e.g. 2026-05-22T00:00:00Z).",
            ) from exc
        # P0 #2 (PR #2067 Codex round 1): task ``updated_at`` is
        # offset-aware in pg, so a naive ``since`` would trigger
        # ``TypeError: can't compare offset-naive and offset-aware``
        # inside ``list_all_tasks`` and surface as a 500 on
        # syntactically-valid input. Reject naive timestamps with a
        # typed 400 — explicit is better than implicit timezone
        # assumptions.
        if since_dt.tzinfo is None or since_dt.tzinfo.utcoffset(since_dt) is None:
            raise APIError(
                status_code=400,
                code="invalid_request",
                message=f"`since` must include a timezone offset: {since!r}",
                hint="Use an offset suffix (e.g. 2026-05-22T00:00:00Z or +00:00).",
            )

    try:
        items, next_cursor, warnings, total = list_all_tasks(
            config,
            project=project,
            statuses=status,
            assignee=assignee,
            since=since_dt,
            include_untracked=include_untracked,
            limit=limit,
            cursor=cursor,
        )
    except StaleCursorError as exc:
        # P0 #3 (PR #2067 Codex round 1): a cursor that no longer
        # matches any current item used to silently restart at page
        # one, creating duplicate rows / infinite-pagination loops
        # under concurrent updates. Surface it as a typed 400 so the
        # client restarts without a cursor explicitly.
        raise APIError(
            status_code=400,
            code="invalid_request",
            message="Cursor is stale (item no longer exists or has been updated).",
            hint="Retry the request without the `cursor` parameter to restart paging.",
        ) from exc

    return TaskListResponse(
        items=items,
        total=total,
        has_more=next_cursor is not None,
        next_cursor=next_cursor,
        warnings=warnings or None,
    )


@router.get(
    "/tasks/{project}/{n}",
    response_model=TaskDetail,
    summary="Task detail",
    operation_id="getTask",
)
def get_task_endpoint(project: str, n: int, config: ConfigDep) -> TaskDetail:
    if project not in config.projects:
        raise not_found(f"Project not registered: {project}")
    detail = get_task_detail(config, project, n)
    if detail is None:
        raise not_found(f"Task not found: {project}/{n}")
    return detail


# ---------------------------------------------------------------------------
# Phase 2 — write endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/tasks/{project}/{n}/queue",
    response_model=ActionResult,
    summary="Queue a draft task for execution",
    operation_id="queueTask",
    responses={
        "401": {"description": "Missing or invalid bearer token."},
        "404": {"description": "Project or task not found."},
        "409": {"description": "Task is not in a queueable state."},
        # #2064 round-9 blocker #3: the service helper catches
        # ``_BACKING_STORE_ERRORS`` and raises ``service_unavailable``;
        # advertise that here so generated clients branch on the same
        # 503 envelope they'll see on a real pg outage.
        "503": {"description": "Backing store unavailable."},
    },
)
def queue_task_endpoint(
    project: str,
    n: int,
    config: ConfigDep,
) -> ActionResult:
    # The work-service knows how to look up the task; we still
    # short-circuit on an unregistered project so the 404 message is
    # specific to the project (matches the GET endpoint above).
    if project not in config.projects:
        raise not_found(f"Project not registered: {project}")
    task = queue_task(config, project, n)
    return ActionResult(ok=True, message=f"queued {task.task_id}")


# ---------------------------------------------------------------------------
# Phase 2 surface #3 — claim / cancel / reassign / PATCH
#
# Each handler short-circuits on an unregistered project so the 404
# message is project-specific (consistent with the GET / queue
# handlers). ``Idempotency-Key`` and ``If-Match`` are intentionally
# NOT declared on these handlers — no replay cache or version-token
# enforcement exists yet (mirrors #2060 round-1 decision for the
# inbox writes). Advertising them but discarding them would lie to
# clients: a lost-response retry would 409 instead of replaying, and
# concurrent edits would race silently. The headers will reappear
# once a real replay store + ``If-Match`` enforcement ships.
# ---------------------------------------------------------------------------


@router.post(
    "/tasks/{project}/{n}/claim",
    response_model=TaskActionResult,
    summary="Atomically claim a queued task",
    operation_id="claimTask",
    responses={
        "401": {"description": "Missing or invalid bearer token."},
        "404": {"description": "Project or task not found."},
        "409": {"description": "Task is not in a claimable state."},
        "422": {"description": "Claim gate failure."},
        "429": {
            "description": (
                "Worker cap exceeded — normal back-pressure from "
                "`max_parallel_workers`. Retry once a slot frees."
            ),
        },
        "503": {"description": "Backing store unavailable."},
    },
)
def claim_task_endpoint(
    project: str,
    n: int,
    body: TaskClaimRequest,
    config: ConfigDep,
) -> TaskActionResult:
    if project not in config.projects:
        raise not_found(f"Project not registered: {project}")
    # ``claim_task`` returns ``(task, warnings)`` — warnings carry
    # post-commit operator advisories like ``last_provision_error``
    # and SessionManager attach failures (#2064 round-10). They never
    # fail the request; the envelope's ``warnings`` field lets the
    # client surface a banner alongside the ``in_progress`` task.
    task, warnings = claim_task(config, project, n, actor=body.actor)
    # #2064 round-11 blocker #2: the message must reflect the actual
    # post-claim state. The post-commit cap race
    # (``pg_service.py:1917-1939``) can roll the row back to
    # ``queued`` after the initial transition — saying "claimed
    # myproj/3" then would lie. Inspect ``task.work_status`` instead
    # of assuming success.
    if task.work_status == "queued":
        message = (
            f"claim attempted but rolled back; task "
            f"{task.task_id} remains queued"
        )
    else:
        message = f"claimed {task.task_id}"
    return TaskActionResult(
        ok=True,
        message=message,
        task=task,
        warnings=warnings,
    )


@router.post(
    "/tasks/{project}/{n}/cancel",
    response_model=TaskActionResult,
    summary="Cancel a non-terminal task",
    operation_id="cancelTask",
    responses={
        "401": {"description": "Missing or invalid bearer token."},
        "404": {"description": "Project or task not found."},
        "409": {"description": "Task is already in a terminal state."},
        "503": {"description": "Backing store unavailable."},
    },
)
def cancel_task_endpoint(
    project: str,
    n: int,
    config: ConfigDep,
    body: TaskCancelRequest | None = None,
    force: Annotated[
        bool,
        Query(
            description=(
                "Confirm cancellation of an in-progress task. Without "
                "force, active-worker cancellation returns 409."
            )
        ),
    ] = False,
) -> TaskActionResult:
    if project not in config.projects:
        raise not_found(f"Project not registered: {project}")
    reason = body.reason if body is not None else None
    task = cancel_task(config, project, n, reason=reason, force=force)
    return TaskActionResult(
        ok=True, message=f"cancelled {task.task_id}", task=task
    )


@router.post(
    "/tasks/{project}/{n}/reopen",
    response_model=TaskActionResult,
    summary="Reopen a cancelled task",
    operation_id="reopenTask",
    responses={
        "401": {"description": "Missing or invalid bearer token."},
        "404": {"description": "Project or task not found."},
        "409": {"description": "Task is not cancelled."},
        "503": {"description": "Backing store unavailable."},
    },
)
def reopen_task_endpoint(
    project: str,
    n: int,
    config: ConfigDep,
    body: TaskReopenRequest | None = None,
) -> TaskActionResult:
    if project not in config.projects:
        raise not_found(f"Project not registered: {project}")
    reason = body.reason if body is not None else None
    task = reopen_task(config, project, n, reason=reason)
    return TaskActionResult(
        ok=True, message=f"reopened {task.task_id}", task=task
    )


@router.post(
    "/tasks/{project}/{n}/reassign",
    response_model=TaskActionResult,
    summary="Change the task's assignee",
    operation_id="reassignTask",
    responses={
        "401": {"description": "Missing or invalid bearer token."},
        "404": {"description": "Project or task not found."},
        # #2064 round-9 blocker #4: reassign now refuses
        # terminal / draft tasks (live-worker-swap invariant) with
        # a 409 invalid_state, matching ``/claim`` and ``/cancel``.
        "409": {
            "description": (
                "Task is in a state that does not permit a worker swap "
                "(draft / done / cancelled)."
            ),
        },
        "422": {"description": "Assignee value rejected."},
        "503": {"description": "Backing store unavailable."},
    },
)
def reassign_task_endpoint(
    project: str,
    n: int,
    body: TaskReassignRequest,
    config: ConfigDep,
) -> TaskActionResult:
    if project not in config.projects:
        raise not_found(f"Project not registered: {project}")
    task = reassign_task(config, project, n, actor=body.actor)
    return TaskActionResult(
        ok=True,
        message=f"reassigned {task.task_id} to {body.actor}",
        task=task,
    )


@router.patch(
    "/tasks/{project}/{n}",
    response_model=TaskActionResult,
    summary="Selective task edits (labels, status, metadata)",
    operation_id="patchTask",
    responses={
        "400": {
            "description": (
                "Cannot combine `status` with other mutable fields in a "
                "single PATCH."
            ),
        },
        "401": {"description": "Missing or invalid bearer token."},
        "404": {"description": "Project or task not found."},
        "409": {"description": "Status transition refused by the state machine."},
        "422": {"description": "Body validation / unsupported status."},
        "503": {"description": "Backing store unavailable."},
    },
)
def patch_task_endpoint(
    project: str,
    n: int,
    body: TaskPatchRequest,
    config: ConfigDep,
) -> TaskActionResult:
    if project not in config.projects:
        raise not_found(f"Project not registered: {project}")
    # No-op guard (#2064 round-6): ``TaskPatchRequest`` makes every
    # field optional so the wire shape can carry "just labels" or
    # "just status" without sending the others. An all-``None`` body
    # has no observable effect on the task, but the handler would
    # still ``svc.get(...)`` and respond ``200 ok`` — masking client
    # bugs (e.g. a frontend that forgot to attach the form payload).
    # Refuse the empty shape up front with the same typed-error
    # helper the combined-shape check below uses.
    if body.labels is None and body.status is None and body.metadata is None:
        raise invalid_request(
            (
                "PATCH body must include at least one of: "
                "`labels`, `status`, `metadata`."
            ),
            hint=(
                "Send the field you want to change; an empty body "
                "or one with only `null` values is rejected to "
                "surface client-side payload bugs."
            ),
        )
    # Atomicity contract (#2064 round-2): PATCH cannot combine
    # ``status`` with labels/metadata. ``svc.update(...)`` and the
    # lifecycle methods (``svc.queue`` / ``svc.cancel``) commit in
    # separate transactions, so a concurrent writer can flip the
    # task's status between the in-memory preflight and the
    # lifecycle call — leaving labels/metadata committed while the
    # status write 409s. Refuse the combined shape up front; clients
    # should send one PATCH per concern, or use the dedicated
    # ``/queue`` / ``/cancel`` / ``/reopen`` / ``/claim`` /
    # ``/reassign`` endpoints
    # for status changes. Rejecting BEFORE any work-service call
    # guarantees no partial commit.
    if body.status is not None and (
        body.labels is not None or body.metadata is not None
    ):
        raise invalid_request(
            (
                "PATCH cannot combine `status` with `labels` or "
                "`metadata` in a single request."
            ),
            hint=(
                "Send separate PATCH requests (one for status, one "
                "for the other fields), or use the dedicated status "
                "endpoints (POST /tasks/{project}/{n}/queue, /cancel, "
                "/reopen, /claim, /reassign)."
            ),
        )
    task = patch_task(
        config,
        project,
        n,
        labels=body.labels,
        status=body.status,
        metadata=body.metadata,
    )
    return TaskActionResult(
        ok=True, message=f"patched {task.task_id}", task=task
    )


# ---------------------------------------------------------------------------
# Lifecycle REST verbs (#2137) — done / approve / hold / rework / block /
# review / in_progress. Each mirrors the existing claim/cancel route
# pattern: same auth (ConfigDep), same envelope (TaskActionResult), same
# error shape (404 unknown project/task, 409 invalid_state, 422
# validation, 503 backing-store). ``Idempotency-Key`` and ``If-Match``
# are intentionally NOT declared (no replay cache, no version-token
# enforcement yet — mirrors #2064 round-1 for the existing verbs).
# ---------------------------------------------------------------------------


_LIFECYCLE_RESPONSES = {
    "401": {"description": "Missing or invalid bearer token."},
    "404": {"description": "Project or task not found."},
    "409": {"description": "Task is not in a state that permits this transition."},
    "422": {"description": "Body validation / illegal transition argument."},
    "503": {"description": "Backing store unavailable."},
}


@router.post(
    "/tasks/{project}/{n}/done",
    response_model=TaskActionResult,
    summary="Force a task to done (operator bypass)",
    operation_id="doneTask",
    responses=_LIFECYCLE_RESPONSES,
)
def done_task_endpoint(
    project: str,
    n: int,
    body: TaskDoneRequest,
    config: ConfigDep,
) -> TaskActionResult:
    if project not in config.projects:
        raise not_found(f"Project not registered: {project}")
    task = done_task(config, project, n, actor=body.actor)
    return TaskActionResult(
        ok=True, message=f"done {task.task_id}", task=task
    )


@router.post(
    "/tasks/{project}/{n}/approve",
    response_model=TaskActionResult,
    summary="Approve a review-state task",
    operation_id="approveTask",
    responses=_LIFECYCLE_RESPONSES,
)
def approve_task_endpoint(
    project: str,
    n: int,
    body: TaskApproveRequest,
    config: ConfigDep,
) -> TaskActionResult:
    if project not in config.projects:
        raise not_found(f"Project not registered: {project}")
    task = approve_task(
        config, project, n, actor=body.actor, reason=body.reason
    )
    return TaskActionResult(
        ok=True, message=f"approved {task.task_id}", task=task
    )


@router.post(
    "/tasks/{project}/{n}/hold",
    response_model=TaskActionResult,
    summary="Move a task to on_hold",
    operation_id="holdTask",
    responses=_LIFECYCLE_RESPONSES,
)
def hold_task_endpoint(
    project: str,
    n: int,
    body: TaskHoldRequest,
    config: ConfigDep,
) -> TaskActionResult:
    if project not in config.projects:
        raise not_found(f"Project not registered: {project}")
    task = hold_task(
        config, project, n, actor=body.actor, reason=body.reason
    )
    return TaskActionResult(
        ok=True, message=f"held {task.task_id}", task=task
    )


@router.post(
    "/tasks/{project}/{n}/rework",
    response_model=TaskActionResult,
    summary="Reject a review-state task back to rework",
    operation_id="reworkTask",
    responses=_LIFECYCLE_RESPONSES,
)
def rework_task_endpoint(
    project: str,
    n: int,
    body: TaskReworkRequest,
    config: ConfigDep,
) -> TaskActionResult:
    if project not in config.projects:
        raise not_found(f"Project not registered: {project}")
    task = rework_task(
        config, project, n, actor=body.actor, reason=body.reason
    )
    return TaskActionResult(
        ok=True, message=f"rework {task.task_id}", task=task
    )


@router.post(
    "/tasks/{project}/{n}/block",
    response_model=TaskActionResult,
    summary="Mark a task blocked by another task",
    operation_id="blockTask",
    responses=_LIFECYCLE_RESPONSES,
)
def block_task_endpoint(
    project: str,
    n: int,
    body: TaskBlockRequest,
    config: ConfigDep,
) -> TaskActionResult:
    if project not in config.projects:
        raise not_found(f"Project not registered: {project}")
    task = block_task(
        config,
        project,
        n,
        actor=body.actor,
        blocker_task_id=body.blocker_task_id,
    )
    return TaskActionResult(
        ok=True, message=f"blocked {task.task_id}", task=task
    )


@router.post(
    "/tasks/{project}/{n}/review",
    response_model=TaskActionResult,
    summary="Force a task into review (operator bypass)",
    operation_id="reviewTask",
    responses=_LIFECYCLE_RESPONSES,
)
def review_task_endpoint(
    project: str,
    n: int,
    body: TaskReviewRequest,
    config: ConfigDep,
) -> TaskActionResult:
    if project not in config.projects:
        raise not_found(f"Project not registered: {project}")
    task = review_task(config, project, n, actor=body.actor)
    return TaskActionResult(
        ok=True, message=f"review {task.task_id}", task=task
    )


@router.post(
    "/tasks/{project}/{n}/in_progress",
    response_model=TaskActionResult,
    summary="Force a non-terminal task into in_progress",
    operation_id="inProgressTask",
    responses=_LIFECYCLE_RESPONSES,
)
def in_progress_task_endpoint(
    project: str,
    n: int,
    body: TaskInProgressRequest,
    config: ConfigDep,
) -> TaskActionResult:
    if project not in config.projects:
        raise not_found(f"Project not registered: {project}")
    task = in_progress_task(config, project, n, actor=body.actor)
    return TaskActionResult(
        ok=True, message=f"in_progress {task.task_id}", task=task
    )
