"""Terminal mechanics for the cockpit tmux window.

This module is intentionally below the router/UI layer.  It does not
resolve rail keys, build cockpit pane commands, import Textual apps, or
inspect content modules.  Integration code should pass already-resolved
commands and persist the returned :class:`CockpitWindowState` alongside
the rest of cockpit state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, Sequence


class TmuxWindowMechanics(Protocol):
    """Small tmux surface required by :class:`CockpitWindowManager`.

    ``pollypm.tmux.client.TmuxClient`` satisfies this protocol.  Tests use
    a deterministic in-memory model so the manager can be verified without
    a live tmux server.
    """

    def list_panes(self, target: str) -> list[Any]: ...

    def list_windows(self, name: str) -> list[Any]: ...

    def split_window(
        self,
        target: str,
        command: str,
        *,
        horizontal: bool = True,
        detached: bool = True,
        percent: int | None = None,
        size: int | None = None,
    ) -> str: ...

    def kill_pane(self, target: str) -> None: ...

    def resize_pane_width(self, target: str, width: int) -> None: ...

    def respawn_pane(self, target: str, command: str) -> None: ...

    def join_pane(self, source: str, target: str, *, horizontal: bool = True) -> None: ...

    def break_pane(self, source: str, target_session: str, window_name: str) -> None: ...

    def rename_window(self, target: str, new_name: str) -> None: ...

    def swap_pane(self, source: str, target: str) -> None: ...

    def select_pane(self, target: str) -> None: ...

    def set_pane_history_limit(self, target: str, limit: int) -> None: ...

    def run(self, *args: str, check: bool = True) -> Any: ...


@dataclass(slots=True, frozen=True)
class CockpitWindowSpec:
    """Static terminal layout configuration.

    The command strings are integration points: the router/content layer
    resolves them, while this manager only decides which pane receives
    each command.
    """

    tmux_session: str
    cockpit_window: str = "PollyPM"
    rail_width: int = 30
    min_content_width: int = 40
    default_content_command: str = "pm cockpit-pane polly"
    rail_command: str | None = None
    rail_commands: tuple[str, ...] = ("uv",)
    live_provider_commands: tuple[str, ...] = ("node", "claude", "codex")
    mounted_history_limit: int = 200

    @property
    def window_target(self) -> str:
        return f"{self.tmux_session}:{self.cockpit_window}"


@dataclass(slots=True, frozen=True)
class CockpitWindowState:
    """Persistable cockpit pane state owned by integration code."""

    right_pane_id: str | None = None
    mounted_session: str | None = None
    mounted_window_name: str | None = None

    def cleared_mount(self) -> "CockpitWindowState":
        return CockpitWindowState(right_pane_id=self.right_pane_id)

    def with_right(self, right_pane_id: str | None) -> "CockpitWindowState":
        return CockpitWindowState(
            right_pane_id=right_pane_id,
            mounted_session=self.mounted_session,
            mounted_window_name=self.mounted_window_name,
        )

    def with_mount(
        self,
        *,
        right_pane_id: str,
        mounted_session: str,
        mounted_window_name: str,
    ) -> "CockpitWindowState":
        return CockpitWindowState(
            right_pane_id=right_pane_id,
            mounted_session=mounted_session,
            mounted_window_name=mounted_window_name,
        )


@dataclass(slots=True, frozen=True)
class LivePaneSpec:
    """A live session window that should be mounted into the cockpit."""

    storage_session: str
    window_name: str
    mounted_session: str
    history_limit: int | None = None


@dataclass(slots=True, frozen=True)
class ParkPaneSpec:
    """Where to park a currently mounted cockpit content pane."""

    storage_session: str
    window_name: str
    mounted_session: str


@dataclass(slots=True, frozen=True)
class PaneClassification:
    panes: tuple[Any, ...]
    live_panes: tuple[Any, ...]
    dead_panes: tuple[Any, ...]
    left_pane: Any | None
    right_pane: Any | None
    rail_pane: Any | None
    content_pane: Any | None
    extra_panes: tuple[Any, ...]


@dataclass(slots=True, frozen=True)
class CockpitPostcondition:
    valid: bool
    errors: tuple[str, ...]
    left_pane_id: str | None = None
    right_pane_id: str | None = None


@dataclass(slots=True, frozen=True)
class CockpitWindowResult:
    state: CockpitWindowState
    actions: tuple[str, ...]
    postcondition: CockpitPostcondition

    @property
    def left_pane_id(self) -> str | None:
        return self.postcondition.left_pane_id

    @property
    def right_pane_id(self) -> str | None:
        return self.postcondition.right_pane_id

    @property
    def ok(self) -> bool:
        return self.postcondition.valid


def _pane_id(pane: Any | None) -> str | None:
    value = getattr(pane, "pane_id", None)
    return value if isinstance(value, str) and value else None


def _pane_left(pane: Any) -> int:
    try:
        return int(getattr(pane, "pane_left", 0))
    except (TypeError, ValueError):
        return 0


def _pane_width(pane: Any) -> int:
    try:
        return int(getattr(pane, "pane_width", 0))
    except (TypeError, ValueError):
        return 0


def _pane_command(pane: Any | None) -> str:
    value = getattr(pane, "pane_current_command", "")
    return value if isinstance(value, str) else ""


def _pane_dead(pane: Any | None) -> bool:
    return bool(getattr(pane, "pane_dead", False))


class CockpitWindowManager:
    """Own tmux pane layout and movement for one cockpit window."""

    def __init__(
        self,
        spec: CockpitWindowSpec,
        tmux: TmuxWindowMechanics | None = None,
    ) -> None:
        self.spec = spec
        if tmux is None:
            from pollypm.tmux.client import TmuxClient

            tmux = TmuxClient()
        self.tmux = tmux

    def classify_panes(self, panes: Sequence[Any] | None = None) -> PaneClassification:
        if panes is None:
            panes = self.tmux.list_panes(self.spec.window_target)
        ordered = tuple(sorted(panes, key=lambda pane: (_pane_left(pane), _pane_id(pane) or "")))
        dead = tuple(pane for pane in ordered if _pane_dead(pane))
        live = tuple(pane for pane in ordered if not _pane_dead(pane))
        left = live[0] if live else None
        right = live[-1] if live else None
        rail = next((pane for pane in live if self._is_rail_pane(pane)), None)
        if rail is None and len(live) >= 2:
            # Real cockpit rail panes may report ``bash`` or ``python`` while
            # the TUI is running through a shell wrapper. In a normal two-pane
            # cockpit, position is the stronger signal: left is rail, right is
            # content. Command matching is only used when it identifies the
            # rail somewhere else and we need to swap it back left.
            rail = left
        content: Any | None = None
        if rail is not None:
            content = next((pane for pane in reversed(live) if pane is not rail), None)
        elif len(live) >= 2:
            content = live[-1]
        extras = tuple(pane for pane in live if pane is not rail and pane is not content)
        return PaneClassification(
            panes=ordered,
            live_panes=live,
            dead_panes=dead,
            left_pane=left,
            right_pane=right,
            rail_pane=rail,
            content_pane=content,
            extra_panes=extras,
        )

    def validate_postcondition(self, panes: Sequence[Any] | None = None) -> CockpitPostcondition:
        classification = self.classify_panes(panes)
        errors: list[str] = []
        if classification.dead_panes:
            errors.append(
                "dead panes present: "
                + ", ".join(_pane_id(pane) or "?" for pane in classification.dead_panes)
            )
        if len(classification.live_panes) != 2:
            errors.append(f"expected exactly 2 live panes, found {len(classification.live_panes)}")
        if classification.rail_pane is None:
            errors.append("rail pane missing")
        elif classification.left_pane is not classification.rail_pane:
            errors.append("rail pane is not leftmost")
        if classification.content_pane is None:
            errors.append("content pane missing")
        elif classification.right_pane is not classification.content_pane:
            errors.append("content pane is not rightmost")
        return CockpitPostcondition(
            valid=not errors,
            errors=tuple(errors),
            left_pane_id=_pane_id(classification.left_pane),
            right_pane_id=_pane_id(classification.content_pane),
        )

    def ensure_layout(
        self,
        state: CockpitWindowState | None = None,
    ) -> CockpitWindowResult:
        state = state or CockpitWindowState()
        actions: list[str] = []
        panes = self.tmux.list_panes(self.spec.window_target)

        state = self._repair_dead_panes(panes, state, actions)
        panes = self.tmux.list_panes(self.spec.window_target)

        classification = self.classify_panes(panes)
        if len(classification.live_panes) == 1:
            state = self._repair_single_pane(classification.live_panes[0], state, actions)
            panes = self.tmux.list_panes(self.spec.window_target)
            classification = self.classify_panes(panes)

        if len(classification.live_panes) < 2 and classification.live_panes:
            right_pane_id = self._split_content_pane(classification.live_panes[0], actions)
            state = state.cleared_mount().with_right(right_pane_id)
            panes = self.tmux.list_panes(self.spec.window_target)
            classification = self.classify_panes(panes)

        if len(classification.live_panes) > 2:
            self._kill_extra_panes(classification, state, actions)
            panes = self.tmux.list_panes(self.spec.window_target)
            classification = self.classify_panes(panes)

        if len(classification.live_panes) == 2:
            if classification.rail_pane is None and self.spec.rail_command is not None:
                left_id = _pane_id(classification.left_pane)
                if left_id is not None:
                    self.tmux.respawn_pane(left_id, self.spec.rail_command)
                    actions.append(f"respawn_rail:{left_id}")
                    state = state.cleared_mount()
                    panes = self.tmux.list_panes(self.spec.window_target)
                    classification = self.classify_panes(panes)
            if (
                classification.rail_pane is not None
                and classification.left_pane is not classification.rail_pane
            ):
                source = _pane_id(classification.rail_pane)
                target = _pane_id(classification.left_pane)
                if source is not None and target is not None:
                    self.tmux.swap_pane(source, target)
                    actions.append(f"swap_rail_left:{source}->{target}")
                    panes = self.tmux.list_panes(self.spec.window_target)
                    classification = self.classify_panes(panes)

        self._resize_rail(classification, actions)
        postcondition = self.validate_postcondition()
        if postcondition.right_pane_id is not None:
            state = state.with_right(postcondition.right_pane_id)
        return CockpitWindowResult(
            state=state,
            actions=tuple(actions),
            postcondition=postcondition,
        )

    def show_static(
        self,
        command: str,
        state: CockpitWindowState | None = None,
        *,
        park: ParkPaneSpec | None = None,
    ) -> CockpitWindowResult:
        state = state or CockpitWindowState()
        actions: list[str] = []
        if park is not None and state.mounted_session:
            park_result = self.park_live_to_storage(state, park=park)
            state = park_result.state
            actions.extend(park_result.actions)
        layout = self.ensure_layout(state)
        state = layout.state
        actions.extend(layout.actions)
        right = self._right_pane()
        right_id = _pane_id(right)
        if right_id is None:
            return self._result(state, actions)
        self.tmux.respawn_pane(right_id, command)
        actions.append(f"respawn_static:{right_id}")
        state = state.cleared_mount().with_right(right_id)
        return self._result(state, actions)

    def join_live_from_storage(
        self,
        live: LivePaneSpec,
        state: CockpitWindowState | None = None,
    ) -> CockpitWindowResult:
        state = state or CockpitWindowState()
        actions: list[str] = []
        layout = self.ensure_layout(state)
        state = layout.state
        actions.extend(layout.actions)

        source_window = self._storage_window(live.storage_session, live.window_name)
        if source_window is None:
            actions.append(f"missing_storage_window:{live.storage_session}:{live.window_name}")
            return self._result(state, actions)

        left = self._left_pane()
        right = self._right_pane()
        left_id = _pane_id(left)
        right_id = _pane_id(right)
        if left_id is None:
            return self._result(state, actions)

        source_id = _pane_id(source_window)
        source = source_id or f"{live.storage_session}:{getattr(source_window, 'index')}.0"
        self.tmux.join_pane(source, left_id, horizontal=True)
        actions.append(f"join_live:{source}->{left_id}")
        if right_id is not None and right_id != source_id:
            self.tmux.kill_pane(right_id)
            actions.append(f"kill_static_right:{right_id}")
        panes = self.tmux.list_panes(self.spec.window_target)
        classification = self.classify_panes(panes)
        if source_id is not None:
            keep_ids = {left_id, source_id}
            for pane in classification.live_panes:
                pane_id = _pane_id(pane)
                if pane_id is None or pane_id in keep_ids:
                    continue
                self.tmux.kill_pane(pane_id)
                actions.append(f"kill_extra:{pane_id}")
            panes = self.tmux.list_panes(self.spec.window_target)
            classification = self.classify_panes(panes)
        self._resize_rail(classification, actions)
        right = (
            next(
                (
                    pane for pane in classification.live_panes
                    if _pane_id(pane) == source_id
                ),
                None,
            )
            if source_id is not None
            else self._right_pane()
        )
        right_id = _pane_id(right)
        if right_id is not None:
            limit = live.history_limit or self.spec.mounted_history_limit
            self.tmux.set_pane_history_limit(right_id, limit)
            actions.append(f"history_limit:{right_id}:{limit}")
            state = state.with_mount(
                right_pane_id=right_id,
                mounted_session=live.mounted_session,
                mounted_window_name=live.window_name,
            )
        return self._result(state, actions)

    def park_live_to_storage(
        self,
        state: CockpitWindowState | None = None,
        *,
        park: ParkPaneSpec | None = None,
    ) -> CockpitWindowResult:
        state = state or CockpitWindowState()
        actions: list[str] = []
        if park is None:
            if not state.mounted_session or not state.mounted_window_name:
                return self._result(state.cleared_mount(), actions)
            storage_session = f"{self.spec.tmux_session}-storage-closet"
            park = ParkPaneSpec(
                storage_session=storage_session,
                window_name=state.mounted_window_name,
                mounted_session=state.mounted_session,
            )

        right = self._right_pane(preferred_id=state.right_pane_id)
        right_id = _pane_id(right)
        if right_id is None or not self._is_live_provider_pane(right):
            actions.append("clear_non_live_mount")
            state = state.cleared_mount()
            layout = self.ensure_layout(state)
            actions.extend(layout.actions)
            return CockpitWindowResult(
                state=layout.state,
                actions=tuple(actions),
                postcondition=layout.postcondition,
            )

        before = self._window_key_set(park.storage_session)
        self.tmux.break_pane(right_id, park.storage_session, park.window_name)
        actions.append(f"break_live:{right_id}->{park.storage_session}:{park.window_name}")
        self._rename_created_window(park.storage_session, before, park.window_name, actions)
        state = state.cleared_mount()

        panes = self.tmux.list_panes(self.spec.window_target)
        if len([pane for pane in panes if not _pane_dead(pane)]) < 2:
            classification = self.classify_panes(panes)
            anchor = classification.rail_pane or classification.left_pane
            if anchor is not None:
                new_right = self._split_content_pane(anchor, actions)
                state = state.with_right(new_right)
        layout = self.ensure_layout(state)
        actions.extend(layout.actions)
        return CockpitWindowResult(
            state=layout.state,
            actions=tuple(actions),
            postcondition=layout.postcondition,
        )

    def focus_right(self, state: CockpitWindowState | None = None) -> CockpitWindowResult:
        actions: list[str] = []
        layout = self.ensure_layout(state)
        actions.extend(layout.actions)
        right_id = layout.right_pane_id
        if right_id is not None:
            self.tmux.run(
                "display-message",
                "-t",
                self.spec.window_target,
                "PollyPM: Ctrl-b Left returns to the rail.",
                check=False,
            )
            self.tmux.select_pane(right_id)
            actions.append(f"focus_right:{right_id}")
        return CockpitWindowResult(
            state=layout.state,
            actions=tuple(actions),
            postcondition=layout.postcondition,
        )

    def send_key_right(
        self,
        key: str,
        state: CockpitWindowState | None = None,
    ) -> CockpitWindowResult:
        actions: list[str] = []
        layout = self.ensure_layout(state)
        actions.extend(layout.actions)
        right_id = layout.right_pane_id
        if right_id is not None:
            self.tmux.run("send-keys", "-t", right_id, key, check=False)
            actions.append(f"send_key_right:{right_id}:{key}")
        return CockpitWindowResult(
            state=layout.state,
            actions=tuple(actions),
            postcondition=layout.postcondition,
        )

    def _result(
        self,
        state: CockpitWindowState,
        actions: list[str],
    ) -> CockpitWindowResult:
        postcondition = self.validate_postcondition()
        if postcondition.right_pane_id is not None:
            state = state.with_right(postcondition.right_pane_id)
        return CockpitWindowResult(
            state=state,
            actions=tuple(actions),
            postcondition=postcondition,
        )

    def _repair_dead_panes(
        self,
        panes: Sequence[Any],
        state: CockpitWindowState,
        actions: list[str],
    ) -> CockpitWindowState:
        if not any(_pane_dead(pane) for pane in panes):
            return state
        ordered = sorted(panes, key=lambda pane: (_pane_left(pane), _pane_id(pane) or ""))
        for pane in ordered:
            if not _pane_dead(pane):
                continue
            pane_id = _pane_id(pane)
            if pane_id is None:
                continue
            if pane_id == state.right_pane_id or pane is ordered[-1]:
                self.tmux.respawn_pane(pane_id, self.spec.default_content_command)
                actions.append(f"respawn_dead_content:{pane_id}")
                state = state.cleared_mount().with_right(pane_id)
            elif self.spec.rail_command is not None:
                self.tmux.respawn_pane(pane_id, self.spec.rail_command)
                actions.append(f"respawn_dead_rail:{pane_id}")
            else:
                self.tmux.kill_pane(pane_id)
                actions.append(f"kill_dead:{pane_id}")
        return state

    def _repair_single_pane(
        self,
        pane: Any,
        state: CockpitWindowState,
        actions: list[str],
    ) -> CockpitWindowState:
        if self._is_rail_pane(pane):
            return state
        pane_id = _pane_id(pane)
        if pane_id is not None and pane_id != state.right_pane_id:
            # After parking a live right pane back to storage, tmux leaves
            # only the shell-wrapped rail pane in the cockpit window. Its
            # foreground command may report as ``bash``/``zsh`` rather than
            # ``uv``. If this lone pane is not the persisted right pane, keep
            # it as rail and let the caller split a fresh content pane.
            actions.append(f"assume_shell_wrapped_rail:{pane_id}")
            return state.cleared_mount().with_right(None)
        if pane_id is not None and self.spec.rail_command is not None:
            self.tmux.respawn_pane(pane_id, self.spec.rail_command)
            actions.append(f"respawn_missing_rail:{pane_id}")
            return state.cleared_mount()
        return state

    def _kill_extra_panes(
        self,
        classification: PaneClassification,
        state: CockpitWindowState,
        actions: list[str],
    ) -> None:
        preferred_content = None
        if state.right_pane_id is not None:
            preferred_content = next(
                (
                    pane
                    for pane in classification.live_panes
                    if _pane_id(pane) == state.right_pane_id
                ),
                None,
            )
        keep_ids = {
            _pane_id(classification.rail_pane or classification.left_pane),
            _pane_id(preferred_content or classification.content_pane or classification.right_pane),
        }
        for pane in classification.live_panes:
            pane_id = _pane_id(pane)
            if pane_id is None or pane_id in keep_ids:
                continue
            self.tmux.kill_pane(pane_id)
            actions.append(f"kill_extra:{pane_id}")

    def _split_content_pane(self, anchor: Any, actions: list[str]) -> str:
        width = max((_pane_width(anchor) or 200) - self.spec.rail_width - 1, self.spec.min_content_width)
        right_pane_id = self.tmux.split_window(
            self.spec.window_target,
            self.spec.default_content_command,
            horizontal=True,
            detached=True,
            size=width,
        )
        actions.append(f"split_content:{right_pane_id}")
        return right_pane_id

    def _resize_rail(
        self,
        classification: PaneClassification,
        actions: list[str],
    ) -> None:
        pane_id = _pane_id(classification.rail_pane)
        if pane_id is None:
            return
        self.tmux.resize_pane_width(pane_id, self.spec.rail_width)
        actions.append(f"resize_rail:{pane_id}:{self.spec.rail_width}")

    def _left_pane(self) -> Any | None:
        return self.classify_panes().left_pane

    def _right_pane(self, *, preferred_id: str | None = None) -> Any | None:
        panes = self.tmux.list_panes(self.spec.window_target)
        if preferred_id:
            preferred = next(
                (pane for pane in panes if _pane_id(pane) == preferred_id and not _pane_dead(pane)),
                None,
            )
            if preferred is not None:
                return preferred
        return self.classify_panes(panes).content_pane

    def _storage_window(self, storage_session: str, window_name: str) -> Any | None:
        matches = [
            window
            for window in self.tmux.list_windows(storage_session)
            if getattr(window, "name", None) == window_name
        ]
        if not matches:
            return None
        return sorted(matches, key=lambda window: int(getattr(window, "index", 0)))[0]

    def _window_key_set(self, storage_session: str) -> set[tuple[int, str]]:
        return {
            (int(getattr(window, "index", 0)), str(getattr(window, "name", "")))
            for window in self.tmux.list_windows(storage_session)
        }

    def _rename_created_window(
        self,
        storage_session: str,
        before: set[tuple[int, str]],
        window_name: str,
        actions: list[str],
    ) -> None:
        after = self.tmux.list_windows(storage_session)
        created = [
            window
            for window in after
            if (int(getattr(window, "index", 0)), str(getattr(window, "name", ""))) not in before
        ]
        if not created:
            return
        created.sort(key=lambda window: int(getattr(window, "index", 0)))
        target = created[-1]
        index = int(getattr(target, "index", 0))
        if getattr(target, "name", None) != window_name:
            self.tmux.rename_window(f"{storage_session}:{index}", window_name)
            actions.append(f"rename_parked:{storage_session}:{index}:{window_name}")

    def _is_rail_pane(self, pane: Any | None) -> bool:
        return _pane_command(pane) in self.spec.rail_commands

    def _is_live_provider_pane(self, pane: Any | None) -> bool:
        command = _pane_command(pane)
        if command in self.spec.live_provider_commands:
            return True
        return bool(command) and all(char.isdigit() or char == "." for char in command)
