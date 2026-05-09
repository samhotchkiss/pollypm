"""Task read endpoints (Phase 1).

Implements ``GET /api/v1/tasks/{project}/{n}``. Approve / reject /
queue land in Phase 2 (#1548).
"""

from __future__ import annotations

from fastapi import APIRouter

from pollypm.web_api.errors import not_found
from pollypm.web_api.models import TaskDetail
from pollypm.web_api.routes._deps import ConfigDep
from pollypm.web_api.service import get_task_detail

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
