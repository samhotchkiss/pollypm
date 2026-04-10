"""Integration tests for the project history import pipeline."""

import json
import subprocess
from pathlib import Path

from pollypm.config import write_config
from pollypm.history_import import (
    import_project_history,
    load_import_state,
    lock_import,
)
from pollypm.models import (
    AccountConfig,
    KnownProject,
    PollyPMConfig,
    PollyPMSettings,
    ProjectKind,
    ProjectSettings,
    ProviderKind,
    SessionConfig,
)


def _config(tmp_path: Path) -> PollyPMConfig:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    return PollyPMConfig(
        project=ProjectSettings(
            name="TestProject",
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
                home=project_root / ".pollypm-state" / "homes" / "claude_main",
            )
        },
        sessions={
            "heartbeat": SessionConfig(
                name="heartbeat",
                role="heartbeat-supervisor",
                provider=ProviderKind.CLAUDE,
                account="claude_main",
                cwd=project_root,
            ),
        },
        projects={
            "testproject": KnownProject(
                key="testproject",
                path=project_root,
                name="TestProject",
                kind=ProjectKind.FOLDER,
            )
        },
    )


def _write_events(project_root: Path, session_id: str, events: list[dict]) -> None:
    """Write events to the project's transcript directory."""
    transcript_dir = project_root / ".pollypm" / "transcripts" / session_id
    transcript_dir.mkdir(parents=True, exist_ok=True)
    events_path = transcript_dir / "events.jsonl"
    events_path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n"
    )


def _init_git_repo(project_root: Path) -> None:
    """Initialize a git repo with some commits."""
    subprocess.run(["git", "init", str(project_root)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(project_root), "config", "user.email", "test@test.com"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(project_root), "config", "user.name", "Test User"],
        check=True, capture_output=True,
    )

    # First commit
    (project_root / "README.md").write_text("# Test Project\nA test project.\n")
    subprocess.run(["git", "-C", str(project_root), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(project_root), "commit", "-m", "Initial commit"],
        check=True, capture_output=True,
    )

    # Second commit
    (project_root / "main.py").write_text("print('hello')\n")
    subprocess.run(["git", "-C", str(project_root), "add", "main.py"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(project_root), "commit", "-m", "Add main entry point"],
        check=True, capture_output=True,
    )


def test_full_import_with_jsonl_and_docs(tmp_path: Path) -> None:
    """Test that the full pipeline discovers JSONL, builds timeline, and generates docs."""
    config = _config(tmp_path)
    project_root = config.project.root_dir

    # Write some JSONL transcript events
    _write_events(project_root, "session-a", [
        {
            "timestamp": "2026-04-01T10:00:00Z",
            "event_type": "user_turn",
            "session_id": "session-a",
            "project_key": "testproject",
            "source_path": "source-a",
            "source_offset": 0,
            "payload": {"text": "Goal: build a CLI tool for project management."},
        },
        {
            "timestamp": "2026-04-01T10:01:00Z",
            "event_type": "assistant_turn",
            "session_id": "session-a",
            "project_key": "testproject",
            "source_path": "source-a",
            "source_offset": 1,
            "payload": {"text": "Decision: we will use Python with Typer for the CLI."},
        },
        {
            "timestamp": "2026-04-01T10:02:00Z",
            "event_type": "assistant_turn",
            "session_id": "session-a",
            "project_key": "testproject",
            "source_path": "source-a",
            "source_offset": 2,
            "payload": {"text": "Convention: always use snake_case for function names."},
        },
    ])

    # Write a README
    (project_root / "README.md").write_text("# TestProject\nA CLI for project management.\n")

    # Write a config file
    (project_root / "pyproject.toml").write_text("[project]\nname = 'testproject'\n")

    result = import_project_history(
        project_root,
        "TestProject",
        skip_interview=True,
        timestamp="2026-04-10T00:00:00Z",
    )

    # Verify sources were found
    assert result.sources_found >= 3  # JSONL + README + pyproject.toml

    # Verify timeline was built
    assert result.timeline_events >= 3  # At least the 3 JSONL events

    # Verify docs were generated
    assert result.docs_generated == 5

    docs_dir = project_root / "docs"
    assert (docs_dir / "project-overview.md").exists()
    assert (docs_dir / "decisions.md").exists()
    assert (docs_dir / "architecture.md").exists()
    assert (docs_dir / "history.md").exists()
    assert (docs_dir / "conventions.md").exists()

    # Verify doc content
    overview = (docs_dir / "project-overview.md").read_text()
    assert "## Summary" in overview
    assert "2026-04-10T00:00:00Z" in overview

    decisions = (docs_dir / "decisions.md").read_text()
    assert "Python" in decisions or "Typer" in decisions or "CLI" in decisions

    conventions = (docs_dir / "conventions.md").read_text()
    assert "snake_case" in conventions

    # Verify import state is locked (skip_interview=True)
    state = load_import_state(project_root)
    assert state["status"] == "locked"
    assert result.locked is True


def test_import_with_git_history(tmp_path: Path) -> None:
    """Test that git history is discovered and included in timeline."""
    config = _config(tmp_path)
    project_root = config.project.root_dir
    _init_git_repo(project_root)

    result = import_project_history(
        project_root,
        "TestProject",
        skip_interview=True,
        timestamp="2026-04-10T00:00:00Z",
    )

    # Git counts as a source, plus README.md created in _init_git_repo
    assert result.sources_found >= 1
    assert result.timeline_events >= 2  # 2 git commits

    # History doc should contain commit messages
    history = (project_root / "docs" / "history.md").read_text()
    assert "Initial commit" in history
    assert "Add main entry point" in history


def test_provider_transcripts_copied(tmp_path: Path) -> None:
    """Test that pre-PollyPM provider transcripts are copied."""
    config = _config(tmp_path)
    project_root = config.project.root_dir

    # Create pre-PollyPM claude transcripts
    claude_dir = project_root / ".claude"
    claude_dir.mkdir()
    (claude_dir / "session.jsonl").write_text(
        json.dumps({
            "timestamp": "2026-03-15T00:00:00Z",
            "type": "user",
            "message": {"content": "Set up the project"},
        })
        + "\n"
    )

    result = import_project_history(
        project_root,
        "TestProject",
        skip_interview=True,
        timestamp="2026-04-10T00:00:00Z",
    )

    assert result.provider_transcripts_copied == 1

    # Verify copied to canonical location
    imported = project_root / ".pollypm" / "transcripts" / "imported-claude" / "session.jsonl"
    assert imported.exists()


def test_import_pending_review_without_skip(tmp_path: Path) -> None:
    """Test that import is pending_review when skip_interview=False."""
    config = _config(tmp_path)
    project_root = config.project.root_dir

    (project_root / "README.md").write_text("# Test\n")

    result = import_project_history(
        project_root,
        "TestProject",
        skip_interview=False,
        timestamp="2026-04-10T00:00:00Z",
    )

    assert result.locked is False
    state = load_import_state(project_root)
    assert state["status"] == "pending_review"

    # Interview questions should be generated
    assert len(result.interview_questions) >= 1

    # Lock after review
    lock_import(project_root)
    state = load_import_state(project_root)
    assert state["status"] == "locked"


def test_import_empty_project(tmp_path: Path) -> None:
    """Test import on a completely empty project."""
    config = _config(tmp_path)
    project_root = config.project.root_dir

    result = import_project_history(
        project_root,
        "EmptyProject",
        skip_interview=True,
        timestamp="2026-04-10T00:00:00Z",
    )

    assert result.sources_found == 0
    assert result.timeline_events == 0
    assert result.docs_generated == 0

    state = load_import_state(project_root)
    assert state["status"] == "completed"


def test_import_no_secrets_in_docs(tmp_path: Path) -> None:
    """Verify that secrets are redacted from generated documentation."""
    config = _config(tmp_path)
    project_root = config.project.root_dir

    _write_events(project_root, "session-a", [
        {
            "timestamp": "2026-04-01T10:00:00Z",
            "event_type": "assistant_turn",
            "session_id": "session-a",
            "project_key": "testproject",
            "source_path": "source-a",
            "source_offset": 0,
            "payload": {"text": "Decision: use API key ghp_aabbccddeeff00112233 for auth."},
        },
    ])

    result = import_project_history(
        project_root,
        "TestProject",
        skip_interview=True,
        timestamp="2026-04-10T00:00:00Z",
    )

    assert result.docs_generated == 5

    # Check no secrets in any doc
    docs_dir = project_root / "docs"
    for doc_file in docs_dir.iterdir():
        content = doc_file.read_text()
        assert "ghp_" not in content


def test_import_with_multiple_sessions(tmp_path: Path) -> None:
    """Test that import handles multiple transcript sessions."""
    config = _config(tmp_path)
    project_root = config.project.root_dir

    _write_events(project_root, "session-a", [
        {
            "timestamp": "2026-04-01T10:00:00Z",
            "event_type": "assistant_turn",
            "session_id": "session-a",
            "project_key": "testproject",
            "source_path": "source-a",
            "source_offset": 0,
            "payload": {"text": "Decision: use SQLite for storage."},
        },
    ])

    _write_events(project_root, "session-b", [
        {
            "timestamp": "2026-04-02T10:00:00Z",
            "event_type": "assistant_turn",
            "session_id": "session-b",
            "project_key": "testproject",
            "source_path": "source-b",
            "source_offset": 0,
            "payload": {"text": "Architecture: refactor into pipeline stages."},
        },
    ])

    result = import_project_history(
        project_root,
        "TestProject",
        skip_interview=True,
        timestamp="2026-04-10T00:00:00Z",
    )

    assert result.timeline_events >= 2
    assert result.docs_generated == 5

    decisions = (project_root / "docs" / "decisions.md").read_text()
    assert "SQLite" in decisions

    architecture = (project_root / "docs" / "architecture.md").read_text()
    assert "pipeline" in architecture
