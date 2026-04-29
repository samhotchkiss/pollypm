import json
from pathlib import Path

from pollypm.models import AccountConfig, ProviderKind, SessionConfig
from pollypm.providers.claude import ClaudeAdapter
from pollypm.providers.codex import CodexAdapter


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
        project="pollypm",
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
    assert snapshot.used_pct == 80
    assert snapshot.remaining_pct == 20
    assert snapshot.reset_at == "Monday 1am"
    assert snapshot.period_label == "current week"
    assert adapter.build_resume_command(session, account) is not None


def test_claude_provider_prefers_recorded_resume_session_id(tmp_path: Path) -> None:
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
        project="pollypm",
        args=["--model", "sonnet"],
    )
    marker = account.home / ".pollypm" / "session-markers" / "operator.resume"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("claude-session-123\n", encoding="utf-8")
    # #935 — ``build_launch_command`` validates the marker by reading
    # the transcript's first user message and confirming it references
    # ``/control-prompts/<session_name>.md`` (the supervisor-written
    # bootstrap). Seed a matching transcript so the resume path is
    # taken; without it the adapter would unlink the marker as poisoned
    # and fall through to a fresh launch (``resume_argv = None``).
    bucket = (
        account.home
        / ".claude"
        / "projects"
        / str(session.cwd.resolve()).replace("/", "-")
    )
    bucket.mkdir(parents=True, exist_ok=True)
    bootstrap = (
        f"Read {tmp_path}/.pollypm/control-prompts/{session.name}.md, "
        "adopt it as your operating instructions, and reply READY."
    )
    transcript_records = [
        {
            "type": "user",
            "message": {"role": "user", "content": bootstrap},
            "uuid": "msg-claude-session-123",
            "sessionId": "claude-session-123",
        },
    ]
    (bucket / "claude-session-123.jsonl").write_text(
        "\n".join(json.dumps(record) for record in transcript_records) + "\n",
        encoding="utf-8",
    )

    launch = adapter.build_launch_command(session, account)

    assert launch.resume_argv == [
        "claude",
        "--dangerously-skip-permissions",
        "--resume",
        "claude-session-123",
        "--model",
        "sonnet",
    ]


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
        project="pollypm",
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
    assert snapshot.used_pct == 0
    assert snapshot.remaining_pct == 100
    assert snapshot.period_label == "current period"
    assert adapter.build_resume_command(session, account) is not None


def test_codex_provider_drives_status_for_new_pane_format(tmp_path: Path) -> None:
    """Regression for #957.

    Codex 0.125+ no longer prints ``% left`` on the welcome screen — usage
    is gated behind the ``/status`` slash command. The adapter must
    drive ``/status`` and parse the resulting ``5h limit`` /
    ``Weekly limit`` block; otherwise every probe lands with
    ``usage unavailable`` and the cockpit panel goes blank.
    """
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
        project="pollypm",
    )
    welcome_pane = (
        "OpenAI Codex (v0.125.0)\n"
        "model: gpt-5.5\n"
        "› Implement {feature}\n"
    )
    status_pane = (
        "OpenAI Codex (v0.125.0)\n"
        "/status\n"
        "  5h limit:    [################----] 80% left (resets 08:59)\n"
        "  Weekly limit: [###############-----] 75% left "
        "(resets 10:09 on 5 May)\n"
        "› Implement {feature}\n"
    )
    tmux = _FakeTmux([welcome_pane, status_pane])

    snapshot = adapter.collect_usage_snapshot(
        tmux, "session:0", account=account, session=session,
    )

    sent = [text for _target, text, _enter in tmux.sent]
    assert "/status" in sent, "adapter must drive /status to surface usage"
    # Weekly bucket is the headline number (matches Claude's weekly summary).
    assert snapshot.health == "healthy"
    assert snapshot.used_pct == 25
    assert snapshot.remaining_pct == 75
    assert snapshot.period_label == "current week"
    assert "75% left this week" in (snapshot.summary or "")
    assert "5h: 80% left" in (snapshot.summary or "")
    assert snapshot.reset_at == "10:09 on 5 May"


def test_codex_provider_uses_cli_prompt_for_fresh_launch(tmp_path: Path) -> None:
    adapter = CodexAdapter()
    account = AccountConfig(
        name="codex_primary",
        provider=ProviderKind.CODEX,
        home=tmp_path / "home",
    )
    session = SessionConfig(
        name="worker",
        role="worker",
        provider=ProviderKind.CODEX,
        account="codex_primary",
        cwd=tmp_path,
        project="pollypm",
        prompt="Investigate the issue queue",
    )

    launch = adapter.build_launch_command(session, account)

    assert launch.argv == ["codex"]
    assert launch.initial_input == "Investigate the issue queue"
