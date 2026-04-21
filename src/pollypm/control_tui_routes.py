"""Pure routing tables for the legacy control TUI.

Contract:
- Inputs: keyboard shortcuts, button ids, and tab ids emitted by the
  Textual control-room surface.
- Outputs: immutable route specs and attribute names the app can
  dispatch through.
- Side effects: none.
- Invariants: tab/button routing stays centralized here so adding a new
  tab or action does not require editing scattered literal maps.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ButtonRoute:
    action_name: str | None = None
    tab_id: str | None = None


_TAB_SHORTCUTS: dict[str, str] = {
    "1": "dashboard-tab",
    "2": "accounts-tab",
    "3": "projects-tab",
    "4": "sessions-tab",
    "5": "alerts-tab",
    "6": "events-tab",
}

_TAB_FOCUS_ATTRS: dict[str, str] = {
    "dashboard-tab": "cockpit_table",
    "accounts-tab": "accounts_table",
    "projects-tab": "projects_table",
    "sessions-tab": "sessions_table",
    "alerts-tab": "alerts_table",
    "events-tab": "events_table",
}

_BUTTON_ROUTES: dict[str, ButtonRoute] = {
    "dashboard-open": ButtonRoute(action_name="action_open_selected_session"),
    "dashboard-ensure": ButtonRoute(action_name="action_ensure_pollypm"),
    "dashboard-heartbeat": ButtonRoute(action_name="action_run_heartbeat"),
    "dashboard-permissions": ButtonRoute(action_name="action_toggle_open_permissions"),
    "accounts-add-codex": ButtonRoute(action_name="action_add_codex_account"),
    "accounts-add-claude": ButtonRoute(action_name="action_add_claude_account"),
    "accounts-usage": ButtonRoute(action_name="action_refresh_selected_account_usage"),
    "accounts-relogin": ButtonRoute(action_name="action_relogin_selected_account"),
    "accounts-switch-operator": ButtonRoute(action_name="action_switch_operator"),
    "accounts-controller": ButtonRoute(action_name="action_make_controller"),
    "accounts-failover": ButtonRoute(action_name="action_toggle_failover"),
    "accounts-remove": ButtonRoute(action_name="action_remove_selected_account"),
    "projects-scan": ButtonRoute(action_name="action_scan_projects"),
    "projects-add": ButtonRoute(action_name="action_add_project"),
    "projects-tracker": ButtonRoute(action_name="action_init_project_tracker"),
    "projects-root": ButtonRoute(action_name="action_set_workspace_root"),
    "projects-worker": ButtonRoute(action_name="action_new_worker"),
    "projects-remove": ButtonRoute(action_name="action_remove_selected_project"),
    "sessions-open": ButtonRoute(action_name="action_open_selected_session"),
    "sessions-send": ButtonRoute(action_name="action_send_input_selected"),
    "sessions-claim": ButtonRoute(action_name="action_claim_selected_session"),
    "sessions-release": ButtonRoute(action_name="action_release_selected_session"),
    "sessions-stop": ButtonRoute(action_name="action_stop_selected_session"),
    "sessions-remove": ButtonRoute(action_name="action_remove_selected_session"),
    "alerts-focus": ButtonRoute(action_name="action_focus_alert_session"),
}


def resolve_tab_shortcut(key: str) -> str | None:
    return _TAB_SHORTCUTS.get(key)


def focus_attr_for_tab(tab_id: str) -> str | None:
    return _TAB_FOCUS_ATTRS.get(tab_id)


def resolve_button_route(button_id: str) -> ButtonRoute | None:
    if button_id.startswith("nav-"):
        return ButtonRoute(tab_id=button_id.removeprefix("nav-"))
    return _BUTTON_ROUTES.get(button_id)
