"""Tests for CLI help examples.

Typer's rich renderer collapses whitespace in the epilog, so examples
are embedded in the top-level help text itself (bullet format, which
Typer preserves). This module asserts that every documented help
surface carries a literal ``Examples:`` block with 2-3 copy-paste
invocations.

It also exercises the new `pm help worker` meta-command.
"""

from __future__ import annotations

import pytest
import typer
from typer.testing import CliRunner

from pollypm.cli import app


@pytest.fixture
def runner():
    return CliRunner()


# Every help surface listed here must carry an "Examples:" section with
# 2-3 `• pm ...` invocations. If a subgroup is deprecated or removed in
# the future, drop it here (and mention why in the commit).
_SURFACES_WITH_EXAMPLES = [
    (),  # top-level pm --help
    ("task",),
    ("session",),
    ("plugins",),
    ("rail",),
    ("project",),
    ("downtime",),
    ("advisor",),
    ("briefing",),
    ("memory",),
    ("activity",),
    ("jobs",),
    ("inbox",),
    ("flow",),
    ("alert",),
    ("heartbeat",),
    ("issue",),
    ("report",),
    ("itsalive",),
    ("up",),
    ("launch",),
    ("status",),
    ("send",),
    ("notify",),
    ("doctor",),
    ("projects",),
    ("add-project",),
]


@pytest.mark.parametrize("surface", _SURFACES_WITH_EXAMPLES)
def test_help_surface_has_uniform_examples_block(runner, surface):
    args = list(surface) + ["--help"]
    result = runner.invoke(app, args)
    # Help should render (exit 0 for --help)
    assert result.exit_code == 0, (
        f"`pm {' '.join(args)}` help did not render:\n{result.output}"
    )
    assert "Examples:" in result.output, (
        f"`pm {' '.join(args)}` --help has no literal Examples: section:\n"
        f"{result.output}"
    )
    example_lines = [
        line for line in result.output.splitlines()
        if "• pm " in line
    ]
    assert 2 <= len(example_lines) <= 3, (
        f"`pm {' '.join(args)}` --help has {len(example_lines)} example lines; "
        "expected 2-3 concrete examples:\n"
        f"{result.output}"
    )


# ---------------------------------------------------------------------------
# pm help worker — meta-command that prints the worker guide
# ---------------------------------------------------------------------------


def test_pm_help_worker_prints_guide(runner):
    result = runner.invoke(app, ["help", "worker"])
    assert result.exit_code == 0, result.output
    # Signature strings from docs/worker-guide.md
    assert "Worker Guide" in result.output
    assert "claim" in result.output
    assert "pm task done" in result.output
    assert "What NOT to do" in result.output


def test_pm_help_unknown_role_lists_available(runner):
    result = runner.invoke(app, ["help", "martian"])
    assert result.exit_code != 0
    assert "martian" in result.output
    # Must name the fix: what roles are available.
    assert "worker" in result.output


# ---------------------------------------------------------------------------
# Specific content checks for the most important groups.
# ---------------------------------------------------------------------------


def test_task_help_shows_typical_worker_flow(runner):
    result = runner.invoke(app, ["task", "--help"])
    assert result.exit_code == 0
    # The worker flow sequence should be readable top-to-bottom.
    assert "pm task next" in result.output
    assert "pm task claim" in result.output
    assert "pm task done" in result.output


def test_top_level_help_points_to_worker_guide(runner):
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    # Users should see the help-worker meta-command prominently.
    assert "pm help worker" in result.output


def test_memory_help_uses_recall_not_search(runner):
    result = runner.invoke(app, ["memory", "--help"])
    assert result.exit_code == 0
    assert "pm memory recall" in result.output
    assert "pm memory search" not in result.output


def test_issue_help_uses_real_issue_commands(runner):
    result = runner.invoke(app, ["issue", "--help"])
    assert result.exit_code == 0
    assert "pm issue info" in result.output
    assert "pm issue transition" in result.output
    assert "pm issue show" not in result.output


def test_rail_help_uses_hide_and_show(runner):
    result = runner.invoke(app, ["rail", "--help"])
    assert result.exit_code == 0
    assert "pm rail hide" in result.output
    assert "pm rail show" in result.output
    assert "pm rail add" not in result.output


def test_plugins_help_uses_install_not_scaffold(runner):
    result = runner.invoke(app, ["plugins", "--help"])
    assert result.exit_code == 0
    assert "pm plugins install" in result.output
    assert "pm plugins scaffold" not in result.output
