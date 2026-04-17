"""Live Activity Feed plugin — entrypoint.

lf01 scope:
    - Plugin skeleton + manifest + ``initialize(api)``.
    - Event projector (``handlers/event_projector.py``) that unifies
      events from the global state store with ``work_transitions`` in
      per-project work DBs.
    - ``activity_events`` SQL view over the global ``events`` table.

Later issues layer on:
    - lf02 — structured ``summary`` / ``severity`` at emission sites.
    - lf03 — cockpit panel widget + rail registration.
    - lf04 — filter UI + entry detail view.
    - lf05 — ``pm activity`` CLI with ``--follow``.

The ``initialize`` hook is intentionally lightweight — it just ensures
the ``activity_events`` view exists on the global DB so downstream
consumers (cockpit, CLI) don't each have to install it. Consumers
instantiate :class:`EventProjector` directly; no shared state lives on
the plugin host.
"""

from __future__ import annotations

import logging
from typing import Any

from pollypm.plugin_api.v1 import Capability, PollyPMPlugin
from pollypm.plugins_builtin.activity_feed.handlers.event_projector import (
    EventProjector,
    FeedEntry,
    ensure_activity_events_view,
)

logger = logging.getLogger(__name__)


def _collect_work_db_paths(config: Any) -> list[tuple[str, Any]]:
    """Build the list of (project_key, work_db_path) for the projector.

    Missing config, missing projects, or missing work DBs are tolerated
    — the projector checks path existence before querying.
    """
    result: list[tuple[str, Any]] = []
    if config is None:
        return result
    projects = getattr(config, "projects", None) or {}
    for key, project in projects.items():
        project_path = getattr(project, "path", None)
        if project_path is None:
            continue
        result.append((str(key), project_path / ".pollypm" / "state.db"))
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
    return EventProjector(state_db, _collect_work_db_paths(config))


def _initialize(api: Any) -> None:
    """Ensure the ``activity_events`` view exists and stash a factory
    the cockpit can use to build projectors.

    We don't register a rail item here — that lives in lf03, guarded
    by a Textual-available check. lf01 keeps the plugin load-safe in
    minimal environments (no Textual, no cockpit).
    """
    config = api.config
    state_db = None
    if config is not None:
        state_db = getattr(getattr(config, "project", None), "state_db", None)
    if state_db is not None:
        try:
            import sqlite3

            conn = sqlite3.connect(str(state_db), check_same_thread=False)
            try:
                ensure_activity_events_view(conn)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            # Observability plugin — never brick the rail. Surface via
            # logger and degraded-plugins machinery if it keeps failing.
            logger.exception("activity_feed: failed to install activity_events view")
    api.emit_event("loaded", {"state_db": str(state_db) if state_db else None})


plugin = PollyPMPlugin(
    name="activity_feed",
    version="0.1.0",
    description=(
        "Live Activity Feed — unified projection of session / task / "
        "worker / inbox / heartbeat events into a reverse-chronological "
        "feed visible in the cockpit and via `pm activity`."
    ),
    capabilities=(
        Capability(kind="hook", name="activity_feed.initialize"),
    ),
    initialize=_initialize,
)


__all__ = [
    "plugin",
    "build_projector",
    "EventProjector",
    "FeedEntry",
]
