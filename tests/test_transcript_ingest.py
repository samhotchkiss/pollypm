import json
from pathlib import Path

from typer.testing import CliRunner

import pollypm.cli as cli
from pollypm.config import write_config
from pollypm.models import AccountConfig, KnownProject, ProjectKind, ProjectSettings, PollyPMConfig, PollyPMSettings, ProviderKind, SessionConfig
from pollypm.service_api import PollyPMService
from pollypm.transcript_ingest import sync_transcripts_once


def _config(tmp_path: Path) -> tuple[PollyPMConfig, Path]:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    config = PollyPMConfig(
        project=ProjectSettings(
            name="pollypm",
            root_dir=project_root,
            base_dir=project_root / ".pollypm-state",
            logs_dir=project_root / ".pollypm-state/logs",
            snapshots_dir=project_root / ".pollypm-state/snapshots",
            state_db=project_root / ".pollypm-state/state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_main"),
        accounts={
            "claude_main": AccountConfig(
                name="claude_main",
                provider=ProviderKind.CLAUDE,
                home=project_root / ".pollypm-state/homes/claude_main",
            ),
            "codex_main": AccountConfig(
                name="codex_main",
                provider=ProviderKind.CODEX,
                home=project_root / ".pollypm-state/homes/codex_main",
            ),
        },
        sessions={
            "heartbeat": SessionConfig(
                name="heartbeat",
                role="heartbeat-supervisor",
                provider=ProviderKind.CLAUDE,
                account="claude_main",
                cwd=project_root,
            )
        },
        projects={
            "demo": KnownProject(
                key="demo",
                path=project_root,
                name="Demo",
                kind=ProjectKind.GIT,
            )
        },
    )
    config_path = project_root / "pollypm.toml"
    write_config(config, config_path, force=True)
    return config, config_path


def test_sync_transcripts_once_normalizes_claude_and_codex_events(tmp_path: Path) -> None:
    config, _config_path = _config(tmp_path)
    claude_file = config.accounts["claude_main"].home / ".claude/projects/demo/session-a.jsonl"
    claude_file.parent.mkdir(parents=True, exist_ok=True)
    claude_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-04-10T00:00:00Z",
                        "type": "user",
                        "sessionId": "session-a",
                        "cwd": str(config.project.root_dir),
                        "message": {"content": "Do the next task"},
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-04-10T00:00:01Z",
                        "type": "assistant",
                        "sessionId": "session-a",
                        "cwd": str(config.project.root_dir),
                        "message": {
                            "model": "claude-opus-4-6",
                            "content": [{"type": "text", "text": "Implemented it."}],
                            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                        },
                    }
                ),
            ]
        )
        + "\n"
    )

    codex_file = config.accounts["codex_main"].home / ".codex/sessions/2026/04/10/rollout-test.jsonl"
    codex_file.parent.mkdir(parents=True, exist_ok=True)
    codex_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-04-10T00:00:00Z",
                        "type": "session_meta",
                        "payload": {"id": "session-b", "cwd": str(config.project.root_dir)},
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-04-10T00:00:01Z",
                        "type": "turn_context",
                        "payload": {"cwd": str(config.project.root_dir), "model": "gpt-5.4"},
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-04-10T00:00:02Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {"last_token_usage": {"total_tokens": 21}},
                        },
                    }
                ),
            ]
        )
        + "\n"
    )

    sync_transcripts_once(config)

    claude_events = [
        json.loads(line)
        for line in (config.project.root_dir / ".pollypm/transcripts/session-a/events.jsonl").read_text().splitlines()
    ]
    codex_events = [
        json.loads(line)
        for line in (config.project.root_dir / ".pollypm/transcripts/session-b/events.jsonl").read_text().splitlines()
    ]

    assert [event["event_type"] for event in claude_events] == ["user_turn", "assistant_turn", "token_usage"]
    assert claude_events[-1]["payload"]["total_tokens"] == 15
    assert [event["event_type"] for event in codex_events] == ["session_state", "token_usage"]
    assert codex_events[-1]["payload"]["total_tokens"] == 21
    assert (config.project.root_dir / ".pollypm/transcripts/session-a/.session.lock").exists()
    assert (config.project.root_dir / ".pollypm/transcripts/session-b/.session.lock").exists()


def test_sync_transcripts_once_resumes_and_picks_up_rotated_file(tmp_path: Path) -> None:
    config, _config_path = _config(tmp_path)
    claude_root = config.accounts["claude_main"].home / ".claude/projects/demo"
    first_file = claude_root / "session-a.jsonl"
    first_file.parent.mkdir(parents=True, exist_ok=True)
    first_file.write_text(
        json.dumps(
            {
                "timestamp": "2026-04-10T00:00:00Z",
                "type": "assistant",
                "sessionId": "session-a",
                "cwd": str(config.project.root_dir),
                "message": {"content": [{"type": "text", "text": "First"}], "usage": {"total_tokens": 1}},
            }
        )
        + "\n"
    )

    sync_transcripts_once(config)
    sync_transcripts_once(config)

    second_file = claude_root / "session-a-rotated.jsonl"
    second_file.write_text(
        json.dumps(
            {
                "timestamp": "2026-04-10T00:00:01Z",
                "type": "assistant",
                "sessionId": "session-a",
                "cwd": str(config.project.root_dir),
                "message": {"content": [{"type": "text", "text": "Second"}], "usage": {"total_tokens": 2}},
            }
        )
        + "\n"
    )

    sync_transcripts_once(config)

    events = [
        json.loads(line)
        for line in (config.project.root_dir / ".pollypm/transcripts/session-a/events.jsonl").read_text().splitlines()
    ]
    assistant_texts = [event["payload"]["text"] for event in events if event["event_type"] == "assistant_turn"]
    assert assistant_texts == ["First", "Second"]


def test_service_load_supervisor_does_not_start_transcript_ingestion(monkeypatch, tmp_path: Path) -> None:
    _loaded_config, config_path = _config(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr("pollypm.cli.start_transcript_ingestion", lambda config: calls.append(str(config.project.base_dir)))

    PollyPMService(config_path).load_supervisor()

    assert calls == []


def test_up_starts_transcript_ingestion(monkeypatch, tmp_path: Path) -> None:
    _loaded_config, config_path = _config(tmp_path)
    calls: list[str] = []

    class FakeTmux:
        def has_session(self, name: str) -> bool:
            return name == "pollypm"

        def current_session_name(self):
            return "pollypm"

    class FakeSupervisor:
        def __init__(self) -> None:
            self.tmux = FakeTmux()
            self.config = type(
                "Config",
                (),
                {
                    "project": type("Project", (), {"tmux_session": "pollypm", "base_dir": config_path.parent / ".pollypm-state"})(),
                    "accounts": {},
                    "projects": {},
                },
            )()

        def ensure_layout(self) -> None:
            return None

        def storage_closet_session_name(self) -> str:
            return "pollypm-storage-closet"

        def ensure_console_window(self) -> None:
            return None

        def ensure_heartbeat_schedule(self) -> None:
            return None

        def focus_console(self) -> None:
            return None

    monkeypatch.setattr(cli, "_load_supervisor", lambda path: FakeSupervisor())
    monkeypatch.setattr(cli, "start_transcript_ingestion", lambda config: calls.append(str(config.project.base_dir)))

    runner = CliRunner()
    result = runner.invoke(cli.app, ["up", "--config", str(config_path)])

    assert result.exit_code == 0
    assert calls == [str(config_path.parent / ".pollypm-state")]
