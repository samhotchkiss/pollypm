# To Discuss with Sam

Items that need human input or decision before proceeding.

## Overnight Session Summary (April 14-15)

### What was built
- Russell the Reviewer agent — dedicated code review with high quality bar
- Redesigned Polly dashboard — shows tasks across all projects, attention items, activity
- Per-task worker sessions — isolated worktrees with task prompts
- Human approval UX — [a] approve / [x] reject from task detail view
- Rail improvements — active/inactive grouping, yellow for active tasks
- Per-task worker sessions visible in rail under projects
- Auth recovery — same-account retry for expired tokens
- Heartbeat review nudge — notifies Russell when tasks enter review
- Session manager lifecycle — claim/approve/cancel hooks

### What was tested
- 7 projects with active tasks (weather-cli, todo-api, camptown, mini-calc, link-checker, git-stats, shortlink)
- All 4 flow types: standard, spike, bug, user-review
- Dependency chains: todo-api (3-task chain, auto-unblock verified)
- Human approval: weather-cli/4 approved as user
- 40+ tasks through full lifecycle
- Russell reviewing real code (git diffs, running CLIs)
- WeatherCLI actually works: `uv run python -m weathercli --lat 40.7 --lon -74.0`

### Remaining Issues
- Input bar submission reliability (messages get stuck)
- Per-task worker sessions exit after completion (need remain-on-exit for rejection flow)
- Knowledge extraction pipeline appends indefinitely to project-overview.md
- Polly's prompt in pollypm.toml overrides builtin.py changes

## Open Questions

1. **Russell's account**: I assigned Russell to `claude_s_swh_me` (your personal Claude account). Should he use a different account, or is sharing with you fine? He'll consume tokens from that subscription.

2. **Deploy step in review**: Should deploy be part of the worker's "done" signal (worker deploys before signaling done), or should Russell trigger deploy after approving? The current flow has no deploy step — the worker just commits.

3. **Human approval UX**: For user-review flow tasks, how should the approval notification look in the cockpit? Options: (a) inbox message with approve/reject buttons, (b) task detail view with approve/reject actions, (c) both.

4. **Project grouping in rail**: You want active projects (task in last 24h) above inactive ones. Should new projects with zero tasks also appear in the "active" group, or only once they have their first task?

## Issues Found During Testing

- `docs/project-overview.md` is 142MB — added to .gitignore. Needs investigation on what's bloating it. Root cause: knowledge extraction pipeline appends JSON blobs indefinitely.
- Operator session prompt is baked into pollypm.toml (not just the profile) — updating builtin.py doesn't fully update the operator prompt without editing the TOML too.
- **Input stuck in bar (recurring)**: Messages sent via `tmux send-keys` sometimes land in the input bar but don't get submitted. The `_verify_input_submitted` retry mechanism doesn't reliably catch multi-line wrapped text. May need to use tmux's `-l` literal mode differently or break long messages into shorter chunks.
- **Worker session uses Codex (gpt-5.4) by default**: The `pm worker-start` command created a Codex worker, not Claude. This is because the config uses `codex_s_swh_me` for workers. Per-task workers from the SessionManager should probably use Claude since the task prompts are designed for Claude Code.

## Overnight Testing Progress

### Weather-CLI project
- [x] Round 1: Project registered, worker created
- [x] Round 2: weather_cli/1 (core fetch module) — full lifecycle completed
  - Polly created task with pm task create + pm task queue
  - Per-task worker session created with worktree (task-weather_cli-1)
  - Worker implemented code, ran CLI to verify (Temperature: 21.1C, Overcast)
  - Russell reviewed: checked git diff, ran CLI, verified all 6 requirements
  - Russell merged branch to main, approved
  - NOTE: Russell had to use --actor polly because task was created with old role binding
- [ ] Round 3: weather_cli/2 (colored output + --units) — in progress
  - Will test rejection flow if Russell finds issues
- [ ] Rounds 4-10: dependency chain, spike, bug, hold/resume, user-review

### Issues to Fix
- Role binding: tasks created before the Russell update use reviewer=polly, preventing Russell from approving as himself
- Per-task worker window disappeared from storage closet during testing (may be duplicate cleanup or heartbeat interference)
- Input bar submission: long messages still sometimes get stuck
