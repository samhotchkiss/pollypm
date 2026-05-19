"""Rail-row ``ListItem`` widget used by the cockpit navigation rail.

Contract:
- Inputs: a ``CockpitItem`` payload (label/state/key/alert metadata) plus
  ``active_view`` / ``first_project`` flags and an optional
  ``CockpitPresence`` for heartbeat / working-spinner frames.
- Outputs: a ``ListItem`` subclass that renders one row in the rail's
  ``ListView``, with CSS classes that mark its kind (inbox, project,
  needs-user, etc.) and a body ``Static`` showing label + indicator +
  optional wrapped alert subtitle.
- Side effects: none beyond mounting and updating its inner ``Static``;
  click/selection wiring is owned by ``_RailListView`` / the cockpit app.
- Invariants: this module owns one widget class and nothing else; rail
  composition / row construction / state-provider plumbing lives in
  ``cockpit_ui``. The alert-subtitle helpers
  (``_wrap_alert_reason`` / ``_rail_alert_subtitle_width``) are still
  owned by ``cockpit_ui`` and imported lazily from inside
  ``update_body`` to avoid an import cycle while ``cockpit_ui`` imports
  this module for the re-export shim — same pattern as
  ``cockpit_inbox_rollup_item``.
- Allowed dependencies: Textual + Rich primitives, plus
  ``CockpitItem`` / ``CockpitPresence`` /
  ``_alert_type_is_user_action_waiting`` / ``_strip_trailing_spark``
  from ``pollypm.cockpit_rail``.
- Private: ``RailItem`` is re-exported via ``cockpit_ui`` for back-compat
  (see #1354).

Wedge of the cockpit_ui.py god-module split tracked by #1354.
"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import ListItem, Static

from pollypm.cockpit_rail import (
    CockpitItem,
    CockpitPresence,
    _alert_type_is_user_action_waiting,
)


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
            "needs-user-warn",
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
            # #989 — Differentiate warn (amber, one click to fix) from
            # error (red, account repair / restart). The base
            # ``needs-user`` class still applies for downstream
            # consumers that don't care about severity.
            self.add_class("needs-user")
            if item.alert_severity == "warn":
                self.add_class("needs-user-warn")
        if (item.state.endswith("live") or item.state.endswith("working")) and item.key in ("polly", "russell"):
            self.add_class("live")
        if active_view:
            self.add_class("active-view")
        self.update_body()

    def update_body(self) -> None:
        # Local import: the alert-subtitle helpers still live in
        # ``cockpit_ui`` and importing them at module load time would
        # create a cycle (``cockpit_ui`` imports this module for the
        # re-export shim). Same lazy-import pattern as
        # ``cockpit_inbox_rollup_item._RollupItem._build_text``.
        from pollypm.cockpit_ui import _rail_alert_subtitle_width, _wrap_alert_reason

        text = Text()
        if self.has_class("active-view"):
            text.append("▌ ", style="#5b8aff")
        else:
            text.append("  ")
        indicator, indicator_style = self._indicator()
        if indicator:
            text.append(f"{indicator} ", style=indicator_style)
        else:
            text.append("  ")
        label = self.item.label
        if self.item.key.startswith("project:"):
            # The router keeps a 10-column activity sparkline on project
            # labels for sorting/data consumers. In the 30-column rail it
            # reads like corruption and crowds the project name, so render
            # the name/pin only here.
            from pollypm.cockpit_rail import _strip_trailing_spark

            label = _strip_trailing_spark(label)[0]
        max_label = 22  # 30 col pane - 2 prefix - 2 indicator - 2 padding
        if len(label) > max_label:
            label = label[: max_label - 1] + "…"
        text.append(label)
        # Show alert reason as dim subtitle for items with alerts.
        # Wrap onto up to 3 lines (≈60 chars each, indented) instead of
        # truncating at 18 chars — Sam on 2026-04-20 reported rail
        # toasts cut off mid-word. The indent keeps them visually
        # attached to the owning item while still readable.
        if self.item.state.startswith("!"):
            reason = self.item.state[2:].strip()  # strip "! " prefix
            if reason:
                # #989 — Dim subtitle picks up severity tint so the
                # subtitle reads as the same alert as the row badge.
                subtitle_style = (
                    "#f0c45a dim"
                    if self.item.alert_severity == "warn"
                    else "#ff5f6d dim"
                )
                for chunk in _wrap_alert_reason(
                    reason,
                    width=_rail_alert_subtitle_width(),
                    max_lines=4,
                ):
                    text.append(f"\n    {chunk}", style=subtitle_style)
        self.body.update(text)

    def _indicator(self) -> tuple[str, str]:
        presence = self.presence
        # #989 — Severity drives the badge color so warn (amber) and
        # error (red) read as different states even when the row label
        # / state string are identical.
        alert_color = (
            "#f0c45a" if self.item.alert_severity == "warn" else "#ff5f6d"
        )
        if self.item.key.startswith("project:"):
            # #1390 — Approval-pending takes precedence over the rollup
            # color so a project parked at user_approval reads as "act
            # on me" even if it was otherwise GREEN/YELLOW. Skip RED so
            # the operational-fault triangle keeps its dedicated slot.
            if (
                getattr(self.item, "approvals_pending", 0) > 0
                and self.item.state != "project-red"
            ):
                return "▶", "#ff5f6d"
            if self.item.state == "project-red":
                return "▲", alert_color
            # #1520 — A "Waiting on you:" alert family on the project's
            # sessions (plan_missing, worker_question, recovery_limit,
            # auth_broken, stuck_on_task:, no_session_for_assignment:,
            # etc.) outranks both the project-rollup glyphs *and* the
            # heartbeat ♥·/♡· pair below. The dashboard banner already
            # says "Waiting on you:" for these; the rail must mirror
            # that signal so the user can see at a glance which projects
            # owe them a decision. Same ◆ glyph the dashboard pill uses
            # for "needs attention". Operational alerts (``pane:*``)
            # are filtered out by ``_alert_type_is_user_action_waiting``
            # — the worker's stuck ⚠ glyph already covers them.
            if _alert_type_is_user_action_waiting(
                getattr(self.item, "alert_type", None)
            ):
                return "◆", "#f0a030"
            if self.item.state == "project-yellow":
                # #1092 — use ◆ to match the dashboard's "needs attention"
                # diamond. ``•`` and the idle ``·`` are visually
                # indistinguishable in many terminal fonts, so a project
                # with held tasks read as idle in the rail.
                return "◆", "#f0a030"
            if self.item.state == "project-green":
                return "•", "#3ddc84"
            if self.item.state == "project-working":
                return "•", "#f0c45a"
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
        # Alerts (red triangle / amber for warn-tier — #989)
        if self.item.state.startswith("!"):
            return "▲", alert_color
        # Separator
        if self.item.state == "separator":
            return "", "#4a5568"
        # Top-level agents (Polly, Russell)
        if self.item.key in ("polly", "russell"):
            if self.item.state.endswith("working"):
                return self.item.state.split(" ", 1)[0], "#3ddc84"  # green spinner
            if self.item.state in {"ready", "idle"}:
                return "•", "#5b8aff"  # blue dot
            return "•", "#5b8aff"
        # Inbox
        if self.item.key == "inbox":
            label = self.item.label
            if "(" in label and not label.endswith("(0)"):
                return "◆", "#f0c45a"  # yellow diamond
            return "◇", "#4a5568"
        # Settings
        if self.item.key == "settings":
            return "⚙", "#6b7a88"
        # Sub-items
        if self.item.state == "sub":
            return " ", "#4a5568"
        # Projects keep their status marker quieter than global rows.
        # A full orange unread dot or animated working spinner makes
        # ordinary project rows compete with true alert/action rows in the
        # narrow rail.
        if self.item.key.startswith("project:"):
            if self.item.state == "unread":
                return "•", "#f0a030"
            if "working" in self.item.state:
                return "•", "#f0c45a"
            return "○", "#4a5568"  # dim circle — idle
        # Unread
        if self.item.state == "unread":
            return "●", "#f0a030"  # orange dot
        # Generic "<glyph> working" state — top-level rail rows
        # (e.g. Workers when any worker is currently turning) used
        # to fall through to the idle circle below, so a
        # ``◆ working`` state from the state-provider was
        # cosmetically indistinguishable from idle. Honour the
        # state with the spinner / active diamond.
        if self.item.state.endswith("working"):
            if presence is not None:
                return presence.working_frame(self.spinner_index), "#3ddc84"
            return "◆", "#f0c45a"
        return "○", "#4a5568"

    def _session_work_glyph(self, work_state: str) -> tuple[str, str]:
        presence = self.presence
        if work_state == "writing":
            if presence is not None:
                if not presence.should_animate():
                    return "…", "#3ddc84"
                return presence.working_frame(self.spinner_index), "#3ddc84"
            return "◆", "#3ddc84"
        if work_state == "reviewing":
            return "✎", "#3ddc84"
        if work_state == "stuck":
            # #989 — Pick amber for warn-tier alerts so the user can
            # distinguish "answer the prompt" from "account repair".
            color = "#f0c45a" if self.item.alert_severity == "warn" else "#ff5f6d"
            return "⚠", color
        if work_state == "exited":
            return "✕", "#4a5568"
        return "·", "#4a5568"


__all__ = ["RailItem"]
