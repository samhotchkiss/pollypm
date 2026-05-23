"""``pm audit`` CLI group — ad-hoc forensics over audit.jsonl files.

Contract:
- Inputs: Typer arguments/options for the ``pm audit grep`` subcommand
  (pattern + ``--project`` / ``--since`` / ``--event-type`` / ``--limit``).
- Outputs: ``audit_app`` Typer mounted under the root ``pm`` app.
- Side effects: reads from ``<project>/.pollypm/audit.jsonl`` (and
  rotated ``.gz`` archives) plus the central tail at
  ``~/.pollypm/audit/<project>.jsonl``. Never mutates.
- Invariants: streams lines (no whole-file slurp) so a multi-MB rotated
  archive doesn't blow memory. Filters cheap-to-expensive
  (project selection → ts → event → regex). Rotation-aware: walks the
  live ``.jsonl`` first, then ``.gz`` archives newest-first.

The forensic surface this replaces is operators shelling out::

    tail -1000 ~/dev/X/.pollypm/audit.jsonl | grep PATTERN | jq ...

That breaks after rotation (see ``audit/log.py::_maybe_rotate`` —
refs #2023 / #2032) because archived events live in ``.gz`` siblings
the ``tail`` invocation never sees. ``pm audit grep`` knows about the
rotation layout, gunzips transparently, and pretty-prints.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import typer

from pollypm.audit.query import (
    iter_matching_events as _iter_matching_events,
    parse_event_ts as _parse_event_ts,
    parse_since as _parse_since_neutral,
    resolve_target_files as _resolve_target_files,
    walk_log_chain as _walk_log_chain,
)
from pollypm.cli_help import help_with_examples
from pollypm.config import DEFAULT_CONFIG_PATH


audit_app = typer.Typer(
    help=help_with_examples(
        "Forensic queries over audit.jsonl files (rotation-aware).",
        [
            (
                'pm audit grep "watchdog.escalation"',
                "find every escalation dispatch across all projects",
            ),
            (
                'pm audit grep "stuck_draft" --project samblog --since 24h',
                "scope to one project + the last 24 hours",
            ),
            (
                'pm audit grep "." --event-type advisor.tick.fired --limit 20',
                "show the last 20 advisor ticks (pattern matches anything)",
            ),
        ],
        trailing=(
            "Filter order is cheap→expensive: project selection narrows "
            "the file set, then --since/--event-type are checked per line "
            "before the regex runs. Archives (`.gz`) are walked newest-"
            "first; older events stream out after the live tail."
        ),
    )
)


# ---------------------------------------------------------------------------
# Typer-friendly --since wrapper. Domain implementation lives in
# :mod:`pollypm.audit.query` so the HTTP surface can share it.
# ---------------------------------------------------------------------------


def parse_since(value: str) -> datetime:
    """Parse ``--since`` into a timezone-aware UTC datetime.

    Thin Typer wrapper around :func:`pollypm.audit.query.parse_since` —
    re-raises the neutral ``ValueError`` as :class:`typer.BadParameter`
    so the CLI surfaces the error inline instead of crashing with a
    stack trace.
    """
    try:
        return _parse_since_neutral(value)
    except ValueError as exc:
        raise typer.BadParameter(
            f"invalid --since value {value!r}: expected ISO-8601 "
            "(e.g. 2026-05-21T03:14:15Z) or shortcut like 1h / 24h / 7d"
        ) from exc


# ---------------------------------------------------------------------------
# CLI-specific formatting.
# ---------------------------------------------------------------------------


_STATUS_COLORS = {
    "warn": typer.colors.YELLOW,
    "error": typer.colors.RED,
    "ok": typer.colors.GREEN,
}


def format_event(record: dict, *, color: bool = True) -> str:
    """Pretty-format a single audit event for the terminal.

    Shape::

        2026-05-21T03:14:15Z [samblog] watchdog.escalation_dispatched \
            samblog/15 (warn) — Draft task ... unpromoted for >5 min

    The trailing summary comes from ``metadata.summary`` /
    ``metadata.message`` if present, otherwise a compact JSON dump of
    metadata (truncated). Severity in parens is colorized via
    :func:`typer.style` when ``color=True`` and stdout supports it.
    """
    ts_raw = str(record.get("ts", ""))
    parsed = _parse_event_ts(ts_raw)
    # Display ts in compact UTC ISO without microseconds for a stable
    # column width. Fall back to the raw value if parsing tripped so
    # operators still see *something* recognisable.
    if parsed is not None:
        ts_display = parsed.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        ts_display = ts_raw or "?"
    project = str(record.get("project") or "-")
    event = str(record.get("event") or "?")
    subject = str(record.get("subject") or "")
    status = str(record.get("status") or "ok")

    metadata = record.get("metadata") or {}
    summary = ""
    if isinstance(metadata, dict):
        for key in ("summary", "message", "reason", "title"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                summary = value.strip()
                break
        if not summary:
            try:
                summary = json.dumps(
                    metadata, ensure_ascii=False, separators=(",", ":"),
                )
            except (TypeError, ValueError):
                summary = repr(metadata)
            # Keep the trailing summary terse — operators wanting full
            # metadata should jq the live file.
            if len(summary) > 200:
                summary = summary[:197] + "..."

    status_token = f"({status})"
    if color and status in _STATUS_COLORS:
        status_token = typer.style(status_token, fg=_STATUS_COLORS[status])

    parts = [f"{ts_display} [{project}] {event}"]
    if subject:
        parts.append(subject)
    parts.append(status_token)
    line = " ".join(parts)
    if summary:
        line = f"{line} — {summary}"
    return line


# ---------------------------------------------------------------------------
# CLI command.
# ---------------------------------------------------------------------------


@audit_app.command(
    "grep",
    help=(
        "Search audit.jsonl files (and rotated .gz archives) for events "
        "matching a regex. Filters narrow the file set + line set before "
        "the pattern runs."
    ),
)
def audit_grep(
    pattern: str = typer.Argument(
        ...,
        help="Regex (Python re.search). Matches the raw JSONL line.",
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Scope to one project's logs (per-project + central tail).",
    ),
    since: str | None = typer.Option(
        None,
        "--since",
        help="Only events with ts >= this. ISO-8601 or shortcut (1h, 24h, 7d).",
    ),
    event_type: str | None = typer.Option(
        None,
        "--event-type",
        help="Exact match on the .event field (not a regex).",
    ),
    limit: int = typer.Option(
        100,
        "--limit",
        help="Cap output (0 = unlimited).",
    ),
    no_color: bool = typer.Option(
        False,
        "--no-color",
        help="Disable severity colorization.",
    ),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH,
        "--config",
        help="PollyPM config path.",
    ),
) -> None:
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise typer.BadParameter(f"invalid regex {pattern!r}: {exc}") from exc

    since_dt = parse_since(since) if since else None
    targets = _resolve_target_files(
        project_filter=project, config_path=config_path,
    )
    if not targets:
        typer.echo("no audit log files found", err=True)
        raise typer.Exit(code=1)

    emitted = 0
    use_color = (not no_color)
    for record in _iter_matching_events(
        targets=targets,
        pattern=compiled,
        since=since_dt,
        event_type=event_type,
    ):
        typer.echo(format_event(record, color=use_color))
        emitted += 1
        if limit and emitted >= limit:
            break

    if emitted == 0:
        typer.echo("no matching events", err=True)
        raise typer.Exit(code=1)


__all__ = [
    "audit_app",
    "format_event",
    "parse_since",
    # Re-exports from the neutral query module — kept for the CLI test
    # surface (``tests/test_cli_audit_grep.py``) which historically
    # imported the rotation-aware helpers via this module. Re-exporting
    # explicitly via ``__all__`` (rather than dropping them) keeps that
    # test surface stable AND silences F401 for the underscored aliases.
    "_iter_matching_events",
    "_parse_event_ts",
    "_parse_since_neutral",
    "_resolve_target_files",
    "_walk_log_chain",
]
