"""Unit tests for the three-tier checkpoint system (Level 0/1/2)."""

import json
from pathlib import Path

import pytest

from pollypm.checkpoints import (
    CheckpointData,
    create_level0_checkpoint,
    create_level1_checkpoint,
    create_level2_checkpoint,
    has_meaningful_work,
    load_canonical_checkpoint,
    _checkpoint_id,
    _extract_commands,
    _extract_l1_heuristic,
    _extract_l2_heuristic,
    _extract_test_results,
    _render_checkpoint_summary,
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


# ---------------------------------------------------------------------------
# CheckpointData
# ---------------------------------------------------------------------------


class TestCheckpointData:
    def test_to_dict_and_back(self) -> None:
        data = CheckpointData(
            checkpoint_id="test-123",
            session_name="worker",
            project="demo",
            role="worker",
            level=1,
            trigger="turn_end",
            created_at="2026-04-10T00:00:00Z",
            objective="Fix the bug",
            work_completed=["Wrote tests"],
        )
        d = data.to_dict()
        restored = CheckpointData.from_dict(d)
        assert restored.checkpoint_id == "test-123"
        assert restored.level == 1
        assert restored.objective == "Fix the bug"
        assert restored.work_completed == ["Wrote tests"]

    def test_from_dict_defaults(self) -> None:
        data = CheckpointData.from_dict({})
        assert data.level == 0
        assert data.is_canonical is True
        assert data.files_changed == []

    def test_to_dict_includes_all_fields(self) -> None:
        data = CheckpointData(checkpoint_id="x", session_name="s")
        d = data.to_dict()
        assert "checkpoint_id" in d
        assert "progress_pct" in d
        assert "risk_factors" in d


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestCheckpointId:
    def test_unique(self) -> None:
        id1 = _checkpoint_id()
        id2 = _checkpoint_id()
        assert id1 != id2

    def test_format(self) -> None:
        cid = _checkpoint_id()
        # Format: YYYYMMDDTHHMMSSZ-XXXXXXXX
        assert "T" in cid
        assert "-" in cid


class TestExtractCommands:
    def test_extracts_dollar_prefix(self) -> None:
        lines = ["$ git status", "On branch main", "$ pytest -q"]
        assert _extract_commands(lines) == ["git status", "pytest -q"]

    def test_extracts_percent_prefix(self) -> None:
        lines = ["% ls -la", "total 0"]
        assert _extract_commands(lines) == ["ls -la"]

    def test_deduplicates(self) -> None:
        lines = ["$ git status", "$ git status"]
        assert _extract_commands(lines) == ["git status"]

    def test_empty(self) -> None:
        assert _extract_commands([]) == []

    def test_limits_to_10(self) -> None:
        lines = [f"$ cmd{i}" for i in range(20)]
        assert len(_extract_commands(lines)) == 10


class TestExtractTestResults:
    def test_pytest_format(self) -> None:
        lines = ["running tests...", "193 passed in 14.26s"]
        assert _extract_test_results(lines) == {"passed": 193}

    def test_pytest_with_failures(self) -> None:
        lines = ["10 passed, 3 failed in 2.5s"]
        assert _extract_test_results(lines) == {"passed": 10, "failed": 3}

    def test_no_test_output(self) -> None:
        lines = ["hello world"]
        assert _extract_test_results(lines) == {}


# ---------------------------------------------------------------------------
# Meaningful work detection
# ---------------------------------------------------------------------------


class TestHasMeaningfulWork:
    def test_first_checkpoint_with_changes(self) -> None:
        l0 = CheckpointData(files_changed=["main.py"])
        assert has_meaningful_work(l0, None) is True

    def test_first_checkpoint_empty(self) -> None:
        l0 = CheckpointData()
        assert has_meaningful_work(l0, None) is False

    def test_files_changed(self) -> None:
        l0 = CheckpointData(files_changed=["main.py"])
        prev = CheckpointData(files_changed=[])
        assert has_meaningful_work(l0, prev) is True

    def test_no_changes(self) -> None:
        l0 = CheckpointData(
            files_changed=["main.py"],
            git_status="M main.py",
            snapshot_hash="abc",
            test_results={"passed": 10},
        )
        prev = CheckpointData(
            files_changed=["main.py"],
            git_status="M main.py",
            snapshot_hash="abc",
            test_results={"passed": 10},
        )
        assert has_meaningful_work(l0, prev) is False

    def test_test_results_changed(self) -> None:
        l0 = CheckpointData(test_results={"passed": 15})
        prev = CheckpointData(test_results={"passed": 10})
        assert has_meaningful_work(l0, prev) is True

    def test_snapshot_hash_changed(self) -> None:
        l0 = CheckpointData(snapshot_hash="new")
        prev = CheckpointData(snapshot_hash="old")
        assert has_meaningful_work(l0, prev) is True


# ---------------------------------------------------------------------------
# Level 1 heuristic extraction
# ---------------------------------------------------------------------------


class TestL1Heuristic:
    def test_extracts_file_changes(self) -> None:
        l0 = CheckpointData(files_changed=["main.py", "test_main.py"])
        result = _extract_l1_heuristic(l0, "")
        assert any("2 file" in item for item in result["work_completed"])

    def test_extracts_test_results(self) -> None:
        l0 = CheckpointData(test_results={"passed": 10, "failed": 2})
        result = _extract_l1_heuristic(l0, "")
        assert any("10 passed" in item for item in result["work_completed"])

    def test_extracts_commands(self) -> None:
        l0 = CheckpointData(commands_observed=["pytest -q", "git add ."])
        result = _extract_l1_heuristic(l0, "")
        assert any("pytest" in item for item in result["work_completed"])

    def test_infers_objective_from_transcript(self) -> None:
        l0 = CheckpointData(transcript_tail=["Implement the new parser module"])
        result = _extract_l1_heuristic(l0, "")
        assert "parser" in result["objective"].lower()

    def test_empty_data(self) -> None:
        l0 = CheckpointData()
        result = _extract_l1_heuristic(l0, "")
        assert result["objective"] == ""
        assert result["work_completed"] == []


# ---------------------------------------------------------------------------
# Level 2 heuristic extraction
# ---------------------------------------------------------------------------


class TestL2Heuristic:
    def test_carries_blockers_as_risks(self) -> None:
        l1 = CheckpointData(blockers=["API rate limit"])
        result = _extract_l2_heuristic(l1)
        assert any("rate limit" in r for r in result["risk_factors"])

    def test_detects_failing_tests(self) -> None:
        l1 = CheckpointData(test_results={"passed": 10, "failed": 3})
        result = _extract_l2_heuristic(l1)
        assert any("3" in r for r in result["risk_factors"])

    def test_empty_data(self) -> None:
        l1 = CheckpointData()
        result = _extract_l2_heuristic(l1)
        assert result["risk_factors"] == []
        assert result["progress_pct"] == 0


# ---------------------------------------------------------------------------
# Render checkpoint summary
# ---------------------------------------------------------------------------


class TestRenderCheckpointSummary:
    def test_level0_basic(self) -> None:
        data = CheckpointData(
            session_name="worker",
            project="test",
            role="worker",
            level=0,
            trigger="heartbeat",
            provider="claude",
            account="claude_main",
            git_branch="main",
            transcript_tail=["$ pytest -q", "10 passed in 1s"],
        )
        summary = _render_checkpoint_summary(data)
        assert "Level 0" in summary
        assert "worker" in summary
        assert "heartbeat" in summary
        assert "```text" in summary

    def test_level1_includes_objective(self) -> None:
        data = CheckpointData(
            session_name="worker",
            project="test",
            role="worker",
            level=1,
            trigger="turn_end",
            provider="claude",
            account="claude_main",
            objective="Fix the login bug",
            work_completed=["Updated auth module"],
            recommended_next_step="Run integration tests",
        )
        summary = _render_checkpoint_summary(data)
        assert "Level 1" in summary
        assert "Fix the login bug" in summary
        assert "Updated auth module" in summary
        assert "Run integration tests" in summary

    def test_level2_includes_progress(self) -> None:
        data = CheckpointData(
            session_name="worker",
            project="test",
            role="worker",
            level=2,
            trigger="pm_request",
            provider="claude",
            account="claude_main",
            progress_pct=75,
            approach_assessment="On track",
            risk_factors=["Deadline pressure"],
        )
        summary = _render_checkpoint_summary(data)
        assert "Level 2" in summary
        assert "75%" in summary
        assert "On track" in summary
        assert "Deadline pressure" in summary


# ---------------------------------------------------------------------------
# Level 0 checkpoint creation
# ---------------------------------------------------------------------------


class TestCreateLevel0:
    def test_creates_files(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        launch = _launch(config)
        data, artifact = create_level0_checkpoint(
            config, launch,
            snapshot_content="$ pytest -q\n10 passed in 1s\n",
        )
        assert artifact.json_path.exists()
        assert artifact.summary_path.exists()
        assert data.level == 0
        assert data.trigger == "heartbeat"
        assert data.is_canonical is True

    def test_extracts_commands(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        launch = _launch(config)
        data, _ = create_level0_checkpoint(
            config, launch,
            snapshot_content="$ git status\n$ pytest -q\n10 passed\n",
        )
        assert "git status" in data.commands_observed
        assert "pytest -q" in data.commands_observed

    def test_extracts_test_results(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        launch = _launch(config)
        data, _ = create_level0_checkpoint(
            config, launch,
            snapshot_content="running tests...\n193 passed in 14.26s\n",
        )
        assert data.test_results.get("passed") == 193

    def test_writes_latest_json(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        launch = _launch(config)
        data, artifact = create_level0_checkpoint(
            config, launch,
            snapshot_content="hello\n",
        )
        latest = artifact.json_path.parent / "latest.json"
        assert latest.exists()

    def test_parent_checkpoint_id(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        launch = _launch(config)
        data, _ = create_level0_checkpoint(
            config, launch,
            snapshot_content="hello\n",
            parent_checkpoint_id="prev-123",
        )
        assert data.parent_checkpoint_id == "prev-123"


# ---------------------------------------------------------------------------
# Level 1 checkpoint creation
# ---------------------------------------------------------------------------


class TestCreateLevel1:
    def test_creates_files(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        launch = _launch(config)
        l0 = CheckpointData(
            checkpoint_id="l0-test",
            session_name="worker",
            project="test",
            role="worker",
            provider="claude",
            account="claude_main",
            files_changed=["main.py"],
            git_branch="main",
        )
        data, artifact = create_level1_checkpoint(
            config, launch,
            level0=l0,
        )
        assert artifact.json_path.exists()
        assert data.level == 1
        assert data.parent_checkpoint_id == "l0-test"

    def test_copies_level0_fields(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        launch = _launch(config)
        l0 = CheckpointData(
            checkpoint_id="l0-test",
            session_name="worker",
            project="test",
            role="worker",
            provider="claude",
            account="claude_main",
            git_branch="feature",
            files_changed=["a.py", "b.py"],
            test_results={"passed": 5},
        )
        data, _ = create_level1_checkpoint(config, launch, level0=l0)
        assert data.git_branch == "feature"
        assert data.files_changed == ["a.py", "b.py"]
        assert data.test_results == {"passed": 5}

    def test_delta_copies_objective_from_previous(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        launch = _launch(config)
        l0 = CheckpointData(checkpoint_id="l0", session_name="worker", project="test", role="worker", provider="claude", account="claude_main")
        prev_l1 = CheckpointData(
            checkpoint_id="prev-l1",
            objective="Fix the parser",
        )
        data, _ = create_level1_checkpoint(
            config, launch,
            level0=l0,
            previous_l1=prev_l1,
        )
        assert data.objective == "Fix the parser"

    def test_delta_carries_blockers(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        launch = _launch(config)
        l0 = CheckpointData(checkpoint_id="l0", session_name="worker", project="test", role="worker", provider="claude", account="claude_main")
        prev_l1 = CheckpointData(
            checkpoint_id="prev-l1",
            blockers=["API rate limit"],
        )
        data, _ = create_level1_checkpoint(
            config, launch,
            level0=l0,
            previous_l1=prev_l1,
        )
        assert data.blockers == ["API rate limit"]


# ---------------------------------------------------------------------------
# Level 2 checkpoint creation
# ---------------------------------------------------------------------------


class TestCreateLevel2:
    def test_creates_files(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        launch = _launch(config)
        l1 = CheckpointData(
            checkpoint_id="l1-test",
            session_name="worker",
            project="test",
            role="worker",
            level=1,
            provider="claude",
            account="claude_main",
            objective="Fix the bug",
            blockers=["Flaky test"],
        )
        data, artifact = create_level2_checkpoint(
            config, launch,
            level1=l1,
        )
        assert artifact.json_path.exists()
        assert data.level == 2
        assert data.parent_checkpoint_id == "l1-test"
        assert data.objective == "Fix the bug"

    def test_inherits_l1_fields(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        launch = _launch(config)
        l1 = CheckpointData(
            checkpoint_id="l1",
            session_name="worker",
            project="test",
            role="worker",
            provider="claude",
            account="claude_main",
            objective="Ship feature",
            work_completed=["Wrote code"],
            blockers=["Test failures"],
            test_results={"passed": 10, "failed": 2},
        )
        data, _ = create_level2_checkpoint(config, launch, level1=l1)
        assert data.objective == "Ship feature"
        assert data.work_completed == ["Wrote code"]
        # Heuristic should carry blocker as risk
        assert any("Test failures" in r for r in data.risk_factors) or any("2" in r for r in data.risk_factors)


# ---------------------------------------------------------------------------
# Load canonical checkpoint
# ---------------------------------------------------------------------------


class TestLoadCanonical:
    def test_loads_after_write(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        launch = _launch(config)
        data, _ = create_level0_checkpoint(
            config, launch,
            snapshot_content="test content\n",
        )
        loaded = load_canonical_checkpoint(config, "worker", "test")
        assert loaded is not None
        assert loaded.checkpoint_id == data.checkpoint_id
        assert loaded.level == 0

    def test_returns_none_if_missing(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        loaded = load_canonical_checkpoint(config, "nonexistent", "test")
        assert loaded is None

    def test_latest_updated_on_new_checkpoint(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        launch = _launch(config)
        data1, _ = create_level0_checkpoint(
            config, launch, snapshot_content="first\n",
        )
        data2, _ = create_level0_checkpoint(
            config, launch, snapshot_content="second\n",
            parent_checkpoint_id=data1.checkpoint_id,
        )
        loaded = load_canonical_checkpoint(config, "worker", "test")
        assert loaded is not None
        assert loaded.checkpoint_id == data2.checkpoint_id
