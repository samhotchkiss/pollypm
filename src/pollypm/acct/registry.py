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

_ENTRY_POINT_GROUP = "pollypm.provider"


def _entry_points() -> list[importlib.metadata.EntryPoint]:
    """Return all registered ``pollypm.provider`` entry points.

    Wrapped in a helper so ``get_provider`` and ``list_providers`` share
    one implementation and tests only need to monkeypatch one function.
    """
    eps = importlib.metadata.entry_points(group=_ENTRY_POINT_GROUP)
    # ``entry_points()`` returns an ``EntryPoints`` selectable view on
    # 3.10+ but iterating it yields ``EntryPoint``s — normalize to a
    # list so callers can cheaply len()/sort() the result.
    return list(eps)


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
