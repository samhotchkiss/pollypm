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
import json
import os
import resource
from pathlib import Path
import subprocess
import time
from typing import Callable, TYPE_CHECKING

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
from textual.widgets import (
    Button,
    DataTable,
    Input,
    ListItem,
    ListView,
    Select,
    Static,
)

from pollypm.approval_notifications import notify_task_approved
from pollypm.cockpit_formatting import format_relative_age as _format_relative_age
from pollypm.model_registry import advisories_for, load_registry, resolve_alias
from pollypm.models import ModelAssignment, ProviderKind
from pollypm.role_routing import resolve_role_assignment
from pollypm.account_usage_sampler import load_cached_account_usage
from pollypm.tz import format_time as _fmt_time
from pollypm.cockpit_activity import (  # noqa: F401  (re-exported)
    PollyActivityFeedApp,
    _activity_type_colour,
)
from pollypm.cockpit_alerts import (  # noqa: F401  (AlertToast re-exported)
    AlertToast,
    _action_view_alerts,
    _setup_alert_notifier,
)
from pollypm.cockpit_inbox import (
    InboxThreadRow,
    build_inbox_thread_rows,
    inbox_thread_left_action,
    inbox_thread_right_action,
)
from pollypm.cockpit_inbox_items import (
    annotate_inbox_entry,
    is_task_inbox_entry,
    load_inbox_entries,
    message_row_to_inbox_entry,
    task_to_inbox_entry,
)
from pollypm.cockpit_metrics import (  # noqa: F401  (re-exported)
    PollyMetricsApp,
    _MetricsDrillDownModal,
)
from pollypm.config import load_config, write_config
from pollypm.cockpit_palette import (  # noqa: F401  (re-exported)
    CommandPaletteModal,
    KeyboardHelpModal,
    _PaletteListItem,
    _PaletteSectionHeader,
    _dispatch_palette_tag,
    _open_command_palette,
    _open_keyboard_help,
    _palette_history,
    _palette_nav,
    _record_palette_command,
    _resolve_recent_commands,
)
from pollypm.cockpit_project_settings import PollyProjectSettingsApp  # noqa: F401
from pollypm.cockpit_sections.action_bar import render_project_action_bar
from pollypm.cockpit_settings_accounts import SETTINGS_ACCOUNT_ACTIONS
from pollypm.cockpit_settings_history import (
    UndoAction,
    consume_settings_history,
    history_rationale_for_account,
    history_rationale_for_project,
    latest_settings_history_entry,
    load_settings_history,
    make_undo_action,
    record_settings_history,
    undo_expired,
    undo_expires_text,
)
from pollypm.cockpit_settings_projects import collect_settings_projects
from pollypm.cockpit_workers import PollyWorkerRosterApp  # noqa: F401
from pollypm.rejection_feedback import (
    feedback_target_task_id,
    is_rejection_feedback_task,
)
# ``PollyPMService`` pulls supervisor → sqlalchemy on import (~211ms
# cumulative on cold spawn). Every cockpit pane (project dashboard,
# inbox, tasks) eats that cost even when the pane never instantiates
# a service. Defer to per-method local imports; keep the symbol
# visible to type-checkers under TYPE_CHECKING for the function
# parameter annotations below (``service: PollyPMService | None``).
if TYPE_CHECKING:
    from pollypm.service_api import PollyPMService  # noqa: F401
from pollypm.cockpit import build_cockpit_detail
from pollypm.cockpit_rail import CockpitItem, CockpitPresence, CockpitRouter


import re as _re


_INLINE_BOLD_RE = _re.compile(r"\*\*(.+?)\*\*")
_INLINE_ITALIC_RE = _re.compile(r"\*(.+?)\*")
_INLINE_CODE_RE = _re.compile(r"`(.+?)`")
_ORDERED_LIST_RE = _re.compile(r"\s*\d+\.\s")
_PROJECT_TASK_REF_RE = _re.compile(
    r"\b(?P<project>[A-Za-z0-9_][A-Za-z0-9_-]*)/(?P<number>\d+)\b"
)
_ACTION_STEP_RE = _re.compile(
    r"^\s*(?:[-*]\s+|\d+\.\s+|\([a-zA-Z]\)\s+)(?P<step>.+\S)\s*$"
)


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
            line = _INLINE_BOLD_RE.sub(r"[b]\1[/b]", line)
            line = _INLINE_ITALIC_RE.sub(r"[i]\1[/i]", line)
            line = _INLINE_CODE_RE.sub(r"[dim]\1[/dim]", line)
            # Bullet points
            if line.strip().startswith("- "):
                indent = len(line) - len(line.lstrip())
                content = line.strip()[2:]
                lines.append(f"{'  ' * (indent // 2)}  • {content}")
            elif _ORDERED_LIST_RE.match(line):
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

POLLY_TAGLINE = "Plans first.\nChaos later."

_PALETTE_TIP_MESSAGE = "Tip: press `:` to open the command palette."


_FIRST_SHIPPED_FRAMES = (
    "  ✨   🎉   ✨\n🎊  First PR shipped  🎊\n  ✨   🎉   ✨",
    "🎉   ✨   🎊   ✨\n  First PR shipped\n✨   🎊   ✨   🎉",
    "  🎊   ✨   🎉\n🎉  First PR shipped  🎉\n  ✨   🎊   ✨",
)


class _FirstShippedCelebrationModal(ModalScreen[None]):
    """Short-lived modal that celebrates the first shipped task."""

    DEFAULT_CSS = """
    #first-shipped-modal {
        width: 60;
        padding: 1 2;
        border: round #6fcf97;
        background: #102019;
        color: #effaf3;
    }
    #first-shipped-title {
        text-align: center;
        margin-bottom: 1;
    }
    #first-shipped-confetti {
        text-align: center;
        color: #ffd166;
        height: auto;
    }
    #first-shipped-hint {
        text-align: center;
        color: #93a7b3;
        margin-top: 1;
    }
    """

    BINDINGS = [Binding("escape", "dismiss", "Dismiss", show=False)]

    def __init__(self) -> None:
        super().__init__()
        self._frame_index = 0

    def compose(self) -> ComposeResult:  # pragma: no cover - Textual harness
        with Vertical(id="first-shipped-modal"):
            yield Static("First PR shipped", id="first-shipped-title")
            yield Static(_FIRST_SHIPPED_FRAMES[0], id="first-shipped-confetti", markup=True)
            yield Static(
                "Recorded once and pinned in Activity.",
                id="first-shipped-hint",
            )

    def on_mount(self) -> None:  # pragma: no cover - Textual harness
        try:
            self.set_interval(0.16, self._advance_frame)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.set_timer(2.0, self.dismiss)
        except Exception:  # noqa: BLE001
            pass

    def _advance_frame(self) -> None:
        self._frame_index = (self._frame_index + 1) % len(_FIRST_SHIPPED_FRAMES)
        try:
            self.query_one("#first-shipped-confetti", Static).update(
                _FIRST_SHIPPED_FRAMES[self._frame_index],
            )
        except Exception:  # noqa: BLE001
            pass


def _celebrate_first_shipped(app) -> None:
    """Announce the one-time shipped milestone in whichever cockpit view approved it."""
    app.notify("🎉 First PR shipped. Nicely done.", severity="information", timeout=2.0)
    if os.getenv("POLLY_NO_CONFETTI") == "1":
        return
    try:
        app.push_screen(_FirstShippedCelebrationModal())
    except Exception:  # noqa: BLE001
        pass


def _wrap_alert_reason(reason: str, *, width: int = 28, max_lines: int = 4) -> list[str]:
    """Break ``reason`` into ≤``max_lines`` display lines of ≤``width`` chars.

    Word-wraps on whitespace so a 120-char alert like
    ``"Window pm-operator has produced effectively the same
    snapshot for 3 heartbeats"`` displays as three dim subtitle
    lines instead of getting chopped at 18 characters — the
    pre-fix behavior that rendered rail toasts unreadable on
    2026-04-20.

    Returns at most ``max_lines`` lines; the final line gets an
    ellipsis when the text ran long. Always returns at least one
    line, even for empty or whitespace-only input.
    """
    if not reason:
        return [""]
    words = reason.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = ""
    for word in words:
        if not current:
            current = word
            continue
        if len(current) + 1 + len(word) <= width:
            current = f"{current} {word}"
        else:
            lines.append(current)
            current = word
            if len(lines) >= max_lines:
                break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) == max_lines and (len(lines) * (width + 1)) < len(reason):
        # Text overflowed the budget — signal it with an ellipsis.
        trimmed = lines[-1][: max(0, width - 1)] + "\u2026"
        lines[-1] = trimmed
    return lines


def _rail_alert_subtitle_width(*, rail_width: int = 30, indent: int = 4) -> int:
    """Visible text budget for wrapped rail alert subtitles.

    The rail pane is 30 columns by default. Subtitle lines are rendered on
    their own line with a 4-space indent, so the actual payload width must fit
    inside ``rail_width - indent``. The previous hard-coded ``28`` overflowed
    the real rail budget and still clipped in production.
    """
    return max(12, rail_width - indent)


class RailItem(ListItem):
    def __init__(
        self,
        item: CockpitItem,
        *,
        active_view: bool,
        first_project: bool = False,
        presence: CockpitPresence | None = None,
        spinner_index: int = 0,
    ) -> None:
        self.body = Static(classes="rail-item-body")
        self.item = item
        self.presence = presence
        self.spinner_index = spinner_index
        super().__init__(self.body, classes="rail-row", disabled=not item.selectable)
        self.apply_item(
            item,
            active_view=active_view,
            first_project=first_project,
            spinner_index=spinner_index,
        )

    @property
    def cockpit_key(self) -> str:
        return self.item.key

    def apply_item(
        self,
        item: CockpitItem,
        *,
        active_view: bool,
        first_project: bool,
        spinner_index: int | None = None,
    ) -> None:
        self.item = item
        if spinner_index is not None:
            self.spinner_index = spinner_index
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
            # Project rows decorate the label as "<emoji>? <name> <spark>".
            # Truncating from the end clobbers the activity sparkline \u2014
            # the highest-signal part. Detect the trailing 10-char spark
            # and truncate the project name instead so the spark
            # survives narrow rail widths.
            from pollypm.cockpit_rail import _strip_trailing_spark
            head, spark = _strip_trailing_spark(label)
            if spark:
                reserved = len(spark) + 1  # spark + separator space
                head_budget = max_label - reserved - 1  # ellipsis
                if head_budget > 3:
                    label = (
                        head[:head_budget].rstrip()
                        + "\u2026 "
                        + spark
                    )
                else:
                    label = label[: max_label - 1] + "\u2026"
            else:
                label = label[: max_label - 1] + "\u2026"
        text.append(label)
        # Show alert reason as dim subtitle for items with alerts.
        # Wrap onto up to 3 lines (≈60 chars each, indented) instead of
        # truncating at 18 chars — Sam on 2026-04-20 reported rail
        # toasts cut off mid-word. The indent keeps them visually
        # attached to the owning item while still readable.
        if self.item.state.startswith("!"):
            reason = self.item.state[2:].strip()  # strip "! " prefix
            if reason:
                for chunk in _wrap_alert_reason(
                    reason,
                    width=_rail_alert_subtitle_width(),
                    max_lines=4,
                ):
                    text.append(f"\n    {chunk}", style="#ff5f6d dim")
        self.body.update(text)

    def _indicator(self) -> tuple[str, str]:
        presence = self.presence
        if (
            presence is not None
            and self.item.session_name
            and self.item.work_state
        ):
            pulse = presence.heartbeat_frame_for(
                self.item.session_name,
                self.item.heartbeat_at,
            )
            work_glyph, color = self._session_work_glyph(self.item.work_state)
            return f"{pulse}{work_glyph}", color
        if presence is not None and self.item.state in {"heartbeat", "watch"}:
            return presence.heartbeat_frame(self.spinner_index), "#3ddc84"
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
                if presence is not None:
                    return presence.working_frame(self.spinner_index), "#3ddc84"
                return "\u25c6", "#f0c45a"  # yellow diamond — active task
            return "\u25cb", "#4a5568"  # dim circle — idle
        # Generic "<glyph> working" state — top-level rail rows
        # (e.g. Workers when any worker is currently turning) used
        # to fall through to the idle circle below, so a
        # ``◆ working`` state from the state-provider was
        # cosmetically indistinguishable from idle. Honour the
        # state with the spinner / active diamond.
        if self.item.state.endswith("working"):
            if presence is not None:
                return presence.working_frame(self.spinner_index), "#3ddc84"
            return "\u25c6", "#f0c45a"
        return "\u25cb", "#4a5568"

    def _session_work_glyph(self, work_state: str) -> tuple[str, str]:
        presence = self.presence
        if work_state == "writing":
            if presence is not None:
                if not presence.should_animate():
                    return "…", "#3ddc84"
                return presence.working_frame(self.spinner_index), "#3ddc84"
            return "\u25c6", "#3ddc84"
        if work_state == "reviewing":
            return "\u270e", "#3ddc84"
        if work_state == "stuck":
            return "\u26a0", "#ff5f6d"
        if work_state == "exited":
            return "\u2715", "#4a5568"
        return "\u00b7", "#4a5568"


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
    #update-pill {
        height: 1;
        padding: 0 1;
        color: #7aa2f7;
        background: transparent;
        text-style: bold;
    }
    #ticker {
        height: 1;
        padding: 0 1;
        color: #4a5568;
        background: transparent;
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
        Binding("i", "open_inbox", "Inbox", show=False, priority=True),
        Binding("t", "open_activity", "Activity"),
        Binding("p", "toggle_project_pin", "Pin Project"),
        Binding("r", "refresh", "Refresh"),
        Binding("s", "open_settings", "Settings"),
        Binding("tab", "forward_tab_to_right", "Right Pane", show=False, priority=True),
        Binding("A", "forward_workers_auto_refresh", "Workers Auto", show=False, priority=True),
        # Forward Action Needed numbered buttons (1/2/3) from the rail
        # to the right pane (#862). The cards advertise "Use 1/2/3 for
        # the buttons below"; without this forward, those keystrokes are
        # silently dropped while the rail tmux pane is the focused pane.
        Binding("1", "forward_action_button_1", "Action 1", show=False, priority=True),
        Binding("2", "forward_action_button_2", "Action 2", show=False, priority=True),
        Binding("3", "forward_action_button_3", "Action 3", show=False, priority=True),
        # Forward project-dashboard keys (#863). The Plan card on idle
        # projects says "Press c to ask the PM to plan it now."; ``l``
        # jumps to the project log. Both must round-trip from the rail
        # whenever a project surface is up in the right pane.
        Binding("c", "forward_project_chat", "Chat PM", show=False, priority=True),
        Binding("l", "forward_project_log", "Project log", show=False, priority=True),
        Binding("u", "trigger_upgrade", "Upgrade", show=False),
        Binding("x", "dismiss_update_pill", "Dismiss Update", show=False),
        Binding("a", "view_alerts", "Alerts", show=False),
        Binding("ctrl+k,colon", "open_command_palette", "Palette", priority=True),
        Binding(
            "question_mark",
            "show_keyboard_help",
            "Help: pulse ♥/♡, writing ◜◝◞◟, review ✎, idle ·, stuck ⚠, exited ✕",
            priority=True,
        ),
        # j/k/arrows are priority bindings (#797). Without ``priority``,
        # any sibling widget that grabs focus (e.g. the right-pane app
        # or a modal that just dismissed) intercepts the keystroke and
        # the rail's cursor stops moving — the user has to Tab back
        # to the nav before navigation responds again.
        Binding("j,down", "cursor_down", "Down", show=False, priority=True),
        Binding("k,up", "cursor_up", "Up", show=False, priority=True),
        Binding("g,home", "cursor_first", "First", show=False, priority=True),
        Binding("G,end", "cursor_last", "Last", show=False, priority=True),
        Binding("q,Q,ctrl+q", "request_quit", "Quit", priority=True),
        Binding("escape", "back_to_home", "Back to Home", show=False, priority=True),
        Binding("w,W,ctrl+w", "detach", "Detach", priority=True),
    ]

    def action_open_command_palette(self) -> None:
        _open_command_palette(self)

    def action_show_keyboard_help(self) -> None:
        _open_keyboard_help(self)

    # Keys the App's BINDINGS treat as priority. When KeyboardHelpModal
    # is on the screen stack we must yield these to the modal so it can
    # dismiss / scroll itself (#917). Textual's priority pass walks from
    # App down, so without this gate the App's priority binding always
    # wins over the modal's own priority binding for the same key.
    _HELP_MODAL_GATED_ACTIONS = frozenset({
        "request_quit",          # q, Q, ctrl+q
        "back_to_home",          # escape
        "show_keyboard_help",    # ?  (so reopening on top is suppressed,
                                 #     letting the modal close itself)
        "cursor_down",           # j, down
        "cursor_up",             # k, up
        "cursor_first",          # g, home
        "cursor_last",           # G, end
        "open_inbox",            # i
        "forward_tab_to_right",  # tab
        "forward_action_button_1",
        "forward_action_button_2",
        "forward_action_button_3",
        "forward_project_chat",  # c
        "forward_project_log",   # l
        "forward_workers_auto_refresh",  # A
    })

    def check_action(
        self, action: str, parameters: tuple[object, ...],
    ) -> bool | None:
        """Suppress App-level priority bindings while help modal is up.

        Textual checks priority bindings App-down, so without this gate
        the App's ``q`` / ``escape`` / ``j`` / ``k`` bindings fire before
        :class:`KeyboardHelpModal` ever sees the keystroke (#917). When
        the modal is on the screen stack, return ``False`` for the gated
        actions so :meth:`textual.app.App.run_action` skips the App
        binding and the chain falls through to the modal's own binding.
        """
        if action in self._HELP_MODAL_GATED_ACTIONS:
            for screen in self.screen_stack:
                if isinstance(screen, KeyboardHelpModal):
                    return False
        return super().check_action(action, parameters)

    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self.config_path = config_path
        self.router = CockpitRouter(config_path)
        self.presence = CockpitPresence(self.router.tmux)
        from pollypm.service_api import PollyPMService
        self.service = PollyPMService(config_path)
        _lines = ASCII_POLLY.split("\n")
        self.brand = Static(
            f"[#5b8aff]{_lines[0]}[/]\n[#3d6bcc]{_lines[1]}[/]",
            id="brand",
            markup=True,
        )
        self.tagline = Static("\n" + POLLY_TAGLINE, id="tagline")
        self.nav = ListView(id="nav")
        self.settings_row = Static("  \u2699 Settings", id="settings-row")
        self.update_pill = Static("", id="update-pill", markup=True)
        self.ticker = Static("", id="ticker")
        self.hint = Static("", id="hint")
        # True once the user presses ``x`` on the pill — hides it for
        # the remainder of this cockpit session. Re-appears on next
        # cockpit launch if an update is still available.
        self._update_pill_dismissed = False
        self.spinner_index = 0
        self._ticker_started_at = time.monotonic()
        self.selected_key = "polly"
        self._last_router_selected_key = "polly"
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
        # Tick index of the last full ``_refresh_rows`` — used by the
        # rate-limited refresh gate so high-frequency epoch bumps
        # (heartbeats + token samples commit ~10/sec) don't cause the
        # visible row flash Sam reported on 2026-04-20.
        self._last_refresh_tick = -10

    def compose(self) -> ComposeResult:
        with Vertical():
            yield self.brand
            yield self.tagline
            yield self.nav
            yield self.settings_row
            yield self.update_pill
            yield self.ticker
            yield self.hint

    def on_mount(self) -> None:
        self.selected_key = self.router.selected_key()
        self._last_router_selected_key = self.selected_key
        self._refresh_rows()
        self._update_ticker()
        self._update_pill_refresh()
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
        self.call_after_refresh(self._show_palette_tip_once)

    def action_view_alerts(self) -> None:
        _action_view_alerts(self)

    def _show_palette_tip_once(self) -> None:
        try:
            if not self.router.should_show_palette_tip():
                return
            self.router.mark_palette_tip_seen()
            self.notify(_PALETTE_TIP_MESSAGE, timeout=10.0)
        except Exception:  # noqa: BLE001
            pass

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

    def _right_pane_has_live_session(self) -> bool:
        try:
            state = self.router._load_state()
        except Exception:  # noqa: BLE001
            return False
        return bool(state.get("mounted_session"))

    def _send_key_to_right_pane(self, key: str) -> None:
        send_method = getattr(self.router, "send_key_to_right_pane", None)
        if callable(send_method):
            send_method(key)

    def _adopt_router_selection_if_changed(self) -> None:
        try:
            external_key = self.router.selected_key()
        except Exception:  # noqa: BLE001
            return
        if not external_key or external_key == self._last_router_selected_key:
            return
        self._last_router_selected_key = external_key
        self.selected_key = external_key

    # Layout check (pane recovery, rail width) — only every ~30s
    _LAYOUT_CHECK_INTERVAL = 38  # ~30s at 0.8s/tick
    # Force GC every ~2 minutes
    _GC_INTERVAL = 150

    def _tick(self) -> None:
        self._tick_count += 1
        self.spinner_index = (self.spinner_index + 1) % 4
        # Periodic GC
        if self._tick_count % self._GC_INTERVAL == 0:
            gc.collect()
        # Check if state changed (one stat() call — no subprocess, no FD leak).
        #
        # Every StateStore.commit() bumps the epoch — heartbeats,
        # token samples, event rows, checkpoints. At the 10s heartbeat
        # cadence, that's ~10 bumps/sec across a live install, and
        # blindly calling ``_refresh_rows`` on every bump caused the
        # visible flash Sam reported on 2026-04-20. Throttle to at
        # most one full refresh per 2 seconds; between refreshes we
        # still pick up individual bumps for the spinner-only update
        # path below, which is cheap and doesn't reflow the list.
        from pollypm.state_epoch import mtime as epoch_mtime
        current_epoch = epoch_mtime()
        state_changed = current_epoch != self._last_epoch_mtime
        refresh_gate_ticks = 3  # 3 * 0.8s = ~2.4s minimum between refreshes
        gated = (
            state_changed
            and (self._tick_count - self._last_refresh_tick) < refresh_gate_ticks
        )
        if state_changed and not gated:
            self._last_epoch_mtime = current_epoch
            self._last_refresh_tick = self._tick_count
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
                should_animate = row.item.state.endswith("working")
                rewrite_state = should_animate
                if row.item.state in {"heartbeat", "watch"}:
                    should_animate = True
                    rewrite_state = False
                if row.item.session_name and row.item.work_state == "writing":
                    should_animate = True
                    rewrite_state = False
                if should_animate:
                    if rewrite_state:
                        row.item.state = f"{frame} working"
                    row.spinner_index = self.spinner_index
                    row.update_body()
        self._update_ticker()
        # Release-check cache is 24h so polling each tick is cheap —
        # almost always a dict lookup. Only the first tick per cache
        # window touches the network (and that path short-circuits on
        # any failure). This keeps the pill current without a separate
        # timer.
        if self._tick_count % 5 == 0:
            self._update_pill_refresh()
        # Post-upgrade flag lands when ``pm upgrade`` finishes — switch
        # the pill to a restart nudge. Check on every tick so the
        # feedback loop from "upgrade complete" → visible notice is
        # sub-second.
        self._check_post_upgrade_flag()
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
        self._adopt_router_selection_if_changed()
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
        # #797: keep selected_key valid against the visible items so
        # the focus marker doesn't drop off when a previously-selected
        # row (e.g. an expanded project sub-row) leaves the list.
        keys = [item.key for item in nav_items]
        if self.selected_key not in keys and self.selected_key != "settings":
            for item in nav_items:
                if item.selectable:
                    self.selected_key = item.key
                    if selected_key is None or selected_key not in keys:
                        selected_key = item.key
                    break
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
                    presence=self.presence,
                    spinner_index=self.spinner_index,
                )
                self._row_widgets[item.key] = row
            else:
                row = self._row_widgets[item.key]
                row.apply_item(
                    item,
                    active_view=item.key == self.selected_key,
                    first_project=first_project,
                    spinner_index=self.spinner_index,
                )
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
        self._apply_active_view_to_rows()
        if any(item.key == "settings" for item in self._items):
            self.settings_row.display = True
        else:
            self.settings_row.display = False
        self._update_ticker()
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

    def _apply_active_view_to_rows(self) -> None:
        for key, row in self._row_widgets.items():
            active = key == self.selected_key
            if row.has_class("active-view") == active:
                continue
            row.set_class(active, "active-view")
            row.update_body()
        self.settings_row.set_class(self.selected_key == "settings", "active-view")
        update_settings = getattr(self.settings_row, "update", None)
        if callable(update_settings):
            marker = "\u258c " if self.selected_key == "settings" else "  "
            update_settings(f"{marker}\u2699 Settings")

    # Internal infrastructure events that have no signal value to a
    # user reading the rail's events strip (#793). Heartbeat ticks and
    # token-ledger syncs run every few seconds; surfacing them as
    # "events" buries actual project activity.
    # Event types whose appearance in the rail ticker is pure plumbing
    # noise — the user can't act on them, they cycle every few seconds,
    # and they push the actual hint line off-screen on the 30-col rail
    # (#876, #793). The rail is the headline status surface; only event
    # types that map to "something worth noticing" pass through.
    _TICKER_SUPPRESSED_EVENT_TYPES = frozenset({
        "heartbeat",
        "token_ledger",
        "lease",
        "launch",
        "recovered",
        "recovery_prompt",
        "recovery_recommendation",
        "scheduler",
        "operator_lease",
    })

    # Friendly labels for the user-facing event types that *do* surface.
    # Keys are ``event_type`` strings; values are short noun phrases that
    # do not include the session name (which is internal and already
    # makes the ticker too long for the 30-col rail).
    _TICKER_EVENT_LABELS: dict[str, str] = {
        "alert": "alert",
        "task_assigned": "task assigned",
        "task_completed": "task done",
        "plan_approved": "plan ok",
        "plan_rejected": "plan rejected",
        "rework_requested": "rework",
        "review_requested": "review",
    }

    def _event_ticker_text(self) -> str:
        # Gate on real tmux-client attachment (not just isatty). When the
        # user detaches from tmux, animation should stop — see #656.
        try:
            if not self.router._presence().is_tmux_attached():
                return ""
        except Exception:  # noqa: BLE001
            pass  # fall through — render as if attached if gate fails
        try:
            supervisor = self.router._load_supervisor()
            # Pull a wider window than we display so we still have signal
            # after suppressing infra ticks.
            raw_events = list(supervisor.store.recent_events(limit=48))
        except Exception:  # noqa: BLE001
            return ""
        events = [
            e for e in raw_events
            if getattr(e, "event_type", "") not in self._TICKER_SUPPRESSED_EVENT_TYPES
        ]
        if not events:
            return ""
        # #667 acceptance: show the 3 newest events, cycle the window so
        # a re-glance sees a different set. supervisor.recent_events()
        # returns rows in arbitrary order — newest-first comes from the
        # created_at column, which the store already sorts descending.
        window_size = min(3, len(events))
        # Advance one event per 10s so the user sees motion without the
        # header flickering on every cockpit tick.
        offset = int((time.monotonic() - self._ticker_started_at) // 10)
        cycled = [events[(offset + i) % len(events)] for i in range(window_size)]
        labels: list[str] = []
        for event in cycled:
            event_type = getattr(event, "event_type", "event")
            label = self._TICKER_EVENT_LABELS.get(
                event_type, event_type.replace("_", " "),
            )
            if label not in labels:
                labels.append(label)
        if not labels:
            return ""
        return "events · " + " · ".join(labels)

    def _update_ticker(self) -> None:
        ticker_text = self._event_ticker_text()
        self.ticker.update(ticker_text)
        self.ticker.display = bool(ticker_text)

    def _update_pill_refresh(self) -> None:
        """Refresh the update-available pill in the rail top area.

        Shows ``↑ v<latest> available · u: upgrade · x: dismiss`` when
        ``release_check.check_latest`` reports a newer version on the
        active channel. Hidden otherwise — including when dismissed for
        this session, when the check is cached as "up-to-date", and
        when the check is offline or raised.
        """
        if self._update_pill_dismissed:
            self.update_pill.display = False
            return
        try:
            from pollypm.release_check import _resolve_channel, check_latest
            channel = _resolve_channel(None)
            check = check_latest(channel)
        except Exception:  # noqa: BLE001
            self.update_pill.display = False
            return
        if check is None or not check.upgrade_available:
            self.update_pill.display = False
            return
        channel_label = (
            f" ({check.channel})" if check.channel != "stable" else ""
        )
        self.update_pill.update(
            f"[#7aa2f7]↑ v{check.latest} available{channel_label}"
            "[/] · [dim]u: upgrade · x: dismiss[/]"
        )
        self.update_pill.display = True

    def action_trigger_upgrade(self) -> None:
        """Spawn ``pm upgrade`` in a new tmux window.

        Runs in its own window so the rail stays usable during install.
        When the upgrade finishes, ``pm upgrade`` writes a sentinel at
        ``~/.pollypm/post-upgrade.flag`` that ``_tick`` watches for —
        on detection the pill swaps to a "Restart cockpit to pick up
        v<new>" nudge. Auto-restart of the rail + daemons ships in
        #720; for now the user presses ctrl+q to relaunch.

        Non-blocking: a failed spawn falls back to a ``notify()``
        pointing at the CLI path.
        """
        tmux_session = "pollypm"
        try:
            supervisor = self.router._load_supervisor()
            tmux_session = supervisor.config.project.tmux_session
        except Exception:  # noqa: BLE001
            pass

        try:
            self.router.tmux.create_window(
                tmux_session,
                "pm-upgrade",
                "pm upgrade; echo; echo '(press enter to close)'; read",
                detached=False,
            )
        except Exception:  # noqa: BLE001
            try:
                self.notify(
                    "Could not open a new tmux window. Run "
                    "`pm upgrade` directly in a terminal.",
                    timeout=6,
                )
            except Exception:  # noqa: BLE001
                pass
            return

        self.update_pill.update(
            "[#e0af68]Upgrading… see window `pm-upgrade`[/]"
        )
        self.update_pill.display = True
        try:
            self.notify(
                "Upgrade started in window `pm-upgrade`. Keep working; "
                "the rail will tell you when it's done.",
                timeout=5,
            )
        except Exception:  # noqa: BLE001
            pass

    def _post_upgrade_flag_path(self) -> Path:
        """Sentinel written by ``pm upgrade`` on success.

        Content is JSON:
        ``{"from": "0.1.0", "to": "0.2.0", "at": 1234567890.0}``.
        The rail reads it to update the pill. ``pm upgrade`` writes it
        atomically (tempfile + rename) so we never see a partial read.
        Cleanup lives in #720's post-upgrade summary flow — the flag
        sticks until the user dismisses the summary.
        """
        return Path.home() / ".pollypm" / "post-upgrade.flag"

    def _check_post_upgrade_flag(self) -> None:
        """Swap the pill to "restart to pick up new code" when the
        sentinel appears.

        Only updates the pill; does NOT delete the flag so a cockpit
        restart can pick up where we left off.
        """
        if self._update_pill_dismissed:
            return
        flag = self._post_upgrade_flag_path()
        if not flag.exists():
            return
        try:
            payload = json.loads(flag.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            # Corrupt flag file (list/null/string) — fall back to ``?``
            # rather than raising AttributeError on the .get below.
            payload = {}
        new_version = str(payload.get("to") or "?")
        old_version = str(payload.get("from") or "?")
        # #720 — render the richer post-upgrade summary when the
        # newer payload fields are present. Old flags written by
        # pre-#720 builds only carry ``from``/``to``, so fall back
        # to the legacy "restart to pick up new code" copy when the
        # counts are missing.
        notified = payload.get("notified")
        recycled = payload.get("recycled")
        pending = payload.get("pending_restart")
        if any(v is not None for v in (notified, recycled, pending)):
            full_parts: list[str] = [
                f"Upgraded v{old_version} → v{new_version}"
            ]
            if isinstance(notified, int) and notified:
                full_parts.append(f"{notified} notified")
            if isinstance(pending, int) and pending:
                full_parts.append(f"{pending} pending restart")
            if isinstance(recycled, int) and recycled:
                full_parts.append(f"{recycled} recycled")
            full_parts.append("ctrl+q to restart cockpit")
            self.update_pill.tooltip = " · ".join(full_parts)
            if isinstance(pending, int) and pending:
                self.update_pill.update(
                    f"[#9ece6a]✓ Upgraded[/] · "
                    f"[#d29922]{pending} pending[/] · [dim]ctrl+q[/dim]"
                )
            else:
                self.update_pill.update(
                    "[#9ece6a]✓ Upgraded[/] · [dim]ctrl+q restart[/dim]"
                )
        else:
            self.update_pill.tooltip = (
                f"Upgraded to v{new_version} · ctrl+q to restart cockpit"
            )
            self.update_pill.update(
                "[#9ece6a]✓ Upgraded[/] · [dim]ctrl+q restart[/dim]"
            )
        self.update_pill.display = True

    def action_dismiss_update_pill(self) -> None:
        """Hide the update pill for this cockpit session.

        Dismissal is session-scoped — the pill re-appears on next
        cockpit launch if the upgrade is still available. This keeps
        the nudge visible to future-you even if current-you is busy.
        """
        self._update_pill_dismissed = True
        self.update_pill.display = False

    _HEARTBEAT_STALE_SECONDS = 180  # warn if no heartbeat in 3 minutes

    def _update_hint(self) -> None:
        # Keep this hint short enough to fit a 30-col rail without
        # wrapping. Anything beyond j/k/\u21b5/?/q is discoverable via the
        # ``?`` overlay (#790). Width budget is roughly 28 chars after
        # the leading pad applied by Textual.
        if self._right_pane_has_live_session():
            hint_text = "Tab detail \u00b7 j/k \u00b7 ? help"
        else:
            hint_text = "j/k \u21b5open \u00b7 ? help \u00b7 q quit"
        try:
            supervisor = self.router._load_supervisor()
            last_hb = supervisor.store.last_heartbeat_at()
            if last_hb:
                from datetime import UTC, datetime
                # Unified ``messages`` table stores SQLite's default
                # ``CURRENT_TIMESTAMP`` which is naive-UTC
                # (``YYYY-MM-DD HH:MM:SS``, no ``+00:00``). Force UTC
                # so ``datetime.now(UTC) - parsed`` doesn't raise
                # ``can't subtract offset-naive and offset-aware``.
                parsed = datetime.fromisoformat(last_hb.replace(" ", "T"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                elapsed = (datetime.now(UTC) - parsed).total_seconds()
                if elapsed > self._HEARTBEAT_STALE_SECONDS:
                    mins = int(elapsed // 60)
                    hint_text = f"\u26a0 Heartbeat offline ({mins}m) \u2014 run `pm heartbeat install`"
        except Exception:  # noqa: BLE001
            pass
        self.hint.update(hint_text)

    def _sync_selected_from_nav(self) -> None:
        """Update selected_key from the current ListView cursor position."""
        key = self._selected_row_key()
        if key is not None:
            self.selected_key = key
            self._last_nav_change = self._tick_count
            self._apply_active_view_to_rows()

    # j/k always navigate the rail's own item list — including
    # stepping over the ``── projects ──`` divider (which is a
    # ``disabled=True`` ListItem so Textual's ListView correctly
    # skips it) and remaining a no-op once the cursor reaches the
    # first/last selectable row. The keys are claimed by the rail's
    # priority binding regardless of cursor position; we never call
    # ``tmux send-keys`` for j/k, which previously leaked the byte
    # into whatever process owned the right-pane TTY (#918, reverts
    # the right-pane forward added in #856).
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
        except Exception as exc:  # noqa: BLE001
            self.hint.update(f"Error: {exc}"[:60])
        self._refresh_rows()

    def action_open_settings(self) -> None:
        self.selected_key = "settings"
        try:
            self.router.route_selected("settings")
            self._last_router_selected_key = self.router.selected_key()
        except Exception as exc:  # noqa: BLE001
            self.hint.update(f"Error: {exc}"[:60])
        self._refresh_rows()

    def action_open_inbox(self) -> None:
        self.selected_key = "inbox"
        try:
            self.router.route_selected("inbox")
            self._last_router_selected_key = self.router.selected_key()
        except Exception as exc:  # noqa: BLE001
            self.hint.update(f"Error: {exc}"[:60])
        self._refresh_rows()

    def action_open_activity(self) -> None:
        self.selected_key = "activity"
        try:
            self.router.route_selected("activity")
            self._last_router_selected_key = self.router.selected_key()
        except Exception as exc:  # noqa: BLE001
            self.hint.update(f"Error: {exc}"[:60])
        self._refresh_rows()

    def action_forward_tab_to_right(self) -> None:
        if self._right_pane_has_live_session():
            self._focus_right_pane()
            return
        self._send_key_to_right_pane("Tab")

    def action_forward_workers_auto_refresh(self) -> None:
        if self.selected_key == "workers":
            self._send_key_to_right_pane("A")

    def action_forward_action_button_1(self) -> None:
        self._send_key_to_right_pane("1")

    def action_forward_action_button_2(self) -> None:
        self._send_key_to_right_pane("2")

    def action_forward_action_button_3(self) -> None:
        self._send_key_to_right_pane("3")

    def _on_project_surface(self) -> bool:
        return isinstance(self.selected_key, str) and self.selected_key.startswith(
            "project:",
        )

    def action_forward_project_chat(self) -> None:
        if self._on_project_surface():
            self._send_key_to_right_pane("c")

    def action_forward_project_log(self) -> None:
        if self._on_project_surface():
            self._send_key_to_right_pane("l")

    def action_toggle_project_pin(self) -> None:
        key = self._selected_row_key()
        if key is None or not key.startswith("project:"):
            return
        project_key = key.split(":", 1)[1]
        try:
            now_pinned = self.router.toggle_pinned_project(project_key)
        except Exception as exc:  # noqa: BLE001
            self.hint.update(f"Error: {exc}"[:60])
            return
        # User feedback so a second ``p`` press is visibly an unpin
        # rather than feeling like a silent no-op (#858).
        if now_pinned:
            self.hint.update(f"Pinned {project_key} — press p again to unpin.")
        else:
            self.hint.update(f"Unpinned {project_key}.")
        self._refresh_rows()

    def action_new_worker(self) -> None:
        key = self._selected_row_key()
        if key is None or not key.startswith("project:"):
            self.hint.update("Select a project first, then press n to launch a worker.")
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
        self._recover_cockpit_render(force_render=False)

    def on_resize(self, _event: events.Resize) -> None:
        self.call_after_refresh(self._recover_after_resize)

    def _recover_after_resize(self) -> None:
        self._recover_cockpit_render(force_render=True)

    def _recover_cockpit_render(self, *, force_render: bool) -> None:
        try:
            self.router.ensure_cockpit_layout()
        except Exception:  # noqa: BLE001
            pass
        self._refresh_rows()
        if not force_render:
            return
        try:
            self.nav.refresh(layout=True)
            self.refresh(layout=True)
        except Exception:  # noqa: BLE001
            pass

    # Surfaces where ``q``/``Esc`` should mean "back to Home" instead of
    # "quit the cockpit" (#864). The user expectation: only Home accepts
    # the destructive shutdown shortcut; sub-surfaces back out first so a
    # user mid-session does not have to navigate via the rail just to
    # leave Settings or Inbox.
    _HOME_RETURN_FROM_KEYS: frozenset[str] = frozenset(
        {"settings", "inbox", "activity", "workers"}
    )

    def _is_on_home(self) -> bool:
        return self.selected_key not in self._HOME_RETURN_FROM_KEYS and not (
            self.selected_key.startswith("project:")
        )

    def _navigate_home(self) -> bool:
        """Switch to the Home (dashboard / polly) surface. Return True on success."""
        try:
            self.router.route_selected("polly")
            self.selected_key = "polly"
            self._last_router_selected_key = self.router.selected_key()
        except Exception as exc:  # noqa: BLE001
            self.hint.update(f"Error: {exc}"[:60])
            return False
        self._refresh_rows()
        return True

    def action_back_to_home(self) -> None:
        if self._is_on_home():
            return
        self._navigate_home()

    def action_request_quit(self) -> None:
        # Sub-surface: ``q`` means "back to Home" first. Only confirm-quit
        # from Home itself so the destructive shortcut is gated by the
        # surface that already framed itself as the cockpit's landing.
        if not self._is_on_home():
            self._navigate_home()
            return
        result = self.router.tmux.run(
            "confirm-before",
            "-p",
            "Shut down PollyPM? This stops ALL agents. (W/Ctrl-W detaches instead) [y/N]",
            "run-shell 'echo CONFIRMED'",
            check=False,
        )
        if result.returncode == 0 and "CONFIRMED" in (result.stdout or ""):
            try:
                from pollypm.service_api import PollyPMService
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


class PollyDashboardApp(App[None]):
    """Rich dashboard: what's happening, what got done, token usage."""

    TITLE = "PollyPM"
    SUB_TITLE = "Dashboard"
    BINDINGS = [
        Binding("i", "jump_inbox", "Inbox"),
        Binding("r", "refresh", "Refresh"),
        # The screen overflows on a 65-row laptop terminal — Tokens
        # sits below the fold (#874). The Screen already declares
        # ``overflow-y: auto`` but Textual does not bind navigation
        # keys to scrolling without explicit actions, so the scroll
        # markers showed but no key reached them.
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
        self.messages_title = Static("[b]Recent Messages[/b]", classes="section-title", markup=True)
        self.messages_body = Static("", classes="section-body", markup=True)
        self.done_title = Static("[b]Done[/b]", classes="section-title", markup=True)
        self.done_body = Static("", classes="done-section", markup=True)
        self.chart_title = Static("[b]Tokens[/b]", classes="section-title", markup=True)
        self.chart_body = Static("", classes="chart-section", markup=True)
        self.footer_w = Static("", classes="footer", markup=True)
        self._dashboard_config = None
        self._dashboard_data = None
        self._refresh_running = False
        self._refresh_error: str | None = None

    def compose(self) -> ComposeResult:
        yield self.header_w
        yield self.now_title
        yield self.now_body
        yield self.messages_title
        yield self.messages_body
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
        self._render_cached_dashboard()
        if self._refresh_running:
            return
        self._refresh_running = True
        self.run_worker(
            self._refresh_dashboard_sync,
            thread=True,
            exclusive=True,
            group="polly_dashboard_refresh",
        )

    def _render_cached_dashboard(self) -> None:
        if self._dashboard_config is not None and self._dashboard_data is not None:
            self._render_dashboard(self._dashboard_config, self._dashboard_data)
            return
        if self._refresh_error:
            self.header_w.update(f"[dim]Error: {_escape(self._refresh_error)}[/dim]")
            return
        self.header_w.update("[dim]Loading dashboard…[/dim]")

    def _refresh_dashboard_sync(self) -> None:
        try:
            from pollypm.dashboard_data import load_dashboard

            config, data = load_dashboard(self.config_path)
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self._finish_dashboard_refresh_error, str(exc))
            return
        self.call_from_thread(self._finish_dashboard_refresh_success, config, data)

    def _finish_dashboard_refresh_success(self, config, data) -> None:
        self._dashboard_config = config
        self._dashboard_data = data
        self._refresh_running = False
        self._refresh_error = None
        self._render_dashboard(config, data)

    def _finish_dashboard_refresh_error(self, error: str) -> None:
        self._refresh_running = False
        self._refresh_error = error
        self._render_cached_dashboard()

    def _render_dashboard(self, config, data) -> None:
        # ── Header ──
        n_projects = len(config.projects)
        n_sessions = len(config.sessions)
        project_word = "project" if n_projects == 1 else "projects"
        agent_word = "agent" if n_sessions == 1 else "agents"
        parts = [
            f"[b]{n_projects}[/b] {project_word}",
            f"[b]{n_sessions}[/b] {agent_word}",
        ]
        if data.inbox_count:
            parts.append(f"[#d29922][b]{data.inbox_count}[/b] inbox[/#d29922]")
        if data.alert_count:
            alert_word = "alert" if data.alert_count == 1 else "alerts"
            parts.append(f"[#f85149][b]{data.alert_count}[/b] {alert_word}[/#f85149]")
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

        # ── Recent messages ──
        message_lines: list[str] = []
        if data.recent_messages:
            for item in data.recent_messages:
                sender = _escape(item.sender)
                title = _escape(item.title)
                age = self._age_str(item.age_seconds)
                message_lines.append(
                    f"[#58a6ff]{sender}[/#58a6ff] [dim]\u2192 you[/dim]  {title}"
                )
                meta = " \u00b7 ".join(
                    part
                    for part in (_escape(item.project), _escape(item.task_id), age)
                    if part
                )
                message_lines.append(f"  [dim]{meta}[/dim]")
                message_lines.append("")
            message_lines.append("[dim]Press [b]i[/b] to jump to the inbox[/dim]")
        elif data.inbox_count:
            # ``recent_messages`` filters to tracked projects only,
            # but ``inbox_count`` (and the rail's Inbox badge) cover
            # all registered projects. Saying "Inbox is clear." here
            # while the rail says ``Inbox (13)`` is the contradiction
            # in #799. Show the actual count instead.
            count = data.inbox_count
            noun = "item" if count == 1 else "items"
            message_lines.append(
                f"[dim]No recent messages from tracked projects "
                f"· [b]{count}[/b] {noun} in the inbox[/dim]"
            )
            message_lines.append("[dim]Press [b]i[/b] to jump to the inbox[/dim]")
        else:
            message_lines.append("[dim]Inbox is clear.[/dim]")
        self.messages_body.update("\n".join(message_lines))

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
            issue_word = "issue" if len(data.completed_items) == 1 else "issues"
            done_lines.append(
                f"[#3fb950]\u2713[/#3fb950] [b]{len(data.completed_items)}[/b] "
                f"{issue_word} completed"
            )
            for item in data.completed_items[:5]:
                age = self._age_str(item.age_seconds)
                done_lines.append(f"  [dim]\u2500[/dim] {item.title}  [dim]{age}[/dim]")
            done_lines.append("")

        if not data.recent_commits and not data.completed_items:
            summary = []
            if data.sweep_count_24h:
                sweep_word = "sweep" if data.sweep_count_24h == 1 else "sweeps"
                summary.append(
                    f"[#3fb950]{data.sweep_count_24h}[/#3fb950] {sweep_word}"
                )
            if data.message_count_24h:
                msg_word = "message" if data.message_count_24h == 1 else "messages"
                summary.append(
                    f"[#58a6ff]{data.message_count_24h}[/#58a6ff] {msg_word}"
                )
            if data.recovery_count_24h:
                rec_word = "recovery" if data.recovery_count_24h == 1 else "recoveries"
                summary.append(
                    f"[#d29922]{data.recovery_count_24h}[/#d29922] {rec_word}"
                )
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
        sweep_word = "sweep" if data.sweep_count_24h == 1 else "sweeps"
        msg_word = "message" if data.message_count_24h == 1 else "messages"
        footer = (
            "[dim]Click Polly to connect  \u00b7  "
            f"{data.sweep_count_24h} {sweep_word} today  \u00b7  "
            f"{data.message_count_24h} {msg_word}"
        )
        if self._refresh_error:
            footer += "  \u00b7  stale cache"
        footer += "[/dim]"
        self.footer_w.update(footer)

    def action_jump_inbox(self) -> None:
        self.run_worker(
            self._route_to_inbox_sync,
            thread=True,
            exclusive=True,
            group="polly_dashboard_inbox",
        )

    def _dashboard_screen(self):  # noqa: ANN202 — Textual Screen
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

    def _route_to_inbox_sync(self) -> None:
        try:
            self._route_to_inbox()
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(
                self.notify,
                f"Jump to inbox failed: {exc}",
                severity="error",
            )

    def _route_to_inbox(self) -> None:
        router = CockpitRouter(self.config_path)
        router.route_selected("inbox")


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


# Re-export the extracted task screen so existing import paths keep working.
from pollypm.cockpit_tasks import PollyTasksApp as PollyTasksApp  # noqa: E402,F401


# ---------------------------------------------------------------------------
# Settings — interactive Textual screen (rebuild)
# ---------------------------------------------------------------------------

_SETTINGS_SECTIONS: tuple[tuple[str, str], ...] = (
    ("accounts", "Accounts"),
    ("projects", "Projects"),
    ("roles", "Roles"),
    ("heartbeat", "Heartbeat & Recovery"),
    ("plugins", "Plugins"),
    ("planner", "Planner"),
    # ``Inbox & Notifications`` overflowed the 28-col settings-nav and
    # rendered as ``Inbox &`` (a dangling conjunction). The shorter
    # rail label keeps the section discoverable; the right-pane
    # heading still says "Inbox & notifications" for full clarity.
    ("inbox", "Notifications"),
    ("about", "About"),
)

_GLOBAL_SETTINGS_ROLE_KEYS = (
    "operator_pm",
    "architect",
    "worker",
    "reviewer",
)

_ROLE_LABELS = {
    "operator_pm": "Operator PM",
    "architect": "Architect",
    "worker": "Worker",
    "reviewer": "Reviewer",
}


def _role_label(role: str) -> str:
    return _ROLE_LABELS.get(role, role.replace("_", " ").title())


def _role_assignment_summary(
    assignment: ModelAssignment | None,
    *,
    registry,
    inherited: bool = False,
) -> str:
    if assignment is None:
        return "inherit" if inherited else "fallback"
    if assignment.alias is not None:
        if resolve_alias(assignment.alias, registry=registry) is None:
            return f"alias:{assignment.alias} (missing)"
        return f"alias:{assignment.alias}"
    return f"{assignment.provider}/{assignment.model}"


def _role_source_text(source: str) -> str:
    if source == "global":
        return "global"
    if source == "project":
        return "project override"
    if source == "fallback":
        return "fallback"
    return source


def _role_source_style(source: str) -> str:
    return {
        "project": "#5b8aff",
        "global": "#3ddc84",
        "fallback": "#97a6b2",
    }.get(source, "#97a6b2")


def _resolved_assignment_from_row(row: dict) -> ModelAssignment:
    alias = row.get("resolved_alias")
    if isinstance(alias, str) and alias:
        return ModelAssignment(alias=alias)
    return ModelAssignment(
        provider=str(row.get("resolved_provider") or ""),
        model=str(row.get("resolved_model") or ""),
    )


def _build_settings_role_rows(config, registry) -> list[dict]:
    rows: list[dict] = []
    assignments = getattr(getattr(config, "pollypm", None), "role_assignments", {}) or {}
    for role in _GLOBAL_SETTINGS_ROLE_KEYS:
        configured = assignments.get(role)
        resolved = resolve_role_assignment(
            role,
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
                "configured_summary": _role_assignment_summary(
                    configured,
                    registry=registry,
                ),
                "configured_alias": (
                    configured.alias if configured is not None else None
                ),
                "configured_provider": (
                    configured.provider if configured is not None else None
                ),
                "configured_model": (
                    configured.model if configured is not None else None
                ),
                "configured_kind": (
                    "alias"
                    if configured is not None and configured.alias is not None
                    else "custom"
                    if configured is not None
                    else "fallback"
                ),
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
                "source_label": _role_source_text(resolved.source),
                "advisories": advisories,
                "has_override": configured is not None,
            }
        )
    return rows


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
    try:
        result = subprocess.run(
            ["du", "-sk", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=0.75,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return -1
    if result.returncode != 0:
        return -1
    try:
        kib = int((result.stdout or "").strip().split()[0])
    except (IndexError, ValueError):
        return -1
    return kib * 1024


def _humanize_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(n)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{n} B"


def _budget_level(summary: str) -> str:
    match = _re.search(r"(\d{1,3})\s*%", summary or "")
    if not match:
        lowered = (summary or "").lower()
        if any(token in lowered for token in ("offline", "unavailable", "error")):
            return "error"
        return "unknown"
    pct = int(match.group(1))
    if pct <= 20:
        return "error"
    if pct <= 50:
        return "warn"
    return "ok"


def _budget_fields_from_cached_usage(record: object | None) -> tuple[str, str, str]:
    if record is None:
        return ("budget unavailable", "unknown", "No cached usage yet.")
    used_pct = getattr(record, "used_pct", None)
    remaining_pct = getattr(record, "remaining_pct", None)
    usage_summary = getattr(record, "usage_summary", "") or "usage unavailable"
    if used_pct is not None and remaining_pct is not None:
        budget_summary = f"{used_pct}% used / {remaining_pct}% left"
    elif remaining_pct is not None:
        budget_summary = f"{remaining_pct}% left"
    else:
        budget_summary = usage_summary
    updated_at = getattr(record, "updated_at", "") or ""
    if updated_at:
        budget_summary = f"{budget_summary} · updated {updated_at}"
    return (budget_summary, _budget_level(budget_summary), "Cached from account_usage")


def _format_recent_task(task: object) -> str:
    task_id = str(getattr(task, "task_id", ""))
    title = str(getattr(task, "title", "") or "(untitled)")
    project = str(getattr(task, "project", "") or "")
    status_obj = getattr(task, "work_status", getattr(task, "status", ""))
    status = getattr(status_obj, "value", status_obj)
    bits = [f"[b]{_escape(task_id)}[/b]"]
    if project:
        bits.append(f"[dim]{_escape(project)}[/dim]")
    if status:
        bits.append(f"[dim]{_escape(str(status))}[/dim]")
    bits.append(f"[dim]{_escape(title)}[/dim]")
    return " · ".join(bits)


class SettingsData:
    """Snapshot of everything the settings screen renders — gathered once."""

    __slots__ = (
        "accounts",
        "projects",
        "roles",
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
        roles: list[dict],
        heartbeat: list[tuple[str, str]],
        plugins: list[dict],
        planner: list[tuple[str, str]],
        inbox: list[tuple[str, str]],
        about: list[tuple[str, str]],
        errors: list[str],
    ) -> None:
        self.accounts = accounts
        self.projects = projects
        self.roles = roles
        self.heartbeat = heartbeat
        self.plugins = plugins
        self.planner = planner
        self.inbox = inbox
        self.about = about
        self.errors = errors


def _collect_recent_tasks_by_account(
    config,
    account_statuses: list,
    *,
    max_per_account: int = 3,
) -> dict[str, list[dict[str, str]]]:
    recent: dict[str, list[dict[str, str]]] = {
        str(getattr(status, "key", "")): [] for status in account_statuses
    }
    projects = getattr(config, "projects", {}) or {}
    for project_key, project in projects.items():
        path = getattr(project, "path", None)
        if path is None:
            continue
        project_path = Path(path)
        if not project_path.exists():
            continue
        db_path = project_path / ".pollypm" / "state.db"
        if not db_path.exists():
            continue
        try:
            from pollypm.work.sqlite_service import SQLiteWorkService

            with SQLiteWorkService(db_path=db_path, project_path=project_path) as svc:
                for status in account_statuses:
                    key = str(getattr(status, "key", ""))
                    if not key:
                        continue
                    try:
                        tasks = svc.list_tasks(assignee=key, limit=max_per_account)
                    except Exception:  # noqa: BLE001
                        continue
                    for task in tasks:
                        recent[key].append(
                            {
                                "task_id": str(getattr(task, "task_id", "")),
                                "project": str(getattr(task, "project", project_key) or project_key),
                                "title": str(getattr(task, "title", "") or "(untitled)"),
                                "work_status": getattr(
                                    getattr(task, "work_status", None),
                                    "value",
                                    str(getattr(task, "work_status", "")),
                                ),
                                "updated_at": (
                                    getattr(task, "updated_at", None).isoformat()
                                    if hasattr(getattr(task, "updated_at", None), "isoformat")
                                    else str(getattr(task, "updated_at", "") or "")
                                ),
                            }
                        )
        except Exception:  # noqa: BLE001
            continue
    for key, rows in recent.items():
        rows.sort(
            key=lambda row: (_iso_sort_weight(row["updated_at"]), row["task_id"]),
            reverse=True,
        )
        recent[key] = rows[:max_per_account]
    return recent


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
            from pollypm.service_api import PollyPMService
            service = PollyPMService(config_path)
        try:
            list_cached = getattr(service, "list_cached_account_statuses", None)
            if callable(list_cached):
                account_statuses = list(list_cached())
            else:
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
    try:
        cached_usages = load_cached_account_usage(config_path) if config is not None else {}
    except Exception:  # noqa: BLE001
        cached_usages = {}
    try:
        history = load_settings_history()
    except Exception:  # noqa: BLE001
        history = []
    for idx, status in enumerate(account_statuses):
        provider = getattr(status, "provider", None)
        provider_name = (
            getattr(provider, "value", "") if provider is not None else ""
        )
        home = getattr(status, "home", None)
        failover_pos = (
            (fo_list.index(status.key) + 1) if status.key in fo_list else None
        )
        usage_record = cached_usages.get(status.key)
        budget_summary, budget_level, budget_rationale = _budget_fields_from_cached_usage(
            usage_record,
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
                "usage_updated_at": getattr(status, "usage_updated_at", "") or "",
                "used_pct": getattr(status, "used_pct", None),
                "remaining_pct": getattr(status, "remaining_pct", None),
                "reset_at": getattr(status, "reset_at", "") or "",
                "period_label": getattr(status, "period_label", "") or "",
                "reason": getattr(status, "reason", "") or "",
                "available_at": getattr(status, "available_at", "") or "",
                "access_expires_at": getattr(status, "access_expires_at", "") or "",
                "isolation_status": getattr(status, "isolation_status", "") or "",
                "auth_storage": getattr(status, "auth_storage", "") or "",
                "budget_summary": budget_summary,
                "budget_level": budget_level,
                "rationale": history_rationale_for_account(
                    status.key,
                    entries=history,
                    default_account=ctrl or None,
                )
                or (
                    "Provider budgets come from the cached account_usage sampler so the UI stays offline-safe."
                ),
                "budget_rationale": budget_rationale,
                "status_obj": status,
                "index": idx,
            }
        )

    projects: list[dict] = []
    if config is not None:
        projects = collect_settings_projects(
            config,
            format_relative_age=_format_relative_age,
        )
        for project in projects:
            project.setdefault(
                "rationale",
                "Tracked projects stay visible in the cockpit and feed task counts.",
            )
            history_rationale = history_rationale_for_project(
                project["key"],
                entries=history,
            )
            if history_rationale:
                project["rationale"] = history_rationale

    if config is not None and accounts:
        recent_by_account = _collect_recent_tasks_by_account(
            config,
            account_statuses or [],
        )
        for account in accounts:
            account["recent_tasks"] = recent_by_account.get(account["key"], [])
    else:
        for account in accounts:
            account["recent_tasks"] = []

    roles: list[dict] = []
    if config is not None:
        try:
            registry = load_registry()
            roles = _build_settings_role_rows(config, registry)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Role registry unavailable: {exc}")

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
    about_section.append(
        (f"Disk usage ({config_path.parent.name}/)", "loading…")
    )

    return SettingsData(
        accounts=accounts,
        projects=projects,
        roles=roles,
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
    #settings-actions {
        height: auto;
        padding-bottom: 1;
    }
    #settings-account-actions {
        height: auto;
        padding-bottom: 1;
    }
    #settings-account-actions Button {
        margin-right: 1;
        height: 1;
        min-height: 1;
        border: none;
        padding: 0 1;
    }
    #settings-role-editor {
        height: auto;
        padding-bottom: 1;
    }
    #settings-role-editor > * {
        margin-right: 1;
    }
    #settings-role-note {
        color: #97a6b2;
        content-align: left middle;
    }
    #settings-role-alias {
        width: 36;
    }
    #settings-role-provider, #settings-role-model {
        width: 24;
    }
    #settings-account-actions-note {
        color: #97a6b2;
        content-align: left middle;
    }
    #settings-reload-cockpit {
        min-width: 18;
        height: 1;
        min-height: 1;
        border: none;
        padding: 0 1;
    }
    #settings-actions-note {
        color: #97a6b2;
        padding-left: 1;
        content-align: left middle;
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
    /* Narrow-mode placeholder (#865). At terminal widths below ~110
       cols, the side-by-side nav + content layout collapses into
       letter-by-letter wrapping. Toggle this overlay so the user
       sees a clear "needs more space" hint instead of broken
       rendering. The overlay lives at the outer container so it can
       cover the entire pane regardless of which inner card is in
       focus. Toggled from PollySettingsPaneApp.on_resize. */
    #settings-narrow-overlay {
        display: none;
        height: 1fr;
        content-align: center middle;
        color: #97a6b2;
    }
    #settings-outer.-narrow #settings-body {
        display: none;
    }
    #settings-outer.-narrow #settings-narrow-overlay {
        display: block;
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
    #settings-preview {
        height: auto;
        min-height: 6;
        color: #d6dee5;
        background: #111820;
        border: round #253140;
        padding: 1;
        margin-bottom: 1;
    }
    #settings-table-wrap {
        height: 1fr;
    }
    #accounts, #projects-table, #roles-table, #plugins-table {
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
        Binding("ctrl+k,colon", "open_command_palette", "Palette", priority=True),
        Binding("r", "refresh", "Refresh"),
        Binding("b", "toggle_permissions", "Permissions"),
        Binding("c", "add_claude_account", "Add Claude", show=False),
        Binding("o", "add_codex_account", "Add Codex", show=False),
        Binding("x", "remove_account", "Remove account", show=False),
        Binding("t", "toggle_project_tracked", "Toggle project", show=False),
        Binding("m", "make_controller", "Controller", show=False),
        Binding("v", "toggle_failover", "Failover", show=False),
        Binding("a", "view_alerts", "Alerts", show=False),
        Binding("u", "undo_recent_change", "Undo", show=False),
        Binding("question_mark", "show_keyboard_help", "Help", priority=True),
        Binding("q,escape", "back_or_cancel", "Back"),
    ]

    def action_open_command_palette(self) -> None:
        _open_command_palette(self)

    def action_show_keyboard_help(self) -> None:
        _open_keyboard_help(self)

    _DEFAULT_HINT = (
        "j/k move \u00b7 Tab section \u00b7 / search \u00b7 r refresh \u00b7 "
        "b permissions \u00b7 c/o add account \u00b7 x remove \u00b7 "
        "t project \u00b7 m controller \u00b7 v failover \u00b7 u undo \u00b7 q close"
    )

    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self.config_path = config_path
        from pollypm.service_api import PollyPMService
        self.service = PollyPMService(config_path)
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
        # Widgets
        self.topbar = Static("", id="settings-topbar", markup=True)
        self.nav = Vertical(id="settings-nav")
        self.section_title = Static(
            "", id="settings-section-title", markup=True,
        )
        self.preview = Static("", id="settings-preview", markup=True)
        self.accounts = DataTable(id="accounts")  # backwards-compat name
        self.projects_table = DataTable(id="projects-table")
        self.roles_table = DataTable(id="roles-table")
        self.plugins_table = DataTable(id="plugins-table")
        self.kv_static = Static("", id="settings-kv", markup=True)
        self.detail = Static("", id="detail", markup=True)
        self.role_alias_select = Select(
            role_alias_options,
            prompt="Registry alias",
            allow_blank=True,
            id="settings-role-alias",
        )
        self.role_provider_input = Input(
            placeholder="provider",
            id="settings-role-provider",
        )
        self.role_model_input = Input(
            placeholder="model",
            id="settings-role-model",
        )
        self.search_input = Input(
            placeholder="Filter \u2026 (Enter to apply, Esc to clear)",
            id="settings-search",
        )
        self.hint = Static(self._DEFAULT_HINT, id="settings-hint", markup=True)
        # State
        self.data: SettingsData | None = None
        self._router: CockpitRouter | None = None
        self._active_section: str = _SETTINGS_SECTIONS[0][0]
        self._search_query: str = ""
        self._nav_widgets: dict[str, Static] = {}
        self._selected_account_key: str | None = None
        self._selected_project_key: str | None = None
        self._selected_role_key: str | None = None
        self._visible_account_rows: list[dict] = []
        self._visible_project_rows: list[dict] = []
        self._visible_role_rows: list[dict] = []
        self._visible_plugin_rows: list[dict] = []
        self._nav_cursor: int = 0
        self._focus_target: str = "nav"  # nav | table
        self._undo_action: UndoAction | None = None
        self._syncing_role_editor = False
        self._suppressed_role_alias_values: list[str] = []

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-outer"):
            yield self.topbar
            # Narrow-mode placeholder (#865). Toggled by on_resize when
            # the terminal is below the threshold where the side-by-side
            # nav + content layout starts wrapping letter-by-letter.
            yield Static(
                "[b]Settings needs at least 110 cols of width.[/b]\n"
                "[dim]Resize the terminal or split — current layout would\n"
                "wrap content letter-by-letter.[/dim]",
                id="settings-narrow-overlay",
                markup=True,
            )
            with Horizontal(id="settings-body"):
                yield self.nav
                with Vertical(id="settings-right"):
                    with Horizontal(id="settings-actions"):
                        yield Button(
                            "Reload Cockpit",
                            id="settings-reload-cockpit",
                            variant="primary",
                        )
                        yield Static(
                            "Reload the shell only. Sessions stay running.",
                            id="settings-actions-note",
                        )
                    yield self.section_title
                    yield self.preview
                    with Horizontal(id="settings-account-actions"):
                        for spec in SETTINGS_ACCOUNT_ACTIONS:
                            yield Button(
                                spec.label,
                                id=spec.button_id,
                                variant=spec.variant,
                            )
                        yield Static(
                            "c/o add \u00b7 x remove \u00b7 u undo",
                            id="settings-account-actions-note",
                        )
                    with Horizontal(id="settings-role-editor"):
                        yield self.role_alias_select
                        yield self.role_provider_input
                        yield self.role_model_input
                        yield Button(
                            "Use Fallback",
                            id="settings-role-fallback",
                        )
                        yield Static(
                            "Pick an alias or type both fields to save a custom pair.",
                            id="settings-role-note",
                        )
                    with Vertical(id="settings-table-wrap"):
                        yield self.accounts
                        yield self.projects_table
                        yield self.roles_table
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
            "", "Key", "Email", "Provider", "Budget", "Ctrl", "FO", "Usage",
        )
        self.projects_table.cursor_type = "row"
        self.projects_table.zebra_stripes = True
        self.projects_table.add_columns(
            "", "Key", "Name", "PM", "Path", "Tasks", "Last activity",
        )
        self.roles_table.cursor_type = "row"
        self.roles_table.zebra_stripes = True
        self.roles_table.add_columns(
            "Role", "Configured", "Resolved", "Source", "Warn",
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
        self._apply_narrow_class()

    # Threshold below which the side-by-side nav + content layout
    # collapses into letter-by-letter wrapping (#865). Empirically a
    # 28-col nav + a content panel that needs ~80 cols itself = 110.
    _NARROW_THRESHOLD: int = 110

    def _apply_narrow_class(self) -> None:
        try:
            outer = self.query_one("#settings-outer", Vertical)
        except Exception:  # noqa: BLE001
            return
        try:
            width = self.size.width
        except Exception:  # noqa: BLE001
            return
        outer.set_class(width < self._NARROW_THRESHOLD, "-narrow")

    def on_resize(self, _event: events.Resize) -> None:  # type: ignore[override]
        self._apply_narrow_class()

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
        self._render_preview(self._active_section)

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
            n_errors = len(data.errors)
            error_word = "error" if n_errors == 1 else "errors"
            bits.append(
                f"[#ff5f6d]\u25cf {n_errors} {error_word}[/]"
            )
        self.topbar.update("   ".join(bits))

    def _render_nav(self) -> None:
        data = self.data
        counts = {
            "accounts": len(data.accounts) if data else 0,
            "projects": len(data.projects) if data else 0,
            "roles": len(data.roles) if data else 0,
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

    def _clear_expired_undo(self) -> None:
        if undo_expired(self._undo_action):
            self._undo_action = None

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

    def _undo_action_from_history(self) -> UndoAction | None:
        entry = latest_settings_history_entry()
        if entry is None:
            return None
        config = load_config(self.config_path)

        if entry.kind == "account.failover":
            account = str(entry.payload.get("account") or "")
            enabled = bool(entry.payload.get("enabled"))
            if not account:
                return None
            setter = getattr(self.service, "toggle_failover_account", None)
            if setter is None:
                return None

            def _apply() -> None:
                current = account in (getattr(config.pollypm, "failover_accounts", []) or [])
                if current != enabled:
                    setter(account)

            return make_undo_action(
                entry.label,
                _apply,
                entry_id=entry.entry_id,
                kind=entry.kind,
                payload=entry.payload,
            )

        if entry.kind == "account.controller":
            account = str(entry.payload.get("account") or "")
            previous = str(entry.payload.get("previous_account") or "")
            if not account or not previous:
                return None
            setter = getattr(self.service, "set_controller_account", None)
            if setter is None:
                return None

            def _apply() -> None:
                setter(previous)

            return make_undo_action(
                entry.label,
                _apply,
                entry_id=entry.entry_id,
                kind=entry.kind,
                payload=entry.payload,
            )

        if entry.kind == "project.tracked":
            project_key = str(entry.payload.get("project_key") or "")
            previous = bool(entry.payload.get("previous"))
            if not project_key:
                return None
            setter = getattr(self.service, "set_project_tracked", None)
            if setter is None:
                return None

            def _apply() -> None:
                setter(project_key, previous)

            return make_undo_action(
                entry.label,
                _apply,
                entry_id=entry.entry_id,
                kind=entry.kind,
                payload=entry.payload,
            )

        if entry.kind == "permissions.toggle":
            previous = bool(entry.payload.get("previous"))
            setter = getattr(self.service, "set_open_permissions_default", None)
            if setter is None:
                return None

            def _apply() -> None:
                setter(previous)

            return make_undo_action(
                entry.label,
                _apply,
                entry_id=entry.entry_id,
                kind=entry.kind,
                payload=entry.payload,
            )

        return None

    def _current_undo_action(self) -> UndoAction | None:
        self._clear_expired_undo()
        if self._undo_action is not None:
            return self._undo_action
        self._undo_action = self._undo_action_from_history()
        return self._undo_action

    def _consume_undo_history(self, action: UndoAction) -> None:
        if action.entry_id:
            consume_settings_history(action.entry_id)

    def _render_preview(self, key: str) -> None:
        self._clear_expired_undo()
        data = self.data
        if data is None:
            self.preview.update("")
            return
        if key == "accounts":
            self.preview.update(self._account_preview_text())
            return
        if key == "projects":
            self.preview.update(self._project_preview_text())
            return
        if key == "roles":
            self.preview.update(self._role_preview_text())
            return
        if key == "plugins":
            self.preview.update(
                "[b]Preview[/b]\n"
                "[dim]Plugin state is read-only in settings. Loaded, degraded, and disabled entries are summarized here.[/dim]"
            )
            return
        if key == "heartbeat":
            self.preview.update(
                "[b]Preview[/b]\n"
                "[dim]Heartbeat controls controller leasing and background scheduling. These values are shown for quick audit before changing account or project defaults.[/dim]"
            )
            return
        if key == "planner":
            self.preview.update(
                "[b]Preview[/b]\n"
                "[dim]Planner settings determine when new projects get work automatically and whether the plan gate stays enforced.[/dim]"
            )
            return
        if key == "inbox":
            self.preview.update(
                "[b]Preview[/b]\n"
                "[dim]Inbox paths point at the shared state database and logs directory for this workspace.[/dim]"
            )
            return
        if key == "about":
            lines = [
                "[b]Preview[/b]",
                "[dim]About is a quick diff-free summary of the install and disk footprint.[/dim]",
            ]
            undo_action = self._current_undo_action()
            if undo_action is not None:
                lines.append(
                    f"[dim]Undo available for {_escape(undo_action.label)} "
                    f"until {undo_expires_text(undo_action)}[/dim]"
                )
            self.preview.update("\n".join(lines))
            return
        self.preview.update("")

    def _account_preview_text(self) -> str:
        rows = self._visible_account_rows
        if not rows:
            return "[b]Preview[/b]\n[dim]No accounts match the current filter.[/dim]"
        key = self._selected_account_key or self._current_accounts_key() or rows[0]["key"]
        selected = next((a for a in rows if a["key"] == key), rows[0])
        lines = [
            "[b]Diff preview[/b]",
            f"Current provider: [b]{_escape(selected['provider'])}[/b]",
            f"Budget indicator: [b]{_escape(selected.get('budget_summary') or '-')}[/b] "
            f"([dim]{selected.get('budget_level', 'unknown')}[/dim])",
            f"Rationale: {_escape(selected.get('rationale') or 'No rationale available.')}",
            "[dim]Actions:[/] c add Claude · o add Codex · x remove selected",
            "[dim]Keyboard:[/] b permissions · m controller · v failover · u undo",
        ]
        undo_action = self._current_undo_action()
        if undo_action is not None:
            lines.append(
                f"[dim]Undo available:[/] {_escape(undo_action.label)} "
                f"until {undo_expires_text(undo_action)}"
            )
        recent = selected.get("recent_tasks") or []
        if recent:
            lines.append("[dim]Recent tasks:[/dim]")
            for task in recent[:3]:
                lines.append("  " + _format_recent_task(type("TaskPreview", (), task)()))
        else:
            lines.append("[dim]Recent tasks: none recorded for this account yet.[/dim]")
        return "\n".join(lines)

    def _project_preview_text(self) -> str:
        rows = self._visible_project_rows
        if not rows:
            return "[b]Preview[/b]\n[dim]No projects match the current filter.[/dim]"
        key = self._selected_project_key or self._current_projects_key() or rows[0]["key"]
        selected = next((p for p in rows if p["key"] == key), rows[0])
        current = "tracked" if selected["tracked"] else "paused"
        next_state = "paused" if selected["tracked"] else "tracked"
        lines = [
            "[b]Diff preview[/b]",
            f"Current status: [b]{current}[/b] -> [b]{next_state}[/b] via [b]t[/b]",
            f"Rationale: {_escape(selected.get('rationale') or 'No rationale available.')}",
            "[dim]Keyboard:[/] t toggle project · u undo",
        ]
        undo_action = self._current_undo_action()
        if undo_action is not None:
            lines.append(
                f"[dim]Undo available:[/] {_escape(undo_action.label)} "
                f"until {undo_expires_text(undo_action)}"
            )
        return "\n".join(lines)

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
        self._render_preview(key)

    def _render_section(self, key: str) -> None:
        self.accounts.display = key == "accounts"
        self.projects_table.display = key == "projects"
        self.roles_table.display = key == "roles"
        self.plugins_table.display = key == "plugins"
        self.kv_static.display = key in {"heartbeat", "planner", "inbox", "about"}
        self.detail.display = key in {"accounts", "projects", "roles", "plugins"}
        try:
            self.query_one("#settings-account-actions").display = key == "accounts"
        except Exception:  # noqa: BLE001
            pass
        try:
            self.query_one("#settings-role-editor").display = key == "roles"
        except Exception:  # noqa: BLE001
            pass

        title_map = dict(_SETTINGS_SECTIONS)
        self.section_title.update(
            f"[b]{_escape(title_map.get(key, key))}[/b]"
        )

        data = self.data
        if data is None:
            return

        if key == "accounts":
            rows = self._filtered_accounts(data)
            self._visible_account_rows = rows
            self._render_accounts(rows)
            self._render_account_detail(rows)
        elif key == "projects":
            rows = self._filtered_projects(data)
            self._visible_project_rows = rows
            self._render_projects(rows)
            self._render_project_detail(rows)
        elif key == "roles":
            rows = self._filtered_roles(data)
            self._visible_role_rows = rows
            self._render_roles(rows)
            self._render_role_detail(rows)
            self._sync_role_editor()
        elif key == "plugins":
            rows = self._filtered_plugins(data)
            self._visible_plugin_rows = rows
            self._render_plugins(rows)
            self._render_plugin_detail(rows)
        elif key == "heartbeat":
            self._render_kv("Heartbeat & recovery", data.heartbeat)
        elif key == "planner":
            self._render_kv("Planner", data.planner)
        elif key == "inbox":
            self._render_kv("Inbox & notifications", data.inbox)
        elif key == "about":
            self._ensure_about_section_loaded()
            self._render_kv("About", data.about)
        self._render_preview(key)

    def _ensure_about_section_loaded(self) -> None:
        data = self.data
        if data is None:
            return
        label = f"Disk usage ({self.config_path.parent.name}/)"
        for idx, (key, value) in enumerate(data.about):
            if key != label or value != "loading…":
                continue
            disk = (
                _settings_dir_size(self.config_path.parent)
                if self.config_path.parent.exists()
                else -1
            )
            rendered = _humanize_bytes(disk) if disk >= 0 else "unavailable"
            data.about[idx] = (key, rendered)
            break

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

    def _render_accounts(self, rows: list[dict]) -> None:
        self.accounts.clear()
        for a in rows:
            dot, colour = _settings_status_dot(a["health"], a["logged_in"])
            fo_mark = f"#{a['failover_pos']}" if a["failover_pos"] else ""
            ctrl_mark = "\u2713" if a["is_controller"] else ""
            budget_level = a.get("budget_level", "unknown")
            budget_style = {
                "ok": "#3ddc84",
                "warn": "#f0c45a",
                "error": "#ff5f6d",
            }.get(budget_level, "#97a6b2")
            budget_cell = Text(a.get("budget_summary") or "-", style=budget_style)
            self.accounts.add_row(
                Text(dot, style=colour),
                a["key"],
                a["email"],
                a["provider"],
                budget_cell,
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

    def _render_account_detail(self, rows: list[dict]) -> None:
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
        dot, colour = _settings_status_dot(
            selected["health"], selected["logged_in"],
        )
        sep = "[dim]" + "\u2500" * 40 + "[/dim]"
        lines = [
            f"[{colour}]{dot}[/{colour}] [b]{_escape(selected['key'])}[/b]"
            f"  [dim]({_escape(selected['provider'])})[/dim]",
            sep,
            f"[dim]Email:[/dim]      {_escape(selected['email'])}",
            f"[dim]Budget:[/dim]     {_escape(selected.get('budget_summary') or '-')}",
            f"[dim]Logged in:[/dim]  {'yes' if selected['logged_in'] else 'no'}",
            f"[dim]Health:[/dim]     {_escape(selected['health']) or '-'}",
            f"[dim]Plan:[/dim]       {_escape(selected['plan']) or '-'}",
            f"[dim]Usage:[/dim]      {_escape(selected['usage_summary']) or '-'}",
            f"[dim]Remaining:[/dim]  "
            f"{selected['remaining_pct']}%" if selected.get("remaining_pct") is not None
            else "[dim]Remaining:[/dim]  -",
            f"[dim]Used:[/dim]       "
            f"{selected['used_pct']}%" if selected.get("used_pct") is not None
            else "[dim]Used:[/dim]       -",
            f"[dim]Window:[/dim]     {_escape(selected.get('period_label') or '-')}",
            f"[dim]Resets:[/dim]     {_escape(selected.get('reset_at') or '-')}",
            f"[dim]Sampled:[/dim]    {_escape(selected.get('usage_updated_at') or '-')}",
            f"[dim]Controller:[/dim] {'yes' if selected['is_controller'] else 'no'}",
            f"[dim]Failover:[/dim]   "
            f"{'#' + str(selected['failover_pos']) if selected['failover_pos'] else 'no'}",
            f"[dim]Home:[/dim]       {_escape(selected['home']) or '-'}",
            f"[dim]Isolation:[/dim]  {_escape(selected['isolation_status']) or '-'}",
            f"[dim]Storage:[/dim]    {_escape(selected['auth_storage']) or '-'}",
            f"[dim]Budget note:[/dim] {_escape(selected.get('budget_rationale', 'Cached from account_usage'))}",
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
            lines.extend([sep, f"[dim]Reason:[/dim]     {_escape(selected['reason'])}"])
        lines.extend(
            [
                sep,
                f"[dim]Rationale:[/dim]  {_escape(selected.get('rationale') or 'No rationale available.')}",
                "[dim]Actions:[/dim]    c add Claude · o add Codex · x remove selected · u undo",
            ]
        )
        if selected["usage_raw_text"]:
            snippet = selected["usage_raw_text"].strip().splitlines()[:6]
            if snippet:
                lines.append(sep)
                lines.append("[dim]Latest usage snapshot:[/dim]")
                lines.extend(f"  {_escape(line)}" for line in snippet)
        recent = selected.get("recent_tasks") or []
        lines.append(sep)
        if recent:
            lines.append("[dim]Recent tasks:[/dim]")
            for task in recent[:3]:
                lines.append("  " + _format_recent_task(type("TaskPreview", (), task)()))
        else:
            lines.append("[dim]Recent tasks: none recorded for this account yet.[/dim]")
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

    def _render_projects(self, rows: list[dict]) -> None:
        self.projects_table.clear()
        for p in rows:
            dot_colour = "#3ddc84" if p["tracked"] else "#4a5568"
            dot = Text("\u25cf", style=dot_colour)
            name_style = "" if p["tracked"] else "dim"
            name_cell = Text(p["name"] or p["key"], style=name_style)
            path = p["path"] or "-"
            path_disp = path if len(path) <= 42 else ("\u2026" + path[-41:])
            path_cell = Text(path_disp, style="dim")
            tasks_cell = Text(str(p.get("task_total_label", p["task_total"])))
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

    def _render_project_detail(self, rows: list[dict]) -> None:
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
            f"[dim]Tasks:[/dim]  {selected.get('task_total_label', selected['task_total'])}",
            f"[dim]Last:[/dim]   {_escape(selected['last_activity']) or '-'}",
            f"[dim]Rationale:[/dim] {_escape(selected.get('rationale') or 'No rationale available.')}",
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

    # ── Roles ───────────────────────────────────────────────────────

    def _filtered_roles(self, data: SettingsData) -> list[dict]:
        q = self._search_query.strip().lower()
        if not q:
            return data.roles
        return [
            role
            for role in data.roles
            if q in role["role"].lower()
            or q in role["label"].lower()
            or q in role["configured_summary"].lower()
            or q in role["resolved_summary"].lower()
            or q in role["source_label"].lower()
            or any(q in warning.lower() for warning in role["advisories"])
        ]

    def _render_roles(self, rows: list[dict]) -> None:
        self.roles_table.clear()
        for row in rows:
            source_cell = Text(
                row["source_label"],
                style=_role_source_style(row["source"]),
            )
            warn_cell = Text(
                "!" if row["advisories"] else "",
                style="#f0c45a",
            )
            self.roles_table.add_row(
                Text(row["label"]),
                Text(row["configured_summary"]),
                Text(row["resolved_summary"], style="dim"),
                source_cell,
                warn_cell,
                key=row["role"],
            )
        if self.roles_table.row_count and self._selected_role_key:
            keys = [row["role"] for row in rows]
            if self._selected_role_key in keys:
                try:
                    self.roles_table.move_cursor(
                        row=keys.index(self._selected_role_key),
                    )
                except Exception:  # noqa: BLE001
                    pass
        elif self.roles_table.row_count and self.roles_table.cursor_row < 0:
            self.roles_table.move_cursor(row=0)

    def _selected_role_row(self, rows: list[dict] | None = None) -> dict | None:
        role_rows = self._visible_role_rows if rows is None else rows
        if not role_rows:
            return None
        key = self._selected_role_key or self._current_roles_key() or role_rows[0]["role"]
        return next((row for row in role_rows if row["role"] == key), role_rows[0])

    def _render_role_detail(self, rows: list[dict]) -> None:
        selected = self._selected_role_row(rows)
        if selected is None:
            self.detail.update("[dim]No roles match the current filter.[/dim]")
            return
        lines = [
            f"[b]{_escape(selected['label'])}[/b]  "
            f"[{_role_source_style(selected['source'])}]{_escape(selected['source_label'])}[/]",
            f"[dim]Configured:[/dim] {_escape(selected['configured_summary'])}",
            f"[dim]Resolved:[/dim]   {_escape(selected['resolved_summary'])}",
            f"[dim]Alias path:[/dim] {_escape(selected['resolved_alias'] or '-')}",
            "[dim]Edit:[/dim]       Pick a registry alias, or type both provider and model to save a custom pair.",
        ]
        if selected["configured_missing_alias"]:
            lines.append(
                "[#f0c45a]Configured alias is missing from the registry; PollyPM is using the next available scope.[/]"
            )
        if selected["advisories"]:
            lines.append("[#f0c45a]Advisories:[/]")
            lines.extend(f"  {_escape(message)}" for message in selected["advisories"])
        else:
            lines.append("[dim]Advisories:[/dim] none.")
        self.detail.update("\n".join(lines))

    def _role_preview_text(self) -> str:
        selected = self._selected_role_row()
        if selected is None:
            return "[b]Preview[/b]\n[dim]No roles match the current filter.[/dim]"
        lines = [
            "[b]Diff preview[/b]",
            f"Role: [b]{_escape(selected['label'])}[/b]",
            f"Configured: [b]{_escape(selected['configured_summary'])}[/b]",
            f"Resolved: [b]{_escape(selected['resolved_summary'])}[/b]",
            f"Source: [b]{_escape(selected['source_label'])}[/b]",
        ]
        if selected["advisories"]:
            lines.append(f"[#f0c45a]Warning:[/] {_escape(selected['advisories'][0])}")
        else:
            lines.append("[dim]No advisories for the current effective assignment.[/dim]")
        return "\n".join(lines)

    def _current_roles_key(self) -> str | None:
        if self.roles_table.row_count == 0 or self.roles_table.cursor_row < 0:
            return None
        try:
            row_key = self.roles_table.coordinate_to_cell_key(
                (self.roles_table.cursor_row, 0),
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
                self.query_one("#settings-role-fallback", Button).disabled = not bool(
                    selected and selected["has_override"]
                )
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._syncing_role_editor = False

    def _write_global_role_assignment(
        self,
        role: str,
        assignment: ModelAssignment,
    ) -> None:
        try:
            config = load_config(self.config_path)
            config.pollypm.role_assignments[role] = assignment
            write_config(config, self.config_path, force=True)
        except Exception as exc:  # noqa: BLE001
            try:
                self.notify(f"Role update failed: {exc}", severity="error")
            except Exception:  # noqa: BLE001
                pass
            return
        self._selected_role_key = role
        self._refresh()
        try:
            self.notify(
                f"Saved {_role_label(role)} role.",
                timeout=1.5,
            )
        except Exception:  # noqa: BLE001
            pass

    def _clear_global_role_assignment(self, role: str) -> None:
        try:
            config = load_config(self.config_path)
            config.pollypm.role_assignments.pop(role, None)
            write_config(config, self.config_path, force=True)
        except Exception as exc:  # noqa: BLE001
            try:
                self.notify(f"Role update failed: {exc}", severity="error")
            except Exception:  # noqa: BLE001
                pass
            return
        self._selected_role_key = role
        self._refresh()
        try:
            self.notify(
                f"{_role_label(role)} now uses fallback routing.",
                timeout=1.5,
            )
        except Exception:  # noqa: BLE001
            pass

    def _persist_role_custom_pair_if_ready(self) -> None:
        if self._active_section != "roles" or self._syncing_role_editor:
            return
        role = self._selected_role_key or self._current_roles_key()
        if not role:
            return
        provider = self.role_provider_input.value.strip()
        model = self.role_model_input.value.strip()
        if not provider or not model:
            return
        self._write_global_role_assignment(
            role,
            ModelAssignment(provider=provider, model=model),
        )

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

    def _render_plugins(self, rows: list[dict]) -> None:
        self.plugins_table.clear()
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

    def _render_plugin_detail(self, rows: list[dict]) -> None:
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

    def action_reload_cockpit(self) -> None:
        try:
            self.router.reload_cockpit_shell(
                kind="settings", selected_key="settings",
            )
        except Exception as exc:  # noqa: BLE001
            try:
                self.notify(f"Reload failed: {exc}", severity="error")
            except Exception:  # noqa: BLE001
                pass

    def action_toggle_permissions(self) -> None:
        try:
            config = load_config(self.config_path)
            previous = bool(getattr(config.pollypm, "open_permissions_by_default", False))
            enabled = not config.pollypm.open_permissions_by_default
            self.service.set_open_permissions_default(enabled)
            self._record_undo(
                f"permissions {'on' if previous else 'off'}",
                lambda: self.service.set_open_permissions_default(previous),
                kind="permissions.toggle",
                payload={
                    "previous": previous,
                    "enabled": enabled,
                },
            )
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
            previous = bool(current["tracked"])
            setter(key, not current["tracked"])
            self._record_undo(
                f"project {key} tracked {'on' if previous else 'off'}",
                lambda: setter(key, previous),
                kind="project.tracked",
                payload={
                    "project_key": key,
                    "previous": previous,
                    "enabled": not previous,
                },
            )
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
            config = load_config(self.config_path)
            previous = getattr(config.pollypm, "controller_account", "")
            setter(key)
            if previous:
                self._record_undo(
                    f"controller {previous}",
                    lambda: setter(previous),
                    kind="account.controller",
                    payload={
                        "account": key,
                        "previous_account": previous,
                    },
                )
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
            config = load_config(self.config_path)
            previous = key in (getattr(config.pollypm, "failover_accounts", []) or [])
            setter(key)
            self._record_undo(
                f"failover {key} {'on' if previous else 'off'}",
                lambda: setter(key),
                kind="account.failover",
                payload={
                    "account": key,
                    "enabled": not previous,
                },
            )
        except Exception as exc:  # noqa: BLE001
            try:
                self.notify(
                    f"Failover toggle failed: {exc}", severity="error",
                )
            except Exception:  # noqa: BLE001
                pass
            return
        self._refresh()

    def action_add_claude_account(self) -> None:
        self._add_account(ProviderKind.CLAUDE)

    def action_add_codex_account(self) -> None:
        self._add_account(ProviderKind.CODEX)

    def action_refresh_selected_account_usage(self) -> None:
        if self._active_section != "accounts":
            return
        key = self._current_accounts_key()
        if not key:
            try:
                self.notify("No account selected.", severity="warning")
            except Exception:  # noqa: BLE001
                pass
            return
        refresher = getattr(self.service, "refresh_account_usage", None)
        if refresher is None:
            return
        try:
            refresher(key)
            self._selected_account_key = key
        except Exception as exc:  # noqa: BLE001
            try:
                self.notify(
                    f"Usage refresh failed: {exc}", severity="error",
                )
            except Exception:  # noqa: BLE001
                pass
            return
        self._refresh()
        try:
            self.notify(f"Refreshed usage for {key}.", timeout=1.5)
        except Exception:  # noqa: BLE001
            pass

    def action_remove_account(self) -> None:
        self.action_remove_selected_account()

    def action_remove_selected_account(self) -> None:
        if self._active_section != "accounts":
            return
        key = self._current_accounts_key()
        if not key:
            try:
                self.notify("No account selected.", severity="warning")
            except Exception:  # noqa: BLE001
                pass
            return
        self.push_screen(
            _SettingsConfirmModal(
                title="Remove account",
                prompt=f"Remove account {key} from PollyPM config?",
                confirm_label="Remove",
            ),
            lambda confirmed: self._confirm_remove_account(key, confirmed),
        )

    def action_undo_recent_change(self) -> None:
        action = self._current_undo_action()
        if action is None:
            try:
                self.notify("Nothing recent to undo.", timeout=1.2)
            except Exception:  # noqa: BLE001
                pass
            return
        try:
            action.apply()
            self._consume_undo_history(action)
            self.notify(f"Undid {action.label}.", timeout=1.5)
        except Exception as exc:  # noqa: BLE001
            try:
                self.notify(f"Undo failed: {exc}", severity="error")
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._undo_action = None
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
        if self._active_section == "roles":
            return self.roles_table
        if self._active_section == "plugins":
            return self.plugins_table
        return None

    @property
    def router(self) -> CockpitRouter:
        if self._router is None:
            self._router = CockpitRouter(self.config_path)
        return self._router

    def _sync_selection(self) -> None:
        data = self.data
        if data is None:
            return
        if self._active_section == "accounts":
            self._selected_account_key = self._current_accounts_key()
            self._render_account_detail(self._visible_account_rows)
        elif self._active_section == "projects":
            self._selected_project_key = self._current_projects_key()
            self._render_project_detail(self._visible_project_rows)
        elif self._active_section == "roles":
            self._selected_role_key = self._current_roles_key()
            self._render_role_detail(self._visible_role_rows)
            self._sync_role_editor()
        elif self._active_section == "plugins":
            self._render_plugin_detail(self._visible_plugin_rows)
        self._render_preview(self._active_section)

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

    @on(DataTable.RowHighlighted, "#roles-table")
    def on_role_highlighted(
        self, _event: DataTable.RowHighlighted,
    ) -> None:
        self._sync_selection()

    @on(DataTable.RowHighlighted, "#plugins-table")
    def on_plugin_highlighted(
        self, _event: DataTable.RowHighlighted,
    ) -> None:
        self._sync_selection()

    @on(Button.Pressed, "#settings-reload-cockpit")
    def on_reload_cockpit_pressed(self, _event: Button.Pressed) -> None:
        self.action_reload_cockpit()

    @on(Button.Pressed, "#settings-account-add-claude")
    def on_add_claude_account_pressed(self, _event: Button.Pressed) -> None:
        self.action_add_claude_account()

    @on(Button.Pressed, "#settings-account-add-codex")
    def on_add_codex_account_pressed(self, _event: Button.Pressed) -> None:
        self.action_add_codex_account()

    @on(Button.Pressed, "#settings-account-refresh-usage")
    def on_refresh_account_usage_pressed(self, _event: Button.Pressed) -> None:
        self.action_refresh_selected_account_usage()

    @on(Button.Pressed, "#settings-account-remove")
    def on_remove_account_pressed(self, _event: Button.Pressed) -> None:
        self.action_remove_selected_account()

    @on(Select.Changed, "#settings-role-alias")
    def on_role_alias_changed(self, event: Select.Changed) -> None:
        if self._active_section != "roles" or self._syncing_role_editor:
            return
        role = self._selected_role_key or self._current_roles_key()
        if not role:
            return
        value = event.value
        if not isinstance(value, str) or not value:
            return
        if value in self._suppressed_role_alias_values:
            self._suppressed_role_alias_values.remove(value)
            return
        self._write_global_role_assignment(
            role,
            ModelAssignment(alias=value),
        )

    @on(Input.Changed, "#settings-role-provider")
    def on_role_provider_changed(self, _event: Input.Changed) -> None:
        self._persist_role_custom_pair_if_ready()

    @on(Input.Changed, "#settings-role-model")
    def on_role_model_changed(self, _event: Input.Changed) -> None:
        self._persist_role_custom_pair_if_ready()

    @on(Button.Pressed, "#settings-role-fallback")
    def on_role_fallback_pressed(self, _event: Button.Pressed) -> None:
        if self._active_section != "roles":
            return
        role = self._selected_role_key or self._current_roles_key()
        if not role:
            return
        self._clear_global_role_assignment(role)

    @on(DataTable.RowSelected, "#accounts")
    def on_account_selected(self, _event: DataTable.RowSelected) -> None:
        self._selected_account_key = self._current_accounts_key()
        if self.data is not None:
            self._render_account_detail(self._visible_account_rows)
            self._render_preview(self._active_section)

    @on(DataTable.RowSelected, "#roles-table")
    def on_role_selected(self, _event: DataTable.RowSelected) -> None:
        self._selected_role_key = self._current_roles_key()
        if self.data is not None:
            self._render_role_detail(self._visible_role_rows)
            self._sync_role_editor()
            self._render_preview(self._active_section)

    def _add_account(self, provider: ProviderKind) -> None:
        adder = getattr(self.service, "add_account", None)
        remover = getattr(self.service, "remove_account", None)
        if adder is None or remover is None:
            return
        try:
            key, email = adder(provider)
            self._selected_account_key = key
            self._record_undo(
                f"add account {key}",
                lambda: remover(key, delete_home=False),
            )
        except Exception as exc:  # noqa: BLE001
            try:
                self.notify(f"Add account failed: {exc}", severity="error")
            except Exception:  # noqa: BLE001
                pass
            return
        self._refresh()
        try:
            self.notify(
                f"Added {provider.value} account {key} ({email}).",
                timeout=1.5,
            )
        except Exception:  # noqa: BLE001
            pass

    def _confirm_remove_account(self, key: str, confirmed: bool) -> None:
        if not confirmed:
            return
        remover = getattr(self.service, "remove_account", None)
        if remover is None:
            return
        try:
            remover(key, delete_home=False)
            if self._selected_account_key == key:
                self._selected_account_key = None
        except Exception as exc:  # noqa: BLE001
            try:
                self.notify(f"Remove account failed: {exc}", severity="error")
            except Exception:  # noqa: BLE001
                pass
            return
        self._refresh()
        try:
            self.notify(f"Removed account {key}.", timeout=1.5)
        except Exception:  # noqa: BLE001
            pass


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
    triage_rank = getattr(task, "triage_rank", None)
    triage_rank = 2 if triage_rank is None else int(triage_rank)
    # Actionable items sort ahead of informational ones; orphaned
    # deleted-project rows sort last. Within a bucket, newer still wins.
    return (
        triage_rank,
        -_iso_sort_weight(iso),
        _INBOX_PRIORITY_RANK.get(prio, 9),
        task.title,
    )


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
    sender = getattr(task, "sender", None)
    if sender and sender != "user":
        return sender
    roles = getattr(task, "roles", {}) or {}
    op = roles.get("operator")
    if op and op != "user":
        return op
    if task.created_by and task.created_by != "user":
        return task.created_by
    # Last resort — unknown sender. Don't show blank.
    return "polly"


def _triage_bucket(task) -> str:
    return str(getattr(task, "triage_bucket", "info") or "info")


def _triage_label(task) -> str:
    label = getattr(task, "triage_label", None)
    if label:
        return str(label)
    if is_rejection_feedback_task(task):
        target = feedback_target_task_id(task)
        if target:
            return f"review feedback for {target}"
        return "review feedback"
    return "update"


def _render_user_prompt_block(payload: object) -> str | None:
    """Build the plain-English action block for a message detail pane.

    Architects, reviewers and PMs that send a structured ``user_prompt``
    in the message payload have already done the work of summarising
    *what the user should do*. The detail pane should lead with that
    block — the raw body still renders underneath for technical
    context, but the operator should not have to parse worker jargon
    to figure out the decision being asked of them.

    Returns ``None`` when the payload has no ``user_prompt`` dict, so
    callers can short-circuit and render the legacy body-only layout.
    """
    if not isinstance(payload, dict):
        return None
    prompt = payload.get("user_prompt")
    if not isinstance(prompt, dict):
        return None

    def _plain(value: object | None) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        return " ".join(part.strip() for part in text.splitlines() if part.strip())

    summary = _plain(prompt.get("summary"))
    question = _plain(prompt.get("question"))
    raw_steps = prompt.get("steps") or prompt.get("required_actions") or []
    if not isinstance(raw_steps, list):
        raw_steps = []
    steps = [_plain(step) for step in raw_steps if _plain(step)][:5]
    if not (summary or steps or question):
        return None

    lines: list[str] = []
    if summary:
        lines.append(f"[#f0c45a]◆[/#f0c45a] {_escape(summary)}")
    heading = _plain(prompt.get("steps_heading")) or "What to do"
    if steps:
        lines.append(f"  [b]{_escape(heading)}[/b]")
        for idx, step in enumerate(steps, start=1):
            lines.append(f"  [dim]{idx}.[/dim] {_escape(step)}")
    if question:
        lines.append(f"  [b]Decision:[/b] {_escape(question)}")
    return "\n".join(lines)


def _render_heuristic_action_block(body: object) -> str | None:
    """Heuristic fallback for messages that lack a ``user_prompt``.

    Mirrors the dashboard's Action Needed card: pull a one-paragraph
    summary out of the body and any numbered "steps" lines, render
    them as the same yellow-diamond block we use for ``user_prompt``
    payloads. The full body still renders below for context, but
    leading with this lifts the operator-visible call to action out
    of jargon-heavy worker output. Returns ``None`` when nothing
    usable can be extracted (e.g. an empty body or a body that's all
    code blocks).
    """
    text = str(body or "")
    if not text.strip():
        return None
    summary = _dashboard_summary_from_body(text)
    steps = _dashboard_steps_from_body(text)[:5]
    if not (summary or steps):
        return None
    lines: list[str] = []
    if summary:
        lines.append(f"[#f0c45a]◆[/#f0c45a] {_escape(summary)}")
    if steps:
        lines.append("  [b]What to do[/b]")
        for idx, step in enumerate(steps, start=1):
            lines.append(f"  [dim]{idx}.[/dim] {_escape(step)}")
    return "\n".join(lines)


def _render_inbox_triage_banner(item) -> str | None:
    bucket = _triage_bucket(item)
    label = _triage_label(item)
    project = (getattr(item, "project", "") or "").strip()
    if bucket == "action":
        return (
            f"[b #f0c45a]Action Required[/b #f0c45a]"
            f"  [dim]· {_escape(label)}[/dim]"
        )
    if bucket == "orphaned":
        detail = f"{project} is no longer a tracked project." if project else "This project is no longer tracked."
        return (
            f"[b #97a6b2]Deleted Project[/b #97a6b2]"
            f"  [dim]· {_escape(detail)}[/dim]"
        )
    if label and label != "update":
        return f"[dim]{_escape(label)}[/dim]"
    return None


def _format_inbox_row(
    task,
    *,
    is_unread: bool,
    width: int = 38,
    tree_marker: str = "",
    reply_count: int = 0,
) -> Text:
    """Render one inbox-list row as two lines of Rich text.

    Matches the cockpit aesthetic from RailItem: yellow diamond for
    unread, dim open circle for read.

    Line 1 is the bold message title (truncated with an ellipsis if it
    won't fit ``width`` chars after the unread-marker glyph — no wrap).
    Line 2 is dim ``project · age`` metadata indented under the title.
    """
    from pollypm.tz import format_relative

    text = Text(no_wrap=True, overflow="ellipsis")
    if tree_marker:
        text.append(tree_marker, style="#6b7a88")
    if is_unread:
        text.append("\u25c6 ", style="#f0c45a")  # yellow diamond
    else:
        text.append("\u25cb ", style="#4a5568")  # dim circle
    subject_prefix = ""
    if is_rejection_feedback_task(task):
        subject_prefix = "🔄 "
        text.append(subject_prefix, style="#ffb454")
    subject = task.title or "(no subject)"
    # Drop the "[Action]" prefix from action-bucket rows. The inbox
    # already groups action-needed items under their own header, so
    # stamping every title with "[Action]" is redundant noise that
    # eats list-pane width and buries the actual subject.
    if getattr(task, "triage_bucket", "") == "action":
        subject = _strip_action_subject_prefix(subject)
    reply_suffix = ""
    if reply_count:
        noun = "reply" if reply_count == 1 else "replies"
        reply_suffix = f" ({reply_count} {noun})"
    # Account for the 2-char marker glyph prefix so the total row still
    # fits the target list-pane width without wrapping.
    max_subject = max(
        8, width - 2 - len(tree_marker) - len(reply_suffix) - len(subject_prefix)
    )
    if len(subject) > max_subject:
        subject = subject[: max_subject - 1] + "\u2026"
    subject_style = "bold #eef2f4" if is_unread else "bold #b8c4cf"
    text.append(subject, style=subject_style)
    if reply_suffix:
        text.append(reply_suffix, style="#6b7a88")

    # Line 2: project · age, dim. Indent by 2 so it lines up under the
    # subject text (past the marker glyph).
    updated = task.updated_at
    iso = updated.isoformat() if hasattr(updated, "isoformat") else str(updated or "")
    age = format_relative(iso) if iso else ""
    raw_project = (task.project or "").strip()
    # The detail pane surfaces workspace-root sentinel items as
    # ``[workspace]`` (cycle 14) \u2014 mirror that here so the list-rail
    # label matches the detail surface instead of leaking the raw
    # ``inbox`` sentinel string.
    if raw_project == "inbox":
        project = "[workspace]"
    else:
        project = raw_project or "\u2014"
    meta_bits = [_triage_label(task), project]
    if age:
        meta_bits.append(age)
    meta_indent = " " * max(2, len(tree_marker) + 2)
    meta_line = meta_indent + "  \u00b7  ".join(meta_bits)
    text.append("\n")
    text.append(meta_line, style="#6b7a88")
    return text


def _format_inbox_reply_row(task, reply, *, width: int = 38) -> Text:
    """Render one inline reply row underneath its parent inbox task."""
    from pollypm.tz import format_relative

    text = Text(no_wrap=True, overflow="ellipsis")
    actor = (getattr(reply, "actor", "") or "user").strip() or "user"
    speaker = "you" if actor == "user" else actor
    target = _format_sender(task) if actor == "user" else "you"
    preview = (getattr(reply, "text", "") or "").strip().splitlines()
    subject = preview[0] if preview else "(no reply text)"
    header = f"{speaker} \u2192 {target}  "
    prefix = "  \u2514 "
    max_subject = max(8, width - len(prefix) - len(header))
    if len(subject) > max_subject:
        subject = subject[: max_subject - 1] + "\u2026"
    text.append(prefix, style="#6b7a88")
    text.append(header, style="#97a6b2")
    text.append(subject, style="#c8d2da")

    stamped = getattr(reply, "timestamp", None)
    iso = stamped.isoformat() if hasattr(stamped, "isoformat") else str(stamped or "")
    age = format_relative(iso) if iso else ""
    text.append("\n")
    text.append("    " + (age or "reply"), style="#586773")
    return text


def _format_inbox_thread_row(
    row: InboxThreadRow,
    *,
    is_unread: bool,
    width: int = 38,
) -> Text:
    """Render either a root task row or an inline reply row."""
    if row.is_reply and row.reply is not None:
        return _format_inbox_reply_row(row.task, row.reply, width=width)
    tree_marker = ""
    if row.has_children:
        tree_marker = "\u25be " if row.expanded else "\u25b8 "
    return _format_inbox_row(
        row.task,
        is_unread=is_unread,
        width=width,
        tree_marker=tree_marker,
        reply_count=row.reply_count,
    )


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
            separator = " · "
            lines.append(f"[dim]{_escape(separator.join(ref_bits))}[/dim]")
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
                meta_separator = " · "
                lines.append(f"[dim]{_escape(meta_separator.join(meta_bits))}[/dim]")
        return "\n".join(lines)


class _InboxListItem(ListItem):
    """One message in the inbox list — carries the task_id + unread flag."""

    def __init__(self, row: InboxThreadRow, *, is_unread: bool) -> None:
        self.row_ref = row
        self.task_id = row.task_id
        self.task_ref = row.task
        self.is_unread = is_unread
        row_classes = "inbox-row reply-row" if row.is_reply else "inbox-row"
        self._body = Static(_format_inbox_thread_row(row, is_unread=is_unread), markup=False)
        super().__init__(self._body, classes=row_classes)
        if is_unread:
            self.add_class("unread")
        if row.is_task and is_rejection_feedback_task(row.task):
            self.add_class("rejection-feedback")
        triage_bucket = _triage_bucket(row.task)
        if triage_bucket == "action":
            self.add_class("action-required")
        elif triage_bucket == "orphaned":
            self.add_class("orphaned")
        else:
            self.add_class("informational")

    def mark_read(self, row: InboxThreadRow | None = None) -> None:
        """Flip the row to read styling in place (no reflow of the list)."""
        if self.is_unread is False:
            return
        self.is_unread = False
        self.remove_class("unread")
        if row is not None:
            self.row_ref = row
            self.task_ref = row.task
        self._body.update(_format_inbox_thread_row(self.row_ref, is_unread=False))


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
        /* #753 — responsive list width. Fixed 42-column list was
           fine on an iPad but left ~80% of a 34" monitor empty next
           to the detail pane, and clipped subject lines with no
           context to spare. Percentage with a min-width floor keeps
           narrow-terminal ergonomics while letting ultrawide displays
           breathe. */
        width: 40%;
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
    #inbox-list > .inbox-row.reply-row {
        color: #97a6b2;
    }
    #inbox-list > .inbox-row.rejection-feedback {
        border-left: thick #ffb454;
        background: #17110d;
    }
    #inbox-list > .inbox-row.action-required {
        border-left: thick #f0c45a;
        background: #171611;
    }
    #inbox-list > .inbox-row.orphaned {
        border-left: thick #586773;
        background: #11161b;
        color: #8b98a4;
    }
    #inbox-list > .inbox-row.-highlight {
        /* Unfocused highlight is also visible (#857). The earlier
           muted ``#1e2730`` was indistinguishable from idle rows on
           tmux capture and made the focused inbox item invisible
           when the user opened the inbox via the rail. */
        background: #2a3a4d;
        color: #f2f6f8;
        border-left: thick #5b8aff;
    }
    #inbox-list > .inbox-row.rejection-feedback.-highlight {
        background: #3a2614;
        color: #fff3df;
        border-left: thick #ffb454;
    }
    #inbox-list > .inbox-row.action-required.-highlight {
        background: #3a3018;
        color: #fff8df;
        border-left: thick #f0c45a;
    }
    #inbox-list > .inbox-row.orphaned.-highlight {
        background: #1d2732;
        color: #d0d8de;
        border-left: thick #5b8aff;
    }
    #inbox-list:focus > .inbox-row.-highlight {
        background: #253140;
        color: #f2f6f8;
    }
    #inbox-list:focus > .inbox-row.rejection-feedback.-highlight {
        background: #2d1e10;
        color: #fff3df;
    }
    #inbox-list:focus > .inbox-row.action-required.-highlight {
        background: #312812;
        color: #fff8df;
    }
    #inbox-list:focus > .inbox-row.orphaned.-highlight {
        background: #1d2732;
        color: #d0d8de;
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
        Binding("right", "thread_right", "Expand", show=False),
        Binding("left", "thread_left", "Collapse", show=False),
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
        Binding("O", "toggle_filter_orphaned", "Orphaned", show=False),
        Binding("m", "toggle_show_all_messages", "All messages", show=False),
        Binding("c", "clear_filters", "Clear filters", show=False),
        # Refresh: ``u`` re-bound to filter, so refresh moves to ``ctrl+r``
        # (palette 'session.refresh' still works from any screen).
        Binding("ctrl+r", "refresh", "Refresh", show=False),
        Binding("ctrl+k,colon", "open_command_palette", "Palette", priority=True),
        Binding("question_mark", "show_keyboard_help", "Help", priority=True),
        # #789: previously labelled "Back", but at the list level
        # ``action_back_or_cancel`` calls ``self.exit()`` which tears
        # down the entire right pane. ``Close`` matches what actually
        # happens; from the filter or reply input we still bounce
        # focus back to the list before exit kicks in.
        Binding("q,escape", "back_or_cancel", "Close"),
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

    def __init__(
        self,
        config_path: Path,
        *,
        initial_project: str | None = None,
    ) -> None:
        super().__init__()
        self.config_path = config_path
        # #751 — when the inbox is launched from a project dashboard,
        # this is the project key to pre-apply as a filter on mount.
        # None means "no initial scope" (legacy behavior).
        self._initial_project = initial_project
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
        self._selected_row_key: str | None = None
        self._unread_ids: set[str] = set()
        self._session_read_ids: set[str] = set()
        self._replies_by_task: dict[str, list] = {}
        self._visible_rows: list[InboxThreadRow] = []
        self._thread_expanded_task_ids: set[str] = set()
        # Filter state (#NEW). Session-scoped — cleared on each mount
        # via :meth:`on_mount`. All filters AND-combine.
        self._filter_text: str = ""
        self._filter_unread_only: bool = False
        self._filter_project: str | None = None
        self._filter_recent: bool = False
        self._filter_plan_review: bool = False
        self._filter_blocking: bool = False
        self._show_orphaned: bool = False
        self._show_all_messages: bool = False
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
        # #751 — when launched with an initial project, pre-apply it
        # as a filter. The user jumped here from a project dashboard
        # and expects to see that project's items. Filter chips stay
        # visible so the scope is obvious and one-click-dismissable.
        if self._initial_project:
            self._filter_project = self._initial_project
            self._filter_bar_visible = True
            self.filter_input.display = False
            self.filter_bar.display = True
            self.filter_chips.display = True
        else:
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

    def _load_inbox(self) -> tuple[list, set[str], dict[str, list]]:
        """Open each inbox DB source and merge messages with task threads.

        All services are closed before return — callers don't need to
        manage lifecycle. Per-task operations open a fresh svc via
        :meth:`_svc_for_task` so we never hold connections across ticks.
        """
        config = load_config(self.config_path)
        tasks, unread, replies_by_task = load_inbox_entries(
            config,
            session_read_ids=self._session_read_ids,
        )
        tasks.sort(key=_inbox_sort_key)
        return tasks, unread, replies_by_task

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

    def _project_key_is_unknown(self, project_key: str) -> bool:
        """True when ``project_key`` is not a registered project.

        Used by the inbox detail fallback (#855) so workspace-scoped
        items render via the message renderer instead of the red
        'Could not open project database' error.
        """
        try:
            config = load_config(self.config_path)
        except Exception:  # noqa: BLE001
            return True
        return project_key not in getattr(config, "projects", {})

    # ------------------------------------------------------------------
    # List rendering
    # ------------------------------------------------------------------

    def _refresh_list(self, *, select_first: bool = False) -> None:
        tasks, unread, replies_by_task = self._load_inbox()
        self._tasks = tasks
        self._unread_ids = unread
        self._replies_by_task = replies_by_task
        active_thread_ids = {
            task.task_id for task in tasks if replies_by_task.get(task.task_id)
        }
        self._thread_expanded_task_ids.intersection_update(active_thread_ids)
        # Prime the signature cache so _background_refresh can cheaply
        # detect unchanged state on the next poll and skip re-rendering
        # (#752: eliminates the visible flash every ~8s).
        self._last_inbox_signature = self._inbox_content_signature(
            tasks, unread, replies_by_task,
        )
        self._render_list(select_first=select_first)

    def _render_list(self, *, select_first: bool = False) -> None:
        previous_row_key = self._selected_row_key
        previous_task_id = self._selected_task_id
        self.list_view.clear()
        self._visible_rows = []
        # Refresh chip line on every render so toggles + project changes
        # land in the UI even when the list itself didn't shrink.
        self._update_filter_chips()
        if not self._tasks:
            self._selected_task_id = None
            self._selected_row_key = None
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
            self._selected_task_id = None
            self._selected_row_key = None
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
        fallback_index: int | None = None
        visible_rows = build_inbox_thread_rows(
            visible,
            self._replies_by_task,
            self._thread_expanded_task_ids,
        )
        self._visible_rows = visible_rows
        for idx, row_ref in enumerate(visible_rows):
            is_unread = row_ref.is_task and row_ref.task_id in self._unread_ids
            row = _InboxListItem(row_ref, is_unread=is_unread)
            self.list_view.append(row)
            if previous_row_key and row_ref.key == previous_row_key:
                restore_index = idx
            elif (
                fallback_index is None
                and previous_task_id
                and row_ref.is_task
                and row_ref.task_id == previous_task_id
            ):
                fallback_index = idx
        if restore_index is None and fallback_index is not None:
            restore_index = fallback_index
        if restore_index is not None and self.list_view.index != restore_index:
            self.list_view.index = restore_index
            # Render detail for the restored selection so the right pane
            # shows content immediately on refresh.
            row_ref = visible_rows[restore_index]
            self._selected_task_id = row_ref.task_id
            self._selected_row_key = row_ref.key
            self._render_detail(row_ref.task_id)
        elif restore_index is not None and 0 <= restore_index < len(visible_rows):
            row_ref = visible_rows[restore_index]
            self._selected_task_id = row_ref.task_id
            self._selected_row_key = row_ref.key
        self._update_status(total=total, shown=len(visible))

    def _update_status(self, *, total: int, shown: int) -> None:
        """Render the bottom counter strip — folds in active filters.

        ``shown`` may equal ``total`` when no filter narrows the list;
        in that case we omit the "N of M" framing for visual calm.
        """
        unread_n = len(self._unread_ids)
        candidates = self._explicitly_filtered_tasks(self._tasks)
        using_action_lens = self._uses_action_lens_for(candidates)
        actionable_n = sum(1 for item in candidates if getattr(item, "needs_action", False))
        hidden_fyi_n = max(0, len(candidates) - shown) if using_action_lens else 0
        hidden_orphaned_n = 0 if self._show_orphaned else sum(
            1 for item in self._tasks if getattr(item, "is_orphaned", False)
        )
        bits: list[str] = []
        if using_action_lens:
            verb = "needs" if shown == 1 else "need"
            bits.append(f"{shown} {verb} action")
        elif (self._has_active_filters() or hidden_orphaned_n) and shown != total:
            bits.append(f"{shown} of {total} shown")
        else:
            msg_word = "message" if shown == 1 else "messages"
            bits.append(f"{shown} {msg_word}")
        if unread_n:
            bits.append(f"{unread_n} unread")
        if actionable_n and not using_action_lens:
            verb = "needs" if actionable_n == 1 else "need"
            bits.append(f"{actionable_n} {verb} action")
        if hidden_fyi_n:
            bits.append(f"{hidden_fyi_n} FYI hidden")
            bits.append("m show all")
        if hidden_orphaned_n:
            bits.append(f"{hidden_orphaned_n} orphaned hidden")
        desc = self._describe_filters()
        if desc:
            bits.append(f"filters: {desc}")
        self.status.update(" \u00b7 ".join(bits))

    def _background_refresh(self) -> None:
        """Periodic re-read; don't stomp the current cursor position.

        #752: also short-circuits when the inbox data is structurally
        unchanged since the last tick. The previous code re-rendered
        the entire ListView every 8s regardless of whether anything
        changed — the user saw this as a visible flash every tick.
        Now we compute a cheap content signature and skip the re-render
        when it matches. Signature invalidates on any change the user
        would care about: task state, replies, unread set, filter
        state, filter chips.
        """
        try:
            tasks, unread, replies_by_task = self._load_inbox()
        except Exception:  # noqa: BLE001
            return
        signature = self._inbox_content_signature(tasks, unread, replies_by_task)
        if signature == getattr(self, "_last_inbox_signature", None):
            return
        self._last_inbox_signature = signature
        # Seed the cached collections the renderer expects; _render_list
        # will consume them via self._tasks / self._unread_ids /
        # self._replies_by_task without re-querying.
        self._tasks = tasks
        self._unread_ids = unread
        self._replies_by_task = replies_by_task
        active_thread_ids = {
            task.task_id for task in tasks if replies_by_task.get(task.task_id)
        }
        self._thread_expanded_task_ids.intersection_update(active_thread_ids)
        try:
            self._render_list(select_first=False)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _inbox_content_signature(
        tasks: list,
        unread: set,
        replies_by_task: dict,
    ) -> tuple:
        """Cheap signature over the inbox's user-visible state.

        Two ticks with identical signatures render identical lists, so
        we can skip the full re-render. Any change the user would
        notice (new task, state change, reply count, unread flip) must
        perturb the signature; details that don't affect rendering
        (internal cache timestamps, raw object identity) must not.
        """
        task_sig = tuple(
            (
                getattr(t, "task_id", ""),
                getattr(t, "source", "task"),
                getattr(
                    getattr(t, "work_status", None), "value",
                    getattr(t, "work_status", ""),
                ),
                getattr(t, "current_node_id", "") or "",
                # updated_at may be datetime or string; coerce to str
                # so equality works across types.
                str(getattr(t, "updated_at", "") or ""),
                getattr(t, "project", "") or "",
                getattr(t, "triage_bucket", "") or "",
                getattr(t, "triage_label", "") or "",
                # reply count drives the "N replies" decoration
                len(replies_by_task.get(getattr(t, "task_id", ""), ()) or ()),
            )
            for t in tasks
        )
        unread_sig = tuple(sorted(unread))
        return (task_sig, unread_sig)

    # ------------------------------------------------------------------
    # Filter / search (#NEW)
    # ------------------------------------------------------------------

    def _reset_filter_state(self) -> None:
        """Clear filters back to the action-focused inbox baseline."""
        self._filter_text = ""
        self._filter_unread_only = False
        self._filter_project = None
        self._filter_recent = False
        self._filter_plan_review = False
        self._filter_blocking = False
        self._show_orphaned = False
        self._show_all_messages = False
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
                self._show_orphaned,
                self._show_all_messages,
            )
        )

    def _filtered_tasks(self, tasks: list) -> list:
        """Apply the AND-combined filter stack to ``tasks``.

        Cheap O(N * filters) — the inbox is at most a few hundred rows
        and the chips short-circuit, so we don't need anything fancier.
        """
        candidates = self._explicitly_filtered_tasks(tasks)
        if self._uses_action_lens_for(candidates):
            return [
                item for item in candidates
                if getattr(item, "needs_action", False)
            ]
        return candidates

    def _explicitly_filtered_tasks(self, tasks: list) -> list:
        """Apply user-selected filters, excluding the default action lens."""
        if not self._has_active_filters() and self._show_orphaned:
            return list(tasks)
        text_q = self._filter_text.strip().lower()
        proj = self._filter_project
        out: list = []
        recent_cutoff_ts = self._recent_cutoff_timestamp() if self._filter_recent else None
        for t in tasks:
            if not self._show_orphaned and getattr(t, "is_orphaned", False):
                continue
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

    def _uses_action_lens_for(self, tasks: list) -> bool:
        """Default inbox view: show actionable work, hide FYI noise."""
        if self._show_all_messages or self._show_orphaned or self._filter_text:
            return False
        return any(getattr(item, "needs_action", False) for item in tasks)

    def _task_haystack(self, task) -> str:
        """Concatenate searchable fields for fuzzy matching."""
        return " ".join(
            [
                task.title or "",
                task.description or "",
                task.project or "",
                _format_sender(task),
                " ".join(list(getattr(task, "labels", []) or [])),
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
        if self._show_orphaned:
            bits.append("show_orphaned")
        if self._uses_action_lens_for(self._explicitly_filtered_tasks(self._tasks)):
            bits.append("action_needed")
        elif self._show_all_messages:
            bits.append("all_messages")
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
        if self._show_orphaned:
            chip_bits.append("[on #1e2730] show orphaned [/on #1e2730]")
        if self._uses_action_lens_for(self._explicitly_filtered_tasks(self._tasks)):
            chip_bits.append("[on #1e2730] action needed [/on #1e2730]")
        elif self._show_all_messages:
            chip_bits.append("[on #1e2730] all messages [/on #1e2730]")
        if self._filter_text:
            chip_bits.append(
                f'[on #1e2730] "{_escape(self._filter_text)}" [/on #1e2730]'
            )
        if chip_bits:
            self.filter_chips.update("  ".join(chip_bits))
            self.filter_chips.display = True
        elif self._filter_bar_visible:
            self.filter_chips.update(
                "[dim]Filter: type to narrow messages · Esc closes[/dim]"
            )
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

    def action_toggle_filter_orphaned(self) -> None:
        if self.reply_input.has_focus or self.filter_input.has_focus:
            return
        self._show_orphaned = not self._show_orphaned
        self._render_list(select_first=True)

    def action_toggle_show_all_messages(self) -> None:
        if self.reply_input.has_focus or self.filter_input.has_focus:
            return
        self._show_all_messages = not self._show_all_messages
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

    def _item_for_id(self, item_id: str | None):
        if item_id is None:
            return None
        for item in self._tasks:
            if item.task_id == item_id:
                return item
        return None

    def _set_reply_mode_for_task(self) -> None:
        self.reply_input.disabled = False
        if self.reply_input.placeholder != "Why reject? (Enter to confirm, Esc to cancel)":
            self.reply_input.placeholder = "Reply … (Enter to send, Esc back to list)"

    def _set_reply_mode_for_message(self) -> None:
        self._awaiting_rejection_task_id = None
        self.reply_input.value = ""
        self.reply_input.disabled = True
        self.reply_input.placeholder = (
            "Notifications are read-only — press d to discuss or a to archive"
        )
        if self.reply_input.has_focus:
            self.list_view.focus()

    def _render_message_detail(self, item) -> None:
        from pollypm.tz import format_relative

        updated_iso = (
            item.updated_at.isoformat()
            if hasattr(item.updated_at, "isoformat") else str(item.updated_at or "")
        )
        created_iso = (
            item.created_at.isoformat()
            if hasattr(item.created_at, "isoformat") else str(item.created_at or "")
        )
        when = _fmt_time(updated_iso or created_iso)
        rel = format_relative(updated_iso or created_iso)

        sender = _format_sender(item)
        _session, pm_label = _resolve_pm_target(self.config_path, item.project)
        sections: list[str] = []
        # Same routing-tag strip as the list rail — the focused message
        # header should not lead with "[Action]" boilerplate.
        subject = item.title or "(no subject)"
        if _triage_bucket(item) == "action":
            subject = _strip_action_subject_prefix(subject)
        sections.append(f"[b #eef2f4]{_escape(subject)}[/b #eef2f4]")
        meta_bits = [f"[#5b8aff]{_escape(sender)}[/#5b8aff]"]
        if when:
            meta_bits.append(f"[#97a6b2]{_escape(when)}[/#97a6b2]")
        if rel:
            meta_bits.append(f"[dim]{_escape(rel)}[/dim]")
        # Workspace-root inbox items get ``project = "inbox"`` as the
        # default sentinel — previously suppressed so they rendered
        # with no project label at all and were indistinguishable from
        # an unlabeled item. Show "[workspace]" so users can tell the
        # source apart from per-project items. Audit UX #6.
        # NOTE: the inline ``· `` prefix used to live here from a
        # pre-join era where each bit emitted its own separator. Now
        # the ``"  ·  ".join(meta_bits)`` below adds the separator,
        # so the prefix produced a duplicate (``· · [workspace]``)
        # visible on the polly_remote inbox detail (Sam, 2026-04-26).
        if item.project == "inbox":
            meta_bits.append("[dim]\\[workspace][/dim]")
        elif item.project:
            meta_bits.append(f"[dim]{_escape(item.project)}[/dim]")
        prio = getattr(item.priority, "value", str(item.priority))
        if prio and prio != "normal":
            meta_bits.append(f"[#f0c45a]◆ {_escape(prio)}[/#f0c45a]")
        meta_bits.append(f"[dim #6b7a88]PM: {_escape(pm_label)}[/dim #6b7a88]")
        sections.append("  ·  ".join(meta_bits))
        tags = [item.message_type or "notify", item.tier or "immediate"]
        labels = list(getattr(item, "labels", []) or [])
        tags.extend(labels)
        sections.append(f"[dim]{_escape(' · '.join([tag for tag in tags if tag]))}[/dim]")
        triage_banner = _render_inbox_triage_banner(item)
        if triage_banner:
            sections.append(triage_banner)
        prompt_block = _render_user_prompt_block(getattr(item, "payload", None))
        if not prompt_block and _triage_bucket(item) == "action":
            # Legacy messages that haven't migrated to the structured
            # ``user_prompt`` payload still benefit from a heuristic
            # summary + steps block at the top — that's what the
            # dashboard's Action Needed card renders for the same
            # data, and the inbox detail surface should match. The
            # raw body still renders below for technical context.
            prompt_block = _render_heuristic_action_block(item.description)
        if prompt_block:
            sections.append("")
            sections.append(prompt_block)
            sections.append("")
            sections.append("[dim]── details from Polly ──[/dim]")
        sections.append("")
        sections.append(_md_to_rich(_escape_body(item.description or "(no body)")))
        self.detail.update("\n".join(sections))
        self._proposal_specs.pop(item.task_id, None)
        self._plan_review_meta.pop(item.task_id, None)
        self._plan_review_round_trip.pop(item.task_id, None)
        self._blocking_question_meta.pop(item.task_id, None)
        self._clear_rollup_items()
        self._update_hint_for_message()
        self._set_reply_mode_for_message()
        try:
            self.query_one("#inbox-detail-scroll", VerticalScroll).scroll_home(
                animate=False,
            )
        except Exception:  # noqa: BLE001
            pass

    def _render_detail(self, task_id: str) -> None:
        item = self._item_for_id(task_id)
        if item is None:
            self.detail.update("[red]Inbox item is no longer available.[/red]")
            self._clear_rollup_items()
            self._set_reply_mode_for_task()
            return
        if not is_task_inbox_entry(item):
            self._render_message_detail(item)
            return
        self._set_reply_mode_for_task()
        svc = self._svc_for_task(task_id)
        if svc is None:
            # Workspace-scoped tasks (project sentinel "inbox" or task IDs
            # whose project is not a registered project) have no per-
            # project database — we can still surface the message body
            # by falling back to the generic message renderer instead of
            # parking the user on a red error string (#855).
            project_key = task_id.split("/", 1)[0] if "/" in task_id else None
            is_workspace = (
                project_key is None
                or project_key in {"inbox", "workspace", "[workspace]"}
                or self._project_key_is_unknown(project_key)
            )
            if is_workspace:
                self._render_message_detail(item)
                return
            self.detail.update(
                "[#f0c45a]This task lives in a project that is not "
                "currently registered with PollyPM. Add the project "
                "with `pm add-project` to load its details here.[/#f0c45a]"
            )
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
        # Same routing-tag strip as the list rail — the focused message
        # header should not lead with "[Action]" boilerplate.
        if getattr(task, "triage_bucket", "") == "action":
            subject = _strip_action_subject_prefix(subject)
        sections.append(f"[b #eef2f4]{_escape(subject)}[/b #eef2f4]")
        meta_bits = [f"[#5b8aff]{_escape(sender)}[/#5b8aff]"]
        if when:
            meta_bits.append(f"[#97a6b2]{_escape(when)}[/#97a6b2]")
        if rel:
            meta_bits.append(f"[dim]{_escape(rel)}[/dim]")
        # See note above on the list-rail render: workspace-root
        # items use ``project == "inbox"`` as the default sentinel;
        # surface "[workspace]" rather than swallow the label. The
        # ``"  \u00b7  ".join(meta_bits)`` below adds the separator, so
        # this bit must NOT prepend its own ``\u00b7 `` (mirrors the
        # message-detail fix on line 6148).
        if task.project == "inbox":
            meta_bits.append("[dim]\\[workspace][/dim]")
        elif task.project:
            meta_bits.append(f"[dim]{_escape(task.project)}[/dim]")
        prio = getattr(task.priority, "value", str(task.priority))
        if prio and prio != "normal":
            meta_bits.append(f"[#f0c45a]\u25c6 {_escape(prio)}[/#f0c45a]")
        # PM hint — dim, trailing, so it reads as metadata not a heading.
        meta_bits.append(f"[dim #6b7a88]PM: {_escape(pm_label)}[/dim #6b7a88]")
        sections.append("  \u00b7  ".join(meta_bits))
        triage_banner = _render_inbox_triage_banner(task)
        if triage_banner:
            sections.append(triage_banner)
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

        # #761 — when the inbox item references a task in review state
        # (plan_review, review_ready, etc.), pull in the review artifact
        # that the task-Review tab renders (#708) so the user can see
        # the summary inline without a separate pane-jump. Same
        # component, same content, same mental model across surfaces.
        try:
            review_block = self._render_inline_review_artifact(task)
        except Exception:  # noqa: BLE001
            review_block = None
        if review_block:
            sections.append("")
            sections.append("[dim]── review artifact ──[/dim]")
            sections.append(review_block)

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

    def _render_inline_review_artifact(self, task) -> str | None:
        """Render the task's review artifact for inline display in the
        inbox detail pane (#761).

        Reuses :mod:`pollypm.cockpit_task_review` — the same module
        that powers the per-task Review tab (#708). When a task has no
        artifact, or a load error occurs, returns None so the caller
        skips the section entirely rather than showing an empty block.
        """
        from pollypm.cockpit_task_review import (
            load_task_review_artifact,
            render_task_review_artifact,
        )

        project_path = None
        try:
            config = load_config(self.config_path)
            project = config.projects.get(getattr(task, "project", "") or "")
            if project is not None:
                project_path = project.path
        except Exception:  # noqa: BLE001
            project_path = None

        artifact = load_task_review_artifact(task, project_path)
        if artifact is None:
            return None
        rendered = render_task_review_artifact(artifact)
        if not rendered or rendered.strip() == "No review artifact is available for this task yet.":
            return None
        return _escape(rendered)

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
        if idx is None or idx < 0 or idx >= len(self._visible_rows):
            return
        row = self._visible_rows[idx]
        if row.key == self._selected_row_key:
            return
        task_changed = row.task_id != self._selected_task_id
        self._selected_task_id = row.task_id
        self._selected_row_key = row.key
        # Clear any in-progress reply draft when the selection changes so
        # a half-typed message doesn't get posted to a different task.
        if task_changed and self.reply_input.value:
            self.reply_input.value = ""
        self._render_detail(row.task_id)
        self._mark_open_read(row.task_id)

    def action_cursor_down(self) -> None:
        if self.reply_input.has_focus or self.filter_input.has_focus:
            return
        self.list_view.action_cursor_down()
        self._sync_selection_from_list()

    def action_cursor_up(self) -> None:
        if self.reply_input.has_focus or self.filter_input.has_focus:
            return
        self.list_view.action_cursor_up()
        self._sync_selection_from_list()

    def action_cursor_first(self) -> None:
        if self._visible_rows:
            self.list_view.index = 0
            self._sync_selection_from_list()

    def action_cursor_last(self) -> None:
        if self._visible_rows:
            self.list_view.index = len(self._visible_rows) - 1
            self._sync_selection_from_list()

    def action_thread_right(self) -> None:
        if self.reply_input.has_focus or self.filter_input.has_focus:
            return
        action, target = inbox_thread_right_action(
            self._visible_rows, self.list_view.index,
        )
        if action == "expand":
            current = self.list_view.index
            if current is None or current < 0 or current >= len(self._visible_rows):
                return
            task_id = self._visible_rows[current].task_id
            self._thread_expanded_task_ids.add(task_id)
            self._selected_row_key = self._visible_rows[current].key
            self._render_list(select_first=False)
            return
        if action == "select_child" and target is not None:
            self.list_view.index = target
            self._sync_selection_from_list()

    def action_thread_left(self) -> None:
        if self.reply_input.has_focus or self.filter_input.has_focus:
            return
        action, target = inbox_thread_left_action(
            self._visible_rows, self.list_view.index,
        )
        if action == "collapse":
            current = self.list_view.index
            if current is None or current < 0 or current >= len(self._visible_rows):
                return
            task_id = self._visible_rows[current].task_id
            self._thread_expanded_task_ids.discard(task_id)
            self._selected_row_key = f"task:{task_id}"
            self._render_list(select_first=False)
            return
        if action == "select_parent" and target is not None:
            self.list_view.index = target
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
        self._selected_row_key = row.row_ref.key
        self._render_detail(row.task_id)
        self._mark_open_read(row.task_id)

    @on(ListView.Highlighted, "#inbox-list")
    def _on_row_highlighted(self, event: ListView.Highlighted) -> None:
        # Keyboard j/k emits Highlighted before any Selected; render eagerly
        # so the right pane tracks the cursor without requiring Enter.
        row = event.item
        if not isinstance(row, _InboxListItem):
            return
        if self._selected_row_key == row.row_ref.key:
            return
        task_changed = self._selected_task_id != row.task_id
        self._selected_task_id = row.task_id
        self._selected_row_key = row.row_ref.key
        if task_changed and self.reply_input.value:
            self.reply_input.value = ""
        self._render_detail(row.task_id)

    # ------------------------------------------------------------------
    # Read / archive / reply actions
    # ------------------------------------------------------------------

    def _mark_open_read(self, task_id: str) -> None:
        if task_id not in self._unread_ids:
            return
        item = self._item_for_id(task_id)
        if item is None:
            return
        if not is_task_inbox_entry(item):
            self._session_read_ids.add(task_id)
            self._unread_ids.discard(task_id)
            try:
                for row in self.list_view.children:
                    if (
                        isinstance(row, _InboxListItem)
                        and row.row_ref.is_task
                        and row.task_id == task_id
                    ):
                        row.mark_read()
                        break
            except Exception:  # noqa: BLE001
                pass
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
            for row in self.list_view.children:
                if (
                    isinstance(row, _InboxListItem)
                    and row.row_ref.is_task
                    and row.task_id == task_id
                ):
                    row.mark_read()
                    break
        except Exception:  # noqa: BLE001
            pass

    def action_archive_selected(self) -> None:
        task_id = self._selected_task_id
        if task_id is None:
            return
        item = self._item_for_id(task_id)
        if item is None:
            return
        if not is_task_inbox_entry(item):
            try:
                from pollypm.store import SQLAlchemyStore

                store = SQLAlchemyStore(f"sqlite:///{item.db_path}")
                try:
                    store.close_message(int(item.message_id))
                finally:
                    store.close()
            except Exception as exc:  # noqa: BLE001
                self.notify(f"Archive failed: {exc}", severity="error")
                return
            self.notify(f"Archived {task_id}", severity="information", timeout=2.0)
            self._tasks = [task for task in self._tasks if task.task_id != task_id]
            self._unread_ids.discard(task_id)
            self._session_read_ids.discard(task_id)
            self._replies_by_task.pop(task_id, None)
            self._thread_expanded_task_ids.discard(task_id)
            if self._selected_task_id == task_id:
                self._selected_task_id = None
                self._selected_row_key = None
            self._render_list(select_first=bool(self._tasks))
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
        self._replies_by_task.pop(task_id, None)
        self._thread_expanded_task_ids.discard(task_id)
        if self._selected_task_id == task_id:
            self._selected_task_id = None
            self._selected_row_key = None
        self._render_list(select_first=bool(self._tasks))

    def action_start_reply(self) -> None:
        """Keyboard shortcut: focus the always-visible reply input."""
        task_id = self._selected_task_id
        if task_id is None:
            return
        item = self._item_for_id(task_id)
        if item is not None and not is_task_inbox_entry(item):
            self.notify(
                "Notifications are read-only — press d to discuss or a to archive.",
                severity="warning",
                timeout=2.5,
            )
            self.list_view.focus()
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
        item = self._item_for_id(task_id)
        if item is None:
            return
        if is_task_inbox_entry(item):
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
        else:
            task = item

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
        item = self._item_for_id(task_id)
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
        if item is None or not is_task_inbox_entry(item):
            self.notify(
                "Notifications are read-only — press d to discuss or a to archive.",
                severity="warning",
                timeout=2.5,
            )
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
        self._refresh_list(select_first=False)

    # ------------------------------------------------------------------
    # Improvement proposals (#275) — Accept / Reject
    # ------------------------------------------------------------------

    _DEFAULT_HINT = (
        "j/k move \u00b7 \u21b5 open \u00b7 r reply \u00b7 a archive "
        "\u00b7 d discuss \u00b7 / filter \u00b7 m all \u00b7 c clear \u00b7 q close"
    )
    _MESSAGE_HINT = (
        "j/k move \u00b7 \u21b5 open \u00b7 a archive "
        "\u00b7 d discuss \u00b7 / filter \u00b7 m all \u00b7 c clear \u00b7 q close"
    )
    _PROPOSAL_HINT = (
        "A accept \u00b7 X reject \u00b7 r reply \u00b7 q close"
    )
    # Plan-review hint bars (#297). The gated variant hides ``A`` until
    # the thread has a round-trip; the ungated variant surfaces it.
    _PLAN_REVIEW_HINT_GATED = (
        "v open explainer \u00b7 d discuss with PM \u00b7 q close"
    )
    _PLAN_REVIEW_HINT_OPEN = (
        "v open explainer \u00b7 d discuss \u00b7 A approve \u00b7 q close"
    )
    _PLAN_REVIEW_HINT_FAST_TRACK = (
        "v open explainer \u00b7 d discuss \u00b7 A approve \u00b7 q close"
    )
    # Blocking-question hint (#302). ``r`` replies to the worker via
    # ``pm send --force`` so the blocker clears without the PM needing
    # to jump to the pane; ``d`` is the direct-conversation escape
    # hatch; ``a`` archives once the blocker is resolved.
    _BLOCKING_QUESTION_HINT = (
        "r reply to worker \u00b7 d jump to worker \u00b7 "
        "a archive \u00b7 q close"
    )

    def _update_hint_for_blocking_question(self) -> None:
        try:
            self.hint.update(self._BLOCKING_QUESTION_HINT)
        except Exception:  # noqa: BLE001
            pass

    def _update_hint_for_message(self) -> None:
        try:
            self.hint.update(self._MESSAGE_HINT)
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
        item = self._item_for_id(task_id)
        if item is None or not is_task_inbox_entry(item):
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
        item = self._item_for_id(task_id)
        if item is None or not is_task_inbox_entry(item):
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
        if getattr(svc, "last_first_shipped_created", False):
            _celebrate_first_shipped(self)
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
    return str(s).replace("[", r"\[")


def _escape_body(s: str) -> str:
    """Escape Rich brackets for body text while preserving newlines.

    ``_md_to_rich`` re-adds its own markup; we only need to neutralise
    user-typed brackets so they render as literal characters.
    """
    if not s:
        return ""
    return str(s).replace("[", r"\[")


# ---------------------------------------------------------------------------
# Per-project dashboard (Textual screen) — #245 follow-up, replaces the
# read-only Static text dump that ``kind == "project"`` used to render
# via ``PollyCockpitPaneApp``.
# ---------------------------------------------------------------------------


from pollypm.plugins_builtin.project_planning.plan_presence import (
    CANONICAL_PLAN_RELATIVE_PATHS as _PLAN_FILE_CANDIDATES,
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


# Per-project plan-staleness cache. Key includes plan_mtime AND
# state.db mtime so the cache invalidates whenever the inputs that
# could change the answer change — no TTL needed. Cycle 133 perf fix:
# the previous implementation opened SQLiteWorkService and walked
# the whole task list on every per-project dashboard refresh tick
# (every 10s). With this cache, a project with no plan or task
# changes pays zero work past the first refresh.
_PLAN_STALENESS_CACHE: dict[tuple[str, float | None, float | None], str | None] = {}


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
    # Cache key: project + plan mtime + db mtime. If neither file has
    # changed since the last call, the answer cannot have changed.
    try:
        db_mtime = db_path.stat().st_mtime
    except OSError:
        db_mtime = None
    cache_key = (project_key, plan_mtime, db_mtime)
    if cache_key in _PLAN_STALENESS_CACHE:
        return _PLAN_STALENESS_CACHE[cache_key]
    try:
        from pollypm.plugins_builtin.project_planning.plan_presence import (
            _find_approved_plan_task,
            _plan_approved_at,
        )
        from pollypm.work.sqlite_service import SQLiteWorkService
    except Exception:  # noqa: BLE001
        return None
    result: str | None = None
    try:
        with SQLiteWorkService(
            db_path=db_path, project_path=project_path,
        ) as svc:
            plan_task = _find_approved_plan_task(svc, project_key)
            if plan_task is None:
                _PLAN_STALENESS_CACHE[cache_key] = None
                return None
            approved_at = _plan_approved_at(svc, plan_task)
            if approved_at is None:
                _PLAN_STALENESS_CACHE[cache_key] = None
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
                result = "plan approved before latest backlog task"
    except Exception:  # noqa: BLE001
        # Don't cache failures — let the next refresh retry.
        return None
    _PLAN_STALENESS_CACHE[cache_key] = result
    return result


def _dashboard_status(
    active_worker: dict | None,
    inbox_count: int,
    alert_count: int,
    idle_minutes: float | None,
    blocker_count: int = 0,
    on_hold_count: int = 0,
    in_progress_count: int = 0,
) -> tuple[str, str, str]:
    """Return (dot, colour, label) for the top-bar project status light.

    * Green — a worker is heartbeat-alive on this project right now.
    * Yellow — the user has inbox items or actionable alerts on this
      project (nothing in flight but attention is required).
    * Dim — idle / no activity.
    """
    # Priority mirrors the banner copy in
    # ``_render_project_state_banner`` so the pill and the banner agree.
    # User-attention states must outrank an active worker \u2014 saying
    # "active" green while the banner says "Waiting on you" is the
    # contradiction the v1 doc called out as a false-positive green
    # light (architect running in the background \u2260 "nothing for me to
    # do here").
    if inbox_count or on_hold_count:
        return ("\u25c6", "#f0c45a", "needs attention")
    if alert_count:
        return ("\u25c6", "#f85149", "alert")
    if active_worker is not None:
        return ("\u25cf", "#3ddc84", "active")
    # #920 \u2014 a task in ``in_progress`` is claimed work, regardless of
    # whether a heartbeat has registered yet. Showing "idle" while a
    # task is in_progress is the lie #920 reproduced.
    if in_progress_count:
        return ("\u25c6", "#f0c45a", "in progress")
    if blocker_count:
        return ("\u25cb", "#6b7a88", "waiting on dependencies")
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
        "action_items",
        "alert_count",
        "enforce_plan",
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
        action_items: list[dict],
        alert_count: int,
        enforce_plan: bool = True,
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
        self.action_items = action_items
        self.alert_count = alert_count
        self.enforce_plan = enforce_plan


# Module-level cache keyed by project plus the task DB paths/mtimes so a
# rapidly-rerendering dashboard doesn't hammer SQLite for the same data. The
# dashboard refreshes every 10s by default; stale-cache hits are a net win
# there too.
_PROJECT_DASHBOARD_TASK_CACHE: dict[
    tuple[str, tuple[tuple[str, float], ...]],
    tuple[dict[str, int], dict[str, list[dict]]],
] = {}


def _dashboard_plain_text(value: object | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = _re.sub(r"[*_`#>\[\]]+", "", text)
    text = " ".join(part.strip() for part in text.splitlines() if part.strip())
    return _re.sub(r"^\([a-zA-Z]\)\s+", "", text)


def _dashboard_trim(text: str, *, limit: int = 220) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _dashboard_summary_from_body(body: str) -> str:
    paragraphs = [
        _dashboard_plain_text(part)
        for part in body.replace("\\n", "\n").split("\n\n")
        if _dashboard_plain_text(part)
    ]
    for paragraph in paragraphs:
        lower = paragraph.lower()
        if lower.startswith("blocker:"):
            return _dashboard_trim(paragraph[8:].strip())
    for paragraph in paragraphs:
        lower = paragraph.lower()
        if lower.startswith(("task:", "status:")):
            continue
        if any(
            token in lower
            for token in (
                "blocked",
                "waiting on",
                "without ",
                "requires ",
                "acceptance gate",
                "scope split",
                "not achievable",
                "request one of",
                "need your call",
                "needs your call",
            )
        ):
            return _dashboard_trim(paragraph)
    for paragraph in paragraphs:
        lower = paragraph.lower()
        if lower.startswith(("task:", "status:")):
            continue
        return _dashboard_trim(paragraph)
    return ""


def _dashboard_requirement_step(part: str) -> str:
    cleaned = _dashboard_plain_text(part).strip(" .,:;")
    if not cleaned:
        return ""
    lower = cleaned.lower()
    if lower.startswith(("a ", "an ", "the ")):
        cleaned = cleaned.split(" ", 1)[1]
        lower = cleaned.lower()
    if lower.endswith(" provisioned"):
        cleaned = cleaned[: -len(" provisioned")]
        return f"Provision {cleaned}"
    if any(token in lower for token in ("cred", "access", "token", "login")):
        return f"Grant {cleaned}"
    if any(token in lower for token in ("app", "pipeline", "deploy", "fly.io")):
        return f"Set up {cleaned}"
    if any(token in lower for token in ("postgres", "redis", "database")):
        return f"Provision {cleaned}"
    if any(
        lower.startswith(verb)
        for verb in (
            "accept",
            "reopen",
            "create",
            "run",
            "exercise",
            "verify",
            "choose",
            "split",
            "grant",
        )
    ):
        return cleaned[:1].upper() + cleaned[1:]
    return cleaned[:1].upper() + cleaned[1:]


def _dashboard_steps_from_body(body: str) -> list[str]:
    body = body.replace("\\n", "\n")
    steps: list[str] = []
    for line in body.splitlines():
        match = _ACTION_STEP_RE.match(line)
        if match is not None:
            step = _dashboard_plain_text(match.group("step"))
            if step:
                steps.append(step)
    for paragraph in body.split("\n\n"):
        plain = _dashboard_plain_text(paragraph)
        lower = plain.lower()
        for marker in ("without ", "requires "):
            idx = lower.find(marker)
            if idx < 0:
                continue
            chunk = plain[idx + len(marker):].split(". ", 1)[0]
            chunk = chunk.replace(", and ", ", ").replace(" and ", ", ")
            for part in chunk.split(","):
                step = _dashboard_requirement_step(part)
                if step:
                    steps.append(step)
    deduped: list[str] = []
    seen: set[str] = set()
    for step in steps:
        key = step.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(step)
    return deduped[:4]


def _dashboard_task_blocker_body(task: object) -> str:
    """Pick the clearest human-facing blocker text from a task."""
    context = list(getattr(task, "context", []) or [])
    for entry in reversed(context):
        text = str(getattr(entry, "text", "") or "").strip()
        lower = text.lower()
        if not text:
            continue
        if any(
            token in lower
            for token in (
                "blocker",
                "blocked",
                "scope split",
                "request one of",
                "not achievable",
                "requires",
                "waiting on",
            )
        ):
            return text
    return str(getattr(task, "description", "") or "")


def _dashboard_task_db_paths(config: object, project_path: Path) -> list[Path]:
    """Return task DBs that can hold work for a registered project."""
    candidates: list[Path] = []

    def _add(path: object) -> None:
        try:
            candidate = Path(path)
        except TypeError:
            return
        if candidate not in candidates and candidate.exists():
            candidates.append(candidate)

    _add(project_path / ".pollypm" / "state.db")

    project_settings = getattr(config, "project", None)
    workspace_root = getattr(project_settings, "workspace_root", None)
    if workspace_root is not None:
        _add(Path(workspace_root) / ".pollypm" / "state.db")

    state_db = getattr(project_settings, "state_db", None)
    if state_db is not None:
        _add(state_db)

    return candidates


def _project_storage_aliases(config: object, project_key: str) -> list[str]:
    """Return every project-name form the work-service may have stored.

    The TOML config key is slugified (``blackjack_trainer``) while the
    work DB stores tasks under the project's ``name`` (``blackjack-trainer``)
    or the on-disk directory name. ``list_tasks(project=...)`` and
    ``state_counts(project=...)`` both do an exact ``project = ?``
    match, so a dashboard or tasks-pane that only passes the config key
    silently shows zero tasks for projects whose key and display name
    differ — issue #920. Return a deduped, ordered list of every form
    we want to try so callers can union the results.
    """
    aliases: list[str] = []

    def _add(value: object) -> None:
        if not isinstance(value, str):
            return
        text = value.strip()
        if not text or text in aliases:
            return
        aliases.append(text)

    _add(project_key)
    project = (getattr(config, "projects", None) or {}).get(project_key)
    if project is not None:
        _add(getattr(project, "name", None))
        path = getattr(project, "path", None)
        if path is not None:
            try:
                _add(Path(path).name)
            except TypeError:
                pass
    # Hyphen <-> underscore swap covers the common slugify ambiguity
    # without depending on a config lookup.
    _add(project_key.replace("_", "-"))
    _add(project_key.replace("-", "_"))
    return aliases


def _dashboard_gather_tasks(
    config: object, project_key: str, project_path: Path,
) -> tuple[dict[str, int], dict[str, list[dict]]]:
    """Fetch task counts + top-N titles per status bucket for a project.

    Uses the same mtime-cache trick as ``_dashboard_project_tasks`` so
    the overall dashboard tick stays cheap when the work service has
    no new writes. Only small dict views of each task are cached (never
    full ``Task`` objects) to keep the cache footprint bounded.
    """
    db_paths = _dashboard_task_db_paths(config, project_path)
    if not db_paths:
        return {}, {}

    cache_parts: list[tuple[str, float]] = []
    for db_path in db_paths:
        try:
            cache_parts.append((str(db_path), db_path.stat().st_mtime))
        except OSError:
            continue
    if not cache_parts:
        return {}, {}
    cache_token = tuple(cache_parts)
    cached = _PROJECT_DASHBOARD_TASK_CACHE.get((project_key, cache_token))
    if cached is not None:
        return cached

    try:
        from pollypm.work.sqlite_service import SQLiteWorkService
    except Exception:  # noqa: BLE001
        return {}, {}

    buckets: dict[str, list[dict]] = {
        "queued": [],
        "in_progress": [],
        "review": [],
        "blocked": [],
        "on_hold": [],
        "done": [],
    }
    # #920 — work-service stores tasks under the project's display name
    # (``blackjack-trainer``) but the dashboard receives the slugified
    # config key (``blackjack_trainer``). ``list_tasks(project=...)``
    # does an exact match, so query every known alias and union the
    # results (tasks are deduped by ``task_id`` below).
    aliases = _project_storage_aliases(config, project_key)
    task_rows: dict[str, tuple[str, dict]] = {}
    for db_path in db_paths:
        try:
            with SQLiteWorkService(db_path=db_path, project_path=project_path) as svc:
                tasks: list = []
                seen_ids: set[str] = set()
                for alias in aliases:
                    for found in svc.list_tasks(project=alias):
                        tid = getattr(found, "task_id", None)
                        if not tid or tid in seen_ids:
                            continue
                        seen_ids.add(tid)
                        tasks.append(found)
        except Exception:  # noqa: BLE001
            continue
        for t in tasks:
            status = getattr(t.work_status, "value", "")
            if status not in buckets:
                continue
            updated_at = getattr(t, "updated_at", "") or ""
            # Normalise to ISO-8601 string — task.updated_at comes
            # back as datetime from the work-service hydrator.
            if hasattr(updated_at, "isoformat"):
                updated_at = updated_at.isoformat()
            blocker_body = _dashboard_task_blocker_body(t)
            hold_reason = ""
            if status == "on_hold":
                # Surface the most recent on_hold transition's
                # reason so the pipeline pane can tell the operator
                # *why* a task is paused, not just that it is.
                for transition in reversed(getattr(t, "transitions", []) or []):
                    if getattr(transition, "to_state", "") == "on_hold":
                        hold_reason = (getattr(transition, "reason", "") or "").strip()
                        break
            row = {
                "task_id": t.task_id,
                "task_number": getattr(t, "task_number", None),
                "title": getattr(t, "title", "") or "(untitled)",
                "updated_at": updated_at,
                "assignee": getattr(t, "assignee", None),
                "current_node_id": getattr(t, "current_node_id", None),
                "summary": _dashboard_summary_from_body(blocker_body),
                "steps": _dashboard_steps_from_body(blocker_body),
                "hold_reason": hold_reason,
                "blocked_by": [
                    f"{proj}/{num}"
                    for proj, num in getattr(t, "blocked_by", [])
                ],
                "source_db": str(db_path),
            }
            existing = task_rows.get(t.task_id)
            if (
                existing is None
                or str(row["updated_at"] or "")
                > str(existing[1].get("updated_at") or "")
            ):
                task_rows[t.task_id] = (status, row)

    for status, row in task_rows.values():
        buckets[status].append(row)

    counts = {
        status: len(items)
        for status, items in buckets.items()
        if items
    }

    for status, items in buckets.items():
        items.sort(key=lambda d: d["updated_at"] or "", reverse=True)

    _PROJECT_DASHBOARD_TASK_CACHE[(project_key, cache_token)] = (counts, buckets)
    return counts, buckets


def _dashboard_active_worker(
    config_path: Path,
    project_key: str,
    *,
    action_items: list[dict] | None = None,
) -> tuple[dict | None, int]:
    """Inspect supervisor state for a live worker on this project.

    Returns ``(worker_info, alert_count)`` where ``worker_info`` is
    ``None`` when no worker is currently heartbeat-alive. ``alert_count``
    counts actionable alerts scoped to this project's sessions so the
    top bar can render the yellow "needs attention" light even when the
    worker is idle.

    When ``action_items`` is supplied, ``stuck_on_task:<task_id>``
    alerts whose task is already represented by an Action Needed card
    are excluded from the count — the user can already see the work
    needs their input, and the mechanically-derived "stuck" alert is
    just the same fact in different words.
    """
    from datetime import UTC, datetime, timedelta

    worker_info: dict | None = None
    alert_count = 0
    try:
        from pollypm.service_api import PollyPMService
        supervisor = PollyPMService(config_path).load_supervisor()
    except Exception:  # noqa: BLE001
        return None, 0
    try:
        try:
            launches = list(supervisor.plan_launches())
        except Exception:  # noqa: BLE001
            launches = []
        # Skip control-plane roles (operator-pm, reviewer,
        # heartbeat-supervisor, triage) — they're system-wide
        # processes, not real work on this project. Counting them
        # as active_worker makes the banner read "Moving now:
        # heartbeat (heartbeat-supervisor) is active" or "Moving
        # now: reviewer (reviewer) is active" when the project
        # actually has nothing in flight; the genuine signal is
        # whether a worker or architect is alive.
        from pollypm.models import CONTROL_ROLES as _CONTROL_ROLES
        # #920 — accept any project alias (config key, display name,
        # path basename, hyphen/underscore swap) so workers launched
        # under the work-DB form (``blackjack-trainer``) are still
        # recognised when the dashboard receives the slugified config
        # key (``blackjack_trainer``).
        try:
            _config = load_config(config_path)
        except Exception:  # noqa: BLE001
            _config = None
        _alias_set = set(_project_storage_aliases(_config, project_key))
        project_sessions = [
            launch.session for launch in launches
            if getattr(launch.session, "project", None) in _alias_set
            and getattr(launch.session, "role", "") not in _CONTROL_ROLES
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
            from pollypm.cockpit_alerts import is_operational_alert

            project_session_names = {
                s.name for s in project_sessions
            }
            # #920 — include the plan_gate session for every project alias
            # so alert counts stay correct under hyphen/underscore swaps.
            for _alias in _alias_set:
                project_session_names.add(f"plan_gate-{_alias}")
            open_alerts_fn = getattr(supervisor, "open_alerts", None)
            if callable(open_alerts_fn):
                open_alerts = open_alerts_fn()
            else:
                open_alerts = supervisor.store.open_alerts()
            covered_task_ids = {
                str(item.get("primary_ref"))
                for item in (action_items or [])
                if item.get("primary_ref")
            }
            alert_count = sum(
                1 for a in open_alerts
                if getattr(a, "session_name", None) in project_session_names
                and not is_operational_alert(getattr(a, "alert_type", ""))
                and not _stuck_alert_covers_action(
                    getattr(a, "alert_type", ""), covered_task_ids,
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


# Cache the project-scoped inbox snapshot by (project, db_mtime).
# The per-project dashboard refreshes every 10s and used to re-open
# the SQLite Store + WorkService on every tick — even when no inbox
# write had landed since the last call. Keying on the state.db
# mtime gives content-addressed invalidation: any new message,
# task, or context entry bumps the mtime and the cache misses; an
# idle project pays one stat() per tick instead of two DB opens.
# Sister to ``_PLAN_STALENESS_CACHE`` (cycle 133).
_DASHBOARD_INBOX_CACHE: dict[
    tuple[str, float | None],
    tuple[int, list[dict], list[dict]],
] = {}


def _dashboard_inbox(
    config_path: Path, project_key: str, project_path: Path,
) -> tuple[int, list[dict], list[dict]]:
    """Return project-scoped inbox items + actionable PM blocker notes."""
    from datetime import datetime

    db_path = project_path / ".pollypm" / "state.db"
    if not db_path.exists():
        return 0, [], []
    # Content-addressed cache: an unchanged db_mtime means no inbox
    # writes since the last call, so the answer is unchanged. The
    # lone ``stat()`` is essentially free compared to the two DB
    # opens + queries inside this function.
    try:
        db_mtime: float | None = db_path.stat().st_mtime
    except OSError:
        db_mtime = None
    cache_key = (project_key, db_mtime)
    cached = _DASHBOARD_INBOX_CACHE.get(cache_key)
    if cached is not None:
        return cached
    try:
        from pollypm.store import SQLAlchemyStore
        from pollypm.work.inbox_view import inbox_tasks
        from pollypm.work.sqlite_service import SQLiteWorkService
        from pollypm.cockpit_inbox import _row_is_dev_channel
    except Exception:  # noqa: BLE001
        return 0, [], []
    try:
        config = load_config(config_path)
    except Exception:  # noqa: BLE001
        return 0, [], []

    def _sort_value(value: object) -> float:
        if not value:
            return 0.0
        try:
            if hasattr(value, "timestamp"):
                return float(value.timestamp())
            return float(datetime.fromisoformat(str(value)).timestamp())
        except Exception:  # noqa: BLE001
            return 0.0

    def _plain_text(value: object | None) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        text = _re.sub(r"[*_`#>\[\]]+", "", text)
        text = " ".join(part.strip() for part in text.splitlines() if part.strip())
        return _re.sub(r"^\([a-zA-Z]\)\s+", "", text)

    def _labels_from_row(row: dict[str, object]) -> list[str]:
        raw = row.get("labels") or []
        if isinstance(raw, list):
            return [str(label) for label in raw if str(label).strip()]
        if isinstance(raw, str):
            try:
                loaded = json.loads(raw)
            except Exception:  # noqa: BLE001
                loaded = []
            if isinstance(loaded, list):
                return [str(label) for label in loaded if str(label).strip()]
        return []

    def _message_body(value: object | None) -> str:
        text = str(value or "")
        # Some PM handoff notes arrive through shell-escaped paths and
        # persist literal "\n" sequences. Normalize before extracting
        # blocker paragraphs and numbered action steps.
        return text.replace("\\n", "\n")

    def _message_projects(row: dict[str, object]) -> set[str]:
        projects: set[str] = set()
        known_projects = set(getattr(config, "projects", {}).keys())
        payload = row.get("payload") or {}
        if isinstance(payload, dict):
            for key in ("project", "task_project"):
                value = payload.get(key)
                if isinstance(value, str) and value in known_projects:
                    projects.add(value)
        scope = row.get("scope")
        if isinstance(scope, str) and scope in known_projects:
            projects.add(scope)
        text = "\n".join(
            str(part or "") for part in (row.get("subject"), row.get("body"))
        )
        for match in _PROJECT_TASK_REF_RE.finditer(text):
            project = match.group("project")
            if project in known_projects:
                projects.add(project)
        return projects

    def _trim(text: str, *, limit: int = 220) -> str:
        text = text.strip()
        if len(text) <= limit:
            return text
        return text[: limit - 1].rstrip() + "…"

    def _summary_from_body(body: str) -> str:
        paragraphs = [
            _plain_text(part)
            for part in body.split("\n\n")
            if _plain_text(part)
        ]
        for paragraph in paragraphs:
            lower = paragraph.lower()
            if lower.startswith("blocker:"):
                return _trim(paragraph[8:].strip())
        for paragraph in paragraphs:
            lower = paragraph.lower()
            if lower.startswith(("task:", "status:")):
                continue
            if any(
                token in lower
                for token in (
                    "blocked",
                    "waiting on",
                    "without ",
                    "requires ",
                    "acceptance gate",
                    "scope split",
                    "need your call",
                    "needs your call",
                )
            ):
                return _trim(paragraph)
        for paragraph in paragraphs:
            lower = paragraph.lower()
            if lower.startswith(("task:", "status:")):
                continue
            return _trim(paragraph)
        return ""

    def _requirement_step(part: str) -> str:
        cleaned = _plain_text(part).strip(" .,:;")
        if not cleaned:
            return ""
        lower = cleaned.lower()
        if lower.startswith(("a ", "an ", "the ")):
            cleaned = cleaned.split(" ", 1)[1]
            lower = cleaned.lower()
        if lower.endswith(" provisioned"):
            cleaned = cleaned[: -len(" provisioned")]
            lower = cleaned.lower()
            return f"Provision {cleaned}"
        if any(token in lower for token in ("cred", "access", "token", "login")):
            return f"Grant {cleaned}"
        if any(token in lower for token in ("app", "pipeline", "deploy", "fly.io")):
            return f"Set up {cleaned}"
        if any(token in lower for token in ("postgres", "redis", "database")):
            return f"Provision {cleaned}"
        if any(lower.startswith(verb) for verb in (
            "accept",
            "reopen",
            "create",
            "run",
            "exercise",
            "verify",
            "choose",
            "split",
        )):
            return cleaned[:1].upper() + cleaned[1:]
        return cleaned[:1].upper() + cleaned[1:]

    def _steps_from_body(body: str) -> list[str]:
        steps: list[str] = []
        for line in body.splitlines():
            match = _ACTION_STEP_RE.match(line)
            if match is not None:
                step = _plain_text(match.group("step"))
                if step:
                    steps.append(step)
        for paragraph in body.split("\n\n"):
            plain = _plain_text(paragraph)
            lower = plain.lower()
            for marker in ("without ", "requires "):
                idx = lower.find(marker)
                if idx < 0:
                    continue
                chunk = plain[idx + len(marker):].split(". ", 1)[0]
                chunk = chunk.replace(", and ", ", ").replace(" and ", ", ")
                for part in chunk.split(","):
                    step = _requirement_step(part)
                    if step:
                        steps.append(step)
        deduped: list[str] = []
        seen: set[str] = set()
        for step in steps:
            key = step.casefold()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(step)
        return deduped[:4]

    def _decision_prompt_from_body(
        subject: str, body: str, steps: list[str],
    ) -> str:
        paragraphs = [
            _plain_text(part)
            for part in body.split("\n\n")
            if _plain_text(part)
        ]
        for paragraph in paragraphs:
            lower = paragraph.lower()
            if "ruling needed:" in lower:
                prompt = paragraph.split(":", 1)[1].strip()
                return _trim(f"Decide: {prompt}", limit=260)
        if len(steps) >= 2:
            lower_body = body.lower()
            lower_subject = subject.lower()
            if (
                "options:" in lower_body
                or "this is your call" in lower_body
                or "decision" in lower_subject
                or "scope escalation" in lower_subject
                or "blocked" in lower_subject
            ):
                option_steps: list[str] = []
                in_options = False
                for line in body.splitlines():
                    if "options:" in line.lower():
                        in_options = True
                        continue
                    if not in_options:
                        continue
                    if line.strip().startswith("**") and option_steps:
                        break
                    match = _ACTION_STEP_RE.match(line)
                    if match is not None:
                        step = _plain_text(match.group("step"))
                        if step:
                            option_steps.append(step)
                if len(option_steps) >= 2:
                    return _trim(
                        f"Choose one: {option_steps[0]}; or {option_steps[1]}",
                        limit=260,
                    )
                return _trim(
                    f"Choose one: {steps[0]}; or {steps[1]}",
                    limit=260,
                )
        for paragraph in paragraphs:
            lower = paragraph.lower()
            if "your call" in lower or "needs your call" in lower:
                return _trim(f"Decide: {paragraph}", limit=260)
        if steps:
            return _trim(f"Next: {steps[0]}", limit=260)
        return ""

    def _user_prompt_decision(
        prompt: object, *, fallback_task_id: str | None = None,
    ) -> dict[str, object] | None:
        if not isinstance(prompt, dict):
            return None
        summary = _plain_text(prompt.get("summary"))
        question = _plain_text(prompt.get("question"))
        raw_steps = prompt.get("steps") or prompt.get("required_actions") or []
        if not isinstance(raw_steps, list):
            raw_steps = []
        steps = [
            _plain_text(step).rstrip(".") + "."
            for step in raw_steps
            if _plain_text(step)
        ][:5]
        raw_actions = prompt.get("actions") or []
        if not isinstance(raw_actions, list):
            raw_actions = []
        actions: list[dict[str, object]] = []
        for raw_action in raw_actions[:2]:
            if not isinstance(raw_action, dict):
                continue
            label = _plain_text(raw_action.get("label"))
            # ``kind`` is a structured dispatch identifier — values like
            # ``approve_task``, ``review_plan``, ``open_inbox``. We MUST
            # NOT route it through ``_plain_text`` because that strips
            # the underscores out of markdown-decoration tokens, leaving
            # ``approvetask`` / ``reviewplan`` etc., which then fail to
            # match every branch in ``_perform_dashboard_action``. That
            # was the silent root cause of "buttons record replies but
            # don't drive the underlying task transition" — every
            # custom action fell through to the generic record path.
            kind = str(raw_action.get("kind") or "").strip()
            if not label or not kind:
                continue
            action = dict(raw_action)
            action["label"] = label
            action["kind"] = kind
            actions.append(action)
        if not actions:
            actions = [
                {
                    "label": "Open task",
                    "kind": "open_task",
                    "task_id": fallback_task_id,
                }
            ]
        primary_action = actions[0]
        secondary_action = actions[1] if len(actions) > 1 else {
            "label": "Open task",
            "kind": "open_task",
            "task_id": fallback_task_id,
        }
        return {
            "plain_prompt": summary or "Polly needs your input before this project can continue.",
            "unblock_steps": steps,
            "steps_heading": _plain_text(prompt.get("steps_heading")) or "What to do",
            "decision_question": question or "Choose how Polly should proceed.",
            "primary_label": str(primary_action.get("label") or "Open task"),
            "secondary_label": str(secondary_action.get("label") or "Open task"),
            "primary_action": primary_action,
            "secondary_action": secondary_action,
            "other_placeholder": _plain_text(prompt.get("other_placeholder"))
            or "Tell Polly what to do instead...",
        }

    def _plan_review_decision(
        labels: list[str], body: str, *, fallback_task_id: str | None = None,
    ) -> dict[str, object] | None:
        if "plan_review" not in labels:
            return None
        meta = _extract_plan_review_meta(labels)
        plan_task_id = str(meta.get("plan_task_id") or fallback_task_id or "")
        steps = [
            "Open the plan review surface.",
            "Read the plan and any open decisions.",
            "Approve the plan when it is ready, or discuss changes with the PM.",
        ]
        return {
            "plain_prompt": "A full project plan is ready for your review.",
            "unblock_steps": steps[:5],
            "steps_heading": "What to do",
            "decision_question": (
                "Review the plan and decide whether it is ready to become "
                "implementation tasks."
            ),
            "primary_label": "Review plan",
            "secondary_label": "Open task",
            "primary_action": {
                "label": "Review plan",
                "kind": "review_plan",
                "task_id": fallback_task_id,
                "plan_task_id": plan_task_id,
            },
            "secondary_action": {
                "label": "Open task",
                "kind": "open_task",
                "task_id": plan_task_id or fallback_task_id,
            },
            "other_placeholder": "Reply with plan feedback...",
        }

    def _deployment_decision(body: str, steps: list[str]) -> dict[str, object]:
        lower = body.lower()
        setup_steps: list[str] = []
        if any(token in lower for token in ("fly.io", "fly ", "live fly")):
            setup_steps.append("Set up the Fly.io app for this project.")
        if any(token in lower for token in ("deploy token", "org cred", "credential", "creds", "fly-enabled", "access")):
            setup_steps.append(
                "Give Polly deployment access, including the Fly.io org/app credentials or deploy token."
            )
        if any(token in lower for token in ("postgres", "redis", "database")):
            setup_steps.append("Provision the required Postgres and Redis services.")
        if any(token in lower for token in ("pipeline", "fly deploy", "deploy can run", "live environment")):
            setup_steps.append("Confirm the deployment pipeline can run against the live environment.")
        if any(token in lower for token in ("rollback", "smoke", "/v1/ping", "walkthrough", "clean laptop")):
            setup_steps.append("Make the live app reachable so Polly can run the smoke test and rollback walkthrough.")
        for step in steps:
            clean = _plain_text(step).rstrip(".")
            if not clean:
                continue
            if not clean.lower().startswith(
                (
                    "add ",
                    "create ",
                    "grant ",
                    "provision ",
                    "set up ",
                    "setup ",
                    "provide ",
                    "enable ",
                    "configure ",
                )
            ):
                continue
            normalized = clean.casefold()
            if any(normalized == existing.rstrip(".").casefold() for existing in setup_steps):
                continue
            setup_steps.append(clean + ".")
        deduped_steps: list[str] = []
        seen: set[str] = set()
        for step in setup_steps:
            key = step.casefold()
            if key in seen:
                continue
            seen.add(key)
            deduped_steps.append(step)
        if not deduped_steps:
            deduped_steps = [
                "Set up the deployment environment Polly needs for end-to-end testing."
            ]
        return {
            "plain_prompt": (
                "The first batch of code is done, but Polly cannot fully test it "
                "until the deployment environment exists."
            ),
            "unblock_steps": deduped_steps[:5],
            "steps_heading": "What you need to set up",
            "decision_question": (
                "Do you want to approve the code work now and make deployment "
                "testing a follow-up, or wait until the environment is set?"
            ),
            "primary_label": "Approve it anyway",
            "secondary_label": "Wait until environment is set",
            "primary_response": (
                "Approve it anyway. Treat the code work as accepted now and create "
                "a follow-up task for deployment, smoke testing, and rollback once "
                "the environment is ready."
            ),
            "secondary_response": (
                "Wait until the environment is set. Do not approve this work until "
                "Polly can deploy and test it end to end."
            ),
            "other_placeholder": "Tell Polly what to do instead...",
        }

    def _plain_decision_from_body(
        subject: str, body: str, steps: list[str],
    ) -> dict[str, object]:
        haystack = f"{subject}\n{body}".lower()
        if any(token in haystack for token in ("reachability", "walkthrough")):
            return {
                "plain_prompt": (
                    "The reachability work is ready, but Polly cannot walk through "
                    "it end to end until the backend deployment exists."
                ),
                "unblock_steps": [
                    "Make the backend deployment available to Polly.",
                    "Give Polly any access needed to run the walkthrough.",
                ],
                "steps_heading": "What you need to set up",
                "decision_question": (
                    "Approve the work now with a follow-up walkthrough, or wait "
                    "until the live environment is available?"
                ),
                "primary_label": "Approve it anyway",
                "secondary_label": "Wait for live environment",
                "primary_response": (
                    "Approve it anyway. Accept the current work and create a "
                    "follow-up task for the live walkthrough."
                ),
                "secondary_response": (
                    "Wait for the live environment. Keep this work pending until "
                    "Polly can complete the walkthrough."
                ),
                "other_placeholder": "Tell Polly what to do instead...",
            }
        if any(
            token in haystack
            for token in (
                "fly.io",
                "fly-enabled",
                "deploy token",
                "postgres",
                "redis",
                "deployment",
                "deploy pipeline",
                "rollback",
                "/v1/ping",
            )
        ):
            return _deployment_decision(body, steps)
        prompt = _decision_prompt_from_body(subject, body, steps)
        return {
            "plain_prompt": prompt or "Polly needs your decision before this project can continue.",
            "unblock_steps": steps[:4],
            "steps_heading": "What to do",
            "decision_question": "Choose how Polly should proceed.",
            "primary_label": "Approve it anyway",
            "secondary_label": "Wait",
            "primary_response": "Approve it anyway and keep the project moving.",
            "secondary_response": "Wait. Do not approve this yet.",
            "other_placeholder": "Tell Polly what to do instead...",
        }

    known_projects = set(getattr(config, "projects", {}).keys())
    items: list[dict] = []
    try:
        with SQLiteWorkService(
            db_path=db_path, project_path=project_path,
        ) as svc:
            # #920 — query each project alias and dedupe so projects
            # whose config key (``foo_bar``) and work-DB project name
            # (``foo-bar``) differ still surface their inbox rows.
            aliases = _project_storage_aliases(config, project_key)
            tasks = []
            seen_ids: set[str] = set()
            for alias in aliases:
                for found in inbox_tasks(svc, project=alias):
                    tid = getattr(found, "task_id", None)
                    if not tid or tid in seen_ids:
                        continue
                    seen_ids.add(tid)
                    tasks.append(found)
            for task in tasks:
                entry = annotate_inbox_entry(
                    task_to_inbox_entry(task, db_path=db_path),
                    known_projects=known_projects,
                )
                updated_at = getattr(entry, "updated_at", "") or ""
                if hasattr(updated_at, "isoformat"):
                    updated_at = updated_at.isoformat()
                items.append(
                    {
                        "task_id": entry.task_id,
                        "title": getattr(entry, "title", "") or "(untitled)",
                        "updated_at": updated_at,
                        "sort_value": _sort_value(updated_at),
                        "triage_label": getattr(entry, "triage_label", ""),
                        "triage_rank": int(getattr(entry, "triage_rank", 2) or 2),
                        "needs_action": bool(getattr(entry, "needs_action", False)),
                        "source": "task",
                        "summary": "",
                        "steps": [],
                        "primary_ref": getattr(entry, "task_id", None),
                    }
                )
    except Exception:  # noqa: BLE001
        return 0, [], []

    message_sources: list[tuple[str, Path]] = [(project_key, db_path)]
    workspace_root = getattr(getattr(config, "project", None), "workspace_root", None)
    if workspace_root is not None:
        workspace_db = Path(workspace_root) / ".pollypm" / "state.db"
        if workspace_db.exists() and workspace_db.resolve() != db_path.resolve():
            message_sources.append(("__workspace__", workspace_db))

    seen_messages: set[tuple[str, object]] = set()
    for source_key, source_db in message_sources:
        try:
            store = SQLAlchemyStore(f"sqlite:///{source_db}")
        except Exception:  # noqa: BLE001
            continue
        try:
            try:
                rows = store.query_messages(
                    state="open",
                    type=["notify", "inbox_task", "alert", "event"],
                    limit=250,
                )
            except Exception:  # noqa: BLE001
                rows = []
            for row in rows:
                row_id = row.get("id")
                message_key = (str(source_db), row_id)
                if row_id is None or message_key in seen_messages:
                    continue
                seen_messages.add(message_key)
                if _row_is_dev_channel(row.get("labels")):
                    continue
                payload = row.get("payload") or {}
                if not isinstance(payload, dict):
                    payload = {}
                labels = _labels_from_row(row)
                is_blocker_summary = (
                    payload.get("event_type") == "project_blocker_summary"
                    or row.get("subject") == "project.blocker_summary"
                )
                if row.get("type") == "event" and not is_blocker_summary:
                    continue
                if project_key not in _message_projects(row):
                    continue
                if is_blocker_summary:
                    updated_at = row.get("updated_at") or row.get("created_at") or ""
                    if hasattr(updated_at, "isoformat"):
                        updated_at = updated_at.isoformat()
                    required_actions = payload.get("required_actions") or []
                    if not isinstance(required_actions, list):
                        required_actions = []
                    owner = str(payload.get("owner") or "").strip().lower()
                    reason = str(payload.get("reason") or "").strip()
                    item_id = row.get("id")
                    affected_tasks = payload.get("affected_tasks") or []
                    if not isinstance(affected_tasks, list):
                        affected_tasks = []
                    primary_ref = (
                        payload.get("task_id")
                        or (affected_tasks[0] if affected_tasks else None)
                        or f"blocker-summary:{item_id}"
                    )
                    clean_required_actions = [
                        _plain_text(step)
                        for step in required_actions
                        if _plain_text(step)
                    ][:4]
                    blocker_body = "\n".join([reason, *clean_required_actions])
                    decision = _plain_decision_from_body(
                        "project blocker", blocker_body, clean_required_actions,
                    )
                    user_prompt_decision = _user_prompt_decision(
                        payload.get("user_prompt"),
                        fallback_task_id=(
                            primary_ref
                            if _PROJECT_TASK_REF_RE.fullmatch(str(primary_ref))
                            else None
                        ),
                    )
                    if user_prompt_decision is not None:
                        decision = user_prompt_decision
                    items.append(
                        {
                            "task_id": f"blocker-summary:{item_id}",
                            "title": f"Unblock {project_key}",
                            "updated_at": updated_at,
                            "sort_value": _sort_value(updated_at),
                            "triage_label": "project blocker",
                            "triage_rank": 0,
                        "needs_action": owner in {"user", "sam", "human"},
                        "source": "blocker_summary",
                        "has_user_prompt": user_prompt_decision is not None,
                        "summary": _trim(reason) if reason else "",
                            "steps": clean_required_actions,
                            "next_action": _trim(
                                (
                                    "Complete: "
                                    + "; ".join(clean_required_actions)
                                ),
                                limit=260,
                            ) if clean_required_actions else "",
                            "primary_ref": primary_ref,
                            **decision,
                        }
                    )
                    continue
                entry = annotate_inbox_entry(
                    message_row_to_inbox_entry(
                        row,
                        source_key=source_key,
                        db_path=source_db,
                    ),
                    known_projects=known_projects,
                )
                updated_at = getattr(entry, "updated_at", "") or ""
                if hasattr(updated_at, "isoformat"):
                    updated_at = updated_at.isoformat()
                body = _message_body(row.get("body"))
                subject = getattr(entry, "title", "") or "(no subject)"
                steps = _steps_from_body(body)
                task_refs = [
                    match.group(0)
                    for match in _PROJECT_TASK_REF_RE.finditer(
                        "\n".join((subject, body))
                    )
                    if match.group("project") == project_key
                ]
                plan_meta = _extract_plan_review_meta(labels)
                plan_task_id = str(plan_meta.get("plan_task_id") or "")
                if (
                    plan_task_id
                    and _PROJECT_TASK_REF_RE.fullmatch(plan_task_id)
                    and plan_task_id not in task_refs
                ):
                    task_refs.insert(0, plan_task_id)
                primary_ref = task_refs[0] if task_refs else payload.get("task_id")
                decision = (
                    _user_prompt_decision(
                        payload.get("user_prompt"),
                        fallback_task_id=(
                            str(primary_ref)
                            if _PROJECT_TASK_REF_RE.fullmatch(str(primary_ref or ""))
                            else None
                        ),
                    )
                    or _plan_review_decision(
                        labels,
                        body,
                        fallback_task_id=(
                            str(payload.get("task_id") or "")
                            if payload.get("task_id")
                            else None
                        ),
                    )
                    or _plain_decision_from_body(subject, body, steps)
                )
                items.append(
                    {
                        "task_id": entry.task_id,
                        "title": subject,
                        "updated_at": updated_at,
                        "sort_value": _sort_value(updated_at),
                        "triage_label": getattr(entry, "triage_label", ""),
                        "triage_rank": int(getattr(entry, "triage_rank", 2) or 2),
                        "needs_action": bool(getattr(entry, "needs_action", False)),
                        "source": "message",
                        "has_user_prompt": payload.get("user_prompt") is not None,
                        "is_plan_review": "plan_review" in labels,
                        "summary": _summary_from_body(body),
                        "steps": steps,
                        "next_action": _decision_prompt_from_body(
                            subject, body, steps,
                        ),
                        "primary_ref": primary_ref,
                        **decision,
                    }
                )
        finally:
            try:
                store.close()
            except Exception:  # noqa: BLE001
                pass

    items.sort(
        key=lambda item: (
            int(item.get("triage_rank", 2)),
            0 if item.get("needs_action") else 1,
            -float(item.get("sort_value", 0.0) or 0.0),
            str(item.get("title", "")).lower(),
        )
    )
    top = [
        {
            "task_id": item.get("task_id"),
            "primary_ref": item.get("primary_ref"),
            "title": item.get("title"),
            "updated_at": item.get("updated_at"),
            "triage_label": item.get("triage_label", ""),
            "source": item.get("source", "task"),
        }
        for item in items[:3]
    ]
    action_items: list[dict] = []
    seen_action_refs: set[str] = set()
    for item in items:
        if item.get("source") not in {"message", "blocker_summary"} or not item.get("needs_action"):
            continue
        dedupe_key = str(item.get("primary_ref") or item.get("title") or "")
        if dedupe_key in seen_action_refs:
            continue
        seen_action_refs.add(dedupe_key)
        action_items.append(item)
        if len(action_items) >= 2:
            break
    result = (_action_count(items, action_items), top, action_items)
    _DASHBOARD_INBOX_CACHE[cache_key] = result
    return result


def _format_blocked_dep(
    ref: str,
    title_map: dict[str, str],
    *,
    current_project: str | None = None,
) -> str:
    """Render a blocked-by dependency reference with its task title.

    Returns ``"#6 (Implement N4: notify-api)"`` for in-project deps —
    the rest of the project dashboard already uses ``#N`` form, so
    leaving the raw ``project/N`` here was a jarring jargon
    inconsistency. Cross-project refs keep the full
    ``other_project/6 (Title)`` form so the operator can tell the dep
    is in another project.

    Falls back to the bare ``ref`` when the title is unknown (archived
    task, cross-project ref we don't have visibility into). Titles are
    truncated to keep the multi-dep line from blowing past pane width.
    """
    title = (title_map.get(ref) or "").strip()
    if not title:
        return _escape(ref)
    # ~28 chars keeps three deps + titles fitting at 210-col without
    # forcing a wrap; longer titles get an ellipsis.
    if len(title) > 28:
        title = title[:27].rstrip() + "…"
    if "/" in ref and current_project:
        ref_project, _, ref_number = ref.partition("/")
        if ref_project == current_project and ref_number:
            return f"#{_escape(ref_number)} ({_escape(title)})"
    return f"{_escape(ref)} ({_escape(title)})"


def _action_card_click_hint(action_items: list[dict]) -> str:
    """Return one line of action-card discoverability copy for the Action
    Needed cards.

    With a single card, "this card" is unambiguous; with multiple,
    we pluralise. Cards backed by an inbox thread (no project task
    ref) open the inbox thread instead of a task — call that out so
    the user isn't confused when the click lands somewhere other than
    the task pane. The first clause names the numbered keyboard
    shortcuts for the visible control row so the dashboard does not
    read as mouse-only.
    """
    if not action_items:
        return ""
    visible = action_items[:2]
    key_hint = (
        "Use 1/2/3 for the buttons below"
        if len(visible) == 1
        else "Use 1-3 for the first card and 4-6 for the second"
    )
    refs = [str(item.get("primary_ref") or "") for item in visible]
    has_task = any(_PROJECT_TASK_REF_RE.fullmatch(ref) for ref in refs if ref)
    has_thread = any(not _PROJECT_TASK_REF_RE.fullmatch(ref) for ref in refs)
    if len(visible) == 1:
        target = (
            "the source task"
            if has_task and not has_thread
            else "the inbox thread"
        )
        return f"{key_hint}, or click this card to open {target}."
    if has_task and not has_thread:
        return f"{key_hint}, or click any card to open its source task."
    if has_thread and not has_task:
        return f"{key_hint}, or click any card to open its inbox thread."
    return f"{key_hint}, or click any card to open its source task or inbox thread."


def _dashboard_action_key(index: int, slot: str) -> str:
    offset = {"primary": 1, "secondary": 2, "other": 3}[slot]
    return str(index * 3 + offset)


_ROUTING_TAG_PREFIXES = ("[action]", "[alert]")


def _strip_action_subject_prefix(subject: str) -> str:
    """Drop a leading routing tag (``[Action]``, ``[Alert]``) from a
    user-facing subject.

    These bracketed prefixes are tier/recipient routing labels added by
    the notify CLI and the supervisor's alert path; they have no
    natural-language value for the operator reading the subject. The
    inbox list rail already strips ``[Action]`` for action-bucket rows;
    the detail pane and the activity feed mirror the strip so a focused
    message or feed row doesn't lead with the routing tag.
    """
    if not subject:
        return subject
    lowered = subject.lower()
    for tag in _ROUTING_TAG_PREFIXES:
        if lowered.startswith(tag):
            return subject[len(tag):].lstrip(" :-—")
    return subject


def _clean_hold_reason(
    reason: str,
    title_map: dict[str, str] | None = None,
    *,
    self_task_id: str | None = None,
) -> str:
    """Strip notification-routing artefacts from auto-generated hold
    reasons before rendering them on the dashboard.

    Auto-holds emit reasons of the shape::

        Waiting on operator: [Action] Done: <subject>

    The ``[Action]`` token is a tier/recipient routing tag from the
    notification system, not natural language — when it leaks into a
    hold reason the user reads "[Action]" and has to mentally parse
    the brackets. Strip it; preserve the rest verbatim so attribution
    ("Waiting on operator:") and content ("Done: <subject>") survive.

    When ``title_map`` is supplied, also rewrite raw ``project_key/N``
    task references into ``#N (Title)`` form so the dashboard speaks
    the same task-number language the user already sees in the
    Task pipeline header rows. Operator-pms and architects often write
    hold reasons that name an upstream task by full ref; that form is
    internal jargon for non-technical operators.

    When ``self_task_id`` is supplied AND the rewrite encounters that
    same ref, drop the self-reference entirely. The held task's row
    already shows its own number and title on the line above the hold
    reason — repeating ``Waiting on operator: #12 (Title)`` is
    tautological. After elision, also clean up the now-dangling
    connector (``operator:  —`` becomes ``operator —``).
    """
    if not reason:
        return ""
    text = reason.replace("[Action] ", "").replace("[Action]", "")
    text = _drop_internal_hold_failure_sentences(text)
    elided_self = False
    if title_map:
        def _replace(match: _re.Match[str]) -> str:
            nonlocal elided_self
            ref = match.group(0)
            if self_task_id and ref == self_task_id:
                elided_self = True
                return ""
            title = (title_map.get(ref) or "").strip()
            if not title:
                return ref
            num = ref.split("/", 1)[1]
            if len(title) > 28:
                title = title[:27].rstrip() + "…"
            return f"#{num} ({title})"
        # Match the same shape as ``_PROJECT_TASK_REF_RE`` (case-aware,
        # allows hyphens) so a project keyed ``MyProject`` or ``proj-x``
        # doesn't slip past the rewrite. Cycle 90 alignment.
        text = _re.sub(r"\b[A-Za-z0-9_][A-Za-z0-9_-]*/\d+\b", _replace, text)
        if elided_self:
            # Drop the colon glued to the now-elided self-ref (so
            # "operator:  — text" reads "operator — text") and
            # collapse runs of whitespace the elision left behind.
            text = _re.sub(r":\s+(?=[—\-,.;]\s|$)", " ", text)
            text = _re.sub(r"\s{2,}", " ", text)
    return text.strip()


def _hold_reason_has_internal_failure(reason: str) -> bool:
    lower = (reason or "").lower()
    return (
        "cannot be provisioned" in lower
        or "re-assigns in a loop" in lower
        or ("tmux session" in lower and "provision" in lower)
    )


def _drop_internal_hold_failure_sentences(reason: str) -> str:
    if not reason or not _hold_reason_has_internal_failure(reason):
        return reason
    parts = _re.split(r"(?<=[.!?])\s+", reason)
    kept = [
        part for part in parts
        if part and not _hold_reason_has_internal_failure(part)
    ]
    return " ".join(kept).strip()


def _project_hold_failure_summary(data: object) -> str:
    buckets = getattr(data, "task_buckets", {}) or {}
    for item in buckets.get("on_hold", []) or []:
        reason = str(item.get("hold_reason") or "")
        if not _hold_reason_has_internal_failure(reason):
            continue
        num = item.get("task_number")
        task_label = f"task #{num}" if num is not None else "an on-hold task"
        return f"{task_label} worker pane could not be provisioned; heartbeat is retrying"
    return ""


def _banner_count_after_action_overlap(
    raw_count: int,
    action_items: list[dict],
    bucket: list[dict],
) -> int:
    """Return ``raw_count`` minus tasks already covered by an Action card.

    Generic helper — matches a task-bucket entry's ``task_id`` against
    the action items' ``primary_ref`` and subtracts each overlap.
    Never goes negative. Used by both the review-count and on-hold-
    count overlap reductions in the banner suffix so the same task
    isn't double-counted across the action lede and the category
    tail.
    """
    if raw_count <= 0 or not action_items:
        return max(0, raw_count)
    bucket_ids = {
        str(item.get("task_id") or "")
        for item in bucket
        if item.get("task_id")
    }
    if not bucket_ids:
        return raw_count
    covered = sum(
        1
        for item in action_items
        if str(item.get("primary_ref") or "") in bucket_ids
    )
    return max(0, raw_count - covered)


def _banner_review_count_after_action_overlap(
    raw_review_count: int,
    action_items: list[dict],
    review_bucket: list[dict],
) -> int:
    """Return the review count after subtracting tasks already
    represented by an Action Needed card.

    The banner suffix shows ``N approval(s)`` to flag review-state
    tasks. When one of those tasks is the exact subject of an action
    card (its ``primary_ref`` matches a task in the review bucket),
    counting it again as a separate "approval" double-states the
    same work. Drop overlaps; never go negative.

    Thin wrapper over the generic ``_banner_count_after_action_overlap``
    — kept as a named alias so existing callers + tests stay readable.
    """
    return _banner_count_after_action_overlap(
        raw_review_count, action_items, review_bucket,
    )


def _stuck_alert_covers_action(
    alert_type: str, covered_task_ids: set[str],
) -> bool:
    """Return True when ``alert_type`` is ``stuck_on_task:<task>`` and the
    task is already represented by an Action Needed card.

    Stuck alerts fire mechanically when a session sits idle waiting on
    user input. If the dashboard is *already* showing a user_prompt
    card for that same task, counting the alert separately just inflates
    the banner number without telling the user anything new.
    """
    prefix = "stuck_on_task:"
    if not alert_type or not alert_type.startswith(prefix):
        return False
    task_id = alert_type[len(prefix):].strip()
    return bool(task_id) and task_id in covered_task_ids


def _action_count(items: list[dict], action_items: list[dict]) -> int:
    """Count distinct user actions across task and message inbox sources.

    A message and a review-stage task that share the same ``primary_ref``
    represent the same conceptual action — count once. Without this
    dedupe the banner reports "N need action" while only one card is
    rendered, and the user has no way to discover what the missing
    item is.
    """
    action_refs = {
        str(item.get("primary_ref"))
        for item in action_items
        if item.get("primary_ref")
    }
    task_action_count = sum(
        1
        for item in items
        if item.get("source") == "task"
        and item.get("needs_action")
        and str(item.get("primary_ref") or "") not in action_refs
    )
    return task_action_count + len(action_items)


# Cache the activity-feed projection by (project, db_mtime, limit).
# Every per-project dashboard refresh used to rebuild the projector
# and walk the messages table to assemble feed entries — pure
# repeated work when no event has landed since the last call.
# Sister to ``_DASHBOARD_INBOX_CACHE`` (cycle 138) and
# ``_PLAN_STALENESS_CACHE`` (cycle 133).
_DASHBOARD_ACTIVITY_CACHE: dict[
    tuple[str, float | None, int], list[dict]
] = {}


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
    # Content-addressed cache: an unchanged db_mtime means no event
    # has landed since the last call so the projection is unchanged.
    project = (config.projects or {}).get(project_key)
    db_mtime: float | None = None
    if project is not None and getattr(project, "path", None) is not None:
        db_path = project.path / ".pollypm" / "state.db"
        try:
            db_mtime = db_path.stat().st_mtime
        except OSError:
            db_mtime = None
    cache_key = (project_key, db_mtime, limit)
    cached = _DASHBOARD_ACTIVITY_CACHE.get(cache_key)
    if cached is not None:
        return cached
    projector = build_projector(config)
    if projector is None:
        return []
    # #920 — pass every alias so projects whose config key (e.g.
    # ``blackjack_trainer``) differs from the work-DB project name
    # (``blackjack-trainer``) still surface their activity rows.
    aliases = _project_storage_aliases(config, project_key)
    try:
        entries = projector.project(projects=aliases, limit=limit)
    except Exception:  # noqa: BLE001
        return []
    result = [
        {
            "timestamp": e.timestamp,
            "actor": e.actor or "",
            "verb": e.verb or "",
            "summary": e.summary or "",
            "kind": e.kind or "",
        }
        for e in entries
    ]
    _DASHBOARD_ACTIVITY_CACHE[cache_key] = result
    return result


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
        counts, buckets = _dashboard_gather_tasks(config, project_key, project_path)
        inbox_count, inbox_top, action_items = _dashboard_inbox(
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
        action_items = []
        plan_path = None
        plan_sections = []
        plan_explainer = None
        plan_text = None
        plan_aux_files = []
        plan_mtime = None
        plan_stale_reason = None
        activity_entries = []

    active_worker, alert_count = _dashboard_active_worker(
        config_path, project_key, action_items=action_items,
    )
    blocker_count = int(counts.get("blocked", 0))
    on_hold_count = int(counts.get("on_hold", 0))
    in_progress_count = int(counts.get("in_progress", 0))

    status_dot, status_color, status_label = _dashboard_status(
        active_worker,
        inbox_count,
        alert_count,
        None,
        blocker_count=blocker_count,
        on_hold_count=on_hold_count,
        in_progress_count=in_progress_count,
    )

    # Resolve effective enforce_plan with the same precedence the rail
    # rollup, sweeper, and task-list use: per-project override wins,
    # else fall back to the global ``[planner].enforce_plan``. The Plan
    # section copy reads this so it doesn't nudge the user to draft a
    # plan for a project they've explicitly bypassed (Sam, media on
    # 2026-04-26 still saw the "Press c to ask the PM to plan it now"
    # nudge after shipping ``enforce_plan = false``).
    planner_settings = getattr(config, "planner", None)
    global_enforce = bool(getattr(planner_settings, "enforce_plan", True))
    project_enforce = getattr(project, "enforce_plan", None)
    enforce_plan = (
        project_enforce if project_enforce is not None else global_enforce
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
        action_items=action_items,
        alert_count=alert_count,
        enforce_plan=enforce_plan,
    )


def _project_dashboard_signature(
    data: "ProjectDashboardData | None",
) -> tuple:
    """Cheap signature over the user-visible state of a project dashboard.

    Used by ``PollyProjectDashboardApp._refresh`` to skip the full
    Textual re-paint when nothing has structurally changed since the
    last tick. Captures every field whose change the user would
    notice; deliberately excludes time-relative labels (those are
    refreshed by a force-tick every Nth refresh).
    """
    if data is None:
        return ("none",)
    worker = data.active_worker or {}
    return (
        data.project_key,
        data.exists_on_disk,
        data.status_dot,
        data.status_label,
        # Worker liveness — heartbeat changes invalidate the signature
        # on purpose so the worker section stays current.
        worker.get("session_name"),
        worker.get("role"),
        worker.get("last_heartbeat"),
        # Task pipeline.
        tuple(sorted((data.task_counts or {}).items())),
        # Action / inbox / alerts.
        data.inbox_count,
        data.alert_count,
        len(data.action_items or []),
        tuple(
            (str(item.get("task_id") or ""), str(item.get("primary_ref") or ""))
            for item in (data.action_items or [])[:5]
        ),
        # Plan section.
        str(data.plan_path) if data.plan_path else None,
        data.plan_mtime,
        data.plan_stale_reason,
        # Activity tail (newest entries' identity, not their age).
        tuple(
            (e.get("event_type"), e.get("created_at"))
            for e in (data.activity_entries or [])[:10]
        ),
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
    #proj-action-bar {
        margin-top: 1;
        padding: 0 1;
        background: #16202a;
        color: #6b7a88;
        border: round #243241;
    }
    #proj-action-bar.-attention {
        background: #3a2c08;
        color: #f7d67a;
        border: round #7a5a14;
    }
    #proj-action-bar.-critical {
        background: #3a1719;
        color: #ffd7d9;
        border: round #8d3137;
    }
    #proj-action-bar.-hover {
        border: round #5b8aff;
    }
    #proj-inbox-section.-hover {
        border: round #5b8aff;
    }
    .proj-section.-hover {
        border: round #5b8aff;
    }
    .proj-action-controls {
        height: auto;
        margin-top: 1;
    }
    .proj-action-controls Button {
        margin-right: 1;
        /* Compact action buttons (#878). Default Textual Button takes
           3 rows (top edge / label / bottom edge); on a 65-row laptop
           that pushes inbox / current activity / pipeline below the
           fold for even a single Action Needed card. ``height: 1`` +
           ``border: none`` keeps the label without the decorative
           ▔▔/▁▁ frames that earned no information. */
        height: 1;
        min-height: 1;
        border: none;
        padding: 0 1;
    }
    .proj-action-other {
        width: 1fr;
    }
    .proj-action-controls.-hidden {
        display: none;
    }
    .proj-action-group {
        height: auto;
        margin-bottom: 1;
    }
    .proj-action-group.-hidden {
        display: none;
    }
    .proj-inbox-lead {
        height: auto;
    }
    .proj-inbox-lead.-hidden {
        display: none;
    }
    .proj-action-card {
        height: auto;
    }
    #proj-body {
        height: 1fr;
        padding: 1 0 0 0;
        scrollbar-size: 1 1;
        scrollbar-color: #2a3340;
    }
    .proj-section {
        height: auto;
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
        height: auto;
        min-height: 1;
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
        Binding("1", "action_card_1", "First card primary action"),
        Binding("2", "action_card_2", "First card secondary action"),
        Binding("3", "action_card_3", "First card reply"),
        Binding("4", "action_card_4", "Second card primary action"),
        Binding("5", "action_card_5", "Second card secondary action"),
        Binding("6", "action_card_6", "Second card reply"),
        Binding("v", "open_explainer", "Explainer", show=False),
        Binding("o", "open_editor", "Editor", show=False),
        Binding("j", "plan_scroll_down", "Scroll down", show=False),
        Binding("k", "plan_scroll_up", "Scroll up", show=False),
        Binding("g", "plan_scroll_top", "Top", show=False),
        Binding("G", "plan_scroll_bottom", "Bottom", show=False),
        Binding("u,r", "refresh", "Refresh", show=False),
        Binding("a", "view_alerts", "Alerts", show=False),
        Binding("ctrl+k,colon", "open_command_palette", "Palette", priority=True),
        Binding("question_mark", "show_keyboard_help", "Help", priority=True),
        Binding("q,escape", "back", "Back"),
    ]

    def action_open_command_palette(self) -> None:
        _open_command_palette(self)

    def action_show_keyboard_help(self) -> None:
        _open_keyboard_help(self)

    _DEFAULT_HINT = (
        "c chat \u00b7 p plan \u00b7 i inbox \u00b7 l log \u00b7 q home"
    )
    _ACTION_HINT = (
        "1 primary \u00b7 2 secondary \u00b7 3 reply \u00b7 c chat "
        "\u00b7 i inbox \u00b7 q home"
    )
    # Two-card variant: per-card 1-3/4-6 instead of singular 1-3.
    # The screen footer needs to match the live bindings so the user
    # isn't told ``2 secondary`` when 2 actually picks the second
    # action card's primary button.
    _ACTION_HINT_TWO_CARDS = (
        "1-3 first card \u00b7 4-6 second card \u00b7 c chat "
        "\u00b7 i inbox \u00b7 q home"
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
        self.action_bar = Static("", id="proj-action-bar", markup=True)
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
        # Lead Static — renders the "To move this project forward"
        # header that introduces the action cards. Hidden when there
        # are no action items.
        self.inbox_lead = Static(
            "", classes="proj-section-body proj-inbox-lead -hidden",
            markup=True,
        )
        # Per-action-card Statics + their wrapping Vertical groups.
        # Each group contains the card body Static and its
        # corresponding response controls row, so the controls render
        # directly under their own card instead of stacking at the
        # bottom of the inbox section. (Issue #2 — was: both control
        # rows mounted after the single inbox_body Static, both ended
        # up under whichever card rendered last.)
        self.action_card_bodies: list[Static] = []
        self.action_card_groups: list[Vertical] = []
        self.inbox_body = Static(
            "", classes="proj-section-body", markup=True,
        )
        self.action_control_rows: list[Horizontal] = []
        self.action_primary_buttons: list[Button] = []
        self.action_secondary_buttons: list[Button] = []
        self.action_other_inputs: list[Input] = []
        for idx in range(2):
            primary = Button(
                "Approve it anyway",
                id=f"proj-action-{idx}-primary",
                variant="success",
                classes="proj-action-control",
            )
            secondary = Button(
                "Wait until environment is set",
                id=f"proj-action-{idx}-secondary",
                variant="warning",
                classes="proj-action-control",
            )
            other = Input(
                placeholder="Tell Polly what to do instead...",
                id=f"proj-action-{idx}-other",
                classes="proj-action-other",
            )
            row = Horizontal(
                primary,
                secondary,
                other,
                id=f"proj-action-{idx}-row",
                classes="proj-action-controls -hidden",
            )
            card_body = Static(
                "",
                id=f"proj-action-{idx}-card",
                classes="proj-section-body proj-action-card",
                markup=True,
            )
            group = Vertical(
                card_body,
                row,
                id=f"proj-action-{idx}-group",
                classes="proj-action-group -hidden",
            )
            self.action_control_rows.append(row)
            self.action_primary_buttons.append(primary)
            self.action_secondary_buttons.append(secondary)
            self.action_other_inputs.append(other)
            self.action_card_bodies.append(card_body)
            self.action_card_groups.append(group)
        self.hint = Static(
            self._DEFAULT_HINT, id="proj-hint", markup=True,
        )
        self.data: ProjectDashboardData | None = None
        self._action_click_task_ids: list[str] = []
        self._action_control_task_ids: list[str | None] = [None, None]
        # When True, the plan section takes over the whole body — other
        # sections are hidden via the ``proj-plan-focus`` screen class.
        self._plan_view_mode: bool = False
        # Cycle 135: signature of last rendered ProjectDashboardData so
        # the 10s refresh tick can skip the (relatively expensive) full
        # re-paint when nothing the user can see has changed. Mirrors
        # the inbox loader's #752 pattern. Force a re-render every Nth
        # tick so age-based labels stay current.
        self._last_render_signature: tuple | None = None
        self._ticks_since_force_render: int = 0
        self._FORCE_RENDER_EVERY_N_TICKS = 6  # ~60s at 10s tick

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="proj-outer"):
            yield self.topbar
            yield self.status_line
            yield self.action_bar
            with VerticalScroll(id="proj-body"):
                with Vertical(classes="proj-section", id="proj-inbox-section"):
                    yield self.inbox_title
                    yield self.inbox_lead
                    # Each action card sits in its own Vertical group
                    # together with its response controls so the
                    # buttons render directly under their card. The
                    # trailing inbox_body Static below holds everything
                    # else (click hint, count line, previews, "press i").
                    for group in self.action_card_groups:
                        yield group
                    yield self.inbox_body
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
        yield self.hint

    def on_mount(self) -> None:
        # Paint a "Loading…" placeholder immediately so the click-to-
        # visible-pane delay is just the Python + Textual cold-boot
        # cost, not an additional 1–2s of synchronous DB walks. The
        # actual gather runs on a worker thread (mirrors the workspace
        # dashboard's ``_refresh_dashboard_sync`` pattern) and the
        # first ``_render`` fires when it completes.
        self.topbar.update(
            f"[b]{_escape(self.project_key)}[/b]   "
            f"[dim]loading project dashboard…[/dim]"
        )
        self._first_refresh_running = False
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
        # First refresh runs off the UI thread so the placeholder topbar
        # stays visible until data lands. Subsequent timer-driven
        # refreshes are cheap (cycle 138/139 caches collapse them to
        # stat()s when nothing has changed), so we keep them on the
        # UI thread to avoid worker-thread overhead.
        if getattr(self, "_first_refresh_running", False):
            # Worker is still gathering — avoid a parallel sync gather
            # that would race the worker's call_from_thread completion.
            return
        if self.data is None:
            self._first_refresh_running = True
            self.run_worker(
                self._first_refresh_sync,
                thread=True,
                exclusive=True,
                group="proj_dashboard_first_refresh",
            )
            return
        try:
            self.data = _gather_project_dashboard(
                self.config_path, self.project_key,
            )
        except Exception as exc:  # noqa: BLE001
            self.topbar.update(
                f"[#ff5f6d]Error loading project:[/#ff5f6d] {_escape(str(exc))}"
            )
            return
        # Skip the full re-paint when nothing the user can see has
        # changed. Force-refresh every Nth tick so "5m ago" age labels
        # don't freeze. Mirrors the inbox loader's #752 signature
        # pattern, applied to the 10s project-dashboard refresh.
        self._ticks_since_force_render += 1
        if self._ticks_since_force_render >= self._FORCE_RENDER_EVERY_N_TICKS:
            self._ticks_since_force_render = 0
            self._last_render_signature = None  # force a re-render below
        signature = _project_dashboard_signature(self.data)
        if signature == self._last_render_signature:
            return
        self._last_render_signature = signature
        self._render()

    def _first_refresh_sync(self) -> None:
        """Off-thread first-refresh: gather then hand back to the UI
        thread for render. Keeps the placeholder topbar visible
        instead of freezing the cockpit pane during cold-boot data
        load."""
        try:
            data = _gather_project_dashboard(
                self.config_path, self.project_key,
            )
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self._first_refresh_failed, str(exc))
            return
        self.call_from_thread(self._first_refresh_completed, data)

    def _first_refresh_completed(self, data) -> None:
        self._first_refresh_running = False
        self.data = data
        self._last_render_signature = _project_dashboard_signature(data)
        self._render()

    def _first_refresh_failed(self, error: str) -> None:
        self._first_refresh_running = False
        self.topbar.update(
            f"[#ff5f6d]Error loading project:[/#ff5f6d] {_escape(error)}"
        )

    def _render(self) -> None:
        data = self.data
        if data is None:
            # Friendlier text + matching 3-space gap so the error
            # case doesn't look subtly different from the regular
            # ``<Project>   PM: <label>`` topbar.
            self.topbar.update(
                f"[b]{_escape(self.project_key)}[/b]   "
                f"[dim]is not a tracked project — "
                f"check ``pm projects`` for the list.[/dim]"
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
        self._update_action_bar(data)

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
        self.inbox_title.update(
            "[b]Action Needed[/b]" if data.action_items else "[b]Inbox[/b]"
        )
        # Update the consolidated string view first so the side
        # effect (``self._action_click_task_ids``) is set before we
        # split the rendered text across the per-card widgets.
        full_inbox_text = self._render_inbox_body(data)
        if data.action_items:
            # Split the consolidated inbox markup into:
            #   • a lead line ("To move this project forward")
            #   • one card body per visible action item (interleaved
            #     with their response controls in the compose tree)
            #   • a remainder block (click hint + count line + previews
            #     + "press i") that lives in inbox_body, mounted below
            #     the last action group.
            self.inbox_lead.update(
                "[#f85149][b]To move this project forward[/b][/]"
            )
            self.inbox_lead.remove_class("-hidden")
            visible = data.action_items[:2]
            compact_cards = len(visible) > 1
            for idx, group in enumerate(self.action_card_groups):
                if idx < len(visible):
                    self.action_card_bodies[idx].update(
                        self._render_action_card_body(
                            visible[idx], compact=compact_cards,
                        )
                    )
                    group.remove_class("-hidden")
                else:
                    self.action_card_bodies[idx].update("")
                    group.add_class("-hidden")
            self.inbox_body.update(self._render_inbox_remainder(data))
        else:
            # No action cards — hide the lead and all groups, render
            # the entire inbox body as before so blocker / on-hold /
            # preview content still shows.
            self.inbox_lead.update("")
            self.inbox_lead.add_class("-hidden")
            for idx, group in enumerate(self.action_card_groups):
                self.action_card_bodies[idx].update("")
                group.add_class("-hidden")
            self.inbox_body.update(full_inbox_text)
        self._sync_action_controls(data)

        if data.action_items:
            visible_action_count = len(data.action_items[:2])
            hint = (
                self._ACTION_HINT_TWO_CARDS
                if visible_action_count > 1
                else self._ACTION_HINT
            )
        else:
            hint = self._DEFAULT_HINT
        self.hint.update(hint)

    def _update_action_bar(self, data: ProjectDashboardData) -> None:
        review_count = int(data.task_counts.get("review", 0))
        blocker_count = int(data.task_counts.get("blocked", 0))
        on_hold_count = int(data.task_counts.get("on_hold", 0))
        counts = render_project_action_bar(
            review_count=review_count,
            alert_count=data.alert_count,
            inbox_count=data.inbox_count,
            blocker_count=blocker_count,
            on_hold_count=on_hold_count,
        )
        summary = self._render_project_state_banner(data, counts)
        self.action_bar.remove_class("-attention")
        self.action_bar.remove_class("-critical")
        if data.alert_count or _project_hold_failure_summary(data):
            self.action_bar.add_class("-critical")
        elif data.action_items or review_count or data.inbox_count or on_hold_count:
            self.action_bar.add_class("-attention")
        self.action_bar.update(f"[b]{_escape(summary)}[/b]")

    def _render_project_state_banner(
        self, data: ProjectDashboardData, counts: str,
    ) -> str:
        internal_failure = _project_hold_failure_summary(data)
        if internal_failure:
            return f"Needs repair: {internal_failure}"
        count_suffix = f" · {counts[2:]}" if counts.startswith("▸ ") else f" · {counts}"
        if data.action_items:
            item = data.action_items[0]
            prompt = str(
                item.get("plain_prompt")
                or item.get("decision_question")
                or "This project needs your input."
            ).strip()
            # The banner already leads with "Waiting on you:" — the
            # tail count "N need action" is redundant noise on top of
            # that lede *and* the rendered Action Needed cards. Drop
            # it specifically while keeping the genuinely-different
            # categories (dependencies, on hold, approvals, alerts).
            #
            # When an action card's primary_ref points at a review
            # task, the "N approval(s)" suffix double-counts the same
            # work — booktalk read "Waiting on you: A full project
            # plan is ready for your review · 1 on hold · 1 approval"
            # where the "1 approval" was the very task the prompt
            # already named. Drop the overlap before formatting.
            review_count = _banner_review_count_after_action_overlap(
                int(data.task_counts.get("review", 0)),
                data.action_items,
                data.task_buckets.get("review", []),
            )
            # Same overlap reduction for on_hold — polly_remote (live,
            # 2026-04-26) had 2 action cards whose primary_refs were
            # the same 2 on_hold tasks, but the banner suffix still
            # read ``· 2 on hold`` so the user couldn't tell whether
            # there were 4 things waiting (2 cards + 2 on_hold) or
            # 2 things double-named.
            on_hold_count = _banner_count_after_action_overlap(
                int(data.task_counts.get("on_hold", 0)),
                data.action_items,
                data.task_buckets.get("on_hold", []),
            )
            action_only_suffix = render_project_action_bar(
                review_count=review_count,
                alert_count=data.alert_count,
                inbox_count=0,
                blocker_count=int(data.task_counts.get("blocked", 0)),
                on_hold_count=on_hold_count,
            )
            # When more than one user-facing action is waiting, the
            # banner prompt only shows the first; surface the rest as
            # a "+N more action(s)" tag so the user doesn't read the
            # banner, act on the first item, and miss the others.
            extras = max(0, int(data.inbox_count) - 1)
            extras_part = (
                f" · +{extras} more action"
                + ("s" if extras != 1 else "")
                if extras
                else ""
            )
            if action_only_suffix.startswith("▸ Clear"):
                # No other categories to mention — drop the suffix entirely
                # so the banner stays a single clean sentence.
                return f"Waiting on you: {prompt}{extras_part}"
            suffix = (
                action_only_suffix[2:]
                if action_only_suffix.startswith("▸ ")
                else action_only_suffix
            )
            return f"Waiting on you: {prompt}{extras_part} · {suffix}"
        if data.alert_count:
            return f"Alert: Polly needs to inspect a project issue{count_suffix}"
        # On-hold tasks must outrank an active background worker. Today
        # media renders ``Moving now: worker_media is active · 1 on
        # hold`` while the pill correctly shows "needs attention" —
        # the banner contradicts the pill and buries the user-facing
        # signal as a tail count. The pill priority in
        # ``_dashboard_status`` already counts on_hold as a needs-
        # attention state; mirror that here. (Sam, media on
        # 2026-04-26: on_hold reason "Awaiting user Phase A approval"
        # was invisible behind a "Moving now" banner.)
        on_hold_count = int(data.task_counts.get("on_hold", 0))
        if on_hold_count:
            label = "task is" if on_hold_count == 1 else "tasks are"
            lead = f"Paused: {on_hold_count} {label} on hold"
            # Drop the redundant ``N on hold`` from the suffix — same
            # trick the action_items branch above uses for inbox/review
            # overlap. Without this, the banner read "Paused: 1 task
            # is on hold · 1 on hold".
            suffix_without_overlap = render_project_action_bar(
                review_count=int(data.task_counts.get("review", 0)),
                alert_count=data.alert_count,
                inbox_count=0,
                blocker_count=int(data.task_counts.get("blocked", 0)),
                on_hold_count=0,
            )
            if data.active_worker is not None:
                worker = data.active_worker
                role = str(worker.get("role") or "worker")
                session = str(worker.get("session_name") or "a session")
                lead += f" · {session} ({role}) active in background"
            if suffix_without_overlap.startswith("▸ Clear"):
                return lead
            tail = (
                suffix_without_overlap[2:]
                if suffix_without_overlap.startswith("▸ ")
                else suffix_without_overlap
            )
            return f"{lead} · {tail}"
        if data.active_worker is not None:
            worker = data.active_worker
            role = str(worker.get("role") or "worker")
            session = str(worker.get("session_name") or "a session")
            return f"Moving now: {session} ({role}) is active{count_suffix}"
        if blocker_count := int(data.task_counts.get("blocked", 0)):
            label = "task is" if blocker_count == 1 else "tasks are"
            return (
                f"Waiting on dependencies: {blocker_count} {label} blocked, "
                f"but no user action is currently requested{count_suffix}"
            )
        if review_count := int(data.task_counts.get("review", 0)):
            label = "task" if review_count == 1 else "tasks"
            return f"Waiting for review: {review_count} {label} ready for approval{count_suffix}"
        queued_count = int(data.task_counts.get("queued", 0))
        if queued_count:
            label = "task" if queued_count == 1 else "tasks"
            return f"Queued: {queued_count} {label} waiting for a worker{count_suffix}"
        return "Clear: no active work, alerts, approvals, or user actions"

    # ------------------------------------------------------------------
    # Section renderers — all return Rich-markup strings, all handle
    # missing-data gracefully with friendly empty-state copy.
    # ------------------------------------------------------------------

    def _render_now_body(self, data: ProjectDashboardData) -> str:
        w = data.active_worker
        if w:
            sess_raw = w.get("session_name") or ""
            role_raw = w.get("role") or "worker"
            # Collapse "<role>_<project_key>" sessions on their own
            # project's dashboard down to just the role \u2014 both the
            # role name and the project context are already implicit
            # (we're on that project's dashboard), so rendering
            # ``architect_polly_remote  architect`` repeats info the
            # operator already has. Leave any session_name with extra
            # information (task-N, workerN, ad-hoc names) unchanged.
            if sess_raw in {role_raw, f"{role_raw}_{self.project_key}"}:
                identity_markup = f"[b]{_escape(role_raw)}[/b]"
            else:
                identity_markup = (
                    f"[b]{_escape(sess_raw)}[/b]  "
                    f"[dim]{_escape(role_raw)}[/dim]"
                )
            hb = w.get("last_heartbeat") or ""
            age = _format_relative_age(hb) if hb else ""
            age_part = f"  [dim]{_escape(age)}[/dim]" if age else ""
            lines = [
                f"[#3ddc84]\u25cf[/#3ddc84] "
                f"{identity_markup}{age_part}",
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
            elif data.action_items:
                # No task in flight but the user has decisions waiting:
                # the operator-facing reality is "I have something to do
                # here." Saying just "<architect> active" while the
                # banner reads "Waiting on you" hides that fact in the
                # very section meant to explain what's happening now.
                # Don't restate the full prompt \u2014 it's already in the
                # Action Needed card right above; point there instead.
                lines.append(
                    "  [#f0c45a]\u25c6[/#f0c45a] Waiting on your "
                    "response \u2014 see [b]Action Needed[/b] above."
                )
            return "\n".join(lines)
        if data.action_items:
            item = data.action_items[0]
            prompt = _escape(
                item.get("plain_prompt")
                or item.get("decision_question")
                or "This project is waiting for your response."
            )
            return (
                "[#f0c45a]\u25c6[/#f0c45a] Waiting for your response.\n"
                f"  {prompt}"
            )
        # #920 — a task can be in_progress (claimed by a worker) even
        # when no heartbeat is alive yet (the worker just claimed and
        # has not registered yet, or the session is being launched).
        # Surface that fact instead of falling through to "Idle".
        in_progress = data.task_buckets.get("in_progress", [])
        if in_progress:
            first = in_progress[0]
            num = first.get("task_number")
            num_part = f"#{num} " if num is not None else ""
            title = _escape(first.get("title") or "")
            assignee = _escape(str(first.get("assignee") or "worker"))
            return (
                "[#f0c45a]◆[/#f0c45a] "
                f"[b]{assignee}[/b] is working on a task.\n"
                f"  In flight: {num_part}{title}"
            )
        queued = data.task_buckets.get("queued", [])
        if queued:
            first = queued[0]
            num = first.get("task_number")
            num_part = f"#{num} " if num is not None else ""
            title = _escape(first.get("title") or "")
            return (
                "[dim]No worker active right now.[/dim]\n"
                f"  Next queued task: {num_part}{title}"
            )
        if data.task_counts.get("blocked", 0):
            return (
                "[dim]No worker active right now.[/dim]\n"
                "  Work is waiting on upstream dependencies. No user action is requested here."
            )
        if data.task_counts.get("review", 0):
            return (
                "[dim]No worker active right now.[/dim]\n"
                "  Work is waiting for review/approval."
            )
        if data.task_counts.get("on_hold", 0):
            return (
                "[dim]No worker active right now.[/dim]\n"
                "  Work is on hold — see Task pipeline for the hold reason."
            )
        return "[dim]Idle. No tasks in flight and no user action needed.[/dim]"

    def _render_pipeline_body(self, data: ProjectDashboardData) -> str:
        if not data.exists_on_disk:
            return "[dim]No project path on disk.[/dim]"
        counts = data.task_counts
        buckets = data.task_buckets
        if not counts and not any(buckets.values()):
            return "[dim]No tasks yet. Press N on the rail to start a lane.[/dim]"

        # Compact count strip. ``in_progress`` and ``blocked`` previously
        # shared the \u25c6 glyph; the colour distinguishes them but the
        # shape didn't, which made the pipeline strip ambiguous in
        # snapshots, screenshots, and any low-colour terminal. Use \u25a3
        # (squared inner square) for blocked so the "waiting on
        # dependencies" rows look distinct from the "in flight" rows
        # at a glance.
        strip_order = [
            ("queued", "#6b7a88", "\u25cb"),
            ("in_progress", "#f0c45a", "\u25c6"),
            ("review", "#5b8aff", "\u25c9"),
            ("blocked", "#f85149", "\u25a3"),
            ("on_hold", "#f0c45a", "\u23f8"),
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

        # Build a task_id \u2192 title map across every visible bucket so
        # the "waiting on:" line for blocked tasks can name what each
        # upstream dep actually *is*. Without this the user reads
        # "waiting on: polly_remote/6, polly_remote/9" and has no
        # signal about whether to wait, escalate, or grab one of the
        # deps themselves.
        title_map: dict[str, str] = {}
        for _bucket_items in buckets.values():
            for entry in _bucket_items:
                tid = str(entry.get("task_id") or "")
                if tid:
                    title_map[tid] = str(entry.get("title") or "")

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
                # For blocked tasks, name the dependencies the task is
                # waiting on. Without this the operator sees a list of
                # blocked titles with no signal about *why* — they have
                # to drill into each task to find the upstream work.
                if status == "blocked":
                    blocked_by = [
                        str(ref) for ref in (t.get("blocked_by") or []) if ref
                    ]
                    if blocked_by:
                        joined = ", ".join(
                            _format_blocked_dep(
                                ref, title_map,
                                current_project=self.project_key,
                            )
                            for ref in blocked_by[:3]
                        )
                        if len(blocked_by) > 3:
                            joined += f" (+{len(blocked_by) - 3} more)"
                        out.append(f"      [dim]waiting on: {joined}[/dim]")
                # Symmetric surface for on_hold tasks: print the hold
                # reason recorded with ``pm task hold --reason``. Without
                # this the operator sees "Paused" with no signal about
                # *why* the work is parked or what would unparked it.
                if status == "on_hold":
                    reason = _clean_hold_reason(
                        str(t.get("hold_reason") or ""),
                        title_map,
                        self_task_id=str(t.get("task_id") or "") or None,
                    )
                    if reason:
                        out.append(f"      [dim]paused: {_escape(reason)}[/dim]")
                # In-progress rows tell the operator which worker is
                # carrying the task and which node they're at right now.
                # Without this signal, the dashboard says "1 in
                # progress" but doesn't tell Sam who to message if he
                # has a question — the assignee is already on the
                # bucket dict, just unsurfaced.
                if status == "in_progress":
                    assignee = str(t.get("assignee") or "").strip()
                    node_id = str(t.get("current_node_id") or "").strip()
                    if assignee:
                        node_part = f" @ {_escape(node_id)}" if node_id else ""
                        out.append(
                            f"      [dim]{_escape(assignee)}{node_part}[/dim]"
                        )
                # Review rows tell the operator who has the ball:
                # auto-reviewer (Russell etc.) or a user-approval
                # node that needs Sam's call. Without this, the user
                # sees "1 task in review" and can't tell whether to
                # wait or act.
                if status == "review":
                    node_id = str(t.get("current_node_id") or "").lower()
                    is_user_review = any(
                        marker in node_id for marker in ("human", "user")
                    )
                    if is_user_review:
                        out.append(
                            "      [#f0c45a]ready for your approval[/]"
                        )
                    else:
                        assignee = str(t.get("assignee") or "").strip()
                        node_part = f" @ {_escape(node_id)}" if node_id else ""
                        if assignee:
                            out.append(
                                f"      [dim]reviewing: "
                                f"{_escape(assignee)}{node_part}[/dim]"
                            )
                        elif node_id:
                            out.append(f"      [dim]@ {_escape(node_id)}[/dim]")
            out.append("")
        # Drop trailing blank for tidy spacing
        while out and out[-1] == "":
            out.pop()
        return "\n".join(out)

    def _render_plan_body(self, data: ProjectDashboardData) -> str:
        if not data.exists_on_disk:
            return "[dim]Virtual project — no plan on disk.[/dim]"
        if data.plan_path is None:
            # Per-project ``[planner].enforce_plan = false`` opts the
            # project out of the planning ceremony entirely (one-off
            # cleanups, single-task scopes). The default "ask the PM to
            # plan it now" nudge contradicts that choice — surface the
            # bypass explicitly instead.
            if not data.enforce_plan:
                return (
                    "[dim]Plan not required — this project is configured "
                    "with [b]\\[planner].enforce_plan = false[/b].\n"
                    "Workers can pick up tasks directly without a plan "
                    "ceremony.[/dim]"
                )
            return (
                "[dim]No plan yet — the PM will draft one when this "
                "project picks up work.\n"
                "Press [b]c[/b] in this pane to chat with the PM and "
                "ask for a plan now.[/dim]"
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
            verb = e.get("verb") or ""
            summary = self._sanitize_activity_summary(e.get("summary") or "")
            kind = e.get("kind") or ""
            ts_part = f"[dim]{ts:>8}[/dim]" if ts else ""
            # Task-transition rows already encode "from → to" in their
            # summary ("task X: review → done"), so the verb prefix
            # ("review->done") just restates the transition. Drop the
            # verb when it would duplicate; keep it when the kind is
            # something the summary doesn't otherwise label.
            if kind == "task_transition" or (
                verb and "->" in verb
                and verb.replace("->", " → ") in summary
            ):
                line = (
                    f"{ts_part}  [#97a6b2]{actor}[/#97a6b2]  "
                    f"{_escape(summary)}"
                )
            else:
                line = (
                    f"{ts_part}  [#97a6b2]{actor}[/#97a6b2]  "
                    f"[b]{_escape(verb)}[/b] {_escape(summary)}"
                )
            lines.append(line)
        return "\n".join(lines)

    def _sanitize_activity_summary(self, summary: str) -> str:
        """Trim project-local jargon from a task-transition summary.

        The projector emits ``task <project>/<N>: <from> → <to>`` for
        every transition; on the per-project dashboard the
        ``<project>/`` prefix is already implicit, so dropping it
        ``task #<N>: <from> → <to>`` aligns with the rest of the
        dashboard's task numbering. Also strip ``[Action]`` routing
        tags that the architect's notify subjects sometimes leak into
        transition reasons.

        When the parenthesised reason then names the held task itself
        (``Waiting on operator: #N — ...`` where #N is the same task
        the row is about), elide the self-ref to avoid the ``task #N
        ... #N ...`` echo. Mirrors the cycle 5 hold-reason elision.
        """
        if not summary:
            return summary
        text = summary.replace("[Action] ", "").replace("[Action]", "")
        project_key = getattr(self, "project_key", "") or ""
        if project_key:
            prefix = f"{project_key}/"
            # Replace bare " <project>/<N>" forms with " #<N>". The
            # leading word boundary keeps us from rewriting paths or
            # other content that incidentally contains the project key.
            text = _re.sub(
                rf"\b{_re.escape(prefix)}(\d+)\b",
                r"#\1",
                text,
            )
        # Self-ref elision: when the row already starts with
        # ``task #N:`` and the parenthesised reason carries
        # ``<verb>: #N — text``, drop the leading ``#N`` from the
        # reason so we read ``<verb> — text``.
        head_match = _re.match(r"task\s+#(\d+):", text)
        if head_match:
            self_num = head_match.group(1)
            text = _re.sub(
                rf":\s+#{_re.escape(self_num)}\s+(?=[—\-,;])",
                " ",
                text,
            )
        return text

    def _render_action_card_body(
        self, item: dict, *, compact: bool = False,
    ) -> str:
        """Render one Action Needed card (prompt + steps + decision).

        Used by ``_render`` to populate each per-card Static so each
        action card's response controls (Approve / Wait / Other) sit
        directly under its own card. Mirrors the per-item branch of
        ``_render_inbox_body`` so the consolidated string view (used
        by tests) stays in sync — both call this helper.

        ``compact`` clips the steps list to the first two entries with
        a ``+N more`` summary tail. Caller passes ``True`` when more
        than one action card is visible — Sam (perf review,
        2026-04-26) flagged the dual-card stack as "too tall/noisy",
        and polly_remote routinely renders two cards with 5 steps
        each, blowing past a single screen.
        """
        prompt = _escape(
            item.get("plain_prompt")
            or item.get("next_action")
            or "Polly needs your decision before this project can continue."
        )
        lines: list[str] = [f"  [#f0c45a]◆[/#f0c45a] {prompt}"]
        unblock_steps = [
            str(step)
            for step in (item.get("unblock_steps") or item.get("steps") or [])
            if str(step).strip()
        ]
        if unblock_steps:
            heading = _escape(item.get("steps_heading") or "What to do")
            lines.append(f"    [b]{heading}[/b]")
            step_cap = 2 if compact else 5
            visible_steps = unblock_steps[:step_cap]
            for idx, step in enumerate(visible_steps, start=1):
                lines.append(f"    [dim]{idx}.[/dim] {_escape(step)}")
            hidden = len(unblock_steps) - len(visible_steps)
            if hidden > 0:
                lines.append(
                    f"    [dim](+{hidden} more — click card to see all)[/dim]"
                )
        question = _escape(
            item.get("decision_question")
            or "Choose how Polly should proceed."
        )
        lines.append(f"    [b]Decision:[/b] {question}")
        return "\n".join(lines)

    def _render_inbox_body(self, data: ProjectDashboardData) -> str:
        """Return the consolidated inbox markup as a single string.

        At runtime ``_render`` actually splits this content across
        multiple Statics so the per-card response controls can sit
        directly under their own card (issue #2). This consolidated
        view stays around for tests + the click-hint computation in
        ``_action_card_click_hint`` callers.
        """
        count = data.inbox_count
        blocked_total = int(data.task_counts.get("blocked", 0))
        on_hold_total = int(data.task_counts.get("on_hold", 0))
        self._action_click_task_ids = [
            str(item.get("primary_ref") or "")
            for item in data.action_items[:2]
            if _PROJECT_TASK_REF_RE.fullmatch(str(item.get("primary_ref") or ""))
        ]
        if count == 0 and not data.action_items and not blocked_total and not on_hold_total:
            return "[dim]Inbox is clear for this project.[/dim]"
        lines: list[str] = []
        if data.action_items:
            lines.append("[#f85149][b]To move this project forward[/b][/]")
            visible_items = data.action_items[:2]
            compact_cards = len(visible_items) > 1
            for item in visible_items:
                lines.append(
                    self._render_action_card_body(item, compact=compact_cards)
                )
        lines.append(self._render_inbox_remainder(data))
        return "\n".join(line for line in lines if line)

    def _render_inbox_remainder(self, data: ProjectDashboardData) -> str:
        """Render the post-action-cards portion of the inbox section.

        Holds the click hint, the blocker / on-hold fallback copy when
        no action cards render, the ``N need action`` overflow line,
        the inbox previews (split into action-needed vs FYI), and the
        ``Press i`` CTA. Mounted in the trailing ``inbox_body`` Static
        so it sits under both action-card groups.
        """
        count = data.inbox_count
        blocked_total = int(data.task_counts.get("blocked", 0))
        on_hold_total = int(data.task_counts.get("on_hold", 0))
        action_ids: set[str] = set()
        for item in data.action_items:
            for key in ("task_id", "primary_ref"):
                value = str(item.get(key) or "")
                if value:
                    action_ids.add(value)
        lines: list[str] = []
        if data.action_items:
            # One discoverability hint at the bottom of the stack — was
            # previously per-item, which read as "Click this message
            # to open the source task." repeated verbatim under each
            # card. Keep it singular when there's one card, generic
            # when there are several.
            click_hint = _action_card_click_hint(data.action_items[:2])
            if click_hint:
                lines.append(f"  [dim]{click_hint}[/dim]")
            # When the project also has on_hold tasks (booktalk: plan
            # review action card + #2 on hold), the banner suffix says
            # "· 1 on hold" but the dashboard previously gave no detail
            # until the user scrolled to Task pipeline. Surface a
            # compact one-line summary directly under the action cards
            # so the user knows what else needs attention without
            # guessing. Skip when on_hold task IDs are already
            # represented by an action card (avoid double-counting).
            if on_hold_total:
                on_hold_items = data.task_buckets.get("on_hold", [])
                summary_items = []
                for item in on_hold_items:
                    num = item.get("task_number")
                    if num is None:
                        continue
                    ref = f"{self.project_key}/{num}"
                    if ref in action_ids:
                        continue
                    summary_items.append(item)
                    if len(summary_items) >= 2:
                        break
                if summary_items:
                    lines.append("")
                    label = (
                        "Also on hold"
                        if len(summary_items) == 1
                        else f"Also on hold ({on_hold_total})"
                    )
                    lines.append(f"  [#f0c45a][dim]{label}:[/dim][/]")
                    for item in summary_items:
                        num = item.get("task_number")
                        title = _escape(item.get("title") or "")
                        if len(title) > 60:
                            title = title[:57] + "…"
                        lines.append(
                            f"  [dim]·[/dim] [b]#{num}[/b] {title}"
                        )
            lines.append("")
        elif blocked_total:
            lines.append("[#f0c45a][b]Blocked, but summary missing[/b][/]")
            lines.append(
                "  [dim]This project is blocked, but Polly has not posted "
                "an unblock note yet.[/dim]"
            )
            lines.append(
                "  [dim]Press [b]c[/b] to ask the PM for a blocker summary.[/dim]"
            )
            lines.append("")
        elif on_hold_total:
            lines.append("[#f0c45a][b]On hold[/b][/]")
            on_hold_items = data.task_buckets.get("on_hold", [])[:2]
            if on_hold_items:
                lines.append(
                    "  [dim]These are the root holds keeping downstream "
                    "work waiting.[/dim]"
                )
                for item in on_hold_items:
                    num = item.get("task_number")
                    num_part = f"#{num} " if num is not None else ""
                    title = _escape(item.get("title") or "")
                    lines.append(f"  [#f0c45a]\u25c6[/#f0c45a] [b]{num_part}{title}[/b]")
                    summary = _escape(item.get("summary") or "")
                    if summary:
                        lines.append(f"    {summary}")
                    for idx, step in enumerate(item.get("steps") or [], start=1):
                        lines.append(
                            f"    [dim]{idx}.[/dim] {_escape(str(step))}"
                        )
                lines.append(
                    "  [dim]Decide whether to approve the scoped code "
                    "delivery, split operational acceptance, or provide the "
                    "missing access/credentials.[/dim]"
                )
            else:
                lines.append(
                    "  [dim]No user inbox action is requested yet. Open Tasks "
                    "for the held work item and resume it when appropriate.[/dim]"
                )
            lines.append("")

        preview_items = [
            item for item in data.inbox_top
            if str(item.get("task_id") or "") not in action_ids
            and str(item.get("primary_ref") or "") not in action_ids
        ]
        # Only print the \u25c6 N need action overflow line when the
        # rendered Action Needed cards do *not* already enumerate the
        # full set. When count == cards displayed, the user can see
        # them above and saying "2 need action" under "2 cards" is
        # noise. When count > cards displayed there's something off
        # screen and the line tells Sam to jump to the inbox for more.
        displayed_actions = len(data.action_items[:2])
        if count and count > displayed_actions:
            # Verb agreement, sister to cycle 117's action-bar fix:
            # ``1 needs action``, ``2 need action``. The overflow line
            # only fires when count > displayed_actions, so the
            # singular case (1 inbox item, 0 cards rendered) is reachable.
            verb = "needs" if count == 1 else "need"
            lines.append(
                f"[#f0c45a]\u25c6[/#f0c45a] [b]{count}[/b] "
                f"[dim]{verb} action[/dim]"
            )
        # Split preview_items into "actually needs action" vs the
        # rest. With no action cards rendered, lumping a "completed
        # update" item under a "2 need action" header (because both
        # happen to be in the top inbox slice) reads as
        # "wait, which 2?" \u2014 the count says 2 but the list shows 3.
        # Section the action-needed items first under the count
        # header, then the remainder under an explicit "Other open
        # items" subhead.
        action_previews = [
            item for item in preview_items
            if item.get("needs_action")
        ]
        info_previews = [
            item for item in preview_items
            if not item.get("needs_action")
        ]

        def _emit_preview_row(item: dict) -> None:
            title = _escape(item.get("title") or "")
            age = _format_relative_age(item.get("updated_at") or "")
            age_part = f"  [dim]{_escape(age)}[/dim]" if age else ""
            label = item.get("triage_label") or ""
            label_part = (
                f"  [dim]{_escape(str(label))}[/dim]" if label else ""
            )
            lines.append(f"  \u00b7 {title}{label_part}{age_part}")

        for item in action_previews[:3]:
            _emit_preview_row(item)
        if info_previews:
            if data.action_items or action_previews:
                lines.append("[dim]Other open items[/dim]")
            for item in info_previews[:3]:
                _emit_preview_row(item)
        if (
            not count
            and not preview_items
            and not data.action_items
            and not on_hold_total
            and not blocked_total
        ):
            # Only print the "no items" reassurance when the section
            # really is empty. The on-hold / blocked branches above
            # already rendered something; saying "No project inbox
            # items are open." right under a held-task card reads as
            # the panel contradicting itself (#794).
            lines.append("[dim]No project inbox items are open.[/dim]")
        # Show the "press i" CTA only when the inbox actually has more
        # to offer than what we just rendered. When every action item
        # is already on screen as a card and there are no other open
        # items, the line is redundant with the screen footer ("i
        # inbox") and just adds vertical noise. Keep it when the
        # inbox has spillover so the user knows where to look.
        has_spillover = (
            (count and count > displayed_actions)
            or bool(action_previews)
            or bool(info_previews)
        )
        if has_spillover:
            lines.append("")
            lines.append("[dim]Press [b]i[/b] to jump to the inbox[/dim]")
        return "\n".join(lines)

    def _inbox_section_text(self) -> str:
        """Return the joined visible text of every inbox sub-Static.

        Test-only helper: when the inbox is split across the lead
        Static, per-card Statics, and the trailing body Static (so
        each card's response controls can sit directly under it),
        tests still want a single string to assert against. This
        helper concatenates the rendered text in mount order.
        """
        parts: list[str] = []
        try:
            parts.append(str(self.inbox_lead.render()))
        except Exception:  # noqa: BLE001
            pass
        for body in self.action_card_bodies:
            try:
                parts.append(str(body.render()))
            except Exception:  # noqa: BLE001
                pass
        try:
            parts.append(str(self.inbox_body.render()))
        except Exception:  # noqa: BLE001
            pass
        return "\n".join(p for p in parts if p)

    def _sync_action_controls(self, data: ProjectDashboardData) -> None:
        """Show decision controls for the visible Action Needed cards."""
        for idx, row in enumerate(self.action_control_rows):
            item = data.action_items[idx] if idx < len(data.action_items) else None
            if item is None:
                row.add_class("-hidden")
                self._action_control_task_ids[idx] = None
                continue
            row.remove_class("-hidden")
            task_id = str(item.get("primary_ref") or "")
            self._action_control_task_ids[idx] = (
                task_id if _PROJECT_TASK_REF_RE.fullmatch(task_id) else None
            )
            self.action_primary_buttons[idx].label = str(
                f"{_dashboard_action_key(idx, 'primary')} "
                f"{item.get('primary_label') or 'Approve it anyway'}"
            )
            self.action_secondary_buttons[idx].label = str(
                f"{_dashboard_action_key(idx, 'secondary')} "
                f"{item.get('secondary_label') or 'Wait'}"
            )
            self.action_other_inputs[idx].placeholder = str(
                f"{_dashboard_action_key(idx, 'other')} "
                f"{item.get('other_placeholder') or 'Tell Polly what to do instead...'}"
            )

    def _action_item_at(self, index: int) -> dict | None:
        data = self.data
        if data is None or index < 0 or index >= len(data.action_items):
            return None
        return data.action_items[index]

    def _record_action_response(
        self, index: int, response: str, *, approve_if_possible: bool = False,
    ) -> None:
        item = self._action_item_at(index)
        if item is None:
            self.notify("That action is no longer available.", severity="warning")
            return
        task_id = str(item.get("primary_ref") or "")
        if not _PROJECT_TASK_REF_RE.fullmatch(task_id):
            self.notify(
                "Saved decision locally, but this item is not linked to a task.",
                severity="warning",
            )
            return
        data = self.data
        if data is None or data.project_path is None:
            self.notify("Could not open project database.", severity="error")
            return
        try:
            from pollypm.work.sqlite_service import SQLiteWorkService
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Could not load task service: {exc}", severity="error")
            return
        db_path = data.project_path / ".pollypm" / "state.db"
        approved_task = None
        # Track the pre/post status so the toast can tell the user
        # whether their click actually moved the task or just left a
        # reply. "Decision recorded." was misleading when the underlying
        # task stayed parked at blocked/on_hold/etc. — the user thought
        # they had pushed the project forward. The pre-status lets us
        # also distinguish "didn't move at all" from "resumed from
        # on_hold but stopped short of approval" — the latter is real
        # progress that the original "stayed at 'X'" copy obscured.
        initial_status = ""
        final_status = ""
        try:
            with SQLiteWorkService(
                db_path=db_path, project_path=data.project_path,
            ) as svc:
                initial_status = svc.get(task_id).work_status.value
                svc.add_reply(task_id, response, actor="user")
                if approve_if_possible:
                    task = svc.get(task_id)
                    if task.work_status.value == "on_hold":
                        task = svc.resume(task_id, "user")
                    if task.work_status.value == "review":
                        approved_task = svc.approve(task_id, "user", response)
                final_status = svc.get(task_id).work_status.value
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Could not record decision: {exc}", severity="error")
            return
        if approved_task is not None:
            # notify_task_approved emits its own success toast — don't
            # also double up with the generic "Decision recorded."
            notify_task_approved(approved_task, notify=self.notify)
        elif approve_if_possible and final_status:
            # Approval was requested but the task wasn't in an approvable
            # state. Distinguish three cases so the user knows what
            # actually happened:
            #   * status changed (on_hold → queued/in_progress) → real
            #     progress; we resumed but stopped short of approval.
            #   * status unchanged → click only saved a reply.
            #   * either way, name the state so the user can decide
            #     the next move.
            if initial_status and initial_status != final_status:
                self.notify(
                    f"Resumed task (was '{initial_status}', now "
                    f"'{final_status}'), reply saved — not yet in a "
                    "state PollyPM can auto-approve from here.",
                    severity="information",
                )
            else:
                self.notify(
                    f"Reply saved — task stayed at '{final_status}' "
                    "(not in a state PollyPM can auto-approve from here).",
                    severity="warning",
                )
        else:
            self.notify("Decision recorded.", severity="information")
        self._refresh()

    def _perform_dashboard_action(self, index: int, slot: str) -> None:
        item = self._action_item_at(index)
        if item is None:
            self.notify("That action is no longer available.", severity="warning")
            return
        action = item.get(f"{slot}_action")
        if not isinstance(action, dict):
            response_key = f"{slot}_response"
            response = str(item.get(response_key) or "")
            self._record_action_response(
                index,
                response or ("Approve it anyway." if slot == "primary" else "Wait."),
                approve_if_possible=(slot == "primary"),
            )
            return
        kind = str(action.get("kind") or "").strip()
        if kind == "review_plan":
            # Audit item: "Review plan currently routes to inbox; it
            # may be more useful to route to the plan review item
            # directly if there is a stable route." When the action
            # card carries a project task ref (the plan_project task
            # at user_approval), land the user on that task — they
            # can act on it from one click instead of navigating from
            # inbox → task. Falls back to inbox when no stable task
            # ref is available (older messages, missing primary_ref).
            task_ref = str(
                action.get("task_id")
                or item.get("primary_ref")
                or ""
            )
            if _PROJECT_TASK_REF_RE.fullmatch(task_ref):
                self._route_to_task(task_ref)
                return
            self.action_jump_inbox()
            return
        if kind == "open_task":
            task_id = str(action.get("task_id") or item.get("primary_ref") or "")
            if _PROJECT_TASK_REF_RE.fullmatch(task_id):
                self._route_to_task(task_id)
                return
            self.action_jump_inbox()
            return
        if kind == "open_inbox":
            self.action_jump_inbox()
            return
        if kind == "discuss_pm":
            self.action_chat_pm()
            return
        if kind == "approve_task":
            response = str(
                action.get("response")
                or item.get(f"{slot}_response")
                or "Approved from project dashboard."
            )
            self._record_action_response(
                index, response, approve_if_possible=True,
            )
            return
        response = str(
            action.get("response")
            or item.get(f"{slot}_response")
            or action.get("label")
            or ""
        )
        self._record_action_response(index, response)

    # ------------------------------------------------------------------
    # Actions — keybindings
    # ------------------------------------------------------------------

    def action_refresh(self) -> None:
        self._refresh()

    def action_back(self) -> None:
        self.run_worker(
            lambda: self._route_to_home_sync(),
            thread=True,
            exclusive=True,
            group="proj_home",
        )

    def _route_to_home_sync(self) -> None:
        try:
            self._route_to_home()
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(
                self.notify, f"Return home failed: {exc}", severity="error",
            )

    def _route_to_home(self) -> None:
        router = CockpitRouter(self.config_path)
        router.route_selected("dashboard")

    def action_chat_pm(self) -> None:
        """Route the cockpit right-pane to this project's PM session.

        Mirrors :meth:`PollyInboxApp.action_jump_to_pm` — resolves the
        persona via :func:`_resolve_pm_target` and uses the same worker
        dispatch so tests can monkeypatch the same hook.

        When the project has no plan and the Plan card is the only thing
        the user sees on the dashboard, the chat is seeded with an
        explicit plan-now request so pressing ``c`` actually does what
        the card said it would do (#863). Otherwise the chat opens with
        the generic 'dashboard discussion' context so ad-hoc questions
        are not pre-loaded with a planning ask.
        """
        cockpit_key, pm_label = _resolve_pm_target(
            self.config_path, self.project_key,
        )
        if self._idle_project_needs_plan():
            context_line = (
                f're: project/{self.project_key} '
                f'"please draft an initial plan for this project"'
            )
        else:
            context_line = f're: project/{self.project_key} "dashboard discussion"'
        self.run_worker(
            lambda: self._dispatch_to_pm_sync(
                cockpit_key, context_line, pm_label,
            ),
            thread=True,
            exclusive=True,
            group="proj_jump_to_pm",
        )

    def _idle_project_needs_plan(self) -> bool:
        """True iff the Plan card is showing the 'press c to ask' nudge."""
        data = getattr(self, "data", None)
        if data is None:
            return False
        if not getattr(data, "exists_on_disk", False):
            return False
        if getattr(data, "plan_path", None) is not None:
            return False
        if not getattr(data, "enforce_plan", True):
            return False
        return True

    def _perform_numbered_action(self, key_number: int) -> None:
        index = (key_number - 1) // 3
        slot_number = (key_number - 1) % 3
        if self._action_item_at(index) is None:
            self.notify("That action is no longer available.", severity="warning")
            return
        if slot_number == 0:
            self._perform_dashboard_action(index, "primary")
            return
        if slot_number == 1:
            self._perform_dashboard_action(index, "secondary")
            return
        try:
            self.action_other_inputs[index].focus()
        except Exception:  # noqa: BLE001
            return
        self.hint.update("Type your reply and press Enter")

    def action_action_card_1(self) -> None:
        self._perform_numbered_action(1)

    def action_action_card_2(self) -> None:
        self._perform_numbered_action(2)

    def action_action_card_3(self) -> None:
        self._perform_numbered_action(3)

    def action_action_card_4(self) -> None:
        self._perform_numbered_action(4)

    def action_action_card_5(self) -> None:
        self._perform_numbered_action(5)

    def action_action_card_6(self) -> None:
        self._perform_numbered_action(6)

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
        # #751 — scope the inbox to the current project on jump so the
        # user doesn't land in the global feed when they came from a
        # specific project. Router resolves ``inbox:<key>`` to a
        # scoped static-view route.
        router.route_selected(f"inbox:{self.project_key}")

    def _route_to_task(self, task_id: str) -> None:
        match = _PROJECT_TASK_REF_RE.fullmatch(task_id)
        if match is None:
            self._route_to_inbox()
            return
        router = CockpitRouter(self.config_path)
        router.route_selected(
            f"project:{match.group('project')}:issues:task:{match.group('number')}"
        )

    def _route_to_tasks(self) -> None:
        router = CockpitRouter(self.config_path)
        router.route_selected(f"project:{self.project_key}:issues")

    def _route_to_current_activity(self) -> None:
        data = self.data
        if data is None:
            self._route_to_tasks()
            return
        in_flight = data.task_buckets.get("in_progress", [])
        if in_flight:
            first = in_flight[0]
            task_number = first.get("task_number")
            if task_number is not None:
                self._route_to_task(f"{self.project_key}/{task_number}")
                return
        self._route_to_tasks()

    def _action_task_for_click(self, event: events.Click) -> str | None:
        ids = self._action_click_task_ids
        if not ids:
            return None
        if len(ids) == 1:
            return ids[0]
        # The Action Needed card starts with a title row plus the
        # "To move this project forward" header. First action rows sit
        # near the top; details/older rows are below. This intentionally
        # keeps click targeting coarse: task-row clicks jump to the
        # nearest actionable task, while whitespace still falls through
        # to the inbox route.
        y = max(0, int(getattr(event, "y", 0) or 0))
        if y <= 7:
            return ids[0]
        if y <= 12:
            return ids[1]
        return None

    # ------------------------------------------------------------------
    # Click handlers (#750)
    # ------------------------------------------------------------------
    # The action bar ("1 approval · 1 new in inbox") and the dedicated
    # Inbox section were previously text-only — clicking them did
    # nothing, so the user had to discover the ``i`` keybinding. Bind
    # clicks on either surface to the same action that keyboard ``i``
    # triggers so mouse users land where they'd expect.
    @on(Button.Pressed, ".proj-action-control")
    def on_action_control_pressed(self, event: Button.Pressed) -> None:
        control_id = str(event.button.id or "")
        match = _re.fullmatch(r"proj-action-(\d+)-(primary|secondary)", control_id)
        if match is None:
            return
        event.stop()
        index = int(match.group(1))
        action = match.group(2)
        item = self._action_item_at(index)
        if item is None:
            return
        if action == "primary":
            self._perform_dashboard_action(index, "primary")
            return
        self._perform_dashboard_action(index, "secondary")

    @on(Input.Submitted, ".proj-action-other")
    def on_action_other_submitted(self, event: Input.Submitted) -> None:
        input_id = str(event.input.id or "")
        match = _re.fullmatch(r"proj-action-(\d+)-other", input_id)
        if match is None:
            return
        event.stop()
        response = (event.value or "").strip()
        if not response:
            return
        self._record_action_response(int(match.group(1)), response)
        event.input.value = ""

    @on(events.Click, "#proj-action-bar")
    def on_action_bar_click(self, _event: events.Click) -> None:
        self.action_jump_inbox()

    @on(events.Click, "#proj-inbox-section")
    def on_inbox_section_click(self, event: events.Click) -> None:
        task_id = self._action_task_for_click(event)
        if task_id:
            event.stop()
            self._route_to_task(task_id)
            return
        self.action_jump_inbox()

    @on(events.Click, "#proj-now-section")
    def on_now_section_click(self, event: events.Click) -> None:
        event.stop()
        self._route_to_current_activity()

    @on(events.Click, "#proj-pipeline-section")
    def on_pipeline_section_click(self, event: events.Click) -> None:
        event.stop()
        self._route_to_tasks()

    @on(events.Click, "#proj-plan-section")
    def on_plan_section_click(self, event: events.Click) -> None:
        event.stop()
        self.action_open_plan()

    @on(events.Click, "#proj-activity-section")
    def on_activity_section_click(self, event: events.Click) -> None:
        event.stop()
        self.action_jump_activity()

    @on(events.Enter, "#proj-action-bar")
    def on_action_bar_enter(self, _event: events.Enter) -> None:
        self.action_bar.add_class("-hover")

    @on(events.Leave, "#proj-action-bar")
    def on_action_bar_leave(self, _event: events.Leave) -> None:
        self.action_bar.remove_class("-hover")

    @on(events.Enter, "#proj-inbox-section")
    def on_inbox_section_enter(self, _event: events.Enter) -> None:
        try:
            self.query_one("#proj-inbox-section").add_class("-hover")
        except Exception:  # noqa: BLE001
            pass

    @on(events.Leave, "#proj-inbox-section")
    def on_inbox_section_leave(self, _event: events.Leave) -> None:
        try:
            self.query_one("#proj-inbox-section").remove_class("-hover")
        except Exception:  # noqa: BLE001
            pass

    @on(
        events.Enter,
        "#proj-now-section,#proj-pipeline-section,#proj-plan-section,#proj-activity-section",
    )
    def on_clickable_section_enter(self, event: events.Enter) -> None:
        try:
            event.control.add_class("-hover")
        except Exception:  # noqa: BLE001
            pass

    @on(
        events.Leave,
        "#proj-now-section,#proj-pipeline-section,#proj-plan-section,#proj-activity-section",
    )
    def on_clickable_section_leave(self, event: events.Leave) -> None:
        try:
            event.control.remove_class("-hover")
        except Exception:  # noqa: BLE001
            pass

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
        with nothing to show. Project-local ``[planner].enforce_plan
        = false`` projects get a different toast that surfaces the
        explicit bypass rather than nudging as if a plan is missing.
        """
        data = self.data
        if data is None or data.plan_path is None:
            if data is not None and not getattr(data, "enforce_plan", True):
                self.notify(
                    "Plan not required for this project "
                    "([planner].enforce_plan = false).",
                    severity="information", timeout=2.0,
                )
            else:
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
            if data is not None and not getattr(data, "enforce_plan", True):
                self.notify(
                    "Plan not required for this project — "
                    "[planner].enforce_plan = false.",
                    severity="information", timeout=2.0,
                )
            else:
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
