**Last Verified:** 2026-04-22

## Summary

`human_notify` fans `TaskAssignmentEvent` events targeting an `ActorType.HUMAN` recipient out to a chain of adapters — macOS Notification Center, opt-in webhook, the cockpit notifier, and anything registered via the `pollypm.human_notifier` entry-point group. It hooks the same in-process bus `task_assignment_notify` uses, so human pushes arrive in the same tick as worker pushes.

No new job handlers, no new roster entries. A bus listener is registered at `initialize(api)` time; synchronous dispatch runs every configured adapter regardless of order, failing safely if one raises.

Touch this plugin when adding a new notification channel. Existing channels should stay self-contained — one module per adapter (`cockpit.py`, `macos.py`, `webhook.py`).

## Core Contracts

```python
# src/pollypm/plugins_builtin/human_notify/protocol.py
class HumanNotifyAdapter(Protocol):
    name: str
    def is_available(self) -> bool: ...
    def notify(self, event: TaskAssignmentEvent) -> None: ...
```

Adapters shipped:

- `MacOsNotifyAdapter` — always tried on Darwin; `osascript` `display notification`.
- `WebhookNotifyAdapter` — opt-in via `[human_notify].webhook_url`.
- `CockpitNotifyAdapter` — posts a cockpit toast via the running cockpit app.
- Entry-point adapters — anything registered under the `pollypm.human_notifier` group.

## File Structure

- `src/pollypm/plugins_builtin/human_notify/plugin.py` — wiring.
- `src/pollypm/plugins_builtin/human_notify/pollypm-plugin.toml` — manifest.
- `src/pollypm/plugins_builtin/human_notify/protocol.py` — `HumanNotifyAdapter` protocol.
- `src/pollypm/plugins_builtin/human_notify/dispatcher.py` — `dispatch(event, adapters)`.
- `src/pollypm/plugins_builtin/human_notify/macos.py` — macOS adapter.
- `src/pollypm/plugins_builtin/human_notify/webhook.py` — webhook adapter + `from_config` loader.
- `src/pollypm/plugins_builtin/human_notify/cockpit.py` — cockpit toast adapter.

## Implementation Details

- **Pre-initialize events dropped.** `_ADAPTERS` is `None` until `_initialize` runs. Events delivered before then (plugin-host test teardown) are silently dropped rather than crashing the bus.
- **Adapter ordering.** "Most specific first, fallback last" for log predictability. The dispatcher runs every adapter regardless, so order does not affect delivery.
- **Webhook availability.** Constructed even when no URL is set so `is_available()` can uniformly signal skip.
- **Event filter.** Only `ActorType.HUMAN` events trigger; worker / architect pushes pass through `task_assignment_notify` without going near `human_notify`.

## Related Docs

- [plugins/task-assignment-notify.md](task-assignment-notify.md) — shares the bus.
- [features/inbox-and-notify.md](../features/inbox-and-notify.md) — complementary surface (inbox rows in `messages`).
