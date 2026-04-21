"""Phase A substrate tests for ``pollypm.acct`` (#397).

Run with::

    HOME=/tmp/pytest-acct-substrate uv run --with pytest \\
        pytest tests/test_acct_substrate.py -x

These tests cover the Phase A deliverables only — Protocol shape,
entry-point registry, and the two placeholder adapters. Real provider
behavior is exercised by the existing ``test_accounts.py`` /
``test_onboarding.py`` suites, which must still pass unchanged.
"""

from __future__ import annotations

import pytest

from pollypm.acct import (
    AccountConfig,
    AccountStatus,
    ProviderAdapter,
    ProviderNotFound,
    RuntimeStatus,
    get_provider,
    list_providers,
)
from pollypm.providers.claude import ClaudeProvider
from pollypm.providers.codex import CodexProvider


@pytest.fixture(autouse=True)
def _reset_registry_cache():
    """Clear the ``lru_cache`` so tests that monkeypatch see a clean slate."""
    get_provider.cache_clear()
    yield
    get_provider.cache_clear()


def test_get_provider_claude_returns_protocol_conforming_instance():
    """``get_provider("claude")`` returns a ``ProviderAdapter``."""
    provider = get_provider("claude")
    assert isinstance(provider, ProviderAdapter)
    assert provider.name == "claude"


def test_get_provider_codex_returns_protocol_conforming_instance():
    """``get_provider("codex")`` returns a ``ProviderAdapter``."""
    provider = get_provider("codex")
    assert isinstance(provider, ProviderAdapter)
    assert provider.name == "codex"


def test_get_provider_unknown_raises_provider_not_found():
    """Missing providers raise ``ProviderNotFound`` with a 3-question message."""
    with pytest.raises(ProviderNotFound) as exc_info:
        get_provider("nonexistent-provider-xyz")

    message = str(exc_info.value)
    # What happened
    assert "'nonexistent-provider-xyz'" in message
    # Why it matters
    assert "entry-point" in message.lower() or "registered" in message.lower()
    # Fix
    assert "Fix:" in message
    # Lists available providers so the user can pick one
    assert "claude" in message
    assert "codex" in message
    # The exception carries the structured data for programmatic callers
    assert exc_info.value.name == "nonexistent-provider-xyz"
    assert "claude" in exc_info.value.available
    assert "codex" in exc_info.value.available


def test_provider_not_found_reports_empty_registry_explicitly():
    """When no providers are registered, the message says so plainly."""
    err = ProviderNotFound("anything", available=[])
    assert "(none registered)" in str(err)


def test_list_providers_returns_sorted_names():
    """``list_providers()`` returns the sorted built-in providers."""
    providers = list_providers()
    assert providers == sorted(providers)
    assert "claude" in providers
    assert "codex" in providers
    # The two Phase A adapters are the only built-ins.
    assert set(providers) >= {"claude", "codex"}


def test_external_provider_entry_points_emit_trust_warning_once(monkeypatch, capsys):
    monkeypatch.setattr(
        "pollypm.plugin_trust._THIRD_PARTY_EXTENSION_WARNING_EMITTED", False,
    )

    class FakeEntryPoint:
        name = "external-provider"
        value = "external_pkg.provider:ExternalProvider"

    monkeypatch.setattr(
        "pollypm.acct.registry.importlib.metadata.entry_points",
        lambda *, group: [FakeEntryPoint()],
    )

    assert list_providers() == ["external-provider"]
    assert "full user privileges" in capsys.readouterr().err

    assert list_providers() == ["external-provider"]
    assert capsys.readouterr().err == ""


def test_builtin_provider_entry_points_do_not_emit_trust_warning(
    monkeypatch, capsys,
):
    monkeypatch.setattr(
        "pollypm.plugin_trust._THIRD_PARTY_EXTENSION_WARNING_EMITTED", False,
    )

    class FakeEntryPoint:
        name = "claude"
        value = "pollypm.providers.claude:ClaudeProvider"

    monkeypatch.setattr(
        "pollypm.acct.registry.importlib.metadata.entry_points",
        lambda *, group: [FakeEntryPoint()],
    )

    assert list_providers() == ["claude"]
    assert capsys.readouterr().err == ""


def test_get_provider_caches_instances():
    """Repeated ``get_provider()`` calls return the same instance."""
    first = get_provider("claude")
    second = get_provider("claude")
    assert first is second


def test_phase_b_claude_provider_satisfies_protocol():
    """The Phase B Claude adapter runtime-checks as ``ProviderAdapter``."""
    adapter = ClaudeProvider()
    assert isinstance(adapter, ProviderAdapter)
    assert adapter.name == "claude"


def test_codex_provider_satisfies_protocol():
    """The Phase C Codex provider runtime-checks as ``ProviderAdapter``."""
    adapter = CodexProvider()
    assert isinstance(adapter, ProviderAdapter)
    assert adapter.name == "codex"


def test_registered_adapters_have_required_method_names():
    """Registered adapters expose the full ProviderAdapter contract."""
    for adapter_cls in (ClaudeProvider, CodexProvider):
        adapter = adapter_cls()
        for method in (
            "detect_logged_in",
            "detect_email",
            "run_login_flow",
            "probe_usage",
            "collect_usage_snapshot",
            "worker_launch_cmd",
            "isolated_env",
            "latest_session_id",
            "resume_launch_cmd",
            "prime_home",
            "login_command",
            "logout_command",
            "login_completion_marker_seen",
        ):
            assert hasattr(adapter, method), f"{adapter_cls.__name__} missing {method}"
            assert callable(getattr(adapter, method))


def test_model_reexports_are_the_canonical_types():
    """``acct.model`` re-exports the same ``AccountConfig`` the rest of the codebase uses."""
    from pollypm.accounts import AccountStatus as _LegacyStatus
    from pollypm.models import AccountConfig as _LegacyConfig

    # Object identity — these must be re-exports, not copies, so
    # callers can pass either name interchangeably during the
    # migration window.
    assert AccountConfig is _LegacyConfig
    assert AccountStatus is _LegacyStatus


def test_runtime_status_defaults_are_safe():
    """``RuntimeStatus`` default values represent "no data yet"."""
    status = RuntimeStatus()
    assert status.status == "unknown"
    assert status.reason == ""
    assert status.available_at is None
    assert status.access_expires_at is None


def test_unknown_provider_message_includes_fix_line():
    """Three-question rule: the error message ends with a ``Fix:`` line."""
    with pytest.raises(ProviderNotFound) as exc_info:
        get_provider("no-such-thing")
    lines = str(exc_info.value).splitlines()
    assert any(line.startswith("Fix:") for line in lines)
