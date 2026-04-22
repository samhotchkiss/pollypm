**Last Verified:** 2026-04-22

## Summary

The Live Activity Feed is a unified reverse-chronological projection of events across the global `messages` table and per-project work-service transitions. It powers the "Activity" rail entry in the cockpit, the `pm activity` CLI, and the rail badge that counts unread events.

Built as the `activity_feed` plugin. It registers one rail item in the `workflows` section (index 30, label `Activity`, icon `●`) with a badge provider that counts messages newer than the last-acknowledged cursor. The cursor is persisted next to the state DB as `activity_feed_last_seen`.

Touch this doc when changing the projection shape, adding a new event source, or modifying rail registration. Do not reach into the state DB directly — route through `Store.query_messages` + the per-project work-DB bridge.

## Core Contracts

```python
# src/pollypm/plugins_builtin/activity_feed/handlers/event_projector.py
@dataclass(slots=True)
class FeedEntry:
    id: str          # "msg:<id>" or "evt:<id>" or "task:<project>/<num>@<ts>"
    ts: datetime
    source: str      # "message" | "event" | "work_transition"
    kind: str
    summary: str
    severity: str
    subject: str | None

class EventProjector:
    def __init__(
        self, state_db: Path, work_db_paths: list[tuple[str, Path]],
    ) -> None: ...
    def project(
        self, *, limit: int = 100, since_id: str | None = None,
        types: Iterable[str] | None = None,
    ) -> list[FeedEntry]: ...

# src/pollypm/plugins_builtin/activity_feed/cockpit/feed_panel.py
def new_event_count(projector: EventProjector, last_seen_id: int | None) -> int: ...
```

`pm activity` CLI:

```
pm activity                 # latest 50 entries
pm activity --follow        # tail-follow mode
pm activity --since <ts>    # filter by timestamp
pm activity --kind <kind>   # filter by kind
pm activity --json          # machine-readable
```

## File Structure

- `src/pollypm/plugins_builtin/activity_feed/plugin.py` — plugin wiring + rail registration.
- `src/pollypm/plugins_builtin/activity_feed/pollypm-plugin.toml` — manifest.
- `src/pollypm/plugins_builtin/activity_feed/handlers/event_projector.py` — projector.
- `src/pollypm/plugins_builtin/activity_feed/cli.py` — `pm activity`.
- `src/pollypm/plugins_builtin/activity_feed/cockpit/feed_panel.py` — Textual widget + plain renderer.
- `src/pollypm/plugins_builtin/activity_feed/summaries.py` — `activity_summary()` helper used by other subsystems (e.g. plugin lifecycle events).

## Implementation Details

- **Projector inputs.** (1) the global state-DB `messages` table, (2) each registered project's per-project work DB (`<project>/.pollypm/state.db`) `work_transitions` table. Missing DBs are tolerated — the projector checks path existence before querying.
- **Id prefixes.** `msg:<id>` for unified messages, `evt:<id>` for legacy events, `task:<project>/<num>@<ts>` for work transitions. The cursor handling (`plugin.py:_save_last_seen_id`) accepts both `msg:` and `evt:` prefixes for back-compat with old cursors.
- **Cursor file.** `<state_db>.parent / "activity_feed_last_seen"` — a one-line integer. When the user selects the rail entry the plugin reads the newest id, strips the prefix, and overwrites the cursor. The badge resets to zero on the next render.
- **Rail registration.** `section="workflows"` (non-reserved), `index=30`. Reserved sections (`core.*`) are not used, so the plugin manifest does not declare a reserved-section flag.
- **`activity_summary` helper.** Used by non-activity-feed subsystems (e.g. `plugin_host._emit_plugin_lifecycle_event`) to write message rows in the shape the feed expects. Keeping the helper near the plugin means writers don't need to know the internal layout.
- **Panel states.** `_state_provider` returns `"watch"` so the rail renderer picks the appropriate palette.

## Related Docs

- [features/cockpit-rail.md](cockpit-rail.md) — rail contract.
- [features/inbox-and-notify.md](inbox-and-notify.md) — shares the `messages` table.
- [modules/state-store.md](../modules/state-store.md) — unified `Store` backend.
- [plugins/activity-feed.md](../plugins/activity-feed.md) — plugin wiring.
