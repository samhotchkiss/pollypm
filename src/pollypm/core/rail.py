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
        """Drive the rail boot sequence.

        Order is fixed and documented:
          1. plugin host readiness (eager load — surfaces errors early)
          2. state store readiness (already opened + migrated in its
             constructor, but logged here so boot has a single narrative)
          3. subsystem boot in registration order (Supervisor first,
             heartbeat worker subsystems next as they get registered)

        Idempotent: a second call while already started is a no-op.
        """
        if self._started:
            logger.debug("CoreRail.start() called while already started — skipping")
            return
        logger.info("CoreRail.start(): loading plugin host")
        # Touch the plugin registry so any load errors surface here with
        # a clear log line instead of at some deep accessor call later.
        self._plugin_host.plugins()
        logger.info("CoreRail.start(): state store ready at %s", self._state_store.path)
        logger.info(
            "CoreRail.start(): booting %d subsystem(s)", len(self._subsystems),
        )
        for subsystem in self._subsystems:
            subsystem.start()
        self._started = True
        logger.info("CoreRail.start(): boot complete")

    def stop(self) -> None:
        """Drive graceful shutdown in reverse order.

        Subsystems stop first (reverse registration order), then the
        state store is closed. Plugin host has no teardown today.
        Idempotent: safe to call before start() or twice.
        """
        if not self._started:
            logger.debug("CoreRail.stop() called while not started — skipping")
            return
        logger.info(
            "CoreRail.stop(): stopping %d subsystem(s)", len(self._subsystems),
        )
        for subsystem in reversed(self._subsystems):
            try:
                subsystem.stop()
            except Exception:  # noqa: BLE001
                logger.exception("CoreRail subsystem stop raised; continuing shutdown")
        # State store close is idempotent — Supervisor.stop may have
        # already closed it, and that's fine. Double-close is swallowed.
        try:
            self._state_store.close()
        except Exception:  # noqa: BLE001
            logger.debug("CoreRail.stop(): state_store.close raised", exc_info=True)
        self._started = False
        logger.info("CoreRail.stop(): shutdown complete")
