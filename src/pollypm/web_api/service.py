"""Read-side adapters between the work-service and Web API models.

The Web API never reaches into ``state.db`` or ``audit.jsonl`` directly
— it composes against :func:`pollypm.work.factory.create_work_service`
(#1389) and :mod:`pollypm.audit.log`. This module owns the conversions
from those internal types to the Pydantic shapes declared in
:mod:`pollypm.web_api.models`.

Phase 1 is read-only, so every function here is pure data shaping;
write paths land in Phase 2 (#1548) and stay in
``pollypm.web_api.routes``.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

import sqlite3

from pollypm.audit.log import AuditEvent, read_events
from pollypm.config import PollyPMConfig, load_config
from pollypm.models import KnownProject
from pollypm.web_api.errors import service_unavailable
from pollypm.web_api.models import (
    ContextEntry as APIContextEntry,
    Event as APIEvent,
    FlowNodeExecution as APIFlowNodeExecution,
    InboxItem as APIInboxItem,
    InboxItemDetail as APIInboxItemDetail,
    InboxMessage as APIInboxMessage,
    Plan as APIPlan,
    PlanJudgmentCall as APIPlanJudgmentCall,
    Project as APIProject,
    ProjectActivityEntry as APIProjectActivityEntry,
    ProjectDrilldown as APIProjectDrilldown,
    TaskDetail as APITaskDetail,
    TaskRelationships as APITaskRelationships,
    TaskSummary as APITaskSummary,
    Transition as APITransition,
    WorkOutput as APIWorkOutput,
    Artifact as APIArtifact,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Read-only work-service open
# ---------------------------------------------------------------------------


_DISABLE_WORK_DB_OPENED_AUDIT_ENV = "POLLYPM_DISABLE_WORK_DB_OPENED_AUDIT"


# Known transient backing-store error classes. Failures of these
# types map to a typed 503 ``service_unavailable`` so the client can
# retry. Anything outside this tuple bubbles up to the FastAPI
# unhandled-exception handler (500 ``internal_error``) so we don't
# silently swallow real bugs.
_BACKING_STORE_ERRORS: tuple[type[BaseException], ...] = (
    sqlite3.OperationalError,
    sqlite3.DatabaseError,
    OSError,
)


@contextlib.contextmanager
def _open_work_service_readonly(
    *, config: PollyPMConfig, project_key: str, project_path: Path | str
):
    """Open a work-service for read-only API consumption.

    SQLiteWorkService doesn't yet have a true ``mode=ro`` URI flag —
    its constructor calls :func:`create_work_tables` and emits a
    ``work_db.opened`` audit row, both of which technically mutate the
    backing store. For the Web API's read endpoints we don't want
    every ``GET`` to write an audit row, so we toggle the existing
    ``POLLYPM_DISABLE_WORK_DB_OPENED_AUDIT`` opt-out env (introduced
    upstream for tests) for the lifetime of the open.

    The ``CREATE TABLE IF NOT EXISTS`` calls in the constructor stay
    no-ops once the tables exist; we accept the first-time bootstrap
    side effect because (a) the cockpit normally bootstraps before the
    API server runs, and (b) without it a fresh workspace would 500
    on every endpoint until something else opened the DB. If/when the
    work service grows a real read-only URI flag this helper should
    forward it; for now the audit-suppression is the only meaningful
    side effect we can avoid.
    """
    from pollypm.work.factory import create_work_service

    prior = os.environ.get(_DISABLE_WORK_DB_OPENED_AUDIT_ENV)
    os.environ[_DISABLE_WORK_DB_OPENED_AUDIT_ENV] = "1"
    try:
        with create_work_service(
            config=config, project_key=project_key, project_path=project_path
        ) as svc:
            yield svc
    finally:
        if prior is None:
            os.environ.pop(_DISABLE_WORK_DB_OPENED_AUDIT_ENV, None)
        else:
            os.environ[_DISABLE_WORK_DB_OPENED_AUDIT_ENV] = prior


# ---------------------------------------------------------------------------
# Project helpers
# ---------------------------------------------------------------------------


def list_projects(config: PollyPMConfig, *, tracked_only: bool = False) -> list[APIProject]:
    """Return every registered project as an :class:`APIProject`.

    Counts and flags are computed against the work-service so the
    response matches what the cockpit dashboard renders. We open one
    work-service per project to keep the implementation simple — the
    factory is cheap and the cockpit does the same.
    """
    out: list[APIProject] = []
    for key, project in config.projects.items():
        if tracked_only and not project.tracked:
            continue
        out.append(_project_to_api(config, key, project))
    return out


def get_project(config: PollyPMConfig, key: str) -> APIProject | None:
    project = config.projects.get(key)
    if project is None:
        return None
    return _project_to_api(config, key, project)


def project_drilldown(config: PollyPMConfig, key: str) -> APIProjectDrilldown | None:
    """Project + recent activity + top tasks + pending plan review.

    One round-trip is enough to render the cockpit's drilldown per
    spec §8 (``GET /api/v1/projects/{key}``).
    """
    base = get_project(config, key)
    if base is None:
        return None
    project_path = config.projects[key].path

    recent: list[APIProjectActivityEntry] = []
    try:
        events = read_events(key, project_path=project_path, limit=25)
    except Exception:  # noqa: BLE001
        events = []
    for event in events:
        try:
            ts = _parse_iso(event.ts)
        except Exception:  # noqa: BLE001
            continue
        if ts is None:
            continue
        meta = event.metadata or {}
        summary = meta.get("summary") or meta.get("message")
        recent.append(APIProjectActivityEntry(
            ts=ts,
            event=event.event,
            subject=event.subject,
            actor=event.actor or "",
            status=event.status,
            summary=str(summary) if summary else None,
        ))

    top_tasks: list[APITaskSummary] = []
    plan: APIPlan | None = None
    try:
        with _open_work_service_readonly(
            config=config, project_key=key, project_path=project_path
        ) as svc:
            tasks = svc.list_tasks(project=key, limit=10)
            for task in tasks:
                top_tasks.append(_task_to_summary(task))
            plan = _active_plan_for_project(svc, key)
    except Exception as exc:  # noqa: BLE001
        logger.debug("drilldown: work-service open failed for %s: %s", key, exc)

    return APIProjectDrilldown(
        **base.model_dump(),
        recent_activity=recent,
        top_tasks=top_tasks,
        plan_review=plan,
    )


def _project_to_api(config: PollyPMConfig, key: str, project: KnownProject) -> APIProject:
    counts: dict[str, int] = {}
    pending_plan_review = False
    open_inbox_count = 0
    glyph = "unknown"
    state_label: str | None = None

    try:
        with _open_work_service_readonly(
            config=config, project_key=key, project_path=project.path
        ) as svc:
            try:
                counts = svc.state_counts(project=key) or {}
            except Exception:  # noqa: BLE001
                counts = {}
            try:
                pending_plan_review = _has_pending_plan_review(svc, key)
            except Exception:  # noqa: BLE001
                pending_plan_review = False
    except Exception as exc:  # noqa: BLE001
        logger.debug("project counts: work-service unavailable for %s: %s", key, exc)

    try:
        open_inbox_count = _count_open_inbox(config, key)
    except Exception as exc:  # noqa: BLE001
        logger.debug("project inbox count failed for %s: %s", key, exc)

    glyph = _glyph_for_project(project, counts, pending_plan_review, open_inbox_count)
    if not project.tracked:
        glyph = "paused"

    return APIProject(
        key=key,
        name=project.display_label(),
        path=str(project.path),
        tracked=project.tracked,
        kind=project.kind.value if hasattr(project.kind, "value") else str(project.kind),
        persona_name=project.persona_name,
        state=state_label,
        glyph=glyph,
        task_counts=counts,
        open_inbox_count=open_inbox_count,
        pending_plan_review=pending_plan_review,
    )


def _glyph_for_project(
    project: KnownProject,
    counts: dict[str, int],
    pending_plan_review: bool,
    open_inbox_count: int,
) -> str:
    """Best-effort stop-light glyph.

    Mirrors the cockpit's signaling: red when there's pending plan
    review or open inbox waiting, amber when there's review/in_progress
    work, green otherwise. Real briefing-derived glyphs land on the
    cockpit dashboard via ``dashboard_data.gather`` — that path needs a
    StateStore + plugin host, which the API server intentionally
    doesn't load. The fallback derives from raw work-service counts so
    it works with the cockpit down.
    """
    if not project.tracked:
        return "paused"
    if pending_plan_review or open_inbox_count > 0:
        return "amber"
    if counts.get("review", 0) > 0 or counts.get("blocked", 0) > 0:
        return "amber"
    if counts.get("in_progress", 0) > 0:
        return "amber"
    return "green"


def _has_pending_plan_review(svc, project_key: str) -> bool:
    """True when any task is sitting at ``review`` on a plan-review node.

    Phase 1 only needs a boolean, so we read tasks-in-review and check
    whether their flow template is plan-shaped.
    """
    try:
        tasks = svc.list_tasks(project=project_key, work_status="review")
    except Exception:  # noqa: BLE001
        return False
    for task in tasks:
        flow_id = getattr(task, "flow_template_id", "") or ""
        if "plan" in flow_id.lower():
            return True
        labels = getattr(task, "labels", []) or []
        if any("plan" in lbl.lower() for lbl in labels):
            return True
    return False


def _count_open_inbox(config: PollyPMConfig, project_key: str) -> int:
    """Count open inbox messages + chat-flow tasks for a project.

    Mirrors :func:`pollypm.dashboard_data._count_inbox_tasks` at a
    project granularity. Best-effort — returns 0 on any failure
    rather than blocking the project list.
    """
    project = config.projects.get(project_key)
    if project is None:
        return 0
    try:
        with _open_work_service_readonly(
            config=config, project_key=project_key, project_path=project.path
        ) as svc:
            tasks = svc.list_tasks(project=project_key)
    except Exception:  # noqa: BLE001
        return 0
    count = 0
    for task in tasks:
        if getattr(task, "flow_template_id", "") != "chat":
            continue
        status = getattr(task, "work_status", None)
        status_value = getattr(status, "value", str(status)) if status else ""
        if status_value not in {"done", "cancelled"}:
            count += 1
    return count


# ---------------------------------------------------------------------------
# Task helpers
# ---------------------------------------------------------------------------


def list_project_tasks(
    config: PollyPMConfig,
    project_key: str,
    *,
    status: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[APITaskSummary], str | None]:
    """Page of task summaries with cursor-based pagination.

    The cursor is the integer ``task_number`` of the last item in the
    previous page; absent ⇒ start. We sort tasks by ``task_number`` so
    the cursor is stable across calls without depending on
    ``updated_at``.
    """
    project = config.projects.get(project_key)
    if project is None:
        return [], None

    cursor_n: int | None = None
    if cursor is not None:
        try:
            cursor_n = int(cursor)
        except ValueError:
            cursor_n = None

    # Wrap the entire ``with`` so failures during work-service
    # construction (DB open, pragmas, schema bootstrap, migrations)
    # also surface as 503 — not just failures inside the body.
    # Spec §6 maps DB lock contention / I/O failures to
    # ``service_unavailable`` so the client can retry. APIError /
    # other typed exceptions fall outside ``_BACKING_STORE_ERRORS``
    # so they pass through unchanged.
    try:
        with _open_work_service_readonly(
            config=config, project_key=project_key, project_path=project.path
        ) as svc:
            tasks = svc.list_tasks(project=project_key, work_status=status)
            tasks.sort(key=lambda t: getattr(t, "task_number", 0))
            if cursor_n is not None:
                tasks = [t for t in tasks if getattr(t, "task_number", 0) > cursor_n]
            page = tasks[:limit]
            next_cursor: str | None = None
            if len(tasks) > limit and page:
                next_cursor = str(getattr(page[-1], "task_number", 0))
            return [_task_to_summary(t) for t in page], next_cursor
    except _BACKING_STORE_ERRORS as exc:
        logger.warning(
            "list_tasks: backing store error for %s: %s",
            project_key,
            exc,
            exc_info=True,
        )
        raise service_unavailable(
            f"Backing store unavailable for project {project_key}",
            hint="Retry shortly; check `pm doctor` if the failure persists.",
        ) from exc


def get_task_detail(
    config: PollyPMConfig, project_key: str, task_number: int
) -> APITaskDetail | None:
    project = config.projects.get(project_key)
    if project is None:
        return None

    task_id = f"{project_key}/{task_number}"
    # Wrap the entire ``with`` so failures during work-service
    # construction (DB open, pragmas, schema bootstrap, migrations)
    # surface as 503, not 500. Genuine missing-task failures (the
    # narrow ``Exception`` swallow inside ``svc.get(...)``) still
    # collapse to ``None`` ⇒ 404 — but a backing-store failure on
    # ``svc.get`` re-raises ``OperationalError`` past the inner
    # swallow so the outer handler can map it to 503.
    try:
        with _open_work_service_readonly(
            config=config, project_key=project_key, project_path=project.path
        ) as svc:
            try:
                task = svc.get(task_id)
            except _BACKING_STORE_ERRORS:
                # Re-raise so the outer handler converts to 503 —
                # don't conflate a DB error with "task not found".
                raise
            except Exception:  # noqa: BLE001
                return None
            plan: APIPlan | None = None
            if _is_plan_task(task) and _is_in_review(task):
                try:
                    plan = _build_plan(svc, task)
                except Exception:  # noqa: BLE001
                    plan = None
            return _task_to_detail(task, plan=plan)
    except _BACKING_STORE_ERRORS as exc:
        logger.warning(
            "get_task_detail: backing store error for %s/%s: %s",
            project_key,
            task_number,
            exc,
            exc_info=True,
        )
        raise service_unavailable(
            f"Backing store unavailable for task {project_key}/{task_number}",
            hint="Retry shortly; check `pm doctor` if the failure persists.",
        ) from exc


# ---------------------------------------------------------------------------
# Plan helpers
# ---------------------------------------------------------------------------


def get_active_plan(
    config: PollyPMConfig, project_key: str, *, version: int | None = None
) -> APIPlan | None:
    """Return the structured plan body for the project's active review task.

    When ``version`` is supplied, return that revision instead of the
    current one (the work-service today only stores the latest body,
    so for now we only honor the version *number* on the active task
    — older revisions surface in Phase 2 once the architecture for
    plan history is decided).
    """
    project = config.projects.get(project_key)
    if project is None:
        return None
    # Wrap the entire ``with`` so failures during work-service
    # construction surface as 503. ``_active_plan_for_project``
    # internally swallows broad exceptions (so a transient query
    # failure during plan reconstruction degrades to "no plan"),
    # but a backing-store failure on ``__enter__`` would otherwise
    # leak as 500.
    try:
        with _open_work_service_readonly(
            config=config, project_key=project_key, project_path=project.path
        ) as svc:
            plan = _active_plan_for_project(svc, project_key)
            if plan is None:
                return None
            if version is not None and plan.version != version:
                # Older versions are not retrievable yet; matching strict
                # version returns the active plan only when it matches.
                return None
            return plan
    except _BACKING_STORE_ERRORS as exc:
        logger.warning(
            "get_active_plan: backing store error for %s: %s",
            project_key,
            exc,
            exc_info=True,
        )
        raise service_unavailable(
            f"Backing store unavailable for project {project_key}",
            hint="Retry shortly; check `pm doctor` if the failure persists.",
        ) from exc


def _active_plan_for_project(svc, project_key: str) -> APIPlan | None:
    try:
        review_tasks = svc.list_tasks(project=project_key, work_status="review")
    except Exception:  # noqa: BLE001
        review_tasks = []
    candidates = [t for t in review_tasks if _is_plan_task(t)]
    if not candidates:
        return None
    # Newest plan_version wins.
    candidates.sort(key=lambda t: getattr(t, "plan_version", 1) or 1, reverse=True)
    return _build_plan(svc, candidates[0])


def _build_plan(svc, task) -> APIPlan:
    body = _extract_plan_body(task)
    summary = _extract_plan_summary(body)
    judgment_calls = [
        APIPlanJudgmentCall(point=point) for point in _extract_judgment_calls(body)
    ]
    critic = _extract_critic_synthesis(body)
    created = getattr(task, "created_at", None) or datetime.utcnow()
    return APIPlan(
        task_id=task.task_id,
        version=getattr(task, "plan_version", 1) or 1,
        predecessor_task_id=getattr(task, "predecessor_task_id", None),
        summary=summary,
        judgment_calls=judgment_calls,
        body=body,
        critic_synthesis=critic,
        created_at=created,
    )


_HEADER_RE = re.compile(r"^#{1,6}\s+(?P<title>.+?)\s*$")
_BULLET_RE = re.compile(r"^\s*[-*]\s+(?P<text>.+?)\s*$")


def _extract_plan_body(task) -> str:
    """Pull the plan markdown out of the task.

    Plans land in the task's ``description`` (the architect writes the
    full markdown there before transitioning to review). We fall back
    to the latest review-node execution's ``work_output.summary`` if
    the description is empty (older flows wrote to that surface).
    """
    desc = getattr(task, "description", "") or ""
    if desc.strip():
        return desc
    executions = getattr(task, "executions", []) or []
    for execution in reversed(executions):
        wo = getattr(execution, "work_output", None)
        if wo is not None and getattr(wo, "summary", None):
            return wo.summary
    return ""


def _extract_plan_summary(body: str) -> str:
    """First non-header paragraph of the plan; mirrors cockpit logic."""
    if not body.strip():
        return ""
    lines = body.splitlines()
    # Prefer a ``## Summary`` block.
    for idx, line in enumerate(lines):
        match = _HEADER_RE.match(line)
        if match and "summary" in match.group("title").lower():
            collected: list[str] = []
            for follow in lines[idx + 1:]:
                if follow.strip().startswith("#"):
                    break
                if not follow.strip():
                    if collected:
                        break
                    continue
                collected.append(follow.strip())
            if collected:
                return " ".join(collected)
            break
    # Fall back to the first paragraph.
    collected = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            if collected:
                break
            continue
        if not stripped:
            if collected:
                break
            continue
        collected.append(stripped)
    return " ".join(collected)


def _extract_judgment_calls(body: str, *, limit: int = 5) -> list[str]:
    """Mirror :func:`pollypm.cockpit_ui._extract_plan_judgment_calls`.

    The cockpit's helper isn't directly importable from the API path
    (it pulls Textual at module import time), so we reproduce its
    bullet-extraction logic here. The behaviour is identical: bullets
    under a ``## Judgment calls`` (or ``Judgement``) header, capped
    at ``limit``.
    """
    if not body.strip():
        return []
    target = {"## judgment calls", "## judgement calls", "### judgment calls"}
    out: list[str] = []
    capturing = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.lower() in target:
            capturing = True
            continue
        if not capturing:
            continue
        if stripped.startswith("#"):
            break
        match = _BULLET_RE.match(line)
        if match:
            point = re.sub(r"\s+", " ", match.group("text")).strip()
            if point:
                out.append(point)
                if len(out) >= limit:
                    break
    return out


def _extract_critic_synthesis(body: str) -> str | None:
    if not body.strip():
        return None
    target = {"## critic synthesis", "### critic synthesis", "## architect critic"}
    collected: list[str] = []
    capturing = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.lower() in target:
            capturing = True
            continue
        if not capturing:
            continue
        if stripped.startswith("#"):
            break
        if not stripped and collected:
            collected.append("")
            continue
        if stripped:
            collected.append(stripped)
    text = "\n".join(collected).strip()
    return text or None


def _is_plan_task(task) -> bool:
    flow_id = (getattr(task, "flow_template_id", "") or "").lower()
    if "plan" in flow_id:
        return True
    labels = getattr(task, "labels", []) or []
    return any("plan" in str(lbl).lower() for lbl in labels)


def _is_in_review(task) -> bool:
    status = getattr(task, "work_status", None)
    if status is None:
        return False
    value = getattr(status, "value", str(status))
    return value == "review"


# ---------------------------------------------------------------------------
# Task → API conversion
# ---------------------------------------------------------------------------


def _task_to_summary(task) -> APITaskSummary:
    return APITaskSummary(
        task_id=task.task_id,
        project=task.project,
        task_number=task.task_number,
        title=task.title,
        work_status=_enum_value(task.work_status),
        type=_enum_value(task.type),
        priority=_enum_value(task.priority),
        assignee=task.assignee,
        current_node_id=task.current_node_id,
        plan_version=getattr(task, "plan_version", None),
        updated_at=getattr(task, "updated_at", None),
    )


def _task_to_detail(task, *, plan: APIPlan | None = None) -> APITaskDetail:
    relationships = APITaskRelationships(
        parent=_pair_to_id(task.parent_project, task.parent_task_number),
        children=[_pair_to_id(p, n) for p, n in (task.children or [])],
        blocks=[_pair_to_id(p, n) for p, n in (task.blocks or [])],
        blocked_by=[_pair_to_id(p, n) for p, n in (task.blocked_by or [])],
        relates_to=[_pair_to_id(p, n) for p, n in (task.relates_to or [])],
        supersedes=_pair_to_id(task.supersedes_project, task.supersedes_task_number),
        superseded_by=_pair_to_id(
            task.superseded_by_project, task.superseded_by_task_number
        ),
    )
    transitions = [
        APITransition(
            from_state=t.from_state,
            to_state=t.to_state,
            actor=t.actor,
            timestamp=t.timestamp,
            reason=t.reason,
        )
        for t in (task.transitions or [])
    ]
    executions = [_execution_to_api(e) for e in (task.executions or [])]
    context = [
        APIContextEntry(
            actor=c.actor,
            timestamp=c.timestamp,
            text=c.text,
            entry_type=c.entry_type or "note",
        )
        for c in (task.context or [])
    ]
    return APITaskDetail(
        task_id=task.task_id,
        project=task.project,
        task_number=task.task_number,
        title=task.title,
        work_status=_enum_value(task.work_status),
        type=_enum_value(task.type),
        priority=_enum_value(task.priority),
        assignee=task.assignee,
        current_node_id=task.current_node_id,
        plan_version=getattr(task, "plan_version", None),
        updated_at=getattr(task, "updated_at", None),
        description=task.description or "",
        acceptance_criteria=task.acceptance_criteria,
        constraints=task.constraints,
        labels=task.labels or [],
        relevant_files=task.relevant_files or [],
        relationships=relationships,
        flow_template_id=task.flow_template_id or None,
        flow_template_version=task.flow_template_version,
        requires_human_review=getattr(task, "requires_human_review", False),
        predecessor_task_id=getattr(task, "predecessor_task_id", None),
        transitions=transitions,
        executions=executions,
        context=context,
        external_refs=task.external_refs or {},
        total_input_tokens=getattr(task, "total_input_tokens", 0),
        total_output_tokens=getattr(task, "total_output_tokens", 0),
        session_count=getattr(task, "session_count", 0),
        created_at=task.created_at,
        created_by=task.created_by or "",
        plan=plan,
    )


def _execution_to_api(execution) -> APIFlowNodeExecution:
    work_output: APIWorkOutput | None = None
    if execution.work_output is not None:
        artifacts = [
            APIArtifact(
                kind=_enum_value(a.kind),
                description=a.description,
                ref=a.ref,
                path=a.path,
                external_ref=a.external_ref,
            )
            for a in (execution.work_output.artifacts or [])
        ]
        work_output = APIWorkOutput(
            type=_enum_value(execution.work_output.type),
            summary=execution.work_output.summary,
            artifacts=artifacts or None,
        )
    return APIFlowNodeExecution(
        task_id=execution.task_id,
        node_id=execution.node_id,
        visit=execution.visit,
        status=_enum_value(execution.status),
        decision=_enum_value(execution.decision) if execution.decision else None,
        decision_reason=execution.decision_reason,
        started_at=execution.started_at,
        completed_at=execution.completed_at,
        work_output=work_output,
    )


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _pair_to_id(project: str | None, number: int | None) -> str | None:
    if not project or number is None:
        return None
    return f"{project}/{number}"


# ---------------------------------------------------------------------------
# Inbox helpers
# ---------------------------------------------------------------------------


def list_inbox(
    config: PollyPMConfig,
    *,
    project: str | None = None,
    type_filter: str | None = None,
    state_filter: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[APIInboxItem], str | None]:
    """Aggregate inbox view across one or all projects.

    Mirrors :func:`pollypm.cockpit_inbox.render_inbox_panel` at a
    coarser granularity — the API only exposes the typed shape per
    spec §7. Projects with no inbox state contribute nothing.
    """
    items = _collect_inbox_items(config, project=project)
    if type_filter is not None:
        items = [i for i in items if i.type == type_filter]
    if state_filter is not None:
        items = [i for i in items if i.state == state_filter]
    items.sort(key=lambda item: item.updated_at, reverse=True)

    cursor_idx = 0
    if cursor is not None:
        for idx, item in enumerate(items):
            if item.id == cursor:
                cursor_idx = idx + 1
                break
    page = items[cursor_idx : cursor_idx + limit]
    next_cursor: str | None = None
    if cursor_idx + limit < len(items) and page:
        next_cursor = page[-1].id
    return page, next_cursor


def get_inbox_item(config: PollyPMConfig, item_id: str) -> APIInboxItemDetail | None:
    items = _collect_inbox_items(config, project=None)
    target = next((item for item in items if item.id == item_id), None)
    if target is None:
        return None
    messages = _load_inbox_messages(config, target)
    return APIInboxItemDetail(
        id=target.id,
        project=target.project,
        type=target.type,
        state=target.state,
        subject=target.subject,
        preview=target.preview,
        owner=target.owner,
        thread_id=target.thread_id,
        created_at=target.created_at,
        updated_at=target.updated_at,
        metadata=target.metadata,
        messages=messages,
    )


def _collect_inbox_items(
    config: PollyPMConfig, *, project: str | None
) -> list[APIInboxItem]:
    """Load inbox items from the work-service for the requested projects.

    "Inbox" here = chat-flow tasks + plan-review tasks. Mirrors the
    set the cockpit panel surfaces. Each task becomes one inbox
    entry; ``id`` is the task_id so later detail / reply paths can
    address it.
    """
    out: list[APIInboxItem] = []
    keys: Iterable[str]
    if project is not None:
        keys = (project,) if project in config.projects else ()
    else:
        keys = config.projects.keys()

    for key in keys:
        proj = config.projects[key]
        try:
            with _open_work_service_readonly(
                config=config, project_key=key, project_path=proj.path
            ) as svc:
                tasks = svc.list_tasks(project=key)
        except _BACKING_STORE_ERRORS as exc:
            # Backing-store failure on a single project: log loudly,
            # skip that project but keep building the aggregate. We
            # intentionally don't 503 the whole inbox — the dashboard
            # would rather show 4-of-5 projects than fail open. Use
            # ``warning`` so this is visible without DEBUG and add
            # exc_info so the stack lands in the operator's logs.
            logger.warning(
                "inbox: backing store error for %s; skipping: %s",
                key,
                exc,
                exc_info=True,
            )
            continue
        for task in tasks:
            entry = _task_to_inbox_item(task)
            if entry is not None:
                out.append(entry)
    return out


def _task_to_inbox_item(task) -> APIInboxItem | None:
    flow = (getattr(task, "flow_template_id", "") or "").lower()
    labels = [str(lbl) for lbl in (getattr(task, "labels", []) or [])]
    is_plan_review = any("plan_review" in lbl for lbl in labels) or _is_plan_task(task)
    is_chat = flow == "chat"
    if not (is_chat or is_plan_review):
        return None
    item_type = "plan_review" if is_plan_review and not is_chat else "message"
    state = _inbox_state_from_task(task)
    if state == "closed":
        return None
    body = task.description or ""
    preview = body.strip().splitlines()[0] if body.strip() else None
    metadata: dict[str, Any] = {
        "task_id": task.task_id,
        "labels": labels,
        "flow_template_id": task.flow_template_id,
    }
    if is_plan_review:
        metadata["judgment_calls"] = _extract_judgment_calls(body)
    return APIInboxItem(
        id=task.task_id,
        project=task.project,
        type=item_type,
        state=state,
        subject=task.title,
        preview=preview,
        owner=_inbox_owner_for_task(task),
        thread_id=task.task_id,
        created_at=task.created_at or datetime.utcnow(),
        updated_at=task.updated_at or task.created_at or datetime.utcnow(),
        metadata=metadata,
    )


def _inbox_state_from_task(task) -> str:
    status = getattr(task, "work_status", None)
    value = getattr(status, "value", str(status)) if status else ""
    if value in {"done", "cancelled"}:
        return "closed"
    if value == "review":
        return "waiting-on-pm"
    if value in {"in_progress", "queued", "draft"}:
        return "open"
    if value in {"blocked", "on_hold", "rework"}:
        return "open"
    return "open"


def _inbox_owner_for_task(task) -> str:
    roles = getattr(task, "roles", {}) or {}
    operator = str(roles.get("operator", "")).lower()
    if operator in {"pm", "pa", "worker", "operator"}:
        return operator
    if "architect" in operator:
        return "pm"
    return "pm"


def _load_inbox_messages(config: PollyPMConfig, item: APIInboxItem) -> list[APIInboxMessage]:
    """Pull the context-log entries that drive an inbox thread."""
    project = config.projects.get(item.project)
    if project is None:
        return []

    out: list[APIInboxMessage] = []
    try:
        with _open_work_service_readonly(
            config=config, project_key=item.project, project_path=project.path
        ) as svc:
            entries = svc.get_context(item.id) or []
    except Exception as exc:  # noqa: BLE001
        logger.debug("inbox: get_context failed for %s: %s", item.id, exc)
        return out
    for idx, entry in enumerate(entries):
        out.append(APIInboxMessage(
            id=f"{item.id}#{idx}",
            sender=entry.actor or "operator",
            timestamp=entry.timestamp,
            body=entry.text,
        ))
    return out


# ---------------------------------------------------------------------------
# Audit-log → API event
# ---------------------------------------------------------------------------


def audit_event_to_api(event: AuditEvent) -> APIEvent:
    ts = _parse_iso(event.ts) or datetime.utcnow()
    return APIEvent.model_validate({
        "schema": event.schema,
        "ts": ts,
        "project": event.project,
        "event": event.event,
        "subject": event.subject,
        "actor": event.actor,
        "status": event.status,
        "metadata": event.metadata or {},
    })


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def load_api_config(config_path: Path | None) -> PollyPMConfig:
    """Load PollyPM config without bringing tmux / supervisor along.

    The API server intentionally avoids ``PollyPMService.load_supervisor``
    so it stays usable with the cockpit down. ``load_config`` is the
    shared, side-effect-free path the cockpit dashboard also uses.
    """
    from pollypm.config import DEFAULT_CONFIG_PATH

    return load_config(config_path or DEFAULT_CONFIG_PATH)


__all__ = [
    "audit_event_to_api",
    "get_active_plan",
    "get_inbox_item",
    "get_project",
    "get_task_detail",
    "list_inbox",
    "list_project_tasks",
    "list_projects",
    "load_api_config",
    "project_drilldown",
]
