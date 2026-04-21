"""Shared helpers for CLI help text."""

from __future__ import annotations

from collections.abc import Sequence

Example = tuple[str, str]


def examples_block(examples: Sequence[Example]) -> str:
    """Render a uniform ``Examples:`` block for Typer help text."""
    if not 2 <= len(examples) <= 3:
        raise ValueError("CLI help examples must contain 2-3 entries.")
    width = max(len(command) for command, _description in examples)
    lines = ["Examples:", ""]
    for command, description in examples:
        lines.append(f"• {command.ljust(width)}  — {description}")
    return "\n".join(lines)


def help_with_examples(
    summary: str,
    examples: Sequence[Example],
    *,
    trailing: str | None = None,
) -> str:
    """Append a uniform examples block to a summary paragraph."""
    parts = [summary.rstrip(), "", examples_block(examples)]
    if trailing:
        parts.extend(["", trailing.rstrip()])
    return "\n".join(parts)
