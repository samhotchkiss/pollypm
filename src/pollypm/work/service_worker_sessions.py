"""Worker-session persistence for the SQLite work service.

Contract:
- Inputs: a ``SQLiteWorkService`` plus worker-session identifiers and
  counters.
- Outputs: typed ``WorkerSessionRecord`` rows.
- Side effects: creates and mutates ``work_sessions`` rows.
- Invariants: SessionManager uses this module instead of reaching into
  ``SQLiteWorkService._conn`` directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pollypm.work.models import WorkerSessionRecord

if TYPE_CHECKING:
    from pollypm.work.sqlite_service import SQLiteWorkService


WORK_SESSIONS_DDL = """
CREATE TABLE IF NOT EXISTS work_sessions (
    task_project TEXT NOT NULL,
    task_number INTEGER NOT NULL,
    agent_name TEXT NOT NULL,
    pane_id TEXT,
    worktree_path TEXT,
    branch_name TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    total_input_tokens INTEGER DEFAULT 0,
    total_output_tokens INTEGER DEFAULT 0,
    archive_path TEXT,
    PRIMARY KEY (task_project, task_number),
    FOREIGN KEY (task_project, task_number) REFERENCES work_tasks(project, task_number)
);
"""


def row_to_worker_session_record(row) -> WorkerSessionRecord:
    return WorkerSessionRecord(
        task_project=row["task_project"],
        task_number=int(row["task_number"]),
        agent_name=row["agent_name"],
        pane_id=row["pane_id"],
        worktree_path=row["worktree_path"],
        branch_name=row["branch_name"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        total_input_tokens=int(row["total_input_tokens"] or 0),
        total_output_tokens=int(row["total_output_tokens"] or 0),
        archive_path=row["archive_path"],
    )


def ensure_worker_session_schema(service: "SQLiteWorkService") -> None:
    service._conn.executescript(WORK_SESSIONS_DDL)


def upsert_worker_session(
    service: "SQLiteWorkService",
    *,
    task_project: str,
    task_number: int,
    agent_name: str,
    pane_id: str,
    worktree_path: str,
    branch_name: str,
    started_at: str,
) -> None:
    service._conn.execute(
        "INSERT INTO work_sessions "
        "(task_project, task_number, agent_name, pane_id, worktree_path, "
        "branch_name, started_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (task_project, task_number) DO UPDATE SET "
        "pane_id=excluded.pane_id, "
        "worktree_path=excluded.worktree_path, "
        "branch_name=excluded.branch_name, "
        "started_at=excluded.started_at, "
        "ended_at=NULL, "
        "archive_path=NULL, "
        "total_input_tokens=0, "
        "total_output_tokens=0",
        (
            task_project,
            task_number,
            agent_name,
            pane_id,
            worktree_path,
            branch_name,
            started_at,
        ),
    )
    service._conn.commit()


def get_worker_session(
    service: "SQLiteWorkService",
    *,
    task_project: str,
    task_number: int,
    active_only: bool = False,
) -> WorkerSessionRecord | None:
    if active_only:
        row = service._conn.execute(
            "SELECT * FROM work_sessions "
            "WHERE task_project = ? AND task_number = ? AND ended_at IS NULL",
            (task_project, task_number),
        ).fetchone()
    else:
        row = service._conn.execute(
            "SELECT * FROM work_sessions "
            "WHERE task_project = ? AND task_number = ?",
            (task_project, task_number),
        ).fetchone()
    if row is None:
        return None
    return row_to_worker_session_record(row)


def list_worker_sessions(
    service: "SQLiteWorkService",
    *,
    project: str | None = None,
    active_only: bool = True,
) -> list[WorkerSessionRecord]:
    clauses: list[str] = []
    params: list[object] = []
    if active_only:
        clauses.append("ended_at IS NULL")
    if project is not None:
        clauses.append("task_project = ?")
        params.append(project)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = service._conn.execute(
        f"SELECT * FROM work_sessions{where}",
        tuple(params),
    ).fetchall()
    return [row_to_worker_session_record(row) for row in rows]


def end_worker_session(
    service: "SQLiteWorkService",
    *,
    task_project: str,
    task_number: int,
    ended_at: str,
    total_input_tokens: int,
    total_output_tokens: int,
    archive_path: str | None,
) -> None:
    service._conn.execute(
        "UPDATE work_sessions SET ended_at = ?, total_input_tokens = ?, "
        "total_output_tokens = ?, archive_path = ? "
        "WHERE task_project = ? AND task_number = ?",
        (
            ended_at,
            total_input_tokens,
            total_output_tokens,
            archive_path,
            task_project,
            task_number,
        ),
    )
    service._conn.commit()


def update_worker_session_tokens(
    service: "SQLiteWorkService",
    *,
    task_project: str,
    task_number: int,
    total_input_tokens: int,
    total_output_tokens: int,
    archive_path: str | None,
) -> None:
    service._conn.execute(
        "UPDATE work_sessions SET total_input_tokens = ?, "
        "total_output_tokens = ?, archive_path = ? "
        "WHERE task_project = ? AND task_number = ?",
        (
            total_input_tokens,
            total_output_tokens,
            archive_path,
            task_project,
            task_number,
        ),
    )
    service._conn.commit()
