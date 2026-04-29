"""Tests for tmux session hardening (idempotency, health checks, teardown, recovery)."""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from pollypm.tmux.client import DeadPaneError, TmuxClient, TmuxWindow, recover_session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def _fail(stderr: str = "", returncode: int = 1) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


def _make_window(session: str = "s", name: str = "w", pane_id: str = "%0", dead: bool = False) -> TmuxWindow:
    return TmuxWindow(
        session=session, index=0, name=name, active=True,
        pane_id=pane_id, pane_current_command="bash",
        pane_current_path="/tmp", pane_dead=dead,
    )


# ---------------------------------------------------------------------------
# Idempotent session operations
# ---------------------------------------------------------------------------

class TestCreateSessionIdempotent:
    def test_second_call_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Call create_session twice with same name. Second call is a no-op."""
        client = TmuxClient()
        calls: list[tuple[str, ...]] = []

        def fake_run(*args, check=True, **kwargs):
            calls.append(args)
            return _ok()

        monkeypatch.setattr(client, "run", fake_run)
        # First call: has_session returns False, then create proceeds
        monkeypatch.setattr(client, "has_session", lambda name: False)
        client.create_session("test-sess", "main", "bash")

        # Second call: has_session returns True, should not call run again
        calls.clear()
        monkeypatch.setattr(client, "has_session", lambda name: True)
        client.create_session("test-sess", "main", "bash")
        assert calls == [], "No tmux commands should be issued for existing session"


class TestCreateWindowIdempotent:
    def test_second_call_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Call create_window twice with same name. Second call is a no-op."""
        client = TmuxClient()
        calls: list[tuple[str, ...]] = []

        def fake_run(*args, check=True, **kwargs):
            calls.append(args)
            return _ok()

        monkeypatch.setattr(client, "run", fake_run)
        monkeypatch.setattr(client, "has_session", lambda name: True)
        monkeypatch.setattr(client, "list_windows", lambda name: [_make_window(name="existing-win")])

        # Window with same name already exists
        client.create_window("test-sess", "existing-win", "bash")
        assert calls == [], "No tmux commands should be issued for existing window"

    def test_creates_when_name_differs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """create_window proceeds when window name doesn't match existing ones."""
        client = TmuxClient()
        calls: list[tuple[str, ...]] = []

        def fake_run(*args, check=True, **kwargs):
            calls.append(args)
            return _ok()

        monkeypatch.setattr(client, "run", fake_run)
        monkeypatch.setattr(client, "has_session", lambda name: True)
        monkeypatch.setattr(client, "list_windows", lambda name: [_make_window(name="other-win")])

        client.create_window("test-sess", "new-win", "bash")
        # #963/#966 — two-phase launch: new-window opens an empty pane,
        # then ``respawn-pane`` (argv form) hands the launch command to
        # the new pane. Both calls are issued; no ``send-keys`` typing
        # layer (the abandoned #963 mechanism that broke in #966).
        assert len(calls) == 2, "Should issue new-window then respawn-pane"
        assert calls[0][0] == "new-window"
        assert "bash" not in calls[0], "new-window must not carry the command"
        assert calls[1][0] == "respawn-pane"
        assert "bash" in calls[1]
        assert "-k" in calls[1]
        assert "send-keys" not in calls[1]


class TestKillSessionIdempotent:
    def test_kill_twice_no_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Kill a session. Kill it again. No error."""
        client = TmuxClient()
        call_count = 0

        def fake_run(*args, check=True, **kwargs):
            nonlocal call_count
            call_count += 1
            return _ok()

        monkeypatch.setattr(client, "run", fake_run)

        # First kill: session exists
        monkeypatch.setattr(client, "has_session", lambda name: True)
        client.kill_session("test-sess")
        assert call_count == 1

        # Second kill: session gone
        call_count = 0
        monkeypatch.setattr(client, "has_session", lambda name: False)
        client.kill_session("test-sess")  # should not raise
        assert call_count == 0, "Should not call run when session is already gone"

    def test_kill_nonexistent_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Kill a session that never existed. No error."""
        client = TmuxClient()
        monkeypatch.setattr(client, "has_session", lambda name: False)
        # Should not raise
        client.kill_session("never-existed")


class TestKillWindowIdempotent:
    def test_kill_nonexistent_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Kill a window that doesn't exist. No error."""
        client = TmuxClient()
        monkeypatch.setattr(client, "run", lambda *args, **kwargs: _fail())
        # Should not raise
        client.kill_window("test-sess:nonexistent")


# ---------------------------------------------------------------------------
# send_keys dead pane
# ---------------------------------------------------------------------------

class TestSendKeysDeadPane:
    def test_send_keys_to_dead_pane_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Send keys to a pane that's dead. Clear DeadPaneError."""
        client = TmuxClient()
        monkeypatch.setattr(client, "is_pane_alive", lambda pane_id: False)

        with pytest.raises(DeadPaneError, match="dead pane"):
            client.send_keys("%42", "hello")

    def test_send_keys_to_live_pane_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Send keys to a live pane succeeds."""
        client = TmuxClient()
        calls: list[tuple[str, ...]] = []
        monkeypatch.setattr(client, "is_pane_alive", lambda pane_id: True)
        monkeypatch.setattr(client, "run", lambda *args, **kwargs: (calls.append(args), _ok())[1])

        client.send_keys("%42", "hello")
        assert len(calls) == 2  # send-keys text + send-keys Enter


# ---------------------------------------------------------------------------
# Health check helpers
# ---------------------------------------------------------------------------

class TestIsSessionAlive:
    def test_true_with_live_panes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Session exists with live panes -> True."""
        client = TmuxClient()
        monkeypatch.setattr(client, "has_session", lambda name: True)
        monkeypatch.setattr(client, "list_windows", lambda name: [_make_window(dead=False)])
        assert client.is_session_alive("test-sess") is True

    def test_false_no_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Session doesn't exist -> False."""
        client = TmuxClient()
        monkeypatch.setattr(client, "has_session", lambda name: False)
        assert client.is_session_alive("test-sess") is False

    def test_false_dead_panes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Session exists but all panes are dead -> False."""
        client = TmuxClient()
        monkeypatch.setattr(client, "has_session", lambda name: True)
        monkeypatch.setattr(client, "list_windows", lambda name: [
            _make_window(dead=True),
            _make_window(name="w2", pane_id="%1", dead=True),
        ])
        assert client.is_session_alive("test-sess") is False


class TestIsPaneAlive:
    def test_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pane exists and not dead -> True."""
        client = TmuxClient()
        monkeypatch.setattr(client, "run", lambda *args, **kwargs: _ok("%42\t0\t12345"))
        assert client.is_pane_alive("%42") is True

    def test_false_dead(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pane exists but is dead -> False."""
        client = TmuxClient()
        monkeypatch.setattr(client, "run", lambda *args, **kwargs: _ok("%42\t1\t12345"))
        assert client.is_pane_alive("%42") is False

    def test_false_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pane doesn't exist -> False."""
        client = TmuxClient()
        monkeypatch.setattr(client, "run", lambda *args, **kwargs: _ok("%99\t0\t12345"))
        assert client.is_pane_alive("%42") is False

    def test_false_on_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """tmux command fails -> False."""
        client = TmuxClient()
        monkeypatch.setattr(client, "run", lambda *args, **kwargs: _fail())
        assert client.is_pane_alive("%42") is False


class TestGetPanePid:
    def test_returns_pid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = TmuxClient()
        monkeypatch.setattr(client, "run", lambda *args, **kwargs: _ok("%42\t12345"))
        assert client.get_pane_pid("%42") == 12345

    def test_returns_none_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = TmuxClient()
        monkeypatch.setattr(client, "run", lambda *args, **kwargs: _ok("%99\t12345"))
        assert client.get_pane_pid("%42") is None


# ---------------------------------------------------------------------------
# Teardown helper
# ---------------------------------------------------------------------------

class TestTeardownSession:
    def test_captures_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Teardown a session, verify output was captured."""
        client = TmuxClient()
        monkeypatch.setattr(client, "has_session", lambda name: True)
        monkeypatch.setattr(client, "list_windows", lambda name: [
            _make_window(pane_id="%10"),
            _make_window(name="w2", pane_id="%11"),
        ])
        monkeypatch.setattr(client, "capture_pane", lambda target, lines=200: f"output-from-{target}")
        monkeypatch.setattr(client, "kill_session", lambda name: None)

        result = client.teardown_session("test-sess")
        assert result == {"%10": "output-from-%10", "%11": "output-from-%11"}

    def test_nonexistent_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Teardown a session that doesn't exist. No error, returns empty."""
        client = TmuxClient()
        monkeypatch.setattr(client, "has_session", lambda name: False)

        result = client.teardown_session("ghost")
        assert result == {}


# ---------------------------------------------------------------------------
# Recovery helper
# ---------------------------------------------------------------------------

class TestRecoverSession:
    def test_when_dead(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Session is dead. recover_session creates a new one. Returns True."""
        client = TmuxClient()
        monkeypatch.setattr(client, "is_session_alive", lambda name: False)
        monkeypatch.setattr(client, "kill_session", lambda name: None)
        created: list[tuple] = []
        monkeypatch.setattr(client, "create_session", lambda *a, **kw: created.append((a, kw)))

        result = recover_session(client, "worker-1", "bash", "/tmp")
        assert result is True
        assert len(created) == 1

    def test_when_alive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Session is alive. recover_session does nothing. Returns False."""
        client = TmuxClient()
        monkeypatch.setattr(client, "is_session_alive", lambda name: True)

        result = recover_session(client, "worker-1", "bash")
        assert result is False
