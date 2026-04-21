"""user_level_tests_pass gate — enforces a user-level test receipt.

Used by the implement_module flow's code_review node. A worker can't
get a module approved until a user-level test (Playwright for web,
tmux-driven scenario for CLI/TUI) has produced a pass receipt at
``.pollypm/test-receipts/<task_id>.json``.

Unit tests are assumed, not sufficient. The receipt schema is
intentionally minimal for v1:

```json
{"passed": true, "details": "Playwright run 5/5 passed; screenshot <path>"}
```

The test runners that *write* receipts (Playwright wiring, tmux
scenario harness) are tracked as separate follow-ups — this gate only
reads.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pollypm.work.models import GateResult, Task


RECEIPTS_DIR = ".pollypm/test-receipts"
RECEIPT_SCHEMA_EXAMPLE = (
    '{"passed": true, "details": "Playwright 5/5 passed; screenshot report.html"}'
)


class UserLevelTestsPass:
    name = "user_level_tests_pass"
    gate_type = "hard"

    def check(self, task: Task, **kwargs: Any) -> GateResult:
        root = kwargs.get("project_root")
        if root is None:
            root = Path.cwd()

        # Task ID is stored as project/number — sanitise to a filesystem-safe
        # filename by replacing the slash. Callers may also supply an explicit
        # receipt path via kwargs for test harness flexibility.
        receipt_path = kwargs.get("receipt_path")
        if receipt_path is None:
            safe_id = f"{task.project}-{task.task_number}"
            receipt_path = Path(root) / RECEIPTS_DIR / f"{safe_id}.json"
        else:
            receipt_path = Path(receipt_path)

        if not receipt_path.is_file():
            return GateResult(
                passed=False,
                reason=(
                    f"No user-level test receipt at {receipt_path}. "
                    f"Expected a JSON object like {RECEIPT_SCHEMA_EXAMPLE}. "
                    "Fix: run the Playwright/tmux scenario, then write that "
                    "exact file before asking the reviewer to approve."
                ),
            )

        try:
            data = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return GateResult(
                passed=False,
                reason=(
                    f"Receipt at {receipt_path} is not valid JSON: {exc}. "
                    f"Expected {RECEIPT_SCHEMA_EXAMPLE}."
                ),
            )

        if not isinstance(data, dict):
            return GateResult(
                passed=False,
                reason=(
                    f"Receipt at {receipt_path} is not a JSON object "
                    f"(got {type(data).__name__}). "
                    f"Expected {RECEIPT_SCHEMA_EXAMPLE}."
                ),
            )

        passed = data.get("passed")
        details = data.get("details")
        if not isinstance(passed, bool):
            return GateResult(
                passed=False,
                reason=(
                    f"Receipt at {receipt_path} has invalid schema. "
                    "Expected boolean field 'passed' and optional string "
                    f"'details'. Example: {RECEIPT_SCHEMA_EXAMPLE}."
                ),
            )
        if details is not None and not isinstance(details, str):
            return GateResult(
                passed=False,
                reason=(
                    f"Receipt at {receipt_path} has invalid schema. "
                    "Field 'details' must be a string when present. "
                    f"Example: {RECEIPT_SCHEMA_EXAMPLE}."
                ),
            )

        if passed is True:
            detail_text = (details or "").strip()
            return GateResult(
                passed=True,
                reason=f"User-level tests pass. {detail_text}".strip(),
            )
        detail_text = (details or "no details supplied").strip()
        return GateResult(
            passed=False,
            reason=(
                f"User-level tests did not pass per {receipt_path}: "
                f"{detail_text}. Reviewer expects {RECEIPT_SCHEMA_EXAMPLE} "
                "with passed=true before code_review can pass."
            ),
        )
