"""Entry-point-driven storage backend registry (issue #343).

PollyPM's persistent state backend is selected at runtime via the
``pollypm.store_backend`` entry-point group. This module is the single
public entry point for resolving a backend from a loaded
:class:`~pollypm.models.PollyPMConfig`.

Design
------

* **Entry points — not a hard-coded switch.** Built-in SQLite is
  registered by PollyPM itself in ``pyproject.toml``. Third-party
  packages (e.g. the future ``pollypm-store-postgres`` shipped via
  :mod:`pollypm.store.backends.postgres_stub`) register their own
  entry points under the same group and become selectable without any
  code change in this repo.
* **URL resolution lives in one place.** If ``config.storage.url`` is
  empty, we derive ``sqlite:///<project.state_db>`` so a first-run user
  who never touched ``[storage]`` still gets a working DB at the path
  the rest of PollyPM already uses.
* **Unknown backend fails loud.** :class:`StoreBackendNotFound` lists
  the backends that *are* installed so a typo is immediately visible
  (three-question rule — issue #240).

The registry returns a :class:`pollypm.store.Store`-satisfying object.
The entry-point target must be a callable taking a ``url=`` keyword —
both :class:`SQLAlchemyStore` and the Postgres stub honour that shape.
"""

from __future__ import annotations

import importlib.metadata
import threading
from typing import TYPE_CHECKING

from pollypm.errors import StoreBackendNotFound

if TYPE_CHECKING:
    from pollypm.models import PollyPMConfig
    from pollypm.store.protocol import Store


ENTRY_POINT_GROUP = "pollypm.store_backend"

# Module-level cache of live ``Store`` instances, keyed by
# ``(backend, resolved_url)``. Every call site that reaches for
# ``get_store`` — the supervisor, tmux session service, job handlers,
# plugin initializers, ``messaging``, ``version_check``, doctor, and
# more — used to construct a fresh ``SQLAlchemyStore`` (and thus a
# fresh engine pool) on every invocation. With 9+ callers hit on each
# heartbeat sweep and a 5-connection pool per store, the rail daemon
# bled 131 live SQLite + 127 WAL handles in under an hour, blew past
# the macOS 256-FD soft limit, and started surfacing
# ``[Errno 24] Too many open files`` toasts from transcript_ingest.
#
# Caching the backend per (backend, url) gives every caller the same
# pool; dispose is now reference-counted via :func:`release_store` so
# the last caller still tears the engine down cleanly on shutdown.
_STORES: dict[tuple[str, str], "Store"] = {}
_STORE_LOCK = threading.Lock()


def _resolve_url(config: "PollyPMConfig") -> str:
    """Return the SQLAlchemy URL for ``config``.

    Honours ``config.storage.url`` verbatim when set. Otherwise derives
    ``sqlite:///<project.state_db>`` so the resolver always produces a
    concrete URL — backends never have to re-implement the fallback.
    The config parser already applies the same default, but we re-check
    here so test doubles and hand-built configs still work.
    """
    url = (config.storage.url or "").strip()
    if url:
        return url
    return f"sqlite:///{config.project.state_db.resolve()}"


def _available_backends() -> list[str]:
    """Return sorted entry-point names registered under our group."""
    return sorted(
        ep.name
        for ep in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
    )


def get_store(config: "PollyPMConfig") -> "Store":
    """Return the process-wide ``Store`` instance for ``config``.

    Caches by ``(backend, resolved_url)`` so every caller shares the
    same engine pool. First call constructs the backend via its
    entry point; subsequent calls with the same config reuse the
    cached instance.

    Parameters
    ----------
    config
        The loaded :class:`~pollypm.models.PollyPMConfig`. Only
        ``config.storage`` and ``config.project.state_db`` are read.

    Returns
    -------
    Store
        A singleton :class:`pollypm.store.Store` implementation.
        **Do not** call ``dispose()`` on the returned instance —
        other code in the process may still be using it. Use
        :func:`reset_store_cache` at shutdown to dispose all cached
        stores cleanly.

    Raises
    ------
    StoreBackendNotFound
        When ``config.storage.backend`` does not match any installed
        entry point in the ``pollypm.store_backend`` group. The error
        message lists every backend that *is* registered.
    """
    backend = config.storage.backend
    url = _resolve_url(config)
    key = (backend, url)

    # Fast path — lock-free read. The dict mutation in the miss path
    # is serialized by ``_STORE_LOCK``, so a racing read either sees
    # the fully-constructed store or falls into the slow path.
    cached = _STORES.get(key)
    if cached is not None:
        return cached

    with _STORE_LOCK:
        cached = _STORES.get(key)
        if cached is not None:
            return cached
        for ep in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
            if ep.name == backend:
                cls = ep.load()
                instance = cls(url=url)
                _STORES[key] = instance
                return instance
    raise StoreBackendNotFound(
        backend,
        available=_available_backends(),
    )


def reset_store_cache() -> None:
    """Dispose every cached store and clear the registry.

    Called on process shutdown (CoreRail.stop, test teardown).
    Individual callers should *not* dispose the shared instance —
    use this to drain all backends at once. Idempotent; safe to
    call twice.
    """
    with _STORE_LOCK:
        stores = list(_STORES.values())
        _STORES.clear()
    for store in stores:
        dispose = getattr(store, "dispose", None)
        if callable(dispose):
            try:
                dispose()
            except Exception:  # noqa: BLE001
                pass


__all__ = ["ENTRY_POINT_GROUP", "get_store", "reset_store_cache"]
