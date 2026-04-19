"""Tests for :mod:`pollypm.providers.claude.detect` (Phase B of #397).

Run with::

    HOME=/tmp/pytest-providers-claude uv run --with pytest \\
        pytest tests/providers/test_claude_detect.py -x

These cover the extracted email-detection helpers and — critically —
preserve the #396 behavior: ``loggedIn: true`` + ``email: null``
(Claude CLI 2.x Max) must return a non-None sentinel, not ``None``.
"""

from __future__ import annotations

import json
from pathlib import Path

from pollypm.providers.claude.detect import (
    claude_prompt_ready,
    detect_claude_email,
    detect_email_from_pane,
    detect_logged_in,
)


class _Result:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


def test_detect_claude_email_returns_email_when_present(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "pollypm.providers.claude.detect.subprocess.run",
        lambda *args, **kwargs: _Result(
            json.dumps({"loggedIn": True, "email": "Pearl@SWH.me"})
        ),
    )
    assert detect_claude_email(tmp_path) == "pearl@swh.me"


def test_detect_claude_email_returns_none_when_logged_out(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "pollypm.providers.claude.detect.subprocess.run",
        lambda *args, **kwargs: _Result(
            json.dumps({"loggedIn": False, "email": "pearl@swh.me"})
        ),
    )
    assert detect_claude_email(tmp_path) is None


def test_detect_claude_email_preserves_issue_396_max_sentinel(
    monkeypatch, tmp_path: Path
) -> None:
    """#396: loggedIn:true + email:null must return a non-None sentinel.

    Claude CLI 2.x Max returns this exact shape. Before #396 the helper
    returned ``None``, which cascaded into ``pm worker-start`` refusing
    the healthy account with "No healthy logged-in account available".
    """
    monkeypatch.setattr(
        "pollypm.providers.claude.detect.subprocess.run",
        lambda *args, **kwargs: _Result(
            json.dumps(
                {
                    "loggedIn": True,
                    "authMethod": "claude.ai",
                    "apiProvider": "firstParty",
                    "email": None,
                    "orgId": None,
                    "orgName": None,
                    "subscriptionType": "max",
                }
            )
        ),
    )
    result = detect_claude_email(tmp_path)
    assert result is not None
    assert result == "claude.ai:max"


def test_detect_claude_email_falls_back_to_text_on_json_failure(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def _fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        if "--json" in cmd:
            return _Result("not-valid-json", returncode=0)
        if "--text" in cmd:
            return _Result("Logged in as user@example.com\n", returncode=0)
        raise AssertionError(f"unexpected cmd: {cmd}")

    monkeypatch.setattr(
        "pollypm.providers.claude.detect.subprocess.run", _fake_run
    )
    assert detect_claude_email(tmp_path) == "user@example.com"
    assert any("--json" in cmd for cmd in calls)
    assert any("--text" in cmd for cmd in calls)


def test_detect_logged_in_delegates_through_email(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "pollypm.providers.claude.detect.detect_claude_email",
        lambda home: "x@y.z",
    )
    assert detect_logged_in(tmp_path) is True

    monkeypatch.setattr(
        "pollypm.providers.claude.detect.detect_claude_email",
        lambda home: None,
    )
    assert detect_logged_in(tmp_path) is False


def test_detect_email_from_pane_always_returns_none() -> None:
    # Claude never prints the email in its pane output — the helper
    # exists only so the shared login loop has a uniform signature.
    assert detect_email_from_pane("Logged in as pearl@swh.me") is None
    assert detect_email_from_pane("") is None


def test_claude_prompt_ready_requires_interactive_markers() -> None:
    assert claude_prompt_ready("❯ \n welcome back, Pearl\n")
    assert claude_prompt_ready("❯ \nClaude Code v2.1.92\n")


def test_claude_prompt_ready_rejects_login_and_setup_screens() -> None:
    assert not claude_prompt_ready("Select login method:\n1. Option A\n")
    assert not claude_prompt_ready("please run /login")
    assert not claude_prompt_ready(
        "Choose the text style that looks best with your terminal"
    )
    assert not claude_prompt_ready("")
