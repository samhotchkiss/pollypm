from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pollypm.cockpit_window_manager import (
    CockpitWindowManager,
    CockpitWindowSpec,
    CockpitWindowState,
    LivePaneSpec,
    ParkPaneSpec,
)


@dataclass(slots=True)
class FakePane:
    pane_id: str
    pane_current_command: str
    pane_left: int
    pane_width: int = 100
    pane_dead: bool = False
    session: str = ""
    window_index: int = 0
    window_name: str = ""
    pane_index: int = 0
    active: bool = False
    pane_current_path: str = "/tmp"


@dataclass(slots=True)
class FakeWindow:
    session: str
    index: int
    name: str
    panes: list[FakePane] = field(default_factory=list)
    active: bool = False

    @property
    def pane_id(self) -> str:
        return self.panes[0].pane_id if self.panes else ""

    @property
    def pane_current_command(self) -> str:
        return self.panes[0].pane_current_command if self.panes else ""

    @property
    def pane_current_path(self) -> str:
        return self.panes[0].pane_current_path if self.panes else ""

    @property
    def pane_dead(self) -> bool:
        return self.panes[0].pane_dead if self.panes else False


class FakeTmux:
    def __init__(self) -> None:
        self.sessions: dict[str, list[FakeWindow]] = {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._next_pane = 1

    def add_window(
        self,
        session: str,
        name: str,
        panes: list[tuple[str, int, int, bool]] | list[tuple[str, int]],
        *,
        index: int | None = None,
    ) -> FakeWindow:
        windows = self.sessions.setdefault(session, [])
        if index is None:
            index = max((window.index for window in windows), default=0) + 1
        window = FakeWindow(session=session, index=index, name=name)
        for raw in panes:
            command, left = raw[0], raw[1]
            width = raw[2] if len(raw) > 2 else 100
            dead = raw[3] if len(raw) > 3 else False
            window.panes.append(
                FakePane(
                    pane_id=self._new_pane_id(),
                    pane_current_command=self._command_name(command),
                    pane_left=left,
                    pane_width=width,
                    pane_dead=dead,
                )
            )
        windows.append(window)
        self._refresh_window(window)
        return window

    def list_panes(self, target: str) -> list[FakePane]:
        window = self._window_for_target(target)
        return sorted(window.panes, key=lambda pane: (pane.pane_left, pane.pane_id))

    def list_windows(self, name: str) -> list[FakeWindow]:
        return sorted(self.sessions.get(name, []), key=lambda window: window.index)

    def split_window(
        self,
        target: str,
        command: str,
        *,
        horizontal: bool = True,
        detached: bool = True,
        percent: int | None = None,
        size: int | None = None,
    ) -> str:
        del horizontal, detached, percent
        window = self._window_for_target(target)
        left = max((pane.pane_left + pane.pane_width for pane in window.panes), default=0) + 1
        pane = FakePane(
            pane_id=self._new_pane_id(),
            pane_current_command=self._command_name(command),
            pane_left=left,
            pane_width=size or 100,
        )
        window.panes.append(pane)
        self._refresh_window(window)
        self.calls.append(("split_window", (target, command, size, pane.pane_id)))
        return pane.pane_id

    def kill_pane(self, target: str) -> None:
        window, pane = self._find_pane(target)
        window.panes.remove(pane)
        self._refresh_window(window)
        self.calls.append(("kill_pane", (target,)))

    def resize_pane_width(self, target: str, width: int) -> None:
        _window, pane = self._find_pane(target)
        pane.pane_width = width
        self.calls.append(("resize_pane_width", (target, width)))

    def respawn_pane(self, target: str, command: str) -> None:
        _window, pane = self._find_pane(target)
        pane.pane_current_command = self._command_name(command)
        pane.pane_dead = False
        self.calls.append(("respawn_pane", (target, command)))

    def join_pane(self, source: str, target: str, *, horizontal: bool = True) -> None:
        del horizontal
        source_window, pane = self._find_source_pane(source)
        target_window, target_pane = self._find_pane(target)
        source_window.panes.remove(pane)
        if not source_window.panes:
            self.sessions[source_window.session].remove(source_window)
        pane.pane_left = target_pane.pane_left + target_pane.pane_width + 1
        target_window.panes.append(pane)
        self._refresh_window(target_window)
        self.calls.append(("join_pane", (source, target)))

    def break_pane(self, source: str, target_session: str, window_name: str) -> None:
        source_window, pane = self._find_pane(source)
        source_window.panes.remove(pane)
        self._refresh_window(source_window)
        window = self.add_window(
            target_session,
            window_name,
            [],
            index=max((w.index for w in self.sessions.get(target_session, [])), default=0) + 1,
        )
        pane.pane_left = 0
        window.panes.append(pane)
        self._refresh_window(window)
        self.calls.append(("break_pane", (source, target_session, window_name)))

    def rename_window(self, target: str, new_name: str) -> None:
        session, raw_index = target.split(":", 1)
        index = int(raw_index)
        for window in self.sessions.get(session, []):
            if window.index == index:
                window.name = new_name
                self._refresh_window(window)
                self.calls.append(("rename_window", (target, new_name)))
                return
        raise KeyError(target)

    def swap_pane(self, source: str, target: str) -> None:
        _source_window, source_pane = self._find_pane(source)
        _target_window, target_pane = self._find_pane(target)
        source_pane.pane_left, target_pane.pane_left = target_pane.pane_left, source_pane.pane_left
        self.calls.append(("swap_pane", (source, target)))

    def select_pane(self, target: str) -> None:
        self.calls.append(("select_pane", (target,)))

    def set_pane_history_limit(self, target: str, limit: int) -> None:
        self.calls.append(("set_pane_history_limit", (target, limit)))

    def run(self, *args: str, check: bool = True) -> None:
        self.calls.append(("run", (*args, check)))

    def _new_pane_id(self) -> str:
        pane_id = f"%{self._next_pane}"
        self._next_pane += 1
        return pane_id

    def _window_for_target(self, target: str) -> FakeWindow:
        if target.startswith("%"):
            return self._find_pane(target)[0]
        session, name = target.split(":", 1)
        for window in self.sessions.get(session, []):
            if window.name == name or str(window.index) == name:
                return window
        raise KeyError(target)

    def _find_pane(self, pane_id: str) -> tuple[FakeWindow, FakePane]:
        for windows in self.sessions.values():
            for window in windows:
                for pane in window.panes:
                    if pane.pane_id == pane_id:
                        return window, pane
        raise KeyError(pane_id)

    def _find_source_pane(self, source: str) -> tuple[FakeWindow, FakePane]:
        session, rest = source.split(":", 1)
        raw_window, raw_pane = rest.split(".", 1)
        window_index = int(raw_window)
        pane_index = int(raw_pane)
        for window in self.sessions.get(session, []):
            if window.index == window_index:
                self._refresh_window(window)
                return window, window.panes[pane_index]
        raise KeyError(source)

    def _refresh_window(self, window: FakeWindow) -> None:
        window.panes.sort(key=lambda pane: (pane.pane_left, pane.pane_id))
        for index, pane in enumerate(window.panes):
            pane.session = window.session
            pane.window_index = window.index
            pane.window_name = window.name
            pane.pane_index = index
            pane.active = index == 0

    @staticmethod
    def _command_name(command: str) -> str:
        if command.startswith("uv"):
            return "uv"
        if command.startswith("node"):
            return "node"
        if command.startswith("claude"):
            return "claude"
        if command.startswith("codex"):
            return "codex"
        if command.startswith("pm"):
            return "pm"
        return command.split(" ", 1)[0] if command else ""


def _manager(tmux: FakeTmux) -> CockpitWindowManager:
    return CockpitWindowManager(
        CockpitWindowSpec(
            tmux_session="pollypm",
            rail_width=30,
            rail_command="uv run pm rail",
            default_content_command="pm cockpit-pane polly",
        ),
        tmux,
    )


def test_classifies_left_right_and_rail_before_repair() -> None:
    tmux = FakeTmux()
    window = tmux.add_window("pollypm", "PollyPM", [("bash", 0), ("uv", 100)])
    manager = _manager(tmux)

    classification = manager.classify_panes()

    assert classification.left_pane == window.panes[0]
    assert classification.right_pane == window.panes[1]
    assert classification.rail_pane == window.panes[1]
    assert classification.content_pane == window.panes[0]
    assert manager.validate_postcondition().errors == ("rail pane is not leftmost", "content pane is not rightmost")


def test_ensure_layout_swaps_rail_left_and_validates_postcondition() -> None:
    tmux = FakeTmux()
    window = tmux.add_window("pollypm", "PollyPM", [("bash", 0), ("uv", 100)])
    old_left_id = window.panes[0].pane_id
    old_right_id = window.panes[1].pane_id
    manager = _manager(tmux)

    result = manager.ensure_layout(CockpitWindowState(right_pane_id=old_right_id))

    assert result.ok
    assert f"swap_rail_left:{old_right_id}->{old_left_id}" in result.actions
    assert result.state.right_pane_id == old_left_id
    assert manager.classify_panes().rail_pane.pane_id == old_right_id


def test_ensure_layout_repairs_missing_content_by_splitting() -> None:
    tmux = FakeTmux()
    tmux.add_window("pollypm", "PollyPM", [("uv", 0, 200, False)])
    manager = _manager(tmux)

    result = manager.ensure_layout()

    assert result.ok
    assert any(action.startswith("split_content:%") for action in result.actions)
    assert len(tmux.list_panes("pollypm:PollyPM")) == 2


def test_ensure_layout_repairs_missing_rail_by_respawning_single_pane_then_splitting() -> None:
    tmux = FakeTmux()
    window = tmux.add_window("pollypm", "PollyPM", [("codex", 0, 180, False)])
    original_id = window.panes[0].pane_id
    manager = _manager(tmux)

    result = manager.ensure_layout(
        CockpitWindowState(
            right_pane_id=original_id,
            mounted_session="worker_demo",
            mounted_window_name="worker-demo",
        )
    )

    assert result.ok
    assert f"respawn_missing_rail:{original_id}" in result.actions
    assert any(action.startswith("split_content:%") for action in result.actions)
    assert result.state.mounted_session is None


def test_ensure_layout_respawns_dead_right_and_kills_extra_panes() -> None:
    tmux = FakeTmux()
    window = tmux.add_window(
        "pollypm",
        "PollyPM",
        [("uv", 0, 30, False), ("node", 40, 80, True), ("bash", 130, 80, False)],
    )
    dead_right_id = window.panes[1].pane_id
    extra_id = window.panes[2].pane_id
    manager = _manager(tmux)

    result = manager.ensure_layout(CockpitWindowState(right_pane_id=dead_right_id))

    assert result.ok
    assert f"respawn_dead_content:{dead_right_id}" in result.actions
    assert f"kill_extra:{extra_id}" in result.actions
    assert {pane.pane_id for pane in tmux.list_panes("pollypm:PollyPM")} == {
        window.panes[0].pane_id,
        dead_right_id,
    }


def test_show_static_respawns_right_and_clears_mounted_state() -> None:
    tmux = FakeTmux()
    window = tmux.add_window("pollypm", "PollyPM", [("uv", 0), ("node", 100)])
    right_id = window.panes[1].pane_id
    manager = _manager(tmux)

    result = manager.show_static(
        "pm cockpit-pane project demo",
        CockpitWindowState(
            right_pane_id=right_id,
            mounted_session="worker_demo",
            mounted_window_name="worker-demo",
        ),
    )

    assert result.ok
    assert f"respawn_static:{right_id}" in result.actions
    assert result.state == CockpitWindowState(right_pane_id=right_id)
    assert ("respawn_pane", (right_id, "pm cockpit-pane project demo")) in tmux.calls


def test_join_live_from_storage_uses_window_index_and_sets_mount_state() -> None:
    tmux = FakeTmux()
    cockpit = tmux.add_window("pollypm", "PollyPM", [("uv", 0), ("pm", 100)])
    left_id = cockpit.panes[0].pane_id
    static_right_id = cockpit.panes[1].pane_id
    tmux.add_window("pollypm-storage-closet", "worker-demo", [("codex", 0)], index=7)
    manager = _manager(tmux)

    result = manager.join_live_from_storage(
        LivePaneSpec(
            storage_session="pollypm-storage-closet",
            window_name="worker-demo",
            mounted_session="worker_demo",
        ),
        CockpitWindowState(right_pane_id=static_right_id),
    )

    assert result.ok
    assert f"kill_static_right:{static_right_id}" in result.actions
    assert f"join_live:pollypm-storage-closet:7.0->{left_id}" in result.actions
    assert result.state.mounted_session == "worker_demo"
    assert result.state.mounted_window_name == "worker-demo"
    assert tmux.list_windows("pollypm-storage-closet") == []
    assert ("set_pane_history_limit", (result.right_pane_id, 200)) in tmux.calls


def test_join_live_from_storage_does_not_kill_right_when_source_missing() -> None:
    tmux = FakeTmux()
    cockpit = tmux.add_window("pollypm", "PollyPM", [("uv", 0), ("pm", 100)])
    right_id = cockpit.panes[1].pane_id
    manager = _manager(tmux)

    result = manager.join_live_from_storage(
        LivePaneSpec(
            storage_session="pollypm-storage-closet",
            window_name="worker-demo",
            mounted_session="worker_demo",
        ),
        CockpitWindowState(right_pane_id=right_id),
    )

    assert result.ok
    assert "missing_storage_window:pollypm-storage-closet:worker-demo" in result.actions
    assert ("kill_pane", (right_id,)) not in tmux.calls
    assert {pane.pane_id for pane in tmux.list_panes("pollypm:PollyPM")} == {
        cockpit.panes[0].pane_id,
        right_id,
    }


def test_park_live_to_storage_breaks_right_and_replaces_static_content() -> None:
    tmux = FakeTmux()
    cockpit = tmux.add_window("pollypm", "PollyPM", [("uv", 0), ("codex", 100)])
    right_id = cockpit.panes[1].pane_id
    manager = _manager(tmux)

    result = manager.park_live_to_storage(
        CockpitWindowState(
            right_pane_id=right_id,
            mounted_session="worker_demo",
            mounted_window_name="worker-demo",
        ),
        park=ParkPaneSpec(
            storage_session="pollypm-storage-closet",
            window_name="worker-demo",
            mounted_session="worker_demo",
        ),
    )

    assert result.ok
    assert f"break_live:{right_id}->pollypm-storage-closet:worker-demo" in result.actions
    assert result.state.mounted_session is None
    assert result.state.right_pane_id != right_id
    storage_windows = tmux.list_windows("pollypm-storage-closet")
    assert len(storage_windows) == 1
    assert storage_windows[0].name == "worker-demo"
    assert storage_windows[0].pane_id == right_id


def test_focus_right_selects_content_pane_and_shows_hint() -> None:
    tmux = FakeTmux()
    cockpit = tmux.add_window("pollypm", "PollyPM", [("uv", 0), ("pm", 100)])
    right_id = cockpit.panes[1].pane_id
    manager = _manager(tmux)

    result = manager.focus_right(CockpitWindowState(right_pane_id=right_id))

    assert result.ok
    assert ("select_pane", (right_id,)) in tmux.calls
    assert any(call[0] == "run" and "display-message" in call[1] for call in tmux.calls)


def test_send_key_right_forwards_key_to_content_pane() -> None:
    tmux = FakeTmux()
    cockpit = tmux.add_window("pollypm", "PollyPM", [("uv", 0), ("pm", 100)])
    right_id = cockpit.panes[1].pane_id
    manager = _manager(tmux)

    result = manager.send_key_right("Tab", CockpitWindowState(right_pane_id=right_id))

    assert result.ok
    assert ("run", ("send-keys", "-t", right_id, "Tab", False)) in tmux.calls
