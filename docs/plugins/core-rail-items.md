**Last Verified:** 2026-04-22

## Summary

`core_rail_items` re-expresses the built-in cockpit rail entries as plugin registrations so third-party plugins can slot alongside them through the same `RailAPI` interface. Before issue #er02 the rail list was hardcoded in `CockpitRouter.build_items`. Now the *behavior* still lives in the router — the registrations here just hand it back to the plugin host.

Touch this plugin when a new built-in rail entry needs to land or a reserved index needs to move. External plugins should not modify this file; they should register their own items in the `workflows` or `tools` section starting at index 100.

## Core Contracts

Registrations are constructed inside `initialize(api)` via `api.rail.register_item(...)` with callables like:

```python
api.rail.register_item(
    section="<section>",
    index=<0-99>,
    label="<label>",
    handler=<RailHandler>,
    key="<stable-key>",
    icon="<optional icon>",
    state_provider=<callable -> str | None>,
    label_provider=<callable -> str>,
    rows_provider=<callable -> list[RailRow]>,
    badge_provider=<callable -> int | None>,
)
```

Row/state providers reach back into the cockpit router via `RailContext.extras["router"]` and `RailContext.extras["spinner_index"]` so they can use the helpers the legacy hardcoded builder used (worker presence, project working-state, per-task worker roster).

## File Structure

- `src/pollypm/plugins_builtin/core_rail_items/plugin.py` — rail registrations.
- `src/pollypm/plugins_builtin/core_rail_items/pollypm-plugin.toml` — manifest.
- `src/pollypm/cockpit_rail.py` — `RailContext.extras` suppliers and the renderer.

## Implementation Details

- **Strict error mode.** `_strict_rail_errors_enabled()` returns true when `CI=1` or `POLLYPM_STRICT_RAIL_ERRORS=1`. Strict mode propagates rendering exceptions instead of swallowing them, so the test suite catches regressions.
- **Reserved indices.** Core uses 0–99 per section. Plugins must start at 100. The constraint is documented but not currently enforced at registration time.
- **Zero new behavior.** The logic lives in `CockpitRouter`. The plugin exists for shape — it is the proof that the rail is fully plugin-driven.

## Related Docs

- [features/cockpit-rail.md](../features/cockpit-rail.md) — rail contract.
- [features/cockpit.md](../features/cockpit.md) — cockpit UI.
