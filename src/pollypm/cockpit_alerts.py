"""Cockpit alert-toast infrastructure.

Contract:
- Inputs: a host Textual ``App`` with ``config_path`` plus alert rows in
  the unified ``messages`` store.
- Outputs: mounted ``AlertToast`` widgets and an ``AlertNotifier`` that
  polls for new alerts.
- Side effects: queries ``state.db`` and mounts/removes transient toast
  widgets on the host app.
- Invariants: alert awareness is additive and reusable across cockpit
  screens; callers only use ``_setup_alert_notifier`` / ``_action_view_alerts``.
"""

from __future__ import annotations

from pathlib import Path
import re

from textual.app import App
from textual.binding import Binding
from textual.containers import Container
from textual.widgets import Static

from pollypm.cli_features.alerts import is_surfaceable_operational_alert
from pollypm.config import load_config
from pollypm.cockpit_palette import _palette_nav


_ALERT_TOAST_SEVERITY_ICONS = {
    "error": "\U0001f534",
    "critical": "\U0001f534",
    "warning": "\U0001f7e1",
    "warn": "\U0001f7e1",
    "info": "\U0001f535",
}

# #765 — Operational alert types never become toasts. They're heartbeat
# classification signals (the heartbeat noticed the snapshot is stable,
# a stabilize step didn't converge, a follow-up is queued) that are
# useful as activity-log / dashboard-list entries but are NOT user-
# actionable. Toasting them trains the user to dismiss toasts, which
# then also dismisses the real action-required ones. Matches the filter
# already used in dashboard_data.py:377, cockpit_ui.py:7884, and
# cockpit.py:172.
_OPERATIONAL_ALERT_TYPES: frozenset[str] = frozenset({
    # Heartbeat classification signals.
    "suspected_loop",
    "stabilize_failed",
    "needs_followup",
    # Window / pane state — the supervisor auto-recovers these without
    # user intervention (respawn missing window, relaunch dead pane,
    # handle shell-return). Toasting every one trained the user to
    # dismiss alerts. See #765 + morning-after follow-up.
    "missing_window",
    "pane_dead",
    "shell_returned",
    "idle_output",
    # Heuristic "this session has been quiet too long" escalation. The
    # reviewer sits idle by design when the queue is empty; an
    # architect sits idle after emit until a human picks. The alert
    # was firing on both — neither is actionable to the user.
    "stuck_session",
    # no_session used to be user-actionable ("manually pm task claim"),
    # but auto-claim (#768) now spawns the worker within a sweep tick.
    # Keep the alert on the activity log for observability but don't
    # interrupt.
    "no_session",
})

# Operational alert *prefixes* — an alert_type whose start matches any
# of these is also treated as operational and filtered from toasts.
# Prefixes that describe a user-actionable task stall belong in
# cli_features.alerts.SURFACEABLE_OPERATIONAL_ALERT_PREFIXES instead,
# not here. #788.
_OPERATIONAL_ALERT_PREFIXES: tuple[str, ...] = (
    "unmanaged_window:",
    # ``pane:<pattern>`` alerts are raised by the heartbeat pattern
    # matcher (stuck_on_error / auth_expired / etc.) — all supervisor-
    # internal. Supervisor auto-handles the recovery; toasting every
    # pattern match just tells the user "something's happening
    # mechanically" and trains them to dismiss real alerts.
    "pane:",
)


def _is_operational_alert(alert_type: str) -> bool:
    """Internal alias — prefer the public :func:`is_operational_alert`."""
    return is_operational_alert(alert_type)


def is_operational_alert(alert_type: str) -> bool:
    """Return True when ``alert_type`` is heartbeat-internal operational noise.

    Public helper shared by dashboards, rail summaries, project
    dashboard, and the cockpit alert toaster. Before this existed,
    five modules each maintained their own inline tuple (``suspected_loop``,
    ``stabilize_failed``, ``needs_followup``) and drifted independently
    — adding a new operational alert type meant hunting for them all.
    """
    name = alert_type or ""
    if is_surfaceable_operational_alert(name):
        return False
    if name in _OPERATIONAL_ALERT_TYPES:
        return True
    return any(name.startswith(prefix) for prefix in _OPERATIONAL_ALERT_PREFIXES)


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _alert_toast_icon(severity: str) -> str:
    """Return the single-glyph icon for ``severity`` with a sane fallback."""
    return _ALERT_TOAST_SEVERITY_ICONS.get(
        (severity or "").lower(),
        "\U0001f7e1",
    )


def _alert_toast_width(*, host_width: int, preferred: int = 52) -> int:
    """Clamp the toast width to the visible host budget."""
    budget = max(18, int(host_width) - 2)
    return min(preferred, budget)


def _sanitize_alert_message(message: str) -> str:
    """Strip terminal control sequences and collapse whitespace."""
    text = _ANSI_ESCAPE_RE.sub("", message or "")
    text = "".join(ch for ch in text if ch >= " " or ch in "\n\t")
    return " ".join(text.split())


class _AlertLikeRecord:
    """Adapter wrapping a :meth:`Store.query_messages` alert row."""

    __slots__ = ("_row",)

    def __init__(self, row: dict) -> None:
        self._row = row

    @property
    def alert_id(self) -> int | None:
        raw_id = self._row.get("id")
        if raw_id is None:
            return None
        try:
            return int(raw_id)
        except (TypeError, ValueError):
            return None

    @property
    def session_name(self) -> str:
        return str(self._row.get("scope") or "")

    @property
    def alert_type(self) -> str:
        return str(self._row.get("sender") or "")

    @property
    def severity(self) -> str:
        payload = self._row.get("payload") or {}
        if isinstance(payload, dict):
            sev = payload.get("severity")
            if isinstance(sev, str) and sev:
                return sev
        return "warn"

    @property
    def message(self) -> str:
        return str(self._row.get("subject") or "")

    @property
    def status(self) -> str:
        return str(self._row.get("state") or "open")

    @property
    def created_at(self) -> str:
        return str(self._row.get("created_at") or "")

    @property
    def updated_at(self) -> str:
        return str(self._row.get("updated_at") or "")


class AlertToast(Static):
    """One bottom-right alert toast."""

    DEFAULT_TIMEOUT_SECONDS = 8.0

    DEFAULT_CSS = """
    AlertToast {
        width: 52;
        height: auto;
        min-height: 3;
        max-height: 6;
        padding: 1 2;
        margin: 0 0 1 0;
        content-align: left top;
        color: #f5f7fa;
    }
    AlertToast.severity-warn {
        background: #2a2411;
        border: round #f0c45a;
    }
    AlertToast.severity-error {
        background: #2c1618;
        border: round #ff5f6d;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_toast", "Dismiss", show=False),
    ]

    def __init__(
        self,
        *,
        alert_id: int | None,
        severity: str,
        message: str,
        show_action_hint: bool = True,
        timeout_seconds: float | None = None,
        width_chars: int | None = None,
    ) -> None:
        super().__init__(markup=True)
        self.alert_id = alert_id
        self.severity = (severity or "warn").lower()
        self.message = message or ""
        self.show_action_hint = show_action_hint
        self.width_chars = width_chars
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else self.DEFAULT_TIMEOUT_SECONDS
        )
        self._dismiss_timer = None
        self.add_class(
            "severity-error"
            if self.severity in ("error", "critical")
            else "severity-warn"
        )
        self.update(self._render_body())

    def _render_body(self) -> str:
        icon = _alert_toast_icon(self.severity)
        text = _sanitize_alert_message(self.message)
        if len(text) > 60:
            text = text[:57] + "\u2026"
        body = f"{icon}  [b]{_escape_markup(text) or 'alert'}[/b]"
        if self.show_action_hint:
            body += "\n[dim]press [b]a[/b] to view all \u00b7 esc to dismiss[/dim]"
        else:
            body += "\n[dim]esc/click to dismiss[/dim]"
        return body

    def on_mount(self) -> None:
        if self.width_chars is not None:
            try:
                self.styles.width = self.width_chars
            except Exception:  # noqa: BLE001
                pass
        try:
            self.call_after_refresh(self._start_dismiss_timer)
        except Exception:  # noqa: BLE001
            self._dismiss_timer = None

    def _start_dismiss_timer(self) -> None:
        try:
            self._dismiss_timer = self.set_timer(
                self.timeout_seconds,
                self.action_dismiss_toast,
            )
        except Exception:  # noqa: BLE001
            self._dismiss_timer = None

    def on_click(self) -> None:
        self.action_dismiss_toast()

    def action_dismiss_toast(self) -> None:
        if self._dismiss_timer is not None:
            try:
                self._dismiss_timer.stop()
            except Exception:  # noqa: BLE001
                pass
            self._dismiss_timer = None
        try:
            self.remove()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.display = False
        except Exception:  # noqa: BLE001
            pass


def _escape_markup(text: str) -> str:
    """Minimal Rich-markup escape — avoids interpreting ``[`` as a tag."""
    return text.replace("[", "\\[")


class AlertNotifier:
    """Background poller that mounts :class:`AlertToast` widgets on an app."""

    POLL_INTERVAL_SECONDS = 5.0
    MAX_VISIBLE = 3

    def __init__(
        self,
        app: App,
        *,
        config_path: Path,
        poll_interval: float | None = None,
        max_visible: int | None = None,
        bind_a: bool = True,
    ) -> None:
        self.app = app
        self.config_path = config_path
        self.poll_interval = (
            poll_interval
            if poll_interval is not None
            else self.POLL_INTERVAL_SECONDS
        )
        self.max_visible = (
            max_visible if max_visible is not None else self.MAX_VISIBLE
        )
        self.bind_a = bind_a
        self._seen_alert_ids: set = set()
        self._toasts: list[AlertToast] = []
        self._container: Container | None = None
        self._timer = None
        self._prime_seen_set()

    def attach(self, container: Container) -> None:
        """Bind the notifier to the app's toast-container widget."""
        self._container = container
        try:
            self._timer = self.app.set_interval(
                self.poll_interval,
                self.poll_now,
            )
        except Exception:  # noqa: BLE001
            self._timer = None

    def stop(self) -> None:
        """Cancel the poll timer. Idempotent."""
        if self._timer is not None:
            try:
                self._timer.stop()
            except Exception:  # noqa: BLE001
                pass
            self._timer = None

    def _prime_seen_set(self) -> None:
        try:
            alerts = self._fetch_alerts()
        except Exception:  # noqa: BLE001
            return
        for record in alerts:
            key = self._dedup_key(record)
            if key is not None:
                self._seen_alert_ids.add(key)

    def _fetch_alerts(self) -> list:
        """Return the current open alerts via :class:`Store`."""
        try:
            config = load_config(self.config_path)
        except Exception:  # noqa: BLE001
            return []
        try:
            from pollypm.store import SQLAlchemyStore

            store = SQLAlchemyStore(f"sqlite:///{config.project.state_db}")
        except Exception:  # noqa: BLE001
            return []
        try:
            rows = store.query_messages(type="alert", state="open")
        except Exception:  # noqa: BLE001
            rows = []
        finally:
            try:
                store.close()
            except Exception:  # noqa: BLE001
                pass
        return [_AlertLikeRecord(row) for row in rows]

    @staticmethod
    def _dedup_key(record) -> object:
        alert_id = getattr(record, "alert_id", None)
        if alert_id is not None:
            return ("id", alert_id)
        session = getattr(record, "session_name", "")
        alert_type = getattr(record, "alert_type", "")
        updated = getattr(record, "updated_at", "")
        return ("sk", session, alert_type, updated)

    def poll_now(self) -> list[AlertToast]:
        """Fetch + mount any new toasts immediately. Returns the new list.

        Operational alert types (``suspected_loop`` etc.) are filtered
        out here — they still appear in the alert list and activity
        feed, but they don't interrupt the user with a toast. See
        :data:`_OPERATIONAL_ALERT_TYPES` and issue #765.
        """
        try:
            alerts = self._fetch_alerts()
        except Exception:  # noqa: BLE001
            return []
        mounted: list[AlertToast] = []
        for record in alerts:
            key = self._dedup_key(record)
            if key in self._seen_alert_ids:
                continue
            # Dedup even for suppressed types so the same operational
            # signal doesn't trip the filter again next poll.
            self._seen_alert_ids.add(key)
            alert_type = getattr(record, "alert_type", "")
            if _is_operational_alert(alert_type):
                continue  # activity log / dashboard, never a toast
            toast = self._mount_toast(record)
            if toast is not None:
                mounted.append(toast)
        return mounted

    def _mount_toast(self, record) -> AlertToast | None:
        container = self._container
        if container is None:
            return None
        host_width = getattr(getattr(container, "size", None), "width", 0) or getattr(
            self.app.size,
            "width",
            52,
        )
        toast = AlertToast(
            alert_id=getattr(record, "alert_id", None),
            severity=getattr(record, "severity", "warn"),
            message=getattr(record, "message", "")
            or getattr(record, "alert_type", ""),
            show_action_hint=self.bind_a,
            width_chars=_alert_toast_width(host_width=host_width),
        )
        try:
            container.mount(toast)
        except Exception:  # noqa: BLE001
            return None
        self._toasts.append(toast)
        self._evict_old()
        return toast

    def _evict_old(self) -> None:
        while len(self._toasts) > self.max_visible:
            oldest = self._toasts.pop(0)
            try:
                oldest.action_dismiss_toast()
            except Exception:  # noqa: BLE001
                pass

    @property
    def visible_toasts(self) -> list[AlertToast]:
        """Return the currently-mounted, non-dismissed toasts."""
        live: list[AlertToast] = []
        for toast in self._toasts:
            try:
                if not toast.is_mounted:
                    continue
                if getattr(toast, "display", True) is False:
                    continue
                live.append(toast)
            except Exception:  # noqa: BLE001
                continue
        self._toasts = live
        return list(live)


def _style_toast_container(container: Container) -> None:
    try:
        container.styles.dock = "bottom"
        container.styles.width = "100%"
        container.styles.height = "auto"
        container.styles.max_height = 16
        container.styles.padding = (0, 1, 1, 1)
        container.styles.background = "transparent"
        container.styles.align_horizontal = "right"
    except Exception:  # noqa: BLE001
        pass


def _setup_alert_notifier(
    app: App,
    *,
    container: Container | None = None,
    bind_a: bool = True,
) -> AlertNotifier | None:
    """Attach an :class:`AlertNotifier` to ``app``."""
    existing = getattr(app, "_alert_notifier", None)
    if existing is not None:
        return existing
    config_path = getattr(app, "config_path", None)
    if config_path is None:
        return None
    if container is None:
        try:
            container = Container(id="alert-toasts")
            _style_toast_container(container)
            app.screen.mount(container)
        except Exception:  # noqa: BLE001
            return None
    notifier = AlertNotifier(app, config_path=config_path, bind_a=bind_a)
    notifier.attach(container)
    setattr(app, "_alert_notifier", notifier)
    setattr(app, "_alert_toasts_container", container)
    return notifier


def _resolve_palette_nav():
    """Preserve the legacy ``pollypm.cockpit_ui`` monkeypatch seam."""
    try:
        from pollypm import cockpit_ui
    except Exception:  # noqa: BLE001
        return _palette_nav
    nav = getattr(cockpit_ui, "_palette_nav", None)
    if callable(nav):
        return nav
    return _palette_nav


def _action_view_alerts(app: App) -> None:
    """Shared ``action_view_alerts`` body — jumps to Metrics."""
    _resolve_palette_nav()(app, "metrics")
