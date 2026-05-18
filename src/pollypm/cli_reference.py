"""Introspect the PollyPM Typer app into a machine-readable schema.

Contract:
- Inputs: the root ``typer.Typer`` app (or any Typer app for testing).
- Outputs: a nested dict shaped as documented in :func:`build_cli_reference`
  — commands, subcommands, parameter metadata. JSON-serialisable.
- Side effects: none. Pure inspection over the click command tree.
- Invariants: never imports cockpit / TUI surfaces; safe to call from any
  process. Returns a dict whose only leaf types are ``str``, ``bool``,
  ``int``, ``list``, ``dict``, or ``None`` so it round-trips through
  ``json.dumps`` without a custom encoder.
- Allowed dependencies: ``typer``, ``click``.
- Private: helpers prefixed with ``_``.

This is the third wedge of #1629 (agent ergonomics): autonomous agents
can grep one ``pm cli-reference --json`` dump instead of recursively
walking ``--help`` pages. Hidden commands / parameters are skipped so
the schema mirrors what end-users actually see.
"""

from __future__ import annotations

from typing import Any

import click
import typer
import typer.main


def build_cli_reference(app: typer.Typer) -> dict[str, Any]:
    """Walk ``app``'s command tree and return a JSON-serialisable schema.

    Output shape::

        {
          "commands": {
            "<name>": <command-node> | <group-node>,
            ...
          }
        }

    A command node looks like::

        {
          "help": "...",
          "params": [
            {"name": "title", "param_type": "argument", "type": "str",
             "required": true, "multiple": false, "help": "...",
             "default": null, "opts": ["title"]},
            ...
          ]
        }

    A group node has ``"subcommands"`` instead of ``"params"``::

        {"help": "...", "subcommands": {"<sub>": <command-node>, ...}}
    """
    root = typer.main.get_command(app)
    return {"commands": _walk_group_children(root)}


def _walk_group_children(group: click.Group) -> dict[str, Any]:
    """Return a ``{name: node}`` dict for the visible subcommands of ``group``."""
    children: dict[str, Any] = {}
    for name in sorted(group.commands):
        cmd = group.commands[name]
        if getattr(cmd, "hidden", False):
            continue
        children[name] = _describe_command(cmd)
    return children


def _describe_command(cmd: click.Command) -> dict[str, Any]:
    """Describe one command node — recurses into ``click.Group`` subcommands.

    Typer surfaces a single-callback subapp as a ``click.Group`` with no
    registered subcommands but params on the group itself (e.g. ``pm
    activity --follow``). We emit ``params`` for those alongside the
    (empty) ``subcommands`` so callers don't have to special-case the
    "group with no children" shape.
    """
    node: dict[str, Any] = {"help": _help_text(cmd)}
    params = [
        entry for entry in (_describe_param(p) for p in cmd.params) if entry is not None
    ]
    if isinstance(cmd, click.Group):
        node["subcommands"] = _walk_group_children(cmd)
        if params:
            node["params"] = params
    else:
        node["params"] = params
    return node


def _describe_param(param: click.Parameter) -> dict[str, Any] | None:
    """Render a single click parameter as a JSON-friendly dict.

    Returns ``None`` for hidden params (Typer's auto-injected shell
    completion options when surfaced as hidden, etc.). Click's
    ``click.Argument`` does not carry a ``hidden`` attribute, so the
    guard only really fires for options.
    """
    if getattr(param, "hidden", False):
        return None

    if isinstance(param, click.Argument):
        param_type = "argument"
    elif isinstance(param, click.Option):
        param_type = "option"
    else:  # Defensive: future click extensions.
        param_type = type(param).__name__.lower()

    return {
        "name": param.name,
        "param_type": param_type,
        "type": _type_name(param.type),
        "required": bool(getattr(param, "required", False)),
        "multiple": bool(getattr(param, "multiple", False)),
        "is_flag": bool(getattr(param, "is_flag", False)),
        "opts": list(param.opts),
        "default": _normalize_default(param.default),
        "help": _help_text(param),
    }


def _help_text(obj: object) -> str | None:
    """Pull help text from a click command/param, preferring long form."""
    text = getattr(obj, "help", None) or getattr(obj, "short_help", None)
    if not text:
        return None
    return str(text).strip() or None


def _type_name(param_type: click.ParamType | None) -> str:
    """Map a click param type to a stable short name.

    Click exposes ``ParamType.name`` for the common cases ("text",
    "integer", "boolean", "path", ...). We normalise a couple of those
    to Python-ish names so agents can pattern-match on familiar terms.
    """
    if param_type is None:
        return "str"
    name = getattr(param_type, "name", None) or type(param_type).__name__
    return {
        "text": "str",
        "integer": "int",
        "integer range": "int",
        "float range": "float",
        "boolean": "bool",
    }.get(name, name)


def _normalize_default(value: object) -> object:
    """Coerce a click parameter default into a JSON-friendly value.

    Click stores ``None`` for "no default"; Typer sometimes stores
    callables / sentinels for eager options. Anything we cannot safely
    serialise becomes its ``repr`` so the schema stays JSON-clean while
    still surfacing *something* observable.
    """
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_normalize_default(item) for item in value]
    return repr(value)
