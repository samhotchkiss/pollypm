"""Canonical factory for the active work-service backend.

This module exists so callers outside ``pollypm.work.*`` do not have to
know how the work-service DB is resolved. Migrating callers through
this factory means the resolver is the single point of truth for
"where does work data live".

Backend
-------

Post-sqlite-ripout (refs #1971), pg is the only supported backend.
``create_work_service`` returns :class:`pollypm.work.pg_service.PgWorkService`
wired against the lazy pool singleton in :mod:`pollypm.storage.pg_pool`.

``db_path`` / ``project_path`` / ``sync_manager`` are kept as
keyword arguments for source-compatibility with the legacy
sqlite-aware callsites; pg ignores them. Callers can drop them at
their own pace — there's no behaviour change.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pollypm.config import PollyPMConfig


logger = logging.getLogger(__name__)


def _resolve_backend(config: "PollyPMConfig | None") -> str:
    """Return the configured backend name (always ``"postgres"`` post-#1971).

    Retained as a thin shim for callers that historically branched on
    backend identity (e.g. cockpit inbox per-project fanout). Post-
    sqlite-ripout (refs #1971) pg is the only supported backend, so
    every call returns ``"postgres"``. Callers can collapse their
    branches at their own pace.
    """
    del config
    return "postgres"


def create_work_service(
    *,
    db_path: str | Path | None = None,
    project_path: str | Path | None = None,
    config: "PollyPMConfig | None" = None,
    project_key: str | None = None,
    sync_manager: object = None,
) -> Any:
    """Construct the configured work-service backend (pg-only post-#1971).

    Parameters
    ----------
    db_path:
        Legacy sqlite escape-hatch argument. Ignored — pg is the only
        supported backend.
    project_path:
        Legacy sqlite-only argument. Ignored on pg.
    config:
        Optional pre-loaded :class:`PollyPMConfig`. Forwarded to the
        pg pool factory to avoid hidden ``load_config()`` calls.
    project_key:
        Optional project key — pg service uses it for the per-row
        ``project_key`` column.
    sync_manager:
        Legacy sqlite-only argument. Ignored on pg.

    Returns
    -------
    object
        A :class:`pollypm.work.pg_service.PgWorkService` instance.
    """
    # Silence unused-arg lint for the legacy compatibility params.
    del db_path, project_path, sync_manager

    from pollypm.work.pg_service import PgWorkService

    return PgWorkService(config=config, project_key=project_key)


__all__ = ["create_work_service"]
