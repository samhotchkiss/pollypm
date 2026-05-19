"""Postgres-backed work service skeleton (issue #1737, Slice A).

This module is the pg-side counterpart to
:class:`pollypm.work.sqlite_service.SQLiteWorkService`. Slice A wires the
constructor + a small read+CRUD subset; the rest of the protocol surface
fills in across Slices B-H. Callers slot :class:`PgWorkService` through
the structural Protocols in :mod:`pollypm.work.service` (#1717-#1736), so
no presentation/plugin code has to change when the cutover flips the
factory.

Slice A scope
-------------

Implemented (covered by tests in ``tests/test_pg_work_service.py``):

* ``__init__`` — accepts a pool (or pulls the lazy singleton), runs the
  migration applier to guarantee schema is up to date.
* ``get(task_id)`` — single-row fetch.
* ``list_tasks(project=...)`` — basic filtering on a subset of the
  protocol's filter fields.
* ``create(...)`` — minimal create returning a typed :class:`Task`.
* ``queue(...)`` / ``done(...)`` / ``cancel(...)`` — the three simplest
  state transitions, all writing through ``work_transitions`` for audit
  parity with sqlite.

Deferred to Slice B:

* The full transition manager (review / approve / reject / claim chain,
  worker sessions, kickoff bookkeeping).
* Context entries, dependencies, flow templates, gates.
* notification_staging writers (port-as-is per Slice C).
* sync adapters, audit JSONL emission.

The deferred surface is stubbed with ``NotImplementedError`` carrying
the three-question message so a premature wire-up fails loud.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pollypm.inbox.kind import coerce_kind as _coerce_inbox_kind
from pollypm.work.models import (
    ContextEntry,
    FlowNodeExecution,
    FlowTemplate,
    GateResult,
    Priority,
    Task,
    TaskType,
    WorkOutput,
    WorkStatus,
    WorkerSessionRecord,
)
from pollypm.work.service_support import (
    InvalidTransitionError,
    TaskNotFoundError,
    ValidationError,
)

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

    from pollypm.models import PollyPMConfig

logger = logging.getLogger(__name__)


_NOT_IMPLEMENTED_SLICE_B = (
    "PgWorkService method not implemented in Slice A. "
    "The full work-service port lands in Slice B (#1737). "
    "Fix: keep ``[storage] backend = 'sqlite'`` until Slice B merges, "
    "or implement the method here if you are wiring Slice B."
)


def _parse_task_id(task_id: str) -> tuple[str, int]:
    """Split ``project/number`` into ``(project, int(number))``.

    Mirrors :func:`pollypm.work.service_support._parse_task_id` but kept
    local so the pg service has no implicit coupling to the sqlite
    helper module — Slice B can lift it out into a shared module if
    needed.
    """
    if "/" not in task_id:
        raise ValidationError(
            f"Invalid task id {task_id!r}: expected ``project/number``."
        )
    project, _, number = task_id.rpartition("/")
    try:
        return project, int(number)
    except ValueError as exc:
        raise ValidationError(
            f"Invalid task id {task_id!r}: number must be an integer."
        ) from exc


def _now_iso() -> datetime:
    return datetime.now(UTC)


def _coerce_status(raw: str) -> WorkStatus:
    try:
        return WorkStatus(raw)
    except ValueError:
        # Unknown statuses surface as DRAFT so a corrupt row is at least
        # readable. Matches the sqlite path's tolerant decode.
        return WorkStatus.DRAFT


def _coerce_priority(raw: str) -> Priority:
    try:
        return Priority(raw)
    except ValueError:
        return Priority.NORMAL


def _coerce_type(raw: str) -> TaskType:
    try:
        return TaskType(raw)
    except ValueError:
        return TaskType.TASK


def _json_loads(raw: Any, default: Any) -> Any:
    """Decode a JSON column tolerantly.

    psycopg returns ``jsonb`` columns as already-decoded Python objects,
    so most rows skip the ``json.loads`` path. Strings only appear when
    the column was inserted as text (test seeds, legacy imports). The
    ``default`` is returned for both ``None`` and decode errors so the
    caller doesn't have to special-case either.
    """
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return default
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default
    return default


class PgWorkService:
    """Postgres-backed work-service implementation (Slice A skeleton).

    The constructor accepts an optional ``pool`` so tests can inject a
    test-container pool; production callers pass ``None`` and let the
    module pull :func:`pollypm.storage.pg_pool.get_rw_pool`.

    The structural Protocol the rest of PollyPM consumes is
    :class:`pollypm.work.service.WorkService` — every method on it must
    eventually have a concrete implementation here. Slice A ships the
    minimum subset documented above.
    """

    def __init__(
        self,
        *,
        pool: "ConnectionPool | None" = None,
        ro_pool: "ConnectionPool | None" = None,
        config: "PollyPMConfig | None" = None,
        project_key: str | None = None,
        apply_migrations: bool = True,
    ) -> None:
        if pool is None:
            from pollypm.storage.pg_pool import get_rw_pool

            pool = get_rw_pool(config)
        if ro_pool is None:
            from pollypm.storage.pg_pool import get_ro_pool

            try:
                ro_pool = get_ro_pool(config)
            except Exception:  # noqa: BLE001 - ro pool is optional in Slice A
                ro_pool = None

        self._pool = pool
        self._ro_pool = ro_pool
        self._project_key = project_key or ""
        # Slice A keeps the same single-process schema-on-open contract
        # the sqlite service has: open the service, schema is current.
        # Slice E adds an explicit ``pm storage migrate`` CLI; until
        # then the constructor is the only writer that runs DDL.
        if apply_migrations:
            from pollypm.storage.pg_migrations import apply_migrations as run

            run(self._pool)

        # Match the sqlite service's last-error breadcrumb (#243) so
        # cockpit code that reads this attribute via Protocol shape
        # doesn't crash on the pg backend.
        self.last_provision_error: str | None = None
        self.last_first_shipped_created: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __enter__(self) -> "PgWorkService":
        return self

    def __exit__(self, *exc: object) -> None:
        # The pool is process-wide and shared; we do not close it on
        # context exit. Test fixtures call
        # ``pg_pool.pg_pool_shutdown()`` explicitly.
        return None

    def close(self) -> None:
        """No-op — pool lifetime is owned by ``pg_pool``."""
        return None

    def set_session_manager(self, session_manager: object) -> None:  # noqa: ARG002
        """Slice A: session manager wiring lands in Slice B."""
        return None

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def get(self, task_id: str) -> Task:
        """Read one task by its ``project/number`` id."""
        project, task_number = _parse_task_id(task_id)
        sql = """
            SELECT project, task_number, project_key, title, type, labels,
                   work_status, flow_template_id, flow_template_version,
                   current_node_id, assignee, priority, requires_human_review,
                   description, acceptance_criteria, constraints, relevant_files,
                   parent_project, parent_task_number,
                   supersedes_project, supersedes_task_number,
                   plan_version, predecessor_task_id, kind,
                   roles, external_refs,
                   created_at, created_by, updated_at
              FROM work_tasks
             WHERE project = %s AND task_number = %s
        """
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (project, task_number))
            row = cur.fetchone()
        if row is None:
            raise TaskNotFoundError(f"Task '{task_id}' not found.")
        return self._row_to_task(row)

    def list_tasks(
        self,
        *,
        work_status: str | None = None,
        owner: str | None = None,  # noqa: ARG002 — Slice B wires owner derivation
        project: str | None = None,
        assignee: str | None = None,
        blocked: bool | None = None,  # noqa: ARG002 — Slice B wires blocked filter
        type: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Task]:
        """Query tasks with optional filters.

        Slice A supports the predicate subset the operator-dashboard
        smoke tests exercise: ``project``, ``work_status``,
        ``assignee``, ``type``, ``limit``/``offset``. The remaining
        filters (``owner``, ``blocked``) are accepted for Protocol
        parity but ignored — Slice B fills them in once the role
        derivation + dependency walk port.
        """
        where: list[str] = []
        params: list[object] = []
        if project is not None:
            where.append("project = %s")
            params.append(project)
        if work_status is not None:
            where.append("work_status = %s")
            params.append(work_status)
        if assignee is not None:
            where.append("assignee = %s")
            params.append(assignee)
        if type is not None:
            where.append("type = %s")
            params.append(type)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        order_limit = " ORDER BY project, task_number"
        if limit is not None:
            order_limit += f" LIMIT {int(limit)}"
        if offset is not None:
            order_limit += f" OFFSET {int(offset)}"
        sql = (
            "SELECT project, task_number, project_key, title, type, labels, "
            "work_status, flow_template_id, flow_template_version, "
            "current_node_id, assignee, priority, requires_human_review, "
            "description, acceptance_criteria, constraints, relevant_files, "
            "parent_project, parent_task_number, "
            "supersedes_project, supersedes_task_number, "
            "plan_version, predecessor_task_id, kind, "
            "roles, external_refs, "
            "created_at, created_by, updated_at "
            "FROM work_tasks" + clause + order_limit
        )
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [self._row_to_task(row) for row in rows]

    def _row_to_task(self, row: tuple) -> Task:
        """Convert a SELECT-* row tuple into a :class:`Task` dataclass.

        Column order MUST match the SELECT list in :meth:`get` and
        :meth:`list_tasks`. Slice A intentionally does not load
        executions / context entries / transitions / token sums — those
        sub-queries land in Slice B alongside the rest of the protocol
        surface.
        """
        (
            project,
            task_number,
            _project_key,
            title,
            type_raw,
            labels_raw,
            work_status_raw,
            flow_template_id,
            flow_template_version,
            current_node_id,
            assignee,
            priority_raw,
            requires_human_review,
            description,
            acceptance_criteria,
            constraints,
            relevant_files_raw,
            parent_project,
            parent_task_number,
            supersedes_project,
            supersedes_task_number,
            plan_version,
            predecessor_task_id,
            kind_raw,
            roles_raw,
            external_refs_raw,
            created_at,
            created_by,
            updated_at,
        ) = row
        return Task(
            project=str(project),
            task_number=int(task_number),
            title=str(title),
            type=_coerce_type(str(type_raw)),
            labels=list(_json_loads(labels_raw, [])),
            work_status=_coerce_status(str(work_status_raw)),
            flow_template_id=str(flow_template_id),
            flow_template_version=int(flow_template_version),
            current_node_id=current_node_id,
            assignee=assignee,
            priority=_coerce_priority(str(priority_raw)),
            requires_human_review=bool(requires_human_review),
            description=str(description or ""),
            acceptance_criteria=acceptance_criteria,
            constraints=constraints,
            relevant_files=list(_json_loads(relevant_files_raw, [])),
            parent_project=parent_project,
            parent_task_number=(
                int(parent_task_number) if parent_task_number is not None else None
            ),
            supersedes_project=supersedes_project,
            supersedes_task_number=(
                int(supersedes_task_number)
                if supersedes_task_number is not None
                else None
            ),
            plan_version=int(plan_version or 1),
            predecessor_task_id=predecessor_task_id,
            kind=_coerce_inbox_kind(kind_raw),
            roles=dict(_json_loads(roles_raw, {})),
            external_refs=dict(_json_loads(external_refs_raw, {})),
            created_at=created_at,
            created_by=str(created_by or ""),
            updated_at=updated_at,
        )

    # ------------------------------------------------------------------
    # Write path — minimal CRUD trio.
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        title: str,
        description: str = "",
        type: str,
        project: str,
        flow_template: str,
        roles: dict[str, str],
        priority: str = "normal",
        created_by: str = "system",
        acceptance_criteria: str | None = None,
        constraints: str | None = None,
        relevant_files: list[str] | None = None,
        labels: list[str] | None = None,
        requires_human_review: bool = False,
        predecessor_task_id: str | None = None,
        kind: str = "legacy",
    ) -> Task:
        """Create a task in ``draft`` state.

        Slice A's create is intentionally minimal — no gate evaluation,
        no flow-template loading, no audit JSONL emission. The DB
        insert plus the resulting :class:`Task` round-trip is enough to
        verify the schema port. Slice B reinstates the full pipeline
        (gates, audit, sync hooks).
        """
        now = _now_iso()
        kind_value = _coerce_inbox_kind(kind).value
        with self._pool.connection() as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                # Allocate the next task_number atomically per project.
                cur.execute(
                    "SELECT COALESCE(MAX(task_number), 0) + 1 "
                    "FROM work_tasks WHERE project = %s",
                    (project,),
                )
                task_number = int(cur.fetchone()[0])
                cur.execute(
                    "INSERT INTO work_tasks ("
                    " project, task_number, project_key, title, type, labels, "
                    " work_status, flow_template_id, flow_template_version, "
                    " priority, requires_human_review, description, "
                    " acceptance_criteria, constraints, relevant_files, "
                    " plan_version, predecessor_task_id, kind, "
                    " roles, external_refs, created_at, created_by, updated_at"
                    ") VALUES ("
                    " %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, "
                    " %s, %s, %s::jsonb, %s, %s, %s, %s::jsonb, %s::jsonb, "
                    " %s, %s, %s"
                    ")",
                    (
                        project,
                        task_number,
                        project,  # project_key mirrors project in Slice A
                        title,
                        type,
                        json.dumps(labels or []),
                        WorkStatus.DRAFT.value,
                        flow_template,
                        1,
                        priority,
                        bool(requires_human_review),
                        description,
                        acceptance_criteria,
                        constraints,
                        json.dumps(relevant_files or []),
                        1,
                        predecessor_task_id,
                        kind_value,
                        json.dumps(roles or {}),
                        json.dumps({}),
                        now,
                        created_by,
                        now,
                    ),
                )
            conn.commit()
        return self.get(f"{project}/{task_number}")

    def queue(
        self,
        task_id: str,
        actor: str,
        skip_gates: bool = False,  # noqa: ARG002 — Slice B wires gates
    ) -> Task:
        """Move a ``draft`` task to ``queued``.

        Slice A skips gate evaluation entirely. The transition row is
        still written so the audit history is sane.
        """
        return self._simple_transition(
            task_id,
            from_state=WorkStatus.DRAFT,
            to_state=WorkStatus.QUEUED,
            actor=actor,
        )

    def cancel(self, task_id: str, actor: str, reason: str) -> Task:
        """Move any non-terminal task to ``cancelled``."""
        task = self.get(task_id)
        if task.work_status in (WorkStatus.DONE, WorkStatus.CANCELLED):
            raise InvalidTransitionError(
                f"Cannot cancel task in terminal state {task.work_status.value!r}."
            )
        return self._simple_transition(
            task_id,
            from_state=task.work_status,
            to_state=WorkStatus.CANCELLED,
            actor=actor,
            reason=reason,
        )

    def mark_done(self, task_id: str, actor: str) -> Task:
        """Force a task to ``done`` without running the flow.

        Mirrors the sqlite ``mark_done`` escape valve used by admin /
        recovery paths. Slice A intentionally leaves the full
        ``node_done`` pipeline (flow advancement, work output coercion,
        gate replay) to Slice B.
        """
        task = self.get(task_id)
        if task.work_status == WorkStatus.DONE:
            return task
        return self._simple_transition(
            task_id,
            from_state=task.work_status,
            to_state=WorkStatus.DONE,
            actor=actor,
        )

    def _simple_transition(
        self,
        task_id: str,
        *,
        from_state: WorkStatus,
        to_state: WorkStatus,
        actor: str,
        reason: str | None = None,
    ) -> Task:
        project, task_number = _parse_task_id(task_id)
        now = _now_iso()
        with self._pool.connection() as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT work_status FROM work_tasks "
                    "WHERE project = %s AND task_number = %s",
                    (project, task_number),
                )
                row = cur.fetchone()
                if row is None:
                    raise TaskNotFoundError(f"Task '{task_id}' not found.")
                current = _coerce_status(str(row[0]))
                # Idempotency: a re-queue against an already-queued task
                # is a no-op. The sqlite service has the same shape.
                if current == to_state:
                    return self.get(task_id)
                cur.execute(
                    "UPDATE work_tasks SET work_status = %s, updated_at = %s "
                    "WHERE project = %s AND task_number = %s",
                    (to_state.value, now, project, task_number),
                )
                cur.execute(
                    "INSERT INTO work_transitions ("
                    "task_project, task_number, from_state, to_state, "
                    "actor, reason, created_at"
                    ") VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        project,
                        task_number,
                        current.value,
                        to_state.value,
                        actor,
                        reason,
                        now,
                    ),
                )
            conn.commit()
        return self.get(task_id)

    # ------------------------------------------------------------------
    # Protocol stubs — Slice B fills these in.
    # ------------------------------------------------------------------

    def list_nonterminal_tasks(
        self,
        *,
        project: str | None = None,
    ) -> list[Task]:
        """Return non-terminal tasks (Slice A: trivial WHERE filter)."""
        terminals = (WorkStatus.DONE.value, WorkStatus.CANCELLED.value)
        sql = (
            "SELECT project, task_number, project_key, title, type, labels, "
            "work_status, flow_template_id, flow_template_version, "
            "current_node_id, assignee, priority, requires_human_review, "
            "description, acceptance_criteria, constraints, relevant_files, "
            "parent_project, parent_task_number, "
            "supersedes_project, supersedes_task_number, "
            "plan_version, predecessor_task_id, kind, roles, external_refs, "
            "created_at, created_by, updated_at "
            "FROM work_tasks "
            "WHERE work_status NOT IN (%s, %s)"
        )
        params: list[object] = list(terminals)
        if project is not None:
            sql += " AND project = %s"
            params.append(project)
        sql += " ORDER BY project, task_number"
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [self._row_to_task(row) for row in rows]

    def update(self, task_id: str, **fields: object) -> Task:  # noqa: ARG002
        raise NotImplementedError(_NOT_IMPLEMENTED_SLICE_B)

    def increment_plan_version(
        self,
        task_id: str,  # noqa: ARG002
        *,
        actor: str = "system",  # noqa: ARG002
        reason: str | None = None,  # noqa: ARG002
    ) -> Task:
        raise NotImplementedError(_NOT_IMPLEMENTED_SLICE_B)

    def list_successors(
        self, predecessor_task_id: str
    ) -> list[Task]:  # noqa: ARG002
        raise NotImplementedError(_NOT_IMPLEMENTED_SLICE_B)

    def claim(
        self, task_id: str, actor: str, skip_gates: bool = False  # noqa: ARG002
    ) -> Task:
        raise NotImplementedError(_NOT_IMPLEMENTED_SLICE_B)

    def next(  # noqa: A003
        self,
        *,
        agent: str | None = None,  # noqa: ARG002
        project: str | None = None,  # noqa: ARG002
    ) -> Task | None:
        raise NotImplementedError(_NOT_IMPLEMENTED_SLICE_B)

    def hold(
        self, task_id: str, actor: str, reason: str | None = None  # noqa: ARG002
    ) -> Task:
        raise NotImplementedError(_NOT_IMPLEMENTED_SLICE_B)

    def resume(self, task_id: str, actor: str) -> Task:  # noqa: ARG002
        raise NotImplementedError(_NOT_IMPLEMENTED_SLICE_B)

    def node_done(
        self,
        task_id: str,  # noqa: ARG002
        actor: str,  # noqa: ARG002
        work_output: WorkOutput | dict | None = None,  # noqa: ARG002
        skip_gates: bool = False,  # noqa: ARG002
    ) -> Task:
        raise NotImplementedError(_NOT_IMPLEMENTED_SLICE_B)

    def approve(
        self,
        task_id: str,  # noqa: ARG002
        actor: str,  # noqa: ARG002
        reason: str | None = None,  # noqa: ARG002
        skip_gates: bool = False,  # noqa: ARG002
        resume_merge: bool = False,  # noqa: ARG002
    ) -> Task:
        raise NotImplementedError(_NOT_IMPLEMENTED_SLICE_B)

    def reject(
        self, task_id: str, actor: str, reason: str  # noqa: ARG002
    ) -> Task:
        raise NotImplementedError(_NOT_IMPLEMENTED_SLICE_B)

    def block(
        self, task_id: str, actor: str, blocker_task_id: str  # noqa: ARG002
    ) -> Task:
        raise NotImplementedError(_NOT_IMPLEMENTED_SLICE_B)

    def get_execution(
        self,
        task_id: str,  # noqa: ARG002
        node_id: str | None = None,  # noqa: ARG002
        visit: int | None = None,  # noqa: ARG002
    ) -> list[FlowNodeExecution]:
        raise NotImplementedError(_NOT_IMPLEMENTED_SLICE_B)

    def add_context(
        self,
        task_id: str,  # noqa: ARG002
        actor: str,  # noqa: ARG002
        text: str,  # noqa: ARG002
        *,
        entry_type: str = "note",  # noqa: ARG002
    ) -> ContextEntry:
        raise NotImplementedError(_NOT_IMPLEMENTED_SLICE_B)

    def get_context(
        self,
        task_id: str,  # noqa: ARG002
        limit: int | None = None,  # noqa: ARG002
        since: str | None = None,  # noqa: ARG002
        entry_type: str | None = None,  # noqa: ARG002
    ) -> list[ContextEntry]:
        raise NotImplementedError(_NOT_IMPLEMENTED_SLICE_B)

    def link(
        self, from_id: str, to_id: str, kind: str  # noqa: ARG002
    ) -> None:
        raise NotImplementedError(_NOT_IMPLEMENTED_SLICE_B)

    def unlink(
        self, from_id: str, to_id: str, kind: str  # noqa: ARG002
    ) -> None:
        raise NotImplementedError(_NOT_IMPLEMENTED_SLICE_B)

    def dependents(self, task_id: str) -> list[Task]:  # noqa: ARG002
        raise NotImplementedError(_NOT_IMPLEMENTED_SLICE_B)

    def available_flows(
        self, project: str | None = None  # noqa: ARG002
    ) -> list[FlowTemplate]:
        raise NotImplementedError(_NOT_IMPLEMENTED_SLICE_B)

    def get_flow(
        self, name: str, project: str | None = None  # noqa: ARG002
    ) -> FlowTemplate:
        raise NotImplementedError(_NOT_IMPLEMENTED_SLICE_B)

    def validate_advance(
        self, task_id: str, actor: str  # noqa: ARG002
    ) -> list[GateResult]:
        raise NotImplementedError(_NOT_IMPLEMENTED_SLICE_B)

    def sync_status(self, task_id: str) -> dict[str, object]:  # noqa: ARG002
        raise NotImplementedError(_NOT_IMPLEMENTED_SLICE_B)

    def trigger_sync(
        self,
        task_id: str | None = None,  # noqa: ARG002
        adapter: str | None = None,  # noqa: ARG002
    ) -> dict[str, object]:
        raise NotImplementedError(_NOT_IMPLEMENTED_SLICE_B)

    def state_counts(
        self, project: str | None = None
    ) -> dict[str, int]:
        """Task counts by state (Slice A: simple GROUP BY)."""
        sql = "SELECT work_status, COUNT(*) FROM work_tasks"
        params: list[object] = []
        if project is not None:
            sql += " WHERE project = %s"
            params.append(project)
        sql += " GROUP BY work_status"
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return {str(row[0]): int(row[1]) for row in cur.fetchall()}

    def my_tasks(self, agent: str) -> list[Task]:  # noqa: ARG002
        raise NotImplementedError(_NOT_IMPLEMENTED_SLICE_B)

    def blocked_tasks(
        self, project: str | None = None  # noqa: ARG002
    ) -> list[Task]:
        raise NotImplementedError(_NOT_IMPLEMENTED_SLICE_B)

    def ensure_worker_session_schema(self) -> None:
        # Schema is installed by the migrations applier — no per-instance
        # bootstrap needed on the pg backend.
        return None

    def upsert_worker_session(self, **kwargs: object) -> None:  # noqa: ARG002
        raise NotImplementedError(_NOT_IMPLEMENTED_SLICE_B)

    def get_worker_session(
        self, **kwargs: object  # noqa: ARG002
    ) -> WorkerSessionRecord | None:
        raise NotImplementedError(_NOT_IMPLEMENTED_SLICE_B)

    def list_worker_sessions(
        self, **kwargs: object  # noqa: ARG002
    ) -> list[WorkerSessionRecord]:
        raise NotImplementedError(_NOT_IMPLEMENTED_SLICE_B)

    def end_worker_session(self, **kwargs: object) -> None:  # noqa: ARG002
        raise NotImplementedError(_NOT_IMPLEMENTED_SLICE_B)

    def mark_worker_session_ended(
        self, **kwargs: object  # noqa: ARG002
    ) -> None:
        raise NotImplementedError(_NOT_IMPLEMENTED_SLICE_B)

    def update_worker_session_tokens(
        self, **kwargs: object  # noqa: ARG002
    ) -> None:
        raise NotImplementedError(_NOT_IMPLEMENTED_SLICE_B)


__all__ = ["PgWorkService"]
