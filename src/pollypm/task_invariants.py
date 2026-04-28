"""Task workflow invariant checker (#886).

One transition table — owner, allowed transitions, capacity
treatment, cockpit visibility, inbox visibility — plus one
checker that runs every invariant the audit identifies as a
recurring failure shape.

The pre-launch audit (``docs/launch-issue-audit-2026-04-27.md``
§5) cites 30+ task-state regressions where one subsystem
(assignment / recovery / capacity / cockpit / inbox / advisor /
metrics) ignored a state another subsystem cared about.
Examples:

* `#770` / `#771` — recovery missed per-project ``IN_PROGRESS``
  tasks; dead-worker work stayed stuck.
* `#816` — ``REWORK`` invisible to dead-worker recovery and
  capacity accounting.
* `#807` — recovery matched the wrong live windows.
* `#806` — stale-claim recovery deleted execution history and
  restarted the flow at the start node.
* `#395` — critic infrastructure leaked synthetic tasks into
  the real task list and shifted task IDs.

The structural fix is a single source of truth for what each
state means *across all subsystems* and a checker that runs the
invariants. The checker is the ``pm doctor --launch-state`` the
audit asks for. The release gate (#889) consults the checker
output before tagging.

Architecture:

* :class:`StateMetadata` — frozen per-state declaration.
* :data:`TASK_TRANSITION_TABLE` — mapping
  ``WorkStatus → StateMetadata``.
* :class:`InvariantViolation` — one detected violation.
* :func:`check_task_invariants` — runs every invariant against
  an iterable of tasks plus context (live-session set, capacity
  account map, …) and returns the violation tuple.

Migration policy: the checker is additive. Existing recovery /
capacity / cockpit code keeps working; the checker reports
disagreement with the canonical table. Promoting a violation
to a hard error happens once the underlying caller migrates.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from pollypm.work.models import WorkStatus


# ---------------------------------------------------------------------------
# Owner enum
# ---------------------------------------------------------------------------


class StateOwner(enum.Enum):
    """Who is responsible for moving a task out of this state."""

    SYSTEM = "system"
    """The flow engine / supervisor will move it without user
    intervention (``QUEUED`` until a worker claims, etc.)."""

    WORKER = "worker"
    """A worker session is actively responsible."""

    REVIEWER = "reviewer"
    """The reviewer role moves the task to APPROVE / REJECT."""

    USER = "user"
    """The user must explicitly decide (``REVIEW`` waiting on
    user, ``ON_HOLD`` waiting on user)."""

    NOBODY = "nobody"
    """Terminal state — nobody owns it."""


# ---------------------------------------------------------------------------
# Per-state metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StateMetadata:
    """Declarative invariant for one ``WorkStatus`` value.

    Every consumer (assignment, recovery, capacity, cockpit,
    inbox, metrics) reads these flags. The table is the single
    place those answers live.
    """

    status: WorkStatus
    owner: StateOwner
    allowed_next: frozenset[WorkStatus] = field(default_factory=frozenset)
    """Statuses a transition can move TO from this state."""

    consumes_capacity: bool = False
    """Whether a task in this state consumes worker capacity. The
    audit cites #816 — REWORK was invisible to capacity."""

    visible_in_cockpit_default: bool = True
    """Whether the default cockpit Tasks view shows this state.
    Terminal states default to hidden."""

    visible_in_inbox: bool = False
    """Whether the inbox surfaces a task in this state. Defaults
    are conservative; only states that need user attention here."""

    is_terminal: bool = False
    counts_as_done: bool = False

    requires_active_worker_session: bool = False
    """``True`` when a task in this state must be backed by a
    live worker tmux window. The recovery sweep targets this set
    (#770 / #771)."""

    requires_recovery_lane: bool = False
    """``True`` when the recovery sweep must inspect this state.
    REWORK qualifies (#816)."""

    user_can_cancel: bool = True


# ---------------------------------------------------------------------------
# Canonical transition table
# ---------------------------------------------------------------------------


TASK_TRANSITION_TABLE: Mapping[WorkStatus, StateMetadata] = {
    WorkStatus.DRAFT: StateMetadata(
        status=WorkStatus.DRAFT,
        owner=StateOwner.USER,
        # ``DRAFT -> DONE`` supports synthetic backfill / migration
        # paths that record a task already-completed (e.g., legacy
        # imports). Caught by the runtime validator (#899); the
        # canonical table now reflects the real production set.
        allowed_next=frozenset(
            {WorkStatus.QUEUED, WorkStatus.DONE, WorkStatus.CANCELLED}
        ),
        visible_in_cockpit_default=False,
    ),
    WorkStatus.QUEUED: StateMetadata(
        status=WorkStatus.QUEUED,
        owner=StateOwner.SYSTEM,
        # ``QUEUED -> ON_HOLD`` is supported (the user can pause a
        # queued task without claiming it). Caught by the runtime
        # validator (#899) when the table omitted it earlier.
        # ``QUEUED -> DONE`` covers ``mark_done`` on a task that
        # never claimed (e.g., the inbox archive contract and the
        # manual fast-complete CLI path); enforced by #909.
        # ``QUEUED -> REVIEW`` covers re-claim of an auto-unblocked
        # task whose current node is a review node — the claim
        # restores REVIEW directly instead of bouncing through
        # IN_PROGRESS.
        allowed_next=frozenset(
            {
                WorkStatus.IN_PROGRESS,
                WorkStatus.REVIEW,
                WorkStatus.BLOCKED,
                WorkStatus.ON_HOLD,
                WorkStatus.CANCELLED,
                WorkStatus.DONE,
            }
        ),
        consumes_capacity=False,
    ),
    WorkStatus.IN_PROGRESS: StateMetadata(
        status=WorkStatus.IN_PROGRESS,
        owner=StateOwner.WORKER,
        # ``IN_PROGRESS -> IN_PROGRESS`` is the flow-engine
        # node-hop case: a multi-stage worker flow (architect's
        # research -> discover -> synthesize) advances the
        # ``current_node_id`` while the task stays IN_PROGRESS.
        # Each hop writes a transition row so the audit trail
        # stays continuous (#909, #295).
        allowed_next=frozenset(
            {
                WorkStatus.IN_PROGRESS,
                WorkStatus.REVIEW,
                WorkStatus.BLOCKED,
                WorkStatus.ON_HOLD,
                WorkStatus.DONE,
                WorkStatus.CANCELLED,
                WorkStatus.QUEUED,
            }
        ),
        consumes_capacity=True,
        requires_active_worker_session=True,
        requires_recovery_lane=True,
    ),
    WorkStatus.REWORK: StateMetadata(
        status=WorkStatus.REWORK,
        owner=StateOwner.WORKER,
        allowed_next=frozenset(
            {
                WorkStatus.IN_PROGRESS,
                WorkStatus.QUEUED,
                WorkStatus.CANCELLED,
            }
        ),
        # #816 — REWORK does consume capacity (the worker session
        # is still active reworking) and recovery must inspect it.
        consumes_capacity=True,
        requires_active_worker_session=True,
        requires_recovery_lane=True,
    ),
    WorkStatus.REVIEW: StateMetadata(
        status=WorkStatus.REVIEW,
        owner=StateOwner.USER,
        # ``REVIEW -> ON_HOLD`` supports pausing a review (#899
        # validator caught the live path).
        # ``REVIEW -> BLOCKED`` is reachable from ``block()`` —
        # the production helper accepts both IN_PROGRESS and
        # REVIEW. Recorded by #909.
        allowed_next=frozenset(
            {
                WorkStatus.DONE,
                WorkStatus.REWORK,
                WorkStatus.IN_PROGRESS,
                WorkStatus.ON_HOLD,
                WorkStatus.BLOCKED,
                WorkStatus.CANCELLED,
            }
        ),
        consumes_capacity=False,
        visible_in_inbox=True,
    ),
    WorkStatus.BLOCKED: StateMetadata(
        status=WorkStatus.BLOCKED,
        owner=StateOwner.USER,
        allowed_next=frozenset(
            {
                WorkStatus.IN_PROGRESS,
                WorkStatus.QUEUED,
                WorkStatus.CANCELLED,
            }
        ),
        consumes_capacity=False,
        visible_in_inbox=True,
    ),
    WorkStatus.ON_HOLD: StateMetadata(
        status=WorkStatus.ON_HOLD,
        owner=StateOwner.USER,
        # ``ON_HOLD -> REVIEW`` resumes a paused review back into
        # the active review state (#899).
        allowed_next=frozenset(
            {
                WorkStatus.IN_PROGRESS,
                WorkStatus.QUEUED,
                WorkStatus.REVIEW,
                WorkStatus.CANCELLED,
            }
        ),
        consumes_capacity=False,
        visible_in_inbox=True,
    ),
    WorkStatus.DONE: StateMetadata(
        status=WorkStatus.DONE,
        owner=StateOwner.NOBODY,
        allowed_next=frozenset(),
        is_terminal=True,
        counts_as_done=True,
        visible_in_cockpit_default=False,
        user_can_cancel=False,
    ),
    WorkStatus.CANCELLED: StateMetadata(
        status=WorkStatus.CANCELLED,
        owner=StateOwner.NOBODY,
        allowed_next=frozenset(),
        is_terminal=True,
        visible_in_cockpit_default=False,
        user_can_cancel=False,
    ),
}


# ---------------------------------------------------------------------------
# Convenience accessors
# ---------------------------------------------------------------------------


def metadata_for(status: WorkStatus) -> StateMetadata:
    """Return the canonical :class:`StateMetadata` for ``status``."""
    return TASK_TRANSITION_TABLE[status]


def is_capacity_consuming(status: WorkStatus) -> bool:
    """The capacity manager calls this. The audit cites #816 —
    REWORK was missed because the capacity check inlined a
    hand-maintained set instead of reading from a contract."""
    return TASK_TRANSITION_TABLE[status].consumes_capacity


def requires_recovery_lane(status: WorkStatus) -> bool:
    """The recovery sweep calls this."""
    return TASK_TRANSITION_TABLE[status].requires_recovery_lane


def is_transition_allowed(
    from_status: WorkStatus, to_status: WorkStatus
) -> bool:
    """Pure check: is ``from_status -> to_status`` permitted?"""
    return to_status in TASK_TRANSITION_TABLE[from_status].allowed_next


# ---------------------------------------------------------------------------
# Invariant violation type
# ---------------------------------------------------------------------------


class ViolationKind(enum.Enum):
    """Catalog of violation shapes the checker reports.

    Naming each shape lets the cockpit / pm doctor render an
    actionable description instead of a generic 'something is
    wrong'."""

    IN_PROGRESS_NO_OWNER = "in_progress_no_owner"
    """Task is IN_PROGRESS / REWORK but no live worker session."""

    QUEUED_NO_ROLE_SESSION = "queued_no_role_session"
    """Task is QUEUED but no reachable role session can claim it."""

    REWORK_OUTSIDE_RECOVERY_LANE = "rework_outside_recovery_lane"
    """Task is in REWORK but recovery has not visited it. The
    audit's #816 bug shape."""

    BLOCKED_NO_UNBLOCK_PATH = "blocked_no_unblock_path"
    """Task is BLOCKED but nothing in the system can unblock it."""

    DEAD_CLAIM_CONSUMES_CAPACITY = "dead_claim_consumes_capacity"
    """A capacity-consuming task has no live session — the
    capacity reservation is wasted."""

    INVALID_TRANSITION = "invalid_transition"
    """A transition violates the canonical table."""

    PLANNER_CRITIC_LEAKED_INTO_TASKS = "planner_critic_leaked_into_tasks"
    """A planner / critic synthetic task is visible in the user
    task list. The audit cites #395."""


@dataclass(frozen=True, slots=True)
class InvariantViolation:
    """One invariant violation."""

    kind: ViolationKind
    task_id: str
    detail: str

    @property
    def summary(self) -> str:
        """One-line human-readable description for the cockpit."""
        return f"{self.kind.value}: {self.task_id} — {self.detail}"


# ---------------------------------------------------------------------------
# Checker context + runner
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TaskCheckContext:
    """Read-only snapshot of the runtime data the checker needs.

    The checker is pure — it never opens a DB connection or
    forks tmux. The caller assembles this snapshot from
    whatever subsystems are authoritative (work service, tmux
    client, capacity manager) and hands it off."""

    tasks: tuple["TaskRow", ...]
    live_worker_session_task_ids: frozenset[str] = field(
        default_factory=frozenset
    )
    """Task IDs whose worker tmux window currently exists."""

    reachable_role_sessions: frozenset[str] = field(default_factory=frozenset)
    """Role keys that have at least one live session right now.
    QUEUED tasks need at least one reachable role for their
    ``current_node_id``'s actor."""

    recovered_task_ids: frozenset[str] = field(default_factory=frozenset)
    """Task IDs the recovery sweep has visited in the current
    cycle."""

    capacity_consumed_task_ids: frozenset[str] = field(default_factory=frozenset)
    """Task IDs the capacity manager believes are consuming
    capacity right now."""

    synthetic_task_id_prefixes: tuple[str, ...] = ("critic-", "planner-")
    """Task ID prefixes the audit (#395) ties to internal
    subflows that should never appear in the user task list."""


@dataclass(slots=True)
class TaskRow:
    """Minimal task shape the checker consumes.

    Real :class:`pollypm.work.models.Task` objects have many more
    fields; the checker only needs these. Defining a separate
    Row type keeps tests tiny and makes cross-store consumers
    (mock + sqlite) work uniformly."""

    task_id: str
    status: WorkStatus
    current_role: str | None
    blocked_on_dependency: bool = False
    last_unblock_signal_seconds_ago: int | None = None


def check_task_invariants(
    context: TaskCheckContext,
) -> tuple[InvariantViolation, ...]:
    """Run every invariant against ``context``."""
    out: list[InvariantViolation] = []

    for task in context.tasks:
        # Synthetic / planner / critic leak (#395).
        if any(
            task.task_id.startswith(prefix)
            for prefix in context.synthetic_task_id_prefixes
        ):
            out.append(
                InvariantViolation(
                    kind=ViolationKind.PLANNER_CRITIC_LEAKED_INTO_TASKS,
                    task_id=task.task_id,
                    detail=(
                        "synthetic planner/critic task is visible in "
                        "the user task list (#395)"
                    ),
                )
            )

        meta = TASK_TRANSITION_TABLE.get(task.status)
        if meta is None:
            # Unknown status - violation but not the same shape.
            continue

        # IN_PROGRESS / REWORK without a live worker session.
        if (
            meta.requires_active_worker_session
            and task.task_id not in context.live_worker_session_task_ids
        ):
            out.append(
                InvariantViolation(
                    kind=ViolationKind.IN_PROGRESS_NO_OWNER,
                    task_id=task.task_id,
                    detail=(
                        f"status={task.status.value} but no live "
                        f"worker tmux window — recovery should claim "
                        f"or cancel"
                    ),
                )
            )

        # REWORK that recovery has not visited (#816).
        if (
            task.status is WorkStatus.REWORK
            and task.task_id not in context.recovered_task_ids
        ):
            out.append(
                InvariantViolation(
                    kind=ViolationKind.REWORK_OUTSIDE_RECOVERY_LANE,
                    task_id=task.task_id,
                    detail=(
                        "task is in REWORK but the recovery sweep "
                        "has not visited it this cycle (#816)"
                    ),
                )
            )

        # QUEUED without a reachable role session.
        if (
            task.status is WorkStatus.QUEUED
            and task.current_role is not None
            and task.current_role not in context.reachable_role_sessions
        ):
            out.append(
                InvariantViolation(
                    kind=ViolationKind.QUEUED_NO_ROLE_SESSION,
                    task_id=task.task_id,
                    detail=(
                        f"queued for role={task.current_role!r} but "
                        f"no live session for that role"
                    ),
                )
            )

        # BLOCKED with no unblock path. The signal is conservative:
        # if blocked_on_dependency is True and we've seen no unblock
        # signal in N seconds, the task is genuinely stuck.
        if task.status is WorkStatus.BLOCKED:
            if (
                not task.blocked_on_dependency
                and task.last_unblock_signal_seconds_ago is None
            ):
                out.append(
                    InvariantViolation(
                        kind=ViolationKind.BLOCKED_NO_UNBLOCK_PATH,
                        task_id=task.task_id,
                        detail=(
                            "task is BLOCKED but no dependency is "
                            "recorded and no unblock signal has been "
                            "seen — the user has no way to advance it"
                        ),
                    )
                )

        # Dead claim still counted as capacity (#816 / #770).
        if (
            meta.consumes_capacity
            and task.task_id in context.capacity_consumed_task_ids
            and task.task_id not in context.live_worker_session_task_ids
        ):
            out.append(
                InvariantViolation(
                    kind=ViolationKind.DEAD_CLAIM_CONSUMES_CAPACITY,
                    task_id=task.task_id,
                    detail=(
                        "capacity manager still counts this task but "
                        "no live worker session exists — capacity "
                        "reservation is wasted"
                    ),
                )
            )

    return tuple(out)


# ---------------------------------------------------------------------------
# Transition validation helper
# ---------------------------------------------------------------------------


def validate_transition(
    *,
    task_id: str,
    from_status: WorkStatus,
    to_status: WorkStatus,
) -> InvariantViolation | None:
    """Return a violation when ``from_status -> to_status`` is
    forbidden, or ``None`` when it is allowed.

    Used by the work service to refuse invalid transitions at
    write time. The audit cites #806 — recovery deleted execution
    history because the transition the recovery applied was not
    in the canonical table; running the validator at write time
    would have rejected it."""
    if is_transition_allowed(from_status, to_status):
        return None
    return InvariantViolation(
        kind=ViolationKind.INVALID_TRANSITION,
        task_id=task_id,
        detail=(
            f"transition {from_status.value} -> {to_status.value} is "
            f"not in the canonical transition table"
        ),
    )


# ---------------------------------------------------------------------------
# Cross-state coverage assertion (release-gate adjacent)
# ---------------------------------------------------------------------------


def all_statuses_have_metadata() -> tuple[str, ...]:
    """Return the names of any :class:`WorkStatus` value missing
    from :data:`TASK_TRANSITION_TABLE`.

    A clean run returns ``()``. The release gate (#889) consults
    this to refuse a tag if a new status was added to the enum
    without a corresponding metadata entry."""
    missing: list[str] = []
    for status in WorkStatus:
        if status not in TASK_TRANSITION_TABLE:
            missing.append(status.name)
    return tuple(missing)
