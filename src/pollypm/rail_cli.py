"""`pm rail` subcommand — list, hide, show cockpit rail items.

See docs/extensible-rail-spec.md §6 and issue #224.

All three commands operate through the plugin-host rail registry — the
same structure the cockpit builder walks. ``hide`` / ``show`` edit the
user-global ``~/.pollypm/pollypm.toml`` ``[rail].hidden_items`` list.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import typer

from pollypm.cli_help import help_with_examples

rail_app = typer.Typer(
    help=help_with_examples(
        "Manage cockpit rail items.",
        [
            ("pm rail list", "show configured rail items"),
            ("pm rail hide tools.activity", "hide one rail item"),
            ("pm rail show tools.activity", "restore a hidden rail item"),
        ],
    )
)


USER_CONFIG_PATH = Path.home() / ".pollypm" / "pollypm.toml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_host():
    """Build a fresh ExtensionHost rooted at cwd, honouring any
    ``[plugins].disabled`` from the user config.
    """
    from pollypm.plugin_host import ExtensionHost

    # Load user disabled list cheaply — avoid a full config parse so
    # `pm rail list` keeps working in partially-configured repos.
    disabled: tuple[str, ...] = ()
    try:
        import tomllib

        if USER_CONFIG_PATH.exists():
            raw = tomllib.loads(USER_CONFIG_PATH.read_text())
            plugins_raw = raw.get("plugins", {})
            if isinstance(plugins_raw, dict):
                entries = plugins_raw.get("disabled", [])
                if isinstance(entries, list):
                    disabled = tuple(
                        str(e).strip() for e in entries if isinstance(e, str) and e.strip()
                    )
    except Exception:  # noqa: BLE001
        pass
    return ExtensionHost(Path.cwd(), disabled=disabled)


def _collect_items() -> list[dict[str, Any]]:
    """Load the rail registry and return a JSON-friendly list of items.

    Each item: ``{section, index, label, plugin, item_key, visibility}``.
    """
    host = _build_host()
    # Invoke every plugin's initialize() so rail items register.
    try:
        host.initialize_plugins()
    except Exception:  # noqa: BLE001
        # initialize() errors are tracked via degraded_plugins; they
        # don't block rail introspection.
        pass

    hidden = _load_hidden_items()
    out: list[dict[str, Any]] = []
    for reg in host.rail_registry().items():
        visibility: str
        if callable(reg.visibility):
            visibility = "predicate"
        else:
            visibility = str(reg.visibility)
        out.append(
            {
                "section": reg.section,
                "index": reg.index,
                "label": reg.label,
                "plugin": reg.plugin_name,
                "item_key": reg.item_key,
                "visibility": visibility,
                "feature_name": reg.feature_name,
                "hidden": reg.item_key in hidden,
            }
        )
    return out


def _load_hidden_items() -> list[str]:
    """Return the current ``[rail].hidden_items`` list as-is."""
    try:
        import tomllib

        if not USER_CONFIG_PATH.exists():
            return []
        raw = tomllib.loads(USER_CONFIG_PATH.read_text())
        rail_raw = raw.get("rail", {})
        if not isinstance(rail_raw, dict):
            return []
        hidden = rail_raw.get("hidden_items", [])
        if not isinstance(hidden, list):
            return []
        return [str(e) for e in hidden if isinstance(e, str) and e.strip()]
    except Exception:  # noqa: BLE001
        return []


def _load_collapsed_sections() -> list[str]:
    try:
        import tomllib

        if not USER_CONFIG_PATH.exists():
            return []
        raw = tomllib.loads(USER_CONFIG_PATH.read_text())
        rail_raw = raw.get("rail", {})
        if not isinstance(rail_raw, dict):
            return []
        collapsed = rail_raw.get("collapsed_sections", [])
        if not isinstance(collapsed, list):
            return []
        return [str(e) for e in collapsed if isinstance(e, str) and e.strip()]
    except Exception:  # noqa: BLE001
        return []


# Matches a ``[rail]`` TOML section: the ``[rail]`` header and every
# subsequent non-header line up to (but not including) the next
# ``[table]`` or EOF. ``(?m)`` so ``^`` matches each line start.
_RAIL_BLOCK_RE = re.compile(
    r"(?m)^\[rail\][ \t]*\n(?:(?!^\[).*\n?)*"
)


def _write_hidden_items(hidden: list[str]) -> None:
    """Rewrite the ``[rail].hidden_items`` list in the user config.

    Preserves ``collapsed_sections`` and every other section of the
    file. If no ``[rail]`` block exists, one is appended. Mirrors the
    approach used by ``plugin_cli._write_disabled_list``.
    """
    _rewrite_rail_block(hidden_items=hidden, collapsed_sections=None)


def _write_collapsed_sections(collapsed: list[str]) -> None:
    _rewrite_rail_block(hidden_items=None, collapsed_sections=collapsed)


def _rewrite_rail_block(
    *,
    hidden_items: list[str] | None,
    collapsed_sections: list[str] | None,
) -> None:
    USER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = USER_CONFIG_PATH.read_text() if USER_CONFIG_PATH.exists() else ""

    # Load current values from the file so we don't clobber untouched
    # keys when only one of hidden_items/collapsed_sections is being
    # updated.
    current_hidden = _load_hidden_items()
    current_collapsed = _load_collapsed_sections()
    final_hidden = current_hidden if hidden_items is None else hidden_items
    final_collapsed = (
        current_collapsed if collapsed_sections is None else collapsed_sections
    )

    def _format_list(values: list[str]) -> str:
        if not values:
            return "[]"
        rendered = ", ".join(f'"{v}"' for v in values)
        return f"[{rendered}]"

    new_block_lines: list[str] = ["[rail]"]
    new_block_lines.append(f"hidden_items = {_format_list(final_hidden)}")
    new_block_lines.append(f"collapsed_sections = {_format_list(final_collapsed)}")
    new_block = "\n".join(new_block_lines) + "\n"

    if "[rail]" in existing:
        # Replace the existing [rail] block in place.
        updated = _RAIL_BLOCK_RE.sub(new_block, existing, count=1)
    else:
        # Append to the end of the file.
        separator = "" if existing.endswith("\n") or not existing else "\n"
        updated = f"{existing}{separator}\n{new_block}"

    USER_CONFIG_PATH.write_text(updated)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@rail_app.command("list")
def list_items(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """List registered rail items with section, index, label, and plugin source."""
    items = _collect_items()
    if json_output:
        typer.echo(json.dumps(items, indent=2))
        return

    if not items:
        typer.echo("No rail items registered.")
        return

    # Group by section for human-readable output.
    by_section: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        by_section.setdefault(item["section"], []).append(item)

    from pollypm.plugin_api.v1 import RAIL_SECTIONS

    for section in RAIL_SECTIONS:
        rows = by_section.get(section, [])
        if not rows:
            continue
        typer.echo(f"── {section.upper()} ──")
        for row in rows:
            marker = "[hidden] " if row["hidden"] else ""
            line = (
                f"  {row['index']:>3}  {row['label']:<24}  "
                f"({row['plugin']})  key={row['item_key']} {marker}".rstrip()
            )
            typer.echo(line)


@rail_app.command("hide")
def hide_item(
    key: str = typer.Argument(
        ...,
        help="Rail item key in 'section.label' form, e.g. 'tools.activity'.",
    ),
) -> None:
    """Add ``key`` to ``[rail].hidden_items`` in pollypm.toml."""
    _validate_key(key)
    hidden = _load_hidden_items()
    if key in hidden:
        typer.echo(f"Item '{key}' already hidden.")
        return
    hidden.append(key)
    _write_hidden_items(hidden)
    typer.echo(f"Hid rail item '{key}'. Wrote {USER_CONFIG_PATH}.")


@rail_app.command("show")
def show_item(
    key: str = typer.Argument(
        ..., help="Rail item key previously hidden via `pm rail hide`.",
    ),
) -> None:
    """Remove ``key`` from ``[rail].hidden_items`` in pollypm.toml."""
    _validate_key(key)
    hidden = _load_hidden_items()
    if key not in hidden:
        typer.echo(f"Item '{key}' is not hidden.")
        return
    hidden.remove(key)
    _write_hidden_items(hidden)
    typer.echo(f"Un-hid rail item '{key}'. Wrote {USER_CONFIG_PATH}.")


def _validate_key(key: str) -> None:
    if not key or "." not in key:
        raise typer.BadParameter(
            "Rail item key must be in the form 'section.label' "
            "(e.g. 'tools.activity'). Use `pm rail list` to see keys."
        )
