"""Job handler registry.

Plugins register named callables via ``pollypm.plugin_api.v1.JobHandlerAPI``;
the worker pool resolves them at claim time through this registry.

Collisions
----------
Two plugins registering the same handler name is a soft event: we log a
warning and let the most-recent registration win. This mirrors the pattern
in ``ExtensionHost._resolve_factory`` (see commit e56ac22) so plugin authors
see the same override semantics everywhere.

Unknown handlers
----------------
Jobs whose ``handler_name`` is not in the registry should fail permanently
with a clear ``last_error`` — the worker pool enforces this. The registry
itself just returns ``None`` from ``get(name)``.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from pollypm.jobs.workers import HandlerSpec


__all__ = ["JobHandlerRegistry"]


logger = logging.getLogger(__name__)


JobHandlerCallable = Callable[[dict[str, Any]], Any]


class JobHandlerRegistry:
    """Thread-safe registry mapping handler name → ``HandlerSpec``.

    One instance per plugin host (``ExtensionHost``). Plugin registration
    happens via ``register_handlers`` hooks; the worker pool reads entries
    through ``get(name)``.
    """

    __slots__ = ("_lock", "_entries", "_sources")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[str, HandlerSpec] = {}
        self._sources: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Plugin-facing registration
    # ------------------------------------------------------------------

    def register(
        self,
        *,
        name: str,
        handler: JobHandlerCallable,
        plugin_name: str = "",
        max_attempts: int = 3,
        timeout_seconds: float = 30.0,
        retry_backoff: str = "exponential",
    ) -> bool:
        """Register a handler. Returns ``True`` when new, ``False`` when
        overriding an existing registration.

        The most recent registration wins (same semantics as provider /
        session-service collisions). Overrides are logged.
        """
        if not name:
            raise ValueError("handler name is required")
        if not callable(handler):
            raise TypeError(f"handler for '{name}' is not callable")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        spec = HandlerSpec(
            name=name,
            handler=handler,
            max_attempts=max_attempts,
            timeout_seconds=timeout_seconds,
            retry_backoff=retry_backoff,
        )
        with self._lock:
            existing_source = self._sources.get(name)
            is_new = name not in self._entries
            if not is_new and existing_source != plugin_name:
                logger.warning(
                    "Job handler '%s' from plugin '%s' overrides registration "
                    "from plugin '%s' — most recent wins",
                    name, plugin_name or "<anonymous>", existing_source or "<anonymous>",
                )
            self._entries[name] = spec
            self._sources[name] = plugin_name
        return is_new

    # ------------------------------------------------------------------
    # Worker-facing lookup
    # ------------------------------------------------------------------

    def get(self, name: str) -> HandlerSpec | None:
        with self._lock:
            return self._entries.get(name)

    def unregister(self, name: str) -> None:
        """Remove a handler. Used by tests; not a plugin-facing operation."""
        with self._lock:
            self._entries.pop(name, None)
            self._sources.pop(name, None)

    def names(self) -> list[str]:
        with self._lock:
            return list(self._entries.keys())

    def source_of(self, name: str) -> str | None:
        with self._lock:
            return self._sources.get(name)

    def snapshot(self) -> dict[str, HandlerSpec]:
        with self._lock:
            return dict(self._entries)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __contains__(self, name: object) -> bool:
        with self._lock:
            return name in self._entries
