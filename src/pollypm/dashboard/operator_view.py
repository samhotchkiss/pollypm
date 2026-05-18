"""Operator dashboard data loader (#1572).

Bridges the leaf categorization module to the workspace's project
config + per-project work-service DBs. Keeps the I/O side-effects
out of :mod:`pollypm.dashboard.categorization` so the categorizer
stays trivially unit-testable against a mock work service.

The loader mirrors :func:`pollypm.cockpit_inbox.pm_inbox_awaits_user_list`'s
DB-discovery shape: one work-service handle per (project, db_path)
tuple, opened with the canonical workspace DB and the legacy
per-project DB so paused-with-work projects still surface.
"""

from __future__ import annotations

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


@dataclass(frozen=True, slots=True)
class _ProjectScan:
    """Resolved scan target — project key + a list of DB paths to try."""

    project_key: str
    project_path: Path
    db_paths: tuple[Path, ...]
    tracked: bool


def _collect_project_scans(config) -> list[_ProjectScan]:  # noqa: ANN001
    """Resolve (project_key, project_path, db_paths, tracked) tuples."""
    project_settings = getattr(config, "project", None)
    workspace_root = getattr(project_settings, "workspace_root", None)

    scans: list[_ProjectScan] = []
    seen_projects: set[str] = set()
    projects = getattr(config, "projects", {}) or {}
    for project_key, project in projects.items():
        if project_key in seen_projects:
            continue
        seen_projects.add(project_key)
        project_path = Path(getattr(project, "path", "."))
        db_candidates: list[Path] = []

        def _add(p: object) -> None:
            try:
                candidate = Path(p)
            except TypeError:
                return
            if candidate in db_candidates:
                return
            try:
                if candidate.exists():
                    db_candidates.append(candidate)
            except OSError:
                return

        if workspace_root is not None:
            _add(Path(workspace_root) / ".pollypm" / "state.db")
        _add(project_path / ".pollypm" / "state.db")

        scans.append(
            _ProjectScan(
                project_key=str(project_key),
                project_path=project_path,
                db_paths=tuple(db_candidates),
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


def _open_work_service(scan: _ProjectScan):  # noqa: ANN202
    """Open the first usable work-service handle for ``scan``."""
    from pollypm.work import create_work_service

    for db_path in scan.db_paths:
        try:
            svc = create_work_service(
                db_path=db_path, project_path=scan.project_path,
            )
        except Exception:  # noqa: BLE001
            continue
        return svc
    return None


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


def load_operator_view_from_config(config) -> OperatorDashboardView:  # noqa: ANN001
    """Like :func:`load_operator_view` but starting from a loaded config.

    Useful for tests + callers that have a config in hand and want to
    avoid re-parsing the TOML.
    """
    scans = _collect_project_scans(config)
    waiting_by_project = _waiting_items_by_project(config)

    waiting: list[OperatorDashboardRow] = []
    working: list[OperatorDashboardRow] = []
    idle: list[OperatorDashboardRow] = []
    paused: list[OperatorDashboardRow] = []

    for scan in scans:
        svc = _open_work_service(scan)
        items = waiting_by_project.get(scan.project_key, [])
        try:
            if svc is None:
                state = (
                    ProjectState.WAITING if items
                    else (ProjectState.PAUSED if not scan.tracked else ProjectState.IDLE)
                )
                detail = (
                    why_waiting(items) if state is ProjectState.WAITING
                    else ("Paused" if state is ProjectState.PAUSED else "Quiet")
                )
                row = OperatorDashboardRow(
                    project_key=scan.project_key,
                    state=state,
                    glyph=glyph_for_project_state(state),
                    detail=detail,
                )
            else:
                state = categorize_project(
                    scan.project_key,
                    work_service=svc,
                    inbox_items=items,
                    tracked=scan.tracked,
                )
                glyph = glyph_for_project_state(state)
                if state is ProjectState.WAITING:
                    detail = why_waiting(items)
                elif state is ProjectState.WORKING:
                    detail = what_working(scan.project_key, work_service=svc)
                elif state is ProjectState.PAUSED:
                    detail = "Paused"
                else:
                    detail = "Quiet"
                row = OperatorDashboardRow(
                    project_key=scan.project_key,
                    state=state,
                    glyph=glyph,
                    detail=detail,
                )
        finally:
            close = getattr(svc, "close", None) if svc is not None else None
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass

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


def project_state_map_from_config(config) -> dict[str, ProjectState]:  # noqa: ANN001
    """Return ``{project_key: ProjectState}`` for every project in ``config``.

    The rail uses this to pick its glyph from the same source the
    dashboard uses — guaranteeing the section a project appears in
    matches the glyph drawn next to it. The function is best-effort:
    a project whose DB can't be opened falls through to a tracked /
    paused IDLE classification rather than raising.
    """
    scans = _collect_project_scans(config)
    waiting_by_project = _waiting_items_by_project(config)
    states: dict[str, ProjectState] = {}
    for scan in scans:
        svc = _open_work_service(scan)
        items = waiting_by_project.get(scan.project_key, [])
        try:
            if svc is None:
                if items:
                    states[scan.project_key] = ProjectState.WAITING
                elif not scan.tracked:
                    states[scan.project_key] = ProjectState.PAUSED
                else:
                    states[scan.project_key] = ProjectState.IDLE
                continue
            states[scan.project_key] = categorize_project(
                scan.project_key,
                work_service=svc,
                inbox_items=items,
                tracked=scan.tracked,
            )
        finally:
            close = getattr(svc, "close", None) if svc is not None else None
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass
    return states


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
