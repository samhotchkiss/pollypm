"""Cockpit project settings panel.

Contract:
- Inputs: a cockpit config path and project key.
- Outputs: ``PollyProjectSettingsApp`` for project session/account control.
- Side effects: loads config, inspects project sessions, and issues
  service calls to stop or retarget the project's worker session.
- Invariants: rendering and behavior stay local to this screen; callers
  can continue importing the app from ``pollypm.cockpit_ui`` for
  compatibility while the implementation lives here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Static

from pollypm.config import load_config
from pollypm.cockpit_settings_history import (
    UndoAction,
    make_undo_action,
    undo_expired,
)
from pollypm.models import ProviderKind
from pollypm.service_api import PollyPMService
from pollypm.work.sqlite_service import SQLiteWorkService


class PollyProjectSettingsApp(App[None]):
    TITLE = "PollyPM"
    SUB_TITLE = "Project Settings"
    CSS = """
    Screen {
        background: #0c0f12;
        color: #eef2f4;
        padding: 1;
    }
    #title-bar {
        height: 1;
        color: #5b8aff;
        text-style: bold;
        padding-bottom: 1;
    }
    #message {
        height: 1;
        color: #7ee8a4;
        padding-bottom: 1;
    }
    #preview {
        height: auto;
        color: #c8d3dd;
        background: #111820;
        border: round #253140;
        padding: 1;
        margin-bottom: 1;
    }
    .settings-section {
        padding: 1;
        border: round #253140;
        background: #111820;
        margin-bottom: 1;
    }
    .section-label {
        color: #5b8aff;
        text-style: bold;
        padding-bottom: 1;
    }
    #actions {
        height: auto;
        padding-top: 1;
    }
    #actions Button {
        margin-right: 1;
    }
    """
    BINDINGS = [
        Binding("u", "undo_recent_change", "Undo", show=False),
        Binding("r", "refresh", "Refresh", show=False),
        Binding("enter", "apply_preview", "Apply", show=False),
        Binding("question_mark", "show_keyboard_help", "Help", priority=True),
    ]

    def __init__(self, config_path: Path, project_key: str) -> None:
        super().__init__()
        self.config_path = config_path
        self.project_key = project_key
        self.title_bar = Static("", id="title-bar")
        self.message_bar = Static("", id="message")
        self.preview_bar = Static("", id="preview", markup=True)
        self._undo_action: UndoAction | None = None

    def compose(self) -> ComposeResult:
        yield self.title_bar
        yield self.message_bar
        yield self.preview_bar
        with Vertical(classes="settings-section"):
            yield Static("Worker Session", classes="section-label")
            yield Static("", id="worker-info")
        with Vertical(classes="settings-section"):
            yield Static("Model & Account", classes="section-label")
            yield Static("", id="model-info")
        with Vertical(classes="settings-section"):
            yield Static("Recent Tasks", classes="section-label")
            yield Static("", id="task-info")
        with Horizontal(id="actions"):
            yield Button(Text("[R] Reset Session"), id="reset-session", variant="warning")
            yield Button(Text("[C] Switch to Claude"), id="switch-claude", variant="primary")
            yield Button(Text("[X] Switch to Codex"), id="switch-codex", variant="primary")
            yield Button(Text("[U] Undo"), id="undo", variant="default")

    def on_mount(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        config = load_config(self.config_path)
        project = config.projects.get(self.project_key)
        if project is None:
            self.title_bar.update(f"Project not found: {self.project_key}")
            return
        self.title_bar.update(f"{project.name or project.key} • Settings")

        worker = None
        for session in config.sessions.values():
            if session.role == "worker" and session.project == self.project_key and session.enabled:
                worker = session
                break

        worker_info = self.query_one("#worker-info", Static)
        model_info = self.query_one("#model-info", Static)
        task_info = self.query_one("#task-info", Static)

        if worker is None:
            worker_info.update("No worker session configured.\nPress N in the sidebar to create one.")
            model_info.update("")
            task_info.update("")
            self.preview_bar.update("[dim]Preview unavailable until a worker session exists.[/dim]")
            return

        account = config.accounts.get(worker.account)
        account_label = f"{account.email} [{account.provider.value}]" if account else worker.account
        provider_budget = self._provider_budget_label(worker.provider.value, account_label=account_label)
        worker_info.update(
            f"[dim]Session:[/] [bold]{worker.name}[/]\n"
            f"[dim]Window:[/]  {worker.window_name}\n"
            f"[dim]CWD:[/]     {worker.cwd}"
        )
        model_info.update(
            f"[dim]Provider:[/] [bold]{worker.provider.value}[/]\n"
            f"[dim]Account:[/]  {account_label}\n"
            f"[dim]Budget:[/]   {provider_budget}\n"
            f"[dim]Args:[/]     {' '.join(worker.args) if worker.args else 'none'}"
        )
        task_info.update(self._render_recent_tasks(worker, config_path=self.config_path))
        self.preview_bar.update(self._build_preview(worker, account_label=account_label))

    def _notify(self, msg: str) -> None:
        self.message_bar.update(msg)

    def _provider_budget_label(self, provider: str, *, account_label: str) -> str:
        label = provider.lower()
        return f"[b]{label}[/b] · budget tracked against {account_label}"

    def _render_recent_tasks(self, worker, *, config_path: Path) -> str:
        config = load_config(config_path)
        project = config.projects.get(self.project_key)
        project_path = getattr(project, "path", None)
        if project is None or project_path is None:
            return "[dim]No project found.[/dim]"
        db_path = project_path / ".pollypm" / "state.db"
        if not db_path.exists():
            return "[dim]No project database yet.[/dim]"
        try:
            with SQLiteWorkService(db_path=db_path, project_path=project_path) as svc:
                tasks = svc.list_tasks(assignee=worker.name, limit=5)
        except Exception as exc:  # noqa: BLE001
            return f"[dim]Recent tasks unavailable: {exc}[/dim]"
        if not tasks:
            return "[dim]No recent tasks assigned to this session.[/dim]"
        lines = []
        for task in sorted(tasks, key=lambda t: getattr(t, "updated_at", None), reverse=True)[:3]:
            status = getattr(task.work_status, "value", str(task.work_status))
            lines.append(
                f"• [b]{task.task_id}[/b] [dim]{status}[/dim] "
                f"[dim]{task.title}[/dim]"
            )
        return "\n".join(lines)

    def _build_preview(self, worker, *, account_label: str) -> str:
        lines = [
            "[b]Diff preview[/b]",
            f"Current: [b]{worker.provider.value}[/b] on {account_label}",
        ]
        claude = self._target_account_for(ProviderKind.CLAUDE)
        codex = self._target_account_for(ProviderKind.CODEX)
        lines.append(f"Claude target: {claude or '[dim]none available[/dim]'}")
        lines.append(f"Codex target: {codex or '[dim]none available[/dim]'}")
        lines.append("[dim]Press Enter or click a switch button to apply. U undoes the last reversible switch for 24h.[/dim]")
        return "\n".join(lines)

    def _target_account_for(self, target_provider: ProviderKind) -> str | None:
        config = load_config(self.config_path)
        for name, account in config.accounts.items():
            if account.provider is target_provider:
                return name
        return None

    def _record_undo(self, label: str, apply: Callable[[], None]) -> None:
        self._undo_action = make_undo_action(label, apply)

    def _clear_undo(self) -> None:
        self._undo_action = None

    @on(Button.Pressed, "#reset-session")
    def on_reset(self, _event: Button.Pressed | None) -> None:
        config = load_config(self.config_path)
        worker = None
        for session in config.sessions.values():
            if session.role == "worker" and session.project == self.project_key and session.enabled:
                worker = session
                break
        if worker is None:
            self._notify("No worker session to reset.")
            return
        try:
            PollyPMService(self.config_path).stop_session(worker.name)
            self._notify(f"Session {worker.name} stopped. Press N to relaunch.")
        except Exception as exc:  # noqa: BLE001
            self._notify(f"Reset failed: {exc}")
        self._clear_undo()
        self._refresh()

    @on(Button.Pressed, "#switch-claude")
    def on_switch_claude(self, _event: Button.Pressed | None) -> None:
        self._switch_provider(ProviderKind.CLAUDE)

    @on(Button.Pressed, "#switch-codex")
    def on_switch_codex(self, _event: Button.Pressed | None) -> None:
        self._switch_provider(ProviderKind.CODEX)

    @on(Button.Pressed, "#undo")
    def on_undo(self, _event: Button.Pressed) -> None:
        self.action_undo_recent_change()

    def _switch_provider(self, target_provider: ProviderKind) -> None:
        config = load_config(self.config_path)
        worker = None
        for session in config.sessions.values():
            if session.role == "worker" and session.project == self.project_key and session.enabled:
                worker = session
                break
        if worker is None:
            self._notify("No worker session to switch.")
            return
        target_account = None
        for name, account in config.accounts.items():
            if account.provider is target_provider:
                target_account = name
                break
        if target_account is None:
            self._notify(f"No {target_provider.value} account available.")
            return
        if worker.provider is target_provider:
            self._notify(f"Already using {target_provider.value}.")
            return
        try:
            previous_account = worker.account
            PollyPMService(self.config_path).switch_session_account(worker.name, target_account)
            self._record_undo(
                f"restore {worker.name} -> {previous_account}",
                lambda: PollyPMService(self.config_path).switch_session_account(
                    worker.name,
                    previous_account,
                ),
            )
            self._notify(f"Switched to {target_provider.value} ({target_account}). Session restarted.")
        except Exception as exc:  # noqa: BLE001
            self._notify(f"Switch failed: {exc}")
        self._refresh()

    def action_refresh(self) -> None:
        self._refresh()

    def action_undo_recent_change(self) -> None:
        action = self._undo_action
        if undo_expired(action):
            self._clear_undo()
            action = None
        if action is None:
            self._notify("Nothing recent to undo.")
            return
        try:
            action.apply()
            self._notify(f"Undid {action.label}.")
        except Exception as exc:  # noqa: BLE001
            self._notify(f"Undo failed: {exc}")
        finally:
            self._clear_undo()
        self._refresh()

    def action_apply_preview(self) -> None:
        self._notify("Use the action buttons to apply a change.")

    def action_show_keyboard_help(self) -> None:
        self._notify("j/k move, Enter apply preview target, u undo, r refresh.")
