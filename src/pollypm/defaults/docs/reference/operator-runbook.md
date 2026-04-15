# Operator Runbook

Step-by-step procedures for common operations.

## Table of Contents

| Procedure | Line |
|-----------|------|
| Delegate Work to a Worker | 20 |
| Review Worker Output | 42 |
| Switch a Worker's Provider (Claude ↔ Codex) | 58 |
| Start a New Worker | 76 |
| Restart a Stuck Worker | 85 |
| Add a New Project | 97 |
| Send a Message to the User | 108 |
| Respond to an Inbox Item | 124 |
| Deploy a Site with ItsAlive | 134 |
| Handle a Heartbeat Escalation | 152 |
| Check System Health | 166 |

## Delegate Work to a Worker

You are the operator. Workers implement. Dispatch all work through the task system:

```bash
# Create a task with clear description and acceptance criteria
pm task create "Title" -p <project_key> \
  -d "Description. Acceptance criteria: ..." \
  -f standard --priority normal \
  -r worker=worker -r reviewer=russell

# Queue it so the worker can pick it up
pm task queue <project>/<number>
```

The heartbeat nudges idle workers to claim queued tasks automatically.

To check on progress:

```bash
pm task status <project>/<number>    # flow state, current node, owner
pm task list -p <project>            # all tasks for project
```

Always use managed workers. Never use Claude's Agent tool or create ad hoc tmux panes.

## Review Worker Output

When a task reaches the review node:

```bash
pm task status <project>/<number>    # see work output summary
```

You can also mount the worker in the cockpit (click PM Chat in the rail) to read its full output, or check git: `cd <project_path> && git log --oneline -5`

Then approve or reject:

```bash
pm task approve <id> --actor russell --reason "Looks good"
pm task reject <id> --actor russell --reason "Specific, actionable feedback"
```

When the top-level goal is complete, notify the user:

```bash
pm notify "Done: <task>" "What was accomplished, key commits, how to verify."
```

## Switch a Worker's Provider (Claude ↔ Codex)

Changing the config file is NOT enough — the running tmux session must be restarted.

```bash
pm switch-provider <session_name> <provider>
# Example: pm switch-provider worker_pollypm_website claude
```

This command:
1. Saves a checkpoint of the current session
2. Stops the old session (kills the tmux window)
3. Updates the config to the new provider/account
4. Relaunches with the new provider and injects a recovery prompt

**Verify it worked:** After switching, run `pm status <session_name>` and check that the provider matches.

## Start a New Worker

```bash
pm worker-start <project_key>
# Example: pm worker-start pollypm_website
```

This creates a managed worker session (separate tmux window) for the project. Then create and queue a task to give it work.

## Restart a Stuck Worker

1. Check what's wrong: `pm status <session_name>`
2. Check alerts: `pm alerts`
3. Check if the worker has queued tasks: `pm task next -p <project>`
4. If recovery limit was hit: `pm reset` clears counters
5. Restart: `pm worker-start <project_key>` (this will relaunch)
6. Queue a task if the worker needs new work:
   ```bash
   pm task create "Continue: <description>" -p <project> -d "..." -f standard -r worker=worker -r reviewer=russell
   pm task queue <project>/<number>
   ```

## Add a New Project

```bash
pm add-project <path>
# Example: pm add-project /Users/sam/dev/new-project
```

This registers the project, scaffolds `.pollypm/` docs, and runs the history import pipeline. Then start a worker for it:

```bash
pm worker-start <project_key>
```

## Send a Message to the User

The user may not be watching your session. Use inbox:

```bash
pm notify "<subject>" "<body>"
```

This creates an inbox item owned by the user. They'll see it in the cockpit inbox.

**After acting on an inbox item:** Reply to the thread, don't just close it:

```bash
pm reply <message_id> "Here's what I did: ..."
```

The user will archive the thread when they're satisfied.

## Respond to an Inbox Item

```bash
pm mail                    # list open items
pm mail <id>              # read a specific message/thread
pm reply <id> "response"  # reply to a thread
```

When you reply, ownership flips to the user and they get notified.

## Deploy a Site with ItsAlive

```bash
# From the project directory:
cd <project_path>
pm itsalive deploy --project <key> --subdomain <name> --email <email> --dir <build_dir>
```

If this returns `status=pending_verification`, the user needs to click a verification email. Send them an inbox notification:

```bash
pm notify "Deploy pending: email verification needed" "A verification email was sent to <email>. Click the link to complete the deploy."
```

After verification, the deploy resumes automatically on the next heartbeat sweep.

## Handle a Heartbeat Escalation

When you receive an `[Escalation]` inbox item from heartbeat:

1. Read the escalation: `pm mail <id>`
2. Check the session: `pm status <session_name>`
3. Try to fix it:
   - Restart the worker: `pm worker-start <project_key>`
   - Switch provider if needed: `pm switch-provider <session> claude`
4. Reply to the thread with what you did: `pm reply <id> "Restarted the worker"`
5. Only escalate to the user if you genuinely can't fix it:
   ```bash
   pm notify "[Escalation] <subject>" "I tried X and Y but the session is still stuck because Z. Need your help."
   ```

## Check System Health

```bash
pm status          # all sessions
pm alerts          # open alerts
pm debug           # diagnostics
pm mail            # inbox items
pm task counts     # task counts across projects
```
