from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import tomllib
import types

from pollypm.plugin_api.v1 import (
    Capability,
    HookContext,
    HookFilterResult,
    JobHandlerAPI,
    KNOWN_CAPABILITY_KINDS,
    PollyPMPlugin,
    RosterAPI,
    check_requires_api,
    normalize_capabilities,
)

logger = logging.getLogger(__name__)

PLUGIN_MANIFEST = "pollypm-plugin.toml"
PLUGIN_API_VERSION = "1"


@dataclass(slots=True)
class PluginManifest:
    name: str
    api_version: str
    version: str
    kind: str
    entrypoint: str
    capabilities: tuple[Capability, ...]
    description: str
    plugin_dir: Path
    source: str
    requires_api: str | None = None


class ExtensionHost:
    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir.resolve()
        self.errors: list[str] = []
        self._plugins: dict[str, PollyPMPlugin] | None = None
        self._plugin_sources: dict[str, str] = {}
        self._job_handler_registry = None
        self._job_handlers_loaded = False

    def plugin_source(self, name: str) -> str | None:
        """Return the discovery source tag (``builtin``, ``entry_point``,
        ``user``, ``project``) for the named plugin, if loaded.
        """
        if self._plugins is None:
            self.plugins()
        return self._plugin_sources.get(name)

    def plugins(self) -> dict[str, PollyPMPlugin]:
        if self._plugins is None:
            self._plugins = self._load_plugins()
        return dict(self._plugins)

    def remove_plugin(self, name: str) -> None:
        """Remove a plugin from the loaded registry (e.g. after validation failure)."""
        if self._plugins is not None:
            self._plugins.pop(name, None)
            self._plugin_sources.pop(name, None)

    def get_provider(self, name: str) -> object:
        return self._resolve_factory(name, lambda plugin: plugin.providers, "provider")

    def get_runtime(self, name: str) -> object:
        return self._resolve_factory(name, lambda plugin: plugin.runtimes, "runtime")

    def get_heartbeat_backend(self, name: str) -> object:
        return self._resolve_factory(name, lambda plugin: plugin.heartbeat_backends, "heartbeat backend")

    def get_scheduler_backend(self, name: str) -> object:
        return self._resolve_factory(name, lambda plugin: plugin.scheduler_backends, "scheduler backend")

    def get_agent_profile(self, name: str) -> object:
        return self._resolve_factory(name, lambda plugin: plugin.agent_profiles, "agent profile")

    def get_session_service(self, name: str, **kwargs: object) -> object:
        return self._resolve_factory(
            name, lambda plugin: plugin.session_services, "session service", **kwargs,
        )

    def get_recovery_policy(self, name: str, **kwargs: object) -> object:
        return self._resolve_factory(
            name, lambda plugin: plugin.recovery_policies, "recovery policy", **kwargs,
        )

    def get_transcript_source(self, name: str, **kwargs: object) -> object:
        return self._resolve_factory(
            name, lambda plugin: plugin.transcript_sources, "transcript source", **kwargs,
        )

    def iter_transcript_sources(self, **kwargs: object) -> list[tuple[str, object]]:
        """Return (name, instance) pairs for all registered transcript sources.

        Kwargs are forwarded to each factory. Factories that raise are skipped
        (the error is recorded on self.errors).
        """
        out: list[tuple[str, object]] = []
        seen: set[str] = set()
        for plugin in self.plugins().values():
            for name, factory in plugin.transcript_sources.items():
                if name in seen:
                    continue
                seen.add(name)
                try:
                    out.append((name, factory(**kwargs)))
                except Exception as exc:  # noqa: BLE001
                    self.errors.append(
                        f"Plugin {plugin.name} transcript_source '{name}' factory failed: {exc}"
                    )
        return out

    def job_handler_registry(self) -> "JobHandlerRegistry":
        """Return the plugin host's ``JobHandlerRegistry`` singleton.

        On first access we invoke every plugin's ``register_handlers`` hook
        (after session-service registration) so the registry is populated
        by the time callers look for a handler.
        """
        # Local import to avoid requiring the jobs package at plugin-host
        # import time.
        from pollypm.jobs.registry import JobHandlerRegistry

        if self._job_handler_registry is None:
            self._job_handler_registry = JobHandlerRegistry()
        if not self._job_handlers_loaded:
            self._job_handlers_loaded = True
            for plugin in self.plugins().values():
                hook = plugin.register_handlers
                if hook is None:
                    continue
                api = JobHandlerAPI(self._job_handler_registry, plugin_name=plugin.name)
                try:
                    hook(api)
                except Exception as exc:  # noqa: BLE001
                    message = f"Plugin {plugin.name} register_handlers hook failed: {exc}"
                    self.errors.append(message)
                    logger.exception(message)
        return self._job_handler_registry

    def build_roster(self) -> "Roster":
        """Build a ``Roster`` by invoking every plugin's ``register_roster`` hook.

        Plugins register in plugin-load order (which follows the search-path
        sequence: builtin → user → repo). Collisions are logged but never
        raise — see :class:`pollypm.plugin_api.v1.RosterAPI`.
        """
        # Local import to keep plugin_host importable without heartbeat deps.
        from pollypm.heartbeat.roster import Roster

        roster = Roster()

        def _on_collision(plugin_name: str, handler_name: str, schedule: str) -> None:
            logger.info(
                "Plugin '%s' re-registered recurring handler '%s' "
                "(schedule=%s) — collision deduped, keeping original",
                plugin_name, handler_name, schedule,
            )

        for plugin in self.plugins().values():
            hook = plugin.register_roster
            if hook is None:
                continue
            api = RosterAPI(roster, plugin_name=plugin.name, on_collision=_on_collision)
            try:
                hook(api)
            except Exception as exc:  # noqa: BLE001
                message = f"Plugin {plugin.name} register_roster hook failed: {exc}"
                self.errors.append(message)
                logger.exception(message)
        return roster

    def _resolve_factory(self, name: str, registry_getter, kind: str, **kwargs: object) -> object:
        registry: dict[str, object] = {}
        sources: dict[str, str] = {}
        # Build a (kind_hint, item_name) -> plugin name map for "explicit
        # replaces" — a plugin whose capability declares replaces=[X] wins
        # over any implicit last-write on X.
        replacements: dict[str, str] = {}  # item_name -> replacing plugin name
        for plugin in self.plugins().values():
            for cap in plugin.capabilities:
                for target in cap.replaces:
                    replacements[target] = plugin.name
        for plugin in self.plugins().values():
            for item_name, factory in registry_getter(plugin).items():
                previous = sources.get(item_name)
                if previous is not None and previous != plugin.name:
                    # Skip the override if an explicit replacement was
                    # already installed by another plugin.
                    if replacements.get(item_name) == previous and replacements.get(item_name) != plugin.name:
                        logger.debug(
                            "%s '%s' from plugin '%s' preserved — explicit replace wins over implicit override from '%s'",
                            kind, item_name, previous, plugin.name,
                        )
                        continue
                    logger.debug(
                        "%s '%s' from plugin '%s' overrides '%s' from plugin '%s'",
                        kind, item_name, plugin.name, item_name, previous,
                    )
                registry[item_name] = factory
                sources[item_name] = plugin.name
        factory = registry.get(name)
        if factory is not None:
            try:
                return factory(**kwargs)
            except Exception as exc:
                self.errors.append(f"Plugin factory for {kind} '{name}' crashed: {exc}")
                raise ValueError(f"Plugin {kind} '{name}' failed to initialize: {exc}") from exc
        raise ValueError(f"Unsupported {kind}: {name}")

    def run_observers(self, hook_name: str, payload: object, *, metadata: dict[str, object] | None = None) -> list[str]:
        context = HookContext(hook_name=hook_name, payload=payload, root_dir=self.root_dir, metadata=dict(metadata or {}))
        failures: list[str] = []
        for plugin in self.plugins().values():
            for observer in plugin.observers.get(hook_name, []):
                try:
                    observer(context)
                except Exception as exc:  # noqa: BLE001
                    message = f"{plugin.name} observer {hook_name} failed: {exc}"
                    self.errors.append(message)
                    failures.append(message)
        return failures

    def run_filters(self, hook_name: str, payload: object, *, metadata: dict[str, object] | None = None) -> HookFilterResult:
        current_payload = payload
        for plugin in self.plugins().values():
            for filter_handler in plugin.filters.get(hook_name, []):
                context = HookContext(
                    hook_name=hook_name,
                    payload=current_payload,
                    root_dir=self.root_dir,
                    metadata=dict(metadata or {}),
                )
                try:
                    result = filter_handler(context)
                except Exception as exc:  # noqa: BLE001
                    self.errors.append(f"{plugin.name} filter {hook_name} failed: {exc}")
                    continue
                if result is None:
                    continue
                if result.action in {"deny", "defer"}:
                    return result
                if result.action == "mutate":
                    current_payload = result.payload
        return HookFilterResult(action="allow", payload=current_payload)

    def _load_plugins(self) -> dict[str, PollyPMPlugin]:
        loaded: dict[str, PollyPMPlugin] = {}
        sources: dict[str, str] = {}  # plugin name -> source tag

        def _install(name: str, plugin: PollyPMPlugin, source: str) -> None:
            # Validate the plugin implements its declared interfaces
            try:
                from pollypm.plugin_validate import validate_plugin
                result = validate_plugin(plugin)
                if not result.passed:
                    failures = ", ".join(c.message for c in result.checks if not c.passed)
                    self.errors.append(f"Plugin {name} failed validation: {failures}")
                    return
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"Plugin {name} validation error: {exc}")
            if name in loaded:
                logger.warning(
                    "Plugin '%s' from source '%s' overrides earlier registration from source '%s'",
                    name, source, sources.get(name),
                )
            loaded[name] = plugin
            sources[name] = source

        # Order matters: later sources win on name collision, matching
        # the spec §2 precedence table.
        for manifest in self._discover_directory_manifests(sources=("builtin",)):
            plugin = self._load_plugin_from_manifest(manifest)
            if plugin is not None:
                _install(manifest.name, plugin, manifest.source)

        for name, plugin, source in self._discover_entry_points():
            _install(name, plugin, source)

        # "repo" is a legacy alias for "project" used by older tests; we
        # accept both so internal call sites can transition gradually.
        for manifest in self._discover_directory_manifests(sources=("user", "project", "repo")):
            plugin = self._load_plugin_from_manifest(manifest)
            if plugin is not None:
                _install(manifest.name, plugin, manifest.source)

        self._plugin_sources = sources  # exposed for doctor / CLI later
        return loaded

    def _discover_manifests(self) -> list[PluginManifest]:
        """Compatibility shim: return manifests for all directory-style
        sources (built-in, user-global, project-local). Entry-point
        plugins are handled separately in ``_load_plugins``.
        """
        return self._discover_directory_manifests()

    def _discover_directory_manifests(
        self,
        *,
        sources: tuple[str, ...] | None = None,
    ) -> list[PluginManifest]:
        manifests: list[PluginManifest] = []
        allowed = set(sources) if sources is not None else None
        for source, base in self._plugin_search_paths():
            if allowed is not None and source not in allowed:
                continue
            if not base.exists():
                continue
            for plugin_dir in sorted(path for path in base.iterdir() if path.is_dir()):
                manifest_path = plugin_dir / PLUGIN_MANIFEST
                if not manifest_path.exists():
                    continue
                try:
                    manifests.append(self._read_manifest(manifest_path, source))
                except Exception as exc:  # noqa: BLE001
                    self.errors.append(f"Invalid plugin manifest at {manifest_path}: {exc}")
        return manifests

    def _discover_entry_points(self) -> list[tuple[str, PollyPMPlugin, str]]:
        """Discover plugins registered via the ``pollypm.plugins`` entry
        point group. Each entry point's loaded object must be a
        ``PollyPMPlugin`` instance; the entry-point name is used as the
        plugin name if the instance doesn't set ``name``.
        """
        found: list[tuple[str, PollyPMPlugin, str]] = []
        try:
            from importlib.metadata import entry_points
        except Exception:  # noqa: BLE001
            return found
        try:
            eps = entry_points(group="pollypm.plugins")
        except TypeError:
            # Python <3.10 API fallback (should never hit on this rail).
            try:
                eps = entry_points().get("pollypm.plugins", [])  # type: ignore[assignment]
            except Exception:  # noqa: BLE001
                return found
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"Failed to enumerate pollypm.plugins entry points: {exc}")
            return found
        for ep in eps:
            try:
                obj = ep.load()
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"Entry-point plugin '{ep.name}' failed to load: {exc}")
                continue
            if not isinstance(obj, PollyPMPlugin):
                self.errors.append(
                    f"Entry-point plugin '{ep.name}' is not a PollyPMPlugin instance "
                    f"(got {type(obj).__name__})"
                )
                continue
            plugin_name = obj.name or ep.name
            # Apply the same api_version / requires_api gating that
            # directory plugins get through _load_plugin_from_manifest.
            if obj.api_version != PLUGIN_API_VERSION:
                self.errors.append(
                    f"Entry-point plugin '{plugin_name}' uses API version {obj.api_version}; "
                    f"expected {PLUGIN_API_VERSION}"
                )
                continue
            found.append((plugin_name, obj, "entry_point"))
        return found

    def _plugin_search_paths(self) -> list[tuple[str, Path]]:
        """Directory-style search paths, in precedence order. Later
        sources win on name collision.

        Per docs/plugin-discovery-spec.md §2:
        1. Built-in: ``src/pollypm/plugins_builtin/``
        2. Python entry_points  (handled in ``_discover_entry_points``)
        3. User-global: ``~/.pollypm/plugins/``
        4. Project-local: ``<project>/.pollypm/plugins/``
        """
        builtins = Path(__file__).resolve().parent / "plugins_builtin"
        user = Path.home() / ".pollypm" / "plugins"
        project = self.root_dir / ".pollypm" / "plugins"
        return [
            ("builtin", builtins),
            ("user", user),
            ("project", project),
        ]

    def _read_manifest(self, manifest_path: Path, source: str) -> PluginManifest:
        raw = tomllib.loads(manifest_path.read_text())
        name = str(raw["name"])
        capabilities = self._parse_capability_entries(raw.get("capabilities", []), plugin_name=name)
        return PluginManifest(
            name=name,
            api_version=str(raw["api_version"]),
            version=str(raw.get("version", "0.1.0")),
            kind=str(raw.get("kind", "")),
            entrypoint=str(raw["entrypoint"]),
            capabilities=capabilities,
            description=str(raw.get("description", "")),
            plugin_dir=manifest_path.parent,
            source=source,
            requires_api=(str(raw["requires_api"]) if raw.get("requires_api") is not None else None),
        )

    def _parse_capability_entries(
        self, raw: object, *, plugin_name: str,
    ) -> tuple[Capability, ...]:
        """Parse the ``capabilities`` manifest field — structured blocks
        (list-of-tables) or legacy bare strings. Bare strings are accepted
        for one release and emit a deprecation warning."""
        if raw is None:
            return ()
        if not isinstance(raw, list):
            raise ValueError("capabilities must be a list")
        structured: list[Capability] = []
        legacy_strings: list[str] = []
        for entry in raw:
            if isinstance(entry, str):
                legacy_strings.append(entry)
                continue
            if isinstance(entry, dict):
                kind = entry.get("kind")
                if not isinstance(kind, str) or not kind.strip():
                    raise ValueError(
                        f"capability block missing 'kind' in plugin '{plugin_name}'"
                    )
                name_field = entry.get("name")
                if name_field is None:
                    cap_name = plugin_name
                else:
                    cap_name = str(name_field)
                replaces_raw = entry.get("replaces", ())
                if isinstance(replaces_raw, (list, tuple)):
                    replaces = tuple(str(item) for item in replaces_raw)
                else:
                    raise ValueError(
                        f"capability 'replaces' must be a list in plugin '{plugin_name}'"
                    )
                requires_api = entry.get("requires_api")
                if requires_api is not None and not isinstance(requires_api, str):
                    raise ValueError(
                        f"capability 'requires_api' must be a string in plugin '{plugin_name}'"
                    )
                if kind not in KNOWN_CAPABILITY_KINDS:
                    logger.warning(
                        "Plugin '%s' declares unknown capability kind '%s' — preserved but not a recognised rail capability.",
                        plugin_name, kind,
                    )
                structured.append(
                    Capability(
                        kind=kind.strip(),
                        name=cap_name,
                        replaces=replaces,
                        requires_api=requires_api,
                    )
                )
                continue
            raise ValueError(
                f"capability entry must be a table or string in plugin '{plugin_name}', got {type(entry).__name__}"
            )

        if legacy_strings and structured:
            logger.warning(
                "Plugin '%s' mixes legacy bare-string capabilities with structured [[capabilities]] blocks; "
                "migrate to fully structured form (kind=, name=).",
                plugin_name,
            )
        if legacy_strings and not structured:
            logger.warning(
                "Plugin '%s' uses legacy bare-string capabilities %s; migrate to structured [[capabilities]] blocks "
                "(kind=, name=) — deprecation target: next release.",
                plugin_name, legacy_strings,
            )
        for raw_string in legacy_strings:
            structured.append(Capability(kind=raw_string, name=plugin_name))
        return tuple(structured)

    def _load_plugin_from_manifest(self, manifest: PluginManifest) -> PollyPMPlugin | None:
        if manifest.api_version != PLUGIN_API_VERSION:
            self.errors.append(
                f"Plugin {manifest.name} uses API version {manifest.api_version}; expected {PLUGIN_API_VERSION}"
            )
            return None
        # Plugin-level requires_api (manifest top-level) must include the
        # current rail API. Per-capability requires_api is enforced in
        # _filter_capabilities below.
        if manifest.requires_api:
            try:
                if not check_requires_api(manifest.requires_api, PLUGIN_API_VERSION):
                    self.errors.append(
                        f"Plugin {manifest.name} requires_api '{manifest.requires_api}' excludes current API "
                        f"version {PLUGIN_API_VERSION}; skipped."
                    )
                    return None
            except ValueError as exc:
                self.errors.append(
                    f"Plugin {manifest.name} has unparseable requires_api '{manifest.requires_api}': {exc}"
                )
                return None
        try:
            module = self._load_module(manifest)
            plugin_obj = self._resolve_entrypoint(module, manifest.entrypoint)
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"Failed to load plugin {manifest.name}: {exc}")
            return None
        if not isinstance(plugin_obj, PollyPMPlugin):
            self.errors.append(f"Plugin {manifest.name} entrypoint did not return PollyPMPlugin")
            return None
        # If the manifest declared structured capabilities, prefer them
        # over whatever the plugin module set — the manifest is canonical
        # for discovery/doctor reporting.
        if manifest.capabilities:
            filtered = self._filter_capabilities(manifest.name, manifest.capabilities)
            plugin_obj.capabilities = filtered
        return plugin_obj

    def _filter_capabilities(
        self, plugin_name: str, capabilities: tuple[Capability, ...],
    ) -> tuple[Capability, ...]:
        """Drop capabilities whose ``requires_api`` excludes the rail."""
        kept: list[Capability] = []
        for cap in capabilities:
            try:
                if not check_requires_api(cap.requires_api, PLUGIN_API_VERSION):
                    self.errors.append(
                        f"Plugin {plugin_name} capability {cap.kind}:{cap.name} requires_api "
                        f"'{cap.requires_api}' excludes current API version {PLUGIN_API_VERSION}; dropped."
                    )
                    continue
            except ValueError as exc:
                self.errors.append(
                    f"Plugin {plugin_name} capability {cap.kind}:{cap.name} has unparseable "
                    f"requires_api '{cap.requires_api}': {exc}; dropped."
                )
                continue
            kept.append(cap)
        return tuple(kept)

    def _load_module(self, manifest: PluginManifest) -> types.ModuleType:
        module_ref, _attr = manifest.entrypoint.split(":", 1)
        if module_ref.endswith(".py"):
            module_path = manifest.plugin_dir / module_ref
        else:
            module_path = manifest.plugin_dir / f"{module_ref.replace('.', '/')}.py"
        spec = spec_from_file_location(f"pollypm_plugin_{manifest.name}_{manifest.source}", module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot import plugin module {module_path}")
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _resolve_entrypoint(self, module: types.ModuleType, entrypoint: str) -> object:
        _module_ref, attr = entrypoint.split(":", 1)
        return getattr(module, attr)


@lru_cache(maxsize=16)
def extension_host_for_root(root_dir: str) -> ExtensionHost:
    return ExtensionHost(Path(root_dir))
