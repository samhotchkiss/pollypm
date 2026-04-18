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
from typing import TYPE_CHECKING

from pollypm.errors import StoreBackendNotFound

if TYPE_CHECKING:
    from pollypm.models import PollyPMConfig
    from pollypm.store.protocol import Store


ENTRY_POINT_GROUP = "pollypm.store_backend"


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
    """Load the configured storage backend via entry points.

    Parameters
    ----------
    config
        The loaded :class:`~pollypm.models.PollyPMConfig`. Only
        ``config.storage`` and ``config.project.state_db`` are read.

    Returns
    -------
    Store
        An instantiated backend satisfying the
        :class:`pollypm.store.Store` protocol. The caller owns its
        lifecycle — call ``store.dispose()`` when finished.

    Raises
    ------
    StoreBackendNotFound
        When ``config.storage.backend`` does not match any installed
        entry point in the ``pollypm.store_backend`` group. The error
        message lists every backend that *is* registered.
    """
    backend = config.storage.backend
    for ep in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        if ep.name == backend:
            cls = ep.load()
            return cls(url=_resolve_url(config))
    raise StoreBackendNotFound(
        backend,
        available=_available_backends(),
    )


__all__ = ["ENTRY_POINT_GROUP", "get_store"]
