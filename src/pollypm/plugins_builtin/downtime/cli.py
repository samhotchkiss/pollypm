"""``pm downtime`` CLI — user-facing controls for the downtime plugin.

See docs/downtime-plugin-spec.md §9.

Subcommands:

* ``pm downtime add <title>``          — append a candidate to the user queue.
* ``pm downtime list``                 — show queued + last-N completed tasks.
* ``pm downtime pause [--until DATE]`` — skip the next tick (24h default).
* ``pm downtime resume``               — clear the pause marker.
* ``pm downtime disable`` / ``enable`` — toggle ``[downtime].enabled``.
* ``pm downtime status``               — summary (enabled, pause, recent).

All commands accept ``--json`` for machine-readable output.
"""
from __future__ import annotations

import json
from datetime import UTC, date as _date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import typer

from pollypm.atomic_io import atomic_write_text
from pollypm.config import DEFAULT_CONFIG_PATH, load_config, resolve_config_path
from pollypm.plugins_builtin.downtime.handlers.pick_candidate import (
    USER_QUEUE_RELATIVE_PATH,
    Candidate,
    append_to_user_queue,
    read_user_queue,
)
from pollypm.plugins_builtin.downtime.settings import (
    KNOWN_CATEGORIES,
    load_downtime_settings,
)
from pollypm.plugins_builtin.downtime.state import (
    load_state,
    save_state,
)


downtime_app = typer.Typer(
    help=(
        "Manage the downtime exploration plugin (idle-LLM-budget tasks).\n\n"
        "Examples:\n\n"
        "• pm downtime status                 — show downtime state\n"
        "• pm downtime enable                 — turn on downtime exploration\n"
        "• pm downtime list                   — show exploration runs\n"
    ),
    no_args_is_help=True,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _resolve(config_path: Path) -> Path:
    return resolve_config_path(config_path)


def _require_config(config_path: Path) -> Path:
    path = _resolve(config_path)
    if not path.exists():
        typer.echo(
            f"No PollyPM config at {path}. Run `pm init` first.", err=True,
        )
        raise typer.Exit(code=1)
    return path


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(payload, indent=2, default=str))


def _base_dir(config_path: Path) -> Path:
    """Resolve the project base_dir the state file lives under."""
    cfg = load_config(config_path)
    return Path(cfg.project.base_dir)


def _project_root(config_path: Path) -> Path:
    cfg = load_config(config_path)
    root = getattr(cfg.project, "root_dir", None)
    if root is None:
        return config_path.parent
    return Path(root)


# ---------------------------------------------------------------------------
# Config mutation — enable/disable
# ---------------------------------------------------------------------------


def _set_downtime_enabled(config_path: Path, enabled: bool) -> bool:
    """Write ``[downtime].enabled = <bool>``. Returns True when file changed.

    Mirrors the briefing plugin's helper. Preserves other keys in the
    ``[downtime]`` section; creates the section when missing.
    """
    text = config_path.read_text()
    target = "true" if enabled else "false"

    lines = text.splitlines()
    section_start: int | None = None
    section_end: int | None = None
    for idx, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped == "[downtime]":
            section_start = idx
            continue
        if section_start is not None and stripped.startswith("[") and stripped.endswith("]"):
            section_end = idx
            break
    if section_start is not None and section_end is None:
        section_end = len(lines)

    if section_start is None:
        # Append a new section at the bottom.
        new_block = ["", "[downtime]", f"enabled = {target}", ""]
        if text and not text.endswith("\n"):
            text = text + "\n"
        new_text = text + "\n".join(new_block)
        if new_text == text:
            return False
        atomic_write_text(config_path, new_text)
        return True

    # Look for an existing enabled = line inside the section.
    assert section_end is not None
    for idx in range(section_start + 1, section_end):
        line = lines[idx]
        stripped = line.strip()
        if stripped.startswith("enabled"):
            already = f"enabled = {target}"
            if stripped == already:
                return False
            lines[idx] = already
            break
    else:
        # Insert enabled = under the header.
        lines.insert(section_start + 1, f"enabled = {target}")

    new_text = "\n".join(lines)
    if text.endswith("\n"):
        new_text = new_text + "\n"
    if new_text == text:
        return False
    atomic_write_text(config_path, new_text)
    return True


# ---------------------------------------------------------------------------
# add / list
# ---------------------------------------------------------------------------


@downtime_app.command("add")
def add_cmd(
    title: str = typer.Argument(..., help="Candidate title — shown in the inbox."),
    kind: str = typer.Option(
        "spec_feature",
        "--kind",
        help=f"Candidate category. One of: {', '.join(sorted(KNOWN_CATEGORIES))}.",
    ),
    description: str = typer.Option(
        "",
        "--description",
        help="Longer description. Defaults to the title when empty.",
    ),
    priority: int = typer.Option(
        3, "--priority", min=1, max=5, help="Priority 1–5 (5 highest).",
    ),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Append a candidate to the downtime user queue."""
    path = _require_config(config_path)
    if kind not in KNOWN_CATEGORIES:
        typer.echo(
            f"Unknown kind '{kind}'. Valid kinds: "
            + ", ".join(sorted(KNOWN_CATEGORIES)),
            err=True,
        )
        raise typer.Exit(code=2)
    candidate = Candidate(
        title=title,
        kind=kind,
        description=description or title,
        priority=priority,
        source="user",
    )
    root = _project_root(path)
    queue_path = root / USER_QUEUE_RELATIVE_PATH
    append_to_user_queue(queue_path, candidate)
    payload = {
        "queued": True,
        "title": title,
        "kind": kind,
        "priority": priority,
        "queue_path": str(queue_path),
    }
    if as_json:
        _emit(payload, as_json=True)
        return
    typer.echo(f"downtime: queued '{title}' ({kind}, priority {priority}).")


@downtime_app.command("list")
def list_cmd(
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Show the user queue + recent downtime titles (from state)."""
    path = _require_config(config_path)
    root = _project_root(path)
    queue_path = root / USER_QUEUE_RELATIVE_PATH
    queued = read_user_queue(queue_path)
    state = load_state(_base_dir(path))
    payload = {
        "queued_count": len(queued),
        "queued": [c.to_dict() for c in queued],
        "recent_titles": list(state.recent_titles[-5:]),
    }
    if as_json:
        _emit(payload, as_json=True)
        return
    typer.echo(f"downtime: {len(queued)} queued")
    for c in queued:
        typer.echo(f"  [{c.kind}] p{c.priority} — {c.title}")
    if state.recent_titles:
        typer.echo("recently scheduled:")
        for title in state.recent_titles[-5:]:
            typer.echo(f"  - {title}")


# ---------------------------------------------------------------------------
# pause / resume
# ---------------------------------------------------------------------------


def _parse_until(raw: str) -> str:
    """Validate a --until argument; return the stored ISO string."""
    candidate = raw.strip()
    try:
        _date.fromisoformat(candidate)
        return candidate
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise typer.BadParameter(
            f"--until must be YYYY-MM-DD or ISO datetime; got {raw!r}"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


@downtime_app.command("pause")
def pause_cmd(
    until: Optional[str] = typer.Option(
        None, "--until", help="Pause until ISO date / datetime (default: +24h).",
    ),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Skip the next downtime tick (24h default)."""
    path = _require_config(config_path)
    base_dir = _base_dir(path)
    state = load_state(base_dir)
    if until:
        state.pause_until = _parse_until(until)
    else:
        state.pause_until = (datetime.now(UTC) + timedelta(hours=24)).isoformat()
    save_state(base_dir, state)
    payload = {"paused": True, "pause_until": state.pause_until}
    if as_json:
        _emit(payload, as_json=True)
        return
    typer.echo(f"downtime: paused until {state.pause_until}")


@downtime_app.command("resume")
def resume_cmd(
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Clear any active pause marker."""
    path = _require_config(config_path)
    base_dir = _base_dir(path)
    state = load_state(base_dir)
    was_paused = bool(state.pause_until)
    state.pause_until = ""
    save_state(base_dir, state)
    payload = {"resumed": True, "was_paused": was_paused}
    if as_json:
        _emit(payload, as_json=True)
        return
    if was_paused:
        typer.echo("downtime: resumed (pause marker cleared)")
    else:
        typer.echo("downtime: no pause was active")


# ---------------------------------------------------------------------------
# enable / disable
# ---------------------------------------------------------------------------


@downtime_app.command("enable")
def enable_cmd(
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Enable the downtime tick."""
    path = _require_config(config_path)
    changed = _set_downtime_enabled(path, True)
    payload = {"enabled": True, "changed": changed, "config_path": str(path)}
    if as_json:
        _emit(payload, as_json=True)
        return
    typer.echo("downtime: enabled" if changed else "downtime: already enabled")


@downtime_app.command("disable")
def disable_cmd(
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Disable the downtime tick until re-enabled."""
    path = _require_config(config_path)
    changed = _set_downtime_enabled(path, False)
    payload = {"enabled": False, "changed": changed, "config_path": str(path)}
    if as_json:
        _emit(payload, as_json=True)
        return
    typer.echo("downtime: disabled" if changed else "downtime: already disabled")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@downtime_app.command("status")
def status_cmd(
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Show enabled flag + pause marker + recent titles."""
    path = _require_config(config_path)
    settings = load_downtime_settings(path)
    state = load_state(_base_dir(path))
    root = _project_root(path)
    queue = read_user_queue(root / USER_QUEUE_RELATIVE_PATH)

    payload = {
        "enabled": settings.enabled,
        "threshold_pct": settings.threshold_pct,
        "cadence": settings.cadence,
        "disabled_categories": list(settings.disabled_categories),
        "pause_until": state.pause_until or None,
        "queued_count": len(queue),
        "recent_titles": list(state.recent_titles[-5:]),
        "last_kind": state.last_kind or None,
        "last_source": state.last_source or None,
    }
    if as_json:
        _emit(payload, as_json=True)
        return
    typer.echo(f"downtime: enabled={settings.enabled}")
    typer.echo(f"  cadence: {settings.cadence}")
    typer.echo(f"  threshold_pct: {settings.threshold_pct}")
    if settings.disabled_categories:
        typer.echo(f"  disabled categories: {', '.join(settings.disabled_categories)}")
    if state.pause_until:
        typer.echo(f"  paused until: {state.pause_until}")
    typer.echo(f"  queued: {len(queue)}")
    if state.recent_titles:
        typer.echo("  recent:")
        for title in state.recent_titles[-5:]:
            typer.echo(f"    - {title}")
