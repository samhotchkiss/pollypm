# Changelog

All notable changes to PollyPM will be documented in this file.

The format is based on Keep a Changelog, and this file also doubles as the
template for user-visible PR descriptions: summarize user-facing changes under
Added, Changed, and Removed.

## [Unreleased]

### Added
- `pm serve --port N [--host H] [--allow-remote]` runs the new Web API
  server (FastAPI) as a peer to the cockpit. Reads the same `state.db`
  / `audit.jsonl` via `pollypm.work.factory.create_work_service`
  (#1389), so the API works when the cockpit is down. Serves the
  Phase 1 read endpoints (projects, tasks, plans, inbox, events SSE)
  documented in `docs/web-api-spec.md` and `docs/api/openapi.yaml`.
  #1547.
- `pm api regen-token` rotates the bearer token at
  `~/.pollypm/api-token` (mode 0600). The new value prints once to
  stdout so a script can capture it with `pm api regen-token > token`.
  #1547.

### Changed
- Cockpit Home dashboard header relabels the curated alert count from
  "N alerts" to "N needs action" so it no longer disagrees with `pm
  alerts`, which lists every open alert (including operational
  heartbeat noise the dashboard intentionally filters out). #999.
- Project dashboard banner uses the configured PM persona (Archie,
  Cole, ...) consistently. The "no plan yet" state reframes from the
  red `◆ alert` framing to a soft `◇ next step` and reads
  `Press c to plan this with <PM>`; the calm-project banner replaces
  the syslog-style `architect_bikepath (architect) is alive but
  standing by — no task in flight` with `<PM> is here when you need
  them. Press c to chat or p to plan.`. The footer hides `p plan`
  for projects with no plan on disk so the keystroke doesn't lead to
  an empty surface. The topbar omits the `PM:` meta entirely when no
  persona is configured (vs the old `PM: Project PM` placeholder).
  #1540 #1541 #1542.

## [1.0.0] - 2026-04-20

### Added
- A stable v1 control plane built around tmux sessions, the Textual cockpit,
  issue-driven task orchestration, threaded inbox handling, and recoverable
  project state.
- Plugin API v1 plus replaceable provider, runtime, scheduler, heartbeat,
  agent-profile, task-backend, and memory-backend seams, with bundled defaults
  for local tmux workflows.
- Headless operations needed for daily use, including the rail daemon,
  architect warm resume, persistent recovery checkpoints, and cached account
  usage refresh.

### Changed
- Provider and account integration now run through the extracted adapter
  substrate and entry-point registry so providers can ship as standalone
  packages instead of core-only integrations.
- Worker lifecycle is task-scoped: claiming work provisions a worker session,
  and teardown happens through the work service instead of long-lived managed
  worker sessions.
- The stable release rolls up the `1.0.0rc1` and `1.0.0rc2` release-candidate
  line into the supported v1 baseline.

### Removed
- `pm worker-start <project>` as the per-task worker launch path; use
  `pm task claim <id>` so workers are provisioned and cleaned up through the
  work service. `pm worker-start --role architect` remains supported for the
  planner lane.
