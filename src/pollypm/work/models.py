"""Data models for the work service.

Defines all domain objects: Task, FlowTemplate, FlowNode,
FlowNodeExecution, WorkOutput, Artifact, ContextEntry, GateResult,
and their associated enums.
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class WorkStatus(enum.Enum):
    """Lifecycle states for a task.

    The state set was originally eight values (draft / queued /
    in_progress / blocked / on_hold / review / done / cancelled).
    #777 adds explicit ``REWORK`` so reviewer-rejected tasks live in
    a distinct state instead of being silently bounced back to
    ``IN_PROGRESS``. The bounceback hid the rework history from the
    cockpit (a rejected task looked identical to a fresh claim) and
    made forward-progress logic rely on hidden coupling between the
    work DB row, the worker session, and UI decoration.

    ``REWORK`` semantics:
    * Set by :meth:`reject` when the reviewer rejects a review-node
      execution. The task's ``current_node_id`` advances to the
      flow's ``reject_node_id`` (typically the worker's implement
      node), exactly as before; only the status is now distinct.
    * Treated as non-terminal everywhere — the task is still
      claimable / advanceable (a follow-up worker claims it and
      transitions back to ``IN_PROGRESS`` via the regular claim
      path).
    * Surfaces in the cockpit as "rework needed" so the user can
      see at a glance that this task came from a rejection, not a
      fresh start.
    """

    DRAFT = "draft"
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    REWORK = "rework"
    BLOCKED = "blocked"
    ON_HOLD = "on_hold"
    REVIEW = "review"
    DONE = "done"
    CANCELLED = "cancelled"


class TaskType(enum.Enum):
    EPIC = "epic"
    TASK = "task"
    SUBTASK = "subtask"
    BUG = "bug"
    SPIKE = "spike"


class Priority(enum.Enum):
    CRITICAL = "critical"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


class NodeType(enum.Enum):
    WORK = "work"
    REVIEW = "review"
    TERMINAL = "terminal"


class ActorType(enum.Enum):
    ROLE = "role"
    AGENT = "agent"
    HUMAN = "human"
    PROJECT_MANAGER = "project_manager"


class ExecutionStatus(enum.Enum):
    PENDING = "pending"
    ACTIVE = "active"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    # #806: a stale ``in_progress`` claim whose worker session went
    # away. Recovery marks the active execution as ``ABANDONED`` and
    # leaves the row + completed prior visits intact, so the next
    # claim resumes the same node with full visit history (no
    # silent restart-from-flow-start).
    ABANDONED = "abandoned"


class Decision(enum.Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


class OutputType(enum.Enum):
    CODE_CHANGE = "code_change"
    ACTION = "action"
    DOCUMENT = "document"
    MIXED = "mixed"


class ArtifactKind(enum.Enum):
    COMMIT = "commit"
    FILE_CHANGE = "file_change"
    ACTION = "action"
    NOTE = "note"


class LinkKind(enum.Enum):
    BLOCKS = "blocks"
    RELATES_TO = "relates_to"
    SUPERSEDES = "supersedes"
    PARENT = "parent"


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------

TERMINAL_STATUSES = frozenset({WorkStatus.DONE, WorkStatus.CANCELLED})


@dataclass(slots=True)
class GateResult:
    """Result of evaluating a gate precondition."""

    passed: bool
    reason: str
    gate_name: str = ""
    gate_type: str = ""  # "hard" or "soft"


@dataclass(slots=True)
class ContextEntry:
    """Append-only log entry attached to a task.

    ``entry_type`` classifies the row so inbox/chat consumers can filter
    without touching the text: ``"note"`` (default, generic context),
    ``"reply"`` (user chat reply in the inbox thread view), or ``"read"``
    (read-marker so repeat opens don't re-emit the read event).
    """

    actor: str
    timestamp: datetime
    text: str
    entry_type: str = "note"


@dataclass(slots=True)
class Artifact:
    """A concrete output artifact produced by a work node."""

    kind: ArtifactKind
    description: str
    ref: str | None = None
    path: str | None = None
    external_ref: str | None = None


@dataclass(slots=True)
class WorkOutput:
    """Proof of work attached to a FlowNodeExecution."""

    type: OutputType
    summary: str
    artifacts: list[Artifact] = field(default_factory=list)


@dataclass(slots=True)
class Transition:
    """Record of a work_status change."""

    from_state: str
    to_state: str
    actor: str
    timestamp: datetime
    reason: str | None = None


# ---------------------------------------------------------------------------
# Flow models
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FlowNode:
    """A single node in a flow graph."""

    name: str
    type: NodeType
    actor_type: ActorType | None = None
    actor_role: str | None = None
    agent_name: str | None = None
    next_node_id: str | None = None
    reject_node_id: str | None = None
    gates: list[str] = field(default_factory=list)
    # Optional per-node wall-clock budget in seconds. ``None`` means
    # "no flow-level cap — whatever the session runtime imposes wins."
    # Consumed by the planner's per-stage budget helper (pp07); other
    # flows may adopt the same field if they want policy-driven caps.
    budget_seconds: int | None = None


@dataclass(slots=True)
class FlowTemplate:
    """An immutable flow definition governing a task lifecycle."""

    name: str
    description: str
    roles: dict[str, dict] = field(default_factory=dict)
    nodes: dict[str, FlowNode] = field(default_factory=dict)
    start_node: str = ""
    version: int = 1
    is_current: bool = True


@dataclass(slots=True)
class FlowNodeExecution:
    """Per-task, per-node, per-visit execution record."""

    task_id: str
    node_id: str
    visit: int
    status: ExecutionStatus = ExecutionStatus.PENDING
    work_output: WorkOutput | None = None
    decision: Decision | None = None
    decision_reason: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Task:
    """The atomic unit of work tracked by the service.

    ``owner`` and ``blocked`` are derived properties, not stored columns.
    """

    # --- Identity ---
    project: str
    task_number: int
    title: str
    type: TaskType
    labels: list[str] = field(default_factory=list)

    # --- State ---
    work_status: WorkStatus = WorkStatus.DRAFT
    flow_template_id: str = ""
    flow_template_version: int = 1
    current_node_id: str | None = None
    assignee: str | None = None
    priority: Priority = Priority.NORMAL
    requires_human_review: bool = False

    # --- Content ---
    description: str = ""
    acceptance_criteria: str | None = None
    constraints: str | None = None
    relevant_files: list[str] = field(default_factory=list)
    context: list[ContextEntry] = field(default_factory=list)

    # --- Relationships ---
    parent_project: str | None = None
    parent_task_number: int | None = None
    blocks: list[tuple[str, int]] = field(default_factory=list)
    blocked_by: list[tuple[str, int]] = field(default_factory=list)
    relates_to: list[tuple[str, int]] = field(default_factory=list)
    children: list[tuple[str, int]] = field(default_factory=list)
    supersedes_project: str | None = None
    supersedes_task_number: int | None = None
    superseded_by_project: str | None = None
    superseded_by_task_number: int | None = None

    # --- Plan task evolution (#1398) ---
    # ``plan_version`` increments on every refinement of a plan task
    # (same task_id, new revision). Defaults to 1.
    # ``predecessor_task_id`` points to the prior attempt when a
    # replan creates a NEW task instead of refining the existing one;
    # canonical ``project/task_number`` form, ``None`` for originals.
    plan_version: int = 1
    predecessor_task_id: str | None = None

    # --- Roles ---
    roles: dict[str, str] = field(default_factory=dict)

    # --- Sync ---
    external_refs: dict[str, str] = field(default_factory=dict)

    # --- Audit ---
    created_at: datetime | None = None
    created_by: str = ""
    updated_at: datetime | None = None
    transitions: list[Transition] = field(default_factory=list)

    # --- Flow execution (loaded separately, not always populated) ---
    executions: list[FlowNodeExecution] = field(default_factory=list)

    # --- Token usage (derived SUM over work_sessions; #86) ---
    # Populated by the service layer from the ``work_sessions`` table.
    # Not stored on ``work_tasks`` — recomputed on read. ``session_count``
    # is the number of worker-session rows bound to this task.
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    session_count: int = 0

    # --- Derived properties ---

    @property
    def owner(self) -> str | None:
        """Who owes the next action, derived from the current node's actor + roles."""
        if self.current_node_id is None:
            return None
        # The actual resolution requires the flow template; as a convenience
        # property on the dataclass we return the assignee when in a work node,
        # but real resolution happens in the service layer.
        return self.assignee

    @property
    def blocked(self) -> bool:
        """True if any blocked_by task is not in a terminal state.

        At the model level this simply checks whether blocked_by is non-empty.
        Full resolution (checking each blocker's status) happens in the service.
        """
        return self.work_status == WorkStatus.BLOCKED

    @property
    def task_id(self) -> str:
        """Canonical string identifier: ``project/task_number``."""
        return f"{self.project}/{self.task_number}"

    # Convenience serialisation helpers

    def labels_json(self) -> str:
        return json.dumps(self.labels)

    def relevant_files_json(self) -> str:
        return json.dumps(self.relevant_files)

    def roles_json(self) -> str:
        return json.dumps(self.roles)

    def external_refs_json(self) -> str:
        return json.dumps(self.external_refs)


# ---------------------------------------------------------------------------
# Worker session record
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class WorkerSessionRecord:
    """Persistent row for a worker-session binding.

    Captures the full row shape used by ``SessionManager`` so the protocol
    can describe session lookups without exposing SQL or requiring callers
    to reach into ``SQLiteWorkService._conn`` (#105).
    """

    task_project: str
    task_number: int
    agent_name: str
    pane_id: str | None
    worktree_path: str | None
    branch_name: str | None
    started_at: str
    ended_at: str | None = None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    archive_path: str | None = None
    # #809: persisted at launch so per-task transcript archival
    # at teardown can find the right Claude/Codex tree without
    # depending on the supervisor process's ambient env. ``None``
    # means "fall back to ambient env" (legacy / unconfigured).
    provider: str | None = None
    provider_home: str | None = None


@dataclass(slots=True)
class DigestRollupCandidate:
    """Typed row consumed by milestone digest rollup code."""

    source: str
    row_id: int
    subject: str
    body: str
    actor: str
    created_at: str
    payload: dict[str, Any] = field(default_factory=dict)
