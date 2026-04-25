from pollypm.cockpit_rail_routes import (
    LiveSessionRoute,
    ProjectRoute,
    StaticViewRoute,
    resolve_live_session_route,
    resolve_project_route,
    resolve_static_view_route,
)


def test_resolve_live_session_route_uses_registry() -> None:
    assert resolve_live_session_route("polly") == LiveSessionRoute(session_name="operator")
    assert resolve_live_session_route("russell") == LiveSessionRoute(session_name="reviewer")
    assert resolve_live_session_route("settings") is None


def test_resolve_static_view_route_supports_registered_and_activity_views() -> None:
    assert resolve_static_view_route("settings") == StaticViewRoute(
        kind="settings",
        project_key=None,
        selected_key="settings",
    )
    assert resolve_static_view_route("activity:demo") == StaticViewRoute(
        kind="activity",
        project_key="demo",
        selected_key="activity:demo",
    )


def test_resolve_project_route_parses_dashboard_and_task_routes() -> None:
    assert resolve_project_route("project:demo") == ProjectRoute(
        project_key="demo",
        sub_view=None,
        task_num=None,
    )
    assert resolve_project_route("project:demo:task:7") == ProjectRoute(
        project_key="demo",
        sub_view="task",
        task_num="7",
    )
    assert resolve_project_route("project:demo:issues:task:7") == ProjectRoute(
        project_key="demo",
        sub_view="issues",
        task_num="7",
    )
    assert resolve_project_route("settings") is None


def test_resolve_static_view_route_parses_scoped_inbox() -> None:
    """#751: ``inbox:<project_key>`` scopes the inbox pane to that
    project on mount. Same pattern as activity:<project_key>."""
    route = resolve_static_view_route("inbox:polly_remote")
    assert route == StaticViewRoute(
        kind="inbox",
        project_key="polly_remote",
        selected_key="inbox",
    )


def test_resolve_static_view_route_bare_inbox_stays_global() -> None:
    """Bare 'inbox' returns the default (global) route with no scope."""
    route = resolve_static_view_route("inbox")
    assert route is not None
    assert route.kind == "inbox"
    assert route.project_key is None
