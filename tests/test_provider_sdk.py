from pathlib import Path

from promptmaster.models import AccountConfig, ProviderKind, SessionConfig
from promptmaster.providers.claude import ClaudeAdapter
from promptmaster.providers.codex import CodexAdapter


class _FakeTmux:
    def __init__(self, panes: list[str]) -> None:
        self._panes = panes
        self.sent: list[tuple[str, str, bool]] = []

    def capture_pane(self, _target: str, lines: int = 320) -> str:
        if self._panes:
            return self._panes.pop(0)
        return ""

    def send_keys(self, target: str, text: str, press_enter: bool = False) -> None:
        self.sent.append((target, text, press_enter))


def test_claude_provider_exposes_transcript_sources_and_usage_snapshot(tmp_path: Path) -> None:
    adapter = ClaudeAdapter()
    account = AccountConfig(
        name="claude_primary",
        provider=ProviderKind.CLAUDE,
        home=tmp_path / "home",
    )
    session = SessionConfig(
        name="operator",
        role="operator-pm",
        provider=ProviderKind.CLAUDE,
        account="claude_primary",
        cwd=tmp_path,
        project="promptmaster",
    )
    tmux = _FakeTmux(
        [
            "Welcome back\n❯",
            "Current week (all models)\n80% used\nResets Monday 1am\n",
        ]
    )

    sources = adapter.transcript_sources(account, session)
    snapshot = adapter.collect_usage_snapshot(tmux, "session:0", account=account, session=session)

    assert sources[0].root == tmp_path / "home" / ".claude" / "projects"
    assert sources[0].pattern == "**/*.jsonl"
    assert snapshot.health == "near-limit"
    assert snapshot.summary == "20% left this week · resets Monday 1am"
    assert adapter.build_resume_command(session, account) is not None


def test_codex_provider_exposes_transcript_sources_and_usage_snapshot(tmp_path: Path) -> None:
    adapter = CodexAdapter()
    account = AccountConfig(
        name="codex_primary",
        provider=ProviderKind.CODEX,
        home=tmp_path / "home",
    )
    session = SessionConfig(
        name="operator",
        role="operator-pm",
        provider=ProviderKind.CODEX,
        account="codex_primary",
        cwd=tmp_path,
        project="promptmaster",
    )
    tmux = _FakeTmux(
        [
            "OpenAI Codex\n› 100% left\n",
        ]
    )

    sources = adapter.transcript_sources(account, session)
    snapshot = adapter.collect_usage_snapshot(tmux, "session:0", account=account, session=session)

    assert sources[0].root == tmp_path / "home" / ".codex" / "sessions"
    assert sources[0].pattern == "**/rollout-*.jsonl"
    assert snapshot.health == "healthy"
    assert snapshot.summary == "100% left"
    assert adapter.build_resume_command(session, account) is not None
