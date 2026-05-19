"""Backend dispatch helper for read-side storage facades (#1737, Slice C).

The read-side ``storage/*`` facades historically open a sqlite file
directly via ``sqlite3.connect(readonly_uri(db_path), uri=True)``. Slice C
extends them so the same call dispatches to Postgres when the active
backend is ``"postgres"`` (issue #1737).

Most call sites already accept a ``db_path: Path`` plus a
``project_key: str`` — that's exactly the per-project partition the pg
schema port keys on. So the dispatch shape is:

* If the active backend is sqlite, run the current path verbatim.
* If the active backend is postgres, run the same query against the
  process-wide RO pool with ``WHERE project_key = %s`` (or ``project = %s``
  where the table uses the legacy column name).

This module is the single shared resolver for that branch. Keeping it in
one place means the dozen facades don't each grow their own copy of the
"is the active backend pg?" probe — and a future config-key rename only
needs to touch one spot.

The resolver is intentionally tolerant: a missing config / unreadable
config / fat-fingered backend value falls back to ``False`` (sqlite).
Slice A's doctor check already surfaces real typos through its own
error path, so the facades themselves stay quiet on bad config.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pollypm.models import PollyPMConfig

logger = logging.getLogger(__name__)


def is_pg_backend(config: "PollyPMConfig | None" = None) -> bool:
    """Return ``True`` when the active storage backend is Postgres.

    Reads ``config.storage.backend``; when ``config`` is ``None`` it
    falls back to :func:`pollypm.config.load_config`. A failed load,
    missing attribute, or non-string value is treated as "not pg" so
    the facade defaults to its current sqlite path.

    The lookup is intentionally cheap — the facades that use it run on
    cockpit hot paths. The config load is memoised by ``load_config``
    itself, so repeated calls inside one process don't reparse the TOML.
    """
    if config is None:
        try:
            from pollypm.config import load_config

            config = load_config()
        except Exception:  # noqa: BLE001 — never break a render on config
            return False
    storage = getattr(config, "storage", None)
    if storage is None:
        return False
    backend = getattr(storage, "backend", "sqlite")
    if not isinstance(backend, str):
        return False
    return backend.strip().lower() == "postgres"


__all__ = ["is_pg_backend"]
