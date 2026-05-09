"""Project read endpoints (Phase 1).

Implements:

- ``GET /api/v1/projects`` — list registered projects.
- ``GET /api/v1/projects/{key}`` — drilldown view.
- ``GET /api/v1/projects/{key}/tasks`` — paginated task list.
- ``GET /api/v1/projects/{key}/plan`` — structured plan body.

Write endpoints (`POST /projects`, `POST /projects/{key}/plan`,
`POST /projects/{key}/chat`) ship in Phase 2 (#1548).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import Field

from pollypm.web_api.errors import not_found
from pollypm.web_api.models import (
    Plan,
    ProjectDrilldown,
    ProjectListResponse,
    TaskListResponse,
)
from pollypm.web_api.routes._deps import ConfigDep
from pollypm.web_api.service import (
    get_active_plan,
    list_project_tasks,
    list_projects,
    project_drilldown,
)

router = APIRouter(tags=["Projects"])


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
