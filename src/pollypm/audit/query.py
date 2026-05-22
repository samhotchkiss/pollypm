"""Rotation-aware audit-log query helpers (CLI + HTTP shared core).

Public domain module owned by :mod:`pollypm.audit` so the HTTP surface
in :mod:`pollypm.web_api.routes.audit` can call the same rotation-aware
walker the CLI uses (``pm audit grep`` — see #2036) without reaching
into a presentation-layer ``cli_features.*`` module. The split was
introduced in PR #2062 round 1 review (Codex P1 boundary finding):

* CLI modules stay thin Typer adapters.
* HTTP routes call this module directly.
* Both surfaces share the same ``ts``-parsing + rotation-aware
  semantics so behaviour matches across the operator's `pm audit grep`
  invocation and a programmatic API client.

The helpers exposed here:

* :func:`parse_since` — ISO-8601 + ``<N><unit>`` shortcut parser.
  Raises :class:`ValueError` (not ``typer.BadParameter`` — caller is
  responsible for translating to its own error envelope).
* :func:`resolve_target_files` — picks the live + central-tail paths
  for one project (with a filter) or every registered project (without).
* :func:`walk_log_chain` — yields live ``.jsonl`` then ``.gz`` archives
  newest-first.
* :func:`open_log_lines` — text-mode line iterator (gzip-aware).
* :func:`parse_event_ts` — best-effort ``ts`` → ``datetime`` parser.
* :func:`iter_matching_events` — apply filters cheap→expensive and
  stream matching dict records.

Nothing here writes to disk; nothing imports from Typer or FastAPI.
"""

from __future__ import annotations

import gzip
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

from pollypm.audit.log import central_log_path
from pollypm.config import load_config
from pollypm.projects import project_audit_log_path


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
    """Parse a ``--since`` value into a timezone-aware UTC datetime.

    Accepts either:

    * an ISO-8601 timestamp (``2026-05-21T03:14:15+00:00`` /
      ``2026-05-21T03:14:15Z`` / ``2026-05-21`` etc.), or
    * a shortcut of the form ``<N><unit>`` where unit is one of
      ``s``/``m``/``h``/``d``/``w`` (seconds / minutes / hours /
      days / weeks). The result is ``now - delta`` in UTC.

    Naive ISO inputs are interpreted as UTC. Raises :class:`ValueError`
    on parse failure — callers (CLI / HTTP) translate to their own
    error envelopes.
    """
    match = _SHORTCUT_RE.match(value)
    if match:
        n = int(match.group(1))
        unit = _SHORTCUT_UNITS[match.group(2).lower()]
        delta = timedelta(**{unit: n})
        return datetime.now(timezone.utc) - delta
    iso = value.strip()
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise ValueError(
            f"invalid since value {value!r}: expected ISO-8601 "
            "(e.g. 2026-05-21T03:14:15Z) or shortcut like 1h / 24h / 7d"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_event_ts(ts: str) -> datetime | None:
    """Parse an audit-event ``ts`` string into a UTC datetime, or None."""
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


# ---------------------------------------------------------------------------
# File discovery — rotation-aware.
# ---------------------------------------------------------------------------


def _archive_sort_key(path: Path) -> tuple[float, str]:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (mtime, path.name)


def walk_log_chain(live: Path) -> Iterator[Path]:
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


def open_log_lines(path: Path) -> Iterator[str]:
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
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Target file selection — central + per-project, project filter aware.
# ---------------------------------------------------------------------------


def resolve_target_files(
    *,
    project_filter: str | None,
    config_path: Path | None = None,
    config: "object | None" = None,
) -> list[Path]:
    """Return the live audit-log paths to walk for this query.

    With ``project_filter`` set, only that project's per-project log +
    its central tail are returned. Without it, every registered
    project's per-project log + every central tail in the audit home
    is included so an operator can grep across the whole fleet.

    Per-project paths are returned even when they don't currently
    exist — :func:`walk_log_chain` filters those out, but we still
    include them so a project whose ``.pollypm`` was archived has a
    chance to surface via its central tail (which is added separately).

    Callers pass either ``config_path`` (CLI — loads from disk) or
    ``config`` (web API — already in memory). When both are supplied
    the in-memory ``config`` wins.
    """
    targets: list[Path] = []
    seen: set[Path] = set()

    def _add(path: Path) -> None:
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
        if config is not None:
            project = config.projects.get(project_filter)
            if project is not None:
                _add(project_audit_log_path(Path(project.path)))
        _add(central_log_path(project_filter))
        return targets

    if config is not None:
        for project in config.projects.values():
            try:
                _add(project_audit_log_path(Path(project.path)))
            except Exception:  # noqa: BLE001
                continue
            _add(central_log_path(project.key))

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
# Filtering.
# ---------------------------------------------------------------------------


def iter_matching_events(
    *,
    targets: Iterable[Path],
    pattern: re.Pattern[str] | None = None,
    literal: str | None = None,
    since: datetime | None,
    event_type: str | None,
) -> Iterator[dict]:
    """Stream parsed events from ``targets`` that pass every filter.

    Exactly one of ``pattern`` (compiled regex) or ``literal`` (plain
    substring) should be supplied — both ``None`` means "match every
    line." Supplying both is allowed (regex wins) but discouraged.

    Filters apply in cheap→expensive order:

    1. ``literal`` / ``pattern`` substring or regex match on the raw
       line.
    2. JSON decode.
    3. ``event_type``: exact ``.event`` field match (string equality,
       NOT regex).
    4. ``since``: drop events with ``ts < since``.

    Malformed JSON lines are skipped silently — the live audit log can
    have a truncated tail mid-write and we don't want one bad line to
    mask the rest of the matches.
    """
    since_iso = since.isoformat() if since is not None else None
    for path in targets:
        for chain_path in walk_log_chain(path):
            for line in open_log_lines(chain_path):
                stripped = line.strip()
                if not stripped:
                    continue
                # Cheap reject — literal substring is much cheaper than
                # regex and immune to catastrophic backtracking.
                if literal:
                    if literal not in stripped:
                        continue
                elif pattern is not None and pattern.pattern:
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
                    if ts_str and ts_str < since_iso:
                        parsed = parse_event_ts(ts_str)
                        if parsed is None or parsed < since:
                            continue
                yield record


__all__ = [
    "iter_matching_events",
    "open_log_lines",
    "parse_event_ts",
    "parse_since",
    "resolve_target_files",
    "walk_log_chain",
]
