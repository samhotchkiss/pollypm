"""`pm rail` subcommand — list, hide, show, restart cockpit rail items.

See docs/extensible-rail-spec.md §6 and issue #224.

The list/hide/show trio operate through the plugin-host rail registry
— the same structure the cockpit builder walks. ``hide`` / ``show``
edit the user-global ``~/.pollypm/pollypm.toml``
``[rail].hidden_items`` list. ``restart`` signals the headless
``pollypm.rail_daemon`` (tracked via ``~/.pollypm/rail_daemon.pid``)
to exit cleanly and respawns it — used by tier-4 Polly's
``pm system restart-rail-daemon`` recovery path.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import typer

from pollypm.cli_help import help_with_examples


logger = logging.getLogger(__name__)

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


# ---------------------------------------------------------------------------
# `pm rail restart` — signal-then-respawn the headless rail_daemon.
# ---------------------------------------------------------------------------


_RAIL_DAEMON_PID_FILE = Path.home() / ".pollypm" / "rail_daemon.pid"
_DEFAULT_GRACE_S = 3.0


def _read_pid_file(pid_path: Path) -> int | None:
    """Return the PID stored in ``pid_path`` or ``None`` if unreadable."""
    try:
        raw = pid_path.read_text().strip()
    except (FileNotFoundError, OSError):
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if pid > 0 else None


def _pid_alive(pid: int) -> bool:
    """Return True iff ``pid`` is currently a live process we can signal."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by someone else — treat as alive
        # but uncontrollable. The caller will refuse to kill it.
        return True
    return True


def _terminate_daemon(
    pid: int,
    *,
    grace_s: float = _DEFAULT_GRACE_S,
    sleep_fn=time.sleep,
    kill_fn=None,
) -> str:
    """SIGTERM ``pid``, wait ``grace_s``, escalate to SIGKILL on holdout.

    Returns one of ``"already_gone"``, ``"SIGTERM"``, ``"SIGKILL"``,
    ``"denied"``. Mirrors :func:`rail_daemon_reaper._terminate_with_grace`
    so the manual restart path uses the same signal discipline as the
    bootstrap reaper.

    ``kill_fn`` defaults to the module-level ``os.kill`` looked up at
    call time so tests can monkeypatch ``rail_cli.os.kill``.
    """
    _kill = kill_fn if kill_fn is not None else os.kill
    if not _pid_alive(pid):
        return "already_gone"
    try:
        _kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "already_gone"
    except PermissionError:
        return "denied"
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return "SIGTERM"
        sleep_fn(0.1)
    try:
        _kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return "SIGTERM"
    except PermissionError:
        return "denied"
    return "SIGKILL"


def _spawn_daemon(
    *,
    spawn_fn=None,
) -> int | None:
    """Spawn a fresh ``python -m pollypm.rail_daemon`` in the background.

    Returns the PID of the new daemon or ``None`` on failure. The new
    process detaches via ``start_new_session=True`` so it survives the
    parent CLI exiting. ``spawn_fn`` is injectable for tests.
    """
    if spawn_fn is not None:
        try:
            return int(spawn_fn())
        except Exception:  # noqa: BLE001
            logger.warning("rail restart: spawn_fn raised", exc_info=True)
            return None
    try:
        proc = subprocess.Popen(  # noqa: S603 — we control argv
            [sys.executable, "-m", "pollypm.rail_daemon"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except Exception:  # noqa: BLE001
        logger.warning("rail restart: Popen raised", exc_info=True)
        return None
    return proc.pid


@rail_app.command("restart")
def restart_daemon(
    grace_seconds: float = typer.Option(
        _DEFAULT_GRACE_S,
        "--grace-seconds",
        help=(
            "Seconds to wait between SIGTERM and SIGKILL on the "
            "existing rail_daemon."
        ),
    ),
    no_spawn: bool = typer.Option(
        False, "--no-spawn/--spawn",
        help=(
            "Skip spawning a replacement daemon — useful when the "
            "cockpit will start one on next boot. Default is to "
            "respawn so the headless rail stays live."
        ),
    ),
    pid_file: Path = typer.Option(
        _RAIL_DAEMON_PID_FILE,
        "--pid-file",
        help="Override pid file location (defaults to ~/.pollypm/rail_daemon.pid).",
    ),
) -> None:
    """Restart the headless rail_daemon.

    Reads the PID from ``~/.pollypm/rail_daemon.pid``, sends SIGTERM
    (escalating to SIGKILL after ``--grace-seconds``), then spawns a
    replacement via ``python -m pollypm.rail_daemon`` unless
    ``--no-spawn`` is set. Tier-4 Polly's
    ``pm system restart-rail-daemon`` shells out to this command.
    """
    pid = _read_pid_file(pid_file)
    if pid is None:
        typer.echo(
            f"no rail_daemon pid file at {pid_file} — nothing to kill."
        )
        # Spawn anyway so a missing-pid-file state recovers cleanly,
        # unless the operator opted out.
        existing_outcome = "no_pid_file"
    elif not _pid_alive(pid):
        typer.echo(
            f"rail_daemon pid={pid} is not alive (stale pid file).",
        )
        try:
            pid_file.unlink()
        except OSError:
            pass
        existing_outcome = "stale"
    else:
        outcome = _terminate_daemon(pid, grace_s=grace_seconds)
        typer.echo(f"rail_daemon pid={pid} {outcome}")
        if outcome == "denied":
            raise typer.Exit(code=2)
        # Daemon's own atexit hook removes the pid file; if that didn't
        # fire (SIGKILL path), clean up so the new daemon can claim it.
        try:
            if pid_file.exists() and not _pid_alive(pid):
                pid_file.unlink()
        except OSError:
            pass
        existing_outcome = outcome

    if no_spawn:
        typer.echo(f"restart outcome={existing_outcome} spawned=skipped")
        return

    new_pid = _spawn_daemon()
    if new_pid is None:
        typer.echo(
            f"restart outcome={existing_outcome} spawned=failed",
            err=True,
        )
        raise typer.Exit(code=3)
    typer.echo(
        f"restart outcome={existing_outcome} spawned_pid={new_pid}"
    )
