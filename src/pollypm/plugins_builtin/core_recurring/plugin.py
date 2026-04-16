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

from pollypm.plugin_api.v1 import JobHandlerAPI, PollyPMPlugin, RosterAPI


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


def inbox_sweep_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Tier-1/2/3 inbox triage sweep — migrated from ``inbox_processor``."""
    from pollypm.inbox_processor import process_inbox

    # Allow the payload to target a specific project root; otherwise fall
    # back to the config's project root for backwards compat.
    project_root_hint = payload.get("project_root") if isinstance(payload, dict) else None
    if project_root_hint:
        project_root = Path(project_root_hint)
        from pollypm.config import DEFAULT_CONFIG_PATH, load_config, resolve_config_path
        from pollypm.storage.state import StateStore

        config_path = resolve_config_path(DEFAULT_CONFIG_PATH)
        if config_path.exists():
            store = StateStore(load_config(config_path).project.state_db)
        else:
            # Fall back to a per-project store — better than crashing.
            store = StateStore(project_root / ".pollypm" / "state.db")
    else:
        config, store = _load_config_and_store(payload)
        project_root = config.project.root_dir

    counts = process_inbox(project_root, store)
    return dict(counts)


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


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------


def _register_handlers(api: JobHandlerAPI) -> None:
    api.register_handler(
        "inbox.sweep", inbox_sweep_handler,
        max_attempts=2, timeout_seconds=30.0,
    )
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


def _register_roster(api: RosterAPI) -> None:
    # Cadences match the task spec for issue #164.
    api.register_recurring("@every 15s", "inbox.sweep", {})
    api.register_recurring("@every 10s", "session.health_sweep", {})
    api.register_recurring("@every 60s", "capacity.probe", {})
    api.register_recurring("@every 30s", "transcript.ingest", {})
    api.register_recurring("@every 5m", "alerts.gc", {})


plugin = PollyPMPlugin(
    name="core_recurring",
    version="0.1.0",
    description=(
        "Built-in recurring handlers — migrated from the old heartbeat loop. "
        "Registers inbox sweep, session health sweep, capacity probe, "
        "transcript ingest, and alerts GC on the roster + job queue."
    ),
    capabilities=("roster", "job_handler"),
    register_handlers=_register_handlers,
    register_roster=_register_roster,
)
