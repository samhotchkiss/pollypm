import json
from pathlib import Path

from pollypm.config import write_config
from pollypm.models import AccountConfig, KnownProject, ProjectKind, ProjectSettings, PollyPMConfig, PollyPMSettings, ProviderKind
from pollypm.transcript_ledger import sync_token_ledger
from pollypm.storage.state import StateStore


def _config(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    config = PollyPMConfig(
        project=ProjectSettings(
            name="pollypm",
            root_dir=root,
            base_dir=root / ".pollypm",
            logs_dir=root / ".pollypm/logs",
            snapshots_dir=root / ".pollypm/snapshots",
            state_db=root / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_main"),
        accounts={
            "claude_main": AccountConfig(
                name="claude_main",
                provider=ProviderKind.CLAUDE,
                home=root / ".pollypm/homes/claude_main",
            ),
            "codex_main": AccountConfig(
                name="codex_main",
                provider=ProviderKind.CODEX,
                home=root / ".pollypm/homes/codex_main",
            ),
        },
        sessions={},
        projects={
            "demo": KnownProject(
                key="demo",
                path=root,
                name="Demo",
                kind=ProjectKind.GIT,
            )
        },
    )
    config_path = root / "pollypm.toml"
    write_config(config, config_path, force=True)
    return config_path


def test_sync_token_ledger_reads_claude_and_codex_jsonl(tmp_path: Path) -> None:
    config_path = _config(tmp_path)
    root = config_path.parent

    claude_home = root / ".pollypm/homes/claude_main"
    claude_file = claude_home / ".claude/projects/-Users-sam-dev-pollypm/session-a.jsonl"
    claude_file.parent.mkdir(parents=True, exist_ok=True)
    claude_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "sessionId": "session-a",
                        "cwd": str(root),
                        "message": {
                            "role": "assistant",
                            "model": "claude-opus-4-6",
                            "usage": {
                                "input_tokens": 100,
                                "output_tokens": 20,
                                "cache_creation_input_tokens": 0,
                                "cache_read_input_tokens": 0,
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "sessionId": "session-a",
                        "cwd": str(root),
                        "message": {
                            "role": "assistant",
                            "model": "claude-opus-4-6",
                            "usage": {
                                "input_tokens": 200,
                                "output_tokens": 50,
                                "cache_creation_input_tokens": 0,
                                "cache_read_input_tokens": 0,
                            },
                        },
                    }
                ),
            ]
        )
        + "\n"
    )

    codex_home = root / ".pollypm/homes/codex_main"
    codex_file = codex_home / ".codex/sessions/2026/04/08/rollout-test.jsonl"
    codex_file.parent.mkdir(parents=True, exist_ok=True)
    codex_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-04-08T10:00:00Z",
                        "type": "session_meta",
                        "payload": {
                            "id": "session-b",
                            "cwd": str(root),
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-04-08T10:00:01Z",
                        "type": "turn_context",
                        "payload": {
                            "turn_id": "turn-b",
                            "cwd": str(root),
                            "model": "gpt-5.4",
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-04-08T10:00:02Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "total_token_usage": {
                                    "input_tokens": 300,
                                    "cached_input_tokens": 20,
                                    "output_tokens": 30,
                                    "reasoning_output_tokens": 5,
                                    "total_tokens": 355,
                                },
                                "last_token_usage": {
                                    "input_tokens": 300,
                                    "cached_input_tokens": 20,
                                    "output_tokens": 30,
                                    "reasoning_output_tokens": 5,
                                    "total_tokens": 355,
                                },
                            },
                            "rate_limits": {
                                "limit_id": "codex",
                                "primary": {"used_percent": 0.0, "window_minutes": 300, "resets_at": 0},
                                "secondary": {"used_percent": 0.0, "window_minutes": 10080, "resets_at": 0},
                                "credits": None,
                                "plan_type": "pro",
                            },
                        },
                    }
                ),
            ]
        )
        + "\n"
    )

    samples = sync_token_ledger(config_path)
    store = StateStore(root / ".pollypm/state.db")
    usage = store.recent_token_usage(limit=10)

    assert len(samples) == 2
    assert {sample.account_name for sample in samples} == {"claude_main", "codex_main"}
    assert {sample.project_key for sample in samples} == {"demo"}
    assert len(usage) == 2
    usage_by_account = {row.account_name: row.tokens_used for row in usage}
    assert usage_by_account["claude_main"] == 370
    assert usage_by_account["codex_main"] == 355
