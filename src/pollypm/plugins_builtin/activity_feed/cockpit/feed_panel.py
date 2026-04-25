"""Cockpit widget + app for the Live Activity Feed (lf03).

Two surfaces:

* :class:`ActivityFeedPanel` — a Textual widget that renders a
  reverse-chronological feed of :class:`FeedEntry` records. Reused by
  lf04 (filter UI + detail view).
* :class:`ActivityFeedApp` — a Textual ``App`` that mounts the panel,
  polls the state-epoch counter, and re-renders when new events land.
  Launched by ``pm cockpit-pane activity``.

A plain-text renderer (:func:`render_activity_feed_text`) is exported
for the cockpit's ``_build_cockpit_detail_dispatch`` so environments
without Textual mounted (e.g. the ``PollyCockpitPaneApp`` fallback)
still show the feed.

The widget uses the state-epoch monotonic counter (see
:mod:`pollypm.state_epoch`) so reads are cheap — the expensive
projection only runs when something changed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from pollypm.plugins_builtin.activity_feed.handlers.event_projector import (
    EventProjector,
    FeedEntry,
)
from pollypm.plugins_builtin.activity_feed.plugin import build_projector

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Filter state (lf04) — composes with AND logic in ``apply_filter``.
# ---------------------------------------------------------------------------


_TIME_WINDOWS: dict[str, timedelta | None] = {
    "all": None,
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(days=7),
}


@dataclass(slots=True, frozen=True)
class FeedFilter:
    """User-set filter state for the feed panel.

    All fields are additive; ``apply_filter`` applies them with AND
    semantics. The default-constructed instance is a no-op filter
    (everything passes), so panels that don't expose filters behave
    exactly as they did in lf03.
    """

    projects: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ()
    actors: tuple[str, ...] = ()
    time_window: str = "all"

    def is_empty(self) -> bool:
        return (
            not self.projects
            and not self.kinds
            and not self.actors
            and self.time_window in ("", "all")
        )

    def since(self) -> timedelta | None:
        return _TIME_WINDOWS.get(self.time_window)

    def with_project(self, project: str | None) -> "FeedFilter":
        return replace(self, projects=(project,) if project else ())

    def with_kind(self, kind: str | None) -> "FeedFilter":
        return replace(self, kinds=(kind,) if kind else ())

    def with_actor(self, actor: str | None) -> "FeedFilter":
        return replace(self, actors=(actor,) if actor else ())

    def with_time_window(self, window: str) -> "FeedFilter":
        if window not in _TIME_WINDOWS:
            window = "all"
        return replace(self, time_window=window)

    def describe(self) -> str:
        """Short one-line summary for the panel header."""
        if self.is_empty():
            return "all activity"
        bits: list[str] = []
        if self.projects:
            bits.append("project=" + ",".join(self.projects))
        if self.kinds:
            bits.append("kind=" + ",".join(self.kinds))
        if self.actors:
            bits.append("actor=" + ",".join(self.actors))
        if self.time_window and self.time_window != "all":
            bits.append("window=" + self.time_window)
        return " | ".join(bits)


def apply_filter(
    entries: Iterable[FeedEntry], feed_filter: FeedFilter,
) -> list[FeedEntry]:
    """Apply a :class:`FeedFilter` to an entry iterable.

    Used both by the Textual panel (to keep its in-memory window
    coherent after a filter change) and by the tests. The projector
    itself accepts the same filters in ``project()`` for efficient
    server-side filtering — this helper exists so we can re-filter an
    already-fetched list without another DB round-trip.
    """
    project_set = set(feed_filter.projects) if feed_filter.projects else None
    kind_set = set(feed_filter.kinds) if feed_filter.kinds else None
    actor_set = set(feed_filter.actors) if feed_filter.actors else None
    cutoff: datetime | None = None
    delta = feed_filter.since()
    if delta is not None:
        cutoff = datetime.now(UTC) - delta

    out: list[FeedEntry] = []
    for entry in entries:
        if project_set is not None and entry.project not in project_set:
            continue
        if kind_set is not None and entry.kind not in kind_set:
            continue
        if actor_set is not None and entry.actor not in actor_set:
            continue
        if cutoff is not None:
            try:
                when = datetime.fromisoformat(entry.timestamp)
            except ValueError:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            if when < cutoff:
                continue
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Pure rendering helpers — testable without Textual.
# ---------------------------------------------------------------------------


_SEVERITY_COLOURS = {
    "critical": "red",
    "recommendation": "yellow",
    "routine": "white",
}


def format_relative_time(
    timestamp: str, *, now: datetime | None = None,
) -> str:
    """Return a relative-time label (``"3m ago"``, ``"2h ago"``).

    ``timestamp`` is an ISO-8601 string (the projector always produces
    these). Unparseable values fall back to the raw string. ``now`` is
    injectable for deterministic tests.
    """
    if not timestamp:
        return "—"
    try:
        when = datetime.fromisoformat(timestamp)
    except ValueError:
        return timestamp
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    anchor = now or datetime.now(UTC)
    delta = anchor - when
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def format_entry_row(entry: FeedEntry, *, now: datetime | None = None) -> str:
    """Render one FeedEntry as a single plain-text row.

    Layout:: ``[rel] [project] [actor] verb summary``. Severity is not
    encoded here — the Textual renderer applies colour; the plain-text
    renderer prefixes a ``!`` on critical entries so `pm activity` can
    stay unstyled but still draw attention.
    """
    rel = format_relative_time(entry.timestamp, now=now)
    project = entry.project or "-"
    actor = entry.actor or "system"
    verb = entry.verb or entry.kind
    prefix = "!" if entry.severity == "critical" else " "
    pin = "📌 " if entry.pinned else ""
    return f"{prefix} {rel:>8}  [{project}]  [{actor}]  {verb}: {pin}{entry.summary}"


def render_entries_as_text(entries: Iterable[FeedEntry]) -> str:
    """Render a list of FeedEntry rows as multi-line plain text.

    Empty input renders a friendly placeholder so the cockpit panel
    doesn't look broken on a brand-new install.
    """
    rows = [format_entry_row(e) for e in entries]
    if not rows:
        return (
            "No activity yet.\n\n"
            "Events accumulate as sessions start, tasks transition, "
            "and heartbeats fire. Check back after the next sweep."
        )
    return "\n".join(rows)


def render_entry_detail(entry: FeedEntry, *, now: datetime | None = None) -> str:
    """Render the per-entry detail view as plain text (lf04).

    Shows absolute + relative timestamps, project / actor / verb,
    severity, a pretty-printed payload JSON, and suggested follow-up
    navigation (task / session links) inferred from the payload.
    """
    lines: list[str] = []
    abs_ts = entry.timestamp
    rel_ts = format_relative_time(entry.timestamp, now=now)
    lines.append(f"Activity entry · {entry.id}")
    lines.append("")
    lines.append(f"When: {abs_ts}  ({rel_ts})")
    lines.append(f"Kind: {entry.kind}")
    if entry.project:
        lines.append(f"Project: {entry.project}")
    lines.append(f"Actor: {entry.actor}")
    if entry.subject:
        lines.append(f"Subject: {entry.subject}")
    lines.append(f"Verb: {entry.verb}")
    lines.append(f"Severity: {entry.severity}")
    if entry.pinned:
        lines.append("Pinned: yes")
    lines.append("")
    lines.append("Summary:")
    lines.append(f"  {entry.summary}")
    # Navigation hints — task / session links inferred from the payload.
    task_project = entry.payload.get("task_project")
    task_number = entry.payload.get("task_number")
    if task_project and task_number is not None:
        lines.append("")
        lines.append(
            f"Related task: project:{task_project}:task:{task_number}  "
            f"(use the rail to open)"
        )
    elif entry.source == "work_transitions" and entry.subject:
        lines.append("")
        lines.append(f"Related task: {entry.subject}")
    if entry.actor and entry.source == "events":
        lines.append("")
        lines.append(f"Related session: {entry.actor}")
    # Payload dump (pretty-printed).
    lines.append("")
    lines.append("Payload:")
    try:
        rendered = json.dumps(entry.payload, indent=2, sort_keys=True, default=str)
    except (TypeError, ValueError):
        rendered = repr(entry.payload)
    for line in rendered.splitlines():
        lines.append(f"  {line}")
    return "\n".join(lines)


def render_activity_feed_text(config: Any, *, limit: int = 50) -> str:
    """Render the feed as plain text for the cockpit's static pane path.

    Picks up the projector from :func:`build_projector` — missing config
    or missing state DB yields the same friendly placeholder as an
    empty feed.
    """
    header = "Activity Feed"
    projector = build_projector(config)
    if projector is None:
        return f"{header}\n\nNo state store configured — nothing to show yet."
    try:
        entries = projector.project(limit=limit)
    except Exception:  # noqa: BLE001
        logger.exception("activity_feed: projection failed for text render")
        return f"{header}\n\nFailed to read activity events."
    return f"{header}\n\n{render_entries_as_text(entries)}"


# ---------------------------------------------------------------------------
# Textual widget + App — imported lazily so the plugin loads in
# environments without Textual (unit tests, headless CI).
# ---------------------------------------------------------------------------


def _severity_style(severity: str) -> str:
    colour = _SEVERITY_COLOURS.get(severity, "white")
    if severity == "critical":
        return f"bold {colour}"
    return colour


class _PanelState:
    """Tracks state between renders so new entries can be highlighted.

    Kept as a plain object (not a dataclass) so the widget can live-
    patch ``last_seen_id`` without freezing semantics.
    """

    __slots__ = ("last_seen_id", "last_epoch", "entries")

    def __init__(self) -> None:
        self.last_seen_id: int | None = None
        self.last_epoch: float = 0.0
        self.entries: list[FeedEntry] = []

    def update_with(self, entries: list[FeedEntry]) -> list[FeedEntry]:
        """Merge a fresh projection in at the top; return the ids of
        rows that are new since the last render (used to highlight).
        """
        existing_ids = {e.id for e in self.entries}
        new_rows = [e for e in entries if e.id not in existing_ids]
        # Keep the window bounded so the widget stays cheap. The spec
        # paginates at 50; we keep 100 so scrolling a bit doesn't drop
        # rows that just arrived.
        merged = (new_rows + self.entries)[:100]
        self.entries = merged
        if entries:
            # Track the numeric id so badge-provider math can detect
            # "new since last view" without re-querying.
            numeric_ids: list[int] = []
            for e in entries:
                if e.id.startswith("evt:"):
                    try:
                        numeric_ids.append(int(e.id.split(":", 1)[1]))
                    except ValueError:
                        continue
            if numeric_ids:
                self.last_seen_id = max(numeric_ids + ([self.last_seen_id] if self.last_seen_id else []))
        return new_rows


def _try_import_textual():  # pragma: no cover - import guard
    try:
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.widget import Widget
        from textual.widgets import Static

        return App, ComposeResult, Binding, Widget, Static
    except Exception as exc:
        raise RuntimeError(
            "Textual is not available — ActivityFeedPanel / ActivityFeedApp "
            "require the cockpit's textual dependency. "
            f"Underlying error: {exc}"
        )


def _build_widget_classes():
    """Construct the Textual widget + app lazily.

    Keeps plugin import cheap; only materialises Textual dependencies
    when someone actually launches the cockpit pane.
    """
    App, ComposeResult, Binding, Widget, Static = _try_import_textual()

    class ActivityFeedPanel(Widget):
        """Reverse-chronological feed of FeedEntry rows.

        Subclasses ``Static`` indirectly through ``Widget`` composition
        so the render path stays simple. ``render()`` returns Rich-style
        markup; the host app triggers ``refresh()`` on new data.
        """

        DEFAULT_CSS = """
        ActivityFeedPanel {
            padding: 1 2;
            background: #10161b;
            color: #eef2f4;
        }
        """

        def __init__(self, projector: EventProjector | None = None, *, limit: int = 50):
            super().__init__()
            self._projector = projector
            self._limit = limit
            self._state = _PanelState()
            self._header = Static("", id="feed-header")
            self._body = Static("", id="feed-body")
            self._last_rendered_ids: set[str] = set()
            # lf04 state — filter + detail-view flag.
            self.feed_filter: FeedFilter = FeedFilter()
            self._detail_entry: FeedEntry | None = None
            self._selected_index: int = 0

        def compose(self) -> ComposeResult:  # pragma: no cover - Textual harness
            yield self._header
            yield self._body

        def refresh_feed(self) -> list[FeedEntry]:
            """Fetch the latest projection and update the body.

            Returns the list of newly-arrived entries (for caller-side
            badge math / notifications). Applies the current filter
            (lf04) both server-side (via the projector's filter args)
            and client-side (so the in-memory window stays coherent).
            """
            if self._projector is None:
                self._body.update(
                    "No projector wired — activity feed is inert.\n"
                    "Ensure `activity_feed` is loaded and the state DB exists."
                )
                return []
            filt = self.feed_filter
            try:
                fresh = self._projector.project(
                    limit=self._limit,
                    projects=filt.projects or None,
                    kinds=filt.kinds or None,
                    actors=filt.actors or None,
                    since=filt.since(),
                )
            except Exception:  # noqa: BLE001  # pragma: no cover
                logger.exception("activity_feed: widget refresh failed")
                return []
            newly = self._state.update_with(fresh)
            # If detail view is active, keep it pinned; the list updates
            # but the body shows the detail markup until the user dismisses.
            if self._detail_entry is not None:
                refreshed = next(
                    (e for e in fresh if e.id == self._detail_entry.id), None,
                )
                if refreshed is not None:
                    self._detail_entry = refreshed
                self._body.update(self._render_detail_markup(self._detail_entry))
            else:
                self._body.update(self._render_markup(self._state.entries, newly))
            self._header.update(self._render_header())
            self._last_rendered_ids = {e.id for e in self._state.entries}
            return newly

        # ---- filter API ----------------------------------------------

        def set_filter(self, feed_filter: FeedFilter) -> None:
            """Replace the filter and refresh the feed."""
            self.feed_filter = feed_filter
            # Drop the cached window so rows that were filtered out
            # don't linger until they age off.
            self._state.entries = []
            self.refresh_feed()

        def clear_filter(self) -> None:
            self.set_filter(FeedFilter())

        # ---- detail-view API -----------------------------------------

        def open_detail(self, entry: FeedEntry) -> None:
            self._detail_entry = entry
            self._body.update(self._render_detail_markup(entry))
            self._header.update(self._render_header())

        def close_detail(self) -> None:
            self._detail_entry = None
            self.refresh_feed()

        @property
        def detail_entry(self) -> FeedEntry | None:
            return self._detail_entry

        def entry_by_index(self, index: int) -> FeedEntry | None:
            if 0 <= index < len(self._state.entries):
                return self._state.entries[index]
            return None

        # ---- rendering ------------------------------------------------

        def _render_header(self) -> str:
            filter_desc = self.feed_filter.describe()
            suffix = "  (detail)" if self._detail_entry is not None else ""
            return (
                f"[bold]Activity Feed[/] \u00b7 "
                f"[cyan]{_rich_escape(filter_desc)}[/]"
                f"{suffix}"
            )

        def _render_markup(
            self, entries: list[FeedEntry], new_ids: list[FeedEntry],
        ) -> str:
            new_id_set = {e.id for e in new_ids}
            now = datetime.now(UTC)
            lines: list[str] = []
            if not entries:
                return (
                    "[dim italic]No activity yet.[/]\n\n"
                    "[dim]Events accumulate as sessions start, tasks "
                    "transition, and heartbeats fire.[/]"
                )
            for entry in entries:
                rel = format_relative_time(entry.timestamp, now=now)
                style = _severity_style(entry.severity)
                project = entry.project or "-"
                actor = entry.actor or "system"
                verb = entry.verb or entry.kind
                marker = "[bold green]\u25cf[/] " if entry.id in new_id_set else "  "
                # Escape Rich-unsafe brackets in user content by passing
                # through the tag-safe escaping helper.
                summary = _rich_escape(entry.summary)
                verb_s = _rich_escape(verb)
                proj_s = _rich_escape(project)
                actor_s = _rich_escape(actor)
                lines.append(
                    f"{marker}[dim]{rel:>8}[/]  "
                    f"[cyan][{proj_s}][/]  [magenta][{actor_s}][/]  "
                    f"[{style}]{verb_s}[/]: {summary}"
                )
            return "\n".join(lines)

        def _render_detail_markup(self, entry: FeedEntry) -> str:
            """Rich-markup variant of ``render_entry_detail`` for the
            Textual panel."""
            text = render_entry_detail(entry)
            # Escape brackets so Rich doesn't try to parse JSON braces.
            return "[dim]" + _rich_escape(text) + "[/]"

    class ActivityFeedApp(App):
        TITLE = "PollyPM"
        SUB_TITLE = "Activity"
        CSS = """
        Screen { background: #10161b; color: #eef2f4; padding: 1; }
        #feed-header { padding: 0 2 1 2; color: #98a7b5; }
        #feed-body { padding: 1 2; }
        """
        BINDINGS = [
            Binding("q", "quit", "Quit"),
            Binding("r", "refresh", "Refresh"),
            # lf04 — cycle the time-window filter and clear filters.
            Binding("t", "cycle_time_window", "Time window"),
            Binding("c", "clear_filter", "Clear filter"),
            Binding("enter", "open_detail", "Detail"),
            Binding("escape", "close_detail", "Back"),
            Binding("down", "cursor_down", "Down"),
            Binding("up", "cursor_up", "Up"),
            Binding("j", "cursor_down", "Down"),
            Binding("k", "cursor_up", "Up"),
        ]

        def __init__(self, config_path: Path, *, limit: int = 50) -> None:
            super().__init__()
            self.config_path = config_path
            self._panel: ActivityFeedPanel | None = None
            self._limit = limit
            self._last_epoch = 0.0

        def compose(self) -> ComposeResult:  # pragma: no cover - Textual harness
            projector = self._load_projector()
            self._panel = ActivityFeedPanel(projector, limit=self._limit)
            yield self._panel

        def _load_projector(self) -> EventProjector | None:
            try:
                from pollypm.config import load_config

                config = load_config(self.config_path)
                return build_projector(config)
            except Exception:  # noqa: BLE001  # pragma: no cover
                logger.exception("activity_feed: could not load projector")
                return None

        def on_mount(self) -> None:  # pragma: no cover - Textual harness
            if self._panel is not None:
                self._panel.refresh_feed()
            # 2-second tick keeps live updates responsive without
            # hammering the DB (the projector only does real work when
            # the state-epoch has moved).
            self.set_interval(2.0, self._tick)

        def _tick(self) -> None:  # pragma: no cover - Textual harness
            try:
                from pollypm.state_epoch import mtime
            except Exception:  # noqa: BLE001
                mtime = lambda: 0.0  # type: ignore[assignment]
            current = mtime()
            if current and current == self._last_epoch:
                return
            self._last_epoch = current
            if self._panel is not None:
                self._panel.refresh_feed()

        def action_refresh(self) -> None:  # pragma: no cover - Textual harness
            if self._panel is not None:
                self._panel.refresh_feed()

        def action_cycle_time_window(self) -> None:  # pragma: no cover
            if self._panel is None:
                return
            order = ("all", "hour", "day", "week")
            current = self._panel.feed_filter.time_window
            try:
                nxt = order[(order.index(current) + 1) % len(order)]
            except ValueError:
                nxt = "hour"
            self._panel.set_filter(self._panel.feed_filter.with_time_window(nxt))

        def action_clear_filter(self) -> None:  # pragma: no cover
            if self._panel is not None:
                self._panel.clear_filter()

        def action_open_detail(self) -> None:  # pragma: no cover
            if self._panel is None:
                return
            entry = self._panel.entry_by_index(self._panel._selected_index)
            if entry is not None:
                self._panel.open_detail(entry)

        def action_close_detail(self) -> None:  # pragma: no cover
            if self._panel is not None:
                self._panel.close_detail()

        def action_cursor_down(self) -> None:  # pragma: no cover
            if self._panel is None:
                return
            total = len(self._panel._state.entries)
            if total == 0:
                return
            self._panel._selected_index = min(
                total - 1, self._panel._selected_index + 1,
            )

        def action_cursor_up(self) -> None:  # pragma: no cover
            if self._panel is None:
                return
            self._panel._selected_index = max(0, self._panel._selected_index - 1)

    return ActivityFeedPanel, ActivityFeedApp


def _rich_escape(text: str) -> str:
    """Escape ``[`` / ``]`` in user content so Rich markup doesn't mis-
    parse summaries that contain tags-looking substrings.
    """
    if not text:
        return ""
    return text.replace("[", "\\[")


def __getattr__(name: str):
    """Lazy-load the Textual classes so the module imports cheaply.

    Tests that don't touch the UI (and CLI paths that render plain
    text) never pay the Textual import cost. The widget / app classes
    are built on first access and cached on the module.
    """
    if name in {"ActivityFeedPanel", "ActivityFeedApp"}:
        panel_cls, app_cls = _build_widget_classes()
        globals()["ActivityFeedPanel"] = panel_cls
        globals()["ActivityFeedApp"] = app_cls
        return globals()[name]
    raise AttributeError(name)


# ---------------------------------------------------------------------------
# Badge + rail helpers — consumed by plugin.py at rail-registration time.
# ---------------------------------------------------------------------------


_LOW_SIGNAL_BADGE_KINDS = frozenset({"heartbeat", "token_ledger"})


def _counts_for_activity_badge(entry: FeedEntry) -> bool:
    kind = (getattr(entry, "kind", "") or "").lower()
    verb = (getattr(entry, "verb", "") or "").lower()
    return kind not in _LOW_SIGNAL_BADGE_KINDS and verb not in _LOW_SIGNAL_BADGE_KINDS


def new_event_count(
    projector: EventProjector | None, last_seen_id: int | None,
) -> int:
    """How many events landed since ``last_seen_id`` was recorded.

    Returned as the badge value for the rail entry. ``None`` means "no
    cursor" — counts all events below the limit (capped so the badge
    doesn't read ``999+`` for a fresh install).
    """
    if projector is None:
        return 0
    try:
        entries = projector.project(since_id=last_seen_id, limit=50)
    except Exception:  # noqa: BLE001
        logger.exception("activity_feed: badge probe failed")
        return 0
    return sum(1 for entry in entries if _counts_for_activity_badge(entry))


__all__ = [
    "ActivityFeedApp",  # noqa: F822 — exported lazily via __getattr__
    "ActivityFeedPanel",  # noqa: F822 — exported lazily via __getattr__
    "FeedFilter",
    "apply_filter",
    "format_entry_row",
    "format_relative_time",
    "new_event_count",
    "render_activity_feed_text",
    "render_entries_as_text",
    "render_entry_detail",
]
