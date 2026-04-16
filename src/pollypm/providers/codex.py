from __future__ import annotations

import re
import shutil
import time
from pathlib import Path

from pollypm.models import AccountConfig, SessionConfig
from pollypm.providers.base import LaunchCommand
from pollypm.provider_sdk import ProviderAdapterBase, ProviderUsageSnapshot, TranscriptSource
from pollypm.runtime_env import codex_home_dir

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from pollypm.tmux.client import TmuxClient


class CodexAdapter(ProviderAdapterBase):
    name = "codex"
    binary = "codex"

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
            fresh_launch_marker = account.home / ".pollypm-state" / "session-markers" / f"{session.name}.fresh"
        if session.role in {"heartbeat-supervisor", "operator-pm"} and account.home is not None:
            resume_argv = [self.binary, "resume", "--last", *session.args]
            resume_marker = account.home / ".pollypm-state" / "session-markers" / f"{session.name}.resume"
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
        if session.role not in {"heartbeat-supervisor", "operator-pm"} or account.home is None:
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
                root=codex_home_dir(account.home) / "sessions",
                pattern="**/rollout-*.jsonl",
                description="Codex session transcript JSONL",
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
        deadline = time.monotonic() + 20
        text = ""
        while time.monotonic() < deadline:
            text = tmux.capture_pane(target, lines=320)
            lowered = text.lower()
            if "do you trust the contents of this directory" in lowered and "1. yes, continue" in lowered:
                tmux.send_keys(target, "", press_enter=True)
                time.sleep(1)
                continue
            if "openai codex" in lowered and ("›" in lowered or "% left" in lowered):
                return self._parse_usage_text(text)
            time.sleep(1)
        return self._parse_usage_text(text)

    def _parse_usage_text(self, text: str) -> ProviderUsageSnapshot:
        match = re.search(r"(\d+)% left", text, re.IGNORECASE)
        if not match:
            return ProviderUsageSnapshot(raw_text=text)
        left = int(match.group(1))
        if left <= 5:
            health = "exhausted"
        elif left <= 20:
            health = "near-limit"
        else:
            health = "healthy"
        return ProviderUsageSnapshot(
            health=health,
            summary=f"{left}% left",
            raw_text=text,
        )
