# Table of Contents

**Last Verified:** 2026-04-22

## Project state (one paragraph)

PollyPM is a single-host, tmux-first supervisor for AI coding agents. It's a modular Python monolith (`src/pollypm/`) built around a **CoreRail** process lifecycle, a sealed **heartbeat + job queue + worker pool**, a sealed **WorkService** (SQLite) for task lifecycle, a **plugin host** that owns every replaceable seam (providers, runtimes, session services, heartbeat/scheduler backends, agent profiles, recovery policy, launch planner, rail items, content), and a **Textual cockpit** plus Typer **CLI**. Polly (the operator PM), Russell (review), and per-task workers are agent personas registered as plugins. The project is post-V1-tag (target 2026-04-16); day-to-day work prioritizes stability and honest documentation of what exists over speculative features. The legacy docs are preserved in `docs/archive/` and should be treated as historical context, not as the current spec — the source of truth is the code in `src/pollypm/` and this doc suite.

---

## Top-level surfaces

- **[features/cli.md](features/cli.md)** — the `pm` Typer CLI. Root commands, sub-apps (`task`, `flow`, `inbox`, `jobs`, `plugins`, `rail`, `activity`, `briefing`, `project`, `memory`, `advisor`, `downtime`), and the help-with-examples convention.
- **[features/cockpit.md](features/cockpit.md)** — the Textual cockpit UI (`pm cockpit` / `pm`). Rail, dashboard, task review, inbox, session panes.
- **[features/service-api.md](features/service-api.md)** — `pollypm.service_api` — the stable facade TUIs/CLIs/tests use. Supervisor-direct imports outside `pollypm.core` are deprecated.

## Core substrate (modules)

- **[modules/core-rail.md](modules/core-rail.md)** — `CoreRail`: process-wide config + state store + plugin host + subsystem lifecycle. Boots the heartbeat rail.
- **[modules/supervisor.md](modules/supervisor.md)** — `Supervisor`: remaining owner of tmux bootstrap/layout, session stabilization, recovery intervention, and `send_input`. Late-stage decomposition.
- **[modules/heartbeat.md](modules/heartbeat.md)** — sealed `Heartbeat` + `Roster` + `JobQueue` + worker pool. Fires due recurring handlers exactly once per tick.
- **[modules/work-service.md](modules/work-service.md)** — sealed task lifecycle. `WorkService` protocol + `SQLiteWorkService` + flow engine + gates + per-task worker sessions.
- **[modules/plugin-host.md](modules/plugin-host.md)** — discovery, manifest parsing, `PluginAPI`/`PollyPMPlugin`, `ExtensionHost`, hook context, roster/job/rail registries.
- **[modules/state-store.md](modules/state-store.md)** — storage primitives. Legacy `StateStore` (domain tables) + the unified `SQLAlchemyStore` / `Store` protocol (messages and events).
- **[modules/session-services.md](modules/session-services.md)** — tmux as the execution surface. `TmuxSessionService`, pane logging, lease model, session-bus events.
- **[modules/providers.md](modules/providers.md)** — provider adapters (Claude, Codex). Launch command construction, env/home priming, transcript ingestion seams.
- **[modules/runtimes.md](modules/runtimes.md)** — execution isolation. Local runtime (account homes under `~/.pollypm/homes/`) and Docker runtime (stub-level).
- **[modules/recovery.md](modules/recovery.md)** — sealed `RecoveryPolicy` + `SessionHealth` classification + intervention ladder (nudge → reset → relaunch → failover → escalate).
- **[modules/accounts.md](modules/accounts.md)** — account declarations, login state, cached usage snapshots, failover eligibility.
- **[modules/memory.md](modules/memory.md)** — memory backends, memory prompts, worker-protocol + recall injection into session prompts.
- **[modules/checkpoints.md](modules/checkpoints.md)** — three-tier checkpoint model (mechanical / AI summary / completion). Recovery data ledger.
- **[modules/transcript-ingest.md](modules/transcript-ingest.md)** — provider JSONL → PollyPM-owned transcript archive. Cursor-based tailer.
- **[modules/release-check.md](modules/release-check.md)** — exemplar-shaped module. Non-raising release-channel update check feeding doctor + rail banner.
- **[modules/doctor.md](modules/doctor.md)** — `pm doctor` environment validator. Answers three questions per failed check.
- **[modules/rail-daemon.md](modules/rail-daemon.md)** — headless `pm-rail-daemon` that keeps the CoreRail ticking when the cockpit is closed.
- **[modules/config.md](modules/config.md)** — `pollypm.toml` loader + project-local override merge. Models in `pollypm.models`.
- **[modules/worktrees-and-projects.md](modules/worktrees-and-projects.md)** — `worktrees.py` + `projects.py`. Project registration, per-session git worktrees, session locks.

## Bounded features

- **[features/tasks-and-flows.md](features/tasks-and-flows.md)** — tasks, flow templates (YAML), gate evaluation, per-task worker sessions.
- **[features/inbox-and-notify.md](features/inbox-and-notify.md)** — `pm notify`, `pm inbox`, the unified `messages` table, work-service tasks bridged in.
- **[features/activity-feed.md](features/activity-feed.md)** — Live Activity Feed plugin: unified projection of events/transitions, rail pill, `pm activity`.
- **[features/cockpit-rail.md](features/cockpit-rail.md)** — how the left-pane rail renders, plugin-registered items, reserved-section rules.
- **[features/agent-personas.md](features/agent-personas.md)** — Polly, Russell, worker, heartbeat, triage personas. Prompt assembly, session manifest injection.
- **[features/itsalive.md](features/itsalive.md)** — the itsalive.co deploy wrapper plugin + module.
- **[features/jobs-queue.md](features/jobs-queue.md)** — durable SQLite job queue, claim/dedupe semantics, `pm jobs` CLI.
- **[features/upgrade.md](features/upgrade.md)** — `pm upgrade` and the release-channel cache feeding it.

## Plugins

Each plugin lives under `src/pollypm/plugins_builtin/<name>/` with a `pollypm-plugin.toml` manifest and a `plugin.py` defining `plugin = PollyPMPlugin(...)`.

- **[plugins/core-agent-profiles.md](plugins/core-agent-profiles.md)** — polly / russell / worker / heartbeat / triage personas.
- **[plugins/core-rail-items.md](plugins/core-rail-items.md)** — built-in rail entries re-expressed as plugin registrations.
- **[plugins/core-recurring.md](plugins/core-recurring.md)** — the recurring handler set migrated off the old heartbeat loop (session health sweep, capacity probe, account usage refresh, transcript ingest, alerts GC, log rotate, and more).
- **[plugins/activity-feed.md](plugins/activity-feed.md)** — Live Activity Feed (dedicated spec).
- **[plugins/advisor.md](plugins/advisor.md)** — 30-minute advisor persona + tick + autoclose.
- **[plugins/morning-briefing.md](plugins/morning-briefing.md)** — daily herald briefing at the configured hour.
- **[plugins/downtime.md](plugins/downtime.md)** — idle-budget exploration lane; nothing auto-deploys.
- **[plugins/project-planning.md](plugins/project-planning.md)** — architect + 5 critic personas, planning flow templates.
- **[plugins/memory-curator.md](plugins/memory-curator.md)** — daily TTL / dedup / decay / promotion pass on the memory store.
- **[plugins/human-notify.md](plugins/human-notify.md)** — fans task-assignment events to macOS / webhook / cockpit adapters.
- **[plugins/task-assignment-notify.md](plugins/task-assignment-notify.md)** — in-process event bus → worker/reviewer ping dispatch + 30-second sweeper.
- **[plugins/itsalive.md](plugins/itsalive.md)** — itsalive.co deploy sweeper + persona augmentation.
- **[plugins/magic.md](plugins/magic.md)** — compatibility alias for the itsalive persona. Also ships the Magic Skills catalog under `skills/`.

### Adapter plugins (inlined here — thin manifest + single-import plugin.py)

Each of the following is a one-file plugin that registers exactly one adapter class. Their plugin.py files are 8–15 lines; there is no additional behavior to document beyond "this wires `<module>` into the plugin host."

- **claude** — `plugins_builtin/claude/plugin.py` registers `providers.claude.ClaudeAdapter` as provider `"claude"`. See [modules/providers.md](modules/providers.md).
- **codex** — `plugins_builtin/codex/plugin.py` registers `providers.codex.CodexAdapter` as provider `"codex"`. See [modules/providers.md](modules/providers.md).
- **local_runtime** — `plugins_builtin/local_runtime/plugin.py` registers `runtimes.local.LocalRuntimeAdapter` as runtime `"local"`. See [modules/runtimes.md](modules/runtimes.md).
- **docker_runtime** — `plugins_builtin/docker_runtime/plugin.py` registers `runtimes.docker.DockerRuntimeAdapter` as runtime `"docker"`. See [modules/runtimes.md](modules/runtimes.md).
- **tmux_session_service** — `plugins_builtin/tmux_session_service/plugin.py` factory-registers `session_services.tmux.TmuxSessionService` as session service `"tmux"`. See [modules/session-services.md](modules/session-services.md).
- **local_heartbeat** — `plugins_builtin/local_heartbeat/plugin.py` registers `heartbeats.local.LocalHeartbeatBackend` as heartbeat backend `"local"`. See [modules/heartbeat.md](modules/heartbeat.md).
- **inline_scheduler** — `plugins_builtin/inline_scheduler/plugin.py` registers `schedulers.inline.InlineSchedulerBackend` as scheduler `"inline"`. (Kept to preserve the plugin seam; v1 runs all scheduling through the heartbeat rail.)
- **default_launch_planner** — `plugins_builtin/default_launch_planner/plugin.py` registers `DefaultLaunchPlanner` as launch planner `"default"`. Owns `plan_launches` / `effective_session` / `tmux_session_for_launch` / `launch_by_session`.
- **default_recovery_policy** — `plugins_builtin/default_recovery_policy/plugin.py` registers `recovery.default.DefaultRecoveryPolicy` as recovery policy `"default"`. See [modules/recovery.md](modules/recovery.md).

## Data & formats

- **pollypm.toml** — global config at `~/.pollypm/pollypm.toml`; project overrides at `<project>/.pollypm/config/project.toml`. See [modules/config.md](modules/config.md).
- **State DB** — per-project SQLite at `<project>/.pollypm/state.db`. Legacy domain tables (sessions, heartbeats, leases, token ledger, memory, worktrees, work_jobs) live on `StateStore`; the unified `messages` and events surface lives on `SQLAlchemyStore`. See [modules/state-store.md](modules/state-store.md).
- **Work DB** — same SQLite file with `work_*` tables owned by `SQLiteWorkService`. See [modules/work-service.md](modules/work-service.md).
- **Transcripts** — standardized JSONL under `<project>/.pollypm/transcripts/`. See [modules/transcript-ingest.md](modules/transcript-ingest.md).
- **Logs** — `<project>/.pollypm/logs/<session>/<launch-id>/{pane.log,supervisor.log,snapshots/}`.

## Gaps and under-specified areas

Honest inventory of places where the code is thin, documentation and code diverge, or the boundary still leaks:

- `Supervisor` is still large and owns tmux bootstrap, stabilization, recovery interventions, and `send_input`. The import-boundary test (`tests/test_import_boundary.py`) enforces a Supervisor allow-list that is still long — see issues #179, #182, #183, #185, #186 (noted in `src/pollypm/service_api/v1.py` module docstring). New code must route through `PollyPMService`.
- `pollypm.storage.state.StateStore` still owns session / heartbeat / lease / token-ledger / memory / worktree / `work_jobs` tables. `SQLAlchemyStore` (in `pollypm.store`) owns the unified `messages` + events surface only. The plan is to port the remaining tables off `StateStore`; see the docstring at `src/pollypm/storage/state.py:1`.
- The Docker runtime is scaffolded but not daily-driver. Most validation is against the local runtime.
- Gemini / Aider / OpenCode providers are not shipped. Only Claude and Codex are live.
- The plugin discovery spec that lived in the archived `docs/plugin-discovery-spec.md` has diverged slightly from the code; `src/pollypm/plugin_api/v1.py` and `src/pollypm/plugin_host.py` are authoritative.
