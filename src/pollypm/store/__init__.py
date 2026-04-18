"""PollyPM storage foundation — ``Store`` protocol and SQLAlchemy backend.

This package is the structural replacement for the ad-hoc ``sqlite3.connect``
callers scattered across the codebase. Every subsystem that needs persistent
state will eventually route through the :class:`Store` protocol defined here.

Issue #337 lands the foundation only:

* :class:`Store` — the structural :class:`typing.Protocol` every caller targets.
* :func:`make_engines` — the dual-pool engine factory (writer pool_size=1 for
  serialized writes, reader pool_size=5 for WAL-concurrent reads).
* :class:`SQLAlchemyStore` — a skeleton backend. Only ``transaction()`` is
  functional; all other methods raise :class:`NotImplementedError` and land in
  later issues (#338 schema, #340 callers).

Downstream issues populate method bodies, define the metadata, and migrate
callers off ``StateStore`` / direct ``sqlite3`` usage. Nothing in this module
should reach into ``~/.pollypm/`` or open a connection at import time.
"""

from __future__ import annotations

from pollypm.store.engine import is_sqlite, make_engines
from pollypm.store.protocol import Store
from pollypm.store.sqlalchemy_store import SQLAlchemyStore

__all__ = [
    "SQLAlchemyStore",
    "Store",
    "is_sqlite",
    "make_engines",
]
