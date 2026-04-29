"""Typed persistence wrapper for ``cockpit_state.json``.

This module owns only JSON state shape and validation. It intentionally
does not know how to find a project config, talk to tmux, create tmux
clients, or load supervisors; callers pass the exact state-file path.

The #970 persisted right-pane state contract intentionally uses compact
``idle`` / ``loading`` / ``static`` / ``live_agent`` / ``error`` values in
JSON. Public cockpit modules use ``RightPaneLifecycleState``; the explicit
mapping functions in this module are the boundary between those shapes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Union, cast

from pollypm.atomic_io import atomic_write_json
from pollypm.cockpit_contracts import RightPaneLifecycleState


JsonValue = Union[
    str,
    int,
    float,
    bool,
    None,
    list["JsonValue"],
    dict[str, "JsonValue"],
]
MountedIdentityPayload = dict[str, JsonValue]
RightPaneState = Literal["idle", "loading", "static", "live_agent", "error"]

STATE_FILE_NAME = "cockpit_state.json"
DEFAULT_SELECTED_KEY = "polly"
DEFAULT_RAIL_WIDTH = 30
MIN_RAIL_WIDTH = 20
MAX_RAIL_WIDTH = 120
RIGHT_PANE_STATES: frozenset[str] = frozenset(
    {"idle", "loading", "static", "live_agent", "error"}
)
RIGHT_PANE_STATE_TO_LIFECYCLE: dict[RightPaneState, RightPaneLifecycleState] = {
    "idle": RightPaneLifecycleState.UNMOUNTED,
    "loading": RightPaneLifecycleState.INITIALIZING,
    "static": RightPaneLifecycleState.STATIC_VIEW,
    "live_agent": RightPaneLifecycleState.LIVE_SESSION,
    "error": RightPaneLifecycleState.ERROR,
}
LIFECYCLE_TO_RIGHT_PANE_STATE: dict[RightPaneLifecycleState, RightPaneState] = {
    lifecycle: state for state, lifecycle in RIGHT_PANE_STATE_TO_LIFECYCLE.items()
}


def right_pane_state_to_lifecycle(state: RightPaneState) -> RightPaneLifecycleState:
    """Translate the persisted JSON state into the public lifecycle contract."""
    if state not in RIGHT_PANE_STATE_TO_LIFECYCLE:
        _raise_invalid_right_pane_state(state)
    return RIGHT_PANE_STATE_TO_LIFECYCLE[state]


def lifecycle_to_right_pane_state(state: RightPaneLifecycleState) -> RightPaneState:
    """Translate a public lifecycle state into the persisted JSON state."""
    try:
        return LIFECYCLE_TO_RIGHT_PANE_STATE[state]
    except KeyError as exc:
        allowed = ", ".join(item.value for item in RIGHT_PANE_STATE_TO_LIFECYCLE.values())
        raise ValueError(
            f"lifecycle state cannot be persisted as right-pane state: {state}"
            f" (allowed: {allowed})"
        ) from exc


def _raise_invalid_right_pane_state(value: str) -> None:
    allowed = ", ".join(sorted(RIGHT_PANE_STATES))
    raise ValueError(f"right pane state must be one of: {allowed}")


@dataclass(frozen=True, slots=True)
class CockpitStateSnapshot:
    selected_key: str
    active_request_id: str | None
    right_pane_state: RightPaneState
    right_pane_id: str | None
    mounted_identity: MountedIdentityPayload | None
    rail_width: int
    pinned_projects: tuple[str, ...]
    palette_tip_seen: bool


class CockpitStateStore:
    """Read and write typed cockpit state with safe defaults.

    Selection intent (``selected``) is deliberately separate from mounted
    truth (``right_pane_*`` and ``mounted_identity``). Clearing a mounted
    pane must not rewrite the user's current rail selection.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def for_project_dir(cls, project_dir: Path) -> "CockpitStateStore":
        return cls(project_dir / STATE_FILE_NAME)

    def snapshot(self) -> CockpitStateSnapshot:
        state = self._load_raw()
        return CockpitStateSnapshot(
            selected_key=self._selected_key_from(state),
            active_request_id=self._active_request_id_from(state),
            right_pane_state=self._right_pane_state_from(state),
            right_pane_id=self._right_pane_id_from(state),
            mounted_identity=self._mounted_identity_from(state),
            rail_width=self._rail_width_from(state),
            pinned_projects=tuple(self._pinned_projects_from(state)),
            palette_tip_seen=self._palette_tip_seen_from(state),
        )

    def raw_state(self) -> dict[str, JsonValue]:
        """Return the raw JSON object, or ``{}`` for missing/corrupt state."""
        return self._load_raw()

    def selected_key(self) -> str:
        return self._selected_key_from(self._load_raw())

    def set_selected_key(self, key: str) -> None:
        self._require_non_empty_string(key, "selected key")
        self._mutate({"selected": key})

    def active_request_id(self) -> str | None:
        return self._active_request_id_from(self._load_raw())

    def set_active_request_id(self, request_id: str | None) -> None:
        if request_id is None:
            self.clear_active_request_id()
            return
        self._require_non_empty_string(request_id, "active request id")
        self._mutate({"active_request_id": request_id})

    def clear_active_request_id(self) -> None:
        self._mutate(remove=("active_request_id",))

    def right_pane_state(self) -> RightPaneState:
        return self._right_pane_state_from(self._load_raw())

    def set_right_pane_state(self, state: RightPaneState) -> None:
        self._require_right_pane_state(state)
        self._mutate({"right_pane_state": state})

    def mark_right_pane_idle(self) -> None:
        self._mutate({"right_pane_state": "idle"}, remove=("active_request_id", "right_pane_error"))

    def mark_right_pane_loading(self, request_id: str | None = None) -> None:
        update: dict[str, JsonValue] = {"right_pane_state": "loading"}
        if request_id is not None:
            self._require_non_empty_string(request_id, "active request id")
            update["active_request_id"] = request_id
        self._mutate(update, remove=("right_pane_error",))

    def mark_right_pane_static(self) -> None:
        self._mutate({"right_pane_state": "static"}, remove=("active_request_id", "right_pane_error"))

    def mark_right_pane_live_agent(self) -> None:
        self._mutate({"right_pane_state": "live_agent"}, remove=("active_request_id", "right_pane_error"))

    def mark_right_pane_error(self, message: str | None = None) -> None:
        update: dict[str, JsonValue] = {"right_pane_state": "error"}
        if message:
            update["right_pane_error"] = message
        self._mutate(update, remove=("active_request_id",))

    def right_pane_id(self) -> str | None:
        return self._right_pane_id_from(self._load_raw())

    def set_right_pane_id(self, pane_id: str | None) -> None:
        if pane_id is None:
            self.clear_right_pane_id()
            return
        self._require_non_empty_string(pane_id, "right pane id")
        self._mutate({"right_pane_id": pane_id})

    def clear_right_pane_id(self) -> None:
        self._mutate(remove=("right_pane_id",))

    def mounted_identity(self) -> MountedIdentityPayload | None:
        return self._mounted_identity_from(self._load_raw())

    def set_mounted_identity(
        self,
        payload: Mapping[str, JsonValue] | None,
    ) -> None:
        if payload is None:
            self.clear_mounted_identity()
            return
        identity = self._copy_json_object(payload, "mounted identity payload")
        if not identity:
            raise ValueError("mounted identity payload must not be empty")
        self._mutate({"mounted_identity": identity})

    def clear_mounted_identity(self) -> None:
        self._mutate(remove=("mounted_identity", "mounted_session"))

    def clear_mounted_and_right_pane_state(self) -> None:
        self._mutate(
            {"right_pane_state": "idle"},
            remove=(
                "active_request_id",
                "mounted_identity",
                "mounted_session",
                "right_pane_error",
                "right_pane_id",
            ),
        )

    def rail_width(self) -> int:
        return self._rail_width_from(self._load_raw())

    def set_rail_width(self, width: int) -> None:
        if not isinstance(width, int):
            raise TypeError("rail width must be an int")
        self._mutate({"rail_width": self.clamp_rail_width(width)})

    @staticmethod
    def clamp_rail_width(width: int) -> int:
        return max(MIN_RAIL_WIDTH, min(MAX_RAIL_WIDTH, width))

    def pinned_projects(self) -> list[str]:
        return self._pinned_projects_from(self._load_raw())

    def pins(self) -> list[str]:
        return self.pinned_projects()

    def set_pinned_projects(self, project_keys: list[str]) -> None:
        ordered = self._normalize_pins(project_keys)
        self._mutate({"pinned_projects": ordered})

    def is_project_pinned(self, project_key: str) -> bool:
        return project_key in self.pinned_projects()

    def pin_project(self, project_key: str) -> None:
        self._require_non_empty_string(project_key, "project key")
        current = self.pinned_projects()
        if project_key in current:
            current.remove(project_key)
        current.insert(0, project_key)
        self._mutate({"pinned_projects": current})

    def unpin_project(self, project_key: str) -> None:
        current = [key for key in self.pinned_projects() if key != project_key]
        self._mutate({"pinned_projects": current})

    def toggle_pinned_project(self, project_key: str) -> bool:
        self._require_non_empty_string(project_key, "project key")
        current = self.pinned_projects()
        if project_key in current:
            current.remove(project_key)
            pinned = False
        else:
            current.insert(0, project_key)
            pinned = True
        self._mutate({"pinned_projects": current})
        return pinned

    def should_show_palette_tip(self) -> bool:
        return not self._palette_tip_seen_from(self._load_raw())

    def palette_tip_seen(self) -> bool:
        return self._palette_tip_seen_from(self._load_raw())

    def set_palette_tip_seen(self, seen: bool) -> None:
        if not isinstance(seen, bool):
            raise TypeError("palette tip flag must be a bool")
        self._mutate({"palette_tip_seen": seen})

    def mark_palette_tip_seen(self) -> None:
        self.set_palette_tip_seen(True)

    def _mutate(
        self,
        update: Mapping[str, JsonValue] | None = None,
        *,
        remove: tuple[str, ...] = (),
    ) -> None:
        state = self._load_raw()
        for key in remove:
            state.pop(key, None)
        if update:
            state.update(update)
        self._write_raw(state)

    def _load_raw(self) -> dict[str, JsonValue]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return {
            str(key): value
            for key, value in payload.items()
            if isinstance(key, str) and self._is_json_value(value)
        }

    def _write_raw(self, state: Mapping[str, JsonValue]) -> None:
        atomic_write_json(self.path, dict(state))

    @staticmethod
    def _selected_key_from(state: Mapping[str, JsonValue]) -> str:
        value = state.get("selected")
        return value if isinstance(value, str) and value else DEFAULT_SELECTED_KEY

    @staticmethod
    def _active_request_id_from(state: Mapping[str, JsonValue]) -> str | None:
        value = state.get("active_request_id")
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _right_pane_state_from(state: Mapping[str, JsonValue]) -> RightPaneState:
        value = state.get("right_pane_state")
        if isinstance(value, str) and value in RIGHT_PANE_STATES:
            return cast(RightPaneState, value)
        return "idle"

    @staticmethod
    def _right_pane_id_from(state: Mapping[str, JsonValue]) -> str | None:
        value = state.get("right_pane_id")
        return value if isinstance(value, str) and value else None

    @classmethod
    def _mounted_identity_from(
        cls, state: Mapping[str, JsonValue],
    ) -> MountedIdentityPayload | None:
        value = state.get("mounted_identity")
        if not isinstance(value, dict):
            return None
        copied = cls._copy_json_object(value, "mounted identity payload")
        return copied or None

    @staticmethod
    def _rail_width_from(state: Mapping[str, JsonValue]) -> int:
        value = state.get("rail_width")
        if isinstance(value, int) and MIN_RAIL_WIDTH <= value <= MAX_RAIL_WIDTH:
            return value
        return DEFAULT_RAIL_WIDTH

    @classmethod
    def _pinned_projects_from(cls, state: Mapping[str, JsonValue]) -> list[str]:
        value = state.get("pinned_projects")
        return cls._normalize_pins(value if isinstance(value, list) else [])

    @staticmethod
    def _palette_tip_seen_from(state: Mapping[str, JsonValue]) -> bool:
        return state.get("palette_tip_seen") is True

    @classmethod
    def _normalize_pins(cls, value: list[JsonValue]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for item in value:
            if isinstance(item, str) and item and item not in seen:
                seen.add(item)
                ordered.append(item)
        return ordered

    @classmethod
    def _copy_json_object(
        cls,
        value: Mapping[str, JsonValue],
        label: str,
    ) -> dict[str, JsonValue]:
        copied: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{label} keys must be strings")
            if not cls._is_json_value(item):
                raise TypeError(f"{label} contains a non-JSON value")
            copied[key] = item
        return copied

    @classmethod
    def _is_json_value(cls, value: Any) -> bool:
        if value is None or isinstance(value, str):
            return True
        if isinstance(value, bool):
            return True
        if isinstance(value, int):
            return True
        if isinstance(value, float):
            return value == value and value not in {float("inf"), float("-inf")}
        if isinstance(value, list):
            return all(cls._is_json_value(item) for item in value)
        if isinstance(value, dict):
            return all(
                isinstance(key, str) and cls._is_json_value(item)
                for key, item in value.items()
            )
        return False

    @staticmethod
    def _require_non_empty_string(value: str, label: str) -> None:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{label} must be a non-empty string")

    @staticmethod
    def _require_right_pane_state(value: str) -> None:
        if value not in RIGHT_PANE_STATES:
            _raise_invalid_right_pane_state(value)


__all__ = [
    "CockpitStateSnapshot",
    "CockpitStateStore",
    "DEFAULT_RAIL_WIDTH",
    "DEFAULT_SELECTED_KEY",
    "LIFECYCLE_TO_RIGHT_PANE_STATE",
    "MAX_RAIL_WIDTH",
    "MIN_RAIL_WIDTH",
    "MountedIdentityPayload",
    "RIGHT_PANE_STATE_TO_LIFECYCLE",
    "RIGHT_PANE_STATES",
    "RightPaneState",
    "STATE_FILE_NAME",
    "lifecycle_to_right_pane_state",
    "right_pane_state_to_lifecycle",
]
