"""``pm briefing`` CLI — user-facing controls for the morning briefing.

Spec: ``docs/morning-briefing-plugin-spec.md`` §7.

Subcommands:

* ``pm briefing now`` — force-fire, bypassing the 6-a.m. gate and the
  date dedupe. Used for testing / manual trigger.
* ``pm briefing preview`` — full gather + synthesize path printed to
  stdout. Does not write to the inbox.
* ``pm briefing status`` — enabled? last briefing? next scheduled?
* ``pm briefing enable`` / ``disable`` — toggle ``[briefing].enabled``.
* ``pm briefing pin <id>`` — pin a briefing (prevents auto-close).

All commands support ``--json`` for machine-readable output.

The commands are wired as a Typer subapp (``briefing_app``) and mounted
in ``pollypm.cli`` alongside the other top-level subcommands.
"""
from __future__ import annotations

import json
import re
from datetime import date as _date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import typer

from pollypm.config import DEFAULT_CONFIG_PATH, load_config, resolve_config_path
from pollypm.plugins_builtin.morning_briefing.handlers import briefing_tick as _tick
from pollypm.plugins_builtin.morning_briefing.inbox import (
    list_briefings,
    pin_briefing as _pin_briefing,
)
from pollypm.plugins_builtin.morning_briefing.settings import (
    BriefingSettings,
    load_briefing_settings,
)
from pollypm.plugins_builtin.morning_briefing.state import (
    BriefingState,
    iso_date,
    load_state,
)


briefing_app = typer.Typer(
    help=(
        "Manage the morning briefing (daily inbox digest).\n\n"
        "Examples:\n\n"
        "• pm briefing status                 — show briefing configuration\n"
        "• pm briefing enable                 — turn on the daily briefing\n"
        "• pm briefing now                    — render today's briefing immediately\n"
        "• pm briefing preview                — dry-run without delivering\n"
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
            f"No PollyPM config at {path}. Run `pm init` or `pm onboard` first.",
            err=True,
        )
        raise typer.Exit(code=1)
    return path


def _current_local_now(settings: BriefingSettings, config) -> datetime:
    fallback = ""
    if config is not None:
        fallback = getattr(getattr(config, "pollypm", None), "timezone", "") or ""
    return _tick._local_now(settings, fallback)


def _emit(payload: Any, *, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(payload, default=str, indent=2, sort_keys=True))
    else:  # pragma: no cover — caller formats text themselves
        typer.echo(str(payload))


# ---------------------------------------------------------------------------
# enabled toggle — simple text-level edit of ``pollypm.toml``.
# ---------------------------------------------------------------------------


_BRIEFING_ENABLED_RE = re.compile(
    r"^(?P<indent>\s*)enabled\s*=\s*(?P<val>true|false)\s*$",
    re.IGNORECASE,
)


def _set_briefing_enabled(config_path: Path, enabled: bool) -> bool:
    """Rewrite ``pollypm.toml`` so ``[briefing].enabled = <bool>``.

    Returns ``True`` when the file was modified, ``False`` when the value
    was already ``<bool>``. Creates the ``[briefing]`` section if missing.
    """
    text = config_path.read_text()
    target = "true" if enabled else "false"

    # Locate the ``[briefing]`` section header.
    lines = text.splitlines()
    section_start: int | None = None
    section_end: int | None = None
    for idx, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped == "[briefing]":
            section_start = idx + 1
            continue
        if section_start is not None and stripped.startswith("[") and stripped.endswith("]"):
            section_end = idx
            break
    if section_start is not None and section_end is None:
        section_end = len(lines)

    if section_start is not None:
        # Find the ``enabled`` line inside the section; replace or insert.
        found_at: int | None = None
        for i in range(section_start, section_end or len(lines)):
            m = _BRIEFING_ENABLED_RE.match(lines[i])
            if m:
                found_at = i
                break
        if found_at is not None:
            # Already set to the right value?
            if lines[found_at].split("=", 1)[1].strip().lower() == target:
                return False
            indent = _BRIEFING_ENABLED_RE.match(lines[found_at]).group("indent")  # type: ignore[union-attr]
            lines[found_at] = f"{indent}enabled = {target}"
        else:
            # Insert at the top of the section.
            lines.insert(section_start, f"enabled = {target}")
    else:
        # Append a fresh section to the end of the file.
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.append("[briefing]")
        lines.append(f"enabled = {target}")

    new_text = "\n".join(lines)
    if not new_text.endswith("\n"):
        new_text += "\n"
    if new_text == text:
        return False
    config_path.write_text(new_text)
    return True


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def _next_scheduled(
    settings: BriefingSettings, state: BriefingState, now_local: datetime,
) -> datetime:
    """Return the next local datetime the briefing is scheduled to fire.

    The actual firing is gated by (a) configured hour, (b) date dedupe,
    and (c) the hourly roster tick. This function reports when the next
    fire *would* occur given the persisted state.
    """
    target_hour = settings.hour
    tz = now_local.tzinfo
    # Candidate at today's configured hour.
    today_at_hour = now_local.replace(
        hour=target_hour, minute=0, second=0, microsecond=0,
    )
    today_iso = iso_date(now_local.date())
    already = state.last_briefing_date == today_iso
    if not already and now_local <= today_at_hour:
        return today_at_hour
    # Otherwise tomorrow at the configured hour.
    tomorrow = now_local.date().fromordinal(now_local.date().toordinal() + 1)
    return datetime(
        tomorrow.year, tomorrow.month, tomorrow.day,
        target_hour, 0, 0, tzinfo=tz,
    )


@briefing_app.command("status")
def status_cmd(
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Print briefing status (enabled, last fire, next fire, mode)."""
    path = _require_config(config_path)
    config = load_config(path)
    settings = load_briefing_settings(path)
    base_dir = config.project.base_dir
    state = load_state(base_dir)

    now_local = _current_local_now(settings, config)
    next_fire = _next_scheduled(settings, state, now_local)

    mode = "daily"
    if state.last_quiet_weekly_date:
        mode = "quiet"

    payload: dict[str, Any] = {
        "enabled": settings.enabled,
        "hour": settings.hour,
        "timezone": settings.timezone or getattr(getattr(config, "pollypm", None), "timezone", "") or "",
        "priorities_count": settings.priorities_count,
        "quiet_mode_after_days": settings.quiet_mode_after_days,
        "last_briefing_date": state.last_briefing_date or None,
        "last_fire_at": state.last_fire_at or None,
        "last_quiet_weekly_date": state.last_quiet_weekly_date or None,
        "next_scheduled_local": next_fire.isoformat(),
        "now_local": now_local.isoformat(),
        "mode": mode,
    }

    if as_json:
        _emit(payload, as_json=True)
        return

    typer.echo(f"enabled:               {'yes' if settings.enabled else 'no'}")
    typer.echo(f"hour (local):          {settings.hour:02d}:00")
    typer.echo(f"timezone:              {payload['timezone'] or '(system default)'}")
    typer.echo(f"priorities_count:      {settings.priorities_count}")
    typer.echo(f"quiet_mode_after_days: {settings.quiet_mode_after_days}")
    typer.echo(f"mode:                  {mode}")
    typer.echo(f"last briefing date:    {state.last_briefing_date or '(never)'}")
    typer.echo(f"last fire timestamp:   {state.last_fire_at or '(never)'}")
    typer.echo(f"now (local):           {now_local.isoformat()}")
    typer.echo(f"next scheduled:        {next_fire.isoformat()}")


# ---------------------------------------------------------------------------
# enable / disable
# ---------------------------------------------------------------------------


@briefing_app.command("enable")
def enable_cmd(
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Enable the daily briefing tick."""
    path = _require_config(config_path)
    changed = _set_briefing_enabled(path, True)
    payload = {"enabled": True, "changed": changed, "config_path": str(path)}
    if as_json:
        _emit(payload, as_json=True)
        return
    if changed:
        typer.echo("briefing: enabled")
    else:
        typer.echo("briefing: already enabled")


@briefing_app.command("disable")
def disable_cmd(
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Disable the daily briefing tick (until re-enabled)."""
    path = _require_config(config_path)
    changed = _set_briefing_enabled(path, False)
    payload = {"enabled": False, "changed": changed, "config_path": str(path)}
    if as_json:
        _emit(payload, as_json=True)
        return
    if changed:
        typer.echo("briefing: disabled")
    else:
        typer.echo("briefing: already disabled")


# ---------------------------------------------------------------------------
# now — force-fire
# ---------------------------------------------------------------------------


@briefing_app.command("now")
def now_cmd(
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Force-fire the briefing immediately (bypasses 6-a.m. gate + dedupe).

    Bypasses the configured hour, the "already briefed today" dedupe, and
    the ``enabled`` flag. Writes to inbox. Useful for manual trigger or
    acceptance testing.
    """
    path = _require_config(config_path)
    config = load_config(path)
    settings = load_briefing_settings(path)
    base_dir = config.project.base_dir
    project_root = config.project.root_dir

    # Use configured timezone for date_local + log timestamps.
    now_local = _current_local_now(settings, config)

    # Load state so fire_briefing can update the quiet-mode marker.
    state = load_state(base_dir)

    result = _tick.fire_briefing(
        project_root=project_root,
        base_dir=base_dir,
        settings=settings,
        now_local=now_local,
        state=state,
        config=config,
        emit_to_inbox=True,
    )

    if as_json:
        _emit(result, as_json=True)
        return

    if not result.get("fired"):
        typer.echo(f"briefing: not fired ({result.get('reason', 'unknown')})")
        raise typer.Exit(code=1)

    draft = result.get("draft") or {}
    date_local = draft.get("date_local") or iso_date(now_local.date())
    mode = draft.get("mode") or "?"
    emitted = result.get("emitted", False)
    typer.echo(f"briefing: fired ({mode}) for {date_local}")
    typer.echo(f"  inbox: {'yes' if emitted else 'no'}")
    md = draft.get("markdown")
    if md:
        typer.echo("")
        typer.echo(md)


# ---------------------------------------------------------------------------
# preview — gather + synthesize without inbox write
# ---------------------------------------------------------------------------


@briefing_app.command("preview")
def preview_cmd(
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Run the full gather + synthesize pipeline, printing the result.

    Does NOT write to the inbox. Intended for dry-runs and operator QA.
    """
    path = _require_config(config_path)
    config = load_config(path)
    settings = load_briefing_settings(path)
    base_dir = config.project.base_dir
    project_root = config.project.root_dir

    now_local = _current_local_now(settings, config)
    state = load_state(base_dir)

    result = _tick.fire_briefing(
        project_root=project_root,
        base_dir=base_dir,
        settings=settings,
        now_local=now_local,
        state=state,
        config=config,
        emit_to_inbox=False,
    )

    if as_json:
        _emit(result, as_json=True)
        return

    if not result.get("fired"):
        typer.echo(f"briefing preview: not fired ({result.get('reason', 'unknown')})")
        raise typer.Exit(code=1)

    draft = result.get("draft") or {}
    mode = draft.get("mode") or "?"
    date_local = draft.get("date_local") or iso_date(now_local.date())
    typer.echo(f"# Morning Briefing Preview — {date_local} (mode: {mode})")
    typer.echo("")
    md = draft.get("markdown") or ""
    typer.echo(md)


# ---------------------------------------------------------------------------
# pin
# ---------------------------------------------------------------------------


def _resolve_briefing_id(base_dir: Path, identifier: str) -> str:
    """Interpret ``identifier`` as a date (YYYY-MM-DD) or 'latest'.

    Returns the ``date_local`` of the matching briefing. Raises
    ``FileNotFoundError`` if nothing matches.
    """
    ident = identifier.strip()
    if ident.lower() in {"latest", "last"}:
        entries = list_briefings(base_dir, status="all", limit=1)
        if not entries:
            raise FileNotFoundError("No briefings exist yet.")
        return entries[0].date_local
    # Accept YYYY-MM-DD.
    try:
        _date.fromisoformat(ident)
    except ValueError as exc:
        raise FileNotFoundError(
            f"Invalid briefing id {ident!r}; expected YYYY-MM-DD or 'latest'."
        ) from exc
    return ident


@briefing_app.command("pin")
def pin_cmd(
    briefing_id: str = typer.Argument(
        ...,
        help="Briefing date (YYYY-MM-DD) or 'latest'.",
    ),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Pin a briefing so the sweep leaves it alone."""
    path = _require_config(config_path)
    config = load_config(path)
    base_dir = config.project.base_dir

    try:
        date_local = _resolve_briefing_id(base_dir, briefing_id)
        entry = _pin_briefing(base_dir, date_local)
    except FileNotFoundError as exc:
        if as_json:
            _emit({"pinned": False, "error": str(exc)}, as_json=True)
        else:
            typer.echo(f"briefing: {exc}", err=True)
        raise typer.Exit(code=1)

    if as_json:
        _emit({"pinned": True, "date_local": entry.date_local}, as_json=True)
        return
    typer.echo(f"briefing: pinned {entry.date_local}")
