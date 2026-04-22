**Last Verified:** 2026-04-22

## Summary

`core_agent_profiles` is the built-in plugin that registers the five canonical personas (`polly`, `russell`, `heartbeat`, `worker`, `triage`). Each is wired as a `StaticPromptProfile` with its prompt from `profiles.py`. The plugin itself is tiny — all the prompt logic lives in `profiles.py` so other plugins (itsalive, magic) can compose on top without depending on this plugin's module.

Touch this plugin only to change the registered profile names or add a new core persona. Prompt text belongs in `profiles.py`.

## Core Contracts

```python
# src/pollypm/plugins_builtin/core_agent_profiles/plugin.py
plugin = PollyPMPlugin(
    name="core_agent_profiles",
    capabilities=(
        Capability(kind="agent_profile", name="polly"),
        Capability(kind="agent_profile", name="russell"),
        Capability(kind="agent_profile", name="heartbeat"),
        Capability(kind="agent_profile", name="worker"),
        Capability(kind="agent_profile", name="triage"),
    ),
    agent_profiles={
        "polly":     lambda: StaticPromptProfile(name="polly",     prompt=polly_prompt()),
        "russell":   lambda: StaticPromptProfile(name="russell",   prompt=reviewer_prompt()),
        "heartbeat": lambda: StaticPromptProfile(name="heartbeat", prompt=heartbeat_prompt()),
        "worker":    lambda: StaticPromptProfile(name="worker",    prompt=worker_prompt()),
        "triage":    lambda: StaticPromptProfile(name="triage",    prompt=triage_prompt()),
    },
)
```

## File Structure

- `src/pollypm/plugins_builtin/core_agent_profiles/plugin.py` — registration.
- `src/pollypm/plugins_builtin/core_agent_profiles/pollypm-plugin.toml` — manifest.
- `src/pollypm/plugins_builtin/core_agent_profiles/profiles.py` — `StaticPromptProfile`, `polly_prompt`, `reviewer_prompt`, `worker_prompt`, `heartbeat_prompt`, `triage_prompt`, `_render_operator_state_brief`, `_worker_context_parts`, `_reference_pointer`.
- `src/pollypm/plugins_builtin/core_agent_profiles/profiles/polly-operator-guide.md` — long-form operator manual referenced from the Polly prompt.

## Implementation Details

- **StaticPromptProfile.** Defined in `profiles.py` so downstream plugins (itsalive, magic) can import it for their own composition. See [features/agent-personas.md](../features/agent-personas.md) for the assembly sequence.
- **Operator state brief.** `_render_operator_state_brief(context)` produces a compact project/task/worker summary capped at 6 projects × 3 items per project. Polly and triage both include it; worker does not (workers are task-scoped).
- **Worker context.** `_worker_context_parts(context, project_root)` adds project persona-name notes and project-level context so a worker knows what project it's in and what its name is.
- **`_reference_pointer`.** Always last in the prompt. Tells the agent where to look things up (`pm help`, `docs/`). Keeping it last means long guide dumps don't push it out of the attention window in the middle.

## Related Docs

- [features/agent-personas.md](../features/agent-personas.md) — full persona assembly.
- [modules/plugin-host.md](../modules/plugin-host.md) — how `agent_profiles` factories are resolved.
