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
| Pytest suite | **Working** | 530+ tests |

### Session Health (Right Now)
```
  pm-heartbeat       [Claude 2.1.101]   HEALTHY   sweeping every 60s
  worker-pollypm     [Codex gpt-5.4]    IDLE      cwd=/Users/sam/dev/pollypm
  worker-otter_camp  [Codex gpt-5.4]    IDLE      cwd=.pollypm/worktrees/otter_camp-pa
  worker-pollypm-web [Codex gpt-5.4]    IDLE      cwd=/Users/sam/dev/pollypm-website
  operator (Polly)   [Claude]           RUNNING   operator session active
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
| **Review gate enforcement still in progress** | Issues can still bypass intended review path in edge cases | Issue 0036 is not complete yet. |
| **Thread reopen still in progress** | Forward-only thread handling remains in some paths | Issue 0037 is still being implemented. |

### Annoying (Degrades Experience)
| Issue | Impact | Root Cause |
|-------|--------|------------|
| Cockpit cursor drift | Navigation sometimes lands wrong | Textual ListView + 0.8s refresh tick. Debounce helps but imperfect. |
| Review gate edge cases | Some flows still need manual scrutiny | Issue 0036 is in progress, not finished. |
| Thread reopen edge cases | Reopening/resuming threads is not fully reliable yet | Issue 0037 is in progress, not finished. |

### Gaps (Feature Missing)
| Feature | Status | Spec Reference |
|---------|--------|----------------|
| GitHub issue backend | Not built | T101-T105, issue 0019 |
| Review gate enforcement | In progress | T041, issue 0036 |
| Thread reopen | In progress | T058, issue 0037 |

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

### 1. Finish Review Gate Enforcement (P0, in progress)
Issue 0036 is in flight. The remaining work is to make the review gate airtight so issues cannot skip the intended states in edge cases.

### 2. Finish Thread Reopen (P0, in progress)
Issue 0037 is in flight. Remaining work is to complete reopen/resume behavior so thread state is no longer effectively forward-only in edge cases.

### 3. GitHub Issue Backend (P1, 3 days)
Issue 0019 specifies the design. Needs: 7 interface methods, `polly:*` label management, gh CLI integration. The file-based tracker contract is already defined.

### 4. Multi-user / operator-sharing hardening (P2)
The current system is still effectively single-operator even though the lease model is now stronger. Shared operation paths still need design and implementation.

### 5. GitHub-native issue sync polish (P2)
Once the backend exists, issue mirroring, reconciliation, and operator-facing visibility still need polish.

### 6. Review UX polish in cockpit (P2)
With review-gate enforcement nearly there, the next improvement is making review state and pending operator actions clearer in the TUI.

### 7. Reopen/resume UX polish (P2)
Once thread reopen lands, the operator-facing flow for reopening and continuing threads should be made explicit and low-friction.

### 8. Higher-level checkpoint policy (P2)
Level 1 checkpoints are now in place; the next step is deciding when to emit higher-level checkpoints and how operators consume them.

### 9. Broader role policy hardening (P2)
Role enforcement landed, but longer-term policy hardening is still useful as session types and tools expand.

### 10. Launch-readiness burn-down (P2)
After issues 0036 and 0037 land, re-audit the remaining rough edges and convert any true blockers into explicit launch criteria.

## Launch Readiness

### Must Have (Not Ready)
- [x] **Operator session working** — operator session is running
- [x] **Scheduler dedup** — completed in issue 0028
- [x] **Codex auto-submit** — `Supervisor.send_input()` sends the extra submit Enter for Codex and is regression-tested
- [x] **Cockpit state cleanup** — completed in issue 0029

### Should Have (Partially Ready)
- [ ] GitHub issue backend (spec exists, not built)
- [x] Lease/cockpit integration — completed in issue 0031
- [x] Worker nudge automation — completed in issue 0030

### Nice to Have (Not Started)
- [x] Memory system — completed in issue 0033
- [x] Level 1 checkpoints — completed in issue 0032
- [x] Role enforcement via tool restrictions — completed in issue 0034
- [ ] Multi-user support

### Already Solid
- [x] Core tmux management
- [x] Heartbeat monitoring + alerting
- [x] Recovery pipeline with failover
- [x] Operator recovery
- [x] Knowledge extraction
- [x] Token/cost tracking
- [x] File-based issue tracker
- [x] Account isolation + security
- [x] 530+ test suite
- [x] Cockpit TUI with navigation
- [x] Onboarding flow
- [x] 5 managed projects
- [x] Lease timeout handling
- [x] Lease/cockpit integration
- [x] Scheduler dedup
- [x] Worker nudge
- [x] Cockpit state cleanup
- [x] Memory system
- [x] Level 1 checkpoints
- [x] Role enforcement

### Verdict
**Close, but not launch-ready yet.** Most of the operational blockers from the April 11 snapshot are now resolved: operator recovery is working, scheduler dedup is done, Codex auto-submit is done, cockpit state cleanup is done, worker nudge is done, lease/cockpit integration is done, level 1 checkpoints are live, the memory system is integrated, role enforcement landed, and lease timeout handling is done. The remaining launch blockers are the in-progress review gate work (issue 0036) and thread reopen work (issue 0037), plus the still-missing GitHub issue backend.
