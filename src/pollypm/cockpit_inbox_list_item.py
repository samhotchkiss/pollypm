"""Inbox row ``ListItem`` widget used by the cockpit inbox.

Contract:
- Inputs: ``row`` (an :class:`InboxThreadRow`), ``is_unread`` (whether the
  message has been seen), and ``config_path`` (cockpit config path used by
  the row formatter for project-pin lookups).
- Outputs: a ``ListItem`` subclass that renders one inbox message row,
  carrying ``task_id`` / ``task_ref`` / ``row_ref`` / ``is_unread`` for
  the inbox app's selection & triage paths.
- Side effects: none beyond mounting a ``Static`` child; ``mark_read``
  flips classes / re-renders the body in place (no list reflow).
- Invariants: this module owns one widget class and nothing else; the
  inbox app handles list-level lifecycle.
- Allowed dependencies: Textual primitives, ``cockpit_inbox`` for the
  ``InboxThreadRow`` payload, ``rejection_feedback`` for the
  feedback-row class hook, and ``cockpit_ui`` via local imports (for
  the ``_format_inbox_thread_row`` / ``_is_plan_review_task`` /
  ``_triage_bucket`` helpers — still in the god-module while #1354
  unwinds, so we lazy-import to avoid the load-time cycle).
- Private: ``_InboxListItem`` is underscore-prefixed and re-exported via
  ``cockpit_ui`` for back-compat (see #1354).

Wedge of the cockpit_ui.py god-module split tracked by #1354.
"""

from __future__ import annotations

from textual.widgets import ListItem, Static

from pollypm.cockpit_inbox import InboxThreadRow
from pollypm.rejection_feedback import is_rejection_feedback_task


class _InboxListItem(ListItem):
    """One message in the inbox list — carries the task_id + unread flag."""

    def __init__(
        self,
        row: InboxThreadRow,
        *,
        is_unread: bool,
        config_path: object | None = None,
    ) -> None:
        # Local imports: ``_format_inbox_thread_row`` /
        # ``_is_plan_review_task`` / ``_triage_bucket`` still live in
        # ``cockpit_ui``. Importing at module load time would create a
        # cycle (cockpit_ui imports this module for the re-export shim).
        from pollypm.cockpit_ui import (
            _format_inbox_thread_row,
            _is_plan_review_task,
            _triage_bucket,
        )

        self.row_ref = row
        self.task_id = row.task_id
        self.task_ref = row.task
        self.is_unread = is_unread
        self._config_path = config_path
        row_classes = "inbox-row reply-row" if row.is_reply else "inbox-row"
        # Plan-review approval rows get a distinct class so the CSS can
        # render a heavier border / background — these are decision
        # cards, not informational pings (#1400).
        self._is_plan_review = row.is_task and _is_plan_review_task(row.task)
        if self._is_plan_review:
            row_classes = f"{row_classes} plan-review-row"
        self._body = Static(
            _format_inbox_thread_row(
                row,
                is_unread=is_unread,
                config_path=config_path,
                show_judgment_calls=False,
            ),
            markup=False,
        )
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
        from pollypm.cockpit_ui import _format_inbox_thread_row

        if self.is_unread is False:
            return
        self.is_unread = False
        self.remove_class("unread")
        if row is not None:
            self.row_ref = row
            self.task_ref = row.task
        self._body.update(
            _format_inbox_thread_row(
                self.row_ref,
                is_unread=False,
                config_path=self._config_path,
            )
        )

    def set_show_judgment_calls(self, show: bool) -> None:
        """Re-render the plan_review row with judgment calls toggled.

        No-op on non-plan_review rows — the regular inbox row renderer
        ignores the flag so calling this on every selection is safe and
        keeps the rendering pipeline uniform across row kinds.
        """
        from pollypm.cockpit_ui import _format_inbox_thread_row

        if not self._is_plan_review:
            return
        self._body.update(
            _format_inbox_thread_row(
                self.row_ref,
                is_unread=self.is_unread,
                config_path=self._config_path,
                show_judgment_calls=show,
            )
        )


__all__ = ["_InboxListItem"]
