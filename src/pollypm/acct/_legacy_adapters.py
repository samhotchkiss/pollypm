"""Phase A placeholder adapter(s) that delegate to the existing modules.

These classes exist so the ``pollypm.acct`` registry has something to
resolve while Phase B (Claude) extracts the real provider package. The
Phase C (Codex) adapter has already been replaced by
:class:`pollypm.providers.codex.CodexProvider`; once Phase B lands,
this whole module can be deleted.

Each method delegates to the current implementation in
``pollypm.accounts`` / ``pollypm.onboarding`` / ``pollypm.providers.*``.
No behavior change: every call path ends in the same function the code
base has always used.

The adapter is deliberately thin — see the docstring on each method
for the exact delegation target. Anything that cannot be answered from
``AccountConfig`` alone (e.g. ``probe_usage`` needs a config_path to
locate the state DB) raises ``NotImplementedError`` with a message that
points at the higher-level API that already handles it.
"""

from __future__ import annotations

from pathlib import Path

from pollypm.models import AccountConfig as _ModelAccountConfig
from pollypm.models import ProviderKind

from .model import AccountConfig, AccountStatus


class _LegacyAdapterBase:
    """Shared scaffolding for the two Phase A legacy adapters.

    Subclasses set ``name`` + ``_provider_kind`` and override the two
    env helpers. Everything else is generic enough to share.
    """

    name: str = ""
    _provider_kind: ProviderKind

    def detect_logged_in(self, account: AccountConfig) -> bool:
        """Delegate to ``pollypm.accounts._account_logged_in``.

        The legacy helper already honours the ``home is None`` guard
        and returns False in that case, so we forward verbatim.
        """
        from pollypm.accounts import _account_logged_in

        return _account_logged_in(account)

    def detect_email(self, account: AccountConfig) -> str | None:
        """Delegate to ``pollypm.onboarding._detect_account_email``."""
        from pollypm.onboarding import _detect_account_email

        if account.home is None:
            return None
        return _detect_account_email(account.provider, account.home)

    def run_login_flow(self, account: AccountConfig) -> None:
        """Not wired in Phase A — use the existing ``pm accounts`` CLI.

        The login flow threads a ``TmuxClient`` and a window label that
        the substrate does not yet model. Phase B/C move the flow into
        the provider packages; until then, callers must keep using
        ``pollypm.accounts.add_account_via_login`` and
        ``pollypm.accounts.relogin_account``, which is what every CLI
        path already does.
        """
        raise NotImplementedError(
            f"Phase A of #397 does not wire the login flow through the "
            f"provider substrate.\n\n"
            f"Why: login requires a TmuxClient and window label that the "
            f"substrate does not yet model; those arguments will be added "
            f"in Phase B ({self._provider_kind.value}).\n\n"
            f"Fix: use `pollypm.accounts.add_account_via_login` or "
            f"`pollypm.accounts.relogin_account` for now."
        )

    def probe_usage(self, account: AccountConfig) -> AccountStatus:
        """Not wired in Phase A — use ``pollypm.accounts.probe_account_usage``.

        The legacy probe reads the config path to locate the state DB
        and pass isolation context. Phase A's Protocol intentionally
        takes only an ``AccountConfig``; Phase B/C will add the context
        object the real implementation needs.
        """
        raise NotImplementedError(
            f"Phase A of #397 does not wire probe_usage through the "
            f"provider substrate.\n\n"
            f"Why: the legacy probe needs the project config path to "
            f"locate the state DB, which the Phase A Protocol does not "
            f"yet carry.\n\n"
            f"Fix: call `pollypm.accounts.probe_account_usage(config_path, "
            f"account_name)` for now; Phase B/C will route this through "
            f"the substrate."
        )

    def worker_launch_cmd(
        self,
        account: AccountConfig,
        args: list[str],
    ) -> list[str]:
        """Return ``[binary, *args]`` — the same shape the adapters use.

        Delegates binary resolution to the existing
        ``pollypm.providers`` adapters so the two surfaces stay in
        sync. Phase B/C replace this with a proper argv builder that
        knows about resume markers, fresh-launch markers, etc.
        """
        from pollypm.providers import get_provider as _legacy_get_provider

        provider = _legacy_get_provider(self._provider_kind)
        return [provider.binary, *args]

    def isolated_env(self, home: Path) -> dict[str, str]:
        """Delegate to ``pollypm.runtime_env.provider_profile_env_for_provider``."""
        from pollypm.runtime_env import provider_profile_env_for_provider

        # ``base_env={}`` so callers see only the additive contribution
        # of the provider — matches the Protocol's contract.
        return provider_profile_env_for_provider(
            self._provider_kind,
            home,
            base_env={},
        )


class LegacyClaudeAdapter(_LegacyAdapterBase):
    """Phase A placeholder for the Claude provider."""

    name = "claude"
    _provider_kind = ProviderKind.CLAUDE


# Re-export the legacy AccountConfig under the private type alias so
# static checkers can confirm the Protocol methods accept the same
# dataclass the rest of the codebase uses. The two symbols resolve to
# the same class — this is just a reminder for code-search.
_LegacyAccountConfig = _ModelAccountConfig


__all__ = ["LegacyClaudeAdapter"]
