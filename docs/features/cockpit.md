**Last Verified:** 2026-04-22

## Summary

The cockpit is PollyPM's Textual-based TUI — the thing you attach to with `pm` or `pm cockpit`. It renders a left-pane **rail** (project + plugin-registered entries) and a right-pane **panel** (dashboard, activity feed, inbox threads, task review, session live-peek, settings). A `CockpitRouter` persists the currently selected rail entry to `<project>/.pollypm/cockpit.json` and resolves each entry to either a live tmux session, a static-data view, or a project-scoped panel.

The cockpit shell lives in `cockpit_ui.py`. The rail renderer + router live in `cockpit_rail.py`. Panels are split across `cockpit_*.py` files plus `cockpit_sections/`. A plain-text rail renderer (`PollyCockpitRail`) exists for `pm rail` and headless contexts.

Touch this module when adding a new top-level panel or a new rail route. Plugin-registered rail items should go through `PluginAPI.rail.register_item(...)` — do not hardcode entries here.

## Core Contracts

```python
# src/pollypm/cockpit_rail.py
@dataclass(slots=True)
class CockpitItem:
    section: str
    index: int
    label: str
    handler: Callable[[RailContext], PanelSpec | None]
    key: str
    icon: str | None = None
    state: str | None = None
    badge: int | None = None
    rows: list[RailRow] = field(default_factory=list)

class CockpitRouter:
    def build_items(self) -> list[CockpitItem]: ...
    def route_selected(self, key: str) -> None: ...
    def selected_key(self) -> str: ...
    def set_selected_key(self, key: str) -> None: ...
    def create_worker_and_route(self, ...) -> None: ...

class PollyCockpitRail:
    """Pre-Textual, stdout-only rail renderer for `pm rail`."""
    def render(self) -> str: ...
```

Plugin rail registration (from the plugin-host side):

```python
# src/pollypm/plugin_api/v1.py
class RailAPI:
    def register_item(
        self, *, section: str, index: int, label: str,
        handler, key: str, icon=None, state_provider=None,
        badge_provider=None, label_provider=None,
        rows_provider=None, help_text=None,
    ) -> None: ...
```

## File Structure

- `src/pollypm/cockpit_ui.py` — the Textual shell, screen composition, app boot.
- `src/pollypm/cockpit.py` — compatibility-shim surface + dashboard section helpers.
- `src/pollypm/cockpit_rail.py` — `CockpitItem`, `CockpitRouter`, `PollyCockpitRail`, color palette.
- `src/pollypm/cockpit_rail_routes.py` — `LiveSessionRoute`, `StaticViewRoute`, `ProjectRoute` resolution.
- `src/pollypm/cockpit_palette.py` — theming / color constants.
- `src/pollypm/cockpit_activity.py` — activity-feed panel.
- `src/pollypm/cockpit_alerts.py` — alert toasts + notifier.
- `src/pollypm/cockpit_inbox.py` + `cockpit_inbox_items.py` — inbox thread rows and the thread viewer.
- `src/pollypm/cockpit_tasks.py` + `cockpit_task_priority.py` + `cockpit_task_review.py` — task list + detail + review modal.
- `src/pollypm/cockpit_workers.py` + `cockpit_worker_identity.py` — worker roster.
- `src/pollypm/cockpit_settings_*.py` — accounts / projects / history settings screens.
- `src/pollypm/cockpit_project_settings.py` — per-project settings.
- `src/pollypm/cockpit_formatting.py` — shared formatting helpers (relative time, tokens).
- `src/pollypm/cockpit_sections/` — dashboard sections (`header`, `summary`, `velocity`, `in_flight`, `recent`, `just_shipped`, `insights`, `health`, `action_bar`, `quick_actions`, `activity`, `downtime`, `you_need_to`, `recent_commits`, `project_dashboard`, `dashboard`).
- `src/pollypm/core/console_window.py` — tmux console-window manager for the cockpit.
- `src/pollypm/control_tui.py` + `control_tui_routes.py` — control-plane TUI variants.

## Implementation Details

- **Selection persistence.** The cockpit stores router state in `cockpit_state.json` next to the state DB (atomic write via `pollypm.atomic_io.atomic_write_json`). Restarts resume at the last-selected rail entry.
- **Three route kinds.** A rail item resolves to either (a) a **live session** (tmux pane embedded into the cockpit), (b) a **static view** (Textual widget tree), or (c) a **project route** (project-scoped panel like project dashboard). `resolve_live_session_route`, `resolve_static_view_route`, and `resolve_project_route` are the canonical resolvers.
- **Plugin rail.** `core_rail_items` is itself a plugin — the default rail entries are re-expressed as plugin registrations so third-party plugins can slot alongside them. Core reserves section indices 0–99; plugins start at 100. The `activity_feed` plugin, for instance, takes index 30 in the non-reserved `workflows` section.
- **FD limit.** `cockpit_ui.py` raises the process soft FD limit to 4096 at import time — the cockpit opens many subprocesses (tmux, provider probes, pane captures) and hits the default 256 limit quickly.
- **Hidden items / collapsed sections.** `[rail]` in `pollypm.toml` supports `hidden_items = ["section.label", ...]` and `collapsed_sections = [...]`. Users can still expand live during a session; the config only sets the boot default.
- **Alerts.** `cockpit_alerts` pops toasts for new state-store alerts. Toast dedupe matches `alert_type + session_name`.

## Related Docs

- [features/cockpit-rail.md](cockpit-rail.md) — deeper dive on the rail contract.
- [features/service-api.md](service-api.md) — what panels call into.
- [modules/plugin-host.md](../modules/plugin-host.md) — `RailAPI.register_item` mechanics.
- [features/activity-feed.md](activity-feed.md) — one of the built-in panels.
