"""``ClaudeProvider`` — the Phase B ``ProviderAdapter`` implementation.

This class is the entry-point-registered adapter for Claude; it
replaces the Phase A ``LegacyClaudeAdapter`` placeholder. The six
Protocol methods compose the small helpers in this package
(:mod:`.detect`, :mod:`.login`, :mod:`.probe`, :mod:`.env`) so each
piece is independently testable.

Two methods still raise ``NotImplementedError`` in Phase B —
``run_login_flow`` and ``probe_usage`` — because their real
implementations need context (TmuxClient, state-DB path) the
:class:`pollypm.acct.protocol.ProviderAdapter` signature does not yet
model. Phase D widens the Protocol. Callers that need the full flow
today keep using :mod:`pollypm.accounts` / :mod:`pollypm.onboarding`.
The NotImplementedError messages follow the three-question rule and
point at the correct legacy entry points.
"""

from __future__ import annotations

from pathlib import Path

from pollypm.acct.model import AccountConfig, AccountStatus

from . import detect as _detect
from . import env as _env
from . import login as _login
from . import probe as _probe


class ClaudeProvider:
    """Claude implementation of :class:`pollypm.acct.ProviderAdapter`."""

    name = "claude"

    def detect_logged_in(self, account: AccountConfig) -> bool:
        """Return True iff ``account.home`` holds valid Claude credentials.

        Delegates to :func:`pollypm.providers.claude.detect.detect_logged_in`,
        with a ``home is None`` guard that returns False (matches the
        legacy ``_account_logged_in`` behavior).
        """
        if account.home is None:
            return False
        return _detect.detect_logged_in(account.home)

    def detect_email(self, account: AccountConfig) -> str | None:
        """Return the authenticated email (or Max sentinel) for ``account``.

        Preserves the #396 fix: ``loggedIn:true`` + ``email:null`` returns
        a stable non-None sentinel instead of ``None``. See
        :func:`pollypm.providers.claude.detect.detect_claude_email`.
        """
        if account.home is None:
            return None
        return _detect.detect_claude_email(account.home)

    def run_login_flow(self, account: AccountConfig) -> None:
        """Drive the interactive Claude login.

        Phase B stub — the real flow lives in
        :func:`pollypm.accounts.add_account_via_login`. See
        :func:`pollypm.providers.claude.login.run_login_flow` for the
        full error message.

        Raises:
            NotImplementedError: always; message points at the legacy
                entry points that already thread the required tmux
                context.
        """
        _login.run_login_flow(account)

    def probe_usage(self, account: AccountConfig) -> AccountStatus:
        """Return a fresh ``AccountStatus`` for ``account``.

        Phase B stub — the real probe lives in
        :func:`pollypm.accounts.probe_account_usage`. See
        :func:`pollypm.providers.claude.probe.probe_usage` for the
        full error message.

        Raises:
            NotImplementedError: always; message points at the legacy
                entry point that carries the state-DB path.
        """
        return _probe.probe_usage(account)

    def worker_launch_cmd(
        self,
        account: AccountConfig,
        args: list[str],
    ) -> list[str]:
        """Return ``["claude", *args]`` — argv for a worker session.

        Phase B intentionally keeps the shape compatible with the
        legacy adapter so call sites can migrate without behavior
        change. Phase D will extend this with resume/fresh markers when
        the Protocol grows a session argument.
        """
        del account  # reserved for Phase D (per-account CLI overrides)
        return ["claude", *args]

    def isolated_env(self, home: Path) -> dict[str, str]:
        """Return ``{"CLAUDE_CONFIG_DIR": str(home / ".claude")}``.

        Additive contribution only — callers layer this onto whatever
        env the runtime supplies. See
        :func:`pollypm.providers.claude.env.isolated_env`.
        """
        return _env.isolated_env(home)


__all__ = ["ClaudeProvider"]
