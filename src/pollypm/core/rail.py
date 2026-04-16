"""CoreRail — the rail that owns config, state store, and plugin host.

The rail is additive scaffolding at this step: nothing has moved out of
:class:`pollypm.supervisor.Supervisor` yet. Callers that construct a
Supervisor without supplying a rail get the same behavior they always
have — the Supervisor builds its own rail internally.

Later decomposition steps (see issues #179 and beyond) promote the rail
from "accessor bag" to the place that actually drives startup and
shutdown.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pollypm.config import PollyPMConfig
    from pollypm.plugin_host import ExtensionHost
    from pollypm.storage.state import StateStore

logger = logging.getLogger(__name__)


@runtime_checkable
class Startable(Protocol):
    """Lifecycle protocol for subsystems driven by :class:`CoreRail`.

    A subsystem exposes ``start()`` and ``stop()`` callables. CoreRail
    invokes them in registration order on start, reverse order on stop.
    This protocol is intentionally tiny — subsystems keep their own
    implementation details private.
    """

    def start(self) -> None:  # pragma: no cover - protocol
        ...

    def stop(self) -> None:  # pragma: no cover - protocol
        ...


class CoreRail:
    """Long-lived process rail holding config, state, and plugin host.

    The rail is constructed once per process and passed to subsystems
    that need its accessors. It is safe to construct a rail even when
    no subsystems are registered — ``start()``/``stop()`` are no-ops
    on an empty subsystem list.
    """

    def __init__(
        self,
        config: "PollyPMConfig",
        state_store: "StateStore",
        plugin_host: "ExtensionHost",
    ) -> None:
        self._config = config
        self._state_store = state_store
        self._plugin_host = plugin_host
        self._subsystems: list[Startable] = []
        self._started = False

    # ── Accessors ──────────────────────────────────────────────────────────

    def get_config(self) -> "PollyPMConfig":
        return self._config

    def get_state_store(self) -> "StateStore":
        return self._state_store

    def get_plugin_host(self) -> "ExtensionHost":
        return self._plugin_host

    # ── Event bus (placeholder) ────────────────────────────────────────────

    def emit_event(self, name: str, payload: dict) -> None:
        """Emit a rail event.

        Current implementation is a debug log — future steps will route
        events to registered subscribers. The signature is fixed now so
        subsystems can start calling it without churn later.
        """
        logger.debug("CoreRail event: %s payload=%r", name, payload)

    # ── Subsystem registration ─────────────────────────────────────────────

    def register_subsystem(self, subsystem: Startable) -> None:
        """Register a subsystem whose lifecycle the rail will manage.

        Subsystems are started in registration order and stopped in
        reverse. Re-registering the same subsystem is a silent no-op —
        the rail keeps the first registration to preserve ordering.
        """
        if subsystem in self._subsystems:
            logger.debug("CoreRail subsystem already registered: %r", subsystem)
            return
        self._subsystems.append(subsystem)

    def subsystems(self) -> list[Startable]:
        """Return a copy of the registered subsystem list (start order)."""
        return list(self._subsystems)

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start all registered subsystems in registration order.

        At this step nothing is registered in the default path; this is
        effectively a no-op for today's callers. When Step 2 lands
        Supervisor will register itself and CoreRail will own the real
        boot sequence.
        """
        if self._started:
            logger.debug("CoreRail.start() called while already started — skipping")
            return
        logger.debug("CoreRail.start(): %d subsystem(s)", len(self._subsystems))
        for subsystem in self._subsystems:
            subsystem.start()
        self._started = True

    def stop(self) -> None:
        """Stop all registered subsystems in reverse registration order."""
        if not self._started:
            logger.debug("CoreRail.stop() called while not started — skipping")
            return
        logger.debug("CoreRail.stop(): %d subsystem(s)", len(self._subsystems))
        for subsystem in reversed(self._subsystems):
            try:
                subsystem.stop()
            except Exception:  # noqa: BLE001
                logger.exception("CoreRail subsystem stop raised; continuing shutdown")
        self._started = False
