"""Rail-aware ``ListView`` subclass used by the cockpit navigation rail.

Contract:
- Inputs: standard Textual ``ListView`` construction (``id``, classes, etc.);
  the override consumes ``ListItem._ChildClicked`` messages that arrive
  via Textual's message dispatch when a row inside the rail is clicked.
- Outputs: posts a standard ``ListView.Selected`` message resolved against
  the live ``_nodes`` list. Click events that cannot be re-resolved are
  swallowed (no crash, no traceback overlay).
- Side effects: focuses the view and updates ``self.index`` when a live
  row is resolved. Otherwise no observable side effect.
- Invariants: this module owns one widget class and nothing else; rail
  composition / row construction lives in ``cockpit_ui``.
- Allowed dependencies: Textual primitives only.
- Private: ``_RailListView`` is underscore-prefixed and re-exported via
  ``cockpit_ui`` for back-compat (see #1354).

Wedge of the cockpit_ui.py god-module split tracked by #1354.
"""

from __future__ import annotations

from textual.widgets import ListItem, ListView


class _RailListView(ListView):
    """Rail-aware ``ListView`` that survives mid-dispatch re-renders.

    Textual's stock ``_on_list_item__child_clicked`` calls
    ``self._nodes.index(event.item)`` on the clicked widget. The rail
    rebuilds its row widgets (``self.nav.clear()`` + ``extend(rows)``)
    every tick that ``keys != list(self._row_widgets)``; a click event
    queued just before that rebuild lands on a widget that is no longer
    in ``_nodes`` and Textual raises ``ValueError: x not in list``,
    swallowing the click and surfacing a traceback in the rail's
    scrollback (#964 boot symptom).

    The defensive override re-resolves the click against the live
    ``_nodes`` list. When the original widget is gone we fall back to
    the cockpit_key recorded on the orphan ``RailItem`` to find the
    matching live row, then post the standard ``Selected`` message so
    downstream handlers see a clean event. If everything fails we
    swallow rather than raise — the rail re-renders next tick and the
    user simply re-clicks; raising would leave a traceback overlay
    obscuring the actual rail and there's no useful recovery for the
    user to take.
    """

    def _on_list_item__child_clicked(  # type: ignore[override]
        self, event: "ListItem._ChildClicked"
    ) -> None:
        # Textual's message dispatch walks the full MRO and invokes
        # every matching ``_on_<message>`` method it finds. To prevent
        # the parent ``ListView._on_list_item__child_clicked`` from
        # also running (and re-raising the very ``ValueError`` we are
        # guarding against), call ``prevent_default()`` so the
        # ``_get_dispatch_methods`` loop breaks before it reaches the
        # parent class. Without this our handler would only catch the
        # error half the time — and the unguarded parent run still
        # surfaces the traceback in the rail.
        event.prevent_default()
        event.stop()
        self.focus()
        clicked = event.item
        try:
            new_index = self._nodes.index(clicked)
        except ValueError:
            # Re-resolve by stable key when the clicked widget has been
            # swapped out by an in-flight rail rebuild.
            target_key = getattr(clicked, "cockpit_key", None)
            new_index = None
            replacement = clicked
            if target_key is not None:
                for idx, candidate in enumerate(self._nodes):
                    if getattr(candidate, "cockpit_key", None) == target_key:
                        new_index = idx
                        replacement = candidate
                        break
            if new_index is None:
                # Nothing actionable — drop the click rather than crash.
                return
            clicked = replacement
        self.index = new_index
        self.post_message(self.Selected(self, clicked, new_index))


__all__ = ["_RailListView"]
