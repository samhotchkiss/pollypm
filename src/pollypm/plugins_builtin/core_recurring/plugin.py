"""Built-in recurring handlers migrated from the old heartbeat loop.

Previously, the supervisor dispatched recurring work (inbox sweep, capacity
probe, session health sweep, transcript ingest, alert GC) on each tick via
direct function calls. Track 7 moves these onto the durable roster + job
queue so the heartbeat's entire responsibility is to enqueue and a separate
worker pool drains the queue.

Handlers live as module-level callables taking a payload dict; plugins
register them via ``register_handlers`` and register cadence via
``register_roster``. See issue #164.

Handlers must be self-sufficient — they receive only a JSON-serializable
payload, so each one loads the shared PollyPM config internally. The payload
may carry per-invocation hints (e.g. ``project_root`` overrides).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pollypm.plugin_api.v1 import Capability, JobHandlerAPI, PollyPMPlugin, RosterAPI


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Handlers — each is a standalone callable, tolerant of partial config.
# ---------------------------------------------------------------------------


def _load_config_and_store(payload: dict[str, Any]):
    """Open the config + state store for a handler invocation.

    Handlers accept an optional ``config_path`` override in ``payload`` so
    tests (and alt installations) can target a non-default config. Falls
    back to the global default discovery.

    Returns ``(config, store)``; the store is closed by caller via
    ``finally: store.close()`` — but for our handlers we use short-lived
    stores that exit with the function so garbage collection handles it.
    """
    from pollypm.config import DEFAULT_CONFIG_PATH, load_config, resolve_config_path

    override = payload.get("config_path") if isinstance(payload, dict) else None
    config_path = Path(override) if override else resolve_config_path(DEFAULT_CONFIG_PATH)
    if not config_path.exists():
        raise RuntimeError(
            f"PollyPM config not found at {config_path}; cannot run recurring handler"
        )
    config = load_config(config_path)

    from pollypm.storage.state import StateStore

    store = StateStore(config.project.state_db)
    return config, store


def session_health_sweep_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Run one round of session health classification.

    Mirrors the supervisor's Phase 2 "fast synchronous sweep" — builds the
    ``SupervisorHeartbeatAPI``, invokes the configured heartbeat backend,
    collects alerts. Returns a small summary.

    The supervisor still owns the tmux-touching pieces, so this handler
    instantiates a transient ``Supervisor`` bound to the current config.
    Works for the co-located single-process setup; plugin overlays can
    replace this with a network-aware implementation.
    """
    config, _store = _load_config_and_store(payload)

    # Late import to avoid a supervisor import cycle at plugin load.
    from pollypm.supervisor import Supervisor

    supervisor = Supervisor(config)
    alerts = supervisor.run_heartbeat(snapshot_lines=int(payload.get("snapshot_lines", 200) or 200))
    return {"alerts_raised": len(alerts)}


def capacity_probe_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Probe capacity for every configured account."""
    config, store = _load_config_and_store(payload)

    from pollypm.capacity import probe_all_accounts

    probes = probe_all_accounts(config, store)
    summary = {probe.account_name: probe.state.value for probe in probes}
    return {"probes": summary}


def transcript_ingest_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Tail provider transcripts into the shared events ledger."""
    config, _store = _load_config_and_store(payload)

    from pollypm.transcript_ingest import sync_transcripts_once

    sync_transcripts_once(config)
    return {"ok": True}


def work_progress_sweep_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Scan in_progress tasks for staleness and emit resume pings (#249).

    Complements the 10s ``session.health_sweep`` — at a lower 5-min
    cadence we iterate every ``in_progress`` task whose current actor is
    a machine (``actor_type != user``) and check:

      * The claimant session exists.
      * The claimant session is idle (``is_turn_active == False``).
      * The session hasn't recorded an event in the last 30 minutes.

    When all three hold we re-emit the assignment event through
    ``task_assignment_notify``'s ``notify()`` helper — the plugin's
    existing 30-min dedupe table (``task_notifications``) guarantees at
    most one ping per (session, task) per 30 minutes.

    Returns a small summary so the job runner records useful output.
    Never raises on a per-task failure — the sweep continues.
    """
    from datetime import UTC, datetime, timedelta

    from pollypm.plugins_builtin.task_assignment_notify.handlers.sweep import (
        _build_event_for_task,
    )
    from pollypm.plugins_builtin.task_assignment_notify.resolver import (
        DEDUPE_WINDOW_SECONDS,
        load_runtime_services,
    )
    from pollypm.plugins_builtin.task_assignment_notify.resolver import (
        notify as _notify,
    )
    from pollypm.work.models import ActorType
    from pollypm.work.task_assignment import SessionRoleIndex

    # 30 min — mirrors the stuck_on_task threshold + dedupe window.
    STALE_THRESHOLD_SECONDS = int(
        payload.get("stale_threshold_seconds") or 1800,
    )

    config_path_hint = payload.get("config_path")
    config_path = Path(config_path_hint) if config_path_hint else None
    services = load_runtime_services(config_path=config_path)
    work = services.work_service
    state_store = services.state_store
    session_svc = services.session_service

    if work is None:
        return {"outcome": "skipped", "reason": "no_work_service"}

    considered = 0
    pinged = 0
    skipped_active_turn = 0
    skipped_recent_event = 0
    skipped_no_session = 0
    deduped = 0

    try:
        try:
            tasks = work.list_tasks(work_status="in_progress")
        except Exception:  # noqa: BLE001
            logger.debug(
                "work.progress_sweep: list_tasks(in_progress) failed",
                exc_info=True,
            )
            return {"outcome": "failed", "reason": "list_tasks_error"}

        # Index: resolve each event's target session handle.
        index = (
            SessionRoleIndex(session_svc, work_service=work)
            if session_svc is not None else None
        )

        now = datetime.now(UTC)
        for task in tasks:
            try:
                event = _build_event_for_task(work, task)
            except Exception:  # noqa: BLE001
                continue
            if event is None:
                continue
            # Only machine actors — we don't ping humans.
            if event.actor_type is ActorType.HUMAN:
                continue
            considered += 1

            # Resolve the target session.
            handle = None
            if index is not None:
                try:
                    handle = index.resolve(
                        event.actor_type, event.actor_name, event.project,
                    )
                except Exception:  # noqa: BLE001
                    handle = None
            if handle is None:
                skipped_no_session += 1
                continue
            target_name = getattr(handle, "name", "")
            if not target_name:
                skipped_no_session += 1
                continue

            # Skip actively-turning sessions — we don't ping mid-work.
            if session_svc is not None:
                checker = getattr(session_svc, "is_turn_active", None)
                if callable(checker):
                    try:
                        if bool(checker(target_name)):
                            skipped_active_turn += 1
                            continue
                    except Exception:  # noqa: BLE001
                        pass

            # Skip sessions that have recorded ANY event recently — the
            # session is clearly still doing something; a stale task
            # here is orthogonal to session liveness.
            if state_store is not None:
                try:
                    row = state_store.execute(
                        "SELECT created_at FROM events WHERE session_name = ? "
                        "ORDER BY id DESC LIMIT 1",
                        (target_name,),
                    ).fetchone()
                    if row and row[0]:
                        last_ts = datetime.fromisoformat(row[0])
                        if last_ts.tzinfo is None:
                            last_ts = last_ts.replace(tzinfo=UTC)
                        if (now - last_ts) < timedelta(
                            seconds=STALE_THRESHOLD_SECONDS,
                        ):
                            skipped_recent_event += 1
                            continue
                except Exception:  # noqa: BLE001
                    pass

            # Fire the ping — notify() enforces the 30-min dedupe.
            try:
                outcome = _notify(
                    event,
                    services=services,
                    throttle_seconds=DEDUPE_WINDOW_SECONDS,
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "work.progress_sweep: notify failed for %s",
                    event.task_id, exc_info=True,
                )
                continue
            result = str(outcome.get("outcome", ""))
            if result == "deduped":
                deduped += 1
            elif result == "sent":
                pinged += 1
                # Raise a low-severity stuck_on_task alert so the
                # cockpit surfaces the event to the operator.
                if state_store is not None:
                    try:
                        state_store.upsert_alert(
                            target_name,
                            f"stuck_on_task:{event.task_id}",
                            "warning",
                            (
                                f"Session {target_name} stuck on "
                                f"{event.task_id} — resume ping sent"
                            ),
                        )
                    except Exception:  # noqa: BLE001
                        pass
    finally:
        closer = getattr(work, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass

    return {
        "outcome": "swept",
        "considered": considered,
        "pinged": pinged,
        "deduped": deduped,
        "skipped_active_turn": skipped_active_turn,
        "skipped_recent_event": skipped_recent_event,
        "skipped_no_session": skipped_no_session,
    }


def alerts_gc_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Release expired leases and prune old events/heartbeat rows.

    Leases are auto-released via ``Supervisor.release_expired_leases``;
    stale rows are pruned via ``StateStore.prune_old_data``. Both are
    cheap, idempotent, and safe to run from any worker thread.
    """
    config, store = _load_config_and_store(payload)

    # Lease GC lives on the supervisor because it records events with
    # owner context; use a transient supervisor here.
    from pollypm.supervisor import Supervisor

    supervisor = Supervisor(config)
    released = supervisor.release_expired_leases()

    pruned = store.prune_old_data()
    return {
        "leases_released": len(released),
        "events_pruned": int(pruned.get("events", 0)),
        "heartbeats_pruned": int(pruned.get("heartbeats", 0)),
    }


def db_vacuum_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Run an incremental vacuum against StateStore to reclaim freelist pages.

    Incremental vacuum is a low-cost operation that only touches pages on
    the SQLite freelist — it does not rewrite the whole database the way
    a full ``VACUUM`` would. Safe to call daily. The shared StateStore
    connection coordinates with concurrent writers via busy_timeout, so
    no external lock is needed beyond what StateStore already holds.
    """
    _config, store = _load_config_and_store(payload)
    bytes_reclaimed = store.incremental_vacuum()
    mb_reclaimed = bytes_reclaimed / (1024 * 1024)
    store.record_event(
        session_name="system",
        event_type="db.vacuum",
        message=f"reclaimed {mb_reclaimed:.1f}MB",
    )
    return {"bytes_reclaimed": bytes_reclaimed, "mb_reclaimed": mb_reclaimed}


def memory_ttl_sweep_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop expired memory_entries (TTL in the past).

    Only affects rows with ``ttl_at IS NOT NULL``. Rows without an
    explicit TTL are left alone — retention policy for those is a
    separate decision, this handler just enforces what's already on
    the row.
    """
    _config, store = _load_config_and_store(payload)
    deleted = store.sweep_expired_memory_entries()
    store.record_event(
        session_name="system",
        event_type="memory.ttl_sweep",
        message=f"dropped {deleted} expired entries",
    )
    return {"deleted": deleted}


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------


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
        "transcript.ingest", transcript_ingest_handler,
        max_attempts=2, timeout_seconds=30.0,
    )
    api.register_handler(
        "alerts.gc", alerts_gc_handler,
        max_attempts=1, timeout_seconds=60.0,
    )
    api.register_handler(
        "work.progress_sweep", work_progress_sweep_handler,
        max_attempts=1, timeout_seconds=60.0,
    )
    # DB hygiene — incremental vacuum + memory TTL sweep. Both are cheap
    # daily sweeps that coordinate with the shared StateStore connection.
    api.register_handler(
        "db.vacuum", db_vacuum_handler,
        max_attempts=1, timeout_seconds=120.0,
    )
    api.register_handler(
        "memory.ttl_sweep", memory_ttl_sweep_handler,
        max_attempts=1, timeout_seconds=60.0,
    )


def _register_roster(api: RosterAPI) -> None:
    # Cadences match the task spec for issue #164. ``inbox.sweep`` was
    # removed with the legacy inbox subsystem (see iv04).
    api.register_recurring("@every 10s", "session.health_sweep", {})
    api.register_recurring("@every 60s", "capacity.probe", {})
    api.register_recurring("@every 30s", "transcript.ingest", {})
    api.register_recurring("@every 5m", "alerts.gc", {})
    # #249 — work-service-aware stuck-task sweeper.
    api.register_recurring("@every 5m", "work.progress_sweep", {})
    # DB hygiene — daily around 4am local. Off-minute (``7``) avoids
    # fleet-wide sync if many cockpits run on the same host. Memory TTL
    # sweep runs a few minutes later so its writes don't collide with
    # the vacuum's page-reclaim pass.
    api.register_recurring("7 4 * * *", "db.vacuum", {}, dedupe_key="db.vacuum")
    api.register_recurring(
        "13 4 * * *", "memory.ttl_sweep", {}, dedupe_key="memory.ttl_sweep",
    )


plugin = PollyPMPlugin(
    name="core_recurring",
    version="0.1.0",
    description=(
        "Built-in recurring handlers — migrated from the old heartbeat loop. "
        "Registers inbox sweep, session health sweep, capacity probe, "
        "transcript ingest, alerts GC, and work-service progress sweep on "
        "the roster + job queue."
    ),
    capabilities=(
        Capability(kind="job_handler", name="inbox.sweep"),
        Capability(kind="job_handler", name="session.health_sweep"),
        Capability(kind="job_handler", name="capacity.probe"),
        Capability(kind="job_handler", name="transcript.ingest"),
        Capability(kind="job_handler", name="alerts.gc"),
        Capability(kind="job_handler", name="work.progress_sweep"),
        Capability(kind="job_handler", name="db.vacuum"),
        Capability(kind="job_handler", name="memory.ttl_sweep"),
        Capability(kind="roster_entry", name="core_recurring"),
    ),
    register_handlers=_register_handlers,
    register_roster=_register_roster,
)
