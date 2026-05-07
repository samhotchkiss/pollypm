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
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, RadioButton, RadioSet, Select, Static

from pollypm.account_usage_sampler import load_cached_account_usage
from pollypm.config import load_config, write_config
from pollypm.cockpit_settings_history import (
    UndoAction,
    consume_settings_history,
    history_rationale_for_account,
    latest_settings_history_entry,
    make_undo_action,
    record_settings_history,
    undo_expired,
)
from pollypm.model_registry import advisories_for, load_registry, resolve_alias
from pollypm.models import ModelAssignment, ProviderKind
from pollypm.role_routing import resolve_role_assignment
from pollypm.service_api import PollyPMService
from pollypm.work import create_work_service

_PROJECT_ROLE_KEYS = ("architect", "worker", "reviewer")
_ROLE_LABELS = {
    "operator_pm": "Operator PM",
    "architect": "Architect",
    "worker": "Worker",
    "reviewer": "Reviewer",
}


def _role_label(role: str) -> str:
    return _ROLE_LABELS.get(role, role.replace("_", " ").title())


def _configured_role_summary(
    assignment: ModelAssignment | None,
    *,
    registry,
) -> str:
    if assignment is None:
        return "inherit"
    if assignment.alias is not None:
        if resolve_alias(assignment.alias, registry=registry) is None:
            return f"alias:{assignment.alias} (missing)"
        return f"alias:{assignment.alias}"
    return f"{assignment.provider}/{assignment.model}"


def _project_role_source_label(source: str) -> str:
    if source == "project":
        return "project override"
    if source == "global":
        return "inherited global"
    if source == "fallback":
        return "inherited fallback"
    return source


def _role_source_style(source: str) -> str:
    return {
        "project": "#5b8aff",
        "global": "#3ddc84",
        "fallback": "#97a6b2",
    }.get(source, "#97a6b2")


def _build_project_role_rows(config, project_key: str, registry) -> list[dict]:
    rows: list[dict] = []
    project = getattr(config, "projects", {}).get(project_key)
    assignments = getattr(project, "role_assignments", {}) if project is not None else {}
    for role in _PROJECT_ROLE_KEYS:
        configured = assignments.get(role) if assignments is not None else None
        resolved = resolve_role_assignment(
            role,
            project_key,
            config=config,
            registry=registry,
        )
        advisories = advisories_for(
            role,
            ModelAssignment(alias=resolved.alias)
            if resolved.alias is not None
            else ModelAssignment(provider=resolved.provider, model=resolved.model),
            registry=registry,
        )
        rows.append(
            {
                "role": role,
                "label": _role_label(role),
                "configured_summary": _configured_role_summary(
                    configured,
                    registry=registry,
                ),
                "configured_alias": configured.alias if configured is not None else None,
                "configured_provider": configured.provider if configured is not None else None,
                "configured_model": configured.model if configured is not None else None,
                "configured_missing_alias": bool(
                    configured is not None
                    and configured.alias is not None
                    and resolve_alias(configured.alias, registry=registry) is None
                ),
                "resolved_provider": resolved.provider,
                "resolved_model": resolved.model,
                "resolved_alias": resolved.alias,
                "resolved_summary": f"{resolved.provider}/{resolved.model}",
                "source": resolved.source,
                "source_label": _project_role_source_label(resolved.source),
                "advisories": advisories,
                "has_override": configured is not None,
            }
        )
    return rows

_PROJECT_GUIDE_ROLES: tuple[str, ...] = ("architect", "worker", "reviewer")


def _project_guides_dir(project_path: Path) -> Path:
    return Path(project_path) / ".pollypm" / "project-guides"


def _project_guide_path(project_path: Path, role: str) -> Path:
    return _project_guides_dir(project_path) / f"{role}.md"


def _drift_value(info: object, field: str, default: object = None) -> object:
    if isinstance(info, dict):
        return info.get(field, default)
    return getattr(info, field, default)


def _project_guide_drift_info(project_path: Path, role: str) -> object | None:
    try:
        from pollypm.project_guides import project_guide_drift_info as helper
    except Exception:  # noqa: BLE001
        helper = None
    if callable(helper):
        try:
            return helper(project_path, role)
        except Exception:  # noqa: BLE001
            pass
    try:
        from pollypm import doctor as doctor_mod
    except Exception:  # noqa: BLE001
        return None
    try:
        return doctor_mod._project_guide_drift_info(project_path, role)
    except Exception:  # noqa: BLE001
        return None
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
    #project-role-table {
        height: 9;
        background: #111820;
        margin-bottom: 1;
    }
    #project-role-editor {
        height: auto;
        margin-bottom: 1;
    }
    #project-role-editor > * {
        margin-right: 1;
    }
    #project-role-alias {
        width: 34;
    }
    #project-role-provider, #project-role-model {
        width: 22;
    }
    #project-role-note {
        color: #97a6b2;
        content-align: left middle;
    }
    #project-role-detail {
        height: auto;
        color: #c8d3dd;
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
        try:
            role_alias_options = [
                (
                    f"{alias} -> {record.provider}/{record.model}",
                    alias,
                )
                for alias, record in sorted(load_registry().aliases.items())
            ]
        except Exception:  # noqa: BLE001
            role_alias_options = []
        self.title_bar = Static("", id="title-bar")
        self.message_bar = Static("", id="message")
        self.preview_bar = Static("", id="preview", markup=True)
        self.role_table = DataTable(id="project-role-table")
        self.role_alias_select = Select(
            role_alias_options,
            prompt="Registry alias",
            allow_blank=True,
            id="project-role-alias",
        )
        self.role_provider_input = Input(
            placeholder="provider",
            id="project-role-provider",
        )
        self.role_model_input = Input(
            placeholder="model",
            id="project-role-model",
        )
        self._undo_action: UndoAction | None = None
        self._selected_role_key: str | None = None
        self._visible_role_rows: list[dict] = []
        self._syncing_role_editor = False
        self._suppressed_role_alias_values: list[str] = []

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
            yield Static("Role Assignments", classes="section-label")
            yield self.role_table
            with Horizontal(id="project-role-editor"):
                yield self.role_alias_select
                yield self.role_provider_input
                yield self.role_model_input
                yield Button("Inherit Global", id="project-role-inherit")
                yield Static(
                    "Pick an alias or type both fields to save a custom override.",
                    id="project-role-note",
                )
            yield Static("", id="project-role-detail", markup=True)
        with Vertical(classes="settings-section"):
            yield Static("Recent Tasks", classes="section-label")
            yield Static("", id="task-info")
        with Vertical(classes="settings-section"):
            yield Static("Project Guides", classes="section-label")
            yield Static("", id="guide-info")
        with Vertical(classes="settings-section", id="release-channel-section"):
            yield Static("Release channel", classes="section-label")
            yield Static(
                "Stable: Production builds. Recommended.\n"
                "Beta: Pre-release builds. Faster features, occasional breakage.",
                id="release-channel-explainer",
            )
            yield RadioSet(
                RadioButton("Stable", id="release-channel-stable"),
                RadioButton("Beta", id="release-channel-beta"),
                id="release-channel-radio",
            )
        with Horizontal(id="actions"):
            yield Button(Text("[R] Reset Session"), id="reset-session", variant="warning")
            yield Button(Text("[C] Switch to Claude"), id="switch-claude", variant="primary")
            yield Button(Text("[X] Switch to Codex"), id="switch-codex", variant="primary")
            yield Button(Text("[U] Undo"), id="undo", variant="default")

    def on_mount(self) -> None:
        self.role_table.cursor_type = "row"
        self.role_table.zebra_stripes = True
        self.role_table.add_columns("Role", "Configured", "Resolved", "Source", "Warn")
        self._refresh()

    def _refresh(self) -> None:
        config = load_config(self.config_path)
        project = config.projects.get(self.project_key)
        if project is None:
            self.title_bar.update(f"Project not found: {self.project_key}")
            return
        self.title_bar.update(f"{project.name or project.key} • Settings")
        pollypm_settings = getattr(config, "pollypm", None)
        current_channel = getattr(pollypm_settings, "release_channel", "stable")
        self._sync_release_channel_radio(current_channel)
        try:
            registry = load_registry()
            role_rows = _build_project_role_rows(
                config,
                self.project_key,
                registry,
            )
        except Exception as exc:  # noqa: BLE001
            role_rows = []
            try:
                self.query_one("#project-role-detail", Static).update(
                    f"[#f0c45a]Role routing unavailable: {exc}[/]"
                )
            except Exception:  # noqa: BLE001
                pass
        self._visible_role_rows = role_rows
        self._render_role_rows(role_rows)
        self._render_role_detail(role_rows)
        self._sync_role_editor()

        worker = None
        for session in config.sessions.values():
            if session.role == "worker" and session.project == self.project_key and session.enabled:
                worker = session
                break

        worker_info = self.query_one("#worker-info", Static)
        model_info = self.query_one("#model-info", Static)
        task_info = self.query_one("#task-info", Static)
        guide_info = self.query_one("#guide-info", Static)
        guide_info.update(self._render_project_guides(getattr(project, "path", None)))

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

    def _selected_role_row(self, rows: list[dict] | None = None) -> dict | None:
        role_rows = self._visible_role_rows if rows is None else rows
        if not role_rows:
            return None
        key = self._selected_role_key or self._current_role_key() or role_rows[0]["role"]
        return next((row for row in role_rows if row["role"] == key), role_rows[0])

    def _render_role_rows(self, rows: list[dict]) -> None:
        self.role_table.clear()
        for row in rows:
            self.role_table.add_row(
                Text(row["label"]),
                Text(row["configured_summary"]),
                Text(row["resolved_summary"], style="dim"),
                Text(
                    row["source_label"],
                    style=_role_source_style(row["source"]),
                ),
                Text("!" if row["advisories"] else "", style="#f0c45a"),
                key=row["role"],
            )
        if self.role_table.row_count and self._selected_role_key:
            keys = [row["role"] for row in rows]
            if self._selected_role_key in keys:
                try:
                    self.role_table.move_cursor(
                        row=keys.index(self._selected_role_key),
                    )
                except Exception:  # noqa: BLE001
                    pass
        elif self.role_table.row_count and self.role_table.cursor_row < 0:
            self.role_table.move_cursor(row=0)

    def _render_role_detail(self, rows: list[dict]) -> None:
        selected = self._selected_role_row(rows)
        try:
            detail = self.query_one("#project-role-detail", Static)
        except Exception:  # noqa: BLE001
            return
        if selected is None:
            detail.update("[dim]No project roles available.[/dim]")
            return
        lines = [
            f"[b]{selected['label']}[/b]  "
            f"[{_role_source_style(selected['source'])}]{selected['source_label']}[/]",
            f"[dim]Configured:[/dim] {selected['configured_summary']}",
            f"[dim]Resolved:[/dim]   {selected['resolved_summary']}",
            f"[dim]Alias path:[/dim] {selected['resolved_alias'] or '-'}",
            "[dim]Edit:[/dim]       Pick an alias, type both custom fields, or revert to inherit the global assignment.",
        ]
        if selected["configured_missing_alias"]:
            lines.append(
                "[#f0c45a]Configured alias is missing from the registry; PollyPM is inheriting the next valid scope.[/]"
            )
        if selected["advisories"]:
            lines.append("[#f0c45a]Advisories:[/]")
            lines.extend(f"  {message}" for message in selected["advisories"])
        else:
            lines.append("[dim]Advisories:[/dim] none.")
        detail.update("\n".join(lines))

    def _current_role_key(self) -> str | None:
        if self.role_table.row_count == 0 or self.role_table.cursor_row < 0:
            return None
        try:
            row_key = self.role_table.coordinate_to_cell_key(
                (self.role_table.cursor_row, 0),
            ).row_key
        except Exception:  # noqa: BLE001
            return None
        return str(row_key.value) if row_key is not None else None

    def _sync_role_editor(self) -> None:
        selected = self._selected_role_row()
        self._syncing_role_editor = True
        try:
            current_alias = selected["configured_alias"] if selected is not None else None
            missing_alias = bool(selected["configured_missing_alias"]) if selected is not None else False
            if isinstance(current_alias, str) and current_alias and not missing_alias:
                self._suppressed_role_alias_values.append(current_alias)
                self.role_alias_select.value = current_alias
            else:
                self.role_alias_select.value = Select.NULL
            self.role_provider_input.placeholder = (
                str(selected["resolved_provider"]) if selected is not None else "provider"
            )
            self.role_model_input.placeholder = (
                str(selected["resolved_model"]) if selected is not None else "model"
            )
            self.role_provider_input.value = (
                str(selected["configured_provider"] or "") if selected is not None else ""
            )
            self.role_model_input.value = (
                str(selected["configured_model"] or "") if selected is not None else ""
            )
            try:
                self.query_one("#project-role-inherit", Button).disabled = not bool(
                    selected and selected["has_override"]
                )
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._syncing_role_editor = False

    def _save_project_role_assignment(
        self,
        role: str,
        assignment: ModelAssignment,
    ) -> None:
        try:
            config = load_config(self.config_path)
            project = config.projects.get(self.project_key)
            if project is None:
                self._notify(f"Project not found: {self.project_key}")
                return
            project.role_assignments[role] = assignment
            write_config(config, self.config_path, force=True)
        except Exception as exc:  # noqa: BLE001
            self._notify(f"Role update failed: {exc}")
            return
        self._selected_role_key = role
        self._notify(f"Saved {_role_label(role)} override.")
        self._refresh()

    def _clear_project_role_override(self, role: str) -> None:
        try:
            config = load_config(self.config_path)
            project = config.projects.get(self.project_key)
            if project is None:
                self._notify(f"Project not found: {self.project_key}")
                return
            project.role_assignments.pop(role, None)
            write_config(config, self.config_path, force=True)
        except Exception as exc:  # noqa: BLE001
            self._notify(f"Role update failed: {exc}")
            return
        self._selected_role_key = role
        self._notify(f"{_role_label(role)} now inherits the global assignment.")
        self._refresh()

    def _persist_project_custom_pair_if_ready(self) -> None:
        if self._syncing_role_editor:
            return
        role = self._selected_role_key or self._current_role_key()
        if not role:
            return
        provider = self.role_provider_input.value.strip()
        model = self.role_model_input.value.strip()
        if not provider or not model:
            return
        self._save_project_role_assignment(
            role,
            ModelAssignment(provider=provider, model=model),
        )

    def _notify(self, msg: str) -> None:
        self.message_bar.update(msg)

    def _provider_budget_label(self, provider: str, *, account_label: str) -> str:
        label = provider.lower()
        return f"[b]{label}[/b] · budget tracked against {account_label}"

    def _current_worker(self):
        config = load_config(self.config_path)
        for session in config.sessions.values():
            if session.role == "worker" and session.project == self.project_key and session.enabled:
                return session
        return None

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
            with create_work_service(db_path=db_path, project_path=project_path) as svc:
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

    def _render_project_guides(self, project_path: Path | None) -> str:
        if project_path is None:
            return "[dim]No project path available.[/dim]"
        lines: list[str] = []
        stale_count = 0
        for role in _PROJECT_GUIDE_ROLES:
            guide_path = _project_guide_path(project_path, role)
            if not guide_path.is_file():
                continue
            info = _project_guide_drift_info(project_path, role)
            drifted = bool(_drift_value(info, "drifted", False))
            if drifted:
                stale_count += 1
            badge = " [#d7b75f]↑ upstream changed[/]" if drifted else ""
            lines.append(
                f"• [b]{role}[/b] [dim]{guide_path.name}[/dim]{badge}"
            )
        if not lines:
            return "[dim]No project-local guides forked yet.[/dim]"
        if stale_count:
            forks_word = "fork" if stale_count == 1 else "forks"
            verb = "needs" if stale_count == 1 else "need"
            lines.insert(
                0,
                f"[dim]{stale_count} guide {forks_word} {verb} refresh.[/dim]",
            )
        return "\n".join(lines)

    def _build_preview(self, worker, *, account_label: str) -> str:
        current_budget = self._budget_summary_for_account(worker.account)
        lines = [
            "[b]Diff preview[/b]",
            f"Current: [b]{worker.provider.value}[/b] on {account_label}",
            f"Current budget: [b]{current_budget}[/b]",
        ]
        claude = self._target_account_for(ProviderKind.CLAUDE)
        codex = self._target_account_for(ProviderKind.CODEX)
        lines.append(
            f"Claude target: {claude or '[dim]none available[/dim]'}"
            f" · budget: [b]{self._budget_summary_for_account(claude)}[/b]"
        )
        lines.append(
            f"Codex target: {codex or '[dim]none available[/dim]'}"
            f" · budget: [b]{self._budget_summary_for_account(codex)}[/b]"
        )
        rationale = history_rationale_for_account(
            worker.account,
            default_account=worker.account,
        )
        lines.append(f"Rationale: {rationale}")
        lines.append("[dim]Press Enter or click a switch button to apply. U undoes the last reversible switch for 24h.[/dim]")
        return "\n".join(lines)

    def _target_account_for(self, target_provider: ProviderKind) -> str | None:
        config = load_config(self.config_path)
        for name, account in config.accounts.items():
            if account.provider is target_provider:
                return name
        return None

    def _budget_summary_for_account(self, account_key: str | None) -> str:
        if not account_key:
            return "budget unavailable"
        try:
            cached = load_cached_account_usage(self.config_path)
        except Exception:  # noqa: BLE001
            cached = {}
        record = cached.get(account_key)
        if record is None:
            return "budget unavailable"
        used_pct = getattr(record, "used_pct", None)
        remaining_pct = getattr(record, "remaining_pct", None)
        usage_summary = getattr(record, "usage_summary", "") or "usage unavailable"
        if used_pct is not None and remaining_pct is not None:
            summary = f"{used_pct}% used / {remaining_pct}% left"
        elif remaining_pct is not None:
            summary = f"{remaining_pct}% left"
        else:
            summary = usage_summary
        updated_at = getattr(record, "updated_at", "") or ""
        if updated_at:
            summary = f"{summary} · updated {updated_at}"
        return summary

    def _record_undo(
        self,
        label: str,
        apply: Callable[[], None],
        *,
        kind: str = "",
        payload: dict[str, object] | None = None,
    ) -> None:
        entry = None
        if kind:
            entry = record_settings_history(kind, label, payload)
        self._undo_action = make_undo_action(
            label,
            apply,
            entry_id=entry.entry_id if entry is not None else "",
            kind=kind,
            payload=payload,
        )

    def _clear_undo(self) -> None:
        self._undo_action = None

    def _history_undo_action(self) -> UndoAction | None:
        entry = latest_settings_history_entry()
        if entry is None or entry.kind != "session.switch":
            return None
        session_name = str(entry.payload.get("session_name") or "")
        previous_account = str(entry.payload.get("from_account") or "")
        if not session_name or not previous_account:
            return None

        def _apply() -> None:
            PollyPMService(self.config_path).switch_session_account(session_name, previous_account)

        return make_undo_action(
            entry.label,
            _apply,
            entry_id=entry.entry_id,
            kind=entry.kind,
            payload=entry.payload,
        )

    def _consume_undo_history(self, action: UndoAction) -> None:
        if action.entry_id:
            consume_settings_history(action.entry_id)

    def _sync_release_channel_radio(self, channel: str) -> None:
        """Mirror the on-disk channel into the radio picker.

        The write-back handler short-circuits when the on-disk value
        already matches the radio, so firing ``RadioSet.Changed`` during
        sync is benign — no suppression plumbing needed.
        """
        try:
            stable = self.query_one("#release-channel-stable", RadioButton)
            beta = self.query_one("#release-channel-beta", RadioButton)
        except Exception:  # noqa: BLE001
            return
        target = beta if channel == "beta" else stable
        if target.value:
            return
        target.value = True

    @on(RadioSet.Changed, "#release-channel-radio")
    def on_release_channel_changed(self, event: RadioSet.Changed) -> None:
        button = event.pressed
        if button is None:
            return
        new_channel = "beta" if button.id == "release-channel-beta" else "stable"
        try:
            config = load_config(self.config_path)
        except Exception as exc:  # noqa: BLE001
            self._notify(f"Release channel update failed: {exc}")
            return
        current = getattr(getattr(config, "pollypm", None), "release_channel", "stable")
        if current == new_channel:
            return
        config.pollypm.release_channel = new_channel
        try:
            write_config(config, self.config_path, force=True)
        except Exception as exc:  # noqa: BLE001
            self._notify(f"Release channel update failed: {exc}")
            return
        # Invalidate the cached release check so the next probe re-queries
        # against the new channel. The cache module from #714 doesn't
        # exist yet — unlink defensively.
        cache_path = Path.home() / ".pollypm" / "release-check.json"
        try:
            cache_path.unlink(missing_ok=True)
        except OSError:
            pass
        self._notify(f"Release channel set to {new_channel}.")

    @on(DataTable.RowHighlighted, "#project-role-table")
    def on_role_highlighted(self, _event: DataTable.RowHighlighted) -> None:
        self._selected_role_key = self._current_role_key()
        self._render_role_detail(self._visible_role_rows)
        self._sync_role_editor()

    @on(DataTable.RowSelected, "#project-role-table")
    def on_role_selected(self, _event: DataTable.RowSelected) -> None:
        self._selected_role_key = self._current_role_key()
        self._render_role_detail(self._visible_role_rows)
        self._sync_role_editor()

    @on(Select.Changed, "#project-role-alias")
    def on_role_alias_changed(self, event: Select.Changed) -> None:
        if self._syncing_role_editor:
            return
        role = self._selected_role_key or self._current_role_key()
        if not role:
            return
        value = event.value
        if not isinstance(value, str) or not value:
            return
        if value in self._suppressed_role_alias_values:
            self._suppressed_role_alias_values.remove(value)
            return
        self._save_project_role_assignment(role, ModelAssignment(alias=value))

    @on(Input.Changed, "#project-role-provider")
    def on_role_provider_changed(self, _event: Input.Changed) -> None:
        self._persist_project_custom_pair_if_ready()

    @on(Input.Changed, "#project-role-model")
    def on_role_model_changed(self, _event: Input.Changed) -> None:
        self._persist_project_custom_pair_if_ready()

    @on(Button.Pressed, "#project-role-inherit")
    def on_project_role_inherit(self, _event: Button.Pressed) -> None:
        role = self._selected_role_key or self._current_role_key()
        if not role:
            return
        self._clear_project_role_override(role)

    @on(Button.Pressed, "#reset-session")
    def on_reset(self, _event: Button.Pressed | None) -> None:
        worker = self._current_worker()
        if worker is None:
            self._notify("No worker session to reset.")
            return
        self.push_screen(
            _SettingsConfirmModal(
                title="Reset worker session?",
                prompt=(
                    f"Stop {worker.name} now? This will leave the project without an active worker until relaunched."
                ),
                confirm_label="Reset",
            ),
            callback=lambda confirmed: self._confirm_reset(worker.name, confirmed),
        )

    @on(Button.Pressed, "#switch-claude")
    def on_switch_claude(self, _event: Button.Pressed | None) -> None:
        self._switch_provider(ProviderKind.CLAUDE)

    @on(Button.Pressed, "#switch-codex")
    def on_switch_codex(self, _event: Button.Pressed | None) -> None:
        self._switch_provider(ProviderKind.CODEX)

    @on(Button.Pressed, "#undo")
    def on_undo(self, _event: Button.Pressed) -> None:
        self.action_undo_recent_change()

    def _confirm_reset(self, worker_name: str, confirmed: bool) -> None:
        if not confirmed:
            return
        try:
            PollyPMService(self.config_path).stop_session(worker_name)
            self._notify(f"Session {worker_name} stopped. Press N to relaunch.")
        except Exception as exc:  # noqa: BLE001
            self._notify(f"Reset failed: {exc}")
            return
        self._clear_undo()
        self._refresh()

    def _switch_provider(self, target_provider: ProviderKind) -> None:
        worker = self._current_worker()
        if worker is None:
            self._notify("No worker session to switch.")
            return
        target_account = None
        config = load_config(self.config_path)
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
        self.push_screen(
            _SettingsConfirmModal(
                title=f"Switch {worker.name} to {target_provider.value}?",
                prompt=(
                    f"Move {worker.name} from {worker.account} to {target_account}. The session will restart."
                ),
                confirm_label="Switch",
            ),
            callback=lambda confirmed: self._confirm_switch_provider(
                worker.name,
                worker.account,
                target_account,
                target_provider,
                confirmed,
            ),
        )

    def _confirm_switch_provider(
        self,
        worker_name: str,
        previous_account: str,
        target_account: str,
        target_provider: ProviderKind,
        confirmed: bool,
    ) -> None:
        if not confirmed:
            return
        try:
            PollyPMService(self.config_path).switch_session_account(worker_name, target_account)
            self._record_undo(
                f"switch {worker_name} to {target_account}",
                lambda: PollyPMService(self.config_path).switch_session_account(
                    worker_name,
                    previous_account,
                ),
                kind="session.switch",
                payload={
                    "session_name": worker_name,
                    "from_account": previous_account,
                    "to_account": target_account,
                    "provider": target_provider.value,
                },
            )
            self._notify(
                f"Switched to {target_provider.value} ({target_account}). Session restarted."
            )
        except Exception as exc:  # noqa: BLE001
            self._notify(f"Switch failed: {exc}")
            return
        self._refresh()

    def action_refresh(self) -> None:
        self._refresh()

    def action_undo_recent_change(self) -> None:
        action = self._undo_action
        if undo_expired(action):
            self._clear_undo()
            action = None
        if action is None:
            action = self._history_undo_action()
        if action is None:
            self._notify("Nothing recent to undo.")
            return
        try:
            action.apply()
            self._consume_undo_history(action)
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


class _SettingsConfirmModal(ModalScreen[bool]):
    CSS = """
    Screen {
        align: center middle;
    }
    #settings-confirm {
        width: 72;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: heavy $warning;
    }
    #settings-confirm-title {
        padding-bottom: 1;
        text-style: bold;
    }
    #settings-confirm-buttons {
        height: auto;
        align-horizontal: right;
        padding-top: 1;
    }
    #settings-confirm-buttons Button {
        margin-left: 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self,
        *,
        title: str,
        prompt: str,
        confirm_label: str = "Confirm",
        cancel_label: str = "Cancel",
    ) -> None:
        super().__init__()
        self._title = title
        self._prompt = prompt
        self._confirm_label = confirm_label
        self._cancel_label = cancel_label

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-confirm"):
            yield Static(self._title, id="settings-confirm-title")
            yield Static(self._prompt)
            with Horizontal(id="settings-confirm-buttons"):
                yield Button(self._cancel_label, id="cancel")
                yield Button(self._confirm_label, variant="primary", id="confirm")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)
