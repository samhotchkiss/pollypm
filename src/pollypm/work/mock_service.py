"""In-memory MockWorkService for testing consumers.

Implements the WorkService protocol using plain dicts and lists, with no
SQLite, filesystem, or external dependencies.  Useful for TUI tests,
consumer tests, and proving the protocol is sufficient.
"""

from __future__ import annotations

from datetime import datetime, timezone
from copy import deepcopy

from pollypm.work.flow_engine import resolve_flow
from pollypm.work.gates import GateRegistry, evaluate_gates, has_hard_failure
from pollypm.work.models import (
    Artifact,
    ArtifactKind,
    ContextEntry,
    Decision,
    ExecutionStatus,
    FlowNodeExecution,
    FlowTemplate,
    GateResult,
    LinkKind,
    NodeType,
    OutputType,
    Priority,
    Task,
    TaskType,
    Transition,
    WorkerSessionRecord,
    WorkOutput,
    WorkStatus,
    TERMINAL_STATUSES,
)


# ---------------------------------------------------------------------------
# Exceptions (mirror sqlite_service for compatibility)
# ---------------------------------------------------------------------------


class WorkServiceError(Exception):
    pass


class TaskNotFoundError(WorkServiceError):
    pass


class InvalidTransitionError(WorkServiceError):
    pass


class ValidationError(WorkServiceError):
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_task_id(task_id: str) -> tuple[str, int]:
    parts = task_id.rsplit("/", 1)
    if len(parts) != 2:
        raise ValidationError(f"Invalid task_id '{task_id}'.")
    return parts[0], int(parts[1])


# ---------------------------------------------------------------------------
# MockWorkService
# ---------------------------------------------------------------------------


class MockWorkService:
    """In-memory WorkService for testing consumers.

    Stores tasks, executions, context, and relationships in dicts.
    Flow templates are resolved from the filesystem (same as SQLite service).
    """

    def __init__(self, project_path: str | None = None) -> None:
        self._tasks: dict[str, Task] = {}
        self._counter: dict[str, int] = {}  # per-project task number counters
        self._flows: dict[str, FlowTemplate] = {}
        self._context: dict[str, list[ContextEntry]] = {}  # task_id -> entries
        self._executions: dict[str, list[FlowNodeExecution]] = {}
        self._transitions: dict[str, list[Transition]] = {}
        self._links: list[tuple[str, str, str]] = []  # (from_id, to_id, kind)
        self._project_path = project_path
        self._gate_registry = GateRegistry(project_path=project_path)
        # Keyed by (project, task_number). Stored as plain dicts so tests
        # can inspect / mutate the row shape directly.
        self._worker_sessions: dict[tuple[str, int], dict] = {}

    # ------------------------------------------------------------------
    # Flow resolution
    # ------------------------------------------------------------------

    def _resolve_flow(self, name: str) -> FlowTemplate:
        if name not in self._flows:
            self._flows[name] = resolve_flow(name, self._project_path)
        return self._flows[name]

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
        template = self._resolve_flow(flow_template)

        # Validate required roles
        for role_name, role_def in template.roles.items():
            is_optional = isinstance(role_def, dict) and role_def.get("optional", False)
            if not is_optional and role_name not in roles:
                raise ValidationError(
                    f"Required role '{role_name}' not provided. "
                    f"Flow '{template.name}' requires: "
                    f"{[r for r, d in template.roles.items() if not (isinstance(d, dict) and d.get('optional', False))]}"
                )

        try:
            task_type = TaskType(type)
        except ValueError:
            raise ValidationError(f"Invalid task type '{type}'.")

        try:
            task_priority = Priority(priority)
        except ValueError:
            raise ValidationError(f"Invalid priority '{priority}'.")

        # Assign task number
        num = self._counter.get(project, 0) + 1
        self._counter[project] = num

        now = _now()
        task = Task(
            project=project,
            task_number=num,
            title=title,
            type=task_type,
            labels=list(labels or []),
            work_status=WorkStatus.DRAFT,
            flow_template_id=template.name,
            current_node_id=None,
            assignee=None,
            priority=task_priority,
            requires_human_review=requires_human_review,
            description=description,
            acceptance_criteria=acceptance_criteria,
            constraints=constraints,
            relevant_files=list(relevant_files or []),
            roles=dict(roles),
            external_refs={},
            created_at=now,
            created_by=created_by,
            updated_at=now,
        )
        self._tasks[task.task_id] = task
        self._context[task.task_id] = []
        self._executions[task.task_id] = []
        self._transitions[task.task_id] = []
        return deepcopy(task)

    def get(self, task_id: str) -> Task:
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(f"Task '{task_id}' not found.")
        t = deepcopy(task)
        t.executions = list(self._executions.get(task_id, []))
        t.transitions = list(self._transitions.get(task_id, []))
        t.context = list(self._context.get(task_id, []))
        # Populate relationship fields
        t.blocks = [(p, n) for fid, tid, k in self._links if k == "blocks" and fid == task_id for p, n in [_parse_task_id(tid)]]
        t.blocked_by = [(p, n) for fid, tid, k in self._links if k == "blocks" and tid == task_id for p, n in [_parse_task_id(fid)]]
        t.relates_to = [(p, n) for fid, tid, k in self._links if k == "relates_to" and (fid == task_id or tid == task_id) for p, n in [_parse_task_id(tid if fid == task_id else fid)]]
        # Aggregate per-task token usage from worker-session rows (#86)
        tin, tout, cnt = self._sum_tokens_for_task(t.project, t.task_number)
        t.total_input_tokens = tin
        t.total_output_tokens = tout
        t.session_count = cnt
        return t

    def _sum_tokens_for_task(
        self, project: str, task_number: int
    ) -> tuple[int, int, int]:
        """Sum (tokens_in, tokens_out, session_count) for one task."""
        tin = 0
        tout = 0
        cnt = 0
        for row in self._worker_sessions.values():
            if row.get("task_project") == project and int(
                row.get("task_number")
            ) == task_number:
                tin += int(row.get("total_input_tokens", 0) or 0)
                tout += int(row.get("total_output_tokens", 0) or 0)
                cnt += 1
        return tin, tout, cnt

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
        tasks = list(self._tasks.values())
        if work_status is not None:
            tasks = [t for t in tasks if t.work_status.value == work_status]
        if project is not None:
            tasks = [t for t in tasks if t.project == project]
        if assignee is not None:
            tasks = [t for t in tasks if t.assignee == assignee]
        if type is not None:
            tasks = [t for t in tasks if t.type.value == type]
        if blocked is not None:
            tasks = [t for t in tasks if t.blocked == blocked]

        tasks.sort(key=lambda t: (t.project, t.task_number))
        if offset:
            tasks = tasks[offset:]
        if limit:
            tasks = tasks[:limit]
        out: list[Task] = []
        for t in tasks:
            copy = deepcopy(t)
            tin, tout, cnt = self._sum_tokens_for_task(copy.project, copy.task_number)
            copy.total_input_tokens = tin
            copy.total_output_tokens = tout
            copy.session_count = cnt
            out.append(copy)
        return out

    def queue(self, task_id: str, actor: str, skip_gates: bool = False) -> Task:
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(f"Task '{task_id}' not found.")
        if task.work_status != WorkStatus.DRAFT:
            raise InvalidTransitionError(
                f"Cannot queue task in '{task.work_status.value}' state."
            )
        if task.requires_human_review:
            raise InvalidTransitionError("Task requires human review before queueing.")

        if not skip_gates:
            results = evaluate_gates(task, ["has_description"], self._gate_registry, get_task=self.get)
            if has_hard_failure(results):
                failing = [r for r in results if not r.passed]
                raise ValidationError(f"Cannot queue task: gate failed -- {failing[0].reason}")

        self._record_transition(task_id, task.work_status.value, WorkStatus.QUEUED.value, actor)
        task.work_status = WorkStatus.QUEUED
        task.updated_at = _now()
        return deepcopy(task)

    def claim(self, task_id: str, actor: str, skip_gates: bool = False) -> Task:
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(f"Task '{task_id}' not found.")
        if task.work_status != WorkStatus.QUEUED:
            raise InvalidTransitionError(
                f"Cannot claim task in '{task.work_status.value}' state."
            )

        flow = self._resolve_flow(task.flow_template_id)
        start_node = flow.start_node
        now = _now()

        task.work_status = WorkStatus.IN_PROGRESS
        task.assignee = actor
        task.current_node_id = start_node
        task.updated_at = now

        exe = FlowNodeExecution(
            task_id=task_id,
            node_id=start_node,
            visit=1,
            status=ExecutionStatus.ACTIVE,
            started_at=now,
        )
        self._executions.setdefault(task_id, []).append(exe)
        self._record_transition(task_id, WorkStatus.QUEUED.value, WorkStatus.IN_PROGRESS.value, actor)
        return deepcopy(task)

    def next(self, *, agent: str | None = None, project: str | None = None) -> Task | None:
        candidates = [
            t for t in self._tasks.values()
            if t.work_status == WorkStatus.QUEUED
        ]
        if project is not None:
            candidates = [t for t in candidates if t.project == project]
        if agent is not None:
            candidates = [t for t in candidates if t.roles.get("worker") == agent]

        priority_order = {"critical": 0, "high": 1, "normal": 2, "low": 3}
        candidates.sort(key=lambda t: (priority_order.get(t.priority.value, 4), t.created_at or _now()))

        return deepcopy(candidates[0]) if candidates else None

    def update(self, task_id: str, **fields: object) -> Task:
        if "work_status" in fields:
            raise ValidationError("Cannot change work_status via update().")

        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(f"Task '{task_id}' not found.")

        allowed = {"title", "description", "priority", "labels", "roles",
                    "acceptance_criteria", "constraints", "relevant_files"}
        for key, value in fields.items():
            if key not in allowed:
                raise ValidationError(f"Field '{key}' is not updatable.")
            setattr(task, key, value)
        task.updated_at = _now()
        return deepcopy(task)

    def cancel(self, task_id: str, actor: str, reason: str) -> Task:
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(f"Task '{task_id}' not found.")
        if task.work_status in TERMINAL_STATUSES:
            raise InvalidTransitionError(
                f"Cannot cancel task in terminal state '{task.work_status.value}'."
            )
        old = task.work_status.value
        task.work_status = WorkStatus.CANCELLED
        task.updated_at = _now()
        self._record_transition(task_id, old, WorkStatus.CANCELLED.value, actor, reason)
        return deepcopy(task)

    def hold(self, task_id: str, actor: str, reason: str | None = None) -> Task:
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(f"Task '{task_id}' not found.")
        if task.work_status not in (WorkStatus.IN_PROGRESS, WorkStatus.QUEUED):
            raise InvalidTransitionError(
                f"Cannot hold task in '{task.work_status.value}' state."
            )
        old = task.work_status.value
        task.work_status = WorkStatus.ON_HOLD
        task.updated_at = _now()
        self._record_transition(task_id, old, WorkStatus.ON_HOLD.value, actor, reason)
        return deepcopy(task)

    def resume(self, task_id: str, actor: str) -> Task:
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(f"Task '{task_id}' not found.")
        if task.work_status != WorkStatus.ON_HOLD:
            raise InvalidTransitionError(
                f"Cannot resume task in '{task.work_status.value}' state."
            )
        task.work_status = WorkStatus.QUEUED
        task.updated_at = _now()
        self._record_transition(task_id, WorkStatus.ON_HOLD.value, WorkStatus.QUEUED.value, actor)
        return deepcopy(task)

    # ------------------------------------------------------------------
    # Flow progression
    # ------------------------------------------------------------------

    def node_done(
        self,
        task_id: str,
        actor: str,
        work_output: WorkOutput | dict | None = None,
        skip_gates: bool = False,
    ) -> Task:
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(f"Task '{task_id}' not found.")
        # #777 — REWORK is a valid source state for node_done; the
        # worker re-runs the implement node after rejection, calls
        # node_done, and the task advances normally.
        if task.work_status not in (WorkStatus.IN_PROGRESS, WorkStatus.REWORK):
            raise InvalidTransitionError(
                f"Cannot complete node on task in '{task.work_status.value}' state."
            )

        flow = self._resolve_flow(task.flow_template_id)
        node = flow.nodes.get(task.current_node_id or "")
        if node is None:
            raise InvalidTransitionError("Task has no current flow node.")
        if node.type != NodeType.WORK:
            raise InvalidTransitionError(
                f"Current node is not a work node (type: {node.type.value})."
            )

        # Coerce work_output from dict if needed
        if isinstance(work_output, dict):
            work_output = self._coerce_work_output(work_output)
        if work_output is None:
            raise ValidationError("Work output is required for node_done.")

        # Complete current execution
        exes = self._executions.get(task_id, [])
        for exe in reversed(exes):
            if exe.node_id == task.current_node_id and exe.status == ExecutionStatus.ACTIVE:
                exe.status = ExecutionStatus.COMPLETED
                exe.work_output = work_output
                exe.completed_at = _now()
                break

        # Advance
        self._advance_to_node(task, flow, node.next_node_id, actor, task.work_status)
        return deepcopy(task)

    def approve(self, task_id: str, actor: str, reason: str | None = None, skip_gates: bool = False, resume_merge: bool = False) -> Task:
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(f"Task '{task_id}' not found.")
        if task.work_status != WorkStatus.REVIEW:
            raise InvalidTransitionError(
                f"Cannot approve task in '{task.work_status.value}' state."
            )

        flow = self._resolve_flow(task.flow_template_id)
        node = flow.nodes.get(task.current_node_id or "")
        if node is None or node.type != NodeType.REVIEW:
            raise InvalidTransitionError("Current node is not a review node.")

        # Complete current execution
        exes = self._executions.get(task_id, [])
        for exe in reversed(exes):
            if exe.node_id == task.current_node_id and exe.status == ExecutionStatus.ACTIVE:
                exe.status = ExecutionStatus.COMPLETED
                exe.decision = Decision.APPROVED
                exe.decision_reason = reason
                exe.completed_at = _now()
                break

        self._advance_to_node(task, flow, node.next_node_id, actor, task.work_status)
        return deepcopy(task)

    def reject(self, task_id: str, actor: str, reason: str) -> Task:
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(f"Task '{task_id}' not found.")
        if task.work_status != WorkStatus.REVIEW:
            raise InvalidTransitionError(
                f"Cannot reject task in '{task.work_status.value}' state."
            )

        flow = self._resolve_flow(task.flow_template_id)
        node = flow.nodes.get(task.current_node_id or "")
        if node is None or node.type != NodeType.REVIEW:
            raise InvalidTransitionError("Current node is not a review node.")

        if not reason or not reason.strip():
            raise ValidationError("Reason is required for rejection.")

        # Complete current execution
        exes = self._executions.get(task_id, [])
        for exe in reversed(exes):
            if exe.node_id == task.current_node_id and exe.status == ExecutionStatus.ACTIVE:
                exe.status = ExecutionStatus.COMPLETED
                exe.decision = Decision.REJECTED
                exe.decision_reason = reason
                exe.completed_at = _now()
                break

        # Move to reject node
        reject_node_id = node.reject_node_id
        if reject_node_id is None:
            raise InvalidTransitionError("Review node has no reject_node defined.")

        reject_node = flow.nodes.get(reject_node_id)
        if reject_node is None:
            raise InvalidTransitionError(f"Reject node '{reject_node_id}' not found.")

        old = task.work_status.value
        # #777 — explicit REWORK state instead of bouncing back to
        # IN_PROGRESS. Mirror of the SQLite path.
        task.work_status = WorkStatus.REWORK
        task.current_node_id = reject_node_id
        task.updated_at = _now()

        # Create new execution at reject target
        visit = sum(1 for e in self._executions.get(task_id, []) if e.node_id == reject_node_id) + 1
        exe = FlowNodeExecution(
            task_id=task_id,
            node_id=reject_node_id,
            visit=visit,
            status=ExecutionStatus.ACTIVE,
            started_at=_now(),
        )
        self._executions.setdefault(task_id, []).append(exe)
        self._record_transition(task_id, old, WorkStatus.IN_PROGRESS.value, actor, reason)
        return deepcopy(task)

    def block(self, task_id: str, actor: str, blocker_task_id: str) -> Task:
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(f"Task '{task_id}' not found.")
        if blocker_task_id not in self._tasks:
            raise TaskNotFoundError(f"Task '{blocker_task_id}' not found.")
        if task.work_status not in (WorkStatus.IN_PROGRESS, WorkStatus.REVIEW):
            raise InvalidTransitionError(
                f"Cannot block task in '{task.work_status.value}' state."
            )
        old = task.work_status.value
        task.work_status = WorkStatus.BLOCKED
        task.updated_at = _now()
        self._record_transition(task_id, old, WorkStatus.BLOCKED.value, actor, f"Blocked by {blocker_task_id}")
        return deepcopy(task)

    def get_execution(
        self,
        task_id: str,
        node_id: str | None = None,
        visit: int | None = None,
    ) -> list[FlowNodeExecution]:
        exes = self._executions.get(task_id, [])
        if node_id is not None:
            exes = [e for e in exes if e.node_id == node_id]
        if visit is not None:
            exes = [e for e in exes if e.visit == visit]
        return list(exes)

    # ------------------------------------------------------------------
    # Context
    # ------------------------------------------------------------------

    def add_context(
        self,
        task_id: str,
        actor: str,
        text: str,
        *,
        entry_type: str = "note",
    ) -> ContextEntry:
        if task_id not in self._tasks:
            raise TaskNotFoundError(f"Task '{task_id}' not found.")
        entry = ContextEntry(
            actor=actor, timestamp=_now(), text=text, entry_type=entry_type,
        )
        self._context.setdefault(task_id, []).append(entry)
        return entry

    def get_context(
        self,
        task_id: str,
        limit: int | None = None,
        since: str | None = None,
        entry_type: str | None = None,
    ) -> list[ContextEntry]:
        entries = list(reversed(self._context.get(task_id, [])))
        if since is not None:
            since_dt = datetime.fromisoformat(since)
            entries = [e for e in entries if e.timestamp > since_dt]
        if entry_type is not None:
            entries = [e for e in entries if e.entry_type == entry_type]
        if limit is not None:
            entries = entries[:limit]
        return entries

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------

    def link(self, from_id: str, to_id: str, kind: str) -> None:
        try:
            LinkKind(kind)
        except ValueError:
            raise ValidationError(f"Invalid link kind '{kind}'.")
        for tid in (from_id, to_id):
            if tid not in self._tasks:
                raise TaskNotFoundError(f"Task '{tid}' not found.")
        self._links.append((from_id, to_id, kind))

    def unlink(self, from_id: str, to_id: str, kind: str) -> None:
        try:
            LinkKind(kind)
        except ValueError:
            raise ValidationError(f"Invalid link kind '{kind}'.")
        self._links = [
            (f, t, k) for f, t, k in self._links
            if not (f == from_id and t == to_id and k == kind)
        ]

    def dependents(self, task_id: str) -> list[Task]:
        visited: set[str] = set()
        queue = [task_id]
        while queue:
            current = queue.pop(0)
            for f, t, k in self._links:
                if f == current and k == "blocks" and t not in visited:
                    visited.add(t)
                    queue.append(t)
        return [self.get(tid) for tid in visited]

    # ------------------------------------------------------------------
    # Flows
    # ------------------------------------------------------------------

    def available_flows(self, project: str | None = None) -> list[FlowTemplate]:
        from pollypm.work.flow_engine import available_flows as _available_flows
        flow_map = _available_flows(self._project_path)
        templates = []
        for name in flow_map:
            try:
                templates.append(self._resolve_flow(name))
            except Exception:
                pass
        return templates

    def get_flow(self, name: str, project: str | None = None) -> FlowTemplate:
        return self._resolve_flow(name)

    def validate_advance(self, task_id: str, actor: str) -> list[GateResult]:
        task = self.get(task_id)
        if task.current_node_id is None:
            return []
        flow = self._resolve_flow(task.flow_template_id)
        node = flow.nodes.get(task.current_node_id)
        if node is None or not node.gates:
            return []
        return evaluate_gates(task, node.gates, self._gate_registry, get_task=self.get)

    # ------------------------------------------------------------------
    # Sync stubs
    # ------------------------------------------------------------------

    def sync_status(self, task_id: str) -> dict[str, object]:
        return {}

    def trigger_sync(
        self,
        task_id: str | None = None,
        adapter: str | None = None,
    ) -> dict[str, object]:
        return {}

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def state_counts(self, project: str | None = None) -> dict[str, int]:
        counts = {s.value: 0 for s in WorkStatus}
        for task in self._tasks.values():
            if project is not None and task.project != project:
                continue
            counts[task.work_status.value] += 1
        return counts

    def my_tasks(self, agent: str) -> list[Task]:
        result = []
        for task in self._tasks.values():
            if task.assignee == agent and task.current_node_id is not None:
                result.append(deepcopy(task))
        return result

    def blocked_tasks(self, project: str | None = None) -> list[Task]:
        result = []
        for task in self._tasks.values():
            if task.work_status == WorkStatus.BLOCKED:
                if project is None or task.project == project:
                    result.append(deepcopy(task))
        return result

    # ------------------------------------------------------------------
    # Worker sessions
    # ------------------------------------------------------------------

    def ensure_worker_session_schema(self) -> None:
        # Mock store is in-memory; nothing to prepare.
        return None

    def _row_to_record(self, row: dict) -> WorkerSessionRecord:
        return WorkerSessionRecord(
            task_project=row["task_project"],
            task_number=int(row["task_number"]),
            agent_name=row["agent_name"],
            pane_id=row.get("pane_id"),
            worktree_path=row.get("worktree_path"),
            branch_name=row.get("branch_name"),
            started_at=row["started_at"],
            ended_at=row.get("ended_at"),
            total_input_tokens=int(row.get("total_input_tokens", 0)),
            total_output_tokens=int(row.get("total_output_tokens", 0)),
            archive_path=row.get("archive_path"),
            provider=row.get("provider"),
            provider_home=row.get("provider_home"),
        )

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
        provider: str | None = None,
        provider_home: str | None = None,
    ) -> None:
        key = (task_project, task_number)
        existing = self._worker_sessions.get(key, {})
        existing.update(
            {
                "task_project": task_project,
                "task_number": task_number,
                "agent_name": agent_name,
                "pane_id": pane_id,
                "worktree_path": worktree_path,
                "branch_name": branch_name,
                "started_at": started_at,
                "ended_at": None,
                "archive_path": None,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "provider": provider,
                "provider_home": provider_home,
            }
        )
        self._worker_sessions[key] = existing

    def get_worker_session(
        self,
        *,
        task_project: str,
        task_number: int,
        active_only: bool = False,
    ) -> WorkerSessionRecord | None:
        row = self._worker_sessions.get((task_project, task_number))
        if row is None:
            return None
        if active_only and row.get("ended_at") is not None:
            return None
        return self._row_to_record(row)

    def list_worker_sessions(
        self,
        *,
        project: str | None = None,
        active_only: bool = True,
    ) -> list[WorkerSessionRecord]:
        out: list[WorkerSessionRecord] = []
        for row in self._worker_sessions.values():
            if active_only and row.get("ended_at") is not None:
                continue
            if project is not None and row["task_project"] != project:
                continue
            out.append(self._row_to_record(row))
        return out

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
        key = (task_project, task_number)
        row = self._worker_sessions.get(key)
        if row is None:
            return
        row["ended_at"] = ended_at
        row["total_input_tokens"] = total_input_tokens
        row["total_output_tokens"] = total_output_tokens
        row["archive_path"] = archive_path

    def update_worker_session_tokens(
        self,
        *,
        task_project: str,
        task_number: int,
        total_input_tokens: int,
        total_output_tokens: int,
        archive_path: str | None,
    ) -> None:
        key = (task_project, task_number)
        row = self._worker_sessions.get(key)
        if row is None:
            return
        row["total_input_tokens"] = total_input_tokens
        row["total_output_tokens"] = total_output_tokens
        row["archive_path"] = archive_path

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _advance_to_node(
        self,
        task: Task,
        flow: FlowTemplate,
        next_node_id: str | None,
        actor: str,
        from_status: WorkStatus,
    ) -> None:
        if next_node_id is None:
            raise InvalidTransitionError("No next node defined.")

        next_node = flow.nodes.get(next_node_id)
        if next_node is None:
            raise InvalidTransitionError(f"Next node '{next_node_id}' not found.")

        now = _now()
        old = from_status.value

        if next_node.type == NodeType.TERMINAL:
            task.work_status = WorkStatus.DONE
            task.current_node_id = None
            task.updated_at = now
            self._record_transition(task.task_id, old, WorkStatus.DONE.value, actor)
        elif next_node.type in (NodeType.REVIEW, NodeType.WORK):
            new_status = WorkStatus.REVIEW if next_node.type == NodeType.REVIEW else WorkStatus.IN_PROGRESS
            task.work_status = new_status
            task.current_node_id = next_node_id
            task.updated_at = now

            visit = sum(1 for e in self._executions.get(task.task_id, []) if e.node_id == next_node_id) + 1
            exe = FlowNodeExecution(
                task_id=task.task_id,
                node_id=next_node_id,
                visit=visit,
                status=ExecutionStatus.ACTIVE,
                started_at=now,
            )
            self._executions.setdefault(task.task_id, []).append(exe)
            self._record_transition(task.task_id, old, new_status.value, actor)

    def _record_transition(
        self,
        task_id: str,
        from_state: str,
        to_state: str,
        actor: str,
        reason: str | None = None,
    ) -> None:
        t = Transition(
            from_state=from_state,
            to_state=to_state,
            actor=actor,
            timestamp=_now(),
            reason=reason,
        )
        self._transitions.setdefault(task_id, []).append(t)

    @staticmethod
    def _coerce_work_output(d: dict) -> WorkOutput:
        artifacts = [
            Artifact(
                kind=ArtifactKind(a["kind"]) if isinstance(a.get("kind"), str) else a.get("kind", ArtifactKind.NOTE),
                description=a.get("description", ""),
                ref=a.get("ref"),
                path=a.get("path"),
                external_ref=a.get("external_ref"),
            )
            for a in d.get("artifacts", [])
        ]
        out_type = d.get("type", OutputType.CODE_CHANGE)
        if isinstance(out_type, str):
            out_type = OutputType(out_type)
        return WorkOutput(type=out_type, summary=d.get("summary", ""), artifacts=artifacts)
