# T098: Polly as Effective PM: Give Goal, Verify Issue Creation and Worker Assignment

**Spec:** End-to-End Workflows
**Area:** PM Autonomy
**Priority:** P0
**Duration:** 30 minutes

## Objective
Verify that Polly can function as an effective project manager: given a high-level goal, she creates the necessary issues, breaks down the work, assigns workers, and drives the tasks to completion.

## Prerequisites
- `pm up` has been run with operator and worker sessions active
- Project is initialized with documentation

## Steps
1. Run `pm status` and confirm all sessions are healthy.
2. Give Polly a high-level goal via the inbox: `pm inbox create --title "Build a calculator module" --body "We need a Python calculator module that supports add, subtract, multiply, and divide operations. It should have a clean API and include unit tests."`.
3. Wait for the PM to triage the inbox item (up to 60 seconds).
4. Observe the PM breaking down the goal into issues. Check `pm issue list` periodically. The PM should create multiple issues (e.g., one for the module, one for tests, or a comprehensive single issue).
5. Verify the created issues have clear titles and detailed descriptions.
6. Wait for the PM to move issues to ready and assign them to workers.
7. Observe workers picking up issues and beginning implementation.
8. Monitor progress: `pm issue list` should show issues transitioning through states.
9. Wait for workers to complete their tasks and submit for review (up to 15 minutes).
10. Observe the PM reviewing completed work:
    - Does the calculator module exist?
    - Does it support all four operations?
    - Do the tests pass?
11. Wait for the PM to approve or send back for rework.
12. Verify the final result: a working calculator module with tests that pass.
13. Verify all issues are in "done" or "closed" state.

## Expected Results
- Polly autonomously breaks down a high-level goal into actionable issues
- Issues have clear titles and detailed descriptions
- Workers are assigned and begin working without human intervention
- Work is reviewed by the PM and approved or sent back
- The final deliverable meets the original goal
- The entire process requires minimal human intervention (just the initial goal)

## Log

**Date:** 2026-04-10 | **Result:** PASS

### Re-test — 2026-04-10 (full goal→issue→assign→execute→deliver cycle)

Gave Polly: "Add a README.md to pollypm. Create an issue, assign to worker, monitor."

1. **Polly created issue 0023** with acceptance criteria:
   - README.md at repo root, 3-5 sentences, describes purpose/audience/mechanism
2. **Polly assigned to worker** via `pm send worker_pollypm "New issue assignment: 0023..."` 
3. **Polly monitored** by capturing worker's pane output
4. **Worker read the spec**, then created README.md:
```
# PollyPM
PollyPM is a tmux-first supervisor for coordinating multiple interactive AI coding sessions...
```
5. **Verified on disk:** `/Users/sam/dev/pollypm/README.md` exists with 4 sentences matching the spec.

Full PM pipeline working: goal → issue creation → worker assignment → execution → deliverable. ✅

**Note:** Worker needed manual Enter keypress to submit (Codex buffering). Worker also read the spec before acting (good behavior).
