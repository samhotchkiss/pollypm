"""Cycle 133 — perf review: cache _dashboard_plan_staleness.

The per-project dashboard refresh tick (every 10s) used to call
``_dashboard_plan_staleness`` which opens SQLiteWorkService and
walks the entire task list to find the most recent backlog task.
On 9 projects that's 9 DB opens + 9 list_tasks() per tick.

The fix caches by ``(project_key, plan_mtime, db_mtime)`` —
content-addressed, no TTL needed since a new task or a plan edit
both bump one of the two mtimes. A project with no plan or task
changes pays zero work past the first refresh.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from pollypm.cockpit_ui import (
    _PLAN_STALENESS_CACHE,
    _dashboard_plan_staleness,
)


def setup_function(_func) -> None:
    _PLAN_STALENESS_CACHE.clear()


def _fresh_plan(tmp_path: Path) -> tuple[Path, float]:
    plan = tmp_path / "docs" / "plan.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("# Plan\n", encoding="utf-8")
    return plan, plan.stat().st_mtime


def _project_with_db(tmp_path: Path) -> Path:
    project = tmp_path / "demo"
    db_dir = project / ".pollypm"
    db_dir.mkdir(parents=True, exist_ok=True)
    (db_dir / "state.db").write_bytes(b"")  # exists for the helper's stat()
    return project


def test_cache_avoids_reopening_workservice_on_repeat_call(tmp_path: Path) -> None:
    """Two back-to-back calls with the same plan_mtime + db_mtime hit
    the cache — the SQLiteWorkService open is only attempted once."""
    project = _project_with_db(tmp_path)
    plan, plan_mtime = _fresh_plan(project)

    open_count = {"n": 0}

    class _FakeSvc:
        def __init__(self, *_, **__):
            open_count["n"] += 1

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def list_tasks(self, *, project=None):
            return []

    fake_plan_task = object()

    with patch(
        "pollypm.work.sqlite_service.SQLiteWorkService", _FakeSvc,
    ), patch(
        "pollypm.plan_presence._find_approved_plan_task",
        lambda _svc, _key: fake_plan_task,
    ), patch(
        "pollypm.plan_presence._plan_approved_at",
        lambda _svc, _task: 1_700_000_000.0,
    ):
        first = _dashboard_plan_staleness(plan, plan_mtime, project, "demo")
        second = _dashboard_plan_staleness(plan, plan_mtime, project, "demo")

    assert first == second
    assert open_count["n"] == 1, "second call must hit the cache, not re-open the DB"


def test_cache_invalidates_when_db_mtime_changes(tmp_path: Path) -> None:
    """Bumping the state.db mtime (i.e. a new task was added) makes
    the cache key change — the helper must re-walk the task list."""
    project = _project_with_db(tmp_path)
    plan, plan_mtime = _fresh_plan(project)

    open_count = {"n": 0}

    class _FakeSvc:
        def __init__(self, *_, **__):
            open_count["n"] += 1

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def list_tasks(self, *, project=None):
            return []

    fake_plan_task = object()

    with patch(
        "pollypm.work.sqlite_service.SQLiteWorkService", _FakeSvc,
    ), patch(
        "pollypm.plan_presence._find_approved_plan_task",
        lambda _svc, _key: fake_plan_task,
    ), patch(
        "pollypm.plan_presence._plan_approved_at",
        lambda _svc, _task: 1_700_000_000.0,
    ):
        _dashboard_plan_staleness(plan, plan_mtime, project, "demo")
        # Touch the db file to bump its mtime (simulate a new task).
        import os
        new_time = (project / ".pollypm" / "state.db").stat().st_mtime + 100
        os.utime(project / ".pollypm" / "state.db", (new_time, new_time))
        _dashboard_plan_staleness(plan, plan_mtime, project, "demo")

    assert open_count["n"] == 2, "cache must invalidate when db mtime changes"


def test_cache_returns_none_when_no_plan_path(tmp_path: Path) -> None:
    """The early-return path (no plan_path) doesn't touch the cache —
    nothing to cache, no I/O to elide."""
    assert _dashboard_plan_staleness(None, None, None, "demo") is None
    assert len(_PLAN_STALENESS_CACHE) == 0
