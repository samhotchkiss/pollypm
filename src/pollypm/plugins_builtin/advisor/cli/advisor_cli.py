"""``pm advisor`` CLI — history, status, pause/resume, enable/disable.

Spec: docs/advisor-plugin-spec.md §8 (history), §9 (CLI / config).
Shipped incrementally: ad04 added ``history``; ad06 adds the rest
(``status``, ``pause``, ``resume``, ``enable``, ``disable``).

``[advisor]`` config block in ``pollypm.toml``:

    [advisor]
    enabled = true             # master kill switch — default true
    cadence = "@every 30m"     # override for lower-noise projects

All config changes take effect on the next rail restart — the plugin
reads cadence once at ``register_roster`` time (spec §9).
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import typer

from pollypm.cli_help import help_with_examples
from pollypm.config import DEFAULT_CONFIG_PATH, load_config, resolve_config_path
from pollypm.plugins_builtin.advisor.handlers.history_log import (
    HistoryEntry,
    entries_in_window,
    stats,
)
from pollypm.plugins_builtin.advisor.settings import (
    DEFAULT_CADENCE,
    load_advisor_settings,
)
from pollypm.plugins_builtin.advisor.state import (
    AdvisorState,
    ProjectAdvisorState,
    is_paused,
    load_state,
    save_state,
)


advisor_app = typer.Typer(
    help=help_with_examples(
        "Manage the advisor (every-30m alignment coach).",
        [
            ("pm advisor status", "show advisor state and next run"),
            ("pm advisor pause", "stop runs without disabling the plugin"),
            ("pm advisor history --since 7d", "inspect recent advisor decisions"),
        ],
    ),
    no_args_is_help=True,
)


# An empty group callback forces Typer into multi-command routing mode so
# subcommands are dispatched by name. Without this, a Typer app with a
# single registered command collapses into "run that command directly",
# which breaks `pm advisor history …` once ad06 adds status/pause/etc.
@advisor_app.callback()
def _advisor_root() -> None:  # pragma: no cover — pure dispatch glue
    """Top-level advisor group callback — intentionally a no-op."""
    return None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _require_config(config_path: Path) -> Path:
    path = resolve_config_path(config_path)
    if not path.exists():
        typer.echo(
            f"No PollyPM config at {path}. Run `pm init` or `pm onboard` first.",
            err=True,
        )
        raise typer.Exit(code=1)
    return path


def _resolve_base_dir(config_path: Path) -> Path:
    """Resolve the ``.pollypm`` directory for the current install."""
    cfg = load_config(config_path)
    base_dir: Path = Path(cfg.project.base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir


_DURATION_RE = re.compile(r"^(?P<n>\d+)\s*(?P<u>[smhdw])$", re.IGNORECASE)


def _parse_since(value: str | None) -> datetime | None:
    """Parse ``--since`` into a UTC datetime.

    Accepted shapes:

    * Duration: ``24h``, ``7d``, ``30m``, ``2w``.
    * ISO-8601 absolute: ``2026-04-16T12:00:00+00:00``.
    * None → None (caller applies its own default).
    """
    if not value:
        return None
    value = value.strip()
    m = _DURATION_RE.match(value)
    if m:
        n = int(m.group("n"))
        unit = m.group("u").lower()
        seconds_per_unit = {
            "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800,
        }
        delta = timedelta(seconds=n * seconds_per_unit[unit])
        return datetime.now(UTC) - delta
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(
            f"--since must be a duration (e.g. 24h, 7d) or ISO-8601; got {value!r}"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _entry_to_dict(entry: HistoryEntry) -> dict[str, Any]:
    return entry.to_dict()


def _format_text(entry: HistoryEntry) -> str:
    """One-line text summary of an entry for human-readable output."""
    when = entry.timestamp or "(no-time)"
    prefix = f"{when} [{entry.project}] {entry.decision}"
    if entry.decision == "emit":
        topic = entry.topic or "other"
        severity = entry.severity or "?"
        return f"{prefix} {severity}/{topic} — {entry.summary}"
    return f"{prefix} — {entry.rationale_if_silent or '(no rationale recorded)'}"


# ---------------------------------------------------------------------------
# pm advisor history
# ---------------------------------------------------------------------------


@advisor_app.command("history")
def history_cmd(
    project: str | None = typer.Option(
        None, "--project", help="Filter entries by project key.",
    ),
    since: str | None = typer.Option(
        None, "--since", help="Only entries newer than this (e.g. 24h, 7d, ISO-8601).",
    ),
    decision: str | None = typer.Option(
        None, "--decision", help="Filter by decision: emit or silent.",
    ),
    stats_flag: bool = typer.Option(
        False, "--stats", help="Aggregate emit-rate + topic distribution.",
    ),
    limit: int = typer.Option(
        100, "--limit", help="Cap listing output (ignored with --stats).",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit machine-readable output.",
    ),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
) -> None:
    """Show recent advisor decisions — emit, silent, or both.

    No rate-limit side-effects; this is purely observability. Use the
    output to decide if the persona prompt needs tuning. If emit-rate
    trends above ~1/day/active-project sustained, the prompt — not a
    system-enforced limit — is what gets adjusted.
    """
    path = _require_config(config_path)
    base_dir = _resolve_base_dir(path)

    if decision is not None and decision not in {"emit", "silent"}:
        typer.echo("--decision must be 'emit' or 'silent'.", err=True)
        raise typer.Exit(code=1)

    since_dt = _parse_since(since)

    if stats_flag:
        summary = stats(
            base_dir,
            since=since_dt,
            project=project,
        )
        if as_json:
            typer.echo(json.dumps(summary, indent=2, sort_keys=True, default=str))
            return

        typer.echo(f"window: {summary['since']} -> {summary['until']}")
        typer.echo(f"total:  {summary['total']}")
        typer.echo(f"emits:  {summary['emit_count']}")
        typer.echo(f"silent: {summary['silent_count']}")
        typer.echo(f"rate:   {summary['emit_rate']}")
        if summary["per_project"]:
            typer.echo("")
            typer.echo("per-project:")
            for name, bucket in sorted(summary["per_project"].items()):
                typer.echo(
                    f"  {name}: {bucket['emit']}/{bucket['total']} "
                    f"(rate={bucket['emit_rate']})"
                )
        if summary["topic_distribution"]:
            typer.echo("")
            typer.echo("topic distribution:")
            for topic, count in sorted(
                summary["topic_distribution"].items(),
                key=lambda kv: (-kv[1], kv[0]),
            ):
                typer.echo(f"  {topic}: {count}")
        return

    entries = entries_in_window(
        base_dir,
        since=since_dt,
        project=project,
        decision=decision,
    )
    if limit > 0:
        entries = entries[-limit:]

    if as_json:
        typer.echo(
            json.dumps(
                [_entry_to_dict(e) for e in entries],
                indent=2,
                sort_keys=True,
                default=str,
            )
        )
        return

    if not entries:
        typer.echo("advisor: no history entries match filters.")
        return

    for e in entries:
        typer.echo(_format_text(e))


# ---------------------------------------------------------------------------
# [advisor].enabled toggle — text-level edit of pollypm.toml.
# ---------------------------------------------------------------------------
#
# Mirrors the morning_briefing cli pattern: locate the [advisor] section,
# replace the enabled line, or append the section if missing. We avoid
# tomli_w / dumping the whole config so comments and ordering survive.


_ADVISOR_ENABLED_RE = re.compile(
    r"^(?P<indent>\s*)enabled\s*=\s*(?P<val>true|false)\s*$",
    re.IGNORECASE,
)


def _set_advisor_enabled(config_path: Path, enabled: bool) -> bool:
    """Rewrite ``pollypm.toml`` so ``[advisor].enabled = <bool>``.

    Returns True when the file was modified, False when the value was
    already correct. Creates the ``[advisor]`` section if missing.
    """
    text = config_path.read_text()
    target = "true" if enabled else "false"

    lines = text.splitlines()
    section_start: int | None = None
    section_end: int | None = None
    for idx, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped == "[advisor]":
            section_start = idx + 1
            continue
        if section_start is not None and stripped.startswith("[") and stripped.endswith("]"):
            section_end = idx
            break
    if section_start is not None and section_end is None:
        section_end = len(lines)

    if section_start is not None:
        found_at: int | None = None
        for i in range(section_start, section_end or len(lines)):
            m = _ADVISOR_ENABLED_RE.match(lines[i])
            if m:
                found_at = i
                break
        if found_at is not None:
            if lines[found_at].split("=", 1)[1].strip().lower() == target:
                return False
            indent = _ADVISOR_ENABLED_RE.match(lines[found_at]).group("indent")  # type: ignore[union-attr]
            lines[found_at] = f"{indent}enabled = {target}"
        else:
            lines.insert(section_start, f"enabled = {target}")
    else:
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.append("[advisor]")
        lines.append(f"enabled = {target}")

    new_text = "\n".join(lines)
    if not new_text.endswith("\n"):
        new_text += "\n"
    if new_text == text:
        return False
    config_path.write_text(new_text)
    return True


# ---------------------------------------------------------------------------
# pm advisor enable / disable — config-level toggle.
# ---------------------------------------------------------------------------


@advisor_app.command("enable")
def enable_cmd(
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Enable the advisor for this install."""
    path = _require_config(config_path)
    changed = _set_advisor_enabled(path, True)
    payload = {"enabled": True, "changed": changed, "config_path": str(path)}
    if as_json:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    typer.echo("advisor: enabled" if changed else "advisor: already enabled")


@advisor_app.command("disable")
def disable_cmd(
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Disable the advisor (until re-enabled)."""
    path = _require_config(config_path)
    changed = _set_advisor_enabled(path, False)
    payload = {"enabled": False, "changed": changed, "config_path": str(path)}
    if as_json:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    typer.echo("advisor: disabled" if changed else "advisor: already disabled")


# ---------------------------------------------------------------------------
# pm advisor pause / resume — per-project pause_until marker.
# ---------------------------------------------------------------------------


def _parse_pause_until(
    *, hours: int | None, until: str | None,
) -> datetime:
    """Resolve a pause_until datetime from CLI flags.

    Priority: ``--until YYYY-MM-DD`` > ``--hours N`` > default 24h.
    Returns a tz-aware UTC datetime.
    """
    if until:
        value = until.strip()
        try:
            # Accept bare dates (YYYY-MM-DD) and full ISO-8601 strings.
            if "T" in value:
                dt = datetime.fromisoformat(value)
            else:
                dt = datetime.fromisoformat(f"{value}T23:59:59+00:00")
        except ValueError as exc:
            raise typer.BadParameter(
                f"--until must be YYYY-MM-DD or ISO-8601; got {until!r}"
            ) from exc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    delta_hours = hours if (hours and hours > 0) else 24
    return datetime.now(UTC) + timedelta(hours=delta_hours)


def _resolve_project_key(
    *, config, project_hint: str | None,
) -> str:
    """Pick a project key from CLI input, falling back to the ambient project."""
    if project_hint:
        return project_hint
    ambient_name = (
        getattr(getattr(config, "project", None), "name", None) or None
    )
    # Fall through to the ambient project name if known, otherwise the
    # single tracked project, otherwise a fallback string.
    if ambient_name:
        return ambient_name
    projects = getattr(config, "projects", {}) or {}
    tracked = [k for k, v in projects.items() if getattr(v, "tracked", False)]
    if len(tracked) == 1:
        return tracked[0]
    return "project"


@advisor_app.command("pause")
def pause_cmd(
    hours: int = typer.Option(
        24, "--hours", help="Pause duration in hours (ignored if --until set).",
    ),
    until: str | None = typer.Option(
        None, "--until", help="Explicit YYYY-MM-DD or ISO-8601 resume time.",
    ),
    project: str | None = typer.Option(
        None, "--project", help="Project key (defaults to the ambient project).",
    ),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Skip advisor ticks for a project until the given moment (default +24h)."""
    path = _require_config(config_path)
    cfg = load_config(path)
    base_dir = _resolve_base_dir(path)
    project_key = _resolve_project_key(config=cfg, project_hint=project)

    pause_until = _parse_pause_until(hours=hours, until=until)
    state = load_state(base_dir)
    proj_state = state.get(project_key)
    proj_state.pause_until = pause_until.isoformat()
    save_state(base_dir, state)

    payload = {
        "project": project_key,
        "pause_until": proj_state.pause_until,
    }
    if as_json:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    typer.echo(f"advisor: paused {project_key} until {proj_state.pause_until}")


@advisor_app.command("resume")
def resume_cmd(
    project: str | None = typer.Option(
        None, "--project", help="Project key (defaults to the ambient project).",
    ),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Clear a pause marker so the advisor resumes normal cadence."""
    path = _require_config(config_path)
    cfg = load_config(path)
    base_dir = _resolve_base_dir(path)
    project_key = _resolve_project_key(config=cfg, project_hint=project)

    state = load_state(base_dir)
    proj_state = state.get(project_key)
    was_paused = bool(proj_state.pause_until)
    proj_state.pause_until = ""
    save_state(base_dir, state)

    payload = {"project": project_key, "was_paused": was_paused}
    if as_json:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    if was_paused:
        typer.echo(f"advisor: resumed {project_key}")
    else:
        typer.echo(f"advisor: {project_key} was not paused")


# ---------------------------------------------------------------------------
# pm advisor status — current state + next-tick projection + 24h emit count.
# ---------------------------------------------------------------------------


def _next_scheduled(cadence: str, *, now_utc: datetime) -> str:
    """Best-effort "next tick" projection for display only.

    For ``@every <dur>`` cadences we project ``now + duration``; other
    shapes (cron, @hourly, etc) emit the cadence string as-is. The
    advisor's actual dispatch is driven by the roster — this field is
    a human-readable cue, not a load-bearing schedule.
    """
    cadence = (cadence or DEFAULT_CADENCE).strip()
    if cadence.startswith("@every"):
        rest = cadence[len("@every"):].strip()
        match = re.match(r"^(?P<n>\d+)\s*(?P<u>[smhd])$", rest, re.IGNORECASE)
        if match:
            n = int(match.group("n"))
            unit = match.group("u").lower()
            per_unit = {"s": 1, "m": 60, "h": 3600, "d": 86400}
            return (now_utc + timedelta(seconds=n * per_unit[unit])).isoformat()
    return cadence


@advisor_app.command("status")
def status_cmd(
    project: str | None = typer.Option(
        None, "--project", help="Project key (defaults to the ambient project).",
    ),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Print advisor status: enabled? paused? last tick? emits-in-24h?"""
    path = _require_config(config_path)
    cfg = load_config(path)
    settings = load_advisor_settings(path)
    base_dir = _resolve_base_dir(path)
    project_key = _resolve_project_key(config=cfg, project_hint=project)

    state = load_state(base_dir)
    proj_state: ProjectAdvisorState = state.get(project_key)

    now_utc = datetime.now(UTC)
    paused = is_paused(proj_state, now_utc=now_utc)

    # 24-hour emit count for the project, via the history log.
    since = now_utc - timedelta(hours=24)
    emit_window = entries_in_window(
        base_dir, since=since, project=project_key, decision="emit",
    )

    payload: dict[str, Any] = {
        "project": project_key,
        "plugin_enabled": settings.enabled,
        "project_enabled": proj_state.enabled,
        "cadence": settings.cadence,
        "paused": paused,
        "pause_until": proj_state.pause_until or None,
        "last_run": proj_state.last_run or None,
        "last_tick_at": proj_state.last_tick_at or None,
        "next_tick": _next_scheduled(settings.cadence, now_utc=now_utc),
        "emits_24h": len(emit_window),
        "now_utc": now_utc.isoformat(),
    }

    if as_json:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return

    typer.echo(f"project:          {payload['project']}")
    typer.echo(f"plugin enabled:   {'yes' if payload['plugin_enabled'] else 'no'}")
    typer.echo(f"project enabled:  {'yes' if payload['project_enabled'] else 'no'}")
    typer.echo(f"cadence:          {payload['cadence']}")
    typer.echo(f"paused:           {'yes' if payload['paused'] else 'no'}")
    if payload["pause_until"]:
        typer.echo(f"pause until:      {payload['pause_until']}")
    typer.echo(f"last run:         {payload['last_run'] or '(never)'}")
    typer.echo(f"last tick:        {payload['last_tick_at'] or '(never)'}")
    typer.echo(f"next tick:        {payload['next_tick']}")
    typer.echo(f"emits in last 24h: {payload['emits_24h']}")
