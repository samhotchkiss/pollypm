"""Rail alert-detail modal — read + recover surface for ``♡⚠`` (#989).

Contract:
- Inputs: ``title`` / ``severity`` / ``meta`` / ``message`` strings plus
  a list of :class:`~pollypm.cockpit_alert_actions.AlertActionPlan`
  describing the recovery actions for the alert under the rail cursor.
- Outputs: a ``ModalScreen[str | None]`` that resolves to the action
  ``kind`` string the user picked (or ``None`` on dismiss).
- Side effects: pushes/dismisses a Textual modal; no I/O of its own.
  The host ``App`` runs the chosen action — this modal only renders +
  collects.
- Invariants: this module owns one widget class and nothing else; it
  does not depend on cockpit state, services, or the rail.
- Allowed dependencies: Textual primitives plus
  ``pollypm.cockpit_theme.State`` for semantic palette values; no
  cockpit state, services, or rail dependencies.
- Private: ``_AlertDetailModal`` is underscore-prefixed and re-exported
  via ``cockpit_ui`` for back-compat (see #1354).

Wedge of the cockpit_ui.py god-module split tracked by #1354.

Why a dedicated modal and not the existing Metrics drill-down: the
drill-down is a generic table-of-rows surface. The follow-up comment
on #989 wanted a one-keystroke recovery path scoped to the alert that
is actually under the cursor. Routing through Metrics works for "see
the alert list" but loses the rail-row context the moment the user
lands there.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import ListItem, ListView, Static

from pollypm.cockpit_theme import State


def _alert_detail_css_with_palette(raw_css: str) -> str:
    """Substitute canonical ``cockpit_theme.State.*`` hex values into the
    alert-detail modal CSS so a palette tweak in ``cockpit_theme``
    propagates here without a manual edit (#1988).

    Mirrors the ``_dashboard_css_with_palette`` pattern in ``cockpit_ui``:
    using ``str.replace`` (rather than an f-string CSS) keeps the source
    CSS readable since Textual rule blocks use ``{ ... }`` everywhere,
    which would force escaping every brace in an f-string. The
    substitution is byte-identical today (each replaced hex matches the
    corresponding ``State.*`` literal), so this is a source-level rename,
    not a redesign.

    What stays literal: the modal dialog ``background`` (``#141a20``) and
    the list-item highlight ``background`` (``#1f4d7a``). Neither has an
    analog elsewhere in the cockpit; promoting them would require
    inventing single-use ``State`` constants and is deferred to a
    follow-up. See TODO comments in the raw CSS below.
    """
    palette = (
        ("#ff5f6d", State.BLOCKED),
        ("#f0c45a", State.WAITING),
        ("#97a6b2", State.NEUTRAL),
        ("#d6dee5", State.BODY_BRIGHT),
        ("#eef6ff", State.HEADING_BRIGHT),
        ("#6b7a88", State.MUTED),
    )
    result = raw_css
    for old, new in palette:
        result = result.replace(old, new)
    return result


class _AlertDetailModal(ModalScreen[str | None]):
    """Read + recover surface for the rail's ``♡⚠`` badge (#989).

    Inputs: a title / meta / message string plus a list of
    :class:`~pollypm.cockpit_alert_actions.AlertActionPlan` describing
    the recovery actions for the alert under the rail cursor. Outputs:
    the action ``kind`` string the user picked (or ``None`` on dismiss).
    The host ``App`` runs the action — this modal only renders + collects.

    Why a dedicated modal and not the existing Metrics drill-down: the
    drill-down is a generic table-of-rows surface. The follow-up comment
    on #989 wanted a one-keystroke recovery path scoped to the alert
    that is actually under the cursor. Routing through Metrics works
    for "see the alert list" but loses the rail-row context the moment
    the user lands there.
    """

    # Modal CSS — routed through ``cockpit_theme.State`` via
    # ``_alert_detail_css_with_palette`` so the alert/warn semantic colors
    # stay in lockstep with the rest of the cockpit (#1988).
    DEFAULT_CSS = _alert_detail_css_with_palette("""
    _AlertDetailModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.45);
    }
    #alert-detail-dialog {
        width: 76;
        max-width: 95%;
        height: auto;
        max-height: 22;
        padding: 1 2;
        /* TODO(#1988): modal dialog panel background — no analog in
           ``cockpit_theme.State`` yet (single-use surface fill). Promote
           to ``State.SURFACE_MODAL_BG`` (or similar) in a follow-up when
           another modal needs the same fill. */
        background: #141a20;
        border: round #ff5f6d;
    }
    #alert-detail-dialog.warn {
        border: round #f0c45a;
    }
    #alert-detail-title {
        text-style: bold;
        color: #ff5f6d;
    }
    #alert-detail-title.warn {
        color: #f0c45a;
    }
    #alert-detail-meta {
        color: #97a6b2;
        margin-bottom: 1;
    }
    #alert-detail-message {
        color: #d6dee5;
        margin-bottom: 1;
        height: auto;
        max-height: 8;
        scrollbar-size: 1 1;
    }
    #alert-detail-actions {
        height: auto;
        margin-top: 1;
    }
    #alert-detail-actions ListItem {
        padding: 0 1;
    }
    #alert-detail-actions ListItem.-highlight {
        /* TODO(#1988): list-item selection highlight — no analog in
           ``cockpit_theme.State`` yet (single-use highlight fill).
           Promote to ``State.SURFACE_HIGHLIGHT_BG`` (or similar) in a
           follow-up when another ListView needs the same selection
           treatment. */
        background: #1f4d7a;
        color: #eef6ff;
    }
    #alert-detail-hint {
        color: #6b7a88;
        margin-top: 1;
    }
    """)

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Close"),
        Binding("q", "dismiss_modal", "Close", show=False),
        Binding("enter", "select_action", "Run"),
        Binding("j,down", "cursor_down", "Down", show=False),
        Binding("k,up", "cursor_up", "Up", show=False),
        Binding("1", "pick_index('0')", "1", show=False),
        Binding("2", "pick_index('1')", "2", show=False),
        Binding("3", "pick_index('2')", "3", show=False),
    ]

    def __init__(
        self,
        *,
        title: str,
        severity: str | None,
        meta: str,
        message: str,
        action_plans: list,
    ) -> None:
        super().__init__()
        self._title = title
        self._severity = severity or "error"
        self._meta = meta
        self._message = message
        self._action_plans = list(action_plans)

    def compose(self) -> ComposeResult:  # pragma: no cover - Textual harness
        warn_class = "warn" if self._severity == "warn" else ""
        with Vertical(id="alert-detail-dialog", classes=warn_class):
            yield Static(
                self._title,
                id="alert-detail-title",
                classes=warn_class,
                markup=False,
            )
            yield Static(self._meta, id="alert-detail-meta", markup=False)
            yield VerticalScroll(
                Static(self._message, markup=False),
                id="alert-detail-message",
            )
            yield ListView(id="alert-detail-actions")
            yield Static(
                "1/2/3 quick-pick · ↵ run · esc close",
                id="alert-detail-hint",
                markup=False,
            )

    def on_mount(self) -> None:  # pragma: no cover - Textual harness
        try:
            list_view = self.query_one("#alert-detail-actions", ListView)
        except Exception:  # noqa: BLE001
            return
        for index, plan in enumerate(self._action_plans):
            label = plan.label
            if index < 9:
                label = f"[{index + 1}] {label}"
            if plan.hint:
                label = f"{label} — {plan.hint}"
            try:
                list_view.append(ListItem(Static(label, markup=False)))
            except Exception:  # noqa: BLE001
                continue
        try:
            list_view.focus()
            if self._action_plans:
                list_view.index = 0
        except Exception:  # noqa: BLE001
            pass

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    def action_select_action(self) -> None:
        try:
            list_view = self.query_one("#alert-detail-actions", ListView)
        except Exception:  # noqa: BLE001
            self.dismiss(None)
            return
        index = list_view.index or 0
        if 0 <= index < len(self._action_plans):
            self.dismiss(self._action_plans[index].kind)
        else:
            self.dismiss(None)

    def action_cursor_down(self) -> None:
        try:
            list_view = self.query_one("#alert-detail-actions", ListView)
        except Exception:  # noqa: BLE001
            return
        if list_view.index is None:
            list_view.index = 0
        elif list_view.index < len(self._action_plans) - 1:
            list_view.index += 1

    def action_cursor_up(self) -> None:
        try:
            list_view = self.query_one("#alert-detail-actions", ListView)
        except Exception:  # noqa: BLE001
            return
        if list_view.index is None:
            list_view.index = 0
        elif list_view.index > 0:
            list_view.index -= 1

    def action_pick_index(self, raw: str) -> None:
        try:
            index = int(raw)
        except (TypeError, ValueError):
            return
        if 0 <= index < len(self._action_plans):
            self.dismiss(self._action_plans[index].kind)


__all__ = ["_AlertDetailModal"]
