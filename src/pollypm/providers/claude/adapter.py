"""Claude runtime-launch adapter.

Moved from ``src/pollypm/providers/claude.py`` by Phase B of #397 so
the Claude code can live together as one package. The class itself is
unchanged aside from pointing the ``_parse_usage_text`` helper at the
shared parser in :mod:`pollypm.providers.claude.usage_parse`.

This adapter is the one the plugin host loads for building launch
commands, resuming sessions, and driving the ``/usage`` tmux probe. It
implements the :class:`pollypm.providers.base.ProviderAdapter` Protocol
(distinct from the Phase A :class:`pollypm.acct.ProviderAdapter`
Protocol — see :mod:`pollypm.providers.claude.provider` for that one).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from pollypm.models import AccountConfig, SessionConfig
from pollypm.providers.base import LaunchCommand
from pollypm.provider_sdk import (
    ProviderAdapterBase,
    ProviderUsageSnapshot,
    TranscriptSource,
)
from pollypm.runtime_env import claude_config_dir

from .probe import collect_usage_snapshot as _collect_usage_snapshot
from .resume import recorded_session_id as _recorded_session_id
from .resume import resume_argv as _resume_argv
from .usage_parse import parse_claude_usage_snapshot

if TYPE_CHECKING:
    from pollypm.tmux.client import TmuxClient


# "Resumable" here is equivalent to the canonical control-role set —
# these are the long-lived sessions worth re-attaching with
# ``claude --continue`` / ``--resume`` on kickoff. Share the
# definition with :data:`pollypm.models.CONTROL_ROLES`.
from pollypm.models import CONTROL_ROLES as _RESUMABLE_CONTROL_ROLES


class ClaudeAdapter(ProviderAdapterBase):
    """Runtime-launch adapter for the Claude CLI."""

    name = "claude"
    binary = "claude"

    def is_available(self) -> bool:
        return shutil.which(self.binary) is not None

    def build_launch_command(
        self,
        session: SessionConfig,
        account: AccountConfig,
    ) -> LaunchCommand:
        argv = [self.binary]
        argv.extend(session.args)
        resume_argv: list[str] | None = None
        resume_marker: Path | None = None
        fresh_launch_marker: Path | None = None
        if account.home is not None:
            fresh_launch_marker = (
                account.home / ".pollypm" / "session-markers" / f"{session.name}.fresh"
            )
        if session.role in _RESUMABLE_CONTROL_ROLES and account.home is not None:
            resume_marker = (
                account.home / ".pollypm" / "session-markers" / f"{session.name}.resume"
            )
            session_id = _recorded_session_id(resume_marker)
            if session_id:
                resume_argv = _resume_argv(session_id, list(session.args))
            else:
                resume_argv = [self.binary, "--continue", *session.args]
        return LaunchCommand(
            argv=argv,
            env=dict(account.env),
            cwd=session.cwd,
            resume_argv=resume_argv,
            resume_marker=resume_marker,
            initial_input=session.prompt,
            fresh_launch_marker=fresh_launch_marker,
        )

    def build_resume_command(
        self,
        session: SessionConfig,
        account: AccountConfig,
    ) -> LaunchCommand | None:
        if (
            session.role not in _RESUMABLE_CONTROL_ROLES
            or account.home is None
        ):
            return None
        return self.build_launch_command(session, account)

    def transcript_sources(
        self,
        account: AccountConfig,
        session: SessionConfig | None = None,
    ) -> tuple[TranscriptSource, ...]:
        if account.home is None:
            return ()
        return (
            TranscriptSource(
                root=claude_config_dir(account.home) / "projects",
                pattern="**/*.jsonl",
                description="Claude project transcript JSONL",
            ),
        )

    def collect_usage_snapshot(
        self,
        tmux: TmuxClient,
        target: str,
        *,
        account: AccountConfig,
        session: SessionConfig,
    ) -> ProviderUsageSnapshot:
        return _collect_usage_snapshot(
            tmux, target, account=account, session=session
        )

    def _parse_usage_text(self, text: str) -> ProviderUsageSnapshot:
        """Thin delegating shim kept for back-compat with existing tests.

        Real parsing lives in
        :func:`pollypm.providers.claude.usage_parse.parse_claude_usage_snapshot`.
        """
        return parse_claude_usage_snapshot(text)


__all__ = ["ClaudeAdapter"]
