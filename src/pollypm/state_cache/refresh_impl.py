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
from pathlib import Path
from typing import Any, Callable

from pollypm.state_cache.entry import (
    WORKSPACE_ENTRY_TTL_SECONDS,
    WORKSPACE_PROJECT_KEY,
    ProjectStateCacheEntry,
    config_identity,
    empty_entry,
)

logger = logging.getLogger(__name__)

# #2051 round-6 (Codex review): the workspace sentinel + its TTL live in
# :mod:`pollypm.state_cache.entry` (a dependency-free leaf) so the
# refresher, this refresh-impl, ``cockpit_inbox``, and the test suite
# all share one source of truth. The names are re-exported here purely
# for back-compat with earlier importers; new code should prefer
# ``from pollypm.state_cache.entry import WORKSPACE_PROJECT_KEY``.
__all__ = [
    "ConfigProvider",
    "WORKSPACE_ENTRY_TTL_SECONDS",
    "WORKSPACE_PROJECT_KEY",
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

    #2050: heartbeat prefetch is back. ``refresher._INVALIDATING_EVENTS``
    now contains ``heartbeat.tick`` so every workspace heartbeat sweep
    invalidates the per-project entries and the refresher repopulates
    ``latest_heartbeat_by_session`` from one bulk pg query per project.
    The PR #2026 v3 fast-path-disable workaround on
    ``cockpit_rail._latest_heartbeat_cached`` is reverted in tandem so
    rail reads serve from the snapshot again.
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

    #2051: when ``project_key`` is :data:`WORKSPACE_PROJECT_KEY`, the
    returned entry carries the workspace-root awaits-user items
    (messages with ``scope IN ('', 'inbox')`` — they map to
    ``project == "inbox"`` after
    :func:`message_row_to_inbox_entry`). The synthetic entry skips
    categorization + rollup (no project to categorize) so the cache
    fast-path can union it with per-project entries without affecting
    rail / dashboard consumers (which iterate over tracked project
    keys and ignore extras).
    """

    if project_key == WORKSPACE_PROJECT_KEY:
        return _compute_workspace_entry(config)

    project = _get_project(config, project_key)
    project_path = _project_path(project)
    tracked = bool(getattr(project, "tracked", True)) if project else False

    awaits_user_items = _awaits_user_items_for(project_key, config)
    # #2049 — workspace-wide alert fetch, filtered to this project's
    # actionable task alerts. ``rollup_project_state`` consumes the
    # filtered set; folding it in here means the cached ``rail_*``
    # fields match the direct path even when a live alert is present.
    # Best-effort: on any failure we degrade to "no alerts," which
    # matches the pre-fix workaround's worst-case (no RED bump) rather
    # than crashing the refresh.
    actionable_alert_ids = _actionable_alert_ids_for(project_key, config)
    state, glyph, detail, rail_rollup, session_names = _categorize_and_rollup(
        project_key=project_key,
        config=config,
        tracked=tracked,
        awaits_user_items=list(awaits_user_items),
        actionable_alert_task_ids=actionable_alert_ids,
    )

    # #2050: bulk-prefetch heartbeats for every session known to this
    # project. ``refresher._INVALIDATING_EVENTS`` listens for
    # ``heartbeat.tick`` so this dict reflects the freshest pg state on
    # every refresh; the rail's ``_latest_heartbeat_cached`` reads from
    # ``latest_heartbeat_by_session`` again and only falls through to
    # the direct facade on a cache miss (cold start / unknown session
    # name). One ``latest_heartbeats_bulk`` query per project replaces
    # the per-call ``latest_heartbeat`` round-trips PR #2026 introduced.
    latest_heartbeat_by_session = _fetch_latest_heartbeats(
        session_names, config,
    )

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
        # #2049 — alert-driven actionable_key (issues-route id) is the
        # 6th tuple slot. Empty actionable alerts produce ``None`` to
        # match ``rollup_project_state``'s direct-path return.
        actionable_key=rail_rollup[5] if rail_rollup else None,
        awaits_user_count=len(awaits_user_items),
        awaits_user_items=tuple(awaits_user_items),
        latest_heartbeat_by_session=latest_heartbeat_by_session,
        computed_at=time.monotonic(),
        # PR #2026 v7 (Codex r7 blocker): stamp the config identity so
        # every cache lookup can verify the snapshot was computed
        # against the same config the caller holds. Without this the
        # singleton cache can serve cross-config data whenever two
        # configs share project keys.
        config_identity=config_identity(config),
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


def _workspace_root_items(config: Any) -> list[Any]:
    """Workspace-wide awaits-user sweep, filtered to workspace-root items.

    #2051: workspace-root messages (``scope IN ('', 'inbox')``) carry
    ``project == "inbox"`` after :func:`message_row_to_inbox_entry` —
    they don't match any tracked project key, so the per-project
    refresh in :func:`_awaits_user_items_for` drops them. This helper
    collects the slice that belongs in the synthetic workspace entry
    instead.

    Calls the DIRECT (uncached) helper for the same recursion-guard
    reason as :func:`_awaits_user_items_for`.
    """

    try:
        from pollypm.cockpit_inbox import _pm_inbox_awaits_user_list_uncached
    except Exception:  # noqa: BLE001
        logger.warning(
            "state_cache: cockpit_inbox import failed during workspace refresh",
            exc_info=True,
        )
        return []
    try:
        items = _pm_inbox_awaits_user_list_uncached(config)
    except Exception:  # noqa: BLE001
        logger.warning(
            "state_cache: _pm_inbox_awaits_user_list_uncached raised "
            "for workspace entry",
            exc_info=True,
        )
        return []

    out: list[Any] = []
    for item in items:
        project = str(getattr(item, "project", "") or "").strip()
        scope = str(getattr(item, "scope", "") or "").strip()
        # Workspace-root rows surface as ``project == "inbox"``
        # (per ``message_row_to_inbox_entry``'s fallback). The legacy
        # source enumeration also propagated empty scopes through;
        # accept those for robustness so a future shape change in
        # ``message_row_to_inbox_entry`` doesn't silently drop them.
        if project == "inbox" or (project == "" and scope in ("", "inbox")):
            out.append(item)
    return out


def _compute_workspace_entry(config: Any) -> ProjectStateCacheEntry:
    """Build the synthetic ``__workspace__`` cache entry (#2051).

    Workspace-root awaits-user items live on this entry instead of
    any per-project entry. The rest of the entry shape is "no data":
    no categorization, no rollup, no project_path — the consumers
    that iterate per-project entries (rail, dashboard) filter on
    ``config.projects`` keys so the synthetic entry is naturally
    ignored by them.

    Failures degrade to an empty entry so a broken pg path never
    propagates a crash through the refresher.
    """

    items = _workspace_root_items(config)
    return ProjectStateCacheEntry(
        project_key=WORKSPACE_PROJECT_KEY,
        project_path=Path(""),
        tracked=False,
        state=None,
        glyph="",
        detail="",
        rail_state=None,
        rail_badge=None,
        rail_sort_rank=0,
        rail_reason="",
        approvals_pending=0,
        awaits_user_count=len(items),
        awaits_user_items=tuple(items),
        computed_at=time.monotonic(),
        config_identity=config_identity(config),
    )


def _actionable_alert_ids_for(
    project_key: str, config: Any,
) -> frozenset[str]:
    """Return the per-project actionable-task-alert ids.

    #2049 — folds the live alert set into the cached entry's rail
    rollup so the cache fast-path can serve a rollup that matches the
    direct path even when a ``stuck_on_task:<project>/<n>`` or
    ``no_session_for_assignment:<project>/<n>`` alert is present.
    Pre-fix the cache declined for any render with a tracked-project
    actionable alert (the PR #2026 workaround) which killed the cache
    benefit during the most common rail state.

    Best-effort: every failure path returns ``frozenset()`` so a broken
    store / supervisor falls back to "no alerts." That matches the
    legacy fallthrough's no-RED-bump behaviour rather than crashing
    the refresh.
    """

    try:
        from pollypm.cockpit_project_state import actionable_alert_task_ids
    except Exception:  # noqa: BLE001
        return frozenset()

    alerts = _open_alerts_for(config)
    try:
        return actionable_alert_task_ids(
            alerts, project_key=project_key,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "state_cache: actionable_alert_task_ids failed for %s",
            project_key,
            exc_info=True,
        )
        return frozenset()


def _open_alerts_for(config: Any) -> list[Any]:
    """Return the workspace's open alerts, or ``[]`` on any failure.

    Reads :meth:`Supervisor.open_alerts` — the same source the cockpit
    direct path uses (cockpit_rail constructs a supervisor and reads
    ``supervisor.open_alerts()`` per render). The lazy import keeps
    the leaf ``state_cache`` package light.

    Construction is best-effort and read-only. Supervisor construction
    can fail on first call (e.g. backend pool not yet warm during cold
    start) — every failure path degrades to ``[]`` so a refresh tick
    never hard-fails on the alert overlay; we just lose the RED-bump
    for that one refresh cycle.
    """

    try:
        from pollypm.supervisor import Supervisor
    except Exception:  # noqa: BLE001
        return []
    supervisor = None
    try:
        supervisor = Supervisor(config, readonly_state=True)
        return list(supervisor.open_alerts())
    except Exception:  # noqa: BLE001
        logger.warning(
            "state_cache: supervisor.open_alerts() failed",
            exc_info=True,
        )
        return []
    finally:
        # ``Supervisor`` opens a StateStore handle on construction; the
        # leaf state_cache module shouldn't leak it across refresh ticks.
        if supervisor is not None:
            try:
                close = getattr(getattr(supervisor, "store", None), "close", None)
                if callable(close):
                    close()
            except Exception:  # noqa: BLE001
                pass


def _categorize_and_rollup(
    *,
    project_key: str,
    config: Any,
    tracked: bool,
    awaits_user_items: list[Any],
    actionable_alert_task_ids: frozenset[str] = frozenset(),
) -> tuple[
    Any, str, str,
    tuple[Any, Any, int, str, int, str | None] | None,
    list[str],
]:
    """Run ``categorize_project`` + ``rollup_project_state`` for one project.

    Returns ``(state, glyph, detail, rollup_tuple_or_None, session_names)``.
    The rollup tuple is ``(rail_state, rail_badge, sort_rank, reason,
    approvals_pending, actionable_key)`` — kept positional so the
    caller can ``zip`` it into the entry fields without a second
    import of the rollup types here. ``session_names`` is every tmux
    session this project is known to drive (canonical
    ``architect_<key>`` / ``plan_gate-<key>`` / ``worker_<key>`` plus
    the per-task ``worker_<key>/<n>`` rows the live worker_sessions
    table reports) — passed to :func:`_fetch_latest_heartbeats` for
    the heartbeat prefetch (#2050).

    #2049: takes ``actionable_alert_task_ids`` so the rail rollup
    folds in the alert overlay (RED bump for non-user-waiting alerted
    tasks, alert-driven ``actionable_key``) at compute time.

    Failures degrade silently — a broken work-service drops the
    project to IDLE (or PAUSED when not tracked) with no rollup and an
    empty session-name list (heartbeat prefetch becomes a no-op).
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
            # Without a work-service we still know the canonical session
            # names for this project — return them so heartbeats keep
            # flowing through the snapshot even when the work db is
            # unreachable.
            return (
                state, glyph, detail, None,
                _canonical_session_names(project_key, []),
            )

        tasks_by_alias, workers_by_alias = _prefetch_project_state(
            config, shared_svc,
        )
        aliases = _aliases_for(config, project_key)
        slice_svc = _ProjectSliceService(
            project_key=project_key,
            aliases=aliases,
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
            rollup = rollup_project_state(
                project_key,
                project_tasks,
                actionable_task_alert_ids=actionable_alert_task_ids,
            )
            rollup_tuple = (
                rollup.state,
                rollup.badge,
                rollup.sort_rank,
                rollup.reason,
                rollup.approvals_pending,
                rollup.actionable_key,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "state_cache: rollup_project_state failed for %s",
                project_key,
                exc_info=True,
            )
            rollup_tuple = None

        # #2050: collect every session_name this project is known to
        # drive, so the bulk heartbeat fetch downstream produces a
        # snapshot the rail can serve. We union the canonical workspace
        # sessions (``architect_<key>`` etc.) with the per-task
        # ``worker_<key>/<n>`` rows the live worker_sessions table
        # reports — the latter come from the already-prefetched
        # ``workers_by_alias`` so this costs no extra db round-trip.
        live_worker_sessions: list[Any] = []
        for alias in [project_key, *aliases]:
            live_worker_sessions.extend(
                workers_by_alias.get(alias, []),
            )
        session_names = _canonical_session_names(
            project_key, aliases,
            live_worker_sessions=live_worker_sessions,
        )

        return state, glyph, detail, rollup_tuple, session_names
    finally:
        if shared_svc is not None:
            _safe_close(shared_svc)


def _canonical_session_names(
    project_key: str,
    aliases: list[str],
    *,
    live_worker_sessions: list[Any] | None = None,
) -> list[str]:
    """Enumerate every tmux session name this project might heartbeat under.

    The rail's ``_latest_heartbeat_cached`` looks up entries by exact
    ``session_name``; we union three sources so the snapshot covers
    every name it might be asked for:

    1. Canonical workspace sessions — ``architect_<key>``,
       ``plan_gate-<key>``, ``worker_<key>`` (and the same for every
       storage alias) — these are the rows
       ``_session_name_for_item`` resolves for project-row PMs.
    2. Per-task worker sessions reported by ``list_worker_sessions``
       (``worker_<key>/<task_num>``) — pulled from the already-
       prefetched ``workers_by_alias`` so no extra db round-trip.
    3. (de-duplicated, empty names dropped)

    The list is intentionally bounded — only sessions this project is
    known to drive contribute, never the workspace-wide heartbeat
    table.
    """

    seen: set[str] = set()
    out: list[str] = []
    keys = [project_key, *aliases]
    for key in keys:
        if not key:
            continue
        for prefix in ("architect_", "plan_gate-", "worker_"):
            name = f"{prefix}{key}"
            if name not in seen:
                seen.add(name)
                out.append(name)
    for session in live_worker_sessions or []:
        name = str(getattr(session, "session_name", "") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _fetch_latest_heartbeats(
    session_names: list[str], config: Any,
) -> dict[str, Any]:
    """Bulk-fetch the most-recent heartbeat row for each session.

    Wraps :func:`pollypm.storage.pg_heartbeats.latest_heartbeats_bulk`
    behind a soft import so the leaf ``state_cache`` package stays
    importable in environments where the storage layer is missing
    (test harnesses that mock the cache out, etc.). On any failure
    the refresher degrades silently — the cache fast-path will miss
    on every session name and the rail will fall through to the
    direct pg facade (same shape as today).
    """

    if not session_names:
        return {}
    try:
        from pollypm.storage import pg_heartbeats
    except Exception:  # noqa: BLE001
        logger.debug(
            "state_cache: pg_heartbeats unavailable; "
            "skipping heartbeat prefetch", exc_info=True,
        )
        return {}
    try:
        return pg_heartbeats.latest_heartbeats_bulk(
            session_names, config=config,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "state_cache: latest_heartbeats_bulk failed; "
            "rail will fall through to direct facade",
            exc_info=True,
        )
        return {}


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
