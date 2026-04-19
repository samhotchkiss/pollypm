"""The ``ProviderAdapter`` Protocol — Phase A of #397.

Every provider package (``pollypm.acct.claude``, ``pollypm.acct.codex``,
and any third-party plugins) implements this contract. Phase A ships
the Protocol plus two legacy adapters (see ``_legacy_adapters``) that
delegate to the existing ``pollypm.accounts`` + ``pollypm.onboarding``
functions so no caller has to move yet.

The Protocol is intentionally narrow: six methods that cover the four
life-cycle events (detect, login, probe, launch) plus the env shim that
makes isolated-home execution possible. Anything broader lives in the
higher-level manager that Phase D will introduce.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from .model import AccountConfig, AccountStatus


@runtime_checkable
class ProviderAdapter(Protocol):
    """Contract every provider package implements.

    Attributes:
        name: Stable identifier used by the entry-point registry
            (``"claude"``, ``"codex"``, …). Must match the entry-point
            name so ``get_provider(adapter.name)`` round-trips.

    The six methods below mirror the four life-cycle events a provider
    participates in — detect, login, probe, launch — plus the env shim
    that makes isolated-home execution possible.
    """

    name: str

    def detect_logged_in(self, account: AccountConfig) -> bool:
        """Return True iff the account currently holds valid credentials.

        Implementations must be side-effect-free: no network calls that
        mutate server state, no writes to ``account.home``.
        """
        ...

    def detect_email(self, account: AccountConfig) -> str | None:
        """Return the authenticated email address, or None if unknown.

        The return value is used as a presence check, not an identity
        source. For providers whose auth API does not expose the email
        (Claude Max 2.x), a stable non-None sentinel is acceptable.
        """
        ...

    def run_login_flow(self, account: AccountConfig) -> None:
        """Drive the interactive login for the account.

        Blocks until the user completes login or aborts. Implementations
        should be idempotent: running login twice on an already-logged-in
        account must not corrupt the existing credentials.
        """
        ...

    def probe_usage(self, account: AccountConfig) -> AccountStatus:
        """Return a fresh ``AccountStatus`` snapshot for the account.

        This is the "pull me the latest" entry point; callers that want
        cached data should read from the state store directly.
        """
        ...

    def worker_launch_cmd(
        self,
        account: AccountConfig,
        args: list[str],
    ) -> list[str]:
        """Return the argv to launch a worker session for this account.

        The returned list is the command the runtime wrapper (local or
        Docker) will execute. It does **not** include env — env is
        delivered separately via ``isolated_env()`` so the runtime
        layer can merge it with its own env contributions.
        """
        ...

    def isolated_env(self, home: Path) -> dict[str, str]:
        """Return env vars that pin the provider binary to ``home``.

        For Claude this is ``CLAUDE_CONFIG_DIR``; for Codex it is
        ``CODEX_HOME``. The returned dict is layered onto whatever env
        the runtime provides — callers should treat it as purely
        additive.
        """
        ...


__all__ = ["ProviderAdapter"]
