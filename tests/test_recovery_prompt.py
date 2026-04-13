"""Unit tests for recovery prompt construction."""

from pathlib import Path

from pollypm.checkpoints import CheckpointData
from pollypm.models import ProviderKind
from pollypm.recovery_prompt import (
    RecoveryPrompt,
    RecoveryPromptSection,
    _build_from_checkpoint,
    _render_claude,
    _render_codex,
    _truncate_sections,
    DEFAULT_MAX_CHARS,
)
from pollypm.models import (
    AccountConfig,
    KnownProject,
    PollyPMConfig,
    PollyPMSettings,
    ProjectKind,
    ProjectSettings,
    SessionConfig,
)


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
        sessions={},
        projects={
            "test": KnownProject(
                key="test",
                path=project_root,
                name="TestProject",
                kind=ProjectKind.FOLDER,
            )
        },
    )


# ---------------------------------------------------------------------------
# RecoveryPrompt
# ---------------------------------------------------------------------------


class TestRecoveryPrompt:
    def test_render_claude(self) -> None:
        prompt = RecoveryPrompt(
            sections=[
                RecoveryPromptSection(key="test", heading="Test", content="Hello"),
            ],
            provider=ProviderKind.CLAUDE,
        )
        rendered = prompt.render()
        assert "## Test" in rendered
        assert "Hello" in rendered
        assert "recovery" in rendered.lower()

    def test_render_codex(self) -> None:
        prompt = RecoveryPrompt(
            sections=[
                RecoveryPromptSection(key="test", heading="Test", content="Hello"),
            ],
            provider=ProviderKind.CODEX,
        )
        rendered = prompt.render()
        assert "### Test" in rendered
        assert "RECOVERY CONTEXT" in rendered

    def test_total_chars(self) -> None:
        prompt = RecoveryPrompt(
            sections=[
                RecoveryPromptSection(key="a", heading="A", content="12345"),
                RecoveryPromptSection(key="b", heading="B", content="67890"),
            ],
        )
        assert prompt.total_chars == 10


# ---------------------------------------------------------------------------
# Provider-specific rendering
# ---------------------------------------------------------------------------


class TestRenderClaude:
    def test_uses_h2_headings(self) -> None:
        sections = [RecoveryPromptSection(key="x", heading="My Section", content="content")]
        text = _render_claude(sections)
        assert "## My Section" in text

    def test_includes_preamble(self) -> None:
        text = _render_claude([])
        assert "recovery" in text.lower()


class TestRenderCodex:
    def test_uses_h3_headings(self) -> None:
        sections = [RecoveryPromptSection(key="x", heading="My Section", content="content")]
        text = _render_codex(sections)
        assert "### My Section" in text


# ---------------------------------------------------------------------------
# Build from checkpoint
# ---------------------------------------------------------------------------


class TestBuildFromCheckpoint:
    def test_includes_all_sections(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        checkpoint = CheckpointData(
            checkpoint_id="test-123",
            session_name="worker",
            project="test",
            role="worker",
            objective="Fix the login bug",
            sub_step="Writing unit tests",
            work_completed=["Updated auth module", "Fixed password hashing"],
            recommended_next_step="Run integration tests",
            blockers=["API rate limit"],
            unresolved_questions=["Should we use bcrypt or argon2?"],
        )

        prompt = _build_from_checkpoint(
            config, checkpoint,
            provider=ProviderKind.CLAUDE,
            task_prompt="",
            max_chars=DEFAULT_MAX_CHARS,
        )

        rendered = prompt.render()
        assert "Fix the login bug" in rendered
        assert "Writing unit tests" in rendered
        assert "Updated auth module" in rendered
        assert "Run integration tests" in rendered
        assert "API rate limit" in rendered
        assert "bcrypt or argon2" in rendered

    def test_includes_task_prompt(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        checkpoint = CheckpointData(
            checkpoint_id="test-123",
            session_name="worker",
            project="test",
        )

        prompt = _build_from_checkpoint(
            config, checkpoint,
            provider=ProviderKind.CLAUDE,
            task_prompt="Implement issue #42",
            max_chars=DEFAULT_MAX_CHARS,
        )

        rendered = prompt.render()
        assert "issue #42" in rendered

    def test_loads_project_context(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        docs_dir = config.project.root_dir / "docs"
        docs_dir.mkdir(parents=True)
        (docs_dir / "project-overview.md").write_text("# TestProject\nA test project.\n")

        checkpoint = CheckpointData(
            checkpoint_id="test-123",
            session_name="worker",
            project="test",
        )

        prompt = _build_from_checkpoint(
            config, checkpoint,
            provider=ProviderKind.CLAUDE,
            task_prompt="",
            max_chars=DEFAULT_MAX_CHARS,
        )

        rendered = prompt.render()
        assert "TestProject" in rendered

    def test_omits_empty_sections(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        checkpoint = CheckpointData(
            checkpoint_id="test-123",
            session_name="worker",
            project="test",
        )

        prompt = _build_from_checkpoint(
            config, checkpoint,
            provider=ProviderKind.CLAUDE,
            task_prompt="",
            max_chars=DEFAULT_MAX_CHARS,
        )

        # No checkpoint data → minimal sections
        rendered = prompt.render()
        assert "Blockers" not in rendered
        assert "What Was Completed" not in rendered

    def test_cross_provider_formatting(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        checkpoint = CheckpointData(
            checkpoint_id="test-123",
            session_name="worker",
            project="test",
            objective="Fix bug",
        )

        claude_prompt = _build_from_checkpoint(
            config, checkpoint,
            provider=ProviderKind.CLAUDE,
            task_prompt="",
            max_chars=DEFAULT_MAX_CHARS,
        )
        codex_prompt = _build_from_checkpoint(
            config, checkpoint,
            provider=ProviderKind.CODEX,
            task_prompt="",
            max_chars=DEFAULT_MAX_CHARS,
        )

        claude_text = claude_prompt.render()
        codex_text = codex_prompt.render()

        # Same content, different formatting
        assert "Fix bug" in claude_text
        assert "Fix bug" in codex_text
        assert "##" in claude_text
        assert "###" in codex_text


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


class TestTruncation:
    def test_no_truncation_when_under_limit(self) -> None:
        sections = [
            RecoveryPromptSection(key="a", heading="A", content="short"),
        ]
        result = _truncate_sections(sections, 1000)
        assert len(result) == 1
        assert result[0].content == "short"

    def test_truncates_high_priority_first(self) -> None:
        sections = [
            RecoveryPromptSection(key="important", heading="Important", content="x" * 500, priority=1),
            RecoveryPromptSection(key="expendable", heading="Expendable", content="y" * 500, priority=5),
        ]
        result = _truncate_sections(sections, 600)
        # The expendable section (higher priority number) should be truncated
        important = next(s for s in result if s.key == "important")
        expendable = next(s for s in result if s.key == "expendable")
        assert len(important.content) >= len(expendable.content)

    def test_truncation_adds_marker(self) -> None:
        sections = [
            RecoveryPromptSection(key="big", heading="Big", content="x" * 1000, priority=5),
        ]
        result = _truncate_sections(sections, 500)
        assert "truncated" in result[0].content.lower()
