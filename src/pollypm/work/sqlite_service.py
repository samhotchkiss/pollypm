"""SQLite-backed implementation of the WorkService protocol.

Provides task CRUD and state transitions backed by a local SQLite database.
Flow templates are resolved via the flow engine and persisted on first use.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

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
    WorkOutput,
    WorkStatus,
    TERMINAL_STATUSES,
)
from pollypm.work.schema import create_work_tables
from pollypm.work.sync import SyncManager


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class WorkServiceError(Exception):
    """Base error for work service operations."""


class TaskNotFoundError(WorkServiceError):
    """Raised when a task_id cannot be resolved."""


class InvalidTransitionError(WorkServiceError):
    """Raised when a state transition is not allowed."""


class ValidationError(WorkServiceError):
    """Raised when input validation fails."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_task_id(task_id: str) -> tuple[str, int]:
    """Parse ``'project/number'`` into (project, task_number)."""
    parts = task_id.rsplit("/", 1)
    if len(parts) != 2:
        raise ValidationError(
            f"Invalid task_id '{task_id}'. Expected format: 'project/number'."
        )
    try:
        return parts[0], int(parts[1])
    except ValueError:
        raise ValidationError(
            f"Invalid task_id '{task_id}'. Task number must be an integer."
        )


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
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        create_work_tables(self._conn)
        self._gate_registry = GateRegistry(project_path=project_path)

    def set_session_manager(self, session_manager: object) -> None:
        """Wire up the session manager after construction.

        This supports two-phase init: the service is created first, then
        the session manager (which needs a reference to the service) is
        created and registered back.
        """
        self._session_mgr = session_manager

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _sync_transition(self, task: Task, old_status: str, new_status: str) -> None:
        """Fire sync adapter hooks for a state transition."""
        if self._sync:
            self._sync.on_transition(task, old_status, new_status)

    # ------------------------------------------------------------------
    # Internal: flow template persistence
    # ------------------------------------------------------------------

    def _ensure_flow_in_db(self, name: str) -> FlowTemplate:
        """Load a flow via the engine, persist it if missing, and return it."""
        template = resolve_flow(name, self._project_path)
        row = self._conn.execute(
            "SELECT 1 FROM work_flow_templates WHERE name = ? AND version = ?",
            (template.name, template.version),
        ).fetchone()
        if row is not None:
            return template

        now = _now()
        self._conn.execute(
            "INSERT INTO work_flow_templates "
            "(name, version, description, roles, start_node, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                template.name,
                template.version,
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
                "type, actor_type, actor_role, next_node_id, reject_node_id, gates) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    template.name,
                    template.version,
                    node_id,
                    node.name,
                    node.type.value,
                    node.actor_type.value if node.actor_type else None,
                    node.actor_role,
                    node.next_node_id,
                    node.reject_node_id,
                    json.dumps(node.gates),
                ),
            )
        self._conn.commit()
        return template

    def _load_flow_from_db(self, name: str, version: int) -> FlowTemplate:
        """Load a flow template from the database."""
        row = self._conn.execute(
            "SELECT * FROM work_flow_templates WHERE name = ? AND version = ?",
            (name, version),
        ).fetchone()
        if row is None:
            # Fall back to engine resolution
            return resolve_flow(name, self._project_path)

        roles = json.loads(row["roles"])
        nodes: dict[str, FlowNode] = {}
        node_rows = self._conn.execute(
            "SELECT * FROM work_flow_nodes "
            "WHERE flow_template_name = ? AND flow_template_version = ?",
            (name, version),
        ).fetchall()
        for nr in node_rows:
            nodes[nr["node_id"]] = FlowNode(
                name=nr["name"],
                type=NodeType(nr["type"]),
                actor_type=ActorType(nr["actor_type"]) if nr["actor_type"] else None,
                actor_role=nr["actor_role"],
                next_node_id=nr["next_node_id"],
                reject_node_id=nr["reject_node_id"],
                gates=json.loads(nr["gates"]),
            )

        return FlowTemplate(
            name=row["name"],
            description=row["description"],
            roles=roles,
            nodes=nodes,
            start_node=row["start_node"],
            version=row["version"],
            is_current=bool(row["is_current"]),
        )

    # ------------------------------------------------------------------
    # Internal: task reconstruction
    # ------------------------------------------------------------------

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        """Build a Task dataclass from a database row."""
        project = row["project"]
        task_number = row["task_number"]

        transitions = self._load_transitions(project, task_number)
        executions = self._load_executions(project, task_number)
        rels = self._load_relationships(project, task_number)

        task = Task(
            project=project,
            task_number=task_number,
            title=row["title"],
            type=TaskType(row["type"]),
            labels=json.loads(row["labels"]),
            work_status=WorkStatus(row["work_status"]),
            flow_template_id=row["flow_template_id"],
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
        )
        return task

    def _load_relationships(
        self, project: str, task_number: int
    ) -> dict:
        """Load dependency relationships for a task from work_task_dependencies."""
        # Outgoing edges: this task is from_id
        out_rows = self._conn.execute(
            "SELECT to_project, to_task_number, kind FROM work_task_dependencies "
            "WHERE from_project = ? AND from_task_number = ?",
            (project, task_number),
        ).fetchall()
        # Incoming edges: this task is to_id
        in_rows = self._conn.execute(
            "SELECT from_project, from_task_number, kind FROM work_task_dependencies "
            "WHERE to_project = ? AND to_task_number = ?",
            (project, task_number),
        ).fetchall()

        rels: dict = {
            "blocks": [],
            "blocked_by": [],
            "relates_to": [],
            "children": [],
            "superseded_by_project": None,
            "superseded_by_task_number": None,
        }

        for r in out_rows:
            kind = r["kind"]
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

        for r in in_rows:
            kind = r["kind"]
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

    # ------------------------------------------------------------------
    # Owner derivation
    # ------------------------------------------------------------------

    def derive_owner(self, task: Task) -> str | None:
        """Derive the current owner from the flow node's actor configuration."""
        if task.current_node_id is None:
            if task.work_status == WorkStatus.DRAFT:
                return "project_manager"
            return None

        try:
            flow = self._load_flow_from_db(
                task.flow_template_id,
                1,  # version
            )
        except Exception:
            return task.assignee

        node = flow.nodes.get(task.current_node_id)
        if node is None:
            return task.assignee

        if node.actor_type == ActorType.ROLE:
            return task.roles.get(node.actor_role or "", task.assignee)
        elif node.actor_type == ActorType.HUMAN:
            return "human"
        elif node.actor_type == ActorType.PROJECT_MANAGER:
            return "project_manager"
        elif node.actor_type == ActorType.AGENT:
            return "agent"
        return task.assignee

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
        # Resolve and persist the flow template
        template = self._ensure_flow_in_db(flow_template)

        # Validate required roles
        for role_name, role_def in template.roles.items():
            is_optional = isinstance(role_def, dict) and role_def.get("optional", False)
            if not is_optional and role_name not in roles:
                raise ValidationError(
                    f"Required role '{role_name}' not provided. "
                    f"Flow '{template.name}' requires: "
                    f"{[r for r, d in template.roles.items() if not (isinstance(d, dict) and d.get('optional', False))]}"
                )

        # Validate enums
        try:
            task_type = TaskType(type)
        except ValueError:
            raise ValidationError(f"Invalid task type '{type}'.")

        try:
            task_priority = Priority(priority)
        except ValueError:
            raise ValidationError(f"Invalid priority '{priority}'.")

        now = _now()

        # Assign sequential task_number per project
        row = self._conn.execute(
            "SELECT COALESCE(MAX(task_number), 0) AS max_num "
            "FROM work_tasks WHERE project = ?",
            (project,),
        ).fetchone()
        task_number = row["max_num"] + 1

        self._conn.execute(
            "INSERT INTO work_tasks "
            "(project, task_number, title, type, labels, work_status, "
            "flow_template_id, flow_template_version, current_node_id, "
            "assignee, priority, requires_human_review, description, "
            "acceptance_criteria, constraints, relevant_files, "
            "roles, external_refs, created_at, created_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project,
                task_number,
                title,
                task_type.value,
                json.dumps(labels or []),
                WorkStatus.DRAFT.value,
                template.name,
                template.version,
                None,  # current_node_id
                None,  # assignee
                task_priority.value,
                int(requires_human_review),
                description,
                acceptance_criteria,
                constraints,
                json.dumps(relevant_files or []),
                json.dumps(roles),
                json.dumps({}),
                now,
                created_by,
                now,
            ),
        )
        self._conn.commit()
        task = self.get(f"{project}/{task_number}")
        if self._sync:
            self._sync.on_create(task)
        return task

    def get(self, task_id: str) -> Task:
        """Read a task by its ``project/number`` identifier."""
        project, task_number = _parse_task_id(task_id)
        row = self._conn.execute(
            "SELECT * FROM work_tasks WHERE project = ? AND task_number = ?",
            (project, task_number),
        ).fetchone()
        if row is None:
            raise TaskNotFoundError(f"Task '{task_id}' not found.")
        return self._row_to_task(row)

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
        clauses: list[str] = []
        params: list[object] = []

        if work_status is not None:
            clauses.append("work_status = ?")
            params.append(work_status)
        if project is not None:
            clauses.append("project = ?")
            params.append(project)
        if assignee is not None:
            clauses.append("assignee = ?")
            params.append(assignee)
        if type is not None:
            clauses.append("type = ?")
            params.append(type)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM work_tasks{where} ORDER BY project, task_number"

        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        if offset is not None:
            sql += f" OFFSET {int(offset)}"

        rows = self._conn.execute(sql, params).fetchall()
        tasks = [self._row_to_task(r) for r in rows]

        # Post-query filters that require derived data
        if owner is not None:
            tasks = [t for t in tasks if self.derive_owner(t) == owner]
        if blocked is not None:
            tasks = [t for t in tasks if t.blocked == blocked]

        return tasks

    def update(self, task_id: str, **fields: object) -> Task:
        """Update mutable fields on a task."""
        if "work_status" in fields:
            raise ValidationError(
                "Cannot change work_status via update(). "
                "Use lifecycle methods (queue, claim, cancel, etc.)."
            )
        if "flow_template" in fields or "flow_template_id" in fields:
            raise ValidationError("Cannot change flow_template after creation.")

        project, task_number = _parse_task_id(task_id)

        # Ensure task exists
        existing = self._conn.execute(
            "SELECT 1 FROM work_tasks WHERE project = ? AND task_number = ?",
            (project, task_number),
        ).fetchone()
        if existing is None:
            raise TaskNotFoundError(f"Task '{task_id}' not found.")

        ALLOWED = {
            "title": "title",
            "description": "description",
            "priority": "priority",
            "labels": "labels",
            "roles": "roles",
            "acceptance_criteria": "acceptance_criteria",
            "constraints": "constraints",
            "relevant_files": "relevant_files",
        }

        set_clauses: list[str] = []
        params: list[object] = []

        for key, value in fields.items():
            col = ALLOWED.get(key)
            if col is None:
                raise ValidationError(f"Field '{key}' is not updatable.")
            if key in ("labels", "relevant_files"):
                value = json.dumps(value)
            elif key == "roles":
                value = json.dumps(value)
            set_clauses.append(f"{col} = ?")
            params.append(value)

        if not set_clauses:
            return self.get(task_id)

        set_clauses.append("updated_at = ?")
        params.append(_now())
        params.extend([project, task_number])

        sql = (
            f"UPDATE work_tasks SET {', '.join(set_clauses)} "
            f"WHERE project = ? AND task_number = ?"
        )
        self._conn.execute(sql, params)
        self._conn.commit()
        task = self.get(task_id)
        if self._sync:
            self._sync.on_update(task, list(fields.keys()))
        return task

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

        if task.requires_human_review:
            raise InvalidTransitionError(
                "Task requires human review before queueing. "
                "Human approval integration is not yet available."
            )

        # Gate: has_description
        gate_results = evaluate_gates(
            task, ["has_description"], self._gate_registry,
            get_task=self.get,
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
            raise InvalidTransitionError(
                f"Cannot claim task in '{task.work_status.value}' state. "
                f"Task must be in 'queued' state."
            )

        # blocked check (for now always False)
        if task.blocked:
            raise InvalidTransitionError("Cannot claim a blocked task.")

        flow = self._load_flow_from_db(task.flow_template_id, 1)
        start_node = flow.start_node
        now = _now()

        try:
            # Atomic: update status, assignee, current_node, and create execution
            self._conn.execute(
                "UPDATE work_tasks SET work_status = ?, assignee = ?, "
                "current_node_id = ?, updated_at = ? "
                "WHERE project = ? AND task_number = ?",
                (
                    WorkStatus.IN_PROGRESS.value,
                    actor,
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
        # Provision a per-task worker session with worktree
        if self._session_mgr is not None:
            try:
                self._session_mgr.provision_worker(task_id, actor)
            except Exception:  # noqa: BLE001
                pass  # Best-effort — task is claimed regardless
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
            except Exception:  # noqa: BLE001
                pass
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
            raise ValidationError("Work output must have a non-empty summary.")
        if not output.artifacts:
            raise ValidationError(
                "Work output must have at least one artifact."
            )
        for i, art in enumerate(output.artifacts):
            if not isinstance(art.kind, ArtifactKind):
                try:
                    ArtifactKind(art.kind)
                except (ValueError, KeyError):
                    raise ValidationError(
                        f"Artifact {i}: invalid kind '{art.kind}'."
                    )
            if not (art.description or art.ref or art.path):
                raise ValidationError(
                    f"Artifact {i}: must have at least one of "
                    f"description, ref, or path."
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
        flow = self._load_flow_from_db(task.flow_template_id, 1)
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

    def _validate_actor_role(
        self, task: Task, node: FlowNode, actor: str
    ) -> None:
        """Validate that actor matches the node's expected role."""
        if node.actor_type == ActorType.HUMAN:
            # Accept well-known human names plus the assigned reviewer role value
            allowed = set(self._HUMAN_ACTOR_NAMES)
            if node.actor_role:
                reviewer = task.roles.get(node.actor_role)
                if reviewer:
                    allowed.add(reviewer)
            if actor not in allowed:
                raise ValidationError(
                    f"Node '{node.name}' requires human review. "
                    f"Actor '{actor}' is not authorized — "
                    f"accepted actors: {', '.join(sorted(allowed))}."
                )
        elif node.actor_type == ActorType.ROLE and node.actor_role:
            expected_actor = task.roles.get(node.actor_role)
            if expected_actor and actor != expected_actor:
                # Also accept the role name itself (e.g. "worker" matches role "worker")
                if actor != node.actor_role:
                    raise ValidationError(
                        f"Actor '{actor}' does not match role "
                        f"'{node.actor_role}' (expected '{expected_actor}')."
                    )

    def _next_visit(
        self, project: str, task_number: int, node_id: str
    ) -> int:
        """Return the next visit number for a node execution."""
        row = self._conn.execute(
            "SELECT COALESCE(MAX(visit), 0) AS max_v "
            "FROM work_node_executions "
            "WHERE task_project = ? AND task_number = ? AND node_id = ?",
            (project, task_number, node_id),
        ).fetchone()
        return row["max_v"] + 1

    def _advance_to_node(
        self,
        task: Task,
        flow: FlowTemplate,
        next_node_id: str | None,
        actor: str,
        from_status: WorkStatus,
    ) -> None:
        """Advance the task to the next node, updating status and execution."""
        now = _now()

        if next_node_id is None:
            raise InvalidTransitionError("No next node defined.")

        next_node = flow.nodes.get(next_node_id)
        if next_node is None:
            raise InvalidTransitionError(
                f"Next node '{next_node_id}' not found in flow."
            )

        if next_node.type == NodeType.TERMINAL:
            new_status = WorkStatus.DONE
            self._conn.execute(
                "UPDATE work_tasks SET work_status = ?, "
                "current_node_id = NULL, updated_at = ? "
                "WHERE project = ? AND task_number = ?",
                (new_status.value, now, task.project, task.task_number),
            )
            self._record_transition(
                task.project,
                task.task_number,
                from_status.value,
                new_status.value,
                actor,
            )
        elif next_node.type in (NodeType.REVIEW, NodeType.WORK):
            new_status = (
                WorkStatus.REVIEW
                if next_node.type == NodeType.REVIEW
                else WorkStatus.IN_PROGRESS
            )
            visit = self._next_visit(
                task.project, task.task_number, next_node_id
            )
            self._conn.execute(
                "UPDATE work_tasks SET work_status = ?, "
                "current_node_id = ?, updated_at = ? "
                "WHERE project = ? AND task_number = ?",
                (
                    new_status.value,
                    next_node_id,
                    now,
                    task.project,
                    task.task_number,
                ),
            )
            self._conn.execute(
                "INSERT INTO work_node_executions "
                "(task_project, task_number, node_id, visit, status, "
                "started_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    task.project,
                    task.task_number,
                    next_node_id,
                    visit,
                    ExecutionStatus.ACTIVE.value,
                    now,
                ),
            )
            self._record_transition(
                task.project,
                task.task_number,
                from_status.value,
                new_status.value,
                actor,
            )

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
                get_task=self.get,
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
            raise ValidationError("Work output is required for node_done.")
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
                except Exception:  # noqa: BLE001
                    pass
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
            raise InvalidTransitionError(
                f"Cannot approve task in '{task.work_status.value}' state. "
                f"Task must be in 'review' state."
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
                get_task=self.get,
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
                except Exception:  # noqa: BLE001
                    pass
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
            self._conn.execute(
                "UPDATE work_tasks SET work_status = ?, "
                "current_node_id = ?, updated_at = ? "
                "WHERE project = ? AND task_number = ?",
                (
                    WorkStatus.IN_PROGRESS.value,
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
        # Notify the per-task worker session about the rejection
        if self._session_mgr is not None:
            try:
                self._session_mgr.notify_rejection(task_id, reason)
            except Exception:  # noqa: BLE001
                pass
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
                    get_task=self.get,
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
        # Validate kind
        try:
            link_kind = LinkKind(kind)
        except ValueError:
            raise ValidationError(
                f"Invalid link kind '{kind}'. "
                f"Must be one of: {[k.value for k in LinkKind]}."
            )

        from_project, from_number = _parse_task_id(from_id)
        to_project, to_number = _parse_task_id(to_id)

        # Validate both tasks exist
        for tid in (from_id, to_id):
            p, n = _parse_task_id(tid)
            row = self._conn.execute(
                "SELECT 1 FROM work_tasks WHERE project = ? AND task_number = ?",
                (p, n),
            ).fetchone()
            if row is None:
                raise TaskNotFoundError(f"Task '{tid}' not found.")

        # Cycle detection for blocks
        if link_kind == LinkKind.BLOCKS:
            if self._would_create_cycle(from_project, from_number, to_project, to_number):
                raise ValidationError("circular dependency detected")

        now = _now()
        self._conn.execute(
            "INSERT OR IGNORE INTO work_task_dependencies "
            "(from_project, from_task_number, to_project, to_task_number, kind, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (from_project, from_number, to_project, to_number, link_kind.value, now),
        )
        self._conn.commit()

        # For blocks: check if to_id should become blocked
        if link_kind == LinkKind.BLOCKS:
            self._maybe_block(to_id)

    def unlink(self, from_id: str, to_id: str, kind: str) -> None:
        """Remove a relationship between two tasks."""
        try:
            link_kind = LinkKind(kind)
        except ValueError:
            raise ValidationError(
                f"Invalid link kind '{kind}'. "
                f"Must be one of: {[k.value for k in LinkKind]}."
            )

        from_project, from_number = _parse_task_id(from_id)
        to_project, to_number = _parse_task_id(to_id)

        self._conn.execute(
            "DELETE FROM work_task_dependencies "
            "WHERE from_project = ? AND from_task_number = ? "
            "AND to_project = ? AND to_task_number = ? AND kind = ?",
            (from_project, from_number, to_project, to_number, link_kind.value),
        )
        self._conn.commit()

        # For blocks: check if to_id should become unblocked
        if link_kind == LinkKind.BLOCKS:
            self._maybe_unblock(to_id)

    def dependents(self, task_id: str) -> list[Task]:
        """Return all tasks blocked by this task, transitively.

        Follows ``blocks`` edges from task_id outward via BFS.
        """
        project, number = _parse_task_id(task_id)
        visited: set[tuple[str, int]] = set()
        queue: list[tuple[str, int]] = [(project, number)]

        while queue:
            current = queue.pop(0)
            rows = self._conn.execute(
                "SELECT to_project, to_task_number FROM work_task_dependencies "
                "WHERE from_project = ? AND from_task_number = ? AND kind = ?",
                (current[0], current[1], LinkKind.BLOCKS.value),
            ).fetchall()
            for r in rows:
                target = (r["to_project"], r["to_task_number"])
                if target not in visited:
                    visited.add(target)
                    queue.append(target)

        return [self.get(f"{p}/{n}") for p, n in visited]

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
        target = (from_project, from_number)
        visited: set[tuple[str, int]] = set()
        stack: list[tuple[str, int]] = [(to_project, to_number)]

        while stack:
            current = stack.pop()
            if current == target:
                return True
            if current in visited:
                continue
            visited.add(current)
            rows = self._conn.execute(
                "SELECT to_project, to_task_number FROM work_task_dependencies "
                "WHERE from_project = ? AND from_task_number = ? AND kind = ?",
                (current[0], current[1], LinkKind.BLOCKS.value),
            ).fetchall()
            for r in rows:
                stack.append((r["to_project"], r["to_task_number"]))

        return False

    def _has_unresolved_blockers(self, task_id: str) -> bool:
        """Check if a task has any blockers that are not done."""
        project, number = _parse_task_id(task_id)
        rows = self._conn.execute(
            "SELECT d.from_project, d.from_task_number "
            "FROM work_task_dependencies d "
            "WHERE d.to_project = ? AND d.to_task_number = ? AND d.kind = ?",
            (project, number, LinkKind.BLOCKS.value),
        ).fetchall()
        for r in rows:
            status_row = self._conn.execute(
                "SELECT work_status FROM work_tasks "
                "WHERE project = ? AND task_number = ?",
                (r["from_project"], r["from_task_number"]),
            ).fetchone()
            if status_row and status_row["work_status"] not in (
                WorkStatus.DONE.value,
                WorkStatus.CANCELLED.value,
            ):
                return True
        return False

    def _maybe_block(self, task_id: str) -> None:
        """If task is queued or in_progress and has unresolved blockers, block it."""
        task = self.get(task_id)
        if task.work_status not in (WorkStatus.QUEUED, WorkStatus.IN_PROGRESS):
            return
        if self._has_unresolved_blockers(task_id):
            now = _now()
            self._record_transition(
                task.project,
                task.task_number,
                task.work_status.value,
                WorkStatus.BLOCKED.value,
                "system",
                "blocked by dependency",
            )
            self._conn.execute(
                "UPDATE work_tasks SET work_status = ?, updated_at = ? "
                "WHERE project = ? AND task_number = ?",
                (WorkStatus.BLOCKED.value, now, task.project, task.task_number),
            )
            self._conn.commit()

    def _maybe_unblock(self, task_id: str) -> None:
        """If task is blocked and has no remaining unresolved blockers, unblock it."""
        task = self.get(task_id)
        if task.work_status != WorkStatus.BLOCKED:
            return
        if not self._has_unresolved_blockers(task_id):
            now = _now()
            self._record_transition(
                task.project,
                task.task_number,
                WorkStatus.BLOCKED.value,
                WorkStatus.QUEUED.value,
                "system",
                "all blockers resolved",
            )
            self._conn.execute(
                "UPDATE work_tasks SET work_status = ?, updated_at = ? "
                "WHERE project = ? AND task_number = ?",
                (WorkStatus.QUEUED.value, now, task.project, task.task_number),
            )
            self._conn.commit()

    def _check_auto_unblock(self, task_id: str) -> None:
        """After a task moves to done, auto-unblock any tasks it was blocking."""
        task = self.get(task_id)
        # Find all tasks directly blocked by this one
        rows = self._conn.execute(
            "SELECT to_project, to_task_number FROM work_task_dependencies "
            "WHERE from_project = ? AND from_task_number = ? AND kind = ?",
            (task.project, task.task_number, LinkKind.BLOCKS.value),
        ).fetchall()
        for r in rows:
            blocked_id = f"{r['to_project']}/{r['to_task_number']}"
            blocked_task = self.get(blocked_id)
            if blocked_task.work_status != WorkStatus.BLOCKED:
                continue
            if not self._has_unresolved_blockers(blocked_id):
                now = _now()
                self._record_transition(
                    blocked_task.project,
                    blocked_task.task_number,
                    WorkStatus.BLOCKED.value,
                    WorkStatus.QUEUED.value,
                    "system",
                    f"auto-unblocked, blocker #{task.task_id} completed",
                )
                self._conn.execute(
                    "UPDATE work_tasks SET work_status = ?, updated_at = ? "
                    "WHERE project = ? AND task_number = ?",
                    (
                        WorkStatus.QUEUED.value,
                        now,
                        blocked_task.project,
                        blocked_task.task_number,
                    ),
                )
                self._conn.commit()
                unblocked = self.get(blocked_id)
                self._sync_transition(
                    unblocked, WorkStatus.BLOCKED.value, WorkStatus.QUEUED.value,
                )

    def _on_cancelled(self, task_id: str) -> None:
        """After a task is cancelled, add context entries on blocked dependents."""
        task = self.get(task_id)
        rows = self._conn.execute(
            "SELECT to_project, to_task_number FROM work_task_dependencies "
            "WHERE from_project = ? AND from_task_number = ? AND kind = ?",
            (task.project, task.task_number, LinkKind.BLOCKS.value),
        ).fetchall()
        for r in rows:
            blocked_id = f"{r['to_project']}/{r['to_task_number']}"
            self.add_context(
                blocked_id,
                "system",
                f"blocker #{task.task_id} was cancelled "
                f"— PM must decide whether to unblock or cancel this task.",
            )

    def mark_done(self, task_id: str, actor: str) -> Task:
        """Move a task to done and trigger auto-unblock on dependents.

        This is a helper for completing tasks. Full flow-based completion
        (approve/node_done) will call ``_check_auto_unblock`` as well.
        """
        task = self.get(task_id)
        if task.work_status in TERMINAL_STATUSES:
            raise InvalidTransitionError(
                f"Cannot mark done task in terminal state '{task.work_status.value}'."
            )

        now = _now()
        self._record_transition(
            task.project,
            task.task_number,
            task.work_status.value,
            WorkStatus.DONE.value,
            actor,
        )
        self._conn.execute(
            "UPDATE work_tasks SET work_status = ?, updated_at = ? "
            "WHERE project = ? AND task_number = ?",
            (WorkStatus.DONE.value, now, task.project, task.task_number),
        )
        self._conn.commit()
        self._check_auto_unblock(task_id)
        return self.get(task_id)

    # ------------------------------------------------------------------
    # Context log
    # ------------------------------------------------------------------

    def add_context(self, task_id: str, actor: str, text: str) -> ContextEntry:
        """Append a context entry to a task's log."""
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
            "(task_project, task_number, actor, text, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (project, number, actor, text, now),
        )
        self._conn.commit()
        return ContextEntry(
            actor=actor,
            timestamp=datetime.fromisoformat(now),
            text=text,
        )

    def get_context(
        self,
        task_id: str,
        limit: int | None = None,
        since: datetime | None = None,
    ) -> list[ContextEntry]:
        """Query context entries for a task, most recent first."""
        project, number = _parse_task_id(task_id)
        clauses = ["task_project = ?", "task_number = ?"]
        params: list[object] = [project, number]

        if since is not None:
            clauses.append("created_at > ?")
            params.append(since.isoformat())

        where = " AND ".join(clauses)
        sql = f"SELECT * FROM work_context_entries WHERE {where} ORDER BY id DESC"

        if limit is not None:
            sql += f" LIMIT {int(limit)}"

        rows = self._conn.execute(sql, params).fetchall()
        return [
            ContextEntry(
                actor=r["actor"],
                timestamp=datetime.fromisoformat(r["created_at"]),
                text=r["text"],
            )
            for r in rows
        ]

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
        clauses = ["t.work_status = ?"]
        params: list[object] = [WorkStatus.QUEUED.value]

        if project is not None:
            clauses.append("t.project = ?")
            params.append(project)

        where = " AND ".join(clauses)

        # Use a CASE expression for priority ordering
        sql = (
            "SELECT t.* FROM work_tasks t "
            f"WHERE {where} "
            "ORDER BY "
            "CASE t.priority "
            "  WHEN 'critical' THEN 0 "
            "  WHEN 'high' THEN 1 "
            "  WHEN 'normal' THEN 2 "
            "  WHEN 'low' THEN 3 "
            "  ELSE 4 "
            "END, "
            "t.created_at ASC"
        )

        rows = self._conn.execute(sql, params).fetchall()

        for row in rows:
            task = self._row_to_task(row)
            # Skip tasks with unresolved blockers
            if self._has_unresolved_blockers(task.task_id):
                continue
            # If agent specified, only tasks where this agent fills the worker role
            if agent is not None:
                if task.roles.get("worker") != agent:
                    continue
            return task

        return None

    def my_tasks(self, agent: str) -> list[Task]:
        """All tasks where *agent* fills a role that owns the current node.

        For each task with a non-null current_node_id, resolve the current
        node's actor and check if the agent matches the expected role.
        """
        rows = self._conn.execute(
            "SELECT * FROM work_tasks WHERE current_node_id IS NOT NULL",
        ).fetchall()

        result: list[Task] = []
        for row in rows:
            task = self._row_to_task(row)
            owner = self.derive_owner(task)
            if owner == agent:
                result.append(task)
        return result

    def state_counts(self, project: str | None = None) -> dict[str, int]:
        """Task counts by work_status. For dashboards."""
        # Initialise with zero counts for all statuses
        counts = {s.value: 0 for s in WorkStatus}

        clauses: list[str] = []
        params: list[object] = []
        if project is not None:
            clauses.append("project = ?")
            params.append(project)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT work_status, COUNT(*) as cnt FROM work_tasks{where} GROUP BY work_status"

        rows = self._conn.execute(sql, params).fetchall()
        for r in rows:
            counts[r["work_status"]] = r["cnt"]

        return counts

    def blocked_tasks(self, project: str | None = None) -> list[Task]:
        """All tasks in a non-terminal state that have unresolved blockers."""
        terminal = tuple(s.value for s in TERMINAL_STATUSES)
        placeholders = ", ".join("?" for _ in terminal)

        clauses = [f"t.work_status NOT IN ({placeholders})"]
        params: list[object] = list(terminal)

        if project is not None:
            clauses.append("t.project = ?")
            params.append(project)

        where = " AND ".join(clauses)
        sql = (
            "SELECT DISTINCT t.* FROM work_tasks t "
            "JOIN work_task_dependencies d "
            "  ON d.to_project = t.project AND d.to_task_number = t.task_number "
            "  AND d.kind = ? "
            f"WHERE {where}"
        )
        params.append(LinkKind.BLOCKS.value)

        # Reorder: the JOIN param (kind) needs to be before WHERE params
        # Actually, let's restructure to be clearer
        join_params: list[object] = [LinkKind.BLOCKS.value]
        where_params: list[object] = list(terminal)
        if project is not None:
            where_params.append(project)

        sql = (
            "SELECT DISTINCT t.* FROM work_tasks t "
            "JOIN work_task_dependencies d "
            "  ON d.to_project = t.project AND d.to_task_number = t.task_number "
            "  AND d.kind = ? "
            f"WHERE {where}"
        )
        all_params = join_params + where_params

        rows = self._conn.execute(sql, all_params).fetchall()

        # Filter in Python: only tasks where at least one blocker is not done
        result: list[Task] = []
        for row in rows:
            task = self._row_to_task(row)
            if self._has_unresolved_blockers(task.task_id):
                result.append(task)
        return result

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
