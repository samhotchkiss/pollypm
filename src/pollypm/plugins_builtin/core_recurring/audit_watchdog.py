"""``audit.watchdog`` cadence handler — surface forensic findings.

Born from the savethenovel post-mortem (2026-05-06): the audit log
(#1342) records every task / marker mutation; this handler reads
those events on a fixed cadence and surfaces "broken state" findings
via the existing ``upsert_alert`` channel — the same surface used by
``blocked_chain.sweep`` (#1073) and the supervisor's session-health
sweep. No new alert channel is introduced.

The handler is a thin wrapper around :mod:`pollypm.audit.watchdog`,
which holds the pure detectors. The heartbeat scheduler invokes
``audit_watchdog_handler`` every 5 minutes; per-tick behaviour:

1. Emit a ``heartbeat.tick`` audit event so liveness is observable.
2. For each known project, read the last ``window_seconds * 2`` of
   audit events and run the four detectors.
3. For every finding, emit an ``audit.finding`` audit event AND
   upsert an alert keyed by ``(rule, project, subject)``.

Idempotent across ticks because :meth:`upsert_alert` collapses
repeats. The emitted ``audit.finding`` events are deliberately
duplicated each tick — the audit log is append-only and operators
want to see "this fired N ticks in a row" as a signal of severity.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pollypm.audit.watchdog import (
    Finding,
    WATCHDOG_ALERT_TYPE,
    WatchdogConfig,
    emit_finding,
    emit_heartbeat_tick,
    format_finding_message,
    scan_project,
    watchdog_alert_session_name,
)


logger = logging.getLogger(__name__)


__all__ = [
    "audit_watchdog_handler",
    "AUDIT_WATCHDOG_HANDLER_NAME",
    "AUDIT_WATCHDOG_SCHEDULE",
]


AUDIT_WATCHDOG_HANDLER_NAME = "audit.watchdog"
# Every 5 minutes is the same cadence as ``stuck_claims.sweep`` /
# ``alerts.gc`` — enough to catch the savethenovel-class symptom
# within ~5 min of occurrence without spamming the queue.
AUDIT_WATCHDOG_SCHEDULE = "@every 5m"


def _route_to_alert_sink(
    finding: Finding,
    *,
    msg_store: Any,
    state_store: Any,
) -> bool:
    """Upsert a finding via the unified Store, falling back to StateStore.

    Mirrors ``blocked_chain._emit_alert``: prefer the unified
    ``msg_store`` (post-#349 path) and fall back to the legacy
    ``StateStore`` so the cadence still works on installs that
    haven't migrated yet.
    """
    target = msg_store or state_store
    if target is None:
        return False
    upsert = getattr(target, "upsert_alert", None)
    if not callable(upsert):
        return False
    session_name = watchdog_alert_session_name(
        finding.rule, finding.project, finding.subject,
    )
    try:
        upsert(
            session_name,
            WATCHDOG_ALERT_TYPE,
            finding.severity,
            format_finding_message(finding),
        )
        return True
    except Exception:  # noqa: BLE001 — alert path failures must not crash the sweep
        logger.debug(
            "audit.watchdog: upsert_alert failed for %s/%s",
            finding.rule, finding.project, exc_info=True,
        )
        return False


def _config_from_payload(payload: dict[str, Any]) -> WatchdogConfig:
    """Build a :class:`WatchdogConfig` from the handler payload."""
    if not isinstance(payload, dict):
        return WatchdogConfig()
    kwargs: dict[str, int] = {}
    for field_name in ("window_seconds", "stuck_draft_seconds", "cancel_grace_seconds"):
        raw = payload.get(field_name)
        if raw is None:
            continue
        try:
            kwargs[field_name] = max(0, int(raw))
        except (TypeError, ValueError):
            continue
    return WatchdogConfig(**kwargs) if kwargs else WatchdogConfig()


def _scan_one_project(
    *,
    project_key: str,
    project_path: Path | None,
    msg_store: Any,
    state_store: Any,
    now: datetime,
    config: WatchdogConfig,
) -> dict[str, int]:
    """Scan one project and route every finding. Returns counters."""
    counters = {
        "findings": 0,
        "alerts_raised": 0,
        "alert_failures": 0,
    }
    try:
        findings = scan_project(
            project_key,
            project_path=project_path,
            now=now,
            config=config,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "audit.watchdog: scan_project(%s) failed",
            project_key, exc_info=True,
        )
        return counters

    for finding in findings:
        counters["findings"] += 1
        emit_finding(finding)
        if _route_to_alert_sink(
            finding, msg_store=msg_store, state_store=state_store,
        ):
            counters["alerts_raised"] += 1
        else:
            counters["alert_failures"] += 1
            # Last-resort surface so a finding with no alert sink
            # still lands somewhere visible to operators.
            logger.warning(
                "audit.watchdog finding [%s] %s/%s: %s | %s",
                finding.rule, finding.project, finding.subject,
                finding.message, finding.recommendation,
            )
    return counters


def audit_watchdog_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Cadence handler entry point.

    Reads the audit log for every known project, runs the watchdog
    detectors, and routes every finding to an alert + a forensic
    audit event.

    Returns a dict with per-project + total counters for observability.
    """
    from pollypm.runtime_services import load_runtime_services

    config = _config_from_payload(payload or {})
    config_path_hint = (
        payload.get("config_path") if isinstance(payload, dict) else None
    )
    config_path = Path(config_path_hint) if config_path_hint else None

    services = load_runtime_services(config_path=config_path)
    now = datetime.now(UTC)

    # Liveness ping — emitted before scanning so that even if
    # scanning blows up the heartbeat-tick still lands in the log.
    emit_heartbeat_tick(metadata={"cadence": AUDIT_WATCHDOG_SCHEDULE})

    totals = {
        "projects_scanned": 0,
        "findings": 0,
        "alerts_raised": 0,
        "alert_failures": 0,
    }

    try:
        seen_projects: set[str] = set()
        for project in services.known_projects or ():
            project_key = getattr(project, "key", None)
            if not project_key or project_key in seen_projects:
                continue
            seen_projects.add(project_key)
            project_path = getattr(project, "path", None)
            partial = _scan_one_project(
                project_key=project_key,
                project_path=Path(project_path) if project_path else None,
                msg_store=services.msg_store,
                state_store=services.state_store,
                now=now,
                config=config,
            )
            totals["projects_scanned"] += 1
            for k, v in partial.items():
                totals[k] += v
    finally:
        try:
            services.close()
        except Exception:  # noqa: BLE001
            logger.debug(
                "audit.watchdog: services.close raised", exc_info=True,
            )

    return {"outcome": "swept", **totals}
