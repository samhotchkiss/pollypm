"""Worker-session manager for the SQLite work service.

Contract:
- Inputs: a ``SQLiteWorkService`` plus worker-session identifiers and
  counters.
- Outputs: typed ``WorkerSessionRecord`` rows and schema setup.
- Side effects: owns the service-facing worker-session boundary so
  callers don't need the underlying helper module layout.
- Invariants: behavior stays delegated to the existing worker-session
  helpers; this manager just groups the service-owned API.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Protocol

from pollypm.work.models import WorkerSessionRecord
from pollypm.work.service_worker_sessions import (
    WORK_SESSIONS_DDL as _WORK_SESSIONS_DDL,
    end_worker_session,
    ensure_worker_session_schema,
    get_worker_session,
    list_worker_sessions,
    mark_worker_session_ended,
    update_worker_session_tokens,
    upsert_worker_session,
)


class _WorkService(Protocol):
    """Structural slice of ``SQLiteWorkService`` used by the worker-session
    helpers.

    The helpers in :mod:`pollypm.work.service_worker_sessions` only reach
    into ``service._conn`` to run the ``work_sessions`` DDL / CRUD. Typing
    against this Protocol lets the manager annotate ``service`` without a
    back-edge ``TYPE_CHECKING`` import of :class:`SQLiteWorkService`,
    which breaks the ``service_worker_session_manager`` <->
    ``sqlite_service`` cycle (#1367).
    """

    _conn: sqlite3.Connection


@dataclass(slots=True)
class WorkSessionManager:
    """Facade for worker-session persistence."""

    service: _WorkService

    WORK_SESSIONS_DDL = _WORK_SESSIONS_DDL

    def ensure_schema(self) -> None:
        ensure_worker_session_schema(self.service)

    def upsert(
        self,
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
        upsert_worker_session(
            self.service,
            task_project=task_project,
            task_number=task_number,
            agent_name=agent_name,
            pane_id=pane_id,
            worktree_path=worktree_path,
            branch_name=branch_name,
            started_at=started_at,
            provider=provider,
            provider_home=provider_home,
        )

    def get(
        self,
        *,
        task_project: str,
        task_number: int,
        active_only: bool = False,
    ) -> WorkerSessionRecord | None:
        return get_worker_session(
            self.service,
            task_project=task_project,
            task_number=task_number,
            active_only=active_only,
        )

    def list(
        self,
        *,
        project: str | None = None,
        active_only: bool = True,
    ) -> list[WorkerSessionRecord]:
        return list_worker_sessions(
            self.service,
            project=project,
            active_only=active_only,
        )

    def end(
        self,
        *,
        task_project: str,
        task_number: int,
        ended_at: str,
        total_input_tokens: int,
        total_output_tokens: int,
        archive_path: str | None,
    ) -> None:
        end_worker_session(
            self.service,
            task_project=task_project,
            task_number=task_number,
            ended_at=ended_at,
            total_input_tokens=total_input_tokens,
            total_output_tokens=total_output_tokens,
            archive_path=archive_path,
        )

    def mark_ended(
        self,
        *,
        task_project: str,
        task_number: int,
        ended_at: str,
    ) -> None:
        """Stamp ``ended_at`` only — preserves token counters (#1014)."""
        mark_worker_session_ended(
            self.service,
            task_project=task_project,
            task_number=task_number,
            ended_at=ended_at,
        )

    def update_tokens(
        self,
        *,
        task_project: str,
        task_number: int,
        total_input_tokens: int,
        total_output_tokens: int,
        archive_path: str | None,
    ) -> None:
        update_worker_session_tokens(
            self.service,
            task_project=task_project,
            task_number=task_number,
            total_input_tokens=total_input_tokens,
            total_output_tokens=total_output_tokens,
            archive_path=archive_path,
        )
