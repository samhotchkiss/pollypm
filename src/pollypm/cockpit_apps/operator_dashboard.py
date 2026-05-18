"""Operator dashboard Textual app (#1572).

The 1-second answer to "what is the state of the system?" Three
vertical sections — Waiting on you, Working, Idle — each driven by
:func:`pollypm.dashboard.categorize_project` so the per-project
category here matches the glyph the rail draws.

The data path runs through :mod:`pollypm.cockpit_inbox` (for the
waits-on-user inbox view) and :mod:`pollypm.work` (for live workers /
task status). No direct ``Supervisor`` import — the boundary stays
clean per ``docs/architecture.md``.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Static

from pollypm.cockpit_markup import _escape
from pollypm.cockpit_palette import _open_keyboard_help
from pollypm.dashboard import (
    OperatorDashboardView,
    ProjectState,
    glyph_for_project_state,
)


def _empty_view() -> OperatorDashboardView:
    return OperatorDashboardView()


class PollyOperatorDashboardApp(App[None]):
    """Top-of-rail operator surface: Waiting / Working / Idle."""

    TITLE = "PollyPM"
    SUB_TITLE = "Operator"
    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("i", "jump_inbox", "Inbox"),
        Binding("question_mark", "show_keyboard_help", "Help", priority=True),
        Binding("j,down", "scroll_down", "Down", show=False),
        Binding("k,up", "scroll_up", "Up", show=False),
        Binding("g,home", "scroll_home", "Top", show=False),
        Binding("G,end", "scroll_end", "Bottom", show=False),
        Binding("pageup,b", "page_up", "Page up", show=False),
        Binding("pagedown,space,f", "page_down", "Page down", show=False),
    ]
    CSS = """
    Screen {
        background: #0d1117;
        color: #c9d1d9;
        padding: 0 1;
        layout: vertical;
        overflow-y: auto;
    }
    .header { padding: 1 0 0 0; color: #8b949e; }
    .section-title {
        color: #58a6ff;
        text-style: bold;
        padding: 1 0 0 0;
    }
    .section-body { padding: 0 0 0 2; }
    .footer { color: #484f58; padding: 1 0 0 0; }
    """

    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self.config_path = config_path
        self.header_w = Static("", classes="header", markup=True)
        self.waiting_title = Static("[b]Waiting on you[/b]", classes="section-title", markup=True)
        self.waiting_body = Static("", classes="section-body", markup=True)
        self.working_title = Static("[b]Working[/b]", classes="section-title", markup=True)
        self.working_body = Static("", classes="section-body", markup=True)
        self.idle_title = Static("[b]Idle[/b]", classes="section-title", markup=True)
        self.idle_body = Static("", classes="section-body", markup=True)
        self.paused_title = Static("[b]Paused[/b]", classes="section-title", markup=True)
        self.paused_body = Static("", classes="section-body", markup=True)
        self.footer_w = Static("", classes="footer", markup=True)
        self._view: OperatorDashboardView | None = None
        self._refresh_running = False
        self._refresh_error: str | None = None

    def compose(self) -> ComposeResult:
        yield self.header_w
        yield self.waiting_title
        yield self.waiting_body
        yield self.working_title
        yield self.working_body
        yield self.idle_title
        yield self.idle_body
        yield self.paused_title
        yield self.paused_body
        yield self.footer_w

    def on_mount(self) -> None:
        self._refresh()
        self.set_interval(10, self._refresh)
        try:
            from pollypm.cockpit_input_bridge import start_input_bridge
            self._input_bridge_handle = start_input_bridge(
                self, kind="operator", config_path=self.config_path,
            )
        except Exception:  # noqa: BLE001
            self._input_bridge_handle = None

    def on_unmount(self) -> None:
        bridge = getattr(self, "_input_bridge_handle", None)
        if bridge is not None:
            try:
                bridge.stop()
            except Exception:  # noqa: BLE001
                pass

    def action_show_keyboard_help(self) -> None:
        _open_keyboard_help(self)

    def action_refresh(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        self._render_view(self._view or _empty_view())
        if self._refresh_running:
            return
        self._refresh_running = True
        self.run_worker(
            self._refresh_view_sync,
            thread=True,
            exclusive=True,
            group="polly_operator_refresh",
        )

    def _refresh_view_sync(self) -> None:
        try:
            from pollypm.dashboard.operator_view import load_operator_view

            view = load_operator_view(self.config_path)
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self._finish_refresh_error, str(exc))
            return
        self.call_from_thread(self._finish_refresh_success, view)

    def _finish_refresh_success(self, view: OperatorDashboardView) -> None:
        self._view = view
        self._refresh_running = False
        self._refresh_error = None
        self._render_view(view)

    def _finish_refresh_error(self, error: str) -> None:
        self._refresh_running = False
        self._refresh_error = error
        self._render_view(self._view or _empty_view())

    def _render_view(self, view: OperatorDashboardView) -> None:
        if self._view is None and self._refresh_error is None:
            self.header_w.update("[dim]Loading operator dashboard…[/dim]")
        else:
            counts = (
                f"[#d29922]{len(view.waiting)}[/#d29922] waiting  "
                f"·  [#3fb950]{len(view.working)}[/#3fb950] working  "
                f"·  [dim]{len(view.idle)} idle[/dim]"
            )
            if view.paused:
                counts += f"  ·  [dim]{len(view.paused)} paused[/dim]"
            self.header_w.update(f"  {counts}")

        self.waiting_body.update(
            _format_section(view.waiting, empty="Nothing waiting.")
        )
        self.working_body.update(
            _format_section(view.working, empty="Nothing actively working.")
        )
        self.idle_body.update(
            _format_section(view.idle, empty="All projects busy.")
        )
        if view.paused:
            self.paused_title.update("[b]Paused[/b]")
            self.paused_body.update(_format_section(view.paused, empty=""))
        else:
            self.paused_title.update("")
            self.paused_body.update("")

        footer = "[dim]Press [b]r[/b] to refresh · [b]i[/b] to jump to inbox[/dim]"
        if self._refresh_error:
            footer += f"  ·  [#f85149]error: {_escape(self._refresh_error)}[/#f85149]"
        self.footer_w.update(footer)

    def action_jump_inbox(self) -> None:
        from pollypm.cockpit_navigation_client import file_navigation_client

        try:
            file_navigation_client(
                self.config_path, client_id="polly-operator",
            ).jump_to_inbox()
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Jump to inbox failed: {exc}", severity="error")

    def _dashboard_screen(self):  # noqa: ANN202
        return self.screen

    def action_scroll_down(self) -> None:
        try:
            self._dashboard_screen().scroll_down(animate=False)
        except Exception:  # noqa: BLE001
            pass

    def action_scroll_up(self) -> None:
        try:
            self._dashboard_screen().scroll_up(animate=False)
        except Exception:  # noqa: BLE001
            pass

    def action_scroll_home(self) -> None:
        try:
            self._dashboard_screen().scroll_home(animate=False)
        except Exception:  # noqa: BLE001
            pass

    def action_scroll_end(self) -> None:
        try:
            self._dashboard_screen().scroll_end(animate=False)
        except Exception:  # noqa: BLE001
            pass

    def action_page_up(self) -> None:
        try:
            self._dashboard_screen().scroll_page_up(animate=False)
        except Exception:  # noqa: BLE001
            pass

    def action_page_down(self) -> None:
        try:
            self._dashboard_screen().scroll_page_down(animate=False)
        except Exception:  # noqa: BLE001
            pass


def _format_section(rows, *, empty: str) -> str:
    """Render one section's rows as a multi-line string."""
    if not rows:
        return f"[dim]{_escape(empty)}[/dim]" if empty else ""
    lines: list[str] = []
    for row in rows:
        glyph = row.glyph if row.glyph else glyph_for_project_state(row.state)
        color = _color_for_state(row.state)
        glyph_markup = f"[{color}]{glyph}[/{color}]" if color else glyph
        project = _escape(row.project_key)
        detail = _escape(row.detail)
        if row.state is ProjectState.IDLE:
            lines.append(f"{glyph_markup} [dim]{project}[/dim]  [dim]{detail}[/dim]")
        elif row.state is ProjectState.PAUSED:
            lines.append(f"{glyph_markup} [dim]{project}[/dim]  [dim]{detail}[/dim]")
        else:
            lines.append(f"{glyph_markup} [b]{project}[/b]  {detail}")
    return "\n".join(lines)


def _color_for_state(state: ProjectState) -> str:
    if state is ProjectState.WAITING:
        return "#d29922"
    if state is ProjectState.WORKING:
        return "#3fb950"
    if state is ProjectState.IDLE:
        return "#484f58"
    if state is ProjectState.PAUSED:
        return "#484f58"
    return ""
