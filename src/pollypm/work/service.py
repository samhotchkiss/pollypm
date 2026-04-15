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
        description: str,
        type: str,
        project: str,
        flow_template: str,
        roles: dict[str, str],
        priority: str,
        acceptance_criteria: str | None = None,
        constraints: str | None = None,
        relevant_files: list[str] | None = None,
        labels: list[str] | None = None,
        requires_human_review: bool = False,
    ) -> Task:
        """Create a task in ``draft`` state.

        Validates that all required roles for the chosen flow are filled.
        """
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

    def queue(self, task_id: str, actor: str) -> Task:
        """Move a task from ``draft`` to ``queued``.

        If ``requires_human_review`` is set, validates that the human has
        approved via inbox.
        """
        ...

    def claim(self, task_id: str, actor: str) -> Task:
        """Atomically set assignee, activate first flow node, and set status to ``in_progress``.

        The task must currently be ``queued``.
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

    def node_done(self, task_id: str, actor: str, work_output: WorkOutput) -> Task:
        """Signal that the current work node is complete.

        Validates that a work output is present, then advances the flow to
        ``next_node``.  Updates ``work_status`` based on the next node type.
        """
        ...

    def approve(self, task_id: str, actor: str, reason: str | None = None) -> Task:
        """Approve at a review node.

        Advances to ``next_node``.  If the target is terminal the task
        becomes ``done``.
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

    def add_context(self, task_id: str, actor: str, text: str) -> ContextEntry:
        """Append an entry to the task's context log."""
        ...

    def get_context(
        self,
        task_id: str,
        limit: int | None = None,
        since: str | None = None,
    ) -> list[ContextEntry]:
        """Read context entries, most recent first."""
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
