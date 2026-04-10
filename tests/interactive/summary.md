# PollyPM Interactive Test Suite — Summary

## Status

| State | Count |
|-------|-------|
| 01-ready | 105 |
| 02-running | 0 |
| 03-complete | 0 |
| **Total** | **105** |

## 01-Architecture & Domain (v1/01)

| Test | Description | Priority | Duration |
|------|-------------|----------|----------|
| T001 | **Fresh Install and pm up Launches All Three Session Roles** — Verify that running `pm up` on a fresh installation correctly launches all three session roles (heartbeat, operator, worker) inside a pollypm tmux ses | P0 | 10 minutes |
| T002 | **Human Can Attach to Any Session and Type Commands** — Verify that a human operator can attach to any active session (heartbeat, operator, worker) via tmux and interact with it by typing commands. | P0 | 10 minutes |
| T003 | **Session Roles Enforce Correct Behavior** — Verify that each session role (heartbeat, operator, worker) enforces its designated behavior: heartbeat monitors health, operator manages workflow and | P0 | 15 minutes |
| T004 | **State Store Records All Session Events with Timestamps** — Verify that the state store (SQLite) records all session lifecycle events with correct timestamps, including session creation, health checks, state tr | P1 | 10 minutes |
| T005 | **System Recovers if Heartbeat Crashes (Auto-Relaunch)** — Verify that if the heartbeat session crashes or exits unexpectedly, the system automatically detects the failure and relaunches the heartbeat session. | P0 | 10 minutes |
| T006 | **Override Hierarchy Works (Built-in < User-Global < Project-Local)** — Verify that the configuration override hierarchy is correctly enforced: built-in defaults are overridden by user-global settings, which are overridden | P1 | 15 minutes |
| T007 | **Cockpit Shows Real-Time Session States Correctly** — Verify that the cockpit (status dashboard) displays accurate, real-time session states for all active roles, updating when states change. | P1 | 10 minutes |

## 02-Configuration & Accounts (v1/02)

| Test | Description | Priority | Duration |
|------|-------------|----------|----------|
| T008 | **Add a New Claude Account via Onboarding** — Verify that the onboarding flow correctly adds a new Claude account, creates its isolated home directory, and makes it available for session assignmen | P0 | 10 minutes |
| T009 | **Add a New Codex Account via Onboarding** — Verify that the onboarding flow correctly adds a new Codex (OpenAI) account, creates its isolated home directory, and makes it available for session a | P0 | 10 minutes |
| T010 | **Account Home Isolation Verified (Separate CLAUDE_CONFIG_DIR per Account)** — Verify that each configured account has its own isolated home directory (e.g., separate CLAUDE_CONFIG_DIR) and that sessions using different accounts  | P0 | 10 minutes |
| T011 | **Failover Selects Healthy Non-Controller Account First** — Verify that when a session's assigned account becomes unhealthy, the failover mechanism selects a healthy non-controller account before considering th | P1 | 15 minutes |
| T012 | **Controller Account Used as Last Resort for Workers** — Verify that the controller account is only used for worker sessions as a last resort, after all other non-controller accounts are exhausted or unhealt | P1 | 15 minutes |
| T013 | **Remove an Account and Verify Sessions Reassigned** — Verify that removing a configured account causes all sessions assigned to that account to be gracefully reassigned to other available accounts. | P1 | 10 minutes |
| T014 | **Account Capacity Tracked and Cooldown Enforced** — Verify that the system tracks account usage capacity (e.g., rate limits, token budgets) and enforces cooldown periods when an account exceeds its limi | P1 | 15 minutes |

## 03-Session Management & Tmux (v1/03)

| Test | Description | Priority | Duration |
|------|-------------|----------|----------|
| T015 | **pm up Creates pollypm Tmux Session with Correct Windows** — Verify that `pm up` creates a tmux session named `pollypm` with the correct window layout: one window per session role, properly named and configured. | P0 | 10 minutes |
| T016 | **Pane Logging Captures All Output to Log Files** — Verify that all tmux pane output is captured to persistent log files on disk, enabling post-mortem analysis and audit. | P1 | 10 minutes |
| T017 | **Lease Model - Human Input Claims Lease, Automation Defers** — Verify that when a human attaches to a session and begins typing, the lease system recognizes human input and prevents automation from sending command | P0 | 10 minutes |
| T018 | **Lease Timeout Returns Control to Automation After 30 Min** — Verify that after a human claims a session lease and then stops interacting, the lease times out after 30 minutes and control returns to automation. | P1 | 35 minutes |
| T019 | **Git Worktree Created for Each Worker Session** — Verify that each worker session operates in its own git worktree, preventing file conflicts between concurrent workers. | P0 | 10 minutes |
| T020 | **Session Recovery with Checkpoint and Recovery Prompt** — Verify that when a session crashes and is relaunched, it receives a recovery prompt that includes the last checkpoint data, allowing it to resume work | P0 | 15 minutes |
| T021 | **Cross-Provider Recovery Reformats Prompt Correctly** — Verify that when a session fails over from one provider (e.g., Claude) to another (e.g., Codex), the recovery prompt is correctly reformatted to match | P1 | 15 minutes |
| T022 | **Ctrl-W Detaches, Ctrl-Q Shuts Down with Confirmation** — Verify that the custom key bindings work correctly: Ctrl-W detaches from the tmux session without affecting it, and Ctrl-Q initiates a shutdown with a | P1 | 10 minutes |

## 04-Extensibility & Plugins (v1/04)

| Test | Description | Priority | Duration |
|------|-------------|----------|----------|
| T023 | **Built-in Plugins Load at Startup** — Verify that all built-in plugins (claude, codex, local_runtime, etc.) are loaded automatically during startup and are functional. | P0 | 10 minutes |
| T024 | **Plugin Discovery Respects Precedence (Project-Local > User-Global > Built-in)** — Verify that when plugins with the same name exist at multiple levels (built-in, user-global, project-local), the system respects the precedence hierar | P1 | 15 minutes |
| T025 | **Plugin Failure Caught at Boundary, Doesn't Crash Core** — Verify that when a plugin throws an exception or fails, the error is caught at the plugin boundary and does not crash the core system or other plugins | P0 | 10 minutes |
| T026 | **Plugin Auto-Disabled After N Repeated Failures** — Verify that a plugin which fails repeatedly (N consecutive failures) is automatically disabled by the system to prevent ongoing disruption. | P1 | 15 minutes |
| T027 | **Service API Exposes All Operations Consistently** — Verify that the internal service API exposes all documented operations (account management, session management, issue management, etc.) and that plugi | P1 | 10 minutes |
| T028 | **Hook Routing Delivers Events to Observers** — Verify that the hook routing system correctly delivers events to all registered observer plugins, and that observers receive the expected event data. | P1 | 10 minutes |

## 05-Provider SDK (v1/05)

| Test | Description | Priority | Duration |
|------|-------------|----------|----------|
| T029 | **Claude Adapter Builds Correct Launch Command with Args** — Verify that the Claude provider adapter constructs the correct CLI launch command with all required arguments (model, system prompt, allowed tools, co | P0 | 10 minutes |
| T030 | **Codex Adapter Builds Correct Launch Command with Args** — Verify that the Codex provider adapter constructs the correct CLI launch command with all required arguments when starting a session. | P0 | 10 minutes |
| T031 | **Usage Snapshot Collected for Each Provider** — Verify that the system collects usage snapshots (token counts, cost estimates, API calls) for each provider session and stores them for tracking and b | P1 | 10 minutes |
| T032 | **Health Classification Parsed Correctly from Pane Output** — Verify that the heartbeat system correctly parses pane output to classify session health status (healthy, idle, stuck, looping, exited). | P0 | 10 minutes |
| T033 | **Transcript Sources Declared and Accessible** — Verify that each provider adapter declares its transcript sources and that the system can access and read transcript data for auditing and cost tracki | P1 | 10 minutes |

## 06-Issue Management (v1/06)

| Test | Description | Priority | Duration |
|------|-------------|----------|----------|
| T034 | **Create New Issue via File-Based Tracker** — Verify that creating a new issue via the CLI results in a correctly formatted issue file in the file-based tracker, with all required metadata fields  | P0 | 10 minutes |
| T035 | **Issue Transitions Through All Six States Correctly** — Verify that an issue can transition through all six lifecycle states (open -> ready -> in_progress -> review -> done -> closed) and that invalid trans | P0 | 15 minutes |
| T036 | **PA Picks Issue from Ready Queue via next_available** — Verify that the PA (Project Assistant / operator) automatically picks the next available issue from the ready queue and assigns it to an idle worker. | P0 | 10 minutes |
| T037 | **PM Reviews Issue and Sends Back for Rework (Reject Loop)** — Verify that when the PM (operator) reviews a completed issue and finds it insufficient, it can reject the issue and send it back to the worker for rew | P0 | 15 minutes |
| T038 | **PM Reviews Issue and Marks Complete** — Verify that when the PM (operator) reviews a completed issue and finds it satisfactory, it marks the issue as done/complete, and the worker is freed f | P0 | 10 minutes |
| T039 | **Issue Counter Increments Atomically** — Verify that the issue counter increments atomically and does not produce duplicate IDs, even when multiple issues are created in rapid succession. | P1 | 10 minutes |
| T040 | **GitHub Issue Backend (if Configured) Syncs Labels** — Verify that when the GitHub issue backend is configured, issue state changes in Polly are synced to GitHub issues including labels (e.g., status label | P2 | 15 minutes |
| T041 | **Report Status Shows Correct Counts per State** — Verify that the issue report/status command shows correct counts of issues in each state (open, ready, in_progress, review, done, closed). | P1 | 10 minutes |

## 07-Project History Import (v1/07)

| Test | Description | Priority | Duration |
|------|-------------|----------|----------|
| T042 | **Import News Project with Git History** — Verify that the project history import feature can import an existing project's git history and create a structured project timeline from it. | P1 | 15 minutes |
| T043 | **Timeline Built from Git Commits Chronologically** — Verify that the project timeline is constructed from git commits in correct chronological order, including merge commits, branch points, and all relev | P1 | 10 minutes |
| T044 | **Generated Docs Include Cross-References** — Verify that documentation generated during project import includes cross-references between related documents (e.g., architecture doc references the d | P1 | 10 minutes |
| T045 | **User Interview Blocks Finalization** — Verify that the project import process includes a user interview step that blocks documentation finalization, ensuring human input shapes the final pr | P1 | 15 minutes |
| T046 | **Docs Committed to Git, INSTRUCT.md Gitignored** — Verify that generated project documentation is committed to git (available to all developers), but INSTRUCT.md (which contains runtime prompt instruct | P1 | 10 minutes |

## 08-Project State & Memory (v1/08)

| Test | Description | Priority | Duration |
|------|-------------|----------|----------|
| T047 | **Project Overview Injected into Worker Session Prompt** — Verify that the project overview document is automatically injected into the worker session's system prompt, giving the worker context about the proje | P0 | 10 minutes |
| T048 | **Summary-First Pattern in All Generated Docs** — Verify that all generated documentation follows the summary-first pattern: each document begins with a concise summary before detailed content. | P1 | 10 minutes |
| T049 | **Memory Scopes (Global, Project, Issue, Session, Thread)** — Verify that the memory system supports all five scopes (global, project, issue, session, thread) and that each scope is isolated and accessible from t | P0 | 15 minutes |
| T050 | **Delta-Based Doc Updates Preserve Unchanged Sections** — Verify that when documentation is updated, only the changed sections are modified and unchanged sections are preserved exactly as-is (delta-based upda | P1 | 10 minutes |
| T051 | **Append-Only Docs (decisions.md, history.md) Never Rewritten** — Verify that append-only documents (decisions.md, history.md) are never rewritten — new entries are only appended to the end, and existing entries are  | P1 | 10 minutes |
| T052 | **Documentation Extraction Runs on Schedule** — Verify that the documentation extraction process runs on its configured schedule (e.g., after every N issues completed, or every M minutes), automatic | P1 | 15 minutes |

## 09-Inbox & Threads (v1/09)

| Test | Description | Priority | Duration |
|------|-------------|----------|----------|
| T053 | **Create Inbox Item and Verify It Appears in open/** — Verify that creating an inbox item places it in the open/ directory (or equivalent "open" state) and that it is visible for triage by the PM. | P0 | 10 minutes |
| T054 | **PM Triages Item into Thread** — Verify that the PM (operator) can triage an inbox item into a thread, creating a structured conversation thread for tracking and resolution. | P0 | 10 minutes |
| T055 | **Thread State Transitions Recorded with Timestamps** — Verify that all thread state transitions are recorded with timestamps, providing a complete audit trail of thread lifecycle events. | P1 | 10 minutes |
| T056 | **PM Routes to PA, PA Executes and Reports Back** — Verify that the PM (operator) can route a thread to a PA (worker), the PA executes the requested action, and the PA reports back to the PM with result | P0 | 15 minutes |
| T057 | **Closed Threads Persist in closed/ Directory** — Verify that when a thread is closed, it moves to the closed/ directory and persists there for historical reference, rather than being deleted. | P1 | 10 minutes |
| T058 | **Reopen Thread Appends New Messages** — Verify that reopening a closed thread moves it back to the open state and appends new messages without losing the original conversation history. | P1 | 10 minutes |

## 10-Heartbeat & Supervision (v1/10)

| Test | Description | Priority | Duration |
|------|-------------|----------|----------|
| T059 | **Heartbeat Launches as Session 0 Before Other Sessions** — Verify that the heartbeat session is always launched first (as Session 0) before operator and worker sessions, ensuring monitoring is in place before  | P0 | 10 minutes |
| T060 | **Health Classified on Every Heartbeat Cycle** — Verify that the heartbeat classifies the health of every session on every heartbeat cycle, producing a health record for each cycle. | P0 | 10 minutes |
| T061 | **Stuck Session Detected After N Consecutive Idle Cycles** — Verify that the heartbeat detects a stuck session after N consecutive cycles where the session shows no progress, and generates an alert. | P0 | 15 minutes |
| T062 | **Looping Detected via Repeated Command Patterns** — Verify that the heartbeat detects a looping session — one that is producing output but repeating the same commands or patterns without making forward  | P1 | 15 minutes |
| T063 | **Exited Session Detected When Process Gone** — Verify that the heartbeat promptly detects when a session's process has exited and classifies it accordingly. | P0 | 10 minutes |
| T064 | **Alert Created and Stored in SQLite** — Verify that alerts generated by the heartbeat system are stored in the SQLite database with all required fields and are queryable. | P1 | 10 minutes |
| T065 | **Intervention Escalation: Nudge -> Reset -> Relaunch -> Failover** — Verify that the heartbeat escalates interventions through the correct sequence: nudge, reset, relaunch, failover — each step attempted only when the p | P0 | 20 minutes |
| T066 | **Heartbeat Checkpoints (Level 0) Recorded Every Cycle** — Verify that a Level 0 checkpoint is recorded on every heartbeat cycle, capturing the state of all sessions at that point in time. | P1 | 10 minutes |

## 11-Agent Personas & Prompts (v1/11)

| Test | Description | Priority | Duration |
|------|-------------|----------|----------|
| T067 | **Persona Assigned at Project Creation** — Verify that when a project is created, a persona is assigned to the operator/PM role, defining its behavior, tone, and responsibilities. | P1 | 10 minutes |
| T068 | **Universal Rules Loaded for All Personas** — Verify that universal rules (applicable to all personas and roles) are loaded at session startup and included in every session's system prompt. | P0 | 10 minutes |
| T069 | **Rules Override Hierarchy (Project-Local Wins)** — Verify that when rules with the same name exist at multiple levels (built-in, user-global, project-local), the project-local version takes precedence. | P1 | 15 minutes |
| T070 | **Magic Catalog Injected at Session Start** — Verify that the "magic catalog" (a set of available commands, tools, and capabilities) is injected into each session's prompt at startup, giving the A | P1 | 10 minutes |
| T071 | **Only One Active Rule at a Time** — Verify that when multiple versions of the same rule exist at different precedence levels, only one version is active in the final prompt (no duplicate | P1 | 10 minutes |
| T072 | **Prompt Assembly Includes All Components** — Verify that the fully assembled prompt for a session includes all required components in the correct order: persona, universal rules, active rules, ma | P0 | 10 minutes |

## 12-Checkpoints & Recovery (v1/12)

| Test | Description | Priority | Duration |
|------|-------------|----------|----------|
| T073 | **Level 0 Checkpoint Created on Every Heartbeat Cycle** — Verify that a Level 0 checkpoint (heartbeat snapshot) is created automatically on every heartbeat cycle, capturing the minimal state needed for recove | P0 | 10 minutes |
| T074 | **Level 1 Checkpoint on Meaningful Work Completion** — Verify that a Level 1 checkpoint is created when a worker completes a meaningful unit of work (e.g., completing a sub-task, passing a test, committing | P0 | 15 minutes |
| T075 | **Level 2 Checkpoint on Major Direction Change** — Verify that a Level 2 checkpoint (full context snapshot) is created when a session undergoes a major direction change, such as switching issues, recei | P1 | 15 minutes |
| T076 | **Recovery Prompt Includes All Required Sections** — Verify that the recovery prompt assembled from a checkpoint includes all required sections: identity/persona, project context, issue details, progress | P0 | 10 minutes |
| T077 | **Recovery Respects Token Budget with Priority Truncation** — Verify that the recovery prompt respects the provider's token budget and uses priority-based truncation when the full recovery context exceeds the lim | P1 | 15 minutes |
| T078 | **Cross-Provider Recovery Reformats Correctly** — Verify that when a recovery requires switching providers (e.g., Claude to Codex), the recovery prompt is reformatted to match the new provider's expec | P1 | 15 minutes |
| T079 | **Rapid Failure Escalation After N Attempts** — Verify that if a session repeatedly fails recovery (crashes again immediately after relaunch), the system escalates rapidly rather than retrying indef | P0 | 15 minutes |

## 13-Security & Observability (v1/13)

| Test | Description | Priority | Duration |
|------|-------------|----------|----------|
| T080 | **Account Homes Created with Mode 700** — Verify that all account home directories are created with restrictive permissions (mode 700 — owner read/write/execute only), preventing other users f | P0 | 10 minutes |
| T081 | **No Credentials in pollypm.toml** — Verify that the main configuration file (pollypm.toml) does not contain any credentials, API keys, tokens, or secrets in plain text. | P0 | 10 minutes |
| T082 | **Event Log Records All Lifecycle Events** — Verify that the event log captures all major lifecycle events: session starts, stops, health checks, issue assignments, checkpoints, alerts, and inter | P1 | 10 minutes |
| T083 | **Cost Tracking via Transcript Ledger** — Verify that the transcript ledger tracks API costs per session and per account, enabling cost monitoring and budgeting. | P1 | 10 minutes |
| T084 | **Alert Pipeline End-to-End** — Verify the complete alert pipeline: from event detection to alert creation, storage, and display — ensuring no alerts are lost and all are actionable. | P1 | 10 minutes |
| T085 | **.pollypm Gitignored, docs/ Committed** — Verify that the `.pollypm` runtime directory is gitignored (keeping runtime state, credentials, and internal data out of version control) while the `d | P1 | 10 minutes |

## 14-Testing & Verification (v1/14)

| Test | Description | Priority | Duration |
|------|-------------|----------|----------|
| T086 | **Full Pytest Suite Passes** — Verify that the full pytest suite passes with no failures or errors, confirming the codebase is in a healthy state. | P0 | 10 minutes |
| T087 | **New Feature Has Unit + Integration Tests** — Verify that any recently added feature includes both unit tests (testing individual functions/classes) and integration tests (testing the feature in c | P1 | 15 minutes |
| T088 | **Bug Fix Includes Failing-First Test** — Verify that bug fixes in the codebase follow the "failing-first" test methodology: a test that reproduces the bug is written first (fails before the f | P1 | 15 minutes |
| T089 | **Plugin Validation Harness Works** — Verify that the plugin validation harness correctly validates plugins before they are loaded, catching malformed plugins and reporting clear error mes | P1 | 10 minutes |
| T090 | **Agent Interacts with Running System (Not Just Tests)** — Verify that the agent (worker AI) can interact with the real running system — executing real commands, modifying real files, and producing real outcom | P0 | 15 minutes |

## 15-Migration & Stability (v1/15)

| Test | Description | Priority | Duration |
|------|-------------|----------|----------|
| T091 | **Existing Config Loads After Update** — Verify that after updating Polly to a new version, existing configuration files load correctly without errors or data loss. | P0 | 10 minutes |
| T092 | **SQLite State Survives Schema Migration** — Verify that the SQLite state database survives schema migrations: existing data is preserved, new columns/tables are added cleanly, and the system fun | P0 | 15 minutes |
| T093 | **Existing Plugins Load After Core Changes** — Verify that existing plugins (both built-in and custom) continue to load and function correctly after core system updates, ensuring backward compatibi | P1 | 10 minutes |
| T094 | **Config Changes Backward-Compatible** — Verify that configuration file format changes are backward-compatible: old config files (without new fields) still work correctly, with new fields rec | P1 | 10 minutes |
| T095 | **Schema Migration Creates Backup** — Verify that when a schema migration is performed on the SQLite database, the system automatically creates a backup of the database before applying cha | P1 | 10 minutes |

## 16-End-to-End Workflows (cross-cutting)

| Test | Description | Priority | Duration |
|------|-------------|----------|----------|
| T096 | **Complete Project Lifecycle: Create, Assign, Complete, Review, Close** — Verify the complete project lifecycle end-to-end: create a project, create an issue, have it assigned to a worker, worker completes the task, PM revie | P0 | 30 minutes |
| T097 | **Multi-Project Orchestration: Two Projects Running Simultaneously** — Verify that Polly can manage two projects simultaneously, with separate sessions, issues, and workers for each project, without cross-contamination. | P1 | 30 minutes |
| T098 | **Polly as Effective PM: Give Goal, Verify Issue Creation and Worker Assignment** — Verify that Polly can function as an effective project manager: given a high-level goal, she creates the necessary issues, breaks down the work, assig | P0 | 30 minutes |
| T099 | **Full Recovery Scenario: Crash Session, Verify Auto-Recovery with Correct Checkpoint** — Verify the complete recovery scenario end-to-end: a worker actively processing an issue crashes, the heartbeat detects it, the system relaunches the s | P0 | 20 minutes |
| T100 | **Full Onboarding to First Completed Issue End-to-End** — Verify the complete end-to-end experience from a fresh installation: onboard an account, create a project, import history, start sessions, create an i | P0 | 45 minutes |

## 17-GitHub Issue Integration (v1/06 — Track 2)

| Test | Description | Priority | Duration |
|------|-------------|----------|----------|
| T101 | **GitHub Issues Display in Cockpit Rail** — Verify the Issues sub-item shows GitHub issues grouped by polly:* labels | P0 | 15 min |
| T102 | **GitHub Issue State Transitions** — Verify polly:* label changes move issues between states correctly | P0 | 15 min |
| T103 | **Polly Assigns GitHub Issue to Worker** — Full PM→PA assignment and execution workflow via GitHub | P0 | 20 min |
| T104 | **GitHub Backend Replaces File Backend** — Verify PollyPM project uses GitHub, not file-based tracker | P0 | 15 min |
| T105 | **GitHub Issue Review and Reject Loop** — Complete reject→fix→resubmit→approve cycle with GitHub comments | P0 | 25 min |

## Bugs Found

| Test | Bug | Fix | Commit |
|------|-----|-----|--------|
| | | | |

## Current Progress

Not started. Awaiting approval to begin testing.
