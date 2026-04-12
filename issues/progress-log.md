# Progress Log

## 2026-04-08

- Initialized the repo-local issue tracker for PollyPM overnight work.
- Backfilled the current work into explicit issues so active/queued/completed state is visible.
- Completed issue 0004 with a concrete Otter Camp docsv2 review report and moved it to `05-completed`.
- Completed issue `0007` by adding the inbox/thread model spec and moving the issue record to `05-completed`.
- Drafted the skills and MCP integration proposal for issue 0005 and moved it to review.
- Moved issue `0006` into `02-in-progress` while iterating on the cockpit UI and live self-hosting flow.
- Added a first-pass extensibility/platform architecture in `docs/extensibility-architecture.md` and moved issue `0008` into review.
- Added ready implementation issues for plugin host, service API/frontend transport, pluggable memory, pluggable task backend, and provider plugin SDK work.
- Implemented the first plugin host with manifest discovery, built-in/user/repo precedence, API version checks, provider/runtime resolution, and safe observer/filter execution.
- Started issue `0010` by adding a first `PollyPMService` layer and migrating worker creation/launch plus session focus/input flows in the TUI onto it.
- Started issue `0012` by extracting the default file issue tracker behind a task backend interface and routing tracker/scaffold/detail logic through it.
- Implemented issue `0011` by adding a pluggable memory backend interface with a default file-plus-SQLite implementation and checkpoint integration, then moved the issue to review.
- Added ready issues for a pluggable scheduler/cron backend, a pluggable heartbeat backend, and an agent profile backend so timing, monitoring, and agent behavior can be swapped like other platform subsystems.
- Implemented issue `0013` by adding a concrete provider SDK with transcript discovery, resume hooks, and usage snapshot collection, then moved the issue to review.
- Implemented issue `0010` by moving the TUI/control plane onto `PollyPMService` for session bootstrap, account mutation, project mutation, lease control, and worker/session operations, then moved the issue to review.
- Implemented issue `0012` by broadening the file task backend seam across project scaffolding and control-room/task flows, adding note/count coverage, and moved the issue to review.
- Implemented issue `0014` by adding a pluggable scheduler seam with an inline backend, due-job execution, recurring jobs, and service/supervisor delegation.
- Implemented issue `0015` by delegating heartbeat execution through a pluggable heartbeat backend and shipping the built-in local monitor backend.
- Implemented issue `0016` by adding built-in agent profiles for Polly, heartbeat, and workers, then routing control-session prompts through the profile seam.
- Implemented issue `0003` by switching token accounting over to JSONL transcript ingestion for Claude/Codex, adding hourly token rollups, and moving the issue to review.
- Began swapping user-facing naming from PollyPM toward PollyPM and Polly in onboarding and control-session prompts.
- Tightened the control room for issue `0006`: PollyPM branding now reaches the live tmux header and CLI help, the cockpit rail boots focused, dashboard rows act more intentionally, and the live tmux validation pass moved the issue to review.
- Closed review on implemented platform seams and docs: `0003`, `0005`, `0008`, `0009`, `0010`, `0011`, `0012`, `0013`, `0014`, `0015`, and `0016` are now in `05-completed`.
- Split adjacent polish work out of `0002` into fresh ready slices: `0017` for onboarding/project-discovery wording and `0018` for worker-lane kickoff/project launch polish.
- Implemented `0018` by making worker kickoff prompts project-aware, seeding tracked projects from the next real issue, and validating the new-worker modal in the live tmux control room before moving it to review.
- Implemented `0017` by tightening onboarding copy around recent local commits, preselected project suggestions, and the handoff into PollyPM, then validated the setup shell in tmux before moving it to review.
- Reviewed and closed the remaining operator-experience items: `0002`, `0006`, `0017`, and `0018` all passed targeted tests plus live tmux validation and moved to `05-completed`.

## 2026-04-11

- Completed issue `0024` by adding Codex `send_input` regression coverage for the extra submit Enter, updating stale docs that still described the behavior as broken, and validating the targeted supervisor tests.
- Continued `0024` cleanup by removing stale launch-blocker references from the readiness docs/visuals and reconciling the blocker counts and timeline estimate with the remaining three launch blockers.
- Completed issue `0025` by tracking superseded facts in `history_import.py`, generating `deprecated-facts.md`, and adding direct/unit/integration coverage for both supersession tracking and resilient knowledge-extraction assertions.
- Verified issue `0025` across the full repo with `pytest -q` (`460 passed`), so the history-import and extraction changes are green in the broader suite, not just the targeted tests.
- Completed issue `0027` by auditing direct module-to-test coverage gaps, adding baseline tests for `runtime_env.py`, `runtime_launcher.py`, and `models.py`, extending `supervisor.py` coverage for lease release and account viability, and moving the issue to `03-needs-review` after a final `uv run pytest -q` pass (`470 passed`).
- Completed issue `0019` by wiring the GitHub task backend through project scaffolding, service and CLI issue flows, review/handoff workflows, activation-time validation, mixed-backend support, and end-to-end GitHub pipeline integration coverage; targeted validation now passes with `80 passed`.
- Completed issue `0023` by replacing the oversized root README with the requested concise 4-sentence project description and moving it to `03-needs-review`.
- Updated issue `0022` with explicit API-level state-transition acceptance criteria, implementation notes, and concrete backend/service/CLI test evidence, then moved it to `03-needs-review`.
- Reviewed issue `0023` against its acceptance criteria, approved the concise root README, and moved it to `05-completed`.
- Reviewed issue `0022` against its API-level transition criteria, approved the documented backend/service/CLI evidence, and moved it to `05-completed`.
- Reviewed issue `0027` against its coverage-audit acceptance criteria, approved the new test coverage and full-suite results, and moved it to `05-completed`.
- Reviewed issue `0019` after fixing GitHub backend query determinism, approved the GitHub issue execution system work, and moved it to `05-completed`.
