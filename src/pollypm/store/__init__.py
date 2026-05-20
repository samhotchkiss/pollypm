"""PollyPM storage foundation — ``Store`` protocol and pg backend.

This package is the structural replacement for the ad-hoc
``sqlite3.connect`` callers scattered across the codebase. Every
subsystem that needs persistent state routes through the
:class:`Store` protocol defined here.

Post-sqlite-ripout (refs #1971) the sqlite backend (the legacy
``SQLAlchemyStore``) is gone — pg is the only supported backend.
``make_engines`` / ``is_sqlite`` are kept for the few callers that
still build their own engine pair for offline data migrations.
"""

from __future__ import annotations

from pollypm.store.engine import is_sqlite, make_engines
from pollypm.store.protocol import Store
from pollypm.store.registry import (
    get_store,
    get_store_by_url,
    register_backend,
    unregister_backend,
)
from pollypm.store.title_contract import apply_title_contract

__all__ = [
    "Store",
    "apply_title_contract",
    "get_store",
    "get_store_by_url",
    "is_sqlite",
    "make_engines",
    "register_backend",
    "unregister_backend",
]
