"""Integration tests for recovery prompt construction."""

from pathlib import Path

from pollypm.checkpoints import CheckpointData, create_level0_checkpoint, create_level1_checkpoint
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
from pollypm.recovery_prompt import build_recovery_prompt


def _config(tmp_path: Path) -> PollyPMConfig:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    return PollyPMConfig(
        project=ProjectSettings(
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
            "worker": SessionConfig(
                name="worker",
                role="worker",
                provider=ProviderKind.CLAUDE,
                account="claude_main",
                cwd=project_root,
                project="test",
                window_name="worker-test",
            )
        },
        projects={
            "test": KnownProject(
                key="test",
                path=project_root,
                name="TestProject",
                kind=ProjectKind.FOLDER,
            )
        },
    )


def _launch(config: PollyPMConfig) -> SessionLaunchSpec:
    return SessionLaunchSpec(
        session=config.sessions["worker"],
        account=config.accounts["claude_main"],
        window_name="worker-test",
        log_path=config.project.root_dir / "logs" / "worker.log",
        command="claude",
    )


def test_recovery_prompt_from_level1_checkpoint(tmp_path: Path) -> None:
    """Build recovery prompt from a Level 1 checkpoint."""
    config = _config(tmp_path)
    launch = _launch(config)

    # Create Level 0
    l0_data, _ = create_level0_checkpoint(
        config, launch,
        snapshot_content="$ pytest -q\n10 passed\n",
    )

    # Create Level 1 with rich data
    l1_data, _ = create_level1_checkpoint(
        config, launch,
        level0=l0_data,
        trigger="failover",
    )
    # Manually set fields that heuristic might not fill
    # The checkpoint is already saved; let's create a new one with explicit data
    l1_enriched = CheckpointData(
        checkpoint_id=l1_data.checkpoint_id,
        session_name="worker",
        project="test",
        role="worker",
        level=1,
        provider="claude",
        account="claude_main",
        objective="Implement the parser module",
        sub_step="Writing unit tests for tokenizer",
        work_completed=["Implemented tokenizer", "Added grammar rules"],
        recommended_next_step="Write tests for the tokenizer",
        blockers=["Need to decide on error recovery strategy"],
    )
    # Write it as the canonical checkpoint
    from pollypm.checkpoints import _write_checkpoint_files, _checkpoint_root
    checkpoint_root = _checkpoint_root(config, "worker", "test")
    _write_checkpoint_files(checkpoint_root, l1_enriched)

    # Build recovery prompt
    prompt = build_recovery_prompt(
        config, "worker", "test",
        provider=ProviderKind.CLAUDE,
    )

    assert not prompt.is_fallback
    rendered = prompt.render()

    assert "parser module" in rendered.lower()
    assert "tokenizer" in rendered.lower()
    assert "error recovery strategy" in rendered.lower()
    assert "resuming" in rendered.lower()


def test_recovery_prompt_fallback_no_checkpoint(tmp_path: Path) -> None:
    """When no checkpoint exists, fall back to project context."""
    config = _config(tmp_path)

    # Create project overview
    docs_dir = config.project.root_dir / "docs"
    docs_dir.mkdir(parents=True)
    (docs_dir / "project-overview.md").write_text(
        "# TestProject\n\nA CLI tool for project management.\n"
    )

    prompt = build_recovery_prompt(
        config, "worker", "test",
        provider=ProviderKind.CLAUDE,
        task_prompt="Implement issue #42: Add user authentication",
    )

    assert prompt.is_fallback
    rendered = prompt.render()

    assert "TestProject" in rendered
    assert "issue #42" in rendered
    assert "resuming" in rendered.lower()


def test_cross_provider_recovery(tmp_path: Path) -> None:
    """Recovery prompt should be formatted differently per provider."""
    config = _config(tmp_path)
    launch = _launch(config)

    # Create a checkpoint
    l0_data, _ = create_level0_checkpoint(
        config, launch,
        snapshot_content="test content\n",
    )

    claude_prompt = build_recovery_prompt(
        config, "worker", "test",
        provider=ProviderKind.CLAUDE,
        task_prompt="Fix the bug",
    )
    codex_prompt = build_recovery_prompt(
        config, "worker", "test",
        provider=ProviderKind.CODEX,
        task_prompt="Fix the bug",
    )

    claude_text = claude_prompt.render()
    codex_text = codex_prompt.render()

    # Both should contain the task
    assert "Fix the bug" in claude_text
    assert "Fix the bug" in codex_text

    # Claude uses ## headings, Codex uses ###
    assert "##" in claude_text
    assert "###" in codex_text


def test_large_prompt_truncation(tmp_path: Path) -> None:
    """Large prompts should be truncated to fit."""
    config = _config(tmp_path)

    # Create a large project overview
    docs_dir = config.project.root_dir / "docs"
    docs_dir.mkdir(parents=True)
    (docs_dir / "project-overview.md").write_text("# Big Project\n" + "x" * 50000)

    prompt = build_recovery_prompt(
        config, "worker", "test",
        provider=ProviderKind.CLAUDE,
        task_prompt="Do something",
        max_chars=5000,
    )

    rendered = prompt.render()
    # Should be truncated
    assert len(rendered) < 10000  # Well under the 50K original


def test_recovery_with_no_data_at_all(tmp_path: Path) -> None:
    """Recovery with no checkpoint, no docs, no task should still work."""
    config = _config(tmp_path)

    prompt = build_recovery_prompt(
        config, "worker", "test",
        provider=ProviderKind.CLAUDE,
    )

    assert prompt.is_fallback
    rendered = prompt.render()
    assert "resuming" in rendered.lower()
