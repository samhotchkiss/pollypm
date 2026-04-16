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


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class WorkStatus(enum.Enum):
    """Eight-state lifecycle for a task."""

    DRAFT = "draft"
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
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
    """Append-only log entry attached to a task."""

    actor: str
    timestamp: datetime
    text: str


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
