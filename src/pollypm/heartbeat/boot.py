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
import threading
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pollypm.heartbeat import Heartbeat, Roster
from pollypm.jobs import JobQueue, JobWorkerPool
from pollypm.storage.sqlite_pragmas import retry_on_database_locked


__all__ = ["HeartbeatRail", "WorkerSettings", "load_worker_settings"]


logger = logging.getLogger(__name__)


DEFAULT_WORKER_CONCURRENCY = 4

# Default ticker cadence. 15s gives two ticks per 30s roster entry so
# any ``@every 30s`` handler fires with at most 15s latency.
DEFAULT_TICK_INTERVAL_SECONDS = 15.0


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
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
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

    ``start()`` also spawns a daemon ticker thread that calls ``tick()``
    on a fixed interval so any long-lived process that calls
    ``rail.start()`` gets periodic ticks for free. The ticker exits when
    ``stop()`` sets the stop event. For tests, use ``tick()`` directly —
    the ticker is a deployment-time convenience, not a test dependency.
    """

    __slots__ = (
        "queue", "pool", "heartbeat", "roster", "_concurrency",
        "_tick_interval", "_tick_thread", "_tick_stop", "_tick_lock",
    )

    def __init__(
        self,
        *,
        queue: JobQueue,
        pool: JobWorkerPool,
        heartbeat: Heartbeat,
        roster: Roster,
        concurrency: int = DEFAULT_WORKER_CONCURRENCY,
        tick_interval_seconds: float = DEFAULT_TICK_INTERVAL_SECONDS,
    ) -> None:
        self.queue = queue
        self.pool = pool
        self.heartbeat = heartbeat
        self.roster = roster
        self._concurrency = concurrency
        self._tick_interval = (
            float(tick_interval_seconds)
            if tick_interval_seconds and tick_interval_seconds > 0
            else DEFAULT_TICK_INTERVAL_SECONDS
        )
        self._tick_thread: threading.Thread | None = None
        self._tick_stop = threading.Event()
        self._tick_lock = threading.RLock()

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
        tick_interval_seconds: float = DEFAULT_TICK_INTERVAL_SECONDS,
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
            tick_interval_seconds=tick_interval_seconds,
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

    def start(self, *, start_workers: bool = True) -> None:
        """Start the worker pool and the background ticker. Idempotent.

        Spawns a daemon thread that calls :meth:`tick` every
        ``tick_interval_seconds`` seconds so recurring roster handlers
        actually fire in long-lived processes (cockpit TUI, future
        daemons) without requiring callers to wire their own clock.
        Calling ``start()`` again while already running is a silent
        no-op — a second ticker thread is never spawned.

        ``start_workers=False`` boots the rail in tick-only mode: the
        queue and heartbeat are ready for :meth:`tick` to enqueue jobs,
        but no JobWorkerPool or ticker thread is spawned. Used by the
        short-lived ``pm heartbeat`` cron path (#1060), which only
        needs to enqueue — the long-lived ``rail_daemon`` / cockpit
        rail drains the queue. Avoids the "thread did not stop within
        timeout" warning that fires when the cron process tears down a
        pool whose workers are mid-handler.
        """
        if self.pool.is_running:
            return
        if start_workers:
            # #1071 — recover any orphaned ``claimed`` rows from a
            # previous process before the worker pool starts. The
            # dedupe unique index covers status IN ('queued','claimed'),
            # so an orphan claimed row keeps its dedupe_key reserved
            # forever, silently blocking every cadence handler that
            # uses that key (e.g. ``task_assignment.sweep``,
            # ``session.health_sweep``, ``stuck_claims.sweep`` itself).
            try:
                recovered, pruned = self.queue.recover_orphaned_claims()
            except Exception:  # noqa: BLE001
                logger.exception(
                    "HeartbeatRail.start(): orphaned-claim recovery failed",
                )
            else:
                if recovered or pruned:
                    logger.info(
                        "HeartbeatRail.start(): requeued %d orphaned "
                        "claimed job(s) and pruned %d duplicate queued "
                        "row(s) from a prior process — dedupe slots freed",
                        recovered, pruned,
                    )
            # Log the cadence-handler roster so operators can verify
            # at a glance that registration landed all entries (#1071).
            try:
                entries = self.roster.entries
                logger.info(
                    "HeartbeatRail.start(): %d cadence handlers "
                    "registered: %s",
                    len(entries),
                    ", ".join(sorted({e.handler_name for e in entries})),
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "HeartbeatRail.start(): cadence-handler summary failed",
                    exc_info=True,
                )
            self.pool.start(concurrency=self._concurrency)
        # Reset the stop event in case ``stop()`` + ``start()`` is called
        # on the same rail (the pool is re-startable).
        self._tick_stop.clear()
        if not start_workers:
            return
        thread = self._tick_thread
        if thread is not None and thread.is_alive():
            # Defensive: should be unreachable because pool.is_running
            # gates entry, but belt-and-suspenders if a caller manually
            # pokes the pool.
            return
        self._tick_thread = threading.Thread(
            target=self._tick_loop,
            daemon=True,
            name="HeartbeatRail-ticker",
        )
        self._tick_thread.start()

    def _tick_loop(self) -> None:
        """Background loop: ``tick()`` every ``_tick_interval`` seconds.

        Exits as soon as ``_tick_stop`` is set. Exceptions from ``tick``
        are logged and swallowed so one bad tick can't kill the thread —
        the next iteration will try again.
        """
        # ``Event.wait(timeout)`` returns True when the event is set,
        # False when the timeout elapses. Looping "while not wait()"
        # gives us a clean interval-plus-fast-shutdown pattern.
        while not self._tick_stop.wait(self._tick_interval):
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                logger.exception("HeartbeatRail tick failed; continuing")

    def stop(self, *, timeout: float = 10.0) -> None:
        """Stop the ticker, the worker pool, and close the queue."""
        self._tick_stop.set()
        thread = self._tick_thread
        if thread is not None:
            thread.join(timeout=timeout)
            self._tick_thread = None
        try:
            self.pool.stop(timeout=timeout)
        finally:
            with self._tick_lock:
                self.queue.close()

    def tick(self, now: datetime | None = None):
        """Run one heartbeat tick — enqueue jobs for due roster entries.

        #1021 — defense-in-depth retry-on-lock at the top level. The
        callees (``Heartbeat.tick`` -> ``JobQueue.enqueue``) also retry
        individually, but if a future caller (e.g. a plugin) injects a
        queue that does not retry, we still don't want a transient WAL
        contention to bubble up here and crash the heartbeat ticker.
        Without this, every locked tick was logged via
        ``logger.exception`` in ``_tick_loop`` and surfaced as a
        ``critical_error`` heartbeat alert (#1021 evidence:
        ``boot.py:258 -> tick.py:143 -> queue.py:269``).
        """
        resolved = now or datetime.now(UTC)
        lock = getattr(self, "_tick_lock", None)

        def _do_tick():
            stop_event = getattr(self, "_tick_stop", None)
            if stop_event is not None and stop_event.is_set():
                return None
            return retry_on_database_locked(
                lambda: self.heartbeat.tick(resolved),
                label="HeartbeatRail.tick",
            )

        if lock is None:
            return _do_tick()
        with lock:
            return _do_tick()

    # Context manager sugar — mostly useful for tests.
    def __enter__(self) -> "HeartbeatRail":
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.stop()
