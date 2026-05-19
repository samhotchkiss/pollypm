"""Tests for advisor lazy work-service opens threading ``config`` (#1881)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pollypm.work as _pollypm_work
from pollypm.plugins_builtin.advisor.handlers import advisor_tick as advisor_module


class _FakeTask:
    def __init__(self, task_id: str = "demo/1") -> None:
        self.task_id = task_id
        self.labels: list[str] = []


class _FakeSvc:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def list_tasks(self, **kwargs: Any) -> list[Any]:
        return []

    def create(self, **kwargs: Any) -> Any:
        self.created.append(kwargs)
        return _FakeTask()

    def queue(self, task_id: str, actor: str) -> Any:
        return SimpleNamespace(task_id=task_id)

    def close(self) -> None:
        pass


def _install_factory(monkeypatch, factory):
    monkeypatch.setattr(_pollypm_work, "create_work_service", factory)


def test_enqueue_advisor_review_threads_config_to_factory(monkeypatch, tmp_path: Path) -> None:
    """#1881: enqueue must pass ``config`` to the factory so the
    configured backend (pg) is selected over the sqlite default."""
    calls: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return _FakeSvc()

    _install_factory(monkeypatch, factory)

    config = SimpleNamespace(storage=SimpleNamespace(backend="postgres"))
    project_path = tmp_path / "demo"
    project_path.mkdir(parents=True, exist_ok=True)

    result = advisor_module.enqueue_advisor_review(
        project_key="demo",
        project_path=project_path,
        config=config,
    )

    assert result.get("enqueued") is True
    assert calls, "factory was not called"
    assert calls[0].get("config") is config, calls


def test_has_in_progress_advisor_task_threads_config(monkeypatch, tmp_path: Path) -> None:
    """#1881: throttle's lazy work-service open must pass config=."""
    calls: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return _FakeSvc()

    _install_factory(monkeypatch, factory)

    config = SimpleNamespace(storage=SimpleNamespace(backend="postgres"))
    project_path = tmp_path / "demo"
    project_path.mkdir(parents=True, exist_ok=True)

    advisor_module.has_in_progress_advisor_task(
        project_key="demo",
        work_service=None,
        project_path=project_path,
        config=config,
    )

    assert calls, "factory was not called"
    assert calls[0].get("config") is config, calls


def test_has_project_stagnation_candidate_threads_config(monkeypatch, tmp_path: Path) -> None:
    """#1881: stagnation probe's lazy work-service open must pass config=."""
    calls: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return _FakeSvc()

    _install_factory(monkeypatch, factory)

    config = SimpleNamespace(storage=SimpleNamespace(backend="postgres"))
    project_path = tmp_path / "demo"
    project_path.mkdir(parents=True, exist_ok=True)

    advisor_module.has_project_stagnation_candidate(
        project_key="demo",
        project_path=project_path,
        work_service=None,
        config=config,
    )

    assert calls, "factory was not called"
    assert calls[0].get("config") is config, calls
