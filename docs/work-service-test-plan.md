# Work Service Integration Test Plan

**Goal**: Validate the work service end-to-end as a real user would experience it. The user ONLY interacts through conversation with PollyPM agents via tmux. The user never runs CLI commands directly — that's the agents' job. CLI commands may be used by the tester ONLY for troubleshooting when a bug is detected, and the bug must then be fixed.

**Context**: A new sealed work service was built in `src/pollypm/work/`. It provides task management with directed-graph flows, gates, dependencies, context logs, sync adapters, and a CLI. The spec is at `docs/work-service-spec.md`. The implementation has 318 unit/integration tests passing, but it has NOT been tested through the actual user experience.

**Interaction model**: The user talks to Polly (the operator PM) through the tmux session at `pollypm-storage-closet:pm-operator`. Polly manages work by running `pm task` and `pm flow` commands internally. Workers run in their own tmux sessions and use the CLI to claim tasks, report work, etc. The user never touches any of this directly.

---

## Architecture Overview (for the tester)

### How PollyPM works

- **The user talks to Polly** via the operator tmux session. That's the only interface.
- **Polly is the PM.** She creates tasks, assigns work, reviews output, manages the pipeline.
- **Workers are agents** in their own tmux sessions. They claim tasks, write code, report results.
- **The work service** is the internal system that Polly and workers use to track state. It exposes `pm task` and `pm flow` CLI commands that agents run.
- **The user never runs CLI commands.** They say "I need X done" and Polly handles it.

### The tmux layout

```
pollypm-storage-closet:
  pm-heartbeat    — health monitoring (Session 0)
  pm-operator     — Polly, the PM (Session 1) ← USER TALKS HERE
  worker-*        — worker agents (Sessions 2+)
```

### How to interact

Send messages to Polly:
```bash
tmux send-keys -t pollypm-storage-closet:pm-operator "your message here" Enter
```

Read Polly's responses:
```bash
tmux capture-pane -t pollypm-storage-closet:pm-operator -p | tail -40
```

Check worker sessions:
```bash
tmux capture-pane -t pollypm-storage-closet:worker-pollypm -p | tail -40
```

### Key files for fixing bugs

- `src/pollypm/work/sqlite_service.py` — work service implementation
- `src/pollypm/work/cli.py` — CLI commands agents use
- `src/pollypm/work/flow_engine.py` — flow loading and validation
- `src/pollypm/work/gates.py` — gate protocol and built-in gates
- `src/pollypm/work/models.py` — data models
- `src/pollypm/work/flows/` — built-in flow YAML definitions
- `src/pollypm/agent_profiles/builtin.py` — agent prompt definitions (operator_prompt, worker_prompt)
- `src/pollypm/supervisor.py` — session management

---

## Phase 0: Integration Prerequisites

Before any user-facing testing, the work service must be wired into the agents' knowledge.

### P0-1: Polly knows about the work service

**Problem**: Polly's prompt currently has no knowledge of `pm task` or `pm flow` commands. She can't manage work through the new system unless her instructions tell her to.

**What to do**:
1. Find the operator prompt: `src/pollypm/agent_profiles/builtin.py` → `operator_prompt()`
2. Add a `<task_management>` section to her prompt that includes:
   - The `pm task` and `pm flow` command reference (create, queue, claim, done, approve, reject, etc.)
   - The flow lifecycle: create → queue → claim → node_done → approve/reject
   - The work output JSON format (what workers submit when signaling done)
   - That she should use `pm task create` for all new work
   - How to assign roles: `--role worker=<agent-name> --role reviewer=polly`
   - How to review: check work output, then `pm task approve` or `pm task reject --reason "..."`
3. Tell Polly that when the user asks for work to be done, she should:
   - Create a task with a rich description, acceptance criteria, and constraints
   - Queue it when it's ready
   - Assign it to an appropriate worker
   - Monitor progress and review when the worker signals done

### P0-2: Workers know about the work service

**Problem**: Workers need to know how to interact with assigned tasks.

**What to do**:
1. Find the worker prompt: `src/pollypm/agent_profiles/builtin.py` → `worker_prompt()`
2. Add instructions for:
   - `pm task mine --agent <my-name>` — see what's assigned to me
   - `pm task get <id>` — read full task details
   - `pm task claim <id>` — claim an assigned task
   - `pm task context <id> "message"` — log progress notes
   - `pm task done <id> --output '<json>'` — signal work is complete with output
   - The work output JSON format (how to describe what they did)
3. Tell workers that when they finish work, they must provide a work output that includes:
   - What type of work (code_change, action, document)
   - A summary of what was done
   - Specific artifacts (commit hashes, file paths, or action descriptions)

### P0-3: Verify database works

Use CLI ONLY for this verification step (this is troubleshooting, not normal operation):
```bash
cd /Users/sam/dev/pollypm
uv run pm flow list
uv run pm task list
uv run pm task counts
```

Expected: 4 flows listed, empty task list, all zero counts. If commands error, fix the bug.

### P0-4: Restart sessions with updated prompts

After updating prompts, restart operator and worker sessions so they pick up new instructions. The supervisor handles this — or kill and re-create the sessions manually if needed.

---

## Phase 1: Conversational Task Management

All interactions are through conversation with Polly. The tester talks to Polly and evaluates whether the system works.

### 1.1: Create a real task

Tell Polly (via tmux send-keys to the operator session):

> "I need a Python script that generates a project status report from our task system. It should read all tasks, group them by status, and write a markdown summary to docs/status-report.md. Create a task for this."

**Verify**:
- Polly runs `pm task create` with a clear description, acceptance criteria, and role assignments
- Polly confirms the task was created and tells you the task ID
- If Polly doesn't know the command: **BUG** — P0-1 wasn't done correctly. Fix the prompt.

### 1.2: Queue and assign

Tell Polly:

> "That looks good. Queue it up and assign it to a worker."

**Verify**:
- Polly runs `pm task queue` to move it from draft to queued
- Polly assigns it (sets the worker role if not already set)
- Polly confirms it's ready for a worker to pick up

### 1.3: Worker picks up and does the work

Check the worker session. The worker should:
- See the task (via `pm task mine` or `pm task next`)
- Claim it (via `pm task claim`)
- Read the description and acceptance criteria
- Actually implement the work (write the Python script)
- Report done with a work output that describes what was created

If the worker doesn't know the commands: **BUG** — P0-2 wasn't done correctly.

**Verify**:
- The worker claims the task
- The worker creates real files/code
- The worker signals done with a proper work output
- The task moves to review state

### 1.4: Review and approve

Tell Polly:

> "How's that task coming? Is there anything ready for review?"

**Verify**:
- Polly checks task status and sees it's in review
- Polly examines the work output
- Polly either approves or rejects with specific feedback
- If approved, the task moves to done

### 1.5: Rejection and rework

If the work wasn't good enough (or to test the rejection flow specifically):

Tell Polly:

> "I looked at the status report script. It's missing error handling for when there are no tasks. Reject it and ask the worker to fix that."

**Verify**:
- Polly runs `pm task reject` with a clear reason
- The task moves back to in_progress
- The worker gets the rejection feedback
- The worker addresses the feedback and signals done again
- Polly reviews again and approves

### 1.6: Multiple tasks and dependencies

Tell Polly:

> "I need two more things done. First, add a configuration file for the status report (which directory to scan, output path, etc.). Second, add a CLI entry point so users can run it as a command. The CLI depends on the config being done first."

**Verify**:
- Polly creates both tasks
- Polly creates a dependency (config blocks CLI)
- The config task is available for pickup; the CLI task is blocked
- After config is done, the CLI task becomes available
- Both complete through the full lifecycle

### 1.7: Check project status

Tell Polly:

> "Give me an overview of where things stand on this project. What's done, what's in progress, what's blocked?"

**Verify**:
- Polly runs `pm task counts` and/or `pm task list` to get status
- Polly gives you a clear summary
- The numbers match reality

### 1.8: Spike/research task

Tell Polly:

> "Before we do anything else, I want someone to research what markdown libraries are available for Python. This is just research, no review needed."

**Verify**:
- Polly creates a task with the `spike` flow (no review stage)
- Worker picks it up, does research, signals done
- Task goes straight to done (no review step)

### 1.9: Bug flow

Tell Polly:

> "There's a bug — the heartbeat sometimes misclassifies idle sessions as stuck. Create a bug task to fix it."

**Verify**:
- Polly creates the task with the `bug` flow (reproduce → fix → code_review → done)
- The flow has a reproduce node before the fix node
- Worker must reproduce the bug first, then fix it, then it goes to review

### 1.10: User-review flow

Tell Polly:

> "I want to personally review the next task, not you. Create a task for updating the project description and set it up so I review it, not you."

**Verify**:
- Polly creates the task with the `user-review` flow
- The review node has `actor_type: human` 
- When the worker signals done, the review lands in the user's inbox (not Polly's)
- The user must approve or reject, not Polly

### 1.11: Context log through conversation

Tell Polly:

> "Add a note to task #1 that we decided to use JSON format for the status report output."

**Verify**:
- Polly runs `pm task context` to add the note
- Asking Polly "what notes are on task #1?" returns the note
- Context entries show the actor (polly) and timestamp

### 1.12: Sync adapter — file projection

After tasks have been created and moved through states:

**Verify** (tester checks filesystem — this is troubleshooting, not normal operation):
- An `issues/` directory exists in the project root
- Task files appear in the correct state subdirectories (e.g., `issues/05-completed/0001-*.md`)
- Moving a task changes which folder the file is in
- File content includes the task title and description

**If files don't appear**: The file sync adapter (`src/pollypm/work/sync_file.py`) may not be registered in the running system. **BUG** — needs to be wired into the work service lifecycle (on_create/on_transition hooks).

### 1.13: Migration from existing issues

If the project has existing issues in the old `issues/` format:

Tell Polly:

> "We have old issues in the issues/ directory from before the new task system. Can you migrate them?"

**Verify**:
- Polly runs the migration tool
- Old issues appear as tasks in the work service
- Task numbers match the original issue numbers
- Descriptions match the original file content
- Completed issues show as done
- Running migration twice doesn't create duplicates

### 1.14: Worker session lifecycle

When a worker claims a task:

**Verify** (tester observes tmux — this is troubleshooting):
- A git worktree is created for the task
- The worker operates in the worktree (not the main working directory)
- When the task reaches done, the JSONL transcript is archived to `.pollypm/transcripts/tasks/<task-id>/`
- Token usage is recorded on the task
- The worktree is cleaned up after completion

**If sessions don't bind to tasks**: The session manager (`src/pollypm/work/session_manager.py`) may not be hooked into the claim/completion lifecycle. **BUG**.

### 1.15: Parallel workers

Tell Polly:

> "I have three independent tasks. Can you run them in parallel with different workers?"

**Verify**:
- Polly creates multiple tasks and assigns them to different workers (or multiple instances)
- Each worker gets its own worktree
- Workers don't interfere with each other
- All tasks complete independently

---

## Phase 2: Edge Cases and Error Recovery

### 2.1: Polly handles bad requests gracefully

Tell Polly things that should fail:

> "Queue that task we already finished."

**Verify**: Polly gets an error from the work service and communicates it clearly to the user (not a raw stack trace).

### 2.2: Cancelled blocker

Tell Polly:

> "Cancel the config task. What happens to the CLI task that depends on it?"

**Verify**: The dependent task stays blocked (cancelled doesn't auto-unblock). Polly should flag this and ask what to do.

### 2.3: Hold and resume

Tell Polly:

> "Put that CLI task on hold. We need to think about the design first."

Then later:

> "OK resume that CLI task."

**Verify**: Task moves to on_hold, then back to queued on resume.

### 2.4: Gate enforcement through Polly

Tell Polly:

> "Create a task with no description and try to queue it immediately."

**Verify**: Polly gets a gate failure (has_description) and reports it clearly to the user. Not a stack trace — a human-readable message.

Tell Polly:

> "Have the worker try to signal done on that task without actually doing any work."

**Verify**: The worker gets a gate failure (has_work_output) and the error is clear.

### 2.5: Circular dependency prevention

Tell Polly:

> "Create two tasks. Make A depend on B, and B depend on A."

**Verify**: The second link is rejected with "circular dependency detected." Polly reports this clearly.

### 2.6: Cross-project dependencies

If multiple projects are configured:

Tell Polly:

> "Task #3 in pollypm is blocked by task #1 in otter-camp."

**Verify**: Cross-project link is created. The blocked task shows the cross-project reference.

### 2.7: Skip-gates escape hatch

Tell Polly:

> "I know the description is empty but queue it anyway. Override the gate."

**Verify**: Polly uses `--skip-gates` flag. The task is queued but with a warning logged in the transition record.

### 2.8: Plugin system verification

**Verify** (tester checks — troubleshooting):
- The MockWorkService can be swapped in for SQLiteWorkService without breaking consumers
- Custom gates placed in `~/.pollypm/gates/` or `<project>/.pollypm/gates/` are discovered and used
- Custom flow definitions in `<project>/.pollypm/flows/` override built-in flows
- The plugin registry (`src/pollypm/work/plugin_registry.py`) wires everything up correctly

---

## Phase 2.5: TUI Views

The TUI (Textual cockpit) has new views powered by the work service. These need to be tested visually by launching the cockpit and navigating.

### How to launch the cockpit

```bash
uv run pm cockpit
```

Or attach to the existing cockpit session if one is running.

### 2.5.1: Project dashboard

Click on a project in the left rail.

**Verify**:
- Summary bar shows task counts by status (e.g., "3 queued · 2 in progress · 1 review · 14 done")
- Non-terminal tasks are listed with status icon, task number, title, assignee, priority
- Status icons are correct: ⟳ for in_progress, ◉ for review, ○ for queued, ◌ for draft, ⊘ for blocked, ⏸ for on_hold, ✓ for done, ✗ for cancelled
- Recently completed tasks (up to 10) appear below the active tasks
- Counts match what Polly reports via conversation

**If the dashboard doesn't show task data**: The dashboard widgets (`src/pollypm/work/dashboard.py`) exist but may not be wired into the cockpit (`src/pollypm/cockpit.py`). This is a **BUG** — the widgets need to be integrated into the cockpit's project view.

### 2.5.2: Task detail view

Click on a task in the dashboard.

**Verify**:
- Task header shows number, title, status badge, priority
- Flow info shows current node name and flow template
- Roles are displayed (worker, reviewer)
- Description and acceptance criteria are shown
- Execution history shows each flow node visit: node name, visit number, actor, status, work output summary, reviewer decision
- Context log shows recent entries with actor and timestamp
- For completed tasks with rejections, the full history is visible (visit 1 → rejected → visit 2 → approved)

### 2.5.3: Project PM chat

Under the project in the rail, there should be a "Chat with PM" entry.

**Verify**:
- Clicking it opens the conversation with the project's PM (Polly)
- This is the same inbox channel, scoped to this project
- Messages sent here reach Polly
- Polly's responses appear here
- Escalations and status updates from Polly show up here

**If this view doesn't exist**: The inbox is the existing system. The test is whether it's accessible from the project rail. If not, **BUG** — needs UI integration.

### 2.5.4: Session list

Below the project in the rail, active worker sessions should be listed.

**Verify**:
- Active worker sessions for this project appear under a "Sessions" header
- Each session shows the task number, title, and worker name
- Clicking a session shows the live terminal output from that worker
- When there are no active sessions, the list is empty or hidden
- Switching to a different project shows that project's sessions (not the previous project's)

**If sessions aren't listed**: The session list widget (`SessionListWidget` in `src/pollypm/work/dashboard.py`) exists but may not be wired into the cockpit rail. **BUG** — needs integration.

### 2.5.5: Live session view

Click into an active worker session.

**Verify**:
- Shows real-time terminal output from the worker agent
- An input line at the bottom allows sending text to the worker (human takeover)
- Sending a message actually reaches the worker's tmux pane
- The view updates as the worker produces output

---

## Phase 3: Real Project End-to-End

Create a real, small, self-contained project and drive ALL work through Polly. The project should produce working code at the end. Ideas:

- A Python script that generates status reports from the work service
- A git pre-commit hook that validates commit message format
- A markdown link checker

The test is complete when:
- 3-5 real tasks are created by Polly with proper descriptions
- Workers implement real code
- At least one rejection/rework cycle happens from actual review
- All tasks reach done
- The final product actually works

---

## Bug Tracking

Every bug found gets a GitHub issue (`gh issue create`) with:
- **Steps to reproduce**: exact messages sent to Polly or exact behavior observed
- **Expected behavior**: what should have happened
- **Actual behavior**: what actually happened (include terminal output)
- **Severity**: blocker (can't continue), major (workflow broken), minor (cosmetic)

Fix the bug, verify the fix, close the issue, continue testing.

---

## Exit Criteria

Testing is complete when:
1. Phase 0 prerequisites are satisfied (agents know the commands)
2. All Phase 1 conversational tests pass (Polly manages tasks through conversation)
3. All Phase 2 edge cases are handled gracefully
4. At least one Phase 3 real project is completed end-to-end through Polly
5. All bugs found during testing are fixed and verified
6. Zero open issues from testing
