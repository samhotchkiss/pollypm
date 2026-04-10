import json
from pathlib import Path

import pytest

from pollypm.checkpoints import write_mechanical_checkpoint
from pollypm.config import write_config
from pollypm.models import (
    AccountConfig,
    KnownProject,
    ProjectKind,
    ProjectSettings,
    PollyPMConfig,
    PollyPMSettings,
    ProviderKind,
    SessionConfig,
    SessionLaunchSpec,
)
from pollypm.supervisor import Supervisor
from pollypm.transcript_ingest import sync_transcripts_once
from pollypm.worktrees import ensure_worktree


def _config(tmp_path: Path) -> tuple[PollyPMConfig, Path]:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".git").mkdir()
    config = PollyPMConfig(
        project=ProjectSettings(
            name="pollypm",
            root_dir=tmp_path,
            base_dir=tmp_path / ".pollypm-state",
            logs_dir=tmp_path / ".pollypm-state/logs",
            snapshots_dir=tmp_path / ".pollypm-state/snapshots",
            state_db=tmp_path / ".pollypm-state/state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_main"),
        accounts={
            "claude_main": AccountConfig(
                name="claude_main",
                provider=ProviderKind.CLAUDE,
                home=tmp_path / ".pollypm-state" / "homes" / "claude_main",
            )
        },
        sessions={
            "worker_demo": SessionConfig(
                name="worker_demo",
                role="worker",
                provider=ProviderKind.CLAUDE,
                account="claude_main",
                cwd=project_root,
                project="demo",
                window_name="worker-demo",
            ),
            "review_demo": SessionConfig(
                name="review_demo",
                role="review",
                provider=ProviderKind.CLAUDE,
                account="claude_main",
                cwd=project_root,
                project="demo",
                window_name="review-demo",
            ),
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
    config_path = tmp_path / "pollypm.toml"
    write_config(config, config_path, force=True)
    return config, config_path


def test_session_scoped_paths_do_not_collide_and_locks_are_respected(tmp_path: Path) -> None:
    config, config_path = _config(tmp_path)
    supervisor = Supervisor(config)
    launches = {launch.session.name: launch for launch in supervisor.plan_launches()}

    assert launches["worker_demo"].log_path == tmp_path / ".pollypm-state/logs/worker_demo/worker-demo.log"
    assert launches["review_demo"].log_path == tmp_path / ".pollypm-state/logs/review_demo/review-demo.log"
    assert launches["worker_demo"].log_path != launches["review_demo"].log_path
    assert (tmp_path / ".pollypm-state/logs/worker_demo/.session.lock").exists()
    assert (tmp_path / ".pollypm-state/logs/review_demo/.session.lock").exists()

    worker_checkpoint = write_mechanical_checkpoint(
        config,
        launches["worker_demo"],
        snapshot_path=tmp_path / ".pollypm-state/snapshots/worker.txt",
        snapshot_content="one\n",
        log_bytes=1,
        alerts=[],
    )
    review_checkpoint = write_mechanical_checkpoint(
        config,
        launches["review_demo"],
        snapshot_path=tmp_path / ".pollypm-state/snapshots/review.txt",
        snapshot_content="two\n",
        log_bytes=2,
        alerts=[],
    )
    assert worker_checkpoint.json_path.parent != review_checkpoint.json_path.parent
    assert worker_checkpoint.json_path.parent == config.projects["demo"].path / ".pollypm/artifacts/checkpoints/worker_demo"
    assert (worker_checkpoint.json_path.parent / ".session.lock").exists()

    transcript_file = config.accounts["claude_main"].home / ".claude/projects/demo/session-a.jsonl"
    transcript_file.parent.mkdir(parents=True, exist_ok=True)
    transcript_file.write_text(
        json.dumps(
            {
                "timestamp": "2026-04-10T00:00:00Z",
                "type": "assistant",
                "sessionId": "session-a",
                "cwd": str(config.projects["demo"].path),
                "message": {"content": [{"type": "text", "text": "Hello"}], "usage": {"total_tokens": 1}},
            }
        )
        + "\n"
    )
    sync_transcripts_once(config)
    assert (config.projects["demo"].path / ".pollypm/transcripts/session-a/.session.lock").exists()

    conflicting_root = config.projects["demo"].path / ".pollypm/worktrees/worker_demo"
    conflicting_root.mkdir(parents=True, exist_ok=True)
    (conflicting_root / ".session.lock").write_text(json.dumps({"session_id": "other_session"}) + "\n")
    with pytest.raises(RuntimeError, match="Session lock conflict"):
        ensure_worktree(
            config_path,
            project_key="demo",
            lane_kind="pa",
            lane_key="task-1",
            session_name="worker_demo",
        )
