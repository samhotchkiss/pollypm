"""Lightweight client for right-pane initiated cockpit navigation.

Right-pane apps currently construct ``CockpitRouter`` directly for jumps such
as ``inbox:<project>``, ``activity:<project>``, and
``project:<project>:issues``. This module is the replacement boundary: apps can
submit typed navigation requests to an active cockpit owner without importing
Textual apps, creating a router, or touching tmux.

Integration points for later wiring:

* The cockpit owner can install :class:`DirectCockpitNavigationAdapter` with a
  callback that hands ``request.navigation`` to the owner-side routing path.
* Out-of-process right-pane apps can use :class:`FileCockpitNavigationQueue`;
  the cockpit owner can poll :meth:`FileCockpitNavigationQueue.pending` and
  apply the newest request through the same owner-side routing path.
* Standalone pane processes can keep the default fallback and surface the
  unsupported result instead of trying to create their own router.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol, TypeAlias, cast

from pollypm.atomic_io import atomic_write_json
from pollypm.cockpit_contracts import NavigationIntent, NavigationRequest


JsonValue: TypeAlias = (
    str
    | int
    | float
    | bool
    | None
    | list["JsonValue"]
    | dict[str, "JsonValue"]
)
JsonObject: TypeAlias = dict[str, JsonValue]

QUEUE_VERSION = 1
QUEUE_FILE_NAME = "cockpit_navigation_queue.json"
DEFAULT_CLIENT_ID = "right-pane"
DEFAULT_ORIGIN = "right_pane"


class CockpitNavigationClientOutcome(StrEnum):
    """Submission outcome from the client boundary."""

    SUBMITTED = "submitted"
    QUEUED = "queued"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CockpitNavigationClientRequest:
    """Monotonic envelope around the pure cockpit navigation request."""

    request_id: str
    sequence: int
    navigation: NavigationRequest

    @property
    def selected_key(self) -> str:
        return self.navigation.selected_key

    @property
    def origin(self) -> str:
        return self.navigation.origin

    @property
    def project_key(self) -> str | None:
        return self.navigation.project_key

    @property
    def task_id(self) -> str | None:
        return self.navigation.task_id

    @property
    def payload(self) -> Mapping[str, object]:
        return self.navigation.payload

    def to_dict(self) -> JsonObject:
        return {
            "request_id": self.request_id,
            "sequence": self.sequence,
            "navigation": {
                "selected_key": self.navigation.selected_key,
                "intent": self.navigation.intent.value,
                "origin": self.navigation.origin,
                "project_key": self.navigation.project_key,
                "task_id": self.navigation.task_id,
                "payload": _json_object(self.navigation.payload, "payload"),
            },
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "CockpitNavigationClientRequest":
        request_id = _required_string(value.get("request_id"), "request_id")
        sequence = _required_int(value.get("sequence"), "sequence")
        navigation_value = value.get("navigation")
        if not isinstance(navigation_value, Mapping):
            raise ValueError("navigation must be an object")

        intent_value = navigation_value.get("intent", NavigationIntent.SELECT.value)
        try:
            intent = NavigationIntent(str(intent_value))
        except ValueError as exc:
            raise ValueError(f"unknown navigation intent: {intent_value!r}") from exc

        payload_value = navigation_value.get("payload", {})
        if payload_value is None:
            payload: JsonObject = {}
        elif isinstance(payload_value, Mapping):
            payload = _json_object(payload_value, "payload")
        else:
            raise ValueError("payload must be an object")

        project_key = _optional_string(navigation_value.get("project_key"), "project_key")
        task_id = _optional_string(navigation_value.get("task_id"), "task_id")
        return cls(
            request_id=request_id,
            sequence=sequence,
            navigation=NavigationRequest(
                selected_key=_required_string(
                    navigation_value.get("selected_key"),
                    "selected_key",
                ),
                intent=intent,
                origin=_required_string(
                    navigation_value.get("origin", DEFAULT_ORIGIN),
                    "origin",
                ),
                project_key=project_key,
                task_id=task_id,
                payload=payload,
            ),
        )


@dataclass(frozen=True, slots=True)
class CockpitNavigationClientResult:
    """Typed result returned to a right-pane navigation caller."""

    request: CockpitNavigationClientRequest
    outcome: CockpitNavigationClientOutcome
    handled: bool
    message: str
    error: str | None = None
    details: Mapping[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None and self.outcome in {
            CockpitNavigationClientOutcome.SUBMITTED,
            CockpitNavigationClientOutcome.QUEUED,
        }


class CockpitNavigationClientAdapter(Protocol):
    """Adapter surface used by :class:`CockpitNavigationClient`."""

    def submit_navigation_request(
        self,
        request: CockpitNavigationClientRequest,
    ) -> CockpitNavigationClientResult:
        """Submit a navigation request to a cockpit owner or queue."""
        ...


NavigationRequestHandler: TypeAlias = Callable[
    [CockpitNavigationClientRequest],
    CockpitNavigationClientResult | Mapping[str, object] | object | None,
]


class DirectCockpitNavigationAdapter:
    """In-process adapter for the active cockpit owner."""

    def __init__(self, handler: NavigationRequestHandler) -> None:
        self._handler = handler

    def submit_navigation_request(
        self,
        request: CockpitNavigationClientRequest,
    ) -> CockpitNavigationClientResult:
        response = self._handler(request)
        if isinstance(response, CockpitNavigationClientResult):
            return response
        details = {"owner_result": response} if response is not None else {}
        return CockpitNavigationClientResult(
            request=request,
            outcome=CockpitNavigationClientOutcome.SUBMITTED,
            handled=True,
            message=f"Navigation request submitted for {request.selected_key}.",
            details=details,
        )


class StandaloneCockpitNavigationAdapter:
    """Safe fallback when no cockpit owner is available."""

    def __init__(self) -> None:
        self.history: list[CockpitNavigationClientRequest] = []

    def submit_navigation_request(
        self,
        request: CockpitNavigationClientRequest,
    ) -> CockpitNavigationClientResult:
        self.history.append(request)
        return CockpitNavigationClientResult(
            request=request,
            outcome=CockpitNavigationClientOutcome.UNSUPPORTED,
            handled=False,
            message=(
                "Cockpit navigation is unsupported because no active cockpit "
                f"owner is connected for {request.selected_key}."
            ),
        )


class FileCockpitNavigationQueue:
    """JSON state-file backed queue for cross-process navigation requests."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def submit_navigation_request(
        self,
        request: CockpitNavigationClientRequest,
    ) -> CockpitNavigationClientResult:
        state = self._load_state()
        requests = _request_dicts(state.get("requests", []))
        requests.append(request.to_dict())
        state["version"] = QUEUE_VERSION
        state["last_sequence"] = max(
            request.sequence,
            _safe_int(state.get("last_sequence"), default=0),
        )
        state["requests"] = requests
        atomic_write_json(self.path, state)
        return CockpitNavigationClientResult(
            request=request,
            outcome=CockpitNavigationClientOutcome.QUEUED,
            handled=True,
            message=f"Navigation request queued for {request.selected_key}.",
            details={"queue_path": str(self.path)},
        )

    def pending(self) -> tuple[CockpitNavigationClientRequest, ...]:
        state = self._load_state()
        requests: list[CockpitNavigationClientRequest] = []
        for item in _request_dicts(state.get("requests", [])):
            requests.append(CockpitNavigationClientRequest.from_dict(item))
        return tuple(sorted(requests, key=lambda request: request.sequence))

    def clear(self) -> None:
        state = self._load_state()
        state["version"] = QUEUE_VERSION
        state["requests"] = []
        atomic_write_json(self.path, state)

    def _load_state(self) -> JsonObject:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {"version": QUEUE_VERSION, "requests": []}
        if not isinstance(payload, dict):
            return {"version": QUEUE_VERSION, "requests": []}
        state = _json_object(payload, "navigation queue state")
        state.setdefault("version", QUEUE_VERSION)
        state.setdefault("requests", [])
        return state


class CockpitNavigationClient:
    """Submit typed navigation requests from right-pane code."""

    def __init__(
        self,
        adapter: CockpitNavigationClientAdapter | None = None,
        *,
        client_id: str = DEFAULT_CLIENT_ID,
        origin: str = DEFAULT_ORIGIN,
        sequence_start: int = 0,
    ) -> None:
        self.adapter = adapter or StandaloneCockpitNavigationAdapter()
        self.client_id = _clean_token(client_id, "client_id")
        self.origin = _required_string(origin, "origin")
        if sequence_start < 0:
            raise ValueError("sequence_start must be non-negative")
        self._sequence = sequence_start
        self.history: list[CockpitNavigationClientResult] = []

    def navigate(
        self,
        selected_key: str,
        *,
        intent: NavigationIntent = NavigationIntent.SELECT,
        origin: str | None = None,
        project_key: str | None = None,
        task_id: str | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> CockpitNavigationClientResult:
        """Submit an arbitrary cockpit selection key."""
        request = self._build_request(
            selected_key,
            intent=intent,
            origin=origin,
            project_key=project_key,
            task_id=task_id,
            payload=payload,
        )
        try:
            result = self.adapter.submit_navigation_request(request)
        except Exception as exc:  # noqa: BLE001
            result = CockpitNavigationClientResult(
                request=request,
                outcome=CockpitNavigationClientOutcome.FAILED,
                handled=False,
                message=(
                    "Cockpit navigation request failed for "
                    f"{request.selected_key}: {exc}"
                ),
                error=str(exc),
            )
        self.history.append(result)
        return result

    def jump_to_inbox(
        self,
        project_key: str | None = None,
        *,
        payload: Mapping[str, object] | None = None,
    ) -> CockpitNavigationClientResult:
        """Jump to the inbox, optionally scoped to a project."""
        key = "inbox" if project_key is None else f"inbox:{_project_key(project_key)}"
        return self.navigate(
            key,
            project_key=project_key,
            payload=_with_action(payload, "jump_to_inbox"),
        )

    def jump_to_activity(
        self,
        project_key: str | None = None,
        *,
        payload: Mapping[str, object] | None = None,
    ) -> CockpitNavigationClientResult:
        """Jump to the activity feed, optionally scoped to a project."""
        key = "activity" if project_key is None else f"activity:{_project_key(project_key)}"
        return self.navigate(
            key,
            project_key=project_key,
            payload=_with_action(payload, "jump_to_activity"),
        )

    def jump_to_project(
        self,
        project_key: str,
        *,
        view: str | None = "dashboard",
        task_number: str | int | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> CockpitNavigationClientResult:
        """Jump to a project route such as dashboard, issues, or a task."""
        clean_project = _project_key(project_key)
        clean_view = None if view is None else _required_string(view, "view")
        selected_key = _project_route_key(clean_project, clean_view, task_number)
        task_id = (
            f"{clean_project}/{task_number}"
            if task_number is not None
            else None
        )
        return self.navigate(
            selected_key,
            project_key=clean_project,
            task_id=task_id,
            payload=_with_action(payload, "jump_to_project"),
        )

    def _build_request(
        self,
        selected_key: str,
        *,
        intent: NavigationIntent,
        origin: str | None,
        project_key: str | None,
        task_id: str | None,
        payload: Mapping[str, object] | None,
    ) -> CockpitNavigationClientRequest:
        sequence = self._next_sequence()
        navigation = NavigationRequest(
            selected_key=_required_string(selected_key, "selected_key"),
            intent=intent,
            origin=_required_string(origin or self.origin, "origin"),
            project_key=project_key,
            task_id=task_id,
            payload={} if payload is None else dict(payload),
        )
        return CockpitNavigationClientRequest(
            request_id=f"{self.client_id}-{sequence:08d}",
            sequence=sequence,
            navigation=navigation,
        )

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence


def cockpit_navigation_queue_path(config_path: Path) -> Path:
    """Return the project-local cross-process cockpit navigation queue path."""
    from pollypm.config import load_config

    config = load_config(config_path)
    config.project.base_dir.mkdir(parents=True, exist_ok=True)
    return config.project.base_dir / QUEUE_FILE_NAME


def file_navigation_client(
    config_path: Path,
    *,
    client_id: str = DEFAULT_CLIENT_ID,
    origin: str = DEFAULT_ORIGIN,
) -> CockpitNavigationClient:
    """Build a file-queue backed client for right-pane cockpit apps."""
    return CockpitNavigationClient(
        FileCockpitNavigationQueue(cockpit_navigation_queue_path(config_path)),
        client_id=client_id,
        origin=origin,
    )


def _project_route_key(
    project_key: str,
    view: str | None,
    task_number: str | int | None,
) -> str:
    if view is None:
        return f"project:{project_key}"
    if task_number is None:
        return f"project:{project_key}:{view}"
    clean_task = _required_string(str(task_number), "task_number")
    if view == "issues":
        return f"project:{project_key}:issues:task:{clean_task}"
    if view == "task":
        return f"project:{project_key}:task:{clean_task}"
    return f"project:{project_key}:{view}:task:{clean_task}"


def _with_action(
    payload: Mapping[str, object] | None,
    action: str,
) -> Mapping[str, object]:
    merged: dict[str, object] = dict(payload or {})
    merged.setdefault("action", action)
    return merged


def _request_dicts(value: JsonValue) -> list[JsonObject]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _json_object(value: Mapping[str, object], label: str) -> JsonObject:
    copied: JsonObject = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise TypeError(f"{label} keys must be strings")
        if not _is_json_value(item):
            raise TypeError(f"{label} contains a non-JSON value")
        copied[key] = cast(JsonValue, item)
    return copied


def _is_json_value(value: object) -> bool:
    if value is None or isinstance(value, str | bool | int):
        return True
    if isinstance(value, float):
        return value == value and value not in {float("inf"), float("-inf")}
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _is_json_value(item)
            for key, item in value.items()
        )
    return False


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, label)


def _required_int(value: object, label: str) -> int:
    if not isinstance(value, int):
        raise ValueError(f"{label} must be an int")
    return value


def _safe_int(value: object, *, default: int) -> int:
    return value if isinstance(value, int) else default


def _project_key(value: str) -> str:
    project_key = _required_string(value, "project_key")
    if ":" in project_key:
        raise ValueError("project_key must not contain ':'")
    return project_key


def _clean_token(value: str, label: str) -> str:
    token = _required_string(value, label)
    return "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in token)


__all__ = [
    "CockpitNavigationClient",
    "CockpitNavigationClientAdapter",
    "CockpitNavigationClientOutcome",
    "CockpitNavigationClientRequest",
    "CockpitNavigationClientResult",
    "DirectCockpitNavigationAdapter",
    "FileCockpitNavigationQueue",
    "StandaloneCockpitNavigationAdapter",
    "cockpit_navigation_queue_path",
    "file_navigation_client",
]
