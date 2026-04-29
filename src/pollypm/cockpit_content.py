"""Pure right-pane content planning for cockpit rail selections.

This module translates rail selection keys into data-only content plans.
It intentionally does not import Textual apps, tmux clients, the service
API, or supervisor code. Callers provide any project/session facts the
resolver needs, then a separate window manager can turn the returned plan
into tmux operations.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from pollypm.cockpit_rail_routes import (
    ProjectRoute,
    resolve_live_session_route,
    resolve_project_route,
    resolve_static_view_route,
)


PaneContentKind = Literal[
    "static_command",
    "live_agent",
    "loading",
    "error",
    "fallback",
]


@dataclass(frozen=True, slots=True)
class CockpitContentContext:
    """Facts the pure resolver may use without loading a supervisor."""

    project_keys: frozenset[str] | None = None
    project_sessions: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_projects(
        cls,
        projects: Iterable[str] | Mapping[str, object] | None = None,
        *,
        project_sessions: Mapping[str, str] | None = None,
    ) -> "CockpitContentContext":
        """Build a context from config-shaped project/session mappings."""
        project_keys: frozenset[str] | None
        if projects is None:
            project_keys = None
        elif isinstance(projects, Mapping):
            project_keys = frozenset(str(key) for key in projects)
        else:
            project_keys = frozenset(str(key) for key in projects)
        return cls(
            project_keys=project_keys,
            project_sessions=dict(project_sessions or {}),
        )


@dataclass(frozen=True, slots=True)
class TextualCommandPane:
    """A static right-pane surface launched through ``pm cockpit-pane``."""

    route_key: str
    selected_key: str
    pane_kind: str
    command_args: tuple[str, ...]
    project_key: str | None = None
    task_id: str | None = None
    content_kind: Literal["static_command"] = field(
        default="static_command",
        init=False,
    )
    right_pane_state: Literal["static"] = field(default="static", init=False)


StaticCommandPane = TextualCommandPane


@dataclass(frozen=True, slots=True)
class LiveAgentPane:
    """A live agent session to mount into the cockpit right pane."""

    route_key: str
    selected_key: str
    session_name: str
    project_key: str | None = None
    fallback: TextualCommandPane | None = None
    content_kind: Literal["live_agent"] = field(default="live_agent", init=False)
    right_pane_state: Literal["live_agent"] = field(
        default="live_agent",
        init=False,
    )


@dataclass(frozen=True, slots=True)
class LoadingPane:
    """A placeholder pane while async route work is pending."""

    route_key: str
    selected_key: str
    title: str
    message: str
    content_kind: Literal["loading"] = field(default="loading", init=False)
    right_pane_state: Literal["loading"] = field(default="loading", init=False)


@dataclass(frozen=True, slots=True)
class ErrorPane:
    """An explicit non-tmux error pane."""

    route_key: str
    selected_key: str
    reason: str
    title: str
    message: str
    content_kind: Literal["error"] = field(default="error", init=False)
    right_pane_state: Literal["error"] = field(default="error", init=False)


@dataclass(frozen=True, slots=True)
class FallbackPane:
    """An explicit fallback plan with a safe static target."""

    route_key: str
    selected_key: str
    reason: str
    message: str
    fallback: TextualCommandPane
    content_kind: Literal["fallback"] = field(default="fallback", init=False)
    right_pane_state: Literal["static"] = field(default="static", init=False)


RightPaneContentPlan = (
    TextualCommandPane
    | LiveAgentPane
    | LoadingPane
    | ErrorPane
    | FallbackPane
)


def loading_content_plan(
    route_key: str,
    *,
    selected_key: str | None = None,
    label: str | None = None,
) -> LoadingPane:
    """Return the optimistic loading plan for a pending route."""
    clean_key = _normalize_route_key(route_key)
    display = (label or clean_key).strip() or "selection"
    return LoadingPane(
        route_key=clean_key,
        selected_key=selected_key or clean_key,
        title="Loading",
        message=f"Loading {display}...",
    )


def resolve_cockpit_content(
    key: str,
    context: CockpitContentContext | None = None,
) -> RightPaneContentPlan:
    """Resolve a rail key to a typed right-pane content plan."""
    route_key = _normalize_route_key(key)
    if not route_key:
        return _error_plan(
            route_key=route_key,
            selected_key=route_key,
            reason="empty_route",
            message="Cockpit route key must not be empty.",
        )

    ctx = context or CockpitContentContext()

    live_route = resolve_live_session_route(route_key)
    if live_route is not None:
        return LiveAgentPane(
            route_key=route_key,
            selected_key=route_key,
            session_name=live_route.session_name,
            fallback=_textual_command_pane(
                route_key=route_key,
                selected_key=route_key,
                pane_kind=live_route.fallback_kind,
            ),
        )

    static_route = resolve_static_view_route(route_key)
    if static_route is not None:
        project_key = static_route.project_key
        if project_key is not None and not _project_exists(ctx, project_key):
            return _missing_project_plan(route_key, project_key)
        return _textual_command_pane(
            route_key=route_key,
            selected_key=static_route.selected_key or route_key,
            pane_kind=static_route.kind,
            project_key=project_key,
        )

    project_route = resolve_project_route(route_key)
    if project_route is not None:
        return _resolve_project_content(route_key, project_route, ctx)

    reason = "invalid_project_route" if route_key.startswith("project:") else "unknown_route"
    return _error_plan(
        route_key=route_key,
        selected_key=route_key,
        reason=reason,
        message=f"Unknown cockpit route: {route_key}",
    )


resolve_right_pane_content = resolve_cockpit_content


def _resolve_project_content(
    route_key: str,
    route: ProjectRoute,
    context: CockpitContentContext,
) -> RightPaneContentPlan:
    project_key = route.project_key
    if not _project_exists(context, project_key):
        return _missing_project_plan(route_key, project_key)

    sub_view = route.sub_view
    if sub_view is None or sub_view == "dashboard":
        return _textual_command_pane(
            route_key=route_key,
            selected_key=f"project:{project_key}:dashboard",
            pane_kind="project",
            project_key=project_key,
        )

    if sub_view == "settings":
        return _textual_command_pane(
            route_key=route_key,
            selected_key=route_key,
            pane_kind="settings",
            project_key=project_key,
        )

    if sub_view == "issues":
        task_id = f"{project_key}/{route.task_num}" if route.task_num else None
        return _textual_command_pane(
            route_key=route_key,
            selected_key=route_key,
            pane_kind="issues",
            project_key=project_key,
            task_id=task_id,
        )

    if sub_view == "task" and route.task_num:
        task_id = f"{project_key}/{route.task_num}"
        return _textual_command_pane(
            route_key=route_key,
            selected_key=route_key,
            pane_kind="issues",
            project_key=project_key,
            task_id=task_id,
        )

    if sub_view == "session":
        session_name = _project_session(context, project_key)
        if session_name is not None:
            return LiveAgentPane(
                route_key=route_key,
                selected_key=route_key,
                session_name=session_name,
                project_key=project_key,
                fallback=_textual_command_pane(
                    route_key=route_key,
                    selected_key=f"project:{project_key}:dashboard",
                    pane_kind="project",
                    project_key=project_key,
                ),
            )
        fallback = _textual_command_pane(
            route_key=route_key,
            selected_key=f"project:{project_key}:dashboard",
            pane_kind="project",
            project_key=project_key,
        )
        return FallbackPane(
            route_key=route_key,
            selected_key=fallback.selected_key,
            reason="missing_worker",
            message=(
                f"Project '{project_key}' has no mapped worker/PM session. "
                "Showing the project dashboard instead."
            ),
            fallback=fallback,
        )

    return _error_plan(
        route_key=route_key,
        selected_key=route_key,
        reason="unsupported_project_route",
        message=f"Unsupported project route: {route_key}",
    )


def _textual_command_pane(
    *,
    route_key: str,
    selected_key: str,
    pane_kind: str,
    project_key: str | None = None,
    task_id: str | None = None,
) -> TextualCommandPane:
    return TextualCommandPane(
        route_key=route_key,
        selected_key=selected_key,
        pane_kind=pane_kind,
        project_key=project_key,
        task_id=task_id,
        command_args=_command_args(pane_kind, project_key, task_id),
    )


def _command_args(
    pane_kind: str,
    project_key: str | None,
    task_id: str | None,
) -> tuple[str, ...]:
    args = ["cockpit-pane", pane_kind]
    if project_key is not None:
        if pane_kind in {"activity", "inbox"}:
            args.extend(["--project", project_key])
        else:
            args.append(project_key)
    if task_id is not None:
        args.extend(["--task", task_id])
    return tuple(args)


def _project_exists(context: CockpitContentContext, project_key: str) -> bool:
    return context.project_keys is None or project_key in context.project_keys


def _project_session(
    context: CockpitContentContext,
    project_key: str,
) -> str | None:
    session_name = context.project_sessions.get(project_key)
    if not isinstance(session_name, str):
        return None
    session_name = session_name.strip()
    return session_name or None


def _missing_project_plan(route_key: str, project_key: str) -> ErrorPane:
    return _error_plan(
        route_key=route_key,
        selected_key=route_key,
        reason="missing_project",
        message=f"Project '{project_key}' is not registered in this workspace.",
    )


def _error_plan(
    *,
    route_key: str,
    selected_key: str,
    reason: str,
    message: str,
) -> ErrorPane:
    return ErrorPane(
        route_key=route_key,
        selected_key=selected_key,
        reason=reason,
        title="Unable to open pane",
        message=message,
    )


def _normalize_route_key(key: str) -> str:
    return str(key or "").strip()


__all__ = [
    "CockpitContentContext",
    "ErrorPane",
    "FallbackPane",
    "LiveAgentPane",
    "LoadingPane",
    "PaneContentKind",
    "RightPaneContentPlan",
    "StaticCommandPane",
    "TextualCommandPane",
    "loading_content_plan",
    "resolve_cockpit_content",
    "resolve_right_pane_content",
]
