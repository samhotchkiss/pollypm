"""Audit query endpoints (Phase 2 — surface #6).

Implements the historical-query side of the audit surface:

* ``GET /api/v1/audit/grep`` — mirrors ``pm audit grep`` (PR #2036).
  Filters: ``project``, ``since`` (ISO-8601 or ``1h``/``24h``/``7d``
  shortcut), ``event_type`` (exact match on ``.event``), ``pattern``
  (Python ``re.search`` regex on the raw JSONL line), ``limit``
  (default 100, max 1000).
* ``GET /api/v1/audit/stats`` — aggregate counts over the same target
  files. Returns ``{by_event, by_severity, total, since}``.

The streaming side (``GET /api/v1/audit/stream``) is already shipped
as ``GET /api/v1/events`` (Phase 1 SSE). This module intentionally does
not re-route that path — it would duplicate the stream multiplexer for
no operator benefit (see Phase 2 spec §8.1).

Helper reuse (per spec): the route module is a thin adapter over the
private helpers in :mod:`pollypm.cli_features.audit`:

* :func:`_resolve_target_files` — picks the right per-project +
  central-tail paths based on the ``project`` filter. The web API
  passes the in-memory :class:`PollyPMConfig` directly so the helper
  doesn't reload the user's config from disk on every request.
* :func:`_iter_matching_events` — applies the cheap→expensive filter
  chain (regex first against the raw line, then ``event_type``
  equality, then ``since``). Streams rather than slurps so a multi-MB
  rotated ``.gz`` archive doesn't blow memory.
* :func:`parse_since` — ISO-8601 / shortcut parsing, identical to the
  CLI surface so the API contract matches what ``pm audit grep --since``
  accepts.

This is intentionally a separate router file (not folded into
``events.py``) because the streaming and grep surfaces have distinct
auth requirements (SSE uses ``?token=`` query auth; grep + stats use
bearer-only) and distinct response shapes (``text/event-stream`` vs.
``application/json``).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from pollypm.cli_features.audit import (
    _iter_matching_events,
    _resolve_target_files,
    parse_since,
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


class AuditGrepResponse(BaseModel):
    """Body for ``GET /audit/grep``.

    Matches the spec §8.1 envelope. ``next_cursor`` is reserved for
    future paging (cursor encoding lives outside this PR per the
    "Phase 2 audit query" scope) — we always return ``None`` today so
    clients see the field shape without depending on its value.
    """

    events: list[Event]
    next_cursor: str | None = None


class AuditStatsResponse(BaseModel):
    """Body for ``GET /audit/stats``.

    Aggregates over the same target-file set the grep endpoint uses
    so totals + per-event / per-severity counts match what a manual
    grep would produce. ``since`` echoes the parsed-and-normalized
    cutoff so the client can confirm the server interpreted shortcuts
    like ``24h`` against its own clock.
    """

    total: int
    by_event: dict[str, int] = Field(default_factory=dict)
    by_severity: dict[str, int] = Field(default_factory=dict)
    since: datetime | None = None


def _parse_since_or_400(value: str | None) -> datetime | None:
    """Wrap :func:`parse_since` to raise the typed API error.

    The CLI helper raises ``typer.BadParameter`` for invalid inputs;
    we re-raise as ``400 invalid_request`` so the API contract (spec
    §6 error envelope) is consistent. Returns ``None`` when the
    caller passed nothing — no filter requested.
    """
    if value is None:
        return None
    try:
        return parse_since(value)
    except Exception as exc:  # noqa: BLE001 — typer.BadParameter is the only path, but stay defensive
        raise invalid_request(
            f"invalid 'since' value {value!r}",
            hint="ISO-8601 (e.g. 2026-05-21T03:14:15Z) or shortcut like 1h / 24h / 7d",
        ) from exc


def _compile_pattern_or_400(pattern: str | None) -> re.Pattern[str]:
    """Compile the grep pattern or raise ``400 invalid_request``.

    ``None`` / empty pattern is treated as "match everything" so the
    endpoint works as a project/event-type filter without forcing the
    caller to send ``pattern=.``.
    """
    if not pattern:
        return re.compile(r"")
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise invalid_request(
            f"invalid 'pattern' regex {pattern!r}: {exc}",
            hint="Use Python re.search syntax.",
        ) from exc


def _coerce_record_to_event(record: dict[str, Any]) -> Event:
    """Reshape an audit JSON record into the ``Event`` response model.

    Tolerates the schema-version + ts shape the writer emits (see
    :func:`pollypm.audit.log._build_record`). Strings that pydantic
    cannot parse as ``datetime`` are passed through verbatim; the
    response_model serializer will normalize them on output.
    """
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


@router.get(
    "/audit/grep",
    response_model=AuditGrepResponse,
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
        Query(description="Python regex (re.search) over the raw JSONL line."),
    ] = None,
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
) -> AuditGrepResponse:
    """Return audit events matching the supplied filters.

    Streams the rotation-aware target files (live ``.jsonl`` + ``.gz``
    archives newest-first) and applies filters in cheap→expensive
    order: regex first, then ``event_type``, then ``since``. The
    response is capped at ``limit`` matches; the spec leaves cursor
    pagination to a follow-up PR (always returns ``next_cursor=null``
    today).
    """
    since_dt = _parse_since_or_400(since)
    compiled = _compile_pattern_or_400(pattern)
    targets = _resolve_target_files(project_filter=project, config=config)

    events: list[Event] = []
    for record in _iter_matching_events(
        targets=targets,
        pattern=compiled,
        since=since_dt,
        event_type=event_type,
    ):
        events.append(_coerce_record_to_event(record))
        if len(events) >= limit:
            break

    return AuditGrepResponse(events=events, next_cursor=None)


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
        Query(description="ISO-8601 timestamp or shortcut (1h, 24h, 7d)."),
    ] = None,
) -> AuditStatsResponse:
    """Return per-event and per-severity counts over the target files.

    Uses the same target-file resolver + matching engine as
    :func:`grep_audit_endpoint` but with a match-everything pattern
    so the aggregation reflects the full set the operator could grep
    (subject only to ``project`` + ``since``). ``severity`` maps to
    the audit record's ``status`` field (``ok`` / ``warn`` / ``error``)
    — the same vocabulary :func:`pollypm.audit.log.emit` writes today.
    """
    since_dt = _parse_since_or_400(since)
    targets = _resolve_target_files(project_filter=project, config=config)

    by_event: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    total = 0
    for record in _iter_matching_events(
        targets=targets,
        pattern=re.compile(r""),
        since=since_dt,
        event_type=None,
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
    )


__all__ = [
    "AuditGrepResponse",
    "AuditStatsResponse",
    "router",
]
