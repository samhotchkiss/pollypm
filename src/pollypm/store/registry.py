"""Entry-point-driven storage backend registry (issue #343).

PollyPM's persistent state backend is selected at runtime via the
``pollypm.store_backend`` entry-point group. This module is the single
public entry point for resolving a backend from a loaded
:class:`~pollypm.models.PollyPMConfig`.

Design
------

* **Entry points — not a hard-coded switch.** Postgres is registered
  by PollyPM itself in ``pyproject.toml``. Third-party packages
  register their own entry points under the same group and become
  selectable without any code change in this repo.
* **Sqlite is not entry-pointed (#1956).** The pg cutover (#1737)
  made postgres the supported production backend; leaving sqlite
  registered let a misconfigured ``[storage].backend = "sqlite"``
  silently open an empty shadow alongside the real pg state. Sqlite
  is now test-only via :func:`register_backend`: the function hard-
  rejects ``name == "sqlite"`` outside a pytest process (refs #1971,
  #1970), and ``get_store(config)`` with backend=="sqlite" on a stock
  install fails loud with :class:`StoreBackendNotFound`. The
  ``pm notify --db <path>`` / ``pm inbox --db <path>`` CLI flags no
  longer opt sqlite back in either — they target a pg URL only.
* **URL resolution lives in one place.** If ``config.storage.url`` is
  empty and the backend is sqlite (i.e. an opt-in caller registered
  it), we derive ``sqlite:///<project.state_db>`` so a test using
  the default ``state_db`` path still gets a working DB. On
  postgres (the production default) an empty URL lets the pg pool
  resolver pick the DSN up from ``[storage.pg].dsn`` /
  ``POLLYPM_PG_DSN``.
* **Unknown backend fails loud.** :class:`StoreBackendNotFound` lists
  the backends that *are* installed (entry-point + in-process
  registry union) so a typo is immediately visible (three-question
  rule — issue #240).

The registry returns a :class:`pollypm.store.Store`-satisfying object.
Every backend factory must be a callable taking a ``url=`` keyword —
both :class:`SQLAlchemyStore` and the Postgres backend honour that.
"""

from __future__ import annotations

import importlib.metadata
import logging
import os
import sys
import threading
from typing import TYPE_CHECKING, Callable

from pollypm.errors import StoreBackendNotFound

if TYPE_CHECKING:
    from pollypm.models import PollyPMConfig
    from pollypm.store.protocol import Store


ENTRY_POINT_GROUP = "pollypm.store_backend"

logger = logging.getLogger(__name__)

# #1956: sqlite was removed from the ``pollypm.store_backend`` entry-point
# group in ``pyproject.toml`` so a misconfigured ``[storage].backend
# = "sqlite"`` fails loud at :func:`get_store` instead of silently
# opening an empty sqlite shadow next to the real pg state. Only the
# pytest suite may re-register sqlite via :func:`register_backend`;
# the function hard-rejects ``name == "sqlite"`` outside a pytest
# process (refs #1971, #1970). The previous ``pm notify --db`` /
# ``pm inbox --db`` CLI escape hatches no longer opt sqlite back in.
#
# Lookup order in the resolvers below:
#   1. ``_REGISTERED_BACKENDS`` (this in-process map; pytest fixtures
#      register here).
#   2. ``importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)``
#      (installed packages; only ``postgres`` ships by default).
_REGISTERED_BACKENDS: dict[str, Callable[..., "Store"]] = {}
_REGISTRATION_LOCK = threading.Lock()


def _running_under_pytest() -> bool:
    """Return True when the active interpreter is a pytest run.

    Used by :func:`register_backend` to gate the sqlite opt-in: only
    the test suite (where re-registering sqlite is the documented
    fixture path; see ``tests/conftest.py``) may opt back in.
    Production processes hard-fail with :class:`ValueError` instead.
    The check is deliberately permissive — either
    ``PYTEST_CURRENT_TEST`` (set by pytest for the duration of each
    item) or ``pytest`` being imported is enough; we'd rather under-
    reject in a niche test harness than break a production rail by
    failing a legitimate pytest run.
    """
    if "PYTEST_CURRENT_TEST" in os.environ:
        return True
    if "pytest" in sys.modules:
        return True
    return False


def register_backend(
    name: str,
    factory: Callable[..., "Store"],
) -> None:
    """Register ``factory`` as the in-process backend for ``name``.

    The opt-in companion to removing sqlite from the
    ``pollypm.store_backend`` entry-point group (#1956). Callers that
    legitimately need a backend not shipped by the installed
    distribution — currently only the pytest suite — call this to plug
    the factory back in for the rest of the process.

    Parameters
    ----------
    name
        The backend key, e.g. ``"sqlite"``. Matches against
        ``config.storage.backend`` and the ``backend=`` kwarg on
        :func:`get_store_by_url`.
    factory
        Anything callable with ``url=<str>`` returning a
        :class:`~pollypm.store.protocol.Store`. Typically
        :class:`~pollypm.store.sqlalchemy_store.SQLAlchemyStore`.

    Raises
    ------
    ValueError
        When ``name == "sqlite"`` is requested from a non-pytest
        process. Post-sqlite-ripout (refs #1971, #1970) sqlite is
        gone from every production code path; the previous warn-log
        plus ``quiet=True`` bypass is now an unconditional hard
        rejection so a stray subprocess that imports
        :class:`~pollypm.store.sqlalchemy_store.SQLAlchemyStore` and
        tries to re-register cannot reactivate the split-brain
        sqlite-shadow class of bugs. There is intentionally no
        production-callable opt-out: a future legacy migration tool
        would live under ``tests/`` or a dedicated migration-only
        module that pytest treats as an in-process test.

    Notes
    -----
    Idempotent: re-registering the same ``(name, factory)`` pair is a
    no-op. Re-registering with a different factory replaces the prior
    entry — the test suite relies on that during teardown.
    """
    if name == "sqlite" and not _running_under_pytest():
        raise ValueError(
            "pollypm.store: refusing to register the sqlite backend in "
            "a production process. The pg cutover (#1737) made postgres "
            "the only supported backend; sqlite was removed from every "
            "production code path in the sqlite-ripout sequence "
            "(refs #1971, #1970). There is no production-callable "
            "opt-out; if you genuinely need sqlite for a migration, "
            "drive it from a pytest-invoked test module."
        )
    with _REGISTRATION_LOCK:
        _REGISTERED_BACKENDS[name] = factory


def unregister_backend(name: str) -> None:
    """Drop ``name`` from the in-process backend registry.

    Used by test teardown to keep the registry isolated between
    pytest sessions. Missing keys are silently ignored.
    """
    with _REGISTRATION_LOCK:
        _REGISTERED_BACKENDS.pop(name, None)


def _resolve_backend_factory(name: str) -> Callable[..., "Store"] | None:
    """Return the factory callable for ``name`` or ``None``.

    Checks the in-process registry first (pytest-only for the
    ``sqlite`` backend — :func:`register_backend` hard-rejects
    ``name == "sqlite"`` outside a pytest process, so production
    sqlite registration is not possible via this map), then falls
    back to the installed ``pollypm.store_backend`` entry-point
    group. Returning ``None`` lets the caller raise
    :class:`StoreBackendNotFound` with the full list of available
    names attached.
    """
    factory = _REGISTERED_BACKENDS.get(name)
    if factory is not None:
        return factory
    for ep in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        if ep.name == name:
            return ep.load()
    return None

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

    Honours ``config.storage.url`` verbatim when set. Otherwise:

    * For the sqlite backend, derives
      ``sqlite:///<project.state_db>`` so the resolver always produces a
      concrete URL — backends never have to re-implement the fallback.
    * For the postgres backend (#1939), prefers ``[storage.pg].dsn``
      (#1952) so the dedicated pg knob is honoured even when the legacy
      shared ``[storage].url`` is blank. Falls back to an empty string,
      which lets the pg pool resolver pick the DSN up from
      ``POLLYPM_PG_DSN`` or the built-in default. Without this, a
      workspace that set ``[storage.pg].dsn`` saw its work-service
      writes route to the configured DB while ``get_store(config)``
      message-store callers silently used the default DB — the
      split-brain failure mode the sqlite ripout was meant to close.
      Fabricating a ``sqlite:///`` URL on a postgres install was the
      original silent-fallback failure the cutover removed.
    """
    url = (config.storage.url or "").strip()
    if url:
        return url
    backend = (config.storage.backend or "").strip().lower()
    if backend == "sqlite":
        return f"sqlite:///{config.project.state_db.resolve()}"
    if backend == "postgres":
        # #1952 — honour ``[storage.pg].dsn`` so the dedicated pg knob
        # routes ``get_store(config)`` to the same DSN that
        # ``pollypm.storage.pg_pool.resolve_dsn`` would pick. Mirrors the
        # priority order there: pg.dsn first, then the legacy shared
        # ``[storage].url`` (already handled above when non-empty).
        pg_section = getattr(config.storage, "pg", None)
        if pg_section is not None:
            pg_dsn = (getattr(pg_section, "dsn", "") or "").strip()
            if pg_dsn:
                return pg_dsn
    return ""


def _available_backends() -> list[str]:
    """Return sorted union of registered + entry-point backend names.

    Includes the in-process :data:`_REGISTERED_BACKENDS` map so a
    ``StoreBackendNotFound`` raised after a test or escape-hatch
    caller has registered sqlite still lists it as installed. Without
    that, the error message would point operators at a backend table
    that disagrees with the resolver they just hit.
    """
    names = {
        ep.name
        for ep in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
    }
    names.update(_REGISTERED_BACKENDS.keys())
    return sorted(names)


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
        factory = _resolve_backend_factory(backend)
        if factory is not None:
            instance = factory(url=url)
            _STORES[key] = instance
            return instance
    raise StoreBackendNotFound(
        backend,
        available=_available_backends(),
    )


def get_store_by_url(url: str, *, backend: str = "sqlite") -> "Store":
    """Return the process-wide ``Store`` for ``url`` without a config.

    Use this when only a DB URL is in scope (plugin handlers, service
    helpers) — it reuses the same ``(backend, url)`` cache as
    :func:`get_store`, so two code paths with the same URL share one
    engine pool. Rail-hot callers that used to construct a fresh
    ``SQLAlchemyStore`` per call would pin ~16 SQLite handles per
    invocation; routing through this helper keeps the pool singleton.

    #1956: sqlite is no longer entry-pointed and the CLI ``--db``
    flags no longer opt sqlite back in. The default
    ``backend="sqlite"`` kwarg only keeps the historical signature
    for the pytest suite, which registers sqlite via
    :func:`register_backend` in ``tests/conftest``. A production
    caller reaching this helper with ``backend="sqlite"`` raises
    :class:`StoreBackendNotFound` listing the backends that *are*
    available; production callers should pass an explicit
    ``backend="postgres"`` instead.
    """
    key = (backend, url)
    cached = _STORES.get(key)
    if cached is not None:
        return cached
    with _STORE_LOCK:
        cached = _STORES.get(key)
        if cached is not None:
            return cached
        factory = _resolve_backend_factory(backend)
        if factory is not None:
            instance = factory(url=url)
            _STORES[key] = instance
            return instance
    raise StoreBackendNotFound(
        backend,
        available=_available_backends(),
    )


def reset_store_cache() -> None:
    """Tear down every cached store and clear the registry.

    Called on process shutdown (CoreRail.stop, test teardown).
    Individual callers should *not* dispose the shared instance —
    use this to drain all backends at once. Idempotent; safe to
    call twice.

    #810: prefer ``close()`` when the backend exposes one — for
    ``SQLAlchemyStore`` that's the only path that flushes the lazy
    :class:`EventBuffer` and deregisters its background thread.
    Calling only ``dispose()`` left queued events in the buffer and
    leaked a background thread/signal-handler entry. ``dispose()``
    stays as the fallback for backends that don't implement ``close``.
    """
    with _STORE_LOCK:
        stores = list(_STORES.values())
        _STORES.clear()
    for store in stores:
        close = getattr(store, "close", None)
        teardown = close if callable(close) else getattr(store, "dispose", None)
        if callable(teardown):
            try:
                teardown()
            except Exception:  # noqa: BLE001
                pass


__all__ = [
    "ENTRY_POINT_GROUP",
    "get_store",
    "get_store_by_url",
    "register_backend",
    "reset_store_cache",
    "unregister_backend",
]
