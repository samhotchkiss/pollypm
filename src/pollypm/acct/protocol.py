"""The ``ProviderAdapter`` Protocol — Phase A of #397.

Every provider package (``pollypm.acct.claude``, ``pollypm.acct.codex``,
and any third-party plugins) implements this contract. Phase A ships
the Protocol plus two legacy adapters (see ``_legacy_adapters``) that
delegate to the existing ``pollypm.accounts`` + ``pollypm.onboarding``
functions so no caller has to move yet.

The Protocol covers the full life-cycle every provider participates
in — detect, login, probe, launch, warm-resume, and onboarding
priming — plus the env shim that makes isolated-home execution
possible. Two later waves extended the original six-method surface:
#406 added ``prime_home`` / ``login_command`` / ``logout_command`` /
``login_completion_marker_seen`` / ``detect_email_from_pane`` so
provider-specific onboarding details can live in provider packages;
the architect-lifecycle work added ``latest_session_id`` /
``resume_launch_cmd`` so idle-closed architects can warm-resume from
their provider's session UUID. Third-party providers implement the
full surface to participate in PollyPM's onboarding + recovery +
lifecycle paths without editing core.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pollypm.provider_sdk import ProviderUsageSnapshot

from .model import AccountConfig, AccountStatus

if TYPE_CHECKING:
    from pollypm.models import SessionConfig
    from pollypm.tmux.client import TmuxClient


@runtime_checkable
class ProviderAdapter(Protocol):
    """Contract every provider package implements.

    Attributes:
        name: Stable identifier used by the entry-point registry
            (``"claude"``, ``"codex"``, …). Must match the entry-point
            name so ``get_provider(adapter.name)`` round-trips.

    The methods below span the four life-cycle events a provider
    participates in — detect, login, probe, launch — plus the env
    shim, the warm-resume pair for architect idle-close (#403-ish
    architect_lifecycle), and the onboarding pair added by #406.
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

    def collect_usage_snapshot(
        self,
        tmux: "TmuxClient",
        target: str,
        *,
        account: AccountConfig,
        session: "SessionConfig",
    ) -> ProviderUsageSnapshot:
        """Return a fresh provider-native usage snapshot for ``account``.

        This is the low-level live probe surface used by the background
        account-usage sampler. The caller owns the short-lived probe pane;
        the provider owns the prompt-driving and text parsing.
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

    def latest_session_id(
        self,
        account: AccountConfig,
        cwd: Path | None,
    ) -> str | None:
        """Return the newest provider session UUID for ``cwd`` under this account.

        Used by the architect-idle-close flow: the supervisor calls this
        right before tearing down a quiet architect window so the UUID
        can be persisted in ``architect_resume_tokens`` for later
        :meth:`resume_launch_cmd` use.

        ``cwd`` may be ``None`` for providers (like Codex) that don't
        bucket sessions by working directory; those implementations
        should fall back to "newest session under the account home".

        Returns ``None`` when no prior session exists — callers should
        skip token persistence rather than store a placeholder.
        """
        ...

    def resume_launch_cmd(
        self,
        account: AccountConfig,
        session_id: str,
        args: list[str],
    ) -> list[str]:
        """Return the argv to relaunch a previously-closed session.

        Mirrors :meth:`worker_launch_cmd` but threads ``session_id``
        into the provider-specific resume incantation (Claude:
        ``--resume <id>``; Codex: ``resume <id>`` subcommand). The
        ``--dangerously-skip-permissions`` posture matches a fresh
        architect launch.
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

    def detect_email_from_pane(self, pane_text: str) -> str | None:
        """Return the authenticated email scraped from ``pane_text``.

        Used by the shared onboarding wait-loop to notice a completed
        login before the provider's on-disk auth files are flushed.
        Providers that never print the email in their pane output
        return ``None``.
        """
        ...


__all__ = ["ProviderAdapter"]
