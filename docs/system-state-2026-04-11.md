# PollyPM System State — April 11, 2026

## What PollyPM Is

PollyPM is a tmux-first supervisor for coordinating multiple interactive AI coding sessions. It lets Claude and Codex agents work in parallel while keeping every session visible, inspectable, and directly controllable by a human operator.

```
                          Human (Sam)
                              |
                    +---------+---------+
                    |   Cockpit (TUI)   |
                    |  Textual rail +   |
                    |  mounted session  |
                    +---+-----+-----+--+
                        |     |     |
          +-------------+     |     +-------------+
          |                   |                   |
   +------+------+    +------+------+    +-------+-----+
   |  Heartbeat  |    |  Operator   |    |   Workers   |
   |  (Claude)   |    |   (Polly)   |    | (Codex x3)  |
   |  monitors   |    |  manages    |    | implement   |
   +------+------+    +------+------+    +------+------+
          |                   |                   |
          +------- SQLite ----+------- tmux ------+
                  state.db         storage-closet
```

## What's Working

### Core Infrastructure (Solid)
| Component | Status | Evidence |
|-----------|--------|----------|
| tmux session management | **Working** | 4 sessions alive, auto-recovery functional |
| Heartbeat supervisor | **Working** | 16,569 heartbeat records, sweeping every 60s |
| Checkpoint system | **Working** | 16,528 level0 checkpoints captured |
| Alert pipeline | **Working** | 208 alerts raised, 8 currently open |
| Event logging | **Working** | 13,063 lifecycle events recorded |
| Token tracking | **Working** | 227 samples, 72 hourly aggregations |
| Knowledge extraction | **Working** | Docs regenerated, 3 successful runs |
| Cockpit TUI | **Working** | Rail navigation, session mounting, spinner |
| File-based issue tracker | **Working** | 23 issues across 6 states |
| Account isolation | **Working** | Separate homes, 700 permissions, keychain auth |
| Git worktree per worker | **Working** | otter-camp worker in dedicated worktree |
| Pytest suite | **Working** | 402 tests, 0 failures |

### Session Health (Right Now)
```
  pm-heartbeat       [Claude 2.1.101]   HEALTHY   sweeping every 60s
  worker-pollypm     [Codex gpt-5.4]    IDLE      cwd=/Users/sam/dev/pollypm
  worker-otter_camp  [Codex gpt-5.4]    IDLE      cwd=.pollypm/worktrees/otter_camp-pa
  worker-pollypm-web [Codex gpt-5.4]    IDLE      cwd=/Users/sam/dev/pollypm-website
  operator (Polly)   [Claude]           DOWN      139 recovery attempts, needs re-auth
```

### Scheduler Jobs
```
  heartbeat (x7)         pending   every 60s    no errors
  knowledge_extract (x3) pending   every 15min  no errors
```

## How It Works

### 1. Session Lifecycle
```
  pm up
    |
    v
  Create tmux sessions
    pollypm (cockpit)
    pollypm-storage-closet (sessions)
    |
    v
  Phase 1: Create windows (fast)
    heartbeat -> operator -> workers
    |
    v
  Phase 2: Cockpit TUI starts
    Textual app with rail + right pane
    |
    v
  Phase 3: Stabilize sessions (background threads)
    Send control prompts, wait for ready
```

### 2. Heartbeat Sweep (every 60s)
```
  Cockpit scheduler tick (every 5s)
    |
    v
  run_due() checks jobs.json
    |
    v
  supervisor.run_heartbeat()
    |
    +-> sync_token_ledger (ingest transcript JSONL)
    +-> For each session:
    |     capture pane snapshot
    |     hash for change detection
    |     classify: healthy / needs_followup / blocked / done
    |     record heartbeat + checkpoint in SQLite
    |     raise/clear alerts
    |     queue followup to operator if needed
    |
    v
  Record event: "Heartbeat sweep completed with N open alerts"
```

### 3. Recovery Pipeline
```
  Heartbeat detects failure
    |
    v
  What kind?
    |
    +-> pane_dead     -> recover immediately
    +-> missing_window -> recover immediately
    +-> auth_broken   -> try different account
    +-> provider_outage -> block 10min, retry
    |
    v
  _maybe_recover_session
    |
    +-> Check lease (defer if human holds it)
    +-> Rate limit (5 per 30min window)
    +-> Hard limit (20 total, then STOP)
    +-> Build candidate accounts (same provider first, then cross-provider)
    +-> _restart_session -> kill old window, launch new one
    |
    v
  On success: clear alerts, update runtime
  On failure: reschedule (recurring) or degrade
```

### 4. Cockpit Navigation
```
  j/k  = move cursor (with 4s debounce against refresh tick)
  Enter = open/mount selected item
  n    = create new worker for project
  s    = settings
  g/G  = first/last item

  Rail items:
    Polly (operator) -> mounts Claude session
    Inbox (N)        -> shows open messages
    Projects         -> mounts worker or shows static view
    Settings         -> account management TUI
```

## Rough Edges

### Critical (Blocks Daily Use)
| Issue | Impact | Root Cause |
|-------|--------|------------|
| **Operator won't recover** | No PM to manage workers | Claude auth needs re-login on macOS Keychain. Not a code bug. |
| **No role enforcement** | Any session does whatever asked | Prompt-based only, no tool restrictions. LLMs ignore role boundaries. |
| **Lease blind to cockpit** | Human typing unprotected | Direct tmux input bypasses supervisor.send_input() which creates leases. |

### Annoying (Degrades Experience)
| Issue | Impact | Root Cause |
|-------|--------|------------|
| Cockpit cursor drift | Navigation sometimes lands wrong | Textual ListView + 0.8s refresh tick. Debounce helps but imperfect. |
| Workers stall after planning | Need manual nudge to continue | Codex tends to plan then wait. Heartbeat escalates but doesn't nudge directly. |
| Codex needs double-Enter | Implementation must send a second Enter to submit | Codex CLI input buffering. `Supervisor.send_input()` handles this for Codex sessions and is now regression-tested. |
| Duplicate heartbeat jobs | 7 heartbeat jobs in scheduler | Jobs accumulate when cockpit restarts. No dedup on restart. |
| Stale cockpit_state.json | Mounted session can be wrong | File persists across cockpit restarts with stale pane IDs. |

### Gaps (Feature Missing)
| Feature | Status | Spec Reference |
|---------|--------|----------------|
| GitHub issue backend | Not built | T101-T105, issue 0019 |
| Memory system | Schema only, unused | T049 |
| Level 1/2 checkpoints | Infrastructure exists, never triggered | T074/T075 |
| Thread reopen | Forward-only state machine | T058 |
| Lease timeout | No auto-release | T018 |
| Review gate enforcement | Issues skip 03/04 states | T041 |

## What We Fixed Today (14 Commits)

```
  65ac0b4 Heartbeat scheduler retry + crash-safe sweep + LLM stdin + security
  71a82db Control session cwd -> workspace_root
  560f721 Classification with transcript snippets
  71a18a0 Proactive idle session escalation
  e62b874 Heartbeat followup feedback loop fix
  7a91cca Mounted pane provider validation
  4b1fe6b Claude Keychain auth viability
  61f87ae Issue transition validation + counter collision prevention
  8143863 Session runtime NULL clearing via sentinel
  8c329c0 Hard recovery limit (20 attempts)
  484852b Cockpit cursor drift debounce
  641695d Test: recovery hard limit
  082002e Tests: scheduler retry + LLM runner
  3d97a0a Tests: config cwd resolution
```

## Next 10 Things To Make It Better

### 1. Fix Operator Recovery (P0, 1 day)
The operator's Claude auth expired. Add `pm reauth <account>` that re-runs the Claude login flow from within a running session. Until this works, the PM role is dead.

### 2. Deduplicate Scheduler Jobs (P0, 2 hours)
7 duplicate heartbeat jobs accumulate when the cockpit restarts. `ensure_heartbeat_schedule` should clean stale/duplicate jobs, not just check if one exists.

### 3. Add Codex Auto-Submit to pm send (P1, 2 hours)
When `pm send` delivers text to a Codex session, follow up with an Enter keypress so the message actually submits. Currently requires manual Enter.

### 4. Lease Integration with Cockpit (P1, 1 day)
When the cockpit mounts a session pane, auto-claim a "cockpit" lease. When unmounted, release it. This protects human input from heartbeat interference.

### 5. Worker Nudge on Stall (P1, 4 hours)
When a worker has been idle for 5+ cycles, the heartbeat should send a direct nudge (not just tell the operator). Something like: "You appear stalled. State the remaining task in one sentence, execute the next step now."

### 6. GitHub Issue Backend (P1, 3 days)
Issue 0019 specifies the design. Needs: 7 interface methods, polly:* label management, gh CLI integration. The file-based tracker contract is already defined.

### 7. Clean Cockpit State on Restart (P1, 2 hours)
When the cockpit starts, validate cockpit_state.json — check that right_pane_id and mounted_session point to real, alive panes. Clear stale entries.

### 8. Memory System Integration (P2, 2 days)
The memory_entries and memory_summaries tables exist but are empty. Wire them into the heartbeat sweep so it records project learnings automatically.

### 9. Level 1 Checkpoints on Issue Completion (P2, 4 hours)
When a worker completes an issue (file moves to 05-completed), create a Level 1 checkpoint with the work summary. Currently only Level 0 (raw snapshots) exists.

### 10. Role Enforcement via Tool Restrictions (P2, 2 days)
Use Claude's `--allowedTools` and Codex's sandbox to restrict what each role can do. Heartbeat: read-only (no file writes). Operator: no direct code changes. Workers: no tmux/session management.

## Launch Readiness

### Must Have (Not Ready)
- [ ] **Operator session working** — needs re-auth or fallback to Codex operator
- [ ] **Scheduler dedup** — cockpit restarts create duplicate jobs
- [x] **Codex auto-submit** — `Supervisor.send_input()` sends the extra submit Enter for Codex and is regression-tested
- [ ] **Cockpit state cleanup** — stale state blocks recovery

### Should Have (Partially Ready)
- [ ] GitHub issue backend (spec exists, not built)
- [ ] Lease/cockpit integration (lease works for API, not cockpit)
- [ ] Worker nudge automation (heartbeat alerts but doesn't nudge directly)

### Nice to Have (Not Started)
- [ ] Memory system
- [ ] Level 1/2 checkpoints
- [ ] Role enforcement via tool restrictions
- [ ] Multi-user support

### Already Solid
- [x] Core tmux management
- [x] Heartbeat monitoring + alerting
- [x] Recovery pipeline with failover
- [x] Knowledge extraction
- [x] Token/cost tracking
- [x] File-based issue tracker
- [x] Account isolation + security
- [x] 402-test suite
- [x] Cockpit TUI with navigation
- [x] Onboarding flow
- [x] 5 managed projects

### Verdict
**Not launch-ready yet.** The operator crash loop and other remaining operational issues mean the system can't run autonomously. A human still needs to babysit sessions. Fix the remaining top blockers and the system can run a real multi-project workload without constant intervention.
