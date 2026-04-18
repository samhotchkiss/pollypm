"""Integration tests for the three-tier checkpoint system."""

import json
from pathlib import Path

from pollypm.checkpoints import (
    CheckpointData,
    create_level0_checkpoint,
    create_level1_checkpoint,
    create_level2_checkpoint,
    has_meaningful_work,
    load_canonical_checkpoint,
    record_checkpoint,
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
    SessionLaunchSpec,
)
from pollypm.storage.state import StateStore


def _config(tmp_path: Path) -> PollyPMConfig:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    return PollyPMConfig(
        project=ProjectSettings(
            root_dir=project_root,
            base_dir=project_root / ".pollypm",
            logs_dir=project_root / ".pollypm/logs",
            snapshots_dir=project_root / ".pollypm/snapshots",
            state_db=project_root / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_main"),
        accounts={
            "claude_main": AccountConfig(
                name="claude_main",
                provider=ProviderKind.CLAUDE,
                home=project_root / ".pollypm" / "homes" / "claude_main",
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


def test_full_checkpoint_lifecycle(tmp_path: Path) -> None:
    """Test Level 0 → Level 1 → Level 2 checkpoint chain."""
    config = _config(tmp_path)
    launch = _launch(config)

    # Level 0: heartbeat snapshot
    l0_data, l0_artifact = create_level0_checkpoint(
        config, launch,
        snapshot_content="$ pytest -q\n10 passed in 1.5s\n$ git status\nM main.py\n",
    )
    assert l0_data.level == 0
    assert l0_data.is_canonical is True
    assert "pytest -q" in l0_data.commands_observed
    assert l0_data.test_results.get("passed") == 10

    # Check canonical points to L0
    canonical = load_canonical_checkpoint(config, "worker", "test")
    assert canonical is not None
    assert canonical.checkpoint_id == l0_data.checkpoint_id

    # Level 1: turn end with meaningful work
    assert has_meaningful_work(l0_data, None) is True

    l1_data, l1_artifact = create_level1_checkpoint(
        config, launch,
        level0=l0_data,
        trigger="turn_end",
        transcript_since_last="Implemented the parser module and wrote tests.",
    )
    assert l1_data.level == 1
    assert l1_data.parent_checkpoint_id == l0_data.checkpoint_id
    assert l1_data.test_results == l0_data.test_results  # Inherited from L0

    # Canonical now points to L1
    canonical = load_canonical_checkpoint(config, "worker", "test")
    assert canonical.checkpoint_id == l1_data.checkpoint_id

    # Level 2: PM request
    l2_data, l2_artifact = create_level2_checkpoint(
        config, launch,
        level1=l1_data,
        trigger="pm_request",
    )
    assert l2_data.level == 2
    assert l2_data.parent_checkpoint_id == l1_data.checkpoint_id
    assert l2_data.objective == l1_data.objective  # Inherited from L1

    # Canonical now points to L2
    canonical = load_canonical_checkpoint(config, "worker", "test")
    assert canonical.checkpoint_id == l2_data.checkpoint_id


def test_level1_skipped_when_no_meaningful_work(tmp_path: Path) -> None:
    """Level 1 should only be created when meaningful work occurred."""
    config = _config(tmp_path)
    launch = _launch(config)

    # Create initial L0
    l0_data, _ = create_level0_checkpoint(
        config, launch,
        snapshot_content="idle session\n",
    )

    # No files changed, no tests, no git diff → no meaningful work
    assert has_meaningful_work(l0_data, None) is False


def test_delta_based_level1_updates(tmp_path: Path) -> None:
    """Sequential Level 1 checkpoints should be delta-based."""
    config = _config(tmp_path)
    launch = _launch(config)

    # First L0 and L1
    l0_1, _ = create_level0_checkpoint(
        config, launch,
        snapshot_content="$ git status\nM auth.py\n",
    )
    l1_1, _ = create_level1_checkpoint(
        config, launch,
        level0=l0_1,
    )

    # Second L0 with changes
    l0_2, _ = create_level0_checkpoint(
        config, launch,
        snapshot_content="$ pytest\n15 passed in 2s\n$ git status\nM auth.py\nM test_auth.py\n",
        parent_checkpoint_id=l0_1.checkpoint_id,
    )

    # Meaningful work detected
    assert has_meaningful_work(l0_2, l1_1) is True

    # Second L1 inherits objective from first L1
    l1_2, _ = create_level1_checkpoint(
        config, launch,
        level0=l0_2,
        previous_l1=l1_1,
    )
    assert l1_2.parent_checkpoint_id == l0_2.checkpoint_id

    # Work completed should reflect new work from L0 data
    # The heuristic detects test results and commands from the L0 snapshot
    assert any("15 passed" in item for item in l1_2.work_completed) or any("pytest" in item for item in l1_2.work_completed)


def test_checkpoint_record_in_state_store(tmp_path: Path) -> None:
    """Checkpoints should be recorded in the SQLite state store."""
    config = _config(tmp_path)
    launch = _launch(config)
    store = StateStore(config.project.state_db)

    _, artifact = create_level0_checkpoint(
        config, launch,
        snapshot_content="test\n",
    )

    record_checkpoint(
        store, launch,
        project_key="test",
        level="level0",
        artifact=artifact,
        snapshot_path=config.project.root_dir / "snapshot.txt",
    )

    latest = store.latest_checkpoint("worker")
    assert latest is not None
    assert latest.level == "level0"
    assert latest.project_key == "test"


def test_checkpoint_json_roundtrip(tmp_path: Path) -> None:
    """Checkpoint JSON files should round-trip through serialization."""
    config = _config(tmp_path)
    launch = _launch(config)

    data, artifact = create_level0_checkpoint(
        config, launch,
        snapshot_content="$ echo hello\nhello\n",
    )

    # Read back JSON
    loaded = json.loads(artifact.json_path.read_text())
    restored = CheckpointData.from_dict(loaded)

    assert restored.checkpoint_id == data.checkpoint_id
    assert restored.session_name == "worker"
    assert restored.level == 0
    assert "echo hello" in restored.commands_observed


def test_multiple_sessions_independent(tmp_path: Path) -> None:
    """Checkpoints for different sessions should be independent."""
    config = _config(tmp_path)
    config.sessions["worker2"] = SessionConfig(
        name="worker2",
        role="worker",
        provider=ProviderKind.CLAUDE,
        account="claude_main",
        cwd=config.project.root_dir,
        project="test",
        window_name="worker2-test",
    )
    launch1 = _launch(config)
    launch2 = SessionLaunchSpec(
        session=config.sessions["worker2"],
        account=config.accounts["claude_main"],
        window_name="worker2-test",
        log_path=config.project.root_dir / "logs" / "worker2.log",
        command="claude",
    )

    d1, _ = create_level0_checkpoint(config, launch1, snapshot_content="session1\n")
    d2, _ = create_level0_checkpoint(config, launch2, snapshot_content="session2\n")

    c1 = load_canonical_checkpoint(config, "worker", "test")
    c2 = load_canonical_checkpoint(config, "worker2", "test")

    assert c1.checkpoint_id == d1.checkpoint_id
    assert c2.checkpoint_id == d2.checkpoint_id
    assert c1.checkpoint_id != c2.checkpoint_id
