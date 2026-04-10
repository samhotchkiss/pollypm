import base64
import json
from pathlib import Path

from pollypm.models import ProviderKind
from pollypm.onboarding import (
    _build_login_shell,
    _detect_claude_email,
    _connect_account_via_tmux,
    _detect_codex_email,
    _detect_email_from_pane,
    _login_completion_marker_seen,
    _provider_choices,
    _slugify_email,
    CliAvailability,
    ConnectedAccount,
    LoginPreferences,
)


def test_slugify_email_generates_stable_account_name() -> None:
    assert _slugify_email(ProviderKind.CODEX, "Jane.Doe+work@example.com") == "codex_jane_doe_work_example_com"


def test_detect_codex_email_reads_isolated_codex_auth_file(tmp_path: Path) -> None:
    payload = {"email": "jane@example.com"}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    token = f"header.{encoded}.sig"
    auth_path = tmp_path / ".codex" / "auth.json"
    auth_path.parent.mkdir(parents=True)
    auth_path.write_text(json.dumps({"tokens": {"id_token": token}}))

    assert _detect_codex_email(tmp_path) == "jane@example.com"


def test_detect_codex_email_from_status_pane() -> None:
    pane = """
    Account:                     s@swh.me (Pro)
    Weekly limit:                [████████████████████] 100% left
    """
    assert _detect_email_from_pane(ProviderKind.CODEX, pane) == "s@swh.me"


def test_login_completion_marker_seen() -> None:
    assert _login_completion_marker_seen("PollyPM: login window complete.")


def test_login_shell_exports_provider_native_profile_envs(tmp_path: Path) -> None:
    codex_shell = _build_login_shell(ProviderKind.CODEX, tmp_path / "codex-home")
    claude_shell = _build_login_shell(ProviderKind.CLAUDE, tmp_path / "claude-home")

    assert "export CODEX_HOME=" in codex_shell
    assert "export CLAUDE_CONFIG_DIR=" in claude_shell


def test_login_shell_can_return_to_onboarding_automatically(tmp_path: Path) -> None:
    shell = _build_login_shell(ProviderKind.CLAUDE, tmp_path / "claude-home", return_to_caller=True)

    assert "Returning to onboarding" in shell
    assert 'exec "${SHELL:-/bin/zsh}" -l' not in shell


def test_login_shell_can_launch_interactive_claude(tmp_path: Path) -> None:
    shell = _build_login_shell(ProviderKind.CLAUDE, tmp_path / "claude-home", interactive=True)

    assert "claude auth login" not in shell
    assert " claude" in shell


def test_login_shell_can_force_fresh_auth(tmp_path: Path) -> None:
    claude_shell = _build_login_shell(
        ProviderKind.CLAUDE,
        tmp_path / "claude-home",
        interactive=True,
        force_fresh_auth=True,
    )
    codex_shell = _build_login_shell(
        ProviderKind.CODEX,
        tmp_path / "codex-home",
        force_fresh_auth=True,
    )

    assert "claude auth logout || true" in claude_shell
    assert "codex logout || true" in codex_shell


def test_login_shell_can_use_headless_codex_auth(tmp_path: Path) -> None:
    shell = _build_login_shell(
        ProviderKind.CODEX,
        tmp_path / "codex-home",
        preferences=LoginPreferences(codex_headless=True),
    )

    assert "codex login --device-auth" in shell


def test_detect_email_from_pane_only_trusts_codex_status_line() -> None:
    assert _detect_email_from_pane(ProviderKind.CLAUDE, "Logged in as pearl@swh.me") is None


def test_detect_claude_email_requires_logged_in_flag(monkeypatch, tmp_path: Path) -> None:
    class Result:
        def __init__(self, stdout: str, returncode: int = 0) -> None:
            self.stdout = stdout
            self.returncode = returncode

    monkeypatch.setattr(
        "pollypm.onboarding.subprocess.run",
        lambda *args, **kwargs: Result('{"loggedIn": false, "email": "pearl@swh.me"}'),
    )

    assert _detect_claude_email(tmp_path) is None


def test_provider_choices_hide_continue_before_first_account(tmp_path: Path) -> None:
    installed = [
        CliAvailability(provider=ProviderKind.CLAUDE, label="Claude CLI", binary="claude", installed=True),
        CliAvailability(provider=ProviderKind.CODEX, label="Codex CLI", binary="codex", installed=True),
    ]

    first_choices = _provider_choices(installed, {})
    later_choices = _provider_choices(
        installed,
        {
            "claude_demo": ConnectedAccount(
                provider=ProviderKind.CLAUDE,
                email="demo@example.com",
                account_name="claude_demo",
                home=tmp_path / "claude_demo",
            )
        },
    )

    assert [item.label for item in first_choices] == ["Claude CLI", "Codex CLI"]
    assert [item.label for item in later_choices] == ["Claude CLI (1 connected)", "Codex CLI", "Continue"]


def test_connect_account_via_tmux_requires_verified_claude_auth(monkeypatch, tmp_path: Path) -> None:
    class FakeTmux:
        pass

    monkeypatch.setattr("pollypm.onboarding._run_login_window", lambda *args, **kwargs: "PollyPM: login window complete.")
    monkeypatch.setattr("pollypm.onboarding._detect_account_email", lambda provider, home: None)

    import pytest

    with pytest.raises(Exception) as exc:
        _connect_account_via_tmux(
            FakeTmux(),
            root_dir=tmp_path,
            provider=ProviderKind.CLAUDE,
            index=1,
            quiet=True,
        )

    assert "managed PollyPM profile is still not authenticated" in str(exc.value)
