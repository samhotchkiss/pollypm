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
from pollypm.audit.log import (
    AGENT_REFUSAL_REASON_UNSIGNED_POLLYPM_CLAIM,
    AGENT_REFUSAL_REASONS,
    EVENT_AUDIT_FINDING_DISMISSED,
    EVENT_AGENT_INJECTION_FLAGGED,
    EVENT_AGENT_REFUSAL,
    audit_record_agent_refusal,
    audit_record_finding_dismissed,
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


def _validate_agent_refusal_reason(value: str) -> str:
    reason = str(value or "").strip()
    if reason not in AGENT_REFUSAL_REASONS:
        allowed = ", ".join(sorted(AGENT_REFUSAL_REASONS))
        raise typer.BadParameter(
            f"invalid --reason {value!r}: expected one of {allowed}"
        )
    return reason


def _resolve_project_path(
    *,
    project: str,
    config_path: Path,
) -> Path | None:
    """Best-effort project-root lookup for audit writer commands."""
    if not project or project == "_workspace":
        return None
    try:
        from pollypm.config import load_config

        config = load_config(config_path)
    except Exception:  # noqa: BLE001 — central-tail write still works
        return None
    known = config.projects.get(project)
    if known is None:
        return None
    return known.path


def _validate_nonempty_token(value: str, *, field: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise typer.BadParameter(f"{field} must not be empty")
    return clean


# ---------------------------------------------------------------------------
# CLI command.
# ---------------------------------------------------------------------------


@audit_app.command(
    "agent-refusal",
    help=(
        "Record that an agent refused a PollyPM-claimed control message "
        "because it was unsigned or had a bad auth marker. This is a "
        "narrow writer for refusal observability; it does not accept raw "
        "message text or arbitrary event names."
    ),
)
def audit_agent_refusal(
    reason: str = typer.Option(
        AGENT_REFUSAL_REASON_UNSIGNED_POLLYPM_CLAIM,
        "--reason",
        help=(
            "Refusal reason: unsigned-pollypm-claim or bad-auth-marker."
        ),
    ),
    project: str = typer.Option(
        "_workspace",
        "--project",
        help="Project key for the audit tail; use _workspace when unknown.",
    ),
    actor: str = typer.Option(
        "agent",
        "--actor",
        help="Agent/session label recording the refusal.",
    ),
    subject: str = typer.Option(
        "pollypm-auth",
        "--subject",
        help="Short non-secret subject label. Do not pass the raw message.",
    ),
    source: str = typer.Option(
        "pollypm-auth",
        "--source",
        help="Short source label such as pollypm-auth, watchdog, or operator.",
    ),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH,
        "--config",
        help="PollyPM config path.",
    ),
) -> None:
    clean_reason = _validate_agent_refusal_reason(reason)
    project_path = _resolve_project_path(
        project=project,
        config_path=config_path,
    )
    audit_record_agent_refusal(
        project=project,
        actor=actor,
        reason=clean_reason,
        source=source,
        subject=subject,
        project_path=project_path,
    )
    typer.echo(
        "recorded "
        f"{EVENT_AGENT_INJECTION_FLAGGED} and {EVENT_AGENT_REFUSAL} "
        f"for {project or '_workspace'}"
    )


@audit_app.command(
    "dismiss-finding",
    help=(
        "Record that a watchdog finding is an invalid project-scoped "
        "miscount. The watchdog treats the resulting audit.finding_dismissed "
        "event as a terminal resolution for that rule/project pair."
    ),
)
def audit_dismiss_finding(
    rule: str = typer.Argument(
        ...,
        help="Watchdog finding rule to dismiss, such as stuck_draft.",
    ),
    project: str = typer.Argument(
        ...,
        help="Project key whose finding should be suppressed.",
    ),
    reason: str = typer.Option(
        ...,
        "--reason",
        help="Cited evidence for why the finding is invalid/non-actionable.",
    ),
    actor: str = typer.Option(
        "agent",
        "--actor",
        help="Agent/session label recording the dismissal.",
    ),
    source: str = typer.Option(
        "watchdog",
        "--source",
        help="Short source label such as watchdog or operator.",
    ),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH,
        "--config",
        help="PollyPM config path.",
    ),
) -> None:
    clean_rule = _validate_nonempty_token(rule, field="rule")
    clean_project = _validate_nonempty_token(project, field="project")
    clean_reason = _validate_nonempty_token(reason, field="--reason")
    clean_actor = str(actor or "").strip() or "agent"
    clean_source = str(source or "").strip() or "watchdog"
    project_path = _resolve_project_path(
        project=clean_project,
        config_path=config_path,
    )
    audit_record_finding_dismissed(
        rule=clean_rule,
        project=clean_project,
        reason=clean_reason,
        actor=clean_actor,
        source=clean_source,
        project_path=project_path,
    )
    typer.echo(
        f"recorded {EVENT_AUDIT_FINDING_DISMISSED} "
        f"for {clean_project}/{clean_rule}"
    )


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
    "audit_agent_refusal",
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
