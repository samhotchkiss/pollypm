"""Phase D manager tests for ``pollypm.acct.manager`` (#397).

Run with::

    HOME=/tmp/pytest-acct-manager uv run --with pytest \\
        pytest tests/test_acct_manager.py -x

These tests exercise the dispatch logic in the centralized manager:
every public helper must route through the entry-point-backed registry,
so this suite swaps in a ``FakeProvider`` at the registry layer and
asserts the manager never touches the provider packages directly.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from pollypm.acct import (
    AccountConfig,
    AccountStatus,
    ProviderNotFound,
    get_provider,
)
from pollypm.acct import manager as acct_manager
from pollypm.acct.model import RuntimeStatus  # noqa: F401 — imported to validate re-export
from pollypm.models import ProviderKind, RuntimeKind


class FakeProvider:
    """Instrumented ``ProviderAdapter`` stand-in for dispatch tests.

    Records every call and returns caller-supplied canned responses so
    the manager-level assertions can check both "was the right method
    called" and "did the return value propagate".
    """

    def __init__(self, name: str = "claude") -> None:
        self.name = name
        self.calls: list[tuple[str, tuple, dict]] = []
        self.logged_in_map: dict[str, bool] = {}
        self.email_map: dict[str, str | None] = {}
        self.status: AccountStatus | None = None
        self.login_invocations = 0

    def _record(self, method: str, args: tuple, kwargs: dict) -> None:
        self.calls.append((method, args, kwargs))

    def detect_logged_in(self, account: AccountConfig) -> bool:
        self._record("detect_logged_in", (account,), {})
        return self.logged_in_map.get(account.name, False)

    def detect_email(self, account: AccountConfig) -> str | None:
        self._record("detect_email", (account,), {})
        return self.email_map.get(account.name)

    def run_login_flow(self, account: AccountConfig) -> None:
        self._record("run_login_flow", (account,), {})
        self.login_invocations += 1

    def probe_usage(self, account: AccountConfig) -> AccountStatus:
        self._record("probe_usage", (account,), {})
        assert self.status is not None, "test must set FakeProvider.status"
        return self.status

    def worker_launch_cmd(self, account: AccountConfig, args: list[str]) -> list[str]:
        self._record("worker_launch_cmd", (account, args), {})
        return [f"fake-{self.name}", *args]

    def isolated_env(self, home: Path) -> dict[str, str]:
        self._record("isolated_env", (home,), {})
        return {f"FAKE_{self.name.upper()}_HOME": str(home)}


@pytest.fixture
def fake_claude(monkeypatch) -> FakeProvider:
    """Register a FakeProvider under the ``claude`` name via the registry."""
    provider = FakeProvider(name="claude")
    get_provider.cache_clear()

    def _fake_get(name: str):
        if name == "claude":
            return provider
        raise ProviderNotFound(name, available=["claude"])

    monkeypatch.setattr("pollypm.acct.manager.get_provider", _fake_get)
    return provider


@pytest.fixture
def fake_pair(monkeypatch) -> tuple[FakeProvider, FakeProvider]:
    """Register separate FakeProviders for claude + codex."""
    claude = FakeProvider(name="claude")
    codex = FakeProvider(name="codex")
    mapping = {"claude": claude, "codex": codex}
    get_provider.cache_clear()

    def _fake_get(name: str):
        if name in mapping:
            return mapping[name]
        raise ProviderNotFound(name, available=sorted(mapping))

    monkeypatch.setattr("pollypm.acct.manager.get_provider", _fake_get)
    return claude, codex


def _account(
    name: str,
    provider: ProviderKind = ProviderKind.CLAUDE,
    home: Path | None = Path("/tmp/pollypm-fake"),
) -> AccountConfig:
    return AccountConfig(
        name=name,
        provider=provider,
        email=f"{name}@example.com",
        runtime=RuntimeKind.LOCAL,
        home=home,
    )


def test_detect_logged_in_dispatches_to_registered_provider(fake_claude: FakeProvider) -> None:
    account = _account("primary")
    fake_claude.logged_in_map[account.name] = True

    assert acct_manager.detect_logged_in(account) is True

    assert fake_claude.calls[0][0] == "detect_logged_in"
    assert fake_claude.calls[0][1][0] is account


def test_detect_logged_in_routes_by_account_provider(fake_pair) -> None:
    claude, codex = fake_pair
    claude_acct = _account("claude_one", ProviderKind.CLAUDE)
    codex_acct = _account("codex_one", ProviderKind.CODEX)
    claude.logged_in_map[claude_acct.name] = True
    codex.logged_in_map[codex_acct.name] = True

    acct_manager.detect_logged_in(claude_acct)
    acct_manager.detect_logged_in(codex_acct)

    # Each adapter only sees its own account — no cross-talk.
    assert len(claude.calls) == 1 and claude.calls[0][1][0] is claude_acct
    assert len(codex.calls) == 1 and codex.calls[0][1][0] is codex_acct


def test_detect_email_returns_adapter_result(fake_claude: FakeProvider) -> None:
    account = _account("primary")
    fake_claude.email_map[account.name] = "primary@example.com"

    assert acct_manager.detect_email(account) == "primary@example.com"


def test_detect_email_preserves_none_from_adapter(fake_claude: FakeProvider) -> None:
    account = _account("primary")
    # Adapter returns None (no email known); manager must not invent one.
    assert acct_manager.detect_email(account) is None


def test_probe_usage_returns_provider_account_status(fake_claude: FakeProvider) -> None:
    account = _account("primary")
    status = AccountStatus(
        key=account.name,
        provider=account.provider,
        email=account.email or account.name,
        home=account.home,
        logged_in=True,
        plan="max",
        health="healthy",
        usage_summary="max until 2030-01-01",
    )
    fake_claude.status = status

    result = acct_manager.probe_usage(account)

    assert result is status
    assert fake_claude.calls[-1][0] == "probe_usage"


def test_worker_launch_cmd_dispatches_with_args(fake_claude: FakeProvider) -> None:
    account = _account("primary")

    cmd = acct_manager.worker_launch_cmd(account, ["--foo", "--bar"])

    assert cmd == ["fake-claude", "--foo", "--bar"]
    recorded = fake_claude.calls[-1]
    assert recorded[0] == "worker_launch_cmd"
    assert recorded[1][1] == ["--foo", "--bar"]


def test_isolated_env_returns_adapter_contribution(
    fake_claude: FakeProvider, tmp_path: Path
) -> None:
    account = _account("primary", home=tmp_path)

    env = acct_manager.isolated_env(account)

    assert env == {"FAKE_CLAUDE_HOME": str(tmp_path)}


def test_isolated_env_short_circuits_when_home_is_none(fake_claude: FakeProvider) -> None:
    """No home = no env contribution, and the adapter should not be consulted."""
    account = _account("primary", home=None)

    env = acct_manager.isolated_env(account)

    assert env == {}
    assert not any(call[0] == "isolated_env" for call in fake_claude.calls)


def test_run_login_flow_delegates(fake_claude: FakeProvider) -> None:
    account = _account("primary")

    acct_manager.run_login_flow(account)

    assert fake_claude.login_invocations == 1
    assert fake_claude.calls[-1][0] == "run_login_flow"


def test_unknown_provider_raises_provider_not_found(monkeypatch) -> None:
    """The registry error surfaces through the manager unchanged."""
    get_provider.cache_clear()

    def _always_missing(name: str):
        raise ProviderNotFound(name, available=["claude", "codex"])

    monkeypatch.setattr("pollypm.acct.manager.get_provider", _always_missing)

    account = _account("primary", provider=ProviderKind.CLAUDE)
    with pytest.raises(ProviderNotFound) as exc_info:
        acct_manager.detect_logged_in(account)

    message = str(exc_info.value)
    assert "Why:" in message
    assert "Fix:" in message


def test_list_logged_in_preserves_iteration_order(fake_claude: FakeProvider) -> None:
    alpha = _account("alpha")
    bravo = _account("bravo")
    charlie = _account("charlie")
    fake_claude.logged_in_map = {"alpha": False, "bravo": True, "charlie": True}

    result = acct_manager.list_logged_in([alpha, bravo, charlie])

    assert result == [bravo, charlie]


def test_choose_healthy_prefers_preferred_when_healthy(fake_claude: FakeProvider) -> None:
    alpha = _account("alpha")
    bravo = _account("bravo")
    fake_claude.logged_in_map = {"alpha": True, "bravo": True}

    choice = acct_manager.choose_healthy_for_worker([alpha, bravo], preferred="bravo")

    assert choice is bravo


def test_choose_healthy_falls_back_when_preferred_unhealthy(
    fake_claude: FakeProvider,
) -> None:
    alpha = _account("alpha")
    bravo = _account("bravo")
    fake_claude.logged_in_map = {"alpha": True, "bravo": False}

    choice = acct_manager.choose_healthy_for_worker([alpha, bravo], preferred="bravo")

    assert choice is alpha


def test_choose_healthy_returns_none_when_all_signed_out(
    fake_claude: FakeProvider,
) -> None:
    alpha = _account("alpha")
    bravo = _account("bravo")
    fake_claude.logged_in_map = {"alpha": False, "bravo": False}

    choice = acct_manager.choose_healthy_for_worker([alpha, bravo])

    assert choice is None


def test_choose_healthy_handles_unknown_preferred_name(fake_claude: FakeProvider) -> None:
    alpha = _account("alpha")
    bravo = _account("bravo")
    fake_claude.logged_in_map = {"alpha": True, "bravo": True}

    # Preferred name isn't in the list — should fall back to first healthy.
    choice = acct_manager.choose_healthy_for_worker(
        [alpha, bravo], preferred="not-configured"
    )

    assert choice is alpha


def test_manager_does_not_import_provider_packages() -> None:
    """Import-boundary guard: the manager must stay provider-agnostic.

    Scans the manager's source for any actual ``import`` or
    ``from ... import`` that references ``pollypm.providers``. Docstring
    prose that names the module is allowed — the rule is on real imports.
    This is the hard rule called out in the module docstring and in
    #397's Phase D deliverable: manager imports must go through
    ``pollypm.acct.protocol`` / ``registry`` / ``model`` only.
    """
    import re

    source = inspect.getsource(acct_manager)
    # Strip module + function docstrings so prose doesn't trip the check.
    without_docstrings = re.sub(r'"""[\s\S]*?"""', "", source)
    import_lines = [
        line
        for line in without_docstrings.splitlines()
        if re.match(r"\s*(?:from\s+\S+\s+import|import\s+\S+)", line)
    ]
    for line in import_lines:
        assert "pollypm.providers" not in line, (
            f"pollypm.acct.manager imports from pollypm.providers.*: {line!r}; "
            "the manager must route through the ProviderAdapter Protocol "
            "registry only."
        )
