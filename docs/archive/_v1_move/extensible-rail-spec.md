# Extensible Cockpit Rail Specification

**Status:** v1 target. Depends on: plugin-api v1, cockpit.

## 0. Surface contract

PollyPM has **two live rail surfaces** and rail-facing issues must say
which one they affect:

- `src/pollypm/cockpit_ui.py` — the Textual cockpit launched by `pm`
  and used for the normal interactive UI.
- `src/pollypm/cockpit_rail.py` plus `src/pollypm/rail_daemon.py` —
  the text/daemon rail path that keeps heartbeat and recovery work
  alive when the cockpit is closed.

If a rail feature changes row rendering, indicators, keyboard
bindings, footer/ticker content, or section collapse behavior, the
default expectation is that **both surfaces** are updated. An issue may
scope itself to only one surface, but it must say so explicitly.

## 1. Purpose

Today the cockpit's left rail is a hardcoded list (Projects, Tasks, Settings, etc.). Plugins that want to add a visible surface — the Live Activity Feed, a Magic Skills explorer, a Downtime Backlog viewer — can't do it without editing cockpit code.

Make the rail pluggable: plugins register rail items against named sections with explicit indexes.

## 2. Rail anatomy

The rail is organized into **sections**. Each section is a named, ordered region. Plugins insert **items** into sections at explicit indexes.

Built-in sections (fixed, in this order):
- `top` (home, dashboard-at-a-glance)
- `projects` (per-project rail items)
- `workflows` (work-service surfaces — tasks, inbox, activity)
- `tools` (plugin-contributed tools)
- `system` (settings, plugins, version)

Plugins typically insert into `workflows` or `tools`. Inserting into `top`, `projects`, or `system` requires a manifest flag (`contributes_to_reserved_section=true`) that the plugin-host warns on at load.

## 3. Registration API

During `initialize(api)`:

```python
def initialize(api: PluginAPI) -> None:
    api.rail.register_item(
        section="workflows",
        index=30,
        label="Activity",
        icon="activity",            # optional; falls back to plugin's default icon
        badge_provider=live_count,  # optional; returns count for small badge on rail
        handler=open_activity_panel, # called when user selects the item
        visibility=always,          # "always" | Callable[[Context], bool] for conditional
    )
```

- **index** — integer ordering within section. Lower = higher in the rail. Convention:
  - Built-in core items own indexes 0–99 per section.
  - Plugin items start at 100.
  - Ties resolve by plugin name alphabetically.
- **badge_provider** — optional callable that returns a number or string for a small badge (e.g., unread count).
- **handler** — invoked when the user selects the item. Receives a `RailContext`. Returns a panel-spec (Textual widget + focus hint).
- **visibility** — controls whether the item is shown. Can be `"always"`, `"has_feature"` (only if a capability is registered), or a predicate.

## 4. Rendering contract

Cockpit's rail builder walks sections in order, within each section walks items by index, and renders:
- Icon (16px or ASCII char).
- Label.
- Badge (if badge_provider returns non-null).
- Keyboard shortcut (auto-assigned from section index + position, override-able).

Selection dispatches to the item's handler; the panel pane updates to show the returned widget.

## 5. Back-compat with current rail

The current hardcoded rail is re-expressed as a set of built-in items registered by `core_rail_items` (a new built-in plugin). This means the existing rail works identically on first load; external plugins just slot in alongside.

## 6. Settings

`pollypm.toml`:

```toml
[rail]
hidden_items = ["tools.activity"]      # list of "section.label" to hide
collapsed_sections = ["system"]        # sections that start collapsed
```

CLI:
- `pm rail list` — list registered items with section + index.
- `pm rail hide <section.label>` — append to hidden_items.
- `pm rail show <section.label>` — remove from hidden_items.

## 7. Implementation roadmap (er01–er04)

1. **er01** — Rail API on `PluginAPI`: `register_item`, `RailContext`, `PanelSpec`. Section definitions as a frozen registry (`top`, `projects`, `workflows`, `tools`, `system`).
2. **er02** — Convert current hardcoded cockpit rail to a built-in plugin `core_rail_items` registering items at indexes 0–99 per section. Rail rendering logic reads from registry instead of hardcoded list.
3. **er03** — Badge + visibility predicate support in the renderer.
4. **er04** — `pm rail list / hide / show` CLI + `[rail]` config.
