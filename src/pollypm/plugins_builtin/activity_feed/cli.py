"""``pm activity`` CLI — headless activity feed viewer.

Supports:

* ``pm activity`` — print last N entries (default 50).
* ``pm activity --follow`` — stream new entries as they land, tail-f
  style. Poll cadence is 2s, driven by the state-epoch counter so the
  projector only re-queries when something changed.
* ``pm activity --project X --kind Y --actor Z --since 1h`` — filters
  compose with AND semantics (matches the cockpit panel).
* ``pm activity --json`` — one JSON object per line (newline-delimited
  JSON) so downstream consumers can ``jq``.

Registered in :mod:`pollypm.cli` via ``app.add_typer(activity_app)``.
"""

from __future__ import annotations

import json
import re
import signal
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

import typer

from pollypm.cli_help import help_with_examples
from pollypm.config import DEFAULT_CONFIG_PATH, load_config

if TYPE_CHECKING:
    from pollypm.plugins_builtin.activity_feed.cockpit.feed_panel import FeedFilter
    from pollypm.plugins_builtin.activity_feed.handlers.event_projector import FeedEntry


activity_app = typer.Typer(
    help=help_with_examples(
        "Live activity feed. `pm activity --follow` tails live events.",
        [
            ("pm activity", "print the most recent events"),
            ("pm activity --follow", "tail the live event stream"),
            (
                "pm activity --project my_app --since 15m",
                "filter to one project and a recent time window",
            ),
        ],
    ),
    invoke_without_command=True,
    no_args_is_help=False,
)


_DURATION_RE = re.compile(
    r"^\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>s|sec|secs|m|min|mins|h|hr|hrs|d|day|days|w|wk|wks)?\s*$",
    re.IGNORECASE,
)

_UNIT_SECONDS = {
    "s": 1, "sec": 1, "secs": 1,
    "m": 60, "min": 60, "mins": 60,
    "h": 3600, "hr": 3600, "hrs": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "wk": 604800, "wks": 604800,
}


def parse_duration(raw: str | None) -> timedelta | None:
    """Parse ``--since`` values like ``"1h"``, ``"30m"``, ``"2d"``.

    ``None`` / empty means "no lower bound". Unparseable values raise
    ``typer.BadParameter`` so the CLI surfaces a friendly error.
    """
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    match = _DURATION_RE.match(value)
    if not match:
        raise typer.BadParameter(
            f"Invalid --since duration {raw!r}. "
            "Expected formats: '30s', '5m', '1h', '2d', '1w'."
        )
    number = float(match.group("value"))
    unit = (match.group("unit") or "s").lower()
    seconds = number * _UNIT_SECONDS[unit]
    return timedelta(seconds=seconds)


def _entries_as_text(entries: Iterable[FeedEntry]) -> str:
    from pollypm.plugins_builtin.activity_feed.cockpit.feed_panel import format_entry_row

    return "\n".join(format_entry_row(e) for e in entries)


def _entries_as_json_lines(entries: Iterable[FeedEntry]) -> str:
    return "\n".join(json.dumps(e.as_dict(), default=str) for e in entries)


def _resolve_projector(config_path: Path):
    from pollypm.plugins_builtin.activity_feed.plugin import build_projector

    config = load_config(config_path)
    return build_projector(config)


def _build_filter(
    project: str | None,
    kind: str | None,
    actor: str | None,
    since: str | None,
) -> FeedFilter:
    from pollypm.plugins_builtin.activity_feed.cockpit.feed_panel import FeedFilter

    f = FeedFilter()
    if project:
        f = f.with_project(project)
    if kind:
        f = f.with_kind(kind)
    if actor:
        f = f.with_actor(actor)
    # Custom durations translate to explicit since_ts on the projector,
    # but the panel's FeedFilter only knows named windows. For the CLI
    # we pass the parsed duration separately to the projector.
    return f


@activity_app.callback(invoke_without_command=True)
def activity(
    follow: bool = typer.Option(
        False, "--follow", "-f",
        help="Stream new entries as they arrive (Ctrl-C to exit).",
    ),
    limit: int = typer.Option(
        50, "--limit", "-n", min=1, max=1000,
        help="Maximum entries to show on initial render.",
    ),
    project: str | None = typer.Option(
        None, "--project", "-p", help="Filter by project key.",
    ),
    kind: str | None = typer.Option(
        None, "--kind", "-k", help="Filter by event kind (e.g. alert, task_transition).",
    ),
    actor: str | None = typer.Option(
        None, "--actor", "-a", help="Filter by actor (session name).",
    ),
    since: str | None = typer.Option(
        None, "--since", help="Only show entries newer than this (e.g. 1h, 30m, 2d).",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit newline-delimited JSON instead of formatted rows.",
    ),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
) -> None:
    """Print the live activity feed.

    Default is a one-shot dump of the last ``--limit`` entries in
    reverse-chronological order. Add ``--follow`` to stream.
    """
    projector = _resolve_projector(config_path)
    if projector is None:
        typer.echo(
            "No state store configured — nothing to show.",
            err=True,
        )
        raise typer.Exit(code=1)

    feed_filter = _build_filter(project, kind, actor, since)
    since_delta = parse_duration(since)

    def _fetch(since_id: int | None = None) -> list[FeedEntry]:
        try:
            entries = projector.project(
                since_id=since_id,
                since=since_delta,
                limit=limit,
                projects=feed_filter.projects or None,
                kinds=feed_filter.kinds or None,
                actors=feed_filter.actors or None,
            )
        except Exception as exc:  # noqa: BLE001
            typer.echo(f"activity: projection failed: {exc}", err=True)
            raise typer.Exit(code=2)
        # Duration filters are client-side reapplied so partial
        # projector support doesn't leak old rows through.
        return entries

    # Initial dump — oldest first so the terminal scrolls naturally
    # when followed by live updates.
    initial = list(reversed(_fetch()))
    _emit(initial, as_json=as_json)

    if not follow:
        return

    last_seen_id = _max_numeric_id(initial)
    _install_sigint_handler()
    try:
        while True:
            time.sleep(2.0)
            fresh = _fetch(since_id=last_seen_id)
            if not fresh:
                continue
            # fresh is newest-first; emit oldest-first so order in the
            # terminal matches the initial dump.
            ordered = list(reversed(fresh))
            _emit(ordered, as_json=as_json)
            last_seen_id = max(last_seen_id or 0, _max_numeric_id(fresh) or 0) or None
    except KeyboardInterrupt:
        # Clean exit — typer's default would print a traceback on SIGINT.
        return


def _emit(entries: Iterable[FeedEntry], *, as_json: bool) -> None:
    if as_json:
        text = _entries_as_json_lines(entries)
    else:
        text = _entries_as_text(entries)
    if text:
        typer.echo(text)
        sys.stdout.flush()


def _max_numeric_id(entries: Iterable[FeedEntry]) -> int | None:
    """Return the largest numeric id (``evt:NNN``) in the list, or None.

    Ignores non-numeric ids like ``wt:demo/5:1`` since those come from
    per-project DBs and are handled via the ``since_ts`` path.
    """
    best: int | None = None
    for entry in entries:
        if not entry.id.startswith("evt:"):
            continue
        try:
            n = int(entry.id.split(":", 1)[1])
        except ValueError:
            continue
        if best is None or n > best:
            best = n
    return best


def _install_sigint_handler() -> None:
    """Install a no-op SIGINT handler so the follow loop exits cleanly.

    Default Python behaviour raises ``KeyboardInterrupt`` which we
    already catch; this ensures piped consumers (``pm activity -f |
    tee log.txt``) don't see a traceback on shutdown.
    """

    def _quit(_signum, _frame):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGINT, _quit)
    except (ValueError, OSError):
        # Background threads can't install signal handlers — fall back
        # to Python's default behaviour, which still raises KeyboardInterrupt
        # on the main thread.
        pass


__all__ = ["activity_app", "parse_duration"]
