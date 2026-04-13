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
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, ListItem, ListView, Static

from pollypm.models import ProviderKind
from pollypm.config import load_config
from pollypm.service_api import PollyPMService
from pollypm.cockpit import CockpitItem, CockpitRouter, build_cockpit_detail
from pollypm.supervisor import Supervisor


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
        if item.state.endswith("live") or item.state.endswith("working"):
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
        self.body.update(text)

    def _indicator(self) -> tuple[str, str]:
        if self.item.state.endswith("working"):
            return self.item.state.split(" ", 1)[0], "#3ddc84"
        if self.item.state == "unread":
            return "\u25cf", "#f0a030"
        if self.item.state.endswith("live"):
            return "\u25cf", "#3ddc84"
        if self.item.state.startswith("!"):
            return "\u25b2", "#ff5f6d"
        if self.item.key == "polly":
            if self.item.state in {"ready", "idle"}:
                return "\u2022", "#5b8aff"
            if self.item.state.endswith("working"):
                return self.item.state.split(" ", 1)[0], "#3ddc84"
            return "\u2022", "#5b8aff"
        if self.item.key == "settings":
            return "\u2699", "#6b7a88"
        if self.item.key == "inbox":
            label = self.item.label
            if "(" in label and not label.endswith("(0)"):
                return "\u25c6", "#f0c45a"
            return "\u25c7", "#4a5568"
        if self.item.state == "sub":
            return " ", "#4a5568"
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
        scrollbar-size: 0 0;
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
        # Delay the cockpit layout split so Textual finishes its first render
        # before the pane is resized.  An immediate split sends SIGWINCH before
        # the alternate screen is active, which can leave the pane in copy mode.
        self.set_timer(0.5, self._deferred_layout)

    def _deferred_layout(self) -> None:
        # Layout is now handled by pm up / ensure_cockpit_layout at startup,
        # NOT from inside the running TUI. Splitting panes while the TUI is
        # rendering sends SIGWINCH that can crash the Textual event loop.
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
                if left_pane.pane_width != self.router._LEFT_PANE_WIDTH:
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
                config = load_config(self.config_path)
                supervisor = Supervisor(config)
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
            from pollypm.supervisor import Supervisor
            supervisor = Supervisor(config)
            supervisor.stop_session(worker.name, force=True)
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
            from pollypm.supervisor import Supervisor
            supervisor = Supervisor(config)
            supervisor.switch_session_account(worker.name, target_account)
            self._notify(f"Switched to {target_provider.value} ({target_account}). Session restarted.")
        except Exception as exc:  # noqa: BLE001
            self._notify(f"Switch failed: {exc}")
        self._refresh()


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
        config = load_config(self.config_path)
        statuses = self.service.list_account_statuses()
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
