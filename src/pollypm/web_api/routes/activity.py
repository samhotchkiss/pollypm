"""Activity feed endpoint backed by audit-log events."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from pollypm.audit.query import iter_recent_matching_events, resolve_target_files
from pollypm.recovery.narration import summarize_audit_event
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

_ACTIVITY_DEFAULT_SINCE = "24h"
_ACTIVITY_PER_TARGET_MIN = 100
_ACTIVITY_PER_TARGET_MULTIPLIER = 4
_ACTIVITY_CROSS_PROJECT_SHARE = 0.5
_ACTIVITY_GROUPED_EVENTS = frozenset({"watchdog.escalation_dispatched"})


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
        Query(
            description=(
                "ISO-8601 timestamp or shortcut (1h, 24h, 7d). "
                f"Defaults to {_ACTIVITY_DEFAULT_SINCE}."
            )
        ),
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

    since_dt = _parse_since_or_400(since or _ACTIVITY_DEFAULT_SINCE)
    literal: str | None = None
    compiled: re.Pattern[str] | None = None
    if pattern:
        _validate_pattern_length(pattern)
        if safe_regex:
            compiled = _compile_safe_regex_or_400(pattern)
        else:
            literal = pattern
    targets = resolve_target_files(project_filter=project, config=config)
    candidates: list[Event] = []
    malformed = 0
    walker_stats: dict[str, int] = {}
    for record in iter_recent_matching_events(
        targets=targets,
        pattern=compiled,
        literal=literal,
        since=since_dt,
        event_type=event_type,
        stats=walker_stats,
        bounded_regex=True,
        deadline_s=deadline_seconds,
        per_target_limit=_per_target_candidate_limit(limit),
    ):
        event = _coerce_record_to_event(record)
        if event is None:
            malformed += 1
            continue
        candidates.append(event)

    return ActivityResponse(
        events=_compose_activity_feed(
            candidates,
            limit=limit,
            project_filter=project,
        ),
        next_cursor=None,
        _malformed_rows_skipped=(
            malformed + walker_stats.get("malformed_rows_skipped", 0)
        ),
        _pattern_timeouts=walker_stats.get("pattern_timeouts", 0),
        _truncated_by_deadline=bool(walker_stats.get("truncated_by_deadline", 0)),
        _lines_scanned=walker_stats.get("lines_scanned", 0),
        _corrupt_archives_skipped=walker_stats.get("corrupt_archives_skipped", 0),
    )


@dataclass(slots=True)
class _ActivityGroup:
    representative: Event
    count: int = 0
    subjects: list[str] = field(default_factory=list)

    def add(self, event: Event) -> None:
        self.count += 1
        if event.subject:
            self.subjects.append(event.subject)


def _per_target_candidate_limit(limit: int) -> int:
    return min(
        _GREP_LIMIT_MAX,
        max(_ACTIVITY_PER_TARGET_MIN, limit * _ACTIVITY_PER_TARGET_MULTIPLIER),
    )


def _compose_activity_feed(
    events: list[Event],
    *,
    limit: int,
    project_filter: str | None,
) -> list[ActivityEvent]:
    deduped: list[Event] = []
    seen: set[tuple[str, str, str, str, str, str, str]] = set()
    for event in events:
        identity = _activity_event_identity(event)
        if identity in seen:
            continue
        seen.add(identity)
        deduped.append(event)

    deduped.sort(key=_event_sort_key, reverse=True)

    entries: list[ActivityEvent] = []
    groups: dict[tuple[str, str, str, str], _ActivityGroup] = {}
    for event in deduped:
        group_key = _activity_group_key(event)
        if group_key is None:
            entries.append(_activity_event_from_audit_event(event))
            continue
        group = groups.get(group_key)
        if group is None:
            group = _ActivityGroup(representative=event)
            groups[group_key] = group
            group.add(event)
            entries.append(_activity_event_from_group(group))
            continue
        group.add(event)

    for index, entry in enumerate(entries):
        group = groups.get(_activity_group_key(entry))
        if group is not None and group.count > 1:
            entries[index] = _activity_event_from_group(group)

    entries.sort(key=_event_sort_key, reverse=True)
    entries = _apply_project_share_cap(
        entries,
        limit=limit,
        project_filter=project_filter,
    )
    return entries[:limit]


def _activity_event_identity(
    event: Event,
) -> tuple[str, str, str, str, str, str, str]:
    return (
        event.ts.isoformat(),
        event.project,
        event.event,
        event.subject,
        event.actor,
        event.status,
        _metadata_fingerprint(event.metadata),
    )


def _metadata_fingerprint(metadata: dict[str, Any] | None) -> str:
    try:
        return json.dumps(metadata or {}, sort_keys=True, default=str)
    except TypeError:
        return repr(metadata)


def _event_sort_key(event: Event) -> datetime:
    return event.ts


def _activity_group_key(event: Event) -> tuple[str, str, str, str] | None:
    if event.event not in _ACTIVITY_GROUPED_EVENTS:
        return None
    metadata = event.metadata or {}
    problem = str(
        metadata.get("rule")
        or metadata.get("finding_type")
        or metadata.get("reason")
        or ""
    )
    return (event.project, event.event, event.status, problem)


def _activity_event_from_group(group: _ActivityGroup) -> ActivityEvent:
    event = group.representative
    data: dict[str, Any] = event.model_dump(by_alias=True)
    metadata = dict(event.metadata or {})
    metadata["activity_group"] = {
        "count": group.count,
        "event": event.event,
        "subjects": group.subjects[:10],
    }
    data["metadata"] = metadata
    if group.count > 1:
        data["summary"] = _activity_group_summary(group)
    else:
        data["summary"] = summarize_audit_event(
            event_name=event.event,
            subject=event.subject,
            actor=event.actor,
            status=event.status,
            project=event.project,
            metadata=event.metadata or {},
        )
    from pollypm.recovery.narration import is_recovery_event

    data["recovery"] = is_recovery_event(event.event)
    return ActivityEvent.model_validate(data)


def _activity_group_summary(group: _ActivityGroup) -> str:
    event = group.representative
    count = group.count
    if event.event == "watchdog.escalation_dispatched":
        problem = _plural_activity_problem(event, count)
        project = _activity_project_label(event.project)
        return f"I sent unstick briefs for {count} {problem} in {project}."
    return summarize_audit_event(
        event_name=event.event,
        subject=event.subject,
        actor=event.actor,
        status=event.status,
        project=event.project,
        metadata=event.metadata or {},
    )


def _plural_activity_problem(event: Event, count: int) -> str:
    metadata = event.metadata or {}
    problem = str(
        metadata.get("rule")
        or metadata.get("finding_type")
        or metadata.get("reason")
        or ""
    ).strip()
    if problem == "stuck_draft":
        return "stuck draft" if count == 1 else "stuck drafts"
    if problem == "queue_without_motion":
        return "queued work stall" if count == 1 else "queued work stalls"
    if problem:
        label = problem.replace("_", " ").replace("-", " ")
        return label if count == 1 else f"{label} findings"
    return "watchdog finding" if count == 1 else "watchdog findings"


def _activity_project_label(project: str) -> str:
    label = (project or "the project").strip()
    return label.replace("_", " ").replace("-", " ")


def _apply_project_share_cap(
    entries: list[ActivityEvent],
    *,
    limit: int,
    project_filter: str | None,
) -> list[ActivityEvent]:
    if project_filter or limit <= 1:
        return entries
    per_project_cap = max(1, int(limit * _ACTIVITY_CROSS_PROJECT_SHARE))
    selected: list[ActivityEvent] = []
    overflow: list[ActivityEvent] = []
    counts: dict[str, int] = {}
    for entry in entries:
        project = entry.project or "workspace"
        current = counts.get(project, 0)
        if current < per_project_cap:
            selected.append(entry)
            counts[project] = current + 1
        else:
            overflow.append(entry)
        if len(selected) >= limit:
            return selected
    for entry in overflow:
        selected.append(entry)
        if len(selected) >= limit:
            break
    selected.sort(key=_event_sort_key, reverse=True)
    return selected


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
