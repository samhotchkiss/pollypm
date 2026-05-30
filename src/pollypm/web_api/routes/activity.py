"""Activity feed endpoint backed by audit-log events."""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from pollypm.audit.query import iter_matching_events, resolve_target_files
from pollypm.recovery.narration import summarize_audit_event
from pollypm.web_api.errors import invalid_request
from pollypm.web_api.models import Event
from pollypm.web_api.routes._deps import ConfigDep
from pollypm.web_api.routes.audit import (
    _GREP_LIMIT_DEFAULT,
    _GREP_LIMIT_MAX,
    _coerce_record_to_event,
    _compile_safe_regex_or_400,
    _parse_since_or_400,
    _validate_pattern_length,
)

router = APIRouter(tags=["Activity"])


class ActivityEvent(Event):
    """Audit event plus the operator-facing activity-feed sentence."""

    summary: str
    recovery: bool = False


class ActivityResponse(BaseModel):
    """Body for ``GET /api/v1/activity``."""

    events: list[ActivityEvent]
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


@router.get(
    "/activity",
    response_model=ActivityResponse,
    response_model_by_alias=True,
    summary="Recent operator-facing activity events",
    operation_id="listActivity",
)
def activity_endpoint(
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
        Query(description="Exact match on the audit .event field."),
    ] = None,
    pattern: Annotated[
        str | None,
        Query(description="Literal substring by default; regex with safe_regex=true."),
    ] = None,
    safe_regex: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=_GREP_LIMIT_MAX)] = _GREP_LIMIT_DEFAULT,
    deadline_seconds: Annotated[float, Query(ge=0.5, le=60.0)] = 5.0,
) -> ActivityResponse:
    """Return audit events shaped for the dashboard activity rail."""

    since_dt = _parse_since_or_400(since)
    literal: str | None = None
    compiled: re.Pattern[str] | None = None
    if pattern:
        _validate_pattern_length(pattern)
        if safe_regex:
            compiled = _compile_safe_regex_or_400(pattern)
        else:
            literal = pattern
    if safe_regex and since_dt is None:
        raise invalid_request(
            "'since' is required when 'safe_regex=true'",
            hint=(
                "Pass an ISO-8601 timestamp or shortcut (e.g. since=24h). "
                "Regex mode walks every line in the window; bound it."
            ),
        )

    targets = resolve_target_files(project_filter=project, config=config)
    events: list[ActivityEvent] = []
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
        events.append(_activity_event_from_audit_event(event))
        if len(events) >= limit:
            break

    return ActivityResponse(
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


def _activity_event_from_audit_event(event: Event) -> ActivityEvent:
    from pollypm.recovery.narration import is_recovery_event

    data: dict[str, Any] = event.model_dump(by_alias=True)
    data["summary"] = summarize_audit_event(
        event_name=event.event,
        subject=event.subject,
        actor=event.actor,
        status=event.status,
        project=event.project,
        metadata=event.metadata or {},
    )
    data["recovery"] = is_recovery_event(event.event)
    return ActivityEvent.model_validate(data)


__all__ = ["ActivityEvent", "ActivityResponse", "router"]
