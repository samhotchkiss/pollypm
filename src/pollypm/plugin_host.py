from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import tomllib
import types

from pollypm.plugin_api.v1 import HookContext, HookFilterResult, PollyPMPlugin, RosterAPI

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
    capabilities: tuple[str, ...]
    description: str
    plugin_dir: Path
    source: str


class ExtensionHost:
    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir.resolve()
        self.errors: list[str] = []
        self._plugins: dict[str, PollyPMPlugin] | None = None

    def plugins(self) -> dict[str, PollyPMPlugin]:
        if self._plugins is None:
            self._plugins = self._load_plugins()
        return dict(self._plugins)

    def remove_plugin(self, name: str) -> None:
        """Remove a plugin from the loaded registry (e.g. after validation failure)."""
        if self._plugins is not None:
            self._plugins.pop(name, None)

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
        for plugin in self.plugins().values():
            for item_name, factory in registry_getter(plugin).items():
                if item_name in registry and sources.get(item_name) != plugin.name:
                    logger.debug(
                        "%s '%s' from plugin '%s' overrides '%s' from plugin '%s'",
                        kind, item_name, plugin.name, item_name, sources[item_name],
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
        for manifest in self._discover_manifests():
            plugin = self._load_plugin_from_manifest(manifest)
            if plugin is None:
                continue
            # Validate the plugin implements its declared interfaces
            try:
                from pollypm.plugin_validate import validate_plugin
                result = validate_plugin(plugin)
                if not result.passed:
                    failures = ", ".join(c.message for c in result.checks if not c.passed)
                    self.errors.append(f"Plugin {manifest.name} failed validation: {failures}")
                    continue  # skip broken plugins
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"Plugin {manifest.name} validation error: {exc}")
            loaded[manifest.name] = plugin
        return loaded

    def _discover_manifests(self) -> list[PluginManifest]:
        manifests: list[PluginManifest] = []
        for source, base in self._plugin_search_paths():
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

    def _plugin_search_paths(self) -> list[tuple[str, Path]]:
        builtins = Path(__file__).resolve().parent / "plugins_builtin"
        user = Path.home() / ".config" / "pollypm" / "plugins"
        repo = self.root_dir / ".pollypm-state" / "plugins"
        return [
            ("builtin", builtins),
            ("user", user),
            ("repo", repo),
        ]

    def _read_manifest(self, manifest_path: Path, source: str) -> PluginManifest:
        raw = tomllib.loads(manifest_path.read_text())
        return PluginManifest(
            name=str(raw["name"]),
            api_version=str(raw["api_version"]),
            version=str(raw.get("version", "0.1.0")),
            kind=str(raw.get("kind", "")),
            entrypoint=str(raw["entrypoint"]),
            capabilities=tuple(str(item) for item in raw.get("capabilities", [])),
            description=str(raw.get("description", "")),
            plugin_dir=manifest_path.parent,
            source=source,
        )

    def _load_plugin_from_manifest(self, manifest: PluginManifest) -> PollyPMPlugin | None:
        if manifest.api_version != PLUGIN_API_VERSION:
            self.errors.append(
                f"Plugin {manifest.name} uses API version {manifest.api_version}; expected {PLUGIN_API_VERSION}"
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
        return plugin_obj

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
