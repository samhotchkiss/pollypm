"""Cycle 112: defend ``next_task`` against corrupt roles JSON.

``service_queries.next_task`` filters by agent via
``json.loads(row["roles"]).get("worker") != agent``. A corrupted or
empty ``roles`` column would crash the whole ``pm task next`` query
mid-loop, blocking the worker from picking up any task — even those
with well-formed roles. Defend with the same shape used elsewhere in
the corrupt-payload defense family (cycles 107-109).
"""

from __future__ import annotations

from pathlib import Path

from pollypm.work.service_queries import next_task
from pollypm.work.sqlite_service import SQLiteWorkService


def _seed_queued_task(svc: SQLiteWorkService, *, project: str = "demo", title: str = "t") -> int:
    task = svc.create(
        title=title,
        description="",
        type="task",
        flow_template="chat",
        roles={"worker": "alice"},
        project=project,
        priority="normal",
        created_by="test",
    )
    svc._conn.execute(
        "UPDATE work_tasks SET work_status = 'queued' WHERE project = ? AND task_number = ?",
        (project, task.task_number),
    )
    svc._conn.commit()
    return task.task_number


def test_next_task_skips_row_with_corrupt_roles(tmp_path: Path) -> None:
    db = tmp_path / "work.db"
    project_path = tmp_path / "proj"
    project_path.mkdir()
    with SQLiteWorkService(db_path=db, project_path=project_path) as svc:
        # Seed two queued tasks. The first row gets a hand-corrupted
        # roles column; the second is well-formed and matches.
        bad_num = _seed_queued_task(svc, title="bad")
        _seed_queued_task(svc, title="good")
        svc._conn.execute(
            "UPDATE work_tasks SET roles = ? WHERE project = ? AND task_number = ?",
            ('[1, 2, 3]', "demo", bad_num),
        )
        svc._conn.commit()
        # Without the defense, the loop would AttributeError on the
        # corrupt row and never reach the well-formed second task.
        picked = next_task(svc, agent="alice", project="demo")
        assert picked is not None
        assert picked.title == "good"


def test_next_task_skips_row_with_empty_roles(tmp_path: Path) -> None:
    """Empty ``roles`` column was the second crash shape — ``json.loads("")``
    raises ValueError before .get() ever runs."""
    db = tmp_path / "work.db"
    project_path = tmp_path / "proj"
    project_path.mkdir()
    with SQLiteWorkService(db_path=db, project_path=project_path) as svc:
        bad_num = _seed_queued_task(svc, title="bad")
        _seed_queued_task(svc, title="good")
        svc._conn.execute(
            "UPDATE work_tasks SET roles = ? WHERE project = ? AND task_number = ?",
            ("", "demo", bad_num),
        )
        svc._conn.commit()
        picked = next_task(svc, agent="alice", project="demo")
        assert picked is not None
        assert picked.title == "good"
