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

from typing import Any

from pollypm.work.models import WorkerSessionRecord

# ``service`` is typed ``Any`` here because these helpers reach into the
# SQLite-specific ``service._conn`` attribute. The previous TYPE_CHECKING
# import of ``SQLiteWorkService`` was an IDE hint only; dropping it
# removes a direct ``sqlite_service`` import so the pg cutover can delete
# that module (#1369, #1737).


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
    -- #809: persisted at launch so per-task transcript archival can
    -- find the right Claude/Codex tree at teardown without depending
    -- on the supervisor process's ambient env. NULL means "fall back
    -- to ambient env" (legacy / unconfigured).
    provider TEXT,
    provider_home TEXT,
    PRIMARY KEY (task_project, task_number),
    FOREIGN KEY (task_project, task_number) REFERENCES work_tasks(project, task_number)
);
"""


def _row_get(row, key: str, default=None):
    """Tolerate fresh DBs (with the column) and pre-#809 rows (without).

    SQLite ``Row`` objects raise ``IndexError`` for unknown columns, so
    this helper makes the optional ``provider`` / ``provider_home``
    columns safe to read on a DB that hasn't yet run migration 6.
    """
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


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
        provider=_row_get(row, "provider"),
        provider_home=_row_get(row, "provider_home"),
    )


def ensure_worker_session_schema(service: Any) -> None:
    service._conn.executescript(WORK_SESSIONS_DDL)


def upsert_worker_session(
    service: Any,
    *,
    task_project: str,
    task_number: int,
    agent_name: str,
    pane_id: str,
    worktree_path: str,
    branch_name: str,
    started_at: str,
    provider: str | None = None,
    provider_home: str | None = None,
) -> None:
    """Insert or refresh the work_sessions row for a task.

    #1014 (Bug B) — preserve ``total_input_tokens`` /
    ``total_output_tokens`` on ``ON CONFLICT``. The schema holds exactly
    one row per task (PK on ``(task_project, task_number)``) so a fresh
    provision is the same row replayed with a new pane. Pre-#1014 this
    upsert hard-reset both counters to ``0`` whenever a re-claim or
    crash-recovery produced a new pane, throwing away tokens that had
    been written by an earlier session's teardown / partial archival.
    The user-visible symptom on ``bikepath/8`` was ``pm task get`` showing
    ``in=0 out=0`` for a worker that had clearly burned real tokens
    across multiple build cycles.

    The next teardown's ``end_worker_session`` rescans the worktree's
    accumulated JSONL files and writes the cumulative total via SET
    semantics, so preserving the prior totals here doesn't risk
    double-counting — we just keep the running tally visible between
    teardowns instead of zeroing it on every fresh provision.
    """
    service._conn.execute(
        "INSERT INTO work_sessions "
        "(task_project, task_number, agent_name, pane_id, worktree_path, "
        "branch_name, started_at, provider, provider_home) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (task_project, task_number) DO UPDATE SET "
        "pane_id=excluded.pane_id, "
        "worktree_path=excluded.worktree_path, "
        "branch_name=excluded.branch_name, "
        "started_at=excluded.started_at, "
        "provider=excluded.provider, "
        "provider_home=excluded.provider_home, "
        "ended_at=NULL, "
        "archive_path=NULL",
        (
            task_project,
            task_number,
            agent_name,
            pane_id,
            worktree_path,
            branch_name,
            started_at,
            provider,
            provider_home,
        ),
    )
    service._conn.commit()


def mark_worker_session_ended(
    service: Any,
    *,
    task_project: str,
    task_number: int,
    ended_at: str,
) -> None:
    """Stamp ``ended_at`` on a worker_session row without touching tokens.

    #1014 (Bug B) — used by the orphan-reap path in ``provision_worker``
    when the prior pane died without going through ``teardown_worker``.
    Pre-#1014 the reap called ``end_worker_session(total_input_tokens=0,
    total_output_tokens=0, ...)`` which clobbered any token totals that
    a partial archival or earlier completed session had written. The
    user has no other surface for "what did this worker cost so far",
    so zeroing tokens on a recovery action was a silent telemetry loss.

    This helper only touches the active-row marker (``ended_at``) so the
    next ``session_for_task(active_only=True)`` returns ``None`` and the
    fresh provision can run without dragging the old pane id forward.
    Token totals stay untouched; the next teardown's archive scan will
    overwrite them with the worktree's cumulative count.
    """
    service._conn.execute(
        "UPDATE work_sessions SET ended_at = ? "
        "WHERE task_project = ? AND task_number = ?",
        (ended_at, task_project, task_number),
    )
    service._conn.commit()


def get_worker_session(
    service: Any,
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
    service: Any,
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
    service: Any,
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
    service: Any,
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
