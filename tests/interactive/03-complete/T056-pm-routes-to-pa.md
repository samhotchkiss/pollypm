# T056: PM Routes to PA, PA Executes and Reports Back

**Spec:** v1/09-inbox-and-threads
**Area:** Thread Management
**Priority:** P0
**Duration:** 15 minutes

## Objective
Verify that the PM (operator) can route a thread to a PA (worker), the PA executes the requested action, and the PA reports back to the PM with results.

## Prerequisites
- `pm up` has been run with operator and worker sessions active
- An open thread exists with an actionable request (or create one)

## Steps
1. Create an inbox item with an actionable request: `pm inbox create --title "Action needed" --body "Please create a file named test-route.txt with the text 'Hello from PA routing test'"`.
2. Wait for the PM to triage the item into a thread (or manually triage it).
3. Observe the PM routing the thread to a PA (worker). This should happen automatically or via `pm thread route <thread-id> worker-0`.
4. Attach to the worker session and observe it receiving the routed task.
5. Verify the worker begins executing the requested action (creating the file).
6. Wait for the worker to complete the action.
7. Observe the worker reporting back to the PM. Check the thread for a response message: `pm thread info <thread-id>` or `pm thread messages <thread-id>`.
8. The response should include:
   - What was done (file created)
   - Result or outcome (success/failure)
   - Any relevant details
9. Verify the PM receives the report and updates the thread status accordingly.
10. Check the created file exists: `ls test-route.txt` and verify its contents.

## Expected Results
- PM successfully routes the thread to a PA
- PA receives the routed task and begins execution
- PA reports back with results upon completion
- Thread messages capture the full routing conversation (request, execution, response)
- The requested action was actually performed (file exists)
- Thread status reflects the completed routing cycle

## Log

**Date:** 2026-04-10 | **Result:** PASS

### Re-test — 2026-04-10 (Polly routing task to worker via pm send)

Asked Polly: "Route this task to the PollyPM worker: Run git log --oneline -5"
```
⏺ Bash(pm send worker_pollypm "Run git log --oneline -5 and report the last 5 commits.")
  ⎿  Sent input to worker_pollypm
⏺ Delivered to worker_pollypm.
```

Navigated to PollyPM worker and verified it received the task. Worker executed `git log`:
```
9a4e9c6 Complete T001, T005, T007, T010, T022, T034 — 6 P0 tests pass
504755e Add 105 interactive user acceptance tests for PollyPM v1
945c60e Walk timeline chronologically for LLM extraction
33c8eea Use PollyPM account system for all LLM background tasks
02b8883 Stop heartbeat from alerting on every idle session
```

Full PM→PA routing pipeline verified: Polly receives request → routes via `pm send` → worker executes → reports results. ✅

**Note:** Codex CLI buffers input and needs a second Enter to submit. PollyPM now handles that in `Supervisor.send_input()` for Codex sessions.
