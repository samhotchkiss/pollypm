"""Pure route-resolution helpers for cockpit rail selections.

Contract:
- Inputs: left-rail selection keys such as ``polly``, ``activity:demo``,
  or ``project:demo:task:7``.
- Outputs: small immutable route specs that describe how the router
  should handle the selection.
- Side effects: none.
- Invariants: static/live route tables stay centralized here so new
  cockpit destinations do not require copy-paste branches in the tmux
  router.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LiveSessionRoute:
    session_name: str
    fallback_kind: str = "polly"


@dataclass(frozen=True, slots=True)
class StaticViewRoute:
    kind: str
    project_key: str | None = None
    selected_key: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectRoute:
    project_key: str
    sub_view: str | None
    task_num: str | None = None


_LIVE_SESSION_ROUTES: dict[str, LiveSessionRoute] = {
    "polly": LiveSessionRoute(session_name="operator"),
    "russell": LiveSessionRoute(session_name="reviewer"),
}

_STATIC_VIEW_ROUTES: dict[str, StaticViewRoute] = {
    "inbox": StaticViewRoute(kind="inbox", selected_key="inbox"),
    "workers": StaticViewRoute(kind="workers", selected_key="workers"),
    "metrics": StaticViewRoute(kind="metrics", selected_key="metrics"),
    "settings": StaticViewRoute(kind="settings", selected_key="settings"),
}


def resolve_live_session_route(key: str) -> LiveSessionRoute | None:
    """Return the registered live-session route for ``key`` if one exists."""
    return _LIVE_SESSION_ROUTES.get(key)


def resolve_static_view_route(key: str) -> StaticViewRoute | None:
    """Return the registered static-view route for ``key`` if one exists."""
    route = _STATIC_VIEW_ROUTES.get(key)
    if route is not None:
        return route
    if key == "activity" or key.startswith("activity:"):
        _, _, project_key = key.partition(":")
        return StaticViewRoute(
            kind="activity",
            project_key=project_key or None,
            selected_key=key,
        )
    return None


def resolve_project_route(key: str) -> ProjectRoute | None:
    """Parse ``project:...`` selections into a structured route."""
    if not key.startswith("project:"):
        return None
    parts = key.split(":")
    if len(parts) < 2 or not parts[1]:
        return None
    project_key = parts[1]
    sub_view = parts[2] if len(parts) > 2 else None
    task_num = parts[3] if sub_view == "task" and len(parts) > 3 else None
    return ProjectRoute(project_key=project_key, sub_view=sub_view, task_num=task_num)
