# Project Planning Plugin Specification

**Status:** v1 shipped. This spec describes the built-in project planning plugin that is already live in PollyPM.
**Implementation:** Entry module: `src/pollypm/plugins_builtin/project_planning/plugin.py`
**Depends on:** plugin-discovery spec (`docs/plugin-discovery-spec.md`), work service, session service, roster/jobs APIs.

## 1. Purpose

An opinionated architecture-agent plugin that runs when a new project is added (or on demand via `pm project replan`) and produces a decomposition into small, independently-testable modules with user-level test coverage and deliberate "magic."

Core principle: **produce the best possible outcome with minimal user input.** One touchpoint at the end (plan presented → approve / adjust / abort); everything upstream is autonomous.

## 2. Plugin containment

Single plugin at `plugins_builtin/project_planning/`. Owns only content + policy:

```
project_planning/
  pollypm-plugin.toml
  plugin.py
  profiles/
    architect.md
    critic_simplicity.md
    critic_maintainability.md
    critic_user.md
    critic_operational.md
    critic_security.md
  flows/
    plan_project.yaml
    critique_flow.yaml
    implement_module.yaml
  gates/
    wait_for_children.py
    log_present.py
    output_present.py
    user_level_tests_pass.py
  cli/project.py
```

Uses (does not own): `SessionService`, providers, runtimes, work service, worktree helpers, roster API, job handlers.

## 3. The planning flow (stages as flow nodes)

| Stage | Actor | What happens | Output |
|---|---|---|---|
| 0. Research | architect | ReAct loop: grep, read, list_files, web_search. Gather context before opinions. Budgeted (default 10 min). | Context artifact |
| 1. Discover | architect | Read spec + context. Ask clarifying questions via chat task only if truly under-specified. | Understanding artifact |
| 2. Decompose (tree-of-plans) | architect | Generate **2–3 alternative decompositions**, each a plugin/microservice breakdown. | Candidate decompositions |
| 3. Test strategy | architect | For each module: user-level test (Playwright for web, tmux for CLI/TUI). Unit tests assumed, not sufficient. | Test matrix per candidate |
| 4. Magic | architect | Dedicated pass: "how do we go 2× above a vanilla implementation?" Opinionated, pushing. | Magic list per candidate |
| 5. **Critic panel** | 5 critic personas, parallel | Each critic evaluates all candidates. Structured JSON output. Short-lived worker sessions. | Structured critiques |
| 6. Synthesize | architect | Pick best candidate, integrate critic objections as mitigations, produce Risk Ledger and narrative session log. | Plan + Risk Ledger + Session Log |
| 7. **User approval** | user | Plan presented with Risk Ledger. User approves, adjusts, or aborts. | Go/no-go |
| 8. Emit | architect | Creates N tasks in work service via `implement_module` flow, with acceptance criteria + test spec + dependency links. | Backlog |

## 4. The critic panel

Each critic runs as a **short-lived worker session** (new tmux pane, Claude Code with persona prompt, critique flow). Terminated after signaling done.

| Critic | Lens |
|---|---|
| simplicity | "What's the 20%-effort version? Where's the over-engineering?" |
| maintainability | "Will this rot in 6 months? Where are the hidden coupling risks?" |
| user | "Is this what the user actually needs? Are we building for an imagined persona?" |
| operational | "How does this deploy, debug, monitor? Where's the operational pain?" |
| security | "What's the attack surface? What fails on malicious input?" |

Critics run in **parallel** (5 panes simultaneously). Each emits structured JSON via `pm task done --output`. Planner synthesizes from the JSON; can read full transcripts via `.pollypm-state/session-archives/` for deep dives.

### Critic diversity rule

If >1 provider is registered (e.g. Claude + Codex), the critic-task provisioner **forces at least one critic onto the non-planner provider**. Real model diversity = less correlated blind spots. Enforced at provisioning time via a small resolver hook in the planner plugin.

## 5. Provider defaults

```yaml
# profiles/architect.md
preferred_providers: [claude, codex]

# Existing Russell (reviewer) profile update
preferred_providers: [claude, codex]

# Existing worker profile update
preferred_providers: [codex, claude]

# Each critic
preferred_providers: [claude, codex]  # diversity resolver overrides one
```

Opinion: **Claude for planning and review. Codex for code writing.** Users override per-persona in `pollypm.toml`.

## 6. Time budgets

Every stage has a default budget, configurable:

| Stage | Default budget |
|---|---|
| Research | 10 min |
| Discover | 5 min |
| Decompose | 10 min (per candidate × 2–3 candidates) |
| Test strategy | 5 min |
| Magic | 10 min |
| Each critic | 5 min |
| Synthesize | 10 min |

Hard cap prevents runaway loops. Scaling inference law applies: more compute → better plan, up to a point.

## 7. Output artifacts

1. **`docs/project-plan.md`** — formal plan (modules, test strategy, magic, sequence). Committed to repo.
2. **Risk Ledger section** — table: risk / category / mitigation / which critic raised it / status.
3. **`docs/planning-session-log.md`** — narrative account of the planning session. Who said what, key decisions, dissents. Human-readable six months later.
4. **Session archives** — full JSONL transcripts per critic session in `.pollypm-state/session-archives/<session-id>.jsonl`. Linked from the session log.

`log_present` gate on stage 6 requires (3) to be non-empty. `output_present` gate on critique_flow requires each critic's structured JSON.

## 8. Memory and replan

`pm project replan` reads (1), (2), (3), git history since last plan, and task completion records. Runs a drift analysis. Ledgered risks that materialized surface explicitly.

Cross-project learning: long-term memory stores "this worked / this didn't" per user. Planner reads past plans on startup and weights accordingly.

## 9. Integration

- **Trigger:** `pm project new <name>` finishes and prompts: "Run the planner now? (Y/n)" Explicit consent, not auto-firing.
- **On-demand:** `pm project plan` / `pm project replan`.
- **Initialization:** plugin's `initialize(api)` registers project.created hook + plan / replan CLI subcommands.

## 10. Implementation roadmap

Tracked as issues (see `pp01`–`pp10`). Order-of-record:

1. pp01: Plugin skeleton + 6 personas
2. pp02: Flow templates (plan_project, critique_flow, implement_module)
3. pp03: Gates (wait_for_children, output_present, log_present, user_level_tests_pass)
4. pp04: ReAct research stage + tool access
5. pp05: Tree-of-plans (multiple decomposition candidates)
6. pp06: Critic panel provisioning + diversity resolver
7. pp07: Time budgets per stage + configurable
8. pp08: Present-plan-to-user touchpoint + approval gate
9. pp09: Memory-backed replan + cross-project learning hooks
10. pp10: `pm project plan` / `replan` CLI + project.created hook + provider-policy updates to existing worker/russell profiles
