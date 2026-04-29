import asyncio

from pollypm.cockpit_navigation import (
    NavigationCommand,
    NavigationContent,
    NavigationController,
    NavigationTransition,
)


def _run(coro):
    return asyncio.run(coro)


class FakeStateStore:
    def __init__(self) -> None:
        self.history: list[NavigationTransition] = []
        self.by_request: dict[int, NavigationTransition] = {}

    def record(self, result: NavigationTransition) -> None:
        self.history.append(result)
        self.by_request[result.request_id] = result

    def states_for(self, request_id: int) -> list[str]:
        return [
            result.state
            for result in self.history
            if result.request_id == request_id
        ]


class ManualResolver:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []
        self._futures: dict[int, asyncio.Future[object]] = {}

    async def resolve(self, request: NavigationCommand) -> object:
        self.calls.append((request.request_id, request.key))
        future = asyncio.get_running_loop().create_future()
        self._futures[request.request_id] = future
        return await future

    async def wait_for_request(self, request_id: int) -> None:
        while request_id not in self._futures:
            await asyncio.sleep(0)

    def complete(self, request_id: int, content: object) -> None:
        self._futures[request_id].set_result(content)


class StaticResolver:
    def __init__(self, content: object | None = None) -> None:
        self.content = content or NavigationContent("resolved")
        self.calls: list[tuple[int, str]] = []

    async def resolve(self, request: NavigationCommand) -> object:
        self.calls.append((request.request_id, request.key))
        return self.content


class FailingResolver:
    async def resolve(self, _request: NavigationCommand) -> object:
        raise RuntimeError("resolver exploded")


class NeverResolver:
    async def resolve(self, _request: NavigationCommand) -> object:
        await asyncio.Event().wait()
        return NavigationContent("never")


class FakeWindowManager:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str, str]] = []

    async def apply(self, request: NavigationCommand, content: object) -> object:
        destination = getattr(content, "destination_key", str(content))
        self.calls.append((request.request_id, request.key, destination))
        return {"shown": destination}


class FailingWindowManager:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    async def apply(self, request: NavigationCommand, _content: object) -> object:
        self.calls.append((request.request_id, request.key))
        raise RuntimeError("window exploded")


def test_rapid_clicks_end_on_final_destination() -> None:
    async def exercise() -> None:
        store = FakeStateStore()
        resolver = ManualResolver()
        window = FakeWindowManager()
        controller = NavigationController(
            state_store=store,
            content_resolver=resolver,
            window_manager=window,
        )

        first = controller.accept("project:demo")
        first_task = asyncio.create_task(controller.resolve_and_apply(first))
        await resolver.wait_for_request(first.request_id)

        second = controller.accept("polly")
        second_task = asyncio.create_task(controller.resolve_and_apply(second))
        await resolver.wait_for_request(second.request_id)

        resolver.complete(second.request_id, NavigationContent("polly"))
        second_result = await second_task
        resolver.complete(first.request_id, NavigationContent("project:demo:dashboard"))
        first_result = await first_task

        assert first_result.state == "stale"
        assert second_result.state == "applied"
        assert second_result.destination_key == "polly"
        assert controller.current_result == second_result
        assert store.states_for(first.request_id) == [
            "accepted",
            "loading",
            "cancelled",
            "stale",
        ]

    _run(exercise())


def test_older_completion_cannot_apply_after_newer_request() -> None:
    async def exercise() -> None:
        store = FakeStateStore()
        resolver = ManualResolver()
        window = FakeWindowManager()
        controller = NavigationController(
            state_store=store,
            content_resolver=resolver,
            window_manager=window,
        )

        first = controller.accept("inbox")
        first_task = asyncio.create_task(controller.resolve_and_apply(first))
        await resolver.wait_for_request(first.request_id)

        second = controller.accept("workers")
        second_task = asyncio.create_task(controller.resolve_and_apply(second))
        await resolver.wait_for_request(second.request_id)

        resolver.complete(first.request_id, NavigationContent("inbox"))
        first_result = await first_task
        resolver.complete(second.request_id, NavigationContent("workers"))
        second_result = await second_task

        assert first_result.state == "stale"
        assert second_result.state == "applied"
        assert window.calls == [(second.request_id, "workers", "workers")]

    _run(exercise())


def test_resolver_error_becomes_visible_error_result() -> None:
    async def exercise() -> None:
        store = FakeStateStore()
        window = FakeWindowManager()
        controller = NavigationController(
            state_store=store,
            content_resolver=FailingResolver(),
            window_manager=window,
        )

        result = await controller.navigate("metrics")

        assert result.state == "failed"
        assert result.error == "resolver exploded"
        assert result.message is not None
        assert "resolver exploded" in result.message
        assert controller.current_result == result
        assert window.calls == []

    _run(exercise())


def test_window_error_becomes_visible_error_result() -> None:
    async def exercise() -> None:
        store = FakeStateStore()
        window = FailingWindowManager()
        controller = NavigationController(
            state_store=store,
            content_resolver=StaticResolver(NavigationContent("settings")),
            window_manager=window,
        )

        result = await controller.navigate("settings")

        assert result.state == "failed"
        assert result.error == "window exploded"
        assert result.message is not None
        assert "window exploded" in result.message
        assert controller.current_result == result
        assert window.calls == [(result.request_id, "settings")]

    _run(exercise())


def test_timeout_produces_timed_out_result() -> None:
    async def exercise() -> None:
        store = FakeStateStore()
        window = FakeWindowManager()
        controller = NavigationController(
            state_store=store,
            content_resolver=NeverResolver(),
            window_manager=window,
            timeout_seconds=0.01,
        )

        result = await controller.navigate("activity")

        assert result.state == "timed_out"
        assert result.error == "timed out"
        assert result.message is not None
        assert "timed out" in result.message
        assert controller.current_result == result
        assert window.calls == []

    _run(exercise())


def test_acknowledgement_happens_before_resolver_and_window_work() -> None:
    async def exercise() -> None:
        store = FakeStateStore()
        events: list[tuple[str, list[str]]] = []

        class ObservingResolver:
            async def resolve(self, _request: NavigationCommand) -> object:
                events.append(("resolver", [result.state for result in store.history]))
                return NavigationContent("dashboard")

        class ObservingWindowManager:
            async def apply(
                self,
                _request: NavigationCommand,
                _content: object,
            ) -> object:
                events.append(("window", [result.state for result in store.history]))
                return "shown"

        controller = NavigationController(
            state_store=store,
            content_resolver=ObservingResolver(),
            window_manager=ObservingWindowManager(),
        )

        request = controller.accept("dashboard")

        assert store.states_for(request.request_id) == ["accepted", "loading"]

        result = await controller.resolve_and_apply(request)

        assert result.state == "applied"
        assert events == [
            ("resolver", ["accepted", "loading"]),
            ("window", ["accepted", "loading"]),
        ]

    _run(exercise())
