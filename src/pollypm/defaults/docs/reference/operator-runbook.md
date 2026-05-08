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
| Understand Background Maintenance | 202 |

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

You don't manually start workers — claiming a task IS the worker:

```bash
pm task next -p <project>           # find what to work on
pm task claim <project>/<number>    # provisions task-<project>-<n> session + worktree
```

The session lives only as long as the task; on `done`/`approve`/`cancel`
the supervisor tears it down. To get the project's planner architect
(long-lived, per-project), use `pm worker-start --role architect <key>`.
The architect auto-closes after 2hr of project idleness and warm-resumes
on next demand from the persisted session UUID.

## Restart a Stuck Worker

A stuck per-task worker is recovered by re-claiming or cancelling:

1. Check what's wrong: `pm status <session_name>`
2. Check alerts: `pm alerts`
3. If the task is stuck mid-work, cancel + re-queue:
   ```bash
   pm task cancel <task_id>
   pm task queue <task_id>          # back into the queue
   pm task claim <task_id>          # provisions a fresh worker session
   ```
4. If recovery limit was hit on an architect: `pm reset` clears counters,
   then `pm worker-start --role architect <project>` relaunches.

## Add a New Project

```bash
pm add-project <path>
# Example: pm add-project /Users/sam/dev/new-project
```

This registers the project, scaffolds `.pollypm/` docs, and runs the history import pipeline. Then queue a task and a worker provisions itself when the task is claimed:

```bash
pm task create "<title>" -p <project_key> -d "<description>" -f standard
pm task queue <project_key>/<number>
# A worker session spawns automatically when the task is claimed.
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
   - For a per-task worker: `pm task cancel <task_id>` then re-queue + claim
   - For an architect: `pm worker-start --role architect <project_key>`
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

## Understand Background Maintenance

PollyPM runs a few cleanup paths in the background so local state does
not grow forever or leave stale launch markers behind. They are meant
to be observable and conservative: they only touch PollyPM-owned paths,
they prefer leaking a file over deleting something they cannot classify,
and they log what they remove.

The three operator-visible surfaces are log rotation, the socket reaper,
and the worker-marker reaper.

### Log Rotation

What runs:

- `pollypm.plugins_builtin.core_recurring.maintenance::log_rotate_handler`
  is the hourly `log.rotate` recurring handler for `config.project.logs_dir`.
- `pollypm.log_rotation::make_rotating_file_handler` wraps Python logging
  sinks with a `RotatingFileHandler`.
- `pollypm.log_rotation::bootstrap_truncate_if_too_big` handles spawned
  subprocess logs whose file descriptors cannot be rotated live.

When it runs:

- The recurring `log.rotate` job is registered by the `core_recurring`
  plugin and normally runs once per hour.
- `RotatingFileHandler`-based logs rotate during writes.
- Spawned logs are checked just before the next rail daemon or phantom
  client process starts.

What it touches:

- Session pane logs under `config.project.logs_dir`, including nested
  `logs/<session>/<window>.log` paths.
- `~/.pollypm/errors.log` and `~/.pollypm/cockpit_debug.log`.
- `~/.pollypm/rail_daemon.log` and `~/.pollypm/phantom_client.log`.
- Rotations appear beside the source log as `.log.<timestamp>.gz` for
  recurring/bootstrap rotation, or as `.1`, `.2`, etc. for Python
  `RotatingFileHandler` backups.

How to tune or disable:

- Tune limits in `~/.pollypm/pollypm.toml`:
  ```toml
  [logging]
  rotate_size_mb = 20
  rotate_keep = 3
  ```
- `rotate_size_mb` is the per-file threshold. `rotate_keep` is retained
  archive count. Invalid or negative values fall back to defaults.
- There is no supported per-log disable switch. If you need temporary
  non-rotation for an investigation, raise `rotate_size_mb` and restore
  it afterward rather than disabling the `core_recurring` plugin, which
  also owns unrelated maintenance jobs.

What an operator sees:

- `pm doctor --fix` can run `log.rotate` immediately for oversized
  workspace logs.
- Rotation failure is best-effort and logged at debug level; it should
  not block `pm up`.
- Log rotation does not emit an audit-log event today. Inspect archive
  files and the live log size when investigating rotation behavior.

### Cockpit Socket Reaper

What runs:

- `pollypm.cockpit_socket_reaper::reap_stale_cockpit_sockets`, called
  from `pollypm.supervisor.Supervisor._bootstrap_clear_markers`.

When it runs:

- Once during supervisor bootstrap, before new cockpit or per-task
  worker panes are launched.

What it touches:

- `~/.pollypm/cockpit_inputs/*.sock`.
- `$TMPDIR/pollypm-cockpit_inputs/*.sock`, the AF_UNIX fallback used
  when the primary socket path is too long.
- Only socket inodes named like `<kind>-<pid>.sock` are candidates. A
  candidate is removed only when the encoded PID is no longer alive.
  Live PIDs, unparseable names, directories, and regular files are left
  alone.

How to tune or disable:

- There is no tuning knob and no supported disable switch.
- Custom tooling should not place durable sockets in `cockpit_inputs/`
  using PollyPM's `<kind>-<pid>.sock` naming shape. Use another
  directory or a name that does not encode a dead owner PID.

What an operator sees:

- A reaped socket produces a warning like `cockpit_socket_reaper: reaped
  ...`.
- Each removal emits a `socket.reaped` audit event under the `_workspace`
  audit stream with `path`, `kind`, `pid`, and `reason` metadata.
- If a custom socket disappeared, first check whether it was a real
  socket file in one of the reaper directories and whether its filename
  encoded a PID that had already exited.

### Worker-Marker Reaper

What runs:

- `pollypm.work.worker_marker_reaper::reap_orphan_worker_markers`, called
  from `pollypm.supervisor.Supervisor._bootstrap_clear_markers`.
- `pollypm.work.worker_marker_reaper::sweep_worker_markers`, called from
  the supervisor heartbeat sweep after startup.

When it runs:

- Bootstrap: once while the supervisor is starting, before per-task
  workers are alive.
- Runtime: on heartbeat sweeps, so markers orphaned after startup do not
  wait for the next `pm up`.

What it touches:

- Each configured project's
  `<project>/.pollypm/worker-markers/*.fresh` files.
- The task row behind a marker's `task-<project>-<number>` window name.
- The storage-closet tmux session's live task windows. The runtime sweep
  may also kill a stale tmux window matching a marker it reaped.
- Markers are removed when the task row is missing, the task is terminal
  (`done`, `cancelled`, or `abandoned`), or the task is non-terminal but
  the corresponding tmux window is gone. Fresh installs, missing DBs,
  and unrecognized marker names are skipped.

How to tune or disable:

- There is no tuning knob and no supported disable switch. These markers
  are an internal launch contract.
- Do not put custom files under `.pollypm/worker-markers/` using the
  `task-<project>-<number>.fresh` shape.
- If you need to inspect a suspected orphan before cleanup, run
  `pm doctor` before booting the cockpit; the doctor check reports
  missing-row and terminal-task markers without unlinking them.

What an operator sees:

- Bootstrap/runtime removal logs a warning and prints
  `[worker_marker_reaper] reaped ...`.
- Each removal emits a `worker.session_reaped` audit event with the task
  subject and marker path metadata.
- `pm doctor` reports `orphan-worker-markers` when it can classify stale
  markers without mutating the filesystem.
- Repeated `worker.session_reaped` events for the same task can trip the
  watchdog's dead-loop rule and route an escalation to the project
  architect.
