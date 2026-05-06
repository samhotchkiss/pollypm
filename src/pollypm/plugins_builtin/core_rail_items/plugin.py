"""Core rail items — the built-in PollyPM cockpit rail, re-expressed as
plugin-registered items.

Before er02 the cockpit's rail was a hardcoded list baked into
:meth:`pollypm.cockpit.CockpitRouter.build_items`. er02 lifts that list
into this built-in plugin so third-party plugins can slot rail entries
alongside the core ones through the same registration API.

Each registration carries a ``state_provider`` / ``label_provider`` /
``rows_provider`` callable where the row needs live data (e.g. inbox
count, project working-state, per-task workers). The cockpit router
passes itself and the spinner index via
``RailContext.extras`` so these callables can reach the same helpers
the legacy hardcoded builder used — no logic has moved out of
:class:`CockpitRouter`, it just runs from registered entries instead of
an in-line list.
"""

from __future__ import annotations

import os
import logging
from typing import Any

from pollypm.plugin_api.v1 import (
    PanelSpec,
    PollyPMPlugin,
    RailContext,
    RailRow,
)

logger = logging.getLogger(__name__)


def _strict_rail_errors_enabled() -> bool:
    """Return ``True`` when rail render failures should surface in tests.

    ``CI=1`` keeps the failure loud in automation, while the explicit
    override gives local tests a way to opt in without changing the
    broader environment.
    """
    return os.getenv("CI") == "1" or os.getenv("POLLYPM_STRICT_RAIL_ERRORS") == "1"


# ---------------------------------------------------------------------------
# Helpers: reach into the cockpit router via RailContext.extras. Missing
# extras are tolerated — the plugin falls back to safe defaults so it can be
# used outside the cockpit (e.g. in `pm rail list`).
# ---------------------------------------------------------------------------


# #800: prefer typed fields on ``RailContext`` and fall back to the
# legacy ``extras`` dict only when a non-cockpit caller (e.g. ``pm
# rail list``) constructs a context manually without populating the
# typed fields. New callers should set the typed fields directly.


def _router(ctx: RailContext):
    return ctx.router or ctx.extras.get("router")


def _spinner_index(ctx: RailContext) -> int:
    return int(ctx.spinner_index or ctx.extras.get("spinner_index", 0) or 0)


def _launches(ctx: RailContext):
    return ctx.launches or ctx.extras.get("launches") or []


def _windows(ctx: RailContext):
    return ctx.windows or ctx.extras.get("windows") or []


def _alerts(ctx: RailContext):
    return ctx.alerts or ctx.extras.get("alerts") or []


def _supervisor(ctx: RailContext):
    return ctx.supervisor or ctx.extras.get("supervisor")


def _config(ctx: RailContext):
    return ctx.config or ctx.extras.get("config")


# ---------------------------------------------------------------------------
# State providers for Polly / Russell (operator / reviewer sessions).
# ---------------------------------------------------------------------------


def _session_state(ctx: RailContext, session_name: str) -> str:
    router = _router(ctx)
    if router is None:
        return "idle"
    try:
        return router._session_state(
            session_name,
            _launches(ctx),
            _windows(ctx),
            _alerts(ctx),
            _spinner_index(ctx),
            user_waiting_task_ids=_user_waiting_task_ids(ctx),
        )
    except Exception:  # noqa: BLE001
        logger.exception("core_rail_items: _session_state(%s) raised", session_name)
        if _strict_rail_errors_enabled():
            raise
        return "idle"


def _user_waiting_task_ids(ctx: RailContext) -> frozenset[str]:
    """Return ``{project/N}`` ids for tasks in user-waiting status,
    sourced from each tracked project's state.db.

    Used to suppress redundant ``stuck_on_task:<id>`` alert glyphs on
    the rail — when the task is already user-waiting, the session
    being idle is the system doing what it should (waiting on the
    user), not a fault to flag.
    """
    config = _config(ctx)
    if config is None:
        return frozenset()
    from pollypm.work.task_state import user_waiting_task_ids

    return user_waiting_task_ids(config)


def _active_task_numbers(project: Any, *, config: Any = None) -> list[int]:
    """Return per-task task numbers that the DB considers actively worked.

    DB-truth source for the rail's per-project ``Task #N`` rows (#1002).
    The legacy implementation enumerated ``task-<project>-<N>`` windows in
    the storage-closet tmux session, which leaked zombie windows
    (post-completion cleanup gaps) into the rail and routed clicks to
    task IDs the DB no longer knows about. Reading from ``work_tasks``
    instead means a missed window kill cannot leak to the UI: only tasks
    in ``in_progress`` / ``rework`` (i.e. an active claim is open)
    surface as worker rows. ``review`` tasks are excluded — work is
    done and the row would lead to a worker pane with nothing to do.

    Resolution mirrors :func:`pollypm.work.db_resolver.resolve_work_db_path`:
    check the per-project ``state.db`` first; if it has no rows for
    ``project.key``, fall through to the workspace-root ``state.db``
    derived from ``config.project.workspace_root``. This keeps the
    rail correct for both the per-project layout and the
    workspace-root layout (untracked / shared-DB projects) without
    introducing a hard dependency on ``load_config`` from the rail.

    Returns numbers sorted ascending so rows render in stable order.
    Failures (missing db, sqlite errors) yield an empty list — the
    rail simply shows no per-task rows rather than crashing.
    """
    project_key = getattr(project, "key", None)
    if not project_key:
        return []
    from pollypm.work.task_state import active_task_numbers

    return active_task_numbers(project, config=config)


def _polly_state(ctx: RailContext) -> str:
    notice_state = _live_chat_network_dead_state(ctx)
    if notice_state is not None:
        return notice_state
    return _session_state(ctx, "operator")


def _russell_state(ctx: RailContext) -> str:
    return _session_state(ctx, "reviewer")


def _live_chat_network_dead_state(ctx: RailContext) -> str | None:
    try:
        from pollypm.cockpit_live_chat_notice import (
            current_live_chat_network_dead_notice,
        )

        notice = current_live_chat_network_dead_notice(ctx.cockpit_state)
    except Exception:  # noqa: BLE001
        return None
    return f"! {notice}" if notice else None


def _session_configured(ctx: RailContext, session_name: str) -> bool:
    """Return ``True`` when ``session_name`` has a config block.

    The cockpit's live-session route for a rail entry calls
    ``supervisor.plan_launches()`` and looks up a ``SessionLaunchSpec``
    by name. When no ``[sessions.<name>]`` block exists the lookup
    returns no match and ``_route_live_session`` falls through to the
    static fallback (``"polly"`` → ``PollyDashboardApp``) — which is
    why clicking ``Russell · chat`` silently lands the user on the
    Dashboard (#962). Hide the rail entry when its backing session
    is not configured so the rail-entry-to-route contract holds:
    every visible entry has a working destination.
    """
    config = _config(ctx)
    if config is None:
        return False
    sessions = getattr(config, "sessions", None)
    if not sessions:
        return False
    return session_name in sessions


def _russell_visible(ctx: RailContext) -> bool:
    return _session_configured(ctx, "reviewer")


# ---------------------------------------------------------------------------
# Inbox label + state.
# ---------------------------------------------------------------------------


# Per-render memoization for ``_count_inbox_tasks_for_label``. The rail
# calls ``_inbox_count`` three times per render (label, state, badge),
# each call would otherwise walk every tracked project's
# SQLAlchemyStore + SQLiteWorkService. The cache key is the router
# instance id (a fresh router per cockpit boot) plus a coarse 1s
# wall-clock bucket so the count refreshes between ticks but doesn't
# triple-fire within one. Cleared lazily as the bucket rolls over.
_INBOX_COUNT_CACHE: dict[tuple[int, int], int] = {}


def _inbox_count(ctx: RailContext) -> int:
    config = _config(ctx)
    if config is None:
        return 0
    from pollypm.cockpit import _count_inbox_tasks_for_label

    import time as _time

    router = _router(ctx)
    bucket = (id(router) if router is not None else 0, int(_time.monotonic()))
    cached = _INBOX_COUNT_CACHE.get(bucket)
    if cached is not None:
        return cached
    try:
        count = int(_count_inbox_tasks_for_label(config) or 0)
    except Exception:  # noqa: BLE001
        logger.exception("core_rail_items: inbox count raised")
        return 0
    # Drop stale per-router buckets so the cache doesn't grow.
    router_id = bucket[0]
    for stale_key in list(_INBOX_COUNT_CACHE.keys()):
        if stale_key[0] == router_id and stale_key != bucket:
            _INBOX_COUNT_CACHE.pop(stale_key, None)
    _INBOX_COUNT_CACHE[bucket] = count
    return count


def _inbox_label(ctx: RailContext) -> str:
    count = _inbox_count(ctx)
    return f"Inbox ({count})" if count else "Inbox"


def _inbox_state(ctx: RailContext) -> str:
    return "mail" if _inbox_count(ctx) else "clear"


def _inbox_badge(ctx: RailContext):
    count = _inbox_count(ctx)
    return count if count else None


# ---------------------------------------------------------------------------
# Projects section — one registration → N rows + optional sub-rows.
# Mirrors the block previously inside CockpitRouter.build_items().
# ---------------------------------------------------------------------------


def _classify_projects(ctx: RailContext) -> tuple[list[tuple[str, Any]], list[tuple[str, Any]], dict[str, bool]]:
    """Split config.projects into (active, inactive) lists and compute
    which projects have a working task. Mirrors the original in-line
    logic so visual behaviour is preserved."""
    router = _router(ctx)
    config = _config(ctx)
    if router is None or config is None:
        return [], [], {}

    from datetime import UTC, datetime, timedelta
    from pathlib import Path

    now = datetime.now(UTC)
    cutoff_iso = (now - timedelta(hours=24)).isoformat()
    cutoff_ts = (now - timedelta(hours=24)).timestamp()
    active_projects: list[tuple[str, Any]] = []
    inactive_projects: list[tuple[str, Any]] = []
    project_has_active_task: dict[str, bool] = {}

    def _path_mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime if path.exists() else 0.0
        except OSError:
            return 0.0

    def _project_activity(project_key: str, project: Any) -> tuple[bool, bool]:
        db_path = project.path / ".pollypm" / "state.db"
        git_dir = project.path / ".git"
        db_mtime = _path_mtime(db_path)
        workspace_root = getattr(getattr(config, "project", None), "workspace_root", None)
        if workspace_root is not None:
            db_mtime = max(
                db_mtime,
                _path_mtime(Path(workspace_root) / ".pollypm" / "state.db"),
            )
        git_mtime = _path_mtime(git_dir)
        cache = getattr(router, "_project_activity_cache", None)
        if isinstance(cache, dict):
            cached = cache.get(project_key)
            if cached is not None and cached[0] == db_mtime and cached[1] == git_mtime:
                return cached[2], cached[3]

        from pollypm.work.task_state import project_activity as _work_project_activity

        is_active, has_working_task = _work_project_activity(
            project_key=project_key,
            project=project,
            cutoff_iso=cutoff_iso,
            config=config,
        )
        if not is_active and git_mtime > cutoff_ts:
            is_active = True
        if isinstance(cache, dict):
            cache[project_key] = (db_mtime, git_mtime, is_active, has_working_task)
        return is_active, has_working_task

    for project_key, project in getattr(config, "projects", {}).items():
        is_active, has_working_task = _project_activity(project_key, project)
        project_has_active_task[project_key] = has_working_task
        if is_active:
            active_projects.append((project_key, project))
        else:
            inactive_projects.append((project_key, project))

    # Evict stale cache entries.
    cache = getattr(router, "_project_activity_cache", None)
    if isinstance(cache, dict):
        live_keys = set(getattr(config, "projects", {}).keys())
        for stale_key in list(cache.keys()):
            if stale_key not in live_keys:
                cache.pop(stale_key, None)

    active_projects.sort(key=lambda x: x[1].display_label().lower())
    inactive_projects.sort(key=lambda x: x[1].display_label().lower())
    return active_projects, inactive_projects, project_has_active_task


def _selected_key(ctx: RailContext) -> str:
    state = ctx.cockpit_state or {}
    value = state.get("selected")
    return str(value) if isinstance(value, str) and value else "polly"


def _project_chat_persona(project: Any, session_role: Any) -> str:
    if isinstance(session_role, str) and session_role.strip():
        try:
            from pollypm.role_contract import canonical_role, persona_for

            if canonical_role(session_role) == "architect":
                return persona_for("architect")
        except ValueError:
            pass

    persona_raw = getattr(project, "persona_name", None)
    return (
        persona_raw.strip()
        if isinstance(persona_raw, str) and persona_raw.strip()
        else "Project PM"
    )


def _project_rows(ctx: RailContext) -> list[RailRow]:
    router = _router(ctx)
    if router is None:
        return []
    active, inactive, has_working = _classify_projects(ctx)
    if not active and not inactive:
        return [RailRow(
            key="projects_root",
            label="Projects",
            state="separator",
            selectable=False,
        )]
    selected = _selected_key(ctx)
    config = _config(ctx)

    # Map project -> session name/role for session_state fallback and label copy.
    from pollypm.models import CONTROL_ROLES

    project_session_map: dict[str, tuple[str, str]] = {}
    for launch in _launches(ctx):
        role = getattr(launch.session, "role", "")
        if role in CONTROL_ROLES:
            continue
        project_session_map.setdefault(
            launch.session.project,
            (launch.session.name, role),
        )

    rows: list[RailRow] = []

    def _emit(project_key: str, project: Any) -> None:
        session_info = project_session_map.get(project_key)
        session_name = session_info[0] if session_info is not None else None
        session_role = session_info[1] if session_info is not None else None
        if has_working.get(project_key):
            state = "\u25c6 working"
        elif session_name is not None:
            state = _session_state(ctx, session_name)
        else:
            state = "idle"
        rows.append(RailRow(
            key=f"project:{project_key}",
            label=project.display_label(),
            state=state,
        ))
        if selected.startswith(f"project:{project_key}"):
            rows.append(RailRow(
                key=f"project:{project_key}:dashboard",
                label="  Dashboard",
                state="sub",
            ))
            persona = _project_chat_persona(project, session_role)
            label = f"  PM Chat ({persona})"
            rows.append(RailRow(
                key=f"project:{project_key}:session",
                label=label,
                state="sub",
            ))
            rows.append(RailRow(
                key=f"project:{project_key}:issues",
                label="  Tasks",
                state="sub",
            ))
            for task_num in _active_task_numbers(project, config=config):
                rows.append(RailRow(
                    key=f"project:{project_key}:task:{task_num}",
                    label=f"  \u27f3 Task #{task_num}",
                    state="sub",
                ))
            rows.append(RailRow(
                key=f"project:{project_key}:settings",
                label="  Settings",
                state="sub",
            ))

    for project_key, project in active:
        _emit(project_key, project)
    if active and inactive:
        rows.append(RailRow(
            key="_separator",
            label="",
            state="separator",
            selectable=False,
        ))
    for project_key, project in inactive:
        _emit(project_key, project)
    return rows


# ---------------------------------------------------------------------------
# Handlers — the rail router owns all selection logic today; the handler
# is a thin stub that records the intent. er03/er04 may extend this.
# ---------------------------------------------------------------------------


def _route_handler(key: str):
    def _handler(ctx: RailContext) -> PanelSpec | None:
        router = _router(ctx)
        if router is not None:
            try:
                router.route_selected(key)
            except Exception:  # noqa: BLE001
                logger.exception("core_rail_items: route_selected(%s) raised", key)
        return PanelSpec(widget=None, focus_hint=key)
    return _handler


def _project_handler(ctx: RailContext) -> PanelSpec | None:
    # Selecting a project row currently routes in CockpitRouter.route_selected
    # based on the selected_key. The handler is a pass-through so plugin
    # authors who want to intercept a specific row can override through the
    # rail registry's deduplication.
    return PanelSpec(widget=None, focus_hint="project")


# ---------------------------------------------------------------------------
# initialize — registers every rail item.
# ---------------------------------------------------------------------------


def _initialize(api) -> None:  # noqa: ANN001
    rail = api.rail

    # Top section — Home plus operator/reviewer/inbox. In the legacy layout
    # these all rendered at the top before the projects section, so we keep
    # them in `top` rather than `workflows` to preserve ordering.
    # Home → static dashboard. Polly / Russell → live operator /
    # reviewer chat panes. Earlier the rail labelled both 'Polly' and
    # the dashboard 'Home' without clarifying which surface lived where
    # (#870), so users routinely opened Polly expecting the dashboard
    # and got dropped into the live operator session. The chat-pane
    # labels make the surface explicit.
    rail.register_item(
        section="top",
        index=0,
        label="Home",
        handler=_route_handler("dashboard"),
        key="dashboard",
        state_provider=lambda _ctx: "home",
    )
    rail.register_item(
        section="top",
        index=10,
        label="Polly · chat",
        handler=_route_handler("polly"),
        key="polly",
        state_provider=_polly_state,
    )
    rail.register_item(
        section="top",
        index=20,
        label="Russell · chat",
        handler=_route_handler("russell"),
        key="russell",
        state_provider=_russell_state,
        # #962 — the rail entry only routes to a live pane when a
        # ``[sessions.reviewer]`` block exists. Without it the live
        # session lookup raises and the router falls through to the
        # static dashboard fallback. Hide the entry instead of dead-
        # ending the click on the Dashboard.
        visibility=_russell_visible,
    )
    rail.register_item(
        section="top",
        index=30,
        label="Inbox",
        handler=_route_handler("inbox"),
        key="inbox",
        label_provider=_inbox_label,
        state_provider=_inbox_state,
        badge_provider=_inbox_badge,
    )

    # Projects section — single registration with dynamic rows.
    rail.register_item(
        section="projects",
        index=0,
        label="Projects",
        handler=_project_handler,
        key="projects_root",
        rows_provider=_project_rows,
    )

    # System section — settings row anchored at the bottom.
    rail.register_item(
        section="system",
        index=0,
        label="Settings",
        handler=_route_handler("settings"),
        key="settings",
        state_provider=lambda _ctx: "config",
    )


plugin = PollyPMPlugin(
    name="core_rail_items",
    version="0.1.0",
    description=(
        "Built-in cockpit rail items — Polly, Russell, Inbox, Projects, Settings. "
        "Registered through the Rail API so third-party plugins can slot "
        "alongside without editing cockpit code."
    ),
    initialize=_initialize,
)
