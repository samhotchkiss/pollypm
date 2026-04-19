"""Shared support primitives for work-service submodules.

Contract:
- Inputs: primitive task ids and state values.
- Outputs: shared exception types plus normalized parsing/time helpers.
- Side effects: none.
- Invariants: task ids always use ``project/number`` and validation
  failures explain both the problem and the expected shape.
"""

from __future__ import annotations

from datetime import datetime, timezone


class WorkServiceError(Exception):
    """Base error for work service operations."""


class TaskNotFoundError(WorkServiceError):
    """Raised when a task_id cannot be resolved."""


class InvalidTransitionError(WorkServiceError):
    """Raised when a state transition is not allowed."""


class ValidationError(WorkServiceError):
    """Raised when input validation fails."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_task_id(task_id: str) -> tuple[str, int]:
    parts = task_id.rsplit("/", 1)
    if len(parts) != 2:
        raise ValidationError(
            f"Invalid task_id '{task_id}'. Expected format: 'project/number'."
        )
    try:
        return parts[0], int(parts[1])
    except ValueError as exc:
        raise ValidationError(
            f"Invalid task_id '{task_id}'. Task number must be an integer."
        ) from exc
