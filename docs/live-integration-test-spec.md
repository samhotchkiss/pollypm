# Live Integration Test Spec — WeatherCLI Project

**Goal**: Build a real project end-to-end through Polly, verifying every step
of the task lifecycle works through the actual user interface. No CLI
shortcuts, no direct database access. Talk to Polly, watch what she does,
verify the workers execute, confirm reviews happen (including rejections).

**Project**: WeatherCLI — a Python CLI tool that fetches weather data from
Open-Meteo (free, no API key) and displays it with colored terminal output.

**Test session**: `/Users/sam/dev/weather-cli`

---

## Pre-flight

Before starting, verify the system is healthy:

1. `tmux ls` — pollypm, pollypm-storage-closet both exist
2. `pm status` — operator is healthy, heartbeat is running
3. Capture the operator pane — confirm it's Polly (not the heartbeat)
4. `pm task list -p weather_cli` — should be empty or not exist yet

If the operator session is the heartbeat or shows auth errors, fix that
before proceeding. The test is about the task flow, not session recovery.

---

## Round 1: Project Setup

**Send to Polly** (via cockpit right pane or tmux send-keys):
> I have a new project at /Users/sam/dev/weather-cli. It's a Python CLI
> tool that fetches weather from the Open-Meteo API and displays it in the
> terminal. Please register it and set up a worker.

**Verify**:
- [ ] Polly runs `pm add-project /Users/sam/dev/weather-cli` or equivalent
- [ ] Polly runs `pm worker-start weather_cli`
- [ ] A worker session appears in the storage closet
- [ ] Project appears in the cockpit rail
- [ ] Polly confirms the project is set up

---

## Round 2: First Task — Basic Fetch

**Send to Polly**:
> Create a task for the weather-cli project: build the core weather
> fetching module. It should use the Open-Meteo API
> (https://api.open-meteo.com/v1/forecast) to get current temperature and
> conditions for a given latitude/longitude. Include a pyproject.toml with
> dependencies. Acceptance criteria: running `uv run python -m weathercli
> --lat 40.7 --lon -74.0` prints the current temperature.

**Verify**:
- [ ] Polly creates a task with `pm task create`
- [ ] Task has a clear description and acceptance criteria
- [ ] Polly queues it with `pm task queue`
- [ ] Worker claims the task (check `pm task status`)
- [ ] Worker implements actual code
- [ ] Worker signals done with work output
- [ ] Task moves to review state
- [ ] Polly reviews — check that she examines the work output
- [ ] Polly approves or rejects with specific feedback

**If Polly approves on first try**: That's fine for round 2.

---

## Round 3: Intentional Rejection

**Send to Polly**:
> Create a task for weather-cli: add colored terminal output using the
> `rich` library. Temperature should be blue if below 50°F, green if
> 50-80°F, red if above 80°F. The output should include a header with the
> location name.

**After the worker signals done, send to Polly**:
> I looked at the colored output task. I want you to reject it — the
> colors should use Celsius thresholds (10°C, 27°C), not Fahrenheit.
> Also make sure there's a --units flag to switch between C and F.

**Verify**:
- [ ] Polly creates and queues the task
- [ ] Worker claims and implements
- [ ] Worker signals done
- [ ] Polly rejects with the specific feedback about Celsius + --units
- [ ] Task moves back to in_progress
- [ ] Worker addresses the feedback
- [ ] Worker signals done again
- [ ] Polly reviews the rework
- [ ] Polly approves (or rejects again with new feedback)
- [ ] Full execution history shows: v1 rejected, v2 approved

---

## Round 4: Dependency Chain

**Send to Polly**:
> I need two more things for weather-cli. First, add a --forecast flag
> that shows a 3-day forecast instead of just current conditions. Second,
> add a --json flag that outputs raw JSON instead of the formatted display.
> The JSON flag depends on the forecast being done first since it needs to
> handle both current and forecast data structures.

**Verify**:
- [ ] Polly creates both tasks
- [ ] Polly creates a dependency (forecast blocks json)
- [ ] Forecast task is queued; json task is blocked
- [ ] Worker picks up forecast task
- [ ] After forecast is approved, json task auto-unblocks to queued
- [ ] Worker picks up json task
- [ ] Both complete through full lifecycle
- [ ] `pm task list -p weather_cli` shows correct states throughout

---

## Round 5: Spike/Research Task

**Send to Polly**:
> Before we add caching, I want someone to research what caching options
> make sense for a CLI tool like this. Should we cache to disk? How long
> should weather data be cached? This is just research, no code needed.

**Verify**:
- [ ] Polly creates a task with the `spike` flow
- [ ] Worker picks it up and does research
- [ ] Worker signals done
- [ ] Task goes straight to done (no review step in spike flow)

---

## Round 6: Bug Fix Flow

**Send to Polly**:
> There's a bug — when you pass invalid coordinates (like --lat 999), the
> tool crashes with an unhandled exception instead of showing a friendly
> error. Create a bug task to fix it.

**Verify**:
- [ ] Polly creates a task with the `bug` flow
- [ ] The flow has a reproduce node before the fix node
- [ ] Worker reproduces, then fixes
- [ ] Task goes to review after fix
- [ ] Polly reviews and approves

---

## Round 7: Second Intentional Rejection

**Send to Polly**:
> Create a task to add a configuration file (~/.weathercli.toml) that
> stores default latitude, longitude, and units. When no flags are passed,
> it should use the config defaults.

**After worker signals done, send to Polly**:
> Reject the config task. The config file should use XDG_CONFIG_HOME
> instead of hardcoding ~/.weathercli.toml. Also needs a `weathercli
> config set` subcommand to update values without hand-editing TOML.

**Verify**:
- [ ] Same rejection/rework flow as Round 3
- [ ] Execution history shows the rejection and rework

---

## Round 8: Hold and Resume

**Send to Polly**:
> Create a task to add location search by city name (geocoding). But put
> it on hold — I want to think about whether to use Open-Meteo's geocoding
> API or a different one.

**Wait, then send**:
> OK, resume the geocoding task. Use Open-Meteo's geocoding endpoint.

**Verify**:
- [ ] Task is created, queued, then put on hold
- [ ] Task resumes to the correct state
- [ ] Worker picks it up and completes it

---

## Round 9: User-Review Flow

**Send to Polly**:
> Create a task to write the README. I want to personally review this one,
> not you. Set it up with the user-review flow.

**Verify**:
- [ ] Task uses the `user-review` flow
- [ ] When worker signals done, review lands in the user's inbox
- [ ] Polly does NOT auto-approve — it requires human approval
- [ ] Approve it manually: `pm task approve weather_cli/<n> --actor user`

---

## Round 10: Status Check and Wrap-up

**Send to Polly**:
> Give me a full status report on weather-cli. What's done, what's in
> progress, what's left?

**Verify**:
- [ ] Polly runs `pm task counts` and/or `pm task list`
- [ ] Polly gives a clear summary
- [ ] Numbers match reality
- [ ] The cockpit dashboard for weather-cli shows correct counts
- [ ] File sync: `ls /Users/sam/dev/weather-cli/issues/05-completed/`
  shows completed task files
- [ ] The actual weather-cli tool works:
  `cd /Users/sam/dev/weather-cli && uv run python -m weathercli --lat 40.7 --lon -74.0`

---

## Per-Task Worker Verification (CRITICAL)

Each task should spin up its own worker session. Verify for EVERY round:

- [ ] `pm task claim` triggers `SessionManager.provision_worker`
- [ ] A new tmux window appears in storage closet: `task-weather_cli-<N>`
- [ ] The worker runs in an isolated git worktree at
  `<project>/.pollypm/worktrees/weather_cli-<N>/`
- [ ] The worker has a clear task prompt (check first few lines of session)
- [ ] The worker knows the exact `pm task done` command with correct JSON format
- [ ] On approval: the worker session is torn down (window killed, worktree removed)
- [ ] On rejection: the SAME worker session receives rejection feedback
  (no new session — same pane, same worktree, same branch)
- [ ] On cancellation: worker session is torn down
- [ ] Multiple tasks can run in parallel with separate worktrees

Verify with:
```bash
# List active task worker sessions
tmux list-windows -t pollypm-storage-closet -F '#{window_name}' | grep task-

# Check worktrees
ls <project>/.pollypm/worktrees/

# Check session binding in DB
sqlite3 <project>/.pollypm/state.db "SELECT * FROM work_sessions WHERE ended_at IS NULL"
```

---

## What to Fix Along the Way

Every issue is a fix-it-now situation. Common problems to expect:

- Polly uses `pm send` instead of `pm task create` → check deployed docs for stale references
- Worker doesn't pick up tasks → check heartbeat nudge, check worker session
- Worker uses wrong JSON format for `pm task done` → check task prompt in session_manager.py
- Per-task worker not created on claim → check session_manager wiring in _svc()
- Worker session not torn down on approval → check teardown_worker hook in approve()
- Rejection doesn't reach worker → check notify_rejection and pane_id binding
- Task transitions fail → check work service errors, fix sqlite_service.py
- File sync stale → check _sync_transition calls
- Dashboard doesn't show project → check cockpit routing
- Review doesn't happen → check flow definition, actor validation
- Git worktree creation fails → check if project has commits, check branch conflicts

---

## Dashboard & Tasks View Verification

After each round, check both the dashboard and the interactive Tasks view.

### Dashboard (click project name or "Dashboard" in rail)

For each project with tasks, verify:
- [ ] Project name as header
- [ ] Summary bar: `○ 3 queued · ⟳ 1 in progress · ◉ 2 review` etc.
- [ ] Active tasks sorted by status (in_progress first, then review, queued, blocked)
- [ ] Each active task shows: status icon, task number, title, assignee, current node
- [ ] Completed tasks section with count and last 5
- [ ] Alerts section if alerts exist for this project
- [ ] For projects without tasks: fallback info (path, kind, tracked, worktrees)
- [ ] Dashboard refreshes within 5 seconds (not laggy)

### Tasks view (click "Tasks" in rail sub-items)

Interactive Textual list with drill-down:
- [ ] Summary bar at top with status counts
- [ ] Active tasks sorted: in_progress → review → queued → blocked → on_hold
- [ ] Status icons correct: ○ queued, ⟳ in_progress, ◉ review, ⊘ blocked, ⏸ on_hold, ◌ draft, ✓ done, ✗ cancelled
- [ ] Completed tasks in a separate section
- [ ] Click a task → detail view shows:
  - Status icon + task number + title
  - Status, priority, current node, owner
  - Flow template name
  - Roles (worker, reviewer)
  - Description (full text)
  - Acceptance criteria (if any)
  - Execution history: each node visit with status, decision, reason, work output summary
  - Context log: last 10 entries with actor and timestamp
- [ ] Press Escape → back to list
- [ ] Press R → list refreshes with current state
- [ ] Empty project (no DB) → shows "No tasks" message, not crash

### Cross-check

After each round, verify dashboard counts match `pm task counts -p weather_cli`
and the file sync in `issues/` matches the task states.

---

## Interaction Method

All communication with Polly happens through tmux:

```bash
# Send a message to Polly
tmux send-keys -t pollypm-storage-closet:pm-operator "your message" Enter

# Read Polly's response
tmux capture-pane -t pollypm-storage-closet:pm-operator -p | tail -40

# Check per-task worker output (use task slug)
tmux capture-pane -t pollypm-storage-closet:task-weather_cli-1 -p | tail -40
```

Or through the cockpit — click Polly in the rail, type in the right pane.

Monitor task state between rounds:
```bash
pm task list -p weather_cli
pm task counts -p weather_cli
```

---

## Exit Criteria

Testing is complete when:
1. All 10 rounds executed through Polly (not CLI shortcuts)
2. At least 2 rejections happened with successful rework
3. All task flows exercised: standard, spike, bug, user-review
4. Dependency chain worked (auto-unblock on approve)
5. Hold/resume worked
6. Per-task workers verified: worktree created, prompt injected, session torn down
7. Rejection delivered to existing worker session (not new session)
8. The weather-cli tool actually runs and produces output
9. Dashboard and file sync reflect reality
10. Zero uses of `pm send` by any agent during the entire test
