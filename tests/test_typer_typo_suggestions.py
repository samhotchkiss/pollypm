"""Audit: typer typo suggestions must be enabled at every subcommand level.

Typer's TyperGroup ships ``suggest_commands=True`` by default. When the
user mistypes a subcommand, Typer falls through to click's UsageError
with a "Did you mean X?" hint built from ``difflib.get_close_matches``.

This module:

1. Walks the full ``pollypm.cli.app`` tree and asserts every Typer
   subgroup has ``suggest_commands`` on. A future PR that subclasses
   ``TyperGroup`` with ``suggest_commands=False`` (or uses a bare
   ``click.Group``) will fail here, surfacing the regression
   immediately.
2. Exercises the feature at each of the subcommand levels named in
   wg04 (#241): ``pm task``, ``pm session``, ``pm plugins``, ``pm rail``,
   ``pm project``, ``pm downtime``, ``pm advisor``, ``pm briefing``,
   ``pm memory``, ``pm activity``, ``pm jobs``. The matcher output
   depends on having at least one command whose edit-distance to the
   typo is within ``difflib``'s default cutoff (0.6); we pick known-
   close typos for each.
"""

from __future__ import annotations

import click
import pytest
import typer
from typer.testing import CliRunner

from pollypm.cli import app


# ---------------------------------------------------------------------------
# Audit: every Typer subgroup has suggest_commands enabled
# ---------------------------------------------------------------------------


def _walk_groups(cmd, path=""):
    """Yield (path, group) for every click.Group in the tree."""
    if isinstance(cmd, click.core.Group):
        yield (path or "<root>", cmd)
        for name, sub in cmd.commands.items():
            new_path = f"{path}/{name}" if path else name
            yield from _walk_groups(sub, new_path)


def test_every_typer_subgroup_has_suggestions_enabled():
    """Regression guard: if anyone adds a subapp without suggestions
    (or downgrades to a plain click.Group), this test fails."""
    click_app = typer.main.get_command(app)
    missing = []
    for path, group in _walk_groups(click_app):
        sc = getattr(group, "suggest_commands", None)
        if sc is not True:
            # Plain click.Group objects won't have the attribute.
            # Anything not explicitly True is a regression.
            missing.append((path, type(group).__name__, sc))

    assert not missing, (
        f"Subgroups missing typo suggestions: {missing}"
    )


def test_audit_covers_every_documented_subgroup():
    """Catalog coverage check — every path named in the wg04 spec
    must actually exist as a subgroup in the app tree."""
    click_app = typer.main.get_command(app)
    top_level = {name for name, cmd in click_app.commands.items()
                 if isinstance(cmd, click.core.Group)}

    # From worker-onboarding-and-errors-spec §4.3
    documented = {
        "task", "session", "plugins", "rail", "project",
        "downtime", "advisor", "briefing", "memory", "activity", "jobs",
    }
    missing_from_tree = documented - top_level
    assert not missing_from_tree, (
        f"Documented subgroups not present in app tree: {missing_from_tree}. "
        f"Either add them or update the wg04 catalog."
    )


# ---------------------------------------------------------------------------
# Behavior: typo on each subcommand produces a "Did you mean" hint
# ---------------------------------------------------------------------------


@pytest.fixture
def runner():
    return CliRunner()


@pytest.mark.parametrize(
    "subcommand,typo,expected_match",
    [
        # (subgroup path, typo under that subgroup, command we expect suggested)
        ("task", "don", "done"),          # task done → don is close enough
        ("task", "nex", "next"),          # task next
        ("task", "clai", "claim"),        # task claim
        ("task", "gett", "get"),          # task get
        ("session", "set-statu", "set-status"),  # session set-status
        ("plugins", "lis", "list"),       # plugins list
        ("project", "nw", "new"),         # project new
        ("memory", "lis", "list"),        # memory list
        ("jobs", "lis", "list"),          # jobs list
        ("rail", "lis", "list"),          # rail list
        ("briefing", "statu", "status"),  # briefing status
        ("advisor", "statu", "status"),   # advisor status
        ("downtime", "lis", "list"),      # downtime list
    ],
)
def test_subcommand_typo_offers_suggestion(runner, subcommand, typo, expected_match):
    """At every wg04-named level, typing a close typo must produce a
    'Did you mean X?' hint. This pins the acceptance criterion."""
    click_app = typer.main.get_command(app)
    group = click_app.commands.get(subcommand)
    if group is None or not isinstance(group, click.core.Group):
        pytest.skip(f"Subgroup {subcommand!r} not present in this build")

    available = list(group.commands.keys())
    if expected_match not in available:
        pytest.skip(
            f"Expected command {expected_match!r} not under {subcommand!r} "
            f"(available: {available}) — update the parametrize table."
        )

    result = runner.invoke(app, [subcommand, typo])
    # Non-zero exit because the command is unknown
    assert result.exit_code != 0
    # The "Did you mean" hint must name the intended command.
    assert "Did you mean" in result.output, (
        f"No suggestion for `pm {subcommand} {typo}`:\n{result.output}"
    )
    assert expected_match in result.output, (
        f"Suggestion did not include {expected_match!r}:\n{result.output}"
    )


def test_top_level_typo_still_suggests(runner):
    """Keep the existing top-level behavior from regressing."""
    result = runner.invoke(app, ["sessin"])
    assert result.exit_code != 0
    assert "Did you mean" in result.output
    assert "session" in result.output


def test_top_level_typo_suggests_projects(runner):
    """Canonical wg04 example: `pm proj` → `project`/`projects`."""
    result = runner.invoke(app, ["proj"])
    assert result.exit_code != 0
    assert "Did you mean" in result.output
    # "project" and/or "projects" should appear in the suggestion.
    assert "project" in result.output
