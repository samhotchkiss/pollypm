# Stability Test Results — April 15-16, 2026

## Phase 1: Config Resolution — PASS
- [x] `pm status` from `~` loads `~/.pollypm/pollypm.toml`
- [x] `pm status` from `~/dev` loads `~/.pollypm/pollypm.toml`
- [x] `pm status` from `~/dev/pollypm` loads `~/.pollypm/pollypm.toml`
- [x] `pm up` creates all sessions from the global config (11 sessions)
- [x] `pm up` when already running says "already running" (no crash, no dup)

## Phase 2: Session Launch — PASS
- [x] `pm up` creates: heartbeat, operator, reviewer, worker_pollypm + project workers
- [x] All sessions use `claude_pearl_swh_me` account
- [x] 6 of 11 sessions running (others degraded = no tmux window, expected for idle projects)
- [x] Pearl account home is `/Users/sam/.pollypm/agent_homes/claude_1`
- [x] Account logged_in=yes, health=healthy

## Phase 3: Cockpit Rail — PASS
- [x] Rail shows Polly at top (with working spinner)
- [x] Rail shows Russell
- [x] Rail shows Inbox (1)
- [x] Rail shows all 16 projects from global config
- [x] Cockpit state correctly tracks mounted_session = "operator"
- [x] Operator has correct tool restrictions (no Write/Edit)

## Phase 4: Settings Pane — PENDING
- [ ] Will test after task lifecycle

## Phase 5: Messaging — PARTIAL
- [x] `pm send operator --force` delivers messages to Polly
- [x] Inbox cleaned (9 stale messages closed)
- [ ] Polly response to message (monitoring)

## Phase 6: Restart Resilience — PASS
- [x] Kill pollypm + storage-closet, `pm up` recovers cleanly
- [x] All sessions recreated
- [x] No IntegrityError or DB crashes
- [x] Cockpit state resets (mounted_session cleared properly)

## Phase 7: Operator Delegation — PASS
- [x] Polly received Recipe Share spec and created task (recipe_share/9)
- [x] Polly queued the task (not implementing herself)
- [x] Polly started a worker for recipe_share
- [x] Polly said "This time I'll do it right — delegate, don't implement"
- [ ] Tool restriction verified (Write/Edit blocked in process args)

## Phase 8: Multi-Project Task Lifecycle — IN PROGRESS

### Recipe Share
- [x] Spec sent to Polly
- [x] Task created: recipe_share/9 "Build Recipe Share CLI"
- [x] Task queued
- [x] Worker start initiated
- [ ] Worker claims task
- [ ] Worker produces code + commits
- [ ] Worker signals done
- [ ] Russell reviews
- [ ] Russell rejects with feedback
- [ ] Worker fixes and resubmits
- [ ] Russell approves
- [ ] CLI actually works

### Expense Tracker
- [x] Spec sent to Polly
- [ ] Polly creates task (blocked on recipe_share worker-start)
- [ ] Task queued
- [ ] Worker claims and implements
- [ ] Russell reviews
- [ ] At least 1 rejection
- [ ] Approved and working

### Team Standup
- [x] Spec sent to Polly
- [ ] Polly creates task (blocked on recipe_share worker-start)
- [ ] Task queued
- [ ] Worker claims and implements
- [ ] Russell reviews
- [ ] At least 1 rejection
- [ ] Approved and working

### Concurrency Status
- All 3 projects running simultaneously: PASS
- recipe_share/10: APPROVED by Russell — CLI tested manually (add, list, search, export)
- expense_tracker/8: REJECTED by Russell — "No work done, task branch identical to main"
- expense_tracker/9: REJECTED by Russell — same reason (worktree code gap)
- team_standup/13: in review — waiting for Russell

### Reviewer Quality Gate: PASS
- Russell ran 2 parallel Explore agents (40+ tool uses each) to deeply read code
- Russell ran pytest on expense-tracker (tests passed but CLI wasn't built)
- Russell ran recipe_share CLI manually: tested list, search, export, edge cases
- Russell checked git branches and found zero commits on expense_tracker task branches
- Russell REJECTED 2 tasks with specific feedback (worktree code gap)
- Russell APPROVED 1 task after manual verification
- Rejection rate: 2/3 — quality gate is real, not rubber-stamp

### Issues Found
1. `worker-start` is a blocking call — Polly can't process other specs while waiting for it to finish. Should be async or have a timeout.
2. **Session identity swap**: Heartbeat got reviewer prompt, reviewer got heartbeat prompt during bootstrap. Root cause: parallel stabilization threads + first window targeted by index `:0` instead of name. Fixed by using window name for all targets.
3. **Codex args on Claude workers**: recipe_share, media, news project-local configs had `--dangerously-bypass-approvals-and-sandbox` (Codex flag). Workers crashed silently in a loop. Fixed: sanitize provider args, updated project configs.
4. **Stale resume markers**: `_bootstrap_clear_markers` missed account home dirs. Sessions used `--continue` and inherited wrong conversation context. Fixed: clear markers from account homes too.

## Phase 9: Inbox Hygiene — PARTIAL
- [x] All 9 stale messages closed
- [ ] No poke loops observed (monitoring)

## Phase 10: Demo Rehearsal — PENDING
