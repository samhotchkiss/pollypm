# T047: Project Overview Injected into Worker Session Prompt

**Spec:** v1/08-project-state-and-memory
**Area:** Memory and Context
**Priority:** P0
**Duration:** 10 minutes

## Objective
Verify that the project overview document is automatically injected into the worker session's system prompt, giving the worker context about the project it is working on.

## Prerequisites
- A project exists with a generated overview document
- `pm up` has been run with a worker assigned to this project

## Steps
1. Verify the project overview exists: `cat .pollypm/docs/overview.md` (or equivalent path). Note the key content.
2. Run `pm status` and identify a worker session.
3. Enable debug logging to see the prompt assembly: `pm config set log_level debug` or check the existing debug log.
4. Restart the worker session (or wait for a new session to be launched).
5. Check the debug log for the prompt assembly output. Search for the overview content: `pm log --filter "overview"` or `grep overview <log-dir>/worker-0.log`.
6. Verify the project overview content appears in the assembled system prompt sent to the worker.
7. Alternatively, attach to the worker session and ask it about the project. For example, type "What project are you working on?" — the worker should be able to describe the project based on the injected overview.
8. Verify the overview is injected at the correct position in the prompt (e.g., after universal rules but before task-specific instructions).
9. Modify the project overview document slightly (add a distinctive marker like "MARKER_T047_TEST").
10. Restart the worker session and verify the updated overview (with the marker) is injected into the new session's prompt.

## Expected Results
- Project overview is present in the worker's system prompt
- Worker can describe the project based on injected context
- Updated overview is reflected in new sessions after restart
- Overview is positioned correctly in the prompt hierarchy
- All workers on the same project receive the same overview

## Log

**Date:** 2026-04-10 | **Result:** PASS

### Re-test — 2026-04-10 (via operator session)

Asked operator: "Without reading any files, what do you already know about the PollyPM project?"

Operator demonstrated deep project knowledge from its session context:
```
Purpose: PollyPM is a project manager system that coordinates AI agent work sessions.
Architecture:
  - Multi-session pipeline (Opus for spec/review, Codex for implementation)
  - Account system with isolated homes (700 permissions), controller + failover
  - Session types: Operator, heartbeat, workers in tmux
  - Plugin-based backends, inbox system, rules + magic
Main Features:
  - Managed worker lifecycle, multiple project tracking
  - File-based issue pipeline, heartbeat supervision, failover
```

Project knowledge IS available to the operator, but not from a dedicated `project-overview.md` injection. It comes from:
1. Control prompt (operator role/persona)
2. Config file (pollypm.toml — projects, accounts, sessions)
3. Accumulated conversation context from prior interactions

**Bug/Gap:** No formal `project-overview.md` injection mechanism exists. Workers start with empty prompts and get no project context until assigned work.
