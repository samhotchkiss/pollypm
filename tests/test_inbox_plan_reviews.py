"""Tests for ``pollypm.work.inbox_plan_reviews`` (#1880)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pollypm.work.inbox_plan_reviews import approved_plan_review_refs
from pollypm.work.models import Decision, ExecutionStatus


class _FakeExec:
    node_id = "user_approval"
    status = ExecutionStatus.COMPLETED
    decision = Decision.APPROVED


class _FakeTask:
    def __init__(self) -> None:
        self.executions = [_FakeExec()]


class _SharedSvc:
    def get(self, task_id: str) -> Any:
        raise KeyError(task_id)

    def close(self) -> None:
        pass


class _PerDbSvc:
    def __init__(self, approved: dict[str, Any]) -> None:
        self._approved = approved

    def get(self, task_id: str) -> Any:
        return self._approved[task_id]

    def close(self) -> None:
        pass


def test_approved_plan_review_refs_uses_per_db_on_sqlite(tmp_path: Path) -> None:
    """#1880: with sqlite backend, the function must walk per-project
    sqlite DBs via the explicit ``db_path`` rather than falling back to
    a single shared svc that bypasses legacy per-project state.dbs."""
    calls: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if kwargs.get("db_path") is None:
            return _SharedSvc()
        return _PerDbSvc({"demo/1": _FakeTask()})

    config = SimpleNamespace(storage=SimpleNamespace(backend="sqlite"))
    db_path = tmp_path / "legacy/demo/.pollypm/state.db"
    project_path = tmp_path / "legacy/demo"

    approved = approved_plan_review_refs(
        refs_by_db={"demo": {("demo", 1)}},
        project_db_paths={"demo": (db_path, project_path)},
        service_factory=factory,
        config=config,
    )

    assert approved == {"demo/1"}
    # Factory was called with explicit per-db path, not the shared one.
    assert any(c.get("db_path") == db_path for c in calls), calls
    # Shared-svc path was not taken.
    assert not any(
        "db_path" not in c and c.get("config") is config for c in calls
    )


def test_approved_plan_review_refs_uses_shared_on_postgres(tmp_path: Path) -> None:
    """#1880: with pg backend, the function opens a single shared svc."""
    calls: list[dict[str, Any]] = []

    pg_approved: dict[str, Any] = {"demo/1": _FakeTask()}

    class _PgSvc:
        def get(self, task_id: str) -> Any:
            return pg_approved[task_id]

        def close(self) -> None:
            pass

    def factory(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return _PgSvc()

    config = SimpleNamespace(storage=SimpleNamespace(backend="postgres"))
    db_path = tmp_path / "anywhere/state.db"
    project_path = tmp_path / "anywhere"

    approved = approved_plan_review_refs(
        refs_by_db={"demo": {("demo", 1)}},
        project_db_paths={"demo": (db_path, project_path)},
        service_factory=factory,
        config=config,
    )

    assert approved == {"demo/1"}
    # One shared svc open without explicit db_path.
    assert calls == [{"config": config}]


def test_approved_plan_review_refs_per_db_when_no_config() -> None:
    """When ``config`` is None the function still walks per-db paths."""
    calls: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return _PerDbSvc({"demo/1": _FakeTask()})

    db_path = Path("/legacy/demo/.pollypm/state.db")
    project_path = Path("/legacy/demo")

    approved = approved_plan_review_refs(
        refs_by_db={"demo": {("demo", 1)}},
        project_db_paths={"demo": (db_path, project_path)},
        service_factory=factory,
        config=None,
    )

    assert approved == {"demo/1"}
    assert calls == [
        {"db_path": db_path, "project_path": project_path, "config": None}
    ]
