"""Shared test fixtures for integration and e2e tests.

Provides sample configs, transcripts, checkpoints, and project
structures that can be used across test suites without touching
production state.
"""

from __future__ import annotations

import json
from pathlib import Path

from pollypm.models import (
    AccountConfig,
    KnownProject,
    PollyPMConfig,
    PollyPMSettings,
    ProjectKind,
    ProjectSettings,
    ProviderKind,
    SessionConfig,
    SessionLaunchSpec,
)


def sample_config(tmp_path: Path, *, project_name: str = "TestProject") -> PollyPMConfig:
    """Create a complete isolated PollyPMConfig for testing."""
    project_root = tmp_path / "repo"
    project_root.mkdir(exist_ok=True)
    return PollyPMConfig(
        project=ProjectSettings(
            name=project_name,
            root_dir=project_root,
            base_dir=project_root / ".pollypm",
            logs_dir=project_root / ".pollypm/logs",
            snapshots_dir=project_root / ".pollypm/snapshots",
            state_db=project_root / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(
            controller_account="claude_main",
            failover_enabled=True,
            failover_accounts=["claude_backup"],
        ),
        accounts={
            "claude_main": AccountConfig(
                name="claude_main",
                provider=ProviderKind.CLAUDE,
                home=project_root / ".pollypm" / "homes" / "claude_main",
            ),
            "claude_backup": AccountConfig(
                name="claude_backup",
                provider=ProviderKind.CLAUDE,
                home=project_root / ".pollypm" / "homes" / "claude_backup",
            ),
        },
        sessions={
            "heartbeat": SessionConfig(
                name="heartbeat",
                role="heartbeat-supervisor",
                provider=ProviderKind.CLAUDE,
                account="claude_main",
                cwd=project_root,
            ),
            "operator": SessionConfig(
                name="operator",
                role="operator-pm",
                provider=ProviderKind.CLAUDE,
                account="claude_main",
                cwd=project_root,
            ),
            "worker": SessionConfig(
                name="worker",
                role="worker",
                provider=ProviderKind.CLAUDE,
                account="claude_main",
                cwd=project_root,
                project="test",
                window_name="worker-test",
            ),
        },
        projects={
            "test": KnownProject(
                key="test",
                path=project_root,
                name=project_name,
                kind=ProjectKind.FOLDER,
            )
        },
    )


def sample_launch(config: PollyPMConfig, session_name: str = "worker") -> SessionLaunchSpec:
    """Create a SessionLaunchSpec for testing."""
    session = config.sessions[session_name]
    account = config.accounts[session.account]
    return SessionLaunchSpec(
        session=session,
        account=account,
        window_name=session.window_name or f"{session_name}-test",
        log_path=config.project.root_dir / "logs" / f"{session_name}.log",
        command="claude",
    )


def sample_transcript_events() -> list[dict]:
    """Sample JSONL transcript events."""
    return [
        {
            "timestamp": "2026-04-01T10:00:00Z",
            "event_type": "user_turn",
            "session_id": "session-a",
            "project_key": "test",
            "source_path": "source-a",
            "source_offset": 0,
            "payload": {"text": "Goal: build a project management CLI."},
        },
        {
            "timestamp": "2026-04-01T10:01:00Z",
            "event_type": "assistant_turn",
            "session_id": "session-a",
            "project_key": "test",
            "source_path": "source-a",
            "source_offset": 1,
            "payload": {"text": "Decision: we will use Python with Typer for the CLI framework."},
        },
        {
            "timestamp": "2026-04-01T10:02:00Z",
            "event_type": "assistant_turn",
            "session_id": "session-a",
            "project_key": "test",
            "source_path": "source-a",
            "source_offset": 2,
            "payload": {"text": "Architecture: plugin-based design with provider adapters."},
        },
    ]


def write_transcript_events(
    project_root: Path,
    session_id: str,
    events: list[dict],
) -> Path:
    """Write events to a transcript JSONL file."""
    transcript_dir = project_root / ".pollypm" / "transcripts" / session_id
    transcript_dir.mkdir(parents=True, exist_ok=True)
    events_path = transcript_dir / "events.jsonl"
    events_path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n"
    )
    return events_path


def write_project_overview(project_root: Path, content: str | None = None) -> Path:
    """Write a project-overview.md for testing."""
    docs_dir = project_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    path = docs_dir / "project-overview.md"
    path.write_text(
        content
        or "# TestProject Overview\n\n## Summary\n\nA test project for validating PollyPM.\n"
    )
    return path


def sample_checkpoint_data() -> dict:
    """Sample checkpoint data dict."""
    return {
        "checkpoint_id": "20260410T000000Z-abcd1234",
        "session_name": "worker",
        "project": "test",
        "role": "worker",
        "level": 1,
        "trigger": "turn_end",
        "created_at": "2026-04-10T00:00:00Z",
        "parent_checkpoint_id": "",
        "is_canonical": True,
        "transcript_tail": ["$ pytest -q", "10 passed in 1.5s"],
        "files_changed": ["main.py", "test_main.py"],
        "git_branch": "main",
        "git_status": "M main.py",
        "git_diff_stat": "",
        "commands_observed": ["pytest -q"],
        "test_results": {"passed": 10},
        "worktree_path": "",
        "provider": "claude",
        "account": "claude_main",
        "lease_holder": "",
        "snapshot_hash": "abc123",
        "objective": "Implement the parser module",
        "sub_step": "Writing unit tests",
        "work_completed": ["Implemented tokenizer", "Added grammar rules"],
        "blockers": [],
        "unresolved_questions": [],
        "recommended_next_step": "Write remaining tests",
        "confidence": "high",
        "progress_pct": 0,
        "approach_assessment": "",
        "drift_analysis": "",
        "risk_factors": [],
        "alternative_approaches": [],
        "cross_session_context": "",
    }
