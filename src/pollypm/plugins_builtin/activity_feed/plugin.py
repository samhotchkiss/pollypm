"""Live Activity Feed plugin — entrypoint.

lf01 scope:
    - Plugin skeleton + manifest + ``initialize(api)``.
    - Event projector (``handlers/event_projector.py``) that unifies
      events from the global state store with ``work_transitions`` in
      per-project work DBs.
    - ``activity_events`` SQL view over the global ``events`` table.

lf03 adds:
    - Rail item registration in the ``workflows`` section (index 30,
      label "Activity", icon "\u25cf") with a badge provider that
      counts unread events.
    - Textual widget + App (``cockpit/feed_panel.py``) and a plain-text
      renderer path for the cockpit static pane.

Later issues:
    - lf04 — filter UI + entry detail view.
    - lf05 — ``pm activity`` CLI with ``--follow``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pollypm.plugin_api.v1 import (
    Capability,
    PanelSpec,
    PollyPMPlugin,
    RailContext,
)
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


_LAST_SEEN_FILE_NAME = "activity_feed_last_seen"


def _last_seen_path(config: Any) -> Path | None:
    """Resolve the small cursor file tracking the rail's badge counter.

    Lives next to the state DB so it shares lifecycle (removed when the
    user wipes state). Returns ``None`` when no config is available.
    """
    if config is None:
        return None
    state_db = getattr(getattr(config, "project", None), "state_db", None)
    if state_db is None:
        return None
    return Path(state_db).parent / _LAST_SEEN_FILE_NAME


def _load_last_seen_id(config: Any) -> int | None:
    path = _last_seen_path(config)
    if path is None or not path.exists():
        return None
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _save_last_seen_id(config: Any, value: int) -> None:
    path = _last_seen_path(config)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(int(value)))
    except OSError:
        logger.debug("activity_feed: failed to persist last-seen cursor", exc_info=True)


def _badge_provider_factory(config: Any):
    """Return a ``badge_provider`` closure for rail registration.

    Counts events that arrived after the cursor file's saved id. When
    the count is zero the rail shows no badge.
    """

    def _provider(_ctx: RailContext) -> int | None:
        projector = build_projector(config)
        if projector is None:
            return None
        try:
            from pollypm.plugins_builtin.activity_feed.cockpit.feed_panel import (
                new_event_count,
            )
        except Exception:  # noqa: BLE001
            logger.debug("activity_feed: badge helper import failed", exc_info=True)
            return None
        last_seen = _load_last_seen_id(config)
        count = new_event_count(projector, last_seen)
        return count if count > 0 else None

    return _provider


def _state_provider(_ctx: RailContext) -> str:
    return "watch"


def _handler_factory(config: Any):
    """Rail handler — records the latest id as "seen" and delegates
    routing to the cockpit router (mirroring the core_rail_items
    pattern).
    """

    def _handler(ctx: RailContext) -> PanelSpec | None:
        router = ctx.extras.get("router")
        if router is not None:
            try:
                router.route_selected("activity")
            except Exception:  # noqa: BLE001
                logger.exception("activity_feed: route_selected raised")
        # Update the last-seen cursor so the badge resets.
        projector = build_projector(config)
        if projector is not None:
            try:
                entries = projector.project(limit=1)
            except Exception:  # noqa: BLE001
                entries = []
            if entries:
                newest = entries[0].id
                # Feed entries from the unified ``messages`` table carry
                # an ``msg:<id>`` prefix (#342). Accept that in addition
                # to the legacy ``evt:<id>`` shape some projectors may
                # still hand back.
                for prefix in ("msg:", "evt:"):
                    if newest.startswith(prefix):
                        try:
                            _save_last_seen_id(
                                config, int(newest.split(":", 1)[1]),
                            )
                        except ValueError:
                            pass
                        break
        return PanelSpec(widget=None, focus_hint="activity")

    return _handler


def _initialize(api: Any) -> None:
    """Install the activity_events view + register the rail item.

    The rail registration uses ``section="workflows"`` (non-reserved)
    so the plugin manifest does NOT need the reserved-section flag.
    Per spec §3 precedence: core_rail_items owns 0-99, plugins start at
    100 — we take index 30 in ``workflows`` which is open territory.
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
    # Rail registration — silent no-op if the host was built without a
    # rail registry (e.g. CLI-only environments / minimal test stubs).
    rail = None
    try:
        rail = api.rail
    except (RuntimeError, AttributeError):
        rail = None
    if rail is not None:
        rail.register_item(
            section="workflows",
            index=30,
            label="Activity",
            handler=_handler_factory(config),
            key="activity",
            icon="\u25cf",
            state_provider=_state_provider,
            badge_provider=_badge_provider_factory(config),
        )
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
