**Last Verified:** 2026-04-22

## Summary

The **rail** is the left-pane list in the cockpit. Every row is a `CockpitItem` — a section name, an index, a label, optional icon / state / badge, and a handler callable that tells the router what to show when the user selects it. The rail is fully driven by plugin registrations through `PluginAPI.rail.register_item(...)`; the built-in entries live in the `core_rail_items` plugin.

Sections 0–99 are reserved for core entries. Plugins start at 100. User config (`[rail]` in `pollypm.toml`) can hide items by `"section.label"` key and pre-collapse sections.

Touch this doc when changing the rail registration contract, section reservations, or row provider semantics. For adding a rail entry as a plugin, see `core_rail_items/plugin.py` for the canonical pattern.

## Core Contracts

```python
# src/pollypm/plugin_api/v1.py
@dataclass(slots=True)
class RailContext:
    config: PollyPMConfig | None
    extras: dict[str, Any]        # e.g. {"router": CockpitRouter, "spinner_index": 2}

@dataclass(slots=True)
class RailRow:
    label: str
    state: str | None = None       # e.g. "live", "idle", "alert", "watch"
    badge: int | None = None
    key: str | None = None

@dataclass(slots=True)
class PanelSpec:
    widget: object | None
    focus_hint: str | None = None

@dataclass(slots=True)
class RailItemRegistration:
    section: str
    index: int
    label: str
    handler: Callable[[RailContext], PanelSpec | None]
    key: str
    icon: str | None
    state_provider: Callable[[RailContext], str | None] | None
    badge_provider: Callable[[RailContext], int | None] | None
    label_provider: Callable[[RailContext], str] | None
    rows_provider: Callable[[RailContext], list[RailRow]] | None
    help_text: str | None

class RailAPI:
    def register_item(self, **kwargs) -> None: ...  # See RailItemRegistration fields

class RailRegistry:
    def items(self) -> list[RailItemRegistration]: ...
```

## File Structure

- `src/pollypm/cockpit_rail.py` — `CockpitItem`, `CockpitRouter`, `PollyCockpitRail`.
- `src/pollypm/cockpit_rail_routes.py` — route resolvers (live session / static view / project).
- `src/pollypm/plugin_api/v1.py` — `RailAPI`, `RailRegistry`, dataclasses.
- `src/pollypm/plugins_builtin/core_rail_items/plugin.py` — built-in rail entries.
- `src/pollypm/plugin_host.py` — owns the `RailRegistry` singleton.

## Implementation Details

- **Section precedence.** Built-ins take index 0–99 per section. Plugins must start at 100 or higher — the host does not currently enforce this but `core_rail_items` relies on it for ordering.
- **`state` semantics.** The renderer maps `"live"` / `"idle"` / `"alert"` / `"watch"` to palette entries (`live_bg`, `alert_bg`, etc. in `cockpit_rail.py:PALETTE`). Unknown states fall back to the normal styling.
- **Badge provider.** A closure called at render time. Return `None` or `0` to hide the badge. The activity feed uses a cursor file next to the state DB (`activity_feed_last_seen`) to remember the last-acknowledged event id.
- **Rows provider.** Rail entries can expose nested rows (e.g. a worker roster under a project). The provider returns a list of `RailRow` objects; the renderer indents them.
- **Handler output.** A handler either returns a `PanelSpec` (static view) or `None` after performing a side effect (e.g. calling `router.route_selected("activity")` to switch to a live view). Most plugin handlers use the side-effect form via `ctx.extras["router"]`.
- **Hidden items / collapsed sections.** `[rail]` in config. Hidden items are keyed by `"<section>.<label>"`. Example: `hidden_items = ["tools.activity"]`.

## Related Docs

- [features/cockpit.md](cockpit.md) — broader cockpit context.
- [modules/plugin-host.md](../modules/plugin-host.md) — `RailAPI` registration flow.
- [plugins/core-rail-items.md](../plugins/core-rail-items.md) — the canonical registration example.
- [plugins/activity-feed.md](../plugins/activity-feed.md) — badge provider example.
