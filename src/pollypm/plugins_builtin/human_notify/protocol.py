"""``HumanNotifyAdapter`` Protocol.

Every adapter that wants to deliver human-addressed task pings to
an external channel implements this contract. The shape is
deliberately narrow:

- :meth:`is_available` — called once at adapter-load time to gate
  platform-specific adapters (e.g. the macOS adapter returns
  ``False`` on Linux) and config-guarded adapters (e.g. webhook
  returns ``False`` when no URL is set).
- :meth:`notify` — deliver a single event. Exceptions are caught
  by the dispatcher so one adapter's outage can't block the rest.

Adapters are stateless across calls — any per-instance config
lives on the adapter object itself, populated at construction
from the resolved :class:`pollypm.models.PollyPMConfig`.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class HumanNotifyAdapter(Protocol):
    """Contract every human-facing notification channel implements.

    Attributes:
        name: Stable identifier — used by the dispatcher for logging
            and dedupe, and by the entry-point registry to ensure
            third-party adapters don't collide with builtins.
    """

    name: str

    def is_available(self) -> bool:
        """Return True iff this adapter can deliver events right now.

        Called once per dispatcher construction, so the result can be
        a one-shot capability probe (e.g. ``shutil.which("osascript")``
        for the macOS adapter, or a config-presence check for the
        webhook adapter).
        """
        ...

    def notify(
        self,
        *,
        title: str,
        body: str,
        task_id: str,
        project: str,
    ) -> None:
        """Deliver a single ``ActorType.HUMAN`` event.

        ``title`` is a short single-line subject suitable for a
        notification header; ``body`` is up to ~2 lines of actionable
        text (task title + "``pm inbox show <id>``" pointer by
        default). ``task_id`` and ``project`` are provided separately
        so adapters that support structured payloads (webhook JSON)
        can serialize them cleanly without re-parsing ``body``.

        Implementations must not raise on routine failure modes —
        network timeouts, missing binaries, etc. — because one
        adapter's outage should never block another. When delivery
        fails, log at WARNING and return; the dispatcher continues
        to the next adapter.
        """
        ...


__all__ = ["HumanNotifyAdapter"]
