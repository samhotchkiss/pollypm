"""``human_notify`` — push ``ActorType.HUMAN`` tasks to human-facing channels.

The cockpit's toast overlay is the only human-visible signal today,
and it only fires while the cockpit is open + focused. When the user
is away from the terminal (most of the time, by design), tasks
entering ``user_approval`` / any ``actor_type: human`` node sit in
their inbox unread until the next cockpit check. That's the real
gap Polly's ``ScheduleWakeup`` pattern was hedging — not reading
messages she shouldn't, just observing state from a distance.

This plugin subscribes to the same ``TaskAssignmentEvent`` bus that
``task_assignment_notify`` uses for worker/architect pushes, filters
for ``ActorType.HUMAN``, and fans out to registered
``HumanNotifyAdapter`` implementations. Three built-ins ship with
PollyPM:

- :class:`MacOsNotifyAdapter` — native Notification Center toast
  via ``osascript``. Always available on Darwin; default-on.
- :class:`WebhookNotifyAdapter` — HTTP POST to a user-configured
  URL (ntfy.sh, Pushbullet, Slack webhook, etc.). Opt-in via
  ``[human_notify.webhook]`` in ``pollypm.toml``.
- :class:`CockpitNotifyAdapter` — always-last fallback that writes
  an alert the cockpit toast already renders; preserves current
  behavior for users on non-macOS without a webhook.

Third parties plug in new channels (email, SMS, push-to-watch)
via the ``pollypm.human_notifier`` entry-point group — no code
changes in this package.
"""

from .protocol import HumanNotifyAdapter

__all__ = ["HumanNotifyAdapter"]
