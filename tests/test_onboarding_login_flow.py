from pathlib import Path

from pollypm.models import ProviderKind
import pytest

from pollypm.onboarding import LoginCancelled, _run_login_window, _wait_for_login_completion


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


def test_run_login_window_cancelled_attach_returns_cleanly_to_caller(tmp_path: Path, monkeypatch) -> None:
    calls: dict[str, object] = {}
    session_alive = False

    class FakeTmux:
        def current_session_name(self):
            return None

        def has_session(self, name: str) -> bool:
            return session_alive

        def kill_session(self, name: str) -> None:
            nonlocal session_alive
            calls["killed"] = name
            session_alive = False

        def create_session(self, name: str, window_name: str, command: str, *, remain_on_exit: bool = True) -> None:
            nonlocal session_alive
            session_alive = True

        def attach_session(self, name: str) -> int:
            calls["attached"] = name
            return 1

    monkeypatch.setattr(
        "pollypm.onboarding._wait_for_login_completion",
        lambda *args, **kwargs: (False, ""),
    )

    with pytest.raises(LoginCancelled):
        _run_login_window(
            FakeTmux(),
            provider=ProviderKind.CODEX,
            home=tmp_path / "codex-home",
            window_label="onboard-codex-1",
            quiet=True,
        )

    assert calls["attached"] == "pollypm-login-onboard-codex-1"
    assert calls["killed"] == "pollypm-login-onboard-codex-1"


def test_wait_for_login_completion_force_fresh_auth_uses_email_detection(monkeypatch, tmp_path):
    """With force_fresh_auth=True, detection of an email at the new home
    counts as completion once the post-logout marker has been seen in the pane.
    This unblocks the Claude REPL case where the printf completion marker only
    fires after the user exits the REPL — which they shouldn't have to do.
    The post-logout sentinel ensures stale credentials are cleared first."""
    from pollypm.onboarding import _wait_for_login_completion
    from pollypm.models import ProviderKind

    home = tmp_path / "home"
    home.mkdir()

    # Pane sequence: first poll is empty, second poll contains the
    # post-logout marker, subsequent polls continue showing it (sticky).
    pane_sequence = [
        "",
        "\nPollyPM: logout-complete\nWelcome back, Sam!",
    ]
    pane_iter = iter(pane_sequence + [pane_sequence[-1]] * 20)

    class FakeTmux:
        def capture_pane(self, target, lines):
            try:
                return next(pane_iter)
            except StopIteration:
                return pane_sequence[-1]

    monkeypatch.setattr(
        "pollypm.onboarding._detect_account_email",
        lambda provider, h: "backup@example.com",
    )

    completed, pane = _wait_for_login_completion(
        FakeTmux(),
        target="dummy",
        provider=ProviderKind.CLAUDE,
        home=home,
        allow_existing_auth_shortcut=False,
        force_fresh_auth=True,
        timeout_seconds=5,
        poll_interval=0.1,
    )
    assert completed is True


def test_wait_for_login_completion_force_fresh_does_not_short_circuit_on_stale_email(monkeypatch, tmp_path):
    """When force_fresh_auth=True and the home still has stale credentials
    BEFORE the logout step completes, polling MUST NOT use that stale email
    as a completion signal. Only after seeing the post-logout marker can
    email detection trigger completion. (Codex round-1 review of #2094.)"""
    from pollypm.onboarding import _wait_for_login_completion
    from pollypm.models import ProviderKind

    home = tmp_path / "home"
    home.mkdir()

    panes = ["", "", "", ""]  # capture_pane returns sequential empty snapshots
    pane_iter = iter(panes)

    class FakeTmux:
        def capture_pane(self, target, lines):
            try:
                return next(pane_iter)
            except StopIteration:
                return ""

    # The stale email IS present at the home from the start.
    monkeypatch.setattr(
        "pollypm.onboarding._detect_account_email",
        lambda provider, h: "stale@example.com",
    )
    monkeypatch.setattr("pollypm.onboarding.time.sleep", lambda seconds: None)

    # Polling should time out, NOT short-circuit on the stale email.
    completed, _pane = _wait_for_login_completion(
        FakeTmux(),
        target="dummy",
        provider=ProviderKind.CLAUDE,
        home=home,
        allow_existing_auth_shortcut=False,
        force_fresh_auth=True,
        timeout_seconds=0.01,
        poll_interval=0,
    )
    assert completed is False


def test_wait_for_login_completion_force_fresh_succeeds_after_logout_marker(monkeypatch, tmp_path):
    """After the post-logout marker is seen in the pane, email detection
    is accepted as completion. (Codex round-1 review of #2094.)"""
    from pollypm.onboarding import _wait_for_login_completion
    from pollypm.models import ProviderKind

    home = tmp_path / "home"
    home.mkdir()

    # Sequence: first poll has no marker; second poll has the post-logout
    # marker; subsequent polls keep it (sticky).
    panes = [
        "",
        "\nPollyPM: logout-complete\n",
        "\nPollyPM: logout-complete\n",
    ]
    pane_iter = iter(panes + [panes[-1]] * 20)  # repeat the last one

    class FakeTmux:
        def capture_pane(self, target, lines):
            try:
                return next(pane_iter)
            except StopIteration:
                return panes[-1]

    monkeypatch.setattr(
        "pollypm.onboarding._detect_account_email",
        lambda provider, h: "fresh@example.com",
    )

    completed, _pane = _wait_for_login_completion(
        FakeTmux(),
        target="dummy",
        provider=ProviderKind.CLAUDE,
        home=home,
        allow_existing_auth_shortcut=False,
        force_fresh_auth=True,
        timeout_seconds=5,
        poll_interval=0.1,
    )
    assert completed is True


def test_wait_for_login_completion_without_force_fresh_does_not_short_circuit(monkeypatch, tmp_path):
    """Without force_fresh_auth, and with allow_existing_auth_shortcut=False,
    email detection alone does NOT trigger completion — protects against
    Keychain-inherited sessions being mistaken for fresh login."""
    from pollypm.onboarding import _wait_for_login_completion
    from pollypm.models import ProviderKind

    home = tmp_path / "home"
    home.mkdir()

    class FakeTmux:
        def capture_pane(self, target, lines):
            return "Welcome"

    monkeypatch.setattr(
        "pollypm.onboarding._detect_account_email",
        lambda provider, h: "leaked@example.com",
    )

    monkeypatch.setattr("pollypm.onboarding.time.sleep", lambda seconds: None)

    # Should time out (return False) — neither marker nor force_fresh signal.
    completed, _pane = _wait_for_login_completion(
        FakeTmux(),
        target="dummy",
        provider=ProviderKind.CLAUDE,
        home=home,
        allow_existing_auth_shortcut=False,
        force_fresh_auth=False,
        timeout_seconds=0.01,
        poll_interval=0,
    )
    assert completed is False
