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

## Sending Messages

| Command | What it does |
|---------|-------------|
| `pm send <session> "<text>"` | Send a message to a session. Auto-submits for Codex. |
| `pm send <session> "<text>" --owner human` | Send as human (claims a lease, blocks automation) |
| `pm send <session> "<text>" --force` | Send even if someone else holds the lease |

## Workers

| Command | What it does |
|---------|-------------|
| `pm worker-start <project_key>` | Create or relaunch a managed worker for a project |
| `pm plan` | Show the launch plan (what sessions would be created) |
| `pm worktrees` | List active git worktrees per worker |

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
| `pm add-project <path>` | Register a project |
| `pm scan-projects` | Auto-discover git repos in workspace |
| `pm init-tracker` | Initialize the issue tracker for a project |

## Issues

| Command | What it does |
|---------|-------------|
| `pm issue list` | List issues by state |
| `pm issue create --title "..." --body "..."` | Create a new issue |
| `pm issue move <id> <state>` | Move an issue to a new state |

## Maintenance

| Command | What it does |
|---------|-------------|
| `pm repair` | Check and fix project scaffolding, docs, and state |
| `pm repair --check` | Report problems without fixing them |
| `pm upgrade` | Check GitHub for new versions and install updates |
| `pm upgrade --check` | Only check if an update is available |

## Diagnostics

| Command | What it does |
|---------|-------------|
| `pm doctor` | Check prerequisites (tmux, claude, codex, docker) |
| `pm account-doctor` | Deep-check account health and isolation |
