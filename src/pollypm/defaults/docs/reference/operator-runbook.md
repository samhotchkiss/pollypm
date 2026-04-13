# Operator Runbook

Step-by-step procedures for common operations. Read ONLY the section you need — use the line numbers to jump directly to it.

## Table of Contents

| Procedure | Line |
|-----------|------|
| Switch a Worker's Provider (Claude ↔ Codex) | 21 |
| Start a New Worker | 40 |
| Restart a Stuck Worker | 53 |
| Add a New Project | 61 |
| Send a Message to the User | 74 |
| Respond to an Inbox Item | 92 |
| Deploy a Site with ItsAlive | 102 |
| Delegate Work (Do NOT Implement Yourself) | 118 |
| Review Worker Output | 138 |
| Handle a Heartbeat Escalation | 150 |
| Check System Health | 166 |

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

**Verify it worked:** After switching, run `pm status <session_name>` and check that the provider matches. Also check the tmux pane — it should show the new provider's CLI prompt, not the old one.

**Do NOT** just edit pollypm.toml and expect the session to restart. The old process keeps running.

## Start a New Worker

```bash
pm worker-start <project_key>
# Example: pm worker-start pollypm_website
```

This creates a managed worker session (separate tmux window) for the project. Then send it work:

```bash
pm send <worker_session_name> "Your task: ..."
```

## Restart a Stuck Worker

1. Check what's wrong: `pm status <session_name>`
2. Check alerts: `pm alerts`
3. Try sending instructions first: `pm send <session_name> "Continue with..." --force`
4. If that doesn't work, restart: `pm worker-start <project_key>` (this will relaunch)
5. If recovery limit was hit: `pm reset` clears counters, then `pm worker-start`

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

The user will archive the thread when they're satisfied. You cannot close threads where the user asked for action — only the user can archive those.

## Respond to an Inbox Item

```bash
pm mail                    # list open items
pm mail <id>              # read a specific message/thread
pm reply <id> "response"  # reply to a thread
```

When you reply, ownership flips to the user and they get notified. Do NOT close — the user archives.

## Deploy a Site with ItsAlive

```bash
# From the project directory:
cd <project_path>
pm itsalive deploy --project <key> --subdomain <name> --email <email> --dir <build_dir>
```

If this returns `status=pending_verification`, the user needs to click a verification email. Send them an inbox notification:

```bash
pm notify "Deploy pending: email verification needed" "A verification email was sent to <email>. Click the link to complete the deploy to <subdomain>.itsalive.co."
```

After verification, the deploy resumes automatically on the next heartbeat sweep.

## Delegate Work (Do NOT Implement Yourself)

You are the operator. Workers implement. Always delegate using inbox messages — this creates a thread the worker can reply to, and ensures the user gets notified of the result.

```bash
# Start or find a worker
pm worker-start <project_key>

# Assign work via inbox (preferred — creates a trackable thread)
pm notify "Task: <description>" "<detailed instructions>" --to worker_<project_key> --sender polly

# The delivery system sends it to the worker's session automatically.
# The worker replies via pm reply when done, which notifies you.
# You then notify the user with the result.
```

Do NOT use `pm send` for task assignments — it types raw text into tmux with no history and no reply path. Use `pm notify --to` instead.

Never use Claude's Agent tool or create ad hoc tmux panes. Always use managed workers.

## Review Worker Output

1. `pm status` — find which workers are done/idle
2. Mount the worker in the cockpit (click it in the rail) to read its output
3. Or check git: `cd <project_path> && git log --oneline -5`
4. Send feedback or next task: `pm send <worker> "Good. Now do X."`
5. When the top-level task is complete, notify the user:

```bash
pm notify "Done: <task>" "What was accomplished, key commits, how to verify."
```

## Handle a Heartbeat Escalation

When you receive an `[Escalation]` inbox item from heartbeat:

1. Read the escalation: `pm mail <id>`
2. Check the session: `pm status <session_name>`
3. Try to fix it:
   - Send instructions: `pm send <session> "..." --force`
   - Restart the worker: `pm worker-start <project_key>`
   - Switch provider if needed: `pm switch-provider <session> claude`
4. Reply to the thread with what you did: `pm reply <id> "Restarted the worker and sent new instructions"`
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
```
