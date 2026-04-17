from __future__ import annotations

import gc
import resource
from pathlib import Path
import subprocess

# Raise FD limit early — the cockpit opens many subprocesses and file handles.
try:
    _soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if _soft < 4096:
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(4096, _hard), _hard))
except (ValueError, OSError):
    pass

from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Input, ListItem, ListView, Static

from pollypm.models import ProviderKind
from pollypm.tz import format_time as _fmt_time
from pollypm.config import load_config
from pollypm.service_api import PollyPMService
from pollypm.cockpit import CockpitItem, CockpitRouter, build_cockpit_detail


import re as _re


def _md_to_rich(text: str) -> str:
    """Convert common markdown to Rich markup for Textual Static widgets."""
    lines: list[str] = []
    in_code = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            lines.append("[dim]" + line + "[/dim]" if not in_code else "[dim]" + line + "[/dim]")
            continue
        if in_code:
            lines.append(f"[dim]{line}[/dim]")
            continue
        # Headers
        if line.startswith("### "):
            lines.append(f"[b]{line[4:]}[/b]")
        elif line.startswith("## "):
            lines.append(f"\n[b]{line[3:]}[/b]")
        elif line.startswith("# "):
            lines.append(f"\n[b u]{line[2:]}[/b u]")
        else:
            # Inline formatting first (applies to all non-code lines)
            line = _re.sub(r"\*\*(.+?)\*\*", r"[b]\1[/b]", line)
            line = _re.sub(r"\*(.+?)\*", r"[i]\1[/i]", line)
            line = _re.sub(r"`(.+?)`", r"[dim]\1[/dim]", line)
            # Bullet points
            if line.strip().startswith("- "):
                indent = len(line) - len(line.lstrip())
                content = line.strip()[2:]
                lines.append(f"{'  ' * (indent // 2)}  • {content}")
            elif _re.match(r"\s*\d+\.\s", line):
                lines.append(f"  {line.strip()}")
            else:
                lines.append(line)
    return "\n".join(lines)


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
        if (item.state.endswith("live") or item.state.endswith("working")) and item.key in ("polly", "russell"):
            self.add_class("live")
        if active_view:
            self.add_class("active-view")
        self.update_body()

    def update_body(self) -> None:
        text = Text()
        if self.has_class("active-view"):
            text.append("\u258c ", style="#5b8aff")
        else:
            text.append("  ")
        indicator, indicator_style = self._indicator()
        if indicator:
            text.append(f"{indicator} ", style=indicator_style)
        else:
            text.append("  ")
        label = self.item.label
        max_label = 22  # 30 col pane - 2 prefix - 2 indicator - 2 padding
        if len(label) > max_label:
            label = label[: max_label - 1] + "\u2026"
        text.append(label)
        # Show alert reason as dim subtitle for items with alerts
        if self.item.state.startswith("!"):
            reason = self.item.state[2:].strip()  # strip "! " prefix
            if reason:
                text.append(f"\n    {reason[:18]}", style="#ff5f6d dim")
        self.body.update(text)

    def _indicator(self) -> tuple[str, str]:
        # Alerts (red triangle)
        if self.item.state.startswith("!"):
            return "\u25b2", "#ff5f6d"
        # Separator
        if self.item.state == "separator":
            return "", "#4a5568"
        # Top-level agents (Polly, Russell)
        if self.item.key in ("polly", "russell"):
            if self.item.state.endswith("working"):
                return self.item.state.split(" ", 1)[0], "#3ddc84"  # green spinner
            if self.item.state in {"ready", "idle"}:
                return "\u2022", "#5b8aff"  # blue dot
            return "\u2022", "#5b8aff"
        # Inbox
        if self.item.key == "inbox":
            label = self.item.label
            if "(" in label and not label.endswith("(0)"):
                return "\u25c6", "#f0c45a"  # yellow diamond
            return "\u25c7", "#4a5568"
        # Settings
        if self.item.key == "settings":
            return "\u2699", "#6b7a88"
        # Sub-items
        if self.item.state == "sub":
            return " ", "#4a5568"
        # Unread
        if self.item.state == "unread":
            return "\u25cf", "#f0a030"  # orange dot
        # Projects: yellow for active task, dim for idle
        if self.item.key.startswith("project:"):
            if "working" in self.item.state:
                return "\u25c6", "#f0c45a"  # yellow diamond — active task
            return "\u25cb", "#4a5568"  # dim circle — idle
        return "\u25cb", "#4a5568"


class PollyCockpitApp(App[None]):
    TITLE = "PollyPM"
    SUB_TITLE = "Cockpit"
    SCHEDULER_POLL_INTERVAL_SECONDS = 5
    CSS = """
    Screen {
        background: #0f1317;
        color: #eef2f4;
        padding: 0;
        border-right: solid #1e2730;
    }
    #brand {
        padding: 1 0 0 0;
        margin-bottom: 0;
        text-align: center;
        color: #f5f7fa;
    }
    #tagline {
        color: #97a6b2;
        padding: 0 0 1 0;
        height: 4;
        text-align: center;
    }
    #nav {
        height: 1fr;
        background: transparent;
        border: none;
        scrollbar-size: 1 1;
        scrollbar-color: #2a3340;
        scrollbar-color-hover: #3a4a5a;
        scrollbar-color-active: #4a5a6a;
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
        margin-top: 0;
    }
    #nav > .section-sep {
        height: 1;
        padding: 0 1;
        color: #4a5568;
        background: transparent;
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
        background: #1a3a5c;
        color: #eef6ff;
        text-style: bold;
    }
    #nav > .rail-row.active-view.-highlight,
    #nav:focus > .rail-row.active-view.-highlight {
        background: #1f4d7a;
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
        background: #1a3a5c;
        color: #eef6ff;
        text-style: bold;
    }
    #settings-row.-hover {
        background: #253140;
        color: #f2f6f8;
    }
    #hint {
        height: 3;
        color: #3e4c5a;
        padding: 1 0 0 0;
    }
    """

    BINDINGS = [
        Binding("enter,o", "open_selected", "Open"),
        Binding("n", "new_worker", "New Worker"),
        Binding("r", "refresh", "Refresh"),
        Binding("s", "open_settings", "Settings"),
        Binding("j,down", "cursor_down", "Down", show=False),
        Binding("k,up", "cursor_up", "Up", show=False),
        Binding("g,home", "cursor_first", "First", show=False),
        Binding("G,end", "cursor_last", "Last", show=False),
        Binding("ctrl+q", "request_quit", "Quit", priority=True),
        Binding("ctrl+w", "detach", "Detach", priority=True),
    ]

    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self.config_path = config_path
        self.router = CockpitRouter(config_path)
        self.service = PollyPMService(config_path)
        _lines = ASCII_POLLY.split("\n")
        self.brand = Static(
            f"[#5b8aff]{_lines[0]}[/]\n[#3d6bcc]{_lines[1]}[/]",
            id="brand",
            markup=True,
        )
        self.tagline = Static("\n" + POLLY_SLOGANS[0], id="tagline")
        self.nav = ListView(id="nav")
        self.settings_row = Static("\u2699 Settings", id="settings-row")
        self.hint = Static("", id="hint")
        self.spinner_index = 0
        self.slogan_index = 0
        self._slogan_tick = 0
        self.selected_key = "polly"
        self._items: list[CockpitItem] = []
        self._row_widgets: dict[str, RailItem] = {}
        self._section_sep: ListItem | None = None
        self._suspend_selection_events = False
        self._scheduler_tick_running = False
        self._working_keys: set[str] = set()
        self._unread_keys: set[str] = set()
        self._tick_count = 0
        self._last_nav_change = -10  # last tick when user navigated
        self._last_epoch_mtime = 0.0  # state epoch mtime for change detection

    def compose(self) -> ComposeResult:
        with Vertical():
            yield self.brand
            yield self.tagline
            yield self.nav
            yield self.settings_row
            yield self.hint

    def on_mount(self) -> None:
        self.selected_key = self.router.selected_key()
        self._refresh_rows()
        self.set_interval(0.8, self._tick)
        self.set_interval(self.SCHEDULER_POLL_INTERVAL_SECONDS, self._tick_scheduler)
        self.nav.focus()
        # Textual's first render after mount reflows panes and can stretch the
        # rail past the persisted width. Re-enforce the rail width on a short
        # one-shot timer (no split — pure resize, SIGWINCH-safe), instead of
        # waiting ~30s for the periodic check to fix it. See issue #102.
        self.set_timer(0.4, self._enforce_rail_width_once)
        self.set_timer(1.5, self._enforce_rail_width_once)
        # Boot the HeartbeatRail so recurring roster handlers
        # (task_assignment.sweep, transcript.ingest, work.progress_sweep,
        # etc.) actually fire while the cockpit is open. The cockpit is
        # the only long-lived Python process in production; short-lived
        # CLIs like `pm up` exit before the rail can do useful work.
        # Failures here are non-fatal — the TUI still works, just
        # without autonomous sweeps. See issue #268 Gap A.
        self._start_core_rail()

    def _start_core_rail(self) -> None:
        """Start the process-wide HeartbeatRail via the supervisor, best-effort."""
        try:
            supervisor = self.router._load_supervisor()
        except Exception:  # noqa: BLE001
            return
        rail = getattr(supervisor, "core_rail", None)
        if rail is None:
            return
        try:
            rail.start()
        except Exception:  # noqa: BLE001
            # Already logged by CoreRail; swallow so the TUI still mounts.
            pass

    def _enforce_rail_width_once(self) -> None:
        try:
            self._enforce_rail_width()
        except Exception:  # noqa: BLE001
            pass

    def _focus_right_pane(self) -> None:
        focus_method = getattr(self.router, "focus_right_pane", None)
        if callable(focus_method):
            focus_method()

    # Layout check (pane recovery, rail width) — only every ~30s
    _LAYOUT_CHECK_INTERVAL = 38  # ~30s at 0.8s/tick
    # Force GC every ~2 minutes
    _GC_INTERVAL = 150

    def _tick(self) -> None:
        self._tick_count += 1
        self.spinner_index = (self.spinner_index + 1) % 4
        self._slogan_tick += 1
        if self._slogan_tick >= 75:
            self._slogan_tick = 0
            self.slogan_index = (self.slogan_index + 1) % len(POLLY_SLOGANS)
            self.tagline.update("\n" + POLLY_SLOGANS[self.slogan_index])
        # Periodic GC
        if self._tick_count % self._GC_INTERVAL == 0:
            gc.collect()
        # Check if state changed (one stat() call — no subprocess, no FD leak)
        from pollypm.state_epoch import mtime as epoch_mtime
        current_epoch = epoch_mtime()
        state_changed = current_epoch != self._last_epoch_mtime
        if state_changed:
            self._last_epoch_mtime = current_epoch
            try:
                self._refresh_rows()
            except Exception:  # noqa: BLE001
                pass
        else:
            # No state change — cheap spinner-only update
            spinners = ["\u25dc", "\u25dd", "\u25de", "\u25df"]
            frame = spinners[self.spinner_index % 4]
            for item in self._items:
                if item.state.endswith("working"):
                    item.state = f"{frame} working"
            for key, row in self._row_widgets.items():
                if row.item.state.endswith("working"):
                    row.item.state = f"{frame} working"
                    row.update_body()
        # Layout check much less frequently
        if self._tick_count % self._LAYOUT_CHECK_INTERVAL == 0:
            try:
                self._enforce_rail_width()
            except Exception:  # noqa: BLE001
                pass

    def _tick_scheduler(self) -> None:
        # The heartbeat and knowledge extraction now run via cron
        # (pm heartbeat install), not from the cockpit event loop.
        # Running them here caused UI freezes during long sweeps and
        # duplicate jobs on cockpit restarts.
        pass

    def _enforce_rail_width(self) -> None:
        """Recover missing panes and fix rail width if it drifted."""
        try:
            supervisor = self.router._load_supervisor()
            target = f"{supervisor.config.project.tmux_session}:{self.router._COCKPIT_WINDOW}"
            panes = self.router.tmux.list_panes(target)
            if len(panes) < 2:
                self.router.ensure_cockpit_layout()
            elif len(panes) >= 2:
                left_pane = min(panes, key=lambda p: p.pane_left)
                expected = self.router.rail_width()
                if left_pane.pane_width != expected:
                    self.router._try_resize_rail(left_pane.pane_id)
        except Exception:  # noqa: BLE001
            pass

    def _nav_items(self) -> list[CockpitItem]:
        return [item for item in self._items if item.key != "settings"]

    def _refresh_rows(self) -> None:
        try:
            self._items = self.router.build_items(spinner_index=self.spinner_index)
        except Exception:  # noqa: BLE001
            return  # keep previous items rather than crashing the rail
        # Track working→idle transitions for unread indicators
        new_working: set[str] = set()
        for item in self._items:
            if item.state.endswith("working"):
                new_working.add(item.key)
        for key in self._working_keys - new_working:
            # Session stopped working — mark unread if not currently viewing it
            if key != self.selected_key:
                self._unread_keys.add(key)
        self._working_keys = new_working
        # Apply unread state to items
        for item in self._items:
            if item.key in self._unread_keys and not item.state.endswith("working"):
                item.state = "unread"
        nav_items = self._nav_items()
        previous_key = self._selected_row_key()
        selected_key = None if self.selected_key == "settings" else (previous_key or self.selected_key)
        keys = [item.key for item in nav_items]
        rebuild = keys != list(self._row_widgets)
        if rebuild:
            self._row_widgets = {}
            self._section_sep: ListItem | None = None
        first_project_seen = False
        rows: list[ListItem] = []
        nav_index = 0
        restore_index: int | None = 0 if selected_key is not None else None
        for item in nav_items:
            first_project = False
            if item.key.startswith("project:") and not first_project_seen:
                first_project = True
                first_project_seen = True
                if rebuild:
                    self._section_sep = ListItem(
                        Static("  \u2500\u2500 projects \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"),
                        classes="section-sep",
                        disabled=True,
                    )
                if self._section_sep is not None:
                    rows.append(self._section_sep)
                    nav_index += 1
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
                restore_index = nav_index
            nav_index += 1
        if rebuild:
            self.nav.clear()
            self.nav.extend(rows)
        # Don't override the cursor position if the user recently
        # navigated with j/k — wait a few ticks for the dust to settle.
        recently_navigated = (self._tick_count - self._last_nav_change) < 5
        if rows and not recently_navigated:
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
        if index is None or index < 0:
            return None
        children = list(self.nav.children)
        if index >= len(children):
            return None
        child = children[index]
        if isinstance(child, RailItem):
            return child.cockpit_key
        return None

    _HEARTBEAT_STALE_SECONDS = 180  # warn if no heartbeat in 3 minutes

    def _update_hint(self) -> None:
        hint_text = "j/k move \u00b7 \u21b5 open \u00b7 n new"
        try:
            supervisor = self.router._load_supervisor()
            last_hb = supervisor.store.last_heartbeat_at()
            if last_hb:
                from datetime import UTC, datetime
                elapsed = (datetime.now(UTC) - datetime.fromisoformat(last_hb)).total_seconds()
                if elapsed > self._HEARTBEAT_STALE_SECONDS:
                    mins = int(elapsed // 60)
                    hint_text = f"\u26a0 Heartbeat offline ({mins}m) \u2014 run `pm heartbeat install`"
        except Exception:  # noqa: BLE001
            pass
        self.hint.update(hint_text)

    def _focus_right_if_live(self) -> None:
        """Focus the right pane only if it shows a live agent session."""
        state = self.router._load_state()
        if state.get("mounted_session"):
            self._focus_right_pane()

    def _sync_selected_from_nav(self) -> None:
        """Update selected_key from the current ListView cursor position."""
        key = self._selected_row_key()
        if key is not None:
            self.selected_key = key
            self._last_nav_change = self._tick_count

    def action_cursor_down(self) -> None:
        if self.nav.index is None:
            self.nav.index = 0
        else:
            self.nav.action_cursor_down()
        self._sync_selected_from_nav()

    def action_cursor_up(self) -> None:
        if self.nav.index is None:
            self.nav.index = 0
        else:
            self.nav.action_cursor_up()
        self._sync_selected_from_nav()

    def action_cursor_first(self) -> None:
        self.nav.index = 0
        self._sync_selected_from_nav()

    def action_cursor_last(self) -> None:
        children = list(self.nav.children)
        if children:
            self.nav.index = len(children) - 1
        self._sync_selected_from_nav()

    def action_open_selected(self) -> None:
        key = self._selected_row_key()
        if key is None:
            return
        self.selected_key = key
        try:
            self.router.route_selected(key)
            self.selected_key = self.router.selected_key()
            self._focus_right_if_live()
        except Exception as exc:  # noqa: BLE001
            self.hint.update(f"Error: {exc}"[:60])
        self._refresh_rows()

    def action_open_settings(self) -> None:
        self.selected_key = "settings"
        try:
            self.router.route_selected("settings")
        except Exception as exc:  # noqa: BLE001
            self.hint.update(f"Error: {exc}"[:60])
        self._refresh_rows()

    def action_new_worker(self) -> None:
        key = self._selected_row_key()
        if key is None or not key.startswith("project:"):
            return
        project_key = key.split(":", 1)[1]
        self.hint.update(f"Launching worker for {project_key}...")
        self.run_worker(
            lambda: self._launch_worker_sync(project_key, key),
            thread=True,
            exclusive=True,
            group="new_worker",
        )

    def _launch_worker_sync(self, project_key: str, key: str) -> None:
        def _on_status(msg: str) -> None:
            self.call_from_thread(self.hint.update, msg)

        try:
            self.router.create_worker_and_route(project_key, on_status=_on_status)
            self.call_from_thread(self._focus_right_pane)
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.hint.update, f"Launch failed: {exc}")
        self.call_from_thread(setattr, self, "selected_key", key)
        self.call_from_thread(self._refresh_rows)

    def action_refresh(self) -> None:
        try:
            self.router.ensure_cockpit_layout()
        except Exception:  # noqa: BLE001
            pass
        self._refresh_rows()

    def action_request_quit(self) -> None:
        result = self.router.tmux.run(
            "confirm-before",
            "-p",
            "Shut down PollyPM? This stops ALL agents. (Ctrl-W detaches instead) [y/N]",
            "run-shell 'echo CONFIRMED'",
            check=False,
        )
        if result.returncode == 0 and "CONFIRMED" in (result.stdout or ""):
            try:
                supervisor = PollyPMService(self.config_path).load_supervisor()
                supervisor.shutdown_tmux()
            except Exception:  # noqa: BLE001
                pass
            self.exit()

    @on(events.Click, "#brand")
    @on(events.Click, "#tagline")
    def on_brand_click(self, event: events.Click) -> None:
        """Clicking the Polly logo/tagline returns to the dashboard."""
        try:
            self.router._show_static_view(
                self.router._load_supervisor(),
                f"{self.router._load_supervisor().config.project.tmux_session}:{self.router._COCKPIT_WINDOW}",
                "dashboard",
            )
        except Exception:  # noqa: BLE001
            pass

    def action_detach(self) -> None:
        self.router.tmux.run("detach-client", check=False)

    def on_unmount(self) -> None:
        """Clean up resources on exit — close store, release leases."""
        try:
            sup = self.router._supervisor
            if sup is not None:
                # Stop the CoreRail (and its HeartbeatRail ticker thread)
                # before closing the store so the ticker isn't racing
                # shutdown. CoreRail.stop() is idempotent.
                rail = getattr(sup, "core_rail", None)
                if rail is not None:
                    try:
                        rail.stop()
                    except Exception:  # noqa: BLE001
                        pass
                # Release any cockpit-held leases
                for lease in sup.store.list_leases():
                    if lease.owner == "cockpit":
                        sup.store.clear_lease(lease.session_name)
                sup.store.close()
        except Exception:  # noqa: BLE001
            pass

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
        self._unread_keys.discard(row.cockpit_key)
        try:
            self.router.route_selected(row.cockpit_key)
            # The router may redirect (e.g. project:x → project:x:dashboard),
            # so re-read the selected key to keep the highlight in sync.
            self.selected_key = self.router.selected_key()
            self._focus_right_if_live()
        except Exception as exc:  # noqa: BLE001
            self.hint.update(f"Error: {exc}"[:60])
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
    .field-row {
        height: auto;
        padding-bottom: 1;
    }
    .field-label {
        color: #6b7a88;
        width: 12;
    }
    .field-value {
        color: #e0e8ef;
    }
    #actions {
        height: auto;
        padding-top: 1;
    }
    #actions Button {
        margin-right: 1;
    }
    """

    def __init__(self, config_path: Path, project_key: str) -> None:
        super().__init__()
        self.config_path = config_path
        self.project_key = project_key
        self.title_bar = Static("", id="title-bar")
        self.message_bar = Static("", id="message")

    def compose(self) -> ComposeResult:
        yield self.title_bar
        yield self.message_bar
        with Vertical(classes="settings-section"):
            yield Static("Worker Session", classes="section-label")
            yield Static("", id="worker-info")
        with Vertical(classes="settings-section"):
            yield Static("Model & Account", classes="section-label")
            yield Static("", id="model-info")
        with Horizontal(id="actions"):
            yield Button("Reset Session", id="reset-session", variant="warning")
            yield Button("Switch to Claude", id="switch-claude", variant="primary")
            yield Button("Switch to Codex", id="switch-codex", variant="primary")

    def on_mount(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        config = load_config(self.config_path)
        project = config.projects.get(self.project_key)
        if project is None:
            self.title_bar.update(f"Project not found: {self.project_key}")
            return
        self.title_bar.update(f"{project.name or project.key} \u2022 Settings")

        # Find worker session for this project
        worker = None
        for session in config.sessions.values():
            if session.role == "worker" and session.project == self.project_key and session.enabled:
                worker = session
                break

        worker_info = self.query_one("#worker-info", Static)
        model_info = self.query_one("#model-info", Static)

        if worker is None:
            worker_info.update("No worker session configured.\nPress N in the sidebar to create one.")
            model_info.update("")
            return

        account = config.accounts.get(worker.account)
        account_label = f"{account.email} [{account.provider.value}]" if account else worker.account
        worker_info.update(
            f"[dim]Session:[/] [bold]{worker.name}[/]\n"
            f"[dim]Window:[/]  {worker.window_name}\n"
            f"[dim]CWD:[/]     {worker.cwd}"
        )
        model_info.update(
            f"[dim]Provider:[/] [bold]{worker.provider.value}[/]\n"
            f"[dim]Account:[/]  {account_label}\n"
            f"[dim]Args:[/]     {' '.join(worker.args) if worker.args else 'none'}"
        )

    def _notify(self, msg: str) -> None:
        self.message_bar.update(msg)

    @on(Button.Pressed, "#reset-session")
    def on_reset(self, event: Button.Pressed) -> None:
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
        self._refresh()

    @on(Button.Pressed, "#switch-claude")
    def on_switch_claude(self, event: Button.Pressed) -> None:
        self._switch_provider(ProviderKind.CLAUDE)

    @on(Button.Pressed, "#switch-codex")
    def on_switch_codex(self, event: Button.Pressed) -> None:
        self._switch_provider(ProviderKind.CODEX)

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
        # Find first account with the target provider
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
            PollyPMService(self.config_path).switch_session_account(worker.name, target_account)
            self._notify(f"Switched to {target_provider.value} ({target_account}). Session restarted.")
        except Exception as exc:  # noqa: BLE001
            self._notify(f"Switch failed: {exc}")
        self._refresh()


class PollyDashboardApp(App[None]):
    """Rich dashboard: what's happening, what got done, token usage."""

    TITLE = "PollyPM"
    SUB_TITLE = "Dashboard"
    CSS = """
    Screen {
        background: #0d1117;
        color: #c9d1d9;
        padding: 0 1;
        layout: vertical;
        overflow-y: auto;
    }
    .header { padding: 1 0 0 0; }
    .header-title { color: #e6edf3; text-style: bold; }
    .header-stats { color: #8b949e; }
    .section-title {
        color: #58a6ff;
        text-style: bold;
        padding: 1 0 0 0;
    }
    .section-body { padding: 0 0 0 2; }
    .done-section { padding: 0 0 0 2; }
    .chart-section { padding: 0 0 0 2; }
    .footer { color: #484f58; padding: 1 0 0 0; }
    """

    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self.config_path = config_path
        self.header_w = Static("", classes="header", markup=True)
        self.now_title = Static("[b]Now[/b]", classes="section-title", markup=True)
        self.now_body = Static("", classes="section-body", markup=True)
        self.done_title = Static("[b]Done[/b]", classes="section-title", markup=True)
        self.done_body = Static("", classes="done-section", markup=True)
        self.chart_title = Static("[b]Tokens[/b]", classes="section-title", markup=True)
        self.chart_body = Static("", classes="chart-section", markup=True)
        self.footer_w = Static("", classes="footer", markup=True)

    def compose(self) -> ComposeResult:
        yield self.header_w
        yield self.now_title
        yield self.now_body
        yield self.done_title
        yield self.done_body
        yield self.chart_title
        yield self.chart_body
        yield self.footer_w

    def on_mount(self) -> None:
        self._refresh()
        self.set_interval(10, self._refresh)

    def _age_str(self, seconds: float) -> str:
        if seconds < 60:
            return "just now"
        if seconds < 3600:
            return f"{int(seconds // 60)}m ago"
        if seconds < 86400:
            return f"{int(seconds // 3600)}h ago"
        return f"{int(seconds // 86400)}d ago"

    def _refresh(self) -> None:
        try:
            from pollypm.dashboard_data import gather
            config = load_config(self.config_path)
            from pollypm.storage.state import StateStore
            store = StateStore(config.project.state_db)
            try:
                data = gather(config, store)
            finally:
                store.close()
        except Exception as exc:  # noqa: BLE001
            self.header_w.update(f"[dim]Error: {exc}[/dim]")
            return

        # ── Header ──
        parts = [f"[b]{len(config.projects)}[/b] projects", f"[b]{len(config.sessions)}[/b] agents"]
        if data.inbox_count:
            parts.append(f"[#d29922][b]{data.inbox_count}[/b] inbox[/#d29922]")
        if data.alert_count:
            parts.append(f"[#f85149][b]{data.alert_count}[/b] alerts[/#f85149]")
        header_text = "  " + "  \u00b7  ".join(parts)
        if data.briefing:
            header_text += f"\n\n  [#58a6ff]{data.briefing}[/#58a6ff]"
        self.header_w.update(header_text)

        # ── Now: what's being worked on ──
        lines: list[str] = []
        for s in data.active_sessions:
            if s.role == "heartbeat-supervisor":
                continue
            if s.status in ("healthy", "needs_followup"):
                icon = "[#3fb950]\u25cf[/#3fb950]"
                name = f"[b]{s.project_label}[/b]" if s.role != "operator-pm" else "[b]Polly[/b]"
                desc = s.description
                age = f"[dim]{self._age_str(s.age_seconds)}[/dim]"
                lines.append(f"{icon} {name}")
                lines.append(f"  [dim]{desc}[/dim]  {age}")
                lines.append("")
            elif s.status == "waiting_on_user":
                icon = "[#f85149]\u25c7[/#f85149]"
                name = f"[b]{s.project_label}[/b]" if s.role != "operator-pm" else "[b]Polly[/b]"
                lines.append(f"{icon} {name}")
                lines.append(f"  [#f85149]{s.description}[/#f85149]")
                lines.append("")
            else:
                icon = "[dim]\u25cb[/dim]"
                name = f"[dim]{s.project_label}[/dim]" if s.role != "operator-pm" else "[dim]Polly[/dim]"
                lines.append(f"{icon} {name}  [dim]{s.status}[/dim]")
        self.now_body.update("\n".join(lines) if lines else "[dim]No active sessions[/dim]")

        # ── Done: commits + completed issues ──
        done_lines: list[str] = []
        if data.recent_commits:
            done_lines.append(f"[#3fb950]\u2713[/#3fb950] [b]{len(data.recent_commits)}[/b] commits today")
            for c in data.recent_commits[:6]:
                age = self._age_str(c.age_seconds)
                done_lines.append(
                    f"  [dim]{c.hash}[/dim] {c.message}"
                )
            if len(data.recent_commits) > 6:
                done_lines.append(f"  [dim]  + {len(data.recent_commits) - 6} more[/dim]")
            done_lines.append("")

        if data.completed_items:
            done_lines.append(f"[#3fb950]\u2713[/#3fb950] [b]{len(data.completed_items)}[/b] issues completed")
            for item in data.completed_items[:5]:
                age = self._age_str(item.age_seconds)
                done_lines.append(f"  [dim]\u2500[/dim] {item.title}  [dim]{age}[/dim]")
            done_lines.append("")

        if not data.recent_commits and not data.completed_items:
            summary = []
            if data.sweep_count_24h:
                summary.append(f"[#3fb950]{data.sweep_count_24h}[/#3fb950] sweeps")
            if data.message_count_24h:
                summary.append(f"[#58a6ff]{data.message_count_24h}[/#58a6ff] messages")
            if data.recovery_count_24h:
                summary.append(f"[#d29922]{data.recovery_count_24h}[/#d29922] recoveries")
            if summary:
                done_lines.append("  ".join(summary))
            else:
                done_lines.append("[dim]No activity in the last 24 hours[/dim]")

        self.done_body.update("\n".join(done_lines))

        # ── Token chart ──
        if data.daily_tokens:
            values = [t for _, t in data.daily_tokens]
            max_val = max(values) or 1
            chart_height = 6
            bars = [max(0, min(chart_height, round(v / max_val * chart_height))) for v in values]

            chart_lines: list[str] = []
            for row in range(chart_height, 0, -1):
                line_chars: list[str] = []
                for bar_h in bars:
                    if bar_h >= row:
                        line_chars.append("[#58a6ff]\u2588\u2588[/#58a6ff]")
                    else:
                        line_chars.append("  ")
                chart_lines.append("".join(line_chars))

            axis = "[dim]" + "\u2500\u2500" * len(bars) + "[/dim]"
            chart_lines.append(axis)
            if len(data.daily_tokens) >= 2:
                first = data.daily_tokens[0][0][-5:]
                last = data.daily_tokens[-1][0][-5:]
                pad = max(1, len(bars) * 2 - len(first) - len(last))
                chart_lines.append(f"[dim]{first}{' ' * pad}{last}[/dim]")
            chart_lines.append("")
            chart_lines.append(
                f"[b]{data.total_tokens:,}[/b] total  \u00b7  [b]{data.today_tokens:,}[/b] today"
            )
            self.chart_body.update("\n".join(chart_lines))
        else:
            self.chart_body.update("[dim]No token data yet[/dim]")

        # ── Footer ──
        self.footer_w.update(
            "[dim]Click Polly to connect  \u00b7  "
            f"{data.sweep_count_24h} sweeps today  \u00b7  "
            f"{data.message_count_24h} messages[/dim]"
        )


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
        border: round #253140;
        background: #111820;
        padding: 1 2;
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


class PollyTasksApp(App[None]):
    """Interactive task list with drill-down detail view."""

    TITLE = "PollyPM"
    SUB_TITLE = "Tasks"
    BINDINGS = [
        Binding("escape", "back", "Back to list"),
        Binding("r", "refresh", "Refresh"),
        Binding("a", "approve_task", "Approve"),
        Binding("x", "reject_task", "Reject"),
    ]
    CSS = """
    Screen { background: #10161b; color: #eef2f4; }
    #task-list { height: 1fr; padding: 1 2; }
    #task-detail-scroll { height: 1fr; display: none; overflow-y: auto; }
    #task-detail-scroll.visible { display: block; }
    #task-detail { padding: 1 2; }
    .task-row { padding: 0 1; }
    .task-row:hover { background: #1a2530; }
    """

    _STATUS_ICONS = {
        "draft": "◌", "queued": "○", "in_progress": "⟳", "blocked": "⊘",
        "on_hold": "⏸", "review": "◉", "done": "✓", "cancelled": "✗",
    }

    def __init__(self, config_path: Path, project_key: str) -> None:
        super().__init__()
        self.config_path = config_path
        self.project_key = project_key
        self._tasks: list = []
        self._selected_task_id: str | None = None

    def compose(self) -> ComposeResult:
        from textual.containers import VerticalScroll
        yield ListView(id="task-list")
        with VerticalScroll(id="task-detail-scroll"):
            yield Static("", id="task-detail")

    def on_mount(self) -> None:
        self._refresh_list()
        self.set_interval(10, self._refresh_list)

    def _get_svc(self):
        from pollypm.work.sqlite_service import SQLiteWorkService
        config = load_config(self.config_path)
        project = config.projects.get(self.project_key)
        if not project:
            return None
        db_path = project.path / ".pollypm" / "state.db"
        if not db_path.exists():
            return None
        return SQLiteWorkService(db_path=db_path, project_path=project.path)

    def _refresh_list(self) -> None:
        svc = self._get_svc()
        lv = self.query_one("#task-list", ListView)
        lv.clear()
        if svc is None:
            lv.append(ListItem(Static("No tasks — work service not initialized for this project.")))
            self._tasks = []
            return
        try:
            self._tasks = svc.list_tasks(project=self.project_key)
        finally:
            svc.close()

        # Summary bar
        counts: dict[str, int] = {}
        for t in self._tasks:
            s = t.work_status.value
            counts[s] = counts.get(s, 0) + 1
        parts = []
        for status in ("queued", "in_progress", "review", "blocked", "on_hold", "done"):
            n = counts.get(status, 0)
            if n:
                icon = self._STATUS_ICONS.get(status, "·")
                parts.append(f"{icon} {n} {status.replace('_', ' ')}")
        summary = " · ".join(parts) if parts else "No tasks"
        lv.append(ListItem(Static(Text(summary, style="bold"))))

        # Active tasks — sorted by status priority
        _STATUS_ORDER = {"in_progress": 0, "review": 1, "queued": 2, "blocked": 3, "on_hold": 4, "draft": 5}
        active = [t for t in self._tasks if t.work_status.value not in ("done", "cancelled")]
        active.sort(key=lambda t: _STATUS_ORDER.get(t.work_status.value, 9))
        for t in active:
            icon = self._STATUS_ICONS.get(t.work_status.value, "·")
            assignee = f" [{t.assignee}]" if t.assignee else ""
            label = f"  {icon} #{t.task_number} {t.title}{assignee}"
            item = ListItem(Static(label), id=f"task-{t.project}-{t.task_number}")
            item._task_id = t.task_id  # type: ignore[attr-defined]
            lv.append(item)

        # Completed
        completed = [t for t in self._tasks if t.work_status.value in ("done", "cancelled")]
        if completed:
            lv.append(ListItem(Static(Text(f"── Completed ({len(completed)}) ──", style="dim"))))
            for t in completed[:10]:
                icon = self._STATUS_ICONS.get(t.work_status.value, "·")
                label = f"  {icon} #{t.task_number} {t.title}"
                item = ListItem(Static(label), id=f"task-{t.project}-{t.task_number}")
                item._task_id = t.task_id  # type: ignore[attr-defined]
                lv.append(item)

    @on(ListView.Selected)
    def _on_task_selected(self, event: ListView.Selected) -> None:
        task_id = getattr(event.item, "_task_id", None)
        if task_id is None:
            return
        self._selected_task_id = task_id
        self._show_detail(task_id)

    def _show_detail(self, task_id: str) -> None:
        svc = self._get_svc()
        if svc is None:
            return
        try:
            task = svc.get(task_id)
            task.context = svc.get_context(task_id, limit=10)
            task.executions = svc.get_execution(task_id)
            owner = svc.derive_owner(task)
        finally:
            svc.close()

        icon = self._STATUS_ICONS.get(task.work_status.value, "·")
        lines = [
            f"{icon} #{task.task_number} {task.title}",
            "",
            f"  Status    {task.work_status.value}",
            f"  Priority  {task.priority.value}",
            f"  Flow      {task.flow_template_id}",
            f"  Node      {task.current_node_id or '—'}",
            f"  Owner     {owner or '—'}",
        ]
        if task.roles:
            roles = ", ".join(f"{k}={v}" for k, v in task.roles.items())
            lines.append(f"  Roles     {roles}")
        if task.assignee:
            lines.append(f"  Assignee  {task.assignee}")
        # Per-task token usage aggregated across worker sessions (#86).
        tokens_in = getattr(task, "total_input_tokens", 0) or 0
        tokens_out = getattr(task, "total_output_tokens", 0) or 0
        sess_count = getattr(task, "session_count", 0) or 0
        if tokens_in or tokens_out or sess_count:
            lines.append(
                f"  Tokens    in={tokens_in}  out={tokens_out}  "
                f"sessions={sess_count}"
            )

        if task.description:
            lines.extend(["", "── Description ──────────────────────────", "", task.description])
        if task.acceptance_criteria:
            lines.extend(["", "── Acceptance Criteria ──────────────────", "", task.acceptance_criteria])

        # State progression timeline
        if task.executions:
            lines.extend(["", "── Timeline ─────────────────────────────", ""])
            for ex in task.executions:
                status = ex.status.value if hasattr(ex.status, "value") else ex.status
                # Visual timeline marker
                if status == "active":
                    marker = "⟳"
                elif ex.decision:
                    dec = ex.decision.value if hasattr(ex.decision, "value") else ex.decision
                    marker = "✓" if dec == "approved" else "✗"
                else:
                    marker = "●"
                # Node label with visit
                visit_label = f" (attempt {ex.visit})" if ex.visit > 1 else ""
                line = f"  {marker} {ex.node_id}{visit_label}"
                # Add decision info
                if ex.decision:
                    dec = ex.decision.value if hasattr(ex.decision, "value") else ex.decision
                    line += f" — {dec}"
                    if ex.decision_reason:
                        lines.append(line)
                        lines.append(f"    \"{ex.decision_reason}\"")
                        line = None
                if line is not None:
                    lines.append(line)
                # Show work output summary
                if ex.work_output:
                    wo_obj = ex.work_output
                    if hasattr(wo_obj, "summary") and wo_obj.summary:
                        lines.append(f"    → {wo_obj.summary}")
                    if hasattr(wo_obj, "artifacts") and wo_obj.artifacts:
                        for art in wo_obj.artifacts[:3]:
                            kind = getattr(art, "kind", None)
                            if hasattr(kind, "value"):
                                kind = kind.value
                            desc = getattr(art, "description", "") or getattr(art, "ref", "")
                            if kind and desc:
                                lines.append(f"    · {kind}: {desc}")
                lines.append("")

        # Context log
        if task.context:
            lines.extend(["── Context Log ──────────────────────────", ""])
            for c in task.context:
                ts = str(c.timestamp)[:16] if c.timestamp else ""
                lines.append(f"  [{c.actor}] {c.text}")
                if ts:
                    lines.append(f"    {ts}")
            lines.append("")

        # Transcript path hint
        transcript_dir = Path(self.config_path).parent.parent
        if hasattr(task, "project"):
            config = load_config(self.config_path)
            proj = config.projects.get(task.project)
            if proj:
                archive = proj.path / ".pollypm" / "transcripts" / "tasks" / task.task_id
                if archive.exists():
                    lines.extend([
                        "── Transcript ───────────────────────────",
                        "",
                        f"  {archive}",
                    ])

        # Show action hint for tasks in review
        if task.work_status.value == "review":
            lines.extend([
                "",
                "── Actions ──────────────────────────────",
                "",
                "  [a] Approve   [x] Reject   [esc] Back",
            ])

        detail = self.query_one("#task-detail", Static)
        detail.update("\n".join(lines))
        self.query_one("#task-detail-scroll").add_class("visible")
        self.query_one("#task-list", ListView).styles.display = "none"

    def action_back(self) -> None:
        self.query_one("#task-detail-scroll").remove_class("visible")
        self.query_one("#task-list", ListView).styles.display = "block"
        self._selected_task_id = None

    def action_refresh(self) -> None:
        if self._selected_task_id:
            self._show_detail(self._selected_task_id)
        else:
            self._refresh_list()

    def action_approve_task(self) -> None:
        """Approve the currently viewed task (human review)."""
        if not self._selected_task_id:
            return
        svc = self._get_svc()
        if svc is None:
            return
        try:
            task = svc.get(self._selected_task_id)
            if task.work_status.value != "review":
                self.notify("Task is not in review state", severity="warning")
                return
            svc.approve(self._selected_task_id, "user", "Approved from cockpit")
            self.notify(f"Approved {self._selected_task_id}", severity="information")
            self._show_detail(self._selected_task_id)
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Approve failed: {exc}", severity="error")
        finally:
            svc.close()

    def action_reject_task(self) -> None:
        """Reject the currently viewed task (human review) — prompts for reason."""
        if not self._selected_task_id:
            return
        svc = self._get_svc()
        if svc is None:
            return
        try:
            task = svc.get(self._selected_task_id)
            if task.work_status.value != "review":
                self.notify("Task is not in review state", severity="warning")
                svc.close()
                return
            # For now, reject with a generic reason — TODO: add input prompt
            svc.reject(self._selected_task_id, "user", "Rejected from cockpit — needs rework")
            self.notify(f"Rejected {self._selected_task_id}", severity="information")
            self._show_detail(self._selected_task_id)
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Reject failed: {exc}", severity="error")
        finally:
            svc.close()


class PollySettingsPaneApp(App[None]):
    TITLE = "PollyPM"
    SUB_TITLE = "Settings"
    CSS = """
    Screen {
        background: #0c0f12;
        color: #eef2f4;
        padding: 1;
        layout: vertical;
    }
    #status {
        height: 1;
        color: #a8b8c4;
        background: #111820;
        padding: 0 1;
    }
    #message {
        height: 1;
        color: #7ee8a4;
        background: #111820;
        padding: 0 1;
    }
    #actions {
        height: auto;
        padding: 1 0;
    }
    #actions Button {
        margin-right: 1;
        min-width: 10;
    }
    #layout {
        height: 1fr;
    }
    #accounts {
        width: 58;
        min-width: 42;
        height: 1fr;
        border: round #1a2230;
        background: #0f1317;
    }
    #detail-pane {
        height: 1fr;
        border: round #1a2230;
        background: #0f1317;
        padding: 1 2;
    }
    .section-title {
        color: #5b8aff;
        text-style: bold;
        padding-bottom: 1;
    }
    #detail {
        height: 1fr;
        color: #b8c4cf;
    }
    #help {
        height: 2;
        color: #3e4c5a;
        background: #0c0f12;
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
        Binding("d", "remove_selected", "Remove"),
        Binding("u", "refresh", "Refresh"),
    ]

    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self.config_path = config_path
        self.service = PollyPMService(config_path)
        self.status_bar = Static("", id="status")
        self.message_bar = Static("", id="message")
        self.accounts = DataTable(id="accounts")
        self.detail = Static("", id="detail")
        self.help = Static(
            "C add Codex · L add Claude · Y usage · R relogin · D remove · J operator · M controller · V failover · B permissions · U refresh",
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
            yield Button("Remove", id="remove", variant="error")
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
        try:
            config = load_config(self.config_path)
            statuses = self.service.list_account_statuses()
        except Exception:  # noqa: BLE001
            return  # Don't crash the TUI on transient errors
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
                        "yes" if config.pollypm.controller_account == status.key else "",
                        "yes" if status.key in config.pollypm.failover_accounts else "",
                        status.usage_summary,
                    ),
                    status.key,
                )
            )
        self._replace_rows(rows, selected)
        current_key = self._current_selected_key()
        self._selected_account_key = current_key
        controller = config.pollypm.controller_account
        self.status_bar.update(
            f"Controller: {controller} · Open permissions: {'on' if config.pollypm.open_permissions_by_default else 'off'} · Accounts: {len(statuses)}"
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
        sep = "[dim]" + "\u2500" * 40 + "[/dim]"
        is_ctrl = config.pollypm.controller_account == status.key
        is_fo = status.key in config.pollypm.failover_accounts
        detail_lines = [
            f"[bold]Account: {status.key}[/bold]",
            sep,
            f"[dim]Email:[/dim]      {status.email or '-'}",
            f"[dim]Provider:[/dim]   {status.provider.value}",
            f"[dim]Logged in:[/dim]  {'yes' if status.logged_in else 'no'}",
            f"[dim]Health:[/dim]     {status.health}",
            f"[dim]Plan:[/dim]       {status.plan}",
            f"[dim]Usage:[/dim]      {status.usage_summary}",
            sep,
            f"[dim]Controller:[/dim] {'yes' if is_ctrl else 'no'}",
            f"[dim]Failover:[/dim]   {'yes' if is_fo else 'no'}",
            f"[dim]Home:[/dim]       {status.home or '-'}",
            sep,
            f"[dim]Isolation:[/dim]  {status.isolation_status}",
            f"[dim]Storage:[/dim]    {status.auth_storage}",
        ]
        if status.available_at:
            detail_lines.append(f"[dim]Available:[/dim]  {status.available_at}")
        if status.access_expires_at:
            detail_lines.append(f"[dim]Expires:[/dim]    {status.access_expires_at}")
        if status.reason:
            detail_lines.extend([sep, f"[dim]Reason:[/dim]     {status.reason}"])
        if status.usage_raw_text:
            snippet = status.usage_raw_text.strip().splitlines()[:8]
            if snippet:
                detail_lines.extend([sep, "[dim]Latest usage snapshot:[/dim]"])
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
        enabled = not config.pollypm.open_permissions_by_default
        self._run_action("Toggle open permissions", lambda: self.service.set_open_permissions_default(enabled))

    def action_remove_selected(self) -> None:
        key = self._selected_key_or_notice()
        if key is None:
            return
        self._run_action("Remove account", lambda: self.service.remove_account(key))

    @on(Button.Pressed)
    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "add-codex": self.action_add_codex,
            "add-claude": self.action_add_claude,
            "usage": self.action_refresh_usage,
            "relogin": self.action_relogin_selected,
            "remove": self.action_remove_selected,
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


# ---------------------------------------------------------------------------
# Inbox — interactive Textual screen
#
# Two-pane layout within the cockpit right pane:
#   left  — scrollable list of inbox items (sender · subject · age · unread)
#   right — focused message: subject, sender, timestamp, body (rich), thread
#
# Keybindings:
#   j/k or arrows  move selection          r  reply
#   enter / click  open (+ mark read)      a  archive
#   q / escape     back to cockpit nav     esc (in reply)  cancel reply
#
# Backend: SQLiteWorkService.add_reply / archive_task / mark_read /
# list_replies. Events (inbox.message.read / archived / reply) are emitted
# via StateStore, matching the shape of ``pm notify``.
# ---------------------------------------------------------------------------


# Sort: most recent first (matches email-inbox affordance), then priority
# as a secondary key so a newly arrived critical item outranks a slightly
# older normal one. Falls back to title for a stable ordering when two
# tasks share the same minute-resolution timestamp.
_INBOX_PRIORITY_RANK = {
    "critical": 0,
    "high": 1,
    "normal": 2,
    "low": 3,
}


def _inbox_sort_key(task) -> tuple:
    updated = task.updated_at
    iso = updated.isoformat() if hasattr(updated, "isoformat") else str(updated or "")
    prio = getattr(task.priority, "value", str(task.priority))
    # Negate updated so newer comes first when sorted ascending; keep
    # priority as a positive rank so critical (0) wins inside a same-age
    # bucket.
    return (-_iso_sort_weight(iso), _INBOX_PRIORITY_RANK.get(prio, 9), task.title)


def _iso_sort_weight(iso: str) -> int:
    """Coerce an ISO timestamp to a comparable integer key.

    Lexicographic compare on ISO-8601 works for "same-offset" strings but
    we want a real ordering regardless. Falling back to string length keeps
    the sort stable for missing/invalid stamps without raising.
    """
    try:
        from datetime import datetime as _dt
        return int(_dt.fromisoformat(iso).timestamp())
    except (ValueError, TypeError):
        return 0


def _format_sender(task) -> str:
    """Best-effort human-friendly sender label for an inbox task.

    Chat-flow tasks have ``roles.operator`` set to whichever agent posted
    (``polly``, ``russell``, …). ``requester=user`` tasks that originate
    from a worker's notify use ``operator`` too. When nothing resolves,
    fall back to ``created_by``.
    """
    roles = getattr(task, "roles", {}) or {}
    op = roles.get("operator")
    if op and op != "user":
        return op
    if task.created_by and task.created_by != "user":
        return task.created_by
    # Last resort — unknown sender. Don't show blank.
    return "polly"


def _format_inbox_row(task, *, is_unread: bool, width: int = 38) -> Text:
    """Render one inbox-list row as two lines of Rich text.

    Matches the cockpit aesthetic from RailItem: yellow diamond for
    unread, dim open circle for read.

    Line 1 is the bold message title (truncated with an ellipsis if it
    won't fit ``width`` chars after the unread-marker glyph — no wrap).
    Line 2 is dim ``project · age`` metadata indented under the title.
    """
    from pollypm.tz import format_relative

    text = Text(no_wrap=True, overflow="ellipsis")
    if is_unread:
        text.append("\u25c6 ", style="#f0c45a")  # yellow diamond
    else:
        text.append("\u25cb ", style="#4a5568")  # dim circle
    subject = task.title or "(no subject)"
    # Account for the 2-char marker glyph prefix so the total row still
    # fits the target list-pane width without wrapping.
    max_subject = max(8, width - 2)
    if len(subject) > max_subject:
        subject = subject[: max_subject - 1] + "\u2026"
    subject_style = "bold #eef2f4" if is_unread else "bold #b8c4cf"
    text.append(subject, style=subject_style)

    # Line 2: project · age, dim. Indent by 2 so it lines up under the
    # subject text (past the marker glyph).
    updated = task.updated_at
    iso = updated.isoformat() if hasattr(updated, "isoformat") else str(updated or "")
    age = format_relative(iso) if iso else ""
    project = (task.project or "").strip() or "\u2014"
    meta_bits = [project]
    if age:
        meta_bits.append(age)
    meta_line = "  " + "  \u00b7  ".join(meta_bits)
    text.append("\n")
    text.append(meta_line, style="#6b7a88")
    return text


def _task_is_rollup(task) -> bool:
    """True when a task was created by notification_staging.flush_milestone_digest.

    Primary signal is the ``rollup`` label added by flush — the title
    regex is a fallback for rollups created before the label landed.
    """
    labels = getattr(task, "labels", None) or []
    if "rollup" in labels:
        return True
    title = (getattr(task, "title", "") or "").lower()
    return "ready for review" in title and "updates" in title


def _resolve_pm_target(config_path: Path, project_key: str | None) -> tuple[str, str]:
    """Resolve the cockpit-router key + display name for a project's PM.

    * Project has a ``persona_name`` configured → dispatch to its PM Chat
      window (``project:<key>:session``) and surface the persona name.
    * Otherwise (including ``project_key`` being empty or absent from
      the config) → fall back to Polly (``polly`` → operator session).
    """
    fallback_key = "polly"
    fallback_name = "Polly"
    if not project_key:
        return fallback_key, fallback_name
    try:
        config = load_config(config_path)
    except Exception:  # noqa: BLE001 — config errors shouldn't crash the TUI
        return fallback_key, fallback_name
    projects = getattr(config, "projects", {}) or {}
    project = projects.get(project_key)
    if project is None:
        return fallback_key, fallback_name
    persona = getattr(project, "persona_name", None)
    if isinstance(persona, str) and persona.strip():
        return f"project:{project_key}:session", persona.strip()
    return fallback_key, fallback_name


def _build_pm_context_line(
    task, *, item: dict | None = None, max_title: int = 64,
) -> str:
    """Compose the contextual first-line Sam sees in the PM input.

    Shape matches the spec: ``re: inbox/<task_number> "<title>"``. When
    ``item`` is provided (a rollup sub-item), include the sub-item
    subject so the PM knows which constituent task Sam wants to discuss.
    """
    task_id = getattr(task, "task_id", None)
    if not task_id:
        # Derive from project + number if the helper was passed a fresh Task.
        project = getattr(task, "project", "")
        number = getattr(task, "task_number", "")
        task_id = f"{project}/{number}" if project and number else "inbox/?"
    if item is not None:
        title = (item.get("subject") or task.title or "").strip()
    else:
        title = (getattr(task, "title", "") or "").strip()
    if len(title) > max_title:
        title = title[: max_title - 1] + "\u2026"
    # Strip embedded quotes so the shell/tmux literal doesn't break.
    title = title.replace('"', "'")
    return f're: inbox/{task_id} "{title}"'


def _extract_proposal_spec(task, *, labels: list[str] | None = None) -> dict:
    """Recover a proposal's ``proposed_task_spec`` from an inbox row.

    The body was rendered at emit time by ``render_proposal_body`` which
    intersperses the rationale with a ``## Proposed task`` markdown
    block. Rather than parse that back, we fall back to title +
    description: the accepted follow-on task uses the proposal title
    and rationale as its description. Tests that care about the exact
    spec shape can stub :meth:`PollyInboxApp._proposal_specs` directly.
    """
    spec: dict[str, object] = {}
    subject = (getattr(task, "title", "") or "").strip()
    if subject:
        spec["title"] = subject
    body = (getattr(task, "description", "") or "").strip()
    # Split at the preview marker so the accepted follow-on task only
    # carries the rationale, not the spec scaffold.
    marker = "## Proposed task"
    if marker in body:
        rationale, _, tail = body.partition(marker)
        spec["description"] = rationale.strip()
        # Recover acceptance criteria from the preview, when present.
        for line in tail.splitlines():
            stripped = line.strip()
            if stripped.startswith("- **acceptance criteria**"):
                # Subsequent indented lines form the AC block.
                continue
        # Heuristic AC extractor: grab the block after ``acceptance criteria:``.
        ac_lines: list[str] = []
        capturing = False
        for line in tail.splitlines():
            low = line.lstrip().lower()
            if low.startswith("- **acceptance criteria**"):
                capturing = True
                continue
            if capturing:
                if line.startswith("- **") and not line.lstrip().startswith(
                    "- **acceptance"
                ):
                    break
                if line.strip():
                    ac_lines.append(line.strip())
        if ac_lines:
            spec["acceptance_criteria"] = "\n".join(ac_lines)
    else:
        spec["description"] = body
    return spec


class _RollupItem(ListItem):
    """One sub-item in a rollup's expanded thread.

    We inherit ListItem for consistent hover/click semantics, but the
    widget lives inside a ``Vertical`` (not a ``ListView``), so it
    behaves as a click target only — no cursor selection. Click emits a
    ``Clicked`` message which the inbox app handles via ``on``.
    """

    def __init__(
        self,
        *,
        index: int,
        item: dict,
        expanded: bool,
        focused: bool,
    ) -> None:
        self.index = index
        self.item = item
        self.expanded = expanded
        self._body = Static(self._build_text(), markup=True)
        classes = ["rollup-item"]
        if expanded:
            classes.append("-expanded")
        if focused:
            classes.append("-focused")
        super().__init__(self._body, classes=" ".join(classes))

    def _build_text(self) -> str:
        from pollypm.tz import format_relative
        subject = self.item.get("subject") or "(no subject)"
        created = self.item.get("created_at") or ""
        age = format_relative(created) if created else ""
        payload = self.item.get("payload") or {}
        ref_bits: list[str] = []
        for key in ("commit", "pr", "pull_request", "url"):
            val = payload.get(key)
            if val:
                ref_bits.append(f"{key}={val}")
        marker = "\u25bc" if self.expanded else "\u25b8"
        header = f"[b]{marker} {_escape(subject)}[/b]"
        if age:
            header += f"  [dim]{_escape(age)}[/dim]"
        lines = [header]
        if ref_bits:
            lines.append(f"[dim]{_escape(' \u00b7 '.join(ref_bits))}[/dim]")
        if self.expanded:
            body = (self.item.get("body") or "").strip()
            if body:
                lines.append("")
                lines.append(_md_to_rich(_escape_body(body)))
            actor = self.item.get("actor") or ""
            source_project = self.item.get("source_project") or ""
            meta_bits: list[str] = []
            if actor:
                meta_bits.append(actor)
            if source_project:
                meta_bits.append(source_project)
            if meta_bits:
                lines.append("")
                lines.append(f"[dim]{_escape(' \u00b7 '.join(meta_bits))}[/dim]")
        return "\n".join(lines)


class _InboxListItem(ListItem):
    """One message in the inbox list — carries the task_id + unread flag."""

    def __init__(self, task, *, is_unread: bool) -> None:
        self.task_id = task.task_id
        self.task_ref = task
        self.is_unread = is_unread
        self._body = Static(_format_inbox_row(task, is_unread=is_unread), markup=False)
        super().__init__(self._body, classes="inbox-row")
        if is_unread:
            self.add_class("unread")

    def mark_read(self, task=None) -> None:
        """Flip the row to read styling in place (no reflow of the list)."""
        if self.is_unread is False:
            return
        self.is_unread = False
        self.remove_class("unread")
        if task is not None:
            self.task_ref = task
        self._body.update(_format_inbox_row(self.task_ref, is_unread=False))


class PollyInboxApp(App[None]):
    """Interactive cockpit inbox — two-pane list + detail with reply/archive.

    Opened via ``pm cockpit-pane inbox``. Replaces the previous read-only
    text dump so the user can drive the inbox entirely from the TUI
    without falling back to the CLI.
    """

    TITLE = "PollyPM"
    SUB_TITLE = "Inbox"
    CSS = """
    Screen {
        background: #0f1317;
        color: #eef2f4;
        padding: 0;
    }
    #inbox-layout {
        height: 1fr;
    }
    #inbox-list {
        width: 42;
        min-width: 32;
        height: 1fr;
        background: #0f1317;
        border: round #1e2730;
        padding: 0;
        scrollbar-size: 1 1;
        scrollbar-color: #2a3340;
    }
    #inbox-list > .inbox-row {
        height: 3;
        padding: 0 1;
        color: #d6dee5;
        background: transparent;
    }
    #inbox-list > .inbox-row.unread {
        color: #eef2f4;
    }
    #inbox-list > .inbox-row.-highlight {
        background: #1e2730;
    }
    #inbox-list:focus > .inbox-row.-highlight {
        background: #253140;
        color: #f2f6f8;
    }
    #inbox-detail-wrap {
        height: 1fr;
        border: round #1e2730;
        background: #0f1317;
        padding: 0;
    }
    #inbox-detail-scroll {
        height: 1fr;
        padding: 1 2;
        scrollbar-size: 1 1;
        scrollbar-color: #2a3340;
    }
    #inbox-detail {
        width: 1fr;
        height: auto;
        color: #d6dee5;
    }
    #inbox-reply {
        height: 3;
        padding: 0 1;
        background: #111820;
        border: round #2a3340;
        color: #d6dee5;
    }
    #inbox-reply:focus {
        border: round #5b8aff;
    }
    #inbox-status {
        height: 1;
        padding: 0 1;
        color: #6b7a88;
        background: #0c0f12;
    }
    #inbox-hint {
        height: 1;
        padding: 0 1;
        color: #3e4c5a;
        background: #0c0f12;
    }
    #inbox-empty {
        width: 1fr;
        height: 1fr;
        content-align: center middle;
        color: #6b7a88;
    }
    .reply-turn {
        background: #111820;
        padding: 1 2;
        margin: 1 0 0 0;
        border-left: thick #5b8aff;
        color: #d6dee5;
    }
    #rollup-items {
        height: auto;
        padding: 0;
    }
    #rollup-items .rollup-item {
        height: auto;
        padding: 0 1;
        margin: 1 0 0 0;
        background: #111820;
        border-left: thick #2a3340;
        color: #d6dee5;
    }
    #rollup-items .rollup-item.-focused {
        border-left: thick #5b8aff;
        background: #14202c;
    }
    #rollup-items .rollup-item.-expanded {
        border-left: thick #f0c45a;
    }
    #rollup-items .rollup-overflow {
        height: auto;
        padding: 0 1;
        margin: 1 0 0 0;
        color: #6b7a88;
    }
    """

    BINDINGS = [
        Binding("j,down", "cursor_down", "Down", show=False),
        Binding("k,up", "cursor_up", "Up", show=False),
        Binding("g,home", "cursor_first", "First", show=False),
        Binding("G,end", "cursor_last", "Last", show=False),
        Binding("enter,o", "open_selected", "Open", show=False),
        Binding("r", "start_reply", "Reply"),
        Binding("a", "archive_selected", "Archive"),
        Binding("d", "jump_to_pm", "Discuss"),
        # Improvement proposals (#275): capital A/X so plain lowercase
        # ``a`` (archive) stays distinct. Accepting / rejecting IS
        # archiving, but with a decision trail.
        Binding("A", "accept_proposal", "Accept", show=False),
        Binding("X", "reject_proposal", "Reject", show=False),
        Binding("e", "expand_all_rollup", "Expand all", show=False),
        Binding("u", "refresh", "Refresh"),
        Binding("q,escape", "back_or_cancel", "Back"),
    ]

    REFRESH_INTERVAL_SECONDS = 8
    # Show the first ``ROLLUP_DEFAULT_VISIBLE`` items of a rollup, collapse
    # the rest behind an expand keybind so a 40-item digest doesn't flood
    # the detail pane.
    ROLLUP_DEFAULT_VISIBLE = 10

    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self.config_path = config_path
        self.list_view = ListView(id="inbox-list")
        self.detail = Static("", id="inbox-detail", markup=True)
        # Rollup items are rendered as sibling widgets under the detail
        # Static so each item is individually clickable. The container
        # stays in the tree for every task; it's empty (display:none) on
        # non-rollup selections.
        self.rollup_items_box = Vertical(id="rollup-items")
        self.reply_input = Input(
            placeholder="Reply \u2026 (Enter to send, Esc back to list)",
            id="inbox-reply",
        )
        self.status = Static("", id="inbox-status")
        self.hint = Static(
            PollyInboxApp._DEFAULT_HINT,
            id="inbox-hint",
        )
        self._tasks: list = []
        self._selected_task_id: str | None = None
        self._unread_ids: set[str] = set()
        # Rollup state — populated on each rollup render. Index-keyed so
        # the click handler can look up which item was expanded.
        self._rollup_items: list[dict] = []
        self._rollup_expanded: set[int] = set()
        self._rollup_show_all: bool = False
        # Focused rollup sub-item for ``d`` dispatch. None when the whole
        # rollup is the jump target (or on a non-rollup task).
        self._rollup_focused_index: int | None = None
        # Improvement-proposal state (#275). When the user presses ``X``
        # on a proposal item, the shared reply_input becomes a rationale
        # prompt. We flag it here so the Input.Submitted handler routes
        # to record_proposal_rejection instead of add_reply.
        self._awaiting_rejection_task_id: str | None = None
        # Cache of parsed proposal_task_spec per inbox task_id, populated
        # on _render_detail. Accept uses it to seed the follow-on task
        # without re-reading the DB.
        self._proposal_specs: dict[str, dict] = {}

    def compose(self) -> ComposeResult:
        with Horizontal(id="inbox-layout"):
            yield self.list_view
            with Vertical(id="inbox-detail-wrap"):
                with VerticalScroll(id="inbox-detail-scroll"):
                    yield self.detail
                    yield self.rollup_items_box
                yield self.reply_input
        yield self.status
        yield self.hint

    def on_mount(self) -> None:
        self._refresh_list(select_first=True)
        self.set_interval(self.REFRESH_INTERVAL_SECONDS, self._background_refresh)
        self.list_view.focus()

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _load_inbox(self) -> tuple[list, set[str]]:
        """Open each project's work-service DB, compute (tasks, unread_ids).

        All services are closed before return — callers don't need to
        manage lifecycle. Per-task operations open a fresh svc via
        :meth:`_svc_for_task` so we never hold connections across ticks.
        """
        from pollypm.work.inbox_view import inbox_tasks
        from pollypm.work.sqlite_service import SQLiteWorkService

        config = load_config(self.config_path)
        tasks: list = []
        unread: set[str] = set()
        for project_key, project in getattr(config, "projects", {}).items():
            db_path = project.path / ".pollypm" / "state.db"
            if not db_path.exists():
                continue
            try:
                svc = SQLiteWorkService(
                    db_path=db_path, project_path=project.path,
                )
            except Exception:  # noqa: BLE001
                continue
            try:
                try:
                    project_tasks = inbox_tasks(svc, project=project_key)
                except Exception:  # noqa: BLE001
                    project_tasks = []
                for t in project_tasks:
                    tasks.append(t)
                    try:
                        rows = svc.get_context(
                            t.task_id, entry_type="read", limit=1,
                        )
                    except Exception:  # noqa: BLE001
                        rows = []
                    if not rows:
                        unread.add(t.task_id)
            finally:
                try:
                    svc.close()
                except Exception:  # noqa: BLE001
                    pass
        tasks.sort(key=_inbox_sort_key)
        return tasks, unread

    def _svc_for_task(self, task_id: str):
        """Open a SQLiteWorkService rooted at the project owning ``task_id``.

        The cockpit inbox spans every tracked project, so archive/reply
        actions must target the project-specific DB. We resolve the
        project from the task_id prefix and look up its path in config.
        """
        from pollypm.work.sqlite_service import SQLiteWorkService

        project_key = task_id.split("/", 1)[0]
        config = load_config(self.config_path)
        project = config.projects.get(project_key)
        if project is None:
            return None
        db_path = project.path / ".pollypm" / "state.db"
        if not db_path.exists():
            return None
        try:
            return SQLiteWorkService(db_path=db_path, project_path=project.path)
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    # List rendering
    # ------------------------------------------------------------------

    def _refresh_list(self, *, select_first: bool = False) -> None:
        tasks, unread = self._load_inbox()
        self._tasks = tasks
        self._unread_ids = unread
        self._render_list(select_first=select_first)

    def _render_list(self, *, select_first: bool = False) -> None:
        previous = self._selected_task_id
        self.list_view.clear()
        if not self._tasks:
            self.list_view.append(
                ListItem(Static("(empty)", classes="inbox-empty"), disabled=True)
            )
            self.detail.update(
                "[dim]No messages.\n\n"
                "Polly will notify you here when she has updates.[/dim]"
            )
            self.status.update("0 messages")
            return
        restore_index: int | None = 0 if select_first else None
        for idx, task in enumerate(self._tasks):
            is_unread = task.task_id in self._unread_ids
            row = _InboxListItem(task, is_unread=is_unread)
            self.list_view.append(row)
            if previous and task.task_id == previous:
                restore_index = idx
        if restore_index is not None and self.list_view.index != restore_index:
            self.list_view.index = restore_index
            # Render detail for the restored selection so the right pane
            # shows content immediately on refresh.
            task = self._tasks[restore_index]
            self._selected_task_id = task.task_id
            self._render_detail(task.task_id)
        unread_n = len(self._unread_ids)
        total = len(self._tasks)
        if unread_n:
            self.status.update(f"{total} messages \u00b7 {unread_n} unread")
        else:
            self.status.update(f"{total} messages")

    def _background_refresh(self) -> None:
        """Periodic re-read; don't stomp the current cursor position."""
        try:
            self._refresh_list(select_first=False)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Detail rendering
    # ------------------------------------------------------------------

    def _render_detail(self, task_id: str) -> None:
        svc = self._svc_for_task(task_id)
        if svc is None:
            self.detail.update("[red]Could not open project database for this task.[/red]")
            self._clear_rollup_items()
            return
        try:
            task = svc.get(task_id)
            replies = svc.list_replies(task_id)
            rollup_items_raw = (
                svc.get_context(task_id, entry_type="rollup_item")
                if _task_is_rollup(task) else []
            )
        except Exception as exc:  # noqa: BLE001
            self.detail.update(f"[red]Error loading task: {exc}[/red]")
            self._clear_rollup_items()
            svc.close()
            return
        finally:
            try:
                svc.close()
            except Exception:  # noqa: BLE001
                pass

        from pollypm.tz import format_relative

        updated_iso = (
            task.updated_at.isoformat()
            if hasattr(task.updated_at, "isoformat") else str(task.updated_at or "")
        )
        created_iso = (
            task.created_at.isoformat()
            if hasattr(task.created_at, "isoformat") else str(task.created_at or "")
        )
        when = _fmt_time(updated_iso or created_iso)
        rel = format_relative(updated_iso or created_iso)

        sender = _format_sender(task)
        # PM persona for the header — so the user knows which discussion
        # pane the ``d`` key will reach out to.
        _session, pm_label = _resolve_pm_target(self.config_path, task.project)
        sections: list[str] = []
        subject = task.title or "(no subject)"
        sections.append(f"[b #eef2f4]{_escape(subject)}[/b #eef2f4]")
        meta_bits = [f"[#5b8aff]{_escape(sender)}[/#5b8aff]"]
        if when:
            meta_bits.append(f"[#97a6b2]{_escape(when)}[/#97a6b2]")
        if rel:
            meta_bits.append(f"[dim]{_escape(rel)}[/dim]")
        if task.project and task.project != "inbox":
            meta_bits.append(f"[dim]\u00b7 {_escape(task.project)}[/dim]")
        prio = getattr(task.priority, "value", str(task.priority))
        if prio and prio != "normal":
            meta_bits.append(f"[#f0c45a]\u25c6 {_escape(prio)}[/#f0c45a]")
        # PM hint — dim, trailing, so it reads as metadata not a heading.
        meta_bits.append(f"[dim #6b7a88]PM: {_escape(pm_label)}[/dim #6b7a88]")
        sections.append("  \u00b7  ".join(meta_bits))
        sections.append("")  # blank line before body
        body = task.description or "(no body)"
        sections.append(_md_to_rich(_escape_body(body)))

        if replies:
            sections.append("")
            sections.append(f"[dim]\u2500\u2500 thread ({len(replies)}) \u2500\u2500[/dim]")
            for entry in replies:
                e_iso = (
                    entry.timestamp.isoformat()
                    if hasattr(entry.timestamp, "isoformat") else str(entry.timestamp)
                )
                age = format_relative(e_iso)
                who = entry.actor or "user"
                sections.append("")
                sections.append(
                    f"[b #5b8aff]{_escape(who)}[/b #5b8aff]  [dim]{_escape(age)}[/dim]"
                )
                sections.append(_md_to_rich(_escape_body(entry.text)))

        # Improvement-proposal detection (#275). Proposal items carry a
        # ``proposal`` label; when present, swap the hint bar for the
        # accept/reject keybindings. The body itself already embeds the
        # proposed-task-spec preview (written at emit time via
        # ``render_proposal_body``), so we don't double-render it here.
        _labels = list(getattr(task, "labels", []) or [])
        _is_proposal = "proposal" in _labels
        if _is_proposal:
            self._proposal_specs[task_id] = _extract_proposal_spec(
                task, labels=_labels,
            )
            self._update_hint_for_proposal()
        else:
            self._proposal_specs.pop(task_id, None)
            self._restore_default_hint()

        self.detail.update("\n".join(sections))
        # Rebuild the rollup item list (empty for non-rollups).
        self._render_rollup_items(rollup_items_raw)
        # Auto-scroll to top of the new message so subject is visible.
        try:
            self.query_one("#inbox-detail-scroll", VerticalScroll).scroll_home(
                animate=False,
            )
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Rollup rendering / expansion
    # ------------------------------------------------------------------

    def _clear_rollup_items(self) -> None:
        """Remove every child of the rollup-items box (safe on unmount).

        ``Widget.remove_children`` is a batch-sync operation — individual
        ``child.remove()`` calls are async and leave orphan widgets
        around during the test pilot's pause window, which produces
        duplicate-id collisions on the next mount.
        """
        self._rollup_items = []
        self._rollup_expanded = set()
        self._rollup_show_all = False
        self._rollup_focused_index = None
        try:
            box = self.rollup_items_box
            if box.children:
                box.remove_children()
            box.display = False
        except Exception:  # noqa: BLE001
            pass

    def _render_rollup_items(self, raw_entries) -> None:
        """Parse ``rollup_item`` context rows and populate ``self._rollup_items``.

        ``raw_entries`` are newest-first from ``get_context``; we reverse
        so the UI reads chronologically (matches the markdown summary).
        Resets expansion/focus state and delegates the actual widget
        mounting to :meth:`_mount_rollup_widgets` so re-render after a
        toggle reuses exactly the same mounting path.
        """
        import json as _json

        self._clear_rollup_items()
        if not raw_entries:
            return
        entries = list(reversed(raw_entries))
        parsed: list[dict] = []
        for e in entries:
            try:
                blob = _json.loads(e.text) if e.text else {}
            except (TypeError, ValueError):
                blob = {"subject": e.text or "(item)", "body": "", "payload": {}}
            if not isinstance(blob, dict):
                blob = {"subject": str(blob), "body": "", "payload": {}}
            blob.setdefault("actor", e.actor or "polly")
            blob.setdefault(
                "created_at",
                e.timestamp.isoformat()
                if hasattr(e.timestamp, "isoformat") else str(e.timestamp),
            )
            parsed.append(blob)
        self._rollup_items = parsed
        self._mount_rollup_widgets()

    def _mount_rollup_widgets(self) -> None:
        """Idempotently refresh the rollup-items box for the current state."""
        box = self.rollup_items_box
        if box.children:
            box.remove_children()
        if not self._rollup_items:
            box.display = False
            return
        box.display = True
        total = len(self._rollup_items)
        header = Static(
            f"[dim]\u2500\u2500 items ({total}) \u2500\u2500[/dim]",
            classes="rollup-items-header",
            markup=True,
        )
        box.mount(header)
        visible_count = (
            total if self._rollup_show_all
            else min(self.ROLLUP_DEFAULT_VISIBLE, total)
        )
        for idx in range(visible_count):
            row = _RollupItem(
                index=idx,
                item=self._rollup_items[idx],
                expanded=idx in self._rollup_expanded,
                focused=self._rollup_focused_index == idx,
            )
            box.mount(row)
        if visible_count < total:
            remaining = total - visible_count
            overflow = Static(
                f"[dim]\u2026 {remaining} more \u2014 press [b]e[/b] to expand all[/dim]",
                classes="rollup-overflow",
                markup=True,
            )
            box.mount(overflow)

    def toggle_rollup_item(self, index: int) -> None:
        """Toggle expansion of the rollup item at ``index``."""
        if not (0 <= index < len(self._rollup_items)):
            return
        if index in self._rollup_expanded:
            self._rollup_expanded.discard(index)
        else:
            self._rollup_expanded.add(index)
        self._rollup_focused_index = index
        self._mount_rollup_widgets()

    def action_expand_all_rollup(self) -> None:
        """When the selected task is a rollup, reveal every item."""
        if not self._rollup_items or self._rollup_show_all:
            return
        self._rollup_show_all = True
        self._mount_rollup_widgets()

    # ------------------------------------------------------------------
    # Selection / navigation
    # ------------------------------------------------------------------

    def _sync_selection_from_list(self) -> None:
        idx = self.list_view.index
        if idx is None or idx < 0 or idx >= len(self._tasks):
            return
        task = self._tasks[idx]
        if task.task_id == self._selected_task_id:
            return
        self._selected_task_id = task.task_id
        # Clear any in-progress reply draft when the selection changes so
        # a half-typed message doesn't get posted to a different task.
        if self.reply_input.value:
            self.reply_input.value = ""
        self._render_detail(task.task_id)
        self._mark_open_read(task.task_id, idx)

    def action_cursor_down(self) -> None:
        if self.reply_input.has_focus:
            return
        self.list_view.action_cursor_down()
        self._sync_selection_from_list()

    def action_cursor_up(self) -> None:
        if self.reply_input.has_focus:
            return
        self.list_view.action_cursor_up()
        self._sync_selection_from_list()

    def action_cursor_first(self) -> None:
        if self._tasks:
            self.list_view.index = 0
            self._sync_selection_from_list()

    def action_cursor_last(self) -> None:
        if self._tasks:
            self.list_view.index = len(self._tasks) - 1
            self._sync_selection_from_list()

    def action_open_selected(self) -> None:
        self._sync_selection_from_list()

    def action_refresh(self) -> None:
        self._refresh_list(select_first=False)

    def action_back_or_cancel(self) -> None:
        """Esc/q returns focus to the list from the reply box, else exits."""
        if self.reply_input.has_focus:
            # Return focus to the list so j/k works again. Don't exit the
            # app — the reply input is always present on the detail pane.
            self.list_view.focus()
            return
        self.exit()

    @on(ListView.Selected, "#inbox-list")
    def _on_row_selected(self, event: ListView.Selected) -> None:
        row = event.item
        if not isinstance(row, _InboxListItem):
            return
        self._selected_task_id = row.task_id
        self._render_detail(row.task_id)
        idx = self.list_view.index or 0
        self._mark_open_read(row.task_id, idx)

    @on(ListView.Highlighted, "#inbox-list")
    def _on_row_highlighted(self, event: ListView.Highlighted) -> None:
        # Keyboard j/k emits Highlighted before any Selected; render eagerly
        # so the right pane tracks the cursor without requiring Enter.
        row = event.item
        if not isinstance(row, _InboxListItem):
            return
        if self._selected_task_id == row.task_id:
            return
        self._selected_task_id = row.task_id
        if self.reply_input.value:
            self.reply_input.value = ""
        self._render_detail(row.task_id)

    # ------------------------------------------------------------------
    # Read / archive / reply actions
    # ------------------------------------------------------------------

    def _mark_open_read(self, task_id: str, row_index: int) -> None:
        if task_id not in self._unread_ids:
            return
        svc = self._svc_for_task(task_id)
        if svc is None:
            return
        wrote = False
        try:
            wrote = svc.mark_read(task_id, actor="user")
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                svc.close()
            except Exception:  # noqa: BLE001
                pass
        if wrote:
            self._emit_event(
                task_id, "inbox.message.read", f"user opened {task_id}",
            )
        # Clear unread styling regardless — the row should update even if
        # the underlying mark_read raced.
        self._unread_ids.discard(task_id)
        try:
            children = list(self.list_view.children)
            if 0 <= row_index < len(children):
                row = children[row_index]
                if isinstance(row, _InboxListItem):
                    row.mark_read()
        except Exception:  # noqa: BLE001
            pass

    def action_archive_selected(self) -> None:
        task_id = self._selected_task_id
        if task_id is None:
            return
        # Improvement proposals force an explicit Accept (A) or Reject
        # (X). Plain ``a`` must not silently archive them — the user
        # gets a visible warning instead so they don't accidentally
        # drop a suggestion without recording the decision.
        task, _labels = self._selected_proposal_task()
        if task is not None:
            self.notify(
                "This is an improvement proposal \u2014 press A to accept or X to reject.",
                severity="warning", timeout=3.0,
            )
            return
        svc = self._svc_for_task(task_id)
        if svc is None:
            self.notify("Could not open project database.", severity="error")
            return
        try:
            svc.archive_task(task_id, actor="user")
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Archive failed: {exc}", severity="error")
            return
        finally:
            try:
                svc.close()
            except Exception:  # noqa: BLE001
                pass
        self._emit_event(
            task_id, "inbox.message.archived", f"user archived {task_id}",
        )
        self.notify(f"Archived {task_id}", severity="information", timeout=2.0)
        # Remove from local state + list so the row disappears immediately
        # (the 8s background refresh would do it anyway, but snappy UX).
        self._tasks = [t for t in self._tasks if t.task_id != task_id]
        self._unread_ids.discard(task_id)
        if self._selected_task_id == task_id:
            self._selected_task_id = None
        self._render_list(select_first=bool(self._tasks))

    def action_start_reply(self) -> None:
        """Keyboard shortcut: focus the always-visible reply input."""
        task_id = self._selected_task_id
        if task_id is None:
            return
        self.reply_input.focus()

    # ------------------------------------------------------------------
    # Jump to PM discussion (d)
    # ------------------------------------------------------------------

    def action_jump_to_pm(self) -> None:
        """Navigate the cockpit right pane to this task's PM session.

        * If focus is inside the reply Input, the action is a no-op —
          Sam's mid-draft and `d` is just a character in his message.
        * Resolves the PM persona via :func:`_resolve_pm_target`, routes
          the cockpit to that window, and injects a context line via
          ``tmux send-keys`` without pressing Enter — Sam finishes the
          follow-up himself.
        * When a rollup sub-item is currently focused, the context line
          carries that sub-item's project + subject instead of the
          rollup's own title so the discussion lands in the right PM.
        """
        if self.reply_input.has_focus:
            return
        task_id = self._selected_task_id
        if task_id is None:
            return
        svc = self._svc_for_task(task_id)
        if svc is None:
            self.notify("Could not open project database.", severity="error")
            return
        try:
            task = svc.get(task_id)
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Could not load task: {exc}", severity="error")
            svc.close()
            return
        finally:
            try:
                svc.close()
            except Exception:  # noqa: BLE001
                pass

        # Determine which item (rollup sub-item vs the rollup itself)
        # we're discussing, and derive the project the PM serves.
        focus_item: dict | None = None
        project_for_pm = task.project
        if self._rollup_items and self._rollup_focused_index is not None:
            idx = self._rollup_focused_index
            if 0 <= idx < len(self._rollup_items):
                focus_item = self._rollup_items[idx]
                sub_project = (focus_item.get("source_project") or "").strip()
                if sub_project:
                    project_for_pm = sub_project

        cockpit_key, pm_label = _resolve_pm_target(
            self.config_path, project_for_pm,
        )
        context_line = _build_pm_context_line(task, item=focus_item)

        # Do the cockpit navigation + tmux send-keys in a worker so a
        # slow tmux call doesn't freeze the TUI. The actual work is
        # pure subprocess invocations — safe off the UI thread.
        self.run_worker(
            lambda: self._dispatch_to_pm_sync(
                cockpit_key, context_line, pm_label,
            ),
            thread=True,
            exclusive=True,
            group="jump_to_pm",
        )

    def _dispatch_to_pm_sync(
        self, cockpit_key: str, context_line: str, pm_label: str,
    ) -> None:
        """Worker-thread body: route cockpit + inject the context line."""
        try:
            self._perform_pm_dispatch(cockpit_key, context_line)
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(
                self.notify, f"Jump to PM failed: {exc}", severity="error",
            )
            return
        self.call_from_thread(
            self.notify,
            f"Jumped to {pm_label} \u2014 finish your message and hit Enter.",
            severity="information",
            timeout=3.0,
        )

    def _perform_pm_dispatch(self, cockpit_key: str, context_line: str) -> None:
        """Actually route to the PM pane and ``send-keys`` the context line.

        Separated so tests can patch/record the call without spinning up
        a real tmux server. See ``test_cockpit_inbox_ui.py`` for the
        monkeypatch target.
        """
        router = CockpitRouter(self.config_path)
        router.route_selected(cockpit_key)
        supervisor = router._load_supervisor()
        window_target = (
            f"{supervisor.config.project.tmux_session}:{router._COCKPIT_WINDOW}"
        )
        right_pane = router._right_pane_id(window_target)
        if right_pane is None:
            # Fall back to the window target — tmux resolves to the
            # active pane, which is almost always the right pane for
            # cockpit flows post-route.
            router.tmux.send_keys(window_target, context_line, press_enter=False)
            return
        router.tmux.send_keys(right_pane, context_line, press_enter=False)

    @on(events.Click, ".rollup-item")
    def _on_rollup_item_click(self, event: events.Click) -> None:
        """Click on a rollup sub-item toggles expansion + focuses it."""
        row = event.widget
        # The click can land on the inner Static — walk up until we
        # find the _RollupItem wrapper.
        while row is not None and not isinstance(row, _RollupItem):
            row = getattr(row, "parent", None)
        if not isinstance(row, _RollupItem):
            return
        self.toggle_rollup_item(row.index)

    @on(Input.Submitted, "#inbox-reply")
    def _on_reply_submitted(self, event: Input.Submitted) -> None:
        body = (event.value or "").strip()
        task_id = self._selected_task_id
        # Rejection path: when the user pressed ``X`` on a proposal, the
        # shared reply input was repurposed to collect a rationale. Route
        # submission through the rejection flow instead of add_reply so
        # the bytes land in planner memory + the task archives with the
        # ``proposal_rejected`` entry_type.
        if self._awaiting_rejection_task_id is not None:
            pending = self._awaiting_rejection_task_id
            self._awaiting_rejection_task_id = None
            self.reply_input.placeholder = (
                "Reply \u2026 (Enter to send, Esc back to list)"
            )
            if not task_id or task_id != pending:
                # Selection moved while typing — abandon the rejection.
                self.reply_input.value = ""
                self.list_view.focus()
                return
            self._finish_reject_proposal(task_id, body)
            return
        if not body or not task_id:
            # Empty submit — just hand focus back to the list.
            self.reply_input.value = ""
            self.list_view.focus()
            return
        svc = self._svc_for_task(task_id)
        if svc is None:
            self.notify("Could not open project database.", severity="error")
            self.reply_input.value = ""
            self.list_view.focus()
            return
        try:
            svc.add_reply(task_id, body, actor="user")
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Reply failed: {exc}", severity="error")
            self.reply_input.value = ""
            self.list_view.focus()
            return
        finally:
            try:
                svc.close()
            except Exception:  # noqa: BLE001
                pass
        self._emit_event(
            task_id, "inbox.reply_received", f"user replied to {task_id}: {body[:60]}",
        )
        # Clear the input and re-render so the new reply appears in-thread.
        # Keep focus on the list so j/k works without further keystrokes.
        self.reply_input.value = ""
        self.list_view.focus()
        self._render_detail(task_id)

    # ------------------------------------------------------------------
    # Improvement proposals (#275) — Accept / Reject
    # ------------------------------------------------------------------

    _DEFAULT_HINT = (
        "j/k move \u00b7 \u21b5 open \u00b7 r reply \u00b7 a archive "
        "\u00b7 d discuss \u00b7 u refresh \u00b7 q back"
    )
    _PROPOSAL_HINT = (
        "A accept \u00b7 X reject \u00b7 r reply \u00b7 q back"
    )

    def _update_hint_for_proposal(self) -> None:
        try:
            self.hint.update(self._PROPOSAL_HINT)
        except Exception:  # noqa: BLE001
            pass

    def _restore_default_hint(self) -> None:
        try:
            self.hint.update(self._DEFAULT_HINT)
        except Exception:  # noqa: BLE001
            pass

    def _selected_proposal_task(self):
        """Return (task, labels) for the current selection if it's a proposal."""
        task_id = self._selected_task_id
        if task_id is None:
            return None, []
        svc = self._svc_for_task(task_id)
        if svc is None:
            return None, []
        try:
            task = svc.get(task_id)
        except Exception:  # noqa: BLE001
            return None, []
        finally:
            try:
                svc.close()
            except Exception:  # noqa: BLE001
                pass
        labels = list(getattr(task, "labels", []) or [])
        if "proposal" not in labels:
            return None, labels
        return task, labels

    def action_accept_proposal(self) -> None:
        """Accept the selected proposal — create a follow-on task + archive."""
        if self.reply_input.has_focus:
            return
        task_id = self._selected_task_id
        if task_id is None:
            return
        task, labels = self._selected_proposal_task()
        if task is None:
            self.notify(
                "Accept/Reject only applies to proposal items.",
                severity="warning", timeout=2.0,
            )
            return
        spec = self._proposal_specs.get(task_id) or _extract_proposal_spec(
            task, labels=labels,
        )
        project_key = task.project
        from pollypm.plugins_builtin.project_planning.proposals import (
            accept_proposal as _accept_helper,
        )
        svc = self._svc_for_task(task_id)
        if svc is None:
            self.notify("Could not open project database.", severity="error")
            return
        try:
            new_task = _accept_helper(
                svc,
                task_id=task_id,
                proposal_spec=spec,
                project_key=project_key,
                actor="user",
            )
            svc.add_context(
                task_id,
                actor="user",
                text=f"Proposal accepted \u2192 {new_task.task_id}",
                entry_type="proposal_accepted",
            )
            svc.archive_task(task_id, actor="user")
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Accept failed: {exc}", severity="error")
            return
        finally:
            try:
                svc.close()
            except Exception:  # noqa: BLE001
                pass
        self._emit_event(
            task_id, "inbox.proposal.accepted",
            f"user accepted proposal {task_id} \u2192 {new_task.task_id}",
        )
        self.notify(
            f"Accepted \u2014 created {new_task.task_id}",
            severity="information", timeout=3.0,
        )
        # Drop the archived row locally so it disappears without waiting
        # for the next background refresh.
        self._tasks = [t for t in self._tasks if t.task_id != task_id]
        self._unread_ids.discard(task_id)
        self._proposal_specs.pop(task_id, None)
        if self._selected_task_id == task_id:
            self._selected_task_id = None
        self._render_list(select_first=bool(self._tasks))
        self._restore_default_hint()

    def action_reject_proposal(self) -> None:
        """Reject the selected proposal — prompt for a rationale first."""
        if self.reply_input.has_focus:
            return
        task_id = self._selected_task_id
        if task_id is None:
            return
        task, _labels = self._selected_proposal_task()
        if task is None:
            self.notify(
                "Accept/Reject only applies to proposal items.",
                severity="warning", timeout=2.0,
            )
            return
        self._awaiting_rejection_task_id = task_id
        self.reply_input.value = ""
        self.reply_input.placeholder = (
            "Why reject? (Enter to confirm, Esc to cancel)"
        )
        self.reply_input.focus()

    def _finish_reject_proposal(self, task_id: str, rationale: str) -> None:
        """Persist rejection in planner memory + archive the inbox row."""
        task, labels = self._selected_proposal_task()
        if task is None:
            # Selection moved to a non-proposal item mid-typing.
            self.reply_input.value = ""
            self.list_view.focus()
            return
        from pollypm.plugins_builtin.project_planning.memory import (
            record_proposal_rejection,
        )
        from pollypm.plugins_builtin.project_planning.proposals import (
            memkey_from_labels,
        )
        memkey = memkey_from_labels(labels) or ""
        project_key = task.project
        try:
            record_proposal_rejection(
                project_key=project_key,
                planner_memory_key=memkey,
                rationale=rationale,
            )
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Reject memory failed: {exc}", severity="error")
            # Still try to archive so the user isn't stuck looking at
            # the rejected proposal.
        svc = self._svc_for_task(task_id)
        if svc is None:
            self.notify("Could not open project database.", severity="error")
            self.reply_input.value = ""
            self.list_view.focus()
            return
        try:
            svc.add_context(
                task_id,
                actor="user",
                text=f"Proposal rejected: {rationale[:200]}" if rationale else
                     "Proposal rejected (no rationale given).",
                entry_type="proposal_rejected",
            )
            svc.archive_task(task_id, actor="user")
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Archive after reject failed: {exc}", severity="error")
            self.reply_input.value = ""
            self.list_view.focus()
            return
        finally:
            try:
                svc.close()
            except Exception:  # noqa: BLE001
                pass
        self._emit_event(
            task_id, "inbox.proposal.rejected",
            f"user rejected proposal {task_id}",
        )
        self.notify("Rejected \u2014 planner will skip this next time.",
                    severity="information", timeout=3.0)
        self._tasks = [t for t in self._tasks if t.task_id != task_id]
        self._unread_ids.discard(task_id)
        self._proposal_specs.pop(task_id, None)
        if self._selected_task_id == task_id:
            self._selected_task_id = None
        self.reply_input.value = ""
        self.list_view.focus()
        self._render_list(select_first=bool(self._tasks))
        self._restore_default_hint()

    # ------------------------------------------------------------------
    # Event emission
    # ------------------------------------------------------------------

    def _emit_event(self, task_id: str, event_type: str, message: str) -> None:
        """Record an activity-feed event on the project-root state.db.

        Matches the shape used by ``pm notify`` so the same consumers see
        inbox.read / archived / reply alongside inbox.message.created.
        Fire-and-forget — we never block the UI on event bookkeeping.
        """
        try:
            from pollypm.storage.state import StateStore
            project_key = task_id.split("/", 1)[0]
            config = load_config(self.config_path)
            project = config.projects.get(project_key)
            if project is None:
                return
            db_path = project.path / ".pollypm" / "state.db"
            if not db_path.exists():
                return
            store = StateStore(db_path)
            try:
                store.record_event("cockpit", event_type, message)
            finally:
                store.close()
        except Exception:  # noqa: BLE001
            pass


def _escape(s: str) -> str:
    """Escape Rich markup brackets in a short span of text."""
    if not s:
        return ""
    return str(s).replace("[", r"\[").replace("]", r"\]")


def _escape_body(s: str) -> str:
    """Escape Rich brackets for body text while preserving newlines.

    ``_md_to_rich`` re-adds its own markup; we only need to neutralise
    user-typed brackets so they render as literal characters.
    """
    if not s:
        return ""
    return str(s).replace("[", r"\[").replace("]", r"\]")


# ---------------------------------------------------------------------------
# Per-project dashboard (Textual screen) — #245 follow-up, replaces the
# read-only Static text dump that ``kind == "project"`` used to render
# via ``PollyCockpitPaneApp``.
# ---------------------------------------------------------------------------


_PLAN_FILE_CANDIDATES: tuple[str, ...] = (
    "docs/plan/plan.md",
    "docs/project-plan.md",
)

# Candidate locations for the plan-review HTML explainer; first hit wins.
_PLAN_EXPLAINER_CANDIDATES_FMT: tuple[str, ...] = (
    "reports/plan-review.html",
    "reports/{key}-plan-review.html",
)


def _dashboard_plan_path(project_path: Path) -> Path | None:
    for rel in _PLAN_FILE_CANDIDATES:
        p = project_path / rel
        if p.is_file():
            return p
    return None


def _dashboard_plan_explainer(project_path: Path, project_key: str) -> Path | None:
    for rel in _PLAN_EXPLAINER_CANDIDATES_FMT:
        p = project_path / rel.format(key=project_key)
        if p.is_file():
            return p
    return None


def _extract_h2_sections(md_text: str, *, limit: int = 12) -> list[str]:
    """Extract level-2 markdown headers (``## Title``) from plan text."""
    out: list[str] = []
    for line in md_text.splitlines():
        if line.startswith("## ") and not line.startswith("### "):
            title = line[3:].strip()
            if title:
                out.append(title)
            if len(out) >= limit:
                break
    return out


def _format_relative_age(value) -> str:
    """Relative-age formatting that tolerates missing / malformed inputs.

    Accepts either an ISO-8601 string or a :class:`datetime` instance
    (the work service surfaces both shapes depending on the call site).
    """
    if not value:
        return ""
    from datetime import datetime as _dt
    iso_str: str
    if isinstance(value, _dt):
        iso_str = value.isoformat()
    else:
        iso_str = str(value)
    try:
        from pollypm.tz import format_relative
        return format_relative(iso_str)
    except Exception:  # noqa: BLE001
        return iso_str[:16]


def _dashboard_status(
    active_worker: dict | None,
    inbox_count: int,
    alert_count: int,
    idle_minutes: float | None,
) -> tuple[str, str, str]:
    """Return (dot, colour, label) for the top-bar project status light.

    * Green — a worker is heartbeat-alive on this project right now.
    * Yellow — the user has inbox items or actionable alerts on this
      project (nothing in flight but attention is required).
    * Dim — idle / no activity.
    """
    if active_worker is not None:
        return ("\u25cf", "#3ddc84", "active")
    if inbox_count or alert_count:
        return ("\u25c6", "#f0c45a", "needs attention")
    return ("\u25cb", "#4a5568", "idle")


class ProjectDashboardData:
    """Snapshot of everything the dashboard renders — cached per tick.

    Constructed off the UI thread via :func:`_gather_project_dashboard`;
    the dashboard app holds the resulting object and reads fields for
    each section. Keep this *data-only* — no rendering — so tests can
    poke individual attributes without mounting a Textual screen.
    """

    __slots__ = (
        "project_key",
        "project_name",
        "project_path",
        "persona_name",
        "pm_label",
        "exists_on_disk",
        "status_dot",
        "status_color",
        "status_label",
        "active_worker",
        "architect",
        "task_counts",
        "task_buckets",
        "plan_path",
        "plan_sections",
        "plan_explainer",
        "activity_entries",
        "inbox_count",
        "inbox_top",
        "alert_count",
    )

    def __init__(
        self,
        *,
        project_key: str,
        project_name: str,
        project_path: Path | None,
        persona_name: str | None,
        pm_label: str,
        exists_on_disk: bool,
        status_dot: str,
        status_color: str,
        status_label: str,
        active_worker: dict | None,
        architect: dict | None,
        task_counts: dict[str, int],
        task_buckets: dict[str, list[dict]],
        plan_path: Path | None,
        plan_sections: list[str],
        plan_explainer: Path | None,
        activity_entries: list[dict],
        inbox_count: int,
        inbox_top: list[dict],
        alert_count: int,
    ) -> None:
        self.project_key = project_key
        self.project_name = project_name
        self.project_path = project_path
        self.persona_name = persona_name
        self.pm_label = pm_label
        self.exists_on_disk = exists_on_disk
        self.status_dot = status_dot
        self.status_color = status_color
        self.status_label = status_label
        self.active_worker = active_worker
        self.architect = architect
        self.task_counts = task_counts
        self.task_buckets = task_buckets
        self.plan_path = plan_path
        self.plan_sections = plan_sections
        self.plan_explainer = plan_explainer
        self.activity_entries = activity_entries
        self.inbox_count = inbox_count
        self.inbox_top = inbox_top
        self.alert_count = alert_count


# Module-level cache keyed by (project_key, db_mtime) so a rapidly-
# rerendering dashboard doesn't hammer SQLite for the same data. The
# dashboard refreshes every 10s by default; stale-cache hits are a net
# win there too.
_PROJECT_DASHBOARD_TASK_CACHE: dict[str, tuple[float, dict[str, int], dict[str, list[dict]]]] = {}


def _dashboard_gather_tasks(
    project_key: str, project_path: Path,
) -> tuple[dict[str, int], dict[str, list[dict]]]:
    """Fetch task counts + top-N titles per status bucket for a project.

    Uses the same mtime-cache trick as ``_dashboard_project_tasks`` so
    the overall dashboard tick stays cheap when the work service has
    no new writes. Only small dict views of each task are cached (never
    full ``Task`` objects) to keep the cache footprint bounded.
    """
    db_path = project_path / ".pollypm" / "state.db"
    if not db_path.exists():
        return {}, {}
    try:
        db_mtime = db_path.stat().st_mtime
    except OSError:
        return {}, {}
    cached = _PROJECT_DASHBOARD_TASK_CACHE.get(project_key)
    if cached is not None and cached[0] == db_mtime:
        return cached[1], cached[2]

    from pollypm.work.sqlite_service import SQLiteWorkService

    buckets: dict[str, list[dict]] = {
        "queued": [],
        "in_progress": [],
        "review": [],
        "blocked": [],
        "done": [],
    }
    counts: dict[str, int] = {}
    try:
        with SQLiteWorkService(db_path=db_path, project_path=project_path) as svc:
            counts = svc.state_counts(project=project_key)
            tasks = svc.list_tasks(project=project_key)
            for t in tasks:
                status = getattr(t.work_status, "value", "")
                if status not in buckets:
                    continue
                updated_at = getattr(t, "updated_at", "") or ""
                # Normalise to ISO-8601 string — task.updated_at comes
                # back as datetime from the work-service hydrator.
                if hasattr(updated_at, "isoformat"):
                    updated_at = updated_at.isoformat()
                buckets[status].append(
                    {
                        "task_id": t.task_id,
                        "task_number": getattr(t, "task_number", None),
                        "title": getattr(t, "title", "") or "(untitled)",
                        "updated_at": updated_at,
                        "assignee": getattr(t, "assignee", None),
                        "current_node_id": getattr(t, "current_node_id", None),
                    }
                )
    except Exception:  # noqa: BLE001
        return {}, {}

    for status, items in buckets.items():
        items.sort(key=lambda d: d["updated_at"] or "", reverse=True)

    _PROJECT_DASHBOARD_TASK_CACHE[project_key] = (db_mtime, counts, buckets)
    return counts, buckets


def _dashboard_active_worker(
    config_path: Path, project_key: str,
) -> tuple[dict | None, int]:
    """Inspect supervisor state for a live worker on this project.

    Returns ``(worker_info, alert_count)`` where ``worker_info`` is
    ``None`` when no worker is currently heartbeat-alive. ``alert_count``
    counts actionable alerts scoped to this project's sessions so the
    top bar can render the yellow "needs attention" light even when the
    worker is idle.
    """
    from datetime import UTC, datetime, timedelta

    worker_info: dict | None = None
    alert_count = 0
    try:
        supervisor = PollyPMService(config_path).load_supervisor()
    except Exception:  # noqa: BLE001
        return None, 0
    try:
        try:
            launches = list(supervisor.plan_launches())
        except Exception:  # noqa: BLE001
            launches = []
        project_sessions = [
            l.session for l in launches
            if getattr(l.session, "project", None) == project_key
            and getattr(l.session, "role", "") != "operator-pm"
        ]
        alive_cutoff = datetime.now(UTC) - timedelta(minutes=5)
        for sess in project_sessions:
            try:
                hb = supervisor.store.latest_heartbeat(sess.name)
            except Exception:  # noqa: BLE001
                continue
            if hb is None:
                continue
            try:
                dt = datetime.fromisoformat(hb.created_at)
            except (ValueError, TypeError):
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            if dt > alive_cutoff and not getattr(hb, "pane_dead", False):
                worker_info = {
                    "session_name": sess.name,
                    "role": getattr(sess, "role", "worker"),
                    "last_heartbeat": hb.created_at,
                }
                break
        # Actionable alerts for this project's sessions.
        try:
            project_session_names = {s.name for s in project_sessions}
            open_alerts = supervisor.store.open_alerts()
            alert_count = sum(
                1 for a in open_alerts
                if getattr(a, "session_name", None) in project_session_names
                and getattr(a, "alert_type", "") not in (
                    "suspected_loop", "stabilize_failed", "needs_followup",
                )
            )
        except Exception:  # noqa: BLE001
            alert_count = 0
    finally:
        try:
            supervisor.store.close()
        except Exception:  # noqa: BLE001
            pass
    return worker_info, alert_count


def _dashboard_inbox(
    config_path: Path, project_key: str, project_path: Path,
) -> tuple[int, list[dict]]:
    """Count inbox tasks for this project and return a top-3 preview."""
    db_path = project_path / ".pollypm" / "state.db"
    if not db_path.exists():
        return 0, []
    try:
        from pollypm.work.inbox_view import inbox_tasks
        from pollypm.work.sqlite_service import SQLiteWorkService
    except Exception:  # noqa: BLE001
        return 0, []
    try:
        with SQLiteWorkService(
            db_path=db_path, project_path=project_path,
        ) as svc:
            tasks = inbox_tasks(svc, project=project_key)
    except Exception:  # noqa: BLE001
        return 0, []
    top: list[dict] = []
    for t in tasks[:3]:
        updated_at = getattr(t, "updated_at", "") or ""
        if hasattr(updated_at, "isoformat"):
            updated_at = updated_at.isoformat()
        top.append(
            {
                "task_id": t.task_id,
                "title": getattr(t, "title", "") or "(untitled)",
                "updated_at": updated_at,
            }
        )
    return len(tasks), top


def _dashboard_activity(
    config_path: Path, project_key: str, *, limit: int = 10,
) -> list[dict]:
    """Fetch the last ``limit`` activity-feed entries for this project.

    Returns lightweight dicts (not ``FeedEntry``) so the dashboard's
    cache + tests can reason about shape without pulling in the
    projector's import graph.
    """
    try:
        from pollypm.plugins_builtin.activity_feed.plugin import build_projector
    except Exception:  # noqa: BLE001
        return []
    try:
        config = load_config(config_path)
    except Exception:  # noqa: BLE001
        return []
    projector = build_projector(config)
    if projector is None:
        return []
    try:
        entries = projector.project(projects=[project_key], limit=limit)
    except Exception:  # noqa: BLE001
        return []
    return [
        {
            "timestamp": e.timestamp,
            "actor": e.actor or "",
            "verb": e.verb or "",
            "summary": e.summary or "",
            "kind": e.kind or "",
        }
        for e in entries
    ]


def _gather_project_dashboard(
    config_path: Path, project_key: str,
) -> ProjectDashboardData | None:
    """Build the full ``ProjectDashboardData`` snapshot for one project."""
    try:
        config = load_config(config_path)
    except Exception:  # noqa: BLE001
        return None
    projects = getattr(config, "projects", {}) or {}
    project = projects.get(project_key)
    if project is None:
        return None

    project_path = getattr(project, "path", None)
    name = (
        project.display_label() if hasattr(project, "display_label")
        else (getattr(project, "name", None) or project_key)
    )
    persona = getattr(project, "persona_name", None)
    pm_label = f"PM: {persona}" if (isinstance(persona, str) and persona.strip()) else "PM: Polly"

    exists_on_disk = bool(
        project_path is not None
        and isinstance(project_path, Path)
        and project_path.exists()
    )

    if exists_on_disk:
        counts, buckets = _dashboard_gather_tasks(project_key, project_path)
        inbox_count, inbox_top = _dashboard_inbox(
            config_path, project_key, project_path,
        )
        plan_path = _dashboard_plan_path(project_path)
        if plan_path is not None:
            try:
                plan_sections = _extract_h2_sections(
                    plan_path.read_text(encoding="utf-8"),
                )
            except OSError:
                plan_sections = []
        else:
            plan_sections = []
        plan_explainer = _dashboard_plan_explainer(project_path, project_key)
        activity_entries = _dashboard_activity(config_path, project_key)
    else:
        counts = {}
        buckets = {}
        inbox_count = 0
        inbox_top = []
        plan_path = None
        plan_sections = []
        plan_explainer = None
        activity_entries = []

    active_worker, alert_count = _dashboard_active_worker(
        config_path, project_key,
    )

    status_dot, status_color, status_label = _dashboard_status(
        active_worker, inbox_count, alert_count, None,
    )

    return ProjectDashboardData(
        project_key=project_key,
        project_name=name,
        project_path=project_path if exists_on_disk else None,
        persona_name=persona if isinstance(persona, str) else None,
        pm_label=pm_label,
        exists_on_disk=exists_on_disk,
        status_dot=status_dot,
        status_color=status_color,
        status_label=status_label,
        active_worker=active_worker,
        architect=None,  # stage info not yet wired — reserved for future
        task_counts=counts,
        task_buckets=buckets,
        plan_path=plan_path,
        plan_sections=plan_sections,
        plan_explainer=plan_explainer,
        activity_entries=activity_entries,
        inbox_count=inbox_count,
        inbox_top=inbox_top,
        alert_count=alert_count,
    )


class PollyProjectDashboardApp(App[None]):
    """Information-dense per-project dashboard — replaces the legacy
    text dump rendered when the user selects a project in the rail.

    Opened via ``pm cockpit-pane project <project_key>``. See issue #245
    for the design intent.
    """

    TITLE = "PollyPM"
    SUB_TITLE = "Project"
    REFRESH_INTERVAL_SECONDS = 10

    CSS = """
    Screen {
        background: #0f1317;
        color: #eef2f4;
        padding: 0;
    }
    #proj-outer {
        height: 1fr;
        padding: 1 2;
    }
    #proj-topbar {
        height: 3;
        padding: 0 0 1 0;
        border-bottom: solid #1e2730;
    }
    #proj-status {
        color: #97a6b2;
        padding-top: 0;
    }
    #proj-body {
        height: 1fr;
        padding: 1 0 0 0;
        scrollbar-size: 1 1;
        scrollbar-color: #2a3340;
    }
    .proj-section {
        margin-bottom: 1;
        padding: 1 2;
        background: #111820;
        border: round #1e2730;
    }
    .proj-section-title {
        color: #5b8aff;
        text-style: bold;
        padding-bottom: 1;
    }
    .proj-section-body {
        color: #d6dee5;
    }
    .proj-empty {
        color: #6b7a88;
    }
    #proj-hint {
        height: 1;
        padding: 0 2;
        color: #3e4c5a;
        background: #0c0f12;
    }
    """

    BINDINGS = [
        Binding("c", "chat_pm", "Chat PM"),
        Binding("p", "open_plan", "Plan"),
        Binding("i", "jump_inbox", "Inbox"),
        Binding("l", "jump_activity", "Log"),
        Binding("u,r", "refresh", "Refresh", show=False),
        Binding("q,escape", "back", "Back"),
    ]

    _DEFAULT_HINT = (
        "c chat \u00b7 p plan \u00b7 i inbox \u00b7 l log \u00b7 q back"
    )

    def __init__(self, config_path: Path, project_key: str) -> None:
        super().__init__()
        self.config_path = config_path
        self.project_key = project_key
        self.topbar = Static("", id="proj-topbar", markup=True)
        self.status_line = Static("", id="proj-status", markup=True)
        self.now_title = Static(
            "[b]Current activity[/b]",
            classes="proj-section-title",
            markup=True,
        )
        self.now_body = Static(
            "", classes="proj-section-body", markup=True,
        )
        self.pipeline_title = Static(
            "[b]Task pipeline[/b]",
            classes="proj-section-title",
            markup=True,
        )
        self.pipeline_body = Static(
            "", classes="proj-section-body", markup=True,
        )
        self.plan_title = Static(
            "[b]Plan[/b]", classes="proj-section-title", markup=True,
        )
        self.plan_body = Static(
            "", classes="proj-section-body", markup=True,
        )
        self.activity_title = Static(
            "[b]Recent activity[/b]",
            classes="proj-section-title",
            markup=True,
        )
        self.activity_body = Static(
            "", classes="proj-section-body", markup=True,
        )
        self.inbox_title = Static(
            "[b]Inbox[/b]", classes="proj-section-title", markup=True,
        )
        self.inbox_body = Static(
            "", classes="proj-section-body", markup=True,
        )
        self.hint = Static(
            self._DEFAULT_HINT, id="proj-hint", markup=True,
        )
        self.data: ProjectDashboardData | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="proj-outer"):
            yield self.topbar
            yield self.status_line
            with VerticalScroll(id="proj-body"):
                with Vertical(classes="proj-section"):
                    yield self.now_title
                    yield self.now_body
                with Vertical(classes="proj-section"):
                    yield self.pipeline_title
                    yield self.pipeline_body
                with Vertical(classes="proj-section"):
                    yield self.plan_title
                    yield self.plan_body
                with Vertical(classes="proj-section"):
                    yield self.activity_title
                    yield self.activity_body
                with Vertical(classes="proj-section"):
                    yield self.inbox_title
                    yield self.inbox_body
        yield self.hint

    def on_mount(self) -> None:
        self._refresh()
        self.set_interval(self.REFRESH_INTERVAL_SECONDS, self._refresh)

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        try:
            self.data = _gather_project_dashboard(
                self.config_path, self.project_key,
            )
        except Exception as exc:  # noqa: BLE001
            self.topbar.update(
                f"[#ff5f6d]Error loading project:[/#ff5f6d] {_escape(str(exc))}"
            )
            return
        self._render()

    def _render(self) -> None:
        data = self.data
        if data is None:
            self.topbar.update(
                f"[b]{_escape(self.project_key)}[/b]  "
                f"[dim]not found in config[/dim]"
            )
            return

        # ── Top bar ──
        title = f"[#eef6ff][b]{_escape(data.project_name)}[/b][/#eef6ff]"
        meta = f"[#97a6b2]{_escape(data.pm_label)}[/#97a6b2]"
        self.topbar.update(f"{title}   {meta}")

        status_markup = (
            f"[{data.status_color}]{data.status_dot}[/] "
            f"[#97a6b2]{_escape(data.status_label)}[/]"
        )
        self.status_line.update(status_markup)

        # ── Current activity ──
        self.now_body.update(self._render_now_body(data))

        # ── Task pipeline ──
        self.pipeline_body.update(self._render_pipeline_body(data))

        # ── Plan summary ──
        self.plan_body.update(self._render_plan_body(data))

        # ── Recent activity ──
        self.activity_body.update(self._render_activity_body(data))

        # ── Inbox ──
        self.inbox_body.update(self._render_inbox_body(data))

        self.hint.update(self._DEFAULT_HINT)

    # ------------------------------------------------------------------
    # Section renderers — all return Rich-markup strings, all handle
    # missing-data gracefully with friendly empty-state copy.
    # ------------------------------------------------------------------

    def _render_now_body(self, data: ProjectDashboardData) -> str:
        w = data.active_worker
        if w:
            sess = _escape(w.get("session_name") or "")
            role = _escape(w.get("role") or "worker")
            hb = w.get("last_heartbeat") or ""
            age = _format_relative_age(hb) if hb else ""
            age_part = f"  [dim]{_escape(age)}[/dim]" if age else ""
            lines = [
                f"[#3ddc84]\u25cf[/#3ddc84] "
                f"[b]{sess}[/b]  [dim]{role}[/dim]{age_part}",
            ]
            # Surface the top-most in-flight task as context.
            in_flight = data.task_buckets.get("in_progress", [])
            if in_flight:
                t = in_flight[0]
                num = t.get("task_number")
                num_part = f"#{num} " if num is not None else ""
                title = _escape(t.get("title") or "")
                node = t.get("current_node_id")
                node_part = (
                    f"  [dim]@ {_escape(str(node))}[/dim]" if node else ""
                )
                lines.append(f"  {num_part}{title}{node_part}")
            return "\n".join(lines)
        return "[dim]Idle. No tasks in flight.[/dim]"

    def _render_pipeline_body(self, data: ProjectDashboardData) -> str:
        if not data.exists_on_disk:
            return "[dim]No project path on disk.[/dim]"
        counts = data.task_counts
        buckets = data.task_buckets
        if not counts and not any(buckets.values()):
            return "[dim]No tasks yet. Press N on the rail to start a lane.[/dim]"

        # Compact count strip
        strip_order = [
            ("queued", "#6b7a88", "\u25cb"),
            ("in_progress", "#f0c45a", "\u25c6"),
            ("review", "#5b8aff", "\u25c9"),
            ("done", "#3ddc84", "\u2713"),
        ]
        strip_parts: list[str] = []
        for status, colour, icon in strip_order:
            n = counts.get(status, 0)
            label = status.replace("_", " ")
            strip_parts.append(
                f"[{colour}]{icon}[/] [b]{n}[/b] [dim]{label}[/dim]"
            )
        out = ["  \u00b7  ".join(strip_parts), ""]

        for status, _colour, _icon in strip_order:
            items = buckets.get(status, [])[:3]
            if not items:
                continue
            header = status.replace("_", " ").title()
            out.append(f"[dim]{header}[/dim]")
            for t in items:
                num = t.get("task_number")
                num_part = f"[dim]#{num}[/dim] " if num is not None else ""
                title = _escape(t.get("title") or "")
                age = _format_relative_age(t.get("updated_at") or "")
                age_part = f"  [dim]{_escape(age)}[/dim]" if age else ""
                out.append(f"  {num_part}{title}{age_part}")
            out.append("")
        # Drop trailing blank for tidy spacing
        while out and out[-1] == "":
            out.pop()
        return "\n".join(out)

    def _render_plan_body(self, data: ProjectDashboardData) -> str:
        if not data.exists_on_disk:
            return "[dim]Virtual project — no plan on disk.[/dim]"
        if data.plan_path is None:
            return (
                "[dim]No plan yet. Run [b]pm project plan[/b] "
                "or let it auto-fire.[/dim]"
            )
        lines: list[str] = []
        rel_path = ""
        try:
            rel_path = str(data.plan_path.relative_to(data.project_path))
        except (ValueError, TypeError):
            rel_path = str(data.plan_path.name)
        lines.append(f"[dim]{_escape(rel_path)}[/dim]")
        if data.plan_sections:
            for title in data.plan_sections:
                lines.append(f"  \u25aa {_escape(title)}")
        else:
            lines.append("  [dim](no H2 sections found)[/dim]")
        if data.plan_explainer is not None:
            lines.append("")
            lines.append("[dim]Press [b]v[/b] to open the visual explainer[/dim]")
        return "\n".join(lines)

    def _render_activity_body(self, data: ProjectDashboardData) -> str:
        if not data.activity_entries:
            return "[dim]No recent activity for this project.[/dim]"
        lines: list[str] = []
        for e in data.activity_entries[:10]:
            ts = _format_relative_age(e.get("timestamp") or "")
            actor = _escape(e.get("actor") or "-")
            verb = _escape(e.get("verb") or "")
            summary = _escape(e.get("summary") or "")
            ts_part = f"[dim]{ts:>8}[/dim]" if ts else ""
            line = (
                f"{ts_part}  [#97a6b2]{actor}[/#97a6b2]  "
                f"[b]{verb}[/b] {summary}"
            )
            lines.append(line)
        return "\n".join(lines)

    def _render_inbox_body(self, data: ProjectDashboardData) -> str:
        count = data.inbox_count
        if count == 0:
            return "[dim]Inbox is clear for this project.[/dim]"
        attention_mark = (
            "[#f0c45a]\u25c6[/#f0c45a] " if count else ""
        )
        lines = [
            f"{attention_mark}[b]{count}[/b] "
            f"[dim]open item{'s' if count != 1 else ''}[/dim]"
        ]
        for item in data.inbox_top:
            title = _escape(item.get("title") or "")
            age = _format_relative_age(item.get("updated_at") or "")
            age_part = f"  [dim]{_escape(age)}[/dim]" if age else ""
            lines.append(f"  \u00b7 {title}{age_part}")
        lines.append("")
        lines.append("[dim]Press [b]i[/b] to jump to the inbox[/dim]")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Actions — keybindings
    # ------------------------------------------------------------------

    def action_refresh(self) -> None:
        self._refresh()

    def action_back(self) -> None:
        self.exit()

    def action_chat_pm(self) -> None:
        """Route the cockpit right-pane to this project's PM session.

        Mirrors :meth:`PollyInboxApp.action_jump_to_pm` — resolves the
        persona via :func:`_resolve_pm_target` and uses the same worker
        dispatch so tests can monkeypatch the same hook.
        """
        cockpit_key, pm_label = _resolve_pm_target(
            self.config_path, self.project_key,
        )
        context_line = f're: project/{self.project_key} "dashboard discussion"'
        self.run_worker(
            lambda: self._dispatch_to_pm_sync(
                cockpit_key, context_line, pm_label,
            ),
            thread=True,
            exclusive=True,
            group="proj_jump_to_pm",
        )

    def _dispatch_to_pm_sync(
        self, cockpit_key: str, context_line: str, pm_label: str,
    ) -> None:
        try:
            self._perform_pm_dispatch(cockpit_key, context_line)
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(
                self.notify, f"Jump to PM failed: {exc}", severity="error",
            )
            return
        self.call_from_thread(
            self.notify,
            f"Jumped to {pm_label} \u2014 finish your message and hit Enter.",
            severity="information",
            timeout=3.0,
        )

    def _perform_pm_dispatch(self, cockpit_key: str, context_line: str) -> None:
        """Route the cockpit to the PM pane and inject a context line.

        Split out exactly like the inbox path so the same
        ``monkeypatch`` strategy works for the dashboard's chat keybind.
        """
        router = CockpitRouter(self.config_path)
        router.route_selected(cockpit_key)
        supervisor = router._load_supervisor()
        window_target = (
            f"{supervisor.config.project.tmux_session}:{router._COCKPIT_WINDOW}"
        )
        right_pane = router._right_pane_id(window_target)
        if right_pane is None:
            router.tmux.send_keys(window_target, context_line, press_enter=False)
            return
        router.tmux.send_keys(right_pane, context_line, press_enter=False)

    def action_jump_inbox(self) -> None:
        """Route the cockpit right-pane to the inbox.

        The inbox app itself doesn't expose a project-filter yet, but
        routing still lands Sam where he can act. A lightweight test
        seam (``_route_to_inbox``) keeps this mockable.
        """
        self.run_worker(
            lambda: self._route_to_inbox_sync(),
            thread=True,
            exclusive=True,
            group="proj_inbox",
        )

    def _route_to_inbox_sync(self) -> None:
        try:
            self._route_to_inbox()
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(
                self.notify, f"Jump to inbox failed: {exc}", severity="error",
            )

    def _route_to_inbox(self) -> None:
        router = CockpitRouter(self.config_path)
        router.route_selected("inbox")

    def action_jump_activity(self) -> None:
        """Route to the activity log / feed for the whole workspace."""
        self.run_worker(
            lambda: self._route_to_activity_sync(),
            thread=True,
            exclusive=True,
            group="proj_activity",
        )

    def _route_to_activity_sync(self) -> None:
        try:
            self._route_to_activity()
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(
                self.notify, f"Jump to activity failed: {exc}", severity="error",
            )

    def _route_to_activity(self) -> None:
        router = CockpitRouter(self.config_path)
        router.route_selected("tools:activity")

    def action_open_plan(self) -> None:
        """Surface the plan file path + section list inline (no shell out).

        The body is already rendered in the plan section — pressing ``p``
        scrolls the body into focus and flashes a hint so Sam knows
        where to look. When no plan exists, friendly notify.
        """
        data = self.data
        if data is None or data.plan_path is None:
            self.notify(
                "No plan file yet for this project.",
                severity="warning", timeout=2.0,
            )
            return
        # Scroll the plan section into view (best-effort).
        try:
            self.plan_title.scroll_visible()
        except Exception:  # noqa: BLE001
            pass
        self.notify(
            f"Plan: {data.plan_path}",
            severity="information",
            timeout=3.0,
        )
