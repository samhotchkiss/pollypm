"""#1832 — route timing logs identify which tmux sub-step blocked the click.

The cockpit's static-route worker dispatches one or more tmux subprocess
calls (``list_panes``, ``try_show_static_fast``, ``show_static``) on its
way to repainting the right pane. When a tmux server is sluggish, one
of those calls can consume the bulk of the click-to-paint budget.

To make a wedged subprocess identifiable from logs without enabling
debug tracing, :class:`pollypm.cockpit_rail.CockpitRouter` wraps each
sub-step in ``_maybe_log_route_step``. The probe emits a WARNING when a
step exceeds :attr:`_ROUTE_TIMING_WARN_SECONDS` and stays silent below
that threshold so quiet, healthy clicks don't pollute the log.

These tests verify both halves of the contract.
"""

from __future__ import annotations

import logging
import time

from pollypm.cockpit_rail import CockpitRouter


class _BareRouter(CockpitRouter):
    """Construct a router without touching disk / config / tmux.

    The timing probe lives on the class and only reads
    ``self._ROUTE_TIMING_WARN_SECONDS`` plus the elapsed monotonic time
    captured by the caller — so a minimally-initialised instance is
    enough to exercise it.
    """

    def __init__(self) -> None:  # noqa: D401
        pass


def test_route_timing_log_silent_below_threshold(
    caplog: logging.LogCaptureFixture,
) -> None:
    """Fast clicks must not emit timing-log noise.

    Regression #1832 — the threshold exists so healthy static clicks
    (which complete in <50ms) leave no trace in the log. Only a
    sluggish tmux subprocess earns a WARNING.
    """
    router = _BareRouter()
    caplog.set_level(logging.WARNING, logger="pollypm.cockpit_rail")
    # Pretend the step just completed in 1ms — well below the 250ms
    # threshold.
    start = time.monotonic() - 0.001
    router._maybe_log_route_step("list_panes", "dashboard", start)
    assert not [
        rec for rec in caplog.records
        if "Cockpit static route step" in rec.getMessage()
    ]


def test_route_timing_log_fires_above_threshold(
    caplog: logging.LogCaptureFixture,
) -> None:
    """A slow tmux sub-step must surface in the log with step name +
    rail key + elapsed ms so the wedged call is identifiable from a
    post-hoc tail of ``~/.pollypm/audit/*.log``."""
    router = _BareRouter()
    caplog.set_level(logging.WARNING, logger="pollypm.cockpit_rail")
    # Simulate a step that took 500ms — twice the warn threshold.
    start = time.monotonic() - 0.5
    router._maybe_log_route_step("show_static", "operator", start)
    records = [
        rec for rec in caplog.records
        if "Cockpit static route step" in rec.getMessage()
    ]
    assert len(records) == 1
    msg = records[0].getMessage()
    assert "show_static" in msg
    assert "operator" in msg
    # Threshold is 250ms; the synthetic step is 500ms, so the elapsed
    # value formatted as ms must be present.
    assert "ms" in msg


def test_route_timing_threshold_is_user_visible_slack() -> None:
    """The threshold should be tight enough that a user-perceptible
    delay (>250ms) trips it, but loose enough that healthy clicks
    don't. Lock the value so a future refactor can't quietly raise it
    to multi-second territory and hide regressions."""
    assert CockpitRouter._ROUTE_TIMING_WARN_SECONDS <= 0.5
    assert CockpitRouter._ROUTE_TIMING_WARN_SECONDS >= 0.05
