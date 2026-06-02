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

import json
from pathlib import Path
import re

from rich.markup import escape as _escape
from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import DataTable, Input, Static

from pollypm.activity_low_signal import (
    LOW_SIGNAL_ACTIVITY_KINDS as _LOW_SIGNAL_ACTIVITY_KINDS,
    LOW_SIGNAL_EMPTY_ACTIVITY_KINDS as _LOW_SIGNAL_EMPTY_ACTIVITY_KINDS,
    is_low_signal_activity as _shared_is_low_signal_activity,
)
from pollypm.cockpit_alerts import _action_view_alerts
from pollypm.cockpit_palette import _open_keyboard_help
from pollypm.cockpit_theme import State
from pollypm.config import load_config


# Event-type to semantic-color map. Rich-markup callers pass the returned
# hex string straight into a ``[<color>]…[/<color>]`` markup span. Sourced
# from ``cockpit_theme.State`` so a palette tweak lands here without
# touching this file — see the 2026-05-20 audit (PR #1988) for why the
# inline hex literals lived in five duplicated copies before.
_ACTIVITY_TYPE_COLOURS: dict[str, str] = {
    "task.done": State.WORKING,
    "task_done": State.WORKING,
    "task.approved": State.WORKING,
    "approve": State.WORKING,
    "approved": State.WORKING,
    "completed": State.WORKING,
    "task.created": State.WAITING,
    "task_created": State.WAITING,
    "task.queued": State.WAITING,
    "queued": State.WAITING,
    "created": State.WAITING,
    "alert": State.BLOCKED,
    "error": State.BLOCKED,
    "stuck": State.BLOCKED,
    "rejection": State.BLOCKED,
    "rejected": State.BLOCKED,
    "state_drift": State.BLOCKED,
    "persona_swap": State.BLOCKED,
    "heartbeat": State.MUTED,
    "ran": State.MUTED,
    "tick": State.MUTED,
    "poll": State.MUTED,
}


def _activity_type_colour(kind: str, severity: str | None = None) -> str:
    """Resolve the Rich colour for an event row's "Event type" column."""
    lowered = (kind or "").lower()
    colour = _ACTIVITY_TYPE_COLOURS.get(lowered)
    if colour is not None:
        return colour
    if "reject" in lowered or "drift" in lowered or "swap" in lowered:
        return State.BLOCKED
    if "done" in lowered or "approve" in lowered or "complete" in lowered:
        return State.WORKING
    if "create" in lowered or "queue" in lowered:
        return State.WAITING
    if "heartbeat" in lowered or "tick" in lowered or "poll" in lowered or "ran" in lowered:
        return State.MUTED
    if severity == "critical":
        return State.BLOCKED
    if severity == "recommendation":
        return State.WAITING
    return State.NEUTRAL


def _format_activity_relative(timestamp: str) -> str:
    """Wrap ``format_relative_time`` in a fallback for empty rows."""
    if not timestamp:
        return "\u2014"
    try:
        # #1363: pulled from the shared protocol module so this file no
        # longer depends on ``plugins_builtin`` for plain-text formatting.
        from pollypm.activity_feed_protocol import format_relative_time

        return format_relative_time(timestamp)
    except Exception:  # noqa: BLE001
        return timestamp[:16]


def _truncate_summary(text: str, *, width: int = 96) -> str:
    """Tail-truncate a summary line while preserving action hints."""
    if not text:
        return ""
    cleaned = text.replace("\n", " ").strip()
    if len(cleaned) <= width:
        return cleaned
    match = re.search(r"\bOpen (?:Tasks|Workers|Settings|Polly|Inbox)\b", cleaned)
    if match is not None:
        action_hint = cleaned[match.start():].strip()
        if len(action_hint) < width - 5:
            head_budget = width - len(action_hint) - 3
            if head_budget >= 12:
                return f"{cleaned[:head_budget].rstrip()}\u2026 {action_hint}"
        if len(action_hint) <= width:
            return action_hint
    return cleaned[: width - 1] + "\u2026"


def _entry_search_haystack(entry) -> str:
    """Flatten a feed entry into searchable text for the live filter."""
    payload = getattr(entry, "payload", {}) or {}
    task_project = payload.get("task_project") if isinstance(payload, dict) else None
    task_number = payload.get("task_number") if isinstance(payload, dict) else None
    bits: list[str] = [
        entry.id or "",
        entry.project or "",
        entry.actor or "",
        entry.kind or "",
        entry.verb or "",
        entry.summary or "",
        entry.subject or "",
        entry.severity or "",
        getattr(entry, "source", "") or "",
    ]
    if task_project and task_number is not None:
        bits.extend(
            [
                f"{task_project}/{task_number}",
                f"project:{task_project}:task:{task_number}",
            ]
        )
    if payload:
        try:
            bits.append(json.dumps(payload, sort_keys=True))
        except (TypeError, ValueError):
            bits.append(str(payload))
    return " ".join(bit for bit in bits if bit).lower()


def _is_low_signal_activity(entry) -> bool:
    return _shared_is_low_signal_activity(
        kind=getattr(entry, "kind", ""),
        verb=getattr(entry, "verb", ""),
        actor=getattr(entry, "actor", ""),
        summary=getattr(entry, "summary", ""),
    )


def _is_noise_type_filter(kind: str | None) -> bool:
    lowered = (kind or "").strip().lower()
    return (
        lowered in _LOW_SIGNAL_ACTIVITY_KINDS
        or lowered in _LOW_SIGNAL_EMPTY_ACTIVITY_KINDS
    )


def _parse_search_query(query: str) -> tuple[re.Pattern[str] | None, list[str]]:
    """Return a compiled regex matcher or lowercase literal tokens."""
    raw = (query or "").strip()
    if not raw:
        return None, []

    regex_source: str | None = None
    literal_source = raw
    if raw.startswith("re:"):
        regex_source = raw[3:].strip()
        literal_source = regex_source
    elif len(raw) >= 2 and raw.startswith("/") and raw.endswith("/"):
        regex_source = raw[1:-1]
        literal_source = regex_source

    if regex_source:
        try:
            return re.compile(regex_source, re.IGNORECASE), []
        except re.error:
            pass

    return None, [token.lower() for token in literal_source.split() if token]


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
        Binding("slash", "start_fuzzy", "Search", show=False),
        Binding("p", "pick_project", "Project"),
        Binding("t", "pick_type", "Type"),
        Binding("N", "toggle_noise", "Noise", show=False),
        Binding("F", "toggle_follow", "Follow"),
        Binding("c", "clear_filters", "Clear"),
        Binding("R,u", "refresh", "Refresh", show=False),
        Binding("a", "view_alerts", "Alerts", show=False),
        Binding("enter", "open_detail", "Open"),
        Binding("question_mark", "show_keyboard_help", "Help", priority=True),
        Binding("q,escape", "back_or_cancel", "Back"),
    ]

    _TYPEAHEAD_RESERVED_KEYS = frozenset({
        "a",
        "c",
        "g",
        "j",
        "k",
        "p",
        "q",
        "r",
        "t",
        "u",
    })

    _DEFAULT_HINT = (
        "j/k move \u00b7 / search \u00b7 p project \u00b7 t type "
        "\u00b7 N noise \u00b7 F follow \u00b7 c clear \u00b7 \u21b5 detail \u00b7 q back"
    )

    def __init__(self, config_path: Path, *, project_key: str | None = None) -> None:
        super().__init__()
        self.config_path = config_path
        self._config = None
        self._initial_project_filter = project_key or None
        self.topbar = Static("", id="af-topbar", markup=True)
        self.counters = Static("", id="af-counters", markup=True)
        self.table = DataTable(id="af-table", zebra_stripes=False)
        self.detail = Static("", id="af-detail", markup=True)
        self.filter_input = Input(
            placeholder=self._search_placeholder(),
            id="af-filter-input",
            select_on_focus=False,
        )
        self.hint = Static(self._DEFAULT_HINT, id="af-hint", markup=True)
        self._entries: list = []
        self._filter_project: str | None = self._initial_project_filter
        self._filter_actor: str | None = None
        self._filter_type: str | None = None
        self._filter_fuzzy: str = ""
        self._filter_mode: str | None = None
        self._show_system_noise: bool = False
        self._open_entry_id: str | None = None
        self._follow_on: bool = False
        self._follow_timer = None
        # #1649 — cold-boot off-thread gather. ``_initial_load_running``
        # gates the follow-mode tick + manual refresh so we don't race
        # the worker's ``call_from_thread`` completion against a parallel
        # sync gather. ``_initial_load_done`` flips to True after the
        # first gather completes (success or failure) so subsequent
        # refresh calls fall back to the synchronous path used by tests
        # that monkeypatch ``_gather``.
        self._initial_load_running: bool = False
        self._initial_load_done: bool = False

    def _load_config(self):
        if self._config is None:
            self._config = load_config(self.config_path)
        return self._config

    def compose(self) -> ComposeResult:
        with Vertical(id="af-outer"):
            yield self.topbar
            yield self.filter_input
            yield self.counters
            with Vertical(id="af-table-wrap"):
                yield self.table
            yield self.detail
        yield self.hint

    def on_mount(self) -> None:
        self.table.cursor_type = "row"
        self.table.add_columns("Time", "Project", "Actor", "Event", "Message")
        self.detail.display = False
        self.filter_input.value = self._filter_fuzzy
        self.table.focus()
        # #1649 — initial activity-feed gather opens the events DB and
        # projects entries on the Textual asyncio main thread, blocking
        # the first paint while sqlite IO finishes. Paint a "Loading
        # activity…" skeleton immediately, then run ``_gather`` on a
        # worker thread and apply the result back via
        # ``call_from_thread``. Follow-mode ticks + manual refresh
        # also go off-thread (see ``_refresh`` / ``_follow_tick``).
        self._paint_loading_skeleton()
        self._schedule_seen_marker()
        self._schedule_initial_load()
        # Alert toast surface removed in #956 — alerts already appear
        # as activity rows in the table below.

    def action_show_keyboard_help(self) -> None:
        _open_keyboard_help(self)

    def action_view_alerts(self) -> None:
        _action_view_alerts(self)

    def on_key(self, event: events.Key) -> None:
        if self.filter_input.has_focus or self._filter_mode is not None:
            return
        char = event.character or ""
        if len(char) != 1 or not char.isprintable() or char.isspace():
            return
        if char != char.lower() or char.lower() in self._TYPEAHEAD_RESERVED_KEYS:
            return
        event.prevent_default()
        event.stop()
        self.call_after_refresh(self._begin_typeahead_search, char)

    def _begin_typeahead_search(self, char: str) -> None:
        self._filter_mode = None
        self._filter_fuzzy = char
        self.filter_input.placeholder = self._search_placeholder()
        self.filter_input.value = char
        self.filter_input.focus()
        self.filter_input.cursor_position = len(char)
        self._render()

    def _gather(self):
        """Hookable seam — tests inject synthetic feed-entry lists."""
        from pollypm.cockpit import _gather_activity_feed

        try:
            config = self._load_config()
        except Exception:  # noqa: BLE001
            return []
        return _gather_activity_feed(
            config,
            project=self._filter_project,
            limit=self.INITIAL_LIMIT,
        )

    def _schedule_seen_marker(self) -> None:
        """Advance the rail badge cursor without delaying first paint."""
        try:
            self.run_worker(
                self._mark_seen_sync,
                thread=True,
                exclusive=True,
                group="activity_feed_seen",
            )
        except Exception:  # noqa: BLE001
            self._mark_seen_sync()

    def _mark_seen_sync(self) -> None:
        try:
            from pollypm.activity_projector_registry import mark_activity_seen

            mark_activity_seen(self._load_config())
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Cold-boot + steady-state refresh (#1649). The activity feed
    # gather opens sqlite + projects entries; running it on the main
    # asyncio thread blocks first paint. Pattern mirrors #1605 (inbox)
    # and #1653 (operator dashboard): paint a skeleton, dispatch a
    # ``run_worker(thread=True)`` gather, marshal the result back via
    # ``call_from_thread``.
    # ------------------------------------------------------------------

    def _paint_loading_skeleton(self) -> None:
        """Render a minimal "Loading…" placeholder before any IO."""
        try:
            self.topbar.update(f"[b {State.HEADING_BRIGHT}]Activity[/b {State.HEADING_BRIGHT}]")
        except Exception:  # noqa: BLE001
            pass
        try:
            self.counters.update("[dim]Loading activity…[/dim]")
        except Exception:  # noqa: BLE001
            pass

    def _schedule_initial_load(self) -> None:
        """Kick the cold-boot gather onto a worker thread."""
        if self._initial_load_running:
            return
        self._initial_load_running = True
        try:
            self.run_worker(
                self._gather_async_sync,
                thread=True,
                exclusive=True,
                group="activity_feed_initial_load",
            )
        except Exception:  # noqa: BLE001
            # Worker dispatch failure (unlikely outside teardown): fall
            # back to the synchronous gather so the pane still loads.
            self._initial_load_running = False
            self._refresh_sync()

    def _gather_async_sync(self) -> None:
        """Worker-thread body: gather entries, hand back to UI thread."""
        try:
            entries = self._gather()
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self._initial_load_failed, str(exc))
            return
        self.call_from_thread(self._initial_load_completed, list(entries))

    def _initial_load_completed(self, entries: list) -> None:
        self._initial_load_running = False
        self._initial_load_done = True
        self._entries = entries[: self.MAX_ROWS_IN_MEMORY]
        self._render()

    def _initial_load_failed(self, error: str) -> None:
        self._initial_load_running = False
        self._initial_load_done = True
        try:
            self.topbar.update(
                f"[{State.BLOCKED}]Error loading activity:[/{State.BLOCKED}] {_escape(error)}"
            )
        except Exception:  # noqa: BLE001
            pass

    def _refresh_sync(self) -> None:
        """Synchronous gather + render (fallback path used on worker
        dispatch failure and by tests that drive ``_refresh`` directly
        without spinning a pilot)."""
        try:
            entries = self._gather()
        except Exception as exc:  # noqa: BLE001
            self.topbar.update(
                f"[{State.BLOCKED}]Error loading activity:[/{State.BLOCKED}] {_escape(str(exc))}"
            )
            return
        self._entries = list(entries)[: self.MAX_ROWS_IN_MEMORY]
        self._render()

    def _refresh(self) -> None:
        # Cold-boot worker still in flight — don't double-dispatch.
        if self._initial_load_running:
            return
        # Once mounted, do refreshes off-thread too so a slow gather
        # doesn't freeze the UI on a manual ``R`` press or follow tick.
        if not self.is_running:
            # Not yet mounted under a pilot/app loop — fall back to
            # the synchronous path so __init__-time refreshes (tests)
            # still work.
            self._refresh_sync()
            return
        try:
            self.run_worker(
                self._refresh_async_sync,
                thread=True,
                exclusive=True,
                group="activity_feed_refresh",
            )
        except Exception:  # noqa: BLE001
            self._refresh_sync()

    def _refresh_async_sync(self) -> None:
        try:
            entries = self._gather()
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self._refresh_failed, str(exc))
            return
        self.call_from_thread(self._refresh_completed, list(entries))

    def _refresh_completed(self, entries: list) -> None:
        self._entries = entries[: self.MAX_ROWS_IN_MEMORY]
        self._render()

    def _refresh_failed(self, error: str) -> None:
        try:
            self.topbar.update(
                f"[{State.BLOCKED}]Error loading activity:[/{State.BLOCKED}] {_escape(error)}"
            )
        except Exception:  # noqa: BLE001
            pass

    def _follow_tick(self) -> None:
        # Don't stack a follow gather on top of an in-flight cold-boot
        # or manual refresh worker.
        if self._initial_load_running:
            return
        if not self.is_running:
            self._follow_tick_sync()
            return
        try:
            self.run_worker(
                self._follow_tick_async_sync,
                thread=True,
                exclusive=True,
                group="activity_feed_follow",
            )
        except Exception:  # noqa: BLE001
            self._follow_tick_sync()

    def _follow_tick_async_sync(self) -> None:
        try:
            fresh = self._gather()
        except Exception:  # noqa: BLE001
            return
        self.call_from_thread(self._follow_tick_completed, list(fresh))

    def _follow_tick_sync(self) -> None:
        try:
            fresh = self._gather()
        except Exception:  # noqa: BLE001
            return
        self._follow_tick_completed(list(fresh))

    def _follow_tick_completed(self, fresh: list) -> None:
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
        regex, fuzzy_terms = _parse_search_query(self._filter_fuzzy)
        has_search = regex is not None or bool(fuzzy_terms)
        explicit_noise_request = (
            self._show_system_noise
            or has_search
            or _is_noise_type_filter(kind)
        )
        if not (project or actor or kind or has_search):
            return [
                entry for entry in rows
                if explicit_noise_request or not _is_low_signal_activity(entry)
            ]
        out = []
        for entry in rows:
            if not explicit_noise_request and _is_low_signal_activity(entry):
                continue
            if project and (entry.project or "") != project:
                continue
            if actor and (entry.actor or "") != actor:
                continue
            if kind and (entry.kind or "") != kind:
                continue
            if has_search:
                hay = _entry_search_haystack(entry)
                if regex is not None:
                    if regex.search(hay) is None:
                        continue
                elif any(term not in hay for term in fuzzy_terms):
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
        visible_count = len(filtered_entries)
        total_count = len(self._entries)
        hidden_noise_count = 0
        if not self._show_system_noise and not self._filter_fuzzy:
            hidden_noise_count = sum(
                1 for entry in self._entries if _is_low_signal_activity(entry)
            )
        title_bits = [f"[b {State.HEADING_BRIGHT}]Activity[/b {State.HEADING_BRIGHT}]"]
        if self._filter_project:
            title_bits.append(
                f"[{State.INFO}]\u00b7 project: [b]{_escape(self._filter_project)}[/b][/{State.INFO}]"
            )
        self.topbar.update("  ".join(title_bits))

        chips: list[str] = [
            f"[b]{events_last_24h}[/b] [dim]event{'s' if events_last_24h != 1 else ''} in last 24h[/dim]",
            f"[b]{visible_count}[/b] [dim]match{'es' if visible_count != 1 else ''} of {total_count} loaded[/dim]",
        ]
        filter_description = self._describe_filters()
        if filter_description:
            chips.append(f"[{State.NEUTRAL}]filters: {filter_description}[/{State.NEUTRAL}]")
        if hidden_noise_count:
            chips.append(f"[dim]{hidden_noise_count} system noise hidden[/dim]")
        chips.append(
            f"[{State.WORKING}]follow on[/{State.WORKING}]" if self._follow_on else "[dim]follow off[/dim]"
        )
        self.counters.update("  \u00b7  ".join(chips))

        if self._open_entry_id is not None and all(
            entry.id != self._open_entry_id for entry in filtered_entries
        ):
            self._open_entry_id = None

        self._render_table(filtered_entries)
        if self._open_entry_id is not None:
            self._render_detail()
        else:
            self.detail.update("")
            self.detail.display = False

        if self._filter_mode is not None:
            mode_label = self._filter_mode or "filter"
            self.hint.update(
                f"[dim]{mode_label}: type to filter \u00b7 \u21b5 apply \u00b7 esc cancel[/dim]"
            )
        elif self.filter_input.has_focus:
            self.hint.update(
                "[dim]search filters live \u00b7 re:<pattern> or /pattern/ for regex "
                "\u00b7 \u21b5 table \u00b7 esc back[/dim]"
            )
        elif self._open_entry_id is not None:
            self.hint.update("[dim]\u21b5 close detail \u00b7 j/k next \u00b7 q back[/dim]")
        else:
            self.hint.update(self._DEFAULT_HINT)

    def _render_table(self, rows: list) -> None:
        self.table.clear()
        for entry in rows:
            time_text = Text(_format_activity_relative(entry.timestamp), style=State.NEUTRAL)
            project_label = entry.project or "\u2014"
            if self._filter_project and (entry.project or "") == self._filter_project:
                project_text = Text(project_label, style=f"bold {State.HEADING_BRIGHT}")
            elif entry.project:
                project_text = Text(project_label, style=State.INFO)
            else:
                project_text = Text(project_label, style=State.MUTED)
            verb_text = entry.verb or entry.kind or ""
            # When the summary literally equals the verb/kind (the
            # legacy ``_fallback_summary`` path picks the event's own
            # name when a row has no body), the Message column just
            # echoes the Event column \u2014 visual noise that buries any
            # row whose summary actually carries information. Blank
            # the Message cell so scanning the feed is a visual scan
            # of the Event column.
            summary_text = entry.summary or ""
            if summary_text and summary_text == verb_text:
                summary_text = ""
            # Strip ``[Action]`` / ``[Alert]`` routing tags from the
            # head of the message \u2014 they're notify/supervisor
            # routing labels, not natural-language content, and the
            # Event column already conveys the kind.
            from pollypm.notify_task import strip_routing_tag_prefix

            summary_text = strip_routing_tag_prefix(summary_text)
            self.table.add_row(
                time_text,
                project_text,
                Text(entry.actor or "system", style=State.BODY_BRIGHT),
                Text(verb_text, style=_activity_type_colour(entry.kind or "", entry.severity)),
                Text(_truncate_summary(summary_text), style=State.BODY_BRIGHT),
                key=entry.id,
            )

    def _render_detail(self) -> None:
        entry = self._entry_by_id(self._open_entry_id)
        if entry is None:
            self.detail.update("")
            self.detail.display = False
            return
        try:
            # #1363: shared renderer lives in the core protocol module so
            # this surface no longer reaches into ``plugins_builtin``.
            from pollypm.activity_feed_protocol import render_entry_detail

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
        if self._show_system_noise:
            bits.append("noise=shown")
        if self._filter_fuzzy:
            bits.append(f'search="{self._filter_fuzzy}"')
        return " \u00b7 ".join(bits)

    def _search_placeholder(self) -> str:
        return "Search task id / worker / type / text  (re:<pattern> for regex)"

    def _focus_search(self) -> None:
        self._filter_mode = None
        self.filter_input.placeholder = self._search_placeholder()
        self.filter_input.value = self._filter_fuzzy
        self.filter_input.focus()
        self.filter_input.cursor_position = len(self.filter_input.value)
        self._render()

    def _set_search_query(self, value: str) -> None:
        self._filter_fuzzy = value or ""
        self.filter_input.placeholder = self._search_placeholder()
        self._render()

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
        self._focus_search()

    def action_pick_project(self) -> None:
        keys = sorted({entry.project or "" for entry in self._entries if entry.project})
        ellipsis = " …" if len(keys) > 6 else ""
        hint = (
            f"project: {', '.join(keys[:6])}{ellipsis}"
            if keys
            else "project: (no projects in current window)"
        )
        self._open_filter("project", placeholder=hint)

    def action_pick_type(self) -> None:
        kinds = sorted({entry.kind or "" for entry in self._entries if entry.kind})
        ellipsis = " …" if len(kinds) > 6 else ""
        hint = (
            f"type: {', '.join(kinds[:6])}{ellipsis}"
            if kinds
            else "type: (no event types in current window)"
        )
        self._open_filter("type", placeholder=hint)

    def action_toggle_noise(self) -> None:
        self._show_system_noise = not self._show_system_noise
        self._render()

    def _open_filter(self, mode: str, *, placeholder: str) -> None:
        self._filter_mode = mode
        if mode == "project":
            self.filter_input.value = self._filter_project or ""
        elif mode == "type":
            self.filter_input.value = self._filter_type or ""
        self.filter_input.placeholder = placeholder
        self._render()
        self.filter_input.focus()
        self.filter_input.cursor_position = len(self.filter_input.value)

    def _close_filter(self) -> None:
        self._filter_mode = None
        self.filter_input.value = self._filter_fuzzy
        self.filter_input.placeholder = self._search_placeholder()
        self._render()
        self.table.focus()

    @on(Input.Changed, "#af-filter-input")
    def _on_filter_changed(self, event: Input.Changed) -> None:
        if self._filter_mode is not None:
            return
        self._set_search_query(event.value or "")

    @on(Input.Submitted, "#af-filter-input")
    def _on_filter_submit(self, event: Input.Submitted) -> None:
        value = (event.value or "").strip()
        if self._filter_mode == "project":
            self._filter_project = value or None
        elif self._filter_mode == "type":
            self._filter_type = value or None
        if self._filter_mode is None:
            self.table.focus()
            self._render()
            return
        self._close_filter()

    def action_clear_filters(self) -> None:
        self._filter_project = None
        self._filter_actor = None
        self._filter_type = None
        self._filter_fuzzy = ""
        self._filter_mode = None
        self.filter_input.value = ""
        self.filter_input.placeholder = self._search_placeholder()
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
        if self._filter_mode is not None:
            self._close_filter()
            return
        if self.filter_input.has_focus:
            self.table.focus()
            self._render()
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
