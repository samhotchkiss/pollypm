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

from pollypm.claim_breadcrumbs import (
    CLAIM_ALREADY_CLAIMED_REASON,
    CLAIM_ATTEMPTED_BY_LOSER,
    CLAIM_WON_BY,
    build_claim_breadcrumb_metadata,
    build_claim_breadcrumb_text,
)
from pollypm.inbox.kind import InboxItemKind, coerce_kind as _coerce_inbox_kind
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
    TaskSummaryCursorError,
    TaskSummaryProjection,
    TaskType,
    TERMINAL_STATUSES,
    Transition,
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
    from pathlib import Path

    from psycopg_pool import ConnectionPool

    from pollypm.models import PollyPMConfig
    from pollypm.work.sync import SyncManager

logger = logging.getLogger(__name__)

_CLAIM_CONTESTED_STATUSES = frozenset({
    WorkStatus.IN_PROGRESS,
    WorkStatus.REVIEW,
})

# #2305 — task states that hold an active per-task worker session row
# (``work_sessions.ended_at IS NULL``). When a transition leaves one of
# these states for anything else, the session row must be stamped ended
# or the per-project ``max_parallel_workers`` cap leaks: cap is counted
# from active session rows, and a leaked row 429s every subsequent claim
# until ``pm serve`` restarts.
_WORKER_SESSION_HOLDING_STATUSES = frozenset({
    WorkStatus.IN_PROGRESS,
    WorkStatus.REVIEW,
    WorkStatus.REWORK,
})

# Per-process dedup for the ``work_db.opened`` audit row (#1808). The
# event is a doctor / heartbeat diagnostic stamped at first open of a
# given (subject, project_path) pair — subsequent opens within the
# same interpreter are noise. The sqlite service applies the same
# pattern; keep them symmetric so a future audit-volume regression is
# easier to spot.
_emitted_pg_work_db_opened: set[tuple[str, str | None]] = set()


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


def _is_cap_exceeded_error(exc: BaseException) -> bool:
    """Return True when ``exc`` is a ``WorkerCapExceededError`` (#1906).

    Matched by qualified class name to avoid importing
    ``pollypm.work.session_manager`` at module-load time (the session
    manager imports this service in some wiring paths, so the explicit
    import would risk circularity).
    """
    for cls in type(exc).__mro__:
        if cls.__name__ == "WorkerCapExceededError":
            return True
    return False


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


def _empty_rels() -> dict:
    """Return an empty relationships dict with stable shape."""
    return {
        "blocks": [],
        "blocked_by": [],
        "relates_to": [],
        "children": [],
        "superseded_by_project": None,
        "superseded_by_task_number": None,
    }


def _aggregate_relationship_rows(rows: list) -> dict:
    """Aggregate raw dependency edges into the per-task relationships dict.

    Row shape: ``(from_project, from_task_number, to_project,
    to_task_number, kind, is_outgoing, is_incoming)``. Mirrors the
    aggregation in :meth:`SQLiteWorkService._load_relationships` so the
    pg and sqlite paths produce identical Task hydration shapes.
    """
    rels = _empty_rels()
    for row in rows:
        from_p = str(row[0])
        from_n = int(row[1])
        to_p = str(row[2])
        to_n = int(row[3])
        kind = row[4]
        is_outgoing = bool(row[5])
        is_incoming = bool(row[6])
        if is_outgoing:
            target = (to_p, to_n)
            if kind == LinkKind.BLOCKS.value:
                rels["blocks"].append(target)
            elif kind == LinkKind.RELATES_TO.value:
                rels["relates_to"].append(target)
            elif kind == LinkKind.PARENT.value:
                rels["children"].append(target)
            elif kind == LinkKind.SUPERSEDES.value:
                # outgoing supersedes is stored on the supersedes_* columns
                pass
        if is_incoming:
            source = (from_p, from_n)
            if kind == LinkKind.BLOCKS.value:
                rels["blocked_by"].append(source)
            elif kind == LinkKind.RELATES_TO.value:
                if source not in rels["relates_to"]:
                    rels["relates_to"].append(source)
            elif kind == LinkKind.PARENT.value:
                # incoming parent edge — the parent_* columns already
                # carry this on the task row, so no extra bookkeeping.
                pass
            elif kind == LinkKind.SUPERSEDES.value:
                rels["superseded_by_project"] = from_p
                rels["superseded_by_task_number"] = from_n
    return rels


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


# #2064 round-9 blocker #4 / round-11 blocker #3: states a task can be
# in and still receive a mid-flight reassign. Defined as the inverse of
# the "no live worker" set (``draft``/``done``/``cancelled``) PLUS
# ``queued`` (round-11): queued dispatch does not use the ``assignee``
# column as the routing source of truth — :meth:`PgWorkService.next`
# filters by ``task.roles["worker"]`` at ``pg_service.py:2059-2060``,
# and :meth:`claim` resolves the assignee from the node role
# (``_resolve_node_assignee`` at ``:4254-4256``), so a reassign on a
# queued task would set ``assignee`` to the new owner but the next
# ``claim()`` would still route to the original ``roles["worker"]``.
# Reassign is a mid-flight worker SWAP, not a queue-time routing
# change — the operator should cancel + re-queue with role assignment
# instead. Everything else (``blocked`` / ``on_hold`` / ``rework`` /
# ``review``) is a live lane with a recoverable worker context. Keep
# this list in sync with :class:`pollypm.work.models.WorkStatus`; the
# assignment is asserted in ``tests/test_pg_work_service_full.py``.
_REASSIGN_ALLOWED_STATUSES: frozenset[str] = frozenset({
    WorkStatus.IN_PROGRESS.value,
    WorkStatus.REWORK.value,
    WorkStatus.BLOCKED.value,
    WorkStatus.ON_HOLD.value,
    WorkStatus.REVIEW.value,
})


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
        sync_manager: "SyncManager | None" = None,
        session_manager: object | None = None,
        project_path: "Path | None" = None,
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
        # #1825: store the constructor config so flow resolution honours
        # the caller's config rather than reaching for ``load_config()``
        # without a path. Multi-workspace / test-isolated callers depend
        # on this so flow templates resolve from the project root the
        # service was opened against.
        self._config = config
        self._project_key = project_key or ""
        # Sync manager — optional collaborator that mirrors task lifecycle
        # events (create / update / transition) onto registered adapters
        # (file, github, etc.). The cockpit / CLI wires this from
        # ``pollypm.work.factory``; tests pass a hand-built one. Same
        # shape as ``SQLiteWorkService._sync`` so the structural helpers
        # in :mod:`pollypm.work.service_sync` can address either backend.
        self._sync = sync_manager
        self._session_mgr = session_manager
        # ``project_path`` is the on-disk root for filesystem-aware gates
        # (receipt lookup, audit log per-project paths). The sqlite path
        # carries this attribute too; pg-only callers can leave it
        # ``None`` and the file-aware gates degrade to project-less mode.
        self._project_path = project_path
        # Slice A keeps the same single-process schema-on-open contract
        # the sqlite service has: open the service, schema is current.
        # Slice E adds an explicit ``pm storage migrate`` CLI; until
        # then the constructor is the only writer that runs DDL.
        applied_versions: list[int] = []
        if apply_migrations:
            from pollypm.storage.pg_migrations import apply_migrations as run

            summary = run(self._pool)
            try:
                applied_versions = [int(v) for v, _ in getattr(summary, "applied", [])]
            except (TypeError, ValueError):  # pragma: no cover — defensive
                applied_versions = []

        # Match the sqlite service's last-error breadcrumb (#243) so
        # cockpit code that reads this attribute via Protocol shape
        # doesn't crash on the pg backend.
        self.last_provision_error: str | None = None
        self.last_first_shipped_created: bool = False

        # #1787: emit ``work_db.opened`` to mirror the sqlite service's
        # audit trail. Without this, central tail watchers and ``pm
        # doctor`` cannot tell when a pg-backed service was opened.
        # Best-effort — audit failure must never block init.
        #
        # #1808: dedup per (subject, project_path) per process. Cockpit
        # panels reopen the service multiple times per second; the
        # event is a startup stamp, not a per-call signal.
        _dedup_key = (
            "postgres",
            str(self._project_path) if self._project_path is not None else None,
        )
        if _dedup_key not in _emitted_pg_work_db_opened:
            _emitted_pg_work_db_opened.add(_dedup_key)
            try:
                from pollypm.audit import emit as _audit_emit
                from pollypm.audit.log import EVENT_WORK_DB_OPENED

                _audit_emit(
                    event=EVENT_WORK_DB_OPENED,
                    project="_workspace",
                    subject="postgres",
                    actor="system",
                    metadata={
                        "backend": "postgres",
                        "tables_created": bool(applied_versions),
                        "applied_migrations": applied_versions,
                        "project_path": (
                            str(self._project_path)
                            if self._project_path is not None
                            else None
                        ),
                    },
                    project_path=self._project_path,
                )
            except Exception:  # noqa: BLE001 — audit must never break init
                logger.debug(
                    "pg work DB opened audit emit failed", exc_info=True
                )

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

    def set_session_manager(self, session_manager: object) -> None:
        """Wire up the session manager after construction.

        Two-phase init: the service is created first, then the session
        manager (which needs a reference to the service) is created and
        registered back. Mirrors :meth:`SQLiteWorkService.set_session_manager`.
        """
        self._session_mgr = session_manager

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def get(self, task_id: str) -> Task:
        """Read one task by its ``project/number`` id."""
        project, task_number = _parse_task_id(task_id)
        sql = """
            SELECT project, task_number, project_key, title, type, labels,
                   work_status, flow_template_id, flow_template_version,
                   current_node_id, assignee, claimed_by_session,
                   priority, requires_human_review,
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
        rels = self._load_relationships(project, task_number)
        task = self._row_to_task(row, relationships=rels)
        task.transitions = self._load_transitions(project, task_number)
        task.context = self._load_context_entries(project, task_number)
        return task

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
            "current_node_id, assignee, claimed_by_session, "
            "priority, requires_human_review, "
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
        # Bulk-load relationships keyed by (project, task_number) to
        # avoid the N+1 hit on big result sets.
        keys = [(str(r[0]), int(r[1])) for r in rows]
        rels_by_key = self._load_relationships_bulk(keys)
        transitions_by_key = self._load_transitions_bulk(keys)
        tasks = []
        for row in rows:
            key = (str(row[0]), int(row[1]))
            task = self._row_to_task(
                row,
                relationships=rels_by_key.get(key, None),
            )
            task.transitions = transitions_by_key.get(key, [])
            tasks.append(task)
        # ``blocked`` post-filter mirrors the sqlite behaviour: the column
        # is derived from ``work_status``, so callers passing
        # ``blocked=True/False`` get the same surface either backend.
        if blocked is not None:
            tasks = [task for task in tasks if task.blocked == blocked]
        return tasks

    def list_task_summary_page(
        self,
        *,
        projects: tuple[str, ...] | list[str] | None = None,
        project: str | None = None,
        work_statuses: tuple[str, ...] | list[str] | None = None,
        assignee: str | None = None,
        since: datetime | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[TaskSummaryProjection], str | None, int]:
        """Return a cursor page of task-list summaries without full hydration."""
        where, params = self._task_summary_where(
            projects=projects,
            project=project,
            work_statuses=work_statuses,
            assignee=assignee,
            since=since,
        )
        total = self._count_task_summary_where(where, params)
        epoch = datetime.min.replace(tzinfo=UTC)
        page_where = list(where)
        page_params = list(params)

        if cursor is not None:
            cursor_updated_at, cursor_task_id = self._parse_task_summary_cursor(cursor)
            anchor_where = [
                *where,
                "COALESCE(wt.updated_at, %s) = %s",
                "(wt.project || '/' || wt.task_number::text) = %s",
            ]
            anchor_params = [*params, epoch, cursor_updated_at, cursor_task_id]
            if not self._task_summary_exists(anchor_where, anchor_params):
                raise TaskSummaryCursorError("stale task summary cursor")
            page_where.append(
                "(COALESCE(wt.updated_at, %s), "
                "(wt.project || '/' || wt.task_number::text)) < (%s, %s)"
            )
            page_params.extend([epoch, cursor_updated_at, cursor_task_id])

        clause = (" WHERE " + " AND ".join(page_where)) if page_where else ""
        capped_limit = max(1, int(limit))
        sql = (
            "SELECT wt.project, wt.task_number, wt.title, wt.work_status, "
            "wt.type, wt.priority, wt.assignee, wt.claimed_by_session, "
            "wt.current_node_id, wt.plan_version, wt.created_at, wt.updated_at, "
            "COALESCE(("
            "SELECT tr.created_at FROM work_transitions tr "
            "WHERE tr.task_project = wt.project "
            "AND tr.task_number = wt.task_number "
            "AND tr.to_state = wt.work_status "
            "ORDER BY tr.id DESC LIMIT 1"
            "), wt.created_at) AS state_entered_at "
            "FROM work_tasks wt"
            + clause
            + " ORDER BY COALESCE(wt.updated_at, %s) DESC, "
            "(wt.project || '/' || wt.task_number::text) DESC "
            "LIMIT %s"
        )
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, [*page_params, epoch, capped_limit + 1])
            rows = cur.fetchall()

        projections = [self._row_to_task_summary_projection(row) for row in rows]
        page = projections[:capped_limit]
        next_cursor: str | None = None
        if len(projections) > capped_limit and page:
            last = page[-1]
            next_cursor = (
                f"{(last.updated_at or epoch).isoformat()}|{last.task_id}"
            )
        return page, next_cursor, total

    def count_task_summary_matches(
        self,
        *,
        projects: tuple[str, ...] | list[str] | None = None,
        exclude_projects: tuple[str, ...] | list[str] | None = None,
        project: str | None = None,
        work_statuses: tuple[str, ...] | list[str] | None = None,
        assignee: str | None = None,
        since: datetime | None = None,
    ) -> int:
        """Count task-summary matches without hydrating task rows."""
        where, params = self._task_summary_where(
            projects=projects,
            exclude_projects=exclude_projects,
            project=project,
            work_statuses=work_statuses,
            assignee=assignee,
            since=since,
        )
        return self._count_task_summary_where(where, params)

    def list_inbox_candidate_tasks(
        self,
        *,
        project: str | None = None,
        type_filter: str | None = None,
        state_filter: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Task]:
        """Return task rows that can belong to the API/cockpit inbox.

        This is intentionally a candidate query: SQL applies cheap indexed
        state/type/identity predicates and the caller still runs the canonical
        :mod:`pollypm.work.inbox_view` predicate for flow-node semantics.
        Keeping the limit in this service method prevents ``GET /inbox`` from
        reading every task just to serve a small first page.
        """
        where: list[str] = []
        params: list[object] = []
        if project is not None:
            where.append("project = %s")
            params.append(project)

        state = (state_filter or "").strip().lower()
        terminal_values = sorted(s.value for s in TERMINAL_STATUSES)
        if state in {"closed", "resolved", "archived"}:
            where.append("work_status = ANY(%s)")
            params.append(terminal_values)
        elif state == "waiting-on-pm":
            where.append("work_status = %s")
            params.append(WorkStatus.REVIEW.value)
        elif state == "open":
            where.append("work_status <> ALL(%s)")
            params.append(terminal_values + [WorkStatus.REVIEW.value])
        elif state in {"threaded", "waiting-on-pa"}:
            where.append("FALSE")
        else:
            where.append("work_status <> ALL(%s)")
            params.append(terminal_values)

        wanted_type = (type_filter or "").strip()
        if wanted_type:
            if wanted_type in {"plan_review", InboxItemKind.PLAN_REVIEW_PENDING.value}:
                where.append("(kind = %s OR labels ? %s)")
                params.extend([InboxItemKind.PLAN_REVIEW_PENDING.value, "plan_review"])
            elif wanted_type == "blocking_question":
                where.append("labels ? %s")
                params.append("blocking_question")
            elif wanted_type == "alert":
                where.append("kind = %s")
                params.append(InboxItemKind.WATCHDOG_OPERATOR_DISPATCH.value)
            elif wanted_type == "message":
                where.append(
                    "(kind = %s AND NOT (labels ? %s) AND NOT (labels ? %s))"
                )
                params.extend(
                    [InboxItemKind.LEGACY.value, "plan_review", "blocking_question"]
                )
            else:
                where.append("kind = %s")
                params.append(wanted_type)

        where.append(
            "("
            "roles ? 'user' "
            "OR EXISTS ("
            "SELECT 1 FROM jsonb_each_text(work_tasks.roles) AS role(k, v) "
            "WHERE role.v = 'user'"
            ") "
            "OR labels ? 'plan_review' "
            "OR current_node_id IS NOT NULL"
            ")"
        )

        clause = " WHERE " + " AND ".join(where)
        order_limit = " ORDER BY updated_at DESC, project ASC, task_number DESC"
        if limit is not None:
            order_limit += " LIMIT %s"
            params.append(int(limit))
        if offset is not None:
            order_limit += " OFFSET %s"
            params.append(int(offset))
        sql = (
            "SELECT project, task_number, project_key, title, type, labels, "
            "work_status, flow_template_id, flow_template_version, "
            "current_node_id, assignee, claimed_by_session, "
            "priority, requires_human_review, "
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
        keys = [(str(r[0]), int(r[1])) for r in rows]
        rels_by_key = self._load_relationships_bulk(keys)
        return [
            self._row_to_task(
                row,
                relationships=rels_by_key.get(
                    (str(row[0]), int(row[1])), None
                ),
            )
            for row in rows
        ]

    def _task_summary_where(
        self,
        *,
        projects: tuple[str, ...] | list[str] | None = None,
        exclude_projects: tuple[str, ...] | list[str] | None = None,
        project: str | None = None,
        work_statuses: tuple[str, ...] | list[str] | None = None,
        assignee: str | None = None,
        since: datetime | None = None,
    ) -> tuple[list[str], list[object]]:
        where: list[str] = []
        params: list[object] = []
        if projects is not None:
            allowed = tuple(str(value) for value in projects)
            if not allowed:
                where.append("FALSE")
            else:
                where.append("wt.project = ANY(%s)")
                params.append(list(allowed))
        if exclude_projects:
            excluded = tuple(str(value) for value in exclude_projects)
            where.append("wt.project <> ALL(%s)")
            params.append(list(excluded))
        if project is not None:
            where.append("wt.project = %s")
            params.append(project)
        if work_statuses:
            statuses = tuple(str(value) for value in work_statuses)
            where.append("wt.work_status = ANY(%s)")
            params.append(list(statuses))
        if assignee is not None:
            where.append("wt.assignee = %s")
            params.append(assignee)
        if since is not None:
            where.append("(wt.updated_at IS NULL OR wt.updated_at > %s)")
            params.append(since)
        return where, params

    def _count_task_summary_where(
        self, where: list[str], params: list[object]
    ) -> int:
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM work_tasks wt" + clause, params)
            row = cur.fetchone()
        return int(row[0] if row else 0)

    def _task_summary_exists(
        self, where: list[str], params: list[object]
    ) -> bool:
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM work_tasks wt" + clause + " LIMIT 1", params)
            return cur.fetchone() is not None

    def _parse_task_summary_cursor(
        self, cursor: str
    ) -> tuple[datetime, str]:
        try:
            updated_raw, task_id = cursor.split("|", 1)
            updated_at = datetime.fromisoformat(updated_raw)
        except (TypeError, ValueError) as exc:
            raise TaskSummaryCursorError("invalid task summary cursor") from exc
        if not task_id:
            raise TaskSummaryCursorError("invalid task summary cursor")
        if updated_at.tzinfo is None or updated_at.tzinfo.utcoffset(updated_at) is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        else:
            updated_at = updated_at.astimezone(UTC)
        return updated_at, task_id

    def _row_to_task_summary_projection(
        self, row: tuple
    ) -> TaskSummaryProjection:
        (
            project,
            task_number,
            title,
            work_status,
            type_raw,
            priority,
            assignee,
            claimed_by_session,
            current_node_id,
            plan_version,
            created_at,
            updated_at,
            state_entered_at,
        ) = row
        task_number_int = int(task_number)
        project_str = str(project)
        return TaskSummaryProjection(
            task_id=f"{project_str}/{task_number_int}",
            project=project_str,
            task_number=task_number_int,
            title=str(title),
            work_status=str(work_status),
            type=str(type_raw),
            priority=str(priority),
            assignee=assignee,
            claimed_by_session=claimed_by_session,
            current_node_id=current_node_id,
            plan_version=int(plan_version or 1),
            created_at=created_at,
            state_entered_at=state_entered_at,
            updated_at=updated_at,
        )

    def _row_to_task(
        self,
        row: tuple,
        *,
        relationships: dict | None = None,
    ) -> Task:
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
            claimed_by_session,
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
        rels: dict = relationships or {}
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
            claimed_by_session=claimed_by_session,
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
            blocks=list(rels.get("blocks", [])),
            blocked_by=list(rels.get("blocked_by", [])),
            relates_to=list(rels.get("relates_to", [])),
            children=list(rels.get("children", [])),
            supersedes_project=supersedes_project,
            supersedes_task_number=(
                int(supersedes_task_number)
                if supersedes_task_number is not None
                else None
            ),
            superseded_by_project=rels.get("superseded_by_project"),
            superseded_by_task_number=rels.get("superseded_by_task_number"),
            plan_version=int(plan_version or 1),
            predecessor_task_id=predecessor_task_id,
            kind=_coerce_inbox_kind(kind_raw),
            roles=dict(_json_loads(roles_raw, {})),
            external_refs=dict(_json_loads(external_refs_raw, {})),
            created_at=created_at,
            created_by=str(created_by or ""),
            updated_at=updated_at,
        )

    def _load_transitions(
        self, project: str, task_number: int
    ) -> list[Transition]:
        """Load transition history for a task, oldest-first."""
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT from_state, to_state, actor, reason, created_at "
                "FROM work_transitions "
                "WHERE task_project = %s AND task_number = %s "
                "ORDER BY id ASC",
                (project, task_number),
            )
            rows = cur.fetchall()
        return [
            Transition(
                from_state=str(r[0]),
                to_state=str(r[1]),
                actor=str(r[2]),
                timestamp=r[4],
                reason=r[3],
            )
            for r in rows
        ]

    def _load_transitions_bulk(
        self, keys: list[tuple[str, int]]
    ) -> dict[tuple[str, int], list[Transition]]:
        """Load transition history for many tasks in one query."""
        if not keys:
            return {}
        pairs = list(dict.fromkeys(keys))
        projects = [project for project, _number in pairs]
        task_numbers = [number for _project, number in pairs]
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT task_project, task_number, from_state, to_state, "
                "actor, reason, created_at "
                "FROM work_transitions "
                "WHERE (task_project, task_number) IN ("
                "SELECT * FROM unnest(%s::text[], %s::int[])) "
                "ORDER BY task_project, task_number, id ASC",
                (projects, task_numbers),
            )
            rows = cur.fetchall()
        by_key: dict[tuple[str, int], list[Transition]] = {}
        for row in rows:
            key = (str(row[0]), int(row[1]))
            by_key.setdefault(key, []).append(
                Transition(
                    from_state=str(row[2]),
                    to_state=str(row[3]),
                    actor=str(row[4]),
                    reason=row[5],
                    timestamp=row[6],
                )
            )
        return by_key

    def _load_context_entries(
        self, project: str, task_number: int
    ) -> list[ContextEntry]:
        """Load context entries for a task, oldest-first."""
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT actor, created_at, text, entry_type "
                "FROM work_context_entries "
                "WHERE task_project = %s AND task_number = %s "
                "ORDER BY id ASC",
                (project, task_number),
            )
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

    def _load_relationships(
        self, project: str, task_number: int
    ) -> dict:
        """Load dependency relationships for a single task.

        Mirrors :meth:`SQLiteWorkService._load_relationships`. Returns a
        dict keyed by relationship kind plus the optional
        ``superseded_by_*`` pair.
        """
        sql = """
            SELECT from_project, from_task_number,
                   to_project, to_task_number, kind,
                   1 AS is_outgoing, 0 AS is_incoming
              FROM work_task_dependencies
             WHERE from_project = %s AND from_task_number = %s
            UNION ALL
            SELECT from_project, from_task_number,
                   to_project, to_task_number, kind,
                   0 AS is_outgoing, 1 AS is_incoming
              FROM work_task_dependencies
             WHERE to_project = %s AND to_task_number = %s
        """
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                sql,
                (project, task_number, project, task_number),
            )
            rows = cur.fetchall()
        return _aggregate_relationship_rows(rows)

    def _load_relationships_bulk(
        self, keys: list[tuple[str, int]]
    ) -> dict[tuple[str, int], dict]:
        """Bulk relationship hydration keyed by ``(project, task_number)``.

        Returns ``{}``  for unrelated tasks (caller defaults). One query
        regardless of result-set size — avoids the N+1 the sqlite path
        was rewritten to escape (#1770).
        """
        if not keys:
            return {}
        pairs = list(dict.fromkeys(keys))
        out: dict[tuple[str, int], dict] = {key: _empty_rels() for key in pairs}
        projects = [project for project, _number in pairs]
        task_numbers = [number for _project, number in pairs]
        sql = (
            "WITH keys(project, task_number) AS ("
            "SELECT * FROM unnest(%s::text[], %s::int[])) "
            "SELECT from_project, from_task_number, "
            "to_project, to_task_number, kind "
            "FROM work_task_dependencies d "
            "JOIN keys k ON "
            "(d.from_project = k.project AND d.from_task_number = k.task_number) "
            "OR (d.to_project = k.project AND d.to_task_number = k.task_number)"
        )
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (projects, task_numbers))
            all_rows = cur.fetchall()
        # Bucket by both endpoints (outgoing and incoming relative to
        # each key) and re-use the same aggregator the per-task path
        # uses, so the wire shape is identical.
        key_set = set(pairs)
        bucket: dict[tuple[str, int], list[tuple]] = {k: [] for k in pairs}
        for row in all_rows:
            from_p, from_n, to_p, to_n, kind = (
                str(row[0]),
                int(row[1]),
                str(row[2]),
                int(row[3]),
                row[4],
            )
            if (from_p, from_n) in key_set:
                bucket[(from_p, from_n)].append(
                    (from_p, from_n, to_p, to_n, kind, 1, 0)
                )
            if (to_p, to_n) in key_set:
                bucket[(to_p, to_n)].append(
                    (from_p, from_n, to_p, to_n, kind, 0, 1)
                )
        for key, rows in bucket.items():
            out[key] = _aggregate_relationship_rows(rows)
        return out

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
                # #1758: serialize per-project task-number allocation via
                # an advisory lock keyed by ``project``. Without this,
                # two concurrent ``create()`` calls for the same project
                # can both read the same MAX(task_number) and race the
                # ``(project, task_number)`` primary key.
                #
                # ``pg_advisory_xact_lock(bigint)`` releases automatically
                # at commit/rollback — no manual unlock required, and the
                # lock doesn't leak if the transaction crashes.
                # ``hashtextextended`` maps the project string to a
                # bigint key deterministically; the second arg is a salt
                # we leave at 0.
                cur.execute(
                    "SELECT pg_advisory_xact_lock("
                    "hashtextextended(%s, 0))",
                    (f"work_tasks.task_number:{project}",),
                )
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
        task = self.get(f"{project}/{task_number}")
        # #1787: emit ``task.created`` audit to mirror sqlite's
        # ``service_queries.create_task`` hook. Best-effort.
        try:
            from pollypm.audit import emit as _audit_emit
            from pollypm.audit.log import EVENT_TASK_CREATED

            _audit_emit(
                event=EVENT_TASK_CREATED,
                project=project,
                subject=task.task_id,
                actor=created_by or "system",
                metadata={
                    "title": title,
                    "type": type,
                    "flow_template": flow_template,
                    "priority": priority,
                    "requires_human_review": bool(requires_human_review),
                },
                project_path=self._project_path,
            )
            if predecessor_task_id is not None:
                from pollypm.audit.log import EVENT_PLAN_SUCCESSOR_CREATED

                _audit_emit(
                    event=EVENT_PLAN_SUCCESSOR_CREATED,
                    project=project,
                    subject=task.task_id,
                    actor=created_by or "system",
                    metadata={
                        "predecessor": predecessor_task_id,
                    },
                    project_path=self._project_path,
                )
        except Exception:  # noqa: BLE001 — audit must never break create
            logger.debug(
                "task.created audit emit failed for %s",
                task.task_id,
                exc_info=True,
            )
        # Mirror :func:`service_queries.create_task`: invoke registered
        # sync adapters and persist any external refs they stamped onto
        # the task object so the github_issue ref (and similar) survives
        # the create transaction round-trip (#1775).
        if self._sync is not None:
            external_refs_before_sync = dict(task.external_refs)
            try:
                self._sync.on_create(task)
            except Exception:  # noqa: BLE001 — sync failure must not break create
                logger.warning(
                    "sync.on_create failed for %s",
                    task.task_id,
                    exc_info=True,
                )
            changed_refs = {
                key: value
                for key, value in task.external_refs.items()
                if external_refs_before_sync.get(key) != value
            }
            for key, value in changed_refs.items():
                self.set_external_ref(task.task_id, key, value)
            if changed_refs:
                task = self.get(task.task_id)
        return task

    def set_external_ref(self, task_id: str, key: str, value: str) -> None:
        """Persist one external reference on a task.

        Mirrors :meth:`SQLiteWorkService.set_external_ref`. Used by the
        sync hooks to record adapter-stamped identifiers (e.g.
        ``github_issue``) without going through ``update()``.
        """
        if not key.strip():
            raise ValidationError(
                "Cannot persist an external ref with an empty key. "
                "The work service would have no stable name for the external "
                "system identifier, so later sync hooks could not retrieve it. "
                "Fix: pass a non-empty key such as 'github_issue'."
            )
        task = self.get(task_id)
        refs = dict(task.external_refs)
        refs[str(key)] = str(value)
        project, task_number = _parse_task_id(task_id)
        with self._pool.connection() as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE work_tasks SET external_refs = %s::jsonb, "
                    "updated_at = %s "
                    "WHERE project = %s AND task_number = %s",
                    (json.dumps(refs), _now_iso(), project, task_number),
                )
            conn.commit()

    def queue(
        self,
        task_id: str,
        actor: str,
        skip_gates: bool = False,
    ) -> Task:
        """Move a ``draft`` task to ``queued``.

        Mirrors the sqlite ``queue`` gate semantics (#1767): when the
        task is flagged ``requires_human_review`` we refuse to transition
        unless either (a) a ``human_review_approved`` context entry has
        been recorded via :meth:`approve_human_review` or (b) the caller
        passes ``skip_gates=True``, in which case we still record an
        audit entry that the bypass happened. When the gate trips we
        also materialise a user-owned inbox task so the operator has a
        place to land the approval/reject decision.
        """
        task = self.get(task_id)
        # Idempotent: re-queueing an already-queued task is a no-op so
        # ``pm task queue`` is safe to retry. Other non-draft states are
        # still rejected with three-question guidance.
        if task.work_status == WorkStatus.QUEUED:
            return task
        if task.work_status != WorkStatus.DRAFT:
            raise InvalidTransitionError(
                f"Cannot queue task in '{task.work_status.value}' state. "
                f"Task must be in 'draft' state."
            )
        if (
            task.requires_human_review
            and not skip_gates
            and not self.has_human_review_approval(task_id)
        ):
            approval_task = self.ensure_human_review_request_task(
                task_id, actor
            )
            raise InvalidTransitionError(
                "Task requires human review before queueing.\n"
                "\n"
                "Why: this task is marked requires_human_review, so it "
                "must be approved by the user or explicitly fast-tracked "
                "by an authorized operator before workers can pick it up.\n"
                "\n"
                f"Created user inbox task: {approval_task.task_id}\n"
                "\n"
                "Fix: approve it with "
                f"`pm task approve-human-review {task_id} --actor user`, "
                "or have the operator use "
                f"`pm task approve-human-review {task_id} --actor polly "
                '--fast-track-authorized --reason "..."`.'
            )
        if (
            task.requires_human_review
            and skip_gates
            and not self.has_human_review_approval(task_id)
        ):
            self.add_context(
                task_id,
                actor,
                "fast-track queue bypass recorded via --skip-gates",
                entry_type="human_review_approved",
            )
        transition_reason = None
        if not skip_gates:
            from pollypm.work.gates import (
                GateRegistry,
                evaluate_gates,
                has_hard_failure,
            )

            registry = GateRegistry(project_path=self._project_path)
            kwargs: dict[str, object] = {"get_task": self.get}
            if self._project_path is not None:
                kwargs["project_root"] = self._project_path
            results = evaluate_gates(
                task, ["has_description"], registry, **kwargs
            )
            if has_hard_failure(results):
                failing = [r for r in results if not r.passed]
                reason = failing[0].reason if failing else "unknown gate failure"
                raise ValidationError(
                    f"Cannot queue task: gate failed -- {reason}"
                )
        else:
            transition_reason = "fast-track queue bypass recorded via --skip-gates"
        return self._simple_transition(
            task_id,
            from_state=WorkStatus.DRAFT,
            to_state=WorkStatus.QUEUED,
            actor=actor,
            reason=transition_reason,
        )

    def has_human_review_approval(self, task_id: str) -> bool:
        """Return True when a pre-queue human review approval is recorded.

        Mirrors :meth:`SQLiteWorkService.has_human_review_approval`. A
        task that doesn't require human review is trivially "approved";
        otherwise we look for at least one ``human_review_approved``
        context entry.
        """
        task = self.get(task_id)
        if not task.requires_human_review:
            return True
        rows = self.get_context(
            task_id, entry_type="human_review_approved", limit=1
        )
        return bool(rows)

    def ensure_human_review_request_task(
        self,
        task_id: str,
        actor: str,
    ) -> Task:
        """Materialize a user-owned task requesting pre-queue approval.

        Mirrors :meth:`SQLiteWorkService.ensure_human_review_request_task`.
        Idempotent: if a non-terminal request task already exists for
        ``task_id`` we return it instead of creating a duplicate.
        """
        target = self.get(task_id)
        label = f"target_task:{target.task_id}"
        for candidate in self.list_tasks(project=target.project):
            labels = set(candidate.labels or [])
            if (
                "human_review_request" in labels
                and label in labels
                and candidate.work_status not in TERMINAL_STATUSES
            ):
                return candidate

        description = "\n".join(
            [
                f"Review whether `{target.task_id}` should enter the worker queue.",
                "",
                f"Task: {target.title}",
                target.description or "(no description)",
                "",
                "Approve if this work is authorized to proceed. Reject or reply "
                "with clarification if it needs changes before delegation.",
            ]
        )
        return self.create(
            title=f"Human review required before queueing {target.task_id}",
            description=description,
            type="task",
            project=target.project,
            flow_template="chat",
            roles={"requester": "user", "operator": actor or "polly"},
            priority=target.priority.value,
            created_by=actor or "system",
            labels=[
                "human_review_request",
                f"project:{target.project}",
                label,
            ],
            requires_human_review=False,
            kind=InboxItemKind.APPROVAL_REQUEST.value,
        )

    def approve_human_review(
        self,
        task_id: str,
        actor: str,
        reason: str | None = None,
        *,
        fast_track_authorized: bool = False,
    ) -> Task:
        """Record pre-queue approval for a ``requires_human_review`` task.

        Mirrors :meth:`SQLiteWorkService.approve_human_review`. Only the
        human user can approve unless an authorised operator passes
        ``fast_track_authorized=True``. On approval we close any open
        approval-request inbox task so the operator's queue clears.
        """
        task = self.get(task_id)
        actor_norm = (actor or "").strip().lower()
        is_user = actor_norm in {"user", "sam", "human"}
        if not is_user and not fast_track_authorized:
            raise InvalidTransitionError(
                "Only the user can approve this review unless the operator "
                "explicitly records --fast-track-authorized."
            )
        detail = reason.strip() if reason else "approved"
        if fast_track_authorized and not is_user:
            detail = f"fast-track authorized by {actor}: {detail}"
        self.add_context(
            task_id,
            actor or "user",
            detail,
            entry_type="human_review_approved",
        )

        label = f"target_task:{task.task_id}"
        for candidate in self.list_tasks(project=task.project):
            labels = set(candidate.labels or [])
            if (
                "human_review_request" in labels
                and label in labels
                and candidate.work_status not in TERMINAL_STATUSES
            ):
                try:
                    self.add_context(
                        candidate.task_id,
                        actor or "user",
                        f"approved target {task.task_id}",
                        entry_type="reply",
                    )
                    self.mark_done(candidate.task_id, actor or "user")
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "failed to close human review request %s",
                        candidate.task_id,
                        exc_info=True,
                    )
        return self.get(task_id)

    def cancel(self, task_id: str, actor: str, reason: str) -> Task:
        """Move any non-terminal task to ``cancelled``.

        Mirrors :meth:`SQLiteWorkService.cancel` — after the transition
        we cascade :meth:`_on_cancelled` so dependents get the breadcrumb
        the PM uses to decide unblock-vs-cancel.
        """
        task = self.get(task_id)
        if task.work_status in (WorkStatus.DONE, WorkStatus.CANCELLED):
            raise InvalidTransitionError(
                f"Cannot cancel task in terminal state {task.work_status.value!r}."
            )
        result = self._simple_transition(
            task_id,
            from_state=task.work_status,
            to_state=WorkStatus.CANCELLED,
            actor=actor,
            reason=reason,
        )
        try:
            self._on_cancelled(task_id)
        except Exception:  # noqa: BLE001
            logger.debug(
                "_on_cancelled cascade failed for %s", task_id, exc_info=True
            )
        # #1780: dispatch the assignment-alert cleanup events the sqlite
        # service's WorkTransitionManager raises so any
        # ``no_session_for_assignment:<task_id>`` /
        # ``worker-<project>/no_session`` alerts raised by the heartbeat
        # sweep get cleared. Without this the task_assignment_notify
        # plugin would leave those alerts open after the cancel.
        try:
            self._dispatch_cancel_assignment_alerts(task)
        except Exception:  # noqa: BLE001
            logger.debug(
                "assignment alert cleanup dispatch failed for %s",
                task_id,
                exc_info=True,
            )
        return result

    def reopen(
        self, task_id: str, actor: str, reason: str | None = None
    ) -> Task:
        """Move a cancelled task back to queued for a fresh claim."""
        task = self.get(task_id)
        if task.work_status != WorkStatus.CANCELLED:
            raise InvalidTransitionError(
                f"Cannot reopen task in '{task.work_status.value}' state. "
                "Only cancelled tasks can be reopened."
            )

        project, task_number = _parse_task_id(task_id)
        now = _now_iso()
        with self._pool.connection() as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE work_node_executions SET status = %s, "
                    "completed_at = %s "
                    "WHERE task_project = %s AND task_number = %s "
                    "AND status = %s",
                    (
                        ExecutionStatus.ABANDONED.value,
                        now,
                        project,
                        task_number,
                        ExecutionStatus.ACTIVE.value,
                    ),
                )
                # #2220: also clear claimed_by_session so the queued
                # row doesn't carry the prior worker's session id —
                # otherwise the next claim attempt sees a stale
                # "already claimed" breadcrumb on a freshly queued task.
                cur.execute(
                    "UPDATE work_tasks SET work_status = %s, "
                    "assignee = NULL, current_node_id = NULL, "
                    "claimed_by_session = NULL, "
                    "updated_at = %s "
                    "WHERE project = %s AND task_number = %s",
                    (WorkStatus.QUEUED.value, now, project, task_number),
                )
                cur.execute(
                    "INSERT INTO work_transitions ("
                    "task_project, task_number, from_state, to_state, "
                    "actor, reason, created_at"
                    ") VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        project,
                        task_number,
                        WorkStatus.CANCELLED.value,
                        WorkStatus.QUEUED.value,
                        actor,
                        reason,
                        now,
                    ),
                )
            conn.commit()

        self._emit_status_changed_audit(
            project=project,
            task_number=task_number,
            from_state=WorkStatus.CANCELLED.value,
            to_state=WorkStatus.QUEUED.value,
            actor=actor,
            reason=reason,
        )
        result = self.get(task_id)
        self._sync_transition(
            result, WorkStatus.CANCELLED.value, WorkStatus.QUEUED.value
        )
        return result

    def release(
        self, task_id: str, actor: str, reason: str | None = None
    ) -> Task:
        """Release an active worker claim back to queued.

        This is the operator/recovery counterpart to the post-commit
        claim rollback path: only active worker-owned states are legal,
        the flow node is preserved, active executions are abandoned, and
        the live assignee/session claim is cleared so a later claim starts
        a fresh visit at the same node.
        """
        project, task_number = _parse_task_id(task_id)
        now = _now_iso()
        release_reason = reason or "released via work service"
        with self._pool.connection() as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT work_status FROM work_tasks "
                    "WHERE project = %s AND task_number = %s "
                    "FOR UPDATE",
                    (project, task_number),
                )
                row = cur.fetchone()
                if row is None:
                    raise TaskNotFoundError(f"Task '{task_id}' not found.")
                current = _coerce_status(str(row[0]))
                if current not in (WorkStatus.IN_PROGRESS, WorkStatus.REWORK):
                    raise InvalidTransitionError(
                        f"Cannot release task in '{current.value}' state. "
                        "Task must be in 'in_progress' or 'rework' state."
                    )
                cur.execute(
                    "UPDATE work_tasks SET work_status = %s, "
                    "assignee = NULL, claimed_by_session = NULL, "
                    "updated_at = %s "
                    "WHERE project = %s AND task_number = %s",
                    (WorkStatus.QUEUED.value, now, project, task_number),
                )
                cur.execute(
                    "UPDATE work_node_executions SET status = %s, "
                    "completed_at = %s "
                    "WHERE task_project = %s AND task_number = %s "
                    "AND status = %s",
                    (
                        ExecutionStatus.ABANDONED.value,
                        now,
                        project,
                        task_number,
                        ExecutionStatus.ACTIVE.value,
                    ),
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
                        WorkStatus.QUEUED.value,
                        actor,
                        release_reason,
                        now,
                    ),
                )
            conn.commit()

        self._emit_status_changed_audit(
            project=project,
            task_number=task_number,
            from_state=current.value,
            to_state=WorkStatus.QUEUED.value,
            actor=actor,
            reason=release_reason,
        )
        self._finish_worker_session_after_release(
            task_id,
            project=project,
            task_number=task_number,
            ended_at=now,
        )
        result = self.get(task_id)
        self._sync_transition(result, current.value, WorkStatus.QUEUED.value)
        return result

    def release_stale_claim(
        self, task_id: str, actor: str, *, reason: str
    ) -> Task:
        return self.release(task_id, actor, reason)

    def _finish_worker_session_after_release(
        self,
        task_id: str,
        *,
        project: str,
        task_number: int,
        ended_at: datetime,
    ) -> None:
        session_mgr = self._session_mgr
        if session_mgr is not None:
            try:
                session_mgr.teardown_worker(task_id)
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "release[%s]: worker teardown failed: %s",
                    task_id,
                    exc,
                )
        try:
            self.mark_worker_session_ended(
                task_project=project,
                task_number=task_number,
                ended_at=ended_at,
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "release[%s]: worker-session end stamp failed",
                task_id,
                exc_info=True,
            )

    def mark_done(self, task_id: str, actor: str) -> Task:
        """Force a task to ``done`` without running the flow.

        Mirrors :meth:`SQLiteWorkService.mark_done`: writes the transition
        + audit row, then cascades :meth:`_check_auto_unblock` so any
        dependents this task was blocking get a chance to drop back to
        QUEUED.

        Refuses ``cancelled`` (and any other terminal state) with
        :class:`InvalidTransitionError` — mirrors the sqlite leaf in
        :func:`pollypm.work.service_transitions.mark_done`. Without
        this guard, the Web API's ``POST /tasks/.../done`` would
        happily resurrect a cancelled task as ``done``, which is the
        bug #2137 verification surfaced.
        """
        task = self.get(task_id)
        if task.work_status == WorkStatus.DONE:
            return task
        if task.work_status in TERMINAL_STATUSES:
            raise InvalidTransitionError(
                f"Cannot mark done task in terminal state "
                f"'{task.work_status.value}'."
            )
        result = self._simple_transition(
            task_id,
            from_state=task.work_status,
            to_state=WorkStatus.DONE,
            actor=actor,
        )
        try:
            self._check_auto_unblock(task_id)
        except Exception:  # noqa: BLE001
            logger.debug(
                "auto_unblock after mark_done failed for %s",
                task_id,
                exc_info=True,
            )
        return result

    def force_review(
        self, task_id: str, actor: str, reason: str | None = None
    ) -> Task:
        """Force ``in_progress`` → ``review`` without running ``node_done``.

        Web UI operator gesture for #2137: lets an operator mark work
        ready for review from the Web UI without supplying a flow
        ``work_output`` payload. The flow's ``node_done`` path remains
        the canonical worker-driven transition; this is the bypass
        equivalent of :meth:`mark_done` for ``review``.

        Only legal from ``in_progress`` / ``rework`` — sources where
        ``node_done`` would otherwise advance into a review node.
        """
        task = self.get(task_id)
        if task.work_status not in (
            WorkStatus.IN_PROGRESS,
            WorkStatus.REWORK,
        ):
            raise InvalidTransitionError(
                f"Cannot move task to 'review' from "
                f"'{task.work_status.value}' state. Task must be in "
                f"'in_progress' or 'rework' state."
            )
        return self._simple_transition(
            task_id,
            from_state=task.work_status,
            to_state=WorkStatus.REVIEW,
            actor=actor,
            reason=reason,
        )

    def force_in_progress(
        self, task_id: str, actor: str, reason: str | None = None
    ) -> Task:
        """Force a non-terminal task into ``in_progress``.

        Web UI operator gesture for #2137. Legal sources are
        ``queued``, ``on_hold``, ``review``, ``rework``, ``blocked``;
        terminal (``done`` / ``cancelled``) and ``draft`` are refused
        with :class:`InvalidTransitionError`. ``on_hold`` callers
        should normally use :meth:`resume` so the flow's current node
        is respected; this method is the explicit override.
        """
        task = self.get(task_id)
        if task.work_status not in (
            WorkStatus.QUEUED,
            WorkStatus.ON_HOLD,
            WorkStatus.REVIEW,
            WorkStatus.REWORK,
            WorkStatus.BLOCKED,
        ):
            raise InvalidTransitionError(
                f"Cannot move task to 'in_progress' from "
                f"'{task.work_status.value}' state. Task must be in a "
                f"non-terminal, non-draft state."
            )
        return self._simple_transition(
            task_id,
            from_state=task.work_status,
            to_state=WorkStatus.IN_PROGRESS,
            actor=actor,
            reason=reason,
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
        # #1787: audit emit after commit so a successful row in
        # ``work_transitions`` always has a paired JSONL entry.
        self._emit_status_changed_audit(
            project=project,
            task_number=task_number,
            from_state=current.value,
            to_state=to_state.value,
            actor=actor,
            reason=reason,
        )
        # #2305: release the worker-cap slot when the task leaves an
        # active worker state for one that does not hold a session
        # (e.g. ``in_progress``→``done`` via ``mark_done``,
        # ``in_progress``→``cancelled`` via ``cancel``,
        # ``in_progress``→``on_hold`` via ``hold``, etc.). The cap is
        # counted from ``work_sessions WHERE ended_at IS NULL``, so a
        # leak here 429s every subsequent claim on the project until
        # ``pm serve`` restarts.
        if (
            current in _WORKER_SESSION_HOLDING_STATUSES
            and to_state not in _WORKER_SESSION_HOLDING_STATUSES
        ):
            self._release_worker_session_for_transition(
                project=project,
                task_number=task_number,
                ended_at=now,
            )
        task = self.get(task_id)
        self._sync_transition(task, current.value, to_state.value)
        return task

    def _emit_status_changed_audit(
        self,
        *,
        project: str,
        task_number: int,
        from_state: str,
        to_state: str,
        actor: str,
        reason: str | None = None,
    ) -> None:
        """Emit ``task.status_changed`` audit row (#1787).

        Mirrors the sqlite ``_record_transition`` audit hook. Best-effort
        — audit failures must never block a transition.
        """
        try:
            from pollypm.audit import emit as _audit_emit
            from pollypm.audit.log import EVENT_TASK_STATUS_CHANGED

            _audit_emit(
                event=EVENT_TASK_STATUS_CHANGED,
                project=project,
                subject=f"{project}/{task_number}",
                actor=actor or "",
                metadata={
                    "from": from_state,
                    "to": to_state,
                    "reason": reason,
                },
                project_path=self._project_path,
            )
        except Exception:  # noqa: BLE001 — audit must never break transitions
            logger.debug(
                "task status audit emit failed for %s/%s",
                project,
                task_number,
                exc_info=True,
            )

    def _emit_claimed_by_session_audit(
        self,
        *,
        project: str,
        task_number: int,
        actor: str,
        assignee: str | None,
    ) -> None:
        """Emit the claim identity breadcrumb without blocking claim."""
        try:
            from pollypm.audit import emit as _audit_emit
            from pollypm.audit.log import EVENT_TASK_CLAIMED_BY_SESSION

            _audit_emit(
                event=EVENT_TASK_CLAIMED_BY_SESSION,
                project=project,
                subject=f"{project}/{task_number}",
                actor=actor or "",
                metadata={
                    "assignee": assignee,
                    "claimed_by_session": actor,
                },
                project_path=self._project_path,
            )
        except Exception:  # noqa: BLE001 — audit must never break claim
            logger.debug(
                "task claimed-by-session audit emit failed for %s/%s",
                project,
                task_number,
                exc_info=True,
            )

    def _insert_claim_breadcrumb_locked(
        self,
        cur,
        *,
        event_type: str,
        project: str,
        task_number: int,
        actor: str,
        session: str | None,
        timestamp: datetime,
        reason: str | None = None,
        assignee: str | None = None,
        winner_session: str | None = None,
    ) -> None:
        task_id = f"{project}/{task_number}"
        text = build_claim_breadcrumb_text(
            event_type=event_type,
            task_id=task_id,
            actor=actor,
            session=session,
            reason=reason,
            assignee=assignee,
            winner_session=winner_session,
        )
        cur.execute(
            "INSERT INTO work_context_entries "
            "(task_project, task_number, actor, text, created_at, "
            "entry_type) VALUES (%s, %s, %s, %s, %s, %s)",
            (
                project,
                task_number,
                actor,
                text,
                timestamp,
                event_type,
            ),
        )

    def _emit_claim_breadcrumb_audit(
        self,
        *,
        event_type: str,
        project: str,
        task_number: int,
        actor: str,
        session: str | None,
        timestamp: datetime,
        reason: str | None = None,
        assignee: str | None = None,
        winner_session: str | None = None,
    ) -> None:
        task_id = f"{project}/{task_number}"
        try:
            from pollypm.audit import emit as _audit_emit

            _audit_emit(
                event=event_type,
                project=project,
                subject=task_id,
                actor=actor or "",
                metadata=build_claim_breadcrumb_metadata(
                    task_id=task_id,
                    actor=actor,
                    session=session,
                    timestamp=timestamp,
                    reason=reason,
                    assignee=assignee,
                    winner_session=winner_session,
                ),
                project_path=self._project_path,
            )
        except Exception:  # noqa: BLE001 — audit must never break claim
            logger.debug(
                "claim breadcrumb audit emit failed for %s",
                task_id,
                exc_info=True,
            )

    def _record_claim_breadcrumb(
        self,
        *,
        event_type: str,
        project: str,
        task_number: int,
        actor: str,
        session: str | None,
        timestamp: datetime,
        reason: str | None = None,
        assignee: str | None = None,
        winner_session: str | None = None,
    ) -> None:
        """Record a claim forensic breadcrumb without changing claim outcome."""
        task_id = f"{project}/{task_number}"
        try:
            with self._pool.connection() as conn:
                conn.autocommit = False
                with conn.cursor() as cur:
                    self._insert_claim_breadcrumb_locked(
                        cur,
                        event_type=event_type,
                        project=project,
                        task_number=task_number,
                        actor=actor,
                        session=session,
                        timestamp=timestamp,
                        reason=reason,
                        assignee=assignee,
                        winner_session=winner_session,
                    )
                conn.commit()
        except Exception:  # noqa: BLE001 — forensics must not change claim
            logger.warning(
                "claim breadcrumb context insert failed for %s",
                task_id,
                exc_info=True,
            )

        self._emit_claim_breadcrumb_audit(
            event_type=event_type,
            project=project,
            task_number=task_number,
            actor=actor,
            session=session,
            timestamp=timestamp,
            reason=reason,
            assignee=assignee,
            winner_session=winner_session,
        )

    def _sync_transition(
        self, task: Task, old_status: str, new_status: str
    ) -> None:
        """Fire registered sync adapters on a state transition.

        Mirrors :meth:`SQLiteWorkService._sync_transition`. Best-effort:
        adapter failures are logged and swallowed so a broken sync
        side-channel never blocks the primary transition.
        """
        if self._sync is None:
            return
        try:
            self._sync.on_transition(task, old_status, new_status)
        except Exception:  # noqa: BLE001
            logger.warning(
                "sync.on_transition failed for %s",
                task.task_id,
                exc_info=True,
            )

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
            "current_node_id, assignee, claimed_by_session, "
            "priority, requires_human_review, "
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
        keys = [(str(r[0]), int(r[1])) for r in rows]
        rels_by_key = self._load_relationships_bulk(keys)
        return [
            self._row_to_task(
                row,
                relationships=rels_by_key.get(
                    (str(row[0]), int(row[1])), None
                ),
            )
            for row in rows
        ]

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
        # ``assignee`` is admitted at the column-schema level for
        # backend symmetry with the legacy SQLite store, but no
        # operator surface targets this path post-#2064 round-9. The
        # PATCH body (``TaskPatchRequest``) refuses ``assignee`` via
        # ``extra='forbid'``, ``POST /reassign`` routes through
        # :meth:`reassign_task`, and ``pm task update`` does not
        # expose ``--assignee``. The single operator path for
        # changing assignee is :meth:`reassign_task`, which writes
        # the column AND appends a breadcrumb in one transaction
        # (spec §P-9). The column is plain text — no JSON encoding —
        # so it doesn't join ``_UPDATE_JSON_COLUMNS``.
        "assignee": "assignee",
        # ``external_refs`` carries the API's free-form ``metadata``
        # surface (spec §5.4 PATCH ``metadata?: {...}``). Stored as
        # ``jsonb`` so it joins the JSON-encoded column set below.
        "external_refs": "external_refs",
    }
    _UPDATE_JSON_COLUMNS = frozenset(
        {"labels", "relevant_files", "roles", "external_refs"}
    )

    def update(self, task_id: str, **fields: object) -> Task:
        """Update mutable fields on a task.

        Slice B port of ``update_task`` (service_queries.py). Refuses
        ``work_status`` and ``flow_template`` changes — those go through
        the lifecycle methods.

        Note on ``assignee``: this method still accepts the column for
        backend symmetry, but post-#2064 round-9 no operator surface
        exposes a breadcrumb-less assignee write. ``PATCH /tasks/
        {p}/{n}`` rejects the ``assignee`` field at request-validation
        (``TaskPatchRequest`` is ``extra='forbid'``), ``POST /tasks/
        {p}/{n}/reassign`` routes through :meth:`reassign_task`, and
        ``pm task update`` does not advertise ``--assignee``. Callers
        that need to change ``assignee`` must call
        :meth:`reassign_task` — it writes the column AND appends the
        ``reassignment`` context-log breadcrumb in a single
        transaction so the new owner can recover context via
        ``pm task get`` (spec §P-9).
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
        task = self.get(task_id)
        if self._sync is not None:
            try:
                self._sync.on_update(task, list(fields.keys()))
            except Exception:  # noqa: BLE001
                logger.warning(
                    "sync.on_update failed for %s",
                    task.task_id,
                    exc_info=True,
                )
        return task

    def reassign_task(
        self,
        task_id: str,
        *,
        new_assignee: str,
        actor: str,
        reason: str | None = None,
    ) -> Task:
        """Reassign a mid-flight task; atomic assignee+context-log write (#2064 round-3).

        Implements work-service spec §P-9: a worker swap on an
        ``in_progress`` task MUST leave a breadcrumb in the context log
        so the new owner can recover context via ``pm task get``.
        ``svc.update(assignee=...)`` (the column-only path used by
        non-handoff PATCHes) does not record this entry; routes that
        represent a real handoff (``POST /tasks/{p}/{n}/reassign``)
        call this method instead.

        Atomicity: the ``UPDATE work_tasks SET assignee = ...`` and the
        ``INSERT INTO work_context_entries`` commit in the same
        transaction. Failure of either rolls both back, so observers
        never see a new assignee without the matching breadcrumb (or
        vice versa).
        """
        project, task_number = _parse_task_id(task_id)
        now = _now_iso()
        with self._pool.connection() as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                # ``FOR UPDATE`` takes a row-level lock on the task row
                # for the duration of this transaction (#2064 round-4
                # concurrency blocker). Two concurrent reassigns
                # without the lock would both read the same ``old``
                # value under READ COMMITTED and emit two breadcrumbs
                # naming the same predecessor — losing the second
                # writer's view of the handoff (``pete -> nora`` +
                # ``pete -> olga`` instead of ``pete -> nora`` +
                # ``nora -> olga``). With the lock, the second SELECT
                # waits on the first transaction's UPDATE; when it
                # unblocks it reads the freshly-committed assignee,
                # so the breadcrumb chain stays coherent.
                #
                # #2064 round-9 blocker #4: also fetch ``work_status``
                # so we can enforce the live-worker-swap invariant
                # (spec §P-9 / web-api-spec §5.3 "mid-flight"). Without
                # this check, reassign happily appends a breadcrumb to
                # ``draft`` / ``done`` / ``cancelled`` tasks even
                # though there is no worker to hand off to. Checking
                # under the same row lock means a task that flipped to
                # terminal between the request and the lock
                # acquisition raises ``InvalidTransitionError`` (→ 409)
                # instead of recording a stray breadcrumb.
                cur.execute(
                    "SELECT assignee, work_status FROM work_tasks "
                    "WHERE project = %s AND task_number = %s "
                    "FOR UPDATE",
                    (project, task_number),
                )
                row = cur.fetchone()
                if row is None:
                    raise TaskNotFoundError(f"Task '{task_id}' not found.")
                old_assignee, locked_status = row[0], row[1]
                if locked_status not in _REASSIGN_ALLOWED_STATUSES:
                    if locked_status == WorkStatus.QUEUED.value:
                        # #2064 round-11 blocker #3: queued
                        # reassign breaks routing. Queued
                        # dispatch routes by ``task.roles["worker"]``,
                        # not ``assignee`` — setting ``assignee``
                        # on a queued task leaves the next
                        # ``claim()`` routing to the original role
                        # owner. Operators who want to redirect a
                        # queued task should cancel + re-queue
                        # with the new role assignment.
                        raise InvalidTransitionError(
                            f"Cannot reassign task in 'queued' "
                            f"state.\n"
                            f"\n"
                            f"Why: queued dispatch routes by "
                            f"`task.roles['worker']`, not the "
                            f"`assignee` column. Setting `assignee` "
                            f"on a queued task does NOT change "
                            f"which worker claims it next.\n"
                            f"\n"
                            f"Fix: queued tasks can't be reassigned; "
                            f"cancel + re-queue with role assignment "
                            f"(`pm task cancel {task_id} --reason "
                            f"'reassigning'` then re-create with the "
                            f"new `roles.worker`)."
                        )
                    raise InvalidTransitionError(
                        f"Cannot reassign task in '{locked_status}' "
                        f"state.\n"
                        f"\n"
                        f"Why: reassign is a mid-flight worker swap "
                        f"(work-service spec §P-9). It refuses "
                        f"`draft` (no worker yet — queue + claim "
                        f"first) and terminal `done` / `cancelled` "
                        f"tasks (no worker lane left).\n"
                        f"\n"
                        f"Fix: pick a different task with "
                        f"`pm task next`, or queue + claim this one "
                        f"if it is still draft."
                    )
                # Column write — same SQL shape as update(assignee=...)
                # but inline so the context-log INSERT lands in the
                # same transaction.
                cur.execute(
                    "UPDATE work_tasks "
                    "SET assignee = %s, updated_at = %s "
                    "WHERE project = %s AND task_number = %s",
                    (new_assignee, now, project, task_number),
                )
                # Breadcrumb body matches the spec's example wording so
                # operators / agents can grep for "reassigned from".
                old_label = old_assignee if old_assignee else "<unassigned>"
                body = (
                    f"worker reassigned from {old_label} to {new_assignee}"
                )
                if reason:
                    body += f" (reason: {reason})"
                cur.execute(
                    "INSERT INTO work_context_entries "
                    "(task_project, task_number, actor, text, created_at, "
                    "entry_type) VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        project,
                        task_number,
                        actor,
                        body,
                        now,
                        "reassignment",
                    ),
                )
            conn.commit()
        task = self.get(task_id)
        if self._sync is not None:
            try:
                self._sync.on_update(task, ["assignee"])
            except Exception:  # noqa: BLE001
                logger.warning(
                    "sync.on_update failed for %s",
                    task.task_id,
                    exc_info=True,
                )
        return task

    def increment_plan_version(
        self,
        task_id: str,
        *,
        actor: str = "system",
        reason: str | None = None,
    ) -> Task:
        """Bump ``plan_version`` on a plan task and emit an audit event (#1398).

        Mirrors :meth:`SQLiteWorkService.increment_plan_version`: writes
        ``plan_version + 1`` then emits ``plan.version_incremented`` via
        :func:`pollypm.audit.emit` (#1773). Audit emission is best-effort
        — a failed audit write never blocks the version bump.
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

        try:
            from pollypm.audit import emit as _audit_emit
            from pollypm.audit.log import EVENT_PLAN_VERSION_INCREMENTED

            metadata: dict[str, object] = {
                "task_id": task_id,
                "old_version": old_version,
                "new_version": new_version,
            }
            if reason:
                metadata["reason"] = reason
            _audit_emit(
                event=EVENT_PLAN_VERSION_INCREMENTED,
                project=project,
                subject=task_id,
                actor=actor or "system",
                metadata=metadata,
                project_path=self._project_path,
            )
        except Exception:  # noqa: BLE001 — audit must never break the bump
            logger.debug(
                "plan version audit emit failed for %s",
                task_id,
                exc_info=True,
            )

        return self.get(task_id)

    def list_successors(self, predecessor_task_id: str) -> list[Task]:
        """Tasks whose ``predecessor_task_id`` matches (#1398)."""
        sql = (
            "SELECT project, task_number, project_key, title, type, labels, "
            "work_status, flow_template_id, flow_template_version, "
            "current_node_id, assignee, claimed_by_session, "
            "priority, requires_human_review, "
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
        keys = [(str(r[0]), int(r[1])) for r in rows]
        rels_by_key = self._load_relationships_bulk(keys)
        return [
            self._row_to_task(
                row,
                relationships=rels_by_key.get(
                    (str(row[0]), int(row[1])), None
                ),
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # State transitions (Slice B port of the transition manager)
    #
    # These intentionally implement a simplified version of the sqlite
    # transition manager: state checks + node advancement + audit row,
    # without the post-commit side effects (session provisioning, auto-
    # repair, sync adapters, plan-review emission). Those land in
    # Slice C alongside the audit / sync adapter ports.
    # ------------------------------------------------------------------

    def claim(self, task_id: str, actor: str, skip_gates: bool = False) -> Task:
        """Atomically claim a queued task.

        Loads the flow template, resolves the start (or current) node,
        moves the task to ``in_progress`` (or ``review`` for a review
        start node), and writes the audit transition row.

        Round-13 (#2064): EVERY claim-dependent read happens INSIDE the
        write transaction against the FOR-UPDATE-locked row. The prior
        implementation pre-read the task outside the lock, validated
        ``work_status``/``blocked``, resolved the node assignee from
        ``task.roles``, then opened the transaction. Between the
        pre-read and the row lock a concurrent writer could:
        * ``pm task update --role`` mutate ``roles`` so the locked-in
          assignee was stale, OR
        * ``pm task link <blocker> <this> blocks`` add a blocker so the
          task should have refused the claim.

        Either window let claim commit on a stale view. With the
        full-row revalidation below, the loser of any concurrent edit
        race re-reads the freshly-committed row under FOR UPDATE and
        bails with ``InvalidTransitionError`` instead of silently
        adopting the old view.
        """
        project, task_number = _parse_task_id(task_id)
        now = _now_iso()
        # Outside-the-lock state we'll read back AFTER the commit so the
        # post-tx side effects (sync, session provisioning, rollback)
        # have the values they need without re-reading the row.
        task_for_postcommit: Task | None = None
        target_status: WorkStatus | None = None
        node_id_committed: str | None = None
        with self._pool.connection() as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                # SELECT the full row + FOR UPDATE in one query. Every
                # subsequent decision (status, roles, current_node_id,
                # assignee resolution) reads from this snapshot, so a
                # concurrent ``update(roles=...)`` or ``link(blocks)``
                # observed AFTER this lock acquires either:
                # (a) committed before our lock — visible here, OR
                # (b) waiting on our lock — invisible until we commit.
                # Either case is consistent: we never decide on a view
                # that was already stale at the time of decision.
                locked_task = self._fetch_task_for_claim_locked(
                    cur, project, task_number, task_id
                )

                if locked_task.work_status != WorkStatus.QUEUED:
                    if locked_task.work_status in _CLAIM_CONTESTED_STATUSES:
                        claimant = (
                            locked_task.claimed_by_session
                            or locked_task.assignee
                            or "another actor"
                        )
                        timestamp = _now_iso()
                        error = InvalidTransitionError(
                            f"Task {task_id} is already claimed by "
                            f"'{claimant}'.\n"
                            f"\n"
                            f"Why: the task is in "
                            f"'{locked_task.work_status.value}' and "
                            f"assigned. A second claim would orphan "
                            f"the first claimant's session.\n"
                            f"\n"
                            f"Fix: use `pm task get {task_id}` to see "
                            f"the current state. If the existing claim "
                            f"is stale (claimant session dead), hold and "
                            f"resume:\n"
                            f"    pm task hold {task_id} --reason 'stale claim'\n"
                            f"    pm task resume {task_id}\n"
                            f"Otherwise, find an unclaimed task with "
                            f"`pm task next`."
                        )
                        try:
                            self._insert_claim_breadcrumb_locked(
                                cur,
                                event_type=CLAIM_ATTEMPTED_BY_LOSER,
                                project=project,
                                task_number=task_number,
                                actor=actor,
                                session=actor,
                                timestamp=timestamp,
                                reason=CLAIM_ALREADY_CLAIMED_REASON,
                                assignee=locked_task.assignee,
                                winner_session=locked_task.claimed_by_session,
                            )
                            conn.commit()
                        except Exception:  # noqa: BLE001
                            try:
                                conn.rollback()
                            except Exception:  # noqa: BLE001
                                logger.debug(
                                    "claim loser rollback failed for %s",
                                    task_id,
                                    exc_info=True,
                                )
                            logger.warning(
                                "claim loser breadcrumb insert failed for %s",
                                task_id,
                                exc_info=True,
                            )
                        else:
                            self._emit_claim_breadcrumb_audit(
                                event_type=CLAIM_ATTEMPTED_BY_LOSER,
                                project=project,
                                task_number=task_number,
                                actor=actor,
                                session=actor,
                                timestamp=timestamp,
                                reason=CLAIM_ALREADY_CLAIMED_REASON,
                                assignee=locked_task.assignee,
                                winner_session=locked_task.claimed_by_session,
                            )
                        raise error
                    raise InvalidTransitionError(
                        f"Cannot claim task in "
                        f"'{locked_task.work_status.value}' state.\n"
                        f"\n"
                        f"Why: only tasks in 'queued' state can be claimed.\n"
                        f"\n"
                        f"Fix: if the task is 'draft', run "
                        f"`pm task queue {task_id}` first. If it's "
                        f"'done' or 'cancelled', find another task "
                        f"with `pm task next`."
                    )
                # Blocker check uses the dependencies table; a fresh
                # ``link(blocks)`` committed after our lock acquires is
                # visible to this READ COMMITTED select. Without the
                # in-tx re-read, a blocker inserted between the pre-tx
                # read and the lock would be ignored and the claim
                # would commit.
                if self._has_unresolved_blockers_locked(
                    cur, project, task_number
                ):
                    raise InvalidTransitionError(
                        f"Cannot claim task {task_id}: it is blocked by "
                        f"another task.\n"
                        f"\n"
                        f"Why: blocking tasks must reach a terminal "
                        f"state before dependents can start.\n"
                        f"\n"
                        f"Fix: run `pm task get {task_id}` to see the "
                        f"blockers, then work on those first (or "
                        f"unblock with `pm task unlink`)."
                    )

                # Flow + node resolution from the LOCKED row. The flow
                # template id is immutable post-create (update() rejects
                # ``flow_template`` changes) so the template load is
                # safe outside the lock, but the resolved
                # ``current_node_id`` and the role binding used by
                # ``_resolve_node_assignee`` come from ``locked_task``.
                flow = self._load_flow(locked_task)
                node_id = locked_task.current_node_id or flow.start_node
                if not node_id:
                    raise InvalidTransitionError(
                        f"Task {task_id} has no claimable flow node."
                    )
                node = flow.nodes.get(node_id)
                if node is None:
                    raise InvalidTransitionError(
                        f"Current node '{node_id}' not found in flow "
                        f"'{flow.name}'."
                    )
                if node.type == NodeType.TERMINAL:
                    raise InvalidTransitionError(
                        f"Current node '{node_id}' is terminal and "
                        f"cannot be claimed."
                    )

                # ``_resolve_node_assignee`` reads ``task.roles`` for
                # ROLE-typed nodes; passing ``locked_task`` ensures we
                # use the role binding visible under the row lock, not
                # a stale pre-tx snapshot.
                assignee = (
                    self._resolve_node_assignee(locked_task, node) or actor
                )
                resolved_target_status = (
                    WorkStatus.REVIEW
                    if node.type == NodeType.REVIEW
                    else WorkStatus.IN_PROGRESS
                )
                # #1737 — pre-claim cap check. Runs INSIDE the
                # transaction but BEFORE the UPDATE so cap-exceeded
                # callers don't commit a transition they'll then have
                # to roll back. The cap check queries worker session
                # rows (not work_tasks), so it doesn't deadlock against
                # our row lock. Mirrors the sqlite transition manager.
                if (
                    resolved_target_status is WorkStatus.IN_PROGRESS
                    and not skip_gates
                    and self._session_mgr is not None
                ):
                    check_cap = getattr(
                        self._session_mgr, "check_parallel_cap", None,
                    )
                    if callable(check_cap):
                        check_cap(project, task_id)

                cur.execute(
                    "UPDATE work_tasks SET work_status = %s, assignee = %s, "
                    "claimed_by_session = %s, current_node_id = %s, "
                    "updated_at = %s "
                    "WHERE project = %s AND task_number = %s",
                    (
                        resolved_target_status.value,
                        assignee,
                        actor,
                        node_id,
                        now,
                        project,
                        task_number,
                    ),
                )
                # If we're resuming a blocked execution, flip its status;
                # otherwise insert a fresh visit row.
                cur.execute(
                    "SELECT id, status FROM work_node_executions "
                    "WHERE task_project = %s AND task_number = %s "
                    "AND node_id = %s "
                    "ORDER BY visit DESC, id DESC LIMIT 1",
                    (project, task_number, node_id),
                )
                latest = cur.fetchone()
                if (
                    locked_task.current_node_id is not None
                    and latest is not None
                    and latest[1] == ExecutionStatus.BLOCKED.value
                ):
                    cur.execute(
                        "UPDATE work_node_executions SET status = %s "
                        "WHERE id = %s",
                        (ExecutionStatus.ACTIVE.value, latest[0]),
                    )
                elif not (
                    locked_task.current_node_id is not None
                    and latest is not None
                    and latest[1] == ExecutionStatus.ACTIVE.value
                ):
                    visit = self._next_visit_locked(
                        cur, project, task_number, node_id
                    )
                    cur.execute(
                        "INSERT INTO work_node_executions "
                        "(task_project, task_number, node_id, visit, "
                        "status, started_at) VALUES "
                        "(%s, %s, %s, %s, %s, %s)",
                        (
                            project,
                            task_number,
                            node_id,
                            visit,
                            ExecutionStatus.ACTIVE.value,
                            now,
                        ),
                    )
                self._insert_transition_locked(
                    cur,
                    project,
                    task_number,
                    WorkStatus.QUEUED.value,
                    resolved_target_status.value,
                    actor,
                    None,
                )
            conn.commit()
            task_for_postcommit = locked_task
            target_status = resolved_target_status
            node_id_committed = node_id
        # Post-commit side effects mirror the pre-refactor surface; the
        # locals above are guaranteed set because conn.commit() raises
        # before we get here on failure.
        assert task_for_postcommit is not None
        assert target_status is not None
        assert node_id_committed is not None
        result = self.get(task_id)
        self._sync_transition(
            result, WorkStatus.QUEUED.value, target_status.value
        )
        # Provision a worker session if a session manager is wired. Best
        # effort — failures land in ``last_provision_error`` for the CLI
        # to render rather than bubbling up. Mirrors the sqlite path.
        self.last_provision_error = None
        if self._session_mgr is not None:
            try:
                self._session_mgr.provision_worker(task_id, assignee)
            except Exception as exc:  # noqa: BLE001
                self.last_provision_error = str(exc)
                logger.warning(
                    "provision_worker failed for %s (actor=%s): %s",
                    task_id,
                    actor,
                    exc,
                )
                # #1906 — atomic cap reserve can still lose the race
                # AFTER the claim transition has already committed. The
                # session manager already released its placeholder cap
                # slot via _release_cap_slot_on_failure; revert the
                # task back to queued so auto-claim re-picks it instead
                # of stranding it ``in_progress`` with no worker. See
                # the sqlite counterpart in service_transition_manager.
                if _is_cap_exceeded_error(exc) and target_status is (
                    WorkStatus.IN_PROGRESS
                ):
                    rollback_committed = self._rollback_claim_to_queued(
                        task_for_postcommit.project,
                        task_for_postcommit.task_number,
                        node_id_committed,
                        actor,
                        exc,
                    )
                    if rollback_committed:
                        # #1953 — refetch so the caller sees the
                        # rolled-back row (queued, abandoned execution)
                        # instead of the stale in_progress snapshot
                        # captured pre-provisioning.
                        result = self.get(task_id)
        if (
            getattr(result, "claimed_by_session", None) == actor
            and result.work_status is target_status
        ):
            self._emit_claimed_by_session_audit(
                project=project,
                task_number=task_number,
                actor=actor,
                assignee=assignee,
            )
            self._record_claim_breadcrumb(
                event_type=CLAIM_WON_BY,
                project=project,
                task_number=task_number,
                actor=actor,
                session=actor,
                timestamp=now,
                assignee=assignee,
            )
        return result

    def _fetch_task_for_claim_locked(
        self, cur, project: str, task_number: int, task_id: str
    ) -> Task:
        """SELECT the full task row + FOR UPDATE and hydrate as a Task.

        Used by :meth:`claim` so every claim-dependent decision
        (status, roles, current_node_id) reads from the locked row
        rather than a pre-transaction snapshot (#2064 round-13).

        Relationships are loaded on the same cursor to inherit the
        tx's read snapshot — a fresh ``link(blocks)`` committed before
        our row lock is visible; one waiting on our lock is not.
        """
        cur.execute(
            "SELECT project, task_number, project_key, title, type, labels, "
            "work_status, flow_template_id, flow_template_version, "
            "current_node_id, assignee, claimed_by_session, "
            "priority, requires_human_review, "
            "description, acceptance_criteria, constraints, relevant_files, "
            "parent_project, parent_task_number, "
            "supersedes_project, supersedes_task_number, "
            "plan_version, predecessor_task_id, kind, "
            "roles, external_refs, "
            "created_at, created_by, updated_at "
            "FROM work_tasks "
            "WHERE project = %s AND task_number = %s "
            "FOR UPDATE",
            (project, task_number),
        )
        row = cur.fetchone()
        if row is None:
            # Row was deleted between any caller-side pre-read and the
            # lock attempt. Vanishingly rare (tasks are not
            # hard-deleted in normal flows) but still safer than
            # blindly UPDATE-ing a ghost row.
            raise TaskNotFoundError(f"Task '{task_id}' not found.")
        # Relationship rows live in work_task_dependencies; reading
        # them on the same cursor pulls the latest committed snapshot
        # which is what ``_has_unresolved_blockers_locked`` will also
        # use below for the blocker re-check.
        cur.execute(
            "SELECT from_project, from_task_number, "
            "       to_project, to_task_number, kind, "
            "       1 AS is_outgoing, 0 AS is_incoming "
            "  FROM work_task_dependencies "
            " WHERE from_project = %s AND from_task_number = %s "
            "UNION ALL "
            "SELECT from_project, from_task_number, "
            "       to_project, to_task_number, kind, "
            "       0 AS is_outgoing, 1 AS is_incoming "
            "  FROM work_task_dependencies "
            " WHERE to_project = %s AND to_task_number = %s",
            (project, task_number, project, task_number),
        )
        rels = _aggregate_relationship_rows(cur.fetchall())
        return self._row_to_task(row, relationships=rels)

    def _has_unresolved_blockers_locked(
        self, cur, project: str, task_number: int
    ) -> bool:
        """In-tx variant of :meth:`_has_unresolved_blockers`.

        Same query body — runs on the caller's cursor so the read
        sees the same READ COMMITTED snapshot the rest of the claim
        transaction does. Used by :meth:`claim` so a blocker dependency
        committed after a hypothetical pre-tx read still gates the
        claim (#2064 round-13).
        """
        cur.execute(
            "SELECT t.work_status FROM work_task_dependencies d "
            "JOIN work_tasks t "
            "  ON t.project = d.from_project "
            " AND t.task_number = d.from_task_number "
            "WHERE d.to_project = %s AND d.to_task_number = %s "
            "AND d.kind = %s",
            (project, task_number, LinkKind.BLOCKS.value),
        )
        for row in cur.fetchall():
            if row[0] not in (
                WorkStatus.DONE.value,
                WorkStatus.CANCELLED.value,
            ):
                return True
        return False

    def _rollback_claim_to_queued(
        self,
        project: str,
        task_number: int,
        node_id: str,
        actor: str,
        exc: BaseException,
    ) -> bool:
        """Revert an in_progress claim back to queued (#1906).

        Postgres counterpart of
        ``WorkTransitionManager._rollback_claim_to_queued``. Best-effort:
        a failed rollback is logged so the operator still sees
        ``last_provision_error`` and can run ``pm task release``
        manually rather than silently wedging the task.

        Returns ``True`` when the rollback committed so ``claim()`` can
        refetch the queued row instead of returning the stale
        in_progress snapshot it captured before provisioning fired
        (#1953). ``False`` on rollback failure.
        """
        now = _now_iso()
        try:
            with self._pool.connection() as conn:
                conn.autocommit = False
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE work_tasks SET work_status = %s, "
                        "claimed_by_session = NULL, updated_at = %s "
                        "WHERE project = %s AND task_number = %s",
                        (
                            WorkStatus.QUEUED.value,
                            now,
                            project,
                            task_number,
                        ),
                    )
                    cur.execute(
                        "UPDATE work_node_executions SET status = %s, "
                        "completed_at = %s "
                        "WHERE task_project = %s AND task_number = %s "
                        "AND node_id = %s AND status = %s",
                        (
                            ExecutionStatus.ABANDONED.value,
                            now,
                            project,
                            task_number,
                            node_id,
                            ExecutionStatus.ACTIVE.value,
                        ),
                    )
                    self._insert_transition_locked(
                        cur,
                        project,
                        task_number,
                        WorkStatus.IN_PROGRESS.value,
                        WorkStatus.QUEUED.value,
                        actor,
                        None,
                    )
                conn.commit()
            # #2305: ensure the worker-cap slot is released. The session
            # manager's failure path already calls
            # ``_release_cap_slot_on_failure`` for the cap-exceeded race
            # that triggered this rollback, but for any other provision
            # failure that lands here we belt-and-suspender stamp
            # ``ended_at`` so a leaked placeholder can't 429 the next
            # claim.
            self._release_worker_session_for_transition(
                project=project,
                task_number=task_number,
                ended_at=now,
            )
            logger.warning(
                "claim rollback: %s/%d returned to queued after "
                "post-commit provision failure (%s)",
                project, task_number, exc,
            )
            return True
        except Exception as rollback_exc:  # noqa: BLE001
            logger.warning(
                "claim rollback failed for %s/%d: %s (task remains "
                "in_progress; operator must release manually)",
                project, task_number, rollback_exc,
            )
            return False

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
            "t.claimed_by_session, t.priority, t.requires_human_review, "
            "t.description, "
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
            rels = self._load_relationships(task_key[0], task_key[1])
            task = self._row_to_task(row, relationships=rels)
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

        # Actor-vs-role validation — sqlite's node_done runs this before
        # the work-output coercion so a stranger trying to ``done`` a
        # task they don't own gets the actor-role error, not a missing
        # --output error. Mirror that here (#1771).
        self._validate_actor_role(task, node, actor)

        coerced = self._coerce_work_output(work_output)
        if coerced is None:
            raise ValidationError(
                "pm task done requires a --output payload describing "
                "what you built.\n"
                "\n"
                "Why: the reviewer cannot evaluate the handoff without "
                "a summary and at least one artifact.\n"
                "\n"
                "Fix: pass --output with a JSON object, e.g.:\n"
                "    pm task done <id> --output '{\n"
                '      "type": "code_change",\n'
                '      "summary": "<what you built>",\n'
                '      "artifacts": [{"kind": "commit", "description": '
                '"impl", "ref": "HEAD"}]\n'
                "    }'"
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
        result = self.get(task_id)
        self._sync_transition(
            result, from_status.value, result.work_status.value
        )
        # #2305: if the advance landed on a state that no longer holds a
        # worker session (terminal ``done``, plus ``queued``/``on_hold``
        # bypass nodes), free the cap slot. ``review`` and a new
        # ``in_progress`` keep the row active for the next node.
        if (
            from_status in _WORKER_SESSION_HOLDING_STATUSES
            and result.work_status not in _WORKER_SESSION_HOLDING_STATUSES
        ):
            self._release_worker_session_for_transition(
                project=task.project,
                task_number=task.task_number,
                ended_at=now,
            )
        self._write_review_summary_after_transition(result)
        if (
            result.flow_template_id == "plan_project"
            and result.current_node_id == "user_approval"
            and result.work_status == WorkStatus.REVIEW
        ):
            try:
                from pollypm.work.plan_review_emit import (
                    maybe_emit_plan_review_on_user_approval,
                )

                maybe_emit_plan_review_on_user_approval(
                    self, task_id, actor or "architect"
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "plan_review user_approval emit skipped for %s",
                    task_id,
                    exc_info=True,
                )
        return result

    def _write_review_summary_after_transition(self, task: Task) -> None:
        """Invoke the LLM review-summary hook after a transition (#1768).

        Mirrors :meth:`_TransitionManager._write_review_summary_after_transition`
        in the sqlite path: when a task lands in ``review`` or ``on_hold``
        we ask :mod:`pollypm.task_review_summary` to generate and persist
        a plain-language summary. Any failure is swallowed with a warning
        so the transition itself remains durable.

        Note: pg's :meth:`get` does not yet hydrate ``executions`` /
        ``context`` (Slice B carryover), so the summary helper would see
        an empty work-output history if called against the raw ``get``
        result. We hydrate those fields locally before invoking the
        generator so the prompt has the worker's submission to summarise.
        """
        if task.work_status not in {WorkStatus.REVIEW, WorkStatus.ON_HOLD}:
            return
        try:
            from pollypm.task_review_summary import (
                PLAIN_SUMMARY_ENTRY_TYPE,
                REVIEW_SUMMARY_ACTOR,
                generate_review_plain_summary,
            )

            hydrated = self._hydrate_task_for_review_summary(task)
            if any(
                entry.entry_type == PLAIN_SUMMARY_ENTRY_TYPE
                for entry in hydrated.context
            ):
                return
            summary = generate_review_plain_summary(hydrated)
            if not summary:
                return
            self.add_context(
                task.task_id,
                REVIEW_SUMMARY_ACTOR,
                summary,
                entry_type=PLAIN_SUMMARY_ENTRY_TYPE,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "review plain summary generation skipped for %s: %s",
                task.task_id,
                exc,
            )

    def _hydrate_task_for_review_summary(self, task: Task) -> Task:
        """Return ``task`` with ``executions`` and ``context`` populated.

        The review-summary generator reads ``task.executions`` (for the
        latest worker output) and ``task.context`` (to short-circuit
        when a summary already exists). pg's :meth:`get` returns a thin
        Task in Slice A; we attach the extra lists here so the generator
        sees the same shape sqlite would have produced.
        """
        executions = self.get_execution(task.task_id)
        context_entries = self.get_context(task.task_id)
        task.executions = executions
        task.context = context_entries
        return task

    def approve(
        self,
        task_id: str,
        actor: str,
        reason: str | None = None,
        skip_gates: bool = False,  # noqa: ARG002 — gate eval is Slice C
        resume_merge: bool = False,  # noqa: ARG002 — git auto-merge is Slice C
    ) -> Task:
        """Approve a review node and advance the flow."""
        self.last_first_shipped_created = False
        task = self.get(task_id)
        if task.work_status != WorkStatus.REVIEW:
            current = task.work_status.value
            if current == "draft":
                hint = (
                    f"Fix: drafts move through the queue, not straight "
                    f"to review. Run `pm task queue {task_id}` to queue "
                    f"it, then have a worker claim + build it."
                )
            elif current == "in_progress":
                hint = (
                    f"Fix: the worker hasn't handed this off yet. Wait "
                    f"for `pm task done {task_id}` to run (which moves "
                    f"the task to 'review'), or check in with the "
                    f"claimant '{task.assignee or 'unknown'}'."
                )
            elif current == "queued":
                hint = (
                    "Fix: this task is waiting for a worker. Approval "
                    "comes after a worker marks it done. Claim + build "
                    "first, or wait for a worker to pick it up."
                )
            else:
                hint = (
                    f"Fix: only tasks in 'review' can be approved. Run "
                    f"`pm task get {task_id}` to inspect the current "
                    f"state, or find a reviewable task with "
                    f"`pm task list --status review`."
                )
            raise InvalidTransitionError(
                f"Cannot approve task in '{current}' state.\n"
                f"\n"
                f"Why: only tasks whose current node is a review node "
                f"(work_status = 'review') can be approved. Approving a "
                f"non-review task would bypass the worker-build step.\n"
                f"\n"
                f"{hint}"
            )
        flow = self._load_flow(task)
        if task.current_node_id is None:
            raise InvalidTransitionError("Task has no current flow node.")
        node = flow.nodes.get(task.current_node_id)
        if node is None or node.type != NodeType.REVIEW:
            raise InvalidTransitionError(
                f"Current node '{task.current_node_id}' is not a review node."
            )

        # Enforce actor-vs-role authorization — same shape sqlite uses
        # so the three-question guidance text drives reviewer errors
        # (#1771).
        self._validate_actor_role(task, node, actor)

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
        result = self.get(task_id)
        self._sync_transition(
            result, WorkStatus.REVIEW.value, result.work_status.value
        )
        # Cascade: any dependents blocked on this task should unblock
        # when approve drives us to DONE. Mirrors sqlite path.
        if result.work_status == WorkStatus.DONE:
            try:
                self._check_auto_unblock(task_id)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "auto_unblock after approve failed for %s",
                    task_id,
                    exc_info=True,
                )
            # #1782: record the first_shipped milestone the same way
            # the sqlite service does. ``maybe_record_first_shipped``
            # checks whether the task landed a commit artifact and, if
            # so, writes the ``first_shipped_at`` state file + pinned
            # activity event. Best-effort; failures are swallowed.
            # #1737: helper now lives in a backend-neutral leaf module
            # so pg_service.py no longer imports from sqlite_service.
            try:
                from pollypm.work.first_shipped import (
                    maybe_record_first_shipped,
                )

                self.last_first_shipped_created = maybe_record_first_shipped(
                    self,
                    task_id,
                    project_path=self._project_path,
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "first_shipped record failed for %s",
                    task_id,
                    exc_info=True,
                )
        # #1780: clear the per-task no_session alert after approve.
        # Mirrors the sqlite WorkTransitionManager._handle_approve_alert_cleanup
        # hook so the alert raised by the heartbeat sweep doesn't sit in
        # the alert store after the task is approved (#953).
        try:
            self._dispatch_clear_no_session_alert(task_id)
        except Exception:  # noqa: BLE001
            logger.debug(
                "no_session alert cleanup after approve failed for %s",
                task_id,
                exc_info=True,
            )
        return result

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
        result = self.get(task_id)
        self._sync_transition(
            result, WorkStatus.REVIEW.value, WorkStatus.REWORK.value
        )
        return result

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
        result = self.get(task_id)
        self._sync_transition(
            result, old_status.value, WorkStatus.BLOCKED.value
        )
        return result

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
    # Notification staging (#1827) — pg-native ports of the sqlite
    # ``service_notifications`` helpers. The pg schema already has the
    # ``notification_staging`` table; these methods read/write it
    # directly. Messages-store digest staging is deferred (it lives in
    # the SQLAlchemy store, which has no pg backend yet); the table-
    # only path is enough for ``maintenance.notification_staging_prune``
    # and the rollup-candidate query the flush job needs.
    # ------------------------------------------------------------------

    def stage_notification(
        self,
        *,
        project: str,
        subject: str,
        body: str,
        actor: str,
        priority: str,
        milestone_key: str | None,
        payload: dict[str, object] | None = None,
    ) -> int:
        """Insert one digest/silent notification staging row."""
        assert priority in {"digest", "silent"}, (
            f"stage_notification called with non-stageable priority {priority!r}"
        )
        payload_dict = dict(payload or {})
        payload_dict.setdefault("subject", subject)
        payload_dict.setdefault("body", body)
        payload_dict.setdefault("actor", actor)
        payload_dict.setdefault("project", project)
        now = _now_iso()
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO notification_staging "
                "(project, subject, body, actor, priority, payload_json, "
                "milestone_key, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s) "
                "RETURNING id",
                (
                    project,
                    subject,
                    body,
                    actor,
                    priority,
                    json.dumps(payload_dict, separators=(",", ":"), default=str),
                    milestone_key,
                    now,
                ),
            )
            row = cur.fetchone()
            new_id = int(row[0]) if row else 0
            conn.commit()
        return new_id

    def list_digest_rollup_candidates(
        self,
        *,
        project: str,
        milestone_key: str | None,
    ):
        """Return un-flushed digest rows for a project / milestone."""
        from pollypm.work.models import DigestRollupCandidate

        if milestone_key is None:
            sql = (
                "SELECT id, subject, body, actor, created_at, payload_json "
                "FROM notification_staging "
                "WHERE project = %s AND milestone_key IS NULL "
                "AND flushed_at IS NULL AND priority = 'digest' "
                "ORDER BY created_at, id"
            )
            params: tuple = (project,)
        else:
            sql = (
                "SELECT id, subject, body, actor, created_at, payload_json "
                "FROM notification_staging "
                "WHERE project = %s AND milestone_key = %s "
                "AND flushed_at IS NULL AND priority = 'digest' "
                "ORDER BY created_at, id"
            )
            params = (project, milestone_key)
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        merged = []
        for row in rows:
            payload_raw = row[5]
            if isinstance(payload_raw, dict):
                payload = payload_raw
            else:
                try:
                    parsed = json.loads(payload_raw) if payload_raw else {}
                except (TypeError, ValueError):
                    parsed = {}
                payload = parsed if isinstance(parsed, dict) else {}
            created_at_raw = row[4]
            created_at = (
                created_at_raw.isoformat()
                if hasattr(created_at_raw, "isoformat")
                else str(created_at_raw or "")
            )
            merged.append(
                DigestRollupCandidate(
                    source="legacy",
                    row_id=int(row[0]),
                    subject=str(row[1] or ""),
                    body=str(row[2] or ""),
                    actor=str(row[3] or "polly"),
                    created_at=created_at,
                    payload=payload,
                )
            )
        return merged

    def mark_rollup_candidates_flushed(
        self,
        candidates,
        *,
        rollup_task_id: str,
        flushed_at: str,
    ) -> None:
        """Mark the given candidates as flushed under a rollup task."""
        legacy_ids = [
            row.row_id for row in candidates if row.source == "legacy"
        ]
        if not legacy_ids:
            return
        placeholders = ",".join("%s" for _ in legacy_ids)
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE notification_staging "
                f"SET flushed_at = %s, rollup_task_id = %s "
                f"WHERE id IN ({placeholders})",
                [flushed_at, rollup_task_id, *legacy_ids],
            )
            conn.commit()

    def has_old_pending_digest_rows(
        self,
        *,
        project: str,
        milestone_key: str | None,
        min_age_seconds: int,
    ) -> bool:
        """True when un-flushed digest rows older than ``min_age_seconds`` exist."""
        from datetime import timedelta

        cutoff = (datetime.now(UTC) - timedelta(seconds=min_age_seconds)).isoformat()
        if milestone_key is None:
            sql = (
                "SELECT COUNT(*) FROM notification_staging "
                "WHERE project = %s AND milestone_key IS NULL "
                "AND flushed_at IS NULL AND priority = 'digest' "
                "AND created_at <= %s"
            )
            params: tuple = (project, cutoff)
        else:
            sql = (
                "SELECT COUNT(*) FROM notification_staging "
                "WHERE project = %s AND milestone_key = %s "
                "AND flushed_at IS NULL AND priority = 'digest' "
                "AND created_at <= %s"
            )
            params = (project, milestone_key, cutoff)
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        return bool(row and row[0])

    def find_flushed_rollup_milestone(self, *, task_id: str) -> str | None:
        """Return the milestone_key for a rollup-flushed staging row."""
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT milestone_key, payload_json FROM notification_staging "
                "WHERE flushed_at IS NOT NULL "
                "ORDER BY flushed_at DESC LIMIT 500"
            )
            rows = cur.fetchall()
        needle = f'"task_id": "{task_id}"'
        needle_compact = f'"task_id":"{task_id}"'
        for row in rows:
            payload = row[1]
            if isinstance(payload, dict):
                payload_str = json.dumps(payload, default=str)
            else:
                payload_str = str(payload or "")
            if (
                needle in payload_str
                or needle_compact in payload_str
                or task_id in payload_str
            ):
                return row[0]
        return None

    def prune_staged_notifications(
        self, *, retain_days: int = 30
    ) -> dict[str, int]:
        """Delete flushed/silent staging rows older than retain_days."""
        from datetime import timedelta

        cutoff = (datetime.now(UTC) - timedelta(days=retain_days)).isoformat()
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM notification_staging "
                "WHERE flushed_at IS NOT NULL AND flushed_at <= %s",
                (cutoff,),
            )
            flushed_deleted = cur.rowcount or 0
            cur.execute(
                "DELETE FROM notification_staging "
                "WHERE priority = 'silent' AND created_at <= %s",
                (cutoff,),
            )
            silent_deleted = cur.rowcount or 0
            conn.commit()
        return {
            "flushed_pruned": int(flushed_deleted),
            "silent_pruned": int(silent_deleted),
        }

    # ------------------------------------------------------------------
    # Inbox interaction methods — reply / archive / read-marker (#1776)
    #
    # These four wrap context-entry primitives with the idempotency +
    # event-emission shape the cockpit's Textual inbox screen relies
    # on. Mirror the sqlite path so cockpit code stays backend-agnostic.
    # ------------------------------------------------------------------

    def add_reply(
        self, task_id: str, body: str, actor: str = "user",
    ) -> ContextEntry:
        """Record a user reply on an inbox task.

        Stored as a ``work_context_entries`` row with
        ``entry_type='reply'`` so :meth:`list_replies` and the inbox
        thread view can render chat turns without collision with
        system/notes context. Raises :class:`ValidationError` when
        ``body`` is empty after strip.
        """
        if not body or not body.strip():
            raise ValidationError("Reply body must not be empty.")
        return self.add_context(
            task_id, actor, body.strip(), entry_type="reply",
        )

    def list_replies(self, task_id: str) -> list[ContextEntry]:
        """Return reply entries for a task in chronological order.

        Thin wrapper over :meth:`get_context` — :meth:`get_context`
        returns newest-first; the inbox view wants oldest-first for the
        natural reading order, so we reverse here.
        """
        entries = self.get_context(task_id, entry_type="reply")
        entries.reverse()
        return entries

    def mark_read(self, task_id: str, actor: str = "user") -> bool:
        """Record a read-marker on an inbox task if one isn't already present.

        Returns ``True`` when a new marker row was written, ``False``
        when a ``read`` row already existed (idempotent repeat-open).
        Callers use the return value to gate event emission so the
        activity feed only sees the *first* open.
        """
        project, task_number = _parse_task_id(task_id)
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
                    "SELECT 1 FROM work_context_entries "
                    "WHERE task_project = %s AND task_number = %s "
                    "AND entry_type = 'read' LIMIT 1",
                    (project, task_number),
                )
                if cur.fetchone() is not None:
                    return False
                cur.execute(
                    "INSERT INTO work_context_entries "
                    "(task_project, task_number, actor, text, created_at, "
                    "entry_type) "
                    "VALUES (%s, %s, %s, %s, %s, 'read')",
                    (
                        project,
                        task_number,
                        actor,
                        "opened in cockpit inbox",
                        _now_iso(),
                    ),
                )
            conn.commit()
        return True

    def archive_task(
        self, task_id: str, actor: str = "user", *, strict: bool = False,
    ) -> Task:
        """Flip an inbox task to the chat-flow terminal state.

        By default this is idempotent: archiving an already-terminal
        task is a no-op and returns the current record unchanged. Uses
        the same underlying transition shape as :meth:`mark_done`
        (audit row + auto-unblock cascade) so dashboard counts and
        dependency unblocking stay consistent.

        When ``strict=True`` is passed, the state transition is
        performed atomically (single conditional UPDATE that asserts
        the row is still non-terminal) and the method raises
        :class:`InvalidTransitionError` if the row was already
        terminal when the write executed. This closes the
        concurrent-archive race the Web API exposes — two simultaneous
        ``POST /inbox/{id}/archive`` calls now produce exactly one 200
        and one 409 ``invalid_state`` (see #2060 Codex review).
        """
        task = self.get(task_id)
        if not strict and task.work_status in TERMINAL_STATUSES:
            return task
        project, task_number = task.project, task.task_number
        now = _now_iso()
        from_status = task.work_status
        terminal_values = sorted(s.value for s in TERMINAL_STATUSES)
        # Inline the terminal-status placeholders — psycopg binds
        # tuples positionally to %s and NOT IN expects an explicit
        # list expression. The values come from the WorkStatus enum
        # so there's no injection surface.
        placeholders = ",".join(["%s"] * len(terminal_values))
        with self._pool.connection() as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                # Atomically claim the transition: the WHERE guard
                # asserts the row is still non-terminal at write time,
                # so concurrent archivers race on this single UPDATE
                # rather than on a stale read. Postgres serialises
                # row-level writes, so exactly one caller sees
                # rowcount==1; the loser sees rowcount==0 and (under
                # ``strict``) raises InvalidTransitionError instead of
                # silently returning the terminal record.
                cur.execute(
                    "UPDATE work_tasks SET work_status = %s, updated_at = %s "
                    "WHERE project = %s AND task_number = %s "
                    f"AND work_status NOT IN ({placeholders}) "
                    "RETURNING work_status",
                    (
                        WorkStatus.DONE.value,
                        now,
                        project,
                        task_number,
                        *terminal_values,
                    ),
                )
                claimed = cur.fetchone() is not None
                if not claimed:
                    conn.rollback()
                    if strict:
                        current = self.get(task_id)
                        raise InvalidTransitionError(
                            f"Task {task_id} is already "
                            f"{current.work_status.value}; cannot archive."
                        )
                    # Non-strict fallthrough: row raced to terminal
                    # between the pre-read and the UPDATE. Return the
                    # current record (idempotent contract).
                    return self.get(task_id)
                cur.execute(
                    "INSERT INTO work_transitions ("
                    "task_project, task_number, from_state, to_state, "
                    "actor, reason, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        project,
                        task_number,
                        from_status.value,
                        WorkStatus.DONE.value,
                        actor,
                        "inbox.archive",
                        now,
                    ),
                )
            conn.commit()
        # #1787: audit emit after commit so the JSONL trail tracks this
        # transition the same way ``_simple_transition`` does.
        self._emit_status_changed_audit(
            project=project,
            task_number=task_number,
            from_state=from_status.value,
            to_state=WorkStatus.DONE.value,
            actor=actor,
            reason="inbox.archive",
        )
        result = self.get(task_id)
        self._sync_transition(result, from_status.value, WorkStatus.DONE.value)
        # Cascade: any dependents blocked on this task should unblock,
        # same as mark_done. archive_task is effectively 'done' for the
        # chat-flow, so we respect the same contract.
        try:
            self._check_auto_unblock(task_id)
        except Exception:  # noqa: BLE001
            logger.debug(
                "auto_unblock after archive failed for %s",
                task_id,
                exc_info=True,
            )
        return result

    def task_numbers_with_context_entry(
        self, *, project: str, entry_type: str,
    ) -> set[int]:
        """Return task numbers that have at least one entry of ``entry_type``.

        Used by the inbox loader's read-marker check — collapses a
        per-task ``get_context(..., entry_type='read', limit=1)`` loop
        into one project-wide query.
        """
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT task_number FROM work_context_entries "
                "WHERE task_project = %s AND entry_type = %s",
                (project, entry_type),
            )
            rows = cur.fetchall()
        return {int(r[0]) for r in rows}

    def bulk_list_replies(self, *, project: str) -> dict[int, list[ContextEntry]]:
        """Return ``task_number -> [reply entries (oldest first)]``.

        One query, bucketed in Python. Replaces a per-task
        :meth:`list_replies` loop on the inbox loader hot path.
        """
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT task_number, actor, created_at, text, entry_type "
                "FROM work_context_entries "
                "WHERE task_project = %s AND entry_type = 'reply' "
                "ORDER BY id ASC",
                (project,),
            )
            rows = cur.fetchall()
        out: dict[int, list[ContextEntry]] = {}
        for r in rows:
            entry = ContextEntry(
                actor=str(r[1]),
                timestamp=r[2],
                text=str(r[3]),
                entry_type=str(r[4] or "reply"),
            )
            out.setdefault(int(r[0]), []).append(entry)
        return out

    def latest_snoozes_bulk(
        self, task_keys: list[tuple[str, int]],
    ) -> dict[tuple[str, int], ContextEntry]:
        """Return ``{(project, task_number): latest_snooze_entry}`` (#2060).

        SINGLE SQL query — replaces the per-task
        ``get_context(entry_type='snooze', limit=1)`` loop the inbox
        list path was running for every visible task (Codex round-2
        blocker on PR #2060). The inbox-list helper uses this to
        decide which items are still snoozed (wake time in the
        future) without paying for N round-trips on a user-facing
        scan path.

        Mirrors the existing :meth:`bulk_list_replies` /
        :meth:`task_numbers_with_context_entry` pattern: one
        statement, bucketed in Python, the "latest" row per task is
        picked via ``DISTINCT ON`` + ``ORDER BY id DESC`` so it
        matches what ``get_context(..., limit=1)`` returns per-row.

        ``task_keys`` is the list of ``(project, task_number)`` pairs
        the caller wants snooze state for; an empty list short-
        circuits to ``{}`` without hitting the DB. Tasks with no
        snooze rows are simply absent from the result mapping.
        """
        if not task_keys:
            return {}
        # ``task_key`` ANY-array filter keeps the statement to one
        # bind regardless of N — psycopg adapts the list of tuples
        # into a row-comparison array. DISTINCT ON (project, num) +
        # ORDER (project, num, id DESC) picks the most-recent row per
        # task, matching ``get_context(..., limit=1)`` semantics.
        projects = [k[0] for k in task_keys]
        numbers = [k[1] for k in task_keys]
        sql = (
            "SELECT DISTINCT ON (task_project, task_number) "
            "task_project, task_number, actor, created_at, text, entry_type "
            "FROM work_context_entries "
            "WHERE entry_type = 'snooze' "
            "AND (task_project, task_number) IN ("
            "SELECT UNNEST(%s::text[]), UNNEST(%s::int[])) "
            "ORDER BY task_project, task_number, id DESC"
        )
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (projects, numbers))
            rows = cur.fetchall()
        out: dict[tuple[str, int], ContextEntry] = {}
        for r in rows:
            entry = ContextEntry(
                actor=str(r[2]),
                timestamp=r[3],
                text=str(r[4]),
                entry_type=str(r[5] or "snooze"),
            )
            out[(str(r[0]), int(r[1]))] = entry
        return out

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
        """All tasks blocked by this task, transitively.

        Bulk-loads in a single SELECT to avoid the per-node ``get()``
        round-trip the slice-A implementation issued (#1770).
        """
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
            task_keys = [(str(row[0]), int(row[1])) for row in cur.fetchall()]
        if not task_keys:
            return []
        # Bulk-fetch every dependent in one query, then hydrate
        # relationships in one more query — never per-row ``get()``.
        where_clauses = " OR ".join(
            "(project = %s AND task_number = %s)" for _ in task_keys
        )
        params: list[object] = []
        for p, n in task_keys:
            params.extend([p, n])
        bulk_sql = (
            "SELECT project, task_number, project_key, title, type, labels, "
            "work_status, flow_template_id, flow_template_version, "
            "current_node_id, assignee, claimed_by_session, "
            "priority, requires_human_review, "
            "description, acceptance_criteria, constraints, relevant_files, "
            "parent_project, parent_task_number, "
            "supersedes_project, supersedes_task_number, "
            "plan_version, predecessor_task_id, kind, roles, external_refs, "
            "created_at, created_by, updated_at "
            f"FROM work_tasks WHERE {where_clauses} "
            "ORDER BY project, task_number"
        )
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(bulk_sql, params)
            rows = cur.fetchall()
        rels_by_key = self._load_relationships_bulk(task_keys)
        return [
            self._row_to_task(
                row,
                relationships=rels_by_key.get(
                    (str(row[0]), int(row[1])), None
                ),
            )
            for row in rows
        ]

    def _check_auto_unblock(self, task_id: str) -> None:
        """After a task hits a terminal state, unblock its dependents.

        Mirrors :meth:`SQLiteWorkService._check_auto_unblock` /
        :func:`service_dependencies.check_auto_unblock`. Walks the
        outgoing ``blocks`` edges and, for each target that's still
        BLOCKED and now has no unresolved blockers, transitions it
        back to QUEUED with a system audit row.
        """
        task = self.get(task_id)
        project, task_number = task.project, task.task_number
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT to_project, to_task_number "
                "FROM work_task_dependencies "
                "WHERE from_project = %s AND from_task_number = %s "
                "AND kind = %s",
                (project, task_number, LinkKind.BLOCKS.value),
            )
            rows = cur.fetchall()
        for row in rows:
            blocked_id = f"{row[0]}/{row[1]}"
            try:
                blocked_task = self.get(blocked_id)
            except TaskNotFoundError:
                continue
            if blocked_task.work_status != WorkStatus.BLOCKED:
                continue
            if self._has_unresolved_blockers(blocked_id):
                continue
            self._simple_transition(
                blocked_id,
                from_state=WorkStatus.BLOCKED,
                to_state=WorkStatus.QUEUED,
                actor="system",
                reason=f"auto-unblocked, blocker {task.task_id} completed",
            )

    # ------------------------------------------------------------------
    # Assignment alert cleanup (#1780)
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_alert_store() -> object | None:
        """Best-effort resolve a Store handle for alert writes.

        Mirrors :meth:`WorkTransitionManager._resolve_alert_store`. Returns
        ``None`` when config / store cannot be loaded — the listener-side
        helper then falls back to its own ``load_runtime_services`` path
        so environments without a config still clear the per-task alert.
        """
        try:
            from pollypm.config import DEFAULT_CONFIG_PATH, load_config
            from pollypm.store.registry import get_store

            config = load_config(DEFAULT_CONFIG_PATH)
            return get_store(config)
        except Exception:  # noqa: BLE001
            return None

    def _dispatch_cancel_assignment_alerts(self, task: Task) -> None:
        """Publish cleanup for assignment alerts on a just-cancelled task.

        Mirrors :meth:`WorkTransitionManager._clear_assignment_alerts_after_cancel`.
        The plugin handles the event when loaded; without that
        subscriber this is a no-op.
        """
        from pollypm.work import task_assignment_alerts

        try:
            roles = tuple((task.roles or {}).keys()) or ("worker",)
        except Exception:  # noqa: BLE001
            roles = ("worker",)
        # #941 same as sqlite path — only QUEUED / IN_PROGRESS / REVIEW
        # / REWORK siblings are "active" for the project-level alert.
        active_statuses = (
            WorkStatus.QUEUED.value,
            WorkStatus.IN_PROGRESS.value,
            WorkStatus.REVIEW.value,
            WorkStatus.REWORK.value,
        )
        active_map: dict[str, bool] = {}
        for role in roles:
            active_map[role] = False
            for status in active_statuses:
                try:
                    siblings = self.list_tasks(
                        project=task.project,
                        work_status=status,
                    )
                except Exception:  # noqa: BLE001
                    siblings = []
                for sibling in siblings:
                    if sibling.task_number == task.task_number:
                        continue
                    sibling_roles = getattr(sibling, "roles", {}) or {}
                    if role in sibling_roles:
                        active_map[role] = True
                        break
                if active_map[role]:
                    break
        task_assignment_alerts.dispatch(
            task_assignment_alerts.CancelledTaskAssignmentAlertsEvent(
                task_id=task.task_id,
                project=task.project,
                role_names=roles,
                has_other_active_for_role=active_map,
                store=self._resolve_alert_store(),
            )
        )

    def _dispatch_clear_no_session_alert(self, task_id: str) -> None:
        """Publish cleanup for a per-task no_session alert after approve.

        Mirrors :meth:`WorkTransitionManager._clear_no_session_alert_after_approve`.
        Narrower than the cancel cleanup: only the per-task alert is
        cleared. The project-level alert stays open because other active
        siblings may still need the role.
        """
        from pollypm.work import task_assignment_alerts

        task_assignment_alerts.dispatch(
            task_assignment_alerts.ClearNoSessionAlertForTaskEvent(
                task_id=task_id,
                store=self._resolve_alert_store(),
            )
        )

    def _on_cancelled(self, task_id: str) -> None:
        """After a cancellation, leave a context note on each dependent.

        Mirrors :func:`service_dependencies.on_cancelled`. The PM still
        decides whether to unblock or cancel each dependent — we just
        record the breadcrumb so the operator notices.
        """
        try:
            task = self.get(task_id)
        except TaskNotFoundError:
            return
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT to_project, to_task_number "
                "FROM work_task_dependencies "
                "WHERE from_project = %s AND from_task_number = %s "
                "AND kind = %s",
                (task.project, task.task_number, LinkKind.BLOCKS.value),
            )
            rows = cur.fetchall()
        for row in rows:
            blocked_id = f"{row[0]}/{row[1]}"
            try:
                self.add_context(
                    blocked_id,
                    "system",
                    f"blocker {task.task_id} was cancelled "
                    "— PM must decide whether to unblock or cancel this task.",
                )
            except TaskNotFoundError:
                continue

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

        Mirrors :meth:`SQLiteWorkService.validate_advance` — evaluates
        the declared gates on the current flow node plus an
        actor-vs-role synthetic gate so permission preflight is
        accurate (#1774).
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
        if node.gates:
            from pollypm.work.gates import GateRegistry, evaluate_gates

            registry = GateRegistry(project_path=self._project_path)
            kwargs: dict[str, object] = {"get_task": self.get}
            if self._project_path is not None:
                kwargs["project_root"] = self._project_path
            results.extend(
                evaluate_gates(task, node.gates, registry, **kwargs)
            )
        return results

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    def sync_status(self, task_id: str) -> dict[str, object]:
        """Current sync state per adapter for a task.

        Reads ``work_sync_state`` and merges in any registered adapters
        that have never run (so the cockpit shows ``attempts=0`` instead
        of hiding the adapter row). Mirrors
        :meth:`SQLiteWorkService.sync_status`.
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
        result: dict[str, object] = {
            str(r[0]): {
                "last_synced_at": r[1],
                "last_error": r[2],
                "attempts": int(r[3] or 0),
            }
            for r in rows
        }
        if self._sync is not None:
            for adapter in self._sync.adapters:
                name = getattr(adapter, "name", None)
                if name and name not in result:
                    result[name] = {
                        "last_synced_at": None,
                        "last_error": None,
                        "attempts": 0,
                    }
        return result

    def _record_sync_state(
        self,
        project: str,
        task_number: int,
        adapter_name: str,
        *,
        success: bool,
        error: str | None,
    ) -> None:
        """Upsert a ``work_sync_state`` row after a sync attempt."""
        now = _now_iso() if success else None
        with self._pool.connection() as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO work_sync_state "
                    "(task_project, task_number, adapter_name, "
                    "last_synced_at, last_error, attempts) "
                    "VALUES (%s, %s, %s, %s, %s, 1) "
                    "ON CONFLICT (task_project, task_number, adapter_name) "
                    "DO UPDATE SET "
                    "last_synced_at = COALESCE(EXCLUDED.last_synced_at, "
                    "work_sync_state.last_synced_at), "
                    "last_error = EXCLUDED.last_error, "
                    "attempts = work_sync_state.attempts + 1",
                    (project, task_number, adapter_name, now, error),
                )
            conn.commit()

    def trigger_sync(
        self,
        task_id: str | None = None,
        adapter: str | None = None,
    ) -> dict[str, object]:
        """Force a sync cycle.

        Mirrors :meth:`SQLiteWorkService.trigger_sync`: iterates every
        task (or just ``task_id``), invokes ``on_create`` on each
        registered adapter, and records per-adapter outcomes in
        ``work_sync_state``. Returns
        ``{"synced": int, "errors": {adapter_name: [task_id, ...]}}``.
        Without a sync manager attached, returns the empty summary.
        """
        summary: dict[str, object] = {"synced": 0, "errors": {}}

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
            task_ids = [task_id]
        else:
            with self._pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT project, task_number FROM work_tasks "
                    "ORDER BY project, task_number"
                )
                task_ids = [f"{r[0]}/{r[1]}" for r in cur.fetchall()]

        if self._sync is None:
            return summary

        adapters = [
            item
            for item in self._sync.adapters
            if adapter is None or getattr(item, "name", None) == adapter
        ]
        if not adapters:
            return summary

        errors: dict[str, list[str]] = {}
        synced = 0
        for tid in task_ids:
            try:
                task = self.get(tid)
            except TaskNotFoundError:
                continue
            project, task_number = _parse_task_id(tid)
            for current in adapters:
                name = getattr(current, "name", "unknown")
                err: str | None = None
                try:
                    current.on_create(task)
                except Exception as exc:  # noqa: BLE001
                    err = str(exc)
                    errors.setdefault(name, []).append(tid)
                self._record_sync_state(
                    project,
                    task_number,
                    name,
                    success=(err is None),
                    error=err,
                )
                if err is None:
                    synced += 1

        summary["synced"] = synced
        summary["errors"] = errors
        return summary

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
            "current_node_id, assignee, claimed_by_session, "
            "priority, requires_human_review, "
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
        keys = [(str(r[0]), int(r[1])) for r in rows]
        rels_by_key = self._load_relationships_bulk(keys)
        return [
            self._row_to_task(
                row,
                relationships=rels_by_key.get(
                    (str(row[0]), int(row[1])), None
                ),
            )
            for row in rows
        ]

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
            "current_node_id, assignee, claimed_by_session, "
            "priority, requires_human_review, "
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
        keys = [(str(r[0]), int(r[1])) for r in rows]
        rels_by_key = self._load_relationships_bulk(keys)
        return [
            self._row_to_task(
                row,
                relationships=rels_by_key.get(
                    (str(row[0]), int(row[1])), None
                ),
            )
            for row in rows
        ]

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

    def reserve_worker_cap_slot(
        self,
        *,
        task_project: str,
        task_number: int,
        agent_name: str,
        started_at: str,
        cap: int,
    ) -> bool:
        """Atomically reserve a per-project worker-cap slot (#1883).

        Holds a transaction-scoped ``pg_advisory_xact_lock`` keyed on
        ``"worker_cap:{task_project}"`` while counting active rows and
        inserting the placeholder. Two concurrent ``provision_worker``
        calls for different ``task_number``\\s on the same project
        therefore serialise on the lock; once the first commits its
        placeholder, the second's COUNT sees the new row and (if at
        cap) returns ``False`` without inserting.

        Idempotent for the same ``(task_project, task_number)``: an
        existing row (active or not) short-circuits to ``True`` without
        another insert. The caller's per-task filesystem lock handles
        the duplicate-provision check upstream; this branch is the
        belt-and-suspenders so a racing harness can't double-count.
        """
        with self._pool.connection() as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock("
                    "hashtextextended(%s, 0))",
                    (f"work_sessions.worker_cap:{task_project}",),
                )
                # Idempotent path: an existing row for this task
                # consumes its own slot already.
                cur.execute(
                    "SELECT ended_at FROM work_sessions "
                    "WHERE task_project = %s AND task_number = %s",
                    (task_project, task_number),
                )
                existing = cur.fetchone()
                if existing is not None and existing[0] is None:
                    conn.commit()
                    return True
                cur.execute(
                    "SELECT COUNT(*) FROM work_sessions "
                    "WHERE task_project = %s AND ended_at IS NULL",
                    (task_project,),
                )
                active = int(cur.fetchone()[0])
                if active >= int(cap):
                    conn.rollback()
                    return False
                # Insert/resurrect a placeholder row so subsequent
                # concurrent callers see the higher count BEFORE the
                # caller finishes worktree creation. ``pane_id=""``
                # is the sentinel for "reservation in flight" — the
                # successful provision upserts real values; a failed
                # provision stamps ``ended_at`` so the slot is freed.
                cur.execute(
                    "INSERT INTO work_sessions ("
                    "task_project, task_number, agent_name, pane_id, "
                    "worktree_path, branch_name, started_at"
                    ") VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (task_project, task_number) DO UPDATE SET "
                    "agent_name = EXCLUDED.agent_name, "
                    "pane_id = EXCLUDED.pane_id, "
                    "worktree_path = EXCLUDED.worktree_path, "
                    "branch_name = EXCLUDED.branch_name, "
                    "started_at = EXCLUDED.started_at, "
                    "ended_at = NULL, archive_path = NULL",
                    (
                        task_project,
                        task_number,
                        agent_name,
                        "",
                        "",
                        "",
                        started_at,
                    ),
                )
            conn.commit()
        return True

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

    def _release_worker_session_for_transition(
        self,
        *,
        project: str,
        task_number: int,
        ended_at: str | datetime,
    ) -> None:
        """End any active worker session row for ``(project, task_number)``.

        #2305 — the per-project worker-cap (``max_parallel_workers``)
        counts ``work_sessions`` rows with ``ended_at IS NULL``. Pre-fix
        only :meth:`release` and the teardown helpers stamped
        ``ended_at``; transitions through :meth:`mark_done`,
        :meth:`force_review`, :meth:`cancel`, :meth:`hold`, the
        ``node_done`` terminal advance, and the post-commit claim
        rollback all left the row open. After enough such transitions
        the project hit cap and every subsequent claim returned 429
        ``worker_cap_exceeded`` until ``pm serve`` restarted.

        This helper stamps ``ended_at`` only on rows that are still
        active so re-running it is a no-op and we don't accidentally
        backdate an already-ended row. Best-effort: a DB error is
        logged at debug and swallowed because the surfaced transition
        is the primary signal; the next claim will re-evaluate cap
        from source-of-truth.
        """
        try:
            with self._pool.connection() as conn:
                conn.autocommit = False
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE work_sessions SET ended_at = %s "
                        "WHERE task_project = %s AND task_number = %s "
                        "AND ended_at IS NULL",
                        (ended_at, project, task_number),
                    )
                conn.commit()
        except Exception:  # noqa: BLE001
            logger.debug(
                "release worker session for %s/%d failed",
                project, task_number, exc_info=True,
            )

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

        #1825: prefer the constructor-supplied config over the operator's
        default ``load_config()``. Multi-workspace / test-isolated
        callers depend on this so custom flow templates resolve against
        the right project root.
        """
        if project is None:
            return None

        def _lookup(config) -> object | None:
            try:
                projects = getattr(config, "projects", None) or {}
                normalized = project.replace("-", "_")
                key = (
                    project
                    if project in projects
                    else (normalized if normalized in projects else None)
                )
                if key is not None:
                    return projects[key].path
            except Exception:  # noqa: BLE001
                logger.debug(
                    "project path config lookup failed for %s",
                    project,
                    exc_info=True,
                )
            return None

        # Prefer the config the constructor was opened with.
        if self._config is not None:
            path = _lookup(self._config)
            if path is not None:
                return path

        # Fall back to ``load_config()``. Logged so the fallback is
        # visible when debugging unexpected flow-resolution behaviour.
        try:
            from pollypm.config import load_config

            fallback_config = load_config()
        except Exception:  # noqa: BLE001 — config lookup is best-effort
            logger.debug(
                "project path config fallback load failed for %s",
                project,
                exc_info=True,
            )
            return None
        if self._config is not None:
            logger.debug(
                "project path fallback to load_config() for %s "
                "(constructor config missing project)",
                project,
            )
        return _lookup(fallback_config)

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
        """Validate that ``actor`` is authorized for ``node``.

        Mirrors :meth:`SQLiteWorkService._validate_actor_role`: emits the
        three-question guidance text (#240) so workers / reviewers can
        copy-paste the rerun command from the error itself.
        """
        if node.actor_type == ActorType.HUMAN:
            reviewer = None
            if node.actor_role:
                reviewer = task.roles.get(node.actor_role)
            allowed: list[str] = []
            seen: set[str] = set()
            for name in (reviewer, *sorted(self._HUMAN_ACTOR_NAMES)):
                if name and name not in seen:
                    allowed.append(name)
                    seen.add(name)
            if actor not in allowed:
                schema = "actor_type='human'"
                if node.actor_role:
                    schema += f", actor_role='{node.actor_role}'"
                    if reviewer:
                        schema += (
                            f", task.roles['{node.actor_role}']='{reviewer}'"
                        )
                raise ValidationError(
                    f"Node '{node.name}' requires human review ({schema}). "
                    f"Actor '{actor}' is not authorized. "
                    f"Accepted actors: {', '.join(repr(name) for name in allowed)}. "
                    f"Fix: rerun this action with --actor {allowed[0]}."
                )
        elif node.actor_type == ActorType.ROLE and node.actor_role:
            expected_actor = task.roles.get(node.actor_role)
            if expected_actor and actor != expected_actor:
                if actor != node.actor_role:
                    raise ValidationError(
                        f"Actor '{actor}' does not match role "
                        f"'{node.actor_role}' (expected '{expected_actor}'). "
                        f"Node '{node.name}' uses actor_type='role', "
                        f"actor_role='{node.actor_role}', "
                        f"task.roles['{node.actor_role}']='{expected_actor}'. "
                        f"Accepted actors: '{expected_actor}' or literal role "
                        f"name '{node.actor_role}'. "
                        f"Fix: rerun this action with --actor {expected_actor}."
                    )
            elif expected_actor is None and actor != node.actor_role:
                raise ValidationError(
                    f"Actor '{actor}' does not match role '{node.actor_role}'. "
                    f"Node '{node.name}' uses actor_type='role', "
                    f"actor_role='{node.actor_role}', but this task has no "
                    f"binding in task.roles['{node.actor_role}']. "
                    f"Accepted actor: '{node.actor_role}'. "
                    f"Fix: rerun this action with --actor {node.actor_role}, "
                    f"or update the role binding."
                )
        elif node.actor_type == ActorType.AGENT and node.agent_name:
            if actor != node.agent_name:
                raise ValidationError(
                    f"Node '{node.name}' is pinned to agent "
                    f"'{node.agent_name}' (actor_type='agent', "
                    f"agent_name='{node.agent_name}'). Actor '{actor}' is not "
                    f"authorized. Fix: rerun this action with --actor "
                    f"{node.agent_name}."
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
        # #1787: emit ``task.status_changed`` audit in-transaction (same
        # shape sqlite uses in ``_record_transition``). The audit module
        # is best-effort and never raises, so a failed write only loses
        # one event — never blocks the transition.
        self._emit_status_changed_audit(
            project=project,
            task_number=task_number,
            from_state=from_state,
            to_state=to_state,
            actor=actor,
            reason=reason,
        )
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
        """Validate a WorkOutput has required fields and at least one artifact.

        Mirrors :meth:`SQLiteWorkService._validate_work_output` — emits the
        three-question guidance text (#240 / #1771) so workers can
        copy-paste the corrected --output JSON from the error.
        """
        if not isinstance(output.type, OutputType):
            try:
                OutputType(output.type)
            except (ValueError, KeyError):
                raise ValidationError(
                    f"Invalid output type '{output.type}'."
                )
        if not output.summary or not output.summary.strip():
            raise ValidationError(
                "Work output has an empty summary.\n"
                "\n"
                "Why: the reviewer needs a one-paragraph explanation of "
                "what you built.\n"
                "\n"
                "Fix: include a non-empty \"summary\" in your --output "
                "JSON, for example:\n"
                "    pm task done <id> --output '{\n"
                "      \"type\": \"code_change\",\n"
                "      \"summary\": \"Implemented X; all tests green.\",\n"
                "      \"artifacts\": [{\"kind\": \"commit\", \"description\": "
                "\"impl\", \"ref\": \"HEAD\"}]\n"
                "    }'"
            )
        if not output.artifacts:
            raise ValidationError(
                "Work output must have at least one artifact.\n"
                "\n"
                "Why: the reviewer needs concrete evidence of what you "
                "built — a commit SHA, a changed file, or a recorded "
                "action — before a task can advance to review.\n"
                "\n"
                "Fix: include an \"artifacts\" array in your --output "
                "JSON. Common shapes:\n"
                "    commit:      {\"kind\": \"commit\", \"description\": "
                "\"impl\", \"ref\": \"HEAD\"}\n"
                "    file change: {\"kind\": \"file_change\", \"description\": "
                "\"docs\", \"path\": \"README.md\"}\n"
                "    note:        {\"kind\": \"note\", \"description\": "
                "\"investigated X; no code change needed\"}\n"
                "\n"
                "Full example:\n"
                "    pm task done <id> --output '{\n"
                "      \"type\": \"code_change\",\n"
                "      \"summary\": \"...\",\n"
                "      \"artifacts\": [{\"kind\": \"commit\", \"description\": "
                "\"impl\", \"ref\": \"HEAD\"}]\n"
                "    }'"
            )
        for i, art in enumerate(output.artifacts):
            if not isinstance(art.kind, ArtifactKind):
                try:
                    ArtifactKind(art.kind)
                except (ValueError, KeyError):
                    raise ValidationError(
                        f"Artifact {i}: invalid kind '{art.kind}'. "
                        f"Expected one of: commit, file_change, action, note. "
                        f"Fix: change \"kind\" in your --output JSON to one "
                        f"of the four supported values."
                    )
            if not (art.description or art.ref or art.path):
                raise ValidationError(
                    f"Artifact {i}: must have at least one of "
                    f"description, ref, or path. "
                    f"Fix: add a \"description\" field, or a \"ref\" (SHA) "
                    f"for commits, or a \"path\" for file changes."
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
