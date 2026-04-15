# Cockpit QA — Automated Testing Prompt

**Goal**: Systematically test every clickable element in the PollyPM cockpit, fix every bug found, and make the dashboard and task views genuinely useful. When Sam tests tonight, nothing should be broken.

**How to run**: Use `/loop` with this file's content, or run it as a long autonomous session.

---

## Context

The cockpit is a tmux-based UI. It has:
- A **left rail** (30 chars wide) with clickable items
- A **right pane** that shows either a live session or a static/Textual view

The rail items are:
```
Polly                    → mounts operator session (launches if missing)
Inbox                    → Textual inbox app
── projects ────────
  otter-camp             → click expands sub-items:
    Dashboard            → static project dashboard (task counts, active tasks, alerts)
    PM Chat (Otto)       → mounts worker session (launches if missing)
    Tasks                → Textual interactive task list with drill-down
    Settings             → project settings view
  PollyPM
    Dashboard
    PM Chat (Pete)
    Tasks
    Settings
  ... (10 projects total)
  Commit Validator
    Dashboard
    PM Chat (Val)
    Tasks
    Settings
Settings                 → global settings
```

### Key files
- `src/pollypm/cockpit.py` — routing, rail items, static view rendering, `_render_project_dashboard`, `_render_work_service_issues`
- `src/pollypm/cockpit_ui.py` — Textual apps: `PollyCockpitPaneApp`, `PollyTasksApp`, `PollyDashboardApp`, `PollyInboxApp`, `PollySettingsPaneApp`
- `src/pollypm/cli.py` — `cockpit-pane` command dispatches to Textual apps
- `src/pollypm/work/cli.py` — `pm task` commands, `_resolve_db_path`, `_svc`
- `src/pollypm/work/sqlite_service.py` — work service with sync hooks
- `src/pollypm/work/dashboard.py` — Textual dashboard widgets (TaskListWidget, TaskDetailWidget, etc.)
- `src/pollypm/supervisor.py` — session management, `send_input`, `_verify_input_submitted`, `_build_task_nudge`
- `src/pollypm/agent_profiles/builtin.py` — agent prompts
- `src/pollypm/transcript_ingest.py` — transcript sync (BlockingIOError fix)

### Config
- Config: `~/.pollypm/pollypm.toml`
- 10 projects, each with persona_name
- Sessions: heartbeat, operator, 8 workers
- Work service DBs: per-project at `<project-path>/.pollypm/state.db`
- Test project: `commit_validator` at `/Users/sam/dev/commit-validator` with 3 tasks (1 queued, 2 blocked)

---

## Phase 1: Rail Navigation — Every Click Works

For each item in the rail, verify that clicking it produces the correct view in the right pane. No errors, no wrong session mounted, no blank pane.

### 1.1: Polly (operator)
- Click "Polly" in the rail
- **Expected**: Operator session mounts in right pane. If operator window doesn't exist in storage-closet, it should be auto-launched first.
- **Verify**: Right pane shows a Claude Code session with Polly's prompt. NOT the heartbeat.
- **Bug indicators**: "heartbeat supervisor" in the session output, or a static fallback view instead of a live session.

### 1.2: Inbox
- Click "Inbox"
- **Expected**: `PollyInboxApp` renders in right pane. Shows inbox threads.
- **Verify**: No crash, inbox renders with correct thread count.

### 1.3: Each project — click to expand
For EACH of the 10 projects (otter_camp, pollypm, sam_blog_rebuild_restart_12, pollypm_website, news, itsalive, health_coach, media, camptown, commit_validator):

1. Click the project name
2. **Expected**: Rail expands to show Dashboard / PM Chat / Tasks / Settings sub-items. Dashboard is auto-selected and highlighted. Right pane shows the project dashboard.
3. **Verify**: The project name in the rail does NOT show persona name (just "camptown", not "camptown (Cole)"). Dashboard sub-item IS highlighted.

### 1.4: Dashboard for each project
For EACH project:
1. Click "Dashboard"
2. **Expected**: Right pane shows `_render_project_dashboard` output:
   - Project name
   - Summary bar with task counts by status (if work service DB exists)
   - Active tasks with status icons, assignee, current node
   - Recently completed tasks (last 5)
   - Alerts for the project's sessions
3. **If no work service DB**: Falls back to basic project info (path, kind, tracked, issue tracker, worktrees, alerts)
4. **Verify**: No crash, no traceback, readable output.

### 1.5: PM Chat for each project
For EACH project:
1. Click "PM Chat (<persona>)"
2. **Expected**: If a worker session exists for this project, mount it. If not, launch one via `supervisor.launch_session()` or `create_worker_and_route()`.
3. **Verify**: Right pane shows a live Claude/Codex session, not a static view. The session corresponds to the correct project (check cwd in the session).
4. **Bug indicators**: Wrong project's session mounted, session shows "not found" error, or static fallback.

### 1.6: Tasks for each project
For EACH project:
1. Click "Tasks"
2. **Expected**: `PollyTasksApp` renders — interactive Textual list view.
3. **For commit_validator**: Should show 3 tasks (1 queued, 2 blocked) with status icons.
4. **For pollypm**: Should show migrated tasks (mostly done).
5. **For projects without work service DB**: Should show "No tasks" or empty list. NOT a crash.
6. **Click a task in the list**: Should show task detail view with status, priority, node, owner, flow, roles, description, execution history, context log.
7. **Press Escape**: Should return to list view.
8. **Verify**: No crash for any project, even those without tasks.

### 1.7: Settings for each project
1. Click "Settings"
2. **Expected**: Project settings view renders.
3. **Verify**: No crash.

### 1.8: Global Settings
1. Click "Settings" at the bottom of the rail
2. **Expected**: Global settings view.
3. **Verify**: No crash.

---

## Phase 2: Dashboard Quality

The dashboard view is the first thing users see when clicking a project. It needs to be genuinely useful.

### 2.1: Dashboard content review
For each project with tasks, verify the dashboard shows:
- [ ] Project name as header
- [ ] Summary bar with counts: `○ 3 queued · ⟳ 1 in progress · ◉ 2 review` etc.
- [ ] Active tasks section with: status icon, task number, title, assignee (if any), current node
- [ ] Completed tasks section with count and last 5
- [ ] Alerts section if any alerts exist for this project's sessions

### 2.2: Dashboard for projects without work service
Projects that haven't had tasks created should show the fallback info view:
- Path, Kind, Tracked status
- Issue tracker status
- Active worktrees count
- Helpful message about how to start

### 2.3: Dashboard refresh
- The dashboard should not be laggy. If `_render_project_dashboard` is slow, profile and fix.
- The static view re-renders on each navigation — it should be fast (< 200ms).

---

## Phase 3: Tasks View Quality

The Tasks view (`PollyTasksApp`) is the interactive task list with drill-down.

### 3.1: Task list rendering
- Summary bar at top with status counts
- Active tasks grouped and sorted (queued first, then in_progress, then review, then blocked)
- Completed tasks in a collapsible section
- Status icons: ○ queued, ⟳ in_progress, ◉ review, ⊘ blocked, ⏸ on_hold, ◌ draft, ✓ done, ✗ cancelled

### 3.2: Task detail drill-down
Click on any task. The detail view should show:
- Status icon + task number + title
- Status, priority, current node, owner
- Flow template name
- Roles (worker, reviewer)
- Description (full text)
- Acceptance criteria (if any)
- Execution history: each node visit with status, decision (approved/rejected), decision reason, work output summary
- Context log: last 10 entries with actor and timestamp
- Press Escape to go back

### 3.3: Empty project
Click Tasks for a project with no work service DB. Should show empty state, not crash.

### 3.4: Task list refresh
Press R to refresh. List should update with current state.

### 3.5: Large task list
PollyPM has ~30 migrated tasks. Verify the list handles this without lag.

---

## Phase 4: Session Lifecycle

### 4.1: Operator auto-launch
- Kill the operator window: `tmux kill-window -t pollypm-storage-closet:pm-operator`
- Click "Polly" in the rail
- **Expected**: Operator session auto-launches and mounts
- **Verify**: Right pane shows Polly, not heartbeat

### 4.2: Worker auto-launch via PM Chat
- For a project whose worker isn't running (e.g. health_coach, media)
- Click "PM Chat (<persona>)"
- **Expected**: Worker session launches and mounts
- **Verify**: Right pane shows a live session in the correct project directory

### 4.3: Session send verification
- Send a message to the operator via `pm send operator "test message"`
- **Expected**: `_verify_input_submitted` confirms message left the input bar
- **Verify**: Message appears in the session's conversation, not stuck in input bar

### 4.4: Heartbeat task nudge
- Verify that when the heartbeat detects an idle worker with queued tasks, it sends a specific task claim message
- Check heartbeat logs or session output for: "You have work waiting. Task X — ..."

---

## Phase 5: Work Service Integration

### 5.1: Multi-project DB resolution
Run from workspace root (`/Users/sam/dev`):
```bash
cd /Users/sam/dev && uv run --directory /Users/sam/dev/pollypm pm task list -p commit_validator
cd /Users/sam/dev && uv run --directory /Users/sam/dev/pollypm pm task list -p pollypm
```
Both should return correct results from their respective DBs.

### 5.2: Task lifecycle via CLI
```bash
uv run pm task claim commit_validator/1
uv run pm task status commit_validator/1
uv run pm task done commit_validator/1 -o '{"type":"code_change","summary":"test","artifacts":[{"kind":"note","description":"test"}]}'
uv run pm task approve commit_validator/1 --actor polly
# Task 2 should auto-unblock
uv run pm task list -p commit_validator
```
Verify: Task 1 done, task 2 unblocked to queued, task 3 still blocked.

### 5.3: Error handling
```bash
uv run pm task queue commit_validator/1  # already done — clean error
uv run pm task get commit_validator/999   # not found — clean error
uv run pm task approve commit_validator/2 --actor worker  # wrong actor — clean error
```
All should show `Error: <message>`, NOT stack traces.

### 5.4: File sync
After task transitions, verify files appear in correct `issues/` subdirectories:
```bash
ls /Users/sam/dev/commit-validator/issues/01-ready/
ls /Users/sam/dev/commit-validator/issues/05-completed/
```

### 5.5: Skip-gates
```bash
uv run pm task create "No desc" -p commit_validator -f standard -r worker=worker -r reviewer=polly
uv run pm task queue commit_validator/4            # should fail (no description)
uv run pm task queue commit_validator/4 --skip-gates  # should succeed
```

---

## Phase 6: Responsiveness

### 6.1: Rail click latency
Time how long each rail click takes. Anything over 2 seconds is a bug.
- `route_selected` calls `_load_supervisor()` which calls `plan_launches()` — this can be slow
- Profile if needed and cache aggressively

### 6.2: Textual app startup
Time how long each Textual app takes to render:
- Tasks app for commit_validator (3 tasks)
- Tasks app for pollypm (30+ tasks)
- Inbox app
- Dashboard app

### 6.3: Background processes
Check that `pm up` doesn't spew errors into the terminal:
- No `BlockingIOError` from transcript ingest
- No uncaught exceptions from heartbeat cron
- No stale lock file errors

---

## Phase 7: Bug Fix Verification

### 7.1: Transcript ingest lock
```bash
# Run two syncs concurrently — second should skip silently
uv run pm heartbeat record &
uv run pm heartbeat record
# No BlockingIOError should appear
```

### 7.2: Resume state consistency
```bash
uv run pm task create "Hold test" -p commit_validator -f standard -r worker=worker -r reviewer=polly -d "test"
uv run pm task queue commit_validator/5
uv run pm task claim commit_validator/5
uv run pm task hold commit_validator/5
uv run pm task status commit_validator/5  # should be on_hold
uv run pm task resume commit_validator/5
uv run pm task status commit_validator/5  # should be in_progress (not queued) because execution is active
uv run pm task done commit_validator/5 -o '{"type":"action","summary":"test","artifacts":[{"kind":"note","description":"test"}]}'
# Should succeed — no state inconsistency
```

### 7.3: Human review enforcement
```bash
uv run pm task create "User review test" -p commit_validator -f user-review -r worker=worker -d "test"
uv run pm task queue commit_validator/6
uv run pm task claim commit_validator/6
uv run pm task done commit_validator/6 -o '{"type":"action","summary":"test","artifacts":[{"kind":"note","description":"test"}]}'
uv run pm task approve commit_validator/6 --actor polly    # should FAIL
uv run pm task approve commit_validator/6 --actor worker   # should FAIL
uv run pm task approve commit_validator/6 --actor user     # should succeed
uv run pm task approve commit_validator/6 --actor sam      # should also succeed
```

### 7.4: Circular dependency detection
```bash
uv run pm task create "A" -p commit_validator -f spike -r worker=worker -d "a"
uv run pm task create "B" -p commit_validator -f spike -r worker=worker -d "b"
uv run pm task link commit_validator/7 commit_validator/8 -k blocks
uv run pm task link commit_validator/8 commit_validator/7 -k blocks  # should fail: "circular dependency detected"
```

### 7.5: Auto-block on queue
```bash
# Tasks 7 and 8 from above — 7 blocks 8
uv run pm task queue commit_validator/7
uv run pm task queue commit_validator/8
uv run pm task list -p commit_validator --status blocked
# Task 8 should be blocked
```

---

## Bug Tracking

Every bug found: fix it immediately. Run `uv run pytest tests/test_cockpit.py tests/test_work_service.py tests/test_work_cli.py tests/test_supervisor.py tests/test_heartbeat_loop.py -x -q` after each fix to verify no regressions.

When all phases pass, run the full test suite: `uv run pytest tests/ -x -q --ignore=tests/integration/test_knowledge_extract_integration.py --ignore=tests/integration/test_history_import_integration.py`

---

## Phase 8: Real Project — ShortLink URL Shortener

A real web app project at `/Users/sam/dev/shortlink` with 5 tasks, full dependency chain, managed through Polly.

### Project: ShortLink
A FastAPI-based URL shortener with SQLite storage and a web UI.

### Tasks (already created in work service):
```
shortlink/1  queued   Project setup and data models     (no blockers)
shortlink/2  blocked  API endpoints                     (blocked by 1)
shortlink/3  blocked  Web UI                            (blocked by 1, 2)
shortlink/4  blocked  Test suite                        (blocked by 1, 2, 3)
shortlink/5  blocked  README and run instructions       (blocked by 4)
```

### 8.1: Tell Polly to build ShortLink
Send to the operator session:
> "I have a new project called ShortLink — a URL shortener. The tasks are already created in the work service under the shortlink project. Check pm task list -p shortlink and start assigning work to a worker."

**Expected**: Polly sees the 5 tasks, notices task 1 is the only one queued (others blocked), and either assigns it to a worker or asks to spin up a new worker.

**Fix-as-you-go rule**: If Polly doesn't know how to find the tasks (wrong DB, wrong project flag), fix the root cause. If the worker doesn't pick up work, fix the heartbeat nudge or the session launch. If messages don't get delivered, fix `send_input`/`_verify_input_submitted`. Do NOT work around problems by manually nudging — fix the underlying issue so it works autonomously.

### 8.2: Task 1 — Project setup
- Worker claims shortlink/1
- Worker creates pyproject.toml, src/shortlink/ package, data models, SQLite storage
- Worker signals done with work output
- Polly reviews and approves (or rejects with feedback)
- **On approval**: Task 2 should auto-unblock
- **Verify**: File sync moves task file from 01-ready to 05-completed

### 8.3: Task 2 — API endpoints
- Task 2 auto-unblocks when task 1 is done
- Worker claims, implements FastAPI endpoints, signals done
- Polly reviews
- **On approval**: Task 3 should auto-unblock

### 8.4: Task 3 — Web UI
- Task 3 auto-unblocks when tasks 1 and 2 are done
- Worker builds the HTML/CSS/JS UI
- Polly reviews

### 8.5: Task 4 — Test suite
- Auto-unblocks when 1, 2, 3 are done
- Worker writes pytest tests
- Tests should actually pass when run

### 8.6: Task 5 — README
- Auto-unblocks when task 4 is done
- Worker writes README
- This is the final task

### 8.7: End-to-end verification
When all 5 tasks are done:
1. The ShortLink app should actually work: `cd /Users/sam/dev/shortlink && uv run uvicorn shortlink.app:app`
2. All tests should pass: `cd /Users/sam/dev/shortlink && uv run pytest`
3. All 5 tasks should be in `done` status
4. File sync should show all 5 in `issues/05-completed/`
5. The dashboard for ShortLink should show `✓ 5 done`

### 8.8: What to fix along the way
This phase will surface real problems. Common ones to expect and fix:
- Polly can't find shortlink tasks (DB resolution)
- Worker session doesn't exist for shortlink (needs launch)
- Worker doesn't pick up work (heartbeat nudge broken)
- Messages don't reach sessions (send_input verification)
- Auto-unblock doesn't fire on approve (check _check_auto_unblock)
- File sync doesn't trigger (check sync hooks in sqlite_service)
- Polly reviews but can't approve because actor validation fails
- Dashboard doesn't show shortlink project data

Every one of these is a fix-it-now situation. Don't work around it. Don't manually nudge. Find the root cause and fix it so the system works autonomously.

---

## Exit Criteria

Testing is complete when:
1. Every rail item click produces the correct view with no errors
2. Dashboard renders useful content for all projects
3. Tasks view with drill-down works for all projects
4. Session auto-launch works for Polly and all PM Chat items
5. Work service CLI commands work across projects with clean errors
6. No `BlockingIOError` or other errors on startup
7. All cockpit, work service, and supervisor tests pass
8. Response time for rail clicks is under 2 seconds
9. ShortLink project completed end-to-end through Polly — all 5 tasks done, app works, tests pass
10. Zero manual nudges required — everything flows autonomously through heartbeat + task system
