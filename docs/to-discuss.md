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

## Late Night Testing Stats (Iteration 3)

### Projects Created Tonight
1. weather-cli (5 tasks done, 1 cancelled)
2. todo-api (3 tasks done — full dependency chain)
3. mini-calc (1 spike task done)
4. link-checker (1 bug flow in progress)
5. git-stats (1 task done)
6. md-render (1 in review, 2 blocked — dependency chain)

### Flows Exercised
- Standard: weather-cli/1,2,3,5, todo-api/1,2,3, camptown/1,2, git-stats/1, md-render/1
- Spike: mini-calc/1 (no review, straight to done)
- Bug: link-checker/1 (reproduce → fix → review)
- User-review: weather-cli/4 (human approval)
- Cancellation: weather-cli/6 (cancelled during implementation)
- Hold/resume: weather-cli/5

### Rejection Verified
- camptown/2: Russell rejected for missed docs/project-overview.md references
- Task cycled from implement v1 → code_review rejected → implement v2

### Working Software Built
- WeatherCLI: `uv run python -m weathercli --city "San Francisco"` → real weather data
- TodoAPI: 30 passing tests covering full CRUD lifecycle

## Iteration 6 Update

### Additional Completions
- pollypm_docs/1: getting started guide (human review flow verified)  
- passgen/1: password generator (approved)
- shortlink/3: Web UI (approved, task 4 auto-unblocked)

### Third Rejection
- commit_validator/1: rejected for same build-backend issue as md2html/1
- Pattern: Claude -p workers consistently generate invalid pyproject.toml build-backend
- Russell catches it every time — quality control is working

### Bug Found and Fixed
- Review nudge was including human_review tasks, sending them to Russell
  when they need user approval. Fixed with node_id filter.

### Stats: 20 tasks completed tonight, 3 rejections, 2 human approvals

## Iteration 7 Update

### Key Finding: Russell's Quality Bar
- Russell rejected rework submissions that hadn't actually changed anything
- "No changes since v1 rejection" — he verified the same issues still existed
- 6 total rejections tonight, Russell never approved unfixed work
- This proves the review quality control is genuine, not rubber-stamping

### Per-Task Worker Rework Gap (CRITICAL for demo)
- When Russell rejects a task, the per-task worker session has already exited
- No worker is alive to receive rejection feedback and do the rework
- Manually signaling done without fixes doesn't fool Russell
- **Fix needed**: On rejection, create a new per-task worker session
  with the rejection reason in its prompt

### Scale Test
- 26 projects in the system
- 23 tasks completed tonight
- Dashboard renders quickly at this scale
- Multiple per-task workers running simultaneously
- Puzzle solver (8-queens) task created as algorithmic challenge

### Dashboard Bug Fixed
- PollyDashboardApp crash due to missing readonly_state on partial Supervisor

## Final Overnight Stats

### Completed Projects
- **weather-cli**: 5 features, working CLI (city search, forecast, colors, units)
- **todo-api**: 3 tasks, 30 passing tests, full CRUD
- **md-render**: 3-task chain, markdown → styled terminal output
- **shortlink**: 5-task dependency chain, all complete
- **link-checker**: bug flow (reproduce → fix → review → done)
- **mini-calc**: spike flow research
- **git-stats**: statistics dashboard
- **passgen**: password generator
- **color-palette**: harmonious color generation
- **file-sorter**: spike flow research
- **pollypm-docs**: getting started guide (user-review flow)
- **puzzle-solver**: 8-queens solver — 92 correct solutions verified by Russell

### By the Numbers
- 25+ tasks completed tonight
- 6 rejections (Russell caught real issues every time)
- 26 projects in system
- 885 PollyPM tests passing
- All 4 flow types exercised + cancellation + hold/resume
- Russell verified 8-queens solver produces exactly 92 valid solutions

## Iteration 8: Rework Cycle Complete

### camptown/2 Full Lifecycle
- v1: rejected (missed docs references)
- v2: rejected (no actual changes — Russell caught it)
- v3: **APPROVED** (actually fixed project-overview.md)
- 3 rejection/rework cycles, Russell only approved when fix was real

### Rework Worker Spawning
- Implemented: when rejection finds dead worker, spawns new Claude
  session with rejection reason in prompt
- Reuses existing worktree (preserves code changes)
- Build-backend fixes applied for commit_validator/1 and md2html/1

### Dashboard Bug Fix
- PollyDashboardApp crash: missing readonly_state on partial Supervisor
  created via __new__

### Stats: 56 tasks done, 26 projects
