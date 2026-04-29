"""Pure navigation state machine for cockpit rail selections.

The rail should acknowledge clicks immediately, then let slower content
resolution and pane/window work happen behind a request-id guard. This module
keeps that sequencing independent of Textual, tmux, and the cockpit router so
it can be tested directly.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Literal, Protocol


NavigationState = Literal[
    "accepted",
    "loading",
    "applied",
    "cancelled",
    "timed_out",
    "failed",
    "stale",
]


@dataclass(frozen=True, slots=True)
class NavigationCommand:
    request_id: int
    key: str


@dataclass(frozen=True, slots=True)
class NavigationContent:
    destination_key: str
    payload: object | None = None


@dataclass(frozen=True, slots=True)
class NavigationTransition:
    request_id: int
    key: str
    state: NavigationState
    destination_key: str | None = None
    content: object | None = None
    window_result: object | None = None
    message: str | None = None
    error: str | None = None
    superseded_by: int | None = None


class NavigationStateStore(Protocol):
    def record(self, result: NavigationTransition) -> None:
        ...


class NavigationContentResolver(Protocol):
    def resolve(self, request: NavigationCommand) -> object | Awaitable[object]:
        ...


class NavigationWindowManager(Protocol):
    def apply(
        self,
        request: NavigationCommand,
        content: object,
    ) -> object | Awaitable[object]:
        ...


class InMemoryNavigationStateStore:
    """Small default store useful for tests and simple integrations."""

    def __init__(self) -> None:
        self.history: list[NavigationTransition] = []
        self.by_request: dict[int, NavigationTransition] = {}

    def record(self, result: NavigationTransition) -> None:
        self.history.append(result)
        self.by_request[result.request_id] = result


class NavigationController:
    """Accept rail clicks and apply only the newest request's completion."""

    def __init__(
        self,
        *,
        state_store: NavigationStateStore,
        content_resolver: NavigationContentResolver,
        window_manager: NavigationWindowManager,
        timeout_seconds: float | None = None,
    ) -> None:
        if timeout_seconds is not None and timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        self._state_store = state_store
        self._content_resolver = content_resolver
        self._window_manager = window_manager
        self._timeout_seconds = timeout_seconds
        self._next_request_id = 0
        self._current_request_id: int | None = None
        self._requests: dict[int, NavigationCommand] = {}
        self._results: dict[int, NavigationTransition] = {}

    @property
    def current_request_id(self) -> int | None:
        return self._current_request_id

    @property
    def current_result(self) -> NavigationTransition | None:
        if self._current_request_id is None:
            return None
        return self._results.get(self._current_request_id)

    def result_for(self, request_id: int) -> NavigationTransition | None:
        return self._results.get(request_id)

    def accept(self, key: str) -> NavigationCommand:
        """Synchronously acknowledge a rail click before slow work starts."""

        self._next_request_id += 1
        request = NavigationCommand(request_id=self._next_request_id, key=key)
        previous_id = self._current_request_id

        if previous_id is not None and self._is_active(previous_id):
            previous = self._requests[previous_id]
            self._record(
                previous,
                "cancelled",
                destination_key=previous.key,
                message=f"Superseded by request {request.request_id}.",
                superseded_by=request.request_id,
            )

        self._requests[request.request_id] = request
        self._current_request_id = request.request_id
        self._record(request, "accepted", destination_key=key)
        self._record(request, "loading", destination_key=key)
        return request

    def cancel(self, request_id: int | None = None) -> NavigationTransition | None:
        """Mark a pending request as cancelled.

        Cancelling is separate from staleness: a newer accepted request marks
        the old one cancelled immediately, and a late old completion is then
        recorded as stale when it tries to finish.
        """

        resolved_request_id = request_id or self._current_request_id
        if resolved_request_id is None:
            return None
        request = self._requests.get(resolved_request_id)
        if request is None:
            return None
        existing = self._results.get(resolved_request_id)
        if existing is not None and existing.state in {
            "applied",
            "timed_out",
            "failed",
            "stale",
        }:
            return existing
        return self._record(
            request,
            "cancelled",
            destination_key=request.key,
            message="Navigation cancelled.",
        )

    async def navigate(self, key: str) -> NavigationTransition:
        """Accept, resolve, and apply a rail navigation request."""

        request = self.accept(key)
        return await self.resolve_and_apply(request)

    async def resolve_and_apply(
        self,
        request: NavigationCommand,
    ) -> NavigationTransition:
        """Resolve content and apply it if ``request`` is still newest."""

        inactive = self._inactive_result(request)
        if inactive is not None:
            return inactive

        try:
            if self._timeout_seconds is None:
                return await self._resolve_and_apply_without_timeout(request)
            return await asyncio.wait_for(
                self._resolve_and_apply_without_timeout(request),
                timeout=self._timeout_seconds,
            )
        except TimeoutError:
            return self._finish_or_stale(
                request,
                "timed_out",
                destination_key=request.key,
                message=f"Navigation to {request.key} timed out.",
                error="timed out",
            )
        except Exception as exc:  # noqa: BLE001
            return self._finish_or_stale(
                request,
                "failed",
                destination_key=request.key,
                message=f"Navigation to {request.key} failed: {exc}",
                error=str(exc),
            )

    async def _resolve_and_apply_without_timeout(
        self,
        request: NavigationCommand,
    ) -> NavigationTransition:
        content = await _maybe_await(self._content_resolver.resolve(request))

        inactive = self._inactive_result(request)
        if inactive is not None:
            return inactive

        destination_key = _destination_key(request, content)
        window_result = await _maybe_await(self._window_manager.apply(request, content))

        inactive = self._inactive_result(request)
        if inactive is not None:
            return inactive

        return self._record(
            request,
            "applied",
            destination_key=destination_key,
            content=content,
            window_result=window_result,
        )

    def _inactive_result(self, request: NavigationCommand) -> NavigationTransition | None:
        existing = self._results.get(request.request_id)
        if request.request_id != self._current_request_id:
            return self._record(
                request,
                "stale",
                destination_key=request.key,
                message="Navigation completion ignored because a newer request exists.",
                superseded_by=self._current_request_id,
            )
        if existing is not None and existing.state == "cancelled":
            return existing
        return None

    def _finish_or_stale(
        self,
        request: NavigationCommand,
        state: Literal["timed_out", "failed"],
        *,
        destination_key: str,
        message: str,
        error: str,
    ) -> NavigationTransition:
        inactive = self._inactive_result(request)
        if inactive is not None:
            return inactive
        return self._record(
            request,
            state,
            destination_key=destination_key,
            message=message,
            error=error,
        )

    def _is_active(self, request_id: int) -> bool:
        result = self._results.get(request_id)
        return result is not None and result.state in {"accepted", "loading"}

    def _record(
        self,
        request: NavigationCommand,
        state: NavigationState,
        *,
        destination_key: str | None = None,
        content: object | None = None,
        window_result: object | None = None,
        message: str | None = None,
        error: str | None = None,
        superseded_by: int | None = None,
    ) -> NavigationTransition:
        result = NavigationTransition(
            request_id=request.request_id,
            key=request.key,
            state=state,
            destination_key=destination_key,
            content=content,
            window_result=window_result,
            message=message,
            error=error,
            superseded_by=superseded_by,
        )
        self._results[request.request_id] = result
        self._state_store.record(result)
        return result


async def _maybe_await(value: object | Awaitable[object]) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


def _destination_key(request: NavigationCommand, content: object) -> str:
    if isinstance(content, NavigationContent):
        return content.destination_key
    if isinstance(content, str):
        return content
    destination_key = getattr(content, "destination_key", None)
    if isinstance(destination_key, str) and destination_key:
        return destination_key
    return request.key


__all__ = [
    "InMemoryNavigationStateStore",
    "NavigationCommand",
    "NavigationContent",
    "NavigationContentResolver",
    "NavigationController",
    "NavigationState",
    "NavigationStateStore",
    "NavigationTransition",
    "NavigationWindowManager",
]
