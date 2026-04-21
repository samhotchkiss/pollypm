"""Tests for :class:`pollypm.providers.claude.ClaudeProvider` (Phase B of #397).

Run with::

    HOME=/tmp/pytest-providers-claude uv run --with pytest \\
        pytest tests/providers/test_claude_provider.py -x

The Phase B Claude adapter must:

* satisfy the :class:`pollypm.acct.ProviderAdapter` runtime check;
* resolve through ``get_provider("claude")`` (entry-point registry);
* delegate the four detection / env methods to the small helpers in
  :mod:`pollypm.providers.claude`;
* raise ``NotImplementedError`` with three-question-rule messages for
  ``run_login_flow`` and ``probe_usage`` (widened in Phase D).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pollypm.acct import ProviderAdapter, get_provider
from pollypm.models import AccountConfig, ProviderKind, RuntimeKind
from pollypm.providers.claude import ClaudeProvider


@pytest.fixture(autouse=True)
def _reset_registry_cache():
    get_provider.cache_clear()
    yield
    get_provider.cache_clear()


def _make_account(home: Path | None) -> AccountConfig:
    return AccountConfig(
        name="claude_primary",
        provider=ProviderKind.CLAUDE,
        email="pearl@example.com",
        runtime=RuntimeKind.LOCAL,
        home=home,
    )


def test_claude_provider_satisfies_protocol() -> None:
    provider = ClaudeProvider()
    assert isinstance(provider, ProviderAdapter)
    assert provider.name == "claude"


def test_entry_point_registry_resolves_claude_to_phase_b_class() -> None:
    provider = get_provider("claude")
    assert isinstance(provider, ClaudeProvider)
    assert provider.name == "claude"


def test_detect_logged_in_guards_missing_home() -> None:
    provider = ClaudeProvider()
    account = _make_account(home=None)
    assert provider.detect_logged_in(account) is False


def test_detect_logged_in_delegates_to_detect_helper(monkeypatch, tmp_path: Path) -> None:
    calls: list[Path] = []

    def _fake(home: Path) -> bool:
        calls.append(home)
        return True

    monkeypatch.setattr(
        "pollypm.providers.claude.provider._detect.detect_logged_in", _fake
    )
    provider = ClaudeProvider()
    account = _make_account(home=tmp_path)
    assert provider.detect_logged_in(account) is True
    assert calls == [tmp_path]


def test_detect_email_returns_none_when_home_is_none() -> None:
    provider = ClaudeProvider()
    assert provider.detect_email(_make_account(home=None)) is None


def test_detect_email_delegates_to_detect_claude_email(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "pollypm.providers.claude.provider._detect.detect_claude_email",
        lambda home: "user@example.com",
    )
    provider = ClaudeProvider()
    assert provider.detect_email(_make_account(home=tmp_path)) == "user@example.com"


def test_detect_email_preserves_issue_396_sentinel(monkeypatch, tmp_path: Path) -> None:
    """Even for the Max null-email case, detect_email must be non-None."""
    monkeypatch.setattr(
        "pollypm.providers.claude.provider._detect.detect_claude_email",
        lambda home: "claude.ai:max",
    )
    provider = ClaudeProvider()
    result = provider.detect_email(_make_account(home=tmp_path))
    assert result is not None
    assert result == "claude.ai:max"


def test_detect_email_from_pane_delegates_to_detect_helper() -> None:
    provider = ClaudeProvider()
    assert provider.detect_email_from_pane("Account: no-email-here") is None


def test_isolated_env_returns_claude_config_dir(tmp_path: Path) -> None:
    provider = ClaudeProvider()
    env = provider.isolated_env(tmp_path)
    assert env == {"CLAUDE_CONFIG_DIR": str(tmp_path / ".claude")}


def test_worker_launch_cmd_returns_argv_prefixed_with_claude(tmp_path: Path) -> None:
    provider = ClaudeProvider()
    account = _make_account(home=tmp_path)
    argv = provider.worker_launch_cmd(account, ["--dangerously-skip-permissions"])
    assert argv == ["claude", "--dangerously-skip-permissions"]


def test_run_login_flow_raises_not_implemented_with_three_question_message() -> None:
    provider = ClaudeProvider()
    with pytest.raises(NotImplementedError) as exc_info:
        provider.run_login_flow(_make_account(home=None))

    message = str(exc_info.value)
    # what happened
    assert "Phase B" in message or "login" in message.lower()
    # why it matters
    assert "Why:" in message
    # how to fix it
    assert "Fix:" in message
    assert "add_account_via_login" in message


def test_probe_usage_raises_not_implemented_with_three_question_message() -> None:
    provider = ClaudeProvider()
    with pytest.raises(NotImplementedError) as exc_info:
        provider.probe_usage(_make_account(home=None))

    message = str(exc_info.value)
    assert "Why:" in message
    assert "Fix:" in message
    assert "probe_account_usage" in message


def test_all_six_protocol_methods_are_present() -> None:
    provider = ClaudeProvider()
    for method in (
        "detect_logged_in",
        "detect_email",
        "run_login_flow",
        "probe_usage",
        "worker_launch_cmd",
        "isolated_env",
        "detect_email_from_pane",
    ):
        assert callable(getattr(provider, method)), f"missing {method}"
