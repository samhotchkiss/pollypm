"""Project-name header and worker-presence indicator (#403)."""

from __future__ import annotations

from pollypm.cockpit_sections.base import (
    _DASHBOARD_BULLET,
    _DASHBOARD_DIVIDER_WIDTH,
    _iso_to_dt,
)


def _section_header(name: str, presence: str) -> str:
    """Project-name header with worker presence in the right gutter."""
    gutter = presence or ""
    # Pad the name so ``presence`` sits at the right edge of the divider.
    total_width = _DASHBOARD_DIVIDER_WIDTH
    # account for 2-space indent; name on the left, presence on the right
    pad = max(1, total_width - len(name) - len(gutter) - 2)
    return f"{_DASHBOARD_BULLET}{name}{' ' * pad}{gutter}"


def _worker_presence(supervisor, project_key: str) -> str:
    """Render the header right-gutter: ``\u25cf worker alive`` / ``\u25cb worker idle`` / ``\u2013 none``.

    A worker is "alive" when at least one planned session for this project
    has a recent heartbeat. "idle" means we know about the session but the
    heartbeat is stale / absent. "none" means no worker session is planned
    for this project at all.
    """
    from datetime import UTC, datetime, timedelta

    try:
        launches = list(supervisor.plan_launches())
    except Exception:  # noqa: BLE001
        return "\u2013 no supervisor"

    session_names = [
        l.session.name for l in launches
        if getattr(l.session, "project", None) == project_key
        and getattr(l.session, "role", "") != "operator-pm"
    ]
    if not session_names:
        return "\u2013 no worker"

    alive_cutoff = datetime.now(UTC) - timedelta(minutes=5)
    for name in session_names:
        try:
            hb = supervisor.store.latest_heartbeat(name)
        except Exception:  # noqa: BLE001
            continue
        if hb is None:
            continue
        dt = _iso_to_dt(hb.created_at)
        if dt is not None and dt > alive_cutoff and not hb.pane_dead:
            return "\u25cf worker alive"
    return "\u25cb worker idle"
