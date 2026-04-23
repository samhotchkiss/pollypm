"""Tests for StructuredUserMessage (#760)."""

from __future__ import annotations

from pollypm.structured_message import StructuredUserMessage


def test_render_cli_minimal_summary_only() -> None:
    msg = StructuredUserMessage(summary="Cannot start.")
    rendered = msg.render_cli()
    assert rendered.startswith("✗ Cannot start.")
    # Single-line message has no extra paragraph breaks.
    assert "\n\n" not in rendered.rstrip()


def test_render_cli_full_shape_produces_all_sections() -> None:
    msg = StructuredUserMessage(
        summary="Cannot start — 1 pending migration.",
        why="PollyPM was upgraded and the database needs an update first.",
        next_action="pm migrate --apply",
        details="DB: /tmp/state.db",
    )
    rendered = msg.render_cli(show_details=True)
    assert "✗ Cannot start — 1 pending migration." in rendered
    assert "PollyPM was upgraded" in rendered
    assert "Next: pm migrate --apply" in rendered
    assert "/tmp/state.db" in rendered


def test_render_cli_collapses_details_by_default() -> None:
    msg = StructuredUserMessage(
        summary="Failure.",
        details="DB: /tmp/x\n+schema_migrations",
    )
    rendered = msg.render_cli()
    # The hint to expand is visible, but the raw details are not.
    assert "> details" in rendered
    assert "/tmp/x" not in rendered


def test_render_cli_expands_details_when_requested() -> None:
    msg = StructuredUserMessage(
        summary="Failure.",
        details="DB: /tmp/x",
    )
    rendered = msg.render_cli(show_details=True)
    assert "/tmp/x" in rendered
    assert "> details" not in rendered


def test_render_cli_custom_icon() -> None:
    msg = StructuredUserMessage(summary="Upgrade available.")
    rendered = msg.render_cli(icon="↑")
    assert rendered.startswith("↑ Upgrade available.")


def test_render_cli_no_icon_when_empty() -> None:
    msg = StructuredUserMessage(summary="Upgrade available.")
    rendered = msg.render_cli(icon="")
    assert rendered.startswith("Upgrade available.")


def test_render_cli_wraps_why_paragraph() -> None:
    msg = StructuredUserMessage(
        summary="Fail.",
        why=(
            "A long-ish reason that should wrap across multiple lines "
            "when the terminal is narrow enough to exercise the wrap "
            "path in the renderer."
        ),
    )
    rendered = msg.render_cli(wrap=40)
    why_block = rendered.split("Fail.\n\n", 1)[1].split("\n\n", 1)[0]
    # Line lengths all ≤ 40 chars (wrapping did fire).
    assert all(len(line) <= 40 for line in why_block.splitlines()), (
        f"lines longer than 40: {why_block!r}"
    )


def test_render_cli_preserves_preformatted_details() -> None:
    """Indented details (code blocks / lists) must not be reflowed."""
    msg = StructuredUserMessage(
        summary="Pending.",
        details="DB: /tmp/state.db\n  [work] v5: hot-path indexes\n  [state] v7: alerts",
    )
    rendered = msg.render_cli(show_details=True, wrap=40)
    # Each indented bullet stays on its own line.
    assert "  [work] v5: hot-path indexes" in rendered
    assert "  [state] v7: alerts" in rendered


def test_render_cli_renders_suggested_actions_as_options_block() -> None:
    """Callers that populate ``suggested_actions`` get a labeled
    Options block with the copy-pasteable command beneath each label.
    This is the UX used by multi-choice recoveries (e.g. pick one of
    uv / pip / brew / npm for an upgrade)."""
    msg = StructuredUserMessage(
        summary="Could not detect installer.",
        why="PollyPM isn't sure how you installed it.",
        next_action="Run the command for whichever tool you used.",
        suggested_actions=(
            ("uv", "uv tool upgrade pollypm"),
            ("pip", "pip install -U pollypm"),
            ("brew", "brew upgrade pollypm"),
            ("npm", "npm update -g pollypm"),
        ),
    )
    rendered = msg.render_cli()
    assert "Options:" in rendered
    # Labels + commands both present, with commands visibly indented so
    # the user's eye lands on them as the action.
    for label, command in [
        ("- uv", "uv tool upgrade pollypm"),
        ("- pip", "pip install -U pollypm"),
        ("- brew", "brew upgrade pollypm"),
        ("- npm", "npm update -g pollypm"),
    ]:
        assert label in rendered
        assert command in rendered
    # Options block sits after Next: and before the (absent) details hint.
    next_pos = rendered.index("Next:")
    opts_pos = rendered.index("Options:")
    assert next_pos < opts_pos


def test_render_cli_skips_options_when_actions_empty() -> None:
    msg = StructuredUserMessage(
        summary="Nothing to choose.",
        suggested_actions=(("", ""),),  # empty label + command
    )
    rendered = msg.render_cli()
    assert "Options:" not in rendered
