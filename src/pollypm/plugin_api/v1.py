from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ProviderFactory = Callable[[], object]
RuntimeFactory = Callable[[], object]
HeartbeatBackendFactory = Callable[[], object]
SchedulerBackendFactory = Callable[[], object]
AgentProfileFactory = Callable[[], object]
SessionServiceFactory = Callable[..., object]
ObserverHandler = Callable[["HookContext"], None]
FilterHandler = Callable[["HookContext"], "HookFilterResult | None"]
RosterRegistrar = Callable[["RosterAPI"], None]
JobHandlerRegistrar = Callable[["JobHandlerAPI"], None]
JobHandlerCallable = Callable[[dict[str, Any]], Any]


@dataclass(slots=True)
class HookContext:
    hook_name: str
    payload: Any
    root_dir: Path
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class HookFilterResult:
    action: str = "allow"
    payload: Any = None
    reason: str | None = None


class RosterAPI:
    """Stable façade plugins use to register recurring schedules.

    Plugins receive a ``RosterAPI`` in their ``register_roster(api)`` hook
    during plugin host bootstrap (after session-service registration). The
    API forwards calls to the underlying ``pollypm.heartbeat.Roster`` and
    the plugin host — plugins should treat it as opaque.

    Supported schedule expressions:

    * ``@on_startup`` — fires exactly once on the first tick.
    * ``@every <duration>`` — ``s``/``m``/``h``/``d`` suffixes.
    * 5-field cron (``minute hour dom month dow``) with ``*``, ``*/N``,
      ``A-B`` ranges, and comma lists.
    * Named aliases: ``@hourly``, ``@daily``, ``@weekly``, ``@monthly``,
      ``@yearly``.

    Collisions (same handler + payload) are detected, logged by the
    plugin host, and deduped — the original registration wins.
    """

    __slots__ = ("_roster", "_plugin_name", "_collision_callback")

    def __init__(
        self,
        roster: Any,
        *,
        plugin_name: str,
        on_collision: Callable[[str, str, str], None] | None = None,
    ) -> None:
        self._roster = roster
        self._plugin_name = plugin_name
        self._collision_callback = on_collision

    @property
    def plugin_name(self) -> str:
        return self._plugin_name

    def register_recurring(
        self,
        schedule: str,
        handler_name: str,
        payload: dict[str, Any] | None = None,
        *,
        dedupe_key: str | None = None,
    ) -> bool:
        """Register a recurring schedule. Returns ``True`` when new.

        Raises ``ValueError`` if the schedule expression is unparseable.
        """
        # Local import to keep plugin_api importable without heartbeat deps.
        from pollypm.heartbeat.roster import parse_schedule

        sched = parse_schedule(schedule)
        _, is_new = self._roster.register(
            schedule=sched,
            handler_name=handler_name,
            payload=payload or {},
            dedupe_key=dedupe_key,
        )
        if not is_new and self._collision_callback is not None:
            self._collision_callback(self._plugin_name, handler_name, schedule)
        return is_new

    def snapshot(self) -> list[Any]:
        """Return the current roster entries (for introspection/testing)."""
        return list(self._roster.snapshot())


class JobHandlerAPI:
    """Stable façade plugins use to register job handlers.

    Plugins receive a ``JobHandlerAPI`` in their ``register_handlers(api)``
    hook during plugin host bootstrap. The API forwards calls to the
    underlying ``JobHandlerRegistry`` singleton.

    Collisions (same handler name across plugins) log a warning and let
    the most recent registration win — matching the pattern established
    by ``ExtensionHost._resolve_factory`` for providers/runtimes (see
    commit e56ac22).
    """

    __slots__ = ("_registry", "_plugin_name")

    def __init__(self, registry: Any, *, plugin_name: str) -> None:
        self._registry = registry
        self._plugin_name = plugin_name

    @property
    def plugin_name(self) -> str:
        return self._plugin_name

    def register_handler(
        self,
        name: str,
        handler: JobHandlerCallable,
        *,
        max_attempts: int = 3,
        timeout_seconds: float = 30.0,
        retry_backoff: str = "exponential",
    ) -> bool:
        """Register a job handler. Returns ``True`` when new, ``False`` when
        overriding an existing registration (logged by the registry).
        """
        return self._registry.register(
            name=name,
            handler=handler,
            plugin_name=self._plugin_name,
            max_attempts=max_attempts,
            timeout_seconds=timeout_seconds,
            retry_backoff=retry_backoff,
        )


@dataclass(slots=True)
class PollyPMPlugin:
    name: str
    api_version: str = "1"
    version: str = "0.1.0"
    description: str = ""
    capabilities: tuple[str, ...] = ()
    providers: dict[str, ProviderFactory] = field(default_factory=dict)
    runtimes: dict[str, RuntimeFactory] = field(default_factory=dict)
    heartbeat_backends: dict[str, HeartbeatBackendFactory] = field(default_factory=dict)
    scheduler_backends: dict[str, SchedulerBackendFactory] = field(default_factory=dict)
    agent_profiles: dict[str, AgentProfileFactory] = field(default_factory=dict)
    session_services: dict[str, SessionServiceFactory] = field(default_factory=dict)
    observers: dict[str, list[ObserverHandler]] = field(default_factory=dict)
    filters: dict[str, list[FilterHandler]] = field(default_factory=dict)
    register_roster: RosterRegistrar | None = None
    register_handlers: JobHandlerRegistrar | None = None
