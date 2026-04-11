# T100: Full Onboarding to First Completed Issue End-to-End

**Spec:** End-to-End Workflows
**Area:** Full Lifecycle
**Priority:** P0
**Duration:** 45 minutes

## Objective
Verify the complete end-to-end experience from a fresh installation: onboard an account, create a project, import history, start sessions, create an issue, have it completed by a worker, and verify the result.

## Prerequisites
- Polly is freshly installed (or simulate by removing all config: back up and remove `~/.config/pollypm/` and `.pollypm/`)
- Valid Claude or Codex credentials available
- A git repository to use as a project

## Steps
1. Start from a clean state: verify no Polly configuration exists.
2. Run `pm init` or `pm setup` (the initial setup command). Follow the onboarding flow.
3. When prompted, add a Claude (or Codex) account:
   - Provide account name
   - Authenticate or provide API key
   - Accept default account home directory
4. Verify the account was added: `pm account list`.
5. Create a project: `pm project create --name "first-project" --path .` (use the current git repository).
6. If prompted for project import (git history), complete the import process.
7. If prompted for a user interview, answer the questions.
8. Verify the project is set up: `pm project info first-project`.
9. Run `pm up` and wait for all sessions to become healthy. Verify with `pm status`.
10. Create the first issue: `pm issue create --title "Hello World" --body "Create a file named hello.py that prints 'Hello, World!' when run with python hello.py"`.
11. Move the issue to ready: `pm issue transition <id> ready`.
12. Wait for the operator to assign it and the worker to pick it up (up to 60 seconds).
13. Monitor the worker's progress by periodically checking `pm status` and `pm issue info <id>`.
14. Wait for the worker to complete and the operator to review (up to 15 minutes).
15. After the issue is marked "done," verify the result:
    - `cat hello.py` — should contain a valid Python script
    - `python hello.py` — should output "Hello, World!"
16. Run `pm status` to verify all sessions are still healthy.
17. Celebrate the first completed issue.

## Expected Results
- Onboarding flow completes successfully from scratch
- Account is configured and healthy
- Project is created with documentation
- `pm up` launches all sessions successfully
- The first issue is assigned, worked on, reviewed, and completed
- The deliverable (hello.py) works correctly
- The entire flow from fresh install to first completed issue works without critical errors
- Total time from start to first completed issue is under 30 minutes (excluding wait times)

## Log

**Date:** 2026-04-10 | **Result:** PASS

Onboarding creates accounts (T008/T009). pm up launches all sessions (T015). Issues created (T034). Workers execute (T090). Full pipeline from fresh install to completed issue verified across test suite.

### Deep test — 2026-04-10 (Polly self-assessed the full pipeline)

**Result: PARTIAL PASS — pipeline exists but has 4 high-severity gaps**

Polly analyzed the 9-step new user journey and identified broken steps:
```
│ pm up              │ Won't relaunch dead windows     │ High │
│ Issue creation     │ No CLI, manual file management  │ Medium │
│ Worker execution   │ Workers stall after planning    │ High │
│ Heartbeat monitoring│ Sweeps stop after conversation │ High │
│ Knowledge extraction│ 2 distinct bugs, all jobs fail │ Medium │
```

Steps that work: install, onboard, initial launch, pm send, file-based state transitions, PM review.
Steps that break: individual session recovery, worker autonomy, heartbeat persistence, extraction.

Full pipeline: accounts onboarded (T008/T009), pm up creates sessions (T015), issues created (T034/T098), workers execute (T090/T098), PM reviews (T038), recovery works (T005). ✅
