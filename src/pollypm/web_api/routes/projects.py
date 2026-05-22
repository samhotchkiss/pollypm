"""Project endpoints (Phase 1 reads + Phase 2 lifecycle writes).

Implements:

- ``GET /api/v1/projects`` — list registered projects.
- ``GET /api/v1/projects/{key}`` — drilldown view.
- ``GET /api/v1/projects/{key}/tasks`` — paginated task list.
- ``GET /api/v1/projects/{key}/plan`` — structured plan body.
- ``POST /api/v1/projects/{key}/pause`` — mark project ``tracked=false``.
- ``POST /api/v1/projects/{key}/resume`` — mark project ``tracked=true``.
- ``POST /api/v1/projects/{key}/archive`` — remove project from config
  (per spec §6.2; source data on disk is untouched).
- ``POST /api/v1/projects/{key}/init-guide`` — seed a project-local
  role guide (``architect`` / ``reviewer`` / ``worker``).
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from pollypm.web_api.errors import not_found
from pollypm.web_api.models import (
    ActionResult,
    Plan,
    Project,
    ProjectDrilldown,
    ProjectListResponse,
    TaskListResponse,
)
from pollypm.web_api.routes._deps import ConfigDep
from pollypm.web_api.service import (
    archive_project,
    get_active_plan,
    init_project_guide_for_role,
    list_project_tasks,
    list_projects,
    project_drilldown,
    set_project_tracked,
)

router = APIRouter(tags=["Projects"])


# ---------------------------------------------------------------------------
# Read endpoints (Phase 1)
# ---------------------------------------------------------------------------


@router.get(
    "/projects",
    response_model=ProjectListResponse,
    summary="List registered projects",
    operation_id="listProjects",
)
def list_projects_endpoint(
    config: ConfigDep,
    tracked: Annotated[bool | None, Query(description="When true, returns only tracked projects.")] = None,
) -> ProjectListResponse:
    items = list_projects(config, tracked_only=bool(tracked))
    return ProjectListResponse(items=items)


@router.get(
    "/projects/{key}",
    response_model=ProjectDrilldown,
    summary="Project drilldown",
    operation_id="getProject",
)
def get_project_endpoint(key: str, config: ConfigDep) -> ProjectDrilldown:
    drilldown = project_drilldown(config, key)
    if drilldown is None:
        raise not_found(f"Project not registered: {key}")
    return drilldown


@router.get(
    "/projects/{key}/tasks",
    response_model=TaskListResponse,
    summary="List tasks for a project",
    operation_id="listProjectTasks",
)
def list_project_tasks_endpoint(
    key: str,
    config: ConfigDep,
    status: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query()] = None,
) -> TaskListResponse:
    if key not in config.projects:
        raise not_found(f"Project not registered: {key}")
    items, next_cursor = list_project_tasks(
        config, key, status=status, limit=limit, cursor=cursor
    )
    return TaskListResponse(items=items, next_cursor=next_cursor)


@router.get(
    "/projects/{key}/plan",
    response_model=Plan,
    summary="Structured plan body for the active plan-review task",
    operation_id="getProjectPlan",
)
def get_project_plan_endpoint(
    key: str,
    config: ConfigDep,
    version: Annotated[int | None, Query(ge=1)] = None,
) -> Plan:
    if key not in config.projects:
        raise not_found(f"Project not registered: {key}")
    plan = get_active_plan(config, key, version=version)
    if plan is None:
        raise not_found(
            "No plan in review for this project",
            hint="Plans appear here once a task reaches the user_approval node.",
        )
    return plan


# ---------------------------------------------------------------------------
# Phase 2 — write endpoints (pause / resume / archive / init-guide)
# ---------------------------------------------------------------------------


class _ReasonBody(BaseModel):
    """Optional `{reason?}` body shared by pause / archive routes.

    ``reason`` is persisted to the per-project audit log as part of
    the ``projects.tracked.set`` (pause/resume) and ``projects.archive``
    events emitted by the service layer, alongside the actor and
    timestamp. Clamped to 500 chars by ``max_length`` here and re-
    clamped in :func:`pollypm.web_api.service._emit_project_audit` as
    belt-and-suspenders.
    """

    reason: str | None = Field(
        default=None,
        max_length=500,
        description=(
            "Optional operator-supplied note. Persisted to the per-"
            "project audit log (`projects.tracked.set` / "
            "`projects.archive` events) along with the actor and "
            "timestamp. Clamped to 500 chars."
        ),
    )


class _InitGuideBody(BaseModel):
    """Body for ``POST /projects/{key}/init-guide``."""

    role: Literal["architect", "reviewer", "worker"] = Field(
        description="Which role-specific guide to seed.",
    )
    force: bool = Field(
        default=False,
        description="Overwrite an existing guide file. Without this, "
        "existing guides return 409 conflict.",
    )


class InitGuideResponse(BaseModel):
    """Return shape for ``POST /projects/{key}/init-guide``.

    Mirrors :class:`pollypm.project_guides.ProjectGuideInfo` but
    serializes ``path`` as a string so the API stays loop-back-portable.
    """

    role: str
    path: str
    forked_from: str | None = None
    body: str


@router.post(
    "/projects/{key}/pause",
    response_model=Project,
    summary="Pause a project (set tracked=false)",
    operation_id="pauseProject",
    responses={
        "401": {"description": "Missing or invalid bearer token."},
        "404": {"description": "Project not registered."},
        "503": {"description": "Backing store unreachable (failed to persist)."},
    },
)
def pause_project_endpoint(
    key: str,
    config: ConfigDep,
    body: _ReasonBody | None = None,
) -> Project:
    reason = body.reason if body is not None else None
    # Idempotency lives in ``set_project_tracked`` (Codex round 2 on
    # #2063): deciding "already paused" against the long-lived
    # ``ConfigDep`` snapshot was returning 200 with a stale value when
    # disk had been edited externally. The service helper reloads disk
    # first and short-circuits there.
    return set_project_tracked(config, key, tracked=False, reason=reason)


@router.post(
    "/projects/{key}/resume",
    response_model=Project,
    summary="Resume a project (set tracked=true)",
    operation_id="resumeProject",
    responses={
        "401": {"description": "Missing or invalid bearer token."},
        "404": {"description": "Project not registered."},
        "503": {"description": "Backing store unreachable (failed to persist)."},
    },
)
def resume_project_endpoint(
    key: str,
    config: ConfigDep,
) -> Project:
    # Idempotency lives in ``set_project_tracked`` (Codex round 2 on
    # #2063): deciding "already tracked" against the long-lived
    # ``ConfigDep`` snapshot was returning 200 with a stale value when
    # disk had been edited externally. The service helper reloads disk
    # first and short-circuits there.
    return set_project_tracked(config, key, tracked=True)


@router.post(
    "/projects/{key}/archive",
    response_model=ActionResult,
    summary="Archive a project (remove from config)",
    operation_id="archiveProject",
    responses={
        "401": {"description": "Missing or invalid bearer token."},
        "404": {"description": "Project not registered."},
        "409": {
            "description": (
                "Project is still referenced by one or more enabled "
                "sessions; the response body names the blocking sessions "
                "in ``error.message``. Disable or remove those sessions "
                "before retrying."
            )
        },
        "503": {"description": "Backing store unreachable (failed to persist)."},
    },
)
def archive_project_endpoint(
    key: str,
    config: ConfigDep,
    body: _ReasonBody | None = None,
) -> ActionResult:
    reason = body.reason if body is not None else None
    label = archive_project(config, key, reason=reason)
    message = f"archived {label}"
    if reason:
        message = f"{message} ({reason})"
    return ActionResult(ok=True, message=message)


@router.post(
    "/projects/{key}/init-guide",
    response_model=InitGuideResponse,
    summary="Seed a project-local role guide",
    operation_id="initProjectGuide",
    responses={
        "401": {"description": "Missing or invalid bearer token."},
        "404": {"description": "Project not registered."},
        "409": {"description": "Guide already exists; resend with force=true."},
        "422": {"description": "Unknown role."},
    },
)
def init_project_guide_endpoint(
    key: str,
    body: _InitGuideBody,
    config: ConfigDep,
) -> InitGuideResponse:
    payload = init_project_guide_for_role(
        config, key, role=body.role, force=body.force
    )
    return InitGuideResponse(**payload)
