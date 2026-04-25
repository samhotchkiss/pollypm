"""Unit tests for recovery prompt construction."""

from pathlib import Path

from pollypm.checkpoints import CheckpointData
from pollypm.models import ProviderKind
from pollypm.recovery_prompt import (
    RecoveryPrompt,
    RecoveryPromptSection,
    _build_from_checkpoint,
    _build_fallback_prompt,
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
    def test_fresh_launch_banner(self, tmp_path: Path) -> None:
        config = _config(tmp_path)

        prompt = _build_fallback_prompt(
            config,
            "test",
            provider=ProviderKind.CLAUDE,
            task_prompt="",
            max_chars=DEFAULT_MAX_CHARS,
        )

        rendered = prompt.render()
        assert "RECOVERY MODE: FRESH LAUNCH" in rendered
        assert "no checkpoint to resume from" in rendered

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
        assert "RECOVERY MODE: RESUMING FROM CHECKPOINT test-123" in rendered
        assert "last state was Writing unit tests" in rendered

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


# ---------------------------------------------------------------------------
# Pending-inbox section
# ---------------------------------------------------------------------------


class TestPendingInboxSection:
    def test_skips_non_tracked_projects(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """Cycle 85: only tracked projects' inbox tasks belong in the
        recovery prompt. A registered-but-not-tracked project may have
        a stale ``.pollypm/state.db`` from a prior tracking run; its
        leftover inbox items must not leak into the resume prompt.
        """
        from pollypm.recovery_prompt import _pending_inbox_section
        from types import SimpleNamespace

        # Tracked project's state.db has a task; non-tracked project's
        # state.db ALSO has a task. Only the tracked one should land
        # in the recovery prompt.
        tracked_path = tmp_path / "tracked"
        (tracked_path / ".pollypm").mkdir(parents=True)
        (tracked_path / ".pollypm" / "state.db").write_text("")

        ghost_path = tmp_path / "ghost"
        (ghost_path / ".pollypm").mkdir(parents=True)
        (ghost_path / ".pollypm" / "state.db").write_text("")

        tracked_proj = SimpleNamespace(path=tracked_path, tracked=True)
        ghost_proj = SimpleNamespace(path=ghost_path, tracked=False)
        config = SimpleNamespace(projects={
            "tracked": tracked_proj,
            "ghost": ghost_proj,
        })

        # Fake the work-service inbox query so we don't need real
        # state.db schema. inbox_tasks is called with svc + project.
        fake_task = SimpleNamespace(
            title="Pending task",
            work_status=SimpleNamespace(value="review"),
        )
        called_with: list[str] = []

        def fake_inbox_tasks(svc, *, project):
            called_with.append(project)
            return [fake_task]

        # Lightweight context-manager stand-in for SQLiteWorkService.
        class _FakeSvc:
            def __init__(self, *_a, **_kw) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

        monkeypatch.setattr(
            "pollypm.work.inbox_view.inbox_tasks", fake_inbox_tasks,
        )
        monkeypatch.setattr(
            "pollypm.work.sqlite_service.SQLiteWorkService", _FakeSvc,
        )

        section = _pending_inbox_section(config)
        assert section is not None
        # Only the tracked project's inbox was scanned.
        assert called_with == ["tracked"]
        assert "[tracked] Pending task" in section.content
        assert "[ghost]" not in section.content
