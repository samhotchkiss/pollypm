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


def _resolve_backend_name(config: object | None) -> str:
    """Return the configured backend string, defaulting to ``"sqlite"``.

    Mirrors :func:`pollypm.work.factory._resolve_backend` defensively
    without importing the private symbol so callers (#1880) can pick
    between shared-pg and per-sqlite paths without coupling.
    """
    if config is None:
        return "sqlite"
    storage = getattr(config, "storage", None)
    if storage is None:
        return "sqlite"
    backend = getattr(storage, "backend", "sqlite")
    if not isinstance(backend, str) or not backend.strip():
        return "sqlite"
    return backend.strip()


def approved_plan_review_refs(
    *,
    refs_by_db: dict[str, set[tuple[str, int]]],
    project_db_paths: dict[str, tuple[Path, Path]],
    service_factory: ServiceFactory | None = None,
    config: object | None = None,
) -> set[str]:
    """Return ``project/N`` refs whose plan-review task is already approved.

    Backend-aware (#1880):

    * On Postgres every ``db_key`` resolves to the same pool, so a
      single shared service handles every ref-set. We open it via
      ``service_factory(config=config)``.
    * On sqlite each ``db_key`` still maps to its own per-project
      ``state.db``. The caller has already collected explicit
      ``(db_path, project_path)`` pairs in ``project_db_paths``; we
      walk them per ref-set so legacy per-project DBs are not bypassed.
    """
    if service_factory is None:
        from pollypm.work.factory import create_work_service

        service_factory = create_work_service
    approved_refs: set[str] = set()
    backend = _resolve_backend_name(config)
    use_shared = backend == "postgres" and config is not None
    shared_svc = None
    if use_shared:
        try:
            shared_svc = service_factory(config=config)
        except Exception:  # noqa: BLE001
            logger.debug(
                "inbox: shared svc open failed; falling back to per-db opens",
                exc_info=True,
            )
            shared_svc = None
    for db_key, refs in refs_by_db.items():
        if shared_svc is not None:
            svc = shared_svc
            opened_local = False
        else:
            try:
                db_path, project_path = project_db_paths[db_key]
            except KeyError:
                logger.debug(
                    "inbox: no db_path for %s during plan-review check",
                    db_key,
                )
                continue
            try:
                svc = service_factory(
                    db_path=db_path,
                    project_path=project_path,
                    config=config,
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "inbox: open svc failed for db %s during plan-review check",
                    db_key,
                    exc_info=True,
                )
                continue
            opened_local = True
        try:
            for project, number in refs:
                if is_plan_task_approved(svc, project, number):
                    approved_refs.add(f"{project}/{number}")
        finally:
            if opened_local:
                close = getattr(svc, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:  # noqa: BLE001
                        pass
    if shared_svc is not None:
        close = getattr(shared_svc, "close", None)
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
