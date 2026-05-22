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
        kind: str = "legacy",
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

        ``kind`` (#1565) stamps the structured inbox-item
        discriminator. Defaults to ``"legacy"`` until emit sites are
        retrained (#1567 / #1568).
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
        """Update mutable fields on a task.

        Low-level column writer. Accepted fields are ``title``,
        ``description``, ``priority``, ``labels``, ``roles``,
        ``acceptance_criteria``, ``constraints``, ``relevant_files``,
        ``assignee``, and ``external_refs``. Cannot change
        ``work_status`` or ``flow_template`` directly — use lifecycle
        methods instead.

        ``assignee`` is accepted by the column schema for symmetry
        with the legacy SQLite backend, but **no operator surface
        exposes a breadcrumb-less assignee write** (#2064 round-9
        blocker #5). The CLI ``pm task update`` does not advertise an
        ``--assignee`` flag, the API ``PATCH /tasks/{p}/{n}`` body
        (``TaskPatchRequest``) refuses the ``assignee`` key with
        ``extra='forbid'``, and ``POST /tasks/{p}/{n}/reassign`` now
        routes exclusively through :meth:`reassign_task`. The single
        operator path for changing ``assignee`` is therefore
        :meth:`reassign_task`, which writes the column AND appends a
        ``reassignment`` breadcrumb in one transaction so the new
        owner can recover context via ``pm task get`` (spec §P-9).
        ``update(assignee=...)`` remains callable from in-process
        plugins / migrations that explicitly accept the audit gap.

        ``external_refs`` replaces the dict wholesale (pass ``{}`` to
        clear); it carries the API's free-form ``metadata`` surface.

        Both ``PgWorkService`` and ``MockWorkService`` must accept the
        same field set so backend swaps don't surface a contract gap.
        """
        ...

    def reassign_task(
        self,
        task_id: str,
        *,
        new_assignee: str,
        actor: str,
        reason: str | None = None,
    ) -> Task:
        """Mid-flight reassign: update ``assignee`` AND append context entry atomically.

        Implements the work-service spec §P-9 invariant: when a live
        worker is swapped, the new owner needs a breadcrumb in the
        context log so they can recover context via ``pm task get``.
        The column write and the context-log row commit in a single
        transaction; failure of either rolls both back.

        ``actor`` is the operator/agent who initiated the reassignment
        (recorded on the context entry). ``reason``, when present, is
        appended to the breadcrumb body.
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

    def list_replies(self, task_id: str) -> list[ContextEntry]:
        """Return reply entries for ``task_id`` oldest-first (#1812).

        Thin wrapper around :meth:`get_context` with
        ``entry_type='reply'`` plus a reversal so the inbox detail pane
        renders the thread in natural reading order. Pinned on the
        protocol so cockpit code never has to ``hasattr`` past a
        backend that forgot to implement it.
        """
        ...

    def bulk_list_replies(self, *, project: str) -> dict[int, list[ContextEntry]]:
        """Return ``task_number -> [reply entries (oldest first)]`` (#1812).

        One project-wide query, bucketed in Python. Replaces a per-task
        :meth:`list_replies` loop on the inbox loader hot path. Pinned
        on the protocol so the bulk path is part of the WorkService
        contract rather than a backend-specific optimisation.
        """
        ...

    def latest_snoozes_bulk(
        self, task_keys: list[tuple[str, int]],
    ) -> dict[tuple[str, int], ContextEntry]:
        """Return ``{(project, task_number): latest_snooze_entry}`` (#2060).

        Single-query bulk fetch of the most-recent ``entry_type='snooze'``
        row per task. Replaces the per-task
        ``get_context(entry_type='snooze', limit=1)`` loop the inbox
        list path was running on a user-facing scan. Tasks with no
        snooze rows are absent from the returned mapping. An empty
        ``task_keys`` short-circuits without a query.

        Pinned on the protocol so the bulk path is part of the
        contract (not a backend-specific optimisation that drifts).
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

    def reserve_worker_cap_slot(
        self,
        *,
        task_project: str,
        task_number: int,
        agent_name: str,
        started_at: str,
        cap: int,
    ) -> bool:
        """Atomically reserve a per-project worker cap slot (#1883).

        Performs the cap check (active-row count vs ``cap``) AND the
        placeholder row insert in a single transaction, gated by a
        project-scoped advisory lock so two concurrent claims for
        different ``task_number``\\s on the same project serialise on
        the count.

        Returns ``True`` when a slot was reserved (a placeholder row
        with ``pane_id=""`` is now visible to subsequent counts).
        Returns ``False`` when the project is already at cap — the
        caller (typically ``SessionManager.provision_worker``) raises
        :class:`pollypm.work.session_manager.WorkerCapExceededError`.

        Idempotent for the same ``(task_project, task_number)``: a
        re-claim against an already-active row returns ``True`` without
        consuming an additional slot — the caller's existing-session
        short-circuit handles this case upstream, but the work-service
        contract is explicit so a test harness racing two calls can't
        accidentally double-count.

        The placeholder row is upgraded to the real binding by the
        subsequent :meth:`upsert_worker_session` call once tmux /
        worktree provisioning succeeds. On provisioning failure the
        caller should call :meth:`mark_worker_session_ended` so the
        slot is freed for the next claim.
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
