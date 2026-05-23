"""Audit-log tail → cache invalidation refresher.

See ``docs/design/move-a-state-cache.md`` §4.1 for the design.

This module wires the **audit-log tail** to a refresher worker
thread. The tail thread polls the central audit-log directory for
appended lines, parses each into an :class:`AuditEvent`, and maps the
event onto a :meth:`ProjectStateCache.invalidate` call. A second
worker thread drains the pending invalidations and calls
:meth:`ProjectStateCache.refresh` for each project — coalesced, so
N invalidations between drains collapse to one refresh.

PR 1 scope (this slice):

* Tail mechanism: subscribe + parse + dispatch event → invalidate.
* Refresh worker: drain pending + call ``cache.refresh``.
* Clean shutdown: ``stop()`` joins both threads.
* The :data:`pollypm.state_cache.project_state_cache.RefreshFn`
  injected here returns an empty entry. PR 2 replaces it with the
  real per-project query.

§9.1 decision baked in here: tail starts at ``seek-to-end``. A full
refresh of all currently-known projects fires on startup to close
the boot-time gap.

§9.6 decision baked in here: the refresher runs in the **cockpit
process**. Module import in any other process is a no-op unless that
process explicitly calls :func:`start_refresher`.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable

from pollypm.state_cache.entry import (
    WORKSPACE_PROJECT_KEY,
    ProjectStateCacheEntry,
    empty_entry,
)
from pollypm.state_cache.project_state_cache import ProjectStateCache

logger = logging.getLogger(__name__)

__all__ = ["StateCacheRefresher", "default_audit_dir", "stub_refresh_fn"]


# Event names that should invalidate a project's cache entry. Pinned
# to the constants exported from ``pollypm.audit.log`` so the cache
# stays in sync if those names ever change (a test pins this list).
#
# ``heartbeat.tick`` is workspace-scoped (audit_watchdog.emit_heartbeat_tick
# fires with ``project=""``) so a single tick invalidates every known
# project's entry. That's the contract that lets
# ``cockpit_rail._latest_heartbeat_cached`` serve the snapshot's
# ``latest_heartbeat_by_session`` fast-path without going stale —
# without this entry the cache would serve an indefinitely-old
# heartbeat and drive the UI's "offline" / "stale" treatments off it
# (#2050).
_INVALIDATING_EVENTS: frozenset[str] = frozenset({
    "task.created",
    "task.status_changed",
    "task.deleted",
    "marker.created",
    "marker.released",
    "marker.create_failed",
    "marker.leaked",
    "work_table.cleared",
    "heartbeat.tick",
})

# Signals that an event touches workspace-root awaits-user rows
# (``messages`` with ``scope IN ('', 'inbox')`` — they surface as
# ``project == 'inbox'`` after :func:`message_row_to_inbox_entry`).
# When :func:`_dispatch_event` sees one of these, it ALSO invalidates
# the ``__workspace__`` synthetic entry on top of whatever per-project
# invalidation the event triggers. Workspace-scoped events (empty
# ``project``) already enqueue every known key via ``invalidate(None)``
# — that path already covers the sentinel because the project-keys
# provider includes it.
#
# Tracked-project ``task.*`` events that ALSO write a workspace-root
# row (cross-posted notifications) are conservatively covered by
# invalidating ``__workspace__`` on every event in
# :data:`_INVALIDATING_EVENTS` — N+1 refreshes per event is cheap
# (the workspace-root sweep is a single bulk query) and the upside
# is that the sentinel can never go stale while the per-project
# entries refresh.
_WORKSPACE_ROOT_PROJECT_SIGNALS: frozenset[str] = frozenset({
    "",       # workspace-scoped emit (no project payload)
    "inbox",  # workspace-root inbox tasks / notifications
})

# Poll interval for the tail thread. Mirrors the existing SSE tail
# cadence (``src/pollypm/web_api/sse.py``) — 250ms keeps invalidation
# latency under a quarter-second without hammering the FS.
_TAIL_POLL_SECONDS = 0.25

# Refresh worker tick. Slightly longer than the tail poll so a burst
# of invalidations has time to coalesce.
_REFRESH_TICK_SECONDS = 0.10

# How long the join() in stop() waits before giving up. Threads are
# daemons so a stuck thread won't block process exit either way; this
# is just for orderly shutdown.
_JOIN_TIMEOUT_SECONDS = 2.0


def default_audit_dir() -> Path:
    """Resolve the central audit-log directory.

    Mirrors ``pollypm.audit.log._central_root`` without taking the
    import-cycle risk of pulling it directly into a leaf module.
    """

    override = os.environ.get("POLLYPM_AUDIT_HOME")
    if override:
        return Path(override).expanduser()
    try:
        from pollypm.config import DEFAULT_CONFIG_PATH

        return Path(DEFAULT_CONFIG_PATH).parent / "audit"
    except Exception:  # noqa: BLE001
        # noqa: pollypm-path-join — import-cycle fallback. This leaf module
        # mirrors ``audit.log._central_root`` without importing it (cycle
        # risk via state_cache → audit → state_cache). When the lazy
        # ``pollypm.config`` import above fails, we cannot route through
        # the typed helper (``pollypm.projects.global_pollypm_dir``)
        # without re-introducing the cycle. The literal is intentional.
        return Path.home() / ".pollypm" / "audit"  # noqa: pollypm-path-join


def stub_refresh_fn(project_key: str) -> ProjectStateCacheEntry | None:
    """PR 1 placeholder refresh — returns an empty entry.

    Replaced in PR 2 with the real per-project query that drives
    ``categorize_project`` + ``rollup_project_state`` +
    ``pm_inbox_awaits_user_list``. Splitting the swap out makes
    PR 1 reviewable as pure infra.
    """

    return empty_entry(project_key)


class _Position:
    """Track the byte offset reached in a single audit log file.

    The tail starts at the file's current size (§9.1 "seek to end").
    Each tick reads from the recorded offset to EOF. File rotation
    (size shrinking) is detected by ``stat().st_size < offset`` and
    resets the offset to 0 so the new file is replayed from start.
    """

    __slots__ = ("offset",)

    def __init__(self, offset: int = 0) -> None:
        self.offset = offset


class StateCacheRefresher:
    """Drives audit-log tail → cache invalidation → cache refresh.

    Spawns two daemon threads on :meth:`start`:

    1. **Tail thread** polls the audit dir, parses appended lines,
       maps each to ``cache.invalidate``.
    2. **Worker thread** drains pending invalidations and calls
       ``cache.refresh`` per project.

    Use :meth:`stop` for clean shutdown (in tests; production
    cockpit teardown can rely on daemon-thread death-on-exit).
    """

    def __init__(
        self,
        cache: ProjectStateCache,
        *,
        audit_dir: Path | None = None,
        tail_poll_seconds: float = _TAIL_POLL_SECONDS,
        refresh_tick_seconds: float = _REFRESH_TICK_SECONDS,
        project_keys: Callable[[], list[str]] | None = None,
        config_provider: Callable[[], object] | None = None,
    ) -> None:
        self._cache = cache
        self._audit_dir = audit_dir if audit_dir is not None else default_audit_dir()
        self._tail_poll_seconds = tail_poll_seconds
        self._refresh_tick_seconds = refresh_tick_seconds
        # Optional candidate-set provider. §9.3: rediscover on every
        # refresh — when supplied, the worker enqueues a refresh for
        # every key returned. PR 2 will pass the config-driven list.
        self._project_keys = project_keys
        # PR #2085 round-2 boundary fix: optional config provider used
        # to pre-fetch the workspace-wide actionable-alert snapshot
        # ONCE per sweep (``_initial_full_refresh`` /
        # ``_worker_once``). Without this each per-project refresh
        # would call ``_open_alerts_for(config)`` — Supervisor
        # construction + open_alerts read — N times per sweep, all
        # under the cache lock. With it, every sweep that touches >1
        # project pays for exactly one alert read.
        self._config_provider = config_provider
        self._positions: dict[Path, _Position] = {}
        self._stop_event = threading.Event()
        self._tail_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None

    # ── lifecycle ─────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the tail + worker threads.

        Idempotent: calling :meth:`start` on a running refresher is a
        no-op. Threads are daemons — they will not block process exit.
        """

        if self._tail_thread is not None and self._tail_thread.is_alive():
            return
        self._stop_event.clear()
        # §9.1: initial full refresh closes the gap between last
        # shutdown and tail start. Fires synchronously before the
        # threads come up so readers don't see an empty cache.
        self._initial_full_refresh()
        # Seed positions at EOF so the tail starts from "now".
        self._seek_to_end()
        self._tail_thread = threading.Thread(
            target=self._tail_loop,
            name="state-cache-tail",
            daemon=True,
        )
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="state-cache-refresh",
            daemon=True,
        )
        self._tail_thread.start()
        self._worker_thread.start()

    def stop(self) -> None:
        """Signal both threads to exit and join them.

        Best-effort; honours :data:`_JOIN_TIMEOUT_SECONDS`. Idempotent.
        """

        self._stop_event.set()
        for thread in (self._tail_thread, self._worker_thread):
            if thread is not None:
                thread.join(timeout=_JOIN_TIMEOUT_SECONDS)
        self._tail_thread = None
        self._worker_thread = None

    @property
    def running(self) -> bool:
        return (
            self._tail_thread is not None and self._tail_thread.is_alive()
        )

    # ── initial-refresh / seek-to-end ────────────────────────────

    def _initial_full_refresh(self) -> None:
        """Force a refresh for every currently-known project.

        §9.1: tail starts at EOF; the full refresh closes the
        startup-gap window. Provider-less refreshers (PR 1 tests)
        do nothing here, which is correct — there is no project set
        to refresh yet.

        PR #2085 round-2 boundary fix: workspace-wide alert snapshot
        is fetched ONCE before the per-project loop and threaded
        through every ``cache.refresh`` call. Without this, a
        workspace with N projects paid N × Supervisor construction
        per sweep — all under the cache lock.
        """

        keys = self._candidate_keys()
        refresh_kwargs = self._sweep_refresh_kwargs(len(keys))
        for key in keys:
            try:
                self._cache.refresh(key, **refresh_kwargs)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "state_cache: initial refresh failed for %s", key,
                )

    def _seek_to_end(self) -> None:
        """Seed file positions at current EOF for every log we will tail."""

        for path in self._discover_logs():
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            self._positions[path] = _Position(size)

    def _discover_logs(self) -> list[Path]:
        """Return every audit-log file under the central dir.

        Re-evaluated on every tail tick so newly-created project logs
        are picked up automatically.
        """

        if not self._audit_dir.exists():
            return []
        try:
            return sorted(self._audit_dir.glob("*.jsonl"))
        except OSError as exc:
            logger.warning(
                "state_cache: audit dir scan failed (%s): %s",
                self._audit_dir,
                exc,
            )
            return []

    def _candidate_keys(self) -> list[str]:
        if self._project_keys is None:
            return []
        try:
            return list(self._project_keys())
        except Exception:  # noqa: BLE001
            logger.exception("state_cache: project-key provider raised")
            return []

    # ── tail thread ──────────────────────────────────────────────

    def _tail_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tail_once()
            except Exception:  # noqa: BLE001 — never let the tail thread die
                logger.exception("state_cache: tail loop iteration failed")
            self._stop_event.wait(self._tail_poll_seconds)

    def _tail_once(self) -> None:
        for path in self._discover_logs():
            pos = self._positions.get(path)
            if pos is None:
                # New file appeared mid-session — start at EOF so we
                # don't replay historical events that pre-date the
                # cache's awareness.
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0
                self._positions[path] = _Position(size)
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size < pos.offset:
                # Truncation / rotation. Replay the new (smaller) file
                # from start so we don't skip events.
                pos.offset = 0
            if size == pos.offset:
                continue
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    fh.seek(pos.offset)
                    chunk = fh.read()
                    new_offset = fh.tell()
            except OSError as exc:
                logger.debug(
                    "state_cache: tail read failed (%s): %s", path, exc,
                )
                continue
            self._dispatch_chunk(chunk)
            pos.offset = new_offset

    def _dispatch_chunk(self, chunk: str) -> None:
        for line in chunk.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                # Truncated final line on a busy writer; the next tail
                # tick will re-read from the same offset and pick it up.
                continue
            if not isinstance(obj, dict):
                continue
            event = str(obj.get("event", ""))
            project = str(obj.get("project", ""))
            self._dispatch_event(event=event, project=project)

    def _dispatch_event(self, *, event: str, project: str) -> None:
        if event not in _INVALIDATING_EVENTS:
            return
        # #2051 (Codex review): every invalidating event MUST also
        # invalidate the ``__workspace__`` sentinel. Tracked-project
        # events can cross-post into the workspace-root inbox (e.g.
        # a task created against project="inbox" surfaces in the
        # workspace-root awaits-user sweep), and the cache-read
        # boundary now refuses to serve a list/count when the
        # sentinel is stale or missing. Refreshing it on every event
        # is cheap — the workspace-root sweep is a single bulk query
        # and coalesces with the rest of the drain.
        self._cache.invalidate(WORKSPACE_PROJECT_KEY)
        if not project or project in _WORKSPACE_ROOT_PROJECT_SIGNALS:
            # Workspace-scoped event (empty project) OR an event whose
            # ``project`` payload is itself a workspace-root signal
            # (``"inbox"`` — workspace-root inbox tasks emit this).
            # Invalidate every known project key so the snapshot stays
            # consistent across the per-project + sentinel union.
            self._cache.invalidate(None)
            return
        self._cache.invalidate(project)

    # ── refresh worker thread ────────────────────────────────────

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._worker_once()
            except Exception:  # noqa: BLE001
                logger.exception("state_cache: worker loop iteration failed")
            self._stop_event.wait(self._refresh_tick_seconds)

    def _worker_once(self) -> None:
        drained = self._cache.drain_pending()
        refresh_kwargs = self._sweep_refresh_kwargs(len(drained))
        for key in drained:
            try:
                self._cache.refresh(key, **refresh_kwargs)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "state_cache: refresh failed for %s", key,
                )

    # ── sweep-level pre-fetch ────────────────────────────────────

    def _sweep_refresh_kwargs(self, key_count: int) -> dict[str, object]:
        """Return kwargs to plumb through every refresh in one sweep.

        PR #2085 round-2 boundary fix: ``_open_alerts_for(config)``
        constructs a :class:`Supervisor` + reads ``open_alerts()`` —
        a workspace-wide read. When the sweep touches >1 project,
        doing this once and passing the result through reduces the
        per-sweep cost from N × Supervisor reads to 1.

        Single-project sweeps (drain has one key, single-project
        event-triggered refreshes) skip the pre-fetch — the
        downstream ``compute_entry_for_project`` will do its own
        on-demand read, identical cost to the pre-PR behaviour for
        that path.

        ``config_provider`` unset (PR 1 tests, refreshers without the
        real refresh function wired in) → no kwargs, same as before.
        """

        if key_count <= 1 or self._config_provider is None:
            return {}
        try:
            from pollypm.state_cache.refresh_impl import _open_alerts_for
        except Exception:  # noqa: BLE001
            return {}
        try:
            config = self._config_provider()
        except Exception:  # noqa: BLE001
            logger.exception(
                "state_cache: config_provider raised during sweep pre-fetch",
            )
            return {}
        try:
            alerts_snapshot = _open_alerts_for(config)
        except Exception:  # noqa: BLE001
            logger.exception(
                "state_cache: _open_alerts_for raised during sweep pre-fetch",
            )
            return {}
        return {"alerts_snapshot": alerts_snapshot}


# ── test hooks ──────────────────────────────────────────────────


def _drain_for_test(refresher: StateCacheRefresher) -> None:
    """Helper for tests — synchronously run one tail + worker cycle."""

    refresher._tail_once()  # noqa: SLF001
    refresher._worker_once()  # noqa: SLF001


def _await_quiescence(
    refresher: StateCacheRefresher, *, timeout: float = 1.0,
) -> None:
    """Block until the refresher's pending set drains, or ``timeout``."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not refresher._cache.pending_invalidations():  # noqa: SLF001
            return
        time.sleep(0.01)
