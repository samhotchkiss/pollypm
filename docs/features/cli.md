**Last Verified:** 2026-04-22

## Summary

`pm` is the PollyPM CLI, built on Typer. The root `app` lives in `src/pollypm/cli.py` and composes sub-apps registered by feature modules (`cli_features/`) and by built-in plugins. Root commands handle onboarding, status, sending input to panes, notifying the user, shutting things down, and launching the cockpit. Sub-apps handle tasks (`pm task`), flows (`pm flow`), inbox (`pm inbox`), jobs (`pm jobs`), plugins (`pm plugins`), rail (`pm rail`), activity (`pm activity`), morning briefing (`pm briefing`), projects (`pm project`), memory (`pm memory`), advisor (`pm advisor`), downtime (`pm downtime`), alerts (`pm alert`), sessions (`pm session`), heartbeat (`pm heartbeat`), issues (`pm issue`, `pm report`), debug (`pm debug`), and itsalive deploys (`pm itsalive`).

Every command uses the `help_with_examples` helper so `--help` output always shows concrete invocations. Error rendering goes through `pollypm.errors.format_cli_error` so task-not-found and invalid-id cases produce consistent, actionable output.

Touch this module when adding a new top-level command. Prefer registering under an existing sub-app over expanding the root. Feature-module-scoped commands go in `cli_features/`; plugin-scoped commands expose a Typer app from the plugin package and `add_typer` it in `cli.py`.

## Core Contracts

`cli.py` exports the Typer `app` and a small set of root command handlers. The registration pattern is either `app.add_typer(sub_app, name="<name>")` or `register_<X>_commands(app)` for feature modules that decorate `app` directly.

Representative sub-app entry points:

```python
# src/pollypm/cli.py
app: typer.Typer

# Task surface
from pollypm.work.cli import task_app, flow_app
app.add_typer(task_app, name="task")
app.add_typer(flow_app, name="flow")

# Inbox
from pollypm.work.inbox_cli import inbox_app
app.add_typer(inbox_app, name="inbox")

# Plugins, rail, jobs, memory
from pollypm.plugin_cli import plugins_app
from pollypm.rail_cli import rail_app
from pollypm.jobs.cli import jobs_app
from pollypm.memory_cli import memory_app
```

Root commands (not exhaustive — see `cli.py:129`–end):

- `pm` / `pm up` — create or attach to the tmux session and cockpit.
- `pm status [session] [--json]` — snapshot launches / windows / alerts / leases.
- `pm send <session> <text> [--owner ...] [--force]` — inject input into a tmux pane.
- `pm notify <subject> <body-or-"-"> [--priority ...]` — write to the unified messages inbox. This is the canonical escalation channel.
- `pm doctor` — environment validator. See [modules/doctor.md](../modules/doctor.md).
- `pm cockpit` — launch the Textual cockpit.
- `pm down` / `pm reset` — stop managed sessions cleanly.
- `pm help <topic>` — role/worker long-form help.

## File Structure

- `src/pollypm/cli.py` — root composition, help text, root commands.
- `src/pollypm/cli_help.py` — `help_with_examples(desc, examples, trailing=None)` builder.
- `src/pollypm/cli_shortcuts.py` — shortcut rendering for terse help output.
- `src/pollypm/cli_features/alerts.py` — `alert_app`, `heartbeat_app`, `session_app`.
- `src/pollypm/cli_features/issues.py` — `issue_app`, `itsalive_app`, `report_app`.
- `src/pollypm/cli_features/maintenance.py` — `debug_app` + `register_maintenance_commands`.
- `src/pollypm/cli_features/migrate.py` — schema-migrate subcommands.
- `src/pollypm/cli_features/projects.py` — `register_project_commands` for `pm add-project` et al.
- `src/pollypm/cli_features/session_runtime.py` — session-runtime commands (attach, switch).
- `src/pollypm/cli_features/ui.py` — UI-launch commands (cockpit, onboarding TUI).
- `src/pollypm/cli_features/upgrade.py` — `pm upgrade` register.
- `src/pollypm/cli_features/workers.py` — per-task worker registrations.
- `src/pollypm/work/cli.py` — `pm task` + `pm flow` commands.
- `src/pollypm/work/inbox_cli.py` — `pm inbox`.
- `src/pollypm/plugin_cli.py` — `pm plugins`.
- `src/pollypm/rail_cli.py` — `pm rail`.
- `src/pollypm/jobs/cli.py` — `pm jobs`.
- `src/pollypm/memory_cli.py` — `pm memory`.
- `src/pollypm/plugins_builtin/activity_feed/cli.py` — `pm activity`.
- `src/pollypm/plugins_builtin/morning_briefing/cli.py` — `pm briefing`.
- `src/pollypm/plugins_builtin/project_planning/cli/` — `pm project`.
- `src/pollypm/plugins_builtin/advisor/cli/advisor_cli.py` — `pm advisor`.
- `src/pollypm/plugins_builtin/downtime/cli.py` — `pm downtime`.
- `src/pollypm/errors.py` — shared CLI error formatters.

## Implementation Details

- The CLI installs the centralized error log at import time via `pollypm.error_log.install(process_label="cli")`. Every invocation writes WARNING+ records (and any `logger.exception`) to `~/.pollypm/errors.log` even if boot crashes before the TUI mounts.
- `app = typer.Typer(..., invoke_without_command=True, no_args_is_help=False)` — `pm` without a subcommand attaches to the existing tmux session. That is intentional; scripts that want help should call `pm --help`.
- Feature modules register *into* the root `app`, while plugin CLIs export their own Typer apps that are attached via `app.add_typer`. Do not flatten plugin commands into the root — keeping them under a sub-app preserves the plugin seam.
- `help_with_examples(description, [(cmd, note), ...], trailing=...)` is the only sanctioned way to build help text. It guarantees every command has at least one example, which is load-bearing for agent UX.

## Related Docs

- [features/service-api.md](service-api.md) — the underlying facade most CLI commands route through.
- [features/cockpit.md](cockpit.md) — what `pm` / `pm cockpit` launches.
- [features/tasks-and-flows.md](tasks-and-flows.md) — `pm task` / `pm flow` semantics.
- [features/inbox-and-notify.md](inbox-and-notify.md) — `pm notify` / `pm inbox` and the unified messages table.
- [modules/doctor.md](../modules/doctor.md) — `pm doctor` checks.
