"""``ClaudeProvider`` — the Phase B ``ProviderAdapter`` implementation.

This class is the entry-point-registered adapter for Claude; it
replaces the Phase A ``LegacyClaudeAdapter`` placeholder. The
Protocol methods compose the small helpers in this package
(:mod:`.detect`, :mod:`.login`, :mod:`.probe`, :mod:`.env`,
:mod:`.resume`, :mod:`.onboarding`) so each piece is independently
testable.

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
from pollypm.models import SessionConfig
from pollypm.provider_sdk import ProviderUsageSnapshot

from . import detect as _detect
from . import env as _env
from . import login as _login
from . import onboarding as _onboarding
from . import probe as _probe
from . import resume as _resume


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

    def collect_usage_snapshot(
        self,
        tmux,
        target: str,
        *,
        account: AccountConfig,
        session: SessionConfig,
    ) -> ProviderUsageSnapshot:
        """Drive Claude's live usage probe for ``account`` in ``target``."""
        return _probe.collect_usage_snapshot(
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

    def latest_session_id(
        self,
        account: AccountConfig,
        cwd: Path | None,
    ) -> str | None:
        """Newest Claude session UUID for ``cwd`` under ``account.home``.

        Claude Code buckets sessions per resolved cwd, so ``cwd`` is
        required. Returns ``None`` when ``account.home`` is unset, the
        cwd argument is missing, or no prior session exists for the
        encoded-cwd bucket.
        """
        if account.home is None or cwd is None:
            return None
        return _resume.latest_session_id(account.home, cwd)

    def resume_launch_cmd(
        self,
        account: AccountConfig,
        session_id: str,
        args: list[str],
    ) -> list[str]:
        """Return ``["claude", "--dangerously-skip-permissions", "--resume", id, *args]``.

        The same ``--dangerously-skip-permissions`` posture as a fresh
        architect launch, with ``--resume <session_id>`` threaded in
        before any caller-provided args.
        """
        del account  # reserved for per-account CLI overrides
        return _resume.resume_argv(session_id, args)

    def prime_home(self, home: Path) -> None:
        """Seed ``.claude.json`` + ``settings.json`` so launches run unattended.

        Delegates to
        :func:`pollypm.providers.claude.onboarding.prime_claude_home`.
        Idempotent.
        """
        _onboarding.prime_claude_home(home)

    def login_command(
        self,
        *,
        interactive: bool = False,
        headless: bool = False,
    ) -> str:
        """Return ``"claude"`` (interactive) or ``"claude auth login --claudeai"``.

        ``headless`` is accepted for Protocol-shape parity with Codex
        and intentionally ignored — Claude has no headless equivalent
        of the browser auth flow today.
        """
        del headless  # Protocol-shape parity; Claude has no headless flow
        return _login.login_command(interactive=interactive)

    def logout_command(self) -> str:
        """Return ``"claude auth logout || true"``."""
        return _login.logout_command()

    def login_completion_marker_seen(self, pane_text: str) -> bool:
        """Return True iff ``pane_text`` contains the PollyPM done-marker."""
        return _login.login_completion_marker_seen(pane_text)

    def detect_email_from_pane(self, pane_text: str) -> str | None:
        """Claude never surfaces the account email in pane output."""
        return _detect.detect_email_from_pane(pane_text)


__all__ = ["ClaudeProvider"]
