"""Gate protocol, built-in gates, registry, and evaluation.

Gates are precondition checks that run before flow transitions.
Each gate inspects a task and returns a GateResult (pass/fail + reason).
Gates are typed as "hard" (blocking) or "soft" (warning only).
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pollypm.work.models import (
    ArtifactKind,
    ExecutionStatus,
    GateResult,
    Task,
    TERMINAL_STATUSES,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Gate(Protocol):
    """Protocol that all gates must satisfy."""

    name: str
    gate_type: str  # "hard" or "soft"

    def check(self, task: Task, **kwargs: Any) -> GateResult: ...


# ---------------------------------------------------------------------------
# Built-in gates
# ---------------------------------------------------------------------------


class HasDescription:
    """Task must have a non-empty description."""

    name = "has_description"
    gate_type = "hard"

    def check(self, task: Task, **kwargs: Any) -> GateResult:
        if task.description and task.description.strip():
            return GateResult(passed=True, reason="Description present.")
        return GateResult(passed=False, reason="Task has no description.")


class HasAssignee:
    """Task must have an assignee."""

    name = "has_assignee"
    gate_type = "hard"

    def check(self, task: Task, **kwargs: Any) -> GateResult:
        if task.assignee and task.assignee.strip():
            return GateResult(passed=True, reason="Assignee present.")
        return GateResult(passed=False, reason="Task has no assignee.")


class HasWorkOutput:
    """Current execution must have a work_output with at least one artifact."""

    name = "has_work_output"
    gate_type = "hard"

    def check(self, task: Task, **kwargs: Any) -> GateResult:
        # Check the most recent completed execution that has a work output.
        # On a review node, the work output lives on the preceding work node's
        # execution, not the current (review) node's execution.
        for exe in reversed(task.executions):
            if exe.work_output and exe.work_output.artifacts:
                return GateResult(
                    passed=True, reason="Work output with artifacts present."
                )
        return GateResult(
            passed=False,
            reason="No work output with artifacts found on any execution.",
        )


class HasCommits:
    """Check if task has any commit-type artifacts in its work output.

    Real git integration comes later; for now we just check artifact kinds.
    """

    name = "has_commits"
    gate_type = "soft"

    def check(self, task: Task, **kwargs: Any) -> GateResult:
        for exe in task.executions:
            if exe.work_output:
                for art in exe.work_output.artifacts:
                    if art.kind == ArtifactKind.COMMIT:
                        return GateResult(
                            passed=True, reason="Commit artifacts found."
                        )
        return GateResult(
            passed=False,
            reason="No commit artifacts found in work output.",
        )


class AcceptanceCriteria:
    """Task should have non-empty acceptance criteria (soft gate)."""

    name = "acceptance_criteria"
    gate_type = "soft"

    def check(self, task: Task, **kwargs: Any) -> GateResult:
        if task.acceptance_criteria and task.acceptance_criteria.strip():
            return GateResult(
                passed=True, reason="Acceptance criteria present."
            )
        return GateResult(
            passed=False,
            reason="Task has no acceptance criteria.",
        )


class AllChildrenDone:
    """All child tasks must be in a terminal state.

    Requires a ``get_task`` callable in kwargs to resolve children.
    """

    name = "all_children_done"
    gate_type = "hard"

    def check(self, task: Task, **kwargs: Any) -> GateResult:
        get_task = kwargs.get("get_task")
        if get_task is None:
            # No way to check children without a resolver; pass vacuously
            if not task.children:
                return GateResult(passed=True, reason="No children.")
            return GateResult(
                passed=False,
                reason="Cannot verify children: no get_task callable provided.",
            )

        for project, number in task.children:
            child = get_task(f"{project}/{number}")
            if child.work_status not in TERMINAL_STATUSES:
                return GateResult(
                    passed=False,
                    reason=(
                        f"Child {project}/{number} is in "
                        f"'{child.work_status.value}' state, not terminal."
                    ),
                )
        return GateResult(passed=True, reason="All children in terminal state.")


# ---------------------------------------------------------------------------
# Registry of all built-in gates
# ---------------------------------------------------------------------------

BUILTIN_GATES: list[type] = [
    HasDescription,
    HasAssignee,
    HasWorkOutput,
    HasCommits,
    AcceptanceCriteria,
    AllChildrenDone,
]


class GateRegistry:
    """Discovers and resolves gates by name.

    Override chain: project-local > user-global > built-in.
    """

    def __init__(
        self,
        project_path: str | Path | None = None,
        user_gates_dir: Path | None = None,
    ) -> None:
        self._gates: dict[str, Gate] = {}
        self._project_path = Path(project_path) if project_path else None
        self._user_gates_dir = user_gates_dir

        # Register built-ins first (lowest precedence)
        for cls in BUILTIN_GATES:
            instance = cls()
            self._gates[instance.name] = instance

        # Discover custom gates (higher precedence layers override)
        self._discover_custom_gates()

    def _discover_custom_gates(self) -> None:
        """Load custom gate modules from filesystem directories."""
        # User-global: ~/.pollypm/gates/
        user_dir = self._user_gates_dir or (Path.home() / ".pollypm" / "gates")
        self._load_gates_from_dir(user_dir)

        # Project-local: <project>/.pollypm/gates/
        if self._project_path is not None:
            proj_dir = self._project_path / ".pollypm" / "gates"
            self._load_gates_from_dir(proj_dir)

    def _load_gates_from_dir(self, directory: Path) -> None:
        """Load all .py files from a directory, looking for Gate implementations."""
        if not directory.is_dir():
            return
        for py_file in sorted(directory.iterdir()):
            if py_file.suffix != ".py" or not py_file.is_file():
                continue
            try:
                spec = importlib.util.spec_from_file_location(
                    f"pollypm_gate_{py_file.stem}", py_file
                )
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                # Look for objects that satisfy the Gate protocol
                for attr_name in dir(module):
                    obj = getattr(module, attr_name)
                    if (
                        isinstance(obj, type)
                        and obj is not Gate
                        and hasattr(obj, "name")
                        and hasattr(obj, "gate_type")
                        and hasattr(obj, "check")
                        and callable(getattr(obj, "check", None))
                    ):
                        instance = obj()
                        if isinstance(instance, Gate):
                            self._gates[instance.name] = instance
            except Exception:
                logger.warning(
                    "Failed to load gate from %s", py_file, exc_info=True
                )

    def get(self, name: str) -> Gate | None:
        """Look up a gate by name. Returns None if not found."""
        return self._gates.get(name)

    def all_gates(self) -> dict[str, Gate]:
        """Return all registered gates."""
        return dict(self._gates)


# ---------------------------------------------------------------------------
# Gate evaluation
# ---------------------------------------------------------------------------


def evaluate_gates(
    task: Task,
    gate_names: list[str],
    registry: GateRegistry,
    **kwargs: Any,
) -> list[GateResult]:
    """Evaluate a list of gates against a task.

    Returns a GateResult per gate name. Unknown gate names produce a
    failing hard-gate result so the caller is aware.
    """
    results: list[GateResult] = []
    for name in gate_names:
        gate = registry.get(name)
        if gate is None:
            results.append(
                GateResult(
                    passed=False,
                    reason=f"Unknown gate '{name}'.",
                )
            )
            continue
        result = gate.check(task, **kwargs)
        result.gate_name = name
        result.gate_type = gate.gate_type
        results.append(result)
    return results


def has_hard_failure(results: list[GateResult]) -> bool:
    """Return True if any hard gate failed."""
    for r in results:
        gate_type = getattr(r, "gate_type", "hard")
        if not r.passed and gate_type == "hard":
            return True
    return False
