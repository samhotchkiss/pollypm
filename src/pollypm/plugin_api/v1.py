"""PollyPM Plugin API v1.

Stable registration surface for plugin authors. The module is mostly type
declarations, so keep a concrete example here to show the intended shape.

Example:
    from pollypm.plugin_api.v1 import Capability, PluginAPI, PollyPMPlugin

    def initialize(api: PluginAPI) -> None:
        for path in api.content_paths(kind="magic_skill"):
            api.emit_event("content_path_seen", {"path": str(path)})

    plugin = PollyPMPlugin(
        name="example_plugin",
        description="Minimal example plugin",
        capabilities=(
            Capability(kind="hook", name="example_plugin"),
        ),
        initialize=initialize,
    )
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Union

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
PluginInitializer = Callable[["PluginAPI"], None]


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
        "launch_planner",
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


# ---------------------------------------------------------------------------
# Rail registration API — see docs/extensible-rail-spec.md.
#
# The cockpit rail is organised into five frozen sections: ``top``,
# ``projects``, ``workflows``, ``tools``, ``system``. Plugins register
# items against a section at an explicit integer ``index`` (convention:
# core owns 0–99; plugins start at 100). Ties resolve by plugin name
# alphabetically. Items outside ``workflows`` / ``tools`` require the
# manifest flag ``contributes_to_reserved_section = true``.
# ---------------------------------------------------------------------------

RAIL_SECTIONS: tuple[str, ...] = ("top", "projects", "workflows", "tools", "system")
RESERVED_RAIL_SECTIONS: frozenset[str] = frozenset({"top", "projects", "system"})

VisibilityLiteral = Literal["always", "has_feature"]
VisibilityPredicate = Callable[["RailContext"], bool]
Visibility = Union[VisibilityLiteral, VisibilityPredicate]

BadgeProvider = Callable[["RailContext"], "int | str | None"]
RailHandler = Callable[["RailContext"], "PanelSpec | None"]
# Dynamic state string (icon colour hint, e.g. "working" / "idle" / "!alert").
# Cockpit-only extension — optional; defaults to "idle".
StateProvider = Callable[["RailContext"], str]
# Dynamic label — optional override so e.g. "Inbox (3)" can rebuild per tick
# without going through badge_provider. Returns None to fall back to the
# static label.
LabelProvider = Callable[["RailContext"], "str | None"]
# Dynamic multi-row expansion for sections that aren't 1:1 with the static
# registration (e.g. `projects` — one registration, N rows). Returns a list
# of (sub_key, label, state, selectable, indent_sub_rows) tuples; the rail
# builder uses these to emit CockpitItem rows.
RowsProvider = Callable[["RailContext"], "list[RailRow]"]


@dataclass(slots=True)
class RailContext:
    """Context object passed to rail item handlers and predicates.

    ``selected_project`` — the currently-selected project key, if any.
    ``user`` — opaque user identity token (today always the default
    actor; reserved for multi-user rails).
    ``cockpit_state`` — the live cockpit state dict (keys like
    ``selected``, ``mounted_session``, ``right_pane_id``). Handlers
    should treat it as read-only; mutations belong to the router.

    The field set is intentionally small and additive so future rails
    can hang extra hints off the same object without breaking plugins.
    """

    selected_project: str | None = None
    user: str | None = None
    cockpit_state: dict[str, Any] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class RailRow:
    """A concrete rail row produced by a ``rows_provider``.

    This is a **Cockpit-internal** extension beyond the plugin-API spec
    — the spec assumes one registration → one rail row. PollyPM's rail
    contains the Projects section which fans out one row per project
    (plus optional sub-items when expanded); those rows are generated
    per-tick by a ``rows_provider`` callable.

    Fields mirror :class:`pollypm.cockpit.CockpitItem` so the renderer
    can consume them with minimal translation:

    ``key`` — stable identifier used for selection (e.g. ``polly``,
    ``project:demo:issues``). Must be unique within a rail build.
    ``label`` — visible text. ``state`` — display-state hint consumed
    by the renderer's indicator logic (``"live"``/``"working"``/
    ``"idle"``/``"! reason"`` etc). ``selectable`` — whether the row
    can take the cursor.
    """

    key: str
    label: str
    state: str = "idle"
    selectable: bool = True


@dataclass(slots=True)
class PanelSpec:
    """Return value from a rail item handler.

    ``widget`` — opaque payload the cockpit panel renderer knows how to
    display (today a str or a Textual widget; typed ``Any`` so plugins
    can hand back provider-specific payloads without a shared import).
    ``focus_hint`` — optional hint the renderer may honour to decide
    which pane or row gets keyboard focus after the panel loads.
    """

    widget: Any = None
    focus_hint: str | None = None


@dataclass(slots=True, frozen=True)
class RailItemRegistration:
    """A single rail item registered by a plugin.

    Collected on the plugin host at ``api.rail.register_item(...)``
    time, then read by the cockpit rail builder. Frozen + slots so the
    registry can be cached cheaply.
    """

    plugin_name: str
    section: str
    index: int
    label: str
    handler: RailHandler
    icon: str | None = None
    badge_provider: BadgeProvider | None = None
    visibility: Visibility = "always"
    feature_name: str | None = None  # for visibility="has_feature"
    key: str | None = None  # optional stable id; falls back to section.label
    # Cockpit-internal extensions: dynamic label/state/multi-row. Optional —
    # simple items pass the static label + default state and never set these.
    state_provider: StateProvider | None = None
    label_provider: LabelProvider | None = None
    rows_provider: RowsProvider | None = None

    @property
    def item_key(self) -> str:
        """Human-facing match key — always ``section.label``.

        Used by the ``pm rail hide/show`` CLI and by ``[rail].hidden_items``
        config matching. Unlike :attr:`selection_key`, this is *always*
        ``section.label`` so the user can type it into the config without
        having to know a plugin's internal row IDs.
        """
        return f"{self.section}.{self.label}"

    @property
    def selection_key(self) -> str:
        """Stable internal id for cockpit row selection.

        Defaults to :attr:`item_key` when the plugin didn't pass an
        explicit ``key`` to ``register_item``. Plugins that emit
        existing cockpit keys (e.g. ``"polly"``, ``"settings"``) can
        override this to keep back-compat with routing logic.
        """
        return self.key if self.key else self.item_key


class RailAPI:
    """Plugin-facing façade for cockpit rail item registration.

    Plugins receive a ``RailAPI`` via ``api.rail`` inside their
    ``initialize(api)`` hook. A single plugin may register multiple
    items. Duplicate ``(section, index, plugin_name)`` triples are
    deduped with a warning — the last registration wins.

    Per spec §3 + er01 scope:

    * ``section`` must be one of :data:`RAIL_SECTIONS` — unknown names
      raise ``ValueError``.
    * Registering into a :data:`RESERVED_RAIL_SECTIONS` section requires
      the plugin manifest to set
      ``contributes_to_reserved_section = true``; otherwise the call
      logs a warning but does not raise (plugins may still register
      for local testing / dev flows).
    * ``index`` ties are resolved at render time by plugin name
      alphabetically (lower name first).
    """

    __slots__ = ("_plugin_name", "_registry", "_reserved_allowed")

    def __init__(
        self,
        *,
        plugin_name: str,
        registry: "RailRegistry",
        reserved_allowed: bool = False,
    ) -> None:
        self._plugin_name = plugin_name
        self._registry = registry
        self._reserved_allowed = reserved_allowed

    @property
    def plugin_name(self) -> str:
        return self._plugin_name

    def register_item(
        self,
        section: str,
        index: int,
        label: str,
        handler: RailHandler,
        *,
        icon: str | None = None,
        badge_provider: BadgeProvider | None = None,
        visibility: Visibility = "always",
        feature_name: str | None = None,
        key: str | None = None,
        state_provider: StateProvider | None = None,
        label_provider: LabelProvider | None = None,
        rows_provider: RowsProvider | None = None,
    ) -> RailItemRegistration:
        """Register a rail item.

        Returns the ``RailItemRegistration`` so callers can keep a
        reference (e.g. to test state).
        """
        if section not in RAIL_SECTIONS:
            raise ValueError(
                f"Unknown rail section '{section}' from plugin '{self._plugin_name}'. "
                f"Must be one of: {', '.join(RAIL_SECTIONS)}."
            )
        if not isinstance(index, int):
            raise TypeError(
                f"Rail item index must be int; plugin '{self._plugin_name}' passed "
                f"{type(index).__name__} for section '{section}' label '{label}'."
            )
        if not callable(handler):
            raise TypeError(
                f"Rail item handler must be callable; plugin '{self._plugin_name}' "
                f"passed {type(handler).__name__} for section '{section}' label '{label}'."
            )
        if visibility != "always" and visibility != "has_feature" and not callable(visibility):
            raise TypeError(
                f"Rail item visibility must be 'always', 'has_feature', or a callable; "
                f"plugin '{self._plugin_name}' passed {visibility!r} for section "
                f"'{section}' label '{label}'."
            )
        if section in RESERVED_RAIL_SECTIONS and not self._reserved_allowed:
            import logging
            logging.getLogger(__name__).warning(
                "Plugin '%s' registering into reserved rail section '%s' without "
                "contributes_to_reserved_section=true — item '%s' (index %d) will "
                "be accepted but the plugin host may block it in the future.",
                self._plugin_name, section, label, index,
            )
        reg = RailItemRegistration(
            plugin_name=self._plugin_name,
            section=section,
            index=index,
            label=label,
            handler=handler,
            icon=icon,
            badge_provider=badge_provider,
            visibility=visibility,
            feature_name=feature_name,
            key=key,
            state_provider=state_provider,
            label_provider=label_provider,
            rows_provider=rows_provider,
        )
        self._registry.add(reg)
        return reg


class RailRegistry:
    """Collects every ``RailAPI.register_item`` call across all plugins.

    Exposes a single ``items()`` accessor that returns registrations in
    rail-render order: sections walked in :data:`RAIL_SECTIONS` order,
    items within each section sorted by ``(index, plugin_name)``.

    The registry is held on the ``ExtensionHost`` so it has the same
    lifetime as the plugin host singleton — see
    :meth:`pollypm.plugin_host.ExtensionHost.rail_registry`.
    """

    __slots__ = ("_items",)

    def __init__(self) -> None:
        self._items: list[RailItemRegistration] = []

    def add(self, reg: RailItemRegistration) -> None:
        # Dedupe identical (plugin, section, label) re-registrations —
        # last write wins. This matches the provider/runtime override
        # pattern in _resolve_factory.
        for i, existing in enumerate(self._items):
            if (
                existing.plugin_name == reg.plugin_name
                and existing.section == reg.section
                and existing.label == reg.label
            ):
                self._items[i] = reg
                return
        self._items.append(reg)

    def items(self) -> list[RailItemRegistration]:
        """Return all registrations in rail-render order."""
        section_order = {name: i for i, name in enumerate(RAIL_SECTIONS)}
        return sorted(
            self._items,
            key=lambda r: (
                section_order.get(r.section, len(RAIL_SECTIONS)),
                r.index,
                r.plugin_name,
            ),
        )

    def items_for_section(self, section: str) -> list[RailItemRegistration]:
        """Return all registrations for ``section`` in render order."""
        return [r for r in self.items() if r.section == section]

    def clear(self) -> None:
        """Drop every registration — used by tests that swap registries."""
        self._items.clear()


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


class PluginAPI:
    """Read/write façade for PollyPMPlugin.initialize callbacks.

    Plugins receive a ``PluginAPI`` in their ``initialize(api)`` hook,
    invoked exactly once after all plugins have been loaded and
    validated but before the first heartbeat tick. This hook replaces
    ad-hoc import-time side effects and core-side plugin orchestration.

    Exposed surface — per docs/plugin-discovery-spec.md §6:

    * ``api.roster`` — ``RosterAPI`` for recurring-schedule registration.
    * ``api.jobs`` — ``JobHandlerAPI`` for job-handler registration.
    * ``api.content_paths(kind=...)`` — resolved content directories for
      this plugin in spec precedence order.
    * ``api.config`` — the loaded ``PollyPMConfig`` (may be ``None`` in
      tests / unconfigured installs; callers should handle gracefully).
    * ``api.state_store`` — ``StateStore`` (lazily opened on first read).
    * ``api.emit_event(name, payload)`` — record an event into the state
      store's ``events`` table. Safe no-op if no store is available.
    """

    __slots__ = (
        "_plugin_name",
        "_roster_api",
        "_jobs_api",
        "_rail_api",
        "_host",
        "_config",
        "_state_store",
    )

    def __init__(
        self,
        *,
        plugin_name: str,
        roster_api: "RosterAPI | None",
        jobs_api: "JobHandlerAPI | None",
        host: Any = None,
        config: Any = None,
        state_store: Any = None,
        rail_api: "RailAPI | None" = None,
    ) -> None:
        self._plugin_name = plugin_name
        self._roster_api = roster_api
        self._jobs_api = jobs_api
        self._rail_api = rail_api
        self._host = host
        self._config = config
        self._state_store = state_store

    @property
    def plugin_name(self) -> str:
        return self._plugin_name

    @property
    def roster(self) -> "RosterAPI":
        if self._roster_api is None:
            raise RuntimeError(
                "RosterAPI not available — plugin host was built without a roster. "
                "This typically means the heartbeat rail is not initialised in this context."
            )
        return self._roster_api

    @property
    def jobs(self) -> "JobHandlerAPI":
        if self._jobs_api is None:
            raise RuntimeError(
                "JobHandlerAPI not available — plugin host was built without a job registry."
            )
        return self._jobs_api

    @property
    def rail(self) -> "RailAPI":
        """Rail-item registration façade. See docs/extensible-rail-spec.md."""
        if self._rail_api is None:
            raise RuntimeError(
                "RailAPI not available — plugin host was built without a rail registry."
            )
        return self._rail_api

    @property
    def config(self) -> Any:
        return self._config

    @property
    def state_store(self) -> Any:
        return self._state_store

    def content_paths(self, kind: str | None = None) -> list[Path]:
        """Return the content search paths for this plugin, scoped to
        ``kind`` if the plugin's manifest declares ``[content].kinds``.
        """
        if self._host is None:
            return []
        return list(self._host.content_paths(self._plugin_name, kind=kind))

    def emit_event(self, name: str, payload: dict[str, Any] | None = None) -> None:
        """Record a lightweight event tagged with this plugin name.

        No-ops silently if no state store / events table is available —
        initialize hooks should be idempotent to rail-less test
        environments.
        """
        store = self._state_store
        if store is None:
            return
        try:
            record = getattr(store, "record_event", None)
            if callable(record):
                record(
                    kind=f"plugin.{self._plugin_name}.{name}",
                    payload=payload or {},
                )
        except Exception:  # noqa: BLE001
            # Events are best-effort observability — swallow.
            pass


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
    initialize: PluginInitializer | None = None

    def __post_init__(self) -> None:
        # Coerce legacy bare-string capabilities into structured form so
        # older plugins (and in-tree builtins mid-migration) keep working.
        if self.capabilities and any(not isinstance(c, Capability) for c in self.capabilities):
            self.capabilities = normalize_capabilities(
                self.capabilities, plugin_name=self.name,
            )
