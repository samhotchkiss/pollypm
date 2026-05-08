# [CANCELLED] Rejected pollypm/13 — A4: Release Gate Golden Path

Confidence: 9/10 — Criterion 1 failed: the golden-path release gate does not run from the documented command in a fresh checkout. README.md:220, docs/golden-path-gate.md:14-16, and scripts/golden_path_gate.py:18-20 all tell operators to run , but with a fresh UV_PROJECT_ENVIRONMENT that installs only project deps, that exits 2 with  because pytest is only in the dev extra. Verified  fails, while  passes. Fix the runner/docs/README so the published command installs or invokes the dev test dependency reliably, then resubmit. Worktree is clean.

Task `pollypm/13` was rejected by `russell` and returned to rework.

Current stage: `build`

Open the linked task in the task cockpit, or jump to the inbox thread to
review the full rejection note before the worker continues.
