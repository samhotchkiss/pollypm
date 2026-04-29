"""Typed contracts for the modular cockpit refactor (#969).

The target cockpit shape is:

    rail UI -> navigation state machine -> content resolver -> window manager

This module declares the typed records passed across those boundaries. It is
intentionally pure: no tmux client construction, no Textual imports, no
Supervisor imports, and no process IO. Runtime code can adopt these contracts
incrementally without changing today's cockpit behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping, Protocol


class NavigationIntent(StrEnum):
    """Why the rail is asking the cockpit to route."""

    SELECT = "select"
    REFRESH = "refresh"
    FOCUS_RIGHT = "focus_right"
    SEND_KEY = "send_key"
    RESTORE = "restore"


class NavigationOutcome(StrEnum):
    """State-machine result before any terminal mutation occurs."""

    NOOP = "noop"
    SHOW_CONTENT = "show_content"
    MOUNT_CONTENT = "mount_content"
    FOCUS_RIGHT = "focus_right"
    ERROR = "error"


class ContentPlanKind(StrEnum):
    """Kinds of right-pane content the resolver may request."""

    STATIC_VIEW = "static_view"
    LIVE_SESSION = "live_session"
    PROJECT_VIEW = "project_view"
    TASK_VIEW = "task_view"
    LAUNCH_STREAM = "launch_stream"


class RightPaneLifecycleState(StrEnum):
    """Lifecycle states for the cockpit right pane.

    These are deliberately higher-level than tmux. The window manager owns
    translating lifecycle transitions into terminal operations.
    """

    UNMOUNTED = "unmounted"
    INITIALIZING = "initializing"
    STATIC_VIEW = "static_view"
    LIVE_SESSION = "live_session"
    LAUNCH_STREAM = "launch_stream"
    STALE = "stale"
    DETACHING = "detaching"
    ERROR = "error"


class MountDisposition(StrEnum):
    """What the window manager did with a content plan."""

    UNCHANGED = "unchanged"
    MOUNTED = "mounted"
    REMOUNTED = "remounted"
    DETACHED = "detached"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class NavigationRequest:
    """Rail/UI request entering the navigation state machine."""

    selected_key: str
    intent: NavigationIntent = NavigationIntent.SELECT
    origin: str = "rail"
    project_key: str | None = None
    task_id: str | None = None
    payload: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ContentPlan:
    """Resolver output consumed by the cockpit window manager.

    ``command`` is a declarative argv tuple. The resolver may name what should
    be shown, but only the window manager may decide how to materialize it.
    """

    kind: ContentPlanKind
    key: str
    title: str
    selected_key: str | None = None
    session_name: str | None = None
    project_key: str | None = None
    task_id: str | None = None
    command: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NavigationResult:
    """State-machine response to a :class:`NavigationRequest`."""

    request: NavigationRequest
    outcome: NavigationOutcome
    active_key: str | None = None
    content_plan: ContentPlan | None = None
    lifecycle_state: RightPaneLifecycleState | None = None
    reason: str = ""
    diagnostics: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PaneSnapshot:
    """Pure data snapshot of a pane inside a cockpit window."""

    pane_id: str
    command: str = ""
    current_path: str | None = None
    active: bool = False
    is_dead: bool = False
    left: int | None = None
    top: int | None = None
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True, slots=True)
class WindowSnapshot:
    """Pure data snapshot of the cockpit window shape."""

    session_name: str
    window_name: str
    target: str
    window_index: int | None = None
    width: int | None = None
    height: int | None = None
    active_pane_id: str | None = None
    right_pane_id: str | None = None
    panes: tuple[PaneSnapshot, ...] = ()


@dataclass(frozen=True, slots=True)
class RightPaneLifecycle:
    """Persistable lifecycle record for the cockpit right pane."""

    state: RightPaneLifecycleState
    content_key: str | None = None
    plan_kind: ContentPlanKind | None = None
    pane_id: str | None = None
    mounted_session: str | None = None
    project_key: str | None = None
    task_id: str | None = None
    updated_at: str | None = None
    detail: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MountResult:
    """Window-manager response after applying a :class:`ContentPlan`."""

    disposition: MountDisposition
    lifecycle: RightPaneLifecycle
    plan: ContentPlan | None = None
    snapshot: WindowSnapshot | None = None
    right_pane_id: str | None = None
    message: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.disposition is not MountDisposition.FAILED


class CockpitNavigationStateMachine(Protocol):
    """Boundary between rail events and route decisions."""

    def navigate(
        self,
        request: NavigationRequest,
        *,
        current: RightPaneLifecycle | None = None,
    ) -> NavigationResult:
        """Return the next navigation result without terminal side effects."""
        ...


class CockpitContentResolver(Protocol):
    """Boundary between route decisions and right-pane content plans."""

    def resolve_content(self, navigation: NavigationResult) -> ContentPlan:
        """Convert a navigation result into a content plan."""
        ...


class CockpitWindowManager(Protocol):
    """Boundary that owns cockpit window and right-pane mechanics."""

    def capture(self) -> WindowSnapshot:
        """Return the current cockpit window snapshot."""
        ...

    def mount_content(
        self,
        plan: ContentPlan,
        *,
        current: RightPaneLifecycle | None = None,
    ) -> MountResult:
        """Apply a content plan to the right pane."""
        ...

    def release_right_pane(self, *, reason: str = "") -> MountResult:
        """Detach or reset the right pane according to implementation policy."""
        ...


class CockpitRightPaneLifecycleStore(Protocol):
    """Persistence boundary for right-pane lifecycle state."""

    def load_lifecycle(self) -> RightPaneLifecycle:
        """Return the last persisted right-pane lifecycle record."""
        ...

    def save_lifecycle(self, lifecycle: RightPaneLifecycle) -> None:
        """Persist a right-pane lifecycle record."""
        ...


__all__ = [
    "CockpitContentResolver",
    "CockpitNavigationStateMachine",
    "CockpitRightPaneLifecycleStore",
    "CockpitWindowManager",
    "ContentPlan",
    "ContentPlanKind",
    "MountDisposition",
    "MountResult",
    "NavigationIntent",
    "NavigationOutcome",
    "NavigationRequest",
    "NavigationResult",
    "PaneSnapshot",
    "RightPaneLifecycle",
    "RightPaneLifecycleState",
    "WindowSnapshot",
]
