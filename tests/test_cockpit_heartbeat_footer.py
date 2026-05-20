"""Tests for the rail footer's heartbeat-offline hint formatting.

The hint sits in the 30-col cockpit rail footer; live capture on
2026-05-20 showed it wrapping to two lines (``⚠ Heartbeat offline
(1568m) — open Settings to repair recovery``) and reading as hostile
UX once the minute count climbed into the thousands. This test locks
in the compacted format.
"""

from __future__ import annotations

from pollypm.cockpit_ui import PollyCockpitApp


def test_format_offline_hint_under_60min_keeps_minute_count() -> None:
    # 5 minutes since last heartbeat — still in the actionable window,
    # so the magnitude is useful to the operator.
    out = PollyCockpitApp._format_heartbeat_offline_hint(5 * 60)
    assert out == "⚠ Heartbeat offline (5m) · open Settings"


def test_format_offline_hint_just_below_60_min_keeps_count() -> None:
    out = PollyCockpitApp._format_heartbeat_offline_hint(59 * 60 + 30)
    assert out == "⚠ Heartbeat offline (59m) · open Settings"


def test_format_offline_hint_at_60_min_drops_count() -> None:
    out = PollyCockpitApp._format_heartbeat_offline_hint(60 * 60)
    assert out == "⚠ Heartbeat offline · open Settings"


def test_format_offline_hint_hours_long_drops_count() -> None:
    # Live capture had 1568m (~26 hrs). That precise number adds nothing.
    out = PollyCockpitApp._format_heartbeat_offline_hint(1568 * 60)
    assert out == "⚠ Heartbeat offline · open Settings"


def test_format_offline_hint_is_shorter_than_legacy() -> None:
    """The audit-time string was 53 chars (``⚠ Heartbeat offline (1568m)
    — open Settings to repair recovery``) which wrapped to 3 lines on
    the 30-col rail. The hours-old form must be materially shorter so
    the wrap is 1-2 lines, not 3.
    """
    legacy = "⚠ Heartbeat offline (1568m) — open Settings to repair recovery"
    long = PollyCockpitApp._format_heartbeat_offline_hint(1568 * 60)
    # Loose bound — the goal is "noticeably shorter so it wraps
    # less", not a strict width budget. 1568m → 53 chars, post-fix → 35.
    assert len(long) < len(legacy) - 10, (
        f"long form {len(long)} chars vs legacy {len(legacy)}: {long!r}"
    )


def test_ticker_suppression_includes_heartbeat_error() -> None:
    """The cockpit ticker must suppress ``heartbeat_error`` events so
    they don't double-up with the stale-heartbeat footer hint.
    """
    assert "heartbeat_error" in PollyCockpitApp._TICKER_SUPPRESSED_EVENT_TYPES


def test_rail_ticker_suppression_includes_heartbeat_error() -> None:
    """The headless ``pm rail`` ticker mirrors the cockpit suppression
    list. Both must agree so detached operators see the same UX as
    attached ones.
    """
    from pollypm.cockpit_rail import PollyCockpitRail

    assert "heartbeat_error" in PollyCockpitRail._TICKER_SUPPRESSED_EVENT_TYPES
