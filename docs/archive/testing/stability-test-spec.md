# Stability Test Spec — Pre-Demo Fix

Sam presents to investors tomorrow. Everything must work: cockpit, sessions,
task lifecycle, reviewer quality gate, multi-project concurrency.

## Fixed (committed 29e80c0)

- Config resolution: always uses `~/.pollypm/pollypm.toml`
- Project-level config renamed to `pollypm.dev.toml`
- Stale account refs: supervisor handles missing accounts gracefully
- Cockpit routing: never mounts heartbeat, prefers operator > reviewer > worker
- Operator tools: Write/Edit/MultiEdit/NotebookEdit blocked, prompt says NEVER write files
- Duplicate alerts: self-healing dedup on startup
- Settings freeze: 5s timeout on `claude auth status`
- Dead sessions: cockpit respawns operator/reviewer when clicked

## Test Checklist

### Phase 1: Config Resolution
- [ ] `pm status` from `~` loads `~/.pollypm/pollypm.toml`
- [ ] `pm status` from `~/dev` loads `~/.pollypm/pollypm.toml`
- [ ] `pm status` from `~/dev/pollypm` loads `~/.pollypm/pollypm.toml`
- [ ] `pm up` creates all sessions from the global config
- [ ] `pm up` when already running reconciles (no crash, no duplicate sessions)

### Phase 2: Session Launch
- [ ] `pm up` creates: heartbeat, operator, reviewer, plus project workers
- [ ] All sessions use `claude_pearl_swh_me` account
- [ ] All sessions show as running (not dead, not login error)
- [ ] Pearl account home is `/Users/sam/.pollypm/agent_homes/claude_1`
- [ ] No "Not logged in" warnings in any session

### Phase 3: Cockpit Rail
- [ ] Rail shows Polly at top
- [ ] Rail shows Russell
- [ ] Rail shows Inbox
- [ ] Rail shows all projects from global config
- [ ] Clicking Polly mounts the OPERATOR session (not heartbeat, not reviewer)
- [ ] Clicking Russell mounts the REVIEWER session
- [ ] Clicking a project shows the project dashboard
- [ ] If operator is dead, clicking Polly respawns it (not shows heartbeat)
- [ ] Kill operator, click Polly 5 times — always get operator back

### Phase 4: Settings Pane
- [ ] Settings loads without freezing
- [ ] Shows exactly 1 account: claude_pearl_swh_me
- [ ] Account shows correct home: /Users/sam/.pollypm/agent_homes/claude_1
- [ ] No stale accounts visible
- [ ] Settings refreshes without locking up

### Phase 5: Messaging
- [ ] Can send a message to Polly via `pm send operator "test message"`
- [ ] Polly responds (session is alive and authenticated)
- [ ] `pm notify` delivers to inbox
- [ ] Inbox shows the message
- [ ] Polly does NOT get poked about messages she sent herself

### Phase 6: Restart Resilience
- [ ] Kill pollypm + storage-closet tmux sessions
- [ ] `pm up` recovers cleanly
- [ ] All sessions come back
- [ ] Rail repopulates
- [ ] No IntegrityError or other DB crashes
- [ ] Cockpit state resets cleanly (no stale mounted_session)

### Phase 7: Operator Delegation
- [ ] Polly NEVER creates/edits files directly (tool-blocked)
- [ ] When given a task, Polly creates tasks via `pm task create`
- [ ] Polly assigns workers and monitors progress
- [ ] Polly notifies user when work is done

### Phase 8: Multi-Project Task Lifecycle (THE BIG ONE)

Run 3 projects through the full task lifecycle concurrently. Each project
gets a spec sent to Polly. Polly must break it into tasks, assign workers,
monitor progress, and have Russell review.

#### Project A: Recipe Share (`recipe_share`)
Spec: "Build a Python CLI recipe sharing tool. Users can add recipes with
ingredients and steps, list all recipes, search by ingredient, and export
to markdown. Use a JSON file for storage. Include a README with usage
examples."

Expected tasks (Polly decides, but roughly):
- Core data model + JSON storage
- CLI commands (add, list, search, export)
- README with usage examples
- Tests

#### Project B: Expense Tracker (`expense_tracker`)
Spec: "Build a Python expense tracking CLI. Users can add expenses with
amount, category, and date. List expenses with optional date range filter.
Show totals by category. Export to CSV. Use SQLite for storage."

Expected tasks:
- SQLite schema + data layer
- CLI commands (add, list, totals, export)
- README
- Tests

#### Project C: Team Standup (`team_standup`)
Spec: "Build a Python CLI for async team standups. Team members post
updates (what I did, what I'm doing, blockers). List today's standups.
Show who hasn't posted. Use a JSON file for storage. Include a simple
web view that renders today's standups as HTML."

Expected tasks:
- Data model + storage
- CLI commands (post, list, missing)
- HTML web view
- Tests

#### Task Lifecycle Verification (per project):
- [ ] Polly receives spec and creates tasks (not implements herself)
- [ ] Tasks have clear descriptions and acceptance criteria
- [ ] Tasks are queued and picked up by workers
- [ ] Workers claim tasks and produce code + commits
- [ ] Workers signal done with proper work output JSON
- [ ] Tasks enter review state
- [ ] Russell reads the actual code (git diff, file reads)
- [ ] Russell REJECTS at least once per project with specific feedback
- [ ] Workers address rejection feedback and resubmit
- [ ] Russell approves only when acceptance criteria are fully met
- [ ] Code is committed to the project repo
- [ ] The CLI actually works (can run the tool and get expected output)
- [ ] Polly notifies user with summary when project is complete

#### Concurrency Verification:
- [ ] All 3 projects run simultaneously (not sequentially)
- [ ] Workers don't interfere with each other
- [ ] Russell reviews tasks from all projects (not stuck on one)
- [ ] No session crashes from concurrent DB access
- [ ] No worker produces empty/no output (the silent worker bug)

#### Reviewer Quality Gate:
- [ ] Russell checks acceptance criteria item by item
- [ ] Russell verifies tests pass (runs them, doesn't trust worker's word)
- [ ] Russell rejects incomplete implementations
- [ ] Russell rejects missing tests
- [ ] Russell rejects code that doesn't build/import
- [ ] Russell provides specific, actionable rejection feedback
- [ ] Russell does NOT rubber-stamp (verify at least 1 rejection per project)

### Phase 9: Inbox Hygiene
- [ ] Close all stale messages (> 24 hours old)
- [ ] Auto-expire escalations that are > 4 hours old
- [ ] No poke loops (agent gets notified, checks, sees own message, loop)
- [ ] Message sender attribution is correct (Polly = "polly", not project name)

### Phase 10: Demo Rehearsal
- [ ] Fresh `pm up` from `~`
- [ ] Cockpit shows all projects, Polly, Russell, Inbox
- [ ] Send Polly a task for a new project
- [ ] Watch task flow: create → queue → claim → implement → review → done
- [ ] Verify the built app works
- [ ] Show inbox notifications
- [ ] Kill and restart — everything comes back
- [ ] Total wall time for one project end-to-end: under 15 minutes
