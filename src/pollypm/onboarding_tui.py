from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import time

from rich.console import Console
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import CenterMiddle, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.worker import Worker, WorkerState
from textual.widgets import Button, Checkbox, Footer, RadioButton, RadioSet, SelectionList, Static

from pollypm.cli_shortcuts import shortcut_rows
from pollypm.config import write_config
from pollypm.doctor import (
    AutoFixPlan,
    check_claude_cli,
    check_codex_cli,
    check_tmux,
    run_auto_fix,
)
from pollypm.models import KnownProject, ProjectKind, PollyPMConfig, ProviderKind
from pollypm.onboarding import (
    CliAvailability,
    ConnectedAccount,
    LoginPreferences,
    LoginCancelled,
    _available_clis,
    _connect_account_via_tmux,
    _default_failover_accounts,
    _display_label,
    _detected_host_account,
    _recover_existing_accounts,
    build_onboarded_config,
    demo_project_fallback_destination,
    discover_recent_project_candidates,
    provision_demo_project_fallback,
)
from pollypm.projects import ensure_project_scaffold, make_project_key
from pollypm.session_services import create_tmux_client

ONBOARDING_STAGES = (
    ("accounts", "Connect account"),
    ("controller", "Choose controller"),
    ("projects", "Add projects"),
    ("tour", "Launch PollyPM"),
)


def stream_text(console: Console, text: str, chars_per_sec: int = 80) -> None:
    """Stream text through ``console`` unless animation should stay static."""
    if chars_per_sec <= 0 or os.environ.get("NO_COLOR") or not console.is_terminal:
        console.print(text, end="")
        return
    delay = 1.0 / chars_per_sec
    for char in text:
        console.file.write(char)
        console.file.flush()
        if char != "\n":
            time.sleep(delay)
    console.file.flush()


def installed_provider_statuses(statuses: list[CliAvailability]) -> list[CliAvailability]:
    return [status for status in statuses if status.installed]


def default_controller_account(accounts: dict[str, ConnectedAccount]) -> str | None:
    if not accounts:
        return None
    return next(iter(accounts))


def merge_selected_projects(
    existing: dict[str, KnownProject],
    selected_paths: list[Path],
) -> dict[str, KnownProject]:
    merged = dict(existing)
    existing_paths = {project.path.resolve() for project in merged.values()}
    keys_in_use = set(merged)
    for repo_path in selected_paths:
        normalized = repo_path.resolve()
        if normalized in existing_paths:
            continue
        project = KnownProject(
            key=make_project_key(normalized, keys_in_use),
            path=normalized,
            name=normalized.name,
            kind=ProjectKind.GIT,
        )
        ensure_project_scaffold(normalized)
        merged[project.key] = project
        existing_paths.add(normalized)
        keys_in_use.add(project.key)
    return merged


def onboarding_step_header(step: str) -> str:
    """Return the visible step counter + progress bar for ``step``."""
    stage_ids = [stage_id for stage_id, _label in ONBOARDING_STAGES]
    total = len(stage_ids)
    try:
        index = stage_ids.index(step)
    except ValueError:
        index = 0
    current = index + 1
    done = min(total, current)
    bar = "[#3ddc84]" + ("█" * done) + "[/][#253140]" + ("█" * (total - done)) + "[/]"
    label = ONBOARDING_STAGES[index][1]
    return f"[#5b8aff bold]Step {current} of {total}[/]  {bar}  [dim]{label}[/]"


def onboarding_progress_lines(step: str) -> list[str]:
    """Return the rail-friendly progress checklist for ``step``."""
    stage_ids = [stage_id for stage_id, _label in ONBOARDING_STAGES]
    try:
        current_index = stage_ids.index(step)
    except ValueError:
        current_index = 0

    lines: list[str] = []
    for index, (_stage_id, label) in enumerate(ONBOARDING_STAGES):
        if index < current_index:
            lines.append(f"[#3ddc84]✓[/] [#3ddc84]{label}[/]")
        elif index == current_index:
            lines.append(f"[#5b8aff]◉[/] [#5b8aff bold]{label}[/]")
        else:
            lines.append(f"[#6b7a88]○[/] [#6b7a88]{label}[/]")
    lines.append("")
    lines.append("[dim]Mouse is enabled. Click buttons, choices, and project selections directly.[/dim]")
    return lines


@dataclass(slots=True)
class OnboardingState:
    statuses: list[CliAvailability]
    accounts: dict[str, ConnectedAccount]
    known_projects: dict[str, KnownProject]
    login_preferences: LoginPreferences = field(default_factory=LoginPreferences)
    controller_account: str | None = None
    open_permissions_by_default: bool = True
    failover_enabled: bool = True
    recent_projects: list[Path] = field(default_factory=list)
    selected_project_paths: list[Path] = field(default_factory=list)
    seeded_demo_task_id: str | None = None
    scan_complete: bool = False
    scan_started: bool = False


@dataclass(slots=True)
class OnboardingResult:
    config_path: Path
    launch_requested: bool = False


@dataclass(slots=True)
class BlockingAutoFix:
    button_id: str
    label: str
    summary: str
    plan: AutoFixPlan


class ExitModal(ModalScreen[None]):
    CSS = """
    ModalScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.55);
    }
    #exit-dialog {
        width: 66;
        height: auto;
        padding: 1 2;
        background: #141a20;
        border: heavy #ff5f6d;
    }
    #exit-title {
        text-style: bold;
        color: #ff5f6d;
        padding-bottom: 1;
    }
    #exit-buttons {
        height: auto;
        padding-top: 1;
        align-horizontal: right;
    }
    #exit-buttons Button {
        margin-left: 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Stay")]

    def compose(self) -> ComposeResult:
        with CenterMiddle():
            with Vertical(id="exit-dialog"):
                yield Static("Leave onboarding?", id="exit-title")
                yield Static("Your current onboarding progress will be lost.")
                with Horizontal(id="exit-buttons"):
                    yield Button("Stay", variant="primary", id="stay")
                    yield Button("Quit", variant="error", id="quit")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None if event.button.id == "stay" else None)
        if event.button.id == "quit":
            self.app.exit(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class CodexLoginModeModal(ModalScreen[str | None]):
    CSS = """
    ModalScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.55);
    }
    #codex-mode-dialog {
        width: 72;
        height: auto;
        padding: 1 2;
        background: #141a20;
        border: heavy #5b8aff;
    }
    #codex-mode-title {
        text-style: bold;
        color: #f0f4f8;
        padding-bottom: 1;
    }
    .codex-mode-button {
        width: 1fr;
        min-height: 4;
        margin-right: 1;
    }
    #codex-mode-actions {
        height: auto;
        padding-top: 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Back")]

    def compose(self) -> ComposeResult:
        with CenterMiddle():
            with Vertical(id="codex-mode-dialog"):
                yield Static("Where is Polly running?", id="codex-mode-title")
                yield Static(
                    "Choose the Codex login path that matches this environment. "
                    "Local launches the normal browser-based flow. Remote/headless uses device auth."
                )
                with Horizontal(id="codex-mode-actions"):
                    yield Button(
                        "Local Machine\nBrowser-based login on this machine",
                        id="codex-mode-local",
                        variant="primary",
                        classes="codex-mode-button",
                    )
                    yield Button(
                        "Remote Or Headless\nUse device auth for VM/server sessions",
                        id="codex-mode-remote",
                        variant="warning",
                        classes="codex-mode-button",
                    )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "codex-mode-local":
            self.dismiss("local")
            return
        if event.button.id == "codex-mode-remote":
            self.dismiss("remote")

    def action_cancel(self) -> None:
        self.dismiss(None)


class DemoTaskModal(ModalScreen[str | None]):
    CSS = """
    ModalScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.55);
    }
    #demo-task-dialog {
        width: 78;
        height: auto;
        padding: 1 2;
        background: #141a20;
        border: heavy #3ddc84;
    }
    #demo-task-title {
        text-style: bold;
        color: #f0f4f8;
        padding-bottom: 1;
    }
    #demo-task-actions {
        height: auto;
        padding-top: 1;
        align-horizontal: right;
    }
    #demo-task-actions Button {
        margin-left: 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with CenterMiddle():
            with Vertical(id="demo-task-dialog"):
                yield Static("Keep the seeded demo task?", id="demo-task-title")
                yield Static(
                    "The demo repo includes one PollyPM task pointing at the intentional bug in demo_app.py. "
                    "Keeping it adds the task to the project DB so it shows up in the cockpit."
                )
                with Horizontal(id="demo-task-actions"):
                    yield Button("Keep", id="demo-task-keep", variant="primary")
                    yield Button("Forget", id="demo-task-forget", variant="warning")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "demo-task-keep":
            self.dismiss("keep")
            return
        if event.button.id == "demo-task-forget":
            self.dismiss("forget")

    def action_cancel(self) -> None:
        self.dismiss(None)


class OnboardingApp(App[OnboardingResult | None]):
    TITLE = "PollyPM"
    SUB_TITLE = "Setup"
    SCAN_FRAMES = ["◜", "◝", "◞", "◟"]

    CSS = """
    Screen {
        background: #0c0f12;
        color: #eef2f3;
    }

    #shell {
        height: 1fr;
        layout: horizontal;
    }

    #rail {
        width: 34;
        background: #0f1317;
        border-right: solid #1a2230;
        padding: 1 1 1 2;
    }

    #brand {
        color: #f0f4f8;
        text-style: bold;
        padding-bottom: 1;
    }

    .rail-block {
        margin-bottom: 1;
        padding: 1;
        border: round #253140;
        background: #141a20;
    }

    #main {
        width: 1fr;
        padding: 1 2;
    }

    #eyebrow {
        color: #6b7a88;
        padding-bottom: 1;
    }

    #title {
        color: #f0f4f8;
        text-style: bold;
        padding-bottom: 1;
    }

    #intro {
        color: #a8b8c4;
        padding-bottom: 1;
    }

    #message {
        height: auto;
        min-height: 1;
        color: #7ee8a4;
        padding-bottom: 1;
    }

    #step-host {
        height: 1fr;
    }

    .stage {
        height: 1fr;
    }

    .section {
        padding: 1;
        border: round #253140;
        background: #111820;
        margin-bottom: 1;
    }

    .section-title {
        text-style: bold;
        color: #e0e8ef;
        padding-bottom: 1;
    }

    .button-row {
        height: auto;
        padding-top: 1;
    }

    .button-row Button {
        margin-right: 1;
        min-width: 18;
    }

    .provider-button {
        width: 1fr;
        min-height: 3;
        margin-right: 1;
    }

    #provider-buttons {
        height: auto;
    }

    #controller-radio {
        padding-bottom: 1;
    }

    #project-list {
        height: 1fr;
        min-height: 12;
    }

    Footer {
        background: #0f1317;
        color: #4a5568;
    }
    """

    BINDINGS = [
        Binding("q", "request_quit", "Quit"),
        Binding("escape", "go_back", "Back"),
    ]

    def __init__(self, config_path: Path, force: bool = False, no_animation: bool = False) -> None:
        super().__init__()
        self.config_path = config_path
        self.force = force
        self.no_animation = no_animation
        self.root_dir = config_path.resolve().parent
        self.tmux = None
        self.step = "accounts"

        known_projects: dict[str, KnownProject] = {}
        open_permissions_by_default = True
        if config_path.exists():
            try:
                from pollypm.config import load_config

                existing = load_config(config_path)
                known_projects = existing.projects
                open_permissions_by_default = existing.pollypm.open_permissions_by_default
            except Exception:  # noqa: BLE001
                known_projects = {}

        accounts = _recover_existing_accounts(self.root_dir)
        for provider in (ProviderKind.CLAUDE, ProviderKind.CODEX):
            detected = _detected_host_account(provider)
            if detected is None or detected.account_name in accounts:
                continue
            accounts[detected.account_name] = detected
        statuses = _available_clis()
        controller = default_controller_account(accounts)
        self.state = OnboardingState(
            statuses=statuses,
            accounts=accounts,
            known_projects=known_projects,
            controller_account=controller,
            open_permissions_by_default=open_permissions_by_default,
            failover_enabled=len(accounts) > 1,
        )

        self.brand = Static("", id="brand")
        self.machine_check = Static(classes="rail-block")
        self.account_summary = Static(classes="rail-block")
        self.progress = Static(classes="rail-block")
        self.eyebrow_widget = Static("", id="eyebrow")
        self.title_widget = Static("", id="title")
        self.intro_widget = Static("", id="intro")
        self.message_widget = Static("", id="message")
        self.step_host = Vertical(id="step-host")
        self.controller_radio: RadioSet | None = None
        self.failover_checkbox: Checkbox | None = None
        self.permissions_checkbox: Checkbox | None = None
        self.project_selection: SelectionList[Path] | None = None
        self.scan_loading_widget: Static | None = None
        self.scan_frame_index = 0
        self.launch_button: Button | None = None
        self._pending_demo_repo_path: Path | None = None

    def _blocking_auto_fixes(self) -> list[BlockingAutoFix]:
        fixes: list[BlockingAutoFix] = []
        if not self._tmux_ready():
            result = check_tmux()
            if result.auto_fix is not None:
                fixes.append(
                    BlockingAutoFix(
                        button_id="fix-tmux",
                        label="Install tmux",
                        summary=result.status,
                        plan=result.auto_fix,
                    )
                )
        if installed_provider_statuses(self.state.statuses):
            return fixes
        for provider, check, label in (
            (ProviderKind.CLAUDE, check_claude_cli, "Install Claude CLI"),
            (ProviderKind.CODEX, check_codex_cli, "Install Codex CLI"),
        ):
            if any(status.provider is provider and status.installed for status in self.state.statuses):
                continue
            result = check()
            if result.auto_fix is None:
                continue
            fixes.append(
                BlockingAutoFix(
                    button_id=f"fix-{provider.value}-cli",
                    label=label,
                    summary=result.status,
                    plan=result.auto_fix,
                )
            )
        return fixes

    def compose(self) -> ComposeResult:
        with Horizontal(id="shell"):
            with Vertical(id="rail"):
                yield self.brand
                yield self.machine_check
                yield self.account_summary
                yield self.progress
            with Vertical(id="main"):
                yield self.eyebrow_widget
                yield self.title_widget
                yield self.intro_widget
                yield self.message_widget
                yield self.step_host
        yield Footer()

    def on_mount(self) -> None:
        self.title_widget = self.query_one("#title", Static)
        self.eyebrow_widget = self.query_one("#eyebrow", Static)
        self.intro_widget = self.query_one("#intro", Static)
        self.message_widget = self.query_one("#message", Static)
        self._refresh_rail()
        self._render_current_step()
        self.set_interval(0.3, self._tick_scan_animation)

    def action_request_quit(self) -> None:
        self.push_screen(ExitModal())

    def action_go_back(self) -> None:
        if self.step == "projects":
            self.step = "controller"
            self._render_current_step()
        elif self.step == "controller":
            self.step = "accounts"
            self._render_current_step()
        elif self.step == "tour":
            self.step = "projects"
            self._render_current_step()

    def _refresh_rail(self) -> None:
        self.brand.update(
            Panel.fit(
                "[#5b8aff bold]█▀█ █▀█ █   █   █▄█[/]\n"
                "[#3d6bcc]█▀▀ █▄█ █▄▄ █▄▄  █[/]\n\n"
                "[dim]Bring your CLI agents online with live accounts, recent projects, and a real control room.[/dim]",
                border_style="#3d6bcc",
            )
        )
        self.machine_check.update(self._machine_check_panel())
        self.account_summary.update(self._account_summary_panel())
        self.progress.update(self._progress_panel())

    def _machine_check_panel(self) -> Panel:
        table = Table.grid(expand=True)
        table.add_column(ratio=3)
        table.add_column(justify="right")
        for item in self.state.statuses:
            status = "[green]✓[/green]" if item.installed else "[red]✗[/red]"
            table.add_row(item.label, status)
        tmux_status = "[green]✓[/green]" if self._tmux_ready() else "[red]✗[/red]"
        table.add_row("tmux", tmux_status)
        return Panel(table, title="Machine", border_style="#253140")

    def _account_summary_panel(self) -> Panel:
        lines: list[str] = []
        if not self.state.accounts:
            lines.append("[dim]No connected accounts yet.[/dim]")
        else:
            for account in self.state.accounts.values():
                lines.append(f"[green]●[/green] [b]{account.email}[/b]")
                lines.append(f"  [dim]{account.provider.value} · {account.account_name}[/dim]")
        return Panel("\n".join(lines), title="Connected Accounts", border_style="#253140")

    def _progress_panel(self) -> Panel:
        return Panel(
            "\n".join(onboarding_progress_lines(self.step)),
            title="Setup Progress",
            border_style="#253140",
        )

    def _tmux_ready(self) -> bool:
        return shutil.which("tmux") is not None

    def _set_message(self, message: str = "") -> None:
        self.message_widget.update(message)

    def _render_current_step(self) -> None:
        self._refresh_rail()
        self.step_host.remove_children()
        if self.step == "accounts":
            self._render_accounts_step()
        elif self.step == "controller":
            self._render_controller_step()
        elif self.step == "projects":
            self._render_projects_step()
        elif self.step == "tour":
            self._render_tour_step()

    def _render_accounts_step(self) -> None:
        installed = installed_provider_statuses(self.state.statuses)
        auto_fixes = self._blocking_auto_fixes()
        self.eyebrow_widget.update(onboarding_step_header("accounts"))
        self.title_widget.update("Connect your first agent account")
        self.intro_widget.update(
            "Hi, I'm Polly — your AI coding PM.\n\n"
            "I'll connect your CLIs, pick a project, and open your control room. "
            "Most of the setup takes about a minute, and I handle most of it for you."
        )
        stage = Vertical(classes="stage")
        self.step_host.mount(stage)

        if not self._tmux_ready():
            blockers = Vertical(classes="section")
            stage.mount(blockers)
            blockers.mount(
                Panel(
                    "[red]tmux is required before PollyPM can continue.[/red]\n"
                    "Install tmux here, then keep going without restarting onboarding.",
                    title="tmux Required",
                    border_style="red",
                )
            )
            if auto_fixes:
                actions = Horizontal(classes="button-row")
                blockers.mount(actions)
                for item in auto_fixes:
                    if item.button_id == "fix-tmux":
                        actions.mount(Button(item.label, id=item.button_id, variant="warning"))
            return

        if not installed:
            blockers = Vertical(classes="section")
            stage.mount(blockers)
            blockers.mount(
                Panel(
                    "[red]No supported agent CLI was found.[/red]\n\n"
                    "Install Claude CLI or Codex CLI here, then continue without restarting onboarding.",
                    title="Install a CLI First",
                    border_style="red",
                )
            )
            if auto_fixes:
                actions = Horizontal(classes="button-row")
                blockers.mount(actions)
                for item in auto_fixes:
                    if item.button_id != "fix-tmux":
                        actions.mount(Button(item.label, id=item.button_id, variant="warning"))
            return

        intro_lines = [
            "Connect one real account to begin.",
            "After that, you can keep adding more now or later from the Accounts tab.",
        ]
        if any(account.home is None for account in self.state.accounts.values()):
            intro_lines.insert(0, "Found a host login and connected it automatically.")
        intro_section = Static("\n".join(intro_lines), classes="section")
        stage.mount(intro_section)
        provider_buttons = Horizontal(id="provider-buttons")
        provider_section = Vertical(classes="section")
        stage.mount(provider_section)
        provider_section.mount(Static("Available Providers", classes="section-title"))
        provider_section.mount(
            Static("Click a provider to choose its login path and open the native login window in tmux.")
        )
        provider_section.mount(provider_buttons)
        for status in installed:
            provider_buttons.mount(
                Button(
                    self._provider_button_label(status.provider),
                    id=f"connect-{status.provider.value}",
                    variant="primary",
                    classes="provider-button",
                )
            )

        if self.state.accounts:
            summary_lines = [
                f"[b]{len(self.state.accounts)}[/b] account{'s' if len(self.state.accounts) != 1 else ''} connected.",
                "You can keep connecting accounts now, or move on and add more later anytime.",
            ]
            actions = Horizontal(classes="button-row")
            ready_section = Vertical(classes="section")
            stage.mount(ready_section)
            ready_section.mount(Static("Ready To Move On", classes="section-title"))
            ready_section.mount(Static("\n".join(summary_lines)))
            ready_section.mount(actions)
            actions.mount(Button("Continue", id="accounts-continue", variant="success"))

    def _provider_button_label(self, provider: ProviderKind) -> str:
        if provider is ProviderKind.CLAUDE:
            return "Connect Claude\nPlanning, review, longer strategic loops"
        return "Connect Codex\nImplementation, shell-heavy work, fast coding loops"

    def _render_controller_step(self) -> None:
        self.eyebrow_widget.update(onboarding_step_header("controller"))
        self.title_widget.update("Choose the account that runs PollyPM")
        self.intro_widget.update(
            "This account runs Polly and heartbeat. If it becomes unavailable, PollyPM can fail over to another "
            "healthy connected account automatically."
        )
        stage = Vertical(classes="stage")
        if not self.state.accounts:
            self.step = "accounts"
            self._render_current_step()
            return

        self.step_host.mount(stage)
        default_choice = self.state.controller_account or default_controller_account(self.state.accounts)
        self.state.controller_account = default_choice
        radio_buttons = [
            RadioButton(
                _display_label(account),
                value=account_name == default_choice,
                id=f"controller-{account_name}",
            )
            for account_name, account in self.state.accounts.items()
        ]
        self.controller_radio = RadioSet(*radio_buttons, id="controller-radio")
        self.failover_checkbox = Checkbox(
            "Fail over automatically if the control account becomes unavailable",
            value=self.state.failover_enabled and len(self.state.accounts) > 1,
            id="failover-checkbox",
            disabled=len(self.state.accounts) <= 1,
        )
        self.permissions_checkbox = Checkbox(
            "Launch sessions with open permissions by default",
            value=self.state.open_permissions_by_default,
            id="open-permissions-checkbox",
        )
        control_section = Vertical(classes="section")
        stage.mount(control_section)
        control_section.mount(Static("Control Account", classes="section-title"))
        control_section.mount(
            Static(
                "Pick the account PollyPM should live on. It can still do real work, but Polly will treat it "
                "as the last resort for brand-new worker assignment."
            )
        )
        control_section.mount(self.controller_radio)
        control_section.mount(
            Static(
                "By default, PollyPM can launch sessions in permissive mode so agents can keep moving without repeated approval prompts."
            )
        )
        control_section.mount(self.permissions_checkbox)
        control_section.mount(self.failover_checkbox)
        actions = Horizontal(classes="button-row")
        stage.mount(actions)
        actions.mount(Button("Back", id="controller-back"))
        actions.mount(Button("Continue", id="controller-next", variant="success"))

    def _render_projects_step(self) -> None:
        self.eyebrow_widget.update(onboarding_step_header("projects"))
        self.title_widget.update("Add active projects")
        self.intro_widget.update(
            "PollyPM looked through your home folder for git repos where your local git identity authored a commit "
            "in the last 14 days. These are the repos most likely to matter right now."
        )
        stage = Vertical(classes="stage")
        self.step_host.mount(stage)

        if not self.state.scan_started:
            self.state.scan_started = True
            self.project_selection = None
            self.scan_loading_widget = Static("", id="scan-loading")
            loading = Vertical(classes="section")
            stage.mount(loading)
            loading.mount(Static("Project Detection", classes="section-title"))
            loading.mount(self.scan_loading_widget)
            self._update_scan_loading()
            self.run_worker(self._scan_recent_projects, thread=True, group="onboarding-project-scan", exclusive=True)
            return

        if not self.state.scan_complete:
            self.scan_loading_widget = Static("", id="scan-loading")
            loading = Vertical(classes="section")
            stage.mount(loading)
            loading.mount(Static("Project Detection", classes="section-title"))
            loading.mount(self.scan_loading_widget)
            self._update_scan_loading()
            return

        self.scan_loading_widget = None

        if not self.state.recent_projects:
            empty = Vertical(classes="section")
            stage.mount(empty)
            empty.mount(Static("Nothing Recent Found", classes="section-title"))
            demo_target = demo_project_fallback_destination(self.config_path)
            empty.mount(
                Static(
                    "No recently active git repos were found in your home folder.\n\n"
                    "You can copy a self-contained demo repo and exercise PollyPM locally without any network dependency, "
                    "or skip this step and add projects later anytime from the control room.\n\n"
                    f"[dim]Demo target: {demo_target}[/dim]"
                )
            )
            demo_actions = Horizontal(classes="button-row")
            empty.mount(demo_actions)
            demo_actions.mount(
                Button("Use Demo Repo", id="projects-use-demo", variant="primary")
            )
        else:
            selections = []
            selected_set = {path.resolve() for path in self.state.selected_project_paths}
            for path in self.state.recent_projects:
                label = f"{path.name}\n[dim]{path}[/dim]"
                selections.append((label, path, path.resolve() in selected_set))
            self.project_selection = SelectionList(*selections, id="project-list")
            project_section = Vertical(classes="section")
            stage.mount(project_section)
            project_section.mount(Static("Recently Active Repositories", classes="section-title"))
            project_section.mount(
                Static(
                    "Everything below is preselected as a suggestion. Deselect anything you do not want PollyPM "
                    "to track yet. You can always add more later."
                )
            )
            project_section.mount(self.project_selection)

        actions = Horizontal(classes="button-row")
        stage.mount(actions)
        actions.mount(Button("Back", id="projects-back"))
        finish_label = "Finish Setup" if not self.state.recent_projects else "Add Selected And Finish"
        actions.mount(Button(finish_label, id="projects-finish", variant="success"))

    def _render_tour_step(self) -> None:
        self.eyebrow_widget.update(onboarding_step_header("tour"))
        self.title_widget.update("Your cockpit is ready")
        self.intro_widget.update("")
        stage = Vertical(classes="stage")
        self.step_host.mount(stage)

        # ── What you'll see
        layout_section = Vertical(classes="section")
        stage.mount(layout_section)
        layout_section.mount(Static("What you'll see", classes="section-title"))
        layout_table = Table.grid(expand=True, padding=(0, 1))
        layout_table.add_column(style="bold #5b8aff", width=14)
        layout_table.add_column(style="#a8b8c4")
        layout_table.add_row("Left rail", "Navigate between Polly, your inbox, projects, and settings.")
        layout_table.add_row("Right pane", "Shows the live agent session or project details for whatever you select.")
        layout_table.add_row("Polly", "Your AI project manager. Tell Polly what to build and she'll spin up workers.")
        layout_table.add_row("Heartbeat", "Runs quietly in the background monitoring all sessions for drift or stalls.")
        layout_section.mount(Static(Group(layout_table)))

        # ── Key controls
        keys_section = Vertical(classes="section")
        stage.mount(keys_section)
        keys_section.mount(Static("Key controls", classes="section-title"))
        keys_table = Table.grid(expand=True, padding=(0, 1))
        keys_table.add_column(style="bold #e0e8ef", width=14)
        keys_table.add_column(style="#a8b8c4")
        keys_table.add_row("j / k", "Move up and down in the sidebar.")
        keys_table.add_row("Enter", "Open the selected item in the right pane.")
        keys_table.add_row("N", "Launch a new worker session for the selected project.")
        keys_table.add_row("S", "Jump to settings (accounts, failover, permissions).")
        keys_table.add_row("Ctrl-W", "Detach from PollyPM. Everything keeps running in the background.")
        keys_table.add_row("Ctrl-Q", "Shut down PollyPM and stop all agent sessions.")
        keys_section.mount(Static(Group(keys_table)))

        shortcuts_section = Vertical(classes="section")
        stage.mount(shortcuts_section)
        shortcuts_section.mount(Static("Shortcut commands", classes="section-title"))
        shortcuts_table = Table.grid(expand=True, padding=(0, 1))
        shortcuts_table.add_column(style="bold #e0e8ef", width=14)
        shortcuts_table.add_column(style="#a8b8c4")
        for label, commands in shortcut_rows():
            shortcuts_table.add_row(label, commands)
        shortcuts_section.mount(Static(Group(shortcuts_table)))

        # ── Tips
        tips_section = Vertical(classes="section")
        stage.mount(tips_section)
        tips_section.mount(Static("Tips", classes="section-title"))
        tips_section.mount(Static(
            "[#a8b8c4]You can reopen PollyPM anytime with [bold #e0e8ef]pm[/] from any terminal.\n"
            "Run [bold #e0e8ef]pm shortcuts[/] anytime for the same quick command list.\n"
            "Add more accounts or projects later from settings.\n"
            "PollyPM stores all state in [bold #e0e8ef]~/.pollypm/[/] — nothing is written to your project repos.[/]"
        ))

        actions = Horizontal(classes="button-row")
        stage.mount(actions)
        actions.mount(Button("Back", id="tour-back"))
        self.launch_button = Button("\u25b6  Open PollyPM", id="tour-launch", variant="success")
        actions.mount(self.launch_button)
        self.call_after_refresh(self._focus_launch_button)

    def _focus_launch_button(self) -> None:
        if self.launch_button is not None:
            self.launch_button.focus()

    def _scan_recent_projects(self) -> list[Path]:
        return discover_recent_project_candidates(self.config_path)

    def _update_scan_loading(self) -> None:
        if self.scan_loading_widget is None:
            return
        frame = self.SCAN_FRAMES[self.scan_frame_index % len(self.SCAN_FRAMES)]
        self.scan_loading_widget.update(
            f"{frame} Scanning for recently active repositories...\n\n[dim]This can take a moment if your home folder has a lot of repositories.[/dim]"
        )

    def _tick_scan_animation(self) -> None:
        if self.step != "projects" or self.state.scan_complete or self.scan_loading_widget is None:
            return
        self.scan_frame_index = (self.scan_frame_index + 1) % len(self.SCAN_FRAMES)
        self._update_scan_loading()

    def _build_config(self) -> PollyPMConfig:
        controller = self.state.controller_account or default_controller_account(self.state.accounts)
        if controller is None:
            raise ValueError("At least one connected account is required.")
        failover_accounts = (
            _default_failover_accounts(self.state.accounts, controller)
            if self.state.failover_enabled
            else []
        )
        projects = merge_selected_projects(self.state.known_projects, self.state.selected_project_paths)
        return build_onboarded_config(
            root_dir=self.root_dir,
            accounts=self.state.accounts,
            controller_account=controller,
            open_permissions_by_default=self.state.open_permissions_by_default,
            failover_enabled=self.state.failover_enabled and bool(failover_accounts),
            failover_accounts=failover_accounts,
            projects=projects,
        )

    def _ensure_tmux_client(self):
        if self.tmux is None:
            self.tmux = create_tmux_client()
        return self.tmux

    def _run_machine_fix(self, button_id: str) -> None:
        for item in self._blocking_auto_fixes():
            if item.button_id != button_id:
                continue
            self._set_message(f"Running: {item.label}…")
            try:
                with self.suspend():
                    success, detail = run_auto_fix(item.plan)
            except Exception as exc:  # noqa: BLE001
                success, detail = (False, str(exc))
            self.state.statuses = _available_clis()
            self.refresh(repaint=True, layout=True)
            self._render_current_step()
            self._set_message(detail)
            return

    def _connect_provider(self, provider: ProviderKind, *, login_preferences: LoginPreferences | None = None) -> None:
        index = len([account for account in self.state.accounts.values() if account.provider is provider]) + 1
        self._set_message(f"Opening a {provider.value} login window…")
        preferences = login_preferences or self.state.login_preferences
        try:
            with self.suspend():
                account = _connect_account_via_tmux(
                    self._ensure_tmux_client(),
                    root_dir=self.root_dir,
                    provider=provider,
                    index=index,
                    quiet=True,
                    preferences=preferences,
                )
        except LoginCancelled as exc:
            self.refresh(repaint=True, layout=True)
            self._render_current_step()
            self._set_message(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            self.refresh(repaint=True, layout=True)
            self._render_current_step()
            self._set_message(f"Login did not complete cleanly: {exc}")
            return
        self.refresh(repaint=True, layout=True)
        if account.account_name in self.state.accounts:
            self._set_message(f"{account.email} is already connected.")
            return
        self.state.accounts[account.account_name] = account
        if self.state.controller_account is None:
            self.state.controller_account = account.account_name
        self.state.failover_enabled = len(self.state.accounts) > 1
        self._set_message(f"Connected {account.email} [{account.provider.value}].")
        self._render_current_step()

    @on(Button.Pressed)
    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id.startswith("fix-"):
            self._run_machine_fix(button_id)
            return
        if button_id == "connect-claude":
            self._connect_provider(ProviderKind.CLAUDE)
            return
        if button_id == "connect-codex":
            self.push_screen(CodexLoginModeModal(), self._handle_codex_login_mode)
            return
        if button_id == "accounts-continue":
            self.step = "controller"
            self._render_current_step()
            return
        if button_id == "controller-back":
            self.step = "accounts"
            self._render_current_step()
            return
        if button_id == "controller-next":
            self.step = "projects"
            self._render_current_step()
            return
        if button_id == "projects-back":
            self.step = "controller"
            self._render_current_step()
            return
        if button_id == "projects-use-demo":
            try:
                demo_path = provision_demo_project_fallback(self.config_path)
            except Exception as exc:  # noqa: BLE001
                self._set_message(f"Could not prepare the demo repo: {exc}")
                return
            self._pending_demo_repo_path = demo_path
            self.push_screen(DemoTaskModal(), self._handle_demo_task_choice)
            return
        if button_id == "projects-finish":
            if self.project_selection is not None:
                self.state.selected_project_paths = list(self.project_selection.selected)
            config = self._build_config()
            write_config(config, path=self.config_path, force=True)
            self.step = "tour"
            self._render_current_step()
            return
        if button_id == "tour-back":
            self.step = "projects"
            self._render_current_step()
            return
        if button_id == "tour-launch":
            self.exit(OnboardingResult(config_path=self.config_path, launch_requested=True))

    @on(RadioSet.Changed)
    def on_controller_changed(self, event: RadioSet.Changed) -> None:
        if event.radio_set.id != "controller-radio":
            return
        if event.pressed is None or event.pressed.id is None:
            return
        self.state.controller_account = event.pressed.id.replace("controller-", "", 1)

    @on(Checkbox.Changed)
    def on_failover_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id != "failover-checkbox":
            if event.checkbox.id == "open-permissions-checkbox":
                self.state.open_permissions_by_default = event.value
            return
        self.state.failover_enabled = event.value

    def _handle_codex_login_mode(self, mode: str | None) -> None:
        if mode is None:
            self._set_message("Codex login cancelled.")
            return
        preferences = LoginPreferences(codex_headless=(mode == "remote"))
        self.state.login_preferences = preferences
        self._connect_provider(ProviderKind.CODEX, login_preferences=preferences)

    @on(SelectionList.SelectedChanged)
    def on_project_selection_changed(self, event: SelectionList.SelectedChanged) -> None:
        if event.selection_list.id != "project-list":
            return
        self.state.selected_project_paths = list(event.selection_list.selected)

    @on(Worker.StateChanged)
    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.group != "onboarding-project-scan":
            return
        if event.state is WorkerState.ERROR:
            self.state.scan_complete = True
            self.state.recent_projects = []
            self._set_message("Project scan failed. You can add projects later from the control room.")
            self._render_current_step()
            return
        if event.state is not WorkerState.SUCCESS:
            return
        self.state.recent_projects = list(event.worker.result)
        self.state.selected_project_paths = list(event.worker.result)
        self.state.scan_complete = True
        if self.state.recent_projects:
            self._set_message(f"Found {len(self.state.recent_projects)} recently active repo suggestion(s).")
        else:
            self._set_message(
                "No recent repos matched your local commit history. You can use the demo repo fallback or add projects later."
            )
        self._render_current_step()

    def _handle_demo_task_choice(self, choice: str | None) -> None:
        demo_path = self._pending_demo_repo_path
        self._pending_demo_repo_path = None
        if demo_path is None:
            return

        self.state.recent_projects = [demo_path]
        self.state.selected_project_paths = [demo_path]
        if choice == "keep":
            from pollypm.onboarding import seed_demo_project_task

            project_key = make_project_key(demo_path, set(self.state.known_projects))
            try:
                task_id = seed_demo_project_task(demo_path, project_key=project_key)
            except Exception as exc:  # noqa: BLE001
                self.state.seeded_demo_task_id = None
                self._set_message(f"Demo repo copied, but seeding the task failed: {exc}")
            else:
                self.state.seeded_demo_task_id = task_id
                self._set_message(
                    f"Demo repo ready at {demo_path}. Seeded task {task_id} is waiting in the cockpit."
                )
        elif choice == "forget":
            self.state.seeded_demo_task_id = None
            self._set_message(f"Demo repo ready at {demo_path}. No seeded task was kept.")
        else:
            self.state.seeded_demo_task_id = None
            self._set_message(f"Demo repo ready at {demo_path}.")
        self._render_current_step()


def run_onboarding_app(config_path: Path, force: bool = False, no_animation: bool = False) -> OnboardingResult:
    console = Console()
    intro = (
        "Hi, I'm Polly — your AI coding PM.\n\n"
        "I'll connect your CLIs, pick a project, and open your control room. "
        "About a minute, most of which I'll handle for you.\n\n"
        "Let's go.\n"
    )
    if no_animation or os.environ.get("NO_COLOR") or not console.is_terminal:
        console.print(intro, end="")
    else:
        stream_text(console, intro, chars_per_sec=80)
    try:
        app = OnboardingApp(config_path=config_path, force=force, no_animation=no_animation)
    except TypeError as exc:
        if "no_animation" not in str(exc):
            raise
        app = OnboardingApp(config_path=config_path, force=force)
    result = app.run(mouse=True)
    if result is None:
        raise SystemExit(1)
    return result
