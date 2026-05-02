"""Built-in recurring handlers migrated from the old heartbeat loop."""

from __future__ import annotations

import logging
from typing import Any

from pollypm.plugin_api.v1 import Capability, JobHandlerAPI, PollyPMPlugin, RosterAPI

from pollypm.plugins_builtin.core_recurring.maintenance import (
    AUDIT_EVENT_SUBJECTS,
    HIGH_VOLUME_EVENT_SUBJECTS,
    OPERATIONAL_EVENT_SUBJECTS,
    account_usage_refresh_handler,
    agent_worktree_prune_handler,
    capacity_probe_handler,
    db_vacuum_handler,
    log_rotate_handler,
    memory_ttl_sweep_handler,
    notification_staging_prune_handler,
    transcript_ingest_handler,
)
from pollypm.plugins_builtin.core_recurring.shared import (  # noqa: F401
    _close_msg_store,
    _ephemeral_alert_type,
    _load_config_and_store,
    _open_msg_store,
    _resolve_config_path,
    is_ephemeral_session_name,
    sweep_ephemeral_sessions,
)
from pollypm.plugins_builtin.core_recurring.sweeps import (
    pane_text_classify_handler,
    work_progress_sweep_handler,
    worktree_state_audit_handler,
)


logger = logging.getLogger(__name__)


def session_health_sweep_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Run one round of session health classification."""
    with _load_config_and_store(payload) as (config, store):
        from pollypm.supervisor import Supervisor

        supervisor = Supervisor(config)
        alerts = supervisor.run_heartbeat(
            snapshot_lines=int(payload.get("snapshot_lines", 200) or 200),
        )

        ephemeral_summary = {
            "considered": 0,
            "alerts_raised": 0,
            "skipped_planned": 0,
            "zombie_task_windows_killed": 0,
        }
        try:
            msg_store = getattr(supervisor, "_msg_store", None)
            ephemeral_summary = sweep_ephemeral_sessions(
                supervisor, msg_store or store,
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "session.health_sweep: ephemeral pass failed", exc_info=True,
            )

        return {
            "alerts_raised": len(alerts),
            "ephemeral_considered": ephemeral_summary["considered"],
            "ephemeral_alerts_raised": ephemeral_summary["alerts_raised"],
            "ephemeral_skipped_planned": ephemeral_summary["skipped_planned"],
            "ephemeral_zombie_task_windows_killed": (
                ephemeral_summary.get("zombie_task_windows_killed", 0)
            ),
        }


def alerts_gc_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Release expired leases and prune old heartbeat rows."""
    with _load_config_and_store(payload) as (config, store):
        from pollypm.supervisor import Supervisor

        supervisor = Supervisor(config)
        released = supervisor.release_expired_leases()
        # ``event_days=None`` skips the events prune — tiered retention
        # is handled by ``events_retention_sweep_handler``. Passing a
        # large sentinel like ``10**6`` used to overflow ``datetime``
        # when subtracted from ``now()`` (#1047).
        pruned = store.prune_old_data(event_days=None)
        return {
            "leases_released": len(released),
            "events_pruned": int(pruned.get("events", 0)),
            "heartbeats_pruned": int(pruned.get("heartbeats", 0)),
        }


def events_retention_sweep_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply tiered retention to ``messages WHERE type='event'`` (#267 / #342)."""
    from datetime import datetime, timedelta, timezone

    with _load_config_and_store(payload) as (config, _store):
        settings = config.events
        now = datetime.now(timezone.utc)
        msg_store = _open_msg_store(config)
        if msg_store is None:
            return {
                "deleted_audit": 0,
                "deleted_operational": 0,
                "deleted_high_volume": 0,
                "deleted_default": 0,
                "total": 0,
            }

        try:
            audit_cutoff = now - timedelta(days=settings.audit_retention_days)
            operational_cutoff = now - timedelta(
                days=settings.operational_retention_days,
            )
            high_volume_cutoff = now - timedelta(
                days=settings.high_volume_retention_days,
            )
            default_cutoff = now - timedelta(days=settings.default_retention_days)

            deleted_audit = 0
            deleted_operational = 0
            deleted_high_volume = 0
            deleted_default = 0

            for subject in AUDIT_EVENT_SUBJECTS:
                deleted_audit += _prune_event_subject(
                    msg_store, subject, audit_cutoff,
                )
            for subject in OPERATIONAL_EVENT_SUBJECTS:
                deleted_operational += _prune_event_subject(
                    msg_store, subject, operational_cutoff,
                )
            for subject in HIGH_VOLUME_EVENT_SUBJECTS:
                deleted_high_volume += _prune_event_subject(
                    msg_store, subject, high_volume_cutoff,
                )

            known = (
                AUDIT_EVENT_SUBJECTS
                | OPERATIONAL_EVENT_SUBJECTS
                | HIGH_VOLUME_EVENT_SUBJECTS
            )
            try:
                from sqlalchemy import and_ as _and
                from sqlalchemy import delete as _delete
                from sqlalchemy import func as _func

                from pollypm.store.schema import messages as _messages

                result = msg_store.execute(
                    _delete(_messages).where(
                        _and(
                            _messages.c.type == "event",
                            _messages.c.subject.notin_(tuple(known)),
                            _messages.c.created_at < default_cutoff,
                            _func.coalesce(
                                _func.json_extract(_messages.c.payload_json, "$.pinned"),
                                0,
                            )
                            != 1,
                        )
                    )
                )
                deleted_default = int(getattr(result, "rowcount", 0) or 0)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "events.retention_sweep: default-tier delete failed",
                    exc_info=True,
                )
                deleted_default = 0

            total = (
                deleted_audit
                + deleted_operational
                + deleted_high_volume
                + deleted_default
            )
            counts = {
                "deleted_audit": deleted_audit,
                "deleted_operational": deleted_operational,
                "deleted_high_volume": deleted_high_volume,
                "deleted_default": deleted_default,
                "total": total,
            }

            if total > 0:
                try:
                    msg_store.record_event(
                        scope="system",
                        sender="system",
                        subject="events.retention_sweep",
                        payload={
                            "message": (
                                f"deleted {total} events "
                                f"(audit={deleted_audit}, "
                                f"operational={deleted_operational}, "
                                f"high_volume={deleted_high_volume}, "
                                f"default={deleted_default})"
                            ),
                            **counts,
                        },
                    )
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "events.retention_sweep: audit-event emit failed",
                        exc_info=True,
                    )
        finally:
            _close_msg_store(msg_store)

        return counts


def _prune_event_subject(msg_store: Any, subject: str, cutoff: Any) -> int:
    """Delete ``type='event'`` rows matching ``subject`` older than ``cutoff``."""
    try:
        from sqlalchemy import and_ as _and
        from sqlalchemy import delete as _delete
        from sqlalchemy import func as _func

        from pollypm.store.schema import messages as _messages

        result = msg_store.execute(
            _delete(_messages).where(
                _and(
                    _messages.c.type == "event",
                    _messages.c.subject == subject,
                    _messages.c.created_at < cutoff,
                    _func.coalesce(
                        _func.json_extract(_messages.c.payload_json, "$.pinned"),
                        0,
                    )
                    != 1,
                )
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)
    except Exception:  # noqa: BLE001
        logger.debug(
            "events.retention_sweep: delete failed for subject=%s",
            subject, exc_info=True,
        )
        return 0


def _register_handlers(api: JobHandlerAPI) -> None:
    api.register_handler(
        "session.health_sweep", session_health_sweep_handler,
        max_attempts=1, timeout_seconds=120.0,
    )
    api.register_handler(
        "capacity.probe", capacity_probe_handler,
        max_attempts=2, timeout_seconds=30.0,
    )
    api.register_handler(
        "account.usage_refresh", account_usage_refresh_handler,
        max_attempts=1, timeout_seconds=300.0,
    )
    api.register_handler(
        "transcript.ingest", transcript_ingest_handler,
        max_attempts=2, timeout_seconds=600.0,
    )
    api.register_handler(
        "alerts.gc", alerts_gc_handler,
        max_attempts=1, timeout_seconds=60.0,
    )
    api.register_handler(
        "work.progress_sweep", work_progress_sweep_handler,
        max_attempts=1, timeout_seconds=60.0,
    )
    api.register_handler(
        "pane.classify", pane_text_classify_handler,
        max_attempts=1, timeout_seconds=60.0,
    )
    api.register_handler(
        "db.vacuum", db_vacuum_handler,
        max_attempts=1, timeout_seconds=120.0,
    )
    api.register_handler(
        "memory.ttl_sweep", memory_ttl_sweep_handler,
        max_attempts=1, timeout_seconds=60.0,
    )
    api.register_handler(
        "events.retention_sweep", events_retention_sweep_handler,
        max_attempts=1, timeout_seconds=60.0,
    )
    api.register_handler(
        "notification_staging.prune", notification_staging_prune_handler,
        max_attempts=1, timeout_seconds=60.0,
    )
    api.register_handler(
        "agent_worktree.prune", agent_worktree_prune_handler,
        max_attempts=1, timeout_seconds=120.0,
    )
    api.register_handler(
        "log.rotate", log_rotate_handler,
        max_attempts=1, timeout_seconds=120.0,
    )
    api.register_handler(
        "worktree.state_audit", worktree_state_audit_handler,
        max_attempts=1, timeout_seconds=120.0,
    )


def _register_roster(api: RosterAPI) -> None:
    api.register_recurring("@every 10s", "session.health_sweep", {})
    api.register_recurring("@every 60s", "capacity.probe", {})
    api.register_recurring("@every 5m", "account.usage_refresh", {})
    api.register_recurring("@every 5m", "transcript.ingest", {})
    api.register_recurring("@every 5m", "alerts.gc", {})
    api.register_recurring("@every 5m", "work.progress_sweep", {})
    api.register_recurring("@every 30s", "pane.classify", {})
    api.register_recurring("7 4 * * *", "db.vacuum", {}, dedupe_key="db.vacuum")
    api.register_recurring(
        "13 4 * * *", "memory.ttl_sweep", {}, dedupe_key="memory.ttl_sweep",
    )
    api.register_recurring(
        "37 * * * *", "events.retention_sweep", {},
        dedupe_key="events.retention_sweep",
    )
    api.register_recurring(
        "19 4 * * *", "notification_staging.prune", {},
        dedupe_key="notification_staging.prune",
    )
    api.register_recurring(
        "23 * * * *", "agent_worktree.prune", {},
        dedupe_key="agent_worktree.prune",
    )
    api.register_recurring(
        "31 * * * *", "log.rotate", {},
        dedupe_key="log.rotate",
    )
    api.register_recurring("@every 10m", "worktree.state_audit", {})


plugin = PollyPMPlugin(
    name="core_recurring",
    version="0.1.0",
    description=(
        "Built-in recurring handlers — migrated from the old heartbeat loop. "
        "Registers inbox sweep, session health sweep, capacity probe, "
        "account usage refresh, transcript ingest, alerts GC, and "
        "work-service progress sweep on the roster + job queue."
    ),
    capabilities=(
        Capability(kind="job_handler", name="inbox.sweep"),
        Capability(kind="job_handler", name="session.health_sweep"),
        Capability(kind="job_handler", name="capacity.probe"),
        Capability(kind="job_handler", name="account.usage_refresh"),
        Capability(kind="job_handler", name="transcript.ingest"),
        Capability(kind="job_handler", name="alerts.gc"),
        Capability(kind="job_handler", name="work.progress_sweep"),
        Capability(kind="job_handler", name="pane.classify"),
        Capability(kind="job_handler", name="db.vacuum"),
        Capability(kind="job_handler", name="memory.ttl_sweep"),
        Capability(kind="job_handler", name="events.retention_sweep"),
        Capability(kind="job_handler", name="notification_staging.prune"),
        Capability(kind="job_handler", name="agent_worktree.prune"),
        Capability(kind="job_handler", name="log.rotate"),
        Capability(kind="job_handler", name="worktree.state_audit"),
        Capability(kind="roster_entry", name="core_recurring"),
    ),
    register_handlers=_register_handlers,
    register_roster=_register_roster,
)
