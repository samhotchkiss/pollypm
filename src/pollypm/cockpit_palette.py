"""Cockpit command-palette and keyboard-help support.

Contract:
- Inputs: a host Textual ``App`` plus palette command metadata from
  ``pollypm.cockpit``.
- Outputs: reusable command-palette and keyboard-help modals, plus
  dispatch helpers shared by cockpit screens.
- Side effects: pushes modal screens and routes cockpit navigation via
  ``CockpitRouter``.
- Invariants: palette/help behavior is shared infrastructure, not owned
  by any single cockpit screen.
"""

from __future__ import annotations

from collections import deque
from textwrap import shorten

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, ListItem, ListView, Static

from pollypm.cockpit import (
    PaletteCommand,
    build_palette_commands,
    filter_palette_commands,
)


class _PaletteListItem(ListItem):
    """A single row inside the ``:`` command palette."""

    def __init__(self, command: PaletteCommand, *, compact: bool = False) -> None:
        self.command = command
        self.body = Static(self._render_body(command, compact=compact), markup=True)
        classes = "palette-row compact" if compact else "palette-row"
        super().__init__(self.body, classes=classes)

    @staticmethod
    def _render_body(command: PaletteCommand, *, compact: bool = False) -> str:
        if compact:
            suffix = f"  [dim]\\[{command.keybind}][/dim]" if command.keybind else ""
            title_width = max(10, 24 - (len(command.keybind or "") + 4 if command.keybind else 0))
            title = shorten(command.title, width=title_width, placeholder="…")
            return f"[b]{title}[/b]{suffix}"
        title = f"[b]{command.title}[/b]"
        if command.keybind:
            title = f"{title}  [dim]\\[{command.keybind}][/dim]"
        subtitle = command.subtitle or ""
        line2 = f"[#6b7a88]{command.category}[/#6b7a88]"
        if subtitle:
            line2 = f"{line2}  [dim]\u00b7[/dim]  [dim]{subtitle}[/dim]"
        return f"{title}\n  {line2}"


class _PaletteSectionHeader(ListItem):
    """Non-selectable header row that labels a group in the palette."""

    def __init__(self, label: str) -> None:
        body = Static(
            f"[#6b7a88]\u2500\u2500 {label} \u2500\u2500[/#6b7a88]",
            markup=True,
        )
        super().__init__(body, classes="palette-section-header")
        self.disabled = True


class CommandPaletteModal(ModalScreen[str | None]):
    """Global ``:`` command palette — fuzzy-searchable command list."""

    CSS = """
    CommandPaletteModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.45);
    }
    #palette-dialog {
        width: 72;
        max-width: 90%;
        height: auto;
        max-height: 22;
        padding: 1 1 0 1;
        background: #141a20;
        border: round #2a3340;
    }
    #palette-input {
        height: 3;
        padding: 0 1;
        background: #0f1317;
        border: round #2a3340;
        color: #eef2f4;
    }
    #palette-input:focus {
        border: round #5b8aff;
    }
    #palette-list {
        height: auto;
        max-height: 15;
        background: #141a20;
        border: none;
        margin-top: 1;
        padding: 0;
        scrollbar-size: 1 1;
        scrollbar-color: #2a3340;
    }
    #palette-list > .palette-row {
        height: 3;
        padding: 0 1;
        color: #d6dee5;
        background: transparent;
    }
    #palette-list > .palette-row.compact {
        height: 1;
    }
    #palette-list > .palette-row.-highlight {
        background: #1e2730;
    }
    #palette-list:focus-within > .palette-row.-highlight {
        background: #253140;
        color: #f2f6f8;
    }
    #palette-list > .palette-section-header {
        height: 1;
        padding: 0 1;
        color: #6b7a88;
        background: transparent;
    }
    #palette-empty {
        height: 3;
        padding: 1;
        color: #6b7a88;
    }
    #palette-hint {
        height: 1;
        padding: 0 1;
        color: #3e4c5a;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Close"),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("enter", "run_selected", "Run", show=False),
    ]

    def __init__(
        self,
        commands: list[PaletteCommand],
        *,
        recent_tags: list[str] | None = None,
    ) -> None:
        super().__init__()
        self._all_commands: list[PaletteCommand] = list(commands)
        self._recent_commands: list[PaletteCommand] = _resolve_recent_commands(
            commands,
            recent_tags or [],
            limit=5,
        )
        self._visible: list[PaletteCommand] = list(commands)
        self._row_kinds: list[str] = []
        self.input = Input(placeholder="Type a command\u2026", id="palette-input")
        self.list_view = ListView(id="palette-list")
        self.empty = Static(
            "[dim]No commands match[/dim]",
            id="palette-empty",
            markup=True,
        )
        self.empty.display = False
        self.hint = Static(
            "[dim]\u21b5 run  \u00b7  \u2191\u2193 move  \u00b7  esc close[/dim]",
            id="palette-hint",
            markup=True,
        )

    def _compact_rows(self) -> bool:
        try:
            return self.app.size.width < 50
        except Exception:  # noqa: BLE001
            return False

    def compose(self) -> ComposeResult:
        with Vertical(id="palette-dialog"):
            yield self.input
            yield self.empty
            yield self.list_view
            yield self.hint

    def on_mount(self) -> None:
        self._filter("")
        self.input.focus()

    def _populate(
        self,
        commands: list[PaletteCommand],
        *,
        recent: list[PaletteCommand] | None = None,
    ) -> None:
        recent = recent or []
        self._visible = list(recent) + list(commands)
        self._row_kinds = []
        self.list_view.clear()
        if not self._visible:
            self.empty.display = True
            self.list_view.display = False
            return
        self.empty.display = False
        self.list_view.display = True

        first_item_index: int | None = None
        compact = self._compact_rows()
        if recent:
            self.list_view.append(_PaletteSectionHeader("Recent"))
            self._row_kinds.append("header")
            for cmd in recent:
                if first_item_index is None:
                    first_item_index = len(self._row_kinds)
                self.list_view.append(_PaletteListItem(cmd, compact=compact))
                self._row_kinds.append("item")
            self.list_view.append(_PaletteSectionHeader("All commands"))
            self._row_kinds.append("header")
        for cmd in commands:
            if first_item_index is None:
                first_item_index = len(self._row_kinds)
            self.list_view.append(_PaletteListItem(cmd, compact=compact))
            self._row_kinds.append("item")

        self.list_view.index = (
            first_item_index if first_item_index is not None else 0
        )

    def _filter(self, query: str) -> None:
        stripped = (query or "").strip()
        if not stripped:
            self._populate(self._all_commands, recent=self._recent_commands)
            return
        matches = filter_palette_commands(self._all_commands, query)
        self._populate(matches)

    @on(Input.Changed, "#palette-input")
    def _on_query_changed(self, event: Input.Changed) -> None:
        self._filter(event.value)

    @on(Input.Submitted, "#palette-input")
    def _on_query_submitted(self, _event: Input.Submitted) -> None:
        self.action_run_selected()

    @on(ListView.Selected, "#palette-list")
    def _on_row_selected(self, event: ListView.Selected) -> None:
        row = event.item
        if isinstance(row, _PaletteListItem):
            self.dismiss(row.command.tag)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_cursor_down(self) -> None:
        self.list_view.action_cursor_down()

    def action_cursor_up(self) -> None:
        self.list_view.action_cursor_up()

    def action_run_selected(self) -> None:
        if not self._visible:
            return
        raw_idx = self.list_view.index or 0
        item_idx = self._resolve_item_index(raw_idx)
        if item_idx is None:
            return
        self.dismiss(self._visible[item_idx].tag)

    def _resolve_item_index(self, list_view_index: int) -> int | None:
        if not self._row_kinds:
            return 0 if self._visible else None
        if list_view_index < 0:
            list_view_index = 0
        items_before = sum(
            1 for kind in self._row_kinds[:list_view_index] if kind == "item"
        )
        for kind in self._row_kinds[list_view_index:]:
            if kind == "item":
                return items_before
        return None


def _current_project_for_palette(app: App) -> str | None:
    project_key = getattr(app, "project_key", None)
    if isinstance(project_key, str) and project_key:
        return project_key
    selected = getattr(app, "selected_key", None)
    if isinstance(selected, str) and selected.startswith("project:"):
        parts = selected.split(":", 2)
        if len(parts) >= 2 and parts[1]:
            return parts[1]
    return None


def _dispatch_palette_tag(app: App, tag: str | None) -> None:
    """Interpret a palette ``tag`` inside the host app."""
    if not tag:
        return
    try:
        if tag == "nav.inbox":
            _palette_nav(app, "inbox")
            return
        if tag == "nav.workers":
            _palette_nav(app, "workers")
            return
        if tag == "nav.activity":
            _palette_nav(app, "activity")
            return
        if tag == "nav.metrics":
            _palette_nav(app, "metrics")
            return
        if tag == "nav.settings":
            _palette_nav(app, "settings")
            return
        if tag == "nav.dashboard":
            _palette_nav(app, "dashboard")
            return
        if tag.startswith("nav.project:"):
            _palette_nav(app, tag.split(":", 1)[1], is_project=True)
            return

        if tag == "session.refresh":
            refresh = getattr(app, "action_refresh", None)
            if callable(refresh):
                refresh()
            else:
                _palette_notify(app, "This screen has no refresh action.")
            return
        if tag == "session.restart":
            _palette_notify(app, "Restarting cockpit\u2026")
            app.exit()
            return
        if tag == "session.shortcuts":
            _palette_show_shortcuts(app)
            return

        if tag == "inbox.notify":
            _palette_notify(
                app,
                "Inbox notification composer is not wired yet. Use Polly chat to send the update.",
            )
            return
        if tag == "inbox.archive_read":
            _palette_notify(
                app,
                "Bulk-archive not wired yet \u2014 press 'a' on an open inbox item.",
            )
            return
        if tag == "system.doctor":
            _palette_notify(app, "Diagnostics are not wired into the palette yet.")
            return
        if tag == "system.edit_config":
            _palette_notify(
                app,
                "Open pollypm.toml in your editor \u2014 palette shortcut landing in a follow-up.",
            )
            return
        if tag.startswith("task.create:"):
            project_key = tag.split(":", 1)[1]
            _palette_notify(
                app,
                f"Create task in {project_key}: open Tasks and use the create action.",
            )
            return
        if tag.startswith("task.queue_next:"):
            project_key = tag.split(":", 1)[1]
            _palette_notify(
                app,
                f"Queue next in {project_key}: ask Polly to pick up the next ready task.",
            )
            return
        # Project lifecycle commands (#875). Each surfaces a clear
        # next-step instruction so the user is not stuck wondering how
        # to add or activate a project from inside the cockpit.
        if tag == "project.add":
            _palette_notify(
                app,
                "Project add is not wired here yet. Open the project picker and choose Add Project.",
            )
            return
        if tag == "project.scan":
            _palette_notify(
                app,
                "Project scan is not wired here yet. Open the project picker and choose Scan.",
            )
            return
        if tag == "project.send_operator":
            _palette_notify(
                app,
                "Open Polly chat and describe the job you want handled.",
            )
            return
    except Exception as exc:  # noqa: BLE001
        _palette_notify(app, f"Command failed: {exc}")


def _resolve_palette_dispatch():
    """Preserve the legacy ``pollypm.cockpit_ui`` monkeypatch seam."""
    try:
        from pollypm import cockpit_ui
    except Exception:  # noqa: BLE001
        return _dispatch_palette_tag
    dispatch = getattr(cockpit_ui, "_dispatch_palette_tag", None)
    if callable(dispatch):
        return dispatch
    return _dispatch_palette_tag


def _palette_nav(app: App, target: str, *, is_project: bool = False) -> None:
    """Route to a top-level cockpit view from any host app."""
    router = getattr(app, "router", None)
    if router is not None and hasattr(router, "route_selected"):
        try:
            if is_project:
                router.route_selected(f"project:{target}")
            else:
                router.route_selected(target)
            app.selected_key = router.selected_key()  # type: ignore[attr-defined]
            refresh = getattr(app, "_refresh_rows", None)
            if callable(refresh):
                refresh()
            return
        except Exception as exc:  # noqa: BLE001
            _palette_notify(app, f"Route failed: {exc}")
            return
    if is_project:
        _palette_notify(app, f"Jump to project: {target} \u2014 exiting this pane.")
    else:
        _palette_notify(app, f"Jump to {target} \u2014 exiting this pane.")
    app.exit()


def _palette_notify(app: App, message: str) -> None:
    """Thin wrapper around :meth:`App.notify` that never raises."""
    notify = getattr(app, "notify", None)
    if callable(notify):
        try:
            notify(message, timeout=3.0)
            return
        except Exception:  # noqa: BLE001
            pass
    setattr(app, "_palette_last_message", message)


def _palette_show_shortcuts(app: App) -> None:
    """Render the host app's registered keybindings."""
    lines: list[str] = []
    bindings = getattr(app, "BINDINGS", None) or []
    for binding in bindings:
        key = getattr(binding, "key", None) or str(binding)
        desc = getattr(binding, "description", "") or ""
        if not key:
            continue
        lines.append(f"{key}  \u2014  {desc}" if desc else key)
    body = "\n".join(lines) if lines else "No keybindings registered."
    setattr(app, "_palette_last_shortcuts", body)

    show_help = getattr(app, "action_show_keyboard_help", None)
    if callable(show_help):
        try:
            show_help()
            return
        except Exception:  # noqa: BLE001
            pass
    _palette_notify(app, body)


_PALETTE_HISTORY_MAX = 10
_PALETTE_RECENT_DISPLAY = 5


def _palette_history(app: App) -> deque[str]:
    """Return the per-app command history deque, initializing lazily."""
    history = getattr(app, "_palette_recent_history", None)
    if not isinstance(history, deque):
        history = deque(maxlen=_PALETTE_HISTORY_MAX)
        setattr(app, "_palette_recent_history", history)
    return history


def _record_palette_command(app: App, tag: str) -> None:
    """Record a dispatched command tag on the host app's history."""
    if not tag:
        return
    history = _palette_history(app)
    try:
        history.remove(tag)
    except ValueError:
        pass
    history.append(tag)


def _resolve_recent_commands(
    all_commands: list[PaletteCommand],
    recent_tags: list[str],
    *,
    limit: int,
) -> list[PaletteCommand]:
    """Resolve a most-recent-first list of tags back to commands."""
    by_tag = {cmd.tag: cmd for cmd in all_commands}
    resolved: list[PaletteCommand] = []
    for tag in recent_tags:
        cmd = by_tag.get(tag)
        if cmd is None:
            continue
        resolved.append(cmd)
        if len(resolved) >= limit:
            break
    return resolved


def _open_command_palette(app: App) -> None:
    """Push :class:`CommandPaletteModal` onto ``app`` with the full command set."""
    config_path = getattr(app, "config_path", None)
    if config_path is None:
        return

    def _on_dismiss(tag: str | None) -> None:
        if tag:
            _record_palette_command(app, tag)
        _resolve_palette_dispatch()(app, tag)

    try:
        commands = build_palette_commands(
            config_path,
            current_project=_current_project_for_palette(app),
        )
    except Exception:  # noqa: BLE001
        commands = []
    recent_tags = list(reversed(_palette_history(app)))
    app.push_screen(
        CommandPaletteModal(commands, recent_tags=recent_tags),
        _on_dismiss,
    )


_GLOBAL_HELP_BINDINGS: list[tuple[str, str]] = [
    ("ctrl+k", "command palette"),
    (":", "command palette"),
    ("?", "this help"),
    ("ctrl+q", "quit"),
    ("ctrl+w", "detach"),
    # PM Chat / live agent panes are raw Claude Code subprocesses that
    # consume Tab themselves (Shift-Tab cycles permission modes inside
    # Claude). The cockpit cannot intercept them, so document the tmux
    # escape so users do not feel trapped (#877).
    ("ctrl+b o", "rotate tmux pane focus (use to leave a chat pane)"),
    ("ctrl+b ←", "focus the rail from a chat pane"),
]

_INBOX_LABEL_HELP: dict[str, list[tuple[str, str]]] = {
    "plan_review": [
        ("v", "open visual explainer"),
        ("A", "approve plan"),
    ],
    "proposal": [
        ("A", "accept proposal"),
        ("X", "reject proposal"),
    ],
    "blocking_question": [
        ("r", "reply to worker"),
        ("d", "jump to worker pane"),
    ],
}

_RAIL_GLYPH_HELP: list[tuple[str, str]] = [
    ("▌", "current rail selection"),
    ("♥ / ♡", "tmux attached / detached session pulse"),
    ("·", "idle session"),
    ("… / ◜◝◞◟", "writing or working"),
    ("✎", "reviewing"),
    ("⚠", "stuck or alerting"),
    ("○", "idle or offline project"),
    # Project sparkline glyphs (#872). The trailing ``·▁▂▃▄▅▆▇█``
    # bar-chart on each project row plots event count per bucket over
    # roughly the last hour — taller bar = busier bucket. Without this
    # legend the bars read as decorative noise.
    ("·▁▂…█", "project activity sparkline (busier = taller bar)"),
]


def _format_binding_keys(key_field: str) -> str:
    """Render a comma-separated Binding.key into a friendly label."""
    pretty: list[str] = []
    for raw in (key_field or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        replacements = {
            "down": "\u2193",
            "up": "\u2191",
            "left": "\u2190",
            "right": "\u2192",
            "escape": "Esc",
            "enter": "Enter",
            "home": "Home",
            "end": "End",
            "tab": "Tab",
            "shift+tab": "Shift+Tab",
            "ctrl+k": "Ctrl+K",
            "colon": ":",
            "slash": "/",
            "question_mark": "?",
            "space": "Space",
        }
        pretty.append(replacements.get(raw, raw))
    return " / ".join(pretty) if pretty else key_field


def _binding_help_priority(norm_keys: set[str]) -> int:
    """Keep high-frequency movement keys visible before lower-traffic rows."""
    if norm_keys & {"enter", "o"}:
        return 10
    if norm_keys & {"j", "down"}:
        return 20
    if norm_keys & {"k", "up"}:
        return 21
    if norm_keys & {"g", "home"}:
        return 22
    if norm_keys & {"G", "end"}:
        return 23
    if norm_keys & {"q", "escape", "ctrl+q"}:
        return 90
    return 50


def _selected_inbox_labels(app: App) -> list[str]:
    """Return labels on the currently-selected inbox item, if any."""
    selected_id = getattr(app, "_selected_task_id", None)
    if not selected_id:
        return []
    tasks = getattr(app, "_tasks", None) or []
    for task in tasks:
        if getattr(task, "task_id", None) == selected_id:
            return list(getattr(task, "labels", []) or [])
    return []


def _right_pane_help_section_for_cockpit(
    app: App,
) -> tuple[str, list[tuple[str, str]]] | None:
    """Return a help section for whatever the cockpit's right pane is showing.

    Resolves ``selected_key`` to the right-pane App class, walks its
    ``BINDINGS`` the same way ``_collect_keybindings_for_screen`` does
    for the host app, and labels the resulting section by surface name
    so the user can tell which keys belong to the right pane vs the
    rail (#860).
    """
    selected = getattr(app, "selected_key", "") or ""

    # Late imports keep cockpit_palette free of the cockpit_ui import
    # graph; only matters when the rail-help dialog opens.
    try:
        from pollypm.cockpit_ui import (
            PollyActivityFeedApp,
            PollyDashboardApp,
            PollyInboxApp,
            PollyProjectDashboardApp,
            PollyProjectSettingsApp,
            PollySettingsPaneApp,
            PollyWorkerRosterApp,
        )
    except Exception:  # noqa: BLE001
        return None

    target_cls = None
    surface_label = ""
    if selected == "inbox":
        target_cls, surface_label = PollyInboxApp, "Inbox"
    elif selected == "activity":
        target_cls, surface_label = PollyActivityFeedApp, "Activity feed"
    elif selected == "settings":
        target_cls, surface_label = PollySettingsPaneApp, "Settings"
    elif selected == "workers":
        target_cls, surface_label = PollyWorkerRosterApp, "Workers"
    elif selected in {"polly", "dashboard"}:
        target_cls, surface_label = PollyDashboardApp, "Home dashboard"
    elif selected.startswith("project:"):
        # ``project:<key>:settings`` vs ``project:<key>[:dashboard]``.
        if selected.endswith(":settings"):
            target_cls, surface_label = (
                PollyProjectSettingsApp, "Project settings",
            )
        else:
            target_cls, surface_label = (
                PollyProjectDashboardApp, "Project dashboard",
            )

    if target_cls is None:
        return None

    rows: list[tuple[int, int, str, str]] = []
    for order, binding in enumerate(getattr(target_cls, "BINDINGS", None) or []):
        key_field = getattr(binding, "key", "") or ""
        desc = getattr(binding, "description", "") or ""
        if not key_field:
            continue
        norm_keys = {key.strip() for key in key_field.split(",")}
        if norm_keys & {"question_mark", "colon", "ctrl+k"}:
            continue
        rows.append(
            (
                _binding_help_priority(norm_keys),
                order,
                _format_binding_keys(key_field),
                desc or "(no description)",
            )
        )
    if not rows:
        return None
    rows.sort(key=lambda row: (row[0], row[1]))
    return (
        f"Right pane: {surface_label}",
        [(key, desc) for _priority, _order, key, desc in rows],
    )


def _collect_keybindings_for_screen(
    app: App,
) -> list[tuple[str, list[tuple[str, str]]]]:
    """Return ordered ``(category, [(key, description), ...])`` sections."""
    sections: list[tuple[str, list[tuple[str, str]]]] = []

    screen_rows: list[tuple[int, int, str, str]] = []
    for order, binding in enumerate(getattr(app, "BINDINGS", None) or []):
        key_field = getattr(binding, "key", "") or ""
        desc = getattr(binding, "description", "") or ""
        if not key_field:
            continue
        norm_keys = {key.strip() for key in key_field.split(",")}
        if norm_keys & {"question_mark", "colon", "ctrl+k"}:
            continue
        screen_rows.append(
            (
                _binding_help_priority(norm_keys),
                order,
                _format_binding_keys(key_field),
                desc or "(no description)",
            )
        )
    if screen_rows:
        screen_rows.sort(key=lambda row: (row[0], row[1]))
        sections.append(
            ("This screen", [(key, desc) for _priority, _order, key, desc in screen_rows])
        )

    if type(app).__name__ == "PollyCockpitApp":
        sections.append(("Rail glyphs", list(_RAIL_GLYPH_HELP)))
        # Contextual right-pane bindings (#860). When the user presses
        # ``?`` from the rail with a sub-surface mounted in the right
        # pane (Inbox, Activity, Settings, Workers, project Dashboard /
        # Settings / Tasks), surface that surface's keybindings here so
        # the help is contextual instead of just rail-only.
        rail_section = _right_pane_help_section_for_cockpit(app)
        if rail_section is not None:
            sections.append(rail_section)

    labels = _selected_inbox_labels(app)
    for label_name, rows in _INBOX_LABEL_HELP.items():
        if label_name in labels:
            sections.append((f"Selected item: {label_name}", list(rows)))

    sections.append(("Global (anywhere in cockpit)", list(_GLOBAL_HELP_BINDINGS)))
    return sections


def _screen_title_for_help(app: App) -> str:
    """Best-effort human label for the host app in the modal title."""
    cls_name = type(app).__name__
    mapping = {
        "PollyCockpitApp": "Cockpit",
        "PollyInboxApp": "Inbox",
        "PollyProjectDashboardApp": "Project dashboard",
        "PollyWorkerRosterApp": "Workers",
        "PollyActivityFeedApp": "Activity feed",
        "PollySettingsPaneApp": "Settings",
    }
    return mapping.get(cls_name, cls_name)


class KeyboardHelpModal(ModalScreen[None]):
    """``?`` keyboard help overlay — categorised, scrollable."""

    CSS = """
    KeyboardHelpModal {
        align: center middle;
        background: #0f1317;
    }
    #kh-dialog {
        width: 64;
        max-width: 90%;
        height: auto;
        max-height: 28;
        padding: 1 1 0 1;
        background: #141a20;
        border: round #2a3340;
    }
    #kh-title {
        height: 1;
        padding: 0 1;
        color: #eef6ff;
    }
    #kh-scroll {
        height: auto;
        max-height: 24;
        background: #141a20;
        border: none;
        margin-top: 1;
        padding: 0 1;
        scrollbar-size: 1 1;
        scrollbar-color: #2a3340;
    }
    #kh-body {
        height: auto;
        color: #d6dee5;
    }
    #kh-hint {
        height: 1;
        padding: 0 1;
        color: #3e4c5a;
    }
    """

    # Priority bindings so the modal traps input — without ``priority``
    # the rail underneath has its own priority j/k/g/G/Esc bindings and
    # eats the keystrokes before this modal sees them, leaving the user
    # convinced the help dialog is broken (#861).
    BINDINGS = [
        Binding("escape,q,question_mark", "cancel", "Close", priority=True),
        Binding("j,down", "scroll_down", "Down", show=False, priority=True),
        Binding("k,up", "scroll_up", "Up", show=False, priority=True),
        Binding("g,home", "scroll_home", "Top", show=False, priority=True),
        Binding("G,end", "scroll_end", "Bottom", show=False, priority=True),
        Binding("pageup,b", "page_up", "Page up", show=False, priority=True),
        Binding(
            "pagedown,space,f", "page_down", "Page down",
            show=False, priority=True,
        ),
    ]

    def __init__(
        self,
        sections: list[tuple[str, list[tuple[str, str]]]],
        *,
        screen_title: str = "",
    ) -> None:
        super().__init__()
        self._sections = list(sections)
        self._screen_title = screen_title or "Cockpit"
        self.title_bar = Static(
            f"[b]Keyboard shortcuts[/b]  [dim]\u2014  {self._screen_title}[/dim]",
            id="kh-title",
            markup=True,
        )
        self.body = Static(self._render_body(), id="kh-body", markup=True)
        self.hint = Static(
            "[dim]Esc / q / ? to close[/dim]",
            id="kh-hint",
            markup=True,
        )

    def _render_body(self) -> str:
        # The modal can render inside narrow panes (e.g. the 30-col
        # rail), where a one-line ``KEY    DESC`` layout wraps and
        # the wrap continuation reverses the visual indent so keys
        # appear *more* indented than descriptions (#795). Use a
        # robust two-line shape per binding instead: key on its own
        # line at indent 2, description below it at indent 6. Both
        # lines are short enough to never wrap, and the deeper
        # description indent makes the pairing unambiguous.
        if not self._sections:
            return "[dim]No keybindings registered.[/dim]"
        lines: list[str] = []
        for idx, (category, rows) in enumerate(self._sections):
            if idx > 0:
                lines.append("")
            lines.append(f"[b #5b8aff]{category}[/b #5b8aff]")
            if not rows:
                lines.append("  [dim](none)[/dim]")
                continue
            for key, desc in rows:
                lines.append(f"  [b]{key}[/b]")
                lines.append(f"      [dim]{desc}[/dim]")
        return "\n".join(lines)

    def compose(self) -> ComposeResult:
        with Vertical(id="kh-dialog"):
            yield self.title_bar
            with VerticalScroll(id="kh-scroll"):
                yield self.body
            yield self.hint

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _scroll_widget(self) -> VerticalScroll | None:
        try:
            return self.query_one("#kh-scroll", VerticalScroll)
        except Exception:  # noqa: BLE001
            return None

    def action_scroll_down(self) -> None:
        widget = self._scroll_widget()
        if widget is not None:
            widget.scroll_down(animate=False)

    def action_scroll_up(self) -> None:
        widget = self._scroll_widget()
        if widget is not None:
            widget.scroll_up(animate=False)

    def action_scroll_home(self) -> None:
        widget = self._scroll_widget()
        if widget is not None:
            widget.scroll_home(animate=False)

    def action_scroll_end(self) -> None:
        widget = self._scroll_widget()
        if widget is not None:
            widget.scroll_end(animate=False)

    def action_page_up(self) -> None:
        widget = self._scroll_widget()
        if widget is not None:
            widget.scroll_page_up(animate=False)

    def action_page_down(self) -> None:
        widget = self._scroll_widget()
        if widget is not None:
            widget.scroll_page_down(animate=False)


def _open_keyboard_help(app: App) -> None:
    """Push :class:`KeyboardHelpModal` onto ``app``."""
    try:
        sections = _collect_keybindings_for_screen(app)
    except Exception:  # noqa: BLE001
        sections = []
    title = _screen_title_for_help(app)
    app.push_screen(KeyboardHelpModal(sections, screen_title=title))
