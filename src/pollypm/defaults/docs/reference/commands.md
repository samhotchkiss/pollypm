# PollyPM Command Reference

All commands use the `pm` CLI. Run `pm --help` for the full list, or `pm <command> --help` for details on any command.

## Session Lifecycle

| Command | What it does |
|---------|-------------|
| `pm up` | Launch all sessions (heartbeat, operator, workers) and open the cockpit |
| `pm reset` | Kill all tmux sessions. Clears scheduler jobs, cockpit state, and leases. Use `pm up` to restart. |
| `pm reset --force` | Same as reset but skips the confirmation prompt |
| `pm status` | Show health of all sessions: status, running, alerts, lease, role |
| `pm status <session>` | Show health of one session |
| `pm debug` | Diagnostic dump: open alerts, session states, recent events. Works outside tmux. |

## Task Management

| Command | What it does |
|---------|-------------|
| `pm task create "Title" -p <project> -d "..." -f <flow> -r worker=worker -r reviewer=russell` | Create a task |
| `pm task queue <id>` | Move draft → queued (available for worker pickup) |
| `pm task list -p <project>` | List tasks for a project |
| `pm task status <id>` | Detailed task summary with flow state |
| `pm task approve <id> --actor russell` | Approve at review node |
| `pm task reject <id> --actor russell --reason "..."` | Reject with feedback |
| `pm task counts -p <project>` | Counts by status |
| `pm flow list` | Show available flow templates |

## Workers

PollyPM uses **per-task workers**: each task that gets claimed (via
`pm task claim` or the auto-sweeper) provisions its own
`task-<project>-<n>` session in an isolated worktree. The session is
torn down automatically when the task hits `done` or `cancelled`. You
do **not** spawn workers manually — claiming a task is the worker.

| Command | What it does |
|---------|-------------|
| `pm task next -p <project_key>` | Find the next queued task for a project |
| `pm task claim <task_id>` | Claim a task (provisions worktree + worker session) |
| `pm task done <task_id>` | Hand the task to review (also tears the worker down on approve) |
| `pm worker-start --role architect <project_key>` | Spawn the project's planner architect (long-lived; auto-closed after 2hr idle) |
| `pm plan` | Show the launch plan (what sessions would be created) |
| `pm worktrees` | List active git worktrees per worker |
| `pm worker-stop <session>` | Stop a managed session and disable heartbeat recovery |
| `pm switch-provider <session> <provider>` | Switch a session between Claude and Codex with checkpoint |

> **Deprecated:** `pm worker-start <project>` (without `--role architect`)
> is no longer supported — managed `worker-<project>` sessions leaked
> RAM because they outlived the task that needed them. Per-task workers
> replaced them; the command exits with code 2 and a fix-it pointer.

## Leases

| Command | What it does |
|---------|-------------|
| `pm claim <session> --owner <name>` | Claim exclusive access to a session |
| `pm release <session>` | Release a lease |

Leases auto-expire after 30 minutes. The cockpit auto-claims/releases leases when mounting sessions.

## Accounts

| Command | What it does |
|---------|-------------|
| `pm accounts` | List all configured accounts with health, usage, isolation info |
| `pm add-account` | Add a new Claude or Codex account interactively |
| `pm relogin <account>` | Re-authenticate an account (run login flow again) |
| `pm remove-account <account>` | Remove an account from config |
| `pm refresh-usage` | Probe all accounts for current usage/quota |
| `pm failover` | Show controller account and failover order |
| `pm tokens` | Show token usage summary |

## Alerts

| Command | What it does |
|---------|-------------|
| `pm alerts` | List all open alerts |
| `pm alert clear <id>` | Clear a specific alert |
| `pm events` | Show recent event log |

## Heartbeat

| Command | What it does |
|---------|-------------|
| `pm heartbeat` | Run one heartbeat sweep manually (capture, classify, alert, recover) |
| `pm heartbeat install` | Install cron job (runs every 60 seconds) |
| `pm heartbeat uninstall` | Remove the cron job |

## Projects

| Command | What it does |
|---------|-------------|
| `pm projects` | List configured projects |
| `pm add-project <path>` | Register a project (defaults to `tracked=true` so cockpit/briefing/recovery surfaces include it) |
| `pm add-project <path> --no-track` | Register without marking the project as tracked (use for experimental/archival registrations) |
| `pm add-project <path> --skip-plan` | Register without auto-firing the planner |
| `pm project new <path>` | Register and immediately run the planner |
| `pm project plan <project>` | Spawn the architect with the planning flow (use for first plan and re-plans, not `pm task create`) |
| `pm project rename <old> <new>` | Rename a project's slug (with `--dry-run` to preview) |
| `pm scan-projects` | Auto-discover git repos in workspace |
| `pm init-tracker` | Initialize the issue tracker for a project |

## History Import

| Command | What it does |
|---------|-------------|
| `pm import <project_key>` | Run the history import pipeline — crawl transcripts, git, and files to generate project docs |

When you add a project with `pm add-project`, the import runs automatically. Use `pm import` to re-run it (e.g., after significant new work).

## Inbox

| Command | What it does |
|---------|-------------|
| `pm notify "<subject>" "<body>"` | Create an inbox item for the human user |
| `pm mail` | List open inbox items |
| `pm mail <id>` | Read a message or thread |
| `pm mail --reply <id> --text "msg"` | Reply to a message (creates thread) |
| `pm mail --close <id> --note "what was done"` | Close with required note |
| `pm mail --archived` | List archived messages |
| `pm mail --threads` | List active threads |
| `pm reply <id> "text"` | Reply to a message (agent shorthand) |
| `pm reply <id> "text" --close` | Reply and close in one step |
| `pm discuss <id>` | Jump into live discussion about an inbox message |

**When to use `pm notify`:** Any time you need the human user's input, approval, or attention — and they may not be watching your session. The inbox is the reliable way to reach the user. Don't just ask in chat and hope they see it.

**Decision prefixes:**
- `[Decision]` — Polly made a judgment call. User can review and override.
- `[Escalation]` — Requires human decision. Work pauses until user responds.

| Command | What it does |
|---------|-------------|
| `pm decisions` | Show recent decisions Polly made (for user review) |

## Maintenance

| Command | What it does |
|---------|-------------|
| `pm repair` | Check and fix project scaffolding, docs, and state |
| `pm repair --check` | Report problems without fixing them |
| `pm upgrade` | Check GitHub for new versions and install updates |
| `pm upgrade --check` | Only check if an update is available |

## Help

| Command | What it does |
|---------|-------------|
| `pm help worker` | Print the canonical worker guide (single source of truth for the worker role) |
| `pm shortcuts` | Curated cheatsheet of common PollyPM commands |

## Diagnostics

| Command | What it does |
|---------|-------------|
| `pm doctor` | Run the full diagnostic checklist: prerequisites (tmux, claude, codex, docker), provider-account auth, dual-DB consistency, and worker-marker health |
| `pm doctor --json` | Emit the report as machine-readable JSON |
| `pm doctor --fix` | Apply the safe subset of automatic repairs (missing dirs, stale panes, orphan worker markers) |
| `pm doctor --fix-dry-run` | Show what `--fix` would change, without mutating anything |
| `pm account-doctor` | Deep-check account health and isolation |
| `pm errors` | Triage view over `~/.pollypm/errors.log` |
