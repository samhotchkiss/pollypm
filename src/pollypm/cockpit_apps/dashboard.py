"""Home dashboard Textual app."""

from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Static

from pollypm.cockpit_markup import _escape
from pollypm.cockpit_navigation_client import file_navigation_client
from pollypm.cockpit_palette import _open_keyboard_help


class PollyDashboardApp(App[None]):
    """Rich dashboard: what's happening, what got done, token usage."""

    TITLE = "PollyPM"
    SUB_TITLE = "Dashboard"
    BINDINGS = [
        Binding("i", "jump_inbox", "Inbox"),
        Binding("r", "refresh", "Refresh"),
        Binding("question_mark", "show_keyboard_help", "Help", priority=True),
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

    def action_show_keyboard_help(self) -> None:
        _open_keyboard_help(self)

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
        # #1109 follow-up — TTY-less keystroke bridge. See
        # ``cockpit_input_bridge`` module docstring for rationale.
        try:
            from pollypm.cockpit_input_bridge import start_input_bridge
            self._input_bridge_handle = start_input_bridge(
                self, kind="dashboard", config_path=self.config_path,
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
            # ``alert_count`` is a *curated* subset of open alerts —
            # operational/heartbeat noise (``pane:*``, ``no_session``,
            # ``stuck_session`` …) and ``stuck_on_task`` alerts whose
            # task is already in a user-waiting state are filtered out
            # so the header only shows what the user can act on. ``pm
            # alerts`` lists *every* open alert (including operational
            # ones), so the two counts disagreed without explanation
            # (#999). Label the curated count "needs action" so users
            # who reach for ``pm alerts`` to drill in aren't surprised
            # by a higher number.
            parts.append(
                f"[#f85149][b]{data.alert_count}[/b] needs action[/#f85149]"
            )
        header_lines = ["  " + "  \u00b7  ".join(parts)]
        account_usages = getattr(data, "account_usages", [])
        if account_usages:
            usage = account_usages[0]
            label = usage.provider or usage.account_name
            header_lines.append(
                "  "
                f"LLM quota: {_escape(label)} \u00b7 "
                f"[b]{usage.used_pct}%[/b] used of {_escape(usage.limit_label)}"
            )
        header_text = "\n".join(header_lines)
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
            # #1100 — capital ``I`` matches the post-#1089 global Inbox
            # binding. Lowercase ``i`` is a no-op from the Home rail
            # because the rail's ``i`` is the (project-surface-only)
            # ``forward_project_jump_inbox`` priority binding; advertising
            # it here misled users into thinking the cockpit was stuck.
            message_lines.append("[dim]Press [b]I[/b] to jump to the inbox[/dim]")
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
            # #1100 — see sibling comment above; capital ``I`` is the
            # actual Home-reachable Inbox keystroke post-#1089.
            message_lines.append("[dim]Press [b]I[/b] to jump to the inbox[/dim]")
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

        # ── Token chart + cached LLM account quota ──
        chart_lines: list[str] = []
        if account_usages:
            chart_lines.append("[b]LLM account quota usage[/b]")
            for usage in account_usages:
                if usage.severity == "critical":
                    marker = "[#f85149]▲[/#f85149]"
                    suffix = " · over limit"
                elif usage.severity == "warning":
                    marker = "[#d29922]◆[/#d29922]"
                    suffix = " · approaching ceiling"
                else:
                    marker = "[dim]·[/dim]"
                    suffix = ""
                label = usage.provider or usage.account_name
                if usage.email:
                    label = f"{label} ({usage.email})"
                line = (
                    f"{marker} {_escape(label)}  "
                    f"[b]{usage.used_pct}%[/b] used of {_escape(usage.limit_label)}"
                )
                if usage.reset_at and usage.severity in {"warning", "critical"}:
                    suffix += f" · resets {_escape(usage.reset_at)}"
                chart_lines.append(line + suffix)
            chart_lines.append("")

        if data.daily_tokens:
            values = [t for _, t in data.daily_tokens]
            max_val = max(values) or 1
            chart_height = 6
            bars = [max(0, min(chart_height, round(v / max_val * chart_height))) for v in values]

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
            chart_lines.append("[dim]No token data yet[/dim]")
            self.chart_body.update("\n".join(chart_lines))

        # ── Footer ──
        sweep_word = "sweep" if data.sweep_count_24h == 1 else "sweeps"
        msg_word = "message" if data.message_count_24h == 1 else "messages"
        footer_action = (
            "Click Polly to connect"
            if n_projects
            else "Add a project: pm add-project"
        )
        footer = (
            f"[dim]{footer_action}  \u00b7  "
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
        file_navigation_client(
            self.config_path,
            client_id="polly-dashboard",
        ).jump_to_inbox()
