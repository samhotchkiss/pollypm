from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import re
import subprocess
from typing import Callable

from rich.columns import Columns
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual import events
from textual.screen import ModalScreen
from textual.worker import Worker, WorkerState
from textual.widgets import Button, DataTable, Header, Input, Static, TabbedContent, TabPane

from pollypm.accounts import (
    AccountStatus,
)
from pollypm.config import DEFAULT_CONFIG_PATH, load_config
from pollypm.messaging import list_open_messages
from pollypm.models import ProviderKind
from pollypm.projects import (
    discover_git_repositories,
    enable_tracked_project,
    project_issues_dir,
    register_project,
    remove_project,
    set_workspace_root,
)
from pollypm.service_api import PollyPMService
from pollypm.supervisor import Supervisor
from pollypm.task_backends import get_task_backend
from pollypm.worktrees import list_worktrees


@dataclass(slots=True)
class InputRequest:
    title: str
    prompt: str
    placeholder: str = ""
    value: str = ""
    button_label: str = "Save"


@dataclass(slots=True)
class ConfirmRequest:
    title: str
    prompt: str
    confirm_label: str = "Confirm"
    cancel_label: str = "Cancel"


@dataclass(slots=True)
class DetailResult:
    request_id: int
    tab_id: str
    content: object


class InputModal(ModalScreen[str | None]):
    CSS = """
    Screen {
        align: center middle;
    }
    #dialog {
        width: 70;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: heavy $accent;
    }
    #dialog-title {
        padding-bottom: 1;
        text-style: bold;
    }
    #dialog-prompt {
        padding-bottom: 1;
    }
    #dialog-input {
        margin-bottom: 1;
    }
    #dialog-buttons {
        height: auto;
        align-horizontal: right;
    }
    #dialog-buttons Button {
        margin-left: 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, request: InputRequest) -> None:
        super().__init__()
        self.request = request

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(self.request.title, id="dialog-title")
            yield Static(self.request.prompt, id="dialog-prompt")
            yield Input(value=self.request.value, placeholder=self.request.placeholder, id="dialog-input")
            with Horizontal(id="dialog-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(self.request.button_label, variant="primary", id="submit")

    def on_mount(self) -> None:
        self.query_one("#dialog-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "submit":
            self.dismiss(self.query_one("#dialog-input", Input).value.strip())
            return
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmModal(ModalScreen[bool]):
    CSS = """
    Screen {
        align: center middle;
    }
    #confirm {
        width: 72;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: heavy $warning;
    }
    #confirm-title {
        padding-bottom: 1;
        text-style: bold;
    }
    #confirm-buttons {
        height: auto;
        align-horizontal: right;
        padding-top: 1;
    }
    #confirm-buttons Button {
        margin-left: 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, request: ConfirmRequest) -> None:
        super().__init__()
        self.request = request

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm"):
            yield Static(self.request.title, id="confirm-title")
            yield Static(self.request.prompt)
            with Horizontal(id="confirm-buttons"):
                yield Button(self.request.cancel_label, id="cancel")
                yield Button(self.request.confirm_label, variant="primary", id="confirm")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)


class RepoScanModal(ModalScreen[tuple[str, list[Path]]]):
    CSS = """
    Screen {
        align: center middle;
    }
    #scan {
        width: 110;
        height: 32;
        padding: 1;
        background: $panel;
        border: heavy $accent;
    }
    #scan-info {
        height: 3;
        padding: 0 1;
    }
    #scan-table {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("escape,q", "cancel", "Close"),
        Binding("enter", "add_selected", "Add Selected"),
        Binding("a", "add_all", "Add All"),
    ]

    def __init__(self, repos: list[Path]) -> None:
        super().__init__()
        self.repos = repos
        self._dismissed = False
        self.info = Static("", id="scan-info")
        self.table = DataTable(id="scan-table")

    def compose(self) -> ComposeResult:
        with Vertical(id="scan"):
            yield Static("Discovered git repositories", id="scan-title")
            yield self.info
            yield self.table

    def on_mount(self) -> None:
        self.table.cursor_type = "row"
        self.table.add_columns("Name", "Path")
        for repo in self.repos:
            self.table.add_row(repo.name, str(repo), key=str(repo))
        self.info.update(
            "Enter adds the selected repo. A adds every discovered repo. Esc closes without changes."
        )
        self.table.focus()

    def _selected_path(self) -> Path | None:
        if self.table.row_count == 0 or self.table.cursor_row < 0:
            return None
        return Path(str(self.table.get_row_at(self.table.cursor_row)[1]))

    def _safe_dismiss(self, result: tuple[str, list[Path]]) -> None:
        if self._dismissed:
            return
        self._dismissed = True
        self.dismiss(result)

    def action_add_selected(self) -> None:
        selected = self._selected_path()
        if selected is None:
            self._safe_dismiss(("cancel", []))
            return
        self._safe_dismiss(("selected", [selected]))

    def action_add_all(self) -> None:
        self._safe_dismiss(("all", list(self.repos)))

    def action_cancel(self) -> None:
        self._safe_dismiss(("cancel", []))

    def on_data_table_row_selected(self, _event: DataTable.RowSelected) -> None:
        self.action_add_selected()


class PollyPMApp(App[None]):
    TITLE = "PollyPM"
    SUB_TITLE = "Control Room"
    NAV_TABS = [
        ("dashboard-tab", "Dashboard"),
        ("accounts-tab", "Accounts"),
        ("projects-tab", "Projects"),
        ("sessions-tab", "Sessions"),
        ("alerts-tab", "Alerts"),
        ("events-tab", "Events"),
    ]
    USAGE_REFRESH_INTERVAL = timedelta(minutes=20)
    STATUS_REFRESH_INTERVAL = timedelta(seconds=20)
    SESSION_PREVIEW_REFRESH_INTERVAL = timedelta(seconds=3)
    UI_REFRESH_INTERVAL_SECONDS = 8

    CSS = """
    Screen {
        layout: vertical;
        background: #0f1317;
        color: #edf2f4;
    }
    #hero {
        height: 6;
        padding: 0 1;
        background: #141a20;
        border-bottom: solid #2a323a;
    }
    #status {
        height: 1;
        padding: 0 1;
        background: #161d24;
        color: #d6dde2;
    }
    #message {
        height: 1;
        padding: 0 1;
        background: #1d252d;
        color: #f5d96b;
    }
    #help {
        height: 1;
        padding: 0 1;
        background: #12181e;
        color: #aeb8bf;
    }
    #tabs {
        height: 1fr;
        padding: 0 1 1 1;
    }
    .tab-layout {
        height: 1fr;
    }
    #cockpit-layout {
        height: 1fr;
    }
    #cockpit-nav {
        width: 34;
        min-width: 24;
        height: 1fr;
        border: round #34404a;
        background: #11161b;
    }
    #cockpit-pane {
        width: 1fr;
        height: 1fr;
        border: round #566574;
        background: #12181e;
        padding: 0 1 1 1;
    }
    #cockpit-body {
        height: 1fr;
    }
    .left-table {
        width: 2fr;
        height: 1fr;
        border: round #34404a;
        background: #11161b;
    }
    .right-pane {
        width: 3fr;
        height: 1fr;
        border: round #566574;
        padding: 0 1 1 1;
        background: #12181e;
    }
    .detail {
        height: 1fr;
    }
    .detail-title {
        height: 1;
        text-style: bold;
        color: #f3f4f6;
        padding: 1 0 0 0;
    }
    #dashboard-body {
        padding: 1;
        background: #10161b;
        border: round #3a4550;
    }
    .action-bar {
        height: auto;
        padding: 0 0 1 0;
    }
    .action-bar Button {
        margin-right: 1;
        min-width: 14;
    }
    TabPane {
        padding: 1 0 0 0;
    }
    TabbedContent Tabs {
        display: none;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("f", "refresh_all", "Refresh"),
        Binding("h", "run_heartbeat", "Heartbeat"),
        Binding("u", "ensure_pollypm", "Ensure Session"),
        Binding("b", "toggle_open_permissions", "Toggle Permissions"),
        Binding("1", "show_dashboard", priority=True, show=False),
        Binding("2", "show_accounts", priority=True, show=False),
        Binding("3", "show_projects", priority=True, show=False),
        Binding("4", "show_sessions", priority=True, show=False),
        Binding("5", "show_alerts", priority=True, show=False),
        Binding("6", "show_events", priority=True, show=False),
        Binding("c", "add_codex_account", "Add Codex"),
        Binding("l", "add_claude_account", "Add Claude"),
        Binding("y", "refresh_selected_account_usage", "Refresh Usage"),
        Binding("r", "context_action_r", "Relogin/Release"),
        Binding("j", "switch_operator", "Switch Operator"),
        Binding("x", "context_action_x", "Remove/Delete"),
        Binding("m", "make_controller", "Set Controller"),
        Binding("v", "toggle_failover", "Toggle Failover"),
        Binding("s", "scan_projects", "Scan Repos"),
        Binding("a", "add_project", "Add Project"),
        Binding("t", "init_project_tracker", "Init Tracker"),
        Binding("w", "set_workspace_root", "Workspace Root"),
        Binding("n", "new_worker", "New Worker"),
        Binding("o", "open_selected_session", "Open Window"),
        Binding("k", "stop_selected_session", "Stop Session"),
        Binding("i", "send_input_selected", "Send Input"),
        Binding("g", "focus_alert_session", "Focus Alert"),
        Binding("p", "claim_selected_session", "Claim"),
    ]

    def __init__(self, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
        super().__init__()
        self.config_path = config_path
        self.service = PollyPMService(config_path)
        self.pending_usage_refreshes: dict[str, datetime] = {}
        self.account_statuses: list[AccountStatus] = []
        self.account_statuses_updated_at: datetime | None = None
        self.session_preview_cache: tuple[str, str, datetime] | None = None
        self.detail_request_id = 0
        self.last_detail_selection: tuple[str, object | None] | None = None
        self.table_snapshots: dict[str, list[tuple[tuple[str, ...], str]]] = {}
        self.notice_text: str | None = None
        self.notice_until: datetime | None = None
        self.hero_bar = Static("", id="hero")
        self.status_bar = Static("", id="status")
        self.message_bar = Static("", id="message")
        self.help_bar = Static("", id="help")
        self.dashboard = Static("", id="cockpit-body")
        self.cockpit_table = DataTable(id="cockpit-nav")
        self.accounts_table = DataTable(id="accounts-table", classes="left-table")
        self.accounts_detail = Static("", classes="detail")
        self.projects_table = DataTable(id="projects-table", classes="left-table")
        self.projects_detail = Static("", classes="detail")
        self.sessions_table = DataTable(id="sessions-table", classes="left-table")
        self.sessions_detail = Static("", classes="detail")
        self.alerts_table = DataTable(id="alerts-table", classes="left-table")
        self.alerts_detail = Static("", classes="detail")
        self.events_table = DataTable(id="events-table", classes="left-table")
        self.events_detail = Static("", classes="detail")
        self.spinner_index = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield self.hero_bar
        yield self.status_bar
        yield self.message_bar
        with TabbedContent(id="tabs"):
            with TabPane("Dashboard", id="dashboard-tab"):
                with Horizontal(classes="action-bar"):
                    yield Button("Go", id="dashboard-open")
                    yield Button("Heartbeat", id="dashboard-heartbeat")
                    yield Button("Permissions", id="dashboard-permissions")
                with Horizontal(id="cockpit-layout"):
                    yield self.cockpit_table
                    with Vertical(id="cockpit-pane"):
                        yield Static("Live View", classes="detail-title")
                        yield self.dashboard
            with TabPane("Accounts", id="accounts-tab"):
                with Vertical():
                    with Horizontal(classes="action-bar"):
                        yield Button("Codex", id="accounts-add-codex")
                        yield Button("Claude", id="accounts-add-claude")
                        yield Button("Usage", id="accounts-usage")
                        yield Button("Relogin", id="accounts-relogin")
                        yield Button("Operator", id="accounts-switch-operator")
                        yield Button("Controller", id="accounts-controller")
                        yield Button("Failover", id="accounts-failover")
                        yield Button("Remove", id="accounts-remove")
                    with Horizontal(classes="tab-layout"):
                        yield self.accounts_table
                        with Vertical(classes="right-pane"):
                            yield Static("Account Details", classes="detail-title")
                            yield self.accounts_detail
            with TabPane("Projects", id="projects-tab"):
                with Vertical():
                    with Horizontal(classes="action-bar"):
                        yield Button("Scan", id="projects-scan")
                        yield Button("Add", id="projects-add")
                        yield Button("Tracker", id="projects-tracker")
                        yield Button("Workspace", id="projects-root")
                        yield Button("Worker", id="projects-worker")
                        yield Button("Remove", id="projects-remove")
                    with Horizontal(classes="tab-layout"):
                        yield self.projects_table
                        with Vertical(classes="right-pane"):
                            yield Static("Project Details", classes="detail-title")
                            yield self.projects_detail
            with TabPane("Sessions", id="sessions-tab"):
                with Vertical():
                    with Horizontal(classes="action-bar"):
                        yield Button("Open", id="sessions-open")
                        yield Button("Send", id="sessions-send")
                        yield Button("Claim", id="sessions-claim")
                        yield Button("Release", id="sessions-release")
                        yield Button("Stop", id="sessions-stop")
                        yield Button("Remove", id="sessions-remove")
                    with Horizontal(classes="tab-layout"):
                        yield self.sessions_table
                        with Vertical(classes="right-pane"):
                            yield Static("Session Preview", classes="detail-title")
                            yield self.sessions_detail
            with TabPane("Alerts", id="alerts-tab"):
                with Vertical():
                    with Horizontal(classes="action-bar"):
                        yield Button("Focus Session", id="alerts-focus")
                    with Horizontal(classes="tab-layout"):
                        yield self.alerts_table
                        with Vertical(classes="right-pane"):
                            yield Static("Alert Details", classes="detail-title")
                            yield self.alerts_detail
            with TabPane("Events", id="events-tab"):
                with Horizontal(classes="tab-layout"):
                    yield self.events_table
                    with Vertical(classes="right-pane"):
                        yield Static("Recent Event", classes="detail-title")
                        yield self.events_detail
        yield self.help_bar

    def on_mount(self) -> None:
        self._init_tables()
        self._refresh_view(force=True)
        self.cockpit_table.focus()
        self.set_interval(self.UI_REFRESH_INTERVAL_SECONDS, self._tick_refresh)
        self.set_interval(0.6, self._tick_spinner)

    def _tick_spinner(self) -> None:
        self.spinner_index = (self.spinner_index + 1) % 4
        if self._active_tab() == "dashboard-tab":
            self._refresh_view(force=False)

    def _tick_refresh(self) -> None:
        self._refresh_view(force=False)

    def _init_tables(self) -> None:
        self.cockpit_table.cursor_type = "row"
        self.cockpit_table.zebra_stripes = True
        self.cockpit_table.add_columns("Item", "State")

        self.accounts_table.cursor_type = "row"
        self.accounts_table.zebra_stripes = True
        self.accounts_table.add_columns("Key", "Email", "Provider", "Login", "Ctrl", "FO", "Usage")

        self.projects_table.cursor_type = "row"
        self.projects_table.zebra_stripes = True
        self.projects_table.add_columns("Key", "Name", "Kind", "Tracked", "Path")

        self.sessions_table.cursor_type = "row"
        self.sessions_table.zebra_stripes = True
        self.sessions_table.add_columns(
            "Name", "Role", "Project", "Account", "State", "Lease", "!"
        )

        self.alerts_table.cursor_type = "row"
        self.alerts_table.zebra_stripes = True
        self.alerts_table.add_columns("Session", "Type", "Severity", "Message")

        self.events_table.cursor_type = "row"
        self.events_table.zebra_stripes = True
        self.events_table.add_columns("When", "Session", "Type", "Message")

    def _load_context(self) -> tuple[Supervisor | None, object | None]:
        try:
            config = load_config(self.config_path)
        except FileNotFoundError:
            return None, None
        supervisor = Supervisor(config)
        supervisor.ensure_layout()
        return supervisor, config

    def _refresh_view(self, *, force: bool) -> None:
        supervisor, config = self._load_context()
        if supervisor is None or config is None:
            self.hero_bar.update(
                Panel(
                    "PollyPM is not configured yet.\nRun onboarding to connect an agent and bring the control room online.",
                    title="PollyPM",
                    border_style="#566574",
                )
            )
            self.status_bar.update(f"Config not found at {self.config_path}. Run onboarding first.")
            self.dashboard.update("PollyPM is not configured yet.")
            return

        self._ensure_account_statuses(force=force)
        launches, windows, alerts, leases, errors = supervisor.status()
        self._refresh_dashboard(supervisor, launches, windows, alerts, leases, errors)
        self._refresh_cockpit(supervisor, config, launches, windows, alerts, leases)
        self._refresh_accounts(supervisor)
        self._refresh_projects(config)
        self._refresh_sessions(supervisor, launches, windows, alerts, leases)
        self._refresh_alerts(alerts)
        self._refresh_events(supervisor)
        self._refresh_details(show_loading=False)
        self._refresh_usage_due_in_background()

    def action_refresh_all(self) -> None:
        self._refresh_view(force=True)

    def _ensure_account_statuses(self, *, force: bool) -> None:
        now = datetime.now()
        if (
            not force
            and self.account_statuses_updated_at is not None
            and now - self.account_statuses_updated_at < self.STATUS_REFRESH_INTERVAL
        ):
            return
        self.account_statuses = self.service.list_account_statuses()
        self.account_statuses_updated_at = now

    def _refresh_usage_due_in_background(self) -> None:
        for account in self.account_statuses:
            pending_started = self.pending_usage_refreshes.get(account.key)
            if pending_started is not None:
                if account.usage_updated_at:
                    self.pending_usage_refreshes.pop(account.key, None)
                    continue
                if datetime.now() - pending_started < timedelta(minutes=3):
                    continue
                self.pending_usage_refreshes.pop(account.key, None)
                continue
            if not account.logged_in:
                continue
            if account.usage_updated_at:
                try:
                    updated = datetime.fromisoformat(account.usage_updated_at)
                except ValueError:
                    updated = None
                if updated is not None and datetime.now(updated.tzinfo) - updated < self.USAGE_REFRESH_INTERVAL:
                    continue
            self._spawn_usage_refresh(account.key, notify=False)

    def _refresh_dashboard(self, supervisor: Supervisor, launches, windows, alerts, leases, errors) -> None:
        session_name = supervisor.config.project.tmux_session
        tmux_exists = supervisor.tmux.has_session(session_name)
        controller = supervisor.config.pollypm.controller_account
        controller_label = supervisor.config.accounts[controller].email or controller
        operator_session = supervisor._effective_session(supervisor.config.sessions["operator"])
        heartbeat_session = supervisor._effective_session(supervisor.config.sessions["heartbeat"])
        operator_label = supervisor.config.accounts[operator_session.account].email or operator_session.account
        heartbeat_label = supervisor.config.accounts[heartbeat_session.account].email or heartbeat_session.account
        failover = ", ".join(supervisor.config.pollypm.failover_accounts) or "none"
        console_running = supervisor.console_window_name() in {window.name for window in windows}
        self.hero_bar.update(self._hero_render(supervisor, launches, windows, alerts, leases))
        self.status_bar.update(
            f"tmux={session_name} running={'yes' if tmux_exists else 'no'}  "
            f"controller={controller_label}  operator={operator_label}  heartbeat={heartbeat_label}  "
            f"console={'yes' if console_running else 'no'}  "
            f"alerts={len(alerts)}  leases={len(leases)}"
        )
        self._update_context_bar()

    def _refresh_cockpit(self, supervisor: Supervisor, config, launches, windows, alerts, leases) -> None:
        rows: list[tuple[tuple[str, ...], str]] = []
        rows.append((("Polly", self._cockpit_state_for_session("operator", launches, windows, alerts)), "polly"))
        inbox_count = len(list_open_messages(config.project.root_dir))
        rows.append(((f"Inbox ({inbox_count})", "mail" if inbox_count else "clear"), "inbox"))
        rows.append((("Projects", "browse"), "section:projects"))

        project_session_map = self._project_session_map(launches)
        for project_key, project in config.projects.items():
            session_name = project_session_map.get(project_key)
            label = project.display_label()
            state = self._cockpit_state_for_session(session_name, launches, windows, alerts) if session_name else "idle"
            rows.append(((label, state), f"project:{project_key}"))

        rows.append((("System", "tools"), "section:system"))
        rows.append((("Settings", "config"), "settings"))
        self._replace_table_rows(self.cockpit_table, rows)

    def _project_session_map(self, launches) -> dict[str, str]:
        project_session_map: dict[str, str] = {}
        for launch in launches:
            if launch.session.role in {"operator-pm", "heartbeat-supervisor"}:
                continue
            if launch.session.role == "worker":
                project_session_map.setdefault(launch.session.project, launch.session.name)
            else:
                project_session_map.setdefault(launch.session.project, launch.session.name)
        return project_session_map

    def _cockpit_state_for_session(self, session_name: str | None, launches, windows, alerts) -> str:
        if session_name is None:
            return "idle"
        alert_count = sum(1 for alert in alerts if alert.session_name == session_name)
        if alert_count:
            return f"! {alert_count}"
        window_map = {window.name: window for window in windows}
        launch = next((item for item in launches if item.session.name == session_name), None)
        if launch is None:
            return "idle"
        window = window_map.get(launch.window_name)
        if window is None:
            return "idle"
        if window.pane_dead:
            return "dead"
        if launch.session.role == "worker":
            return self._spinner_frame() + " live"
        if launch.session.role == "operator-pm":
            return "ready"
        if launch.session.role == "heartbeat-supervisor":
            return "watch"
        return "live"

    def _spinner_frame(self) -> str:
        return ["·", "•", "●", "•"][self.spinner_index % 4]

    def _hero_render(self, supervisor: Supervisor, launches, windows, alerts, leases) -> object:
        healthy_accounts = sum(1 for account in self.account_statuses if account.logged_in and account.health == "healthy")
        active_windows = sum(1 for window in windows if not window.pane_dead)
        left = Text()
        left.append("██████   ██████  ██      ██   ██    ██\n", style="bold #f2f4f8")
        left.append("██   ██ ██    ██ ██      ██    ██  ██\n", style="bold #f2f4f8")
        left.append("██████  ██    ██ ██      ██      ██\n", style="bold #f2f4f8")
        left.append("██      ██    ██ ██      ██    ██  ██\n", style="bold #f2f4f8")
        left.append("██       ██████  ███████ ██████    ██\n", style="bold #f2f4f8")
        left.append("PollyPM control room for live Claude CLI and Codex CLI sessions.", style="#b8c2cc")

        metrics = [
            self._metric_panel("Accounts", f"{healthy_accounts}/{len(self.account_statuses)} healthy", "#2d6a4f"),
            self._metric_panel("Projects", str(len(supervisor.config.projects)), "#355070"),
            self._metric_panel("Sessions", f"{len(launches)} planned · {active_windows} live", "#6d597a"),
            self._metric_panel("Alerts", str(len(alerts)), "#8d3b3b" if alerts else "#355f4a"),
        ]

        rail = Table.grid(expand=True)
        rail.add_column(ratio=2)
        rail.add_column(ratio=3)
        rail.add_row(
            left,
            Columns(metrics, expand=True, equal=True),
        )
        operator_session = supervisor._effective_session(supervisor.config.sessions["operator"])
        heartbeat_session = supervisor._effective_session(supervisor.config.sessions["heartbeat"])
        operator_label = supervisor.config.accounts[operator_session.account].email or operator_session.account
        heartbeat_label = supervisor.config.accounts[heartbeat_session.account].email or heartbeat_session.account
        return Panel(
            rail,
            border_style="#566574",
            subtitle=(
                f"Controller: {supervisor.config.accounts[supervisor.config.pollypm.controller_account].email or supervisor.config.pollypm.controller_account}"
                f" · Operator: {operator_label}"
                f" · Heartbeat: {heartbeat_label}"
            ),
        )

    def _metric_panel(self, title: str, value: str, border_style: str) -> Panel:
        text = Text(justify="center")
        text.append(f"{value}\n", style="bold white")
        text.append(title, style="#c7d0d6")
        return Panel(text, border_style=border_style, padding=(0, 1))

    def _update_context_bar(self) -> None:
        active = self._active_tab()
        hint_map = {
            "dashboard-tab": "Cockpit · Enter or click to act on Polly, Inbox, a project, or Settings · O follows the selected row · 1-6 switch views",
            "accounts-tab": "Accounts · C add Codex · L add Claude · Y usage · R relogin · J operator · M controller · V failover",
            "projects-tab": "Projects · S scan repos · A add project · T tracker · W workspace · N worker",
            "sessions-tab": "Sessions · O open window · I send input · P claim lease · R release · K stop · X remove",
            "alerts-tab": "Alerts · G focus selected session · Enter opens the linked session",
            "events-tab": "Events · Scroll recent activity and inspect the latest session history",
        }
        self.help_bar.update(hint_map.get(active, ""))
        now = datetime.now()
        if self.notice_text and self.notice_until and now < self.notice_until:
            self.message_bar.update(self.notice_text)
            return
        self.notice_text = None
        self.notice_until = None
        self.message_bar.update("Ready.")

    def _refresh_accounts(self, supervisor: Supervisor) -> None:
        controller = supervisor.config.pollypm.controller_account
        failover = set(supervisor.config.pollypm.failover_accounts)
        rows = [
            (
                (
                    account.key,
                    account.email,
                    account.provider.value,
                    "yes" if account.logged_in else "no",
                    "yes" if account.key == controller else "",
                    "yes" if account.key in failover else "",
                    account.usage_summary,
                ),
                account.key,
            )
            for account in self.account_statuses
        ]
        self._replace_table_rows(self.accounts_table, rows)

    def _refresh_projects(self, config) -> None:
        rows = [
            (
                (
                    project.key,
                    project.name or project.key,
                    project.kind.value,
                    "yes" if project.tracked else "",
                    str(project.path),
                ),
                project.key,
            )
            for project in config.projects.values()
        ]
        self._replace_table_rows(self.projects_table, rows)

    def _refresh_sessions(self, supervisor: Supervisor, launches, windows, alerts, leases) -> None:
        window_map = {window.name: window for window in windows}
        alert_counts: dict[str, int] = {}
        for alert in alerts:
            alert_counts[alert.session_name] = alert_counts.get(alert.session_name, 0) + 1
        lease_map = {lease.session_name: lease for lease in leases}

        rows: list[tuple[tuple[str, ...], str]] = []
        for launch in launches:
            window = window_map.get(launch.window_name)
            state = "running" if window else "missing"
            if window and window.pane_dead:
                state = "dead"
            lease = lease_map.get(launch.session.name)
            account_label = launch.account.email or launch.account.name
            rows.append(
                (
                    (
                        launch.session.name,
                        self._display_role_label(launch.session.role),
                        launch.session.project,
                        self._display_account_label(account_label),
                        state,
                        lease.owner if lease else "",
                        str(alert_counts.get(launch.session.name, 0)),
                    ),
                    launch.session.name,
                )
            )
        self._replace_table_rows(self.sessions_table, rows)

    def _refresh_alerts(self, alerts) -> None:
        rows = [
            (
                (
                    alert.session_name,
                    alert.alert_type,
                    alert.severity,
                    alert.message,
                ),
                f"{alert.session_name}:{alert.alert_type}",
            )
            for alert in alerts
        ]
        self._replace_table_rows(self.alerts_table, rows)

    def _refresh_events(self, supervisor: Supervisor) -> None:
        rows = [
            (
                (
                    event.created_at.replace("T", " ").split(".")[0],
                    event.session_name,
                    event.event_type,
                    event.message,
                ),
                f"{event.created_at}:{event.session_name}:{event.event_type}",
            )
            for event in supervisor.store.recent_events(limit=50)
        ]
        self._replace_table_rows(self.events_table, rows)

    def _replace_table_rows(
        self,
        table: DataTable,
        rows: list[tuple[tuple[str, ...], str]],
    ) -> None:
        snapshot_key = table.id or str(id(table))
        if self.table_snapshots.get(snapshot_key) == rows:
            return

        selected_key = self._selected_row_key(table)
        selected = self._selected_row_data(table)
        table.clear(columns=False)
        for cells, row_key in rows:
            table.add_row(*cells, key=row_key)
        self.table_snapshots[snapshot_key] = rows
        self._restore_table_selection(table, selected_key=selected_key, selection=selected)

    def _selected_row_key(self, table: DataTable) -> str | None:
        snapshot_key = table.id or str(id(table))
        snapshot = self.table_snapshots.get(snapshot_key, [])
        if table.row_count == 0 or table.cursor_row < 0:
            return None
        if table.cursor_row >= len(snapshot):
            return None
        return snapshot[table.cursor_row][1]

    def _selected_row_data(self, table: DataTable) -> tuple[str, ...] | None:
        if table.row_count == 0 or table.cursor_row < 0:
            return None
        return tuple(str(item) for item in table.get_row_at(table.cursor_row))

    def _restore_table_selection(
        self,
        table: DataTable,
        *,
        selected_key: str | None,
        selection: tuple[str, ...] | None,
    ) -> None:
        if table.row_count == 0:
            return
        snapshot_key = table.id or str(id(table))
        snapshot = self.table_snapshots.get(snapshot_key, [])
        if selected_key is not None:
            for index, (_cells, row_key) in enumerate(snapshot):
                if row_key == selected_key:
                    table.move_cursor(row=index, animate=False, scroll=False)
                    return
        if selection is None:
            if table.cursor_row < 0:
                table.move_cursor(row=0, animate=False, scroll=False)
            return
        for index in range(table.row_count):
            if tuple(str(item) for item in table.get_row_at(index)) == selection:
                table.move_cursor(row=index, animate=False, scroll=False)
                return
        table.move_cursor(row=0, animate=False, scroll=False)

    def _refresh_details(self, *, show_loading: bool) -> None:
        active = self._active_tab()
        selection = self._detail_selection_snapshot(active)
        detail_signature = (active, selection)
        if self.last_detail_selection == detail_signature and not show_loading:
            return
        self.last_detail_selection = detail_signature
        loading_map = {
            "dashboard-tab": self.dashboard,
            "accounts-tab": self.accounts_detail,
            "projects-tab": self.projects_detail,
            "sessions-tab": self.sessions_detail,
            "alerts-tab": self.alerts_detail,
            "events-tab": self.events_detail,
        }
        widget = loading_map.get(active)
        if widget is None:
            return
        if show_loading:
            widget.update("Loading…")
        request_id = self.detail_request_id = self.detail_request_id + 1
        status_map = {status.key: status for status in self.account_statuses}
        self.run_worker(
            lambda: self._build_detail_result(active, selection, status_map, request_id),
            thread=True,
            group="detail",
            exclusive=True,
            exit_on_error=False,
        )

    def _detail_selection_snapshot(self, active: str) -> object | None:
        if active == "accounts-tab":
            return self._selected_value(self.accounts_table)
        if active == "dashboard-tab":
            return self._selected_row_key_from_snapshot(self.cockpit_table)
        if active == "projects-tab":
            return self._selected_value(self.projects_table)
        if active == "sessions-tab":
            return self._selected_value(self.sessions_table)
        if active == "alerts-tab":
            if self.alerts_table.row_count == 0 or self.alerts_table.cursor_row < 0:
                return None
            return tuple(str(item) for item in self.alerts_table.get_row_at(self.alerts_table.cursor_row))
        if active == "events-tab":
            if self.events_table.row_count == 0 or self.events_table.cursor_row < 0:
                return None
            return tuple(str(item) for item in self.events_table.get_row_at(self.events_table.cursor_row))
        return None

    def _build_detail_result(
        self,
        active: str,
        selection: object | None,
        status_map: dict[str, AccountStatus],
        request_id: int,
    ) -> DetailResult:
        supervisor, config = self._load_context()
        if supervisor is None or config is None:
            return DetailResult(request_id=request_id, tab_id=active, content="PollyPM is not configured yet.")
        if active == "accounts-tab":
            text = self._account_detail(supervisor, selection if isinstance(selection, str) else None, status_map)
        elif active == "dashboard-tab":
            text = self._cockpit_detail(supervisor, config, selection if isinstance(selection, str) else None)
        elif active == "projects-tab":
            text = self._project_detail(config, selection if isinstance(selection, str) else None)
        elif active == "sessions-tab":
            text = self._session_detail(supervisor, selection if isinstance(selection, str) else None)
        elif active == "alerts-tab":
            text = self._alert_detail(selection if isinstance(selection, tuple) else None)
        elif active == "events-tab":
            text = self._event_detail(selection if isinstance(selection, tuple) else None)
        else:
            text = ""
        return DetailResult(request_id=request_id, tab_id=active, content=text)

    def _selected_value(self, table: DataTable, column: int = 0) -> str | None:
        if table.row_count == 0 or table.cursor_row < 0:
            return None
        return str(table.get_row_at(table.cursor_row)[column])

    def _selected_row_key_from_snapshot(self, table: DataTable) -> str | None:
        snapshot_key = table.id or str(id(table))
        snapshot = self.table_snapshots.get(snapshot_key, [])
        if table.row_count == 0 or table.cursor_row < 0 or table.cursor_row >= len(snapshot):
            return None
        return snapshot[table.cursor_row][1]

    def _display_command_label(self, raw_command: str, provider: ProviderKind) -> str:
        if re.fullmatch(r"\d+(?:\.\d+){1,2}", raw_command):
            return provider.value
        return raw_command

    def _display_role_label(self, role: str) -> str:
        return {
            "heartbeat-supervisor": "heartbeat",
            "operator-pm": "operator",
            "worker": "worker",
        }.get(role, role)

    def _display_account_label(self, email_or_name: str) -> str:
        if "@" in email_or_name:
            return email_or_name.split("@", 1)[0]
        return email_or_name

    def _summarize_usage_snapshot(self, raw_text: str) -> str:
        lines = [line.rstrip() for line in raw_text.splitlines()]
        useful: list[str] = []
        capture = False
        for line in lines:
            stripped = line.strip()
            lowered = stripped.lower()
            if not stripped:
                if capture and useful and useful[-1] != "":
                    useful.append("")
                continue
            if "status   config   usage   stats" in lowered:
                capture = True
            if stripped.startswith("Account:"):
                capture = True
            if not capture:
                continue
            if "welcome back" in lowered or "tips for getting started" in lowered or "recent activity" in lowered:
                continue
            if lowered in {"esc to cancel", "esc to interrupt"}:
                continue
            if "shift+tab to cycle" in lowered or lowered.endswith("tokens"):
                continue
            useful.append(stripped)

        if not useful:
            useful = [line.strip() for line in lines if line.strip()]

        compact: list[str] = []
        for line in useful:
            if compact and compact[-1] == "" and line == "":
                continue
            compact.append(line)
        return "\n".join(compact[:18])

    def _account_detail(
        self,
        supervisor: Supervisor,
        key: str | None,
        status_map: dict[str, AccountStatus],
    ) -> str:
        if key is None or key not in supervisor.config.accounts:
            return "Select an account to inspect it."
        account = supervisor.config.accounts[key]
        lines = [
            f"Key: {key}",
            f"Email: {account.email or '-'}",
            f"Provider: {account.provider.value}",
            f"Runtime: {account.runtime.value}",
            f"Home: {account.home or '-'}",
            f"Controller: {'yes' if key == supervisor.config.pollypm.controller_account else 'no'}",
            f"Failover: {'yes' if key in supervisor.config.pollypm.failover_accounts else 'no'}",
        ]
        status = status_map.get(key)
        if status is not None:
            lines.extend(
                [
                    f"Logged in: {'yes' if status.logged_in else 'no'}",
                    f"Plan: {status.plan}",
                    f"Health: {status.health}",
                    f"Status: {status.usage_summary}",
                    f"Isolation: {status.isolation_status}",
                    f"Auth Storage: {status.auth_storage}",
                    f"Profile Root: {status.profile_root or '-'}",
                    f"Isolation Notes: {status.isolation_summary}",
                    f"Usage Updated: {status.usage_updated_at or 'never'}",
                ]
            )
            if status.isolation_recommendation:
                lines.append(f"Recommendation: {status.isolation_recommendation}")
            if status.usage_raw_text:
                lines.extend(["", "Latest Usage Snapshot:", self._summarize_usage_snapshot(status.usage_raw_text)])
        return "\n".join(lines)

    def _project_detail(self, config, key: str | None) -> str:
        if key is None or key not in config.projects:
            return "Select a project to inspect it."
        project = config.projects[key]
        sessions = [
            session.name
            for session in config.sessions.values()
            if session.project == key and session.enabled
        ]
        worktrees = [item for item in list_worktrees(self.config_path, key) if item.status == "active"]
        task_backend = get_task_backend(project.path)
        issues_dir = task_backend.issues_root()
        state_counts = task_backend.state_counts() if task_backend.exists() else {}
        lines = [
            f"Key: {project.key}",
            f"Name: {project.name or project.key}",
            f"Path: {project.path}",
            f"Kind: {project.kind.value}",
            f"Tracked: {'yes' if project.tracked else 'no'}",
            f"Issue Tracker: {issues_dir if issues_dir.exists() else 'not initialized'}",
            f"Active Worktrees: {len(worktrees)}",
            f"Sessions: {', '.join(sessions) if sessions else 'none'}",
        ]
        if state_counts:
            lines.append(
                "Task States: "
                + ", ".join(f"{state}={count}" for state, count in state_counts.items() if count)
                or "Task States: empty"
            )
        if worktrees:
            lines.extend(["", "Worktrees:"])
            lines.extend(f"- {item.lane_kind}/{item.lane_key}: {item.path}" for item in worktrees[:5])
        return "\n".join(lines)

    def _cockpit_detail(self, supervisor: Supervisor, config, key: str | None) -> str:
        if key is None:
            return "Select Polly, Inbox, a project, or Settings."
        if key == "polly":
            return self._session_detail(supervisor, "operator")
        if key == "inbox":
            messages = list_open_messages(config.project.root_dir)
            if not messages:
                return "Inbox is clear."
            lines = ["Open Inbox Messages:", ""]
            for message in messages[:8]:
                lines.append(f"- {message.subject} · from {message.sender}")
            lines.extend(
                [
                    "",
                    "Reply flow:",
                    "Replies should go to Polly first. PM keeps the inbox thread, decides whether to resolve it, continue the conversation, or route a distilled action to a worker session.",
                ]
            )
            return "\n".join(lines)
        if key == "section:projects":
            return "Projects\n\nPick a project to open its live lane or start a new worker if it is idle."
        if key == "section:system":
            return "System\n\nSettings holds account controls, permissions, failover order, and other PollyPM runtime knobs."
        if key == "settings":
            recent_usage = supervisor.store.recent_token_usage(limit=5)
            lines = [
                "Settings",
                "",
                f"Workspace root: {supervisor.config.project.workspace_root}",
                f"Open permissions by default: {'on' if supervisor.config.pollypm.open_permissions_by_default else 'off'}",
                f"Controller account: {supervisor.config.pollypm.controller_account}",
                f"Failover order: {', '.join(supervisor.config.pollypm.failover_accounts) or 'none'}",
                "",
                "Secondary views:",
                "2 Accounts · 3 Projects · 4 Sessions · 5 Alerts · 6 Events",
            ]
            if recent_usage:
                lines.extend(["", "Recent token usage:"])
                for row in recent_usage[:4]:
                    lines.append(
                        f"- {row.project_key} · {row.account_name} · {row.model_name} · {row.tokens_used} tokens"
                    )
            return "\n".join(lines)
        if key.startswith("project:"):
            project_key = key.split(":", 1)[1]
            project_session_map = self._project_session_map(supervisor.plan_launches())
            session_name = project_session_map.get(project_key)
            if session_name:
                return self._session_detail(supervisor, session_name)
            detail = self._project_detail(config, project_key)
            return (
                f"{detail}\n\n"
                "No active session is running for this project.\n"
                "Use N to kick off a worker for the selected project."
            )
        return "Select a project to inspect it."

    def _session_detail(self, supervisor: Supervisor, session_name: str | None) -> str:
        if session_name is None:
            return "Select a session to preview it."
        now = datetime.now()
        if (
            self.session_preview_cache is not None
            and self.session_preview_cache[0] == session_name
            and now - self.session_preview_cache[2] < self.SESSION_PREVIEW_REFRESH_INTERVAL
        ):
            return self.session_preview_cache[1]
        try:
            launch = next(item for item in supervisor.plan_launches() if item.session.name == session_name)
        except StopIteration:
            return "Unknown session."
        tmux_session = supervisor._tmux_session_for_launch(launch)
        if not supervisor.tmux.has_session(tmux_session):
            return f"tmux session {tmux_session} is not running."
        window_map = supervisor._window_map()
        window = window_map.get(launch.window_name)
        if window is None:
            return "Window is not running."
        preview = supervisor.tmux.capture_pane(
            f"{tmux_session}:{launch.window_name}",
            lines=80,
        )
        command_label = self._display_command_label(window.pane_current_command, launch.session.provider)
        header = [
            f"tmux session: {tmux_session}",
            f"Window: {launch.window_name}",
            f"Provider: {launch.session.provider.value}",
            f"Account: {launch.account.email or launch.account.name}",
            f"Command: {command_label}",
            f"Path: {window.pane_current_path}",
            "",
        ]
        value = "\n".join(header) + preview
        self.session_preview_cache = (session_name, value, now)
        return value

    def _alert_detail(self, row: tuple[str, ...] | None) -> str:
        if row is None:
            return "No open alerts."
        session_name, alert_type, severity, message = row
        return f"Session: {session_name}\nType: {alert_type}\nSeverity: {severity}\n\n{message}"

    def _event_detail(self, row: tuple[str, ...] | None) -> str:
        if row is None:
            return "No events recorded."
        when, session_name, event_type, message = row
        return f"When: {when}\nSession: {session_name}\nType: {event_type}\n\n{message}"

    def on_data_table_row_highlighted(self, _event: DataTable.RowHighlighted) -> None:
        self._refresh_details(show_loading=True)
        self._update_context_bar()

    @on(Worker.StateChanged)
    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.group != "detail":
            return
        if event.state is WorkerState.ERROR:
            self._notify(f"Detail load failed: {event.worker.error}")
            return
        if event.state is not WorkerState.SUCCESS:
            return
        result = event.worker.result
        if not isinstance(result, DetailResult):
            return
        if result.request_id != self.detail_request_id:
            return
        target_map = {
            "dashboard-tab": self.dashboard,
            "accounts-tab": self.accounts_detail,
            "projects-tab": self.projects_detail,
            "sessions-tab": self.sessions_detail,
            "alerts-tab": self.alerts_detail,
            "events-tab": self.events_detail,
        }
        widget = target_map.get(result.tab_id)
        if widget is not None and self._active_tab() == result.tab_id:
            widget.update(result.content)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "cockpit-nav":
            self.action_open_selected_session()
        elif event.data_table.id == "sessions-table":
            self.action_open_selected_session()
        elif event.data_table.id == "alerts-table":
            self.action_focus_alert_session()

    def on_key(self, event: events.Key) -> None:
        tab_map = {
            "1": "dashboard-tab",
            "2": "accounts-tab",
            "3": "projects-tab",
            "4": "sessions-tab",
            "5": "alerts-tab",
            "6": "events-tab",
        }
        if event.key == "b":
            self.action_toggle_open_permissions()
            event.stop()
            return
        tab_id = tab_map.get(event.key)
        if tab_id is None:
            return
        self._set_active_tab(tab_id)
        event.stop()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "dashboard-open": self.action_open_selected_session,
            "dashboard-ensure": self.action_ensure_pollypm,
            "dashboard-heartbeat": self.action_run_heartbeat,
            "dashboard-permissions": self.action_toggle_open_permissions,
            "accounts-add-codex": self.action_add_codex_account,
            "accounts-add-claude": self.action_add_claude_account,
            "accounts-usage": self.action_refresh_selected_account_usage,
            "accounts-relogin": self.action_relogin_selected_account,
            "accounts-switch-operator": self.action_switch_operator,
            "accounts-controller": self.action_make_controller,
            "accounts-failover": self.action_toggle_failover,
            "accounts-remove": self.action_remove_selected_account,
            "projects-scan": self.action_scan_projects,
            "projects-add": self.action_add_project,
            "projects-tracker": self.action_init_project_tracker,
            "projects-root": self.action_set_workspace_root,
            "projects-worker": self.action_new_worker,
            "projects-remove": self.action_remove_selected_project,
            "sessions-open": self.action_open_selected_session,
            "sessions-send": self.action_send_input_selected,
            "sessions-claim": self.action_claim_selected_session,
            "sessions-release": self.action_release_selected_session,
            "sessions-stop": self.action_stop_selected_session,
            "sessions-remove": self.action_remove_selected_session,
            "alerts-focus": self.action_focus_alert_session,
        }
        button_id = event.button.id or ""
        if button_id.startswith("nav-"):
            self._set_active_tab(button_id.removeprefix("nav-"))
            return
        action = actions.get(button_id)
        if action is not None:
            action()

    def _active_tab(self) -> str:
        try:
            return self.query_one("#tabs", TabbedContent).active or "dashboard-tab"
        except NoMatches:
            return "dashboard-tab"

    def _set_active_tab(self, tab_id: str) -> None:
        self.query_one("#tabs", TabbedContent).active = tab_id
        focus_map = {
            "dashboard-tab": self.cockpit_table,
            "accounts-tab": self.accounts_table,
            "projects-tab": self.projects_table,
            "sessions-tab": self.sessions_table,
            "alerts-tab": self.alerts_table,
            "events-tab": self.events_table,
        }
        widget = focus_map.get(tab_id)
        if widget is not None:
            widget.focus()
        self.session_preview_cache = None
        self._update_context_bar()
        self._refresh_details(show_loading=True)

    def _notify(self, message: str) -> None:
        self.notice_text = message
        self.notice_until = datetime.now() + timedelta(seconds=6)
        self._update_context_bar()

    def action_show_dashboard(self) -> None:
        self._set_active_tab("dashboard-tab")

    def action_show_accounts(self) -> None:
        self._set_active_tab("accounts-tab")

    def action_show_projects(self) -> None:
        self._set_active_tab("projects-tab")

    def action_show_sessions(self) -> None:
        self._set_active_tab("sessions-tab")

    def action_show_alerts(self) -> None:
        self._set_active_tab("alerts-tab")

    def action_show_events(self) -> None:
        self._set_active_tab("events-tab")

    def _run(self, label: str, callback: Callable[[], object]) -> object | None:
        try:
            result = callback()
        except Exception as exc:  # noqa: BLE001
            self._notify(f"{label} failed: {exc}")
            return None
        self._notify(f"{label} completed.")
        self.session_preview_cache = None
        self._refresh_view(force=True)
        return result

    def action_ensure_pollypm(self) -> None:
        def _ensure() -> None:
            supervisor, _config = self._load_context()
            if supervisor is None:
                raise RuntimeError("PollyPM is not configured.")
            controller = self.service.ensure_pollypm()
            self._notify(f"Started control room with controller {controller}.")

        self._run("Ensure session", _ensure)

    def action_run_heartbeat(self) -> None:
        def _heartbeat() -> None:
            supervisor, _config = self._load_context()
            if supervisor is None:
                raise RuntimeError("PollyPM is not configured.")
            self.service.run_heartbeat()

        self._run("Heartbeat", _heartbeat)

    def action_toggle_open_permissions(self) -> None:
        def _toggle() -> None:
            supervisor, _config = self._load_context()
            if supervisor is None:
                raise RuntimeError("PollyPM is not configured.")
            enabled = not supervisor.config.pollypm.open_permissions_by_default
            self.service.set_open_permissions_default(enabled)

        self._run("Toggle open permissions default", _toggle)

    def action_add_codex_account(self) -> None:
        if self._active_tab() != "accounts-tab":
            return
        self._run("Add Codex account", lambda: self.service.add_account(ProviderKind.CODEX))

    def action_add_claude_account(self) -> None:
        if self._active_tab() != "accounts-tab":
            return
        self._run("Add Claude account", lambda: self.service.add_account(ProviderKind.CLAUDE))

    def action_context_action_r(self) -> None:
        active = self._active_tab()
        if active == "accounts-tab":
            self.action_relogin_selected_account()
        elif active == "sessions-tab":
            self.action_release_selected_session()

    def action_context_action_x(self) -> None:
        active = self._active_tab()
        if active == "accounts-tab":
            self.action_remove_selected_account()
        elif active == "projects-tab":
            self.action_remove_selected_project()
        elif active == "sessions-tab":
            self.action_remove_selected_session()

    def action_relogin_selected_account(self) -> None:
        key = self._selected_value(self.accounts_table)
        if key is None:
            self._notify("No account selected.")
            return
        self._run("Re-authenticate account", lambda: self.service.relogin_account(key))

    def _spawn_usage_refresh(self, account_key: str, *, notify: bool = True) -> None:
        self.pending_usage_refreshes[account_key] = datetime.now()
        subprocess.Popen(
            ["uv", "run", "pm", "refresh-usage", account_key],
            cwd=self.config_path.parent,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        if notify:
            self._notify(f"Refreshing usage for {account_key} in the background.")
        self.account_statuses_updated_at = None

    def action_refresh_selected_account_usage(self) -> None:
        if self._active_tab() != "accounts-tab":
            return
        key = self._selected_value(self.accounts_table)
        if key is None:
            self._notify("No account selected.")
            return
        self._spawn_usage_refresh(key)

    def action_remove_selected_account(self) -> None:
        key = self._selected_value(self.accounts_table)
        if key is None:
            self._notify("No account selected.")
            return

        self.push_screen(
            ConfirmModal(
                ConfirmRequest(
                    title="Remove account",
                    prompt=f"Remove account {key} from PollyPM config?",
                    confirm_label="Remove",
                )
            ),
            lambda confirmed: self._run("Remove account", lambda: self.service.remove_account(key, delete_home=False))
            if confirmed else None,
        )

    def action_make_controller(self) -> None:
        if self._active_tab() != "accounts-tab":
            return
        key = self._selected_value(self.accounts_table)
        if key is None:
            self._notify("No account selected.")
            return
        self._run("Set controller account", lambda: self.service.set_controller_account(key))

    def action_toggle_failover(self) -> None:
        if self._active_tab() != "accounts-tab":
            return
        key = self._selected_value(self.accounts_table)
        if key is None:
            self._notify("No account selected.")
            return
        self._run("Toggle failover", lambda: self.service.toggle_failover_account(key))

    def action_switch_operator(self) -> None:
        if self._active_tab() != "accounts-tab":
            return
        key = self._selected_value(self.accounts_table)
        if key is None:
            self._notify("No account selected.")
            return

        def _switch() -> None:
            supervisor, _config = self._load_context()
            if supervisor is None:
                raise RuntimeError("PollyPM is not configured.")
            self.service.switch_session_account("operator", key)

        self._run("Switch operator", _switch)

    def action_scan_projects(self) -> None:
        if self._active_tab() != "projects-tab":
            return
        supervisor, config = self._load_context()
        if supervisor is None or config is None:
            self._notify("PollyPM is not configured.")
            return

        repos = discover_git_repositories(Path.home(), known_paths={item.path for item in config.projects.values()})
        if not repos:
            self._notify("No new git repos were found.")
            return

        def _handle_scan(result: tuple[str, list[Path]]) -> None:
            mode, paths = result
            if mode == "cancel" or not paths:
                self._notify("Project scan closed.")
                return
            for repo_path in paths:
                self.service.register_project(repo_path)
            self._notify(f"Added {len(paths)} project(s).")
            self.action_refresh_all()

        self.push_screen(RepoScanModal(repos), _handle_scan)

    def action_add_project(self) -> None:
        if self._active_tab() != "projects-tab":
            return

        self.push_screen(
            InputModal(
                InputRequest(
                    title="Add project",
                    prompt="Enter the path to a git repository to register.",
                    placeholder="/Users/sam/dev/wire",
                    button_label="Register",
                )
            ),
            lambda value: self._run("Register project", lambda: self.service.register_project(Path(value)))
            if value else None,
        )

    def action_init_project_tracker(self) -> None:
        if self._active_tab() != "projects-tab":
            return
        key = self._selected_value(self.projects_table)
        if key is None:
            self._notify("No project selected.")
            return
        self._run("Initialize project tracker", lambda: self.service.enable_tracked_project(key))

    def action_set_workspace_root(self) -> None:
        if self._active_tab() != "projects-tab":
            return
        supervisor, _config = self._load_context()
        if supervisor is None:
            self._notify("PollyPM is not configured.")
            return
        current = str(supervisor.config.project.workspace_root)
        self.push_screen(
            InputModal(
                InputRequest(
                    title="Set workspace root",
                    prompt="Choose the default directory for new project worktrees.",
                    value=current,
                    placeholder="/Users/sam/dev",
                    button_label="Save",
                )
            ),
            lambda value: self._run("Set workspace root", lambda: self.service.set_workspace_root(Path(value)))
            if value else None,
        )

    def action_remove_selected_project(self) -> None:
        key = self._selected_value(self.projects_table)
        if key is None:
            self._notify("No project selected.")
            return
        self.push_screen(
            ConfirmModal(
                ConfirmRequest(
                    title="Remove project",
                    prompt=f"Remove project {key} from PollyPM?",
                    confirm_label="Remove",
                )
            ),
            lambda confirmed: self._run("Remove project", lambda: self.service.remove_project(key))
            if confirmed else None,
        )

    def action_new_worker(self) -> None:
        active = self._active_tab()
        if active == "dashboard-tab":
            selected = self._selected_row_key_from_snapshot(self.cockpit_table)
            if selected and selected.startswith("project:"):
                project_key = selected.split(":", 1)[1]
            else:
                project_key = None
        elif active == "projects-tab":
            project_key = self._selected_value(self.projects_table)
        elif active == "sessions-tab":
            project_key = self._selected_value(self.sessions_table, 2)
            if project_key == "pollypm":
                project_key = None
        else:
            return
        if project_key is None:
            self._notify("Select a project first.")
            return
        self._prompt_new_worker_for_project(project_key)

    def _prompt_new_worker_for_project(self, project_key: str) -> None:
        default_prompt = self.service.suggest_worker_prompt(project_key=project_key)
        self.push_screen(
            InputModal(
                InputRequest(
                    title="New worker session",
                    prompt=f"Enter the initial prompt for project {project_key}.",
                    value=default_prompt,
                    placeholder=default_prompt,
                    button_label="Create",
                )
            ),
            lambda value: self._create_and_maybe_launch_worker(project_key, value) if value else None,
        )

    def _create_and_maybe_launch_worker(self, project_key: str, prompt: str) -> None:
        session = self._run(
            "Create worker session",
            lambda: self.service.create_and_launch_worker(project_key=project_key, prompt=prompt),
        )
        if session is None:
            return

    def action_open_selected_session(self) -> None:
        active = self._active_tab()
        session_name: str | None
        if active == "dashboard-tab":
            selected = self._selected_row_key_from_snapshot(self.cockpit_table)
            if selected == "polly":
                session_name = "operator"
            elif selected == "inbox":
                self._notify("Inbox stays in the right pane. Use Polly to work through open messages.")
                return
            elif selected == "section:projects":
                self._notify("Pick a project row below to open or start work.")
                return
            elif selected == "section:system":
                self._notify("Open Settings below for runtime controls.")
                return
            elif selected == "settings":
                self.action_show_accounts()
                self._notify("Jumped to account and runtime controls.")
                return
            elif selected and selected.startswith("project:"):
                supervisor, _config = self._load_context()
                session_name = None
                if supervisor is not None:
                    project_key = selected.split(":", 1)[1]
                    session_name = self._project_session_map(supervisor.plan_launches()).get(project_key)
                    if session_name is None:
                        self._prompt_new_worker_for_project(project_key)
                        return
            else:
                session_name = None
        else:
            session_name = self._selected_value(self.sessions_table)
        if session_name is None:
            self._notify("No session selected.")
            return
        self._run("Focus session window", lambda: self.service.focus_session(session_name))

    def action_stop_selected_session(self) -> None:
        session_name = self._selected_value(self.sessions_table)
        if session_name is None:
            self._notify("No session selected.")
            return
        self._run("Stop session", lambda: self.service.stop_session(session_name))

    def action_remove_selected_session(self) -> None:
        session_name = self._selected_value(self.sessions_table)
        if session_name is None:
            self._notify("No session selected.")
            return

        def _remove() -> None:
            self.service.stop_session(session_name)
            self.service.remove_session(session_name)

        self.push_screen(
            ConfirmModal(
                ConfirmRequest(
                    title="Remove session",
                    prompt=f"Stop and remove worker session {session_name}?",
                    confirm_label="Remove",
                )
            ),
            lambda confirmed: self._run("Remove session", _remove) if confirmed else None,
        )

    def action_send_input_selected(self) -> None:
        session_name = self._selected_value(self.sessions_table)
        if session_name is None:
            self._notify("No session selected.")
            return
        self.push_screen(
            InputModal(
                InputRequest(
                    title="Send input",
                    prompt=f"Send text to session {session_name}.",
                    value="Continue with the next step.",
                    placeholder="Continue with the next step.",
                    button_label="Send",
                )
            ),
            lambda value: self._run(
                "Send input",
                lambda: self.service.send_input(session_name, value, owner="human"),
            )
            if value else None,
        )

    def action_claim_selected_session(self) -> None:
        session_name = self._selected_value(self.sessions_table)
        if session_name is None:
            self._notify("No session selected.")
            return
        self._run(
            "Claim lease",
            lambda: self.service.claim_lease(session_name, "human", "claimed from TUI"),
        )

    def action_release_selected_session(self) -> None:
        session_name = self._selected_value(self.sessions_table)
        if session_name is None:
            self._notify("No session selected.")
            return
        self._run(
            "Release lease",
            lambda: self.service.release_lease(session_name),
        )

    def action_focus_alert_session(self) -> None:
        if self._active_tab() != "alerts-tab":
            return
        session_name = self._selected_value(self.alerts_table, 0)
        if session_name is None:
            self._notify("No alert selected.")
            return
        self._run("Focus alert session", lambda: self.service.focus_session(session_name))
