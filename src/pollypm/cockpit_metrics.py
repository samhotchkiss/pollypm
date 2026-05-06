"""Cockpit observability metrics panel.

Contract:
- Inputs: a cockpit config path plus metrics snapshots gathered from
  ``pollypm.cockpit``.
- Outputs: ``PollyMetricsApp`` and the drill-down helpers it owns.
- Side effects: loads config, optionally inspects live supervisor state,
  mounts alert toasts, and opens modal drill-down screens.
- Invariants: metrics rendering and drill-down behavior stay local to
  this module and use only public service/palette boundaries.
"""

from __future__ import annotations

from pathlib import Path

from rich.markup import escape as _escape
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from pollypm.cockpit_palette import _open_command_palette, _open_keyboard_help
from pollypm.config import load_config


_METRICS_TONE_COLOURS: dict[str, str] = {
    "ok": "#3ddc84",
    "warn": "#f0c45a",
    "alert": "#ff5f6d",
    "muted": "#6b7a88",
}


class PollyMetricsApp(App[None]):
    """Observability metrics panel — ``pm cockpit-pane metrics``."""

    TITLE = "PollyPM"
    SUB_TITLE = "Metrics"

    AUTO_REFRESH_SECONDS = 10.0

    CSS = """
    Screen {
        background: #0f1317;
        color: #eef2f4;
        padding: 0;
    }
    #me-outer {
        height: 1fr;
        padding: 1 2;
    }
    #me-topbar {
        height: 3;
        padding: 0 0 1 0;
        border-bottom: solid #1e2730;
    }
    #me-counters {
        height: 1;
        color: #97a6b2;
    }
    #me-scroll {
        height: 1fr;
        padding: 1 0 0 0;
        scrollbar-size: 1 1;
        scrollbar-color: #2a3340;
    }
    .me-section {
        height: auto;
        margin-bottom: 1;
        padding: 1 2;
        background: #111820;
        border: round #1e2730;
    }
    .me-section-title {
        color: #5b8aff;
        text-style: bold;
        padding-bottom: 0;
    }
    .me-section-body {
        color: #d6dee5;
    }
    .me-section.-selected {
        border: round #5b8aff;
    }
    #me-hint {
        height: 1;
        padding: 0 2;
        color: #3e4c5a;
        background: #0c0f12;
    }
    """

    BINDINGS = [
        Binding("r,R", "refresh", "Refresh"),
        Binding("a,A", "toggle_auto", "Auto-refresh"),
        Binding("down,j", "cursor_down", "Next"),
        Binding("up,k", "cursor_up", "Prev"),
        Binding("enter", "drill_down", "Drill-down"),
        Binding("ctrl+k,colon", "open_command_palette", "Palette", priority=True),
        Binding("question_mark", "show_keyboard_help", "Help", priority=True),
        Binding("q,escape", "back", "Back"),
    ]

    _DEFAULT_HINT = (
        "R refresh \u00b7 A auto-refresh \u00b7 \u2191\u2193 select "
        "\u00b7 \u21b5 drill-down \u00b7 q back"
    )

    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self.config_path = config_path
        self.topbar = Static("[b]Metrics[/b]", id="me-topbar", markup=True)
        self.counters = Static("", id="me-counters", markup=True)
        self.hint = Static(self._DEFAULT_HINT, id="me-hint", markup=True)
        self.snapshot = None
        self._auto_refresh: bool = False
        self._auto_refresh_timer = None
        self._selected_index: int = 0
        self._section_order = [
            "fleet",
            "resources",
            "throughput",
            "failures",
            "schedulers",
        ]
        self._section_titles: dict[str, Static] = {
            key: Static(f"[b]{key.title()}[/b]", classes="me-section-title", markup=True)
            for key in self._section_order
        }
        self._section_bodies: dict[str, Static] = {
            key: Static("", classes="me-section-body", markup=True)
            for key in self._section_order
        }

    def compose(self) -> ComposeResult:
        with Vertical(id="me-outer"):
            yield self.topbar
            yield self.counters
            with VerticalScroll(id="me-scroll"):
                for key in self._section_order:
                    with Vertical(classes="me-section", id=f"me-section-{key}"):
                        yield self._section_titles[key]
                        yield self._section_bodies[key]
        yield self.hint

    def on_mount(self) -> None:
        self._refresh()
        # Alert toast surface removed in #956 — the alert list still
        # renders inside this metrics pane via the drill-down modal.

    def action_open_command_palette(self) -> None:
        _open_command_palette(self)

    def action_show_keyboard_help(self) -> None:
        _open_keyboard_help(self)

    def _gather(self):
        """Build a ``MetricsSnapshot``; tests monkeypatch this seam."""
        from pollypm.cockpit import _gather_metrics_snapshot

        try:
            config = load_config(self.config_path)
        except Exception:  # noqa: BLE001
            return None
        try:
            return _gather_metrics_snapshot(config)
        except Exception:  # noqa: BLE001
            return None

    def _refresh(self) -> None:
        self.snapshot = self._gather()
        self._render()

    @staticmethod
    def _tone_colour(tone: str) -> str:
        return _METRICS_TONE_COLOURS.get(tone, "#d6dee5")

    def _render(self) -> None:
        snap = self.snapshot
        auto_tag = (
            "[#3ddc84]auto on[/#3ddc84]" if self._auto_refresh
            else "[dim]auto off[/dim]"
        )
        ts = getattr(snap, "captured_at", None) if snap else None
        if ts:
            ts_label = ts[11:19] if len(ts) >= 19 else ts
            self.counters.update(
                f"[dim]last refresh {_escape(ts_label)} UTC[/dim]  \u00b7  {auto_tag}"
            )
        else:
            self.counters.update(f"[#ff5f6d]no snapshot[/#ff5f6d]  \u00b7  {auto_tag}")
        if snap is None:
            empty = "[dim](metrics unavailable — check state.db)[/dim]"
            for key in self._section_order:
                self._section_bodies[key].update(empty)
            return

        sections = {section.key: section for section in snap.sections()}
        for key in self._section_order:
            section = sections.get(key)
            title_widget = self._section_titles[key]
            body_widget = self._section_bodies[key]
            if section is None:
                title_widget.update(f"[b]{key.title()}[/b]")
                body_widget.update("[dim](no data)[/dim]")
                continue
            title_widget.update(f"[b]{_escape(section.title)}[/b]")
            if not section.rows:
                body_widget.update("[dim](no data)[/dim]")
                continue
            lines: list[str] = []
            for label, value, tone in section.rows:
                colour = self._tone_colour(tone)
                if value:
                    lines.append(
                        f"[{colour}]\u25cf[/{colour}] "
                        f"[dim]{_escape(label)}:[/dim] "
                        f"[{colour}]{_escape(value)}[/{colour}]"
                    )
                else:
                    # Narrative-only rows (empty-state messages) drop
                    # the ``:`` so the line reads as a single sentence
                    # instead of "label: \u2014".
                    lines.append(
                        f"[{colour}]\u25cf[/{colour}] "
                        f"[dim]{_escape(label)}[/dim]"
                    )
            body_widget.update("\n".join(lines))

        for index, key in enumerate(self._section_order):
            try:
                node = self.query_one(f"#me-section-{key}")
            except Exception:  # noqa: BLE001
                continue
            if index == self._selected_index:
                node.add_class("-selected")
            else:
                node.remove_class("-selected")

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

    def action_cursor_down(self) -> None:
        if self._selected_index < len(self._section_order) - 1:
            self._selected_index += 1
            self._render()

    def action_cursor_up(self) -> None:
        if self._selected_index > 0:
            self._selected_index -= 1
            self._render()

    def action_drill_down(self) -> None:
        snap = self.snapshot
        if snap is None:
            self.notify("No snapshot loaded yet.", severity="warning", timeout=2.0)
            return
        try:
            key = self._section_order[self._selected_index]
        except IndexError:
            return
        section = next((section for section in snap.sections() if section.key == key), None)
        if section is None:
            return
        body = self._build_drill_down_body(section)
        try:
            self.push_screen(_MetricsDrillDownModal(section.title, body))
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Drill-down failed: {exc}", severity="error", timeout=3.0)

    def _build_drill_down_body(self, section) -> str:
        lines: list[str] = [f"[b]{_escape(section.title)}[/b]", ""]
        if not section.rows:
            lines.append("[dim](no data)[/dim]")
            return "\n".join(lines)
        for label, value, tone in section.rows:
            colour = self._tone_colour(tone)
            if value:
                lines.append(
                    f"[{colour}]\u25cf[/{colour}]  [b]{_escape(label)}[/b]"
                    f"\n    [dim]{_escape(value)}[/dim]"
                )
            else:
                # Narrative-only row \u2014 skip the second value line.
                lines.append(
                    f"[{colour}]\u25cf[/{colour}]  [b]{_escape(label)}[/b]"
                )
            lines.append("")
        if section.key == "resources":
            try:
                proc_lines = _metrics_process_breakdown(self.config_path)
            except Exception:  # noqa: BLE001
                proc_lines = []
            if proc_lines:
                lines.append("[b]Live sessions[/b]")
                for text in proc_lines:
                    lines.append(f"  {_escape(text)}")
        return "\n".join(lines)

    def action_back(self) -> None:
        self.exit()


class _MetricsDrillDownModal(ModalScreen[None]):
    """Modal overlay shown when the user presses Enter on a metrics section."""

    CSS = """
    _MetricsDrillDownModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.45);
    }
    #me-modal-dialog {
        width: 76;
        max-width: 95%;
        height: auto;
        max-height: 26;
        padding: 1 2;
        background: #141a20;
        border: round #5b8aff;
    }
    #me-modal-body {
        height: auto;
        max-height: 22;
        color: #d6dee5;
        scrollbar-size: 1 1;
        scrollbar-color: #2a3340;
    }
    """

    BINDINGS = [Binding("escape,q,enter", "dismiss", "Close")]

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self._title = title
        self._body_text = body

    def compose(self) -> ComposeResult:
        with Vertical(id="me-modal-dialog"):
            yield Static(f"[b]{_escape(self._title)}[/b]", id="me-modal-title", markup=True)
            yield VerticalScroll(Static(self._body_text, markup=True), id="me-modal-body")
            yield Static("[dim]esc to close[/dim]", id="me-modal-hint", markup=True)

    def action_dismiss(self) -> None:
        self.dismiss(None)


def _metrics_process_breakdown(config_path: Path) -> list[str]:
    """Return one-line-per-session RSS breakdown for the drill-down modal."""
    try:
        config = load_config(config_path)
    except Exception:  # noqa: BLE001
        return []
    try:
        from pollypm.cockpit import _humanize_bytes, _rss_bytes_for_pid
        from pollypm.service_api import PollyPMService

        cfg_path = getattr(getattr(config, "project", None), "config_file", None) or getattr(
            getattr(config, "project", None), "config_path", None,
        ) or config_path
        supervisor = PollyPMService(cfg_path).load_supervisor(readonly_state=True)
    except Exception:  # noqa: BLE001
        return []
    try:
        launches, windows, _alerts, _leases, _errors = supervisor.status()
    except Exception:  # noqa: BLE001
        launches, windows = [], []
    finally:
        try:
            supervisor.store.close()
        except Exception:  # noqa: BLE001
            pass
    window_map = {window.name: window for window in windows}
    lines: list[str] = []
    for launch in launches:
        window = window_map.get(launch.window_name)
        if window is None or getattr(window, "pane_dead", False):
            continue
        try:
            pid = int(getattr(window, "pane_pid", 0) or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid <= 0:
            continue
        rss = _rss_bytes_for_pid(pid)
        if rss is None:
            continue
        lines.append(f"{launch.session.name} (pid {pid}) · {_humanize_bytes(rss)}")
    return lines
