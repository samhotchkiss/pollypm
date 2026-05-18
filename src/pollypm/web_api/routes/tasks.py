"""Task endpoints.

Phase 1 (#1547) implemented ``GET /api/v1/tasks/{project}/{n}``.

Phase 2 (#1548) is layering in the write surface; ``POST .../queue``
is the wedge — no request body, no kind discriminator, no validation
beyond the existing path params, so it exercises every piece of the
Phase 2 scaffolding (work-service factory, typed errors, ActionResult
envelope, Idempotency-Key plumbing) without dragging in the
plan/code-review state machine. ``approve`` / ``reject`` follow once
the wedge is in.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header

from pollypm.web_api.errors import not_found
from pollypm.web_api.models import ActionResult, TaskDetail
from pollypm.web_api.routes._deps import ConfigDep
from pollypm.web_api.service import get_task_detail, queue_task

router = APIRouter(tags=["Tasks"])


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
    },
)
def queue_task_endpoint(
    project: str,
    n: int,
    config: ConfigDep,
    # ``Idempotency-Key`` is accepted in Phase 2 per the issue scope
    # ("OPTIONAL in Phase 2 — actual replay-cache persistence ships in
    # Phase 3"). We don't dedupe yet; declaring the header keeps the
    # OpenAPI contract honest and lets clients send it from day one.
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ActionResult:
    # The work-service knows how to look up the task; we still
    # short-circuit on an unregistered project so the 404 message is
    # specific to the project (matches the GET endpoint above).
    if project not in config.projects:
        raise not_found(f"Project not registered: {project}")
    task = queue_task(config, project, n)
    return ActionResult(ok=True, message=f"queued {task.task_id}")
