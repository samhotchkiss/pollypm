from pathlib import Path

from pollypm.models import ProviderKind
from pollypm.onboarding import _run_login_window, _wait_for_login_completion


def test_run_login_window_outside_tmux_uses_non_persistent_temp_session(tmp_path: Path, monkeypatch) -> None:
    calls: dict[str, object] = {}
    session_alive = False

    class FakeTmux:
        def current_session_name(self):
            return None

        def has_session(self, name: str) -> bool:
            calls.setdefault("has_session", []).append(name)
            return session_alive

        def kill_session(self, name: str) -> None:
            nonlocal session_alive
            calls["killed"] = name
            session_alive = False

        def create_session(self, name: str, window_name: str, command: str, *, remain_on_exit: bool = True) -> None:
            nonlocal session_alive
            calls["created"] = (name, window_name, command, remain_on_exit)
            session_alive = True

        def attach_session(self, name: str) -> int:
            calls["attached"] = name
            return 0

    monkeypatch.setattr(
        "pollypm.onboarding._wait_for_login_completion",
        lambda *args, **kwargs: (True, "PollyPM: login window complete."),
    )

    pane_text = _run_login_window(
        FakeTmux(),
        provider=ProviderKind.CLAUDE,
        home=tmp_path / "claude-home",
        window_label="onboard-claude-1",
        quiet=True,
    )

    assert pane_text == "PollyPM: login window complete."
    assert calls["created"][0] == "pollypm-login-onboard-claude-1"
    assert calls["created"][3] is False
    assert calls["attached"] == "pollypm-login-onboard-claude-1"
    assert calls["killed"] == "pollypm-login-onboard-claude-1"


def test_wait_for_login_completion_requires_real_claude_auth(monkeypatch, tmp_path: Path) -> None:
    class FakeTmux:
        def capture_pane(self, target: str, lines: int = 200) -> str:
            return "Claude Code v2.1.92\nWelcome back\n❯ "

    monkeypatch.setattr("pollypm.onboarding.time.sleep", lambda seconds: None)
    monkeypatch.setattr("pollypm.onboarding._detect_account_email", lambda provider, home: None)
    monkeypatch.setattr("pollypm.onboarding._detect_email_from_pane", lambda provider, pane: None)

    completed, pane_text = _wait_for_login_completion(
        FakeTmux(),
        target="pollypm-login-onboard-claude-1:0",
        provider=ProviderKind.CLAUDE,
        home=tmp_path / "claude-home",
        timeout_seconds=0.01,
        poll_interval=0,
    )

    assert completed is False
    assert "Welcome back" in pane_text
