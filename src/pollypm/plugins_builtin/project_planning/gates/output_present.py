"""output_present gate — task cannot advance without a structured output.

Used by the critique_flow: each critic must emit structured JSON via
``pm task done --output`` before their critique task can terminate. The
gate inspects the task's most recent execution for a non-empty
``WorkOutput`` with at least one artifact AND a non-empty ``summary``.

Unlike the built-in ``has_work_output`` gate (which accepts any artifact),
``output_present`` insists the summary itself is non-empty so a bare
``artifacts=[]`` stub cannot satisfy it — the point of the critique is
the structured content, not just the shape of the envelope.
"""

from __future__ import annotations

from typing import Any

from pollypm.work.models import GateResult, Task


class OutputPresent:
    name = "output_present"
    gate_type = "hard"

    def check(self, task: Task, **kwargs: Any) -> GateResult:
        for execution in reversed(task.executions):
            output = execution.work_output
            if output is None:
                continue
            summary_ok = bool(output.summary and output.summary.strip())
            artifacts_ok = bool(output.artifacts)
            if summary_ok and artifacts_ok:
                return GateResult(
                    passed=True,
                    reason=(
                        f"Work output present on execution "
                        f"{execution.node_id}/visit{execution.visit}: "
                        f"{len(output.artifacts)} artifact(s)."
                    ),
                )
        return GateResult(
            passed=False,
            reason=(
                "No structured work output with summary + artifacts. "
                "Critic must call `pm task done --output '<json>'` "
                "with a non-empty payload."
            ),
        )
