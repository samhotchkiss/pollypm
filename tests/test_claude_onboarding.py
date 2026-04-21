from __future__ import annotations

import json
from pathlib import Path

from pollypm.providers.claude.onboarding import detected_claude_version, prime_claude_home
from pollypm.providers.claude.provider import ClaudeProvider


def test_claude_provider_login_helpers() -> None:
    provider = ClaudeProvider()

    assert provider.login_command(interactive=True) == "claude"
    assert provider.login_command() == "claude auth login --claudeai"
    assert provider.logout_command() == "claude auth logout || true"
    assert provider.login_completion_marker_seen("PollyPM: login window complete.")
    assert provider.detect_email_from_pane("Logged in as pearl@example.com") is None


def test_prime_claude_home_writes_state_inside_config_dir(tmp_path: Path) -> None:
    home = tmp_path / "homes" / "claude_test"

    prime_claude_home(home)

    state_path = home / ".claude" / ".claude.json"
    settings_path = home / ".claude" / "settings.json"

    assert state_path.exists()
    assert settings_path.exists()

    state = json.loads(state_path.read_text())
    settings = json.loads(settings_path.read_text())

    assert state["hasCompletedOnboarding"] is True
    assert settings["skipDangerousModePermissionPrompt"] is True
    assert settings["bypassWorkspaceTrust"] is True
    assert settings["permissions"]["dangerouslySkipPermissions"] is True


def test_detected_claude_version_uses_binary_output(monkeypatch) -> None:
    class Result:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout

    monkeypatch.setattr(
        "pollypm.providers.claude.onboarding.subprocess.run",
        lambda *args, **kwargs: Result("Claude Code 3.2.1\n"),
    )

    assert detected_claude_version() == "3.2.1"
