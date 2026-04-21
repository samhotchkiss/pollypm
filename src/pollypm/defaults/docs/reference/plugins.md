# Plugin System

This is the short current reference page for PollyPM plugins.

- For the working author walkthrough, start with `docs/plugin-authoring.md`.
- For the discovery and manifest spec, read `docs/plugin-discovery-spec.md`.

## Current Discovery Sources

Plugins are discovered from these sources, in precedence order:

1. Built-in bundles under `src/pollypm/plugins_builtin/`
2. Python entry points in the `pollypm.plugins` group
3. User-global drop-ins under `~/.pollypm/plugins/*/`
4. Project-local overrides under `<project>/.pollypm/plugins/*/`

## Current Manifest Shape

Directory plugins use `pollypm-plugin.toml` plus a `plugin.py` entrypoint.
Capabilities are declared with structured `[[capabilities]]` blocks, not the
older flat capabilities-list form.

Minimal example:

```toml
api_version = "1"
name = "my-plugin"
version = "0.1.0"
entrypoint = "plugin.py:plugin"
description = "My custom PollyPM plugin"

[[capabilities]]
kind = "provider"
name = "my-agent"
requires_api = ">=1,<2"
```

## Which Doc To Read

- Building or installing a plugin now: `docs/plugin-authoring.md`
- Understanding discovery precedence and manifest rules: `docs/plugin-discovery-spec.md`
- Provider-specific adapter contract: `docs/provider-plugin-sdk.md`
