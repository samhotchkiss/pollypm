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
    ActorType,
    ArtifactKind,
    ContextEntry,
    Decision,
    ExecutionStatus,
    FlowNode,
    FlowNodeExecution,
    FlowTemplate,
    GateResult,
    LinkKind,
    NodeType,
    OutputType,
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

    # ------------------------------------------------------------------
    # Mutable field updates
    # ------------------------------------------------------------------

    _UPDATE_ALLOWED_COLUMNS = {
        "title": "title",
        "description": "description",
        "priority": "priority",
        "labels": "labels",
        "roles": "roles",
        "acceptance_criteria": "acceptance_criteria",
        "constraints": "constraints",
        "relevant_files": "relevant_files",
    }
    _UPDATE_JSON_COLUMNS = frozenset({"labels", "relevant_files", "roles"})

    def update(self, task_id: str, **fields: object) -> Task:
        """Update mutable fields on a task.

        Slice B port of ``update_task`` (service_queries.py). Refuses
        ``work_status`` and ``flow_template`` changes — those go through
        the lifecycle methods.
        """
        if "work_status" in fields:
            raise ValidationError(
                "Cannot change work_status via update(). "
                "Use lifecycle methods (queue, claim, cancel, etc.)."
            )
        if "flow_template" in fields or "flow_template_id" in fields:
            raise ValidationError("Cannot change flow_template after creation.")

        project, task_number = _parse_task_id(task_id)
        set_clauses: list[str] = []
        params: list[object] = []
        for key, value in fields.items():
            column = self._UPDATE_ALLOWED_COLUMNS.get(key)
            if column is None:
                raise ValidationError(f"Field '{key}' is not updatable.")
            if key in self._UPDATE_JSON_COLUMNS:
                params.append(json.dumps(value))
                set_clauses.append(f"{column} = %s::jsonb")
            else:
                params.append(value)
                set_clauses.append(f"{column} = %s")

        with self._pool.connection() as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM work_tasks "
                    "WHERE project = %s AND task_number = %s",
                    (project, task_number),
                )
                if cur.fetchone() is None:
                    raise TaskNotFoundError(f"Task '{task_id}' not found.")
                if not set_clauses:
                    return self.get(task_id)
                set_clauses.append("updated_at = %s")
                params.append(_now_iso())
                params.extend([project, task_number])
                cur.execute(
                    f"UPDATE work_tasks SET {', '.join(set_clauses)} "
                    "WHERE project = %s AND task_number = %s",
                    params,
                )
            conn.commit()
        return self.get(task_id)

    def increment_plan_version(
        self,
        task_id: str,
        *,
        actor: str = "system",  # noqa: ARG002 — audit emission is Slice C
        reason: str | None = None,  # noqa: ARG002 — audit emission is Slice C
    ) -> Task:
        """Bump ``plan_version`` on a plan task (#1398).

        Slice B keeps the version bump itself; the audit emission
        (``plan.version_incremented``) lands when Slice C ports the
        audit log adapter. Behavior is otherwise 1:1 with the sqlite
        path.
        """
        project, task_number = _parse_task_id(task_id)
        with self._pool.connection() as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT plan_version FROM work_tasks "
                    "WHERE project = %s AND task_number = %s",
                    (project, task_number),
                )
                row = cur.fetchone()
                if row is None:
                    raise TaskNotFoundError(f"Task '{task_id}' not found.")
                old_version = int(row[0] or 1)
                new_version = old_version + 1
                cur.execute(
                    "UPDATE work_tasks "
                    "SET plan_version = %s, updated_at = %s "
                    "WHERE project = %s AND task_number = %s",
                    (new_version, _now_iso(), project, task_number),
                )
            conn.commit()
        return self.get(task_id)

    def list_successors(self, predecessor_task_id: str) -> list[Task]:
        """Tasks whose ``predecessor_task_id`` matches (#1398)."""
        sql = (
            "SELECT project, task_number, project_key, title, type, labels, "
            "work_status, flow_template_id, flow_template_version, "
            "current_node_id, assignee, priority, requires_human_review, "
            "description, acceptance_criteria, constraints, relevant_files, "
            "parent_project, parent_task_number, "
            "supersedes_project, supersedes_task_number, "
            "plan_version, predecessor_task_id, kind, roles, external_refs, "
            "created_at, created_by, updated_at "
            "FROM work_tasks WHERE predecessor_task_id = %s "
            "ORDER BY project, task_number"
        )
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (predecessor_task_id,))
            rows = cur.fetchall()
        return [self._row_to_task(row) for row in rows]

    # ------------------------------------------------------------------
    # State transitions (Slice B port of the transition manager)
    #
    # These intentionally implement a simplified version of the sqlite
    # transition manager: state checks + node advancement + audit row,
    # without the post-commit side effects (session provisioning, auto-
    # repair, sync adapters, plan-review emission). Those land in
    # Slice C alongside the audit / sync adapter ports.
    # ------------------------------------------------------------------

    def claim(self, task_id: str, actor: str, skip_gates: bool = False) -> Task:  # noqa: ARG002
        """Atomically claim a queued task.

        Loads the flow template, resolves the start (or current) node,
        moves the task to ``in_progress`` (or ``review`` for a review
        start node), and writes the audit transition row.
        """
        task = self.get(task_id)
        if task.work_status != WorkStatus.QUEUED:
            raise InvalidTransitionError(
                f"Cannot claim task in '{task.work_status.value}' state. "
                f"Task must be in 'queued' state."
            )
        if task.blocked:
            raise InvalidTransitionError(
                f"Cannot claim task {task_id}: it is blocked by another task."
            )

        flow = self._load_flow(task)
        node_id = task.current_node_id or flow.start_node
        if not node_id:
            raise InvalidTransitionError(
                f"Task {task_id} has no claimable flow node."
            )
        node = flow.nodes.get(node_id)
        if node is None:
            raise InvalidTransitionError(
                f"Current node '{node_id}' not found in flow '{flow.name}'."
            )
        if node.type == NodeType.TERMINAL:
            raise InvalidTransitionError(
                f"Current node '{node_id}' is terminal and cannot be claimed."
            )

        assignee = self._resolve_node_assignee(task, node) or actor
        target_status = (
            WorkStatus.REVIEW
            if node.type == NodeType.REVIEW
            else WorkStatus.IN_PROGRESS
        )
        now = _now_iso()
        with self._pool.connection() as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE work_tasks SET work_status = %s, assignee = %s, "
                    "current_node_id = %s, updated_at = %s "
                    "WHERE project = %s AND task_number = %s",
                    (
                        target_status.value,
                        assignee,
                        node_id,
                        now,
                        task.project,
                        task.task_number,
                    ),
                )
                # If we're resuming a blocked execution, flip its status;
                # otherwise insert a fresh visit row.
                cur.execute(
                    "SELECT id, status FROM work_node_executions "
                    "WHERE task_project = %s AND task_number = %s "
                    "AND node_id = %s "
                    "ORDER BY visit DESC, id DESC LIMIT 1",
                    (task.project, task.task_number, node_id),
                )
                latest = cur.fetchone()
                if (
                    task.current_node_id is not None
                    and latest is not None
                    and latest[1] == ExecutionStatus.BLOCKED.value
                ):
                    cur.execute(
                        "UPDATE work_node_executions SET status = %s "
                        "WHERE id = %s",
                        (ExecutionStatus.ACTIVE.value, latest[0]),
                    )
                elif not (
                    task.current_node_id is not None
                    and latest is not None
                    and latest[1] == ExecutionStatus.ACTIVE.value
                ):
                    visit = self._next_visit_locked(
                        cur, task.project, task.task_number, node_id
                    )
                    cur.execute(
                        "INSERT INTO work_node_executions "
                        "(task_project, task_number, node_id, visit, "
                        "status, started_at) VALUES "
                        "(%s, %s, %s, %s, %s, %s)",
                        (
                            task.project,
                            task.task_number,
                            node_id,
                            visit,
                            ExecutionStatus.ACTIVE.value,
                            now,
                        ),
                    )
                self._insert_transition_locked(
                    cur,
                    task.project,
                    task.task_number,
                    WorkStatus.QUEUED.value,
                    target_status.value,
                    actor,
                    None,
                )
            conn.commit()
        return self.get(task_id)

    def next(  # noqa: A003
        self,
        *,
        agent: str | None = None,
        project: str | None = None,
    ) -> Task | None:
        """Highest-priority queued+unblocked task; does NOT claim."""
        clauses = ["t.work_status = %s"]
        params: list[object] = [WorkStatus.QUEUED.value]
        if project is not None:
            clauses.append("t.project = %s")
            params.append(project)
        where = " AND ".join(clauses)
        sql = (
            "SELECT t.project, t.task_number, t.project_key, t.title, t.type, "
            "t.labels, t.work_status, t.flow_template_id, "
            "t.flow_template_version, t.current_node_id, t.assignee, "
            "t.priority, t.requires_human_review, t.description, "
            "t.acceptance_criteria, t.constraints, t.relevant_files, "
            "t.parent_project, t.parent_task_number, "
            "t.supersedes_project, t.supersedes_task_number, "
            "t.plan_version, t.predecessor_task_id, t.kind, t.roles, "
            "t.external_refs, t.created_at, t.created_by, t.updated_at "
            "FROM work_tasks t "
            f"WHERE {where} "
            "ORDER BY CASE t.priority "
            "  WHEN 'critical' THEN 0 "
            "  WHEN 'high' THEN 1 "
            "  WHEN 'normal' THEN 2 "
            "  WHEN 'low' THEN 3 "
            "  ELSE 4 END, t.created_at ASC"
        )
        blocked_keys = self._unresolved_blocked_keys(project)
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        for row in rows:
            task_key = (str(row[0]), int(row[1]))
            if task_key in blocked_keys:
                continue
            task = self._row_to_task(row)
            if agent is not None and task.roles.get("worker") != agent:
                continue
            return task
        return None

    def hold(
        self, task_id: str, actor: str, reason: str | None = None
    ) -> Task:
        """Move in_progress / rework / review / queued → on_hold."""
        task = self.get(task_id)
        if task.work_status not in (
            WorkStatus.IN_PROGRESS,
            WorkStatus.REWORK,
            WorkStatus.QUEUED,
            WorkStatus.REVIEW,
        ):
            raise InvalidTransitionError(
                f"Cannot hold task in '{task.work_status.value}' state. "
                f"Task must be in 'in_progress', 'rework', 'review', "
                f"or 'queued' state."
            )
        return self._simple_transition(
            task_id,
            from_state=task.work_status,
            to_state=WorkStatus.ON_HOLD,
            actor=actor,
            reason=reason,
        )

    def resume(self, task_id: str, actor: str) -> Task:
        """Move on_hold → queued (or in_progress if a node is active)."""
        task = self.get(task_id)
        if task.work_status != WorkStatus.ON_HOLD:
            raise InvalidTransitionError(
                f"Cannot resume task in '{task.work_status.value}' state. "
                f"Task must be in 'on_hold' state."
            )
        target_status = WorkStatus.QUEUED
        if task.current_node_id:
            # If an execution is still ACTIVE for the current node, the
            # task was held mid-work and resumes in-progress/review.
            with self._pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM work_node_executions "
                    "WHERE task_project = %s AND task_number = %s "
                    "AND node_id = %s AND status = %s",
                    (
                        task.project,
                        task.task_number,
                        task.current_node_id,
                        ExecutionStatus.ACTIVE.value,
                    ),
                )
                if cur.fetchone() is not None:
                    flow = self._load_flow(task)
                    node = flow.nodes.get(task.current_node_id)
                    if node is not None:
                        target_status = (
                            WorkStatus.REVIEW
                            if node.type == NodeType.REVIEW
                            else WorkStatus.IN_PROGRESS
                        )
        return self._simple_transition(
            task_id,
            from_state=WorkStatus.ON_HOLD,
            to_state=target_status,
            actor=actor,
        )

    def node_done(
        self,
        task_id: str,
        actor: str,
        work_output: WorkOutput | dict | None = None,
        skip_gates: bool = False,  # noqa: ARG002 — gate eval is Slice C
    ) -> Task:
        """Complete a work node and advance to the next flow node."""
        task = self.get(task_id)
        if task.work_status not in (WorkStatus.IN_PROGRESS, WorkStatus.REWORK):
            raise InvalidTransitionError(
                f"Cannot complete node on task in "
                f"'{task.work_status.value}' state. "
                f"Task must be in 'in_progress' or 'rework' state."
            )
        flow = self._load_flow(task)
        if task.current_node_id is None:
            raise InvalidTransitionError("Task has no current flow node.")
        node = flow.nodes.get(task.current_node_id)
        if node is None:
            raise InvalidTransitionError(
                f"Current node '{task.current_node_id}' not found in flow "
                f"'{task.flow_template_id}'."
            )
        if node.type != NodeType.WORK:
            raise InvalidTransitionError(
                f"Current node '{task.current_node_id}' is not a work node "
                f"(type: {node.type.value})."
            )

        coerced = self._coerce_work_output(work_output)
        if coerced is None:
            raise ValidationError(
                "pm task done requires a --output payload describing "
                "what you built."
            )
        self._validate_work_output(coerced)

        now = _now_iso()
        wo_jsonb = self._serialize_work_output_for_pg(coerced)
        from_status = task.work_status
        with self._pool.connection() as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE work_node_executions SET status = %s, "
                    "work_output = %s::jsonb, completed_at = %s "
                    "WHERE task_project = %s AND task_number = %s "
                    "AND node_id = %s AND status = %s",
                    (
                        ExecutionStatus.COMPLETED.value,
                        wo_jsonb,
                        now,
                        task.project,
                        task.task_number,
                        task.current_node_id,
                        ExecutionStatus.ACTIVE.value,
                    ),
                )
                self._advance_to_node_locked(
                    cur, task, flow, node.next_node_id, actor, from_status
                )
            conn.commit()
        return self.get(task_id)

    def approve(
        self,
        task_id: str,
        actor: str,
        reason: str | None = None,
        skip_gates: bool = False,  # noqa: ARG002 — gate eval is Slice C
        resume_merge: bool = False,  # noqa: ARG002 — git auto-merge is Slice C
    ) -> Task:
        """Approve a review node and advance the flow."""
        task = self.get(task_id)
        if task.work_status != WorkStatus.REVIEW:
            raise InvalidTransitionError(
                f"Cannot approve task in '{task.work_status.value}' state. "
                f"Only tasks in 'review' can be approved."
            )
        flow = self._load_flow(task)
        if task.current_node_id is None:
            raise InvalidTransitionError("Task has no current flow node.")
        node = flow.nodes.get(task.current_node_id)
        if node is None or node.type != NodeType.REVIEW:
            raise InvalidTransitionError(
                f"Current node '{task.current_node_id}' is not a review node."
            )

        now = _now_iso()
        with self._pool.connection() as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE work_node_executions SET status = %s, "
                    "decision = %s, decision_reason = %s, completed_at = %s "
                    "WHERE task_project = %s AND task_number = %s "
                    "AND node_id = %s AND status = %s",
                    (
                        ExecutionStatus.COMPLETED.value,
                        Decision.APPROVED.value,
                        reason,
                        now,
                        task.project,
                        task.task_number,
                        task.current_node_id,
                        ExecutionStatus.ACTIVE.value,
                    ),
                )
                self._advance_to_node_locked(
                    cur, task, flow, node.next_node_id, actor, WorkStatus.REVIEW
                )
            conn.commit()
        return self.get(task_id)

    def reject(self, task_id: str, actor: str, reason: str) -> Task:
        """Reject a review node and bounce the task to ``rework``."""
        task = self.get(task_id)
        if task.work_status != WorkStatus.REVIEW:
            raise InvalidTransitionError(
                f"Cannot reject task in '{task.work_status.value}' state. "
                f"Task must be in 'review' state."
            )
        if not reason or not reason.strip():
            raise ValidationError("Reason is required for rejection.")
        flow = self._load_flow(task)
        if task.current_node_id is None:
            raise InvalidTransitionError("Task has no current flow node.")
        node = flow.nodes.get(task.current_node_id)
        if node is None or node.type != NodeType.REVIEW:
            raise InvalidTransitionError(
                f"Current node '{task.current_node_id}' is not a review node."
            )
        if node.reject_node_id is None:
            raise InvalidTransitionError(
                f"Review node '{task.current_node_id}' has no reject_node defined."
            )
        reject_target = flow.nodes.get(node.reject_node_id)
        if reject_target is None:
            raise InvalidTransitionError(
                f"Reject node '{node.reject_node_id}' not found in flow."
            )

        now = _now_iso()
        with self._pool.connection() as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE work_node_executions SET status = %s, "
                    "decision = %s, decision_reason = %s, completed_at = %s "
                    "WHERE task_project = %s AND task_number = %s "
                    "AND node_id = %s AND status = %s",
                    (
                        ExecutionStatus.COMPLETED.value,
                        Decision.REJECTED.value,
                        reason,
                        now,
                        task.project,
                        task.task_number,
                        task.current_node_id,
                        ExecutionStatus.ACTIVE.value,
                    ),
                )
                reject_assignee = self._resolve_node_assignee(task, reject_target)
                cur.execute(
                    "UPDATE work_tasks SET work_status = %s, assignee = %s, "
                    "current_node_id = %s, updated_at = %s "
                    "WHERE project = %s AND task_number = %s",
                    (
                        WorkStatus.REWORK.value,
                        reject_assignee,
                        node.reject_node_id,
                        now,
                        task.project,
                        task.task_number,
                    ),
                )
                visit = self._next_visit_locked(
                    cur, task.project, task.task_number, node.reject_node_id
                )
                cur.execute(
                    "INSERT INTO work_node_executions "
                    "(task_project, task_number, node_id, visit, status, "
                    "started_at) VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        task.project,
                        task.task_number,
                        node.reject_node_id,
                        visit,
                        ExecutionStatus.ACTIVE.value,
                        now,
                    ),
                )
                self._insert_transition_locked(
                    cur,
                    task.project,
                    task.task_number,
                    WorkStatus.REVIEW.value,
                    WorkStatus.REWORK.value,
                    actor,
                    reason,
                )
            conn.commit()
        return self.get(task_id)

    def block(self, task_id: str, actor: str, blocker_task_id: str) -> Task:
        """Mark a task blocked by ``blocker_task_id``."""
        task = self.get(task_id)
        if task.work_status not in (WorkStatus.IN_PROGRESS, WorkStatus.REVIEW):
            raise InvalidTransitionError(
                f"Cannot block task in '{task.work_status.value}' state. "
                f"Task must be in 'in_progress' or 'review' state."
            )
        # Validate blocker exists.
        self.get(blocker_task_id)
        blocker_project, blocker_number = _parse_task_id(blocker_task_id)
        if self._would_create_cycle(
            blocker_project,
            blocker_number,
            task.project,
            task.task_number,
        ):
            raise ValidationError("circular dependency detected")

        now = _now_iso()
        old_status = task.work_status
        with self._pool.connection() as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO work_task_dependencies "
                    "(from_project, from_task_number, to_project, "
                    "to_task_number, kind, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (
                        blocker_project,
                        blocker_number,
                        task.project,
                        task.task_number,
                        LinkKind.BLOCKS.value,
                        now,
                    ),
                )
                cur.execute(
                    "UPDATE work_tasks SET work_status = %s, updated_at = %s "
                    "WHERE project = %s AND task_number = %s",
                    (
                        WorkStatus.BLOCKED.value,
                        now,
                        task.project,
                        task.task_number,
                    ),
                )
                if task.current_node_id:
                    cur.execute(
                        "UPDATE work_node_executions SET status = %s "
                        "WHERE task_project = %s AND task_number = %s "
                        "AND node_id = %s AND status = %s",
                        (
                            ExecutionStatus.BLOCKED.value,
                            task.project,
                            task.task_number,
                            task.current_node_id,
                            ExecutionStatus.ACTIVE.value,
                        ),
                    )
                self._insert_transition_locked(
                    cur,
                    task.project,
                    task.task_number,
                    old_status.value,
                    WorkStatus.BLOCKED.value,
                    actor,
                    f"Blocked by {blocker_task_id}",
                )
            conn.commit()
        return self.get(task_id)

    # ------------------------------------------------------------------
    # Execution query
    # ------------------------------------------------------------------

    def get_execution(
        self,
        task_id: str,
        node_id: str | None = None,
        visit: int | None = None,
    ) -> list[FlowNodeExecution]:
        """Read execution records for a task with optional filters."""
        project, task_number = _parse_task_id(task_id)
        clauses = ["task_project = %s", "task_number = %s"]
        params: list[object] = [project, task_number]
        if node_id is not None:
            clauses.append("node_id = %s")
            params.append(node_id)
        if visit is not None:
            clauses.append("visit = %s")
            params.append(visit)
        where = " AND ".join(clauses)
        sql = (
            "SELECT task_project, task_number, node_id, visit, status, "
            "work_output, decision, decision_reason, started_at, "
            "completed_at FROM work_node_executions "
            f"WHERE {where} ORDER BY id"
        )
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        result: list[FlowNodeExecution] = []
        for row in rows:
            (
                tp,
                tn,
                nid,
                visit_v,
                status_raw,
                work_output_raw,
                decision_raw,
                decision_reason,
                started_at,
                completed_at,
            ) = row
            wo = self._decode_work_output(work_output_raw)
            try:
                status = ExecutionStatus(status_raw)
            except ValueError:
                status = ExecutionStatus.PENDING
            decision: Decision | None = None
            if decision_raw:
                try:
                    decision = Decision(decision_raw)
                except ValueError:
                    decision = None
            result.append(
                FlowNodeExecution(
                    task_id=f"{tp}/{tn}",
                    node_id=str(nid),
                    visit=int(visit_v),
                    status=status,
                    work_output=wo,
                    decision=decision,
                    decision_reason=decision_reason,
                    started_at=started_at,
                    completed_at=completed_at,
                )
            )
        return result

    # ------------------------------------------------------------------
    # Context log
    # ------------------------------------------------------------------

    def add_context(
        self,
        task_id: str,
        actor: str,
        text: str,
        *,
        entry_type: str = "note",
    ) -> ContextEntry:
        """Append a context entry to a task's log."""
        project, task_number = _parse_task_id(task_id)
        now = _now_iso()
        with self._pool.connection() as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM work_tasks "
                    "WHERE project = %s AND task_number = %s",
                    (project, task_number),
                )
                if cur.fetchone() is None:
                    raise TaskNotFoundError(f"Task '{task_id}' not found.")
                cur.execute(
                    "INSERT INTO work_context_entries "
                    "(task_project, task_number, actor, text, created_at, "
                    "entry_type) VALUES (%s, %s, %s, %s, %s, %s)",
                    (project, task_number, actor, text, now, entry_type),
                )
            conn.commit()
        return ContextEntry(
            actor=actor,
            timestamp=now,
            text=text,
            entry_type=entry_type,
        )

    def get_context(
        self,
        task_id: str,
        limit: int | None = None,
        since: datetime | None = None,
        entry_type: str | None = None,
    ) -> list[ContextEntry]:
        """Query context entries for a task, most recent first."""
        project, task_number = _parse_task_id(task_id)
        clauses = ["task_project = %s", "task_number = %s"]
        params: list[object] = [project, task_number]
        if since is not None:
            clauses.append("created_at > %s")
            params.append(since)
        if entry_type is not None:
            clauses.append("entry_type = %s")
            params.append(entry_type)
        where = " AND ".join(clauses)
        sql = (
            "SELECT actor, created_at, text, entry_type "
            "FROM work_context_entries "
            f"WHERE {where} ORDER BY id DESC"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [
            ContextEntry(
                actor=str(r[0]),
                timestamp=r[1],
                text=str(r[2]),
                entry_type=str(r[3] or "note"),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Dependencies
    # ------------------------------------------------------------------

    def link(self, from_id: str, to_id: str, kind: str) -> None:
        """Create a relationship between two tasks."""
        try:
            link_kind = LinkKind(kind)
        except ValueError as exc:
            raise ValidationError(
                f"Invalid link kind '{kind}'. "
                f"Must be one of: {[item.value for item in LinkKind]}."
            ) from exc

        from_project, from_number = _parse_task_id(from_id)
        to_project, to_number = _parse_task_id(to_id)

        with self._pool.connection() as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                for project, number, tid in (
                    (from_project, from_number, from_id),
                    (to_project, to_number, to_id),
                ):
                    cur.execute(
                        "SELECT 1 FROM work_tasks "
                        "WHERE project = %s AND task_number = %s",
                        (project, number),
                    )
                    if cur.fetchone() is None:
                        raise TaskNotFoundError(f"Task '{tid}' not found.")
                if link_kind is LinkKind.BLOCKS and self._would_create_cycle(
                    from_project, from_number, to_project, to_number
                ):
                    raise ValidationError("circular dependency detected")
                cur.execute(
                    "INSERT INTO work_task_dependencies "
                    "(from_project, from_task_number, to_project, "
                    "to_task_number, kind, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (
                        from_project,
                        from_number,
                        to_project,
                        to_number,
                        link_kind.value,
                        _now_iso(),
                    ),
                )
            conn.commit()
        if link_kind is LinkKind.BLOCKS:
            self._maybe_block(to_id)

    def unlink(self, from_id: str, to_id: str, kind: str) -> None:
        """Remove a relationship between two tasks."""
        try:
            link_kind = LinkKind(kind)
        except ValueError as exc:
            raise ValidationError(
                f"Invalid link kind '{kind}'. "
                f"Must be one of: {[item.value for item in LinkKind]}."
            ) from exc
        from_project, from_number = _parse_task_id(from_id)
        to_project, to_number = _parse_task_id(to_id)
        with self._pool.connection() as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM work_task_dependencies "
                    "WHERE from_project = %s AND from_task_number = %s "
                    "AND to_project = %s AND to_task_number = %s "
                    "AND kind = %s",
                    (
                        from_project,
                        from_number,
                        to_project,
                        to_number,
                        link_kind.value,
                    ),
                )
            conn.commit()
        if link_kind is LinkKind.BLOCKS:
            self._maybe_unblock(to_id)

    def dependents(self, task_id: str) -> list[Task]:
        """All tasks blocked by this task, transitively."""
        project, number = _parse_task_id(task_id)
        sql = """
            WITH RECURSIVE deps(project, task_number) AS (
                SELECT to_project, to_task_number
                FROM work_task_dependencies
                WHERE from_project = %s AND from_task_number = %s
                  AND kind = %s
                UNION
                SELECT d.to_project, d.to_task_number
                FROM work_task_dependencies d
                JOIN deps
                  ON d.from_project = deps.project
                 AND d.from_task_number = deps.task_number
                WHERE d.kind = %s
            )
            SELECT DISTINCT project, task_number
            FROM deps
            ORDER BY project, task_number
        """
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                sql,
                (project, number, LinkKind.BLOCKS.value, LinkKind.BLOCKS.value),
            )
            task_keys = [(row[0], int(row[1])) for row in cur.fetchall()]
        return [self.get(f"{p}/{n}") for p, n in task_keys]

    # ------------------------------------------------------------------
    # Flow templates
    # ------------------------------------------------------------------

    def available_flows(self, project: str | None = None) -> list[FlowTemplate]:
        """List available flows (file-based — pg backend reuses resolver)."""
        from pollypm.work.flow_engine import (
            available_flows as _available_flows,
            resolve_flow,
        )

        project_path = self._resolve_project_path(project)
        flow_map = _available_flows(project_path)
        templates: list[FlowTemplate] = []
        for name in flow_map:
            try:
                templates.append(resolve_flow(name, project_path))
            except Exception:  # noqa: BLE001 — skip unresolvable templates
                logger.debug(
                    "skipping unavailable flow template %s", name, exc_info=True
                )
        return templates

    def get_flow(
        self, name: str, project: str | None = None
    ) -> FlowTemplate:
        """Resolve a flow by name."""
        from pollypm.work.flow_engine import resolve_flow

        return resolve_flow(name, self._resolve_project_path(project))

    # ------------------------------------------------------------------
    # Gate dry-run
    # ------------------------------------------------------------------

    def validate_advance(
        self, task_id: str, actor: str
    ) -> list[GateResult]:
        """Dry-run preflight: would advancing the current node succeed?

        Slice B ships the actor-role check. Full gate evaluation lands
        in Slice C alongside the gate registry / kwargs port.
        """
        task = self.get(task_id)
        if task.current_node_id is None:
            return []
        try:
            flow = self._load_flow(task)
            node = flow.nodes.get(task.current_node_id)
        except (TaskNotFoundError, InvalidTransitionError):
            return []
        if node is None:
            return []
        results: list[GateResult] = []
        try:
            self._validate_actor_role(task, node, actor)
        except Exception as exc:  # noqa: BLE001
            results.append(
                GateResult(
                    passed=False,
                    reason=str(exc),
                    gate_name="actor_role",
                    gate_type="hard",
                )
            )
        return results

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    def sync_status(self, task_id: str) -> dict[str, object]:
        """Current sync state per adapter for a task.

        Slice B reads ``work_sync_state`` and returns the per-adapter
        map. The actual adapter list (and force-sync) lands when Slice
        C wires the sync manager. Without an adapter manager every task
        returns an empty dict.
        """
        project, task_number = _parse_task_id(task_id)
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM work_tasks "
                "WHERE project = %s AND task_number = %s",
                (project, task_number),
            )
            if cur.fetchone() is None:
                raise TaskNotFoundError(f"Task '{task_id}' not found.")
            cur.execute(
                "SELECT adapter_name, last_synced_at, last_error, attempts "
                "FROM work_sync_state "
                "WHERE task_project = %s AND task_number = %s",
                (project, task_number),
            )
            rows = cur.fetchall()
        return {
            str(r[0]): {
                "last_synced_at": r[1],
                "last_error": r[2],
                "attempts": int(r[3] or 0),
            }
            for r in rows
        }

    def trigger_sync(
        self,
        task_id: str | None = None,
        adapter: str | None = None,  # noqa: ARG002 — adapter wiring is Slice C
    ) -> dict[str, object]:
        """Force a sync cycle.

        Slice B returns ``{synced: 0, errors: {}}`` because the pg
        service has no sync adapter manager yet — Slice C ports
        ``service_sync.SyncManager`` to the pg pool. Callers can already
        invoke this method; the sqlite path still goes through its own
        adapters when they're registered there.
        """
        if task_id is not None:
            project, task_number = _parse_task_id(task_id)
            with self._pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM work_tasks "
                    "WHERE project = %s AND task_number = %s",
                    (project, task_number),
                )
                if cur.fetchone() is None:
                    raise TaskNotFoundError(f"Task '{task_id}' not found.")
        return {"synced": 0, "errors": {}}

    # ------------------------------------------------------------------
    # Aggregate queries
    # ------------------------------------------------------------------

    def state_counts(
        self, project: str | None = None
    ) -> dict[str, int]:
        """Task counts by state, zero-filled across every WorkStatus."""
        counts = {status.value: 0 for status in WorkStatus}
        sql = "SELECT work_status, COUNT(*) FROM work_tasks"
        params: list[object] = []
        if project is not None:
            sql += " WHERE project = %s"
            params.append(project)
        sql += " GROUP BY work_status"
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            for row in cur.fetchall():
                counts[str(row[0])] = int(row[1])
        return counts

    def my_tasks(self, agent: str) -> list[Task]:
        """All tasks where ``agent`` is the live assignee."""
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
            "WHERE current_node_id IS NOT NULL AND assignee = %s "
            "ORDER BY project, task_number"
        )
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (agent,))
            rows = cur.fetchall()
        return [self._row_to_task(row) for row in rows]

    def blocked_tasks(self, project: str | None = None) -> list[Task]:
        """All tasks with ``work_status == blocked``."""
        clauses = ["work_status = %s"]
        params: list[object] = [WorkStatus.BLOCKED.value]
        if project is not None:
            clauses.append("project = %s")
            params.append(project)
        sql = (
            "SELECT project, task_number, project_key, title, type, labels, "
            "work_status, flow_template_id, flow_template_version, "
            "current_node_id, assignee, priority, requires_human_review, "
            "description, acceptance_criteria, constraints, relevant_files, "
            "parent_project, parent_task_number, "
            "supersedes_project, supersedes_task_number, "
            "plan_version, predecessor_task_id, kind, roles, external_refs, "
            "created_at, created_by, updated_at "
            f"FROM work_tasks WHERE {' AND '.join(clauses)} "
            "ORDER BY project, task_number"
        )
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [self._row_to_task(row) for row in rows]

    # ------------------------------------------------------------------
    # Worker sessions
    # ------------------------------------------------------------------

    def ensure_worker_session_schema(self) -> None:
        # Schema is installed by the migrations applier — no per-instance
        # bootstrap needed on the pg backend.
        return None

    def upsert_worker_session(
        self,
        *,
        task_project: str,
        task_number: int,
        agent_name: str,
        pane_id: str,
        worktree_path: str,
        branch_name: str,
        started_at: str | datetime,
        provider: str | None = None,
        provider_home: str | None = None,
    ) -> None:
        """Insert or refresh the work_sessions row for a task."""
        with self._pool.connection() as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO work_sessions ("
                    "task_project, task_number, agent_name, pane_id, "
                    "worktree_path, branch_name, started_at, provider, "
                    "provider_home) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (task_project, task_number) DO UPDATE SET "
                    "pane_id = EXCLUDED.pane_id, "
                    "worktree_path = EXCLUDED.worktree_path, "
                    "branch_name = EXCLUDED.branch_name, "
                    "started_at = EXCLUDED.started_at, "
                    "provider = EXCLUDED.provider, "
                    "provider_home = EXCLUDED.provider_home, "
                    "ended_at = NULL, archive_path = NULL",
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
            conn.commit()

    def get_worker_session(
        self,
        *,
        task_project: str,
        task_number: int,
        active_only: bool = False,
    ) -> WorkerSessionRecord | None:
        """Read one worker_session row."""
        sql = (
            "SELECT task_project, task_number, agent_name, pane_id, "
            "worktree_path, branch_name, started_at, ended_at, "
            "total_input_tokens, total_output_tokens, archive_path, "
            "provider, provider_home FROM work_sessions "
            "WHERE task_project = %s AND task_number = %s"
        )
        if active_only:
            sql += " AND ended_at IS NULL"
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (task_project, task_number))
            row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_worker_session(row)

    def list_worker_sessions(
        self,
        *,
        project: str | None = None,
        active_only: bool = True,
    ) -> list[WorkerSessionRecord]:
        """List worker_session rows."""
        clauses: list[str] = []
        params: list[object] = []
        if active_only:
            clauses.append("ended_at IS NULL")
        if project is not None:
            clauses.append("task_project = %s")
            params.append(project)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT task_project, task_number, agent_name, pane_id, "
            "worktree_path, branch_name, started_at, ended_at, "
            "total_input_tokens, total_output_tokens, archive_path, "
            "provider, provider_home FROM work_sessions" + where
        )
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [self._row_to_worker_session(row) for row in rows]

    def end_worker_session(
        self,
        *,
        task_project: str,
        task_number: int,
        ended_at: str | datetime,
        total_input_tokens: int,
        total_output_tokens: int,
        archive_path: str | None,
    ) -> None:
        """Mark a worker_session as ended and record final token counts."""
        with self._pool.connection() as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE work_sessions SET ended_at = %s, "
                    "total_input_tokens = %s, total_output_tokens = %s, "
                    "archive_path = %s "
                    "WHERE task_project = %s AND task_number = %s",
                    (
                        ended_at,
                        total_input_tokens,
                        total_output_tokens,
                        archive_path,
                        task_project,
                        task_number,
                    ),
                )
            conn.commit()

    def mark_worker_session_ended(
        self,
        *,
        task_project: str,
        task_number: int,
        ended_at: str | datetime,
    ) -> None:
        """Stamp ``ended_at`` without zeroing tokens (#1014)."""
        with self._pool.connection() as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE work_sessions SET ended_at = %s "
                    "WHERE task_project = %s AND task_number = %s",
                    (ended_at, task_project, task_number),
                )
            conn.commit()

    def update_worker_session_tokens(
        self,
        *,
        task_project: str,
        task_number: int,
        total_input_tokens: int,
        total_output_tokens: int,
        archive_path: str | None,
    ) -> None:
        """Refresh token totals on an open or completed session row."""
        with self._pool.connection() as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE work_sessions SET total_input_tokens = %s, "
                    "total_output_tokens = %s, archive_path = %s "
                    "WHERE task_project = %s AND task_number = %s",
                    (
                        total_input_tokens,
                        total_output_tokens,
                        archive_path,
                        task_project,
                        task_number,
                    ),
                )
            conn.commit()

    # ------------------------------------------------------------------
    # Internal helpers (private)
    # ------------------------------------------------------------------

    def _row_to_worker_session(self, row: tuple) -> WorkerSessionRecord:
        (
            tp,
            tn,
            agent,
            pane,
            worktree,
            branch,
            started,
            ended,
            tot_in,
            tot_out,
            archive,
            provider,
            provider_home,
        ) = row
        return WorkerSessionRecord(
            task_project=str(tp),
            task_number=int(tn),
            agent_name=str(agent),
            pane_id=pane,
            worktree_path=worktree,
            branch_name=branch,
            started_at=started.isoformat() if hasattr(started, "isoformat") else started,
            ended_at=(
                ended.isoformat() if ended is not None and hasattr(ended, "isoformat") else ended
            ),
            total_input_tokens=int(tot_in or 0),
            total_output_tokens=int(tot_out or 0),
            archive_path=archive,
            provider=provider,
            provider_home=provider_home,
        )

    def _load_flow(self, task: Task) -> FlowTemplate:
        """Resolve the task's flow template from the file system.

        Slice B uses the file-based resolver (same as sqlite's
        ``available_flows`` fallback) because the pg ``work_flow_*``
        tables are populated on demand by the sqlite path and the
        Slice B port does not yet write to them. Slice C adds a DB
        cache layer.
        """
        from pollypm.work.flow_engine import resolve_flow

        try:
            return resolve_flow(
                task.flow_template_id, self._resolve_project_path(task.project)
            )
        except Exception as exc:  # noqa: BLE001
            raise InvalidTransitionError(
                f"Flow template '{task.flow_template_id}' not found: {exc}"
            ) from exc

    def _resolve_project_path(self, project: str | None):
        """Resolve a project name to a filesystem path via config.

        Returns ``None`` when no path is registered; the file-based
        flow resolver tolerates ``None`` and falls back to the bundled
        flow set.
        """
        if project is None:
            return None
        try:
            from pollypm.config import load_config

            config = load_config()
            normalized = project.replace("-", "_")
            key = (
                project
                if project in config.projects
                else (normalized if normalized in config.projects else None)
            )
            if key is not None:
                return config.projects[key].path
        except Exception:  # noqa: BLE001 — config lookup is best-effort
            logger.debug(
                "project path config lookup failed for %s", project, exc_info=True
            )
        return None

    def _resolve_node_assignee(
        self, task: Task, node: FlowNode
    ) -> str | None:
        if node.actor_type == ActorType.ROLE:
            return task.roles.get(node.actor_role or "", task.assignee)
        if node.actor_type == ActorType.HUMAN:
            return "human"
        if node.actor_type == ActorType.PROJECT_MANAGER:
            return "project_manager"
        if node.actor_type == ActorType.AGENT:
            return node.agent_name or task.assignee
        return task.assignee

    _HUMAN_ACTOR_NAMES = frozenset({"human", "user", "sam"})

    def _validate_actor_role(
        self, task: Task, node: FlowNode, actor: str
    ) -> None:
        if node.actor_type == ActorType.HUMAN:
            reviewer = None
            if node.actor_role:
                reviewer = task.roles.get(node.actor_role)
            allowed = []
            seen = set()
            for name in (reviewer, *sorted(self._HUMAN_ACTOR_NAMES)):
                if name and name not in seen:
                    allowed.append(name)
                    seen.add(name)
            if actor not in allowed:
                raise ValidationError(
                    f"Node '{node.name}' requires human review. "
                    f"Actor '{actor}' is not authorized."
                )
        elif node.actor_type == ActorType.ROLE and node.actor_role:
            expected = task.roles.get(node.actor_role)
            if expected and actor != expected:
                if actor != node.actor_role:
                    raise ValidationError(
                        f"Actor '{actor}' does not match role "
                        f"'{node.actor_role}' (expected '{expected}')."
                    )
            elif expected is None and actor != node.actor_role:
                raise ValidationError(
                    f"Actor '{actor}' does not match role '{node.actor_role}'."
                )
        elif node.actor_type == ActorType.AGENT and node.agent_name:
            if actor != node.agent_name:
                raise ValidationError(
                    f"Node '{node.name}' is pinned to agent "
                    f"'{node.agent_name}'. Actor '{actor}' is not authorized."
                )

    def _advance_to_node_locked(
        self,
        cur,
        task: Task,
        flow: FlowTemplate,
        next_node_id: str | None,
        actor: str,
        from_status: WorkStatus,
    ) -> None:
        """Advance task to ``next_node_id`` (or terminal). Mutates inside an open tx."""
        now = _now_iso()
        if next_node_id is None:
            raise InvalidTransitionError("No next node defined.")
        next_node = flow.nodes.get(next_node_id)
        if next_node is None:
            raise InvalidTransitionError(
                f"Next node '{next_node_id}' not found in flow."
            )
        if next_node.type == NodeType.TERMINAL:
            cur.execute(
                "UPDATE work_tasks SET work_status = %s, "
                "current_node_id = NULL, updated_at = %s "
                "WHERE project = %s AND task_number = %s",
                (WorkStatus.DONE.value, now, task.project, task.task_number),
            )
            self._insert_transition_locked(
                cur,
                task.project,
                task.task_number,
                from_status.value,
                WorkStatus.DONE.value,
                actor,
                None,
            )
            return
        new_status = (
            WorkStatus.REVIEW
            if next_node.type == NodeType.REVIEW
            else WorkStatus.IN_PROGRESS
        )
        next_assignee = self._resolve_node_assignee(task, next_node)
        visit = self._next_visit_locked(
            cur, task.project, task.task_number, next_node_id
        )
        cur.execute(
            "UPDATE work_tasks SET work_status = %s, assignee = %s, "
            "current_node_id = %s, updated_at = %s "
            "WHERE project = %s AND task_number = %s",
            (
                new_status.value,
                next_assignee,
                next_node_id,
                now,
                task.project,
                task.task_number,
            ),
        )
        cur.execute(
            "INSERT INTO work_node_executions "
            "(task_project, task_number, node_id, visit, status, started_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                task.project,
                task.task_number,
                next_node_id,
                visit,
                ExecutionStatus.ACTIVE.value,
                now,
            ),
        )
        self._insert_transition_locked(
            cur,
            task.project,
            task.task_number,
            from_status.value,
            new_status.value,
            actor,
            None,
        )

    def _next_visit_locked(
        self, cur, project: str, task_number: int, node_id: str
    ) -> int:
        cur.execute(
            "SELECT COALESCE(MAX(visit), 0) + 1 "
            "FROM work_node_executions "
            "WHERE task_project = %s AND task_number = %s AND node_id = %s",
            (project, task_number, node_id),
        )
        return int(cur.fetchone()[0])

    def _insert_transition_locked(
        self,
        cur,
        project: str,
        task_number: int,
        from_state: str,
        to_state: str,
        actor: str,
        reason: str | None,
    ) -> None:
        cur.execute(
            "INSERT INTO work_transitions ("
            "task_project, task_number, from_state, to_state, "
            "actor, reason, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                project,
                task_number,
                from_state,
                to_state,
                actor,
                reason,
                _now_iso(),
            ),
        )

    def _unresolved_blocked_keys(
        self, project: str | None
    ) -> set[tuple[str, int]]:
        clauses = [
            "d.kind = %s",
            "t.work_status NOT IN (%s, %s)",
        ]
        params: list[object] = [
            LinkKind.BLOCKS.value,
            WorkStatus.DONE.value,
            WorkStatus.CANCELLED.value,
        ]
        if project is not None:
            clauses.append("d.to_project = %s")
            params.append(project)
        sql = (
            "SELECT DISTINCT d.to_project, d.to_task_number "
            "FROM work_task_dependencies d "
            "JOIN work_tasks t "
            "  ON t.project = d.from_project "
            " AND t.task_number = d.from_task_number "
            f"WHERE {' AND '.join(clauses)}"
        )
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return {(str(r[0]), int(r[1])) for r in cur.fetchall()}

    def _would_create_cycle(
        self,
        from_project: str,
        from_number: int,
        to_project: str,
        to_number: int,
    ) -> bool:
        target = (from_project, from_number)
        visited: set[tuple[str, int]] = set()
        stack: list[tuple[str, int]] = [(to_project, to_number)]
        with self._pool.connection() as conn, conn.cursor() as cur:
            while stack:
                current = stack.pop()
                if current == target:
                    return True
                if current in visited:
                    continue
                visited.add(current)
                cur.execute(
                    "SELECT to_project, to_task_number "
                    "FROM work_task_dependencies "
                    "WHERE from_project = %s AND from_task_number = %s "
                    "AND kind = %s",
                    (current[0], current[1], LinkKind.BLOCKS.value),
                )
                for row in cur.fetchall():
                    stack.append((str(row[0]), int(row[1])))
        return False

    def _has_unresolved_blockers(self, task_id: str) -> bool:
        project, number = _parse_task_id(task_id)
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT t.work_status FROM work_task_dependencies d "
                "JOIN work_tasks t "
                "  ON t.project = d.from_project "
                " AND t.task_number = d.from_task_number "
                "WHERE d.to_project = %s AND d.to_task_number = %s "
                "AND d.kind = %s",
                (project, number, LinkKind.BLOCKS.value),
            )
            for row in cur.fetchall():
                if row[0] not in (
                    WorkStatus.DONE.value,
                    WorkStatus.CANCELLED.value,
                ):
                    return True
        return False

    def _maybe_block(self, task_id: str) -> None:
        task = self.get(task_id)
        if task.work_status not in (WorkStatus.QUEUED, WorkStatus.IN_PROGRESS):
            return
        if not self._has_unresolved_blockers(task_id):
            return
        self._simple_transition(
            task_id,
            from_state=task.work_status,
            to_state=WorkStatus.BLOCKED,
            actor="system",
            reason="blocked by dependency",
        )

    def _maybe_unblock(self, task_id: str) -> None:
        task = self.get(task_id)
        if task.work_status != WorkStatus.BLOCKED:
            return
        if self._has_unresolved_blockers(task_id):
            return
        self._simple_transition(
            task_id,
            from_state=WorkStatus.BLOCKED,
            to_state=WorkStatus.QUEUED,
            actor="system",
            reason="all blockers resolved",
        )

    # ------------------------------------------------------------------
    # Work-output coercion / serialization
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_work_output(output: WorkOutput) -> None:
        if not isinstance(output.type, OutputType):
            try:
                OutputType(output.type)
            except (ValueError, KeyError):
                raise ValidationError(
                    f"Invalid output type '{output.type}'."
                )
        if not output.summary or not output.summary.strip():
            raise ValidationError("Work output has an empty summary.")
        if not output.artifacts:
            raise ValidationError(
                "Work output must have at least one artifact."
            )
        for art in output.artifacts:
            if not isinstance(art.kind, ArtifactKind):
                try:
                    ArtifactKind(art.kind)
                except (ValueError, KeyError):
                    raise ValidationError(
                        f"Invalid artifact kind '{art.kind}'."
                    )

    @staticmethod
    def _coerce_work_output(
        output: WorkOutput | dict | None,
    ) -> WorkOutput | None:
        if output is None:
            return None
        if isinstance(output, WorkOutput):
            return output
        if not isinstance(output, dict):
            raise ValidationError(
                "Work output must be a WorkOutput or dict."
            )
        from pollypm.work.models import Artifact

        try:
            otype = OutputType(output.get("type", ""))
        except (ValueError, KeyError):
            raise ValidationError(
                f"Invalid output type '{output.get('type')!r}'."
            )
        artifacts_raw = output.get("artifacts") or []
        artifacts: list[Artifact] = []
        for art in artifacts_raw:
            if isinstance(art, Artifact):
                artifacts.append(art)
                continue
            if not isinstance(art, dict):
                raise ValidationError("Artifact entries must be objects.")
            try:
                kind = ArtifactKind(art.get("kind", ""))
            except (ValueError, KeyError):
                raise ValidationError(
                    f"Invalid artifact kind '{art.get('kind')!r}'."
                )
            artifacts.append(
                Artifact(
                    kind=kind,
                    description=str(art.get("description", "")),
                    ref=art.get("ref"),
                    path=art.get("path"),
                    external_ref=art.get("external_ref"),
                )
            )
        return WorkOutput(
            type=otype,
            summary=str(output.get("summary", "")),
            artifacts=artifacts,
        )

    @staticmethod
    def _serialize_work_output_for_pg(output: WorkOutput) -> str:
        return json.dumps(
            {
                "type": output.type.value
                if isinstance(output.type, OutputType)
                else str(output.type),
                "summary": output.summary,
                "artifacts": [
                    {
                        "kind": (
                            a.kind.value
                            if isinstance(a.kind, ArtifactKind)
                            else str(a.kind)
                        ),
                        "description": a.description,
                        "ref": a.ref,
                        "path": a.path,
                        "external_ref": a.external_ref,
                    }
                    for a in output.artifacts
                ],
            }
        )

    @staticmethod
    def _decode_work_output(raw: Any) -> WorkOutput | None:
        if raw is None:
            return None
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                return None
        if not isinstance(raw, dict):
            return None
        from pollypm.work.models import Artifact

        try:
            otype = OutputType(raw.get("type", ""))
        except (ValueError, KeyError):
            return None
        artifacts: list[Artifact] = []
        for art in raw.get("artifacts") or []:
            if not isinstance(art, dict):
                continue
            try:
                kind = ArtifactKind(art.get("kind", ""))
            except (ValueError, KeyError):
                continue
            artifacts.append(
                Artifact(
                    kind=kind,
                    description=str(art.get("description", "")),
                    ref=art.get("ref"),
                    path=art.get("path"),
                    external_ref=art.get("external_ref"),
                )
            )
        return WorkOutput(
            type=otype,
            summary=str(raw.get("summary", "")),
            artifacts=artifacts,
        )


__all__ = ["PgWorkService"]
