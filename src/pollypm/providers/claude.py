from __future__ import annotations

import re
import shutil
import time
from pathlib import Path

from pollypm.models import AccountConfig, SessionConfig
from pollypm.providers.base import LaunchCommand
from pollypm.provider_sdk import ProviderAdapterBase, ProviderUsageSnapshot, TranscriptSource
from pollypm.runtime_env import claude_config_dir

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from pollypm.tmux.client import TmuxClient


class ClaudeAdapter(ProviderAdapterBase):
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
            fresh_launch_marker = account.home / ".pollypm" / "session-markers" / f"{session.name}.fresh"
        if session.role in {"heartbeat-supervisor", "operator-pm"} and account.home is not None:
            resume_argv = [self.binary, "--continue", *session.args]
            resume_marker = account.home / ".pollypm" / "session-markers" / f"{session.name}.resume"
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
        deadline = time.monotonic() + 60
        last_action = ""
        while time.monotonic() < deadline:
            pane = tmux.capture_pane(target, lines=320)
            lowered = pane.lower()
            if "select login method:" in lowered or "please run /login" in lowered:
                raise RuntimeError("Claude probe session is not authenticated.")
            if "choose the text style that looks best with your terminal" in lowered and last_action != "theme":
                tmux.send_keys(target, "", press_enter=True)
                last_action = "theme"
                time.sleep(1)
                continue
            if "quick safety check" in lowered and "yes, i trust this folder" in lowered and last_action != "trust":
                tmux.send_keys(target, "", press_enter=True)
                last_action = "trust"
                time.sleep(1)
                continue
            if "we recommend medium effort for opus" in lowered and last_action != "effort":
                tmux.send_keys(target, "", press_enter=True)
                last_action = "effort"
                time.sleep(1)
                continue
            if "❯" in pane and ("welcome back" in lowered or "/usage" not in lowered):
                tmux.send_keys(target, "/usage", press_enter=True)
                time.sleep(3)
                text = tmux.capture_pane(target, lines=320)
                return self._parse_usage_text(text)
            time.sleep(1)
        raise RuntimeError("Claude probe session did not reach an interactive prompt in time.")

    def _parse_usage_text(self, text: str) -> ProviderUsageSnapshot:
        weekly_match = re.search(
            r"Current week \(all models\).*?(\d+)% used.*?Resets ([^\n]+)",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if not weekly_match:
            return ProviderUsageSnapshot(raw_text=text)
        used = int(weekly_match.group(1))
        reset = " ".join(weekly_match.group(2).split())
        left = max(0, 100 - used)
        if used >= 95:
            health = "exhausted"
        elif used >= 80:
            health = "near-limit"
        else:
            health = "healthy"
        return ProviderUsageSnapshot(
            health=health,
            summary=f"{left}% left this week · resets {reset}",
            raw_text=text,
        )
