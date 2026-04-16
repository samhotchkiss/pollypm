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
TranscriptSourceFactory = Callable[..., object]
RecoveryPolicyFactory = Callable[..., object]
LaunchPlannerFactory = Callable[..., object]
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


# Capability kinds recognised by the rail plugin API. See
# docs/plugin-discovery-spec.md §4. Unknown kinds don't fail load —
# they're preserved as-is so forward-compatible plugins keep working
# on older rails with a logged warning.
KNOWN_CAPABILITY_KINDS: frozenset[str] = frozenset(
    {
        "provider",
        "runtime",
        "session_service",
        "heartbeat",
        "scheduler",
        "agent_profile",
        "task_backend",
        "memory_backend",
        "doc_backend",
        "sync_adapter",
        "transcript_source",
        "recovery_policy",
        "job_handler",
        "roster_entry",
        # Plugin-level hook capability (observers / filters) — not tied to
        # a specific factory kind. Accepted so plugins declaring hook-only
        # surfaces pass validation.
        "hook",
        # Legacy: pre-structured manifests used these bare-string values.
        "roster",
    }
)


def _parse_api_major(value: str) -> int:
    """Parse a rail API version literal (e.g. ``"1"``, ``"2.3"``) to its
    major integer. Non-numeric or empty values raise ``ValueError``.
    """
    if not value:
        raise ValueError("empty API version")
    token = value.strip()
    # Accept bare integers ("1") and dotted forms ("1.0").
    head = token.split(".", 1)[0]
    return int(head)


def check_requires_api(expression: str | None, current_api_version: str) -> bool:
    """Return ``True`` if ``current_api_version`` satisfies ``expression``.

    ``expression`` is a comma-separated list of simple constraints:

        >=1   <2   ==1   !=2   >0   <=1

    or a bare version ("1") which is treated as ``==1``. Missing /
    ``None`` expressions always satisfy (returns ``True``).

    Only the major-version integer is compared — this matches the rail's
    compatibility model where the current API is a major number.
    """
    if expression is None:
        return True
    expr = expression.strip()
    if not expr:
        return True
    current_major = _parse_api_major(current_api_version)

    for raw_part in expr.split(","):
        part = raw_part.strip()
        if not part:
            continue
        op = "=="
        rest = part
        for candidate in (">=", "<=", "==", "!=", ">", "<"):
            if part.startswith(candidate):
                op = candidate
                rest = part[len(candidate):].strip()
                break
        else:
            # Bare "1" → "==1".
            op = "=="
            rest = part
        try:
            target_major = _parse_api_major(rest)
        except ValueError:
            raise ValueError(
                f"Unparseable API version constraint '{part}' in '{expression}'"
            ) from None
        if op == ">=" and not (current_major >= target_major):
            return False
        if op == "<=" and not (current_major <= target_major):
            return False
        if op == "==" and not (current_major == target_major):
            return False
        if op == "!=" and not (current_major != target_major):
            return False
        if op == ">" and not (current_major > target_major):
            return False
        if op == "<" and not (current_major < target_major):
            return False
    return True


@dataclass(slots=True, frozen=True)
class Capability:
    """A structured plugin capability declaration.

    Each plugin advertises one ``Capability`` per surface it provides.
    See docs/plugin-discovery-spec.md §4.

    * ``kind`` — capability kind (see ``KNOWN_CAPABILITY_KINDS``).
    * ``name`` — unique-within-(kind) identifier (e.g. ``"claude"`` for
      a provider). Defaults to the plugin name when omitted.
    * ``replaces`` — tuple of capability ``name`` values within the same
      ``kind`` that this capability explicitly supersedes. Explicit
      replacement wins over implicit last-write discovery order.
    * ``requires_api`` — optional rail API version constraint (e.g.
      ``">=1,<2"``). If the current rail API is outside the range the
      capability is skipped. Missing means "any".
    """

    kind: str
    name: str = ""
    replaces: tuple[str, ...] = ()
    requires_api: str | None = None


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


def normalize_capabilities(
    raw: "tuple[Capability | str, ...] | list[Capability | str]",
    *,
    plugin_name: str = "",
) -> tuple[Capability, ...]:
    """Coerce a tuple of ``Capability`` / bare-string entries into a
    tuple of ``Capability``.

    Bare-string form (legacy) is accepted for one release with a
    deprecation warning. Each bare string is mapped to
    ``Capability(kind=<string>, name=plugin_name or <string>)``.
    """
    import logging

    logger = logging.getLogger(__name__)
    out: list[Capability] = []
    for entry in raw:
        if isinstance(entry, Capability):
            out.append(entry)
            continue
        if isinstance(entry, str):
            if plugin_name:
                logger.warning(
                    "Plugin '%s' uses legacy bare-string capability '%s'; "
                    "migrate to structured [[capabilities]] blocks "
                    "(kind=, name=) — deprecation target: next release.",
                    plugin_name, entry,
                )
            out.append(Capability(kind=entry, name=plugin_name or entry))
            continue
        raise TypeError(
            f"Capability entry must be Capability or str, got {type(entry).__name__}: {entry!r}"
        )
    return tuple(out)


@dataclass(slots=True)
class PollyPMPlugin:
    name: str
    api_version: str = "1"
    version: str = "0.1.0"
    description: str = ""
    capabilities: tuple[Capability, ...] = ()
    providers: dict[str, ProviderFactory] = field(default_factory=dict)
    runtimes: dict[str, RuntimeFactory] = field(default_factory=dict)
    heartbeat_backends: dict[str, HeartbeatBackendFactory] = field(default_factory=dict)
    scheduler_backends: dict[str, SchedulerBackendFactory] = field(default_factory=dict)
    agent_profiles: dict[str, AgentProfileFactory] = field(default_factory=dict)
    session_services: dict[str, SessionServiceFactory] = field(default_factory=dict)
    transcript_sources: dict[str, TranscriptSourceFactory] = field(default_factory=dict)
    recovery_policies: dict[str, RecoveryPolicyFactory] = field(default_factory=dict)
    launch_planners: dict[str, LaunchPlannerFactory] = field(default_factory=dict)
    observers: dict[str, list[ObserverHandler]] = field(default_factory=dict)
    filters: dict[str, list[FilterHandler]] = field(default_factory=dict)
    register_roster: RosterRegistrar | None = None
    register_handlers: JobHandlerRegistrar | None = None

    def __post_init__(self) -> None:
        # Coerce legacy bare-string capabilities into structured form so
        # older plugins (and in-tree builtins mid-migration) keep working.
        if self.capabilities and any(not isinstance(c, Capability) for c in self.capabilities):
            self.capabilities = normalize_capabilities(
                self.capabilities, plugin_name=self.name,
            )
