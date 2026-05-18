"""Tests for ``pm cli-reference --json`` and the underlying introspection.

The introspection helper is the contract — the CLI command is a thin
wrapper. Tests cover both, plus a smoke test against the real
``pollypm.cli.app`` so a regression in any registered subapp surfaces
here.
"""

from __future__ import annotations

import json

import typer
from typer.testing import CliRunner

import pollypm.cli as cli
from pollypm.cli_reference import build_cli_reference


def _make_fixture_app() -> typer.Typer:
    """Return a small Typer app that exercises every schema branch."""
    app = typer.Typer(help="Fixture app for cli-reference tests.")

    sub = typer.Typer(help="Sub group.")
    app.add_typer(sub, name="sub")

    @sub.command("create")
    def _create(
        title: str = typer.Argument(..., help="Title arg."),
        role: list[str] = typer.Option(  # noqa: B008 — Typer pattern
            None, "--role", "-r", help="Repeatable role.",
        ),
        force: bool = typer.Option(
            False, "--force", help="Boolean flag.",
        ),
        limit: int = typer.Option(
            10, "--limit", help="Numeric option.",
        ),
    ) -> None:
        """Fixture create command."""
        _ = (title, role, force, limit)

    @app.command("flat")
    def _flat(
        target: str = typer.Argument(..., help="Required positional."),
    ) -> None:
        """A flat root-level command."""
        _ = target

    @app.command("hidden-cmd", hidden=True)
    def _hidden() -> None:
        """Should not appear in the reference."""

    # A group with no subcommands but with params (Typer single-callback
    # pattern, e.g. `pm activity --follow`).
    leaf_group = typer.Typer(help="Leaf-style group.", invoke_without_command=True)
    app.add_typer(leaf_group, name="leaf")

    @leaf_group.callback(invoke_without_command=True)
    def _leaf_cb(
        follow: bool = typer.Option(False, "--follow", "-f", help="Tail mode."),
    ) -> None:
        _ = follow

    return app


def test_build_cli_reference_schema_for_fixture_app() -> None:
    app = _make_fixture_app()
    schema = build_cli_reference(app)

    # Top-level commands sorted, hidden command absent.
    assert "commands" in schema
    names = list(schema["commands"].keys())
    assert names == sorted(names)
    assert "hidden-cmd" not in names
    assert {"sub", "flat", "leaf"}.issubset(set(names))

    # Flat command exposes its single required argument.
    flat = schema["commands"]["flat"]
    assert flat["help"] == "A flat root-level command."
    assert "subcommands" not in flat
    assert flat["params"][0] == {
        "name": "target",
        "param_type": "argument",
        "type": "str",
        "required": True,
        "multiple": False,
        "is_flag": False,
        "opts": ["target"],
        "default": None,
        "help": "Required positional.",
    }

    # Sub group recurses; create subcommand exposes all params with the
    # right shapes.
    sub = schema["commands"]["sub"]
    assert "subcommands" in sub
    create = sub["subcommands"]["create"]
    by_name = {p["name"]: p for p in create["params"]}
    assert by_name["title"]["required"] is True
    assert by_name["title"]["param_type"] == "argument"
    assert by_name["role"]["multiple"] is True
    assert by_name["role"]["param_type"] == "option"
    assert by_name["role"]["opts"] == ["--role", "-r"]
    assert by_name["force"]["type"] == "bool"
    assert by_name["force"]["is_flag"] is True
    assert by_name["limit"]["type"] == "int"
    assert by_name["limit"]["default"] == 10

    # Leaf group: empty subcommands AND surfaced callback params.
    leaf = schema["commands"]["leaf"]
    assert leaf["subcommands"] == {}
    leaf_param_names = {p["name"] for p in leaf["params"]}
    assert "follow" in leaf_param_names


def test_build_cli_reference_output_is_json_serialisable() -> None:
    """The schema must round-trip through ``json.dumps`` with no encoder."""
    schema = build_cli_reference(_make_fixture_app())
    # Will raise TypeError if any non-JSON-native leaves leaked through.
    blob = json.dumps(schema)
    assert json.loads(blob) == schema


def test_cli_reference_command_emits_json() -> None:
    runner = CliRunner()
    result = runner.invoke(cli.app, ["cli-reference", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "commands" in payload
    # Spot-check a few known commands ship in the dump.
    for expected in ("task", "inbox", "project", "cli-reference"):
        assert expected in payload["commands"], expected
    # task is a group, and its subcommands include `create`.
    task = payload["commands"]["task"]
    assert "subcommands" in task
    assert "create" in task["subcommands"]
    create = task["subcommands"]["create"]
    create_param_names = {p["name"] for p in create["params"]}
    assert {"title", "project", "role"}.issubset(create_param_names)


def test_cli_reference_without_json_flag_exits_nonzero() -> None:
    runner = CliRunner()
    result = runner.invoke(cli.app, ["cli-reference"])
    assert result.exit_code == 2
    assert "--json" in result.output
