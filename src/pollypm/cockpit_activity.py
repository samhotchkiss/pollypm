"""Cockpit activity-feed panel.

Contract:
- Inputs: a cockpit config path plus projected activity-feed entries from
  ``pollypm.cockpit``.
- Outputs: ``PollyActivityFeedApp`` and the event-colour helpers it owns.
- Side effects: loads config, refreshes/polls activity rows, mounts alert
  toasts, and opens detail/filter UI inside the panel.
- Invariants: activity filtering stays local to the loaded window and the
  module exposes a stable public surface for the activity cockpit screen.
"""

from __future__ import annotations

from pathlib import Path

from rich.markup import escape as _escape
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import DataTable, Input, Static

from pollypm.cockpit_alerts import _action_view_alerts, _setup_alert_notifier
from pollypm.cockpit_palette import _open_keyboard_help
from pollypm.config import load_config


_ACTIVITY_TYPE_COLOURS: dict[str, str] = {
    "task.done": "#3ddc84",
    "task_done": "#3ddc84",
    "task.approved": "#3ddc84",
    "approve": "#3ddc84",
    "approved": "#3ddc84",
    "completed": "#3ddc84",
    "task.created": "#f0c45a",
    "task_created": "#f0c45a",
    "task.queued": "#f0c45a",
    "queued": "#f0c45a",
    "created": "#f0c45a",
    "alert": "#ff5f6d",
    "error": "#ff5f6d",
    "stuck": "#ff5f6d",
    "rejection": "#ff5f6d",
    "rejected": "#ff5f6d",
    "state_drift": "#ff5f6d",
    "persona_swap": "#ff5f6d",
    "heartbeat": "#6b7a88",
    "ran": "#6b7a88",
    "tick": "#6b7a88",
    "poll": "#6b7a88",
}


def _activity_type_colour(kind: str, severity: str | None = None) -> str:
    """Resolve the Rich colour for an event row's "Event type" column."""
    lowered = (kind or "").lower()
    colour = _ACTIVITY_TYPE_COLOURS.get(lowered)
    if colour is not None:
        return colour
    if "reject" in lowered or "drift" in lowered or "swap" in lowered:
        return "#ff5f6d"
    if "done" in lowered or "approve" in lowered or "complete" in lowered:
        return "#3ddc84"
    if "create" in lowered or "queue" in lowered:
        return "#f0c45a"
    if "heartbeat" in lowered or "tick" in lowered or "poll" in lowered or "ran" in lowered:
        return "#6b7a88"
    if severity == "critical":
        return "#ff5f6d"
    if severity == "recommendation":
        return "#f0c45a"
    return "#97a6b2"


def _format_activity_relative(timestamp: str) -> str:
    """Wrap ``format_relative_time`` in a fallback for empty rows."""
    if not timestamp:
        return "\u2014"
    try:
        from pollypm.plugins_builtin.activity_feed.cockpit.feed_panel import (
            format_relative_time,
        )

        return format_relative_time(timestamp)
    except Exception:  # noqa: BLE001
        return timestamp[:16]


def _truncate_summary(text: str, *, width: int = 80) -> str:
    """Tail-truncate a summary line so wide rows stay one cell tall."""
    if not text:
        return ""
    cleaned = text.replace("\n", " ").strip()
    if len(cleaned) <= width:
        return cleaned
    return cleaned[: width - 1] + "\u2026"


class PollyActivityFeedApp(App[None]):
    """Full-screen activity feed — ``pm cockpit-pane activity``."""

    TITLE = "PollyPM"
    SUB_TITLE = "Activity"

    INITIAL_LIMIT = 200
    MAX_ROWS_IN_MEMORY = 500
    FOLLOW_INTERVAL_SECONDS = 2.0

    CSS = """
    Screen {
        background: #0f1317;
        color: #eef2f4;
        padding: 0;
    }
    #af-outer {
        height: 1fr;
        padding: 1 2;
    }
    #af-topbar {
        height: 1;
        padding: 0 0 0 0;
        color: #eef6ff;
    }
    #af-counters {
        height: 1;
        padding: 0 0 1 0;
        color: #97a6b2;
        border-bottom: solid #1e2730;
    }
    #af-table-wrap {
        height: 1fr;
        padding: 1 0 0 0;
        background: #0f1317;
    }
    #af-table {
        height: 1fr;
        background: #0f1317;
        color: #d6dee5;
        scrollbar-size: 1 1;
        scrollbar-color: #2a3340;
    }
    #af-table > .datatable--header {
        background: #111820;
        color: #97a6b2;
        text-style: bold;
    }
    #af-table > .datatable--cursor {
        background: #253140;
        color: #f2f6f8;
    }
    #af-table > .datatable--hover {
        background: #1e2730;
    }
    #af-detail {
        height: auto;
        max-height: 16;
        padding: 1 2;
        background: #111820;
        border: round #1e2730;
        color: #d6dee5;
    }
    #af-filter-input {
        height: 3;
        padding: 0 1;
        background: #111820;
        border: round #2a3340;
        color: #d6dee5;
    }
    #af-filter-input:focus {
        border: round #5b8aff;
    }
    #af-hint {
        height: 1;
        padding: 0 2;
        color: #3e4c5a;
        background: #0c0f12;
    }
    """

    BINDINGS = [
        Binding("j,down", "cursor_down", "Down", show=False),
        Binding("k,up", "cursor_up", "Up", show=False),
        Binding("g,home", "cursor_first", "Top", show=False),
        Binding("G,end", "cursor_last", "Bottom", show=False),
        Binding("slash", "start_fuzzy", "Filter", show=False),
        Binding("p", "pick_project", "Project"),
        Binding("t", "pick_type", "Type"),
        Binding("F", "toggle_follow", "Follow"),
        Binding("c", "clear_filters", "Clear"),
        Binding("R,u", "refresh", "Refresh", show=False),
        Binding("a", "view_alerts", "Alerts", show=False),
        Binding("enter", "open_detail", "Open"),
        Binding("question_mark", "show_keyboard_help", "Help", priority=True),
        Binding("q,escape", "back_or_cancel", "Back"),
    ]

    _DEFAULT_HINT = (
        "j/k move \u00b7 / fuzzy \u00b7 p project \u00b7 t type "
        "\u00b7 F follow \u00b7 c clear \u00b7 \u21b5 detail \u00b7 q back"
    )

    def __init__(self, config_path: Path, *, project_key: str | None = None) -> None:
        super().__init__()
        self.config_path = config_path
        self._initial_project_filter = project_key or None
        self.topbar = Static("", id="af-topbar", markup=True)
        self.counters = Static("", id="af-counters", markup=True)
        self.table = DataTable(id="af-table", zebra_stripes=False)
        self.detail = Static("", id="af-detail", markup=True)
        self.filter_input = Input(
            placeholder="filter \u2026  (Enter to apply, Esc to cancel)",
            id="af-filter-input",
        )
        self.hint = Static(self._DEFAULT_HINT, id="af-hint", markup=True)
        self._entries: list = []
        self._filter_project: str | None = self._initial_project_filter
        self._filter_actor: str | None = None
        self._filter_type: str | None = None
        self._filter_fuzzy: str = ""
        self._filter_mode: str | None = None
        self._show_filter_input: bool = False
        self._open_entry_id: str | None = None
        self._follow_on: bool = False
        self._follow_timer = None

    def compose(self) -> ComposeResult:
        with Vertical(id="af-outer"):
            yield self.topbar
            yield self.counters
            with Vertical(id="af-table-wrap"):
                yield self.table
            yield self.detail
            yield self.filter_input
        yield self.hint

    def on_mount(self) -> None:
        self.table.cursor_type = "row"
        self.table.add_columns("Time", "Project", "Actor", "Event", "Message")
        self.detail.display = False
        self.filter_input.display = False
        self._refresh()
        self.table.focus()
        _setup_alert_notifier(self, bind_a=True)

    def action_show_keyboard_help(self) -> None:
        _open_keyboard_help(self)

    def action_view_alerts(self) -> None:
        _action_view_alerts(self)

    def _gather(self):
        """Hookable seam — tests inject synthetic feed-entry lists."""
        from pollypm.cockpit import _gather_activity_feed

        try:
            config = load_config(self.config_path)
        except Exception:  # noqa: BLE001
            return []
        return _gather_activity_feed(
            config,
            project=self._filter_project,
            limit=self.INITIAL_LIMIT,
        )

    def _refresh(self) -> None:
        try:
            entries = self._gather()
        except Exception as exc:  # noqa: BLE001
            self.topbar.update(
                f"[#ff5f6d]Error loading activity:[/#ff5f6d] {_escape(str(exc))}"
            )
            return
        self._entries = list(entries)[: self.MAX_ROWS_IN_MEMORY]
        self._render()

    def _follow_tick(self) -> None:
        try:
            fresh = self._gather()
        except Exception:  # noqa: BLE001
            return
        if not fresh:
            return
        seen = {entry.id for entry in self._entries}
        new_rows = [entry for entry in fresh if entry.id not in seen]
        if not new_rows:
            return
        self._entries = (list(new_rows) + self._entries)[: self.MAX_ROWS_IN_MEMORY]
        self._render()

    def _filtered_entries(self) -> list:
        rows = self._entries
        project = self._filter_project
        actor = self._filter_actor
        kind = self._filter_type
        fuzzy = self._filter_fuzzy.strip().lower()
        if not (project or actor or kind or fuzzy):
            return list(rows)
        out = []
        for entry in rows:
            if project and (entry.project or "") != project:
                continue
            if actor and (entry.actor or "") != actor:
                continue
            if kind and (entry.kind or "") != kind:
                continue
            if fuzzy:
                hay = " ".join(
                    [
                        entry.actor or "",
                        entry.kind or "",
                        entry.verb or "",
                        entry.summary or "",
                        entry.project or "",
                    ]
                ).lower()
                if fuzzy not in hay:
                    continue
            out.append(entry)
        return out

    def _events_in_last_24h(self) -> int:
        from datetime import UTC, datetime, timedelta

        cutoff = datetime.now(UTC) - timedelta(hours=24)
        count = 0
        for entry in self._entries:
            timestamp = getattr(entry, "timestamp", "") or ""
            try:
                when = datetime.fromisoformat(timestamp)
            except (TypeError, ValueError):
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            if when >= cutoff:
                count += 1
        return count

    def _render(self) -> None:
        filtered_entries = self._filtered_entries()
        events_last_24h = self._events_in_last_24h()
        title_bits = ["[b #eef6ff]Activity[/b #eef6ff]"]
        if self._filter_project:
            title_bits.append(
                f"[#5b8aff]\u00b7 project: [b]{_escape(self._filter_project)}[/b][/#5b8aff]"
            )
        self.topbar.update("  ".join(title_bits))

        chips: list[str] = [
            f"[b]{events_last_24h}[/b] [dim]event{'s' if events_last_24h != 1 else ''} in last 24h[/dim]"
        ]
        filter_description = self._describe_filters()
        if filter_description:
            chips.append(f"[#97a6b2]filters: {filter_description}[/#97a6b2]")
        chips.append(
            "[#3ddc84]follow on[/#3ddc84]" if self._follow_on else "[dim]follow off[/dim]"
        )
        self.counters.update("  \u00b7  ".join(chips))

        self._render_table(filtered_entries)
        if self._open_entry_id is not None:
            self._render_detail()
        else:
            self.detail.update("")
            self.detail.display = False

        self.filter_input.display = self._show_filter_input
        if self._show_filter_input:
            mode_label = self._filter_mode or "filter"
            self.hint.update(
                f"[dim]{mode_label}: type to filter \u00b7 \u21b5 apply \u00b7 esc cancel[/dim]"
            )
        elif self._open_entry_id is not None:
            self.hint.update("[dim]\u21b5 close detail \u00b7 j/k next \u00b7 q back[/dim]")
        else:
            self.hint.update(self._DEFAULT_HINT)

    def _render_table(self, rows: list) -> None:
        self.table.clear()
        for entry in rows:
            time_text = Text(_format_activity_relative(entry.timestamp), style="#97a6b2")
            project_label = entry.project or "\u2014"
            if self._filter_project and (entry.project or "") == self._filter_project:
                project_text = Text(project_label, style="bold #eef6ff")
            elif entry.project:
                project_text = Text(project_label, style="#5b8aff")
            else:
                project_text = Text(project_label, style="#6b7a88")
            self.table.add_row(
                time_text,
                project_text,
                Text(entry.actor or "system", style="#d6dee5"),
                Text(entry.verb or entry.kind or "", style=_activity_type_colour(entry.kind or "", entry.severity)),
                Text(_truncate_summary(entry.summary or ""), style="#d6dee5"),
                key=entry.id,
            )

    def _render_detail(self) -> None:
        entry = self._entry_by_id(self._open_entry_id)
        if entry is None:
            self.detail.update("")
            self.detail.display = False
            return
        try:
            from pollypm.plugins_builtin.activity_feed.cockpit.feed_panel import (
                render_entry_detail,
            )

            text = render_entry_detail(entry)
        except Exception:  # noqa: BLE001
            text = (
                f"id: {entry.id}\nkind: {entry.kind}\nactor: {entry.actor}"
                f"\nsummary: {entry.summary}"
            )
        self.detail.update(f"[dim]{_escape(text)}[/dim]")
        self.detail.display = True

    def _describe_filters(self) -> str:
        bits: list[str] = []
        if self._filter_project:
            bits.append(f"project={self._filter_project}")
        if self._filter_actor:
            bits.append(f"actor={self._filter_actor}")
        if self._filter_type:
            bits.append(f"type={self._filter_type}")
        if self._filter_fuzzy:
            bits.append(f'"{self._filter_fuzzy}"')
        return " \u00b7 ".join(bits)

    def _entry_by_id(self, entry_id: str | None):
        if entry_id is None:
            return None
        for entry in self._entries:
            if entry.id == entry_id:
                return entry
        return None

    def action_cursor_down(self) -> None:
        try:
            self.table.action_cursor_down()
        except Exception:  # noqa: BLE001
            pass

    def action_cursor_up(self) -> None:
        try:
            self.table.action_cursor_up()
        except Exception:  # noqa: BLE001
            pass

    def action_cursor_first(self) -> None:
        try:
            self.table.move_cursor(row=0)
        except Exception:  # noqa: BLE001
            pass

    def action_cursor_last(self) -> None:
        try:
            self.table.move_cursor(row=max(0, self.table.row_count - 1))
        except Exception:  # noqa: BLE001
            pass

    def action_start_fuzzy(self) -> None:
        self._open_filter("fuzzy", placeholder="fuzzy: actor or event type")

    def action_pick_project(self) -> None:
        keys = sorted({entry.project or "" for entry in self._entries if entry.project})
        hint = (
            f"project: {', '.join(keys[:6])}{' \u2026' if len(keys) > 6 else ''}"
            if keys
            else "project: (no projects in current window)"
        )
        self._open_filter("project", placeholder=hint)

    def action_pick_type(self) -> None:
        kinds = sorted({entry.kind or "" for entry in self._entries if entry.kind})
        hint = (
            f"type: {', '.join(kinds[:6])}{' \u2026' if len(kinds) > 6 else ''}"
            if kinds
            else "type: (no event types in current window)"
        )
        self._open_filter("type", placeholder=hint)

    def _open_filter(self, mode: str, *, placeholder: str) -> None:
        self._filter_mode = mode
        self._show_filter_input = True
        if mode == "fuzzy":
            self.filter_input.value = self._filter_fuzzy
        elif mode == "project":
            self.filter_input.value = self._filter_project or ""
        elif mode == "type":
            self.filter_input.value = self._filter_type or ""
        self.filter_input.placeholder = placeholder
        self._render()
        self.filter_input.focus()

    def _close_filter(self) -> None:
        self._filter_mode = None
        self._show_filter_input = False
        self.filter_input.value = ""
        self._render()
        self.table.focus()

    @on(Input.Submitted, "#af-filter-input")
    def _on_filter_submit(self, event: Input.Submitted) -> None:
        value = (event.value or "").strip()
        if self._filter_mode == "fuzzy":
            self._filter_fuzzy = value
        elif self._filter_mode == "project":
            self._filter_project = value or None
        elif self._filter_mode == "type":
            self._filter_type = value or None
        self._close_filter()

    def action_clear_filters(self) -> None:
        self._filter_project = None
        self._filter_actor = None
        self._filter_type = None
        self._filter_fuzzy = ""
        self._refresh()

    def action_toggle_follow(self) -> None:
        self._follow_on = not self._follow_on
        if self._follow_on:
            if self._follow_timer is None:
                self._follow_timer = self.set_interval(
                    self.FOLLOW_INTERVAL_SECONDS,
                    self._follow_tick,
                )
            self.notify(
                f"Follow mode on ({int(self.FOLLOW_INTERVAL_SECONDS)}s).",
                severity="information",
                timeout=2.0,
            )
        else:
            if self._follow_timer is not None:
                try:
                    self._follow_timer.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._follow_timer = None
            self.notify("Follow mode off.", severity="information", timeout=2.0)
        self._render()

    def action_open_detail(self) -> None:
        if self._open_entry_id is not None:
            self._open_entry_id = None
            self._render()
            self.table.focus()
            return
        rows = self._filtered_entries()
        if not rows:
            return
        try:
            cursor = self.table.cursor_row
        except Exception:  # noqa: BLE001
            cursor = 0
        if cursor is None:
            cursor = 0
        cursor = max(0, min(cursor, len(rows) - 1))
        self._open_entry_id = rows[cursor].id
        self._render()

    def action_refresh(self) -> None:
        self._refresh()

    def action_back_or_cancel(self) -> None:
        if self._show_filter_input:
            self._close_filter()
            return
        if self._open_entry_id is not None:
            self._open_entry_id = None
            self._render()
            self.table.focus()
            return
        self.exit()

    @on(DataTable.RowSelected, "#af-table")
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a row → open the detail pane for that entry."""
        try:
            key = event.row_key.value if event.row_key else None
        except Exception:  # noqa: BLE001
            key = None
        if key is None:
            self.action_open_detail()
            return
        self._open_entry_id = key
        self._render()
