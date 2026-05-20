"""Tests for the unified cockpit footer status formatter.

Pin the format + per-state color cues + truncation rules so a future
caller can't accidentally break the operator-visible status line.
"""

from __future__ import annotations

import re

import pytest

from pollypm.cockpit_footer_status import render_footer_status
from pollypm.cockpit_theme import State


_MARKUP_RE = re.compile(r"\[/?[^\]]+\]")


def _plain(markup: str) -> str:
    """Strip Rich markup tags for length-budget assertions."""
    return _MARKUP_RE.sub("", markup)


def test_full_layout_includes_all_three_counts_and_alert() -> None:
    """At a comfortable width the formatter renders every chunk."""
    result = render_footer_status(
        project_count=12,
        agent_count=38,
        inbox_count=23,
        alert="heartbeat 26h offline",
        width=80,
    )
    plain = _plain(result)
    assert "12 projects" in plain
    assert "38 agents" in plain
    assert "23 inbox" in plain
    assert "heartbeat 26h offline" in plain
    assert "  |  " in plain  # major separator between counts + alert


def test_color_cues_match_state_constants() -> None:
    """Each chunk must be wrapped in the right ``State.*`` color tag."""
    result = render_footer_status(
        project_count=1,
        agent_count=2,
        inbox_count=5,
        alert="heartbeat offline",
        width=80,
    )
    # Counts ride on MUTED; non-zero inbox count flips to WAITING amber.
    assert f"[{State.MUTED}]1 project" in result
    assert f"[{State.MUTED}]2 agents" in result
    assert f"[{State.WAITING}]5 inbox" in result
    # Alert chunk wraps the glyph + text in BLOCKED red.
    assert f"[{State.BLOCKED}]⚠ heartbeat offline" in result
    # Separators are IDLE slate.
    assert f"[{State.IDLE}] · " in result


def test_inbox_zero_renders_muted_not_amber() -> None:
    """An empty inbox shouldn't pretend to need attention."""
    result = render_footer_status(
        project_count=5,
        agent_count=0,
        inbox_count=0,
        width=80,
    )
    assert f"[{State.MUTED}]0 inbox" in result
    assert f"[{State.WAITING}]0 inbox" not in result


def test_singular_label_for_one() -> None:
    """1 project / 1 agent should not pluralise to 'projects' / 'agents'."""
    result = render_footer_status(
        project_count=1,
        agent_count=1,
        inbox_count=1,
        width=80,
    )
    plain = _plain(result)
    assert "1 project " in plain or plain.endswith("1 project")
    assert "1 agent " in plain or "1 agent " in plain or "1 agent" in plain
    # ``inbox`` is its own plural — no ``inboxes``.
    assert "1 inbox" in plain
    assert "inboxes" not in plain


def test_no_alert_drops_separator_and_alert_chunk() -> None:
    """When alert is None / empty the major separator must not render."""
    result = render_footer_status(
        project_count=3,
        agent_count=4,
        inbox_count=0,
        width=80,
    )
    plain = _plain(result)
    assert "|" not in plain
    assert "⚠" not in plain


def test_compact_layout_drops_count_labels_to_make_room_for_alert() -> None:
    """When the alert needs space, counts collapse to bare digits joined
    by ``·`` (no spaces) instead of ``N projects · N agents``."""
    # Pick a width where the full count chunk + a long alert won't fit
    # but the compact form will.
    result = render_footer_status(
        project_count=12,
        agent_count=38,
        inbox_count=23,
        alert="heartbeat offline — open Settings",
        width=55,
    )
    plain = _plain(result)
    # Compact counts join with bare "·".
    assert "12·38·23" in plain
    # The alert still gets some text (truncated or whole, depending).
    assert "heartbeat" in plain


def test_alert_truncates_with_ellipsis_when_overlong() -> None:
    """A very long alert should ellipsis-tail-trim before falling off the
    rail rather than overflow the width budget."""
    long_alert = "heartbeat offline since cycle 26 — open Settings to repair"
    result = render_footer_status(
        project_count=0,
        agent_count=0,
        inbox_count=0,
        alert=long_alert,
        width=40,
    )
    plain = _plain(result)
    assert plain.endswith("…")
    assert "heartbeat" in plain
    # Plain rendering must still fit the width budget.
    assert len(plain) <= 40


def test_total_width_stays_under_budget() -> None:
    """Truncation guarantees the rendered plain text fits ``width``."""
    for w in (20, 32, 50, 80, 120):
        result = render_footer_status(
            project_count=99,
            agent_count=99,
            inbox_count=99,
            alert="heartbeat 999h offline since the dawn of time",
            width=w,
        )
        plain = _plain(result)
        assert len(plain) <= w, f"width={w}, len(plain)={len(plain)}, plain={plain!r}"


def test_zero_or_negative_width_returns_empty_string() -> None:
    assert render_footer_status(
        project_count=10, agent_count=10, inbox_count=10, alert="x", width=0,
    ) == ""
    assert render_footer_status(
        project_count=10, agent_count=10, inbox_count=10, width=-5,
    ) == ""


def test_alert_only_layout_when_counts_overflow() -> None:
    """When the counts themselves overflow ``width`` but an alert is set,
    the formatter prefers surfacing the alert over showing nothing —
    the heartbeat warning is the more actionable signal."""
    # Width chosen below ``11 (compact counts) + 5 (major sep) + 2 (glyph)``,
    # i.e. the compact-counts column itself can't make room for an alert
    # glyph + any text. The formatter must drop the counts entirely and
    # render only the alert.
    result = render_footer_status(
        project_count=999,
        agent_count=999,
        inbox_count=999,
        alert="heartbeat offline",
        width=15,
    )
    plain = _plain(result)
    # Alert glyph + at least the first word must survive.
    assert "⚠" in plain
    assert "heartbeat" in plain
    assert "999" not in plain  # counts dropped entirely
    assert len(plain) <= 15


@pytest.mark.parametrize(
    "project_count,agent_count,inbox_count",
    [(0, 0, 0), (1, 1, 1), (50, 100, 25), (999, 999, 999)],
)
def test_no_alert_renders_only_count_chunks(
    project_count: int, agent_count: int, inbox_count: int,
) -> None:
    result = render_footer_status(
        project_count=project_count,
        agent_count=agent_count,
        inbox_count=inbox_count,
        alert=None,
        width=80,
    )
    plain = _plain(result)
    assert str(project_count) in plain
    assert str(agent_count) in plain
    assert str(inbox_count) in plain
    assert "⚠" not in plain  # no alert glyph
    assert "|" not in plain
