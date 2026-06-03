"""Dashboard read endpoint (Phase 2 — Q1, §3.1 of the Phase 2 spec).

Implements ``GET /api/v1/dashboard`` per
``~/Desktop/pollypm-phase2-endpoints-spec.md`` §3.1. The route is a
thin adapter over :func:`pollypm.dashboard_data.gather` (the helper
the cockpit rail renders from) plus :func:`pollypm.web_api.service.list_projects`.

Out of scope for this PR:

- ``GET /api/v1/dashboard/stream`` SSE (deferred to a follow-up PR;
  spec §3.2).
- ``?async=true`` long-op path (spec §2.7) — sync only for now.

The route's behaviour mirrors the cockpit per spec §3.3 edge cases:

- A missing daemon is a normal operating mode — return ``200`` with
  ``daemon_status="down"`` instead of 503.
- A transient ``gather`` failure (pg pool down, broken project)
  surfaces as ``503 service_unavailable`` so clients retry rather than
  treat empty data as truth.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from pollypm.web_api.dashboard_snapshot_cache import (
    DashboardSnapshot,
    DashboardSnapshotCache,
)
from pollypm.web_api.errors import not_found, service_unavailable
from pollypm.web_api.models import Project
from pollypm.web_api.routes._deps import ConfigDep
from pollypm.web_api.service import list_projects

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Dashboard"])


# ---------------------------------------------------------------------------
# Response models (spec §3.1 — mirror the cockpit dataclasses)
# ---------------------------------------------------------------------------


class SessionActivityModel(BaseModel):
    """Active-session row for the dashboard (cockpit ``SessionActivity``)."""

    name: str
    role: str
    project: str
    project_label: str
    status: str
    description: str
    age_seconds: float


class CommitInfoModel(BaseModel):
    """Recent commit row (cockpit ``CommitInfo``)."""

    hash: str
    message: str
    author: str
    age_seconds: float
    project: str


class CompletedItemModel(BaseModel):
    """Recently-completed work item (cockpit ``CompletedItem``)."""

    title: str
    kind: str
    project: str
    age_seconds: float


class InboxPreviewModel(BaseModel):
    """Recent inbox preview row (cockpit ``InboxPreview``)."""

    sender: str
    title: str
    project: str
    task_id: str
    age_seconds: float


class AccountQuotaUsageModel(BaseModel):
    """Per-account quota row (cockpit ``AccountQuotaUsage``)."""

    account_name: str
    provider: str
    email: str
    used_pct: int
    summary: str
    severity: str
    limit_label: str = "limit"
    reset_at: str = ""


class DashboardRollups(BaseModel):
    """Aggregate counters mirrored from the cockpit rail.

    Under ``?project=<key>``, only the project-derived counters
    (``tracked_count``, ``open_inbox_count``, ``pending_plan_reviews``,
    ``alert_count``) are narrowed to that project. The three ``*_24h``
    activity counters remain whole-system because the underlying
    ``gather`` pipeline aggregates them globally before returning (see
    :func:`pollypm.dashboard_data.gather`). The set of fields actually
    narrowed for a given response is enumerated on the envelope's
    ``scoped_fields`` list so clients don't have to guess.
    """

    tracked_count: int
    open_inbox_count: int
    pending_plan_reviews: int
    alert_count: int
    sweep_count_24h: int
    message_count_24h: int
    recovery_count_24h: int


class DashboardTokens(BaseModel):
    """Token-usage summary."""

    today: int
    total: int


class DashboardAlertPreviewModel(BaseModel):
    """Project-scoped actionable alert preview for selected-project views."""

    session_name: str
    alert_type: str
    severity: str
    message: str
    updated_at: str


class DashboardResponse(BaseModel):
    """``GET /api/v1/dashboard`` envelope (spec §3.1)."""

    generated_at: datetime
    daemon_status: str = Field(
        description=(
            "One of 'up' (active sessions present anywhere in the "
            "system) or 'down' (no active sessions / supervisor "
            "unreachable). Always derived from the unfiltered gather "
            "result — ``?project=`` does NOT influence this field, so "
            "callers polling per-project still see the true daemon "
            "health. Per spec §3.3, a down daemon is a normal "
            "operating mode — not a 503."
        ),
    )
    projects: list[Project]
    rollups: DashboardRollups
    scoped_fields: list[str] = Field(
        default_factory=list,
        description=(
            "Names of response fields actually narrowed by "
            "``?project=``. Empty when no filter is in effect. Lets "
            "clients tell which counters reflect the requested project "
            "vs which remain whole-system (see ``DashboardRollups`` "
            "docstring for why a subset of rollups stay global)."
        ),
    )
    active_sessions: list[SessionActivityModel]
    recent_commits: list[CommitInfoModel]
    completed_items: list[CompletedItemModel]
    recent_messages: list[InboxPreviewModel]
    tokens: DashboardTokens
    account_usages: list[AccountQuotaUsageModel]
    project_alerts: list[DashboardAlertPreviewModel] = Field(
        default_factory=list,
        description=(
            "Actionable alerts owned by the requested ``?project=``. "
            "Empty for unfiltered/all-project responses."
        ),
    )
    daily_tokens: list[list[Any]] | None = Field(
        default=None,
        description=(
            "Optional ``[date, tokens]`` history (last 30 days). Only "
            "populated when ``?include_token_history=true`` since the "
            "list can be large."
        ),
    )
    briefing: str | None = Field(
        default=None,
        description=(
            "Optional morning-briefing narrative. Populated by default; "
            "set ``?include_briefing=false`` only for very small polling "
            "responses."
        ),
    )


# ---------------------------------------------------------------------------
# Adapters (dataclass -> pydantic wire shape)
# ---------------------------------------------------------------------------


def _session_activity_to_wire(row: Any) -> SessionActivityModel:
    return SessionActivityModel(
        name=row.name,
        role=row.role,
        project=row.project,
        project_label=row.project_label,
        status=row.status,
        description=row.description,
        age_seconds=float(row.age_seconds),
    )


def _commit_to_wire(row: Any) -> CommitInfoModel:
    return CommitInfoModel(
        hash=row.hash,
        message=row.message,
        author=row.author,
        age_seconds=float(row.age_seconds),
        project=row.project,
    )


def _completed_to_wire(row: Any) -> CompletedItemModel:
    return CompletedItemModel(
        title=row.title,
        kind=row.kind,
        project=row.project,
        age_seconds=float(row.age_seconds),
    )


def _inbox_preview_to_wire(row: Any) -> InboxPreviewModel:
    return InboxPreviewModel(
        sender=row.sender,
        title=row.title,
        project=row.project,
        task_id=row.task_id,
        age_seconds=float(row.age_seconds),
    )


def _account_usage_to_wire(row: Any) -> AccountQuotaUsageModel:
    return AccountQuotaUsageModel(
        account_name=row.account_name,
        provider=row.provider,
        email=row.email,
        used_pct=int(row.used_pct),
        summary=row.summary,
        severity=row.severity,
        limit_label=row.limit_label,
        reset_at=row.reset_at,
    )


def _alert_preview_to_wire(row: Any) -> DashboardAlertPreviewModel:
    return DashboardAlertPreviewModel(
        session_name=str(getattr(row, "session_name", "") or ""),
        alert_type=str(getattr(row, "alert_type", "") or ""),
        severity=str(getattr(row, "severity", "") or ""),
        message=str(getattr(row, "message", "") or ""),
        updated_at=str(getattr(row, "updated_at", "") or ""),
    )


# ---------------------------------------------------------------------------
# Gather wrapper (pg-only path; sqlite store-open lives in load_dashboard)
# ---------------------------------------------------------------------------


def _gather_dashboard(config: Any) -> Any:
    """Call :func:`pollypm.dashboard_data.gather` for the route.

    Passes ``store=None`` — :func:`gather` already branches on the
    pg backend and opens its own pg facades. We do not open a
    :class:`StateStore` here because the spec mandates pg-only paths
    for new web-api code.
    """
    from pollypm.dashboard_data import gather

    return gather(config, None, use_state_cache=False)


# ---------------------------------------------------------------------------
# Snapshot loading / cache access
# ---------------------------------------------------------------------------


def _get_dashboard_snapshot_cache(request: Request) -> DashboardSnapshotCache:
    """Resolve the app-scoped dashboard cache, creating a fallback for tests."""

    cache = getattr(request.app.state, "dashboard_snapshot_cache", None)
    if cache is None:
        cache = DashboardSnapshotCache()
        request.app.state.dashboard_snapshot_cache = cache
    return cache


def _call_dashboard_source_pair(
    config: Any,
) -> tuple[tuple[bool, Any], tuple[bool, Any]]:
    """Load project rows and rollup data concurrently."""

    results: dict[str, tuple[bool, Any]] = {}
    lock = threading.Lock()

    def _run(name: str, func: Any) -> None:
        try:
            value = func()
        except Exception as exc:  # noqa: BLE001
            value = exc
            ok = False
        else:
            ok = True
        with lock:
            results[name] = (ok, value)

    threads = [
        threading.Thread(
            target=_run,
            name="dashboard-list-projects",
            args=("projects", lambda: list_projects(config, operator_facing=True)),
            daemon=True,
        ),
        threading.Thread(
            target=_run,
            name="dashboard-gather",
            args=("data", lambda: _gather_dashboard(config)),
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    return results["projects"], results["data"]


def _fresh_dashboard_project_alerts(
    config: Any,
    project: str,
) -> list[object] | None:
    """Return fresh project-scoped actionable alerts, or ``None`` on failure."""

    try:
        from pollypm.dashboard_data import dashboard_actionable_alerts_for_project
        from pollypm.storage.pg_alerts import open_alerts

        return dashboard_actionable_alerts_for_project(
            config,
            project,
            list(open_alerts(config=config)),
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "dashboard: fresh project alert refresh failed; using cached value",
            exc_info=True,
        )
        return None


def _fresh_dashboard_alert_count(
    config: Any,
    project: str | None = None,
) -> int | None:
    """Return a fresh alert count so stale dashboard snapshots cannot alarm high."""

    try:
        if project is not None:
            alerts = _fresh_dashboard_project_alerts(config, project)
            return None if alerts is None else len(alerts)
        from pollypm.dashboard_data import count_dashboard_alerts

        return count_dashboard_alerts(config)
    except Exception:  # noqa: BLE001
        logger.debug(
            "dashboard: fresh alert-count refresh failed; using cached value",
            exc_info=True,
        )
        return None


def _normalize_dashboard_alert_count(config: Any, data: Any) -> int | None:
    """Write the liveness-filtered alert count onto dashboard data."""

    fresh_alert_count = _fresh_dashboard_alert_count(config)
    if fresh_alert_count is None:
        return None
    try:
        data.alert_count = int(fresh_alert_count)
    except Exception:  # noqa: BLE001
        logger.debug(
            "dashboard: failed to normalize cached alert_count",
            exc_info=True,
        )
        return None
    return int(fresh_alert_count)


def _load_dashboard_snapshot(config: Any) -> DashboardSnapshot:
    """Run the expensive dashboard load once and normalize errors."""

    projects_slot, data_slot = _call_dashboard_source_pair(config)
    projects_ok, projects_result = projects_slot
    data_ok, data_result = data_slot

    if not projects_ok:
        # list_projects swallows per-project failures internally; a
        # raise here means the config itself is unreadable.
        logger.warning("dashboard: list_projects failed: %s", projects_result)
        raise service_unavailable(
            "Failed to load project list for dashboard",
            hint="Check `pm doctor` and config.toml health.",
        ) from projects_result
    if not data_ok:
        logger.warning("dashboard: gather failed: %s", data_result)
        raise service_unavailable(
            "Dashboard gather failed; backing store may be unreachable",
            hint="Retry shortly; check `pm doctor` for pg pool health.",
        ) from data_result

    normalized_alert_count = _normalize_dashboard_alert_count(
        config,
        data_result,
    )
    return DashboardSnapshot(
        generated_at=datetime.now(timezone.utc),
        projects=tuple(projects_result),
        data=data_result,
        refreshed_at_monotonic=time.monotonic(),
        normalized_alert_count=normalized_alert_count,
    )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/dashboard",
    response_model=DashboardResponse,
    summary="Aggregate dashboard / cockpit state",
    operation_id="getDashboard",
)
async def get_dashboard_endpoint(
    config: ConfigDep,
    request: Request,
    project: Annotated[
        str | None,
        Query(
            description=(
                "Narrow the per-project lists (projects, sessions, "
                "commits, completed items, recent messages) and the "
                "project-derived rollup counters to one project key. "
                "The response's ``scoped_fields`` enumerates the exact "
                "fields narrowed; the three 24h activity rollups and "
                "``daemon_status`` remain whole-system."
            ),
        ),
    ] = None,
    include_token_history: Annotated[
        bool,
        Query(
            description=(
                "Include the last 30 days of token usage as "
                "``daily_tokens=[[date, tokens], ...]``. Default false — "
                "the list can be large."
            ),
        ),
    ] = False,
    include_briefing: Annotated[
        bool,
        Query(
            description=(
                "Include the morning-briefing narrative in the response. "
                "Default true because the Home surface uses this field; "
                "set false for tiny machine-polling responses."
            ),
        ),
    ] = True,
) -> DashboardResponse:
    """GET /api/v1/dashboard per Phase 2 spec §3.1.

    Returns the operator's whole-system snapshot — same shape the
    cockpit rail computes. Eventually-consistent: clients that need
    sub-second freshness must use the deferred SSE channel
    (``/dashboard/stream``, follow-up PR).
    """
    if project is not None and project not in config.projects:
        raise not_found(
            f"Project not registered: {project}",
            hint="Drop ?project= to fetch the whole-system snapshot.",
        )

    snapshot_cache = _get_dashboard_snapshot_cache(request)
    snapshot = await snapshot_cache.get_or_refresh(config, _load_dashboard_snapshot)

    # Projects view — list_projects is the authoritative cockpit row
    # builder; reuse it so the rollups derived below match the project
    # rows exactly. In parallel, gather loads the aggregate dashboard
    # payload (active sessions, commits, tokens, …). Transient pg outages
    # surface as 503 per spec §3.3 final bullet; a healthy daemon-down
    # state is captured below as ``daemon_status="down"`` (200, not 503).
    projects_all = list(snapshot.projects)
    data = snapshot.data

    if project is not None:
        projects_view = [p for p in projects_all if p.key == project]
    else:
        projects_view = list(projects_all)

    # Derive daemon health from the UNFILTERED gather result. This is a
    # system-wide signal — a caller polling ``?project=foo`` must still
    # see ``daemon_status="up"`` when the supervisor is healthy and the
    # only live sessions happen to be on ``bar``. Computed above the
    # per-project list slice below so the filter cannot mask supervisor
    # state.
    #
    # We can't use ``bool(data.active_sessions)`` directly: ``gather``
    # appends a ``SessionActivity`` for every planned launch in
    # ``config.projects`` (see ``dashboard_data.gather`` around L823),
    # and rows with no matching ``runtime_map`` entry are stamped with
    # the synthetic ``status="unknown"`` sentinel. On a configured
    # workspace with the supervisor down and no runtime rows in pg, the
    # active-sessions list is still non-empty — so list-truthiness would
    # report ``daemon_status="up"`` while the daemon is genuinely down
    # (Codex review on #2057). Only rows whose status came from a real
    # runtime record (anything other than ``"unknown"``) count as live.
    daemon_status = (
        "up"
        if any(s.status != "unknown" for s in data.active_sessions)
        else "down"
    )

    # Narrow per-project lists when ?project= is set so the response is
    # self-consistent (a caller filtering to one project shouldn't see
    # other projects' commits / inbox previews).
    active_sessions = list(data.active_sessions)
    recent_commits = list(data.recent_commits)
    completed_items = list(data.completed_items)
    recent_messages = list(data.recent_messages)
    scoped_fields: list[str] = []
    if project is not None:
        active_sessions = [s for s in active_sessions if s.project == project]
        recent_commits = [c for c in recent_commits if c.project == project]
        completed_items = [c for c in completed_items if c.project == project]
        recent_messages = [m for m in recent_messages if m.project == project]
        # Enumerate the fields that are actually project-scoped under
        # ``?project=`` so clients don't have to guess which rollups
        # were narrowed. The three ``rollups.*`` 24h activity counters
        # (sweep_count_24h, message_count_24h, recovery_count_24h)
        # are intentionally NOT listed because they are aggregated
        # globally inside ``gather``.
        scoped_fields = [
            "projects",
            "active_sessions",
            "recent_commits",
            "completed_items",
            "recent_messages",
            "rollups.tracked_count",
            "rollups.open_inbox_count",
            "rollups.pending_plan_reviews",
            "rollups.alert_count",
        ]

    # Rollups — project-derived counters narrow with ``?project=``;
    # the three ``gather``-computed 24h activity counters stay global and
    # are documented as such on the response (see ``scoped_fields``
    # above and the ``DashboardRollups`` docstring).
    tracked_count = sum(1 for p in projects_view if p.tracked)
    open_inbox_count = sum(p.open_inbox_count for p in projects_view)
    pending_plan_reviews = sum(
        1 for p in projects_view if p.pending_plan_review
    )
    # The full snapshot is intentionally stale-while-refresh. The loader
    # normalizes alert_count before any sync/background refresh is cached; for
    # older snapshots, refresh that one cheap counter and write it through so
    # the Home headline does not hold an old-high value (#2504).
    snapshot_age = time.monotonic() - snapshot.refreshed_at_monotonic
    project_alerts: list[object] = []
    if project is not None:
        fresh_project_alerts = _fresh_dashboard_project_alerts(config, project)
        if fresh_project_alerts is not None:
            project_alerts = fresh_project_alerts
            fresh_alert_count = len(project_alerts)
        else:
            raw_counts = getattr(data, "alert_counts_by_project", {}) or {}
            try:
                fresh_alert_count = int(raw_counts.get(project, 0) or 0)
            except (AttributeError, TypeError, ValueError):
                fresh_alert_count = 0
    elif snapshot_age > 2.0:
        fresh_alert_count = _normalize_dashboard_alert_count(config, data)
    else:
        # Prefer the loader's explicit normalized value so the sync
        # cache-miss response cannot fall back to the raw gathered count.
        fresh_alert_count = snapshot.normalized_alert_count

    rollups = DashboardRollups(
        tracked_count=tracked_count,
        open_inbox_count=open_inbox_count,
        pending_plan_reviews=pending_plan_reviews,
        alert_count=int(
            fresh_alert_count
            if fresh_alert_count is not None
            else (getattr(data, "alert_count", 0) or 0)
        ),
        sweep_count_24h=int(getattr(data, "sweep_count_24h", 0) or 0),
        message_count_24h=int(getattr(data, "message_count_24h", 0) or 0),
        recovery_count_24h=int(getattr(data, "recovery_count_24h", 0) or 0),
    )

    daily_tokens: list[list[Any]] | None = None
    if include_token_history:
        daily_tokens = [
            [str(date), int(tokens)]
            for date, tokens in (data.daily_tokens or [])
        ]

    briefing: str | None = None
    if include_briefing:
        briefing = getattr(data, "briefing", "") or ""

    return DashboardResponse(
        generated_at=snapshot.generated_at,
        daemon_status=daemon_status,
        projects=projects_view,
        rollups=rollups,
        scoped_fields=scoped_fields,
        active_sessions=[
            _session_activity_to_wire(s) for s in active_sessions
        ],
        recent_commits=[_commit_to_wire(c) for c in recent_commits],
        completed_items=[
            _completed_to_wire(c) for c in completed_items
        ],
        recent_messages=[
            _inbox_preview_to_wire(m) for m in recent_messages
        ],
        tokens=DashboardTokens(
            today=int(getattr(data, "today_tokens", 0) or 0),
            total=int(getattr(data, "total_tokens", 0) or 0),
        ),
        account_usages=[
            _account_usage_to_wire(u)
            for u in (data.account_usages or [])
        ],
        project_alerts=[
            _alert_preview_to_wire(alert)
            for alert in project_alerts[:5]
        ],
        daily_tokens=daily_tokens,
        briefing=briefing,
    )


__all__ = [
    "AccountQuotaUsageModel",
    "CommitInfoModel",
    "CompletedItemModel",
    "DashboardAlertPreviewModel",
    "DashboardResponse",
    "DashboardRollups",
    "DashboardSnapshotCache",
    "DashboardTokens",
    "InboxPreviewModel",
    "SessionActivityModel",
    "get_dashboard_endpoint",
    "router",
]
