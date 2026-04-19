"""Tests for ``pollypm.providers.codex.usage_parse`` (Phase C of #397).

Run with::

    HOME=/tmp/pytest-providers-codex uv run --with pytest \\
        pytest tests/providers/test_codex_usage_parse.py -x

Covers the weekly-quota parser that reads the Codex ``/status`` pane
dump and returns ``(health, summary)``. The legacy test at
``tests/test_account_usage.py::test_parse_codex_status_text`` exercises
the back-compat shim; these tests pin the new module directly so the
shim can be removed later without losing coverage.
"""

from __future__ import annotations

from pollypm.providers.codex.usage_parse import parse_codex_status_text


def test_parse_codex_status_text_extracts_percent_left() -> None:
    text = (
        "› Implement {feature}\n"
        "\n"
        "  gpt-5.4 default · 100% left · /Users/sam/dev/pollypm\n"
    )

    assert parse_codex_status_text(text) == ("healthy", "100% left")


def test_parse_codex_status_text_keeps_single_digit_percent() -> None:
    text = "gpt-5.4 default · 7% left · /tmp/foo"

    assert parse_codex_status_text(text) == ("healthy", "7% left")


def test_parse_codex_status_text_flags_capacity_exhausted() -> None:
    text = "Codex error: usage limit reached. Please retry later."

    assert parse_codex_status_text(text) == (
        "capacity-exhausted",
        "usage limit reached",
    )


def test_parse_codex_status_text_capacity_exhausted_is_case_insensitive() -> None:
    text = "ERROR: USAGE LIMIT exceeded"

    health, summary = parse_codex_status_text(text)
    assert health == "capacity-exhausted"
    assert summary == "usage limit reached"


def test_parse_codex_status_text_returns_unknown_for_empty_pane() -> None:
    assert parse_codex_status_text("") == ("unknown", "usage unavailable")


def test_parse_codex_status_text_returns_unknown_when_no_marker() -> None:
    text = "Welcome to Codex. Type your prompt below.\n"

    assert parse_codex_status_text(text) == ("unknown", "usage unavailable")


def test_parse_codex_status_text_percent_left_beats_usage_limit_marker() -> None:
    """If Codex prints both markers, the fresh quota reading wins.

    Guards against a regression where a stale ``usage limit`` fragment
    elsewhere in the scrollback would override a live ``% left`` line.
    """
    text = "old: usage limit reached\nnew: 42% left"

    assert parse_codex_status_text(text) == ("healthy", "42% left")


def test_parse_codex_status_text_matches_legacy_shim() -> None:
    """The back-compat shim at ``pollypm.accounts._parse_codex_status_text``
    must return the same tuple so existing callers keep working."""
    from pollypm.accounts import _parse_codex_status_text as _legacy_shim

    text = "gpt-5.4 default · 33% left"
    assert _legacy_shim(text) == parse_codex_status_text(text)
