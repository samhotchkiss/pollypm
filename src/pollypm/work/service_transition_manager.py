"""Transition manager for the SQLite work service.

Contract:
- Inputs: a ``SQLiteWorkService`` plus task ids, actor names, and flow
  payloads for task state transitions.
- Outputs: updated ``Task`` models after the transition commits.
- Side effects: owns the validate / execute / post-commit boundary for
  work state changes.
- Invariants: the manager delegates all persistence and domain-specific
  checks back to the service so behavior stays centralized, but it keeps
  the transition orchestration in one place.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from pollypm.work.gates import evaluate_gates, has_hard_failure
from pollypm.work.models import (
    Decision,
    ExecutionStatus,
    LinkKind,
    NodeType,
    Task,
    TERMINAL_STATUSES,
    WorkOutput,
    WorkStatus,
)
from pollypm.work.service_support import (
    InvalidTransitionError,
    ValidationError,
    _now,
    _parse_task_id,
)

if TYPE_CHECKING:
    from pollypm.work.sqlite_service import SQLiteWorkService

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class WorkTransitionManager:
    """Group the state-transition operations for ``SQLiteWorkService``."""

    service: "SQLiteWorkService"

    def _commit(self, mutate: Callable[[], None]) -> None:
        try:
            mutate()
            self.service._conn.commit()
        except Exception:
            self.service._conn.rollback()
            raise

    def _finish(
        self,
        task_id: str,
        old_status: str,
        *,
        new_status: str | None = None,
        before_reload: Callable[[], None] | None = None,
        after_reload: Callable[[Task], None] | None = None,
        after_sync: Callable[[Task], None] | None = None,
    ) -> Task:
        if before_reload is not None:
            before_reload()
        result = self.service.get(task_id)
        if after_reload is not None:
            after_reload(result)
        self.service._sync_transition(
            result,
            old_status,
            new_status or result.work_status.value,
        )
        if after_sync is not None:
            after_sync(result)
        return result

    def queue(self, task_id: str, actor: str, skip_gates: bool = False) -> Task:
        task = self.service.get(task_id)

        if task.work_status != WorkStatus.DRAFT:
            raise InvalidTransitionError(
                f"Cannot queue task in '{task.work_status.value}' state. "
                f"Task must be in 'draft' state."
            )

        if task.requires_human_review and not skip_gates:
            raise InvalidTransitionError(
                "Task requires human review before queueing. "
                "Pass skip_gates=True to bypass this check once approval "
                "has been obtained out-of-band (inbox integration pending)."
            )

        gate_results = evaluate_gates(
            task,
            ["has_description"],
            self.service._gate_registry,
            **self.service._gate_kwargs(),
        )
        if not skip_gates and has_hard_failure(gate_results):
            failing = [r for r in gate_results if not r.passed]
            raise ValidationError(
                f"Cannot queue task: gate failed — {failing[0].reason}"
            )

        now = _now()
        gate_reason = (
            self.service._gate_skip_reason(gate_results) if skip_gates else None
        )

        self._commit(
            lambda: (
                self.service._record_transition(
                    task.project,
                    task.task_number,
                    WorkStatus.DRAFT.value,
                    WorkStatus.QUEUED.value,
                    actor,
                    reason=gate_reason,
                ),
                self.service._conn.execute(
                    "UPDATE work_tasks SET work_status = ?, updated_at = ? "
                    "WHERE project = ? AND task_number = ?",
                    (WorkStatus.QUEUED.value, now, task.project, task.task_number),
                ),
            )
        )
        return self._finish(
            task_id,
            WorkStatus.DRAFT.value,
            before_reload=lambda: self.service._maybe_block(task_id),
        )

    def claim(self, task_id: str, actor: str, skip_gates: bool = False) -> Task:
        task = self.service.get(task_id)

        if task.work_status != WorkStatus.QUEUED:
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

        flow = self.service._load_flow_from_db(
            task.flow_template_id,
            task.flow_template_version,
        )
        start_node = flow.start_node
        start_node_cfg = flow.nodes.get(start_node)
        assignee = self.service._resolve_node_assignee(task, start_node_cfg) or actor
        now = _now()

        self._commit(
            lambda: (
                self.service._conn.execute(
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
                ),
                self.service._conn.execute(
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
                ),
                self.service._record_transition(
                    task.project,
                    task.task_number,
                    WorkStatus.QUEUED.value,
                    WorkStatus.IN_PROGRESS.value,
                    actor,
                ),
            )
        )

        def _after_sync(_: Task) -> None:
            self.service.last_provision_error = None
            if self.service._session_mgr is not None:
                try:
                    self.service._session_mgr.provision_worker(task_id, actor)
                except Exception as exc:  # noqa: BLE001
                    self.service.last_provision_error = str(exc)
                    logger.warning(
                        "provision_worker failed for %s (actor=%s): %s",
                        task_id,
                        actor,
                        exc,
                    )

        result = self._finish(
            task_id,
            WorkStatus.QUEUED.value,
            after_sync=_after_sync,
        )
        return result

    def cancel(self, task_id: str, actor: str, reason: str) -> Task:
        task = self.service.get(task_id)

        if task.work_status in TERMINAL_STATUSES:
            raise InvalidTransitionError(
                f"Cannot cancel task in terminal state '{task.work_status.value}'."
            )

        now = _now()
        self._commit(
            lambda: (
                self.service._record_transition(
                    task.project,
                    task.task_number,
                    task.work_status.value,
                    WorkStatus.CANCELLED.value,
                    actor,
                    reason,
                ),
                self.service._conn.execute(
                    "UPDATE work_tasks SET work_status = ?, updated_at = ? "
                    "WHERE project = ? AND task_number = ?",
                    (
                        WorkStatus.CANCELLED.value,
                        now,
                        task.project,
                        task.task_number,
                    ),
                ),
            )
        )

        def _before_reload() -> None:
            self.service._on_cancelled(task_id)
            if self.service._session_mgr is not None:
                try:
                    self.service._session_mgr.teardown_worker(task_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "teardown_worker on cancel failed for %s: %s",
                        task_id,
                        exc,
                    )

        return self._finish(
            task_id,
            task.work_status.value,
            new_status=WorkStatus.CANCELLED.value,
            before_reload=_before_reload,
            after_sync=lambda result: self.service._prune_cancelled_critique_child(
                result
            ),
        )

    def hold(self, task_id: str, actor: str, reason: str | None = None) -> Task:
        task = self.service.get(task_id)

        if task.work_status not in (WorkStatus.IN_PROGRESS, WorkStatus.QUEUED):
            raise InvalidTransitionError(
                f"Cannot hold task in '{task.work_status.value}' state. "
                f"Task must be in 'in_progress' or 'queued' state."
            )

        now = _now()
        self._commit(
            lambda: (
                self.service._record_transition(
                    task.project,
                    task.task_number,
                    task.work_status.value,
                    WorkStatus.ON_HOLD.value,
                    actor,
                    reason,
                ),
                self.service._conn.execute(
                    "UPDATE work_tasks SET work_status = ?, updated_at = ? "
                    "WHERE project = ? AND task_number = ?",
                    (
                        WorkStatus.ON_HOLD.value,
                        now,
                        task.project,
                        task.task_number,
                    ),
                ),
            )
        )
        return self._finish(
            task_id,
            task.work_status.value,
            new_status=WorkStatus.ON_HOLD.value,
        )

    def resume(self, task_id: str, actor: str) -> Task:
        task = self.service.get(task_id)

        if task.work_status != WorkStatus.ON_HOLD:
            raise InvalidTransitionError(
                f"Cannot resume task in '{task.work_status.value}' state. "
                f"Task must be in 'on_hold' state."
            )

        has_active_execution = False
        if task.current_node_id:
            row = self.service._conn.execute(
                "SELECT 1 FROM work_node_executions "
                "WHERE task_project = ? AND task_number = ? "
                "AND node_id = ? AND status = ?",
                (
                    task.project,
                    task.task_number,
                    task.current_node_id,
                    ExecutionStatus.ACTIVE.value,
                ),
            ).fetchone()
            has_active_execution = row is not None

        target_status = (
            WorkStatus.IN_PROGRESS if has_active_execution else WorkStatus.QUEUED
        )
        now = _now()
        self._commit(
            lambda: (
                self.service._record_transition(
                    task.project,
                    task.task_number,
                    WorkStatus.ON_HOLD.value,
                    target_status.value,
                    actor,
                ),
                self.service._conn.execute(
                    "UPDATE work_tasks SET work_status = ?, updated_at = ? "
                    "WHERE project = ? AND task_number = ?",
                    (target_status.value, now, task.project, task.task_number),
                ),
            )
        )
        return self._finish(
            task_id,
            WorkStatus.ON_HOLD.value,
            new_status=target_status.value,
        )

    def node_done(
        self,
        task_id: str,
        actor: str,
        work_output: WorkOutput | dict | None = None,
        skip_gates: bool = False,
    ) -> Task:
        task = self.service.get(task_id)

        if task.work_status != WorkStatus.IN_PROGRESS:
            raise InvalidTransitionError(
                f"Cannot complete node on task in "
                f"'{task.work_status.value}' state. "
                f"Task must be in 'in_progress' state."
            )

        flow, node = self.service._get_current_flow_node(task)

        if node.type != NodeType.WORK:
            raise InvalidTransitionError(
                f"Current node '{task.current_node_id}' is not a work node "
                f"(type: {node.type.value})."
            )

        self.service._validate_actor_role(task, node, actor)

        if node.gates:
            gate_results = evaluate_gates(
                task,
                node.gates,
                self.service._gate_registry,
                **self.service._gate_kwargs(),
            )
            if not skip_gates and has_hard_failure(gate_results):
                failing = [
                    r for r in gate_results if not r.passed and r.gate_type == "hard"
                ]
                reasons = "; ".join(r.reason for r in failing)
                raise ValidationError(
                    f"Gate check failed on node '{node.name}': {reasons}"
                )

        work_output = self.service._coerce_work_output(work_output)
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
        self.service._validate_work_output(work_output)

        now = _now()
        wo_json = self.service._serialize_work_output(work_output)

        self._commit(
            lambda: (
                self.service._conn.execute(
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
                ),
                self.service._advance_to_node(
                    task,
                    flow,
                    node.next_node_id,
                    actor,
                    WorkStatus.IN_PROGRESS,
                ),
            )
        )

        def _before_sync(result: Task) -> None:
            if result.work_status == WorkStatus.DONE:
                self.service._check_auto_unblock(task_id)
                if self.service._session_mgr is not None:
                    try:
                        self.service._session_mgr.teardown_worker(task_id)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "teardown_worker on done failed for %s: %s",
                            task_id,
                            exc,
                        )
                self.service._on_task_done(task_id, actor)

        return self._finish(
            task_id,
            task.work_status.value,
            after_reload=_before_sync,
        )

    def approve(
        self,
        task_id: str,
        actor: str,
        reason: str | None = None,
        skip_gates: bool = False,
    ) -> Task:
        task = self.service.get(task_id)

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

        flow, node = self.service._get_current_flow_node(task)

        if node.type != NodeType.REVIEW:
            raise InvalidTransitionError(
                f"Current node '{task.current_node_id}' is not a review "
                f"node (type: {node.type.value})."
            )

        self.service._validate_actor_role(task, node, actor)

        if node.gates:
            gate_results = evaluate_gates(
                task,
                node.gates,
                self.service._gate_registry,
                **self.service._gate_kwargs(),
            )
            if not skip_gates and has_hard_failure(gate_results):
                failing = [
                    r for r in gate_results if not r.passed and r.gate_type == "hard"
                ]
                reasons = "; ".join(r.reason for r in failing)
                raise ValidationError(
                    f"Gate check failed on node '{node.name}': {reasons}"
                )

        if task.current_node_id == "code_review" and node.next_node_id == "done":
            self.service._auto_merge_approved_task_branch(task)

        now = _now()

        def _mutate() -> None:
            self.service._conn.execute(
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

            if (
                task.flow_template_id == "plan_project"
                and task.current_node_id == "user_approval"
            ):
                self.service._conn.execute(
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

            self.service._advance_to_node(
                task,
                flow,
                node.next_node_id,
                actor,
                WorkStatus.REVIEW,
            )

        self._commit(_mutate)

        def _after_reload(result: Task) -> None:
            if result.work_status == WorkStatus.DONE:
                self.service._check_auto_unblock(task_id)
                if self.service._session_mgr is not None:
                    try:
                        self.service._session_mgr.teardown_worker(task_id)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "teardown_worker on approve failed for %s: %s",
                            task_id,
                            exc,
                        )
                self.service._on_task_done(task_id, actor)

        return self._finish(
            task_id,
            task.work_status.value,
            after_reload=_after_reload,
        )

    def reject(
        self,
        task_id: str,
        actor: str,
        reason: str,
    ) -> Task:
        task = self.service.get(task_id)

        if task.work_status != WorkStatus.REVIEW:
            raise InvalidTransitionError(
                f"Cannot reject task in '{task.work_status.value}' state. "
                f"Task must be in 'review' state."
            )

        flow, node = self.service._get_current_flow_node(task)

        if node.type != NodeType.REVIEW:
            raise InvalidTransitionError(
                f"Current node '{task.current_node_id}' is not a review "
                f"node."
            )

        self.service._validate_actor_role(task, node, actor)

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

        def _mutate() -> None:
            self.service._conn.execute(
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

            next_visit = self.service._next_visit(
                task.project,
                task.task_number,
                node.reject_node_id,
            )
            reject_assignee = self.service._resolve_node_assignee(task, reject_target)

            self.service._conn.execute(
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

            self.service._conn.execute(
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

            self.service._record_transition(
                task.project,
                task.task_number,
                WorkStatus.REVIEW.value,
                WorkStatus.IN_PROGRESS.value,
                actor,
                reason,
            )

        self._commit(_mutate)

        def _after_sync(result: Task) -> None:
            try:
                from pollypm.rejection_feedback import emit_rejection_feedback

                emit_rejection_feedback(
                    self.service,
                    task=result,
                    reviewer=actor,
                    reason=reason,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "emit_rejection_feedback failed for %s: %s",
                    task_id,
                    exc,
                )
            if self.service._session_mgr is not None:
                try:
                    self.service._session_mgr.notify_rejection(task_id, reason)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("notify_rejection failed for %s: %s", task_id, exc)

        return self._finish(
            task_id,
            WorkStatus.REVIEW.value,
            new_status=WorkStatus.IN_PROGRESS.value,
            after_sync=_after_sync,
        )

    def block(self, task_id: str, actor: str, blocker_task_id: str) -> Task:
        task = self.service.get(task_id)

        if task.work_status not in (
            WorkStatus.IN_PROGRESS,
            WorkStatus.REVIEW,
        ):
            raise InvalidTransitionError(
                f"Cannot block task in '{task.work_status.value}' state. "
                f"Task must be in 'in_progress' or 'review' state."
            )

        self.service.get(blocker_task_id)
        blocker_project, blocker_number = _parse_task_id(blocker_task_id)

        if self.service._would_create_cycle(
            blocker_project,
            blocker_number,
            task.project,
            task.task_number,
        ):
            raise ValidationError("circular dependency detected")

        now = _now()
        old_status = task.work_status

        def _mutate() -> None:
            self.service._conn.execute(
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

            self.service._conn.execute(
                "UPDATE work_tasks SET work_status = ?, updated_at = ? "
                "WHERE project = ? AND task_number = ?",
                (
                    WorkStatus.BLOCKED.value,
                    now,
                    task.project,
                    task.task_number,
                ),
            )

            if task.current_node_id:
                self.service._conn.execute(
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

            self.service._record_transition(
                task.project,
                task.task_number,
                old_status.value,
                WorkStatus.BLOCKED.value,
                actor,
                f"Blocked by {blocker_task_id}",
            )

        self._commit(_mutate)
        return self._finish(
            task_id,
            old_status.value,
            new_status=WorkStatus.BLOCKED.value,
        )

    def mark_done(self, task_id: str, actor: str) -> Task:
        task = self.service.get(task_id)
        if task.work_status in TERMINAL_STATUSES:
            raise InvalidTransitionError(
                f"Cannot mark done task in terminal state '{task.work_status.value}'."
            )

        now = _now()

        self._commit(
            lambda: (
                self.service._record_transition(
                    task.project,
                    task.task_number,
                    task.work_status.value,
                    WorkStatus.DONE.value,
                    actor,
                ),
                self.service._conn.execute(
                    "UPDATE work_tasks SET work_status = ?, updated_at = ? "
                    "WHERE project = ? AND task_number = ?",
                    (
                        WorkStatus.DONE.value,
                        now,
                        task.project,
                        task.task_number,
                    ),
                ),
            )
        )

        def _before_reload() -> None:
            self.service._check_auto_unblock(task_id)
            self.service._on_task_done(task_id, actor)

        return self._finish(
            task_id,
            task.work_status.value,
            new_status=WorkStatus.DONE.value,
            before_reload=_before_reload,
        )
