"""Tests for the TTY-less cockpit input bridge (#1109 follow-up).

The bridge is the recovery path when the cockpit's Textual ``LinuxDriver``
stops processing keystrokes after the tmux session loses its attached
client. Spinning a real Textual ``App`` in a unit test is overkill — we
substitute a tiny stub that records ``simulate_key`` invocations and
tracks accidental ``call_from_thread`` use.
"""

from __future__ import annotations

import os
import shutil
import socket
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from pollypm.cockpit_input_bridge import (
    list_bridge_sockets,
    send_key,
    send_key_to_first_live,
    start_input_bridge,
)


class _FakeApp:
    """Minimal stand-in for ``textual.app.App``.

    The bridge only needs ``simulate_key``. ``call_from_thread`` remains on
    the fake so tests can assert the accept loop is not blocking on it.
    """

    def __init__(self) -> None:
        self.keys: list[str] = []
        self._lock = threading.Lock()
        self.raise_runtime_error = False
        self.raise_simulate_error = False
        self.call_from_thread_calls = 0

    def simulate_key(self, key: str) -> None:
        if self.raise_simulate_error:
            raise RuntimeError("simulate failed")
        with self._lock:
            self.keys.append(key)

    def call_from_thread(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        self.call_from_thread_calls += 1
        if self.raise_runtime_error:
            raise RuntimeError("App is not running")
        return fn(*args, **kwargs)


def _wait_for(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture()
def fake_config() -> Iterator[Path]:
    """A fake config_path whose ``parent`` is the test's tmp dir.

    The bridge drops sockets in ``config_path.parent / 'cockpit_inputs'``,
    matching how it co-locates with ``cockpit_debug.log``.
    """
    root = Path("/tmp") / f"pb-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=False)
    config = root / "config.toml"
    config.write_text("# fake\n")
    try:
        yield config
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture()
def valid_cockpit_config(tmp_path: Path) -> Path:
    """Minimal config that ``CockpitRouter`` can use for state lookup."""
    base_dir = tmp_path / ".pollypm"
    config = tmp_path / "pollypm.toml"
    config.write_text(
        "[project]\n"
        'name = "PollyPM"\n'
        'tmux_session = "pollypm-test"\n'
        f'base_dir = "{base_dir}"\n'
    )
    return config


def test_start_input_bridge_creates_socket(fake_config: Path) -> None:
    app = _FakeApp()
    handle = start_input_bridge(app, kind="cockpit", config_path=fake_config)
    assert handle is not None
    try:
        assert handle.socket_path.exists()
        assert handle.socket_path.is_socket()
        # macOS AF_UNIX ``sun_path`` is 104 chars; under deep pytest
        # tmp dirs the bridge falls back to ``$TMPDIR``. Accept either.
        assert handle.socket_path.parent.name in {
            "cockpit_inputs",
            "pollypm-cockpit_inputs",
        }
        assert handle.socket_path.name.startswith("cockpit-")
    finally:
        handle.stop()


def test_send_key_dispatches_to_simulate_key(fake_config: Path) -> None:
    app = _FakeApp()
    handle = start_input_bridge(app, kind="cockpit", config_path=fake_config)
    assert handle is not None
    try:
        send_key(handle.socket_path, "I")
        assert _wait_for(lambda: app.keys == ["I"])
    finally:
        handle.stop()


def test_special_tokens_normalize_to_textual_key_names(fake_config: Path) -> None:
    app = _FakeApp()
    handle = start_input_bridge(app, kind="cockpit", config_path=fake_config)
    assert handle is not None
    try:
        # Send each token; the bridge accept loop reads newline-delimited
        # records, so concatenate.
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(str(handle.socket_path))
        sock.sendall(b"<bs>\n<cr>\n<esc>\n<tab>\n<space>\n<up>\n")
        sock.close()
        assert _wait_for(
            lambda: app.keys == [
                "backspace",
                "enter",
                "escape",
                "tab",
                "space",
                "up",
            ]
        ), f"got: {app.keys}"
    finally:
        handle.stop()


def test_literal_question_mark_normalizes_to_textual_key_name(
    fake_config: Path,
) -> None:
    app = _FakeApp()
    handle = start_input_bridge(app, kind="cockpit", config_path=fake_config)
    assert handle is not None
    try:
        send_key(handle.socket_path, "?")
        assert _wait_for(lambda: app.keys == ["question_mark"])
    finally:
        handle.stop()


def test_modifier_tokens_pass_through(fake_config: Path) -> None:
    app = _FakeApp()
    handle = start_input_bridge(app, kind="cockpit", config_path=fake_config)
    assert handle is not None
    try:
        send_key(handle.socket_path, "ctrl+l")
        assert _wait_for(lambda: app.keys == ["ctrl+l"])
    finally:
        handle.stop()


def test_stop_removes_socket_file(fake_config: Path) -> None:
    app = _FakeApp()
    handle = start_input_bridge(app, kind="cockpit", config_path=fake_config)
    assert handle is not None
    socket_path = handle.socket_path
    handle.stop()
    assert _wait_for(lambda: not socket_path.exists())


def test_list_bridge_sockets_filters_by_kind(fake_config: Path) -> None:
    app = _FakeApp()
    cockpit = start_input_bridge(app, kind="cockpit", config_path=fake_config)
    dashboard = start_input_bridge(app, kind="dashboard", config_path=fake_config)
    assert cockpit is not None and dashboard is not None
    try:
        cockpits = list_bridge_sockets(fake_config, kind="cockpit")
        dashboards = list_bridge_sockets(fake_config, kind="dashboard")
        assert cockpit.socket_path in cockpits
        assert dashboard.socket_path not in cockpits
        assert dashboard.socket_path in dashboards
        assert cockpit.socket_path not in dashboards
        all_sockets = list_bridge_sockets(fake_config)
        assert cockpit.socket_path in all_sockets
        assert dashboard.socket_path in all_sockets
    finally:
        cockpit.stop()
        dashboard.stop()


def test_cockpit_send_key_defaults_to_cockpit_socket(fake_config: Path) -> None:
    import typer
    from typer.testing import CliRunner

    from pollypm.cli_features.ui import register_ui_commands

    cockpit_app = _FakeApp()
    dashboard_app = _FakeApp()
    cockpit = start_input_bridge(cockpit_app, kind="cockpit", config_path=fake_config)
    assert cockpit is not None
    dashboard = start_input_bridge(
        dashboard_app, kind="dashboard", config_path=fake_config
    )
    assert dashboard is not None
    try:
        # Simulate issue #1125: a dashboard bridge has the newest socket
        # timestamp, but bare `pm cockpit-send-key` should still target
        # the rail cockpit unless the caller explicitly passes `--kind`.
        future = time.time() + 5
        os.utime(dashboard.socket_path, (future, future))

        app = typer.Typer()
        register_ui_commands(app)
        result = CliRunner().invoke(
            app, ["cockpit-send-key", "I", "--config", str(fake_config)]
        )
        assert result.exit_code == 0, result.output
        assert f"via {cockpit.socket_path}" in result.output
        assert _wait_for(lambda: cockpit_app.keys == ["I"])
        assert dashboard_app.keys == []
    finally:
        cockpit.stop()
        dashboard.stop()


@pytest.mark.parametrize(
    ("selected_key", "bridge_kind"),
    [
        ("dashboard", "dashboard"),
        ("polly", "dashboard"),
        ("inbox", "pane-inbox"),
    ],
)
def test_cockpit_send_key_question_mark_prefers_content_pane_bridge(
    valid_cockpit_config: Path,
    selected_key: str,
    bridge_kind: str,
) -> None:
    import typer
    from typer.testing import CliRunner

    from pollypm.cli_features.ui import register_ui_commands
    from pollypm.cockpit_rail import CockpitRouter

    cockpit_app = _FakeApp()
    content_app = _FakeApp()
    cockpit = start_input_bridge(
        cockpit_app, kind="cockpit", config_path=valid_cockpit_config,
    )
    assert cockpit is not None
    content = start_input_bridge(
        content_app, kind=bridge_kind, config_path=valid_cockpit_config,
    )
    assert content is not None
    try:
        CockpitRouter(valid_cockpit_config).set_selected_key(selected_key)

        app = typer.Typer()
        register_ui_commands(app)
        result = CliRunner().invoke(
            app, ["cockpit-send-key", "?", "--config", str(valid_cockpit_config)]
        )
        assert result.exit_code == 0, result.output
        assert f"via {content.socket_path}" in result.output
        assert _wait_for(lambda: content_app.keys == ["question_mark"])
        assert cockpit_app.keys == []
    finally:
        cockpit.stop()
        content.stop()


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("<pgdn>", "pagedown"),
        ("<space>", "space"),
        ("f", "f"),
        ("<down>", "down"),
    ],
)
def test_cockpit_send_key_help_modal_controls_stay_on_content_bridge(
    valid_cockpit_config: Path,
    key: str,
    expected: str,
) -> None:
    import typer
    from typer.testing import CliRunner

    from pollypm.cli_features.ui import register_ui_commands
    from pollypm.cockpit_rail import CockpitRouter

    cockpit_app = _FakeApp()
    dashboard_app = _FakeApp()
    cockpit = start_input_bridge(
        cockpit_app, kind="cockpit", config_path=valid_cockpit_config,
    )
    assert cockpit is not None
    dashboard = start_input_bridge(
        dashboard_app, kind="dashboard", config_path=valid_cockpit_config,
    )
    assert dashboard is not None
    try:
        CockpitRouter(valid_cockpit_config).set_selected_key("dashboard")

        app = typer.Typer()
        register_ui_commands(app)
        result = CliRunner().invoke(
            app, ["cockpit-send-key", "?", "--config", str(valid_cockpit_config)]
        )
        assert result.exit_code == 0, result.output
        assert f"via {dashboard.socket_path}" in result.output
        assert _wait_for(lambda: dashboard_app.keys == ["question_mark"])

        result = CliRunner().invoke(
            app, ["cockpit-send-key", key, "--config", str(valid_cockpit_config)]
        )
        assert result.exit_code == 0, result.output
        assert f"via {dashboard.socket_path}" in result.output
        assert _wait_for(
            lambda: dashboard_app.keys == ["question_mark", expected]
        )
        assert cockpit_app.keys == []
    finally:
        cockpit.stop()
        dashboard.stop()


def test_cockpit_send_key_help_modal_dismiss_clears_content_bridge(
    valid_cockpit_config: Path,
) -> None:
    import typer
    from typer.testing import CliRunner

    from pollypm.cli_features.ui import register_ui_commands
    from pollypm.cockpit_rail import CockpitRouter

    cockpit_app = _FakeApp()
    dashboard_app = _FakeApp()
    cockpit = start_input_bridge(
        cockpit_app, kind="cockpit", config_path=valid_cockpit_config,
    )
    assert cockpit is not None
    dashboard = start_input_bridge(
        dashboard_app, kind="dashboard", config_path=valid_cockpit_config,
    )
    assert dashboard is not None
    try:
        CockpitRouter(valid_cockpit_config).set_selected_key("dashboard")

        app = typer.Typer()
        register_ui_commands(app)
        result = CliRunner().invoke(
            app, ["cockpit-send-key", "?", "--config", str(valid_cockpit_config)]
        )
        assert result.exit_code == 0, result.output
        assert _wait_for(lambda: dashboard_app.keys == ["question_mark"])

        result = CliRunner().invoke(
            app, ["cockpit-send-key", "<esc>", "--config", str(valid_cockpit_config)]
        )
        assert result.exit_code == 0, result.output
        assert f"via {dashboard.socket_path}" in result.output
        assert _wait_for(
            lambda: dashboard_app.keys == ["question_mark", "escape"]
        )

        result = CliRunner().invoke(
            app, ["cockpit-send-key", "<down>", "--config", str(valid_cockpit_config)]
        )
        assert result.exit_code == 0, result.output
        assert f"via {cockpit.socket_path}" in result.output
        assert _wait_for(lambda: cockpit_app.keys == ["down"])
    finally:
        cockpit.stop()
        dashboard.stop()


def test_help_modal_control_key_survives_home_alias_change(
    valid_cockpit_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pollypm.cli_features import ui as ui_commands
    import pollypm.cockpit_input_bridge as input_bridge
    from pollypm.cockpit_rail import CockpitRouter

    calls: list[tuple[str, str | None]] = []

    def fake_send_key_to_first_live(
        _config_path: Path,
        key: str,
        *,
        kind: str | None = None,
        timeout: float = 2.0,
    ) -> Path:
        calls.append((key, kind))
        return Path("/tmp/dashboard.sock")

    monkeypatch.setattr(
        input_bridge,
        "send_key_to_first_live",
        fake_send_key_to_first_live,
    )
    router = CockpitRouter(valid_cockpit_config)
    router.set_selected_key("polly")
    ui_commands._remember_help_modal_bridge(
        valid_cockpit_config,
        kind="dashboard",
        selected_key="polly",
    )

    router.set_selected_key("dashboard")

    assert (
        ui_commands._send_help_modal_key_to_recorded_bridge(
            valid_cockpit_config,
            "<pgdn>",
        )
        == Path("/tmp/dashboard.sock")
    )
    assert calls == [("<pgdn>", "dashboard")]


@pytest.mark.parametrize("selected_key", ["dashboard", "polly"])
def test_cockpit_send_key_question_mark_on_home_without_dashboard_bridge_stays_on_cockpit_bridge(
    valid_cockpit_config: Path,
    selected_key: str,
) -> None:
    import typer
    from typer.testing import CliRunner

    from pollypm.cli_features.ui import register_ui_commands
    from pollypm.cockpit_rail import CockpitRouter

    cockpit_app = _FakeApp()
    cockpit = start_input_bridge(
        cockpit_app, kind="cockpit", config_path=valid_cockpit_config,
    )
    assert cockpit is not None
    try:
        CockpitRouter(valid_cockpit_config).set_selected_key(selected_key)

        app = typer.Typer()
        register_ui_commands(app)
        result = CliRunner().invoke(
            app, ["cockpit-send-key", "?", "--config", str(valid_cockpit_config)]
        )
        assert result.exit_code == 0, result.output
        assert f"via {cockpit.socket_path}" in result.output
        assert _wait_for(lambda: cockpit_app.keys == ["question_mark"])
    finally:
        cockpit.stop()


def test_cockpit_send_key_question_mark_falls_back_to_visible_dashboard_bridge(
    valid_cockpit_config: Path,
) -> None:
    import typer
    from typer.testing import CliRunner

    from pollypm.cli_features.ui import register_ui_commands
    from pollypm.cockpit_rail import CockpitRouter

    cockpit_app = _FakeApp()
    dashboard_app = _FakeApp()
    cockpit = start_input_bridge(
        cockpit_app, kind="cockpit", config_path=valid_cockpit_config,
    )
    assert cockpit is not None
    dashboard = start_input_bridge(
        dashboard_app, kind="dashboard", config_path=valid_cockpit_config,
    )
    assert dashboard is not None
    try:
        CockpitRouter(valid_cockpit_config).set_selected_key("inbox")

        app = typer.Typer()
        register_ui_commands(app)
        result = CliRunner().invoke(
            app, ["cockpit-send-key", "?", "--config", str(valid_cockpit_config)]
        )
        assert result.exit_code == 0, result.output
        assert f"via {dashboard.socket_path}" in result.output
        assert _wait_for(lambda: dashboard_app.keys == ["question_mark"])
        assert "question_mark" not in cockpit_app.keys
    finally:
        cockpit.stop()
        dashboard.stop()


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("d", "d"),
        ("/", "/"),
        ("a", "a"),
        ("A", "A"),
    ],
)
def test_cockpit_send_key_inbox_action_prefers_inbox_bridge_over_live_pane(
    valid_cockpit_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    expected: str,
) -> None:
    import typer
    from typer.testing import CliRunner

    from pollypm.cli_features import ui as ui_commands
    from pollypm.cockpit_rail import CockpitRouter

    inbox_app = _FakeApp()
    inbox = start_input_bridge(
        inbox_app, kind="pane-inbox", config_path=valid_cockpit_config,
    )
    assert inbox is not None
    live_pane_attempts: list[tuple[Path, str]] = []

    def fake_live_pane(config_path: Path, key: str) -> str | None:
        live_pane_attempts.append((config_path, key))
        return "%2"

    monkeypatch.setattr(
        ui_commands,
        "_send_key_to_active_live_right_pane",
        fake_live_pane,
    )
    try:
        CockpitRouter(valid_cockpit_config).set_selected_key("inbox")

        app = typer.Typer()
        ui_commands.register_ui_commands(app)
        result = CliRunner().invoke(
            app, ["cockpit-send-key", key, "--config", str(valid_cockpit_config)]
        )
        assert result.exit_code == 0, result.output
        assert f"via {inbox.socket_path}" in result.output
        assert _wait_for(lambda: inbox_app.keys == [expected])
        assert live_pane_attempts == []
    finally:
        inbox.stop()


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("j", "j"),
        ("k", "k"),
        ("<down>", "down"),
        ("<up>", "up"),
    ],
)
def test_cockpit_send_key_inbox_nav_keeps_default_cockpit_bridge(
    valid_cockpit_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    expected: str,
) -> None:
    """#1246: rail navigation reaches the cockpit before Inbox fallback."""
    import typer
    from typer.testing import CliRunner

    from pollypm.cli_features import ui as ui_commands
    from pollypm.cockpit_rail import CockpitRouter

    cockpit_app = _FakeApp()
    inbox_app = _FakeApp()
    cockpit = start_input_bridge(
        cockpit_app, kind="cockpit", config_path=valid_cockpit_config,
    )
    assert cockpit is not None
    inbox = start_input_bridge(
        inbox_app, kind="pane-inbox", config_path=valid_cockpit_config,
    )
    assert inbox is not None

    monkeypatch.setattr(
        ui_commands,
        "_send_key_to_active_live_right_pane",
        lambda _config_path, _key: None,
    )
    try:
        CockpitRouter(valid_cockpit_config).set_selected_key("inbox")

        app = typer.Typer()
        ui_commands.register_ui_commands(app)
        result = CliRunner().invoke(
            app, ["cockpit-send-key", key, "--config", str(valid_cockpit_config)]
        )
        assert result.exit_code == 0, result.output
        assert f"via {cockpit.socket_path}" in result.output
        assert _wait_for(lambda: cockpit_app.keys == [expected])
        assert inbox_app.keys == []
    finally:
        cockpit.stop()
        inbox.stop()


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("j", "j"),
        ("k", "k"),
        ("<down>", "down"),
        ("<up>", "up"),
        ("<tab>", "tab"),
    ],
)
def test_cockpit_send_key_settings_nav_prefers_settings_bridge_over_live_pane(
    valid_cockpit_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    expected: str,
) -> None:
    import typer
    from typer.testing import CliRunner

    from pollypm.cli_features import ui as ui_commands
    from pollypm.cockpit_rail import CockpitRouter

    settings_app = _FakeApp()
    settings = start_input_bridge(
        settings_app, kind="settings", config_path=valid_cockpit_config,
    )
    assert settings is not None
    live_pane_attempts: list[tuple[Path, str]] = []

    def fake_live_pane(config_path: Path, key: str) -> str | None:
        live_pane_attempts.append((config_path, key))
        return "%2"

    monkeypatch.setattr(
        ui_commands,
        "_send_key_to_active_live_right_pane",
        fake_live_pane,
    )
    try:
        CockpitRouter(valid_cockpit_config).set_selected_key("settings")

        app = typer.Typer()
        ui_commands.register_ui_commands(app)
        result = CliRunner().invoke(
            app, ["cockpit-send-key", key, "--config", str(valid_cockpit_config)]
        )
        assert result.exit_code == 0, result.output
        assert f"via {settings.socket_path}" in result.output
        assert _wait_for(lambda: settings_app.keys == [expected])
        assert live_pane_attempts == []
    finally:
        settings.stop()


def test_cockpit_send_key_forwards_to_focused_live_right_pane(
    fake_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import typer
    from typer.testing import CliRunner

    from pollypm.cli_features import ui as ui_commands

    cockpit_app = _FakeApp()
    cockpit = start_input_bridge(cockpit_app, kind="cockpit", config_path=fake_config)
    assert cockpit is not None
    forwarded: list[tuple[Path, str]] = []

    def fake_forward(config_path: Path, key: str) -> str | None:
        forwarded.append((config_path, key))
        return "%2"

    monkeypatch.setattr(
        ui_commands,
        "_send_key_to_active_live_right_pane",
        fake_forward,
    )

    try:
        app = typer.Typer()
        ui_commands.register_ui_commands(app)
        result = CliRunner().invoke(
            app, ["cockpit-send-key", "s", "--config", str(fake_config)]
        )
        assert result.exit_code == 0, result.output
        assert "via cockpit right pane %2" in result.output
        assert forwarded == [(fake_config, "s")]
        time.sleep(0.1)
        assert cockpit_app.keys == []
    finally:
        cockpit.stop()


def test_cockpit_send_key_enter_consumes_network_dead_for_live_right_pane(
    valid_cockpit_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pollypm.cockpit_rail as cockpit_rail
    from pollypm.cli_features import ui as ui_commands
    from pollypm.dev_network_simulation import arm_network_dead, network_dead_armed

    class FakeTmux:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def run(self, *args: object, **kwargs: object) -> None:
            self.calls.append(("run", *args, kwargs))

        def send_keys(
            self, target: str, text: str, *, press_enter: bool = True,
        ) -> None:
            self.calls.append(("send_keys", target, text, press_enter))

    class FakeRouter:
        def __init__(self) -> None:
            self.tmux = FakeTmux()
            self.state: dict[str, object] = {}

        def active_live_right_pane_id(self) -> str:
            return "%2"

        def _load_state(self) -> dict[str, object]:
            return dict(self.state)

        def _write_state(self, state: dict[str, object]) -> None:
            self.state = dict(state)

    router = FakeRouter()
    monkeypatch.setattr(cockpit_rail, "CockpitRouter", lambda _path: router)

    arm_network_dead(valid_cockpit_config)

    delivered = ui_commands._send_key_to_active_live_right_pane(
        valid_cockpit_config, "<cr>",
    )

    assert delivered == "%2"
    assert not network_dead_armed(valid_cockpit_config)
    assert router.tmux.calls == [
        ("run", "send-keys", "-t", "%2", "C-u", {"check": False}),
        (
            "send_keys",
            "%2",
            "PollyPM chat failed: network unreachable. "
            "Type again to clear this and retry; check connection.",
            False,
        ),
    ]
    assert router.state["live_chat_network_dead_prompt_active"] is True


def test_cockpit_send_key_after_network_dead_prompt_clears_before_typing(
    valid_cockpit_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pollypm.cockpit_rail as cockpit_rail
    from pollypm.cli_features import ui as ui_commands

    class FakeTmux:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def run(self, *args: object, **kwargs: object) -> None:
            self.calls.append(("run", *args, kwargs))

        def send_keys(
            self, target: str, text: str, *, press_enter: bool = True,
        ) -> None:
            self.calls.append(("send_keys", target, text, press_enter))

    class FakeRouter:
        def __init__(self) -> None:
            self.tmux = FakeTmux()
            self.state = {
                "mounted_session": "operator",
                "right_pane_id": "%2",
                "live_right_pane_input_sticky": True,
                "live_chat_network_dead_prompt_active": True,
            }

        def active_live_right_pane_id(self) -> str:
            return "%2"

        def _load_state(self) -> dict[str, object]:
            return dict(self.state)

        def _write_state(self, state: dict[str, object]) -> None:
            self.state = dict(state)

    router = FakeRouter()
    monkeypatch.setattr(cockpit_rail, "CockpitRouter", lambda _path: router)

    delivered = ui_commands._send_key_to_active_live_right_pane(
        valid_cockpit_config, "h",
    )

    assert delivered == "%2"
    assert router.tmux.calls == [
        ("run", "send-keys", "-t", "%2", "C-u", {"check": False}),
        ("send_keys", "%2", "h", False),
    ]
    assert "live_chat_network_dead_prompt_active" not in router.state


def test_cockpit_send_key_keeps_live_right_pane_sticky_after_first_char(
    valid_cockpit_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pollypm.cockpit_rail as cockpit_rail
    from pollypm.cli_features import ui as ui_commands

    class FakeTmux:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def list_panes(self, target: str):
            assert target == "pollypm-test:PollyPM"
            return [
                SimpleNamespace(
                    pane_id="%1", active=True, pane_dead=False,
                    pane_current_command="uv",
                ),
                SimpleNamespace(
                    pane_id="%2", active=False, pane_dead=False,
                    pane_current_command="codex",
                ),
            ]

        def send_keys(
            self, target: str, text: str, *, press_enter: bool = True,
        ) -> None:
            self.calls.append(("send_keys", target, text, press_enter))

    class FakeRouter:
        _COCKPIT_WINDOW = "PollyPM"

        def __init__(self) -> None:
            self.tmux = FakeTmux()
            self.state = {
                "mounted_session": "architect_demo",
                "right_pane_id": "%2",
            }
            self.active_calls = 0

        def active_live_right_pane_id(self) -> str | None:
            self.active_calls += 1
            return "%2" if self.active_calls == 1 else None

        def _load_state(self) -> dict[str, object]:
            return dict(self.state)

        def _write_state(self, state: dict[str, object]) -> None:
            self.state = dict(state)

        def _load_config(self):
            return SimpleNamespace(
                project=SimpleNamespace(tmux_session="pollypm-test")
            )

        def _is_live_provider_pane(self, pane: object) -> bool:
            return getattr(pane, "pane_current_command", None) == "codex"

    router = FakeRouter()
    monkeypatch.setattr(cockpit_rail, "CockpitRouter", lambda _path: router)

    assert ui_commands._send_key_to_active_live_right_pane(
        valid_cockpit_config, "R",
    ) == "%2"
    assert router.state["live_right_pane_input_sticky"] is True

    assert ui_commands._send_key_to_active_live_right_pane(
        valid_cockpit_config, "e",
    ) == "%2"
    assert router.tmux.calls == [
        ("send_keys", "%2", "R", False),
        ("send_keys", "%2", "e", False),
    ]


def test_cockpit_send_key_escape_clears_live_right_pane_sticky(
    valid_cockpit_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pollypm.cockpit_rail as cockpit_rail
    from pollypm.cli_features import ui as ui_commands

    class FakeRouter:
        def __init__(self) -> None:
            self.state = {
                "mounted_session": "architect_demo",
                "right_pane_id": "%2",
                "live_right_pane_input_sticky": True,
                "live_chat_network_dead_prompt_active": True,
            }

        def active_live_right_pane_id(self) -> str | None:
            return "%2"

        def _load_state(self) -> dict[str, object]:
            return dict(self.state)

        def _write_state(self, state: dict[str, object]) -> None:
            self.state = dict(state)

    router = FakeRouter()
    monkeypatch.setattr(cockpit_rail, "CockpitRouter", lambda _path: router)

    assert ui_commands._send_key_to_active_live_right_pane(
        valid_cockpit_config, "<esc>",
    ) is None
    assert "live_right_pane_input_sticky" not in router.state
    assert "live_chat_network_dead_prompt_active" not in router.state


def test_send_key_to_first_live_skips_stale_sockets(fake_config: Path, tmp_path: Path) -> None:
    bridge_dir = fake_config.parent / "cockpit_inputs"
    bridge_dir.mkdir(parents=True, exist_ok=True)
    # Drop a stale socket file that nobody is listening on. Use a real
    # AF_UNIX socket but close it immediately so connect() fails — the
    # bridge should unlink it and move on.
    stale = bridge_dir / "cockpit-99999.sock"
    stale_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale_sock.bind(str(stale))
    stale_sock.close()
    # ``stale`` now exists as a file but isn't being accepted on.

    app = _FakeApp()
    handle = start_input_bridge(app, kind="cockpit", config_path=fake_config)
    assert handle is not None
    try:
        # If list_bridge_sockets sorts newest-first, the live socket
        # should be picked first; but we want to verify resilience to
        # stale entries either way.
        delivered = send_key_to_first_live(fake_config, "G", kind="cockpit")
        assert delivered == handle.socket_path
        assert _wait_for(lambda: app.keys == ["G"])
    finally:
        handle.stop()


def test_send_key_to_first_live_returns_none_when_no_bridge(fake_config: Path) -> None:
    delivered = send_key_to_first_live(fake_config, "I", kind="cockpit")
    assert delivered is None


def test_bridge_dispatch_does_not_block_on_call_from_thread(fake_config: Path) -> None:
    app = _FakeApp()
    app.raise_runtime_error = True
    handle = start_input_bridge(app, kind="cockpit", config_path=fake_config)
    assert handle is not None
    try:
        send_key(handle.socket_path, "I")
        assert _wait_for(lambda: app.keys == ["I"])
        assert app.call_from_thread_calls == 0
    finally:
        handle.stop()


def test_bridge_handles_simulate_key_error_gracefully(fake_config: Path) -> None:
    app = _FakeApp()
    app.raise_simulate_error = True
    handle = start_input_bridge(app, kind="cockpit", config_path=fake_config)
    assert handle is not None
    try:
        send_key(handle.socket_path, "I")
        time.sleep(0.2)
        assert app.keys == []
        app.raise_simulate_error = False
        send_key(handle.socket_path, "J")
        assert _wait_for(lambda: app.keys == ["J"])
    finally:
        handle.stop()


def test_send_key_to_first_live_keeps_refused_socket_when_owner_pid_alive(
    fake_config: Path,
) -> None:
    bridge_dir = fake_config.parent / "cockpit_inputs"
    bridge_dir.mkdir(parents=True, exist_ok=True)
    refused = bridge_dir / f"cockpit-{os.getpid()}.sock"
    stale_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale_sock.bind(str(refused))
    stale_sock.close()
    try:
        delivered = send_key_to_first_live(fake_config, "I", kind="cockpit")
        assert delivered is None
        assert refused.exists()
    finally:
        try:
            refused.unlink()
        except OSError:
            pass


def test_bridge_returns_handle_with_correct_filename_pattern(fake_config: Path) -> None:
    app = _FakeApp()
    handle = start_input_bridge(app, kind="pane-inbox", config_path=fake_config)
    assert handle is not None
    try:
        # Non-alphanumeric chars in `kind` should be sanitized.
        assert "pane_inbox" in handle.socket_path.name
        assert handle.socket_path.name.endswith(".sock")
    finally:
        handle.stop()
