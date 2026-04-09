from __future__ import annotations

from pathlib import Path
import subprocess

from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, ListItem, ListView, Static

from promptmaster.models import ProviderKind
from promptmaster.config import load_config
from promptmaster.service_api import PromptMasterService
from promptmaster.cockpit import CockpitItem, CockpitRouter, build_cockpit_detail


ASCII_POLLY = "\n".join(
    [
        "█▀█ █▀█ █   █   █▄█",
        "█▀▀ █▄█ █▄▄ █▄▄  █ ",
    ]
)

POLLY_SLOGANS = [
    "Plans first.\nChaos later.",
    "Inbox clear.\nProjects moving.",
    "Small steps.\nSharp turns.",
    "Less thrash.\nMore shipped.",
    "Watch the drift.\nTrim the waste.",
    "Keep it modular.\nKeep it moving.",
    "Fewer heroics.\nMore progress.",
    "Big picture.\nTight loops.",
    "Plan clean.\nLand faster.",
    "Break it down.\nShip it right.",
    "Stay useful.\nStay honest.",
    "No mystery.\nJust momentum.",
    "Steady lanes.\nClean handoffs.",
    "Less panic.\nMore process.",
    "Trim the scope.\nRaise the bar.",
    "One project.\nMany good turns.",
    "Spot the loop.\nCut the loop.",
    "Move with proof.\nNot vibes.",
    "Less flailing.\nMore finish.",
    "Make it test.\nMake it stick.",
    "Short chunks.\nLong horizons.",
    "Good prompts.\nBetter outcomes.",
    "Less yak.\nMore traction.",
    "Fewer tabs.\nBetter work.",
    "Lean plans.\nStrong reviews.",
    "Stop guessing.\nStart steering.",
    "Keep the lane.\nKeep the pace.",
    "Quick checks.\nClear calls.",
    "Draft less.\nDecide more.",
    "Tidy queue.\nDirty hands.",
    "See the north star.\nMiss fewer turns.",
    "Leave breadcrumbs.\nResume anywhere.",
    "Build smaller.\nLearn faster.",
    "Catch drift.\nSave days.",
    "Queue the next thing.\nFinish this thing.",
    "Reduce friction.\nIncrease signal.",
    "Nudge gently.\nCorrect early.",
    "Own the workflow.\nTrust the craft.",
    "Tiny slices.\nReal progress.",
    "Smart defaults.\nHuman override.",
    "Project calm.\nTerminal alive.",
    "Good rails.\nBetter velocity.",
    "Less ceremony.\nMore clarity.",
    "Aim tighter.\nShip cleaner.",
    "One click.\nLive session.",
    "Watch the turns.\nGuard the goal.",
    "From idea pad\nto finished lane.",
    "Short feedback.\nLong memory.",
    "Keep receipts.\nKeep moving.",
    "Review hard.\nMerge clean.",
    "Sharp prompts.\nSofter chaos.",
    "Block less.\nGuide more.",
    "Trust, but\ntest anyway.",
    "No death march.\nJust leverage.",
    "Make the path.\nThen walk it.",
    "Less spinning.\nMore shipping.",
    "Keep sessions warm.\nKeep context warmer.",
    "One rail.\nMany lanes.",
    "Catch the stall.\nResume the work.",
    "Progress counts.\nBusy doesn’t.",
    "Think in chunks.\nLand in commits.",
    "Inbox first.\nPanic never.",
    "Good queues.\nGreat sleep.",
    "Watch costs.\nKeep quality.",
    "Clean exits.\nFast resumes.",
    "See the risk.\nCut the waste.",
    "Polish later.\nStructure now.",
    "Measure the turn.\nThen decide.",
    "Right agent.\nRight depth.",
    "Move the issue.\nNot the goalposts.",
    "Less prompting.\nMore orchestration.",
    "Make it reviewable.\nMake it real.",
    "Clear lanes.\nClear heads.",
    "Let workers work.\nLet Polly steer.",
    "Guide the build.\nGuard the vision.",
    "A little ruthless.\nA lot helpful.",
    "Catch regressions.\nKeep momentum.",
    "Small commits.\nBig confidence.",
    "Hold the thread.\nFinish the stitch.",
    "Save the state.\nSkip the scramble.",
    "Slow is smooth.\nSmooth ships.",
    "Cut the loop.\nKeep the lesson.",
    "Treat drift early.\nAvoid rewrites.",
    "Less dashboard.\nMore cockpit.",
    "State on disk.\nCalm in motion.",
    "Poke the blocker.\nNot the user.",
    "Prompt with intent.\nRecover with context.",
    "Make the queue sing.\nNot sprawl.",
    "Better defaults.\nFewer excuses.",
    "Choose the lane.\nOwn the turn.",
    "See the whole board.\nMove one piece.",
    "One source of truth.\nMany good views.",
    "Do the next thing.\nNot all things.",
    "Structured memory.\nFlexible brains.",
    "Project first.\nEgo later.",
    "Real progress.\nVisible proof.",
    "Keep it humming.\nKeep it human.",
    "Good systems.\nFewer hero saves.",
    "Clear eyes.\nLive panes.",
    "Tight feedback.\nLoose shoulders.",
    "Fewer surprises.\nBetter launches.",
    "Guide the chaos.\nShip the value.",
]


class RailItem(ListItem):
    def __init__(
        self,
        item: CockpitItem,
        *,
        active_view: bool,
        first_project: bool = False,
    ) -> None:
        self.body = Static(classes="rail-item-body")
        self.item = item
        super().__init__(self.body, classes="rail-row", disabled=not item.selectable)
        self.apply_item(item, active_view=active_view, first_project=first_project)

    @property
    def cockpit_key(self) -> str:
        return self.item.key

    def apply_item(self, item: CockpitItem, *, active_view: bool, first_project: bool) -> None:
        self.item = item
        self.disabled = not item.selectable
        for class_name in [
            "inbox-entry",
            "project-start",
            "project-row",
            "needs-user",
            "live",
            "active-view",
        ]:
            self.remove_class(class_name)
        if item.key == "inbox":
            self.add_class("inbox-entry")
        if first_project:
            self.add_class("project-start")
        if item.key.startswith("project:"):
            self.add_class("project-row")
        if item.state.startswith("!"):
            self.add_class("needs-user")
        if item.state.endswith("live"):
            self.add_class("live")
        if active_view:
            self.add_class("active-view")
        self.update_body()

    def update_body(self) -> None:
        text = Text()
        indicator, indicator_style = self._indicator()
        if indicator:
            text.append(f"{indicator} ", style=indicator_style)
        else:
            text.append("  ")
        text.append(self.item.label)
        self.body.update(text)

    def _indicator(self) -> tuple[str, str]:
        if self.item.state.endswith("live"):
            return self.item.state.split(" ", 1)[0], "#63d48b"
        if self.item.state.startswith("!"):
            return "!", "#d9767b"
        if self.item.key == "polly":
            return "•", "#8aa8c2"
        if self.item.key == "settings":
            return "•", "#8aa8c2"
        return "", ""


class PollyCockpitApp(App[None]):
    TITLE = "PollyPM"
    SUB_TITLE = "Cockpit"
    CSS = """
    Screen {
        background: #0f1317;
        color: #eef2f4;
        padding: 0;
    }
    #brand {
        padding: 1 0 0 0;
        color: #f5f7fa;
    }
    #tagline {
        color: #97a6b2;
        padding: 0 0 1 0;
        height: 4;
    }
    #nav {
        height: 1fr;
        background: transparent;
        border: none;
        scrollbar-size: 0 0;
    }
    #nav > .rail-row {
        height: 1;
        padding: 0 1;
        color: #d6dee5;
        background: transparent;
        text-style: none;
    }
    #nav > .rail-row.inbox-entry {
        margin-top: 1;
    }
    #nav > .rail-row.project-start {
        margin-top: 1;
    }
    #nav > .rail-row.-highlight {
        background: #1e2730;
        color: #f2f6f8;
    }
    #nav:focus > .rail-row.-highlight {
        background: #253140;
        color: #f2f6f8;
    }
    #nav > .rail-row.needs-user {
        background: #34191c;
        color: #f2d7da;
    }
    #nav > .rail-row.live {
        background: #152a1f;
        color: #dcf4e6;
    }
    #nav > .rail-row.active-view {
        background: #183c68;
        color: #eef6ff;
        text-style: bold;
    }
    #nav > .rail-row.active-view.-highlight,
    #nav:focus > .rail-row.active-view.-highlight {
        background: #22528c;
        color: #eef6ff;
    }
    #nav > .rail-row .rail-item-body {
        width: 1fr;
    }
    #settings-row {
        height: 1;
        margin-top: 1;
        padding: 0 1;
        color: #d6dee5;
        background: transparent;
    }
    #settings-row.active-view {
        background: #183c68;
        color: #eef6ff;
        text-style: bold;
    }
    #settings-row.-hover {
        background: #253140;
        color: #f2f6f8;
    }
    #hint {
        height: 3;
        color: #8f9da9;
        padding: 1 0 0 0;
    }
    """

    BINDINGS = [
        Binding("enter,o", "open_selected", "Open"),
        Binding("n", "new_worker", "New Worker"),
        Binding("r", "refresh", "Refresh"),
        Binding("s", "open_settings", "Settings"),
    ]

    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self.config_path = config_path
        self.router = CockpitRouter(config_path)
        self.brand = Static(ASCII_POLLY, id="brand")
        self.tagline = Static("\n" + POLLY_SLOGANS[0], id="tagline")
        self.nav = ListView(id="nav")
        self.settings_row = Static("• Settings", id="settings-row")
        self.hint = Static("", id="hint")
        self.spinner_index = 0
        self.slogan_index = 0
        self._slogan_tick = 0
        self.selected_key = "polly"
        self._items: list[CockpitItem] = []
        self._row_widgets: dict[str, RailItem] = {}
        self._suspend_selection_events = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield self.brand
            yield self.tagline
            yield self.nav
            yield self.settings_row
            yield self.hint

    def on_mount(self) -> None:
        self.router.ensure_cockpit_layout()
        self.selected_key = self.router.selected_key()
        self._refresh_rows()
        self.set_interval(0.8, self._tick)
        self.nav.focus()

    def _focus_right_pane(self) -> None:
        focus_method = getattr(self.router, "focus_right_pane", None)
        if callable(focus_method):
            focus_method()

    def _tick(self) -> None:
        self.spinner_index = (self.spinner_index + 1) % 4
        self._slogan_tick += 1
        if self._slogan_tick >= 75:
            self._slogan_tick = 0
            self.slogan_index = (self.slogan_index + 1) % len(POLLY_SLOGANS)
            self.tagline.update("\n" + POLLY_SLOGANS[self.slogan_index])
        self._refresh_rows()

    def _nav_items(self) -> list[CockpitItem]:
        return [item for item in self._items if item.key != "settings"]

    def _refresh_rows(self) -> None:
        self._items = self.router.build_items(spinner_index=self.spinner_index)
        nav_items = self._nav_items()
        previous_key = self._selected_row_key()
        selected_key = None if self.selected_key == "settings" else (previous_key or self.selected_key)
        keys = [item.key for item in nav_items]
        rebuild = keys != list(self._row_widgets)
        if rebuild:
            self._row_widgets = {}
        first_project_seen = False
        rows: list[RailItem] = []
        restore_index: int | None = 0 if selected_key is not None else None
        for index, item in enumerate(nav_items):
            first_project = False
            if item.key.startswith("project:") and not first_project_seen:
                first_project = True
                first_project_seen = True
            if rebuild:
                row = RailItem(
                    item,
                    active_view=item.key == self.selected_key,
                    first_project=first_project,
                )
                self._row_widgets[item.key] = row
            else:
                row = self._row_widgets[item.key]
                row.apply_item(item, active_view=item.key == self.selected_key, first_project=first_project)
            rows.append(row)
            if selected_key is not None and item.key == selected_key:
                restore_index = index
        if rebuild:
            self.nav.clear()
            self.nav.extend(rows)
        if rows:
            if restore_index is None:
                if self.nav.index is not None:
                    self._suspend_selection_events = True
                    try:
                        self.nav.index = None
                    finally:
                        self._suspend_selection_events = False
            elif self.nav.index != restore_index:
                self._suspend_selection_events = True
                try:
                    self.nav.index = restore_index
                finally:
                    self._suspend_selection_events = False
        self.settings_row.set_class(self.selected_key == "settings", "active-view")
        if any(item.key == "settings" for item in self._items):
            self.settings_row.display = True
        else:
            self.settings_row.display = False
        self._update_hint()

    def _selected_row_key(self) -> str | None:
        index = self.nav.index
        nav_items = self._nav_items()
        if index is None or index < 0 or index >= len(nav_items):
            return None
        return nav_items[index].key

    def _update_hint(self) -> None:
        key = self._selected_row_key() or self.selected_key
        if key == "settings":
            self.hint.update("Press S to open\nSettings.")
            return
        if key == "polly":
            self.hint.update("Enter opens\nPolly.")
            return
        if key == "inbox":
            self.hint.update("Enter opens\nInbox.")
            return
        if key and key.startswith("project:"):
            self.hint.update("Enter opens.\nN starts work.")
            return
        self.hint.update("Choose a lane.")

    def action_open_selected(self) -> None:
        key = self._selected_row_key()
        if key is None:
            return
        self.selected_key = key
        self.router.route_selected(key)
        self._focus_right_pane()
        self._refresh_rows()

    def action_open_settings(self) -> None:
        self.selected_key = "settings"
        self.router.route_selected("settings")
        self._focus_right_pane()
        self._refresh_rows()

    def action_new_worker(self) -> None:
        key = self._selected_row_key()
        if key is None or not key.startswith("project:"):
            return
        project_key = key.split(":", 1)[1]
        self.router.create_worker_and_route(project_key)
        self._focus_right_pane()
        self.selected_key = key
        self._refresh_rows()

    def action_refresh(self) -> None:
        self.router.ensure_cockpit_layout()
        self._refresh_rows()

    @on(ListView.Selected, "#nav")
    def on_nav_selected(self, event: ListView.Selected) -> None:
        if self._suspend_selection_events:
            return
        if not self.nav.has_focus:
            return
        row = event.item
        if not isinstance(row, RailItem):
            return
        self.selected_key = row.cockpit_key
        self.router.route_selected(row.cockpit_key)
        self._focus_right_pane()
        self._refresh_rows()

    @on(events.Click, "#settings-row")
    def on_settings_click(self, _event: events.Click) -> None:
        self.action_open_settings()

    @on(events.Enter, "#settings-row")
    def on_settings_enter(self, _event: events.Enter) -> None:
        self.settings_row.add_class("-hover")

    @on(events.Leave, "#settings-row")
    def on_settings_leave(self, _event: events.Leave) -> None:
        self.settings_row.remove_class("-hover")


class PollyCockpitPaneApp(App[None]):
    TITLE = "PollyPM"
    SUB_TITLE = "Pane"
    CSS = """
    Screen {
        background: #10161b;
        color: #eef2f4;
        padding: 1;
    }
    #body {
        border: round #4c5967;
        background: #131a20;
        padding: 1;
    }
    """

    def __init__(self, config_path: Path, kind: str, target: str | None = None) -> None:
        super().__init__()
        self.config_path = config_path
        self.kind = kind
        self.target = target
        self.body = Static("", id="body")

    def compose(self) -> ComposeResult:
        yield self.body

    def on_mount(self) -> None:
        self._refresh()
        self.set_interval(5, self._refresh)

    def _refresh(self) -> None:
        self.body.update(build_cockpit_detail(self.config_path, self.kind, self.target))


class PollySettingsPaneApp(App[None]):
    TITLE = "PollyPM"
    SUB_TITLE = "Settings"
    CSS = """
    Screen {
        background: #10161b;
        color: #eef2f4;
        padding: 1;
        layout: vertical;
    }
    #status {
        height: 1;
        color: #cfd7de;
        background: #141c23;
        padding: 0 1;
    }
    #message {
        height: 1;
        color: #f4d76e;
        background: #1a222a;
        padding: 0 1;
    }
    #actions {
        height: auto;
        margin: 1 0;
    }
    #actions Button {
        margin-right: 1;
    }
    #layout {
        height: 1fr;
    }
    #accounts {
        width: 58;
        min-width: 42;
        height: 1fr;
        border: round #41505f;
        background: #131a20;
    }
    #detail-pane {
        height: 1fr;
        border: round #41505f;
        background: #131a20;
        padding: 1;
    }
    .section-title {
        color: #eef4f8;
        text-style: bold;
        padding-bottom: 1;
    }
    #detail {
        height: 1fr;
        color: #d9e1e7;
    }
    #help {
        height: 2;
        color: #8f9da9;
        padding-top: 1;
    }
    """

    BINDINGS = [
        Binding("r", "relogin_selected", "Relogin"),
        Binding("y", "refresh_usage", "Usage"),
        Binding("j", "switch_operator", "Operator"),
        Binding("m", "make_controller", "Controller"),
        Binding("v", "toggle_failover", "Failover"),
        Binding("b", "toggle_permissions", "Permissions"),
        Binding("c", "add_codex", "Add Codex"),
        Binding("l", "add_claude", "Add Claude"),
        Binding("u", "refresh", "Refresh"),
    ]

    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self.config_path = config_path
        self.service = PromptMasterService(config_path)
        self.status_bar = Static("", id="status")
        self.message_bar = Static("", id="message")
        self.accounts = DataTable(id="accounts")
        self.detail = Static("", id="detail")
        self.help = Static(
            "C add Codex · L add Claude · Y usage · R relogin · J operator · M controller · V failover · B permissions · U refresh",
            id="help",
        )
        self._selected_account_key: str | None = None

    def compose(self) -> ComposeResult:
        yield self.status_bar
        yield self.message_bar
        with Horizontal(id="actions"):
            yield Button("Add Codex", id="add-codex")
            yield Button("Add Claude", id="add-claude")
            yield Button("Usage", id="usage")
            yield Button("Relogin", id="relogin")
            yield Button("Operator", id="operator")
            yield Button("Controller", id="controller")
            yield Button("Failover", id="failover")
            yield Button("Permissions", id="permissions")
            yield Button("Refresh", id="refresh")
        with Horizontal(id="layout"):
            yield self.accounts
            with Vertical(id="detail-pane"):
                yield Static("Settings", classes="section-title")
                yield self.detail
        yield self.help

    def on_mount(self) -> None:
        self.accounts.cursor_type = "row"
        self.accounts.zebra_stripes = True
        self.accounts.add_columns("Key", "Email", "Provider", "Login", "Ctrl", "FO", "Usage")
        self._refresh()
        self.set_interval(8, self._refresh)
        self.accounts.focus()

    def _notify(self, message: str) -> None:
        self.message_bar.update(message)

    def _refresh(self) -> None:
        config = load_config(self.config_path)
        statuses = self.service.list_account_statuses()
        selected = self._selected_account_key or self._current_selected_key()
        rows: list[tuple[tuple[str, ...], str]] = []
        for status in statuses:
            rows.append(
                (
                    (
                        status.key,
                        status.email or "-",
                        status.provider.value,
                        "yes" if status.logged_in else "no",
                        "yes" if config.promptmaster.controller_account == status.key else "",
                        "yes" if status.key in config.promptmaster.failover_accounts else "",
                        status.usage_summary,
                    ),
                    status.key,
                )
            )
        self._replace_rows(rows, selected)
        current_key = self._current_selected_key()
        self._selected_account_key = current_key
        controller = config.promptmaster.controller_account
        self.status_bar.update(
            f"Controller: {controller} · Open permissions: {'on' if config.promptmaster.open_permissions_by_default else 'off'} · Accounts: {len(statuses)}"
        )
        self._refresh_detail(statuses, config)

    def _replace_rows(self, rows: list[tuple[tuple[str, ...], str]], selected: str | None) -> None:
        self.accounts.clear()
        new_order = [key for _row, key in rows]
        for row, key in rows:
            self.accounts.add_row(*row, key=key)
        if self.accounts.row_count == 0:
            return
        if selected and selected in new_order:
            self.accounts.move_cursor(row=new_order.index(selected))
        elif self.accounts.cursor_row < 0:
            self.accounts.move_cursor(row=0)

    def _current_selected_key(self) -> str | None:
        if self.accounts.row_count == 0 or self.accounts.cursor_row < 0:
            return None
        try:
            row_key = self.accounts.coordinate_to_cell_key((self.accounts.cursor_row, 0)).row_key
        except Exception:
            return None
        return str(row_key.value) if row_key is not None else None

    def _selected_status(self, statuses) -> object | None:
        key = self._current_selected_key()
        if key is None:
            return None
        for status in statuses:
            if status.key == key:
                return status
        return None

    def _refresh_detail(self, statuses, config) -> None:
        status = self._selected_status(statuses)
        if status is None:
            self.detail.update("No connected accounts.\n\nUse Add Codex or Add Claude to connect one.")
            return
        detail_lines = [
            f"Account: {status.key}",
            f"Email: {status.email or '-'}",
            f"Provider: {status.provider.value}",
            f"Logged in: {'yes' if status.logged_in else 'no'}",
            f"Health: {status.health}",
            f"Plan: {status.plan}",
            f"Usage: {status.usage_summary}",
            f"Controller: {'yes' if config.promptmaster.controller_account == status.key else 'no'}",
            f"Failover: {'yes' if status.key in config.promptmaster.failover_accounts else 'no'}",
            f"Home: {status.home or '-'}",
            "",
            f"Isolation: {status.isolation_status}",
            f"Storage: {status.auth_storage}",
        ]
        if status.available_at:
            detail_lines.append(f"Available at: {status.available_at}")
        if status.access_expires_at:
            detail_lines.append(f"Access expires: {status.access_expires_at}")
        if status.reason:
            detail_lines.extend(["", f"Reason: {status.reason}"])
        if status.usage_raw_text:
            snippet = status.usage_raw_text.strip().splitlines()[:8]
            if snippet:
                detail_lines.extend(["", "Latest usage snapshot:"])
                detail_lines.extend(f"  {line}" for line in snippet)
        self.detail.update("\n".join(detail_lines))

    def _run_action(self, label: str, callback) -> None:
        try:
            callback()
        except Exception as exc:  # noqa: BLE001
            self._notify(f"{label} failed: {exc}")
            return
        self._notify(f"{label} completed.")
        self._refresh()

    def _selected_key_or_notice(self) -> str | None:
        key = self._current_selected_key()
        if key is None:
            self._notify("No account selected.")
        return key

    def action_refresh(self) -> None:
        self._refresh()

    def action_add_codex(self) -> None:
        self._run_action("Add Codex account", lambda: self.service.add_account(ProviderKind.CODEX))

    def action_add_claude(self) -> None:
        self._run_action("Add Claude account", lambda: self.service.add_account(ProviderKind.CLAUDE))

    def action_relogin_selected(self) -> None:
        key = self._selected_key_or_notice()
        if key is None:
            return
        self._run_action("Re-authenticate account", lambda: self.service.relogin_account(key))

    def action_refresh_usage(self) -> None:
        key = self._selected_key_or_notice()
        if key is None:
            return
        try:
            subprocess.run(
                ["uv", "run", "pm", "refresh-usage", key],
                cwd=self.config_path.parent,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception as exc:  # noqa: BLE001
            self._notify(f"Usage refresh failed: {exc}")
            return
        self._notify(f"Usage refreshed for {key}.")
        self._refresh()

    def action_switch_operator(self) -> None:
        key = self._selected_key_or_notice()
        if key is None:
            return
        self._run_action("Switch operator", lambda: self.service.switch_session_account("operator", key))

    def action_make_controller(self) -> None:
        key = self._selected_key_or_notice()
        if key is None:
            return
        self._run_action("Set controller account", lambda: self.service.set_controller_account(key))

    def action_toggle_failover(self) -> None:
        key = self._selected_key_or_notice()
        if key is None:
            return
        self._run_action("Toggle failover", lambda: self.service.toggle_failover_account(key))

    def action_toggle_permissions(self) -> None:
        config = load_config(self.config_path)
        enabled = not config.promptmaster.open_permissions_by_default
        self._run_action("Toggle open permissions", lambda: self.service.set_open_permissions_default(enabled))

    @on(Button.Pressed)
    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "add-codex": self.action_add_codex,
            "add-claude": self.action_add_claude,
            "usage": self.action_refresh_usage,
            "relogin": self.action_relogin_selected,
            "operator": self.action_switch_operator,
            "controller": self.action_make_controller,
            "failover": self.action_toggle_failover,
            "permissions": self.action_toggle_permissions,
            "refresh": self.action_refresh,
        }
        action = actions.get(event.button.id or "")
        if action is not None:
            action()

    @on(DataTable.RowSelected, "#accounts")
    def on_account_selected(self, _event: DataTable.RowSelected) -> None:
        self._selected_account_key = self._current_selected_key()
        self._refresh()
