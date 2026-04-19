"""Tests for :mod:`pollypm.providers.claude.usage_parse` (Phase B of #397).

Run with::

    HOME=/tmp/pytest-providers-claude uv run --with pytest \\
        pytest tests/providers/test_claude_usage_parse.py -x

The parser is the single source of truth for Claude ``/usage`` pane
output; both the batch list (``pm accounts``) and the live tmux probe
call through here.
"""

from __future__ import annotations

from pollypm.providers.claude.usage_parse import (
    parse_claude_usage_snapshot,
    parse_claude_usage_text,
)


WEEKLY_SAMPLE = """
Status   Config   Usage   Stats

Current week (all models)
██████████▌                                        21% used
Resets Apr 10 at 1am (America/Denver)
"""


def test_parse_usage_text_returns_remaining_percent_and_reset() -> None:
    health, summary = parse_claude_usage_text(WEEKLY_SAMPLE)
    assert health == "healthy"
    assert summary == "79% left this week · resets Apr 10 at 1am (America/Denver)"


def test_parse_usage_text_returns_unknown_on_empty_input() -> None:
    health, summary = parse_claude_usage_text("")
    assert health == "unknown"
    assert summary == "usage unavailable"


def test_parse_usage_text_returns_unknown_on_unrelated_output() -> None:
    health, summary = parse_claude_usage_text("Totally unrelated CLI help text")
    assert health == "unknown"
    assert summary == "usage unavailable"


def test_parse_usage_snapshot_weekly_healthy() -> None:
    snapshot = parse_claude_usage_snapshot(WEEKLY_SAMPLE)
    assert snapshot.health == "healthy"
    assert "79% left this week" in snapshot.summary
    assert "Apr 10 at 1am" in snapshot.summary
    assert snapshot.raw_text == WEEKLY_SAMPLE


def test_parse_usage_snapshot_near_limit_above_eighty_percent() -> None:
    snapshot = parse_claude_usage_snapshot(
        "Current week (all models) 85% used Resets tomorrow"
    )
    assert snapshot.health == "near-limit"
    assert "15% left" in snapshot.summary


def test_parse_usage_snapshot_exhausted_above_ninety_five_percent() -> None:
    snapshot = parse_claude_usage_snapshot(
        "Current week (all models) 97% used Resets Friday"
    )
    assert snapshot.health == "exhausted"
    assert "3% left" in snapshot.summary


def test_parse_usage_snapshot_returns_raw_text_only_on_failure() -> None:
    snapshot = parse_claude_usage_snapshot("unrelated noise")
    assert snapshot.health == "unknown"
    assert snapshot.summary == "usage unavailable"
    assert snapshot.raw_text == "unrelated noise"


def test_legacy_accounts_shim_still_routes_to_new_parser() -> None:
    """The back-compat shim in :mod:`pollypm.accounts` must preserve behavior."""
    from pollypm.accounts import _parse_claude_usage_text

    assert _parse_claude_usage_text(WEEKLY_SAMPLE) == parse_claude_usage_text(
        WEEKLY_SAMPLE
    )
