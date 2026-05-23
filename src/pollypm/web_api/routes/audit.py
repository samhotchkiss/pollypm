"""Audit query endpoints (Phase 2 — surface #6).

Implements the historical-query side of the audit surface:

* ``GET /api/v1/audit/grep`` — mirrors ``pm audit grep`` (PR #2036).
  Filters: ``project``, ``since`` (ISO-8601 or ``1h``/``24h``/``7d``
  shortcut), ``event_type`` (exact match on ``.event``), ``pattern``
  (literal substring by default; opt-in regex via ``safe_regex=true``),
  ``limit`` (default 100, max 1000).
* ``GET /api/v1/audit/stats`` — aggregate counts over the same target
  files. Returns ``{by_event, by_severity, total, since}``. The HTTP
  surface REQUIRES a bounded time window (``since`` query param) —
  unbounded full-history scans must use the CLI (``pm audit grep``).

The streaming side (``GET /api/v1/audit/stream``) is already shipped
as ``GET /api/v1/events`` (Phase 1 SSE). This module intentionally does
not re-route that path — it would duplicate the stream multiplexer for
no operator benefit (see Phase 2 spec §8.1).

Module boundary (Codex P1 review, PR #2062 round 2): rotation-aware
query helpers live in :mod:`pollypm.audit.query`, NOT in
``cli_features.audit``. The CLI module is a thin Typer adapter over
the same domain code; both surfaces share parse/walk semantics.

HTTP guardrails added in round 2:

* ReDoS: ``pattern`` is a LITERAL substring match by default. Regex
  is opt-in via ``safe_regex=true``; both modes cap pattern length
  at :data:`_PATTERN_LENGTH_MAX` so a pathological client can't ship
  a 100 KB regex through the audit walker.
* Malformed rows: corrupt ``ts`` values (and any other
  ``ValidationError``-raising field) are skipped per-row with a counted
  diagnostic on the response (``_malformed_rows_skipped``) — one bad
  archived line no longer 500s the entire query.
* Unbounded stats: ``/audit/stats`` requires ``since``; without it the
  endpoint returns ``400 invalid_request`` pointing the operator at
  the CLI for whole-history aggregation.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field, ValidationError

from pollypm.audit.query import (
    iter_matching_events,
    parse_since,
    resolve_target_files,
)
from pollypm.web_api.errors import invalid_request
from pollypm.web_api.models import Event
from pollypm.web_api.routes._deps import ConfigDep

router = APIRouter(tags=["Audit"])


# Cap matches in a single grep response. The spec calls for 1000 hard
# max with a default of 100. Spec §8.3 — "Cap one response at 1000
# events; client must paginate."
_GREP_LIMIT_DEFAULT = 100
_GREP_LIMIT_MAX = 1000

# Cap pattern length (literal AND regex). Anything longer is almost
# certainly an abuse vector — operator patterns top out around 50
# chars in practice. 200 chars leaves headroom for json-escape-heavy
# regexes without giving an attacker room for a 50 KB catastrophic
# backtracker (Codex round-1 P0 ReDoS finding).
_PATTERN_LENGTH_MAX = 200


class AuditGrepResponse(BaseModel):
    """Body for ``GET /audit/grep``.

    Matches the spec §8.1 envelope. ``next_cursor`` is reserved for
    future paging (cursor encoding lives outside this PR per the
    "Phase 2 audit query" scope) — we always return ``None`` today so
    clients see the field shape without depending on its value.

    ``_malformed_rows_skipped`` is a diagnostic counter — non-zero
    means at least one archived line failed Pydantic ``Event``
    validation (typically a non-ISO ``ts`` from a pre-schema row) and
    was dropped rather than 500'ing the response.

    ``_pattern_timeouts`` counts lines where the bounded-time regex
    search exceeded the per-line timeout. Non-zero means a pathological
    ReDoS pattern is being thrown at the API; the request still
    returned within bounded time (each timed-out line is treated as a
    no-match) but the operator should know which queries triggered the
    safety net. Always zero in literal-substring mode.

    ``_truncated_by_deadline`` (Codex round-4 finding, PR #2062) is
    ``True`` when the request-level wall-clock deadline fired before
    the walker finished scanning every target file. ``_lines_scanned``
    reports how many non-empty lines were actually examined. Together
    they let the caller distinguish a complete-but-empty scan from a
    bounded-and-truncated one. ``limit`` caps returned matches; it
    does NOT cap scanned lines, so a no-match pathological regex
    still costs per-line work — the deadline is the hard upper bound.

    ``_corrupt_archives_skipped`` (Codex round-7 finding, PR #2062)
    counts rotated ``.gz`` archives the walker could not finish
    reading — truncated mid-deflate (``EOFError``), bad gzip header
    (``gzip.BadGzipFile``), or deeper deflate corruption
    (``zlib.error``). The walker now treats those as best-effort
    (one bad archive shouldn't break grep over the remaining files)
    instead of letting the exception escape as a 500. Non-zero means
    at least one archive needs operator attention.
    """

    events: list[Event]
    next_cursor: str | None = None
    malformed_rows_skipped: int = Field(default=0, alias="_malformed_rows_skipped")
    pattern_timeouts: int = Field(default=0, alias="_pattern_timeouts")
    truncated_by_deadline: bool = Field(
        default=False, alias="_truncated_by_deadline"
    )
    lines_scanned: int = Field(default=0, alias="_lines_scanned")
    corrupt_archives_skipped: int = Field(
        default=0, alias="_corrupt_archives_skipped"
    )

    model_config = {"populate_by_name": True}


class AuditStatsResponse(BaseModel):
    """Body for ``GET /audit/stats``.

    Aggregates over the same target-file set the grep endpoint uses
    so totals + per-event / per-severity counts match what a manual
    grep would produce. ``since`` echoes the parsed-and-normalized
    cutoff so the client can confirm the server interpreted shortcuts
    like ``24h`` against its own clock. The HTTP surface requires
    ``since`` to keep stats bounded (see module docstring).

    ``_truncated_by_deadline`` + ``_lines_scanned`` (Codex round-6
    finding, PR #2062) mirror the grep envelope: ``since`` is a
    semantic bound but the walker still walks every target file/line
    until it parses ``ts`` and filters. ``deadline_seconds`` is the
    request-level wall-clock cap; when it fires the caller can tell
    a complete aggregation from a bounded one and re-issue with a
    tighter project/since to make progress.

    ``_malformed_rows_skipped`` (Codex round-9 finding, PR #2062)
    mirrors the grep envelope: the parse-gated walker bumps the
    counter when a row's ``ts`` won't parse (typically a non-ISO
    string from a pre-schema archived row). The previous route
    dropped this; surfacing it lets the operator distinguish
    "two events matched" from "two events matched + N archived
    rows were silently skipped" without having to cross-check via
    ``/audit/grep``.
    """

    total: int
    by_event: dict[str, int] = Field(default_factory=dict)
    by_severity: dict[str, int] = Field(default_factory=dict)
    since: datetime | None = None
    truncated_by_deadline: bool = Field(
        default=False, alias="_truncated_by_deadline"
    )
    lines_scanned: int = Field(default=0, alias="_lines_scanned")
    corrupt_archives_skipped: int = Field(
        default=0, alias="_corrupt_archives_skipped"
    )
    malformed_rows_skipped: int = Field(
        default=0, alias="_malformed_rows_skipped"
    )

    model_config = {"populate_by_name": True}


def _parse_since_or_400(value: str | None) -> datetime | None:
    """Wrap :func:`parse_since` to raise the typed API error.

    The neutral helper raises :class:`ValueError`; we re-raise as
    ``400 invalid_request`` so the API contract (spec §6 error
    envelope) is consistent. Returns ``None`` when the caller passed
    nothing — no filter requested.
    """
    if value is None:
        return None
    try:
        return parse_since(value)
    except ValueError as exc:
        raise invalid_request(
            f"invalid 'since' value {value!r}",
            hint="ISO-8601 (e.g. 2026-05-21T03:14:15Z) or shortcut like 1h / 24h / 7d",
        ) from exc


def _validate_pattern_length(pattern: str) -> None:
    """Reject over-long patterns up-front (ReDoS guardrail, P0)."""
    if len(pattern) > _PATTERN_LENGTH_MAX:
        raise invalid_request(
            f"'pattern' is {len(pattern)} chars; max is {_PATTERN_LENGTH_MAX}",
            hint=(
                "Audit-grep patterns are operator-scale (a few dozen chars). "
                "Truncate the pattern or filter via project/event_type/since."
            ),
        )


def _compile_safe_regex_or_400(pattern: str) -> re.Pattern[str]:
    """Compile an opt-in regex, after the length cap has been checked.

    Compiling a regex is cheap and deterministic; the catastrophic
    backtracking risk is at *match time*. The query walker pipes each
    line through :func:`pollypm.audit.query.safe_pattern_search`, which
    runs the search on a bounded-time worker pool — a short pathological
    pattern like ``(a+)+b`` can no longer hang the request. See the
    module docstring "Round 2 guardrails" section for the full chain.
    """
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise invalid_request(
            f"invalid 'pattern' regex {pattern!r}: {exc}",
            hint="Use Python re.search syntax.",
        ) from exc


def _coerce_record_to_event(record: dict[str, Any]) -> Event | None:
    """Reshape an audit JSON record into the ``Event`` response model.

    Returns ``None`` instead of raising when the record is too
    malformed for Pydantic to accept (typically a non-ISO ``ts``
    string from a pre-schema archived row). Callers should increment
    a diagnostic counter so the operator can tell that historical
    rows were silently dropped vs. there simply being no matches.
    """
    try:
        return Event(
            schema=int(record.get("schema", 1)),
            ts=record.get("ts") or datetime.now(timezone.utc),
            project=str(record.get("project") or ""),
            event=str(record.get("event") or ""),
            subject=str(record.get("subject") or ""),
            actor=str(record.get("actor") or ""),
            status=str(record.get("status") or "ok"),
            metadata=record.get("metadata") or None,
        )
    except ValidationError:
        return None
    except (TypeError, ValueError):
        # ``int(record.get("schema"))`` etc. can blow up on garbage
        # input types — treat the same as a validation miss.
        return None


@router.get(
    "/audit/grep",
    response_model=AuditGrepResponse,
    response_model_by_alias=True,
    summary="Search audit-log events (rotation-aware)",
    operation_id="grepAudit",
)
def grep_audit_endpoint(
    config: ConfigDep,
    project: Annotated[
        str | None,
        Query(description="Scope to one project (per-project log + central tail)."),
    ] = None,
    since: Annotated[
        str | None,
        Query(description="ISO-8601 timestamp or shortcut (1h, 24h, 7d)."),
    ] = None,
    event_type: Annotated[
        str | None,
        Query(description="Exact match on the .event field (not a regex)."),
    ] = None,
    pattern: Annotated[
        str | None,
        Query(
            description=(
                "Substring matched against the raw JSONL line. Literal by "
                "default; pass safe_regex=true to interpret as a Python "
                f"re.search pattern. Capped at {_PATTERN_LENGTH_MAX} chars."
            ),
        ),
    ] = None,
    safe_regex: Annotated[
        bool,
        Query(
            description=(
                "Opt in to regex semantics for 'pattern'. Default false "
                "(literal substring) avoids ReDoS hazards on the API worker."
            ),
        ),
    ] = False,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=_GREP_LIMIT_MAX,
            description=(
                "Cap matches in the response. Default 100, max 1000 (spec §8.3)."
            ),
        ),
    ] = _GREP_LIMIT_DEFAULT,
    deadline_seconds: Annotated[
        float,
        Query(
            ge=0.5,
            le=60.0,
            description=(
                "Request-level wall-clock budget (seconds). Default 5.0. "
                "When exceeded, the response sets _truncated_by_deadline=true "
                "and _lines_scanned reports how far the walker got. Caps "
                "no-match pathological regex cost; required because 'limit' "
                "only caps matches, not scanned lines (Codex round-4)."
            ),
        ),
    ] = 5.0,
) -> AuditGrepResponse:
    """Return audit events matching the supplied filters.

    Streams the rotation-aware target files (live ``.jsonl`` + ``.gz``
    archives newest-first) and applies filters in cheap→expensive
    order (Codex round-10 reorder, PR #2062): JSON decode → exact
    ``event_type`` match → ``ts`` parse → ``since`` cutoff →
    ``pattern`` substring/regex match on the raw line. The pattern
    runs LAST so the ``safe_regex=true`` per-line wall-clock budget
    is only spent on rows already inside the requested ``since``
    window — this ordering is part of the safety/performance
    contract for regex mode (paired with the ``safe_regex`` +
    ``since`` requirement and ``deadline_seconds`` bound below).
    The response is capped at ``limit`` matches; the spec leaves
    cursor pagination to a follow-up PR (always returns
    ``next_cursor=null`` today).
    """
    since_dt = _parse_since_or_400(since)

    literal: str | None = None
    compiled: re.Pattern[str] | None = None
    if pattern:
        _validate_pattern_length(pattern)
        if safe_regex:
            compiled = _compile_safe_regex_or_400(pattern)
        else:
            literal = pattern
    elif safe_regex:
        # Treat ``safe_regex=true`` with no pattern as "match anything"
        # — operator probably just enabled it speculatively.
        pass

    # Round-4 (Codex): ``safe_regex=true`` MUST come with ``since`` so the
    # pattern only runs over a bounded slice of history — mirrors the
    # round-2 stats invariant. Without this, a no-match pathological
    # regex (``(a+)+b``) walks every live + rotated log; ``deadline_s``
    # bounds wall-clock but ``since`` bounds the row count up front,
    # which is cheaper and gives the operator an actionable error.
    # Checked AFTER pattern compilation so a malformed regex still
    # surfaces its own 400 first — operator gets the actionable error.
    if safe_regex and since_dt is None:
        raise invalid_request(
            "'since' is required when 'safe_regex=true'",
            hint=(
                "Pass an ISO-8601 timestamp or shortcut (e.g. since=24h). "
                "Regex mode walks every line in the window; bound it. "
                "Literal-substring mode (default) has no such requirement."
            ),
        )

    targets = resolve_target_files(project_filter=project, config=config)

    events: list[Event] = []
    malformed = 0
    walker_stats: dict[str, int] = {}
    for record in iter_matching_events(
        targets=targets,
        pattern=compiled,
        literal=literal,
        since=since_dt,
        event_type=event_type,
        stats=walker_stats,
        bounded_regex=True,
        deadline_s=deadline_seconds,
    ):
        event = _coerce_record_to_event(record)
        if event is None:
            malformed += 1
            continue
        events.append(event)
        if len(events) >= limit:
            break

    # Total malformed = rows dropped by the walker's ts-parse gate
    # (Codex round-3) PLUS rows that survived parsing but failed the
    # Pydantic ``Event`` model (older / partial schemas). Surfacing
    # both under one counter keeps the contract simple: any non-zero
    # value means archived rows were silently skipped.
    return AuditGrepResponse(
        events=events,
        next_cursor=None,
        _malformed_rows_skipped=(
            malformed + walker_stats.get("malformed_rows_skipped", 0)
        ),
        _pattern_timeouts=walker_stats.get("pattern_timeouts", 0),
        _truncated_by_deadline=bool(walker_stats.get("truncated_by_deadline", 0)),
        _lines_scanned=walker_stats.get("lines_scanned", 0),
        _corrupt_archives_skipped=walker_stats.get("corrupt_archives_skipped", 0),
    )


@router.get(
    "/audit/stats",
    response_model=AuditStatsResponse,
    summary="Aggregate counts of audit events by type and severity",
    operation_id="statsAudit",
)
def stats_audit_endpoint(
    config: ConfigDep,
    project: Annotated[
        str | None,
        Query(description="Scope to one project (per-project log + central tail)."),
    ] = None,
    since: Annotated[
        str | None,
        Query(
            description=(
                "REQUIRED. ISO-8601 timestamp or shortcut (1h, 24h, 7d). "
                "The HTTP surface refuses unbounded full-history stats — "
                "use the CLI (`pm audit grep`) for that."
            ),
        ),
    ] = None,
    deadline_seconds: Annotated[
        float,
        Query(
            ge=0.5,
            le=120.0,
            description=(
                "Request-level wall-clock budget (seconds). Default 10.0 "
                "(higher than /audit/grep's 5.0 because aggregation has "
                "no early-exit on match-count). When exceeded the walker "
                "stops, the response sets _truncated_by_deadline=true, "
                "and _lines_scanned reports how many non-empty lines "
                "were examined. Required because `since` is only a "
                "semantic bound — the walker still reads every target "
                "file/line until it parses `ts` and filters "
                "(Codex round-6 finding)."
            ),
        ),
    ] = 10.0,
) -> AuditStatsResponse:
    """Return per-event and per-severity counts over the target files.

    Bounded by ``since`` (mandatory on the HTTP surface — see module
    docstring). ``severity`` maps to the audit record's ``status``
    field (``ok`` / ``warn`` / ``error``) — the same vocabulary
    :func:`pollypm.audit.log.emit` writes today.
    """
    if since is None:
        raise invalid_request(
            "'since' is required for /audit/stats",
            hint=(
                "Pass an ISO-8601 timestamp or shortcut (e.g. since=24h). "
                "Use the CLI `pm audit grep` for unbounded history scans."
            ),
        )
    since_dt = _parse_since_or_400(since)
    targets = resolve_target_files(project_filter=project, config=config)

    by_event: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    total = 0
    # Round-3 fix: route stats through the same parse-gated walker as
    # grep so malformed-ts rows (and the broken-string-compare bug in
    # round 2) can't inflate ``total``. We pass a ``stats`` dict so
    # the walker bumps ``malformed_rows_skipped`` for us; round-9
    # surfaces the counter in the response so callers can distinguish
    # a clean aggregation from one where archived rows were silently
    # skipped.
    walker_stats: dict[str, int] = {}
    for record in iter_matching_events(
        targets=targets,
        pattern=None,
        literal=None,
        since=since_dt,
        event_type=None,
        stats=walker_stats,
        deadline_s=deadline_seconds,
    ):
        total += 1
        event_name = str(record.get("event") or "")
        severity = str(record.get("status") or "ok")
        by_event[event_name] = by_event.get(event_name, 0) + 1
        by_severity[severity] = by_severity.get(severity, 0) + 1

    return AuditStatsResponse(
        total=total,
        by_event=by_event,
        by_severity=by_severity,
        since=since_dt,
        _truncated_by_deadline=bool(walker_stats.get("truncated_by_deadline", 0)),
        _lines_scanned=walker_stats.get("lines_scanned", 0),
        _corrupt_archives_skipped=walker_stats.get("corrupt_archives_skipped", 0),
        _malformed_rows_skipped=walker_stats.get("malformed_rows_skipped", 0),
    )


__all__ = [
    "AuditGrepResponse",
    "AuditStatsResponse",
    "router",
]
