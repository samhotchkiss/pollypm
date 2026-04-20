"""Cockpit worker-roster panel.

Contract:
- Inputs: a cockpit config path plus worker-roster rows gathered from
  ``pollypm.cockpit``.
- Outputs: the ``PollyWorkerRosterApp`` Textual screen and route actions
  for project dashboards / worker windows.
- Side effects: loads config, gathers worker status, mounts live alert
  toasts, and routes the cockpit pane through ``CockpitRouter``.
- Invariants: worker-roster rendering stays isolated from unrelated
  cockpit panels and keeps navigation on public palette/alert helpers.
"""

from __future__ import annotations

from pathlib import Path

from rich.markup import escape as _escape
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import DataTable, Static

from pollypm.cockpit_rail import CockpitRouter
from pollypm.cockpit_alerts import _setup_alert_notifier
from pollypm.cockpit_palette import _open_command_palette, _open_keyboard_help
from pollypm.config import load_config


class PollyWorkerRosterApp(App[None]):
    """Interactive worker-roster panel — ``pm cockpit-pane workers``."""

    TITLE = "PollyPM"
    SUB_TITLE = "Workers"

    CSS = """
    Screen {
        background: #0f1317;
        color: #eef2f4;
        padding: 0;
    }
    #wr-outer {
        height: 1fr;
        padding: 1 2;
    }
    #wr-topbar {
        height: 3;
        padding: 0 0 1 0;
        border-bottom: solid #1e2730;
    }
    #wr-counters {
        height: 1;
        padding: 0 0 0 0;
        color: #97a6b2;
    }
    #wr-table-wrap {
        height: 1fr;
        padding: 1 0 0 0;
        background: #0f1317;
    }
    #wr-table {
        height: 1fr;
        background: #0f1317;
        color: #d6dee5;
        scrollbar-size: 1 1;
        scrollbar-color: #2a3340;
    }
    #wr-table > .datatable--header {
        background: #111820;
        color: #97a6b2;
        text-style: bold;
    }
    #wr-table > .datatable--cursor {
        background: #253140;
        color: #f2f6f8;
    }
    #wr-table > .datatable--hover {
        background: #1e2730;
    }
    #wr-empty {
        height: 1fr;
        content-align: center middle;
        color: #6b7a88;
    }
    #wr-hint {
        height: 1;
        padding: 0 2;
        color: #3e4c5a;
        background: #0c0f12;
    }
    """

    BINDINGS = [
        Binding("r,R", "refresh", "Refresh"),
        Binding("a,A", "toggle_auto", "Auto-refresh"),
        Binding("enter", "jump_to_project", "Open"),
        Binding("d", "jump_to_worker", "Discuss"),
        Binding("colon", "open_command_palette", "Palette", priority=True),
        Binding("question_mark", "show_keyboard_help", "Help", priority=True),
        Binding("q,escape", "back", "Back"),
    ]

    AUTO_REFRESH_SECONDS = 5.0

    _STATUS_DOTS: dict[str, tuple[str, str]] = {
        "working": ("\u25cf", "#3ddc84"),
        "idle": ("\u25cb", "#97a6b2"),
        "stuck": ("\u25b2", "#ff5f6d"),
        "offline": ("\u25cf", "#4a5568"),
    }

    _DEFAULT_HINT = (
        "R refresh \u00b7 A auto-refresh \u00b7 \u21b5 open project "
        "\u00b7 d discuss \u00b7 q back"
    )

    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self.config_path = config_path
        self.topbar = Static(
            "[b #eef6ff]Workers[/b #eef6ff]", id="wr-topbar", markup=True,
        )
        self.counters = Static("", id="wr-counters", markup=True)
        self.table = DataTable(id="wr-table", zebra_stripes=False)
        self.hint = Static(self._DEFAULT_HINT, id="wr-hint", markup=True)
        self.empty = Static(
            "[dim]No workers yet.\n\n"
            "Start a worker from a project dashboard (press [b]w[/b]).[/dim]",
            id="wr-empty",
            markup=True,
        )
        self._rows: list = []
        self._auto_refresh: bool = False
        self._auto_refresh_timer = None

    def compose(self) -> ComposeResult:
        with Vertical(id="wr-outer"):
            yield self.topbar
            yield self.counters
            with Vertical(id="wr-table-wrap"):
                yield self.table
        yield self.hint

    def on_mount(self) -> None:
        self.table.cursor_type = "row"
        self.table.add_columns(
            "Project", "Session", " ", "Task", "Node", "Turn", "Last commit",
        )
        self._refresh()
        _setup_alert_notifier(self, bind_a=False)

    def action_open_command_palette(self) -> None:
        _open_command_palette(self)

    def action_show_keyboard_help(self) -> None:
        _open_keyboard_help(self)

    def _gather(self) -> list:
        """Hookable seam: tests monkeypatch this to inject synthetic rows."""
        from pollypm.cockpit import _gather_worker_roster

        try:
            config = load_config(self.config_path)
        except Exception:  # noqa: BLE001
            return []
        try:
            return _gather_worker_roster(config)
        except Exception:  # noqa: BLE001
            return []

    def _refresh(self) -> None:
        try:
            rows = self._gather()
        except Exception as exc:  # noqa: BLE001
            self.topbar.update(
                f"[#ff5f6d]Error loading workers:[/#ff5f6d] {_escape(str(exc))}"
            )
            return
        self._rows = rows
        self._render()

    def _render(self) -> None:
        self.table.clear()
        rows = self._rows
        n_working = sum(1 for row in rows if row.status == "working")
        n_idle = sum(1 for row in rows if row.status == "idle")
        n_stuck = sum(1 for row in rows if row.status == "stuck")
        n_offline = sum(1 for row in rows if row.status == "offline")
        auto_tag = (
            "[#3ddc84]auto on[/#3ddc84]" if self._auto_refresh
            else "[dim]auto off[/dim]"
        )
        self.counters.update(
            f"[b]{n_working}[/b] [dim]working[/dim]  "
            f"[b]{n_idle}[/b] [dim]idle[/dim]  "
            f"[#ff5f6d]{n_stuck}[/#ff5f6d] [dim]stuck[/dim]  "
            f"[dim]{n_offline} offline[/dim]  \u00b7  {auto_tag}"
        )
        self.topbar.update(
            "   ".join(
                [
                    "[b #eef6ff]Workers[/b #eef6ff]",
                    f"[#97a6b2]{len(rows)} session{'s' if len(rows) != 1 else ''}[/#97a6b2]",
                ]
            )
        )

        if not rows:
            self.hint.update(
                "[dim]No workers \u00b7 press [b]R[/b] to refresh \u00b7 "
                "[b]A[/b] auto-refresh \u00b7 [b]q[/b] back[/dim]"
            )
            return

        self.hint.update(self._DEFAULT_HINT)
        for row in rows:
            glyph, colour = self._STATUS_DOTS.get(row.status, ("\u25cb", "#6b7a88"))
            dot = Text.assemble((glyph, colour))
            project_cell = Text(row.project_name or row.project_key, style="#5b8aff")
            session_cell = Text(row.session_name, style="#d6dee5")
            task_text = (
                f"#{row.task_number} {row.task_title}".rstrip()
                if row.task_number is not None
                else "(none)"
            )
            self.table.add_row(
                project_cell,
                session_cell,
                dot,
                Text(task_text, style="#d6dee5"),
                Text(row.current_node or "\u2014", style="#97a6b2"),
                Text(row.turn_label, style="#97a6b2"),
                Text(row.last_commit_label, style="#6b7a88"),
                key=f"{row.project_key}:{row.session_name}",
            )

    def _selected_row(self):
        """Return the row currently under the cursor, or ``None``."""
        if not self._rows:
            return None
        try:
            cursor = self.table.cursor_row
        except Exception:  # noqa: BLE001
            cursor = 0
        if cursor is None:
            cursor = 0
        if not (0 <= cursor < len(self._rows)):
            return None
        return self._rows[cursor]

    def action_refresh(self) -> None:
        self._refresh()

    def action_toggle_auto(self) -> None:
        self._auto_refresh = not self._auto_refresh
        if self._auto_refresh:
            if self._auto_refresh_timer is None:
                self._auto_refresh_timer = self.set_interval(
                    self.AUTO_REFRESH_SECONDS, self._refresh,
                )
            self.notify(
                f"Auto-refresh on ({int(self.AUTO_REFRESH_SECONDS)}s).",
                severity="information",
                timeout=2.0,
            )
        else:
            if self._auto_refresh_timer is not None:
                try:
                    self._auto_refresh_timer.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._auto_refresh_timer = None
            self.notify("Auto-refresh off.", severity="information", timeout=2.0)
        self._render()

    def action_back(self) -> None:
        self.exit()

    def action_jump_to_project(self) -> None:
        row = self._selected_row()
        if row is None:
            return
        self.run_worker(
            lambda: self._route_to_project_sync(row.project_key),
            thread=True,
            exclusive=True,
            group="wr_jump_project",
        )

    def _route_to_project_sync(self, project_key: str) -> None:
        try:
            self._perform_route_to_project(project_key)
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(
                self.notify,
                f"Jump to project failed: {exc}",
                severity="error",
            )
            return
        self.call_from_thread(
            self.notify,
            f"Opened dashboard for {project_key}.",
            severity="information",
            timeout=2.0,
        )

    def _perform_route_to_project(self, project_key: str) -> None:
        """Route the cockpit right pane to the project's dashboard."""
        router = CockpitRouter(self.config_path)
        router.route_selected(f"project:{project_key}:dashboard")

    def action_jump_to_worker(self) -> None:
        row = self._selected_row()
        if row is None:
            return
        self.run_worker(
            lambda: self._dispatch_to_worker_sync(row),
            thread=True,
            exclusive=True,
            group="wr_jump_worker",
        )

    def _dispatch_to_worker_sync(self, row) -> None:
        try:
            self._perform_worker_dispatch(row)
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(
                self.notify,
                f"Jump to worker failed: {exc}",
                severity="error",
            )
            return
        self.call_from_thread(
            self.notify,
            f"Jumped to {row.session_name}.",
            severity="information",
            timeout=2.0,
        )

    def _perform_worker_dispatch(self, row) -> None:
        """Mount the worker's tmux window in the right pane."""
        router = CockpitRouter(self.config_path)
        if row.task_number is not None:
            router.route_selected(f"project:{row.project_key}:task:{row.task_number}")
            return
        router.route_selected(f"project:{row.project_key}:dashboard")

    @on(DataTable.RowSelected, "#wr-table")
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a row → jump to its project dashboard."""
        self.action_jump_to_project()
