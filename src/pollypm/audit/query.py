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
* :func:`aggregate_recent_stats` — stats-specific recent-window
  aggregation optimized for the Web UI Activity rollup.

Nothing here writes to disk; nothing imports from Typer or FastAPI.
"""

from __future__ import annotations

import gzip
import json
import logging
import multiprocessing
import multiprocessing.connection
import re
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, MutableMapping

from pollypm.audit.log import central_log_path
from pollypm.config import load_config
from pollypm.projects import project_audit_log_path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bounded-time regex search — ReDoS guardrail (PR #2062 round 2 Codex P0).
# ---------------------------------------------------------------------------
#
# Stdlib ``re`` has no per-call timeout. The 200-char pattern cap added in
# round 1 keeps the *pattern* small but does nothing to stop a short
# pathological pattern like ``(a+)+b`` against backtracking-heavy input
# (each input line independently triggers catastrophic backtracking that
# the C engine will not surface back to Python until it finishes — and
# the engine holds the GIL throughout, so daemon threads don't help
# either: the calling thread can't wake up to honour its timeout).
#
# The only stdlib path that actually interrupts a runaway match is the
# OS killing the process. We pay for that with a fork per request: a
# child worker is spawned lazily the first time we need to match a
# regex line, and the parent drives it via a Pipe with a per-line
# wall-clock deadline. When a line exceeds the deadline the child is
# ``terminate()``d (kernel signal — works through the GIL because the
# C runtime checks pending signals at every regex backtrack step in
# CPython's signal-aware loop), a fresh child is spawned, and the
# diagnostic counter is bumped. Worst-case cost: ``(matched_lines + N_timeouts) * fork_cost``.
#
# We deliberately don't ship a long-lived pool because the only point
# of forking is to make ``terminate()`` available — sharing a worker
# across requests would let one ReDoS query break the next request's
# search even without a timeout.
_PER_LINE_TIMEOUT_S = 0.1  # 100 ms per line is well above any sane operator regex.

# Startup is much slower than steady state: spawning a forkserver helper
# (first call only) plus forking and importing ``pollypm.audit.query``
# in the child can take ~1-2 s on a cold macOS test runner. We grant the
# very first line a generous startup grace so the legitimate operator
# query doesn't get falsely timed out by fork-server warmup. Once the
# child is alive and answering, subsequent lines are bound by
# ``_PER_LINE_TIMEOUT_S``.
_STARTUP_GRACE_S = 5.0

# ``fork`` is fastest but unsafe in multi-threaded parents (TestClient,
# FastAPI workers), and CPython deprecated it on Linux/macOS in 3.12+.
# ``forkserver`` runs a tiny single-threaded helper that fork()s the
# worker on demand — safe with threads, only available on POSIX.
# ``spawn`` is the fallback (Windows or unusual platforms) but is
# significantly slower (~200 ms per child). The choice happens once at
# import; on the supported platforms (macOS / Linux) we always land on
# ``forkserver``.
try:
    _MP_CTX = multiprocessing.get_context("forkserver")
    # Preload our own module in the forkserver helper so each child
    # fork doesn't have to re-import it. Cuts steady-state respawn
    # cost from ~80 ms (cold import) to ~10 ms (fork only).
    try:
        _MP_CTX.set_forkserver_preload(["pollypm.audit.query"])
    except Exception:  # noqa: BLE001 — best-effort optimisation
        pass
except ValueError:
    _MP_CTX = multiprocessing.get_context("spawn")


def _regex_worker_main(
    pattern_str: str,
    request_conn: "multiprocessing.connection.Connection",
) -> None:
    """Worker entry point. Compiles ``pattern_str`` once and loops on lines."""
    compiled = re.compile(pattern_str)
    while True:
        try:
            line = request_conn.recv()
        except (EOFError, OSError):
            return
        if line is None:  # graceful shutdown sentinel
            return
        try:
            match = compiled.search(line)
        except Exception:  # noqa: BLE001
            match = None
        try:
            request_conn.send(bool(match))
        except (BrokenPipeError, OSError):
            return


class _BoundedRegexSession:
    """Per-request driver for the regex worker process.

    Lazily spawns a child the first time :meth:`search` is called, and
    keeps the same child for subsequent lines. When a search exceeds
    the per-line deadline the child is killed and a fresh one is
    spawned on the next call — the abandoned worker can take as long
    as it needs to finish backtracking; the OS reaps it.

    Call :meth:`close` (or use as a context manager) at the end of the
    request to terminate the child cleanly.
    """

    def __init__(self, pattern: re.Pattern[str]) -> None:
        self._pattern_str = pattern.pattern
        self._process: multiprocessing.Process | None = None
        self._conn: "multiprocessing.connection.Connection | None" = None
        # First call to a fresh worker pays for forkserver warmup +
        # ``import pollypm.audit.query`` in the child; subsequent calls
        # only pay the round-trip. Track first-call separately so the
        # initial line isn't falsely flagged as a ReDoS timeout.
        self._needs_warmup = True

    def __enter__(self) -> "_BoundedRegexSession":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _spawn(self) -> None:
        parent_conn, child_conn = _MP_CTX.Pipe(duplex=True)
        process = _MP_CTX.Process(
            target=_regex_worker_main,
            args=(self._pattern_str, child_conn),
            name="audit-regex-worker",
            daemon=True,
        )
        process.start()
        # Close the child end in the parent so EOF propagates when the
        # child dies (otherwise our recv() blocks forever).
        child_conn.close()
        self._process = process
        self._conn = parent_conn

    def search(
        self, line: str, *, max_wall_clock_s: float | None = None
    ) -> tuple[bool, bool]:
        """Return ``(matched, timed_out)`` for ``line``.

        On timeout: kills the child, returns ``(False, True)``. The
        next call respawns. On crash: returns ``(False, True)`` too —
        we'd rather count a phantom timeout than silently drop matches.

        ``max_wall_clock_s`` (Codex round-5 finding, PR #2062) clamps
        BOTH the steady-state per-line timeout AND the first-call
        startup grace to the remaining request budget. Without this,
        an operator who specifies ``deadline_seconds=0.5`` on a regex
        request can still pay the full ``_STARTUP_GRACE_S = 5.0`` on
        the first line before the walker can truncate — contradicting
        the request-level deadline contract.
        """
        if self._process is None or self._conn is None:
            self._spawn()
        assert self._conn is not None
        assert self._process is not None
        try:
            self._conn.send(line)
        except (BrokenPipeError, OSError):
            self._teardown()
            return (False, True)
        # Wait for the child's response with a wall-clock deadline.
        # ``poll`` is the only Pipe API that takes a timeout in stdlib
        # multiprocessing.
        base_deadline = (
            _STARTUP_GRACE_S if self._needs_warmup else _PER_LINE_TIMEOUT_S
        )
        self._needs_warmup = False
        if max_wall_clock_s is not None:
            # Clamp to remaining request budget. ``max(0.0, …)`` keeps
            # ``poll`` from rejecting a negative timeout if the caller
            # is already past their deadline — in that case we want a
            # non-blocking poll that returns immediately.
            deadline = max(0.0, min(base_deadline, max_wall_clock_s))
        else:
            deadline = base_deadline
        if not self._conn.poll(deadline):
            self._teardown()
            return (False, True)
        try:
            matched = bool(self._conn.recv())
        except (EOFError, OSError):
            self._teardown()
            return (False, True)
        return (matched, False)

    def _teardown(self) -> None:
        if self._process is not None:
            try:
                self._process.terminate()
            except Exception:  # noqa: BLE001
                pass
            try:
                self._process.join(timeout=0.5)
            except Exception:  # noqa: BLE001
                pass
            if self._process.is_alive():
                try:
                    self._process.kill()
                except Exception:  # noqa: BLE001
                    pass
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
        self._process = None
        self._conn = None
        # Don't re-arm warmup grace for respawns: by the time a respawn
        # happens, the forkserver helper is already running and module
        # imports are cached, so subsequent forks land within the
        # per-line timeout. Re-arming would let a sustained ReDoS
        # attack reset the budget on every line.

    def close(self) -> None:
        # Polite shutdown: send sentinel, give the child a moment, then
        # terminate if it ignores us.
        if self._conn is not None and self._process is not None and self._process.is_alive():
            try:
                self._conn.send(None)
            except (BrokenPipeError, OSError):
                pass
        self._teardown()


def safe_pattern_search(
    pattern: re.Pattern[str], line: str
) -> re.Match[str] | None:
    """Bounded-time wrapper around ``pattern.search``.

    Compatibility shim — spawns a one-shot worker process, runs the
    search with the per-line timeout, and returns the match (or
    ``None`` on no-match / timeout / crash). Callers that need to
    distinguish timeout from no-match should use
    :class:`_BoundedRegexSession` directly so they can read both
    return-tuple fields.

    Because each call forks its own worker, this helper is only
    appropriate for one-off lookups. The walker in
    :func:`iter_matching_events` uses :class:`_BoundedRegexSession`
    instead so the fork cost is amortised across an entire request.
    """
    with _BoundedRegexSession(pattern) as session:
        matched, _timed_out = session.search(line)
    if not matched:
        return None
    # The worker only returned a bool to keep the IPC payload tiny; the
    # caller of this helper only needs match-or-not, so we re-run the
    # search in-process on the matched line to materialise the Match
    # object. This is safe because we already know the line did NOT
    # cause catastrophic backtracking (otherwise the worker would have
    # timed out instead of returning True).
    return pattern.search(line)


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
_TAIL_READ_CHUNK_BYTES = 128 * 1024
_ARCHIVE_TS_RE = re.compile(r"\.(\d+)(?:\.\d+)?\.gz$")


@dataclass(slots=True)
class AuditStatsAggregate:
    """Aggregated audit counts plus the route diagnostic counters."""

    total: int = 0
    by_event: dict[str, int] = field(default_factory=dict)
    by_severity: dict[str, int] = field(default_factory=dict)
    truncated_by_deadline: bool = False
    lines_scanned: int = 0
    corrupt_archives_skipped: int = 0
    malformed_rows_skipped: int = 0


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


def open_log_lines(
    path: Path,
    *,
    stats: MutableMapping[str, int] | None = None,
) -> Iterator[str]:
    """Stream decoded text lines from a live ``.jsonl`` or gzipped archive.

    Routes ``.gz`` paths through :func:`gzip.open` in text mode so the
    caller gets the same line iteration shape regardless of file
    format. Decoding errors are swallowed per-file (best-effort: a
    corrupt archive shouldn't break grep across the other files).

    Round-7 hardening (Codex PR #2062): a truncated gzip raises
    :class:`EOFError` mid-iteration (``Compressed file ended before the
    end-of-stream marker was reached``); a corrupted gzip header raises
    :class:`gzip.BadGzipFile`; deeper deflate corruption can surface
    :class:`zlib.error`. None of those are ``OSError`` subclasses, so
    the previous narrow ``except OSError`` let them escape past
    :func:`iter_matching_events` and bubble up to the HTTP route as a
    500 — defeating the "one bad archive shouldn't break the grep"
    contract documented above. We now catch the full corrupt-archive
    set and (when ``stats`` is provided) bump
    ``stats["corrupt_archives_skipped"]`` so callers can surface the
    diagnostic in their response envelope (mirrors the
    ``malformed_rows_skipped`` / ``pattern_timeouts`` pattern).
    """
    try:
        if path.suffix == ".gz":
            fh = gzip.open(path, "rt", encoding="utf-8", errors="replace")
        else:
            fh = open(path, "r", encoding="utf-8", errors="replace")
    except (OSError, EOFError, gzip.BadGzipFile, zlib.error) as exc:
        logger.warning("Skipping corrupt audit archive %s: %s", path, exc)
        if stats is not None:
            stats["corrupt_archives_skipped"] = (
                stats.get("corrupt_archives_skipped", 0) + 1
            )
        return
    try:
        for line in fh:
            yield line
    except (OSError, EOFError, gzip.BadGzipFile, zlib.error) as exc:
        logger.warning("Truncated audit archive %s: %s", path, exc)
        if stats is not None:
            stats["corrupt_archives_skipped"] = (
                stats.get("corrupt_archives_skipped", 0) + 1
            )
        return
    finally:
        try:
            fh.close()
        except Exception:  # noqa: BLE001
            pass


def _iter_live_log_lines_reverse(path: Path) -> Iterator[str]:
    """Yield non-empty live JSONL lines newest-first without full-file reads."""
    try:
        file_size = path.stat().st_size
    except OSError as exc:
        logger.warning("audit.query: stat failed for %s: %s", path, exc)
        return
    if file_size <= 0:
        return

    carry = b""
    offset = file_size
    try:
        with path.open("rb") as handle:
            while offset > 0:
                read_size = min(_TAIL_READ_CHUNK_BYTES, offset)
                offset -= read_size
                handle.seek(offset)
                data = handle.read(read_size) + carry
                lines = data.splitlines()
                if offset > 0:
                    if lines:
                        carry = lines[0]
                        lines = lines[1:]
                    else:
                        carry = data
                        continue
                else:
                    carry = b""
                for raw in reversed(lines):
                    stripped = raw.decode("utf-8", errors="replace").strip()
                    if stripped:
                        yield stripped
    except OSError as exc:
        logger.warning("audit.query: tail read failed for %s: %s", path, exc)


def _archive_predates_since(path: Path, since: datetime) -> bool:
    """Return true when archive metadata proves all rows are older."""
    if path.suffix != ".gz":
        return False
    since_ts = since.timestamp()
    match = _ARCHIVE_TS_RE.search(path.name)
    if match is not None:
        try:
            return int(match.group(1)) < since_ts
        except ValueError:
            pass
    try:
        return path.stat().st_mtime < since_ts
    except OSError:
        return False


def _iter_log_lines_newest_first(
    path: Path,
    *,
    stats: MutableMapping[str, int],
) -> Iterator[str]:
    """Yield raw lines newest-first for live files and best-effort archives."""
    if path.suffix != ".gz":
        yield from _iter_live_log_lines_reverse(path)
        return
    rows = [
        line.strip()
        for line in open_log_lines(path, stats=stats)
        if line.strip()
    ]
    yield from reversed(rows)


def _stats_deadline_expired(deadline_at: float | None) -> bool:
    return deadline_at is not None and time.monotonic() >= deadline_at


def _apply_stats_record(
    record: dict,
    aggregate: AuditStatsAggregate,
) -> None:
    aggregate.total += 1
    event_name = str(record.get("event") or "")
    severity = str(record.get("status") or "ok")
    aggregate.by_event[event_name] = aggregate.by_event.get(event_name, 0) + 1
    aggregate.by_severity[severity] = (
        aggregate.by_severity.get(severity, 0) + 1
    )


def aggregate_recent_stats(
    *,
    targets: Iterable[Path],
    since: datetime,
    deadline_s: float | None = None,
) -> AuditStatsAggregate:
    """Aggregate recent audit stats without scanning known-old history.

    This is intentionally stats-specific. Grep keeps the complete
    forward walker because it supports arbitrary pattern semantics,
    while the Activity rollup only needs counts inside a mandatory
    ``since`` window. Audit writers append chronologically, so reading
    live logs newest-first lets this helper stop a target's rotation
    chain once it sees the first valid row older than ``since``.
    """
    stats: dict[str, int] = {}
    aggregate = AuditStatsAggregate()
    deadline_at = (
        time.monotonic() + deadline_s
        if deadline_s is not None and deadline_s > 0
        else None
    )

    for live_path in targets:
        stop_chain = False
        for chain_path in walk_log_chain(live_path):
            if _archive_predates_since(chain_path, since):
                continue
            for stripped in _iter_log_lines_newest_first(chain_path, stats=stats):
                if _stats_deadline_expired(deadline_at):
                    aggregate.truncated_by_deadline = True
                    stop_chain = True
                    break
                aggregate.lines_scanned += 1
                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                ts_raw = record.get("ts")
                parsed_ts = (
                    parse_event_ts(ts_raw) if isinstance(ts_raw, str) else None
                )
                if parsed_ts is None:
                    aggregate.malformed_rows_skipped += 1
                    continue
                if parsed_ts < since:
                    stop_chain = True
                    break
                _apply_stats_record(record, aggregate)
            if stop_chain:
                break

    aggregate.corrupt_archives_skipped = stats.get("corrupt_archives_skipped", 0)
    return aggregate


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
    stats: MutableMapping[str, int] | None = None,
    bounded_regex: bool = False,
    deadline_s: float | None = None,
) -> Iterator[dict]:
    """Stream parsed events from ``targets`` that pass every filter.

    Exactly one of ``pattern`` (compiled regex) or ``literal`` (plain
    substring) should be supplied — both ``None`` means "match every
    line." Supplying both is allowed (regex wins) but discouraged.

    Filters apply in cheap→expensive order. Codex round-10 (PR #2062)
    reordered so the bounded-regex pattern only runs on rows inside
    the requested ``since`` window — old pathological rows can no
    longer drain the request deadline:

    1. JSON decode.
    2. ``event_type``: exact ``.event`` field match (string equality,
       NOT regex).
    3. ``ts`` parse — rows whose ``ts`` field is missing, non-string,
       or fails ISO-8601 parsing are dropped and counted under
       ``stats["malformed_rows_skipped"]`` (Codex round-3 finding,
       PR #2062). Both ``since`` filtering AND ``/audit/stats`` totals
       go through this gate so a malformed archived row can't inflate
       the count.
    4. ``since``: drop events whose parsed ``ts`` is older than the
       cutoff. Parsed as ``datetime`` objects so a record like
       ``2026-05-21T01:00:00+02:00`` (≡ ``2026-05-20T23:00:00Z``) is
       correctly excluded by ``since=2026-05-21T00:00:00Z`` — earlier
       round-2 code did a raw-string lex compare here, which let
       lex-greater-but-time-earlier rows through (timezone offsets
       sort wrong; malformed strings passed through entirely).
    5. ``literal`` / ``pattern`` substring or regex match on the raw
       line. For the bounded (HTTP) regex path this runs LAST so the
       per-line wall-clock budget is only spent on in-window rows.

    Malformed JSON lines are skipped silently — the live audit log can
    have a truncated tail mid-write and we don't want one bad line to
    mask the rest of the matches.

    Regex behaviour depends on ``bounded_regex``:

    * ``False`` (default — CLI / trusted-caller path): runs
      ``pattern.search`` inline. The operator chose the pattern; if it
      backtracks catastrophically that's on them and ``Ctrl+C`` is the
      escape hatch.
    * ``True`` (HTTP / untrusted-caller path): each line goes through
      a multiprocessing worker with a per-line wall-clock budget
      (:data:`_PER_LINE_TIMEOUT_S`). Timed-out lines are treated as
      no-matches and (if ``stats`` is provided)
      ``stats["pattern_timeouts"]`` is bumped so the caller can
      surface the count in its response envelope.

    Request-level bound (Codex round-4 finding, PR #2062): ``limit``
    caps matches but NOT scanned lines. A pathological no-match regex
    can still hold an API worker for ``per_line_timeout * timed_out_lines``
    across every live + rotated log. ``deadline_s`` adds a wall-clock
    request budget: after each line the walker checks elapsed time and
    stops if exceeded, surfacing ``stats["truncated_by_deadline"] = 1``
    + ``stats["lines_scanned"]`` so the caller can tell the response
    is bounded rather than complete. ``None`` (default — CLI path) =
    no deadline; the CLI operator owns the clock via Ctrl+C.

    Round-5 follow-up: the remaining budget is also passed into
    :meth:`_BoundedRegexSession.search` so the worker's per-line poll
    (including the first-line startup grace) cannot itself exceed the
    request deadline. Otherwise a caller requesting ``deadline_s=0.5``
    on a pathological pattern would still spend ``_STARTUP_GRACE_S``
    (~5 s) on the very first line before the walker's pre-line check
    could fire again.
    """
    use_bounded_regex = (
        bounded_regex and pattern is not None and pattern.pattern and not literal
    )
    session: _BoundedRegexSession | None = (
        _BoundedRegexSession(pattern) if use_bounded_regex else None  # type: ignore[arg-type]
    )
    # Request-level deadline (Codex round-4): check elapsed wall-clock
    # after every non-empty line. The literal/regex cost is paid before
    # the check, but the per-line timeout already bounds a single line
    # so the budget overshoot is at most one per_line_timeout (~100 ms
    # in HTTP mode) — bounded and predictable.
    deadline_at: float | None = (
        time.monotonic() + deadline_s
        if deadline_s is not None and deadline_s > 0
        else None
    )
    try:
        for path in targets:
            for chain_path in walk_log_chain(path):
                for line in open_log_lines(chain_path, stats=stats):
                    stripped = line.strip()
                    if not stripped:
                        continue
                    remaining_budget: float | None = None
                    if deadline_at is not None:
                        remaining_budget = deadline_at - time.monotonic()
                        if remaining_budget <= 0:
                            if stats is not None:
                                stats["truncated_by_deadline"] = 1
                            return
                    if stats is not None:
                        stats["lines_scanned"] = (
                            stats.get("lines_scanned", 0) + 1
                        )
                    # Codex round-10 (PR #2062): parse + window-filter
                    # BEFORE running the line-level pattern. The bounded
                    # regex path advertises ``since`` as the work bound
                    # (see ``grep_audit_endpoint`` + openapi prose); if
                    # the regex runs first, a wall of old pathological
                    # no-match rows at the front of the live log can
                    # spend the entire deadline_seconds budget on rows
                    # that ``since`` would have rejected anyway. The
                    # JSON decode + ts parse are cheap (microseconds)
                    # next to a single per-line regex timeout (~100 ms),
                    # so the reorder is a win for the literal/trusted
                    # paths too — it just moves a cheap gate earlier.
                    try:
                        record = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    if event_type is not None and record.get("event") != event_type:
                        continue
                    # Always parse the timestamp — both ``since``
                    # filtering and downstream counters need a real
                    # ``datetime`` (string compare is broken for
                    # tz-offset rows; see docstring filter step 4).
                    # Malformed rows are dropped + counted so callers
                    # can surface the diagnostic.
                    ts_raw = record.get("ts")
                    parsed_ts = (
                        parse_event_ts(ts_raw) if isinstance(ts_raw, str) else None
                    )
                    if parsed_ts is None:
                        if stats is not None:
                            stats["malformed_rows_skipped"] = (
                                stats.get("malformed_rows_skipped", 0) + 1
                            )
                        continue
                    if since is not None and parsed_ts < since:
                        # Outside the requested window — skip BEFORE
                        # spending the per-line regex budget on a row
                        # the caller never asked about.
                        continue
                    # In-window: now run the line-level pattern. Cheap
                    # reject (``literal``) first — substring is much
                    # cheaper than regex and immune to catastrophic
                    # backtracking.
                    if literal:
                        if literal not in stripped:
                            continue
                    elif session is not None:
                        # Codex round-5: pass remaining request budget so
                        # the worker poll (including first-line startup
                        # grace) can't outrun the request-level deadline.
                        matched, timed_out = session.search(
                            stripped, max_wall_clock_s=remaining_budget
                        )
                        if timed_out:
                            if stats is not None:
                                stats["pattern_timeouts"] = (
                                    stats.get("pattern_timeouts", 0) + 1
                                )
                            continue
                        if not matched:
                            continue
                    elif pattern is not None and pattern.pattern:
                        # Trusted-caller path (CLI / in-process callers
                        # that opted out of bounded_regex). The operator
                        # owns the pattern; if it backtracks they can
                        # Ctrl+C.
                        if not pattern.search(stripped):
                            continue
                    yield record
    finally:
        if session is not None:
            session.close()


def iter_recent_matching_events(
    *,
    targets: Iterable[Path],
    since: datetime,
    pattern: re.Pattern[str] | None = None,
    literal: str | None = None,
    event_type: str | None,
    stats: MutableMapping[str, int] | None = None,
    bounded_regex: bool = False,
    deadline_s: float | None = None,
    per_target_limit: int | None = None,
) -> Iterator[dict]:
    """Stream recent parsed events newest-first for each audit target.

    This is the feed-oriented sibling of :func:`iter_matching_events`.
    It assumes audit files are append-only chronological logs, reads
    live logs from the tail, and stops each target's chain as soon as a
    valid row predates ``since``. That keeps recent dashboard surfaces
    from walking all-time history before they can show current activity.
    """
    use_bounded_regex = (
        bounded_regex and pattern is not None and pattern.pattern and not literal
    )
    session: _BoundedRegexSession | None = (
        _BoundedRegexSession(pattern) if use_bounded_regex else None  # type: ignore[arg-type]
    )
    deadline_at: float | None = (
        time.monotonic() + deadline_s
        if deadline_s is not None and deadline_s > 0
        else None
    )
    try:
        for path in targets:
            emitted_for_target = 0
            stop_target = False
            for chain_path in walk_log_chain(path):
                if _archive_predates_since(chain_path, since):
                    continue
                line_stats: MutableMapping[str, int] = (
                    stats if stats is not None else {}
                )
                for stripped in _iter_log_lines_newest_first(
                    chain_path, stats=line_stats
                ):
                    if not stripped:
                        continue
                    remaining_budget: float | None = None
                    if deadline_at is not None:
                        remaining_budget = deadline_at - time.monotonic()
                        if remaining_budget <= 0:
                            if stats is not None:
                                stats["truncated_by_deadline"] = 1
                            return
                    if stats is not None:
                        stats["lines_scanned"] = (
                            stats.get("lines_scanned", 0) + 1
                        )
                    try:
                        record = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    ts_raw = record.get("ts")
                    parsed_ts = (
                        parse_event_ts(ts_raw) if isinstance(ts_raw, str) else None
                    )
                    if parsed_ts is None:
                        if stats is not None:
                            stats["malformed_rows_skipped"] = (
                                stats.get("malformed_rows_skipped", 0) + 1
                            )
                        continue
                    if parsed_ts < since:
                        stop_target = True
                        break
                    if event_type is not None and record.get("event") != event_type:
                        continue
                    if literal:
                        if literal not in stripped:
                            continue
                    elif session is not None:
                        matched, timed_out = session.search(
                            stripped, max_wall_clock_s=remaining_budget
                        )
                        if timed_out:
                            if stats is not None:
                                stats["pattern_timeouts"] = (
                                    stats.get("pattern_timeouts", 0) + 1
                                )
                            continue
                        if not matched:
                            continue
                    elif pattern is not None and pattern.pattern:
                        if not pattern.search(stripped):
                            continue
                    yield record
                    emitted_for_target += 1
                    if (
                        per_target_limit is not None
                        and per_target_limit > 0
                        and emitted_for_target >= per_target_limit
                    ):
                        stop_target = True
                        break
                if stop_target:
                    break
    finally:
        if session is not None:
            session.close()


__all__ = [
    "AuditStatsAggregate",
    "aggregate_recent_stats",
    "iter_matching_events",
    "iter_recent_matching_events",
    "open_log_lines",
    "parse_event_ts",
    "parse_since",
    "resolve_target_files",
    "safe_pattern_search",
    "walk_log_chain",
]
