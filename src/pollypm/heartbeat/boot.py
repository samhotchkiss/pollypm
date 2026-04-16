"""Wire the sealed heartbeat + job queue + worker pool from config.

``HeartbeatRail`` is the small orchestration object that constructs the three
moving parts of the Track 7 architecture and starts them together:

* ``JobQueue`` — durable SQLite-backed queue (on ``config.project.state_db``).
* ``JobWorkerPool`` — background workers drain the queue via the plugin
  host's ``JobHandlerRegistry``.
* ``Heartbeat`` — sealed tick, consults the plugin-built roster and
  enqueues jobs at their scheduled cadences.

The heartbeat's tick cadence is driven by an external timer (the existing
supervisor or a user-level loop). HeartbeatRail only wires construction +
lifecycle; callers call ``tick()`` themselves when they're ready.

The worker concurrency is read from ``[heartbeat.workers]`` in the
``pollypm.toml`` config when present — default 4 if unspecified. Keeping
the knob in config avoids plumbing it through every construction call.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pollypm.heartbeat import Heartbeat, Roster
from pollypm.jobs import JobQueue, JobWorkerPool


__all__ = ["HeartbeatRail", "WorkerSettings", "load_worker_settings"]


logger = logging.getLogger(__name__)


DEFAULT_WORKER_CONCURRENCY = 4


@dataclass(slots=True)
class WorkerSettings:
    """Subset of ``pollypm.toml`` that controls the worker pool."""

    concurrency: int = DEFAULT_WORKER_CONCURRENCY
    poll_interval: float = 0.5


def load_worker_settings(config_path: Path) -> WorkerSettings:
    """Parse ``[heartbeat.workers]`` from the config (best-effort).

    Unknown or missing keys fall back to defaults. Errors are logged but
    never raise — worker defaults are safe for any installation.
    """
    try:
        raw = tomllib.loads(config_path.read_text())
    except FileNotFoundError:
        return WorkerSettings()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to parse %s for worker settings: %s", config_path, exc)
        return WorkerSettings()

    heartbeat_raw = raw.get("heartbeat", {})
    if not isinstance(heartbeat_raw, dict):
        return WorkerSettings()
    workers_raw = heartbeat_raw.get("workers", {})
    if not isinstance(workers_raw, dict):
        return WorkerSettings()

    try:
        concurrency = max(1, int(workers_raw.get("concurrency", DEFAULT_WORKER_CONCURRENCY)))
    except (TypeError, ValueError):
        concurrency = DEFAULT_WORKER_CONCURRENCY
    try:
        poll_interval = float(workers_raw.get("poll_interval", 0.5))
        if poll_interval <= 0:
            poll_interval = 0.5
    except (TypeError, ValueError):
        poll_interval = 0.5

    return WorkerSettings(concurrency=concurrency, poll_interval=poll_interval)


class HeartbeatRail:
    """Construct + start a JobQueue + JobWorkerPool + Heartbeat trio.

    Typical usage (e.g. from a supervisor or tests)::

        from pollypm.heartbeat.boot import HeartbeatRail

        rail = HeartbeatRail.from_config(config_path, plugin_host)
        rail.start()
        try:
            while running:
                rail.tick()
                sleep(60)
        finally:
            rail.stop()

    The roster is built from the plugin host at construction. The handler
    registry is also resolved via the plugin host so newly-registered
    handlers fire on the next tick without a restart.
    """

    __slots__ = ("queue", "pool", "heartbeat", "roster", "_concurrency")

    def __init__(
        self,
        *,
        queue: JobQueue,
        pool: JobWorkerPool,
        heartbeat: Heartbeat,
        roster: Roster,
        concurrency: int = DEFAULT_WORKER_CONCURRENCY,
    ) -> None:
        self.queue = queue
        self.pool = pool
        self.heartbeat = heartbeat
        self.roster = roster
        self._concurrency = concurrency

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_plugin_host(
        cls,
        *,
        state_db: Path,
        plugin_host: Any,
        config_path: Path | None = None,
        concurrency: int | None = None,
    ) -> "HeartbeatRail":
        """Build a HeartbeatRail from an already-resolved plugin host + DB path.

        The plugin host must expose ``build_roster()`` and
        ``job_handler_registry()`` (see :class:`pollypm.plugin_host.ExtensionHost`).
        """
        worker_settings = (
            load_worker_settings(config_path) if config_path else WorkerSettings()
        )
        effective_concurrency = (
            concurrency if concurrency is not None else worker_settings.concurrency
        )

        roster = plugin_host.build_roster()
        registry = plugin_host.job_handler_registry()

        # Fire the PluginAPI initialize hook now that roster + handler
        # registry are built — before the first heartbeat tick. Failures
        # mark the plugin degraded but don't kill the rail. See
        # docs/plugin-discovery-spec.md §6.
        init_method = getattr(plugin_host, "initialize_plugins", None)
        if callable(init_method):
            try:
                init_method(roster=roster, job_registry=registry)
            except Exception:  # noqa: BLE001
                # initialize_plugins already logs and tracks degraded
                # state; defensive catch here so boot never aborts.
                pass

        state_db = Path(state_db)
        state_db.parent.mkdir(parents=True, exist_ok=True)
        queue = JobQueue(db_path=state_db)
        pool = JobWorkerPool(
            queue, registry=registry, poll_interval=worker_settings.poll_interval,
        )
        heartbeat = Heartbeat(roster, queue)
        return cls(
            queue=queue, pool=pool, heartbeat=heartbeat,
            roster=roster, concurrency=effective_concurrency,
        )

    @classmethod
    def from_config(cls, config_path: Path, plugin_host: Any) -> "HeartbeatRail":
        """Convenience: load the config, resolve the DB path, build from host."""
        from pollypm.config import load_config

        config = load_config(config_path)
        return cls.from_plugin_host(
            state_db=config.project.state_db,
            plugin_host=plugin_host,
            config_path=config_path,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the worker pool. Idempotent — no-op if already started."""
        if self.pool.is_running:
            return
        self.pool.start(concurrency=self._concurrency)

    def stop(self, *, timeout: float = 10.0) -> None:
        """Stop the worker pool and close the queue connection."""
        try:
            self.pool.stop(timeout=timeout)
        finally:
            self.queue.close()

    def tick(self, now: datetime | None = None):
        """Run one heartbeat tick — enqueue jobs for due roster entries."""
        return self.heartbeat.tick(now or datetime.now(UTC))

    # Context manager sugar — mostly useful for tests.
    def __enter__(self) -> "HeartbeatRail":
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.stop()
