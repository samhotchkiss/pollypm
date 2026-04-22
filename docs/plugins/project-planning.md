**Last Verified:** 2026-04-22

## Summary

`project_planning` is the architect + 5 critics planning pipeline. It registers six agent profiles (`architect`, `critic_simplicity`, `critic_maintainability`, `critic_user`, `critic_operational`, `critic_security`), one or more planning flow templates, and the accompanying gates / approval / replanning logic.

Touch this plugin when changing the planner persona text, critic count, flow templates, or planning-specific gates. Do not add unrelated planning subsystems here — split them.

## Core Contracts

Registered:

- Six `agent_profile` capabilities (one per persona) — all are `MarkdownPromptProfile` instances reading from `profiles/<name>.md`.
- Flow templates hosted via the `[content]` block in the manifest; loaded in `initialize(api)` at runtime.
- Project-planner gates registered against the work-service gate registry.

`pm project` CLI lives under `plugins_builtin/project_planning/cli/` — commands for creating plans, applying them, listing research, and running a critic panel.

Config (`pollypm.toml`):

```toml
[planner]
# see pollypm/models.py:PlannerSettings for knobs
```

## File Structure

- `src/pollypm/plugins_builtin/project_planning/plugin.py` — registration + `MarkdownPromptProfile`.
- `src/pollypm/plugins_builtin/project_planning/pollypm-plugin.toml` — manifest + `[content]` block.
- `src/pollypm/plugins_builtin/project_planning/profiles/architect.md` + `critic_*.md` — personas.
- `src/pollypm/plugins_builtin/project_planning/approval.py` — plan approval flow.
- `src/pollypm/plugins_builtin/project_planning/budgets.py` — per-stage budgets.
- `src/pollypm/plugins_builtin/project_planning/critic_panel.py` — parallel critic invocation.
- `src/pollypm/plugins_builtin/project_planning/downtime_backlog.py` — downtime backlog feeder.
- `src/pollypm/plugins_builtin/project_planning/flows/` — plan flow YAMLs.
- `src/pollypm/plugins_builtin/project_planning/gates/` — planner gates.
- `src/pollypm/plugins_builtin/project_planning/memory.py` — plan→memory projection.
- `src/pollypm/plugins_builtin/project_planning/plan_presence.py` — "does this project have a plan" checks.
- `src/pollypm/plugins_builtin/project_planning/proposals.py` — plan proposal assembly.
- `src/pollypm/plugins_builtin/project_planning/replan.py` — replan trigger.
- `src/pollypm/plugins_builtin/project_planning/research_stage.py` — research phase.
- `src/pollypm/plugins_builtin/project_planning/tree_of_plans.py` — branching plan structures.
- `src/pollypm/plugins_builtin/project_planning/cli/` — `pm project` commands.
- `src/pollypm/architect_lifecycle.py` + `src/pollypm/recovery_prompt.py` — integration with recovery + lifecycle outside the plugin package.

## Implementation Details

- **`MarkdownPromptProfile`.** Same class shape as the advisor / downtime personas. Plain `__slots__` class to avoid Python 3.14 dataclass + dynamic-module loading issues.
- **Critic panel.** `critic_panel.run_critic_panel(...)` invokes all five critics on a single plan payload in parallel, collects structured critiques, and returns them to the architect for incorporation.
- **Memory integration.** Approved plans project into the memory store so downstream workers have architectural context.
- **Downtime feeder.** `downtime_backlog.py` enables the downtime plugin to pull exploration candidates from the planning backlog.

## Related Docs

- [features/agent-personas.md](../features/agent-personas.md) — persona registration pattern.
- [features/tasks-and-flows.md](../features/tasks-and-flows.md) — gates and flows.
- [plugins/downtime.md](downtime.md) — consumer of the planner backlog.
