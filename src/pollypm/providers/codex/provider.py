"""The :class:`CodexProvider` adapter — Phase C of #397.

``CodexProvider`` implements the ``pollypm.acct.protocol.ProviderAdapter``
Protocol by delegating to the helpers in the sibling modules
(``detect``, ``login``, ``probe``, ``env``, ``resume``). Keeping the
class body thin means each life-cycle event has an obvious entry
point for Phase D when the probe/login signatures grow to include
the manager context.

This replaces ``LegacyCodexAdapter`` from
``pollypm.acct._legacy_adapters``: the entry-point registration in
``pyproject.toml`` now points here, and the legacy adapter has been
removed.
"""

from __future__ import annotations

from pathlib import Path

from pollypm.acct.model import AccountConfig, AccountStatus
from pollypm.models import SessionConfig
from pollypm.provider_sdk import ProviderUsageSnapshot

from . import login as _login
from .adapter import CodexAdapter as _RuntimeCodexAdapter
from .detect import detect_codex_email, detect_email_from_pane, detect_logged_in
from .env import isolated_env as _codex_isolated_env
from .login import run_login_flow as _codex_run_login_flow
from .probe import probe_usage as _codex_probe_usage
from .resume import latest_session_id as _codex_latest_session_id
from .resume import resume_argv as _codex_resume_argv


class CodexProvider:
    """Codex adapter that satisfies ``ProviderAdapter``.

    All six Protocol methods delegate to the sibling helpers in this
    package. The class body is deliberately thin — behavior lives in
    the helpers so tests can exercise each surface in isolation without
    constructing a full adapter.
    """

    name = "codex"

    def detect_logged_in(self, account: AccountConfig) -> bool:
        """Return True iff the account's ``.codex/auth.json`` is valid."""
        return detect_logged_in(account.home)

    def detect_email(self, account: AccountConfig) -> str | None:
        """Return the authenticated email or ``None``.

        Null-safe on ``account.home`` so callers don't need to guard it.
        """
        if account.home is None:
            return None
        return detect_codex_email(account.home)

    def run_login_flow(self, account: AccountConfig) -> None:
        """Drive ``codex login`` for ``account``.

        Raises ``ValueError`` when the account has no isolated home —
        Codex refuses to share credentials between accounts on the same
        host, so the caller must configure a home before login.
        """
        if account.home is None:
            raise ValueError(
                f"Cannot run the Codex login flow for account "
                f"{account.name!r} without an isolated home.\n\n"
                f"Why: Codex stores OAuth tokens under CODEX_HOME; "
                f"without a home the login would write into the "
                f"user's ambient ~/.codex and clobber other accounts.\n\n"
                f"Fix: set `home` on the AccountConfig (PollyPM's "
                f"onboarding picks a default under the project base "
                f"dir) before calling run_login_flow."
            )
        _codex_run_login_flow(account.home, window_label=f"login-{account.name}")

    def probe_usage(self, account: AccountConfig) -> AccountStatus:
        """Return a fresh ``AccountStatus`` for ``account``.

        Currently delegates to :func:`pollypm.providers.codex.probe.probe_usage`,
        which raises ``NotImplementedError`` until Phase D threads the
        manager context through.
        """
        return _codex_probe_usage(account)

    def collect_usage_snapshot(
        self,
        tmux,
        target: str,
        *,
        account: AccountConfig,
        session: SessionConfig,
    ) -> ProviderUsageSnapshot:
        """Drive Codex's live usage probe for ``account`` in ``target``."""
        return _RuntimeCodexAdapter().collect_usage_snapshot(
            tmux,
            target,
            account=account,
            session=session,
        )

    def worker_launch_cmd(
        self,
        account: AccountConfig,
        args: list[str],
    ) -> list[str]:
        """Return the argv to launch a Codex worker for ``account``.

        Phase C keeps the shape identical to the legacy adapter:
        ``[binary, *args]``. The old ``pollypm.providers.codex.CodexAdapter``
        builds a richer ``LaunchCommand`` (resume markers, env dict) for
        the tmux launch path; that adapter is unaffected by this
        refactor and remains the source of truth for worker launches.
        The helper here exists so the Protocol-level callers have a
        simple argv builder.
        """
        # Import locally to avoid importing the tmux-heavy module when
        # callers only want the Protocol surface.
        from .adapter import CodexAdapter as _LegacyBinaryProvider

        return [_LegacyBinaryProvider.binary, *args]

    def isolated_env(self, home: Path) -> dict[str, str]:
        """Return ``{CODEX_HOME: <home>/.codex}``.

        Matches the Protocol contract: the returned dict is purely
        additive — callers layer it onto whatever env the runtime
        already provides.
        """
        return _codex_isolated_env(home, base_env={})

    def latest_session_id(
        self,
        account: AccountConfig,
        cwd: Path | None,
    ) -> str | None:
        """Newest Codex session UUID under ``account.home``.

        Codex doesn't bucket sessions by working directory the way
        Claude Code does, so ``cwd`` is unused here. Returns ``None``
        when ``account.home`` is unset or no rollouts exist.
        """
        del cwd  # Codex sessions are not cwd-bucketed
        if account.home is None:
            return None
        return _codex_latest_session_id(account.home)

    def resume_launch_cmd(
        self,
        account: AccountConfig,
        session_id: str,
        args: list[str],
    ) -> list[str]:
        """Return ``["codex", "--dangerously-skip-permissions", "resume", id, *args]``.

        Codex's ``resume`` is a subcommand (not a flag), so the
        positional ordering matters: top-level flags first, then
        ``resume <id>``, then any caller-supplied args.
        """
        del account  # reserved for per-account CLI overrides
        return _codex_resume_argv(session_id, args)

    def prime_home(self, home: Path) -> None:
        """No-op for Codex.

        Codex writes its own state on first launch — there is no
        equivalent of the Claude welcome wizard to skip. Implemented
        so the cross-provider dispatch in :mod:`pollypm.onboarding`
        does not need to special-case the provider kind.
        """
        del home  # nothing to seed for Codex

    def login_command(
        self,
        *,
        interactive: bool = False,
        headless: bool = False,
    ) -> str:
        """Return ``"codex login"`` (or ``"codex login --device-auth"``).

        ``interactive`` is accepted for Protocol-shape parity with
        Claude and ignored — Codex has no interactive REPL login
        equivalent.
        """
        del interactive  # Protocol-shape parity; Codex has no REPL login
        return _login.login_command(headless=headless)

    def logout_command(self) -> str:
        """Return ``"codex logout || true"``."""
        return _login.logout_command()

    def login_completion_marker_seen(self, pane_text: str) -> bool:
        """Return True iff ``pane_text`` contains the PollyPM done-marker."""
        return _login.login_completion_marker_seen(pane_text)

    def detect_email_from_pane(self, pane_text: str) -> str | None:
        """Return the account email if the Codex login pane prints it."""
        return detect_email_from_pane(pane_text)


__all__ = ["CodexProvider"]
