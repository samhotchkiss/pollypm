"""Cockpit alert *policy* helpers (data-layer only).

The visible toast surface that used to live in this module was removed
in #956 — alerts are still raised, persisted, and routed (``pm alerts``,
inbox listing, plugin notify adapters), but the cockpit no longer
mounts floating yellow toast cards on top of any pane. Everything in
this file is non-rendering policy: classification helpers used by
dashboards, the activity feed, signal routing, and tests to decide
which alerts are operational noise vs. user-actionable.

If/when toasts come back, rebuild a renderer in a fresh module and
keep these classifiers as the input contract — see #956 for context.
"""

from __future__ import annotations

import enum

from textual.app import App

from pollypm.cli_features.alerts import is_surfaceable_operational_alert
from pollypm.cockpit_palette import _palette_nav


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
    dashboard, and the cockpit alert list. Before this existed,
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


# ---------------------------------------------------------------------------
# Alert tier policy (#765, #956).
# ---------------------------------------------------------------------------
#
# Pre-#956 this enum drove a toast renderer. The renderer is gone, but the
# classification is still useful: signal routing, dashboards, and tests
# use it to decide which alerts are operational noise, which are
# informational (inbox-only), and which are action-required.

class AlertChannel(enum.Enum):
    """Where an alert is delivered.

    * :attr:`OPERATIONAL` — activity log only. Heartbeat classification
      events, supervisor self-recovery signals, plugin lifecycle.
      Useful for debugging and forensic scans; never interrupts.
    * :attr:`INFORMATIONAL` — activity log + inbox item. The user
      probably wants to know about it but doesn't have to act now
      (plan ready for review, worker completed task).
    * :attr:`ACTION_REQUIRED` — activity log + inbox. Reserved for cases
      the heartbeat / supervisor cannot handle on its own and where
      there's a concrete user action. Pre-#956 this tier also mounted
      a floating toast card; the toast surface is removed but the tier
      is still consumed by signal_routing tests + dashboards.
    """

    OPERATIONAL = "operational"
    INFORMATIONAL = "informational"
    ACTION_REQUIRED = "action_required"


# Alert types known to be informational rather than operational/action.
# Today this set is small — most real alerts are either operational
# (filtered above) or action-required (everything else). Surfacing this
# explicitly lets future plugins register their own informational
# alerts without retraining the whole pipeline.
_INFORMATIONAL_ALERT_TYPES: frozenset[str] = frozenset({
    # Plan-ready / worker-completed / first-shipped — already routed
    # via the inbox. Listed here so a registry-based router can opt
    # them out of the action-required tier explicitly instead of
    # relying on the absence of an operational match.
})


def alert_channel(alert_type: str) -> AlertChannel:
    """Classify an ``alert_type`` into a :class:`AlertChannel`.

    Used by :mod:`pollypm.signal_routing` and the dashboard data layer
    to decide which alerts surface on which panes. Operational alerts
    are still recorded on the activity log + cockpit alert list —
    they just don't escalate.
    """
    if is_operational_alert(alert_type):
        return AlertChannel.OPERATIONAL
    if alert_type in _INFORMATIONAL_ALERT_TYPES:
        return AlertChannel.INFORMATIONAL
    return AlertChannel.ACTION_REQUIRED


def alert_should_toast(alert_type: str) -> bool:
    """Return True for the legacy ACTION_REQUIRED tier.

    The toast renderer is gone (#956) so this function no longer drives
    any rendering, but :mod:`pollypm.signal_routing` re-exports it as
    part of the policy contract and downstream callers (and tests) use
    it as a synonym for "would have interrupted the user". Keeping the
    name avoids a churny rename across signal routing + the heartbeat
    plugin API; if/when toasts come back the renderer can consult this
    again unchanged.
    """
    return alert_channel(alert_type) is AlertChannel.ACTION_REQUIRED


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
    """Shared ``action_view_alerts`` body — jumps to Metrics.

    The toast surface is gone (#956) but the ``a`` keybinding still
    routes the user to the alert list (Metrics → Alerts drill-down)
    so the data-layer view stays one keystroke away.
    """
    _resolve_palette_nav()(app, "metrics")
