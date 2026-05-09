# Changelog

All notable changes to PollyPM will be documented in this file.

The format is based on Keep a Changelog, and this file also doubles as the
template for user-visible PR descriptions: summarize user-facing changes under
Added, Changed, and Removed.

## [Unreleased]

### Added
- Web API implementation tracker: Phase 1 (#1547), Phase 2 (#1548), and
  Phase 3 (#1549) of the design spec landed in #1438. Phase 1 ships the
  `pm serve` CLI, `pm api regen-token` token rotation, bearer auth, all
  read endpoints, and the Server-Sent Events activity stream.

### Changed
- Cockpit Home dashboard header relabels the curated alert count from
  "N alerts" to "N needs action" so it no longer disagrees with `pm
  alerts`, which lists every open alert (including operational
  heartbeat noise the dashboard intentionally filters out). #999.

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
