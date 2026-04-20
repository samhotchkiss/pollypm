"""Always-on fallback adapter that writes a cockpit-visible alert.

Preserves the pre-#410 behavior for users on non-macOS hosts without
a configured webhook: the task still shows up in the cockpit's
Inbox panel via a ``human_task_waiting`` alert. Less intrusive than
a banner, but never silently dropped.

The actual toast rendering is handled by ``cockpit_ui.AlertToast``
— this adapter just upserts the alert; the cockpit polls the
alerts table and renders.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pollypm.storage.state import StateStore

logger = logging.getLogger(__name__)


class CockpitNotifyAdapter:
    """``HumanNotifyAdapter`` — upsert an alert the cockpit toast renders.

    Constructed with a :class:`StateStore` (or a unified
    :mod:`pollypm.store` handle — the adapter accepts either, since
    both expose ``upsert_alert``). The store reference is threaded
    in at plugin-initialize time rather than re-resolved per-event so
    we don't pay the config-load cost on every push.
    """

    name = "cockpit"

    def __init__(self, store: object | None) -> None:
        self._store = store

    def is_available(self) -> bool:
        """True iff we have a store to write alerts to.

        ``None`` is valid construction (e.g. in tests) and maps to
        "skip silently" rather than erroring.
        """
        return self._store is not None

    def notify(
        self,
        *,
        title: str,
        body: str,
        task_id: str,
        project: str,
    ) -> None:
        """Upsert a ``human_task_waiting`` alert keyed by ``task_id``.

        Using the task id as the alert key means re-pings for the
        same task replace the existing alert instead of stacking —
        matches the dedupe behavior of :mod:`task_assignment_notify`.
        """
        if self._store is None:
            return
        try:
            self._store.upsert_alert(
                f"task:{task_id}",
                "human_task_waiting",
                "warn",
                f"{title}\n{body}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "human_notify[cockpit]: upsert_alert failed for %s: %s",
                task_id, exc,
            )


__all__ = ["CockpitNotifyAdapter"]
