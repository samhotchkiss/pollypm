"""SQLite-backed implementation of the WorkService protocol.

Contract:
- Inputs: typed task/workflow fields, notification metadata, dependency
  links, worker-session updates, and sync adapter names.
- Outputs: typed ``Task`` / ``WorkerSessionRecord`` models plus durable
  side effects in the work SQLite database.
- Side effects: owns schema creation, task state transitions, workflow
  execution bookkeeping, dependency mutations, notification staging, and
  sync-attempt persistence.
- Invariants: this module is the only owner of work-database connection
  lifecycle and schema writes; callers use typed methods instead of
  reaching into ``_conn`` or issuing ad-hoc SQL.
- Allowed dependencies: flow engine, gates, schema helpers, and the
  boundary-owned ``service_*.py`` submodules in this package.
- Private: SQL row shapes, sqlite connection helpers, and internal
  adapter plumbing delegated to the ``service_*.py`` modules.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

from pollypm.rejection_feedback import emit_rejection_feedback
from pollypm.work.flow_engine import resolve_flow
from pollypm.work.gates import GateRegistry, evaluate_gates, has_hard_failure
from pollypm.work.models import (
    GateResult,
    ActorType,
    Artifact,
    ArtifactKind,
    ContextEntry,
    Decision,
    ExecutionStatus,
    FlowNode,
    FlowNodeExecution,
    FlowTemplate,
    LinkKind,
    NodeType,
    OutputType,
    Priority,
    Task,
    TaskType,
    Transition,
    DigestRollupCandidate,
    WorkerSessionRecord,
    WorkOutput,
    WorkStatus,
    TERMINAL_STATUSES,
)
from pollypm.work.schema import create_work_tables
from pollypm.work.service_notifications import (
    find_flushed_rollup_milestone,
    has_old_pending_digest_rows,
    list_digest_rollup_candidates,
    mark_rollup_candidates_flushed,
    prune_staged_notifications,
    stage_notification_row,
)
from pollypm.work.service_dependencies import (
    check_auto_unblock,
    dependent_tasks,
    has_unresolved_blockers,
    link_tasks,
    maybe_block,
    maybe_unblock,
    on_cancelled,
    unlink_tasks,
    would_create_cycle,
)
from pollypm.work.service_queries import (
    blocked_tasks as read_blocked_tasks,
    create_task,
    get_task,
    list_tasks as read_tasks,
    my_tasks as read_my_tasks,
    next_task,
    state_counts as read_state_counts,
    update_task,
)
from pollypm.work.service_support import (
    InvalidTransitionError,
    TaskNotFoundError,
    ValidationError,
    WorkServiceError,
    _now,
    _parse_task_id,
)
from pollypm.work.service_sync import (
    record_sync_state,
    sync_status as read_sync_status,
    trigger_sync as run_trigger_sync,
)
from pollypm.work.service_transitions import (
    advance_to_node,
    current_node_visit as read_current_node_visit,
    mark_done as mark_task_done,
    next_visit as read_next_visit,
    on_task_done,
    on_task_transition,
)
from pollypm.work.service_worker_sessions import (
    WORK_SESSIONS_DDL,
    end_worker_session as finish_worker_session,
    ensure_worker_session_schema as ensure_worker_sessions,
    get_worker_session as read_worker_session,
    list_worker_sessions as read_worker_sessions,
    row_to_worker_session_record,
    update_worker_session_tokens as save_worker_session_tokens,
    upsert_worker_session as save_worker_session,
)
from pollypm.work.sync import SyncManager


# ---------------------------------------------------------------------------
# SQLiteWorkService
# ---------------------------------------------------------------------------


class SQLiteWorkService:
    """SQLite-backed work service implementing the WorkService protocol."""

    def __init__(
        self,
        db_path: Path,
        project_path: Path | None = None,
        sync_manager: SyncManager | None = None,
        session_manager: object | None = None,
    ) -> None:
        self._db_path = db_path
        self._project_path = project_path
        self._sync = sync_manager
        self._session_mgr = session_manager
        self._conn = sqlite3.connect(str(db_path), timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        # Wait up to 30s for busy locks before raising — prevents the
        # Textual inbox UI from erroring when the heartbeat or other
        # writers hold the DB briefly.
        self._conn.execute("PRAGMA busy_timeout=30000")
        create_work_tables(self._conn)
        self._gate_registry = GateRegistry(project_path=project_path)
        self._flow_cache: dict[tuple[str, int], FlowTemplate] = {}
        # Last-provision-error breadcrumb — set by ``claim()`` when
        # ``provision_worker`` fails so the CLI can surface it instead
        # of reporting a silent success (#243).
        self.last_provision_error: str | None = None

    def set_session_manager(self, session_manager: object) -> None:
        """Wire up the session manager after construction.

        This supports two-phase init: the service is created first, then
        the session manager (which needs a reference to the service) is
        created and registered back.
        """
        self._session_mgr = session_manager

    def close(self) -> None:
        """Close the database connection."""
        self._flow_cache.clear()
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _sync_transition(self, task: Task, old_status: str, new_status: str) -> None:
        """Fire sync adapter hooks + task-assignment bus for a state transition."""
        if self._sync:
            self._sync.on_transition(task, old_status, new_status)

        # Emit a TaskAssignmentEvent whenever the task's new current node
        # is waiting on a non-user actor. The event bus is consumed by
        # the built-in ``task_assignment_notify`` plugin which resolves
        # the target session by naming convention and sends a short
        # imperative ping — see ``docs/`` and issue #244.
        #
        # Best-effort: bus dispatch never bubbles exceptions, and any
        # lookup error here is swallowed so the transition always
        # commits cleanly.
        try:
            if not task.flow_template_id:
                return
            flow = self._load_flow_from_db(
                task.flow_template_id, task.flow_template_version,
            )
            # Task nodes don't live on the task until claim() wires
            # ``current_node_id``. For a queued task (and similarly for
            # a freshly-queued task with no active execution), the
            # *implicit* current node is the flow's start node — that's
            # where the next actor will pick up. The spec treats the
            # queued→worker handoff as a first-class assignment event,
            # so we fall back to ``flow.start_node`` when the task has
            # no explicit current node set.
            node_id = task.current_node_id or flow.start_node
            if not node_id:
                return
            node = flow.nodes.get(node_id)
            if node is None:
                return
            from pollypm.work.task_assignment import (
                build_event_from_task,
                dispatch as _dispatch_task_assignment,
            )
            # Inject the effective current_node so build_event sees it
            # on tasks that haven't yet been claimed.
            effective_task = task
            if task.current_node_id is None:
                # Shallow copy avoids mutating the caller's dataclass.
                from dataclasses import replace
                effective_task = replace(task, current_node_id=node_id)
            # #279: carry the current node's visit counter so the
            # notifier's dedupe treats a reject-bounce (which opens a
            # fresh execution row at a higher visit) as a new ping
            # opportunity instead of suppressing it inside the 30-min
            # window keyed on (session, task).
            try:
                execution_version = self.current_node_visit(
                    effective_task.project,
                    effective_task.task_number,
                    node_id,
                )
            except Exception:  # noqa: BLE001
                execution_version = 0
            event = build_event_from_task(
                effective_task,
                node,
                transitioned_by=task.assignee or "system",
                execution_version=execution_version,
            )
            if event is not None:
                _dispatch_task_assignment(event)
        except Exception:  # noqa: BLE001
            logger.debug(
                "task_assignment dispatch failed for %s", task.task_id,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Internal: flow template persistence
    # ------------------------------------------------------------------

    @staticmethod
    def _flow_content_hash(template: FlowTemplate) -> str:
        """Compute a stable content hash over the flow's structural fields.

        Used to detect YAML changes. We intentionally ignore the ``version``
        and ``is_current`` fields so that re-saving the same content with
        a different version number doesn't count as a change.
        """
        payload = {
            "description": template.description,
            "roles": template.roles,
            "start_node": template.start_node,
            "nodes": {
                nid: {
                    "name": n.name,
                    "type": n.type.value,
                    "actor_type": n.actor_type.value if n.actor_type else None,
                    "actor_role": n.actor_role,
                    "agent_name": n.agent_name,
                    "next_node_id": n.next_node_id,
                    "reject_node_id": n.reject_node_id,
                    "gates": n.gates,
                }
                for nid, n in template.nodes.items()
            },
        }
        serialised = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialised.encode("utf-8")).hexdigest()

    def _insert_flow_version(
        self, template: FlowTemplate, version: int,
    ) -> None:
        """Persist ``template`` at ``version`` in work_flow_templates/nodes."""
        self._invalidate_flow_cache(name=template.name, version=version)
        now = _now()
        self._conn.execute(
            "INSERT INTO work_flow_templates "
            "(name, version, description, roles, start_node, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                template.name,
                version,
                template.description,
                json.dumps(template.roles),
                template.start_node,
                now,
            ),
        )
        for node_id, node in template.nodes.items():
            self._conn.execute(
                "INSERT INTO work_flow_nodes "
                "(flow_template_name, flow_template_version, node_id, name, "
                "type, actor_type, actor_role, agent_name, "
                "next_node_id, reject_node_id, gates) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    template.name,
                    version,
                    node_id,
                    node.name,
                    node.type.value,
                    node.actor_type.value if node.actor_type else None,
                    node.actor_role,
                    node.agent_name,
                    node.next_node_id,
                    node.reject_node_id,
                    json.dumps(node.gates),
                ),
            )

    def _invalidate_flow_cache(
        self,
        *,
        name: str | None = None,
        version: int | None = None,
    ) -> None:
        if name is None:
            self._flow_cache.clear()
            return
        if version is not None:
            self._flow_cache.pop((name, version), None)
            return
        stale = [key for key in self._flow_cache if key[0] == name]
        for key in stale:
            self._flow_cache.pop(key, None)

    def _ensure_flow_in_db(self, name: str) -> FlowTemplate:
        """Load a flow via the engine and persist it, bumping version on change.

        - If no row exists for this flow name, persist at version 1 (or the
          template's stated version).
        - If the latest stored row matches the current YAML content hash,
          reuse that version.
        - If the latest stored row differs from the current YAML, INSERT a
          new row at max(version)+1, keeping older versions intact so
          in-flight tasks that reference them still execute the old graph.

        Returns a FlowTemplate whose ``version`` is the one new tasks
        should be pinned to.
        """
        template = resolve_flow(name, self._project_path)
        current_hash = self._flow_content_hash(template)

        # Look up the latest stored version for this flow.
        latest = self._conn.execute(
            "SELECT version FROM work_flow_templates "
            "WHERE name = ? ORDER BY version DESC LIMIT 1",
            (template.name,),
        ).fetchone()

        if latest is None:
            # First time we've seen this flow. Persist at the stated version.
            self._insert_flow_version(template, template.version)
            self._conn.commit()
            return template

        latest_version = int(latest["version"])

        # Compare stored content hash at latest version to the current YAML.
        stored = self._load_flow_from_db(template.name, latest_version)
        stored_hash = self._flow_content_hash(stored)

        if stored_hash == current_hash:
            # Unchanged — reuse the latest version.
            # Return a template object whose ``version`` reflects the stored
            # row so new tasks get pinned correctly.
            if template.version != latest_version:
                template = FlowTemplate(
                    name=template.name,
                    description=template.description,
                    roles=template.roles,
                    nodes=template.nodes,
                    start_node=template.start_node,
                    version=latest_version,
                    is_current=True,
                )
            return template

        # YAML differs — bump to a new version row.
        new_version = latest_version + 1
        self._insert_flow_version(template, new_version)
        self._conn.commit()

        return FlowTemplate(
            name=template.name,
            description=template.description,
            roles=template.roles,
            nodes=template.nodes,
            start_node=template.start_node,
            version=new_version,
            is_current=True,
        )

    def _load_flow_from_db(self, name: str, version: int) -> FlowTemplate:
        """Load a flow template from the database."""
        cache_key = (name, version)
        cached = self._flow_cache.get(cache_key)
        if cached is not None:
            return cached
        row = self._conn.execute(
            "SELECT * FROM work_flow_templates WHERE name = ? AND version = ?",
            (name, version),
        ).fetchone()
        if row is None:
            # Fall back to engine resolution
            flow = resolve_flow(name, self._project_path)
            self._flow_cache[cache_key] = flow
            return flow

        roles = json.loads(row["roles"])
        nodes: dict[str, FlowNode] = {}
        node_rows = self._conn.execute(
            "SELECT * FROM work_flow_nodes "
            "WHERE flow_template_name = ? AND flow_template_version = ?",
            (name, version),
        ).fetchall()
        for nr in node_rows:
            # agent_name column may not exist on very old DBs — guard with
            # a dict-style fallback.
            try:
                agent_name = nr["agent_name"]
            except (IndexError, KeyError):
                agent_name = None
            nodes[nr["node_id"]] = FlowNode(
                name=nr["name"],
                type=NodeType(nr["type"]),
                actor_type=ActorType(nr["actor_type"]) if nr["actor_type"] else None,
                actor_role=nr["actor_role"],
                agent_name=agent_name,
                next_node_id=nr["next_node_id"],
                reject_node_id=nr["reject_node_id"],
                gates=json.loads(nr["gates"]),
            )

        flow = FlowTemplate(
            name=row["name"],
            description=row["description"],
            roles=roles,
            nodes=nodes,
            start_node=row["start_node"],
            version=row["version"],
            is_current=bool(row["is_current"]),
        )
        self._flow_cache[cache_key] = flow
        return flow

    # ------------------------------------------------------------------
    # Internal: task reconstruction
    # ------------------------------------------------------------------

    def _row_to_task(
        self,
        row: sqlite3.Row,
        token_sums: dict[tuple[str, int], tuple[int, int, int]] | None = None,
    ) -> Task:
        """Build a Task dataclass from a database row.

        ``token_sums`` is an optional pre-computed map keyed by
        ``(project, task_number)`` holding
        ``(total_input_tokens, total_output_tokens, session_count)`` so
        callers issuing batch reads (e.g. ``list_tasks``) can avoid the
        N+1 query hit. When ``None``, a single per-task aggregate query
        is issued against ``work_sessions`` (#86).
        """
        project = row["project"]
        task_number = row["task_number"]

        transitions = self._load_transitions(project, task_number)
        executions = self._load_executions(project, task_number)
        rels = self._load_relationships(project, task_number)

        if token_sums is not None:
            tokens_in, tokens_out, sess_count = token_sums.get(
                (project, task_number), (0, 0, 0)
            )
        else:
            tokens_in, tokens_out, sess_count = self._load_task_token_sum(
                project, task_number
            )

        task = Task(
            project=project,
            task_number=task_number,
            title=row["title"],
            type=TaskType(row["type"]),
            labels=json.loads(row["labels"]),
            work_status=WorkStatus(row["work_status"]),
            flow_template_id=row["flow_template_id"],
            flow_template_version=row["flow_template_version"],
            current_node_id=row["current_node_id"],
            assignee=row["assignee"],
            priority=Priority(row["priority"]),
            requires_human_review=bool(row["requires_human_review"]),
            description=row["description"],
            acceptance_criteria=row["acceptance_criteria"],
            constraints=row["constraints"],
            relevant_files=json.loads(row["relevant_files"]),
            parent_project=row["parent_project"],
            parent_task_number=row["parent_task_number"],
            blocks=rels.get("blocks", []),
            blocked_by=rels.get("blocked_by", []),
            relates_to=rels.get("relates_to", []),
            children=rels.get("children", []),
            supersedes_project=row["supersedes_project"],
            supersedes_task_number=row["supersedes_task_number"],
            superseded_by_project=rels.get("superseded_by_project"),
            superseded_by_task_number=rels.get("superseded_by_task_number"),
            roles=json.loads(row["roles"]),
            external_refs=json.loads(row["external_refs"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            created_by=row["created_by"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
            transitions=transitions,
            executions=executions,
            total_input_tokens=tokens_in,
            total_output_tokens=tokens_out,
            session_count=sess_count,
        )
        return task

    # ------------------------------------------------------------------
    # Per-task token aggregation (#86)
    # ------------------------------------------------------------------
    #
    # Token counts live on the ``work_sessions`` rows (populated by the
    # session teardown path — see #150). Per-task visibility is a SUM
    # over every session row bound to the task. The single-row helper is
    # cheap enough for ``get()`` / ``_row_to_task`` lookups; the batch
    # helper avoids N+1 in ``list_tasks`` when large result sets are
    # involved.

    def _load_task_token_sum(
        self, project: str, task_number: int
    ) -> tuple[int, int, int]:
        """Return ``(tokens_in, tokens_out, session_count)`` for one task."""
        row = self._conn.execute(
            "SELECT "
            "COALESCE(SUM(total_input_tokens), 0) AS tin, "
            "COALESCE(SUM(total_output_tokens), 0) AS tout, "
            "COUNT(*) AS cnt "
            "FROM work_sessions "
            "WHERE task_project = ? AND task_number = ?",
            (project, task_number),
        ).fetchone()
        if row is None:
            return (0, 0, 0)
        return (int(row["tin"] or 0), int(row["tout"] or 0), int(row["cnt"] or 0))

    def _load_task_token_sums_bulk(
        self,
        project: str | None = None,
    ) -> dict[tuple[str, int], tuple[int, int, int]]:
        """Return aggregated tokens for every task, optionally filtered.

        Keyed by ``(project, task_number)``. Returns zero-sum tuples
        only for tasks that have at least one session row — callers
        should default to ``(0, 0, 0)`` for misses.
        """
        clauses: list[str] = []
        params: list[object] = []
        if project is not None:
            clauses.append("task_project = ?")
            params.append(project)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT task_project, task_number, "
            "COALESCE(SUM(total_input_tokens), 0) AS tin, "
            "COALESCE(SUM(total_output_tokens), 0) AS tout, "
            "COUNT(*) AS cnt "
            f"FROM work_sessions{where} "
            "GROUP BY task_project, task_number"
        )
        rows = self._conn.execute(sql, tuple(params)).fetchall()
        out: dict[tuple[str, int], tuple[int, int, int]] = {}
        for r in rows:
            key = (r["task_project"], int(r["task_number"]))
            out[key] = (
                int(r["tin"] or 0),
                int(r["tout"] or 0),
                int(r["cnt"] or 0),
            )
        return out

    def _load_relationships(
        self, project: str, task_number: int
    ) -> dict:
        """Load dependency relationships for a task from work_task_dependencies."""
        rows = self._conn.execute(
            "SELECT "
            "from_project, from_task_number, "
            "to_project, to_task_number, "
            "kind "
            "FROM work_task_dependencies "
            "WHERE (from_project = ? AND from_task_number = ?) "
            "   OR (to_project = ? AND to_task_number = ?)",
            (project, task_number, project, task_number),
        ).fetchall()

        rels: dict = {
            "blocks": [],
            "blocked_by": [],
            "relates_to": [],
            "children": [],
            "superseded_by_project": None,
            "superseded_by_task_number": None,
        }

        for r in rows:
            kind = r["kind"]
            is_outgoing = (
                r["from_project"] == project and r["from_task_number"] == task_number
            )
            is_incoming = (
                r["to_project"] == project and r["to_task_number"] == task_number
            )
            if is_outgoing:
                target = (r["to_project"], r["to_task_number"])
                if kind == LinkKind.BLOCKS.value:
                    rels["blocks"].append(target)
                elif kind == LinkKind.RELATES_TO.value:
                    rels["relates_to"].append(target)
                elif kind == LinkKind.PARENT.value:
                    rels["children"].append(target)
                elif kind == LinkKind.SUPERSEDES.value:
                    # outgoing supersedes: this task supersedes target
                    pass  # stored in supersedes_project/supersedes_task_number columns
            if is_incoming:
                source = (r["from_project"], r["from_task_number"])
                if kind == LinkKind.BLOCKS.value:
                    rels["blocked_by"].append(source)
                elif kind == LinkKind.RELATES_TO.value:
                    # relates_to is bidirectional
                    if source not in rels["relates_to"]:
                        rels["relates_to"].append(source)
                elif kind == LinkKind.PARENT.value:
                    # incoming parent: source is parent of this task
                    # update parent fields (override column-based values)
                    pass  # parent is set via from_id=parent, to_id=child
                elif kind == LinkKind.SUPERSEDES.value:
                    # incoming supersedes: source supersedes this task
                    rels["superseded_by_project"] = r["from_project"]
                    rels["superseded_by_task_number"] = r["from_task_number"]

        return rels

    def _load_transitions(self, project: str, task_number: int) -> list[Transition]:
        rows = self._conn.execute(
            "SELECT * FROM work_transitions "
            "WHERE task_project = ? AND task_number = ? ORDER BY id",
            (project, task_number),
        ).fetchall()
        return [
            Transition(
                from_state=r["from_state"],
                to_state=r["to_state"],
                actor=r["actor"],
                timestamp=datetime.fromisoformat(r["created_at"]),
                reason=r["reason"],
            )
            for r in rows
        ]

    def _load_executions(
        self, project: str, task_number: int
    ) -> list[FlowNodeExecution]:
        rows = self._conn.execute(
            "SELECT * FROM work_node_executions "
            "WHERE task_project = ? AND task_number = ? ORDER BY id",
            (project, task_number),
        ).fetchall()
        result: list[FlowNodeExecution] = []
        for r in rows:
            wo_raw = r["work_output"]
            work_output: WorkOutput | None = None
            if wo_raw:
                wo_dict = json.loads(wo_raw)
                work_output = WorkOutput(
                    type=OutputType(wo_dict["type"]),
                    summary=wo_dict["summary"],
                    artifacts=[
                        Artifact(
                            kind=ArtifactKind(a["kind"]),
                            description=a.get("description", ""),
                            ref=a.get("ref"),
                            path=a.get("path"),
                            external_ref=a.get("external_ref"),
                        )
                        for a in wo_dict.get("artifacts", [])
                    ],
                )
            result.append(
                FlowNodeExecution(
                    task_id=f"{r['task_project']}/{r['task_number']}",
                    node_id=r["node_id"],
                    visit=r["visit"],
                    status=ExecutionStatus(r["status"]),
                    work_output=work_output,
                    decision=(
                        Decision(r["decision"]) if r["decision"] else None
                    ),
                    decision_reason=r["decision_reason"],
                    started_at=(
                        datetime.fromisoformat(r["started_at"])
                        if r["started_at"]
                        else None
                    ),
                    completed_at=(
                        datetime.fromisoformat(r["completed_at"])
                        if r["completed_at"]
                        else None
                    ),
                )
            )
        return result

    def _record_transition(
        self,
        project: str,
        task_number: int,
        from_state: str,
        to_state: str,
        actor: str,
        reason: str | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO work_transitions "
            "(task_project, task_number, from_state, to_state, actor, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (project, task_number, from_state, to_state, actor, reason, _now()),
        )

    @staticmethod
    def _gate_skip_reason(results: list[GateResult]) -> str | None:
        """Build a reason string from skipped gate failures."""
        failures = [r for r in results if not r.passed]
        if not failures:
            return None
        parts = [f"[skip-gates] {r.gate_name}: {r.reason}" for r in failures]
        return "; ".join(parts)

    def _gate_kwargs(self) -> dict[str, object]:
        """Build common kwargs for gate evaluation."""
        kwargs: dict[str, object] = {"get_task": self.get}
        if self._project_path is not None:
            kwargs["project_root"] = self._project_path
        return kwargs

    # ------------------------------------------------------------------
    # Owner derivation
    # ------------------------------------------------------------------

    def _resolve_node_assignee(
        self, task: Task, node: FlowNode | None
    ) -> str | None:
        """Return the effective owner for ``node`` using ``task`` roles."""
        if node is None:
            return task.assignee

        if node.actor_type == ActorType.ROLE:
            return task.roles.get(node.actor_role or "", task.assignee)
        if node.actor_type == ActorType.HUMAN:
            return "human"
        if node.actor_type == ActorType.PROJECT_MANAGER:
            return "project_manager"
        if node.actor_type == ActorType.AGENT:
            # Fall back to the prior assignee only for legacy rows that
            # predate stricter flow validation.
            return node.agent_name or task.assignee
        return task.assignee

    def derive_owner(self, task: Task) -> str | None:
        """Derive the current owner from the flow node's actor configuration."""
        if task.current_node_id is None:
            if task.work_status == WorkStatus.DRAFT:
                return "project_manager"
            return None

        try:
            flow = self._load_flow_from_db(
                task.flow_template_id,
                task.flow_template_version,
            )
        except Exception:
            return task.assignee

        node = flow.nodes.get(task.current_node_id)
        if node is None:
            return task.assignee

        return self._resolve_node_assignee(task, node)

    # ------------------------------------------------------------------
    # Task CRUD
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
    ) -> Task:
        """Create a task in draft state."""
        return create_task(
            self,
            title=title,
            description=description,
            type=type,
            project=project,
            flow_template=flow_template,
            roles=roles,
            priority=priority,
            created_by=created_by,
            acceptance_criteria=acceptance_criteria,
            constraints=constraints,
            relevant_files=relevant_files,
            labels=labels,
            requires_human_review=requires_human_review,
        )

    def set_external_ref(self, task_id: str, key: str, value: str) -> None:
        """Persist one external reference on a task."""
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
        self._conn.execute(
            "UPDATE work_tasks SET external_refs = ?, updated_at = ? "
            "WHERE project = ? AND task_number = ?",
            (json.dumps(refs), _now(), project, task_number),
        )
        self._conn.commit()

    def get(self, task_id: str) -> Task:
        """Read a task by its ``project/number`` identifier."""
        return get_task(self, task_id)

    def list_tasks(
        self,
        *,
        work_status: str | None = None,
        owner: str | None = None,
        project: str | None = None,
        assignee: str | None = None,
        blocked: bool | None = None,
        type: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Task]:
        """Query tasks with optional filters."""
        return read_tasks(
            self,
            work_status=work_status,
            owner=owner,
            project=project,
            assignee=assignee,
            blocked=blocked,
            type=type,
            limit=limit,
            offset=offset,
        )

    def update(self, task_id: str, **fields: object) -> Task:
        """Update mutable fields on a task."""
        return update_task(self, task_id, **fields)

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def queue(self, task_id: str, actor: str, skip_gates: bool = False) -> Task:
        """Move from draft to queued."""
        task = self.get(task_id)

        if task.work_status != WorkStatus.DRAFT:
            raise InvalidTransitionError(
                f"Cannot queue task in '{task.work_status.value}' state. "
                f"Task must be in 'draft' state."
            )

        if task.requires_human_review and not skip_gates:
            # Full inbox-backed approval is a follow-up; for now, callers
            # with an out-of-band approval signal must opt in explicitly
            # via skip_gates=True so the feature is at least usable.
            raise InvalidTransitionError(
                "Task requires human review before queueing. "
                "Pass skip_gates=True to bypass this check once approval "
                "has been obtained out-of-band (inbox integration pending)."
            )

        # Gate: has_description
        gate_results = evaluate_gates(
            task, ["has_description"], self._gate_registry,
            **self._gate_kwargs(),
        )
        if not skip_gates and has_hard_failure(gate_results):
            failing = [r for r in gate_results if not r.passed]
            raise ValidationError(
                f"Cannot queue task: gate failed — {failing[0].reason}"
            )

        now = _now()
        gate_reason = self._gate_skip_reason(gate_results) if skip_gates else None
        self._record_transition(
            task.project,
            task.task_number,
            WorkStatus.DRAFT.value,
            WorkStatus.QUEUED.value,
            actor,
            reason=gate_reason,
        )
        self._conn.execute(
            "UPDATE work_tasks SET work_status = ?, updated_at = ? "
            "WHERE project = ? AND task_number = ?",
            (WorkStatus.QUEUED.value, now, task.project, task.task_number),
        )
        self._conn.commit()
        # Auto-block if there are unresolved blockers
        self._maybe_block(task_id)
        task = self.get(task_id)
        self._sync_transition(task, WorkStatus.DRAFT.value, task.work_status.value)
        return task

    def claim(self, task_id: str, actor: str, skip_gates: bool = False) -> Task:
        """Atomically claim a queued task."""
        task = self.get(task_id)

        if task.work_status != WorkStatus.QUEUED:
            # Already-claimed is a distinct, common case — surface the
            # current claimant and how to proceed.
            if task.work_status == WorkStatus.IN_PROGRESS:
                claimant = task.assignee or "another actor"
                raise InvalidTransitionError(
                    f"Task {task_id} is already claimed by '{claimant}'.\n"
                    f"\n"
                    f"Why: the task is in 'in_progress' and assigned. A "
                    f"second claim would orphan the first worker's "
                    f"session.\n"
                    f"\n"
                    f"Fix: use `pm task get {task_id}` to see the current "
                    f"state. If the existing claim is stale (worker "
                    f"session dead), hold and resume:\n"
                    f"    pm task hold {task_id} --reason 'stale claim'\n"
                    f"    pm task resume {task_id}\n"
                    f"Otherwise, find an unclaimed task with `pm task next`."
                )
            raise InvalidTransitionError(
                f"Cannot claim task in '{task.work_status.value}' state.\n"
                f"\n"
                f"Why: only tasks in 'queued' state can be claimed.\n"
                f"\n"
                f"Fix: if the task is 'draft', run "
                f"`pm task queue {task_id}` first. If it's 'done' or "
                f"'cancelled', find another task with `pm task next`."
            )

        # blocked check (for now always False)
        if task.blocked:
            raise InvalidTransitionError(
                f"Cannot claim task {task_id}: it is blocked by another "
                f"task.\n"
                f"\n"
                f"Why: blocking tasks must reach a terminal state before "
                f"dependents can start.\n"
                f"\n"
                f"Fix: run `pm task get {task_id}` to see the blockers, "
                f"then work on those first (or unblock with "
                f"`pm task unlink`)."
            )

        flow = self._load_flow_from_db(
            task.flow_template_id, task.flow_template_version,
        )
        start_node = flow.start_node
        start_node_cfg = flow.nodes.get(start_node)
        assignee = self._resolve_node_assignee(task, start_node_cfg) or actor
        now = _now()

        try:
            # Atomic: update status, assignee, current_node, and create execution
            self._conn.execute(
                "UPDATE work_tasks SET work_status = ?, assignee = ?, "
                "current_node_id = ?, updated_at = ? "
                "WHERE project = ? AND task_number = ?",
                (
                    WorkStatus.IN_PROGRESS.value,
                    assignee,
                    start_node,
                    now,
                    task.project,
                    task.task_number,
                ),
            )
            self._conn.execute(
                "INSERT INTO work_node_executions "
                "(task_project, task_number, node_id, visit, status, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    task.project,
                    task.task_number,
                    start_node,
                    1,
                    ExecutionStatus.ACTIVE.value,
                    now,
                ),
            )
            self._record_transition(
                task.project,
                task.task_number,
                WorkStatus.QUEUED.value,
                WorkStatus.IN_PROGRESS.value,
                actor,
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

        result = self.get(task_id)
        self._sync_transition(result, WorkStatus.QUEUED.value, result.work_status.value)
        # Reset any stale provision-error breadcrumb before attempting
        # provisioning for this claim.
        self.last_provision_error = None
        # Provision a per-task worker session with worktree
        if self._session_mgr is not None:
            try:
                self._session_mgr.provision_worker(task_id, actor)
            except Exception as exc:  # noqa: BLE001
                # Best-effort — task is claimed regardless. Record the
                # reason on the service so ``pm task claim`` can surface
                # it (instead of a silent success, which was the #243
                # E2E blocker).
                self.last_provision_error = str(exc)
                logger.warning(
                    "provision_worker failed for %s (actor=%s): %s",
                    task_id, actor, exc,
                )
        return result

    def cancel(self, task_id: str, actor: str, reason: str) -> Task:
        """Move any non-terminal task to cancelled."""
        task = self.get(task_id)

        if task.work_status in TERMINAL_STATUSES:
            raise InvalidTransitionError(
                f"Cannot cancel task in terminal state '{task.work_status.value}'."
            )

        now = _now()
        self._record_transition(
            task.project,
            task.task_number,
            task.work_status.value,
            WorkStatus.CANCELLED.value,
            actor,
            reason,
        )
        self._conn.execute(
            "UPDATE work_tasks SET work_status = ?, updated_at = ? "
            "WHERE project = ? AND task_number = ?",
            (WorkStatus.CANCELLED.value, now, task.project, task.task_number),
        )
        self._conn.commit()
        self._on_cancelled(task_id)
        # Tear down the per-task worker session
        if self._session_mgr is not None:
            try:
                self._session_mgr.teardown_worker(task_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "teardown_worker on cancel failed for %s: %s",
                    task_id, exc,
                )
        result = self.get(task_id)
        self._sync_transition(result, task.work_status.value, WorkStatus.CANCELLED.value)
        return result

    def hold(self, task_id: str, actor: str, reason: str | None = None) -> Task:
        """Move in_progress or queued to on_hold."""
        task = self.get(task_id)

        if task.work_status not in (WorkStatus.IN_PROGRESS, WorkStatus.QUEUED):
            raise InvalidTransitionError(
                f"Cannot hold task in '{task.work_status.value}' state. "
                f"Task must be in 'in_progress' or 'queued' state."
            )

        now = _now()
        self._record_transition(
            task.project,
            task.task_number,
            task.work_status.value,
            WorkStatus.ON_HOLD.value,
            actor,
            reason,
        )
        self._conn.execute(
            "UPDATE work_tasks SET work_status = ?, updated_at = ? "
            "WHERE project = ? AND task_number = ?",
            (WorkStatus.ON_HOLD.value, now, task.project, task.task_number),
        )
        self._conn.commit()
        result = self.get(task_id)
        self._sync_transition(result, task.work_status.value, WorkStatus.ON_HOLD.value)
        return result

    def resume(self, task_id: str, actor: str) -> Task:
        """Move on_hold back to queued (or in_progress if a flow node is active)."""
        task = self.get(task_id)

        if task.work_status != WorkStatus.ON_HOLD:
            raise InvalidTransitionError(
                f"Cannot resume task in '{task.work_status.value}' state. "
                f"Task must be in 'on_hold' state."
            )

        # If there's an active flow execution, resume to in_progress
        # (the task was held mid-work). Otherwise resume to queued.
        has_active_execution = False
        if task.current_node_id:
            row = self._conn.execute(
                "SELECT 1 FROM work_node_executions "
                "WHERE task_project = ? AND task_number = ? "
                "AND node_id = ? AND status = ?",
                (task.project, task.task_number, task.current_node_id,
                 ExecutionStatus.ACTIVE.value),
            ).fetchone()
            has_active_execution = row is not None

        target_status = WorkStatus.IN_PROGRESS if has_active_execution else WorkStatus.QUEUED

        now = _now()
        self._record_transition(
            task.project,
            task.task_number,
            WorkStatus.ON_HOLD.value,
            target_status.value,
            actor,
        )
        self._conn.execute(
            "UPDATE work_tasks SET work_status = ?, updated_at = ? "
            "WHERE project = ? AND task_number = ?",
            (target_status.value, now, task.project, task.task_number),
        )
        self._conn.commit()
        result = self.get(task_id)
        self._sync_transition(result, WorkStatus.ON_HOLD.value, target_status.value)
        return result

    # ------------------------------------------------------------------
    # Flow progression
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_work_output(output: WorkOutput) -> None:
        """Validate a WorkOutput has required fields and at least one artifact."""
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
        work_output: WorkOutput | dict | None,
    ) -> WorkOutput | None:
        """Convert a dict to WorkOutput if needed."""
        if work_output is None:
            return None
        if isinstance(work_output, dict):
            artifacts = [
                Artifact(
                    kind=(
                        ArtifactKind(a["kind"])
                        if isinstance(a.get("kind"), str)
                        else a.get("kind", ArtifactKind.NOTE)
                    ),
                    description=a.get("description", ""),
                    ref=a.get("ref"),
                    path=a.get("path"),
                    external_ref=a.get("external_ref"),
                )
                for a in work_output.get("artifacts", [])
            ]
            out_type = work_output.get("type", OutputType.CODE_CHANGE)
            if isinstance(out_type, str):
                out_type = OutputType(out_type)
            return WorkOutput(
                type=out_type,
                summary=work_output.get("summary", ""),
                artifacts=artifacts,
            )
        return work_output

    @staticmethod
    def _serialize_work_output(output: WorkOutput) -> str:
        """Serialize a WorkOutput to a JSON string for DB storage."""
        return json.dumps(
            {
                "type": (
                    output.type.value
                    if isinstance(output.type, OutputType)
                    else output.type
                ),
                "summary": output.summary,
                "artifacts": [
                    {
                        "kind": (
                            a.kind.value
                            if isinstance(a.kind, ArtifactKind)
                            else a.kind
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

    def _get_current_flow_node(
        self, task: Task
    ) -> tuple[FlowTemplate, FlowNode]:
        """Load the flow and return the current node."""
        flow = self._load_flow_from_db(
            task.flow_template_id, task.flow_template_version,
        )
        if task.current_node_id is None:
            raise InvalidTransitionError("Task has no current flow node.")
        node = flow.nodes.get(task.current_node_id)
        if node is None:
            raise InvalidTransitionError(
                f"Current node '{task.current_node_id}' not found in flow "
                f"'{task.flow_template_id}'."
            )
        return flow, node

    # Actors that are always treated as human for actor_type=HUMAN nodes.
    _HUMAN_ACTOR_NAMES = frozenset({"human", "user", "sam"})

    @staticmethod
    def _ordered_names(*names: str | None) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for name in names:
            if not name or name in seen:
                continue
            ordered.append(name)
            seen.add(name)
        return ordered

    def _validate_actor_role(
        self, task: Task, node: FlowNode, actor: str
    ) -> None:
        """Validate that actor matches the node's expected role."""
        if node.actor_type == ActorType.HUMAN:
            # Accept well-known human names plus the assigned reviewer role value
            reviewer = None
            if node.actor_role:
                reviewer = task.roles.get(node.actor_role)
            allowed = self._ordered_names(
                reviewer, *sorted(self._HUMAN_ACTOR_NAMES),
            )
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
                # Also accept the role name itself (e.g. "worker" matches role "worker")
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

    def _next_visit(
        self, project: str, task_number: int, node_id: str
    ) -> int:
        """Return the next visit number for a node execution."""
        return read_next_visit(self, project, task_number, node_id)

    def current_node_visit(
        self, project: str, task_number: int, node_id: str
    ) -> int:
        """Return the visit number of the current execution at ``node_id``.

        Used by the task-assignment notifier (#279) to key its dedupe on
        ``(session, task, execution_version)`` so a reject-bounce back to
        an earlier node unlocks the retry ping instead of being
        suppressed by the 30-minute window that originally pinged the
        worker at ``visit=1``.

        Returns ``0`` when the task has no recorded execution for the
        node yet — a queued task whose start node hasn't been entered
        carries an implicit "zeroth visit", and downstream dedupe
        treats that as one identity bucket (matching the pre-#279
        column default).
        """
        return read_current_node_visit(self, project, task_number, node_id)

    def _advance_to_node(
        self,
        task: Task,
        flow: FlowTemplate,
        next_node_id: str | None,
        actor: str,
        from_status: WorkStatus,
    ) -> None:
        """Advance the task to the next node, updating status and execution."""
        advance_to_node(self, task, flow, next_node_id, actor, from_status)

    def node_done(
        self,
        task_id: str,
        actor: str,
        work_output: WorkOutput | dict | None = None,
        skip_gates: bool = False,
    ) -> Task:
        """Signal that the current work node is complete."""
        task = self.get(task_id)

        if task.work_status != WorkStatus.IN_PROGRESS:
            raise InvalidTransitionError(
                f"Cannot complete node on task in "
                f"'{task.work_status.value}' state. "
                f"Task must be in 'in_progress' state."
            )

        flow, node = self._get_current_flow_node(task)

        if node.type != NodeType.WORK:
            raise InvalidTransitionError(
                f"Current node '{task.current_node_id}' is not a work node "
                f"(type: {node.type.value})."
            )

        self._validate_actor_role(task, node, actor)

        # Evaluate gates on the current node
        if node.gates:
            gate_results = evaluate_gates(
                task, node.gates, self._gate_registry,
                **self._gate_kwargs(),
            )
            if not skip_gates and has_hard_failure(gate_results):
                failing = [r for r in gate_results if not r.passed and r.gate_type == "hard"]
                reasons = "; ".join(r.reason for r in failing)
                raise ValidationError(
                    f"Gate check failed on node '{node.name}': {reasons}"
                )

        # Coerce and validate work output
        work_output = self._coerce_work_output(work_output)
        if work_output is None:
            raise ValidationError(
                "pm task done requires a --output payload describing "
                "what you built.\n"
                "\n"
                "Why: the reviewer cannot evaluate the handoff without "
                "a summary and at least one artifact.\n"
                "\n"
                "Fix: pass --output with a JSON object, e.g.:\n"
                "    pm task done <id> --output '{\n"
                "      \"type\": \"code_change\",\n"
                "      \"summary\": \"<what you built>\",\n"
                "      \"artifacts\": [{\"kind\": \"commit\", \"description\": "
                "\"impl\", \"ref\": \"HEAD\"}]\n"
                "    }'"
            )
        self._validate_work_output(work_output)

        now = _now()
        wo_json = self._serialize_work_output(work_output)

        try:
            # Complete current execution
            self._conn.execute(
                "UPDATE work_node_executions SET status = ?, "
                "work_output = ?, completed_at = ? "
                "WHERE task_project = ? AND task_number = ? "
                "AND node_id = ? AND status = ?",
                (
                    ExecutionStatus.COMPLETED.value,
                    wo_json,
                    now,
                    task.project,
                    task.task_number,
                    task.current_node_id,
                    ExecutionStatus.ACTIVE.value,
                ),
            )

            # Advance to next node
            self._advance_to_node(
                task,
                flow,
                node.next_node_id,
                actor,
                WorkStatus.IN_PROGRESS,
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

        result = self.get(task_id)
        if result.work_status == WorkStatus.DONE:
            self._check_auto_unblock(task_id)
            if self._session_mgr is not None:
                try:
                    self._session_mgr.teardown_worker(task_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "teardown_worker on done failed for %s: %s",
                        task_id, exc,
                    )
            self._on_task_done(task_id, actor)
        self._sync_transition(result, task.work_status.value, result.work_status.value)
        return result

    def approve(
        self,
        task_id: str,
        actor: str,
        reason: str | None = None,
        skip_gates: bool = False,
    ) -> Task:
        """Approve at a review node."""
        task = self.get(task_id)

        if task.work_status != WorkStatus.REVIEW:
            current = task.work_status.value
            # Tailor the fix hint to the specific state so workers who
            # accidentally try to approve their own draft get routed to
            # the right command.
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
                    f"Fix: this task is waiting for a worker. Approval "
                    f"comes after a worker marks it done. Claim + build "
                    f"first, or wait for a worker to pick it up."
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

        flow, node = self._get_current_flow_node(task)

        if node.type != NodeType.REVIEW:
            raise InvalidTransitionError(
                f"Current node '{task.current_node_id}' is not a review "
                f"node (type: {node.type.value})."
            )

        self._validate_actor_role(task, node, actor)

        # Evaluate gates on the current node
        if node.gates:
            gate_results = evaluate_gates(
                task, node.gates, self._gate_registry,
                **self._gate_kwargs(),
            )
            if not skip_gates and has_hard_failure(gate_results):
                failing = [r for r in gate_results if not r.passed and r.gate_type == "hard"]
                reasons = "; ".join(r.reason for r in failing)
                raise ValidationError(
                    f"Gate check failed on node '{node.name}': {reasons}"
                )

        now = _now()

        try:
            # Complete current execution with approval
            self._conn.execute(
                "UPDATE work_node_executions SET status = ?, "
                "decision = ?, decision_reason = ?, completed_at = ? "
                "WHERE task_project = ? AND task_number = ? "
                "AND node_id = ? AND status = ?",
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

            # Plan-presence gate (#281): when the architect's user_approval
            # node on a plan_project task is approved, stamp a dedicated
            # ``plan_approved'' context entry. The gate in
            # ``plan_presence.has_acceptable_plan`` reads this timestamp
            # instead of relying on ``plan.md``'s mtime — file mtimes are
            # perturbed by git checkouts, editor saves, and the planner's
            # own stage-8 emit, so they make the gate unstable.
            if (
                task.flow_template_id == "plan_project"
                and task.current_node_id == "user_approval"
            ):
                self._conn.execute(
                    "INSERT INTO work_context_entries "
                    "(task_project, task_number, actor, text, "
                    "created_at, entry_type) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        task.project,
                        task.task_number,
                        actor,
                        "plan approved",
                        now,
                        "plan_approved",
                    ),
                )

            # Advance to next node
            self._advance_to_node(
                task,
                flow,
                node.next_node_id,
                actor,
                WorkStatus.REVIEW,
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

        result = self.get(task_id)
        if result.work_status == WorkStatus.DONE:
            self._check_auto_unblock(task_id)
            # Tear down the per-task worker session
            if self._session_mgr is not None:
                try:
                    self._session_mgr.teardown_worker(task_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "teardown_worker on approve failed for %s: %s",
                        task_id, exc,
                    )
            self._on_task_done(task_id, actor)
        self._sync_transition(result, task.work_status.value, result.work_status.value)
        return result

    def reject(
        self,
        task_id: str,
        actor: str,
        reason: str,
    ) -> Task:
        """Reject at a review node."""
        task = self.get(task_id)

        if task.work_status != WorkStatus.REVIEW:
            raise InvalidTransitionError(
                f"Cannot reject task in '{task.work_status.value}' state. "
                f"Task must be in 'review' state."
            )

        flow, node = self._get_current_flow_node(task)

        if node.type != NodeType.REVIEW:
            raise InvalidTransitionError(
                f"Current node '{task.current_node_id}' is not a review "
                f"node."
            )

        self._validate_actor_role(task, node, actor)

        if not reason or not reason.strip():
            raise ValidationError("Reason is required for rejection.")

        if node.reject_node_id is None:
            raise InvalidTransitionError(
                f"Review node '{task.current_node_id}' has no reject_node "
                f"defined."
            )

        reject_target = flow.nodes.get(node.reject_node_id)
        if reject_target is None:
            raise InvalidTransitionError(
                f"Reject node '{node.reject_node_id}' not found in flow."
            )

        now = _now()

        try:
            # Complete current execution with rejection
            self._conn.execute(
                "UPDATE work_node_executions SET status = ?, "
                "decision = ?, decision_reason = ?, completed_at = ? "
                "WHERE task_project = ? AND task_number = ? "
                "AND node_id = ? AND status = ?",
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

            # Determine visit number for the reject target
            max_visit_row = self._conn.execute(
                "SELECT COALESCE(MAX(visit), 0) AS max_v "
                "FROM work_node_executions "
                "WHERE task_project = ? AND task_number = ? "
                "AND node_id = ?",
                (task.project, task.task_number, node.reject_node_id),
            ).fetchone()
            next_visit = max_visit_row["max_v"] + 1

            # Set status back to in_progress at the reject target
            reject_assignee = self._resolve_node_assignee(task, reject_target)
            self._conn.execute(
                "UPDATE work_tasks SET work_status = ?, assignee = ?, "
                "current_node_id = ?, updated_at = ? "
                "WHERE project = ? AND task_number = ?",
                (
                    WorkStatus.IN_PROGRESS.value,
                    reject_assignee,
                    node.reject_node_id,
                    now,
                    task.project,
                    task.task_number,
                ),
            )

            # Create new execution at the reject target
            self._conn.execute(
                "INSERT INTO work_node_executions "
                "(task_project, task_number, node_id, visit, status, "
                "started_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    task.project,
                    task.task_number,
                    node.reject_node_id,
                    next_visit,
                    ExecutionStatus.ACTIVE.value,
                    now,
                ),
            )

            self._record_transition(
                task.project,
                task.task_number,
                WorkStatus.REVIEW.value,
                WorkStatus.IN_PROGRESS.value,
                actor,
                reason,
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

        result = self.get(task_id)
        self._sync_transition(result, WorkStatus.REVIEW.value, WorkStatus.IN_PROGRESS.value)
        try:
            emit_rejection_feedback(self, task=result, reviewer=actor, reason=reason)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "emit_rejection_feedback failed for %s: %s", task_id, exc,
            )
        # Notify the per-task worker session about the rejection
        if self._session_mgr is not None:
            try:
                self._session_mgr.notify_rejection(task_id, reason)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "notify_rejection failed for %s: %s", task_id, exc,
                )
        return result

    def block(self, task_id: str, actor: str, blocker_task_id: str) -> Task:
        """Mark a task as blocked by another task."""
        task = self.get(task_id)

        if task.work_status not in (
            WorkStatus.IN_PROGRESS,
            WorkStatus.REVIEW,
        ):
            raise InvalidTransitionError(
                f"Cannot block task in '{task.work_status.value}' state. "
                f"Task must be in 'in_progress' or 'review' state."
            )

        # Validate the blocker exists
        self.get(blocker_task_id)

        # Parse the blocker id for the dependency INSERT below.
        blocker_project, blocker_number = _parse_task_id(blocker_task_id)

        # Cycle-check: would this blocks edge create a cycle?
        if self._would_create_cycle(
            blocker_project, blocker_number, task.project, task.task_number,
        ):
            raise ValidationError("circular dependency detected")

        now = _now()
        old_status = task.work_status

        try:
            # Persist the blocks dependency row so auto-unblock /
            # blocked_tasks() / dependents() can find it. Use INSERT OR
            # IGNORE so a pre-existing link is a no-op.
            self._conn.execute(
                "INSERT OR IGNORE INTO work_task_dependencies "
                "(from_project, from_task_number, to_project, to_task_number, "
                "kind, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    blocker_project,
                    blocker_number,
                    task.project,
                    task.task_number,
                    LinkKind.BLOCKS.value,
                    now,
                ),
            )

            self._conn.execute(
                "UPDATE work_tasks SET work_status = ?, updated_at = ? "
                "WHERE project = ? AND task_number = ?",
                (
                    WorkStatus.BLOCKED.value,
                    now,
                    task.project,
                    task.task_number,
                ),
            )

            # Set current execution to blocked
            if task.current_node_id:
                self._conn.execute(
                    "UPDATE work_node_executions SET status = ? "
                    "WHERE task_project = ? AND task_number = ? "
                    "AND node_id = ? AND status = ?",
                    (
                        ExecutionStatus.BLOCKED.value,
                        task.project,
                        task.task_number,
                        task.current_node_id,
                        ExecutionStatus.ACTIVE.value,
                    ),
                )

            self._record_transition(
                task.project,
                task.task_number,
                old_status.value,
                WorkStatus.BLOCKED.value,
                actor,
                f"Blocked by {blocker_task_id}",
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

        result = self.get(task_id)
        self._sync_transition(result, old_status.value, WorkStatus.BLOCKED.value)
        return result

    def get_execution(
        self,
        task_id: str,
        node_id: str | None = None,
        visit: int | None = None,
    ) -> list[FlowNodeExecution]:
        """Read execution records for a task with optional filters."""
        project, task_number = _parse_task_id(task_id)

        clauses = ["task_project = ?", "task_number = ?"]
        params: list[object] = [project, task_number]

        if node_id is not None:
            clauses.append("node_id = ?")
            params.append(node_id)
        if visit is not None:
            clauses.append("visit = ?")
            params.append(visit)

        where = " AND ".join(clauses)
        rows = self._conn.execute(
            f"SELECT * FROM work_node_executions "
            f"WHERE {where} ORDER BY id",
            params,
        ).fetchall()

        result: list[FlowNodeExecution] = []
        for r in rows:
            wo_raw = r["work_output"]
            wo: WorkOutput | None = None
            if wo_raw:
                wo_dict = json.loads(wo_raw)
                wo = WorkOutput(
                    type=OutputType(wo_dict["type"]),
                    summary=wo_dict["summary"],
                    artifacts=[
                        Artifact(
                            kind=ArtifactKind(a["kind"]),
                            description=a.get("description", ""),
                            ref=a.get("ref"),
                            path=a.get("path"),
                            external_ref=a.get("external_ref"),
                        )
                        for a in wo_dict.get("artifacts", [])
                    ],
                )
            result.append(
                FlowNodeExecution(
                    task_id=f"{r['task_project']}/{r['task_number']}",
                    node_id=r["node_id"],
                    visit=r["visit"],
                    status=ExecutionStatus(r["status"]),
                    work_output=wo,
                    decision=(
                        Decision(r["decision"])
                        if r["decision"]
                        else None
                    ),
                    decision_reason=r["decision_reason"],
                    started_at=(
                        datetime.fromisoformat(r["started_at"])
                        if r["started_at"]
                        else None
                    ),
                    completed_at=(
                        datetime.fromisoformat(r["completed_at"])
                        if r["completed_at"]
                        else None
                    ),
                )
            )
        return result

    # ------------------------------------------------------------------
    # Gate validation (dry-run)
    # ------------------------------------------------------------------

    def validate_advance(self, task_id: str, actor: str) -> list[GateResult]:
        """Dry-run: would advancing the current node succeed for this actor?

        Evaluates all gates listed on the current flow node, plus an
        actor-vs-role check matching what the real transition methods do.
        Returns the combined results without modifying any state.
        """
        task = self.get(task_id)
        if task.current_node_id is None:
            return []

        try:
            flow, node = self._get_current_flow_node(task)
        except InvalidTransitionError:
            return []

        results: list[GateResult] = []

        # Actor-vs-role check: synthesised as a hard gate result so callers
        # using validate_advance for permission preflight get a correct answer.
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
            results.extend(
                evaluate_gates(
                    task, node.gates, self._gate_registry,
                    **self._gate_kwargs(),
                )
            )

        return results

    # ------------------------------------------------------------------
    # Dependencies
    # ------------------------------------------------------------------

    def link(self, from_id: str, to_id: str, kind: str) -> None:
        """Create a relationship between two tasks.

        ``kind`` must be one of: blocks, relates_to, supersedes, parent.
        For ``blocks``, cycle detection is performed before committing.
        """
        link_tasks(self, from_id, to_id, kind)

    def unlink(self, from_id: str, to_id: str, kind: str) -> None:
        """Remove a relationship between two tasks."""
        unlink_tasks(self, from_id, to_id, kind)

    def dependents(self, task_id: str) -> list[Task]:
        """Return all tasks blocked by this task, transitively.

        Follows ``blocks`` edges from task_id outward via BFS.
        """
        return dependent_tasks(self, task_id)

    # ------------------------------------------------------------------
    # Dependency helpers
    # ------------------------------------------------------------------

    def _would_create_cycle(
        self,
        from_project: str,
        from_number: int,
        to_project: str,
        to_number: int,
    ) -> bool:
        """DFS from to_id following blocks edges; returns True if from_id is reachable."""
        return would_create_cycle(
            self,
            from_project,
            from_number,
            to_project,
            to_number,
        )

    def _has_unresolved_blockers(self, task_id: str) -> bool:
        """Check if a task has any blockers that are not done."""
        return has_unresolved_blockers(self, task_id)

    def _maybe_block(self, task_id: str) -> None:
        """If task is queued or in_progress and has unresolved blockers, block it."""
        maybe_block(self, task_id)

    def _maybe_unblock(self, task_id: str) -> None:
        """If task is blocked and has no remaining unresolved blockers, unblock it."""
        maybe_unblock(self, task_id)

    def _check_auto_unblock(self, task_id: str) -> None:
        """After a task moves to done, auto-unblock any tasks it was blocking."""
        check_auto_unblock(self, task_id)

    def _on_cancelled(self, task_id: str) -> None:
        """After a task is cancelled, add context entries on blocked dependents."""
        on_cancelled(self, task_id)

    def _on_task_done(self, task_id: str, actor: str) -> None:
        """Post-commit hook — fire milestone/digest flush on done transitions.

        Best-effort: failures are logged and swallowed so a broken
        notification side-channel never blocks the primary transition.
        Runs after ``_check_auto_unblock`` and ``teardown_worker`` so
        the ordering matches the rest of the completion pipeline.
        """
        on_task_done(self, task_id, actor)

    def _on_task_transition(
        self,
        task_id: str,
        from_state: str,
        to_state: str,
        actor: str,
    ) -> None:
        """Post-commit hook for any state transition.

        Currently routes regression detection (away from ``done``). Safe
        on every transition; cheap when not applicable.
        """
        on_task_transition(self, task_id, from_state, to_state, actor)

    def mark_done(self, task_id: str, actor: str) -> Task:
        """Move a task to done and trigger auto-unblock on dependents.

        This is a helper for completing tasks. Full flow-based completion
        (approve/node_done) will call ``_check_auto_unblock`` as well.
        """
        return mark_task_done(self, task_id, actor)

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
        """Append a context entry to a task's log.

        ``entry_type`` classifies the row. ``"note"`` is the default (generic
        context log, mirrors prior behaviour). Inbox callers use ``"reply"``
        or ``"read"`` via :meth:`add_reply` and :meth:`mark_read` helpers.
        """
        project, number = _parse_task_id(task_id)
        # Validate task exists
        row = self._conn.execute(
            "SELECT 1 FROM work_tasks WHERE project = ? AND task_number = ?",
            (project, number),
        ).fetchone()
        if row is None:
            raise TaskNotFoundError(f"Task '{task_id}' not found.")

        now = _now()
        self._conn.execute(
            "INSERT INTO work_context_entries "
            "(task_project, task_number, actor, text, created_at, entry_type) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (project, number, actor, text, now, entry_type),
        )
        self._conn.commit()
        return ContextEntry(
            actor=actor,
            timestamp=datetime.fromisoformat(now),
            text=text,
            entry_type=entry_type,
        )

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
        """Public typed API for digest/silent notification staging."""
        return stage_notification_row(
            self,
            project=project,
            subject=subject,
            body=body,
            actor=actor,
            priority=priority,
            milestone_key=milestone_key,
            payload=payload,
        )

    def list_digest_rollup_candidates(
        self,
        *,
        project: str,
        milestone_key: str | None,
    ) -> list[DigestRollupCandidate]:
        return list_digest_rollup_candidates(
            self,
            project=project,
            milestone_key=milestone_key,
        )

    def mark_rollup_candidates_flushed(
        self,
        candidates: list[DigestRollupCandidate],
        *,
        rollup_task_id: str,
        flushed_at: str,
    ) -> None:
        mark_rollup_candidates_flushed(
            self,
            candidates,
            rollup_task_id=rollup_task_id,
            flushed_at=flushed_at,
        )

    def has_old_pending_digest_rows(
        self,
        *,
        project: str,
        milestone_key: str | None,
        min_age_seconds: int,
    ) -> bool:
        return has_old_pending_digest_rows(
            self,
            project=project,
            milestone_key=milestone_key,
            min_age_seconds=min_age_seconds,
        )

    def find_flushed_rollup_milestone(self, *, task_id: str) -> str | None:
        return find_flushed_rollup_milestone(self, task_id=task_id)

    def prune_staged_notifications(self, *, retain_days: int = 30) -> dict[str, int]:
        return prune_staged_notifications(self, retain_days=retain_days)

    def get_context(
        self,
        task_id: str,
        limit: int | None = None,
        since: datetime | None = None,
        entry_type: str | None = None,
    ) -> list[ContextEntry]:
        """Query context entries for a task, most recent first.

        When ``entry_type`` is given, only rows matching that tag are
        returned — pass ``"reply"`` for the inbox thread view,
        ``"read"`` for read markers, or ``None`` for every row.
        """
        project, number = _parse_task_id(task_id)
        clauses = ["task_project = ?", "task_number = ?"]
        params: list[object] = [project, number]

        if since is not None:
            clauses.append("created_at > ?")
            params.append(since.isoformat())

        if entry_type is not None:
            clauses.append("entry_type = ?")
            params.append(entry_type)

        where = " AND ".join(clauses)
        sql = f"SELECT * FROM work_context_entries WHERE {where} ORDER BY id DESC"

        if limit is not None:
            sql += f" LIMIT {int(limit)}"

        rows = self._conn.execute(sql, params).fetchall()
        entries = []
        for r in rows:
            # entry_type may be absent on rows written before migration 3
            # on a DB that was still being upgraded; coerce defensively.
            try:
                etype = r["entry_type"] or "note"
            except (KeyError, IndexError):
                etype = "note"
            entries.append(
                ContextEntry(
                    actor=r["actor"],
                    timestamp=datetime.fromisoformat(r["created_at"]),
                    text=r["text"],
                    entry_type=etype,
                )
            )
        return entries

    # ------------------------------------------------------------------
    # Inbox actions — reply / archive / read-marker
    #
    # These three methods are the work-service backing for the cockpit's
    # interactive inbox screen. Each wraps a primitive (add_context,
    # mark_done) with the idempotency + event-emission shape the UI
    # expects, so the Textual layer stays focused on interaction and
    # doesn't reinvent state management.
    # ------------------------------------------------------------------

    def add_reply(
        self, task_id: str, body: str, actor: str = "user",
    ) -> ContextEntry:
        """Record a user reply on an inbox task.

        Stored as a ``work_context_entries`` row with ``entry_type='reply'``
        so :meth:`list_replies` and the inbox thread view can render chat
        turns without collision with system/notes context.

        Raises :class:`ValidationError` when ``body`` is empty after strip.
        """
        if not body or not body.strip():
            raise ValidationError("Reply body must not be empty.")
        return self.add_context(
            task_id, actor, body.strip(), entry_type="reply",
        )

    def archive_task(self, task_id: str, actor: str = "user") -> Task:
        """Flip an inbox task to the chat-flow terminal state.

        Idempotent: archiving an already-terminal task is a no-op and
        returns the current record unchanged. Uses the same underlying
        transition shape as :meth:`mark_done` so dashboard counts and
        dependency unblocking stay consistent.
        """
        task = self.get(task_id)
        if task.work_status in TERMINAL_STATUSES:
            return task
        now = _now()
        self._record_transition(
            task.project,
            task.task_number,
            task.work_status.value,
            WorkStatus.DONE.value,
            actor,
            reason="inbox.archive",
        )
        self._conn.execute(
            "UPDATE work_tasks SET work_status = ?, updated_at = ? "
            "WHERE project = ? AND task_number = ?",
            (WorkStatus.DONE.value, now, task.project, task.task_number),
        )
        self._conn.commit()
        # Cascade: any dependents blocked on this task should unblock,
        # same as mark_done. archive_task is effectively 'done' for the
        # chat-flow, so we respect the same contract.
        try:
            self._check_auto_unblock(task_id)
        except Exception:  # noqa: BLE001
            # Unblock cascading is best-effort — never let it break archive.
            logger.debug("auto_unblock after archive failed", exc_info=True)
        return self.get(task_id)

    def mark_read(self, task_id: str, actor: str = "user") -> bool:
        """Record a read-marker on an inbox task if one isn't already present.

        Returns ``True`` when a new marker row was written, ``False`` when
        a read row already existed (idempotent repeat-open). Callers use
        the return value to gate event emission so the activity feed only
        sees the *first* open.
        """
        project, number = _parse_task_id(task_id)
        row = self._conn.execute(
            "SELECT 1 FROM work_tasks WHERE project = ? AND task_number = ?",
            (project, number),
        ).fetchone()
        if row is None:
            raise TaskNotFoundError(f"Task '{task_id}' not found.")
        existing = self._conn.execute(
            "SELECT 1 FROM work_context_entries "
            "WHERE task_project = ? AND task_number = ? AND entry_type = 'read' "
            "LIMIT 1",
            (project, number),
        ).fetchone()
        if existing is not None:
            return False
        now = _now()
        self._conn.execute(
            "INSERT INTO work_context_entries "
            "(task_project, task_number, actor, text, created_at, entry_type) "
            "VALUES (?, ?, ?, ?, ?, 'read')",
            (project, number, actor, "opened in cockpit inbox", now),
        )
        self._conn.commit()
        return True

    def list_replies(self, task_id: str) -> list[ContextEntry]:
        """Return reply entries for a task in chronological (oldest first) order.

        Thin wrapper over :meth:`get_context` so callers don't need to
        remember the ``entry_type='reply'`` convention, and so the inbox
        view can render the thread in natural reading order without the
        reverse() gymnastics the general context log requires.
        """
        entries = self.get_context(task_id, entry_type="reply")
        entries.reverse()  # get_context returns newest-first
        return entries

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def next(
        self, *, agent: str | None = None, project: str | None = None
    ) -> Task | None:
        """Return the highest-priority queued+unblocked task.

        Priority ordering: critical > high > normal > low, then FIFO by created_at.
        Does NOT claim the task.
        """
        return next_task(self, agent=agent, project=project)

    def my_tasks(self, agent: str) -> list[Task]:
        """All tasks where *agent* fills a role that owns the current node.

        For each task with a non-null current_node_id, resolve the current
        node's actor and check if the agent matches the expected role.
        """
        return read_my_tasks(self, agent)

    def state_counts(self, project: str | None = None) -> dict[str, int]:
        """Task counts by work_status. For dashboards."""
        return read_state_counts(self, project)

    def blocked_tasks(self, project: str | None = None) -> list[Task]:
        """All tasks with ``work_status == blocked`` in a non-terminal state.

        Per spec (§5 + OQ-7): a task that is currently ``blocked`` must be
        surfaced regardless of whether its blocker is still active, done,
        or cancelled — the PM needs to see cancelled-blocker cases to
        decide whether to unblock or cancel. Dependency-resolution gating
        is internal to ``next()`` and auto-unblock.
        """
        return read_blocked_tasks(self, project)

    # ------------------------------------------------------------------
    # Flows (public API)
    # ------------------------------------------------------------------

    def _resolve_project_path(self, project: str | None) -> Path | None:
        """Resolve a project name to a filesystem path.

        Falls back to the constructor-provided ``project_path`` when the
        name can't be resolved via config. Returns ``None`` only when no
        fallback is available.
        """
        if project is None:
            return self._project_path

        # Try the pollypm config for a matching project name.
        try:
            from pollypm.config import load_config
            config = load_config()
            normalized = project.replace("-", "_")
            key = project if project in config.projects else (
                normalized if normalized in config.projects else None
            )
            if key is not None:
                return config.projects[key].path
        except Exception:
            pass

        # Fallback: if it looks like a path, use it; otherwise stick with
        # the service's bound project_path.
        candidate = Path(project)
        if candidate.exists() and candidate.is_dir():
            return candidate

        return self._project_path

    def available_flows(self, project: str | None = None) -> list[FlowTemplate]:
        """List all available flows after override resolution.

        When ``project`` is supplied, resolves to that project's path (via
        the pollypm config) and includes its project-local flows.
        """
        from pollypm.work.flow_engine import available_flows as _available_flows

        project_path = self._resolve_project_path(project)
        flow_map = _available_flows(project_path)
        templates: list[FlowTemplate] = []
        for name, path in flow_map.items():
            try:
                tmpl = resolve_flow(name, project_path)
                templates.append(tmpl)
            except Exception:
                pass
        return templates

    def get_flow(self, name: str, project: str | None = None) -> FlowTemplate:
        """Resolve a flow by name through the override chain.

        When ``project`` is supplied, resolves to that project's path (via
        the pollypm config) so project-local overrides apply.
        """
        project_path = self._resolve_project_path(project)
        return resolve_flow(name, project_path)

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    def _record_sync_state(
        self,
        project: str,
        task_number: int,
        adapter_name: str,
        success: bool,
        error: str | None,
    ) -> None:
        """Upsert a work_sync_state row after attempting to sync a task."""
        record_sync_state(
            self,
            project,
            task_number,
            adapter_name,
            success=success,
            error=error,
        )

    def sync_status(self, task_id: str) -> dict[str, object]:
        """Current sync state per registered adapter for a task.

        Returns a mapping ``adapter_name -> {last_synced_at, last_error,
        attempts}``. Adapters that have never attempted a sync for this
        task appear with ``None`` fields and ``attempts=0``.
        """
        return read_sync_status(self, task_id)

    def trigger_sync(
        self,
        task_id: str | None = None,
        adapter: str | None = None,
    ) -> dict[str, object]:
        """Force a sync cycle. Optional filters.

        - ``task_id``: only sync this task (otherwise sync every task).
        - ``adapter``: only dispatch to the adapter with this ``name``.

        Returns a summary: ``{synced: int, errors: {adapter_name:
        [task_id, ...]}}``.
        """
        return run_trigger_sync(self, task_id=task_id, adapter=adapter)

    # ------------------------------------------------------------------
    # Worker sessions
    # ------------------------------------------------------------------
    #
    # Schema + CRUD for the work_sessions table backing SessionManager.
    # Owning these rows here (instead of SessionManager reaching into
    # ``self._conn``) keeps the session manager honest about the service
    # protocol surface and makes the whole binding mockable.

    _WORK_SESSIONS_DDL = WORK_SESSIONS_DDL

    @staticmethod
    def _row_to_worker_session_record(row) -> WorkerSessionRecord:
        return row_to_worker_session_record(row)

    def ensure_worker_session_schema(self) -> None:
        ensure_worker_sessions(self)

    def upsert_worker_session(
        self,
        *,
        task_project: str,
        task_number: int,
        agent_name: str,
        pane_id: str,
        worktree_path: str,
        branch_name: str,
        started_at: str,
    ) -> None:
        save_worker_session(
            self,
            task_project=task_project,
            task_number=task_number,
            agent_name=agent_name,
            pane_id=pane_id,
            worktree_path=worktree_path,
            branch_name=branch_name,
            started_at=started_at,
        )

    def get_worker_session(
        self,
        *,
        task_project: str,
        task_number: int,
        active_only: bool = False,
    ) -> WorkerSessionRecord | None:
        return read_worker_session(
            self,
            task_project=task_project,
            task_number=task_number,
            active_only=active_only,
        )

    def list_worker_sessions(
        self,
        *,
        project: str | None = None,
        active_only: bool = True,
    ) -> list[WorkerSessionRecord]:
        return read_worker_sessions(
            self,
            project=project,
            active_only=active_only,
        )

    def end_worker_session(
        self,
        *,
        task_project: str,
        task_number: int,
        ended_at: str,
        total_input_tokens: int,
        total_output_tokens: int,
        archive_path: str | None,
    ) -> None:
        finish_worker_session(
            self,
            task_project=task_project,
            task_number=task_number,
            ended_at=ended_at,
            total_input_tokens=total_input_tokens,
            total_output_tokens=total_output_tokens,
            archive_path=archive_path,
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
        save_worker_session_tokens(
            self,
            task_project=task_project,
            task_number=task_number,
            total_input_tokens=total_input_tokens,
            total_output_tokens=total_output_tokens,
            archive_path=archive_path,
        )
