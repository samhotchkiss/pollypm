"""Backend-neutral :class:`SyncManager` dispatch + failure isolation (#1794).

Restores the ``TestSyncManager`` half of the deleted
``tests/test_sync_adapters.py``. These tests don't touch the work
service — they probe the manager's fan-out behaviour using
``MagicMock`` adapters, so there's no sqlite or pg coupling to port.
The work-service-coupled half (``sync_status`` / ``trigger_sync``
contract, GitHub adapter ``external_refs`` round-trip) lives in
``tests/test_pg_sync_adapters.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from pollypm.work.models import Task, TaskType
from pollypm.work.sync import SyncManager


def _bare_task(title: str = "Test") -> Task:
    return Task(
        project="proj",
        task_number=1,
        title=title,
        type=TaskType.TASK,
    )


def test_dispatches_to_all_registered_adapters():
    """Registering N adapters fans on_create out to all of them."""
    manager = SyncManager()
    adapter1 = MagicMock()
    adapter1.name = "a1"
    adapter2 = MagicMock()
    adapter2.name = "a2"

    manager.register(adapter1)
    manager.register(adapter2)

    task = _bare_task()
    manager.on_create(task)

    adapter1.on_create.assert_called_once_with(task)
    adapter2.on_create.assert_called_once_with(task)


def test_isolates_failures_between_adapters():
    """One adapter raising must not prevent the other from running."""
    manager = SyncManager()

    failing = MagicMock()
    failing.name = "failing"
    failing.on_create.side_effect = RuntimeError("boom")

    succeeding = MagicMock()
    succeeding.name = "succeeding"

    manager.register(failing)
    manager.register(succeeding)

    task = _bare_task()
    manager.on_create(task)

    failing.on_create.assert_called_once()
    succeeding.on_create.assert_called_once()


def test_dispatches_transition_to_all_adapters():
    manager = SyncManager()
    adapter = MagicMock()
    adapter.name = "test"
    manager.register(adapter)

    task = _bare_task()
    manager.on_transition(task, "draft", "queued")
    adapter.on_transition.assert_called_once_with(task, "draft", "queued")


def test_dispatches_update_to_all_adapters():
    manager = SyncManager()
    adapter = MagicMock()
    adapter.name = "test"
    manager.register(adapter)

    task = _bare_task()
    manager.on_update(task, ["title"])
    adapter.on_update.assert_called_once_with(task, ["title"])
