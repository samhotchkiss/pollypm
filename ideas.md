# PollyPM Ideas

1. Account Auto-Failover
PollyPM should checkpoint a worker, switch to a healthy backup account, and resume automatically when an account hits its usage limit or starts throttling.

Feature 1 spec:
- Failover should happen automatically, without waiting for operator approval.
- Cross-provider failover is allowed by default so Claude work can move to Codex and vice versa.
- The controller account is eligible for worker failover, but it should be the last account selected.
- The `pm-heartbeat` and `pm-operator` sessions should use the same automatic failover and recovery model as normal worker sessions.
- Same-provider failover should still be preferred when a healthy account is available.
- Failover should reuse the same project and worktree, not create a second active worker.
- The tmux window should keep the same name and position through failover so the operator's workspace does not jump around.
- PollyPM should not discover capacity by trial-and-error failover attempts; it should maintain an explicit account-capacity registry.
- Both Claude and Codex should be treated as authoritative sources for remaining capacity and unblock timing.
- A pane becomes human-leased as soon as the human sends input into that session.
- A human lease ends either when PollyPM is explicitly told it may retake the pane, or after 30 minutes with no human interaction.
- If a human lease is active, PollyPM should keep monitoring and queue recommended actions, but it should not type into the pane or execute failover until the lease expires or is released.

Suggested selection order:
1. healthy non-controller account on the same provider
2. healthy non-controller account on a different provider
3. healthy controller account on the same provider
4. healthy controller account on a different provider

Recovery priority when capacity returns:
1. `pm-heartbeat`
2. `pm-operator`
3. human-interrupted sessions waiting to resume
4. worker sessions that were preempted by exhaustion or throttling
5. newly requested work

Suggested failover flow:
1. detect exhaustion, throttling, or auth failure
2. stop further automated input to the pane
3. capture transcript tail, latest snapshot, changed files, and checkpoint summary
4. select the best remaining eligible account
5. stop the failed worker pane
6. update the worker session to the new account and provider
7. relaunch in the same project/worktree
8. inject a recovery prompt built from the checkpoint
9. record the event and show it in the TUI

Control-session behavior:
- `pm-heartbeat` and `pm-operator` should be treated as pinned system roles that always recover into the same tmux window name and position.
- If either control session fails over, PollyPM should preserve the role prompt and recover it in place instead of creating a replacement window elsewhere.
- If both control sessions are down and only one account has recovered, `pm-heartbeat` should restart first because it is the liveness authority for the rest of the system.
- If no account has capacity, both sessions should move to `blocked_no_capacity` with the next known unblock time visible in the TUI.

Human lease behavior:
- Human input into a pane should automatically claim the lease for `owner=human`.
- PollyPM input should not overwrite an active human lease unless the user explicitly overrides it.
- Lease expiry should be activity-based, not heartbeat-based: each human message resets the 30-minute timer.
- While human-leased, the TUI should show:
  - lease owner
  - lease age
  - lease expiry time
  - whether a failover or restart is waiting behind the lease

Account capacity model:
- Each account should track:
  - current capacity state
  - last observed usage
  - reset / unblock time
  - next scheduled refresh time
  - whether it can currently accept work
  - the source of truth for that decision
  - live auth state
  - access-token expiry when the provider exposes it
  - whether refresh credentials are present
- Claude capacity should come from `/usage`.
- Codex capacity should come from `/status` or equivalent live status telemetry.
- Failover selection should only consider accounts that already show available capacity.
- If no eligible account exists, the worker should become `blocked_no_capacity` with a visible `retry_at`.
- When an account becomes available again, PollyPM should automatically reactivate waiting sessions based on the scheduler and queue state.

Failure classification:
- PollyPM should distinguish between:
  - `exhausted`
  - `throttled`
  - `auth_broken`
  - `provider_outage`
  - `blocked_no_capacity`
- A live `401`, `/login` prompt, or failed token refresh should move an account to `auth_broken`.
- A temporary upstream Claude/Codex outage should not be treated as a logout; it should move the affected account or provider pool to `provider_outage` with retry cadence and alerts.
- When the provider exposes token expiry or refresh metadata, PollyPM should track it and refresh or fail over proactively before the pane goes hard-fail.

2. Project Registry With Folder Health
Each known project should be a first-class managed folder with health, goals, queue state, and durable project memory.

Feature 2 spec:
- A project is a 1:1 relationship with a canonical folder path.
- A project does not have to be a git repo, but git-aware health and workflow should be used whenever git is present.
- PollyPM is a management layer on top of the project folder and the agent work happening inside it.
- Project-local PollyPM state should live in `<project>/.pollypm-state/`.
- If the project is a git repo, PollyPM should add `.pollypm-state/` to `.gitignore`.
- PollyPM should maintain a living project dossier automatically from observed PA/user/PM conversations, project inspection, and explicit clarification with the user.
- PollyPM should be able to ask lightweight confirmation questions when project priorities or values are inferred but not certain.
- PollyPM should also provide pushback when new instructions appear to conflict with the project's confirmed goals, values, or architectural direction.
- If the user asked only for an end result, PM may automatically redirect method and execution.
- If the user explicitly required a build mechanism or architectural approach, PM should escalate to the user instead of silently overriding it.

Project dossier:
- The dossier should be a curated understanding layer, not just raw logs.
- It should include:
  - north star / project vision
  - confirmed goals and values
  - inferred goals and values
  - constraints and architectural preferences
  - important decisions and rationale
  - active risks and drift concerns
  - open questions / needs-confirmation items
  - idea pad for ideas not ready to act on yet
  - up-next for likely next work the user wants queued mentally without distracting the PA now
- PM should preserve changes in project direction over time instead of flattening them into one static snapshot.

Logs and artifacts:
- Raw logs should still be stored in the project-local `.pollypm` directory, but separate from the curated dossier.
- Suggested structure:
  - `.pollypm-state/dossier/`
  - `.pollypm-state/logs/`
  - `.pollypm-state/artifacts/`
- Raw logs should include tmux captures, session transcripts, handoff notes, heartbeat snapshots, and review transcripts.
- Artifacts should include checkpoints, summaries, review bundles, merge notes, and incident exports.

Existing-project intake:
- When adding an existing project, PollyPM should do a discovery pass:
  - inspect folder structure
  - inspect key docs and entrypoints
  - inspect config and test setup
  - inspect commit history when git exists
  - inspect current active work
- PollyPM should synthesize a draft understanding of what matters, what the goals appear to be, and what the likely priorities are.
- Then it should interview the user to confirm or correct that understanding before locking it into the dossier.

Turn-end project management:
- On turn end, PM should not just note that a turn ended; it should review whether the work actually moved the project forward.
- PM should check:
  - did the agent complete the assigned work
  - does the output align with the intended task
  - did the agent stop early despite clear next steps
  - did it test or verify the work
  - can PM run or request additional verification
  - is the agent implicitly asking for a nudge or clarification
  - is there an obvious next step to keep momentum moving
  - did the agent misunderstand the assignment
  - did it drift from the larger project direction
  - did it touch files outside intended scope
  - did it leave the repo or folder in a worse state
  - did it claim success without evidence
  - did it skip obvious verification
  - did it produce new review or merge work for PM
  - did it consume a lot of time/budget for too little progress
- PM should also ensure the agent is committing meaningful progress regularly.
- PM should also choose the right model/provider and reasoning level for the PA, and adjust that choice when the work changes or the current setup is underperforming.

Meaningful progress enforcement:
- PM should optimize for meaningful project progress, not just visible activity.
- PM should detect and interrupt long-running low-yield loops, even when the agent appears to be making incremental progress.
- PM should enforce work being broken into small, measurable, usable, and testable chunks.
- PM should push agents toward modular, maintainable changes instead of giant monolithic edits.

Tracked-project mode:
- For substantial projects, PM should recommend installing a local issue-tracking queue.
- If the user approves, PollyPM should scaffold the queue automatically.
- PollyPM should keep a canonical setup/usage doc in the PollyPM repo, then apply that workflow to the project.
- Tracked projects should use the OtterCamp folder-state workflow:
  - `00-not-ready`
  - `01-ready`
  - `02-in-progress`
  - `03-needs-review`
  - `04-in-review`
  - `05-completed`
- PM owns the review-side transitions into `04-in-review` and `05-completed`.
- The issues directory should include a persistent latest-issue-number file so new issue IDs are monotonic and not guessed from filenames.

PA / PM split in tracked mode:
- PA is the implementation agent.
- PM is the manager, reviewer, and merge authority.
- PA should:
  - move an issue into `02-in-progress`
  - do the implementation work
  - move the issue into `03-needs-review`
  - hand off to PM in real time
- PM should:
  - notice or receive the review request
  - move the issue into `04-in-review`
  - review the work
  - either request changes or move the issue into `05-completed`
  - merge if ready
- PollyPM should expose a simple CLI/event path so PA can notify PM immediately when review is needed or a blocker occurs.

Worktree model:
- Without tracked-project mode, PollyPM should default to one active worker lane per project.
- With tracked-project mode, PollyPM should support:
  - one active PA implementation lane
  - one active PM review lane
- These should use separate worktrees so PA can start the next issue while PM reviews the last one.
- Helper lanes should be optional and explicitly spawned for bounded subproblems, not the default.

Project health:
- Suggested states:
  - `idle`
  - `active`
  - `waiting_human`
  - `needs_clarification`
  - `degraded`
  - `blocked_no_capacity`
  - `blocked_project`
  - `failed`
- `blocked_project` should cover missing folders, permissions issues, broken worktrees, invalid local setup, or other local project problems.
- Project health should reflect both folder state and supervised-work state.

3. Worker Templates
Users should be able to create reusable worker presets like `bug-fix`, `frontend-build`, `code-review`, or `docs-pass` with default prompts, provider preferences, and runtime settings.

4. Lease-Aware Human Takeover
When a human takes over a pane, PollyPM should automatically pause automation, mark the session as human-controlled, and offer a clean handoff flow back to automation.

5. Automatic Checkpoint Summaries
Every heartbeat should generate a compact restart prompt that captures goal, recent progress, current blockers, changed files, and next step.

Feature 3 spec:
- Checkpointing should be token-disciplined by default.
- PollyPM should maintain a cheap recovery spine continuously, and only spend tokens when a distilled summary is actually needed.

Checkpoint tiers:
- Level 0: mechanical snapshot
  - no model call
  - transcript tail
  - files changed
  - git status / diff metadata when available
  - commands/tests observed
  - queue/worktree/session state
- Level 1: compact summary
  - short model-generated handoff summary
  - used on turn end, failover, crash recovery, review handoff, or meaningful milestone
- Level 2: strategic summary
  - richer PM synthesis for major direction changes, drift concerns, or high-value restarts
  - used sparingly

Checkpoint triggers:
- Every heartbeat should create a Level 0 checkpoint for active sessions.
- Turn end should create a Level 1 checkpoint when meaningful work happened.
- Failover, crash recovery, restart, or review handoff should force a Level 1 checkpoint.
- PM may request a Level 2 checkpoint when strategic context is changing or a deeper intervention is needed.

Token-saving rules:
- Never resummarize unchanged context repeatedly.
- Summaries should be delta-based from the last meaningful checkpoint whenever possible.
- Transcript input should be capped aggressively.
- If there was no meaningful new activity, PollyPM should skip model-generated summarization.
- PollyPM should reuse prior checkpoint state and append or revise only the changed portions.

Checkpoint contents:
- project
- issue/task ID if any
- role
- current objective
- current sub-step
- work completed since last checkpoint
- files changed
- tests run and outcomes
- blockers / unresolved questions
- recommended next step
- confidence / quality notes
- queue/review state for tracked projects

Storage model:
- Raw logs remain in `.pollypm-state/logs/`.
- Checkpoints should live under `.pollypm-state/artifacts/checkpoints/`.
- PollyPM should keep both machine-readable metadata and a human-readable summary.
- One checkpoint should be marked as the latest canonical recovery point per session, while history remains available.

6. Worker Recovery After Crash
If a pane dies or a CLI exits unexpectedly, PollyPM should relaunch the session with the most recent checkpoint and transcript tail instead of just reporting failure.

7. Git Worktree Manager
PollyPM should create, name, track, and clean up per-worker git worktrees automatically so parallel sessions do not step on each other.

8. Merge Queue For Worker Output
Completed workers should be able to submit their branch, diff summary, and handoff note into a review queue so the operator can approve merge order cleanly.

9. Stuck Loop Detection
PollyPM should detect repetitive command loops, repeated failed edits, or circular reasoning and recommend either a reset, a new provider, or a different plan.

10. Strategy Reviewer Mode
Session 1 should be able to periodically review all active workers and suggest a better decomposition, kill low-value work, or spin up missing supporting sessions.

11. Budget Policy Engine
Users should be able to set rules like “use cheap accounts for exploration, expensive accounts for merge-critical work” or “never burn the controller account on worker tasks.”

12. Usage Forecasting
PollyPM should estimate how long each account will last at the current burn rate and warn before a project is likely to run out of usable capacity.

13. Session Timeline View
The TUI should include a per-session timeline showing prompts sent, major tool actions, file changes, alerts, restarts, and lease changes.

14. Transcript Search
Users should be able to search across all tmux logs and checkpoints by keyword, file path, project, provider, or session role.

15. Project-Specific Rulesets
Each project should support saved operating rules like preferred accounts, forbidden tools, review requirements, checkpoint cadence, and escalation thresholds.

16. Multi-Project Dashboard
PollyPM should show all tracked projects in one board with active sessions, account spend, blockers, and recommended next actions.

17. Auto-Spawn Supporting Workers
If a worker gets blocked on a narrow subproblem, PollyPM should suggest or launch a helper worker for that subtask instead of letting the main worker churn.

18. Daily Status Digest
PollyPM should generate a plain-English summary of what happened across projects: completed work, failed attempts, risky areas, account usage, and what needs attention next.

19. Exportable Incident Bundle
When things go off the rails, PollyPM should be able to export logs, checkpoints, alerts, pane captures, and project state into a single sharable debug bundle.

20. Plugin Adapter System
PollyPM should support third-party CLI adapters so new tools can be integrated without editing the core runtime and supervision logic.

## Sprint 1 Scope

In scope:
- Account auto-failover
- Project registry with project-local dossier and tracked-project mode foundations
- Token-disciplined checkpointing
- Worker/control-session crash recovery
- Git worktree manager
- Real-time PM messaging / handoff events
- Basic stuck-loop / low-value progress detection

Out of scope for Sprint 1:
- Usage forecasting
- Plugin adapter marketplace
- Daily digests
- Auto-spawn supporting workers by default
- Advanced merge queue orchestration
- Transcript search across all history
- Rich worker template system
- Deep strategic reviewer mode beyond basic drift/loop detection

Sprint 1 messaging default:
- The first messaging mechanism should be file-based and simple.
- PM messages should be created as files under `pollypm/inbox/00-open/`.
- Reviewed/handled messages should be moved to `pollypm/inbox/01-closed/`.
- The `pollypm/inbox/` directory should be gitignored.
- PA or PollyPM components should be able to create a new inbox message by writing a file there instead of requiring a richer message bus.
- The operator checks messages by asking PM whether there is any mail, and PM should walk through the open inbox items and then archive them to `01-closed` as they are handled.
