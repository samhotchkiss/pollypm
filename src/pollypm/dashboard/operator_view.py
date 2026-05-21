"""Operator dashboard data loader (#1572).

Bridges the leaf categorization module to the workspace's project
config + the canonical work-service. Keeps the I/O side-effects
out of :mod:`pollypm.dashboard.categorization` so the categorizer
stays trivially unit-testable against a mock work service.

Post-#1634 perf fix: open ONE work-service handle keyed on the
workspace config (which resolves to the canonical workspace DB on
sqlite and the pg pool on postgres) and prefetch every project's
tasks + active worker sessions in two bulk queries before categorizing.
The historic per-project fanout opened a fresh sqlite handle per
project even on a postgres backend (``_open_work_service`` did not
forward ``config`` to the factory, so the resolver fell back to
``"sqlite"``) and queried stale ``state.db`` files on every dashboard
mount — 12 projects × ~5–350ms sqlite opens dominated the cold path.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from pollypm.dashboard.categorization import (
    OperatorDashboardRow,
    OperatorDashboardView,
    ProjectState,
    build_operator_dashboard_view,
    categorize_project,
    glyph_for_project_state,
    what_working,
    why_waiting,
)
from pollypm.state_cache.divergence import (
    DivergenceCounter as _DivergenceCounter,
    compare_state_maps as _compare_state_maps,
    log_divergence as _log_divergence,
)

logger = logging.getLogger(__name__)


# #1634 #1945 perf — per-process TTL cache for ``_prefetch_project_state``.
#
# When the rail's 2s categorization TTL expires, the same rail
# ``build_items()`` tick runs both ``_project_state_rollups`` (which
# pulls bulk tasks via :func:`pollypm.cockpit_pg_aggregates.all_tasks_grouped`)
# and ``_project_categorizations`` → :func:`project_state_map_from_config`,
# which opens its own shared work-service and runs
# ``svc.list_tasks()`` + ``svc.list_worker_sessions(active_only=True)``
# against pg. Two broad task reads, milliseconds apart, before the
# rail can render.
#
# Mirrors the same wedge PRs #1907 + #1928 used for
# ``pm_inbox_awaits_user_list`` and ``Supervisor.status()``: a tiny
# module-level dict keyed on ``id(config)`` with a short TTL. The
# ``svc`` argument is intentionally excluded from the key because the
# resolved backend (pg pool or sqlite handle) is uniquely determined
# by ``config``; the work-service handle is just a thin shim and a
# fresh one each call would otherwise defeat the cache. The cockpit
# router invalidates its config cache on mtime change, so a reload
# yields a fresh ``id(config)`` and bypasses this cache automatically.
# Callers receive fresh dict + inner-list copies so downstream
# mutation cannot leak into the cached snapshot.
_PREFETCH_PROJECT_STATE_TTL_SECONDS = 1.0
_PREFETCH_PROJECT_STATE_CACHE: dict[
    int,
    tuple[
        float,
        tuple[dict[str, tuple], dict[str, tuple]],
    ],
] = {}


@dataclass(frozen=True, slots=True)
class _ProjectScan:
    """Resolved scan target — project key + tracked flag.

    Pre-#1634 this also carried per-project ``db_paths`` for the
    sqlite fanout; post-fix the loader opens ONE shared work-service
    via the factory's config-resolved path, so the per-project DB
    list is no longer needed.
    """

    project_key: str
    project_path: Path
    tracked: bool


def _collect_project_scans(config) -> list[_ProjectScan]:  # noqa: ANN001
    """Resolve (project_key, project_path, tracked) tuples for every project."""
    scans: list[_ProjectScan] = []
    seen_projects: set[str] = set()
    projects = getattr(config, "projects", {}) or {}
    for project_key, project in projects.items():
        if project_key in seen_projects:
            continue
        seen_projects.add(project_key)
        project_path = Path(getattr(project, "path", "."))
        scans.append(
            _ProjectScan(
                project_key=str(project_key),
                project_path=project_path,
                tracked=bool(getattr(project, "tracked", False)),
            )
        )
    return scans


def _waiting_items_by_project(config) -> dict[str, list]:  # noqa: ANN001
    """Group ``pm_inbox_awaits_user_list`` items by project key."""
    from pollypm.cockpit_inbox import pm_inbox_awaits_user_list

    grouped: dict[str, list] = {}
    try:
        items = pm_inbox_awaits_user_list(config)
    except Exception:  # noqa: BLE001
        # #1355: previously silent. A failure here means the dashboard
        # quietly shows zero waiting items for the whole workspace —
        # log so a broken inbox query is debuggable.
        logger.warning(
            "operator_view: pm_inbox_awaits_user_list failed; dashboard will show no waiting items",
            exc_info=True,
        )
        items = []
    for item in items:
        key = (
            str(getattr(item, "project", "") or "").strip()
            or str(getattr(item, "scope", "") or "").strip()
        )
        if not key or key == "inbox":
            continue
        grouped.setdefault(key, []).append(item)
    return grouped


def _open_shared_work_service(config):  # noqa: ANN001, ANN202
    """Open one work-service handle keyed on ``config``.

    The factory routes to the configured backend (pg pool on
    ``storage.backend = "postgres"``, the canonical workspace
    ``state.db`` on sqlite). Returns ``None`` on import / open
    failure so the caller can degrade rather than crash the
    dashboard.

    #1634 — replaces ``_open_work_service`` which opened a fresh
    sqlite handle per project (the historic dual-DB fallback walk)
    even on a pg backend, since the legacy callsite never forwarded
    ``config`` to the factory.
    """
    try:
        from pollypm.work import create_work_service
    except Exception:  # noqa: BLE001
        logger.warning(
            "operator_view: create_work_service import failed",
            exc_info=True,
        )
        return None
    try:
        return create_work_service(config=config)
    except Exception:  # noqa: BLE001
        logger.warning(
            "operator_view: create_work_service(config=...) failed; "
            "dashboard will degrade to inbox-only categorization",
            exc_info=True,
        )
        return None


def _safe_close(svc) -> None:  # noqa: ANN001
    close = getattr(svc, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001
            pass


def _prefetch_project_state(config, svc) -> tuple[  # noqa: ANN001
    "dict[str, list]", "dict[str, list]"
]:
    """Return ``(tasks_by_alias, workers_by_alias)`` in two bulk queries.

    Single ``list_tasks(project=None)`` + ``list_worker_sessions(project=None,
    active_only=True)`` against the shared work-service, grouped in
    Python by the project alias each row carries. The categorizer then
    reads its per-project slice from these dicts via
    :func:`pollypm.work.project_aliases.project_storage_aliases` —
    replacing N ``list_tasks(project=<key>)`` calls and N
    ``list_worker_sessions(project=<key>)`` calls with two queries.

    #1634 #1945 perf — results are memoised per ``id(config)`` with a
    short TTL (:data:`_PREFETCH_PROJECT_STATE_TTL_SECONDS`) so the
    rail glyph categorization shares its bulk task/session reads
    across the multiple rail-build surfaces that hit
    :func:`project_state_map_from_config` on overlapping ticks.
    Callers receive fresh dict + inner-list copies so mutation is safe.
    """
    cache_key = id(config)
    now = time.monotonic()
    cached = _PREFETCH_PROJECT_STATE_CACHE.get(cache_key)
    if cached is not None and now - cached[0] < _PREFETCH_PROJECT_STATE_TTL_SECONDS:
        tasks_snap, workers_snap = cached[1]
        return (
            {key: list(rows) for key, rows in tasks_snap.items()},
            {key: list(rows) for key, rows in workers_snap.items()},
        )

    result = _prefetch_project_state_uncached(svc)
    # #1957 perf — stamp the cache AFTER the uncached prefetch returns so
    # a cold task+session sweep that exceeds the TTL doesn't write a
    # born-expired entry that forces the next rail tick to recompute.
    completed_at = time.monotonic()
    # Best-effort eviction so the cache doesn't grow across long-lived
    # processes with config reloads (each reload yields a fresh
    # ``id(config)``).
    if len(_PREFETCH_PROJECT_STATE_CACHE) > 8:
        for stale_key in [
            k for k, (ts, _v) in _PREFETCH_PROJECT_STATE_CACHE.items()
            if completed_at - ts >= _PREFETCH_PROJECT_STATE_TTL_SECONDS
        ]:
            _PREFETCH_PROJECT_STATE_CACHE.pop(stale_key, None)
    tasks_by_alias, workers_by_alias = result
    _PREFETCH_PROJECT_STATE_CACHE[cache_key] = (
        completed_at,
        (
            {key: tuple(rows) for key, rows in tasks_by_alias.items()},
            {key: tuple(rows) for key, rows in workers_by_alias.items()},
        ),
    )
    return result


def _prefetch_project_state_uncached(svc) -> tuple[  # noqa: ANN001
    "dict[str, list]", "dict[str, list]"
]:
    """Underlying implementation of :func:`_prefetch_project_state`.

    Separated from the public entry point so the cache wrapper stays
    trivially readable. Tests that need to bypass the cache can call
    this directly (or clear :data:`_PREFETCH_PROJECT_STATE_CACHE`).
    """
    tasks_by_alias: dict[str, list] = {}
    workers_by_alias: dict[str, list] = {}
    if svc is None:
        return tasks_by_alias, workers_by_alias
    try:
        all_tasks = svc.list_tasks()
    except Exception:  # noqa: BLE001
        logger.warning(
            "operator_view: bulk list_tasks failed; categorization "
            "degrades to inbox-only",
            exc_info=True,
        )
        all_tasks = []
    for task in all_tasks or []:
        key = str(getattr(task, "project", "") or "")
        if not key:
            continue
        tasks_by_alias.setdefault(key, []).append(task)
    try:
        all_workers = svc.list_worker_sessions(active_only=True)
    except Exception:  # noqa: BLE001
        logger.warning(
            "operator_view: bulk list_worker_sessions failed; "
            "live-worker WORKING signal will degrade",
            exc_info=True,
        )
        all_workers = []
    for session in all_workers or []:
        key = str(getattr(session, "task_project", "") or "")
        if not key:
            continue
        workers_by_alias.setdefault(key, []).append(session)
    return tasks_by_alias, workers_by_alias


def _aliases_for(config, project_key: str) -> list[str]:  # noqa: ANN001
    """Resolve every storage alias for ``project_key`` (defensive)."""
    try:
        from pollypm.work.project_aliases import project_storage_aliases

        return project_storage_aliases(config, project_key)
    except Exception:  # noqa: BLE001
        return [project_key]


class _ProjectSliceService:
    """Read-only adapter exposing one project's pre-fetched rows.

    Implements the :class:`_WorkServiceLike` Protocol the categorizer
    consumes (``list_tasks`` / ``list_worker_sessions`` / ``get``)
    without touching the underlying DB. The shared svc has already
    paid the round-trip; this adapter just hands the categorizer the
    per-project slice keyed on every known alias for the project.
    """

    __slots__ = ("_project_key", "_aliases", "_tasks", "_workers", "_svc")

    def __init__(
        self,
        project_key: str,
        aliases: list[str],
        tasks_by_alias: dict[str, list],
        workers_by_alias: dict[str, list],
        shared_svc,
    ) -> None:
        self._project_key = project_key
        self._aliases = aliases
        self._tasks = tasks_by_alias
        self._workers = workers_by_alias
        self._svc = shared_svc

    def list_tasks(
        self, *, project: str | None = None, **_kwargs: object,
    ) -> list:
        out: list = []
        seen: set[str] = set()
        # ``project=None`` returns everything we have for this slice;
        # the categorizer always passes the project key explicitly,
        # so this branch is just a defensive default.
        target_aliases = (
            self._aliases if project is None else [project, *self._aliases]
        )
        for alias in target_aliases:
            for task in self._tasks.get(alias, []):
                tid = getattr(task, "task_id", None)
                if tid and tid in seen:
                    continue
                if tid:
                    seen.add(tid)
                out.append(task)
        return out

    def list_worker_sessions(
        self, *, project: str | None = None, active_only: bool = True,
    ) -> list:
        del project  # adapter is already project-scoped
        del active_only  # prefetch already filters to active_only=True
        out: list = []
        seen: set[tuple[str, object]] = set()
        for alias in self._aliases:
            for session in self._workers.get(alias, []):
                key = (alias, getattr(session, "task_number", None))
                if key in seen:
                    continue
                seen.add(key)
                out.append(session)
        return out

    def get(self, task_id: str):  # noqa: ANN201
        # Categorizer's ``what_working`` reaches for a task title via
        # ``svc.get`` after picking a worker; route the single-row
        # fetch through the shared svc so we don't have to prefetch
        # every task body. Falls back to a scan over the prefetched
        # slice when no shared svc is available.
        if self._svc is not None and hasattr(self._svc, "get"):
            return self._svc.get(task_id)
        for alias in self._aliases:
            for task in self._tasks.get(alias, []):
                if getattr(task, "task_id", "") == task_id:
                    return task
        raise KeyError(task_id)


def load_operator_view(config_path: Path) -> OperatorDashboardView:
    """Read the workspace config + every project DB into the view model.

    Mirrors the rail-badge data path so the dashboard and rail see
    the same projects and the same inbox-waits-on-user list. Each
    project gets its own work-service handle, opened against the
    canonical workspace DB first and the legacy per-project DB
    second (matching the rail rollup's iteration order).
    """
    from pollypm.config import load_config

    config = load_config(config_path)
    return load_operator_view_from_config(config)


def _scan_to_row(
    scan: _ProjectScan,
    items: list,
    *,
    slice_svc: "_ProjectSliceService | None",
) -> OperatorDashboardRow:
    """Categorize one project using a pre-fetched slice adapter.

    Pre-#1634 every call opened its own sqlite handle; now the per-
    project slice is supplied by the caller (one shared work-service
    feeds every scan) so the categorizer pays only Python work.
    """
    if slice_svc is None:
        state = (
            ProjectState.WAITING if items
            else (ProjectState.PAUSED if not scan.tracked else ProjectState.IDLE)
        )
        detail = (
            why_waiting(items) if state is ProjectState.WAITING
            else ("Paused" if state is ProjectState.PAUSED else "Quiet")
        )
        return OperatorDashboardRow(
            project_key=scan.project_key,
            state=state,
            glyph=glyph_for_project_state(state),
            detail=detail,
        )
    state = categorize_project(
        scan.project_key,
        work_service=slice_svc,
        inbox_items=items,
        tracked=scan.tracked,
    )
    glyph = glyph_for_project_state(state)
    if state is ProjectState.WAITING:
        detail = why_waiting(items)
    elif state is ProjectState.WORKING:
        detail = what_working(scan.project_key, work_service=slice_svc)
    elif state is ProjectState.PAUSED:
        detail = "Paused"
    else:
        detail = "Quiet"
    return OperatorDashboardRow(
        project_key=scan.project_key,
        state=state,
        glyph=glyph,
        detail=detail,
    )


def load_operator_view_from_config(config) -> OperatorDashboardView:  # noqa: ANN001
    """Like :func:`load_operator_view` but starting from a loaded config.

    Useful for tests + callers that have a config in hand and want to
    avoid re-parsing the TOML.

    Post-#1634 data path:

    1. Open one shared work-service via the config-resolved factory.
       On pg this is the pool singleton; on sqlite it's the canonical
       workspace ``state.db``. No per-project handles.
    2. Prefetch every task + active worker session in two bulk
       queries, grouped by project alias.
    3. Per-project: gather the inbox slice (already a single pg
       query inside ``_waiting_items_by_project``) and categorize
       against an adapter view of the prefetched dicts. Serial loop —
       no DB I/O remains in the per-project step, so parallel threads
       buy nothing.

    Move A PR 3 (#1664): when ``POLLYPM_STATE_CACHE=1`` AND the cache
    has every tracked project populated, this iterates
    ``cache.snapshot()`` and builds rows from the pre-computed
    ``state`` / ``glyph`` / ``detail`` fields, skipping the bulk
    work-service open + prefetch entirely. Falls through to the
    direct path on cold / partial cache.
    """
    cached_view = _maybe_cache_route_operator_view(config)
    if cached_view is not None:
        return cached_view

    scans = _collect_project_scans(config)
    waiting_by_project = _waiting_items_by_project(config)

    shared_svc = _open_shared_work_service(config)
    try:
        tasks_by_alias, workers_by_alias = _prefetch_project_state(
            config, shared_svc,
        )
        rows: list[OperatorDashboardRow] = []
        for scan in scans:
            if shared_svc is None:
                slice_svc = None
            else:
                slice_svc = _ProjectSliceService(
                    project_key=scan.project_key,
                    aliases=_aliases_for(config, scan.project_key),
                    tasks_by_alias=tasks_by_alias,
                    workers_by_alias=workers_by_alias,
                    shared_svc=shared_svc,
                )
            rows.append(
                _scan_to_row(
                    scan,
                    waiting_by_project.get(scan.project_key, []),
                    slice_svc=slice_svc,
                )
            )
    finally:
        if shared_svc is not None:
            _safe_close(shared_svc)

    waiting: list[OperatorDashboardRow] = []
    working: list[OperatorDashboardRow] = []
    idle: list[OperatorDashboardRow] = []
    paused: list[OperatorDashboardRow] = []
    for row in rows:
        if row.state is ProjectState.WAITING:
            waiting.append(row)
        elif row.state is ProjectState.WORKING:
            working.append(row)
        elif row.state is ProjectState.PAUSED:
            paused.append(row)
        else:
            idle.append(row)

    waiting.sort(key=lambda r: r.project_key.lower())
    working.sort(key=lambda r: r.project_key.lower())
    idle.sort(key=lambda r: r.project_key.lower())
    paused.sort(key=lambda r: r.project_key.lower())
    return OperatorDashboardView(
        waiting=tuple(waiting),
        working=tuple(working),
        idle=tuple(idle),
        paused=tuple(paused),
    )


def _scan_to_state(
    scan: _ProjectScan,
    items: list,
    *,
    slice_svc: "_ProjectSliceService | None",
) -> tuple[str, ProjectState]:
    """Return ``(project_key, ProjectState)`` for one scan.

    Mirrors :func:`_scan_to_row` but skips ``what_working`` /
    ``why_waiting`` — the rail just needs the category for its glyph
    table. Reads from the pre-fetched slice adapter, no DB I/O.
    """
    if slice_svc is None:
        if items:
            return scan.project_key, ProjectState.WAITING
        if not scan.tracked:
            return scan.project_key, ProjectState.PAUSED
        return scan.project_key, ProjectState.IDLE
    return scan.project_key, categorize_project(
        scan.project_key,
        work_service=slice_svc,
        inbox_items=items,
        tracked=scan.tracked,
    )


# Move A PR 2 — divergence sampler for the cache-routed fast path
# (``docs/design/move-a-state-cache.md`` §6.2 last bullet). Per-call-site
# counter so a busy site doesn't borrow samples from a quiet one.
_STATE_MAP_DIVERGENCE_COUNTER = _DivergenceCounter()


def _maybe_cache_route_state_map(config) -> dict[str, ProjectState] | None:
    """Return the cache-routed state map, or ``None`` to fall through.

    Returns ``None`` when the env flag is off, when the cache is cold
    (no entries yet — letting the direct path warm it via the
    refresher), when the cache lacks a state for any tracked project
    (the direct path is needed to fill the gap), or on any
    unexpected exception. The 1-in-N divergence sampler then runs
    both paths and logs a WARN on mismatch.
    """

    try:
        from pollypm.state_cache import get_cache, is_enabled
    except Exception:  # noqa: BLE001
        return None
    if not is_enabled():
        return None
    try:
        cache = get_cache()
        snapshot = cache.snapshot()
    except Exception:  # noqa: BLE001
        return None
    if not snapshot:
        return None

    projects = getattr(config, "projects", {}) or {}
    known_keys = set(projects.keys())
    if not known_keys:
        return {}
    # The cache is authoritative ONLY when it knows every tracked
    # project. A missing key forces the direct path so the rail
    # never paints a stale-missing project as PAUSED.
    if not known_keys.issubset(set(snapshot.keys())):
        return None

    cached_map: dict[str, ProjectState] = {}
    for key in known_keys:
        entry = snapshot.get(key)
        state = getattr(entry, "state", None) if entry is not None else None
        if state is None:
            # Refresher hasn't populated this entry yet (stub entry).
            # Fall through to the direct path.
            return None
        cached_map[key] = state

    if _STATE_MAP_DIVERGENCE_COUNTER.should_sample():
        try:
            direct = _direct_project_state_map_from_config(config)
        except Exception:  # noqa: BLE001
            direct = None
        if direct is not None:
            matched, reason = _compare_state_maps(cached_map, direct)
            if not matched:
                _log_divergence("project_state_map_from_config", reason)

    return cached_map


def _maybe_cache_route_operator_view(config) -> "OperatorDashboardView | None":  # noqa: ANN001
    """Return the cache-routed dashboard view, or ``None`` to fall through.

    Move A PR 3 (#1664): with the env flag on AND the cache populated
    for every tracked project, the dashboard view is built from
    pre-computed ``state`` / ``glyph`` / ``detail`` fields on each
    cache entry — no bulk work-service open, no per-project query
    fanout. The fall-through path covers cold start and any project
    the refresher hasn't filled yet.
    """

    try:
        from pollypm.state_cache import get_cache, is_enabled
    except Exception:  # noqa: BLE001
        return None
    if not is_enabled():
        return None
    try:
        cache = get_cache()
        snapshot = cache.snapshot()
    except Exception:  # noqa: BLE001
        return None
    if not snapshot:
        return None

    scans = _collect_project_scans(config)
    if not scans:
        return OperatorDashboardView(
            waiting=(), working=(), idle=(), paused=(),
        )
    # Authoritative only when every tracked project has a cache entry.
    snapshot_keys = set(snapshot.keys())
    if not all(scan.project_key in snapshot_keys for scan in scans):
        return None

    waiting: list[OperatorDashboardRow] = []
    working: list[OperatorDashboardRow] = []
    idle: list[OperatorDashboardRow] = []
    paused: list[OperatorDashboardRow] = []
    for scan in scans:
        entry = snapshot[scan.project_key]
        state = getattr(entry, "state", None)
        if state is None:
            # Refresher hasn't filled in state yet — defer.
            return None
        glyph = getattr(entry, "glyph", "") or glyph_for_project_state(state)
        detail = getattr(entry, "detail", "") or "Quiet"
        row = OperatorDashboardRow(
            project_key=scan.project_key,
            state=state,
            glyph=glyph,
            detail=detail,
        )
        if state is ProjectState.WAITING:
            waiting.append(row)
        elif state is ProjectState.WORKING:
            working.append(row)
        elif state is ProjectState.PAUSED:
            paused.append(row)
        else:
            idle.append(row)

    waiting.sort(key=lambda r: r.project_key.lower())
    working.sort(key=lambda r: r.project_key.lower())
    idle.sort(key=lambda r: r.project_key.lower())
    paused.sort(key=lambda r: r.project_key.lower())
    return OperatorDashboardView(
        waiting=tuple(waiting),
        working=tuple(working),
        idle=tuple(idle),
        paused=tuple(paused),
    )


def _direct_project_state_map_from_config(
    config,  # noqa: ANN001
) -> dict[str, ProjectState]:
    """Run the direct (non-cache-routed) state-map computation.

    Extracted so the divergence sampler can compare cache vs direct
    without re-entering ``project_state_map_from_config`` (which
    would short-circuit back through the cache fast path on hit).
    """

    scans = _collect_project_scans(config)
    waiting_by_project = _waiting_items_by_project(config)
    if not scans:
        return {}
    shared_svc = _open_shared_work_service(config)
    try:
        tasks_by_alias, workers_by_alias = _prefetch_project_state(
            config, shared_svc,
        )
        pairs: list[tuple[str, ProjectState]] = []
        for scan in scans:
            if shared_svc is None:
                slice_svc = None
            else:
                slice_svc = _ProjectSliceService(
                    project_key=scan.project_key,
                    aliases=_aliases_for(config, scan.project_key),
                    tasks_by_alias=tasks_by_alias,
                    workers_by_alias=workers_by_alias,
                    shared_svc=shared_svc,
                )
            pairs.append(
                _scan_to_state(
                    scan,
                    waiting_by_project.get(scan.project_key, []),
                    slice_svc=slice_svc,
                )
            )
        return {key: state for key, state in pairs}
    finally:
        if shared_svc is not None:
            _safe_close(shared_svc)


def project_state_map_from_config(config) -> dict[str, ProjectState]:  # noqa: ANN001
    """Return ``{project_key: ProjectState}`` for every project in ``config``.

    The rail uses this to pick its glyph from the same source the
    dashboard uses — guaranteeing the section a project appears in
    matches the glyph drawn next to it. The function is best-effort:
    a project whose DB can't be opened falls through to a tracked /
    paused IDLE classification rather than raising.

    #1634 — opens ONE shared work-service and prefetches every
    project's tasks + active worker sessions in two bulk queries
    before classifying. Replaces the parallel per-project sqlite
    fanout that ran one ``state.db`` open per project per refresh
    (~5–350ms each on a 9MB workspace DB, dominating the dashboard
    mount path even though the system is on postgres).

    Move A PR 2 (#1664): with ``POLLYPM_STATE_CACHE=1`` AND the cache
    populated, this collapses to ``{key: entry.state for ... in
    snapshot.items()}`` and skips the bulk work-service open. Flag is
    OFF by default; PR 4 flips it after divergence-sampler telemetry
    is green.
    """
    cached_map = _maybe_cache_route_state_map(config)
    if cached_map is not None:
        return cached_map

    scans = _collect_project_scans(config)
    waiting_by_project = _waiting_items_by_project(config)
    if not scans:
        return {}
    shared_svc = _open_shared_work_service(config)
    try:
        tasks_by_alias, workers_by_alias = _prefetch_project_state(
            config, shared_svc,
        )
        pairs: list[tuple[str, ProjectState]] = []
        for scan in scans:
            if shared_svc is None:
                slice_svc = None
            else:
                slice_svc = _ProjectSliceService(
                    project_key=scan.project_key,
                    aliases=_aliases_for(config, scan.project_key),
                    tasks_by_alias=tasks_by_alias,
                    workers_by_alias=workers_by_alias,
                    shared_svc=shared_svc,
                )
            pairs.append(
                _scan_to_state(
                    scan,
                    waiting_by_project.get(scan.project_key, []),
                    slice_svc=slice_svc,
                )
            )
        return {key: state for key, state in pairs}
    finally:
        if shared_svc is not None:
            _safe_close(shared_svc)


def view_as_ascii(view: OperatorDashboardView) -> str:
    """Render an operator dashboard view as ASCII text (for tests + PR body)."""
    lines: list[str] = []

    def _section(title: str, rows: Iterable[OperatorDashboardRow], empty: str) -> None:
        lines.append(title)
        rendered = list(rows)
        if not rendered:
            lines.append(f"  {empty}")
            lines.append("")
            return
        for row in rendered:
            glyph = row.glyph or glyph_for_project_state(row.state)
            lines.append(f"  {glyph} {row.project_key}  {row.detail}")
        lines.append("")

    _section("Waiting on you", view.waiting, "Nothing waiting.")
    _section("Working", view.working, "Nothing actively working.")
    _section("Idle", view.idle, "All projects busy.")
    if view.paused:
        _section("Paused", view.paused, "")
    return "\n".join(lines).rstrip() + "\n"
