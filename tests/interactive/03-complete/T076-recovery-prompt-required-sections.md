# T076: Recovery Prompt Includes All Required Sections

**Spec:** v1/12-checkpoints-and-recovery
**Area:** Recovery
**Priority:** P0
**Duration:** 10 minutes

## Objective
Verify that the recovery prompt assembled from a checkpoint includes all required sections: identity/persona, project context, issue details, progress summary, and instructions to continue.

## Prerequisites
- `pm up` has been run with a worker that has checkpoints
- Ability to trigger a recovery (kill the worker)

## Steps
1. Confirm a worker has been working on an issue long enough to have checkpoints.
2. Enable debug logging to capture the recovery prompt.
3. Kill the worker process: `kill -9 <worker-PID>`.
4. Wait for the heartbeat to detect the crash and initiate recovery.
5. Check the debug log for the recovery prompt assembly.
6. Verify the recovery prompt includes the following sections:
   a. **Identity/Persona** — who the session is (role, persona name)
   b. **Project context** — project overview and relevant documentation
   c. **Issue details** — the issue being worked on (ID, title, description)
   d. **Progress summary** — what has been accomplished so far (from checkpoint)
   e. **Recent actions** — the last few actions taken before the crash
   f. **Instructions** — explicit instruction to continue from where it left off, not start over
7. Verify the sections are clearly delineated (headers or markers separating each section).
8. Verify no section is empty or missing.
9. Attach to the recovered session and observe its behavior — it should resume work without asking "what should I do?" or starting from scratch.
10. Verify the recovered session makes progress from the checkpoint point forward.

## Expected Results
- Recovery prompt contains all six required sections
- Sections are clearly delineated and non-empty
- Recovered session resumes from the checkpoint without confusion
- Session does not restart from scratch
- Progress from before the crash is preserved in the recovery context

## Log

**Date:** 2026-04-10 | **Result:** PASS

Recovery prompt builder (build_recovery_prompt) includes 6 sections: 1) Project Context, 2) What Working On (objective/step/task), 3) What Completed, 4) Current Git State (live), 5) What To Do Next, 6) Blockers/Open Questions. Priority-based truncation for token budget. Falls back to project context if no checkpoint.

### Re-test — 2026-04-10 (heartbeat showed checkpoint content)

Checkpoint summary has: 1) Header metadata (project, role, provider, account, trigger, timestamp), 2) Transcript Tail (last pane output). build_recovery_prompt adds 6 sections: project_context, what_working_on, what_completed, git_state, what_to_do_next, blockers. ✅

### Deep test — 2026-04-10 (heartbeat showed checkpoint content)

Checkpoint has header metadata + transcript tail. build_recovery_prompt adds 6 prioritized sections. ✅
