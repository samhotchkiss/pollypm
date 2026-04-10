# T096: Complete Project Lifecycle: Create, Assign, Complete, Review, Close

**Spec:** End-to-End Workflows
**Area:** Full Lifecycle
**Priority:** P0
**Duration:** 30 minutes

## Objective
Verify the complete project lifecycle end-to-end: create a project, create an issue, have it assigned to a worker, worker completes the task, PM reviews and approves, issue is closed.

## Prerequisites
- Polly is installed and configured with at least one account
- `pm down` has been run (clean slate)
- A git repository for the project

## Steps
1. Create a new project: `pm project create --name "lifecycle-test" --path /tmp/lifecycle-test`. Verify the project is created with `pm project list`.
2. Run `pm up` and wait for all sessions to become healthy. Verify with `pm status`.
3. Create an issue: `pm issue create --title "Add greeting module" --body "Create a file named greeting.py that contains a function greet(name) which returns 'Hello, {name}!'. Include a unit test."`.
4. Move the issue to ready: `pm issue transition <id> ready`.
5. Wait for the operator to pick up the issue and assign it to a worker (up to 60 seconds). Monitor with `pm status` and `pm issue info <id>`.
6. Verify the issue is now "in_progress" with a worker assigned: `pm issue info <id>`.
7. Attach to the worker session and observe it creating the greeting.py file and writing the test.
8. Wait for the worker to complete and move the issue to "review" (this may take 5-10 minutes).
9. Verify the issue is in "review": `pm issue info <id>`.
10. Observe the operator reviewing the work:
    - Does greeting.py exist with the correct function?
    - Does the test pass?
11. Wait for the operator to approve and move the issue to "done."
12. Verify: `pm issue info <id>` shows status "done."
13. Verify the files exist: `cat /tmp/lifecycle-test/greeting.py` and verify the function is correct.
14. Run `pm down` and clean up: `rm -rf /tmp/lifecycle-test`.

## Expected Results
- Complete lifecycle from project creation to issue closure works without manual intervention
- Issue flows through all states: open -> ready -> in_progress -> review -> done
- Worker creates the requested files correctly
- PM reviews and approves the work
- All transitions are logged and visible
- Files created by the worker are functional

## Log
