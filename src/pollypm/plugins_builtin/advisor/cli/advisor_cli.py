"""``pm advisor`` CLI — history, status, pause/resume, enable/disable.

ad04 ships ``pm advisor history`` (the sole observability surface for
tuning the persona — NOT a rate limit). ad06 will mount the rest of
the commands in this Typer app (status / pause / resume / enable /
disable / history-stats). We declare the full app skeleton here so
ad06 only has to add subcommands — not re-wire the mount.

Spec: docs/advisor-plugin-spec.md §8 (history), §9 (CLI / config).
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import typer

from pollypm.config import DEFAULT_CONFIG_PATH, load_config, resolve_config_path
from pollypm.plugins_builtin.advisor.handlers.history_log import (
    HistoryEntry,
    entries_in_window,
    stats,
)


advisor_app = typer.Typer(
    help="Manage the advisor (every-30m alignment coach).",
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
    """Resolve the ``.pollypm-state`` directory for the current install."""
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
