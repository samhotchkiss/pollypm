from pollypm.control_tui_routes import (
    ButtonRoute,
    focus_attr_for_tab,
    resolve_button_route,
    resolve_tab_shortcut,
)


def test_resolve_tab_shortcut_returns_registered_tab_ids() -> None:
    assert resolve_tab_shortcut("1") == "dashboard-tab"
    assert resolve_tab_shortcut("6") == "events-tab"
    assert resolve_tab_shortcut("x") is None


def test_resolve_button_route_returns_action_routes() -> None:
    assert resolve_button_route("accounts-remove") == ButtonRoute(
        action_name="action_remove_selected_account",
    )
    assert resolve_button_route("alerts-focus") == ButtonRoute(
        action_name="action_focus_alert_session",
    )


def test_resolve_button_route_handles_nav_buttons() -> None:
    assert resolve_button_route("nav-dashboard-tab") == ButtonRoute(tab_id="dashboard-tab")
    assert resolve_button_route("nav-alerts-tab") == ButtonRoute(tab_id="alerts-tab")
    assert resolve_button_route("unknown-button") is None


def test_focus_attr_for_tab_returns_table_attribute_names() -> None:
    assert focus_attr_for_tab("dashboard-tab") == "cockpit_table"
    assert focus_attr_for_tab("sessions-tab") == "sessions_table"
    assert focus_attr_for_tab("missing") is None
