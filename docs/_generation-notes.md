# Generation Notes

**Generated:** 2026-04-22
**Generator:** Claude Opus 4.7 (1M context), per `/Users/sam/Desktop/llm-first-docs-prompt.md`
**Source of truth:** code in `src/pollypm/` at commit `5c9ad61` on branch `docs/llm-first-suite` off `main`

## Archive handling

All previous docs were moved to `docs/archive/_v1_move/` via `git mv` (preserves history). The pre-existing `docs/archive/` content (`deprecated-facts.md`, `notes/`, `README.md`, `snapshots/`, `testing/`) was left in place. `docs/archive/_v1_move/` now contains the previous `v1/`, `visuals/`, `future/`, `internals/`, `project-specs/` subdirectories along with the top-level markdown files (`architecture.md`, `conventions.md`, `decisions.md`, `history.md`, `ideas.md`, `risks.md`, `worker-guide.md`, `plugin-*.md`, etc.).

No legacy doc content was copied into the new suite. The new docs were re-derived from the code; archived docs were consulted only for "what topics exist."

## Features / modules documented (files written)

North Star and TOC:

- `docs/North-Star.md`
- `docs/Table-Of-Contents.md`

Top-level features (`docs/features/`):

- `cli.md` — `pm` Typer root + sub-apps
- `cockpit.md` — Textual cockpit shell
- `cockpit-rail.md` — rail registration contract
- `service-api.md` — `PollyPMService` facade
- `tasks-and-flows.md` — task lifecycle + flow templates + gates
- `inbox-and-notify.md` — `pm notify` + unified messages
- `activity-feed.md` — feature-level activity feed
- `agent-personas.md` — persona assembly
- `itsalive.md` — feature-level itsalive wrapper
- `jobs-queue.md` — durable SQLite job queue
- `upgrade.md` — `pm upgrade`

Modules (`docs/modules/`):

- `core-rail.md`, `supervisor.md`, `heartbeat.md`
- `work-service.md`, `plugin-host.md`, `state-store.md`
- `session-services.md`, `providers.md`, `runtimes.md`, `recovery.md`, `accounts.md`
- `memory.md`, `checkpoints.md`, `transcript-ingest.md`
- `release-check.md` (the exemplar-shaped module), `doctor.md`, `rail-daemon.md`
- `config.md`, `worktrees-and-projects.md`

Plugins (`docs/plugins/`):

- `core-agent-profiles.md`, `core-rail-items.md`, `core-recurring.md`
- `activity-feed.md`, `advisor.md`, `morning-briefing.md`, `downtime.md`
- `project-planning.md`, `memory-curator.md`
- `human-notify.md`, `task-assignment-notify.md`
- `itsalive.md`, `magic.md`

**Total new docs:** 45 (`North-Star.md` + `Table-Of-Contents.md` + 11 features + 19 modules + 13 plugins).

## Features collapsed into the TOC rather than given files

Per the prompt's guidance on "small, self-explanatory features" (< 200 words of real content). All of these are one-file plugins that simply register a single adapter class; a full file would be noise. Each has a one-line description in [Table-Of-Contents.md](Table-Of-Contents.md):

- `claude` plugin
- `codex` plugin
- `local_runtime` plugin
- `docker_runtime` plugin
- `tmux_session_service` plugin
- `local_heartbeat` plugin
- `inline_scheduler` plugin
- `default_launch_planner` plugin
- `default_recovery_policy` plugin

These are linked from the modules that carry the actual behavior (providers, runtimes, session-services, heartbeat, recovery).

## Verification results

Two automated passes were run after writing:

1. **Source path resolution.** Every backtick-wrapped src path that is not a glob or placeholder template must exist on disk. Result: **clean** (45 files).
2. **Cross-doc links.** Every relative markdown link must resolve. Result: **clean** (45 files).
3. **Symbol verification.** Every `def foo(` and `class Foo` referenced in a Core Contracts block was grepped against `src/`. Initial pass surfaced 15 symbols I had invented when writing contracts. Each was reconciled against the actual source:

| Symbol as written | Reality | Resolution |
|---|---|---|
| `NotifyAdapter` | `HumanNotifyAdapter` | Renamed in `plugins/human-notify.md` |
| `RetryPolicy` dataclass | `RetryPolicy = Callable[[int], timedelta]` | Corrected in `features/jobs-queue.md` |
| `JobQueue.claim_one / mark_done / mark_failed` | `claim / complete / fail` | Corrected in `features/jobs-queue.md` |
| `CockpitRouter.persist_selection` | `selected_key / set_selected_key` (state persisted to `cockpit_state.json`) | Corrected in `features/cockpit.md` |
| `perform_upgrade` | `upgrade(...)` (plus `plan_upgrade`, `detect_installer`) | Rewrote contracts in `features/upgrade.md` |
| `StateStore.record_session / set_session_runtime / record_account_usage / record_worktree` | `upsert_session / upsert_session_runtime / upsert_account_usage / upsert_worktree` | Corrected in `modules/state-store.md` |
| `Heartbeat.last_tick_at @property` | plain attribute | Corrected in `modules/heartbeat.md` |
| `Schedule.due_at` | `Schedule.next_due(after) + expression()` | Corrected in `modules/heartbeat.md` |
| `MemoryBackend.delete / sweep_ttl` | methods do not exist; TTL enforced via `StateStore.sweep_expired_memory_entries` | Corrected in `modules/memory.md` |
| `Supervisor.ensure_tmux_session / set_session_status / raise_alert / clear_alert / remove_session` | not on Supervisor (some on PollyPMService, alerts managed via StateStore) | Corrected in `modules/supervisor.md`; surface clarified as `bootstrap_tmux` / `shutdown_tmux` / `open_alerts` |

After reconciliation: **all referenced symbols resolve to a `def` or `class` in `src/`**.

## Judgment calls worth reviewer attention

- **Gaps called out.** The TOC's "Gaps and under-specified areas" section lists places where code and doc still diverge (Supervisor decomposition is incomplete, StateStore has tables not yet on SQLAlchemyStore, Docker runtime is stub-level, Gemini/Aider/OpenCode providers are not shipped). These are honest signal for reviewers; I did not try to smooth them over.
- **One-file plugins collapsed.** Nine thin adapter plugins (listed above) got TOC stubs instead of files. I judged a full file per "8-line plugin.py that forwards to a module" would be noise. Easy to split back out if a reviewer disagrees.
- **`magic` plugin.** Written as a compat-alias doc because the skills/ catalog is currently static content with no runtime registration. If the user plans to register skills via `[content]` in a future release, this doc will need an update.
- **Config doc** documents `PollyPMConfig` at a high level. I did not enumerate every dataclass field because `src/pollypm/models.py` is the authoritative source and it's short. Linking there instead.
- **Supervisor contracts.** The real Supervisor has ~50 methods. I listed the ones the service facade actually uses + the lifecycle hooks. The rest are internal and rendering them would conflict with the "still decomposing" narrative.
- **Personas doc** gives assembly order but does not reproduce persona text — that would make the doc a duplicate of `profiles.py`. The prompt rule "do not copy-paste large chunks of existing docs" applied here too.

## Process observations (for the blog post)

Four things stood out:

1. **Writing contracts from memory is a reliable source of small lies.** My first pass added plausible method names that didn't exist (`claim_one`, `mark_done`, `perform_upgrade`, `ensure_tmux_session`, `due_at`). The automated symbol-grep verifier caught 15 of them in one pass. Without that check, the docs would have shipped wrong. Lesson: every Core Contracts block needs a machine-checkable step.
2. **Cross-doc link discipline pays off immediately.** Because I used a consistent relative-link convention across the tree, the verifier caught zero broken links. Flat URL schemes (no versioning, no dated filenames) and a strict doc-tree shape (`features/`, `modules/`, `plugins/`) kept this tractable.
3. **The archive move was uncomfortably easy to get wrong.** `git mv docs/*` globs expand to include `docs/archive` itself, which would have created a recursive `docs/archive/archive` loop. I caught this on the first attempt by looping and skipping `archive` explicitly — worth noting for any repeat runs of this workflow.
4. **The prompt's "inverted pyramid" instruction is load-bearing for LLM consumers.** I wrote every Summary as if the reader might stop after that section, and the File Structure / Implementation Details blocks as "if you're still here, here's the hard stuff." The result reads much more like an agent-ready runbook than the archived docs did.
