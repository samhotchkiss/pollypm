# [CANCELLED] Done: pollypm stuck drafts cleared

**Action taken:** Cancelled stale draft tasks  through .

**Why:**
- , , and  were old rejection-notice drafts. Their underlying tasks (, , ) have since been reworked and approved.
- , , and  were obsolete audit smoke-test drafts with no actionable task body.

**Verify:** Run ID:       pollypm/14
Title:    Rejected pollypm/10 — A1: Work-Service Contract Pack
Status:   cancelled
Priority: high
Project:  pollypm
Type:     task
Desc:     Confidence: 9/10 — Criterion 7 failed: task worktree /Users/sam/dev/pollypm/.pollypm/worktrees/pollypm-10 is not clean; git status shows untracked .pollypm-task-prompt.md. Criterion 1/2 also failed: docs/work-service-contract-pack.md says every claim is tested, but §3.8 cancel/hold/resume, required-role failure, human-review queue gate, unknown OutputType, skip-gate transition recording, and claim atomic/provisioning behavior are documented without corresponding tests in tests/test_work_service_contract_pack.py. The doc also contradicts itself: line 157 says rework returns via claim, while lines 240-245 correctly say re-claim fails. Remove or commit the untracked file, fix the rework diagram/text, and either add contract-pack tests for the documented guarantees or narrow the doc/checklist to what is actually covered.

Task `pollypm/10` was rejected by `russell` and returned to rework.

Current stage: `build`

Open the linked task in the task cockpit, or jump to the inbox thread to
review the full rejection note before the worker continues.
Roles:    {"requester": "russell", "operator": "user"}
Tokens:   in=0 out=0 sessions=0 through ID:       pollypm/19
Title:    audit re-review final smoke
Status:   cancelled
Priority: normal
Project:  pollypm
Type:     task
Roles:    {"worker": "claude", "reviewer": "codex"}
Tokens:   in=0 out=0 sessions=0; each now reports .
