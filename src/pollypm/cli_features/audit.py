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

import gzip
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

import typer

from pollypm.audit.log import central_log_path
from pollypm.cli_help import help_with_examples
from pollypm.config import DEFAULT_CONFIG_PATH, load_config
from pollypm.projects import project_audit_log_path


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
# --since parsing (ISO 8601 or shortcuts like ``1h`` / ``24h`` / ``7d``).
# ---------------------------------------------------------------------------


_SHORTCUT_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)
_SHORTCUT_UNITS = {
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
    "w": "weeks",
}


def parse_since(value: str) -> datetime:
    """Parse ``--since`` into a timezone-aware UTC datetime.

    Accepts either:

    * an ISO-8601 timestamp (``2026-05-21T03:14:15+00:00`` /
      ``2026-05-21T03:14:15Z`` / ``2026-05-21`` etc.), or
    * a shortcut of the form ``<N><unit>`` where unit is one of
      ``s``/``m``/``h``/``d``/``w`` (seconds / minutes / hours /
      days / weeks). The result is ``now - delta`` in UTC.

    Naive ISO inputs are interpreted as UTC. Raises
    :class:`typer.BadParameter` on parse failure so the CLI surfaces
    the error inline instead of crashing with a stack trace.
    """
    match = _SHORTCUT_RE.match(value)
    if match:
        n = int(match.group(1))
        unit = _SHORTCUT_UNITS[match.group(2).lower()]
        delta = timedelta(**{unit: n})
        return datetime.now(timezone.utc) - delta
    # ISO-8601 path. ``fromisoformat`` accepts ``Z`` suffix on 3.11+ but
    # we mirror the audit-log emit format which uses ``+00:00`` — strip
    # a trailing ``Z`` to stay portable across Python versions.
    iso = value.strip()
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise typer.BadParameter(
            f"invalid --since value {value!r}: expected ISO-8601 "
            "(e.g. 2026-05-21T03:14:15Z) or shortcut like 1h / 24h / 7d"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# File discovery — rotation-aware.
# ---------------------------------------------------------------------------


def _archive_sort_key(path: Path) -> tuple[float, str]:
    """Sort key for ``audit.jsonl.<ts>[.bump].gz`` archives.

    We want newest-first iteration. Use mtime as primary because the
    rotation timestamp is embedded in the filename but tests + edge
    cases can leave clock-skew; fall back to filename for stable
    ordering within the same mtime second.
    """
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (mtime, path.name)


def _walk_log_chain(live: Path) -> Iterator[Path]:
    """Yield ``live`` first, then ``live.<ts>[.bump].gz`` archives newest-first.

    Yields only paths that exist. The caller is responsible for
    opening with the right decompressor (``open`` vs ``gzip.open``).
    Missing parent directory is treated as no-files.
    """
    if live.exists():
        yield live
    parent = live.parent
    if not parent.exists():
        return
    prefix = live.name + "."
    archives: list[Path] = []
    try:
        for sibling in parent.iterdir():
            if not sibling.is_file():
                continue
            if not sibling.name.startswith(prefix):
                continue
            if not sibling.name.endswith(".gz"):
                continue
            archives.append(sibling)
    except OSError:
        return
    archives.sort(key=_archive_sort_key, reverse=True)
    for archive in archives:
        yield archive


def _open_log_lines(path: Path) -> Iterator[str]:
    """Stream decoded text lines from a live ``.jsonl`` or gzipped archive.

    Routes ``.gz`` paths through :func:`gzip.open` in text mode so the
    caller gets the same line iteration shape regardless of file
    format. Decoding errors are swallowed per-file (best-effort: a
    corrupt archive shouldn't break grep across the other files).
    """
    try:
        if path.suffix == ".gz":
            fh = gzip.open(path, "rt", encoding="utf-8", errors="replace")
        else:
            fh = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return
    try:
        for line in fh:
            yield line
    except OSError:
        return
    finally:
        try:
            fh.close()
        except Exception:  # noqa: BLE001 — close-time errors are noise
            pass


# ---------------------------------------------------------------------------
# Target file selection — central + per-project, project filter aware.
# ---------------------------------------------------------------------------


def _resolve_target_files(
    *,
    project_filter: str | None,
    config_path: Path | None = None,
    config: "object | None" = None,
) -> list[Path]:
    """Return the live audit-log paths to walk for this query.

    With ``--project NAME`` set, only that project's per-project log +
    its central tail are returned. Without it, every registered
    project's per-project log + every central tail in the audit home
    is included so an operator can grep across the whole fleet.

    Per-project paths are returned even when they don't currently
    exist — :func:`_walk_log_chain` filters those out, but we still
    include them so a project whose ``.pollypm`` was archived has a
    chance to surface via its central tail (which is added separately).

    Callers pass either ``config_path`` (CLI — loads from disk) or
    ``config`` (web API — already in memory). When both are supplied
    the in-memory ``config`` wins; the CLI never sets ``config`` so
    behaviour is unchanged for the existing ``pm audit grep`` path.
    """
    targets: list[Path] = []
    seen: set[Path] = set()

    def _add(path: Path) -> None:
        # Use ``resolve()`` so symlinked workspaces don't double-up.
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key in seen:
            return
        seen.add(key)
        targets.append(path)

    if config is None and config_path is not None:
        try:
            config = load_config(config_path)
        except Exception:  # noqa: BLE001
            config = None

    if project_filter is not None:
        # Per-project log (best path: live config). When config can't
        # be loaded or the project isn't registered, fall through to
        # the central-tail-only branch — the operator may be grepping
        # a project that was removed from config but whose central
        # tail still carries the relevant trail.
        if config is not None:
            project = config.projects.get(project_filter)
            if project is not None:
                _add(project_audit_log_path(Path(project.path)))
        _add(central_log_path(project_filter))
        return targets

    # No filter: walk every registered project + every central tail.
    if config is not None:
        for project in config.projects.values():
            try:
                _add(project_audit_log_path(Path(project.path)))
            except Exception:  # noqa: BLE001
                continue
            _add(central_log_path(project.key))

    # Also include any central-tail files for projects that aren't
    # in config (renamed / removed projects whose tail still exists).
    central_root = central_log_path("_probe").parent
    if central_root.exists():
        try:
            for sibling in central_root.iterdir():
                if not sibling.is_file():
                    continue
                if sibling.suffix != ".jsonl":
                    continue
                _add(sibling)
        except OSError:
            pass

    return targets


# ---------------------------------------------------------------------------
# Filtering + formatting.
# ---------------------------------------------------------------------------


_STATUS_COLORS = {
    "warn": typer.colors.YELLOW,
    "error": typer.colors.RED,
    "ok": typer.colors.GREEN,
}


def _parse_event_ts(ts: str) -> datetime | None:
    """Parse an audit event ``ts`` field into a UTC datetime, or None."""
    if not ts:
        return None
    raw = ts.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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


def _iter_matching_events(
    *,
    targets: Iterable[Path],
    pattern: re.Pattern[str],
    since: datetime | None,
    event_type: str | None,
) -> Iterator[dict]:
    """Stream parsed events from ``targets`` that pass every filter.

    Filters apply in cheap→expensive order:

    1. ``event_type``: exact ``.event`` field match (string equality,
       NOT regex).
    2. ``since``: drop events with ``ts < since``.
    3. ``pattern``: ``re.search`` over the full line text. Matching the
       raw line (not a stringified parse) means operators can grep
       metadata fields without knowing the JSON shape.

    Malformed JSON lines are skipped silently — the live audit log
    can have a truncated tail mid-write and we don't want one bad
    line to mask the rest of the matches.
    """
    since_iso = since.isoformat() if since is not None else None
    for path in targets:
        for chain_path in _walk_log_chain(path):
            for line in _open_log_lines(chain_path):
                stripped = line.strip()
                if not stripped:
                    continue
                # Pattern match first against the raw line — fastest
                # possible reject for non-matches, which dominate in
                # a typical grep against a long log.
                if not pattern.search(stripped):
                    continue
                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                if event_type is not None and record.get("event") != event_type:
                    continue
                if since_iso is not None:
                    ts_str = str(record.get("ts", ""))
                    # ISO-8601 UTC sorts lexicographically; compare as
                    # strings to skip a datetime parse on the hot path.
                    if ts_str and ts_str < since_iso:
                        # Parse-and-compare fallback for the rare ts
                        # that doesn't lex-sort against our since
                        # (e.g. naive iso vs aware). Cheap because we
                        # only land here for matches.
                        parsed = _parse_event_ts(ts_str)
                        if parsed is None or parsed < since:
                            continue
                yield record


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
]
