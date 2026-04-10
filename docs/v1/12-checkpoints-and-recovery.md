---
## Summary

Checkpoints are PollyPM's recovery spine. They capture enough context to restart any session without losing progress. A three-tier checkpoint model balances cost against fidelity: mechanical snapshots are free and frequent, compact summaries are model-generated at meaningful boundaries, and strategic summaries are reserved for major direction changes. Recovery uses the latest checkpoint to construct a structured prompt that re-orients a fresh agent session to continue where the previous one left off.

---

# 12. Checkpoints and Recovery

## Design Goals

Checkpoints exist to solve one problem: when a session dies, restarts, or fails over to a different account, the new session must pick up where the old one left off without re-doing completed work or losing context about what was attempted.

This requires:

- Capturing enough state to reconstruct the agent's working context
- Doing so cheaply enough that it happens continuously
- Producing output structured enough that a recovery prompt can be built mechanically
- Never wasting tokens re-summarizing information that has not changed


## Checkpoint Tiers

PollyPM uses three checkpoint levels. Each tier builds on the one below it, adding model-generated intelligence at increasing cost.

### Level 0: Mechanical Snapshot

Level 0 is a pure data capture with no model call. It records observable state from the session environment.

Contents:

- Transcript tail: last N lines of pane output (configurable, default 100 lines)
- Files changed: list of modified, added, and deleted files since last checkpoint
- Git status: branch, clean/dirty, ahead/behind, uncommitted changes summary
- Git diff summary: stat-level diff (files changed, insertions, deletions)
- Commands observed: recent shell commands extracted from pane output
- Test results observed: pass/fail counts if test output is detected
- Queue state: current position in the project's work queue
- Worktree state: which worktree the session is operating in
- Session state: provider, account, role, tmux window, lease holder

Creation triggers:

- Every heartbeat cycle for active sessions (default: 30 seconds)
- On any session state transition detected by the heartbeat supervisor

Level 0 checkpoints are cheap. They involve only filesystem reads and tmux captures. They form the continuous baseline that ensures no state is ever more than one heartbeat interval stale.

### Level 1: Compact Summary

Level 1 adds a short model-generated handoff summary to the Level 0 data. This summary is written from the perspective of an agent handing off work to a successor.

Additional contents beyond Level 0:

- Objective summary: one-line description of what the session was working on
- Sub-step: where within the objective the session was (e.g., "writing tests for the parser module")
- Work completed: bullet list of concrete accomplishments since last Level 1 checkpoint
- Blockers: anything that was preventing progress
- Unresolved questions: decisions that need human or PM input
- Recommended next step: what the successor session should do first
- Confidence notes: how confident the agent was in its current approach

Creation triggers:

- Turn end, if meaningful work has occurred since the last Level 1
- Failover initiation (before switching accounts)
- Crash recovery (created from available state after detecting the crash)
- Review handoff (when work moves from implementation to review lane)
- Meaningful milestone (agent completed a significant sub-task)

"Meaningful work" is determined by comparing the current Level 0 against the last Level 1. If files have changed, tests have been run, or the git state has moved, work is meaningful. If the only change is elapsed time or repeated identical pane output, it is not.

### Level 2: Strategic Summary

Level 2 is a richer synthesis intended for human consumption and major context switches. It provides PM-level analysis of the session's trajectory.

Additional contents beyond Level 1:

- Progress assessment: how much of the overall task is complete
- Approach evaluation: whether the current technical approach is sound
- Drift analysis: whether the work has diverged from the original plan
- Risk factors: what could go wrong from here
- Alternative approaches: if the current path is questionable, what else could be tried
- Cross-session context: how this session's work relates to other active sessions

Creation triggers:

- PM request (the operator or human explicitly asks for a strategic checkpoint)
- Major direction change detected (significant divergence from original task description)
- Drift concern raised by heartbeat health classification
- High-value restart (session is being restarted on a premium account for critical work)

Level 2 checkpoints are created sparingly. They consume more tokens and are only valuable when strategic context matters.


## Checkpoint Data Schema

Every checkpoint, regardless of tier, contains a common metadata envelope.

### Metadata Fields

| Field | Type | Description |
|-------|------|-------------|
| `checkpoint_id` | string | Unique identifier (UUID) |
| `session_id` | string | Session this checkpoint belongs to |
| `project` | string | Project identifier |
| `issue_id` | string | Issue or task ID being worked on (if applicable) |
| `role` | string | Session role (worker, reviewer, operator, heartbeat) |
| `level` | int | Checkpoint tier (0, 1, or 2) |
| `created_at` | datetime | When this checkpoint was created |
| `trigger` | string | What caused this checkpoint (heartbeat, turn_end, failover, crash, pm_request, etc.) |
| `parent_checkpoint_id` | string | Previous checkpoint in this session's chain |
| `is_canonical` | bool | Whether this is the current recovery point for the session |

### Level 0 Fields

| Field | Type | Description |
|-------|------|-------------|
| `transcript_tail` | string | Last N lines of pane output |
| `files_changed` | list | Files modified since last checkpoint |
| `git_branch` | string | Current branch name |
| `git_status` | string | Clean/dirty, ahead/behind summary |
| `git_diff_stat` | string | Stat-level diff output |
| `commands_observed` | list | Recent commands extracted from pane output |
| `test_results` | object | Pass/fail/skip counts if detected |
| `queue_position` | object | Current queue state for the project |
| `worktree_path` | string | Active worktree directory |
| `provider` | string | Provider CLI in use |
| `account` | string | Account in use |
| `lease_holder` | string | Current input lease holder (automation or human) |

### Level 1 Fields (in addition to Level 0)

| Field | Type | Description |
|-------|------|-------------|
| `objective` | string | One-line description of current task |
| `sub_step` | string | Current position within the task |
| `work_completed` | list | Bullet list of accomplishments since last Level 1 |
| `blockers` | list | Things preventing progress |
| `unresolved_questions` | list | Decisions needing input |
| `recommended_next_step` | string | What the successor should do first |
| `confidence` | string | Confidence level and notes |

### Level 2 Fields (in addition to Level 1)

| Field | Type | Description |
|-------|------|-------------|
| `progress_pct` | int | Estimated percentage complete |
| `approach_assessment` | string | Evaluation of current technical approach |
| `drift_analysis` | string | Divergence from original plan |
| `risk_factors` | list | Things that could go wrong |
| `alternative_approaches` | list | Other paths if current one fails |
| `cross_session_context` | string | Relationship to other active sessions |


## Token-Saving Rules

Checkpoints must be economical. Every token spent on summarization is a token not spent on actual work.

### Delta-Based Summaries

Level 1 and Level 2 summaries are deltas from the previous checkpoint of the same or higher level. They describe what changed, not the full state.

- If the objective has not changed since the last Level 1, it is copied by reference, not re-summarized
- Work completed lists only work since the last Level 1, not all work ever done
- Blockers and questions are carried forward only if still relevant, removed if resolved

### Transcript Input Capping

When generating Level 1 or Level 2 summaries, the transcript input provided to the model is aggressively capped:

- Only the transcript since the last Level 1 checkpoint is included
- Long transcript segments are truncated to keep total input under a configurable limit (default: 4000 tokens)
- Structured data (git status, file lists, test results) is preferred over raw transcript text

### Skip Conditions

Model-generated summarization is skipped entirely when:

- No meaningful new activity has occurred (no file changes, no git movement, no new test results)
- The session has been idle since the last checkpoint
- The session is in a known-healthy idle state waiting for input

### State Reuse

When creating a new checkpoint:

- Unchanged fields from the previous checkpoint are copied, not regenerated
- Only fields affected by new activity are updated
- The checkpoint chain (via `parent_checkpoint_id`) makes it possible to reconstruct full state by walking backwards


## Storage Layout

### File Structure

```
<project>/.pollypm/
  logs/
    <session-id>/
      <launch-id>/
        pane.log
        supervisor.log
        snapshots/
  artifacts/
    checkpoints/
      <session-id>/
        <checkpoint-id>.json       # Machine-readable checkpoint
        <checkpoint-id>.md         # Human-readable summary (Level 1+)
        latest.json                # Symlink/copy of canonical recovery point
```

In multi-session scenarios, checkpoint paths are scoped per-session via the `<session-id>` directory. Each session's checkpoints are fully independent, enabling parallel recovery without path collisions.

### Storage Properties

- Machine-readable checkpoints are JSON files containing all fields from the schema above
- Human-readable summaries are generated only for Level 1 and Level 2 checkpoints
- The `latest.json` file always points to the most recent canonical checkpoint for the session
- Checkpoint history is retained indefinitely by default
- Old checkpoints can be pruned by a maintenance command, but the canonical checkpoint is never pruned
- Level 0 checkpoints older than a configurable retention period (default: 24 hours) may be pruned to save disk space

### State Store Integration

Checkpoint metadata is also recorded in the SQLite state store's `checkpoints` table. This enables:

- Fast lookup of the canonical checkpoint for any session
- Querying checkpoint history without scanning the filesystem
- Cross-session checkpoint queries (e.g., "what is the latest checkpoint for each session in project X")

The JSON files on disk are the source of truth. The SQLite records are an index.


## Recovery Flow

Recovery is the primary consumer of checkpoints. When a session fails, recovery uses the checkpoint to construct a new session that continues the work.

### Step 1: Detect Failure

Failure detection comes from:

- Heartbeat supervisor detects session exited, stuck, or looping
- Crash detection: tmux window disappeared or pane is unresponsive
- Explicit stop: human or operator stops a session that will be restarted

The heartbeat supervisor emits a `session_failure` event with the failure type and last known state.

### Step 2: Create Recovery Checkpoint

Before any recovery action, PollyPM creates a Level 1 checkpoint from whatever state is available:

- If the session is still alive (stuck, looping), capture current pane state and git status
- If the session has exited, use the last Level 0 snapshot and pane log tail
- If the tmux window is gone, reconstruct from the last available checkpoint and pane log file

This ensures the recovery checkpoint reflects the most recent state possible.

### Step 3: Select Target Account

Account selection follows the failover logic from doc 02. Reuse the same account if healthy; otherwise select the next available account for the provider, or attempt cross-provider failover. Account selection respects cooldown timers and capacity limits.

### Step 4: Relaunch Session

A fresh provider CLI session is launched in the same tmux window with the same project directory, role, and configuration. If failover occurred, the new account and provider are used. A new launch ID is recorded in the state store.

### Step 5: Inject Recovery Prompt

The recovery prompt is the critical artifact. It bridges the old session and the new one (see construction details below).

### Step 6: Record and Display

Recovery event is recorded in the state store (old/new launch IDs, failure type, checkpoint ID, account change). TUI is updated and operator is notified.


## Recovery Prompt Construction

The recovery prompt follows a fixed structure. It is not freeform — it is assembled mechanically from checkpoint data and project context.

### Prompt Sections

**1. Project Context**

Sourced from the project's `docs/project-overview.md` (see doc 07). Provides high-level context about the codebase, architecture, and conventions. Included verbatim if short, or summarized if large.

**2. What You Were Working On**

Sourced from the checkpoint's `objective` and `sub_step` fields. Tells the new session exactly what task it was performing and where it was in that task.

```
You were working on: Implementing the heartbeat health classifier
You were at step: Writing unit tests for the classify_health() function
```

**3. What Was Completed**

Sourced from the checkpoint's `work_completed` field. Lists concrete accomplishments so the new session does not repeat them.

```
Completed so far:
- Implemented classify_health() with five health states
- Added HealthState enum to models.py
- Wrote 3 of 7 planned unit tests
```

**4. Current File State**

Sourced from live `git status` at relaunch time (not from the checkpoint, which may be stale).

```
Git state:
- Branch: feature/health-classifier
- Modified: src/pollypm/health.py, tests/test_health.py
- Uncommitted changes: 2 files
```

**5. What To Do Next**

Sourced from the checkpoint's `recommended_next_step` field.

```
Next step: Complete the remaining 4 unit tests for classify_health(), then run the full test suite.
```

**6. Blockers and Open Questions**

Sourced from the checkpoint's `blockers` and `unresolved_questions` fields. Only included if non-empty.

```
Open questions:
- Should looping detection use a fixed threshold or adaptive windowing?
```

### Prompt Formatting

- The recovery prompt is provider-specific. Each provider adapter formats it according to the provider's prompt conventions.
- Total prompt size is capped to fit within the provider's context window, with headroom for the agent's own work.
- If the prompt would exceed the cap, sections are truncated in priority order: project context is summarized more aggressively first, then work completed is trimmed to the most recent items.


## Edge Cases

### No Checkpoint Available

If a session fails before any checkpoint was created (e.g., it crashed during initial launch), recovery falls back to:

- Project context from `docs/project-overview.md`
- The original task prompt from the work queue
- Git status from the project directory

This is equivalent to a fresh start with project context.

### Stale Checkpoint

If the most recent checkpoint is old (e.g., the session ran for a long time without triggering a Level 1), recovery supplements the checkpoint with:

- The pane log tail from the current launch
- Fresh git status and diff

The recovery prompt notes that the checkpoint may be stale and instructs the agent to verify current state before proceeding.

### Cross-Provider Recovery

When recovery involves switching providers (e.g., from Claude to Codex), the recovery prompt is reformatted by the new provider's adapter. The semantic content is the same; only the formatting changes.

### Multiple Rapid Failures

If a session fails, recovers, and fails again quickly:

- Each recovery creates a new checkpoint, forming a chain
- After a configurable number of rapid failures (default: 3), recovery is paused and an alert is escalated to the operator
- The operator can investigate, adjust the account or task, and manually resume


## Opinionated but Pluggable

The three-tier checkpoint model described above is PollyPM's opinionated default strategy. It is implemented as a **checkpoint strategy plugin** — the built-in strategy ships with PollyPM and is active unless overridden.

Users can replace the checkpoint strategy by registering a custom checkpoint plugin (see doc 04). A custom plugin can change:

- Which tiers exist and what triggers them
- What data each tier captures
- How summaries are generated (different models, different prompts, or no model at all)
- Retention and pruning policies
- Storage format and location (within the `.pollypm/` directory)

The plugin interface for checkpoint strategies is defined in the extensibility system (doc 04). The built-in strategy serves as both the default and a reference implementation.

This pattern — strong defaults that are fully replaceable — applies throughout PollyPM. Checkpoint strategy, security policies, testing requirements, and migration approach are all configurable and overridable. PollyPM ships opinionated defaults so the system works out of the box, but no default is sacred. Users can tailor any of these to their project's needs.


## Resolved Decisions

1. **Three-tier checkpoint model.** Level 0 is mechanical data capture, Level 1 adds model-generated summary, Level 2 adds strategic analysis. This layering keeps costs proportional to need — most checkpoints are free Level 0 snapshots.

2. **Level 0 is free.** No model calls for the baseline tier. This means continuous state capture has zero token cost, and checkpoint frequency is limited only by filesystem I/O.

3. **Level 1 only on meaningful work.** Model-generated summaries are skipped when nothing has changed. "Meaningful" is defined by observable state changes (files, git, tests), not elapsed time.

4. **Delta-based, not full-rewrite.** Summaries describe changes since the last checkpoint, not the full state from scratch. Unchanged fields are copied by reference. This saves tokens and keeps summaries focused.

5. **Transcript input is capped aggressively.** When generating summaries, only recent transcript since the last Level 1 is provided to the model, with a hard token cap. Raw transcript is never the primary recovery artifact — structured checkpoint data is.

6. **Recovery prompt is structured, not freeform.** The prompt follows a fixed section order assembled from checkpoint fields and project context. This ensures consistency across recoveries and providers, and makes the prompt mechanically verifiable.


## Cross-Doc References

- Account isolation, failover logic, and account selection: [02-configuration-accounts-and-isolation.md](02-configuration-accounts-and-isolation.md)
- Plugin system for custom checkpoint strategies: [04-extensibility-and-plugin-system.md](04-extensibility-and-plugin-system.md)
- Project history import and project-overview.md: [07-project-history-import.md](07-project-history-import.md)
- Heartbeat monitoring and health classification: [10-heartbeat-and-supervision.md](10-heartbeat-and-supervision.md)
- Task-specific prompt system: [11-agent-personas-and-prompt-system.md](11-agent-personas-and-prompt-system.md)
- Security and observability of checkpoint data: [13-security-observability-and-cost.md](13-security-observability-and-cost.md)
