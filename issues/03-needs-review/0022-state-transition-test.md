# 0022 State Transition Test

## Goal

Validate PollyPM's six-state task lifecycle through the task backend API rather than raw file moves, and prove that invalid skip-state transitions are rejected while the review rejection loop remains allowed.

## Acceptance Criteria

- File-backed tasks can move sequentially through `01-ready` -> `02-in-progress` -> `03-needs-review`.
- File-backed tasks can move through the review rejection loop `04-in-review` -> `02-in-progress`.
- Skip-state attempts such as `01-ready` -> `03-needs-review` fail with an explicit error.
- Service and CLI layers surface the same transition rules as the backend.
- Concrete test evidence is recorded in this issue file.

## Implementation Details

- Added shared state-machine validation in `src/pollypm/task_backends/base.py`.
- Enforced that validation in both `src/pollypm/task_backends/file.py` and `src/pollypm/task_backends/github.py`.
- Updated `src/pollypm/cli.py` so invalid transitions fail cleanly with exit code `1`.
- Added direct backend, service, and CLI regression coverage for valid sequential moves, skip-state rejection, and the review rejection loop.

## Test Evidence

- `tests/test_task_backend.py`
  - `test_file_task_backend_moves_tasks_between_states`
  - `test_file_task_backend_rejects_skipped_transition`
  - `test_file_task_backend_allows_review_rejection_loop`
- `tests/test_service_api.py`
  - `test_service_move_task_rejects_skipped_transition`
- `tests/test_cli_issue.py`
  - `test_issue_cli_rejects_skipped_transition`
- Targeted verification:
  - `uv run pytest -q tests/test_task_backend.py tests/test_service_api.py tests/test_cli_issue.py`
  - Result after enforcement slice: `38 passed in 0.54s`
- Broader workflow verification:
  - `uv run pytest -q tests/test_cli_issue.py tests/test_projects.py tests/test_task_backend.py tests/test_service_api.py tests/test_cockpit.py tests/test_control_tui.py tests/integration/test_prompt_assembly_integration.py`
  - Result after enforcement slice: `71 passed in 4.71s`

## Review Notes

- This issue's original filesystem-only verification is now superseded by API-level state machine coverage.
- The next review decision should focus on whether this documented test evidence is sufficient to close the issue, not on raw file movement through directories.
