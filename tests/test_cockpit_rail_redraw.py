"""Regression sentinels for the cockpit rail redraw recovery (#XXXX).

The cockpit's left rail can stale visibly when tmux drops a redraw
signal (focus loss, client reattach, SIGWINCH delivered to the wrong
pane). The user's documented workaround was detach + reattach; these
tests pin the auto-recovery so the workaround stays unnecessary.

Two recovery paths are pinned:

1. **Periodic stale-paint refresh** in ``_tick``: at the configured
   ``_STALE_PAINT_INTERVAL`` cadence, the rail App calls
   ``self.refresh(layout=False)`` defensively. This catches the case
   where Textual believes no widgets need a repaint but tmux has
   dropped the buffer.

2. **AppFocus handler**: when the tmux pane regains focus,
   ``on_app_focus`` fires the same recovery path as ``on_resize``
   (force a layout refresh + ``_recover_cockpit_render``). This catches
   the user-visible "switched panes and back, rail is blank" case.

Both are non-invasive — they pile on extra repaints, not rebuilds.
"""

from __future__ import annotations

import inspect

from pollypm.cockpit_ui import PollyCockpitApp


def test_stale_paint_interval_constant_present() -> None:
    """The cadence constant must exist so ``_tick`` can read it."""
    assert hasattr(PollyCockpitApp, "_STALE_PAINT_INTERVAL")
    interval = PollyCockpitApp._STALE_PAINT_INTERVAL
    assert isinstance(interval, int)
    # ~4s at the 0.8s tick rate gives a reasonable ceiling on
    # how long a staled rail can stay visibly blank.
    assert 1 <= interval <= 20, f"_STALE_PAINT_INTERVAL={interval} feels off"


def test_tick_calls_refresh_at_stale_paint_cadence() -> None:
    """The defensive refresh call must run at ``_STALE_PAINT_INTERVAL``."""
    interval = PollyCockpitApp._STALE_PAINT_INTERVAL
    # Inspect the source of ``_tick`` for the gated refresh call. Using
    # source inspection rather than a full Textual harness because
    # constructing a real PollyCockpitApp pulls in a config + supervisor
    # graph that's unreasonable for a unit test.
    src = inspect.getsource(PollyCockpitApp._tick)
    assert "_STALE_PAINT_INTERVAL" in src, (
        "_tick must reference _STALE_PAINT_INTERVAL — the defensive refresh "
        "is the entire point of this regression."
    )
    assert "self.refresh(" in src, (
        "_tick must call self.refresh() somewhere; without it the rail "
        "cannot recover from a dropped redraw signal."
    )


def test_on_app_focus_handler_exists() -> None:
    """AppFocus must trigger the same recovery path as Resize."""
    assert hasattr(PollyCockpitApp, "on_app_focus"), (
        "PollyCockpitApp must define on_app_focus to recover the rail "
        "when the tmux pane regains focus."
    )
    handler = PollyCockpitApp.on_app_focus
    sig = inspect.signature(handler)
    # (self, event) — accept the AppFocus event positionally.
    params = list(sig.parameters.values())
    assert len(params) >= 2, (
        f"on_app_focus must accept an event argument; got {sig}"
    )


def test_on_app_focus_calls_recovery_path() -> None:
    """The focus handler must funnel through the same recovery as resize."""
    src = inspect.getsource(PollyCockpitApp.on_app_focus)
    # The body should defer through ``call_after_refresh`` so the
    # recovery doesn't re-enter the event loop synchronously, and it
    # should target ``_recover_after_resize`` (the existing function
    # that drives ``_recover_cockpit_render(force_render=True)``).
    assert "call_after_refresh" in src, (
        "on_app_focus should defer recovery via call_after_refresh, "
        "matching the on_resize pattern."
    )
    assert "_recover_after_resize" in src, (
        "on_app_focus should reuse _recover_after_resize so both paths "
        "share the same force-redraw recovery."
    )


def test_on_resize_handler_unchanged() -> None:
    """The pre-existing resize recovery path must still be wired."""
    assert hasattr(PollyCockpitApp, "on_resize")
    src = inspect.getsource(PollyCockpitApp.on_resize)
    assert "call_after_refresh" in src
    assert "_recover_after_resize" in src
