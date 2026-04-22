**Last Verified:** 2026-04-22

## Summary

`activity_feed` is the plugin that surfaces the Live Activity Feed — a unified reverse-chronological projection of events across the global `messages` table and per-project `work_transitions`. It registers one rail item (section `workflows`, index 30, icon `●`, label `Activity`) with a badge provider that counts unread events.

Feature-level details live in [features/activity-feed.md](../features/activity-feed.md). This doc covers only the plugin wiring.

## Core Contracts

```python
# src/pollypm/plugins_builtin/activity_feed/plugin.py
def build_projector(config: Any) -> EventProjector | None: ...

plugin = PollyPMPlugin(
    name="activity_feed",
    version="0.1.0",
    description="Live Activity Feed — unified projection ...",
    capabilities=(Capability(kind="hook", name="activity_feed.initialize"),),
    initialize=_initialize,
)
```

## File Structure

- `src/pollypm/plugins_builtin/activity_feed/plugin.py` — rail wiring, projector factory, last-seen cursor.
- `src/pollypm/plugins_builtin/activity_feed/pollypm-plugin.toml` — manifest.
- `src/pollypm/plugins_builtin/activity_feed/handlers/event_projector.py` — `EventProjector`, `FeedEntry`.
- `src/pollypm/plugins_builtin/activity_feed/summaries.py` — `activity_summary()` used by non-feed subsystems.
- `src/pollypm/plugins_builtin/activity_feed/cockpit/feed_panel.py` — Textual widget + plain renderer + `new_event_count`.
- `src/pollypm/plugins_builtin/activity_feed/cli.py` — `pm activity`.

## Implementation Details

- **No config.** Works out of the box. `build_projector(config)` returns `None` when no state DB is configured (e.g. minimal test stubs); callers handle `None` as "empty feed".
- **Cursor file.** `_last_seen_path(config)` returns `<state_db>.parent / "activity_feed_last_seen"`. Missing file → badge count = current event count.
- **Id prefixes.** `_handler_factory` accepts both `msg:` and `evt:` prefixes when writing the cursor — forward-compat with the unified messages ids and back-compat with legacy event ids.
- **Section choice.** `workflows` is non-reserved. The plugin manifest does not need the reserved-section flag.

## Related Docs

- [features/activity-feed.md](../features/activity-feed.md) — feature-level spec.
- [features/cockpit-rail.md](../features/cockpit-rail.md) — rail contract.
