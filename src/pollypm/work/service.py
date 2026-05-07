"""WorkService protocol definition.

Defines the interface that any work service implementation must satisfy.
Method bodies are not implemented here -- only signatures and docstrings.
"""

from __future__ import annotations

from typing import Protocol

from pollypm.work.models import (
    ContextEntry,
    FlowNodeExecution,
    FlowTemplate,
    GateResult,
    Task,
    WorkerSessionRecord,
    WorkOutput,
)


class WorkService(Protocol):
    """Sealed work-management service.

    All mutations are serialised through a single-writer daemon.
    Implementations must satisfy every method listed here.
    """

    # ------------------------------------------------------------------
    # Task lifecycle
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
    ) -> Task:
        """Create a task in ``draft`` state.

        Validates that all required roles for the chosen flow are filled.

        ``created_by`` records the author of the task. Defaults to
        ``"system"`` for orchestrator-spawned work; CLI / API callers
        should pass a real actor (#796).

        ``predecessor_task_id`` (#1398) wires the new task as the
        successor of an earlier attempt (replan flow). Defaults to
        ``None`` (no predecessor — original task). Setting the value
        emits a ``plan.successor_created`` audit event.
        """
        ...

    def increment_plan_version(
        self,
        task_id: str,
        *,
        actor: str = "system",
        reason: str | None = None,
    ) -> Task:
        """Bump ``plan_version`` on a plan task and emit an audit event (#1398).

        Used by the plan-refinement flow when an architect updates a
        plan in place. Emits ``plan.version_incremented`` with the
        old/new version pair so plan-history consumers can reconstruct
        the revision timeline.
        """
        ...

    def list_successors(self, predecessor_task_id: str) -> list[Task]:
        """Return tasks that descend from ``predecessor_task_id`` (#1398)."""
        ...

    def get(self, task_id: str) -> Task:
        """Read a task with all fields including current flow node and execution state."""
        ...

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
        ...

    def queue(self, task_id: str, actor: str, skip_gates: bool = False) -> Task:
        """Move a task from ``draft`` to ``queued``.

        If ``requires_human_review`` is set, validates that the human has
        approved via inbox.

        ``skip_gates`` bypasses gate evaluation when the caller has
        explicit authority (``pm task queue --skip-gates``). Implementations
        must record the bypass on the resulting transition (#796).
        """
        ...

    def claim(self, task_id: str, actor: str, skip_gates: bool = False) -> Task:
        """Atomically set assignee, activate first flow node, and set status to ``in_progress``.

        The task must currently be ``queued``.

        ``skip_gates`` mirrors :meth:`queue` — used by ``pm task claim
        --skip-gates`` for repair flows where the operator has decided
        to bypass advancement guards (#796).
        """
        ...

    def next(self, *, agent: str | None = None, project: str | None = None) -> Task | None:
        """Return the highest-priority queued and unblocked task.

        Optionally filtered by project.  Does **not** claim it.
        """
        ...

    def update(self, task_id: str, **fields: object) -> Task:
        """Update mutable fields (title, description, priority, labels, roles).

        Cannot change ``work_status`` directly -- use lifecycle methods instead.
        """
        ...

    def cancel(self, task_id: str, actor: str, reason: str) -> Task:
        """Move any non-terminal task to ``cancelled``."""
        ...

    def hold(self, task_id: str, actor: str, reason: str | None = None) -> Task:
        """Move an ``in_progress`` or ``queued`` task to ``on_hold``."""
        ...

    def resume(self, task_id: str, actor: str) -> Task:
        """Move an ``on_hold`` task back to ``queued``."""
        ...

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
        """Signal that the current work node is complete.

        Validates that a work output is present, then advances the flow to
        ``next_node``.  Updates ``work_status`` based on the next node type.

        ``work_output`` accepts either the typed dataclass or a dict
        shape for callers that don't share the import; implementations
        coerce on the way in. ``skip_gates`` bypasses advancement
        guards (#796).
        """
        ...

    def approve(
        self,
        task_id: str,
        actor: str,
        reason: str | None = None,
        skip_gates: bool = False,
        resume_merge: bool = False,
    ) -> Task:
        """Approve at a review node.

        Advances to ``next_node``.  If the target is terminal the task
        becomes ``done``.

        ``skip_gates`` bypasses gate evaluation (#796).
        ``resume_merge`` lets a caller continue after hand-resolving a
        non-safelist merge conflict raised by a previous approve attempt
        (#925).
        """
        ...

    def reject(self, task_id: str, actor: str, reason: str) -> Task:
        """Reject at a review node.

        Moves to ``reject_node``.  Reason is required.  Creates a new
        execution record (visit N+1) at the target node.
        """
        ...

    def block(self, task_id: str, actor: str, blocker_task_id: str) -> Task:
        """Mark a task as blocked by another task.

        Sets ``work_status`` to ``blocked``.  The flow stays at the current
        node.
        """
        ...

    def get_execution(
        self,
        task_id: str,
        node_id: str | None = None,
        visit: int | None = None,
    ) -> list[FlowNodeExecution]:
        """Read execution records, optionally filtered by node and/or visit."""
        ...

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
        """Append an entry to the task's context log.

        ``entry_type`` classifies the row. ``"note"`` is the generic
        context-log default (mirrors prior behaviour); inbox flows
        also use ``"reply"`` and ``"read"`` markers (#796).
        """
        ...

    def get_context(
        self,
        task_id: str,
        limit: int | None = None,
        since: str | None = None,
        entry_type: str | None = None,
    ) -> list[ContextEntry]:
        """Read context entries, most recent first.

        When ``entry_type`` is supplied, restricts the result to rows
        of that classification — pass ``"reply"`` for the inbox thread
        view, ``"read"`` for read markers, ``None`` for every row (#796).
        """
        ...

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------

    def link(self, from_id: str, to_id: str, kind: str) -> None:
        """Create a relationship between two tasks.

        Kind is one of ``blocks``, ``relates_to``, ``supersedes``, ``parent``.
        Validates both tasks exist.  For ``blocks``, checks for circular
        dependencies.
        """
        ...

    def unlink(self, from_id: str, to_id: str, kind: str) -> None:
        """Remove a relationship between two tasks."""
        ...

    def dependents(self, task_id: str) -> list[Task]:
        """Return all tasks blocked by this task (transitively)."""
        ...

    # ------------------------------------------------------------------
    # Flows
    # ------------------------------------------------------------------

    def available_flows(self, project: str | None = None) -> list[FlowTemplate]:
        """List all flows after override resolution.

        If *project* is specified, includes project-local flows.
        """
        ...

    def get_flow(self, name: str, project: str | None = None) -> FlowTemplate:
        """Resolve a flow by name through the override chain."""
        ...

    def validate_advance(self, task_id: str, actor: str) -> list[GateResult]:
        """Dry-run: can this actor advance the current node?

        Returns pass/fail with reasons for each gate.
        """
        ...

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    def sync_status(self, task_id: str) -> dict[str, object]:
        """Current sync state per adapter."""
        ...

    def trigger_sync(
        self,
        task_id: str | None = None,
        adapter: str | None = None,
    ) -> dict[str, object]:
        """Force a sync cycle.  Optional filters."""
        ...

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def state_counts(self, project: str | None = None) -> dict[str, int]:
        """Task counts by state.  For dashboards."""
        ...

    def my_tasks(self, agent: str) -> list[Task]:
        """All tasks where *agent* fills a role that owns the current state."""
        ...

    def blocked_tasks(self, project: str | None = None) -> list[Task]:
        """All tasks in a non-terminal state with unresolved blockers."""
        ...

    # ------------------------------------------------------------------
    # Worker sessions (binding tasks to tmux/worktree sessions)
    # ------------------------------------------------------------------

    def ensure_worker_session_schema(self) -> None:
        """Idempotently create the persistence schema worker sessions need.

        Invoked by :class:`pollypm.work.session_manager.SessionManager` at
        construction. Implementations may no-op if the schema ships with the
        main tables.
        """
        ...

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
        """Record a new worker-session binding or resurrect an ended one.

        Resurrecting clears ``ended_at``, ``archive_path`` and the token
        counters so the row is reusable after cancel → re-claim.

        ``provider`` (``claude``/``codex``/...) and ``provider_home``
        (``CLAUDE_CONFIG_DIR`` / ``CODEX_HOME``) are persisted at launch
        so per-task transcript archival can locate the right tree at
        teardown without depending on the ambient process env (#809).
        """
        ...

    def get_worker_session(
        self,
        *,
        task_project: str,
        task_number: int,
        active_only: bool = False,
    ) -> WorkerSessionRecord | None:
        """Return the binding row for a task, or ``None`` if absent.

        When ``active_only`` is true, rows with a non-null ``ended_at`` are
        filtered out.
        """
        ...

    def list_worker_sessions(
        self,
        *,
        project: str | None = None,
        active_only: bool = True,
    ) -> list[WorkerSessionRecord]:
        """Return worker-session bindings, optionally filtered by project."""
        ...

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
        """Stamp ``ended_at`` and final accounting on a worker session."""
        ...

    def mark_worker_session_ended(
        self,
        *,
        task_project: str,
        task_number: int,
        ended_at: str,
    ) -> None:
        """Stamp ``ended_at`` without touching token counters (#1014).

        Used by the orphan-reap path in ``provision_worker`` so a
        crash-recovery doesn't zero the token totals an earlier session
        wrote. Implementations may default to ``end_worker_session``
        with the existing counters when they don't track token
        accumulation.
        """
        ...

    def update_worker_session_tokens(
        self,
        *,
        task_project: str,
        task_number: int,
        total_input_tokens: int,
        total_output_tokens: int,
        archive_path: str | None,
    ) -> None:
        """Record partial accounting when teardown could not kill the pane.

        Leaves ``ended_at`` untouched so a future sweep can retry.
        """
        ...
