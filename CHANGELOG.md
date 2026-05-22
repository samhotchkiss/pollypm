# Changelog

All notable changes to PollyPM will be documented in this file.

The format is based on Keep a Changelog, and this file also doubles as the
template for user-visible PR descriptions: summarize user-facing changes under
Added, Changed, and Removed.

## [Unreleased]

### Fixed
- `render_footer_status()` in `pollypm.cockpit_footer_status` no longer
  overflows the width budget for very narrow alert-only layouts. The
  alert-only path previously emitted `"⚠ h…"` (4 plain chars) at
  `width=3` because `_truncate_alert` returned a 2-char ellipsis stub
  even when the budget could only fit 1 char. The contract is now
  enforced explicitly: when the truncation can't fit at least 1 body
  char plus the ellipsis, the formatter drops to `""` per the existing
  full → compact → alert-only → empty cascade. PR #2014 review blocker.

### Added
- `pm cli-reference --json` dumps the full Typer command tree (commands,
  subcommands, flags, types, help text, defaults) as a single JSON
  document. Lets autonomous agents grep one structured surface instead
  of recursively walking `--help` pages. Third wedge of #1629 after
  the auto-role default (#1637) and task context/transitions
  inspection CLIs (#1669).
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
- Project dashboard Plan card empty-state copy names the configured PM
  persona instead of "the PM" — `No plan yet — Archie will draft one
  when this project picks up work. Press c in this pane to chat with
  Archie and ask for a plan now.` Falls back to "the PM" when no
  persona is configured, matching the banner-copy reframe so the
  Plan card no longer reads anonymously on the very surface that
  invites the user to start a plan. #1540 follow-up.

### Fixed
- `state_cache` production singleton now constructs
  `StateCacheRefresher` with a config-backed `project_keys` provider
  (`lambda: list(load_config().projects.keys())`), so the §9.1 startup
  full-refresh actually enqueues per-project refreshes. Without the
  provider, `_initial_full_refresh()` saw `[]` and the cache stayed
  empty until per-project audit events arrived — meaning workspace-
  scoped invalidations on a cold cache were no-ops because
  `ProjectStateCache.invalidate(None)` only iterates known entries.
  PR #2016 review blocker.

### Changed
- State-cache routing (Move A PR 4, closes #1664) flips the
  `POLLYPM_STATE_CACHE` env-flag default from OFF to **ON**. The
  cache is now the authoritative source for the 7 hot-path call
  sites routed in PRs 2 and 3. After PR4 lands, there is no
  runtime parity sampler. The cache is authoritative by default.
  Rollback path is `POLLYPM_STATE_CACHE=0` which makes every
  routed call site fall through to the direct facade. Parity is
  covered by tests; runtime divergence telemetry is deferred to
  future observability work.
- State-cache routing (Move A PR 3, refs #1664) extends the
  `POLLYPM_STATE_CACHE=1` fast path to the remaining 5 hot-path call
  sites from `docs/design/move-a-state-cache.md` §5. With the flag
  on and the per-project cache populated:
  `cockpit_inbox._count_inbox_tasks_for_label` sums
  `entry.awaits_user_count` instead of running the workspace sweep;
  `dashboard.operator_view.load_operator_view_from_config` builds
  the view from the snapshot instead of opening the shared work
  service; `cockpit_rail._project_state_rollups` reads pre-computed
  `rail_state` / `rail_badge` / `approvals_pending` fields;
  `cockpit_rail._project_tasks_for_rollup` is folded into the
  refresher (no longer called from `build_items` when cache is
  authoritative); and `cockpit_rail`'s two `latest_heartbeat()`
  per-project sites read directly from the pg facade
  (`pollypm.storage.pg_heartbeats.latest_heartbeat`) — the cache had
  no `heartbeat.*` invalidation, so any prefetch could only serve a
  stale snapshot. Bulk heartbeat prefetch is deferred to #2050 (once
  the refresher subscribes to heartbeat audit events). Each call
  site keeps a flag-off fall-through, so behaviour is unchanged
  until PR 4 flips the default. Flag still defaults OFF.

### Removed
- Rail-side 2s TTL on `CockpitRouter._project_categorizations`
  (`_PROJECT_CATEGORIZATIONS_TTL_SECONDS = 2.0`). Per
  `docs/design/move-a-state-cache.md` §9.5, the state cache's
  `global_version()` short-circuit plus the per-call TTL inside
  `project_state_map_from_config` already collapse navigation-burst
  refreshes — two cache layers were worse than one. The TTL
  pathology (freshly completed task stuck as WORKING for up to 2s)
  is gone with this change.

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
