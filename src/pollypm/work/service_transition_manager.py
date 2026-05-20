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
from typing import Any, Callable

from pollypm.review_notify import notify_requires_review_hold
from pollypm.task_review_summary import store_review_plain_summary
from pollypm.work import task_assignment_alerts
from pollypm.work.gates import evaluate_gates, has_hard_failure
from pollypm.work.models import (
    ActorType,
    Decision,
    ExecutionStatus,
    FlowNode,
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

# ``service`` is typed ``Any`` here because the manager calls into the
# SQLite-specific service internals (``_conn``, ``_record_transition``,
# ``_session_mgr``, etc.) that aren't part of the public ``WorkService``
# Protocol. The previous TYPE_CHECKING import of ``SQLiteWorkService``
# was an IDE hint only; dropping it removes a direct ``sqlite_service``
# import so the pg cutover can delete that module (#1369, #1737).

logger = logging.getLogger(__name__)


_AUTO_HOLD_REASON_PREFIX = "Waiting on operator:"
_EXPECTED_CRITIC_PANEL_ROLES = (
    "critic_simplicity",
    "critic_maintainability",
    "critic_user",
    "critic_operational",
    "critic_security",
)


def _format_csv(values: tuple[str, ...] | list[str]) -> str:
    return ", ".join(values)


# #1026 — phrases in a reject reason that signal a stale-base blocker
# the PM can probably fix by merging main into the worker branch.
_STALE_BASE_REASON_HINTS: tuple[str, ...] = (
    "stale base",
    "stale-base",
    "behind main",
    "rebase against main",
    "rebase onto main",
    "merge main",
    "out of date with main",
    "base is stale",
    "needs rebase",
    "out-of-date base",
)


def _looks_like_stale_base(reason: str) -> bool:
    detail = (reason or "").lower()
    return any(hint in detail for hint in _STALE_BASE_REASON_HINTS)


def _is_cap_exceeded_error(exc: BaseException) -> bool:
    """Return True when ``exc`` is a ``WorkerCapExceededError`` (#1906).

    Walks the exception class hierarchy by qualified name so the
    transition manager doesn't have to import
    ``pollypm.work.session_manager`` at module-load time (the
    session-manager module imports transition helpers in some test
    paths, so a top-level import would risk circularity).
    """
    for cls in type(exc).__mro__:
        if cls.__name__ == "WorkerCapExceededError":
            return True
    return False


@dataclass(slots=True)
class _PMRepairCallSiteOutcome:
    """Internal wrapper that the reject() call site consumes (#1026)."""

    repaired: bool
    recipe_name: str
    diagnosis: str


@dataclass(slots=True)
class WorkTransitionManager:
    """Group the state-transition operations for ``SQLiteWorkService``."""

    service: Any

    def _commit(self, mutate: Callable[[], None]) -> None:
        try:
            mutate()
            self.service._conn.commit()
        except Exception:
            self.service._conn.rollback()
            raise

    def _write_review_summary_after_transition(self, task: Task) -> None:
        if task.work_status not in {WorkStatus.REVIEW, WorkStatus.ON_HOLD}:
            return
        try:
            store_review_plain_summary(self.service, task.task_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "review plain summary generation skipped for %s: %s",
                task.task_id,
                exc,
            )

    def _maybe_pm_auto_repair(
        self, task: Task, reason: str
    ) -> "_PMRepairCallSiteOutcome | None":
        """Run PM auto-repair before a reject (#1026).

        Returns ``None`` when repair is disabled or unavailable; an
        outcome wrapper otherwise. The wrapper carries a ``repaired``
        flag (caller short-circuits the reject) and a diagnosis +
        recipe name (caller enriches the rejection reason).

        Defensive: any exception in the repair scaffold is logged and
        treated as "no repair attempted" so a buggy recipe never
        blocks the reject path.
        """

        try:
            from pollypm.pm_auto_repair import (
                BlockerContext,
                BlockerType,
                RepairResult,
                try_pm_repair,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("pm_auto_repair unavailable: %s", exc)
            return None
        try:
            worktree_path = self._lookup_worker_worktree_path(task)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "pm_auto_repair: worker worktree lookup failed for %s: %s",
                task.task_id,
                exc,
            )
            worktree_path = None
        blocker_type = (
            BlockerType.STALE_BASE
            if _looks_like_stale_base(reason)
            else BlockerType.UNKNOWN
        )
        ctx = BlockerContext(
            project_key=task.project,
            task_id=task.task_id,
            worker_role=getattr(task, "assignee", "") or "worker",
            blocker_type=blocker_type,
            blocker_detail=reason or "",
            worktree_path=worktree_path,
        )
        try:
            outcome = try_pm_repair(ctx)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "pm_auto_repair: try_pm_repair raised for %s: %s",
                task.task_id,
                exc,
            )
            return None
        if outcome.result == RepairResult.NOT_APPLICABLE:
            return None
        repaired = outcome.result == RepairResult.REPAIRED
        if repaired:
            logger.info(
                "pm_auto_repair: %s repaired blocker on %s — short-circuiting reject",
                outcome.recipe_name,
                task.task_id,
            )
        return _PMRepairCallSiteOutcome(
            repaired=repaired,
            recipe_name=outcome.recipe_name,
            diagnosis=outcome.diagnosis,
        )

    def _lookup_worker_worktree_path(self, task: Task):
        """Return the worker session's worktree path, or ``None``."""

        from pathlib import Path

        try:
            record = self.service.get_worker_session(
                task_project=task.project,
                task_number=task.task_number,
                active_only=True,
            )
        except Exception:  # noqa: BLE001
            return None
        if record is None:
            return None
        worktree = getattr(record, "worktree_path", None)
        if not worktree:
            return None
        return Path(worktree)

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

    def _resolve_claim_node(self, task: Task) -> tuple[str, FlowNode]:
        flow = self.service._load_flow_from_db(
            task.flow_template_id,
            task.flow_template_version,
        )
        node_id = task.current_node_id or flow.start_node
        if not node_id:
            raise InvalidTransitionError(
                f"Task {task.task_id} has no claimable flow node."
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
        return node_id, node

    def _latest_node_execution(
        self,
        task: Task,
        node_id: str,
    ):
        return self.service._conn.execute(
            "SELECT id, visit, status FROM work_node_executions "
            "WHERE task_project = ? AND task_number = ? AND node_id = ? "
            "ORDER BY visit DESC, id DESC LIMIT 1",
            (task.project, task.task_number, node_id),
        ).fetchone()

    def _approved_work_integrated(self, task: Task) -> bool:
        """Return True only when the task branch is merged into a non-task HEAD."""
        project_path = self.service._resolve_project_path(task.project)
        if project_path is None or not (project_path / ".git").exists():
            return False

        task_branch = f"task/{task.project}-{task.task_number}"
        current_branch = self.service._git_run(
            project_path,
            "rev-parse",
            "--abbrev-ref",
            "HEAD",
        )
        if current_branch.returncode != 0:
            logger.debug(
                "could not verify integration state for %s: rev-parse failed",
                task.task_id,
            )
            return False

        current_branch_name = current_branch.stdout.strip()
        if current_branch_name == task_branch:
            return False

        merged = self.service._git_run(
            project_path,
            "merge-base",
            "--is-ancestor",
            task_branch,
            "HEAD",
        )
        return merged.returncode == 0

    @staticmethod
    def _has_structured_critique_output(task: Task) -> bool:
        for execution in task.executions:
            if (
                execution.node_id != "critique"
                or execution.status != ExecutionStatus.COMPLETED
            ):
                continue
            output = execution.work_output
            if (
                output is not None
                and output.summary
                and output.summary.strip()
                and output.artifacts
            ):
                return True
        return False

    def _validate_critic_panel_children(
        self,
        task: Task,
    ) -> tuple[tuple[str, str], ...]:
        """Enforce the real 5-critic panel before plan_project can synthesize."""
        if (
            task.flow_template_id != "plan_project"
            or task.current_node_id != "critic_panel"
        ):
            return ()

        expected = set(_EXPECTED_CRITIC_PANEL_ROLES)
        found: dict[str, str] = {}
        problems: list[str] = []

        if len(task.children) != len(_EXPECTED_CRITIC_PANEL_ROLES):
            problems.append(
                "expected exactly five critic child tasks "
                f"({_format_csv(_EXPECTED_CRITIC_PANEL_ROLES)}), "
                f"found {len(task.children)}"
            )

        for project, number in task.children:
            child_id = f"{project}/{number}"
            child = self.service.get(child_id)
            critic_role = child.roles.get("critic")

            if child.flow_template_id != "critique_flow":
                problems.append(
                    f"{child_id} uses flow '{child.flow_template_id}', "
                    "expected 'critique_flow'"
                )

            if critic_role not in expected:
                problems.append(
                    f"{child_id} has unexpected critic role "
                    f"'{critic_role or '<missing>'}'"
                )
            elif critic_role in found:
                problems.append(
                    f"{child_id} duplicates critic role '{critic_role}' "
                    f"already provided by {found[critic_role]}"
                )
            else:
                found[critic_role] = child_id

            if child.work_status != WorkStatus.DONE:
                problems.append(
                    f"{child_id} ({critic_role or '<missing>'}) is "
                    f"'{child.work_status.value}', expected 'done'"
                )

            if not self._has_structured_critique_output(child):
                problems.append(
                    f"{child_id} ({critic_role or '<missing>'}) has no "
                    "completed structured critique output"
                )

        missing = [role for role in _EXPECTED_CRITIC_PANEL_ROLES if role not in found]
        if missing:
            role_word = "role" if len(missing) == 1 else "roles"
            problems.append(f"missing critic {role_word}: {_format_csv(missing)}")

        if problems:
            raise ValidationError(
                "Cannot complete critic_panel: " + "; ".join(problems)
            )

        return tuple((role, found[role]) for role in _EXPECTED_CRITIC_PANEL_ROLES)

    def _record_critic_panel_children(
        self,
        task: Task,
        actor: str,
        children_by_role: tuple[tuple[str, str], ...],
        now: str,
    ) -> None:
        if not children_by_role:
            return
        child_summary = ", ".join(
            f"{role}={child_id}" for role, child_id in children_by_role
        )
        self.service._conn.execute(
            "INSERT INTO work_context_entries "
            "(task_project, task_number, actor, text, created_at, entry_type) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                task.project,
                task.task_number,
                actor,
                f"critic_panel children: {child_summary}",
                now,
                "critic_panel_children",
            ),
        )

    def queue(self, task_id: str, actor: str, skip_gates: bool = False) -> Task:
        task = self.service.get(task_id)

        if task.work_status != WorkStatus.DRAFT:
            raise InvalidTransitionError(
                f"Cannot queue task in '{task.work_status.value}' state. "
                f"Task must be in 'draft' state."
            )

        if task.requires_human_review and not skip_gates:
            if not self.service.has_human_review_approval(task_id):
                approval_task = self.service.ensure_human_review_request_task(
                    task_id,
                    actor,
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
            and not self.service.has_human_review_approval(task_id)
        ):
            self.service.add_context(
                task_id,
                actor,
                "fast-track queue bypass recorded via --skip-gates",
                entry_type="human_review_approved",
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

        node_id, claim_node = self._resolve_claim_node(task)
        assignee = self.service._resolve_node_assignee(task, claim_node) or actor
        target_status = (
            WorkStatus.REVIEW
            if claim_node.type == NodeType.REVIEW
            else WorkStatus.IN_PROGRESS
        )
        # #1737 — enforce the per-project parallel-worker cap BEFORE the
        # DB transition so a cap-exceeded claim surfaces a clean error
        # instead of leaving the task ``in_progress`` with no worker.
        # Review claims and skip_gates bypass the cap (review windows
        # have their own naming + lifecycle; skip_gates is the operator
        # escape hatch).
        if (
            target_status is WorkStatus.IN_PROGRESS
            and not skip_gates
            and self.service._session_mgr is not None
        ):
            check_cap = getattr(
                self.service._session_mgr, "check_parallel_cap", None,
            )
            if callable(check_cap):
                check_cap(task.project, task_id)
        latest_execution = self._latest_node_execution(task, node_id)
        resume_blocked_execution = (
            task.current_node_id is not None
            and latest_execution is not None
            and latest_execution["status"] == ExecutionStatus.BLOCKED.value
        )
        reuse_active_execution = (
            task.current_node_id is not None
            and latest_execution is not None
            and latest_execution["status"] == ExecutionStatus.ACTIVE.value
        )
        next_visit = None
        if not resume_blocked_execution and not reuse_active_execution:
            next_visit = self.service._next_visit(
                task.project,
                task.task_number,
                node_id,
            )
        now = _now()

        def _mutate() -> None:
            self.service._conn.execute(
                "UPDATE work_tasks SET work_status = ?, assignee = ?, "
                "current_node_id = ?, updated_at = ? "
                "WHERE project = ? AND task_number = ?",
                (
                    target_status.value,
                    assignee,
                    node_id,
                    now,
                    task.project,
                    task.task_number,
                ),
            )
            if resume_blocked_execution:
                self.service._conn.execute(
                    "UPDATE work_node_executions SET status = ? WHERE id = ?",
                    (
                        ExecutionStatus.ACTIVE.value,
                        latest_execution["id"],
                    ),
                )
            elif next_visit is not None:
                self.service._conn.execute(
                    "INSERT INTO work_node_executions "
                    "(task_project, task_number, node_id, visit, status, started_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        task.project,
                        task.task_number,
                        node_id,
                        next_visit,
                        ExecutionStatus.ACTIVE.value,
                        now,
                    ),
                )
            self.service._record_transition(
                task.project,
                task.task_number,
                WorkStatus.QUEUED.value,
                target_status.value,
                actor,
            )

        self._commit(_mutate)

        # #1953 — ``_after_sync`` receives the post-commit Task snapshot
        # that ``_finish`` reloads from the store. When the worker-cap
        # rollback fires, that snapshot becomes stale (in_progress with
        # an active execution) even though the DB row is back to
        # queued/abandoned. Capture the rollback outcome via a closure
        # cell so the outer claim() can refetch and return the fresh
        # queued Task instead of the stale in_progress one.
        rollback_committed = [False]

        def _after_sync(_: Task) -> None:
            self.service.last_provision_error = None
            if self.service._session_mgr is not None:
                try:
                    self.service._session_mgr.provision_worker(task_id, assignee)
                except Exception as exc:  # noqa: BLE001
                    self.service.last_provision_error = str(exc)
                    logger.warning(
                        "provision_worker failed for %s (actor=%s): %s",
                        task_id,
                        actor,
                        exc,
                    )
                    # #1906 — the atomic cap reserve can still lose the
                    # race after the claim transition has already
                    # committed (two queued tasks both pass the pre-check,
                    # one wins the atomic reserve, the other commits its
                    # task transition and then raises WorkerCapExceededError
                    # from the reserve). Without this rollback the loser
                    # is left ``in_progress`` with no worker, no tmux
                    # window, and no worktree — auto-claim won't naturally
                    # re-pick it because it's no longer ``queued``.
                    #
                    # The session manager already released its placeholder
                    # cap slot via _release_cap_slot_on_failure, so all
                    # that's left is reverting the work_tasks row + the
                    # active node execution so the next tick re-claims.
                    if _is_cap_exceeded_error(exc):
                        rollback_committed[0] = self._rollback_claim_to_queued(
                            task, node_id, actor, exc,
                        )

        result = self._finish(
            task_id,
            WorkStatus.QUEUED.value,
            after_sync=_after_sync,
        )
        if rollback_committed[0]:
            # #1953 — refetch so the caller sees the rolled-back row
            # (queued, no active execution) instead of the stale
            # in_progress snapshot ``_finish`` captured pre-rollback.
            result = self.service.get(task_id)
        return result

    def _rollback_claim_to_queued(
        self,
        task: Task,
        node_id: str,
        actor: str,
        exc: BaseException,
    ) -> bool:
        """Revert an in_progress claim back to queued (#1906).

        Called when post-commit ``provision_worker`` fails with a
        cap-exceeded error. Drops the active node execution row that
        the claim mutate inserted and stamps the task back to
        ``queued`` so the next auto-claim tick can re-pick it.

        Best-effort: any DB error is logged and swallowed because the
        provision exception in ``last_provision_error`` is the primary
        operator signal. Without the rollback the task is silently
        wedged; with a failed rollback the operator still sees the
        provision error and can run ``pm task release`` manually.

        Returns ``True`` when the rollback committed successfully so
        callers can refetch the now-queued task instead of returning
        the stale in_progress snapshot they captured before
        provisioning fired (#1953). ``False`` on rollback failure —
        the row is still in_progress, the stale snapshot is still
        accurate.
        """
        now = _now()
        try:
            self._commit(
                lambda: (
                    self.service._conn.execute(
                        "UPDATE work_tasks SET work_status = ?, "
                        "updated_at = ? "
                        "WHERE project = ? AND task_number = ?",
                        (
                            WorkStatus.QUEUED.value,
                            now,
                            task.project,
                            task.task_number,
                        ),
                    ),
                    self.service._conn.execute(
                        "UPDATE work_node_executions SET status = ?, "
                        "completed_at = ? "
                        "WHERE task_project = ? AND task_number = ? "
                        "AND node_id = ? AND status = ?",
                        (
                            ExecutionStatus.ABANDONED.value,
                            now,
                            task.project,
                            task.task_number,
                            node_id,
                            ExecutionStatus.ACTIVE.value,
                        ),
                    ),
                    self.service._record_transition(
                        task.project,
                        task.task_number,
                        WorkStatus.IN_PROGRESS.value,
                        WorkStatus.QUEUED.value,
                        actor,
                    ),
                )
            )
            logger.warning(
                "claim rollback: %s/%d returned to queued after "
                "post-commit provision failure (%s)",
                task.project, task.task_number, exc,
            )
            return True
        except Exception as rollback_exc:  # noqa: BLE001
            logger.warning(
                "claim rollback failed for %s/%d: %s (task remains "
                "in_progress; operator must release manually)",
                task.project, task.task_number, rollback_exc,
            )
            return False

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
            # #1737 — kill any per-task reviewer window on cancel.
            # Idempotent: no-op if the window does not exist (e.g. the
            # task was cancelled before reaching review).
            self._teardown_per_task_reviewer(task_id)
            # #927: clear the no_session_for_assignment alerts the
            # heartbeat sweep raised while this task was active. The
            # per-task alert is unambiguous (alert key is the task id);
            # the per-project worker alert is only cleared when no other
            # active task on the project still routes to that role.
            # Best-effort — any error is swallowed so a misconfigured
            # alert store can't block the cancel transition.
            self._clear_assignment_alerts_after_cancel(task)

        return self._finish(
            task_id,
            task.work_status.value,
            new_status=WorkStatus.CANCELLED.value,
            before_reload=_before_reload,
            after_sync=lambda result: self.service._prune_cancelled_critique_child(
                result
            ),
        )

    def _clear_assignment_alerts_after_cancel(self, task: Task) -> None:
        """Publish cleanup for no_session alerts on a just-cancelled task.

        The task_assignment_notify plugin owns these alert families. The
        work service publishes a neutral cleanup event and the plugin
        handles it when loaded; without that subscriber this is a no-op.

        Per-project ``(worker-<project>, no_session)`` is only cleared
        when no other active task on the project still routes to that
        role. We compute the "other active" map here against the work
        service so the subscriber doesn't need to reach back into the
        transition manager.

        We resolve the alert store from the unified Store registry and
        pass it in, so the subscriber doesn't need to spin up a second
        work-service connection against the same SQLite file while our
        caller's cancel transaction is still in flight.
        """
        try:
            roles = tuple((task.roles or {}).keys()) or ("worker",)
        except Exception:  # noqa: BLE001
            roles = ("worker",)
        active_map: dict[str, bool] = {}
        # #941: ``BLOCKED`` is intentionally excluded. The sweep contract
        # in ``task_assignment_notify/handlers/sweep.py`` (see
        # ``_NON_ACTIVE_SWEEP_STATUSES``) refuses to raise
        # ``no_session_for_assignment`` for blocked tasks — they live in
        # their own gate-blocked alert family. Treating a blocked sibling
        # as "active" here would keep the project-level
        # ``worker-<project>/no_session`` alert open even though no future
        # sweep will ever re-raise it, recreating part of the alert-noise
        # bug #927 fixed. Queued / in_progress / review / rework siblings
        # remain "active" — the sweep WILL re-emit for those, so the
        # project-level alert must stay open for them.
        active_statuses = (
            WorkStatus.QUEUED.value,
            WorkStatus.IN_PROGRESS.value,
            WorkStatus.REVIEW.value,
            WorkStatus.REWORK.value,
        )
        for role in roles:
            active_map[role] = False
            for status in active_statuses:
                try:
                    siblings = self.service.list_tasks(
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
        try:
            task_assignment_alerts.dispatch(
                task_assignment_alerts.CancelledTaskAssignmentAlertsEvent(
                    task_id=task.task_id,
                    project=task.project,
                    role_names=roles,
                    has_other_active_for_role=active_map,
                    store=self._resolve_alert_store(),
                )
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "assignment alert cleanup dispatch failed for %s",
                task.task_id,
                exc_info=True,
            )

    def _close_plan_review_notify_after_approve(self, task: Task) -> None:
        """Close any open ``plan_review`` notify message for this plan_task.

        Belt-and-suspenders for #1103: when the user approves a
        ``plan_project``/``user_approval`` task, also close the
        architect's ``[Action] Plan ready for review: ...`` notify
        message that points at this task via the
        ``plan_task:<project>/<number>`` label. The cockpit inbox has
        a per-render filter that hides such phantom rows, but closing
        the underlying row here keeps the source clean too — anyone
        querying open ``notify`` messages directly sees the right
        state, not just cockpit users.

        Fire-and-forget; any error is swallowed.
        """
        if task.flow_template_id != "plan_project":
            return
        try:
            store = self._resolve_alert_store()
            if store is None:
                return
            target_label = f"plan_task:{task.task_id}"
            try:
                rows = store.query_messages(
                    recipient="user",
                    state="open",
                    type=["notify"],
                )
            except Exception:  # noqa: BLE001
                return
            closed = 0
            for row in rows or []:
                labels_raw = row.get("labels") or []
                labels: list[str] = []
                if isinstance(labels_raw, list):
                    labels = [str(x) for x in labels_raw]
                elif isinstance(labels_raw, str) and labels_raw:
                    import json as _json

                    try:
                        parsed = _json.loads(labels_raw)
                        if isinstance(parsed, list):
                            labels = [str(x) for x in parsed]
                    except ValueError:
                        labels = []
                if "plan_review" not in labels:
                    continue
                if target_label not in labels:
                    continue
                msg_id = row.get("id")
                if msg_id is None:
                    continue
                try:
                    store.close_message(int(msg_id))
                    closed += 1
                except Exception:  # noqa: BLE001
                    continue
            if closed:
                logger.info(
                    "approve(%s): closed %d phantom plan_review notify "
                    "row(s)",
                    task.task_id,
                    closed,
                )
        except Exception:  # noqa: BLE001
            logger.debug(
                "_close_plan_review_notify_after_approve failed for %s",
                task.task_id,
                exc_info=True,
            )

    def _clear_no_session_alert_after_approve(self, task: Task) -> None:
        """Publish cleanup for a per-task no_session alert after approve.

        This is narrower than the cancel cleanup (#927): only the
        per-task ``no_session_for_assignment:<task_id>`` alert is stale.
        The project-level ``worker-<project>/no_session`` alert is left
        alone, because the just-approved task may now route to a new role
        on the same project, or other active siblings may still need the
        role.
        """
        try:
            task_assignment_alerts.dispatch(
                task_assignment_alerts.ClearNoSessionAlertForTaskEvent(
                    task_id=task.task_id,
                    store=self._resolve_alert_store(),
                )
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "no_session alert cleanup dispatch failed for %s",
                task.task_id,
                exc_info=True,
            )

    @staticmethod
    def _resolve_alert_store() -> object | None:
        """Best-effort resolve a Store handle for alert writes.

        Returns ``None`` when config / store cannot be loaded — the
        resolver helper then falls back to its own ``load_runtime_services``
        path so callers in environments without a config file still
        get the unambiguous per-task alert clear.
        """
        try:
            from pollypm.config import DEFAULT_CONFIG_PATH, load_config
            from pollypm.store.registry import get_store

            config = load_config(DEFAULT_CONFIG_PATH)
            return get_store(config)
        except Exception:  # noqa: BLE001
            return None

    def hold(self, task_id: str, actor: str, reason: str | None = None) -> Task:
        task = self.service.get(task_id)

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

        if task.work_status == WorkStatus.REVIEW:
            try:
                _flow, node = self.service._get_current_flow_node(task)
            except Exception:  # noqa: BLE001
                node = None
            if getattr(node, "actor_type", None) == ActorType.HUMAN:
                raise InvalidTransitionError(
                    "Cannot hold a task at a human review/approval node. "
                    "Keep it in 'review' so the human accept/reject path stays visible."
                )

            reason_text = (reason or "").strip()
            if reason_text.startswith(_AUTO_HOLD_REASON_PREFIX):
                notify_subject = reason_text[len(_AUTO_HOLD_REASON_PREFIX) :].strip()
                if notify_subject.startswith(
                    "[Action]"
                ) and not notify_requires_review_hold(
                    subject=notify_subject,
                    body="",
                ):
                    raise InvalidTransitionError(
                        "Cannot auto-hold a review-ready task from a non-blocking "
                        "operator/reviewer notification."
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
            after_reload=self._write_review_summary_after_transition,
        )

    def resume(self, task_id: str, actor: str) -> Task:
        task = self.service.get(task_id)

        if task.work_status != WorkStatus.ON_HOLD:
            raise InvalidTransitionError(
                f"Cannot resume task in '{task.work_status.value}' state. "
                f"Task must be in 'on_hold' state."
            )

        target_status = WorkStatus.QUEUED
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
            if row is not None:
                _node_id, node = self._resolve_claim_node(task)
                target_status = (
                    WorkStatus.REVIEW
                    if node.type == NodeType.REVIEW
                    else WorkStatus.IN_PROGRESS
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
            after_reload=self._write_review_summary_after_transition,
        )

    def node_done(
        self,
        task_id: str,
        actor: str,
        work_output: WorkOutput | dict | None = None,
        skip_gates: bool = False,
    ) -> Task:
        task = self.service.get(task_id)

        if task.work_status not in (
            WorkStatus.IN_PROGRESS,
            WorkStatus.REWORK,  # #777 — rework node_done is a normal transition
        ):
            raise InvalidTransitionError(
                f"Cannot complete node on task in "
                f"'{task.work_status.value}' state. "
                f"Task must be in 'in_progress' or 'rework' state."
            )

        flow, node = self.service._get_current_flow_node(task)

        if node.type != NodeType.WORK:
            raise InvalidTransitionError(
                f"Current node '{task.current_node_id}' is not a work node "
                f"(type: {node.type.value})."
            )

        self.service._validate_actor_role(task, node, actor)
        critic_panel_children = self._validate_critic_panel_children(task)

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
                '      "type": "code_change",\n'
                '      "summary": "<what you built>",\n'
                '      "artifacts": [{"kind": "commit", "description": '
                '"impl", "ref": "HEAD"}]\n'
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
                self._record_critic_panel_children(
                    task,
                    actor,
                    critic_panel_children,
                    now,
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
            self._write_review_summary_after_transition(result)
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
                # #1737 — kill the per-task reviewer window on terminal
                # done. Safe to call whether or not the task ever
                # visited a review node (idempotent: no-op if the
                # window does not exist).
                self._teardown_per_task_reviewer(task_id)
                self.service._on_task_done(task_id, actor)
            elif result.work_status == WorkStatus.REVIEW:
                # #1737 — spawn the per-task reviewer window so a
                # dedicated Russell session reviews THIS task (and
                # remembers what it flagged across reject -> rework
                # -> resubmit cycles). Idempotent: if the window
                # already exists we re-use it. Replaces the older
                # per-project reviewer-lane bootstrap (#1413) which
                # spawned a single ``reviewer-<project>`` window that
                # tried to multiplex every task.
                self._provision_per_task_reviewer(result)
                # #1413 — keep the per-project bootstrap call as a
                # belt-and-suspenders hook for installs that still
                # have ``reviewer_<project>`` entries in their toml
                # (Sam's deployed config is migrating session-by-
                # session). The function is idempotent: when no
                # static reviewer session is configured it no-ops.
                self._ensure_reviewer_for_review_handoff(result)

        return self._finish(
            task_id,
            task.work_status.value,
            after_reload=_before_sync,
        )

    def _provision_per_task_reviewer(self, task: Task) -> None:
        """Spawn the per-task reviewer window on a review handoff.

        Architectural alignment per #1737: reviewers move from
        per-project persistent (``reviewer-<project>``) to per-task
        ephemeral (``reviewer-<project>-<task_number>``). Each task in
        review gets its own dedicated Russell window that persists
        across reject -> rework -> resubmit cycles for that task and is
        torn down on accept or terminal cancel.

        Best-effort: any failure logs and swallows so the transition
        completes. The watchdog's ``role_session_missing`` detector
        will retry within ~5 minutes if the spawn failed.
        """
        if self.service._session_mgr is None:
            return
        provision_fn = getattr(
            self.service._session_mgr, "provision_reviewer", None,
        )
        if not callable(provision_fn):
            return
        try:
            window_name = provision_fn(task.task_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "provision_per_task_reviewer failed for %s: %s",
                task.task_id, exc,
            )
            return
        if window_name:
            logger.info(
                "provisioned per-task reviewer %s for %s",
                window_name, task.task_id,
            )

    def _teardown_per_task_reviewer(self, task_id: str) -> None:
        """Kill the per-task reviewer window on terminal done / cancel.

        Best-effort: any error logs and swallows so the transition
        completes. Idempotent — safe to call whether or not the task
        ever visited a review node (#1737 architectural alignment).
        """
        if self.service._session_mgr is None:
            return
        teardown_fn = getattr(
            self.service._session_mgr, "teardown_reviewer", None,
        )
        if not callable(teardown_fn):
            return
        try:
            teardown_fn(task_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "teardown_per_task_reviewer failed for %s: %s",
                task_id, exc,
            )

    def _ensure_reviewer_for_review_handoff(self, task: Task) -> None:
        """Fire-and-forget provision of a reviewer session for ``task.project``.

        Called from ``complete_node`` when the task transitions to
        ``review``. Mirrors the worker provision hook in :meth:`claim`
        (which calls ``self.service._session_mgr.provision_worker``).
        Reviewer provisioning lives in
        :mod:`pollypm.recovery.reviewer_provisioning` because it operates
        on the long-lived per-project reviewer lane (config-level), not
        per-task tmux windows like worker provisioning.

        Best-effort: any failure is logged and swallowed — the task is
        already in REVIEW state and the heartbeat's no_session recovery
        catches stragglers within ~60s. We log so the audit trail
        records the attempt, and we never block the transition path.
        """
        try:
            from pollypm.config import DEFAULT_CONFIG_PATH
            from pollypm.recovery.reviewer_provisioning import (
                ensure_reviewer_session_for_project,
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "complete_node: reviewer provisioning import failed",
                exc_info=True,
            )
            return
        try:
            created, detail = ensure_reviewer_session_for_project(
                DEFAULT_CONFIG_PATH,
                task.project,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "complete_node: ensure_reviewer_session failed for %s: %s",
                task.project, exc,
            )
            return
        if created:
            logger.info(
                "complete_node: provisioned reviewer session for %s "
                "on review handoff (%s)",
                task.project, detail,
            )

    def approve(
        self,
        task_id: str,
        actor: str,
        reason: str | None = None,
        skip_gates: bool = False,
        resume_merge: bool = False,
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
            self.service._auto_merge_approved_task_branch(
                task,
                resume_merge=resume_merge,
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
                if (
                    self.service._session_mgr is not None
                    and self._approved_work_integrated(result)
                ):
                    try:
                        self.service._session_mgr.teardown_worker(task_id)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "teardown_worker on approve failed for %s: %s",
                            task_id,
                            exc,
                        )
                # #1737 — per-task reviewer windows are torn down on
                # accept (the reviewer's job for this task is complete).
                # The same window is preserved across reject -> rework
                # -> resubmit cycles (the reviewer remembers what it
                # flagged); only the terminal-done transition kills it.
                self._teardown_per_task_reviewer(task_id)
                self.service._on_task_done(task_id, actor)
            # #953: clear the per-task no_session_for_assignment alert
            # the heartbeat sweep raised while the task was sitting in
            # ``review`` waiting for a reviewer session. The CLI approve
            # the user just ran IS the canonical reviewer path, so the
            # alert is stale the moment we leave ``review``. Best-effort
            # — any error is swallowed so a misconfigured alert store
            # can't block the approve transition.
            self._clear_no_session_alert_after_approve(task)
            # #1103: close any open plan_review notify message that
            # points at this just-approved plan_task. Cockpit already
            # filters phantom rows at render-time, but closing the
            # underlying row keeps the message store honest for any
            # other consumer. Best-effort — any error is swallowed.
            self._close_plan_review_notify_after_approve(task)

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
                f"Current node '{task.current_node_id}' is not a review node."
            )

        self.service._validate_actor_role(task, node, actor)

        if not reason or not reason.strip():
            raise ValidationError("Reason is required for rejection.")

        if node.reject_node_id is None:
            raise InvalidTransitionError(
                f"Review node '{task.current_node_id}' has no reject_node defined."
            )

        reject_target = flow.nodes.get(node.reject_node_id)
        if reject_target is None:
            raise InvalidTransitionError(
                f"Reject node '{node.reject_node_id}' not found in flow."
            )

        # #1026 — give the project PM a chance to auto-repair the
        # blocker before we burn the worker's submission. If the PM
        # repairs it (e.g. merges main into the worker branch on a
        # stale-base rejection), we leave the task in REVIEW so the
        # reviewer can re-evaluate against the fresh tree. If the PM
        # tries and fails, we enrich the rejection reason with the
        # diagnosis so the human gets richer context. If the PM has
        # nothing to offer (no recipe matched), we fall through to
        # the existing behavior unchanged.
        repair_outcome = self._maybe_pm_auto_repair(task, reason)
        if repair_outcome is not None and repair_outcome.repaired:
            return self.service.get(task_id)
        if repair_outcome is not None and repair_outcome.diagnosis:
            reason = (
                f"{reason.rstrip()}\n\n"
                f"[pm_auto_repair:{repair_outcome.recipe_name}] "
                f"{repair_outcome.diagnosis}"
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

            # #777 — explicit REWORK state instead of bouncing back
            # to IN_PROGRESS. Reject-target node is still active
            # (so the worker can re-claim) but the status now
            # carries the rework signal so cockpit / inbox can
            # surface "this came from a rejection" instead of
            # presenting a freshly-claimed-looking task.
            self.service._conn.execute(
                "UPDATE work_tasks SET work_status = ?, assignee = ?, "
                "current_node_id = ?, updated_at = ? "
                "WHERE project = ? AND task_number = ?",
                (
                    WorkStatus.REWORK.value,
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
                WorkStatus.REWORK.value,
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
