"""Real per-project refresh — populates a :class:`ProjectStateCacheEntry`.

Per ``docs/design/move-a-state-cache.md`` §6.2 / §10, PR 2 replaces
the PR 1 ``stub_refresh_fn`` with this real implementation. It
mirrors the data path the (uncached) call sites take so cache reads
match direct reads:

* ``_pm_inbox_awaits_user_list_uncached(config)`` — for the items
  the routed ``pm_inbox_awaits_user_list`` returns. Per project we
  filter the workspace-wide result to the items whose project key
  matches.
* ``categorize_project`` — for the ``state`` field
  ``project_state_map_from_config`` returns.
* ``rollup_project_state`` — for the rail-rollup fields the PR 3
  call sites will consume. Populated now so PR 3 is a pure wiring
  change.

**Critical recursion guard.** This module calls
``_pm_inbox_awaits_user_list_uncached`` — NOT
``pm_inbox_awaits_user_list``. The public helper is the cache-routed
one (PR 2 wired it that way); routing the refresher through it would
mean the refresher's read goes back through the cache fast-path and
returns ``cache.snapshot()`` (which is whatever it was on the
previous refresh), forming a feedback loop where the cache snapshots
its own snapshot. The ``_uncached`` suffix on the helper is the
explicit "direct DB path; do not change me without reading the
state_cache refresher" contract.

The refresher is best-effort: every per-project compute path catches
exceptions and degrades to an :func:`empty_entry`. A broken project
DB returns an entry that says "no data" — never propagates a crash.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from pollypm.state_cache.entry import ProjectStateCacheEntry, empty_entry

logger = logging.getLogger(__name__)

__all__ = [
    "ConfigProvider",
    "build_refresh_fn",
    "compute_entry_for_project",
]


# Callable returning the current workspace config. Injected so a
# config reload (cockpit mtime change) picks the new config up on
# the next refresh without having to plumb explicit invalidation
# through the refresher.
ConfigProvider = Callable[[], Any]


def build_refresh_fn(
    config_provider: ConfigProvider,
) -> Callable[[str], ProjectStateCacheEntry | None]:
    """Return a :data:`RefreshFn` bound to ``config_provider``.

    The returned closure is the callable :class:`ProjectStateCache`
    invokes per project. It loads the config fresh on every call so
    a config reload during a long-running cockpit picks up the new
    paths automatically.
    """

    def _refresh(project_key: str) -> ProjectStateCacheEntry | None:
        try:
            config = config_provider()
        except Exception:  # noqa: BLE001
            logger.warning(
                "state_cache: config_provider raised; "
                "returning empty entry for %s",
                project_key,
                exc_info=True,
            )
            return empty_entry(project_key)
        return compute_entry_for_project(project_key, config)

    return _refresh


def compute_entry_for_project(
    project_key: str, config: Any,
) -> ProjectStateCacheEntry:
    """Recompute the entry for ``project_key`` against ``config``.

    Public so tests can drive it directly without spinning up a real
    cache + refresher. The shape mirrors the four call-site reads PR
    2 + PR 3 wire up:

    1. ``awaits_user_items`` — filtered slice of the workspace-wide
       awaits-user list (used by the routed
       :func:`pm_inbox_awaits_user_list`).
    2. ``state`` — categorization output (used by the routed
       :func:`project_state_map_from_config`).
    3. ``rail_*`` — rollup output (PR 3 consumer).
    4. ``project_path`` / ``tracked`` — straight from config.
    """

    project = _get_project(config, project_key)
    project_path = _project_path(project)
    tracked = bool(getattr(project, "tracked", True)) if project else False

    awaits_user_items = _awaits_user_items_for(project_key, config)
    state, glyph, detail, rail_rollup, live_workers = _categorize_and_rollup(
        project_key=project_key,
        config=config,
        tracked=tracked,
        awaits_user_items=list(awaits_user_items),
    )

    # PR 3: populate live worker sessions + latest_heartbeat_by_session
    # so cockpit_rail can short-circuit its per-project ``latest_heartbeat()``
    # calls. Best-effort: failures degrade silently to an empty mapping
    # (the rail's fall-through path re-fetches direct).
    latest_heartbeat_by_session = _heartbeats_for_sessions(live_workers)

    entry = ProjectStateCacheEntry(
        project_key=project_key,
        project_path=project_path,
        tracked=tracked,
        state=state,
        glyph=glyph,
        detail=detail,
        rail_state=rail_rollup[0] if rail_rollup else None,
        rail_badge=rail_rollup[1] if rail_rollup else None,
        rail_sort_rank=rail_rollup[2] if rail_rollup else 0,
        rail_reason=rail_rollup[3] if rail_rollup else "",
        approvals_pending=rail_rollup[4] if rail_rollup else 0,
        awaits_user_count=len(awaits_user_items),
        awaits_user_items=tuple(awaits_user_items),
        live_worker_sessions=tuple(live_workers),
        latest_heartbeat_by_session=latest_heartbeat_by_session,
        computed_at=time.monotonic(),
    )
    return entry


# ── helpers ────────────────────────────────────────────────────────


def _get_project(config: Any, project_key: str) -> Any | None:
    try:
        projects = getattr(config, "projects", {}) or {}
        return projects.get(project_key)
    except Exception:  # noqa: BLE001
        return None


def _project_path(project: Any) -> Path:
    if project is None:
        return Path("")
    try:
        return Path(getattr(project, "path", "") or "")
    except Exception:  # noqa: BLE001
        return Path("")


def _awaits_user_items_for(project_key: str, config: Any) -> list[Any]:
    """Workspace-wide awaits-user sweep, filtered to ``project_key``.

    Calls the DIRECT (uncached) helper. Routing through the cached
    public ``pm_inbox_awaits_user_list`` here would mean the
    refresher reads its own snapshot — see this module's docstring
    for the recursion guard rationale.
    """

    try:
        # Local import — the cockpit_inbox module pulls a lot of
        # cockpit-side dependencies; keeping it lazy keeps the leaf
        # state_cache package light to import.
        from pollypm.cockpit_inbox import _pm_inbox_awaits_user_list_uncached
    except Exception:  # noqa: BLE001
        logger.warning(
            "state_cache: cockpit_inbox import failed during refresh",
            exc_info=True,
        )
        return []
    try:
        items = _pm_inbox_awaits_user_list_uncached(config)
    except Exception:  # noqa: BLE001
        logger.warning(
            "state_cache: _pm_inbox_awaits_user_list_uncached raised for %s",
            project_key,
            exc_info=True,
        )
        return []

    out: list[Any] = []
    for item in items:
        # Match the operator-view group-by: prefer ``project`` and
        # fall back to ``scope`` so messages keyed on scope land in
        # the right per-project entry.
        key = (
            str(getattr(item, "project", "") or "").strip()
            or str(getattr(item, "scope", "") or "").strip()
        )
        if key == project_key:
            out.append(item)
    return out


def _categorize_and_rollup(
    *,
    project_key: str,
    config: Any,
    tracked: bool,
    awaits_user_items: list[Any],
) -> tuple[
    Any, str, str,
    tuple[Any, Any, int, str, int] | None,
    list[Any],
]:
    """Run ``categorize_project`` + ``rollup_project_state`` for one project.

    Returns ``(state, glyph, detail, rollup_tuple_or_None, live_workers)``.
    The rollup tuple is ``(rail_state, rail_badge, sort_rank, reason,
    approvals_pending)`` — kept positional so the caller can ``zip``
    it into the entry fields without a second import of the rollup
    types here. ``live_workers`` is the list of active worker session
    records for the project, used by PR 3 to populate
    ``latest_heartbeat_by_session``.

    Failures degrade silently — a broken work-service drops the
    project to IDLE (or PAUSED when not tracked) with no rollup.
    """

    try:
        from pollypm.dashboard.categorization import (
            ProjectState,
            categorize_project,
            glyph_for_project_state,
            what_working,
            why_waiting,
        )
        from pollypm.dashboard.operator_view import (
            _aliases_for,
            _open_shared_work_service,
            _prefetch_project_state,
            _ProjectSliceService,
            _safe_close,
        )
        from pollypm.cockpit_project_state import rollup_project_state
    except Exception:  # noqa: BLE001
        logger.warning(
            "state_cache: dashboard imports failed during refresh for %s",
            project_key,
            exc_info=True,
        )
        return None, "", "", None, []

    shared_svc = _open_shared_work_service(config)
    try:
        if shared_svc is None:
            state = (
                ProjectState.WAITING if awaits_user_items
                else (ProjectState.PAUSED if not tracked else ProjectState.IDLE)
            )
            glyph = glyph_for_project_state(state)
            if state is ProjectState.WAITING:
                detail = why_waiting(awaits_user_items)
            elif state is ProjectState.PAUSED:
                detail = "Paused"
            else:
                detail = "Quiet"
            return state, glyph, detail, None, []

        tasks_by_alias, workers_by_alias = _prefetch_project_state(
            config, shared_svc,
        )
        slice_svc = _ProjectSliceService(
            project_key=project_key,
            aliases=_aliases_for(config, project_key),
            tasks_by_alias=tasks_by_alias,
            workers_by_alias=workers_by_alias,
            shared_svc=shared_svc,
        )
        state = categorize_project(
            project_key,
            work_service=slice_svc,
            inbox_items=awaits_user_items,
            tracked=tracked,
        )
        glyph = glyph_for_project_state(state)
        if state is ProjectState.WAITING:
            detail = why_waiting(awaits_user_items)
        elif state is ProjectState.WORKING:
            try:
                detail = what_working(
                    project_key, work_service=slice_svc,
                )
            except Exception:  # noqa: BLE001
                detail = "Active"
        elif state is ProjectState.PAUSED:
            detail = "Paused"
        else:
            detail = "Quiet"

        # PR 3 will consume these fields; populate them now so PR 3
        # is pure wiring. ``rollup_project_state`` reads only tasks
        # — the ``_ProjectSliceService`` slice already filters them
        # to this project's aliases.
        try:
            project_tasks = slice_svc.list_tasks(project=project_key)
        except Exception:  # noqa: BLE001
            project_tasks = []
        try:
            rollup = rollup_project_state(project_key, project_tasks)
            rollup_tuple = (
                rollup.state,
                rollup.badge,
                rollup.sort_rank,
                rollup.reason,
                rollup.approvals_pending,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "state_cache: rollup_project_state failed for %s",
                project_key,
                exc_info=True,
            )
            rollup_tuple = None

        # PR 3: gather live workers so the per-project heartbeat
        # lookup can short-circuit in cockpit_rail. ``slice_svc``
        # already filters by this project's aliases.
        try:
            live_workers = list(slice_svc.list_worker_sessions(
                project=project_key, active_only=True,
            ))
        except Exception:  # noqa: BLE001
            live_workers = []

        return state, glyph, detail, rollup_tuple, live_workers
    finally:
        if shared_svc is not None:
            _safe_close(shared_svc)


def _heartbeats_for_sessions(
    live_workers: list[Any],
) -> dict[str, Any]:
    """Return ``{session_name: HeartbeatRecord}`` for ``live_workers``.

    PR 3 (§5 row 9): the rail's ``latest_heartbeat()`` is called once
    per project per refresh. Folding it into the cache refresh means
    the rail can short-circuit on the cache snapshot instead of
    issuing N pg queries. Best-effort — a failed lookup degrades to
    a missing key (the rail's fall-through then re-queries direct).

    The mapping is keyed by ``session.name`` (matching the rail's
    ``_session_name_for_item`` resolution path) — NOT by task identity.
    """

    if not live_workers:
        return {}
    try:
        from pollypm.supervisor import Supervisor
        from pollypm.config import DEFAULT_CONFIG_PATH, load_config
    except Exception:  # noqa: BLE001
        logger.debug(
            "state_cache: supervisor import failed; "
            "skipping heartbeat prefetch",
            exc_info=True,
        )
        return {}

    try:
        # Re-load the config; the cache refresher is the only consumer
        # here, so the extra load is bounded to one per refresh-pass.
        config = load_config(DEFAULT_CONFIG_PATH)
        supervisor = Supervisor(config)
    except Exception:  # noqa: BLE001
        logger.debug(
            "state_cache: Supervisor init failed during heartbeat prefetch",
            exc_info=True,
        )
        return {}

    out: dict[str, Any] = {}
    store = getattr(supervisor, "store", None)
    if store is None:
        return {}
    for session in live_workers:
        # Look up the session_name from the worker record. The session
        # naming convention is ``worker_<project>/<task_number>``; the
        # supervisor.store.latest_heartbeat call sites in cockpit_rail
        # take the resolved session name from launches, which match
        # the worker session bindings.
        session_name = _session_name_for_worker(session)
        if not session_name:
            continue
        try:
            heartbeat = store.latest_heartbeat(session_name)
        except Exception:  # noqa: BLE001
            heartbeat = None
        if heartbeat is not None:
            out[session_name] = heartbeat
    return out


def _session_name_for_worker(session: Any) -> str:
    """Resolve the session_name string for a worker-session record.

    Workers may surface their session name as ``session_name`` (the
    canonical tmux session) or be derivable from
    ``(task_project, task_number)`` via the standard
    ``worker_<project>/<number>`` convention. Returns ``""`` when no
    name can be inferred.
    """

    direct = getattr(session, "session_name", None)
    if direct:
        return str(direct)
    project = str(getattr(session, "task_project", "") or "")
    number = getattr(session, "task_number", None)
    if project and number is not None:
        return f"worker_{project}/{number}"
    return ""


# ── refresher integration ─────────────────────────────────────────


def install_real_refresh(
    cache: Any, config_provider: ConfigProvider,
) -> None:
    """Swap the cache's ``_refresh_fn`` to use the real per-project query.

    Used by callers that build their own :class:`ProjectStateCache`
    (the production lazy-singleton wires this automatically — see
    :func:`pollypm.state_cache.get_cache`). Test fixtures can use
    this to flip a cache from stub to real without reaching into
    ``_refresh_fn`` directly.
    """

    cache._refresh_fn = build_refresh_fn(config_provider)  # noqa: SLF001
