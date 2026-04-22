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


def _router(ctx: RailContext):
    return ctx.extras.get("router")


def _spinner_index(ctx: RailContext) -> int:
    return int(ctx.extras.get("spinner_index", 0) or 0)


def _launches(ctx: RailContext):
    return ctx.extras.get("launches") or []


def _windows(ctx: RailContext):
    return ctx.extras.get("windows") or []


def _alerts(ctx: RailContext):
    return ctx.extras.get("alerts") or []


def _supervisor(ctx: RailContext):
    return ctx.extras.get("supervisor")


def _config(ctx: RailContext):
    return ctx.extras.get("config")


# ---------------------------------------------------------------------------
# State providers for Polly / Russell (operator / reviewer sessions).
# ---------------------------------------------------------------------------


def _session_state(ctx: RailContext, session_name: str) -> str:
    router = _router(ctx)
    if router is None:
        return "idle"
    try:
        return router._session_state(
            session_name, _launches(ctx), _windows(ctx), _alerts(ctx), _spinner_index(ctx),
        )
    except Exception:  # noqa: BLE001
        logger.exception("core_rail_items: _session_state(%s) raised", session_name)
        if _strict_rail_errors_enabled():
            raise
        return "idle"


def _polly_state(ctx: RailContext) -> str:
    return _session_state(ctx, "operator")


def _russell_state(ctx: RailContext) -> str:
    return _session_state(ctx, "reviewer")


# ---------------------------------------------------------------------------
# Inbox label + state.
# ---------------------------------------------------------------------------


def _inbox_count(ctx: RailContext) -> int:
    config = _config(ctx)
    if config is None:
        return 0
    from pollypm.cockpit import _count_inbox_tasks_for_label

    try:
        return int(_count_inbox_tasks_for_label(config) or 0)
    except Exception:  # noqa: BLE001
        logger.exception("core_rail_items: inbox count raised")
        return 0


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
    import sqlite3

    now = datetime.now(UTC)
    cutoff_iso = (now - timedelta(hours=24)).isoformat()
    cutoff_ts = (now - timedelta(hours=24)).timestamp()
    active_projects: list[tuple[str, Any]] = []
    inactive_projects: list[tuple[str, Any]] = []
    project_has_active_task: dict[str, bool] = {}

    def _project_activity(project_key: str, project: Any) -> tuple[bool, bool]:
        db_path = project.path / ".pollypm" / "state.db"
        git_dir = project.path / ".git"
        try:
            db_mtime = db_path.stat().st_mtime if db_path.exists() else 0.0
        except OSError:
            db_mtime = 0.0
        try:
            git_mtime = git_dir.stat().st_mtime if git_dir.exists() else 0.0
        except OSError:
            git_mtime = 0.0
        cache = getattr(router, "_project_activity_cache", None)
        if isinstance(cache, dict):
            cached = cache.get(project_key)
            if cached is not None and cached[0] == db_mtime and cached[1] == git_mtime:
                return cached[2], cached[3]

        is_active = False
        has_working_task = False
        if db_mtime > 0.0:
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                try:
                    conn.row_factory = sqlite3.Row
                    row = conn.execute(
                        "SELECT "
                        "  SUM(CASE WHEN work_status IN ('in_progress','review') "
                        "           THEN 1 ELSE 0 END) AS working_count, "
                        "  MAX(updated_at) AS max_updated "
                        "FROM work_tasks WHERE project = ?",
                        (project_key,),
                    ).fetchone()
                finally:
                    conn.close()
                if row is not None:
                    working_count = row["working_count"] or 0
                    max_updated = row["max_updated"] or ""
                    if working_count > 0:
                        has_working_task = True
                        is_active = True
                    if max_updated and max_updated >= cutoff_iso:
                        is_active = True
            except (sqlite3.Error, OSError):
                pass
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


def _project_rows(ctx: RailContext) -> list[RailRow]:
    router = _router(ctx)
    if router is None:
        return []
    active, inactive, has_working = _classify_projects(ctx)
    if not active and not inactive:
        return []
    selected = _selected_key(ctx)

    # Map project -> session name for session_state fallback.
    project_session_map: dict[str, str] = {}
    for launch in _launches(ctx):
        role = getattr(launch.session, "role", "")
        if role in {"operator-pm", "heartbeat-supervisor", "triage", "reviewer"}:
            continue
        project_session_map.setdefault(launch.session.project, launch.session.name)

    rows: list[RailRow] = []

    def _emit(project_key: str, project: Any) -> None:
        session_name = project_session_map.get(project_key)
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
            persona = getattr(project, "persona_name", None) or "Polly"
            rows.append(RailRow(
                key=f"project:{project_key}:session",
                label=f"  PM Chat ({persona})",
                state="sub",
            ))
            rows.append(RailRow(
                key=f"project:{project_key}:issues",
                label="  Tasks",
                state="sub",
            ))
            supervisor = _supervisor(ctx)
            if supervisor is not None:
                try:
                    storage = supervisor.storage_closet_session_name()
                    task_prefix = f"task-{project_key}-"
                    for win in router.tmux.list_windows(storage):
                        if win.name.startswith(task_prefix):
                            task_num = win.name[len(task_prefix):]
                            rows.append(RailRow(
                                key=f"project:{project_key}:task:{task_num}",
                                label=f"  \u27f3 Task #{task_num}",
                                state="sub",
                            ))
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "core_rail_items: storage-closet enumeration failed", exc_info=True,
                    )
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

    # Top section — operator + reviewer + inbox. In the legacy layout these
    # all rendered at the top before the projects section, so we keep them
    # in `top` rather than `workflows` to preserve ordering.
    rail.register_item(
        section="top",
        index=0,
        label="Polly",
        handler=_route_handler("polly"),
        key="polly",
        state_provider=_polly_state,
    )
    rail.register_item(
        section="top",
        index=10,
        label="Russell",
        handler=_route_handler("russell"),
        key="russell",
        state_provider=_russell_state,
    )
    rail.register_item(
        section="top",
        index=20,
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
