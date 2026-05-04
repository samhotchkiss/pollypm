"""Tests for the TTY-less cockpit input bridge (#1109 follow-up).

The bridge is the recovery path when the cockpit's Textual ``LinuxDriver``
stops processing keystrokes after the tmux session loses its attached
client. Spinning a real Textual ``App`` in a unit test is overkill — we
substitute a tiny stub that records ``simulate_key`` invocations and
implements ``call_from_thread`` as a synchronous dispatch.
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from pollypm.cockpit_input_bridge import (
    BridgeHandle,
    list_bridge_sockets,
    send_key,
    send_key_to_first_live,
    start_input_bridge,
)


class _FakeApp:
    """Minimal stand-in for ``textual.app.App``.

    The bridge only needs ``simulate_key`` (called via ``call_from_thread``)
    and ``call_from_thread`` itself. We make the latter synchronous so the
    tests can assert on key arrival without sleeping for an event loop.
    """

    def __init__(self) -> None:
        self.keys: list[str] = []
        self._lock = threading.Lock()
        self.raise_runtime_error = False

    def simulate_key(self, key: str) -> None:
        with self._lock:
            self.keys.append(key)

    def call_from_thread(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
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
def fake_config(tmp_path: Path) -> Path:
    """A fake config_path whose ``parent`` is the test's tmp dir.

    The bridge drops sockets in ``config_path.parent / 'cockpit_inputs'``,
    matching how it co-locates with ``cockpit_debug.log``.
    """
    config = tmp_path / "config.toml"
    config.write_text("# fake\n")
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


def test_bridge_handles_app_runtime_error_gracefully(fake_config: Path) -> None:
    """When the app stops mid-connection, the bridge logs and bails."""
    app = _FakeApp()
    app.raise_runtime_error = True
    handle = start_input_bridge(app, kind="cockpit", config_path=fake_config)
    assert handle is not None
    try:
        # Should not crash even though call_from_thread raises.
        send_key(handle.socket_path, "I")
        # Give the listener a moment to process and bail.
        time.sleep(0.2)
        assert app.keys == []
    finally:
        handle.stop()


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
