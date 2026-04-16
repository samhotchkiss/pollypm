"""wait_for_children gate — holds a task until all child tasks terminal.

Used by the plan_project flow's critic_panel stage: the architect spawns
5 critic subtasks in parallel, and the flow must not advance to
synthesize until every critic task is ``done`` or ``cancelled``.

This mirrors the built-in ``all_children_done`` gate but uses the
``get_task`` callable that the work service passes through ``kwargs``
so the gate stays service-agnostic. When no resolver is provided and
the task has no children, we pass vacuously rather than block.
"""

from __future__ import annotations

from typing import Any

from pollypm.work.models import GateResult, Task, TERMINAL_STATUSES


class WaitForChildren:
    name = "wait_for_children"
    gate_type = "hard"

    def check(self, task: Task, **kwargs: Any) -> GateResult:
        if not task.children:
            return GateResult(
                passed=True,
                reason="No child tasks; nothing to wait on.",
            )

        get_task = kwargs.get("get_task")
        if get_task is None:
            return GateResult(
                passed=False,
                reason=(
                    "wait_for_children needs a get_task resolver to check "
                    f"{len(task.children)} child tasks; none provided."
                ),
            )

        unfinished: list[str] = []
        for project, number in task.children:
            try:
                child = get_task(f"{project}/{number}")
            except Exception as exc:  # noqa: BLE001
                return GateResult(
                    passed=False,
                    reason=f"Failed to resolve child {project}/{number}: {exc}",
                )
            if child.work_status not in TERMINAL_STATUSES:
                unfinished.append(
                    f"{project}/{number}={child.work_status.value}"
                )

        if unfinished:
            return GateResult(
                passed=False,
                reason=(
                    f"{len(unfinished)} child task(s) not terminal: "
                    + ", ".join(unfinished)
                ),
            )
        return GateResult(
            passed=True,
            reason=f"All {len(task.children)} child task(s) terminal.",
        )
