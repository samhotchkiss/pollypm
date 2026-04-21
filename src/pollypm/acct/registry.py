"""Entry-point-backed registry for provider adapters.

Phase A ships two adapters — ``claude`` and ``codex`` — registered via
``pyproject.toml``'s ``[project.entry-points."pollypm.provider"]``
section. Third-party plugins can register additional providers the same
way; ``get_provider()`` does not distinguish built-in from third-party.

The registry is cached with ``functools.lru_cache`` so repeated
``get_provider()`` calls in the same process return the same adapter
instance. That matters for adapters that hold lazy state (a subprocess
pool, a cached login timestamp). The cache is keyed by the provider
name only — clearing it is normally unnecessary, but tests that mutate
the entry-point table can call ``get_provider.cache_clear()``.
"""

from __future__ import annotations

import importlib.metadata
from functools import lru_cache

from .errors import ProviderNotFound
from .protocol import ProviderAdapter
from pollypm.plugin_trust import warn_third_party_extension_trust_once

_ENTRY_POINT_GROUP = "pollypm.provider"


def _is_builtin_provider_entry_point(ep: importlib.metadata.EntryPoint) -> bool:
    """Return True when ``ep`` resolves to a PollyPM-shipped provider."""
    module_name = getattr(ep, "module", None)
    if not module_name:
        value = getattr(ep, "value", "")
        module_name = str(value).split(":", 1)[0]
    return str(module_name).startswith("pollypm.")


def _entry_points() -> list[importlib.metadata.EntryPoint]:
    """Return all registered ``pollypm.provider`` entry points.

    Wrapped in a helper so ``get_provider`` and ``list_providers`` share
    one implementation and tests only need to monkeypatch one function.
    """
    eps = importlib.metadata.entry_points(group=_ENTRY_POINT_GROUP)
    # ``entry_points()`` returns an ``EntryPoints`` selectable view on
    # 3.10+ but iterating it yields ``EntryPoint``s — normalize to a
    # list so callers can cheaply len()/sort() the result.
    normalized = list(eps)
    if any(not _is_builtin_provider_entry_point(ep) for ep in normalized):
        warn_third_party_extension_trust_once()
    return normalized


@lru_cache(maxsize=None)
def get_provider(name: str) -> ProviderAdapter:
    """Resolve a provider adapter by name.

    Loads the class registered under the ``pollypm.provider``
    entry-point group, instantiates it, and returns the instance. The
    result is cached — see the module docstring.

    Raises:
        ProviderNotFound: no adapter is registered under ``name``.
    """
    available: list[str] = []
    for ep in _entry_points():
        available.append(ep.name)
        if ep.name == name:
            cls = ep.load()
            return cls()
    raise ProviderNotFound(name, available=sorted(available))


def list_providers() -> list[str]:
    """Return the sorted names of every registered provider."""
    return sorted(ep.name for ep in _entry_points())


__all__ = ["get_provider", "list_providers"]
