"""Alert list endpoints for the web dashboard.

The dashboard rollup already exposes an alert count via
``/api/v1/dashboard``. This route provides the matching drill-down
surface without teaching the UI how alerts are stored: rows come from
the existing ``pg_alerts`` facade and action labels come from the
cockpit alert-action registry.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from pollypm.cockpit_alert_actions import (
    recovery_actions_for,
    task_id_from_alert_type,
)
from pollypm.cockpit_alerts import alert_channel, is_operational_alert
from pollypm.storage.records import AlertRecord
from pollypm.web_api.errors import service_unavailable
from pollypm.web_api.routes._deps import ConfigDep

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Alerts"])


class AlertActionResponse(BaseModel):
    kind: str
    label: str
    session_name: str | None = None
    project_key: str | None = None
    task_id: str | None = None
    hint: str | None = None


class AlertResponse(BaseModel):
    id: int | None = None
    session_name: str
    alert_type: str
    severity: str
    message: str
    status: str
    channel: str
    created_at: str
    updated_at: str
    actions: list[AlertActionResponse] = Field(default_factory=list)


class AlertsResponse(BaseModel):
    generated_at: datetime
    total: int
    alerts: list[AlertResponse] = Field(default_factory=list)


def _task_project_key(task_id: str | None) -> str | None:
    if not task_id or "/" not in task_id:
        return None
    project, _, _number = task_id.partition("/")
    return project or None


def _alert_to_response(alert: AlertRecord) -> AlertResponse:
    task_id = task_id_from_alert_type(alert.alert_type)
    project_key = _task_project_key(task_id)
    plans = recovery_actions_for(
        alert.alert_type,
        session_name=alert.session_name,
        project_key=project_key,
        task_id=task_id,
        severity=alert.severity,
    )
    return AlertResponse(
        id=alert.alert_id,
        session_name=alert.session_name,
        alert_type=alert.alert_type,
        severity=alert.severity,
        message=alert.message,
        status=alert.status,
        channel=alert_channel(alert.alert_type).value,
        created_at=alert.created_at,
        updated_at=alert.updated_at,
        actions=[
            AlertActionResponse(
                kind=plan.kind,
                label=plan.label,
                session_name=plan.session_name,
                project_key=plan.project_key,
                task_id=plan.task_id,
                hint=plan.hint,
            )
            for plan in plans
        ],
    )


def _read_open_alerts(config: Any) -> list[AlertRecord]:
    try:
        from pollypm.storage.pg_alerts import open_alerts

        return list(open_alerts(config=config))
    except Exception as exc:  # noqa: BLE001
        logger.warning("alerts: open_alerts failed: %s", exc, exc_info=True)
        raise service_unavailable(
            "Failed to load alerts",
            hint="Retry shortly; check `pm doctor` for pg pool health.",
        ) from exc


@router.get(
    "/alerts",
    response_model=AlertsResponse,
    summary="List currently open alerts",
    operation_id="listAlerts",
)
def list_alerts_endpoint(
    config: ConfigDep,
    include_operational: Annotated[
        bool,
        Query(
            description=(
                "Include operational heartbeat/supervisor alerts. Default "
                "false so the list mirrors the dashboard's action-required "
                "rollup."
            ),
        ),
    ] = False,
    limit: Annotated[
        int,
        Query(ge=1, le=200, description="Maximum alert rows to return."),
    ] = 100,
) -> AlertsResponse:
    alerts = _read_open_alerts(config)
    if not include_operational:
        alerts = [
            alert for alert in alerts
            if not is_operational_alert(alert.alert_type)
        ]
    total = len(alerts)
    rows = [_alert_to_response(alert) for alert in alerts[:limit]]
    return AlertsResponse(
        generated_at=datetime.now(timezone.utc),
        total=total,
        alerts=rows,
    )


__all__ = [
    "AlertActionResponse",
    "AlertResponse",
    "AlertsResponse",
    "list_alerts_endpoint",
    "router",
]
