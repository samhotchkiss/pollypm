**Last Verified:** 2026-04-22

## Summary

`magic` is a compatibility-alias plugin. It registers a single `magic` agent profile whose prompt is `build_deploy_instructions()` from the itsalive module. Historically this lived under the umbrella "magic skills" branding; the current shape is a minimal shim that keeps older configs referencing `agent_profile = "magic"` working.

The plugin also ships a **Magic Skills catalog** — a directory of markdown files under `plugins_builtin/magic/skills/` (brainstorming, docx-create, mermaid-diagram, python-performance-optimization, etc.) that downstream tooling can reference. The catalog is static content; no runtime logic registers it today.

Touch this plugin only if an old config still references the `magic` persona. New work should use `itsalive` or the domain-specific persona instead.

## Core Contracts

```python
# src/pollypm/plugins_builtin/magic/plugin.py
plugin = PollyPMPlugin(
    name="magic",
    version="0.2.0",
    description="Compatibility alias for the dedicated itsalive plugin instructions.",
    capabilities=(Capability(kind="agent_profile", name="magic"),),
    agent_profiles={
        "magic": lambda: StaticPromptProfile(name="magic", prompt=build_deploy_instructions()),
    },
)
```

## File Structure

- `src/pollypm/plugins_builtin/magic/plugin.py` — the alias registration.
- `src/pollypm/plugins_builtin/magic/pollypm-plugin.toml` — manifest.
- `src/pollypm/plugins_builtin/magic/skills/*.md` — the skills catalog (~70 files: brainstorming, critique, design-taste-frontend, impeccable-code, pre-mortem, release-engineering, security-audit, systematic-debugging, etc.).

## Implementation Details

- **Shim only.** The plugin re-exports `StaticPromptProfile` from `core_agent_profiles` and `build_deploy_instructions` from the itsalive module. If both dependencies move, update the imports here.
- **Catalog lifecycle.** The `skills/` directory is not actively registered as content in v1. It remains as reference material that agents can read when pointed at a specific skill file. A future plugin may register it via the `[content]` manifest block.

## Related Docs

- [plugins/itsalive.md](itsalive.md) — source of the prompt text.
- [features/agent-personas.md](../features/agent-personas.md) — persona machinery.
