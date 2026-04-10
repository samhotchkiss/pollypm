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
