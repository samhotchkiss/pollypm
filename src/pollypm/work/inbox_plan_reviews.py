"""Plan-review approval checks used by inbox surfaces."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from pollypm.work.models import Decision, ExecutionStatus

logger = logging.getLogger(__name__)

PLAN_APPROVAL_NODE_ID = "user_approval"
ServiceFactory = Callable[..., Any]


def task_user_approval_is_approved(task: Any) -> bool:
    """Return True when a task's latest completed user approval is approved."""
    for execution in reversed(getattr(task, "executions", []) or []):
        if getattr(execution, "node_id", None) != PLAN_APPROVAL_NODE_ID:
            continue
        if getattr(execution, "status", None) is not ExecutionStatus.COMPLETED:
            continue
        decision = getattr(execution, "decision", None)
        if decision is Decision.APPROVED:
            return True
        if decision is Decision.REJECTED:
            return False
    return False


def is_plan_task_approved(svc: Any, project: str, task_number: int) -> bool:
    """Return True when ``project/task_number`` has an approved plan review."""
    task_id = f"{project}/{task_number}"
    try:
        task = svc.get(task_id)
    except Exception:  # noqa: BLE001
        logger.debug(
            "inbox: get(%s) failed during plan-review approval check",
            task_id,
            exc_info=True,
        )
        return False
    return task_user_approval_is_approved(task)


def approved_plan_review_refs(
    *,
    refs_by_db: dict[str, set[tuple[str, int]]],
    project_db_paths: dict[str, tuple[Path, Path]],
    service_factory: ServiceFactory | None = None,
) -> set[str]:
    """Return ``project/N`` refs whose plan-review task is already approved."""
    if service_factory is None:
        from pollypm.work.sqlite_service import SQLiteWorkService

        service_factory = SQLiteWorkService
    approved_refs: set[str] = set()
    for db_key, refs in refs_by_db.items():
        db_path, project_path = project_db_paths[db_key]
        try:
            svc = service_factory(db_path=db_path, project_path=project_path)
        except Exception:  # noqa: BLE001
            logger.debug(
                "inbox: open svc failed for db %s during plan-review check",
                db_key,
                exc_info=True,
            )
            continue
        try:
            for project, number in refs:
                if is_plan_task_approved(svc, project, number):
                    approved_refs.add(f"{project}/{number}")
        finally:
            close = getattr(svc, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass
    return approved_refs


__all__ = [
    "PLAN_APPROVAL_NODE_ID",
    "approved_plan_review_refs",
    "is_plan_task_approved",
    "task_user_approval_is_approved",
]
