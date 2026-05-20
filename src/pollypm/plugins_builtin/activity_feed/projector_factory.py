"""Construct :class:`EventProjector` instances from a host config.

Extracted from ``plugin.py`` to break the import cycle between the
plugin entrypoint and ``cockpit/feed_panel.py``: the panel needs
``build_projector`` at module import time, while the plugin needs the
panel's badge helpers — moving the factory here lets both sides import
from a leaf module without lazy imports (#1367).
"""

from __future__ import annotations

from typing import Any

from pollypm.plugins_builtin.activity_feed.handlers.event_projector import (
    EventProjector,
)


def _collect_work_db_paths(config: Any) -> list[tuple[str, Any]]:
    """Build the list of (project_key, work_db_path) for the projector.

    Missing config, missing projects, or missing work DBs are tolerated
    — the projector checks path existence before querying.
    """
    result: list[tuple[str, Any]] = []
    if config is None:
        return result
    from pollypm.projects import project_state_db_path

    projects = getattr(config, "projects", None) or {}
    for key, project in projects.items():
        project_path = getattr(project, "path", None)
        if project_path is None:
            continue
        result.append((str(key), project_state_db_path(project_path)))
    return result


def build_projector(config: Any) -> EventProjector | None:
    """Construct an :class:`EventProjector` wired to the active config.

    Returns ``None`` if no state DB is configured (typical in test
    harnesses with stub configs). Callers treat ``None`` as "no feed
    available" and show an empty panel.
    """
    if config is None:
        return None
    state_db = getattr(getattr(config, "project", None), "state_db", None)
    if state_db is None:
        return None
    # #1816: thread the config through so the projector can detect a
    # pg backend and route state-store reads to the pg-backed Store
    # instead of the (possibly stale) sqlite file at ``state_db``.
    return EventProjector(
        state_db,
        _collect_work_db_paths(config),
        config=config,
    )


__all__ = ["build_projector"]
