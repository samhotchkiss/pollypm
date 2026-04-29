from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path

from pollypm.cockpit_content import (
    CockpitContentContext,
    ErrorPane,
    FallbackPane,
    LiveAgentPane,
    RightPaneContentPlan,
    TextualCommandPane,
    resolve_cockpit_content,
)
from pollypm.cockpit_contracts import PaneSnapshot, WindowSnapshot
from pollypm.cockpit_navigation import (
    NavigationCommand,
    NavigationController,
    NavigationTransition,
)
from pollypm.cockpit_state_store import CockpitStateStore


def _run(coro: Awaitable[None]) -> None:
    asyncio.run(coro)


@dataclass(frozen=True, slots=True)
class ResolvedPaneContent:
    route_key: str
    destination_key: str
    plan: RightPaneContentPlan


@dataclass(frozen=True, slots=True)
class VisiblePane:
    request_id: int
    destination_key: str
    pane_kind: str
    right_pane_state: str
    title: str
    message: str
    command_args: tuple[str, ...] = ()
    session_name: str | None = None


class ManualContentResolver:
    """Content resolver fake with manual completion for out-of-order clicks."""

    def __init__(self, context: CockpitContentContext) -> None:
        self.context = context
        self.calls: list[tuple[int, str]] = []
        self._futures: dict[int, asyncio.Future[str | BaseException | None]] = {}

    async def resolve(self, request: NavigationCommand) -> ResolvedPaneContent:
        self.calls.append((request.request_id, request.key))
        future = asyncio.get_running_loop().create_future()
        self._futures[request.request_id] = future
        route_key_or_error = await future
        if isinstance(route_key_or_error, BaseException):
            raise route_key_or_error
        route_key = route_key_or_error or request.key
        plan = resolve_cockpit_content(route_key, self.context)
        return ResolvedPaneContent(
            route_key=route_key,
            destination_key=getattr(plan, "selected_key", route_key),
            plan=plan,
        )

    async def wait_for_request(self, request_id: int) -> None:
        while request_id not in self._futures:
            await asyncio.sleep(0)

    def complete(
        self,
        request_id: int,
        route_key: str | BaseException | None = None,
    ) -> None:
        self._futures[request_id].set_result(route_key)


class ImmediateContentResolver:
    def __init__(self, context: CockpitContentContext) -> None:
        self.context = context

    async def resolve(self, request: NavigationCommand) -> ResolvedPaneContent:
        plan = resolve_cockpit_content(request.key, self.context)
        return ResolvedPaneContent(
            route_key=request.key,
            destination_key=getattr(plan, "selected_key", request.key),
            plan=plan,
        )


class ModularStateStore:
    """Navigation history plus persisted right-pane state for the harness."""

    def __init__(self, path: Path) -> None:
        self.persisted = CockpitStateStore(path)
        self.history: list[NavigationTransition] = []
        self.by_request: dict[int, NavigationTransition] = {}

    def record(self, result: NavigationTransition) -> None:
        self.history.append(result)
        self.by_request[result.request_id] = result

        request_id = str(result.request_id)
        if result.state == "accepted":
            self.persisted.set_selected_key(result.key)
            self.persisted.set_active_request_id(request_id)
        elif result.state == "loading":
            self.persisted.mark_right_pane_loading(request_id)
        elif result.state == "applied":
            self._record_applied(result)
        elif result.state in {"failed", "timed_out"}:
            self.persisted.mark_right_pane_error(result.error or result.message)

    def is_active(self, request_id: int) -> bool:
        return self.persisted.active_request_id() == str(request_id)

    def states_for(self, request_id: int) -> list[str]:
        return [
            result.state
            for result in self.history
            if result.request_id == request_id
        ]

    def _record_applied(self, result: NavigationTransition) -> None:
        content = result.content
        plan = content.plan if isinstance(content, ResolvedPaneContent) else None
        destination_key = result.destination_key or result.key
        self.persisted.set_selected_key(destination_key)
        if isinstance(plan, LiveAgentPane):
            self.persisted.set_mounted_identity(
                {
                    "rail_key": result.key,
                    "session_name": plan.session_name,
                    "right_pane_id": "%right",
                }
            )
            self.persisted.set_right_pane_id("%right")
            self.persisted.mark_right_pane_live_agent()
        elif isinstance(plan, ErrorPane):
            self.persisted.mark_right_pane_error(plan.message)
        else:
            self.persisted.clear_mounted_identity()
            self.persisted.set_right_pane_id("%right")
            self.persisted.mark_right_pane_static()


class DeterministicWindowManager:
    """Non-tmux right-pane model used as the modular verification boundary."""

    def __init__(self, state_store: ModularStateStore) -> None:
        self.state_store = state_store
        self.visible_pane: VisiblePane | None = None
        self.applied_request_ids: list[int] = []
        self.rejected_request_ids: list[int] = []
        self._held: dict[int, asyncio.Future[None]] = {}

    async def apply(
        self,
        request: NavigationCommand,
        content: ResolvedPaneContent,
    ) -> VisiblePane | str:
        future = self._held.get(request.request_id)
        if future is not None:
            await future
        if not self.state_store.is_active(request.request_id):
            self.rejected_request_ids.append(request.request_id)
            return "stale-window-worker"

        visible = self._render(request, content)
        self.visible_pane = visible
        self.applied_request_ids.append(request.request_id)
        self.assert_pane_invariants()
        return visible

    def hold(self, request_id: int) -> None:
        self._held[request_id] = asyncio.get_running_loop().create_future()

    def release(self, request_id: int) -> None:
        self._held[request_id].set_result(None)

    def snapshot(self) -> WindowSnapshot:
        right_command = "pm"
        if self.visible_pane and self.visible_pane.session_name:
            right_command = "codex"
        return WindowSnapshot(
            session_name="pollypm-test",
            window_name="PollyPM",
            target="pollypm-test:PollyPM",
            active_pane_id="%rail",
            right_pane_id="%right",
            panes=(
                PaneSnapshot(
                    pane_id="%rail",
                    command="uv",
                    active=True,
                    left=0,
                    top=0,
                    width=30,
                    height=40,
                ),
                PaneSnapshot(
                    pane_id="%right",
                    command=right_command,
                    left=31,
                    top=0,
                    width=100,
                    height=40,
                ),
            ),
        )

    def assert_pane_invariants(self) -> None:
        snapshot = self.snapshot()
        assert snapshot.right_pane_id == "%right"
        assert len(snapshot.panes) == 2
        rail, right = snapshot.panes
        assert rail.pane_id == "%rail"
        assert right.pane_id == "%right"
        assert rail.left == 0
        assert right.left is not None and right.left > (rail.left or 0)
        assert rail.command == "uv"
        assert right.command in {"pm", "codex"}

    @staticmethod
    def _render(
        request: NavigationCommand,
        content: ResolvedPaneContent,
    ) -> VisiblePane:
        plan = content.plan
        if isinstance(plan, ErrorPane):
            return VisiblePane(
                request_id=request.request_id,
                destination_key=content.destination_key,
                pane_kind="error",
                right_pane_state=plan.right_pane_state,
                title=plan.title,
                message=plan.message,
            )
        if isinstance(plan, FallbackPane):
            return VisiblePane(
                request_id=request.request_id,
                destination_key=content.destination_key,
                pane_kind="fallback",
                right_pane_state=plan.right_pane_state,
                title="Fallback",
                message=plan.message,
                command_args=plan.fallback.command_args,
            )
        if isinstance(plan, LiveAgentPane):
            return VisiblePane(
                request_id=request.request_id,
                destination_key=content.destination_key,
                pane_kind="live_agent",
                right_pane_state=plan.right_pane_state,
                title=plan.session_name,
                message=f"Mounted live session {plan.session_name}.",
                session_name=plan.session_name,
            )
        if isinstance(plan, TextualCommandPane):
            return VisiblePane(
                request_id=request.request_id,
                destination_key=content.destination_key,
                pane_kind=plan.pane_kind,
                right_pane_state=plan.right_pane_state,
                title=plan.pane_kind,
                message="Rendered static cockpit pane.",
                command_args=plan.command_args,
            )
        return VisiblePane(
            request_id=request.request_id,
            destination_key=content.destination_key,
            pane_kind="loading",
            right_pane_state="loading",
            title="Loading",
            message="Loading cockpit pane.",
        )


@dataclass(slots=True)
class ModularHarness:
    """Composable cockpit harness that does not require a live tmux server."""

    state_store: ModularStateStore
    content_resolver: ManualContentResolver | ImmediateContentResolver
    window_manager: DeterministicWindowManager
    navigation: NavigationController

    @classmethod
    def manual(
        cls,
        tmp_path: Path,
        context: CockpitContentContext,
    ) -> ModularHarness:
        state_store = ModularStateStore(tmp_path / "cockpit_state.json")
        content_resolver = ManualContentResolver(context)
        window_manager = DeterministicWindowManager(state_store)
        navigation = NavigationController(
            state_store=state_store,
            content_resolver=content_resolver,
            window_manager=window_manager,
        )
        return cls(state_store, content_resolver, window_manager, navigation)

    @classmethod
    def immediate(
        cls,
        tmp_path: Path,
        context: CockpitContentContext,
    ) -> ModularHarness:
        state_store = ModularStateStore(tmp_path / "cockpit_state.json")
        content_resolver = ImmediateContentResolver(context)
        window_manager = DeterministicWindowManager(state_store)
        navigation = NavigationController(
            state_store=state_store,
            content_resolver=content_resolver,
            window_manager=window_manager,
        )
        return cls(state_store, content_resolver, window_manager, navigation)


def _context_with_worker() -> CockpitContentContext:
    return CockpitContentContext.from_projects(
        ["demo", "other"],
        project_sessions={"demo": "worker_demo"},
    )


def test_rapid_clicks_commit_only_final_selection(tmp_path: Path) -> None:
    async def exercise() -> None:
        harness = ModularHarness.manual(tmp_path, _context_with_worker())

        first = harness.navigation.accept("project:demo:session")
        first_task = asyncio.create_task(
            harness.navigation.resolve_and_apply(first)
        )
        await harness.content_resolver.wait_for_request(first.request_id)

        second = harness.navigation.accept("metrics")
        second_task = asyncio.create_task(
            harness.navigation.resolve_and_apply(second)
        )
        await harness.content_resolver.wait_for_request(second.request_id)

        final = harness.navigation.accept("inbox")
        final_task = asyncio.create_task(
            harness.navigation.resolve_and_apply(final)
        )
        await harness.content_resolver.wait_for_request(final.request_id)

        harness.content_resolver.complete(final.request_id)
        final_result = await final_task
        harness.content_resolver.complete(first.request_id)
        harness.content_resolver.complete(second.request_id)
        first_result = await first_task
        second_result = await second_task

        snapshot = harness.state_store.persisted.snapshot()
        assert first_result.state == "stale"
        assert second_result.state == "stale"
        assert final_result.state == "applied"
        assert snapshot.selected_key == "inbox"
        assert snapshot.right_pane_state == "static"
        assert snapshot.active_request_id is None
        assert harness.window_manager.applied_request_ids == [final.request_id]
        assert harness.window_manager.visible_pane is not None
        assert harness.window_manager.visible_pane.destination_key == "inbox"
        assert harness.state_store.states_for(first.request_id) == [
            "accepted",
            "loading",
            "cancelled",
            "stale",
        ]

    _run(exercise())


def test_stale_window_worker_cannot_overwrite_newer_result(tmp_path: Path) -> None:
    async def exercise() -> None:
        harness = ModularHarness.immediate(tmp_path, _context_with_worker())

        first = harness.navigation.accept("project:demo:session")
        harness.window_manager.hold(first.request_id)
        first_task = asyncio.create_task(
            harness.navigation.resolve_and_apply(first)
        )
        await asyncio.sleep(0)

        second = harness.navigation.accept("dashboard")
        harness.window_manager.hold(second.request_id)
        second_task = asyncio.create_task(
            harness.navigation.resolve_and_apply(second)
        )
        await asyncio.sleep(0)

        harness.window_manager.release(second.request_id)
        second_result = await second_task
        harness.window_manager.release(first.request_id)
        first_result = await first_task

        snapshot = harness.state_store.persisted.snapshot()
        assert second_result.state == "applied"
        assert first_result.state == "stale"
        assert snapshot.selected_key == "dashboard"
        assert snapshot.right_pane_state == "static"
        assert harness.window_manager.applied_request_ids == [second.request_id]
        assert harness.window_manager.rejected_request_ids == [first.request_id]
        assert harness.window_manager.visible_pane is not None
        assert harness.window_manager.visible_pane.destination_key == "dashboard"

    _run(exercise())


def test_visible_error_and_fallback_are_rendered_without_tmux(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        context = CockpitContentContext.from_projects(["demo"])
        harness = ModularHarness.immediate(tmp_path, context)

        error_result = await harness.navigation.navigate("project:missing:dashboard")

        assert error_result.state == "applied"
        assert harness.window_manager.visible_pane is not None
        assert harness.window_manager.visible_pane.pane_kind == "error"
        assert "missing" in harness.window_manager.visible_pane.message
        assert harness.state_store.persisted.right_pane_state() == "error"

        fallback_result = await harness.navigation.navigate("project:demo:session")

        assert fallback_result.state == "applied"
        visible = harness.window_manager.visible_pane
        assert visible is not None
        assert visible.pane_kind == "fallback"
        assert visible.destination_key == "project:demo:dashboard"
        assert "Showing the project dashboard instead." in visible.message
        assert visible.command_args == ("cockpit-pane", "project", "demo")
        snapshot = harness.state_store.persisted.snapshot()
        assert snapshot.selected_key == "project:demo:dashboard"
        assert snapshot.right_pane_state == "static"

    _run(exercise())


def test_deterministic_fake_window_manager_pane_invariants(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        harness = ModularHarness.immediate(tmp_path, _context_with_worker())

        for key in ("dashboard", "project:demo:session", "not-a-route"):
            result = await harness.navigation.navigate(key)
            assert result.state == "applied"
            harness.window_manager.assert_pane_invariants()

        snapshot = harness.window_manager.snapshot()
        rail, right = snapshot.panes
        assert snapshot.active_pane_id == "%rail"
        assert snapshot.right_pane_id == "%right"
        assert rail.pane_id == "%rail"
        assert right.pane_id == "%right"
        assert rail.width == 30
        assert right.width == 100
        assert harness.window_manager.applied_request_ids == [1, 2, 3]

    _run(exercise())
