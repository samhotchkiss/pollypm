"""Cockpit root UI composition and compatibility exports.

Contract:
- Inputs: config paths, cockpit router state, and screen-specific events
  for the remaining root-owned cockpit surfaces.
- Outputs: Textual apps/screens exported for CLI entry points and tests,
  plus compatibility re-exports for panel modules split into sibling
  ``cockpit_*`` files.
- Side effects: launches cockpit screens, reads config/state on demand,
  and coordinates alert/palette helpers across screens.
- Invariants: panel-specific implementations live in their owning
  ``cockpit_*`` modules; this file keeps the stable import surface while
  holding only the still-unsplit root cockpit screens.
- Allowed dependencies: public cockpit router/build helpers, service
  facade, and sibling cockpit modules.
- Private: root-only screen helpers that still belong to the unsplit
  cockpit shell.
"""

from __future__ import annotations

import gc
import resource
from collections import deque
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
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, ListItem, ListView, Static

from pollypm.models import ProviderKind
from pollypm.tz import format_time as _fmt_time
from pollypm.cockpit_activity import (
    PollyActivityFeedApp,
    _activity_type_colour,
    _format_activity_relative,
    _truncate_summary,
)
from pollypm.cockpit_alerts import (
    AlertNotifier,
    AlertToast,
    _action_view_alerts,
    _setup_alert_notifier,
)
from pollypm.cockpit_metrics import (
    PollyMetricsApp,
    _MetricsDrillDownModal,
    _metrics_process_breakdown,
)
from pollypm.config import load_config
from pollypm.cockpit_palette import (
    CommandPaletteModal,
    KeyboardHelpModal,
    _PaletteListItem,
    _PaletteSectionHeader,
    _dispatch_palette_tag,
    _open_command_palette,
    _open_keyboard_help,
    _palette_nav,
    _palette_history,
    _record_palette_command,
    _resolve_recent_commands,
)
from pollypm.cockpit_workers import PollyWorkerRosterApp
from pollypm.service_api import PollyPMService
from pollypm.cockpit import build_cockpit_detail
from pollypm.cockpit_rail import CockpitItem, CockpitRouter


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
        Binding("a", "view_alerts", "Alerts", show=False),
        Binding("colon", "open_command_palette", "Palette", priority=True),
        Binding("question_mark", "show_keyboard_help", "Help", priority=True),
        Binding("j,down", "cursor_down", "Down", show=False),
        Binding("k,up", "cursor_up", "Up", show=False),
        Binding("g,home", "cursor_first", "First", show=False),
        Binding("G,end", "cursor_last", "Last", show=False),
        Binding("ctrl+q", "request_quit", "Quit", priority=True),
        Binding("ctrl+w", "detach", "Detach", priority=True),
    ]

    def action_open_command_palette(self) -> None:
        _open_command_palette(self)

    def action_show_keyboard_help(self) -> None:
        _open_keyboard_help(self)

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
        # Live alert toasts — non-intrusive bottom-right overlay.
        _setup_alert_notifier(self, bind_a=True)

    def action_view_alerts(self) -> None:
        _action_view_alerts(self)

    def _start_core_rail(self) -> None:
        """Start the process-wide HeartbeatRail via the supervisor, best-effort.

        Skips the start when a headless ``pollypm.rail_daemon`` is
        already running (tracked via ``~/.pollypm/rail_daemon.pid``).
        Otherwise we'd run two HeartbeatRails in parallel — the
        daemon's and the cockpit's — racing each other on the same
        state.db, doubling heartbeat sweeps, and burning CPU in a
        busy-contention loop. That's the failure mode that pinned
        ``pm cockpit`` at ~200% CPU for hours on 2026-04-20.
        """
        if self._rail_daemon_alive():
            return
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

    def _rail_daemon_alive(self) -> bool:
        """True iff the headless rail daemon currently holds its PID file."""
        import os as _os
        from pathlib import Path as _Path

        pid_path = _Path.home() / ".pollypm" / "rail_daemon.pid"
        if not pid_path.exists():
            return False
        try:
            pid = int(pid_path.read_text().strip())
        except (ValueError, OSError):
            return False
        if pid <= 0:
            return False
        try:
            _os.kill(pid, 0)
            return True
        except ProcessLookupError:
            # Stale PID file — let the daemon's own cleanup clear it
            # next run; we just report "not alive" for this boot.
            return False
        except PermissionError:
            # Different user owns the PID — treat as alive from our POV.
            return True

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


# ---------------------------------------------------------------------------
# Settings — interactive Textual screen (rebuild)
# ---------------------------------------------------------------------------

_SETTINGS_SECTIONS: tuple[tuple[str, str], ...] = (
    ("accounts", "Accounts"),
    ("projects", "Projects"),
    ("heartbeat", "Heartbeat & Recovery"),
    ("plugins", "Plugins"),
    ("planner", "Planner"),
    ("inbox", "Inbox & Notifications"),
    ("about", "About"),
)


def _settings_status_dot(health: str, logged_in: bool) -> tuple[str, str]:
    if not logged_in:
        return ("\u25cf", "#ff5f6d")
    h = (health or "").lower()
    if h in ("capacity-exhausted", "auth-broken", "signed-out"):
        return ("\u25cf", "#ff5f6d")
    if h in ("capacity-low", "warning", "degraded"):
        return ("\u25cf", "#f0c45a")
    if h == "healthy":
        return ("\u25cf", "#3ddc84")
    return ("\u25cf", "#6b7a88")


def _settings_dir_size(path: Path) -> int:
    total = 0
    try:
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total


def _humanize_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(n)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{n} B"


class SettingsData:
    """Snapshot of everything the settings screen renders — gathered once."""

    __slots__ = (
        "accounts",
        "projects",
        "heartbeat",
        "plugins",
        "planner",
        "inbox",
        "about",
        "errors",
    )

    def __init__(
        self,
        *,
        accounts: list[dict],
        projects: list[dict],
        heartbeat: list[tuple[str, str]],
        plugins: list[dict],
        planner: list[tuple[str, str]],
        inbox: list[tuple[str, str]],
        about: list[tuple[str, str]],
        errors: list[str],
    ) -> None:
        self.accounts = accounts
        self.projects = projects
        self.heartbeat = heartbeat
        self.plugins = plugins
        self.planner = planner
        self.inbox = inbox
        self.about = about
        self.errors = errors


def _gather_settings_data(
    config_path: Path,
    *,
    service: PollyPMService | None = None,
    account_statuses: list | None = None,
) -> SettingsData:
    """Build a :class:`SettingsData` snapshot in a single pass.

    All fields are loaded once so the cockpit settings pane can render
    instantly without firing per-tick subprocesses (the source of the
    legacy lag). ``service`` and ``account_statuses`` are injection
    hooks for tests.
    """
    errors: list[str] = []
    try:
        config = load_config(config_path)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Config load failed: {exc}")
        config = None

    accounts: list[dict] = []
    if account_statuses is None:
        if service is None:
            service = PollyPMService(config_path)
        try:
            account_statuses = list(service.list_account_statuses())
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Accounts unavailable: {exc}")
            account_statuses = []
    pp = getattr(config, "pollypm", None) if config is not None else None
    ctrl = getattr(pp, "controller_account", "") if pp is not None else ""
    fo_list = (
        list(getattr(pp, "failover_accounts", []) or [])
        if pp is not None else []
    )
    for idx, status in enumerate(account_statuses):
        provider = getattr(status, "provider", None)
        provider_name = (
            getattr(provider, "value", "") if provider is not None else ""
        )
        home = getattr(status, "home", None)
        failover_pos = (
            (fo_list.index(status.key) + 1) if status.key in fo_list else None
        )
        accounts.append(
            {
                "key": status.key,
                "email": getattr(status, "email", "") or "-",
                "provider": provider_name,
                "home": str(home) if home else "",
                "is_controller": status.key == ctrl,
                "failover_pos": failover_pos,
                "logged_in": bool(getattr(status, "logged_in", False)),
                "health": getattr(status, "health", "") or "",
                "plan": getattr(status, "plan", "") or "",
                "usage_summary": getattr(status, "usage_summary", "") or "",
                "usage_raw_text": getattr(status, "usage_raw_text", "") or "",
                "reason": getattr(status, "reason", "") or "",
                "available_at": getattr(status, "available_at", "") or "",
                "access_expires_at": getattr(status, "access_expires_at", "") or "",
                "isolation_status": getattr(status, "isolation_status", "") or "",
                "auth_storage": getattr(status, "auth_storage", "") or "",
                "status_obj": status,
                "index": idx,
            }
        )

    projects: list[dict] = []
    if config is not None:
        from datetime import datetime as _dt
        for key, project in (getattr(config, "projects", {}) or {}).items():
            path = getattr(project, "path", None)
            persona = getattr(project, "persona_name", None)
            path_str = str(path) if path else ""
            tracked = bool(getattr(project, "tracked", False))
            path_exists = False
            task_total = 0
            last_activity = ""
            try:
                if path is not None and path.exists():
                    path_exists = True
                    db_path = path / ".pollypm" / "state.db"
                    if db_path.exists():
                        try:
                            mtime = db_path.stat().st_mtime
                            last_activity = _format_relative_age(
                                _dt.fromtimestamp(mtime).isoformat()
                            )
                        except OSError:
                            last_activity = ""
                        try:
                            from pollypm.work.sqlite_service import SQLiteWorkService
                            with SQLiteWorkService(
                                db_path=db_path, project_path=path,
                            ) as svc:
                                counts = svc.state_counts(project=key)
                                task_total = sum(counts.values())
                        except Exception:  # noqa: BLE001
                            task_total = 0
            except OSError:
                path_exists = False
            projects.append(
                {
                    "key": key,
                    "name": getattr(project, "name", None) or key,
                    "persona": (
                        persona
                        if isinstance(persona, str) and persona.strip()
                        else "Polly"
                    ),
                    "path": path_str,
                    "path_exists": path_exists,
                    "tracked": tracked,
                    "task_total": task_total,
                    "last_activity": last_activity,
                    "project_obj": project,
                }
            )

    heartbeat: list[tuple[str, str]] = []
    if pp is not None:
        failover_accounts = getattr(pp, "failover_accounts", []) or []
        heartbeat = [
            ("Controller account", getattr(pp, "controller_account", "") or "-"),
            ("Failover enabled", "yes" if getattr(pp, "failover_enabled", False) else "no"),
            (
                "Failover order",
                ", ".join(failover_accounts) if failover_accounts else "none",
            ),
            ("Lease timeout", f"{getattr(pp, 'lease_timeout_minutes', 30)} min"),
            ("Heartbeat backend", getattr(pp, "heartbeat_backend", "") or "-"),
            ("Scheduler backend", getattr(pp, "scheduler_backend", "") or "-"),
            (
                "Open permissions",
                "on" if getattr(pp, "open_permissions_by_default", False) else "off",
            ),
            ("Timezone", getattr(pp, "timezone", "") or "(auto-detect)"),
        ]

    plugins: list[dict] = []
    try:
        from pollypm.plugin_host import ExtensionHost
        host = ExtensionHost(
            config_path.parent,
            disabled=tuple(
                getattr(getattr(config, "plugins", None), "disabled", ()) or ()
            ),
        )
        loaded = host.plugins()
        degraded = host.degraded_plugins
        for name, plugin in sorted(loaded.items()):
            source = host.plugin_source(name) or "-"
            status = "degraded" if name in degraded else "loaded"
            plugins.append(
                {
                    "name": name,
                    "version": getattr(plugin, "version", ""),
                    "description": getattr(plugin, "description", "") or "",
                    "source": source,
                    "status": status,
                    "degraded_reason": degraded.get(name, ""),
                }
            )
        for name, record in sorted(host.disabled_plugins.items()):
            plugins.append(
                {
                    "name": name,
                    "version": "",
                    "description": "",
                    "source": getattr(record, "source", "-") or "-",
                    "status": "disabled",
                    "degraded_reason": getattr(record, "reason", "") or "",
                }
            )
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Plugin host unavailable: {exc}")

    planner: list[tuple[str, str]] = []
    pl = getattr(config, "planner", None) if config is not None else None
    if pl is not None:
        planner = [
            (
                "Auto-fire on project created",
                "yes" if getattr(pl, "auto_on_project_created", False) else "no",
            ),
            ("Enforce plan gate", "yes" if getattr(pl, "enforce_plan", False) else "no"),
            ("Plan directory", getattr(pl, "plan_dir", "") or "docs/plan"),
        ]

    inbox_section: list[tuple[str, str]] = []
    project_settings = (
        getattr(config, "project", None) if config is not None else None
    )
    if project_settings is not None:
        ws = getattr(project_settings, "workspace_root", None)
        if ws is not None:
            inbox_section.append(("Workspace root", str(ws)))
        sdb = getattr(project_settings, "state_db", None)
        if sdb is not None:
            inbox_section.append(("Global state DB", str(sdb)))
        logs = getattr(project_settings, "logs_dir", None)
        if logs is not None:
            inbox_section.append(("Logs directory", str(logs)))

    about_section: list[tuple[str, str]] = []
    try:
        from pollypm import __version__ as _pp_version
    except Exception:  # noqa: BLE001
        _pp_version = "unknown"
    import sys as _sys
    about_section.append(("PollyPM version", _pp_version))
    about_section.append(("Python", _sys.version.split()[0]))
    about_section.append(("Config path", str(config_path)))
    if project_settings is not None:
        sdb = getattr(project_settings, "state_db", None)
        if sdb is not None:
            about_section.append(("State DB", str(sdb)))
    pollypm_dir = config_path.parent
    disk = _settings_dir_size(pollypm_dir) if pollypm_dir.exists() else 0
    about_section.append(
        (f"Disk usage ({pollypm_dir.name}/)", _humanize_bytes(disk))
    )

    return SettingsData(
        accounts=accounts,
        projects=projects,
        heartbeat=heartbeat,
        plugins=plugins,
        planner=planner,
        inbox=inbox_section,
        about=about_section,
        errors=errors,
    )


class PollySettingsPaneApp(App[None]):
    """Interactive settings cockpit — fast, sections-based, searchable.

    Layout:
      * Top: status line (controller / permissions / counts).
      * Left: section nav.
      * Right: a DataTable for rowed sections (accounts, projects,
        plugins) or a key/value Static for configuration sections.
        Detail Static under the table shows the selected row's full
        metadata.
      * Bottom: search input (hidden until ``/``) + keybind hint.

    Backwards compatibility: ``self.accounts`` is still the DataTable
    of accounts; ``self.detail`` is the per-account info Static; ``b``
    still toggles permissions; ``self.service`` stays swappable. The
    legacy ``test_settings_pane_renders_accounts_and_toggles_permissions``
    regression test continues to pass.
    """

    TITLE = "PollyPM"
    SUB_TITLE = "Settings"

    CSS = """
    Screen {
        background: #0f1317;
        color: #eef2f4;
        padding: 0;
    }
    #settings-outer {
        height: 1fr;
        padding: 1 2 0 2;
    }
    #settings-topbar {
        height: 1;
        color: #97a6b2;
        padding: 0 0 1 0;
    }
    #settings-body {
        height: 1fr;
    }
    #settings-nav {
        width: 28;
        min-width: 22;
        height: 1fr;
        background: #111820;
        border: round #1e2730;
        padding: 1 1;
        margin-right: 1;
    }
    #settings-nav > .nav-item {
        height: 1;
        padding: 0 1;
        color: #b8c4cf;
    }
    #settings-nav > .nav-item.-selected {
        background: #1e2730;
        color: #eef2f4;
        text-style: bold;
    }
    #settings-nav > .nav-item.-section-active {
        background: #253140;
        color: #f2f6f8;
    }
    #settings-right {
        height: 1fr;
        border: round #1e2730;
        background: #0f1317;
        padding: 1 2;
    }
    #settings-section-title {
        color: #5b8aff;
        text-style: bold;
        padding-bottom: 1;
        height: 1;
    }
    #settings-table-wrap {
        height: 1fr;
    }
    #accounts, #projects-table, #plugins-table {
        height: 1fr;
        background: #0f1317;
    }
    #detail {
        height: auto;
        color: #b8c4cf;
        padding-top: 1;
    }
    #settings-kv {
        height: 1fr;
        color: #d6dee5;
    }
    #settings-search {
        height: 3;
        padding: 0 1;
        margin-top: 1;
        background: #111820;
        border: round #2a3340;
        color: #d6dee5;
        display: none;
    }
    #settings-search.-active {
        display: block;
    }
    #settings-search:focus {
        border: round #5b8aff;
    }
    #settings-hint {
        height: 1;
        padding: 0 2;
        color: #3e4c5a;
        background: #0c0f12;
    }
    """

    BINDINGS = [
        Binding("j,down", "nav_down", "Down", show=False),
        Binding("k,up", "nav_up", "Up", show=False),
        Binding("tab", "section_next", "Next section", show=False, priority=True),
        Binding("shift+tab", "section_prev", "Prev section", show=False, priority=True),
        Binding("]", "section_next", "Next section", show=False),
        Binding("[", "section_prev", "Prev section", show=False),
        Binding("enter", "activate_row", "Open", show=False),
        Binding("slash", "start_search", "Search", show=False),
        Binding("r,u", "refresh", "Refresh"),
        Binding("b", "toggle_permissions", "Permissions"),
        Binding("t", "toggle_project_tracked", "Toggle project", show=False),
        Binding("m", "make_controller", "Controller", show=False),
        Binding("v", "toggle_failover", "Failover", show=False),
        Binding("a", "view_alerts", "Alerts", show=False),
        Binding("question_mark", "show_keyboard_help", "Help", priority=True),
        Binding("q,escape", "back_or_cancel", "Back"),
    ]

    def action_show_keyboard_help(self) -> None:
        _open_keyboard_help(self)

    _DEFAULT_HINT = (
        "j/k move \u00b7 Tab section \u00b7 / search \u00b7 R refresh \u00b7 "
        "b permissions \u00b7 t toggle project \u00b7 q back"
    )

    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self.config_path = config_path
        self.service = PollyPMService(config_path)
        # Widgets
        self.topbar = Static("", id="settings-topbar", markup=True)
        self.nav = Vertical(id="settings-nav")
        self.section_title = Static(
            "", id="settings-section-title", markup=True,
        )
        self.accounts = DataTable(id="accounts")  # backwards-compat name
        self.projects_table = DataTable(id="projects-table")
        self.plugins_table = DataTable(id="plugins-table")
        self.kv_static = Static("", id="settings-kv", markup=True)
        self.detail = Static("", id="detail", markup=True)
        self.search_input = Input(
            placeholder="Filter \u2026 (Enter to apply, Esc to clear)",
            id="settings-search",
        )
        self.hint = Static(self._DEFAULT_HINT, id="settings-hint", markup=True)
        # State
        self.data: SettingsData | None = None
        self._active_section: str = _SETTINGS_SECTIONS[0][0]
        self._search_query: str = ""
        self._nav_widgets: dict[str, Static] = {}
        self._selected_account_key: str | None = None
        self._selected_project_key: str | None = None
        self._nav_cursor: int = 0
        self._focus_target: str = "nav"  # nav | table

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-outer"):
            yield self.topbar
            with Horizontal(id="settings-body"):
                yield self.nav
                with Vertical(id="settings-right"):
                    yield self.section_title
                    with Vertical(id="settings-table-wrap"):
                        yield self.accounts
                        yield self.projects_table
                        yield self.plugins_table
                        yield self.kv_static
                    yield self.detail
                    yield self.search_input
        yield self.hint

    def on_mount(self) -> None:
        # Prepare DataTables once — add_columns fails if called twice.
        self.accounts.cursor_type = "row"
        self.accounts.zebra_stripes = True
        self.accounts.add_columns(
            "", "Key", "Email", "Provider", "Ctrl", "FO", "Usage",
        )
        self.projects_table.cursor_type = "row"
        self.projects_table.zebra_stripes = True
        self.projects_table.add_columns(
            "", "Key", "Name", "PM", "Path", "Tasks", "Last activity",
        )
        self.plugins_table.cursor_type = "row"
        self.plugins_table.zebra_stripes = True
        self.plugins_table.add_columns(
            "Name", "Version", "Source", "Status",
        )

        for key, label in _SETTINGS_SECTIONS:
            item = Static(
                self._nav_label(key, label, count=None),
                classes="nav-item",
                markup=True,
            )
            self._nav_widgets[key] = item
            self.nav.mount(item)

        self._refresh()
        self._show_section(self._active_section)
        # Live alert toasts. Settings has no ``a`` binding so the toast
        # surfaces the full hint.
        _setup_alert_notifier(self, bind_a=True)

    def action_view_alerts(self) -> None:
        _action_view_alerts(self)

    # ------------------------------------------------------------------
    # Data refresh
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        try:
            self.data = _gather_settings_data(
                self.config_path, service=self.service,
            )
        except Exception as exc:  # noqa: BLE001
            self.topbar.update(
                f"[#ff5f6d]Settings load failed:[/] {_escape(str(exc))}"
            )
            return
        self._render_topbar()
        self._render_nav()
        self._render_section(self._active_section)

    def _render_topbar(self) -> None:
        data = self.data
        if data is None:
            self.topbar.update("")
            return
        try:
            config = load_config(self.config_path)
            pp = config.pollypm
            controller = getattr(pp, "controller_account", "") or "-"
            perms = "on" if getattr(pp, "open_permissions_by_default", False) else "off"
        except Exception:  # noqa: BLE001
            controller = "-"
            perms = "?"
        bits = [
            "[b]Settings[/b]",
            f"[dim]controller:[/dim] {_escape(controller)}",
            f"[dim]permissions:[/dim] {perms}",
            f"[dim]accounts:[/dim] {len(data.accounts)}",
            f"[dim]projects:[/dim] {len(data.projects)}",
        ]
        if data.errors:
            bits.append(f"[#ff5f6d]\u25cf {len(data.errors)} error(s)[/]")
        self.topbar.update("   ".join(bits))

    def _render_nav(self) -> None:
        data = self.data
        counts = {
            "accounts": len(data.accounts) if data else 0,
            "projects": len(data.projects) if data else 0,
            "heartbeat": len(data.heartbeat) if data else 0,
            "plugins": len(data.plugins) if data else 0,
            "planner": len(data.planner) if data else 0,
            "inbox": len(data.inbox) if data else 0,
            "about": len(data.about) if data else 0,
        }
        for i, (key, label) in enumerate(_SETTINGS_SECTIONS):
            widget = self._nav_widgets.get(key)
            if widget is None:
                continue
            widget.update(self._nav_label(key, label, count=counts.get(key)))
            widget.remove_class("-selected")
            widget.remove_class("-section-active")
            if i == self._nav_cursor:
                widget.add_class("-selected")
            if key == self._active_section:
                widget.add_class("-section-active")

    def _nav_label(self, key: str, label: str, *, count: int | None) -> str:
        marker = "\u25b8" if key == self._active_section else " "
        cnt = f"  [dim]{count}[/dim]" if count is not None else ""
        return f"{marker} {_escape(label)}{cnt}"

    # ------------------------------------------------------------------
    # Section switching / rendering
    # ------------------------------------------------------------------

    def _show_section(self, key: str) -> None:
        self._active_section = key
        self._search_query = ""
        self.search_input.remove_class("-active")
        self.search_input.value = ""
        self._render_nav()
        self._render_section(key)

    def _render_section(self, key: str) -> None:
        self.accounts.display = key == "accounts"
        self.projects_table.display = key == "projects"
        self.plugins_table.display = key == "plugins"
        self.kv_static.display = key in {"heartbeat", "planner", "inbox", "about"}
        self.detail.display = key in {"accounts", "projects", "plugins"}

        title_map = dict(_SETTINGS_SECTIONS)
        self.section_title.update(
            f"[b]{_escape(title_map.get(key, key))}[/b]"
        )

        data = self.data
        if data is None:
            return

        if key == "accounts":
            self._render_accounts(data)
        elif key == "projects":
            self._render_projects(data)
        elif key == "plugins":
            self._render_plugins(data)
        elif key == "heartbeat":
            self._render_kv("Heartbeat & recovery", data.heartbeat)
        elif key == "planner":
            self._render_kv("Planner", data.planner)
        elif key == "inbox":
            self._render_kv("Inbox & notifications", data.inbox)
        elif key == "about":
            self._render_kv("About", data.about)

    # ── Accounts ───────────────────────────────────────────────────

    def _filtered_accounts(self, data: SettingsData) -> list[dict]:
        q = self._search_query.strip().lower()
        if not q:
            return data.accounts
        return [
            a for a in data.accounts
            if q in a["key"].lower()
            or q in a["email"].lower()
            or q in a["provider"].lower()
            or q in a["plan"].lower()
        ]

    def _render_accounts(self, data: SettingsData) -> None:
        self.accounts.clear()
        rows = self._filtered_accounts(data)
        for a in rows:
            dot, colour = _settings_status_dot(a["health"], a["logged_in"])
            fo_mark = f"#{a['failover_pos']}" if a["failover_pos"] else ""
            ctrl_mark = "\u2713" if a["is_controller"] else ""
            self.accounts.add_row(
                Text(dot, style=colour),
                a["key"],
                a["email"],
                a["provider"],
                ctrl_mark,
                fo_mark,
                a["usage_summary"] or "-",
                key=a["key"],
            )
        if self.accounts.row_count and self._selected_account_key:
            try:
                keys = [a["key"] for a in rows]
                if self._selected_account_key in keys:
                    self.accounts.move_cursor(
                        row=keys.index(self._selected_account_key),
                    )
            except Exception:  # noqa: BLE001
                pass
        elif self.accounts.row_count and self.accounts.cursor_row < 0:
            self.accounts.move_cursor(row=0)
        self._render_account_detail(data)

    def _render_account_detail(self, data: SettingsData) -> None:
        rows = self._filtered_accounts(data)
        if not rows:
            self.detail.update(
                "[dim]No accounts match the current filter.[/dim]\n\n"
                "Press [b]Esc[/b] to clear the search."
            )
            return
        key = (
            self._selected_account_key
            or self._current_accounts_key()
            or rows[0]["key"]
        )
        selected = next((a for a in rows if a["key"] == key), rows[0])
        sep = "[dim]" + "\u2500" * 40 + "[/dim]"
        dot, colour = _settings_status_dot(
            selected["health"], selected["logged_in"],
        )
        lines = [
            f"[{colour}]{dot}[/{colour}] [b]{_escape(selected['key'])}[/b]"
            f"  [dim]({_escape(selected['provider'])})[/dim]",
            sep,
            f"[dim]Email:[/dim]      {_escape(selected['email'])}",
            f"[dim]Logged in:[/dim]  {'yes' if selected['logged_in'] else 'no'}",
            f"[dim]Health:[/dim]     {_escape(selected['health']) or '-'}",
            f"[dim]Plan:[/dim]       {_escape(selected['plan']) or '-'}",
            f"[dim]Usage:[/dim]      {_escape(selected['usage_summary']) or '-'}",
            f"[dim]Controller:[/dim] {'yes' if selected['is_controller'] else 'no'}",
            f"[dim]Failover:[/dim]   "
            f"{'#' + str(selected['failover_pos']) if selected['failover_pos'] else 'no'}",
            f"[dim]Home:[/dim]       {_escape(selected['home']) or '-'}",
            f"[dim]Isolation:[/dim]  {_escape(selected['isolation_status']) or '-'}",
            f"[dim]Storage:[/dim]    {_escape(selected['auth_storage']) or '-'}",
        ]
        if selected["available_at"]:
            lines.append(
                f"[dim]Available:[/dim]  {_escape(selected['available_at'])}"
            )
        if selected["access_expires_at"]:
            lines.append(
                f"[dim]Expires:[/dim]    {_escape(selected['access_expires_at'])}"
            )
        if selected["reason"]:
            lines.extend(
                [sep, f"[dim]Reason:[/dim]     {_escape(selected['reason'])}"]
            )
        if selected["usage_raw_text"]:
            snippet = selected["usage_raw_text"].strip().splitlines()[:6]
            if snippet:
                lines.append(sep)
                lines.append("[dim]Latest usage snapshot:[/dim]")
                lines.extend(f"  {_escape(line)}" for line in snippet)
        self.detail.update("\n".join(lines))

    def _current_accounts_key(self) -> str | None:
        if self.accounts.row_count == 0 or self.accounts.cursor_row < 0:
            return None
        try:
            row_key = self.accounts.coordinate_to_cell_key(
                (self.accounts.cursor_row, 0),
            ).row_key
        except Exception:  # noqa: BLE001
            return None
        return str(row_key.value) if row_key is not None else None

    # ── Projects ───────────────────────────────────────────────────

    def _filtered_projects(self, data: SettingsData) -> list[dict]:
        q = self._search_query.strip().lower()
        if not q:
            return data.projects
        return [
            p for p in data.projects
            if q in p["key"].lower()
            or q in p["name"].lower()
            or q in p["persona"].lower()
            or q in p["path"].lower()
        ]

    def _render_projects(self, data: SettingsData) -> None:
        self.projects_table.clear()
        rows = self._filtered_projects(data)
        for p in rows:
            dot_colour = "#3ddc84" if p["tracked"] else "#4a5568"
            dot = Text("\u25cf", style=dot_colour)
            name_style = "" if p["tracked"] else "dim"
            name_cell = Text(p["name"] or p["key"], style=name_style)
            path = p["path"] or "-"
            path_disp = path if len(path) <= 42 else ("\u2026" + path[-41:])
            path_cell = Text(path_disp, style="dim")
            tasks_cell = Text(str(p["task_total"]))
            last_cell = Text(p["last_activity"] or "-", style="dim")
            key_cell = Text(p["key"], style=name_style)
            persona_cell = Text(p["persona"], style="dim")
            self.projects_table.add_row(
                dot, key_cell, name_cell, persona_cell,
                path_cell, tasks_cell, last_cell,
                key=p["key"],
            )
        if self.projects_table.row_count and self._selected_project_key:
            keys = [p["key"] for p in rows]
            if self._selected_project_key in keys:
                try:
                    self.projects_table.move_cursor(
                        row=keys.index(self._selected_project_key),
                    )
                except Exception:  # noqa: BLE001
                    pass
        elif (
            self.projects_table.row_count
            and self.projects_table.cursor_row < 0
        ):
            self.projects_table.move_cursor(row=0)
        self._render_project_detail(data)

    def _render_project_detail(self, data: SettingsData) -> None:
        rows = self._filtered_projects(data)
        if not rows:
            self.detail.update(
                "[dim]No projects match the current filter.[/dim]"
            )
            return
        key = (
            self._selected_project_key
            or self._current_projects_key()
            or rows[0]["key"]
        )
        selected = next((p for p in rows if p["key"] == key), rows[0])
        tracked_line = (
            "[#3ddc84]tracked[/#3ddc84]"
            if selected["tracked"]
            else "[dim]paused (press [b]t[/b] to enable)[/dim]"
        )
        lines = [
            f"[b]{_escape(selected['name'])}[/b]  "
            f"[dim]({_escape(selected['key'])})[/dim]",
            f"[dim]PM:[/dim]     {_escape(selected['persona'])}",
            f"[dim]Path:[/dim]   {_escape(selected['path']) or '-'}  "
            f"{'' if selected['path_exists'] else '[#ff5f6d](missing)[/]'}",
            f"[dim]Status:[/dim] {tracked_line}",
            f"[dim]Tasks:[/dim]  {selected['task_total']}",
            f"[dim]Last:[/dim]   {_escape(selected['last_activity']) or '-'}",
        ]
        self.detail.update("\n".join(lines))

    def _current_projects_key(self) -> str | None:
        if (
            self.projects_table.row_count == 0
            or self.projects_table.cursor_row < 0
        ):
            return None
        try:
            row_key = self.projects_table.coordinate_to_cell_key(
                (self.projects_table.cursor_row, 0),
            ).row_key
        except Exception:  # noqa: BLE001
            return None
        return str(row_key.value) if row_key is not None else None

    # ── Plugins ────────────────────────────────────────────────────

    def _filtered_plugins(self, data: SettingsData) -> list[dict]:
        q = self._search_query.strip().lower()
        if not q:
            return data.plugins
        return [
            p for p in data.plugins
            if q in p["name"].lower()
            or q in p["description"].lower()
            or q in p["source"].lower()
            or q in p["status"].lower()
        ]

    def _render_plugins(self, data: SettingsData) -> None:
        self.plugins_table.clear()
        rows = self._filtered_plugins(data)
        for p in rows:
            status = p["status"]
            if status == "loaded":
                status_text = Text("\u25cf loaded", style="#3ddc84")
            elif status == "degraded":
                status_text = Text("\u25cf degraded", style="#f0c45a")
            else:
                status_text = Text("\u25cf disabled", style="#6b7a88")
            name_style = "" if status == "loaded" else "dim"
            self.plugins_table.add_row(
                Text(p["name"], style=name_style),
                Text(p["version"] or "-", style="dim"),
                Text(p["source"] or "-", style="dim"),
                status_text,
                key=p["name"],
            )
        if (
            self.plugins_table.row_count
            and self.plugins_table.cursor_row < 0
        ):
            self.plugins_table.move_cursor(row=0)
        self._render_plugin_detail(data)

    def _render_plugin_detail(self, data: SettingsData) -> None:
        rows = self._filtered_plugins(data)
        if not rows:
            self.detail.update(
                "[dim]No plugins match the current filter.[/dim]"
            )
            return
        idx = 0
        if (
            self.plugins_table.row_count
            and self.plugins_table.cursor_row >= 0
        ):
            idx = min(self.plugins_table.cursor_row, len(rows) - 1)
        selected = rows[idx]
        lines = [
            f"[b]{_escape(selected['name'])}[/b]  "
            f"[dim]v{_escape(selected['version'] or '?')}[/dim]",
            f"[dim]Source:[/dim] {_escape(selected['source'])}",
            f"[dim]Status:[/dim] {_escape(selected['status'])}",
        ]
        if selected["description"]:
            lines.append("")
            lines.append(_escape(selected["description"]))
        if selected["degraded_reason"]:
            lines.append("")
            lines.append(
                f"[#f0c45a]Reason:[/] {_escape(selected['degraded_reason'])}"
            )
        self.detail.update("\n".join(lines))

    # ── Key/value sections ─────────────────────────────────────────

    def _render_kv(self, _title: str, pairs: list[tuple[str, str]]) -> None:
        q = self._search_query.strip().lower()
        if q:
            pairs = [
                (k, v) for k, v in pairs
                if q in k.lower() or q in v.lower()
            ]
        if not pairs:
            self.kv_static.update(
                "[dim]No entries match the current filter.[/dim]"
            )
            return
        key_width = max((len(k) for k, _ in pairs), default=10)
        lines = []
        for k, v in pairs:
            lines.append(
                f"[dim]{_escape(k.ljust(key_width))}[/dim]  {_escape(v)}"
            )
        self.kv_static.update("\n".join(lines))

    # ------------------------------------------------------------------
    # Keybindings — nav, search, actions
    # ------------------------------------------------------------------

    def action_nav_down(self) -> None:
        if self._focus_target == "nav":
            self._nav_cursor = (
                self._nav_cursor + 1
            ) % len(_SETTINGS_SECTIONS)
            self._render_nav()
        else:
            table = self._active_table()
            if table is not None and table.row_count:
                new = min(table.cursor_row + 1, table.row_count - 1)
                table.move_cursor(row=new)
                self._sync_selection()

    def action_nav_up(self) -> None:
        if self._focus_target == "nav":
            self._nav_cursor = (
                self._nav_cursor - 1
            ) % len(_SETTINGS_SECTIONS)
            self._render_nav()
        else:
            table = self._active_table()
            if table is not None and table.row_count:
                new = max(table.cursor_row - 1, 0)
                table.move_cursor(row=new)
                self._sync_selection()

    def action_section_next(self) -> None:
        idx = next(
            (
                i for i, (k, _l) in enumerate(_SETTINGS_SECTIONS)
                if k == self._active_section
            ),
            0,
        )
        idx = (idx + 1) % len(_SETTINGS_SECTIONS)
        self._nav_cursor = idx
        self._show_section(_SETTINGS_SECTIONS[idx][0])

    def action_section_prev(self) -> None:
        idx = next(
            (
                i for i, (k, _l) in enumerate(_SETTINGS_SECTIONS)
                if k == self._active_section
            ),
            0,
        )
        idx = (idx - 1) % len(_SETTINGS_SECTIONS)
        self._nav_cursor = idx
        self._show_section(_SETTINGS_SECTIONS[idx][0])

    def action_activate_row(self) -> None:
        if self._focus_target == "nav":
            key = _SETTINGS_SECTIONS[self._nav_cursor][0]
            self._show_section(key)
            table = self._active_table()
            if table is not None and table.row_count:
                self._focus_target = "table"
                try:
                    table.focus()
                except Exception:  # noqa: BLE001
                    pass

    def action_start_search(self) -> None:
        self.search_input.add_class("-active")
        self.search_input.value = ""
        self.search_input.focus()

    def action_refresh(self) -> None:
        self._refresh()
        try:
            self.notify("Settings refreshed.", timeout=1.5)
        except Exception:  # noqa: BLE001
            pass

    def action_toggle_permissions(self) -> None:
        try:
            config = load_config(self.config_path)
            enabled = not config.pollypm.open_permissions_by_default
            self.service.set_open_permissions_default(enabled)
        except Exception as exc:  # noqa: BLE001
            try:
                self.notify(
                    f"Toggle permissions failed: {exc}", severity="error",
                )
            except Exception:  # noqa: BLE001
                pass
            return
        try:
            self.notify(
                f"Open permissions {'enabled' if enabled else 'disabled'}.",
                timeout=1.5,
            )
        except Exception:  # noqa: BLE001
            pass
        self._refresh()

    def action_toggle_project_tracked(self) -> None:
        if self._active_section != "projects":
            return
        key = self._current_projects_key()
        if key is None:
            return
        setter = getattr(self.service, "set_project_tracked", None)
        if setter is None:
            return
        try:
            data = self.data
            current = next(
                (
                    p for p in (data.projects if data else [])
                    if p["key"] == key
                ),
                None,
            )
            if current is None:
                return
            setter(key, not current["tracked"])
        except Exception as exc:  # noqa: BLE001
            try:
                self.notify(f"Toggle failed: {exc}", severity="error")
            except Exception:  # noqa: BLE001
                pass
            return
        self._refresh()

    def action_make_controller(self) -> None:
        if self._active_section != "accounts":
            return
        key = self._current_accounts_key()
        if not key:
            return
        setter = getattr(self.service, "set_controller_account", None)
        if setter is None:
            return
        try:
            setter(key)
        except Exception as exc:  # noqa: BLE001
            try:
                self.notify(
                    f"Controller change failed: {exc}", severity="error",
                )
            except Exception:  # noqa: BLE001
                pass
            return
        self._refresh()

    def action_toggle_failover(self) -> None:
        if self._active_section != "accounts":
            return
        key = self._current_accounts_key()
        if not key:
            return
        setter = getattr(self.service, "toggle_failover_account", None)
        if setter is None:
            return
        try:
            setter(key)
        except Exception as exc:  # noqa: BLE001
            try:
                self.notify(
                    f"Failover toggle failed: {exc}", severity="error",
                )
            except Exception:  # noqa: BLE001
                pass
            return
        self._refresh()

    def action_back_or_cancel(self) -> None:
        if self.search_input.has_class("-active"):
            self.search_input.remove_class("-active")
            self._search_query = ""
            self.search_input.value = ""
            self._render_section(self._active_section)
            try:
                self.nav.focus()
            except Exception:  # noqa: BLE001
                pass
            self._focus_target = "nav"
            return
        if self._focus_target == "table":
            self._focus_target = "nav"
            try:
                self.nav.focus()
            except Exception:  # noqa: BLE001
                pass
            return
        self.exit()

    # ------------------------------------------------------------------
    # Search input handlers
    # ------------------------------------------------------------------

    @on(Input.Changed, "#settings-search")
    def on_search_changed(self, event: Input.Changed) -> None:
        self._search_query = event.value or ""
        self._render_section(self._active_section)

    @on(Input.Submitted, "#settings-search")
    def on_search_submitted(self, _event: Input.Submitted) -> None:
        table = self._active_table()
        if table is not None:
            try:
                table.focus()
                self._focus_target = "table"
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _active_table(self) -> DataTable | None:
        if self._active_section == "accounts":
            return self.accounts
        if self._active_section == "projects":
            return self.projects_table
        if self._active_section == "plugins":
            return self.plugins_table
        return None

    def _sync_selection(self) -> None:
        data = self.data
        if data is None:
            return
        if self._active_section == "accounts":
            self._selected_account_key = self._current_accounts_key()
            self._render_account_detail(data)
        elif self._active_section == "projects":
            self._selected_project_key = self._current_projects_key()
            self._render_project_detail(data)
        elif self._active_section == "plugins":
            self._render_plugin_detail(data)

    @on(DataTable.RowHighlighted, "#accounts")
    def on_account_highlighted(
        self, _event: DataTable.RowHighlighted,
    ) -> None:
        self._sync_selection()

    @on(DataTable.RowHighlighted, "#projects-table")
    def on_project_highlighted(
        self, _event: DataTable.RowHighlighted,
    ) -> None:
        self._sync_selection()

    @on(DataTable.RowHighlighted, "#plugins-table")
    def on_plugin_highlighted(
        self, _event: DataTable.RowHighlighted,
    ) -> None:
        self._sync_selection()

    @on(DataTable.RowSelected, "#accounts")
    def on_account_selected(self, _event: DataTable.RowSelected) -> None:
        self._selected_account_key = self._current_accounts_key()
        if self.data is not None:
            self._render_account_detail(self.data)


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


def _fuzzy_subseq_match(query: str, hay: str) -> bool:
    """Subsequence fuzzy match: every char in ``query`` appears in ``hay`` in order.

    ``"shp"`` matches ``"shipped"`` (s, h, p land in order). Substring
    matches are a subset, so a plain ``"deploy"`` query against
    ``"deploy blocked"`` still matches via the same algorithm.
    Empty query matches everything; case is folded before compare so
    the call site doesn't have to.
    """
    if not query:
        return True
    if not hay:
        return False
    qi = 0
    q = query
    for ch in hay:
        if ch == q[qi]:
            qi += 1
            if qi == len(q):
                return True
    return False


def _task_recent_timestamp(task) -> float | None:
    """Return the most-recent updated/created stamp as a unix timestamp.

    Prefers ``updated_at`` (newer thread activity wins), falls back to
    ``created_at``. Returns ``None`` for unparseable values so the
    "recent 24h" filter simply drops them rather than including weirdly
    dated tasks by accident.
    """
    from datetime import datetime as _dt

    for attr in ("updated_at", "created_at"):
        value = getattr(task, attr, None)
        if value is None:
            continue
        try:
            if hasattr(value, "timestamp"):
                return float(value.timestamp())
            return _dt.fromisoformat(str(value)).timestamp()
        except (TypeError, ValueError):
            continue
    return None


class _InboxProjectPickerModal(ModalScreen[str | None]):
    """Tiny modal listing project keys for the ``p`` filter chip.

    Returns the selected key (string) or ``None`` when dismissed via
    Esc. Selecting the currently-active project clears the chip.
    """

    CSS = """
    _InboxProjectPickerModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.45);
    }
    #ipp-dialog {
        width: 48;
        max-width: 90%;
        height: auto;
        max-height: 18;
        padding: 1 1 0 1;
        background: #141a20;
        border: round #2a3340;
    }
    #ipp-title {
        height: 1;
        padding: 0 1;
        color: #97a6b2;
    }
    #ipp-list {
        height: auto;
        max-height: 14;
        background: #141a20;
        border: none;
        margin-top: 1;
        padding: 0;
    }
    #ipp-list > .ipp-row {
        height: 1;
        padding: 0 1;
        color: #d6dee5;
        background: transparent;
    }
    #ipp-list > .ipp-row.-highlight {
        background: #1e2730;
    }
    #ipp-hint {
        height: 1;
        padding: 0 1;
        color: #3e4c5a;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Close"),
        Binding("down,j", "cursor_down", "Down", show=False),
        Binding("up,k", "cursor_up", "Up", show=False),
        Binding("enter", "select", "Pick", show=False),
    ]

    def __init__(self, keys: list[str], current: str | None) -> None:
        super().__init__()
        self._keys = list(keys)
        self._current = current
        self.list_view = ListView(id="ipp-list")
        self.title_bar = Static(
            "[b]Filter by project[/b]", id="ipp-title", markup=True,
        )
        self.hint = Static(
            "[dim]\u21b5 select  \u00b7  esc cancel  \u00b7  pick the active "
            "project to clear[/dim]",
            id="ipp-hint", markup=True,
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="ipp-dialog"):
            yield self.title_bar
            yield self.list_view
            yield self.hint

    def on_mount(self) -> None:
        for key in self._keys:
            label = key
            if key == self._current:
                label = f"\u25cf {key}"
            self.list_view.append(
                ListItem(Static(label, markup=False), classes="ipp-row")
            )
        self.list_view.index = 0
        self.list_view.focus()

    def action_cursor_down(self) -> None:
        self.list_view.action_cursor_down()

    def action_cursor_up(self) -> None:
        self.list_view.action_cursor_up()

    def action_select(self) -> None:
        idx = self.list_view.index or 0
        if 0 <= idx < len(self._keys):
            picked = self._keys[idx]
            # Picking the current chip again is a "clear" gesture.
            if picked == self._current:
                self.dismiss("")
            else:
                self.dismiss(picked)
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(ListView.Selected, "#ipp-list")
    def _on_row_selected(self, _event: ListView.Selected) -> None:
        self.action_select()


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


def _extract_plan_review_meta(labels: list[str] | None) -> dict:
    """Parse plan_review sidecar labels into a structured dict.

    The architect emits a plan_review item with labels that encode the
    plan task id, the explainer HTML path, and the fast-track flag:

        plan_review
        project:<key>
        plan_task:<project/number>
        explainer:<abs path to plan-review.html>
        fast_track             (optional; present only for fast-track)

    Returns ``{plan_task_id, explainer_path, fast_track, project}``;
    keys are present only when the source label was present.
    """
    meta: dict[str, object] = {"fast_track": False}
    for raw in labels or []:
        if not isinstance(raw, str):
            continue
        label = raw.strip()
        if label == "fast_track":
            meta["fast_track"] = True
            continue
        if label.startswith("plan_task:"):
            meta["plan_task_id"] = label[len("plan_task:"):].strip()
        elif label.startswith("explainer:"):
            path_str = label[len("explainer:"):].strip()
            if path_str:
                meta["explainer_path"] = path_str
        elif label.startswith("project:"):
            meta["project"] = label[len("project:"):].strip()
    return meta


def _extract_blocking_question_meta(labels: list[str] | None) -> dict:
    """Parse ``blocking_question`` sidecar labels into a structured dict.

    The drift sweep emits a blocking_question item with labels that
    encode the blocked task id, the worker session doing the asking,
    and the project key:

        blocking_question
        project:<key>
        task:<project/number>
        blocking_worker:<session_name>

    Returns ``{task_id, blocking_worker, project}``; keys are
    present only when the corresponding label was present.
    """
    meta: dict[str, object] = {}
    for raw in labels or []:
        if not isinstance(raw, str):
            continue
        label = raw.strip()
        if label.startswith("task:"):
            meta["task_id"] = label[len("task:"):].strip()
        elif label.startswith("blocking_worker:"):
            meta["blocking_worker"] = label[
                len("blocking_worker:"):
            ].strip()
        elif label.startswith("project:"):
            meta["project"] = label[len("project:"):].strip()
    return meta


def _plan_review_has_round_trip(
    replies, *, requester: str = "user",
) -> bool:
    """True if the thread shows both the reviewer and the PM have spoken.

    "Round-trip" means at least one reply entry from the reviewer
    (the requester role — normally ``user``, or ``polly`` for
    fast-tracked items) AND at least one reply entry from the PM side
    (architect / polly / project persona — any non-reviewer actor).

    The gate is intentionally lenient: we don't care about ordering,
    we just need evidence of a conversation before Accept unlocks.
    """
    reviewer = (requester or "user").strip().lower() or "user"
    saw_reviewer = False
    saw_other = False
    for entry in replies or []:
        actor = (getattr(entry, "actor", "") or "").strip().lower()
        if not actor:
            continue
        if actor == reviewer:
            saw_reviewer = True
        else:
            saw_other = True
        if saw_reviewer and saw_other:
            return True
    return False


def _build_plan_review_primer(
    *,
    project_key: str,
    plan_path: str,
    explainer_path: str,
    plan_task_id: str,
    reviewer_name: str = "Sam",
) -> str:
    """Build the PM input primer injected on ``d`` for a plan_review item.

    Distinct from the generic ``re: inbox/N ...`` shape — this primer
    hands the PM a short brief plus the canonical co-refinement job
    description so the conversation starts on-topic without Sam (or
    Polly, when fast-tracked) having to type the frame themselves.
    """
    person = reviewer_name.strip() or "Sam"
    pronoun_subject = "Sam" if person == "Sam" else person
    return (
        f"{pronoun_subject} has opened plan review for project: {project_key}.\n"
        f"Plan: {plan_path}\n"
        f"Explainer: {explainer_path}\n"
        "\n"
        "Your job in this conversation:\n"
        f"- Co-refine the plan with {pronoun_subject}\n"
        "- Push hard for decomposition into the smallest reasonable tasks\n"
        "- Each task should ship a small module with clean interfaces\n"
        "- Challenge large lumps: a 500-LoC module with 3 concerns should "
        "become 3 modules with 1 concern each\n"
        "- Surface cross-cutting risks that span modules \u2014 integration "
        "bugs live there\n"
        "- Propose rewrites of any decision that's load-bearing without "
        "clear justification\n"
        f"- If {pronoun_subject} pings without a specific concern, your "
        "default opener is to walk through the plan's riskiest decisions + "
        "decomposition and ask where to dig in \u2014 don't just wait for "
        "a question\n"
        "\n"
        f"When {pronoun_subject} signs off (says 'approved' or equivalent): "
        f"call `pm task approve {plan_task_id} --actor "
        f"{'user' if person == 'Sam' else 'polly'}`\n"
        "Don't create backlog tasks yourself \u2014 emit_backlog fires "
        "after approval.\n"
        "This small-tasks / small-modules bias matters because it's much "
        "more maintainable for agentic development."
    )


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
    #inbox-filter-bar {
        height: auto;
        padding: 0 1;
        background: #0c0f12;
    }
    #inbox-filter-input {
        height: 3;
        padding: 0 1;
        background: #111820;
        border: round #2a3340;
        color: #d6dee5;
    }
    #inbox-filter-input:focus {
        border: round #5b8aff;
    }
    #inbox-filter-chips {
        height: 1;
        padding: 0 1;
        color: #97a6b2;
    }
    #inbox-empty-state {
        height: 1fr;
        content-align: center middle;
        color: #6b7a88;
        background: #0f1317;
    }
    """

    # Filter keybindings (inbox-search #NEW): `/` opens a fuzzy text
    # filter, chip-toggle keys flip AND-combined filter chips, ``c``
    # clears all. Conflicts with existing bindings resolved as:
    #   * ``r`` stays as reply → capital ``R`` opens recent-24h.
    #   * ``u`` was refresh → promoted filter (unread-only); refresh
    #     remains available via ``ctrl+r`` and the ``:`` palette.
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
        # Plan review (#297). ``v`` opens the rendered HTML explainer in
        # the user's browser — reviewing against the visual page is the
        # whole point of the plan-review flow. Accept (capital A) routes
        # through ``action_accept_proposal`` which branches on label,
        # so no separate keybinding is needed here for approve.
        Binding("v", "open_plan_explainer", "Explainer", show=False),
        Binding("e", "expand_all_rollup", "Expand all", show=False),
        # Filter / search bar (#NEW).
        Binding("slash", "start_filter", "Filter", show=False),
        Binding("u", "toggle_filter_unread", "Unread", show=False),
        Binding("p", "pick_filter_project", "Project", show=False),
        Binding("R", "toggle_filter_recent", "Recent", show=False),
        Binding("l", "toggle_filter_plan_review", "Plan review", show=False),
        Binding("b", "toggle_filter_blocking", "Blocking", show=False),
        Binding("c", "clear_filters", "Clear filters", show=False),
        # Refresh: ``u`` re-bound to filter, so refresh moves to ``ctrl+r``
        # (palette 'session.refresh' still works from any screen).
        Binding("ctrl+r", "refresh", "Refresh", show=False),
        Binding("colon", "open_command_palette", "Palette", priority=True),
        Binding("question_mark", "show_keyboard_help", "Help", priority=True),
        Binding("q,escape", "back_or_cancel", "Back"),
    ]

    def action_open_command_palette(self) -> None:
        _open_command_palette(self)

    def action_show_keyboard_help(self) -> None:
        _open_keyboard_help(self)

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
        # Filter bar (#NEW). Hidden until `/` mounts the Input. The
        # chips Static is always part of the tree so toggles can update
        # it without an explicit mount; it renders an empty string when
        # no filters are active.
        self.filter_input = Input(
            placeholder="filter \u2026  (Esc clears + closes)",
            id="inbox-filter-input",
        )
        self.filter_chips = Static("", id="inbox-filter-chips", markup=True)
        self.filter_bar = Vertical(
            self.filter_input, self.filter_chips, id="inbox-filter-bar",
        )
        self.status = Static("", id="inbox-status")
        self.hint = Static(
            PollyInboxApp._DEFAULT_HINT,
            id="inbox-hint",
        )
        self._tasks: list = []
        self._selected_task_id: str | None = None
        self._unread_ids: set[str] = set()
        # Filter state (#NEW). Session-scoped — cleared on each mount
        # via :meth:`on_mount`. All filters AND-combine.
        self._filter_text: str = ""
        self._filter_unread_only: bool = False
        self._filter_project: str | None = None
        self._filter_recent: bool = False
        self._filter_plan_review: bool = False
        self._filter_blocking: bool = False
        self._filter_bar_visible: bool = False
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
        # Plan-review state (#297). Keyed by inbox task_id:
        # * _plan_review_meta — parsed sidecar labels (plan_task id,
        #   explainer path, fast_track flag) so the approve/open
        #   handlers don't re-parse on every keystroke.
        # * _plan_review_round_trip — whether the thread already has the
        #   user-plus-PM exchange that unlocks Accept (gating rule).
        self._plan_review_meta: dict[str, dict] = {}
        self._plan_review_round_trip: dict[str, bool] = {}
        # Blocking-question state (#302). Worker drift → inbox handoff.
        # Parsed sidecar labels (``task:<id>``, ``blocking_worker:<name>``,
        # ``project:<key>``) land here so reply / jump paths don't have
        # to re-parse on each keystroke.
        self._blocking_question_meta: dict[str, dict] = {}

    def compose(self) -> ComposeResult:
        yield self.filter_bar
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
        # Session-scoped filters: clear on each mount so restarts of the
        # cockpit land on a pristine full-list view.
        self._reset_filter_state()
        self.filter_input.display = False
        self.filter_bar.display = False
        self.filter_chips.display = False
        self._refresh_list(select_first=True)
        self.set_interval(self.REFRESH_INTERVAL_SECONDS, self._background_refresh)
        self.list_view.focus()
        # Live alert toasts. Inbox already claims ``a`` for archive so the
        # toast advertises "esc/click to dismiss" instead of the ``a`` hint.
        _setup_alert_notifier(self, bind_a=False)

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
        # Refresh chip line on every render so toggles + project changes
        # land in the UI even when the list itself didn't shrink.
        self._update_filter_chips()
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
        # Apply filter stack — fuzzy text + chip toggles AND-combine.
        visible = self._filtered_tasks(self._tasks)
        total = len(self._tasks)
        if not visible:
            # Friendly empty-match copy so a fully-filtered list isn't a
            # blank pane. The list stays in the tree (one disabled row)
            # so cursor focus has somewhere to land without crashing.
            self.list_view.append(
                ListItem(
                    Static(
                        "No matches. Press c to clear filters.",
                        classes="inbox-empty",
                    ),
                    disabled=True,
                )
            )
            self.detail.update(
                "[dim]No matches for the current filter set.\n\n"
                "Press [b]c[/b] to clear filters and see every message.[/dim]"
            )
            self._update_status(total=total, shown=0)
            return
        restore_index: int | None = 0 if select_first else None
        for idx, task in enumerate(visible):
            is_unread = task.task_id in self._unread_ids
            row = _InboxListItem(task, is_unread=is_unread)
            self.list_view.append(row)
            if previous and task.task_id == previous:
                restore_index = idx
        if restore_index is not None and self.list_view.index != restore_index:
            self.list_view.index = restore_index
            # Render detail for the restored selection so the right pane
            # shows content immediately on refresh.
            task = visible[restore_index]
            self._selected_task_id = task.task_id
            self._render_detail(task.task_id)
        self._update_status(total=total, shown=len(visible))

    def _update_status(self, *, total: int, shown: int) -> None:
        """Render the bottom counter strip — folds in active filters.

        ``shown`` may equal ``total`` when no filter narrows the list;
        in that case we omit the "N of M" framing for visual calm.
        """
        unread_n = len(self._unread_ids)
        bits: list[str] = []
        if self._has_active_filters() and shown != total:
            bits.append(f"{shown} of {total} shown")
        else:
            bits.append(f"{total} messages")
        if unread_n:
            bits.append(f"{unread_n} unread")
        desc = self._describe_filters()
        if desc:
            bits.append(f"filters: {desc}")
        self.status.update(" \u00b7 ".join(bits))

    def _background_refresh(self) -> None:
        """Periodic re-read; don't stomp the current cursor position."""
        try:
            self._refresh_list(select_first=False)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Filter / search (#NEW)
    # ------------------------------------------------------------------

    def _reset_filter_state(self) -> None:
        """Clear every filter back to the "show everything" baseline."""
        self._filter_text = ""
        self._filter_unread_only = False
        self._filter_project = None
        self._filter_recent = False
        self._filter_plan_review = False
        self._filter_blocking = False
        self._filter_bar_visible = False

    def _has_active_filters(self) -> bool:
        return any(
            (
                self._filter_text,
                self._filter_unread_only,
                self._filter_project,
                self._filter_recent,
                self._filter_plan_review,
                self._filter_blocking,
            )
        )

    def _filtered_tasks(self, tasks: list) -> list:
        """Apply the AND-combined filter stack to ``tasks``.

        Cheap O(N * filters) — the inbox is at most a few hundred rows
        and the chips short-circuit, so we don't need anything fancier.
        """
        if not self._has_active_filters():
            return list(tasks)
        text_q = self._filter_text.strip().lower()
        proj = self._filter_project
        out: list = []
        recent_cutoff_ts = self._recent_cutoff_timestamp() if self._filter_recent else None
        for t in tasks:
            if self._filter_unread_only and t.task_id not in self._unread_ids:
                continue
            if proj and (t.project or "") != proj:
                continue
            labels = list(getattr(t, "labels", []) or [])
            if self._filter_plan_review and "plan_review" not in labels:
                continue
            if self._filter_blocking and "blocking_question" not in labels:
                continue
            if recent_cutoff_ts is not None:
                ts = _task_recent_timestamp(t)
                if ts is None or ts < recent_cutoff_ts:
                    continue
            if text_q:
                hay = self._task_haystack(t).lower()
                if not _fuzzy_subseq_match(text_q, hay):
                    continue
            out.append(t)
        return out

    def _task_haystack(self, task) -> str:
        """Concatenate searchable fields for fuzzy matching."""
        return " ".join(
            [
                task.title or "",
                task.project or "",
                _format_sender(task),
            ]
        )

    def _recent_cutoff_timestamp(self) -> float:
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        return (_dt.now(_tz.utc) - _td(hours=24)).timestamp()

    def _describe_filters(self) -> str:
        bits: list[str] = []
        if self._filter_unread_only:
            bits.append("unread")
        if self._filter_project:
            bits.append(f"project:{self._filter_project}")
        if self._filter_recent:
            bits.append("recent")
        if self._filter_plan_review:
            bits.append("plan_review")
        if self._filter_blocking:
            bits.append("blocking_question")
        if self._filter_text:
            bits.append(f'"{self._filter_text}"')
        return " \u00b7 ".join(bits)

    def _update_filter_chips(self) -> None:
        """Re-render the chip strip + drive bar visibility.

        The filter bar (Input + chips) hides entirely when there are no
        active filters AND the user hasn't pressed `/` to open the
        Input. Otherwise it shows: the Input is gated by
        ``_filter_bar_visible``, the chips render whenever any chip is
        on (so toggling without `/` still surfaces feedback).
        """
        chip_bits: list[str] = []
        if self._filter_unread_only:
            chip_bits.append("[on #1e2730] unread [/on #1e2730]")
        if self._filter_project:
            chip_bits.append(
                f"[on #1e2730] project:{_escape(self._filter_project)} [/on #1e2730]"
            )
        if self._filter_recent:
            chip_bits.append("[on #1e2730] recent 24h [/on #1e2730]")
        if self._filter_plan_review:
            chip_bits.append("[on #1e2730] plan_review [/on #1e2730]")
        if self._filter_blocking:
            chip_bits.append("[on #1e2730] blocking_question [/on #1e2730]")
        if self._filter_text:
            chip_bits.append(
                f'[on #1e2730] "{_escape(self._filter_text)}" [/on #1e2730]'
            )
        if chip_bits:
            self.filter_chips.update("  ".join(chip_bits))
            self.filter_chips.display = True
        else:
            self.filter_chips.update("")
            self.filter_chips.display = False
        # Bar visibility tracks either the explicit Input toggle OR any
        # active chip (so the user sees the chip rendered without
        # needing to keep `/` open).
        self.filter_input.display = self._filter_bar_visible
        self.filter_bar.display = self._filter_bar_visible or bool(chip_bits)

    # --- filter actions ------------------------------------------------

    def action_start_filter(self) -> None:
        """`/` — mount + focus the fuzzy filter Input."""
        self._filter_bar_visible = True
        self.filter_input.value = self._filter_text
        self._update_filter_chips()
        self.filter_input.focus()

    def action_toggle_filter_unread(self) -> None:
        if self.reply_input.has_focus or self.filter_input.has_focus:
            return
        self._filter_unread_only = not self._filter_unread_only
        self._render_list(select_first=True)

    def action_toggle_filter_recent(self) -> None:
        if self.reply_input.has_focus or self.filter_input.has_focus:
            return
        self._filter_recent = not self._filter_recent
        self._render_list(select_first=True)

    def action_toggle_filter_plan_review(self) -> None:
        if self.reply_input.has_focus or self.filter_input.has_focus:
            return
        self._filter_plan_review = not self._filter_plan_review
        self._render_list(select_first=True)

    def action_toggle_filter_blocking(self) -> None:
        if self.reply_input.has_focus or self.filter_input.has_focus:
            return
        self._filter_blocking = not self._filter_blocking
        self._render_list(select_first=True)

    def action_clear_filters(self) -> None:
        if self.reply_input.has_focus or self.filter_input.has_focus:
            return
        self._reset_filter_state()
        self.filter_input.value = ""
        self._render_list(select_first=True)

    def action_pick_filter_project(self) -> None:
        """`p` — open a small modal listing project keys for selection."""
        if self.reply_input.has_focus or self.filter_input.has_focus:
            return
        keys = sorted({(t.project or "").strip() for t in self._tasks if t.project})
        if not keys:
            self.notify("No projects in the current inbox.", severity="warning")
            return
        # If only one project is in scope, just toggle it instead of
        # bothering the user with a modal.
        if len(keys) == 1:
            self._filter_project = (
                None if self._filter_project == keys[0] else keys[0]
            )
            self._render_list(select_first=True)
            return

        def _on_pick(value: str | None) -> None:
            if value is None:
                return
            self._filter_project = value or None
            self._render_list(select_first=True)

        self.push_screen(_InboxProjectPickerModal(keys, self._filter_project), _on_pick)

    @on(Input.Changed, "#inbox-filter-input")
    def _on_filter_changed(self, event: Input.Changed) -> None:
        # Live-filter as the user types — cheap enough on inbox-sized data.
        self._filter_text = event.value or ""
        self._render_list(select_first=True)

    @on(Input.Submitted, "#inbox-filter-input")
    def _on_filter_submitted(self, _event: Input.Submitted) -> None:
        # Enter inside the filter Input applies + returns focus to the
        # list so j/k works without an extra Esc.
        self.list_view.focus()

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
        # Plan-review detection (#297). Plan-review items carry a
        # ``plan_review`` label and a ``plan_task:<id>`` sidecar that
        # points at the plan_project task the inbox item is reviewing.
        # A separate hint bar + gating state tracks whether the user has
        # conversed with the PM at least once (round-trip) before the
        # Accept key is surfaced. Fast-tracked items (``fast_track``
        # label) skip gating entirely.
        _is_plan_review = "plan_review" in _labels
        if _is_plan_review:
            meta = _extract_plan_review_meta(_labels)
            self._plan_review_meta[task_id] = meta
            round_trip = _plan_review_has_round_trip(
                replies, requester=(task.roles or {}).get("requester", "user"),
            )
            self._plan_review_round_trip[task_id] = round_trip
            self._update_hint_for_plan_review(
                fast_track=meta.get("fast_track", False),
                round_trip=round_trip,
            )
        else:
            self._plan_review_meta.pop(task_id, None)
            self._plan_review_round_trip.pop(task_id, None)
        # Blocking-question detection (#302). Worker sessions that end
        # their turn without a state flip and show blocker language
        # bubble up as a ``blocking_question`` item; the PM's inbox
        # gets its own hint bar so Sam / the persona knows ``r`` sends
        # a reply back to the worker via ``pm send --force`` and ``d``
        # jumps straight into the worker's pane.
        _is_blocking_question = "blocking_question" in _labels
        if _is_blocking_question:
            meta = _extract_blocking_question_meta(_labels)
            self._blocking_question_meta[task_id] = meta
            self._update_hint_for_blocking_question()
        else:
            self._blocking_question_meta.pop(task_id, None)
        if _is_proposal:
            self._proposal_specs[task_id] = _extract_proposal_spec(
                task, labels=_labels,
            )
            self._update_hint_for_proposal()
        elif _is_plan_review:
            self._proposal_specs.pop(task_id, None)
        elif _is_blocking_question:
            self._proposal_specs.pop(task_id, None)
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
        """Esc/q returns focus to the list from inputs, else exits.

        From the filter Input: clears the typed query + closes the bar
        (per the brief — "Esc clears + closes"). Chip toggles aren't
        cleared here; ``c`` is the explicit "wipe everything" key.
        """
        if self.filter_input.has_focus:
            self._filter_text = ""
            self.filter_input.value = ""
            self._filter_bar_visible = False
            self._render_list(select_first=False)
            self.list_view.focus()
            return
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

        # Blocking-question items (#302): ``d`` jumps to the worker
        # session so the PM can talk to the blocked worker directly,
        # not the PM persona. We short-circuit before the PM resolution
        # below so the cockpit route lands in the worker window.
        task_labels = list(getattr(task, "labels", []) or [])
        if (
            "blocking_question" in task_labels
            and focus_item is None
        ):
            bq_meta = _extract_blocking_question_meta(task_labels)
            worker_name = str(bq_meta.get("blocking_worker") or "")
            if worker_name:
                context_line = _build_pm_context_line(task, item=focus_item)
                self.run_worker(
                    lambda: self._dispatch_to_worker_sync(
                        worker_name, context_line,
                    ),
                    thread=True,
                    exclusive=True,
                    group="jump_to_pm",
                )
                return

        cockpit_key, pm_label = _resolve_pm_target(
            self.config_path, project_for_pm,
        )
        # Plan-review items (#297) inject a richer primer instead of the
        # generic ``re: inbox/N ...`` line so the PM lands in the
        # conversation with the co-refinement brief already framed.
        if "plan_review" in task_labels and focus_item is None:
            meta = _extract_plan_review_meta(task_labels)
            explainer_path = str(meta.get("explainer_path") or "")
            plan_task_id = str(meta.get("plan_task_id") or task_id or "")
            project_key = str(meta.get("project") or task.project or "")
            # Derive the plan file location alongside the explainer — the
            # architect writes ``docs/plan/plan.md`` via the planning
            # skill by convention; fall back to the canonical relative
            # path when we can't resolve the absolute one.
            plan_path = ""
            try:
                config = load_config(self.config_path)
                project = config.projects.get(project_key)
                if project is not None:
                    for candidate in _PLAN_FILE_CANDIDATES:
                        p = project.path / candidate
                        if p.is_file():
                            plan_path = str(p)
                            break
            except Exception:  # noqa: BLE001
                plan_path = ""
            if not plan_path:
                plan_path = "docs/plan/plan.md"
            reviewer_requester = (task.roles or {}).get("requester", "user")
            reviewer_name = "Polly" if reviewer_requester == "polly" else "Sam"
            context_line = _build_plan_review_primer(
                project_key=project_key or task.project or "",
                plan_path=plan_path,
                explainer_path=explainer_path,
                plan_task_id=plan_task_id,
                reviewer_name=reviewer_name,
            )
        else:
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

    def _dispatch_to_worker_sync(
        self, worker_name: str, context_line: str,
    ) -> None:
        """Worker-thread body: route cockpit to the worker window.

        Used by the ``blocking_question`` flow when the PM jumps
        straight to the stuck worker (``d`` key). Mirrors
        :meth:`_dispatch_to_pm_sync` but keys the route on the worker
        session name rather than a PM persona.
        """
        try:
            router = CockpitRouter(self.config_path)
            router.route_selected(worker_name)
            supervisor = router._load_supervisor()
            window_target = (
                f"{supervisor.config.project.tmux_session}:"
                f"{router._COCKPIT_WINDOW}"
            )
            right_pane = router._right_pane_id(window_target)
            if right_pane is None:
                router.tmux.send_keys(
                    window_target, context_line, press_enter=False,
                )
            else:
                router.tmux.send_keys(
                    right_pane, context_line, press_enter=False,
                )
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(
                self.notify,
                f"Jump to worker failed: {exc}",
                severity="error",
            )
            return
        self.call_from_thread(
            self.notify,
            f"Jumped to worker {worker_name}.",
            severity="information", timeout=3.0,
        )

    def _send_reply_to_worker(
        self, task_id: str, worker_name: str, body: str,
    ) -> None:
        """Forward a blocking_question reply back to the worker pane.

        Uses the supervisor's ``send_input`` with ``force=True`` to
        bypass the worker-role gate (``pm send --force`` semantics from
        #261). Best-effort: any failure surfaces as a TUI notification
        but does not roll back the in-thread reply that already landed
        in the inbox task.
        """
        try:
            from pollypm.service_api.v1 import PollyPMService
            supervisor = PollyPMService(self.config_path).load_supervisor()
            supervisor.send_input(
                worker_name, body,
                owner="pollypm", force=True, press_enter=True,
            )
        except Exception as exc:  # noqa: BLE001
            self.notify(
                f"Reply to worker {worker_name} failed: {exc}",
                severity="warning", timeout=3.0,
            )
            return
        self._emit_event(
            task_id,
            "inbox.blocking_question.reply_forwarded",
            f"reply forwarded to {worker_name} for {task_id}",
        )
        self.notify(
            f"Reply sent to worker {worker_name}.",
            severity="information", timeout=2.0,
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
        # Blocking-question reply (#302): when the inbox item carries
        # the ``blocking_question`` label, route the reply back to the
        # worker via ``pm send --force`` so the worker unblocks and
        # resumes. The in-thread ``add_reply`` above still happens so
        # the conversation is preserved for audit.
        meta = self._blocking_question_meta.get(task_id)
        if meta is not None:
            worker_name = str(meta.get("blocking_worker") or "")
            if worker_name:
                self._send_reply_to_worker(task_id, worker_name, body)
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
        "\u00b7 d discuss \u00b7 / filter \u00b7 c clear \u00b7 q back"
    )
    _PROPOSAL_HINT = (
        "A accept \u00b7 X reject \u00b7 r reply \u00b7 q back"
    )
    # Plan-review hint bars (#297). The gated variant hides ``A`` until
    # the thread has a round-trip; the ungated variant surfaces it.
    _PLAN_REVIEW_HINT_GATED = (
        "v open explainer \u00b7 d discuss with PM \u00b7 q back"
    )
    _PLAN_REVIEW_HINT_OPEN = (
        "v open explainer \u00b7 d discuss \u00b7 A approve \u00b7 q back"
    )
    _PLAN_REVIEW_HINT_FAST_TRACK = (
        "v open explainer \u00b7 d discuss \u00b7 A approve \u00b7 q back"
    )
    # Blocking-question hint (#302). ``r`` replies to the worker via
    # ``pm send --force`` so the blocker clears without the PM needing
    # to jump to the pane; ``d`` is the direct-conversation escape
    # hatch; ``a`` archives once the blocker is resolved.
    _BLOCKING_QUESTION_HINT = (
        "r reply to worker \u00b7 d jump to worker \u00b7 "
        "a archive \u00b7 q back"
    )

    def _update_hint_for_blocking_question(self) -> None:
        try:
            self.hint.update(self._BLOCKING_QUESTION_HINT)
        except Exception:  # noqa: BLE001
            pass

    def _update_hint_for_proposal(self) -> None:
        try:
            self.hint.update(self._PROPOSAL_HINT)
        except Exception:  # noqa: BLE001
            pass

    def _update_hint_for_plan_review(
        self, *, fast_track: bool, round_trip: bool,
    ) -> None:
        """Render the plan-review hint bar based on state.

        Fast-tracked items (Polly's inbox) never gate — Accept is live
        from the first render. User-inbox items are gated until the
        thread has at least one exchange with the PM.
        """
        if fast_track:
            text = self._PLAN_REVIEW_HINT_FAST_TRACK
        elif round_trip:
            text = self._PLAN_REVIEW_HINT_OPEN
        else:
            text = self._PLAN_REVIEW_HINT_GATED
        try:
            self.hint.update(text)
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
        """Accept the selected proposal or plan_review — branch on label."""
        if self.reply_input.has_focus:
            return
        task_id = self._selected_task_id
        if task_id is None:
            return
        # Plan-review items (#297) reuse the capital-A keybinding for
        # approve, but the action is different: we call
        # ``pm task approve`` against the referenced plan_task, not the
        # inbox item, and we don't create a follow-on task. Branch here
        # before the proposal-only guard below.
        if self._is_plan_review_selected():
            self._approve_plan_review()
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
    # Plan review (#297) — approve / open-explainer / PM primer
    # ------------------------------------------------------------------

    def _selected_plan_review_task(self):
        """Return (task, labels) for the current selection if it's plan_review.

        Mirrors :meth:`_selected_proposal_task` shape so callers can
        branch without awkward sentinel checks.
        """
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
        if "plan_review" not in labels:
            return None, labels
        return task, labels

    def _is_plan_review_selected(self) -> bool:
        """Fast check for branching inside action handlers."""
        task_id = self._selected_task_id
        if task_id is None:
            return False
        # Prefer the cached meta so we don't hit the DB for a keystroke
        # when the render path just populated it.
        if task_id in self._plan_review_meta:
            return True
        task, _labels = self._selected_plan_review_task()
        return task is not None

    def action_open_plan_explainer(self) -> None:
        """``v`` — open the plan-review HTML file for the selected item.

        No-op when focus is in the reply Input (so ``v`` types a letter),
        or when the selected item isn't a plan_review (the explainer
        concept doesn't apply to generic inbox items).
        """
        if self.reply_input.has_focus:
            return
        task_id = self._selected_task_id
        if task_id is None:
            return
        meta = self._plan_review_meta.get(task_id)
        if meta is None:
            task, labels = self._selected_plan_review_task()
            if task is None:
                self.notify(
                    "Open explainer only applies to plan_review items.",
                    severity="warning", timeout=2.0,
                )
                return
            meta = _extract_plan_review_meta(labels)
            self._plan_review_meta[task_id] = meta
        path = meta.get("explainer_path")
        if not path:
            self.notify(
                "No explainer path recorded on this plan_review item.",
                severity="warning", timeout=2.0,
            )
            return
        try:
            self._open_explainer(str(path))
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Open explainer failed: {exc}", severity="error")
            return
        self._emit_event(
            task_id, "inbox.plan_review.explainer_opened",
            f"user opened explainer for {task_id} \u2192 {path}",
        )

    def _open_explainer(self, path: str) -> None:
        """Shell out to ``open`` (macOS) or ``xdg-open`` (linux).

        Separated so tests patch one method instead of mocking the
        subprocess module directly.
        """
        import platform
        cmd = "open" if platform.system() == "Darwin" else "xdg-open"
        subprocess.run([cmd, path], check=False)

    def _approve_plan_review(self) -> None:
        """Call ``pm task approve`` against the plan_task, then archive.

        Gating (user-inbox only): when ``fast_track`` is NOT set, the
        approve keybinding should have been hidden by the hint bar
        until the thread has a round-trip. We still enforce the gate
        here — belt-and-braces against stale state — and warn the user
        if they somehow triggered Accept before the conversation.
        """
        task_id = self._selected_task_id
        if task_id is None:
            return
        task, labels = self._selected_plan_review_task()
        if task is None:
            self.notify(
                "Approve only applies to plan_review items.",
                severity="warning", timeout=2.0,
            )
            return
        meta = self._plan_review_meta.get(task_id) or _extract_plan_review_meta(
            labels,
        )
        plan_task_id = meta.get("plan_task_id")
        if not plan_task_id:
            self.notify(
                "Missing plan_task label; cannot approve.",
                severity="error", timeout=3.0,
            )
            return
        fast_track = bool(meta.get("fast_track", False))
        actor_name = "polly" if fast_track else "user"
        if not fast_track and not self._plan_review_round_trip.get(task_id, False):
            self.notify(
                "Discuss the plan with your PM first (press d). "
                "Approve unlocks after the first round-trip.",
                severity="warning", timeout=4.0,
            )
            return
        # Route through the work-service approve path directly — the
        # CLI entry point is just sugar over ``svc.approve``, and we
        # already hold the project context.
        svc = self._svc_for_task(plan_task_id)
        if svc is None:
            self.notify(
                "Could not open project database for plan task.",
                severity="error",
            )
            return
        try:
            svc.approve(plan_task_id, actor_name, None)
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Approve failed: {exc}", severity="error")
            return
        finally:
            try:
                svc.close()
            except Exception:  # noqa: BLE001
                pass
        # Archive the inbox row so it drops out of the list.
        svc = self._svc_for_task(task_id)
        if svc is not None:
            try:
                svc.add_context(
                    task_id,
                    actor=actor_name,
                    text=f"Plan approved \u2192 {plan_task_id}",
                    entry_type="plan_review_approved",
                )
                svc.archive_task(task_id, actor=actor_name)
            except Exception:  # noqa: BLE001
                pass
            finally:
                try:
                    svc.close()
                except Exception:  # noqa: BLE001
                    pass
        self._emit_event(
            task_id, "inbox.plan_review.approved",
            f"{actor_name} approved plan {plan_task_id} via {task_id}",
        )
        self.notify(
            f"Plan approved \u2014 {plan_task_id}",
            severity="information", timeout=3.0,
        )
        self._tasks = [t for t in self._tasks if t.task_id != task_id]
        self._unread_ids.discard(task_id)
        self._plan_review_meta.pop(task_id, None)
        self._plan_review_round_trip.pop(task_id, None)
        if self._selected_task_id == task_id:
            self._selected_task_id = None
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
        #349: routes through the unified ``messages`` table via Store.
        """
        try:
            from pollypm.store import SQLAlchemyStore
            project_key = task_id.split("/", 1)[0]
            config = load_config(self.config_path)
            project = config.projects.get(project_key)
            if project is None:
                return
            db_path = project.path / ".pollypm" / "state.db"
            if not db_path.exists():
                return
            store = SQLAlchemyStore(f"sqlite:///{db_path}")
            try:
                store.append_event(
                    scope="cockpit",
                    sender="cockpit",
                    subject=event_type,
                    payload={"message": message},
                )
            finally:
                close = getattr(store, "close", None)
                if callable(close):
                    close()
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


# Plan-viewer staleness threshold — see the task brief. 30 days of no
# edits to ``plan.md`` (or the plan being older than any backlog task)
# flips the UI's warning badge on.
_PLAN_STALE_DAYS = 30


def _dashboard_plan_aux_files(project_path: Path) -> list[Path]:
    """Return auxiliary files under ``docs/plan/`` (excluding ``plan.md``).

    Surfaces ``architecture.md``, ``risks.md``, ``milestones/*.md`` etc.
    so the dashboard can offer one-press jumps. Only top-level entries
    plus files one level deep in ``milestones/`` are returned — we don't
    walk arbitrarily deep, keeping the UI list bounded.
    """
    plan_dir = project_path / "docs" / "plan"
    if not plan_dir.is_dir():
        return []
    out: list[Path] = []
    try:
        for entry in sorted(plan_dir.iterdir()):
            if entry.name == "plan.md":
                continue
            if entry.is_file() and entry.suffix.lower() in (".md", ".txt"):
                out.append(entry)
            elif entry.is_dir() and entry.name == "milestones":
                try:
                    for sub in sorted(entry.iterdir()):
                        if sub.is_file() and sub.suffix.lower() in (".md", ".txt"):
                            out.append(sub)
                except OSError:
                    continue
    except OSError:
        return []
    # Bound the list so a runaway milestones folder doesn't drown the UI.
    return out[:12]


def _dashboard_plan_staleness(
    plan_path: Path | None,
    plan_mtime: float | None,
    project_path: Path | None,
    project_key: str,
) -> str | None:
    """Return a human-readable stale reason, or ``None`` when fresh.

    Two checks — either fires:

    * File mtime older than ``_PLAN_STALE_DAYS`` days.
    * The most recent approved ``plan_project`` task's approval
      timestamp is older than the newest non-planning (backlog) task's
      ``created_at``. Mirrors the plan-presence gate's staleness rule
      so the UI warning agrees with the sweeper.

    Both checks fail-open (return None) on any SQLite or datetime
    parse error — the warning is informational, not load-bearing.
    """
    if plan_path is None:
        return None
    from datetime import datetime as _dt, timezone as _tz

    now = _dt.now(_tz.utc).timestamp()
    if plan_mtime is not None:
        age_days = (now - plan_mtime) / 86400.0
        if age_days > _PLAN_STALE_DAYS:
            return f"plan.md last touched {int(age_days)} days ago"

    if project_path is None:
        return None
    db_path = project_path / ".pollypm" / "state.db"
    if not db_path.exists():
        return None
    try:
        from pollypm.plugins_builtin.project_planning.plan_presence import (
            _find_approved_plan_task,
            _plan_approved_at,
        )
        from pollypm.work.sqlite_service import SQLiteWorkService
    except Exception:  # noqa: BLE001
        return None
    try:
        with SQLiteWorkService(
            db_path=db_path, project_path=project_path,
        ) as svc:
            plan_task = _find_approved_plan_task(svc, project_key)
            if plan_task is None:
                return None
            approved_at = _plan_approved_at(svc, plan_task)
            if approved_at is None:
                return None
            # Find latest non-planning task's created_at.
            tasks = svc.list_tasks(project=project_key)
            latest_backlog: float | None = None
            for t in tasks:
                flow = getattr(t, "flow_template_id", "") or ""
                if flow in ("plan_project", "critique_flow"):
                    continue
                created = getattr(t, "created_at", None)
                if created is None:
                    continue
                try:
                    if hasattr(created, "timestamp"):
                        ts = created.timestamp()
                    else:
                        ts = _dt.fromisoformat(str(created)).timestamp()
                except (ValueError, TypeError):
                    continue
                if latest_backlog is None or ts > latest_backlog:
                    latest_backlog = ts
            if latest_backlog is not None and approved_at < latest_backlog:
                return "plan approved before latest backlog task"
    except Exception:  # noqa: BLE001
        return None
    return None


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
        "plan_text",
        "plan_aux_files",
        "plan_mtime",
        "plan_stale_reason",
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
        plan_text: str | None,
        plan_aux_files: list[Path],
        plan_mtime: float | None,
        plan_stale_reason: str | None,
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
        self.plan_text = plan_text
        self.plan_aux_files = plan_aux_files
        self.plan_mtime = plan_mtime
        self.plan_stale_reason = plan_stale_reason
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
        plan_text: str | None = None
        plan_mtime: float | None = None
        if plan_path is not None:
            try:
                plan_text = plan_path.read_text(encoding="utf-8")
                plan_sections = _extract_h2_sections(plan_text)
            except OSError:
                plan_sections = []
                plan_text = None
            try:
                plan_mtime = plan_path.stat().st_mtime
            except OSError:
                plan_mtime = None
        else:
            plan_sections = []
        plan_explainer = _dashboard_plan_explainer(project_path, project_key)
        plan_aux_files = _dashboard_plan_aux_files(project_path)
        plan_stale_reason = _dashboard_plan_staleness(
            plan_path, plan_mtime, project_path, project_key,
        )
        activity_entries = _dashboard_activity(config_path, project_key)
    else:
        counts = {}
        buckets = {}
        inbox_count = 0
        inbox_top = []
        plan_path = None
        plan_sections = []
        plan_explainer = None
        plan_text = None
        plan_aux_files = []
        plan_mtime = None
        plan_stale_reason = None
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
        plan_text=plan_text,
        plan_aux_files=plan_aux_files,
        plan_mtime=plan_mtime,
        plan_stale_reason=plan_stale_reason,
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
    #proj-plan-scroll {
        height: auto;
        max-height: 30;
        background: #0c1116;
        padding: 0 1 0 1;
        scrollbar-size: 1 1;
        scrollbar-color: #2a3340;
    }
    #proj-plan-scroll.-plan-focus {
        max-height: 100vh;
        height: 1fr;
    }
    #proj-plan-content {
        color: #d6dee5;
    }
    #proj-plan-stale {
        color: #6b7a88;
        padding-top: 0;
    }
    .proj-section.-hidden {
        display: none;
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
        Binding("v", "open_explainer", "Explainer", show=False),
        Binding("o", "open_editor", "Editor", show=False),
        Binding("j", "plan_scroll_down", "Scroll down", show=False),
        Binding("k", "plan_scroll_up", "Scroll up", show=False),
        Binding("g", "plan_scroll_top", "Top", show=False),
        Binding("G", "plan_scroll_bottom", "Bottom", show=False),
        Binding("u,r", "refresh", "Refresh", show=False),
        Binding("a", "view_alerts", "Alerts", show=False),
        Binding("colon", "open_command_palette", "Palette", priority=True),
        Binding("question_mark", "show_keyboard_help", "Help", priority=True),
        Binding("q,escape", "back", "Back"),
    ]

    def action_open_command_palette(self) -> None:
        _open_command_palette(self)

    def action_show_keyboard_help(self) -> None:
        _open_keyboard_help(self)

    _DEFAULT_HINT = (
        "c chat \u00b7 p plan \u00b7 i inbox \u00b7 l log \u00b7 q back"
    )
    _PLAN_VIEW_HINT = (
        "j/k scroll \u00b7 g/G top/bottom \u00b7 v explainer "
        "\u00b7 o editor \u00b7 p back \u00b7 q exit"
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
        self.plan_stale = Static(
            "", id="proj-plan-stale", markup=True,
        )
        self.plan_content = Static(
            "", id="proj-plan-content", markup=True,
        )
        self.plan_scroll = VerticalScroll(
            self.plan_content, id="proj-plan-scroll",
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
        # When True, the plan section takes over the whole body — other
        # sections are hidden via the ``proj-plan-focus`` screen class.
        self._plan_view_mode: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="proj-outer"):
            yield self.topbar
            yield self.status_line
            with VerticalScroll(id="proj-body"):
                with Vertical(classes="proj-section", id="proj-now-section"):
                    yield self.now_title
                    yield self.now_body
                with Vertical(classes="proj-section", id="proj-pipeline-section"):
                    yield self.pipeline_title
                    yield self.pipeline_body
                with Vertical(classes="proj-section", id="proj-plan-section"):
                    yield self.plan_title
                    yield self.plan_body
                    yield self.plan_stale
                    yield self.plan_scroll
                with Vertical(classes="proj-section", id="proj-activity-section"):
                    yield self.activity_title
                    yield self.activity_body
                with Vertical(classes="proj-section", id="proj-inbox-section"):
                    yield self.inbox_title
                    yield self.inbox_body
        yield self.hint

    def on_mount(self) -> None:
        self._refresh()
        self.set_interval(self.REFRESH_INTERVAL_SECONDS, self._refresh)
        # Live alert toasts.
        _setup_alert_notifier(self, bind_a=True)

    def action_view_alerts(self) -> None:
        _action_view_alerts(self)

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
        self.plan_stale.update(self._render_plan_stale(data))
        self.plan_content.update(self._render_plan_content(data))

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
        if data.plan_aux_files:
            lines.append("")
            lines.append("[dim]Also in docs/plan/:[/dim]")
            for aux in data.plan_aux_files:
                try:
                    aux_rel = str(aux.relative_to(data.project_path))
                except (ValueError, TypeError):
                    aux_rel = aux.name
                lines.append(f"  \u00b7 {_escape(aux_rel)}")
        if data.plan_explainer is not None:
            lines.append("")
            lines.append("[dim]Press [b]v[/b] to open the visual explainer[/dim]")
        return "\n".join(lines)

    def _render_plan_stale(self, data: ProjectDashboardData) -> str:
        """Return the staleness warning line, or empty string when fresh."""
        if data.plan_stale_reason:
            return f"[dim]\u26a0 plan may be stale \u2014 {_escape(data.plan_stale_reason)}[/dim]"
        return ""

    def _render_plan_content(self, data: ProjectDashboardData) -> str:
        """Render ``plan.md`` inline via the existing ``_md_to_rich`` helper.

        Returns an empty string when there's no plan — the VerticalScroll
        container is still present but renders nothing, so the plan
        section simply stays compact.
        """
        text = data.plan_text
        if not text:
            return ""
        try:
            return _md_to_rich(_escape_body(text))
        except Exception:  # noqa: BLE001
            # Defensive — never let a malformed plan crash the dashboard.
            return _escape_body(text)

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
        """Route to the activity feed with this project's filter preloaded.

        The router accepts ``activity:<project_key>`` and forwards the
        filter through ``pm cockpit-pane activity --project <key>`` so
        the user lands on a scoped view, not the global firehose.
        """
        router = CockpitRouter(self.config_path)
        router.route_selected(f"activity:{self.project_key}")

    def action_open_plan(self) -> None:
        """Toggle plan-view mode — plan.md takes over the body.

        When no plan exists, friendly-notify instead of flipping a mode
        with nothing to show.
        """
        data = self.data
        if data is None or data.plan_path is None:
            self.notify(
                "No plan file yet for this project.",
                severity="warning", timeout=2.0,
            )
            return
        self._plan_view_mode = not self._plan_view_mode
        other_section_ids = (
            "#proj-now-section",
            "#proj-pipeline-section",
            "#proj-activity-section",
            "#proj-inbox-section",
        )
        try:
            if self._plan_view_mode:
                for sid in other_section_ids:
                    try:
                        self.query_one(sid).add_class("-hidden")
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    self.plan_scroll.add_class("-plan-focus")
                except Exception:  # noqa: BLE001
                    pass
                self.hint.update(self._PLAN_VIEW_HINT)
                # Scroll the plan content to the top so every toggle
                # starts from a predictable position.
                try:
                    self.plan_scroll.scroll_home(animate=False)
                except Exception:  # noqa: BLE001
                    pass
            else:
                for sid in other_section_ids:
                    try:
                        self.query_one(sid).remove_class("-hidden")
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    self.plan_scroll.remove_class("-plan-focus")
                except Exception:  # noqa: BLE001
                    pass
                self.hint.update(self._DEFAULT_HINT)
        except Exception:  # noqa: BLE001
            pass

    def action_open_explainer(self) -> None:
        """Open the plan-review HTML explainer in the system browser.

        No-op with a friendly notify when the explainer artifact is
        absent — shipping ``v`` is cheap even without an explainer on
        disk because we just warn and move on.
        """
        data = self.data
        if data is None or data.plan_explainer is None:
            self.notify(
                "No plan-review explainer found (reports/plan-review.html).",
                severity="warning", timeout=2.0,
            )
            return
        try:
            self._open_external(data.plan_explainer)
        except Exception as exc:  # noqa: BLE001
            self.notify(
                f"Open failed: {exc}", severity="error", timeout=3.0,
            )
            return
        self.notify(
            f"Opened {data.plan_explainer.name}",
            severity="information", timeout=2.0,
        )

    def action_open_editor(self) -> None:
        """Open ``plan.md`` in the user's preferred viewer (``open`` on mac).

        Best-effort — on platforms where ``open``/``xdg-open`` is absent
        we notify instead of crashing the dashboard.
        """
        data = self.data
        if data is None or data.plan_path is None:
            self.notify(
                "No plan file to open.",
                severity="warning", timeout=2.0,
            )
            return
        try:
            self._open_external(data.plan_path)
        except Exception as exc:  # noqa: BLE001
            self.notify(
                f"Open failed: {exc}", severity="error", timeout=3.0,
            )
            return
        self.notify(
            f"Opened {data.plan_path.name}",
            severity="information", timeout=2.0,
        )

    def _open_external(self, path: Path) -> None:
        """Shell out to the platform opener. Test seam — monkeypatch this."""
        import platform
        cmd = "open" if platform.system() == "Darwin" else "xdg-open"
        subprocess.run([cmd, str(path)], check=False)

    # ── Plan-view scroll helpers (active when ``_plan_view_mode`` is on) ──

    def action_plan_scroll_down(self) -> None:
        if not self._plan_view_mode:
            return
        try:
            self.plan_scroll.scroll_down(animate=False)
        except Exception:  # noqa: BLE001
            pass

    def action_plan_scroll_up(self) -> None:
        if not self._plan_view_mode:
            return
        try:
            self.plan_scroll.scroll_up(animate=False)
        except Exception:  # noqa: BLE001
            pass

    def action_plan_scroll_top(self) -> None:
        if not self._plan_view_mode:
            return
        try:
            self.plan_scroll.scroll_home(animate=False)
        except Exception:  # noqa: BLE001
            pass

    def action_plan_scroll_bottom(self) -> None:
        if not self._plan_view_mode:
            return
        try:
            self.plan_scroll.scroll_end(animate=False)
        except Exception:  # noqa: BLE001
            pass
