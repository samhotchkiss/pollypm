"""Sync adapter protocol and manager.

Defines the SyncAdapter protocol that external system adapters implement,
and the SyncManager that dispatches lifecycle events to all registered adapters.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from pollypm.work.models import Task

logger = logging.getLogger(__name__)


@runtime_checkable
class SyncAdapter(Protocol):
    """Protocol for one-way push sync adapters."""

    name: str

    def on_create(self, task: Task) -> None:
        """Called when a new task is created."""
        ...

    def on_transition(self, task: Task, old_status: str, new_status: str) -> None:
        """Called when a task transitions between work statuses."""
        ...

    def on_update(self, task: Task, changed_fields: list[str]) -> None:
        """Called when mutable task fields are updated."""
        ...


class SyncManager:
    """Dispatches sync events to all registered adapters.

    Each adapter is called in a try/except so one adapter failing
    does not affect others or the work service.
    """

    def __init__(self) -> None:
        self._adapters: list[SyncAdapter] = []

    def register(self, adapter: SyncAdapter) -> None:
        """Register a sync adapter."""
        self._adapters.append(adapter)

    @property
    def adapters(self) -> list[SyncAdapter]:
        return list(self._adapters)

    def on_create(self, task: Task) -> None:
        """Dispatch a create event to all adapters."""
        for adapter in self._adapters:
            try:
                adapter.on_create(task)
            except Exception:
                logger.exception(
                    "Sync adapter %s failed on create for %s",
                    adapter.name,
                    task.task_id,
                )

    def on_transition(self, task: Task, old_status: str, new_status: str) -> None:
        """Dispatch a transition event to all adapters."""
        for adapter in self._adapters:
            try:
                adapter.on_transition(task, old_status, new_status)
            except Exception:
                logger.exception(
                    "Sync adapter %s failed on transition for %s",
                    adapter.name,
                    task.task_id,
                )

    def on_update(self, task: Task, changed_fields: list[str]) -> None:
        """Dispatch an update event to all adapters."""
        for adapter in self._adapters:
            try:
                adapter.on_update(task, changed_fields)
            except Exception:
                logger.exception(
                    "Sync adapter %s failed on update for %s",
                    adapter.name,
                    task.task_id,
                )
