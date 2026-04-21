from __future__ import annotations

from pathlib import Path
import sqlite3
import time

from pollypm.cockpit_settings_projects import collect_settings_projects


class _FakeProject:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.name = "Demo"
        self.persona_name = None
        self.tracked = True


class _FakeConfig:
    def __init__(self, path: Path) -> None:
        self.projects = {"demo": _FakeProject(path)}


def _seed_work_db(project_path: Path) -> Path:
    db_path = project_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE work_tasks (project TEXT NOT NULL, task_number INTEGER NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO work_tasks (project, task_number) VALUES (?, ?)",
            [("demo", 1), ("demo", 2), ("other", 1)],
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def test_collect_settings_projects_reads_fast_task_totals(tmp_path: Path) -> None:
    project_path = tmp_path / "demo"
    project_path.mkdir()
    _seed_work_db(project_path)

    rows = collect_settings_projects(
        _FakeConfig(project_path),
        format_relative_age=lambda _value: "moments ago",
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["key"] == "demo"
    assert row["name"] == "Demo"
    assert row["persona"] == "Polly"
    assert row["path"] == str(project_path)
    assert row["path_exists"] is True
    assert row["tracked"] is True
    assert row["task_total"] == "2"
    assert row["task_total_label"] == "2"
    assert row["last_activity"] == "moments ago"


def test_collect_settings_projects_marks_busy_db_without_blocking(tmp_path: Path) -> None:
    project_path = tmp_path / "demo"
    project_path.mkdir()
    db_path = _seed_work_db(project_path)
    conn = sqlite3.connect(db_path, timeout=0.01)
    try:
        conn.execute("BEGIN EXCLUSIVE")
        start = time.perf_counter()
        rows = collect_settings_projects(
            _FakeConfig(project_path),
            format_relative_age=lambda _value: "moments ago",
        )
        elapsed = time.perf_counter() - start
    finally:
        conn.rollback()
        conn.close()

    assert elapsed < 1.0
    assert rows[0]["task_total_label"] == "busy"
    assert rows[0]["last_activity"] == "moments ago"
