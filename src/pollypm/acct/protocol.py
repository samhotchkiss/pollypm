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

    def prime_home(self, home: Path) -> None:
        """Seed any provider state that must exist before first launch.

        Issue #406 added this to lift the last Claude-specific dispatch
        out of :mod:`pollypm.onboarding`. Claude pre-populates
        ``.claude.json`` and ``settings.json`` so the welcome wizard
        does not block unattended launches; providers that need no
        seeding (Codex) implement this as a no-op.

        Idempotent: callers may invoke ``prime_home`` repeatedly during
        onboarding, control-home sync, and supervisor bootstrap.
        """
        ...

    def login_command(
        self,
        *,
        interactive: bool = False,
        headless: bool = False,
    ) -> str:
        """Return the shell snippet that launches the provider login.

        ``interactive`` and ``headless`` are the two flags the shared
        login loop in :mod:`pollypm.onboarding` understands today.
        Providers ignore flags they do not care about — Claude reads
        ``interactive``; Codex reads ``headless``. Adding a new flag
        here is the only path that lets a third-party provider expose
        a new launch mode without editing onboarding.
        """
        ...

    def logout_command(self) -> str:
        """Return the shell snippet that clears provider credentials.

        Used by the ``force_fresh_auth=True`` path of the shared login
        loop (e.g. ``pm accounts relogin``) so the next launch starts
        from a clean credential store. Implementations should suffix
        their command with ``|| true`` so a "not currently logged in"
        exit code does not abort the shell pipeline.
        """
        ...

    def login_completion_marker_seen(self, pane_text: str) -> bool:
        """Return True iff ``pane_text`` shows the provider's done-marker.

        The shared login loop writes ``PollyPM: login window complete.``
        at the tail of the login shell so providers without a strong
        native signal can use the same marker. Providers with a richer
        signal (e.g. a CLI banner) are free to widen this check.
        """
        ...


__all__ = ["ProviderAdapter"]
