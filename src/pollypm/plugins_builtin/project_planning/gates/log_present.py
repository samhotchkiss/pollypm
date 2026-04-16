"""log_present gate — ``docs/planning-session-log.md`` must exist + be non-empty.

Used by the plan_project flow's synthesize stage to enforce the
architect produced the narrative session log before advancing to user
approval. Per spec §7, the session log is the durable human-readable
artifact of the planning run, and its presence is load-bearing — the
Risk Ledger references it, the replan command reads it for drift
analysis, and the user's approval decision is partly informed by it.

The gate resolves the log path against the project root via kwargs —
``project_root`` (a ``Path``) is supplied by the work service at
evaluation time. When it's not supplied we fall back to the current
working directory, which keeps unit tests simple while still failing
loudly in production if the work service forgets to pass it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pollypm.work.models import GateResult, Task


DEFAULT_LOG_PATH = "docs/planning-session-log.md"


class LogPresent:
    name = "log_present"
    gate_type = "hard"

    def check(self, task: Task, **kwargs: Any) -> GateResult:
        root = kwargs.get("project_root")
        if root is None:
            root = Path.cwd()
        log_path = Path(root) / DEFAULT_LOG_PATH

        if not log_path.is_file():
            return GateResult(
                passed=False,
                reason=(
                    f"Planning session log not found at {log_path}. "
                    "Architect must write the narrative session log "
                    "before synthesize advances."
                ),
            )

        try:
            text = log_path.read_text(encoding="utf-8")
        except OSError as exc:
            return GateResult(
                passed=False,
                reason=f"Could not read {log_path}: {exc}",
            )

        if not text.strip():
            return GateResult(
                passed=False,
                reason=f"Planning session log at {log_path} is empty.",
            )
        return GateResult(
            passed=True,
            reason=f"Session log present ({len(text)} chars) at {log_path}.",
        )
