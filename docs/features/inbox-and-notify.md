**Last Verified:** 2026-04-22

## Summary

The **inbox** is PollyPM's single channel for agent-to-human communication. It is *not* a storage subsystem — it's a query over two unified surfaces:

1. **The `messages` table** on the state DB. `pm notify` (the canonical escalation command) writes rows here via `Store.enqueue_message(...)`. Issue #341 migrated the inbox list reader onto this table.
2. **Work-service tasks** whose current node has `actor_type=human`, whose `roles` dict contains a `user` key, or whose roles assign any role the literal string `"user"`. These are surfaced as inbox items via `pollypm.work.inbox_view.inbox_tasks`.

Terminal tasks (`done`, `cancelled`) are excluded. Results sort by priority desc, then `updated_at` desc.

Touch this doc when changing the inbox query shape, the `pm notify` tiers, or the unified-messages schema. Do not store inbox state elsewhere — a second data store would split the surface.

## Core Contracts

```python
# pm notify command (src/pollypm/cli_features/session_runtime.py:306)
pm notify <subject> <body-or-"-">
    --actor polly                    # who is posting
    --project inbox                  # project namespace
    --priority auto                  # "immediate" | "digest" | "silent" | "auto"
    --milestone <key>                # optional milestone bucket for digest rollup
    --label <name>                   # repeatable; drives typed flows like plan_review
```

```python
# src/pollypm/store/protocol.py
class Store(Protocol):
    def enqueue_message(
        self, *, scope, type, recipient, sender, subject, body="",
        payload=None, labels=(), tier="immediate", parent_id=None,
    ) -> int: ...
    def query_messages(
        self, *, recipient=None, state=None, type=None, tier=None,
        scope=None, limit=None, offset=None,
    ) -> list[dict]: ...

# src/pollypm/work/inbox_view.py
def inbox_tasks(
    service: WorkService, *, project: str | None = None,
    flow_templates: dict[str, FlowTemplate] | None = None,
) -> list[Task]: ...
```

Display ids: `msg:<id>` prefix for unified-message rows (`src/pollypm/work/inbox_cli.py:_message_row_to_display`); `<project>/<number>` for work-service tasks. The prefixes prevent collision in merged listings.

## File Structure

- `src/pollypm/cli_features/session_runtime.py:305`+ — `pm notify` command.
- `src/pollypm/work/inbox_cli.py` — `pm inbox`, `pm inbox show`.
- `src/pollypm/work/inbox_view.py` — the work-task inbox query.
- `src/pollypm/store/protocol.py` / `sqlalchemy_store.py` — `enqueue_message`, `query_messages`, `append_event`.
- `src/pollypm/storage/state.py` — shared `messages` schema.
- `src/pollypm/cockpit_inbox.py` + `cockpit_inbox_items.py` — cockpit thread panel.
- `src/pollypm/notification_staging.py` — digest staging and rollup logic.
- `src/pollypm/approval_notifications.py` — task-approved notifications.

## Implementation Details

- **Three tiers.**
  - `immediate` — surfaces in the inbox now, also pushed to `human_notify` adapters (macOS / webhook / cockpit).
  - `digest` — staged silently; `morning_briefing` or milestone rollup flushes them.
  - `silent` — audit event only; never surfaces to the human.
  - `auto` — sniffs subject/body for keywords (`blocker`, `critical`, `urgent`, etc.) and falls back to `immediate` when ambiguous.
- **Canonical channel.** Agents MUST use `pm notify` for human escalation. Any other path (direct tmux `send_input`, out-of-band chat) is not guaranteed to be seen. The Polly persona (`core_agent_profiles/profiles.py:polly_prompt`) enforces this in the prompt.
- **Parent threading.** `enqueue_message(parent_id=...)` builds a thread. The cockpit thread viewer (`cockpit_inbox.InboxThreadRow`) shows parent + children grouped.
- **Activity feed.** `pm notify` writes to `messages`; the activity feed projector (`plugins_builtin/activity_feed`) surfaces them alongside task transitions and session events. The rail badge counts unread messages.
- **Bridging legacy tasks.** The cockpit flow still emits work-service tasks with `requester=user`. `pm inbox` UNIONs them in via `inbox_view.inbox_tasks` until all writers migrate to `enqueue_message`.

## Related Docs

- [features/cli.md](cli.md) — `pm notify` / `pm inbox`.
- [modules/state-store.md](../modules/state-store.md) — `messages` table layout.
- [modules/work-service.md](../modules/work-service.md) — work-service task surface bridged in.
- [plugins/human-notify.md](../plugins/human-notify.md) — fan-out to macOS / webhook / cockpit.
- [plugins/activity-feed.md](../plugins/activity-feed.md) — reverse-chronological projection.
