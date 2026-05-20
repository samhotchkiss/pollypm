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
# Live git state (#1974 — checkpoint emitting stale branch → architect
# rejects the recovery prompt as prompt-injection)
# ---------------------------------------------------------------------------


class TestLiveGitStateFromSessionCwd:
    """Architect / worker sessions live in per-session worktrees; the
    git state shown in the recovery prompt must reflect the session's
    actual ``cwd``, not the project root, or the agent rejects the
    checkpoint."""

    def test_session_cwd_branch_overrides_project_root(
        self, tmp_path: Path,
    ) -> None:
        import subprocess

        project_root = tmp_path / "repo"
        project_root.mkdir()
        # Initialise the project repo on ``main``.
        subprocess.run(
            ["git", "init", "-b", "main", str(project_root)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git",
             "-c", "user.email=test@example.com",
             "-c", "user.name=test",
             "-C", str(project_root), "commit",
             "--allow-empty", "-m", "init"],
            check=True, capture_output=True,
        )

        # Session worktree on a different branch (this is what
        # architect-<project> sessions look like in production:
        # ``<project>/.pollypm/worktrees/<session>/`` checked out to
        # ``fix/some-branch``).
        worktree = tmp_path / "worktree"
        subprocess.run(
            ["git", "-C", str(project_root), "worktree", "add",
             "-b", "fix/recovery-checkpoint-live-state",
             str(worktree)],
            check=True, capture_output=True,
        )

        config = PollyPMConfig(
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
                ),
            },
            sessions={
                "architect_test": SessionConfig(
                    name="architect_test",
                    role="architect",
                    provider=ProviderKind.CLAUDE,
                    account="claude_main",
                    cwd=worktree,
                    project="test",
                ),
            },
            projects={
                "test": KnownProject(
                    key="test",
                    path=project_root,
                    name="TestProject",
                    kind=ProjectKind.FOLDER,
                ),
            },
        )

        checkpoint = CheckpointData(
            checkpoint_id="ckpt-1974",
            session_name="architect_test",
            project="test",
            objective="Test live git state",
        )

        prompt = _build_from_checkpoint(
            config, checkpoint,
            provider=ProviderKind.CLAUDE,
            task_prompt="",
            max_chars=DEFAULT_MAX_CHARS,
            session_name="architect_test",
        )

        rendered = prompt.render()
        # The worktree's branch must appear — that is the whole bug.
        assert "Branch: fix/recovery-checkpoint-live-state" in rendered, (
            "Recovery checkpoint must read live git state from the "
            "session's cwd (the worktree), not the project root. See "
            "issue #1974."
        )
        # And it must NOT say the main-repo branch, or the architect
        # treats the mismatch as prompt-injection and refuses.
        assert "Branch: main" not in rendered

    def test_falls_back_to_project_root_without_session_cwd(
        self, tmp_path: Path,
    ) -> None:
        """When no session_name is supplied (or the session has no
        cwd configured), the existing behavior — reading from
        project.path — is preserved."""
        import subprocess

        project_root = tmp_path / "repo"
        project_root.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main", str(project_root)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git",
             "-c", "user.email=test@example.com",
             "-c", "user.name=test",
             "-C", str(project_root), "commit",
             "--allow-empty", "-m", "init"],
            check=True, capture_output=True,
        )

        config = _config(tmp_path)
        # Re-init under the same project root the helper made.
        # Helper above already created ``repo``, so reuse it: rebuild
        # config explicitly to point ``test`` at the git repo we just
        # initialised.
        config = PollyPMConfig(
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
                ),
            },
            sessions={},
            projects={
                "test": KnownProject(
                    key="test",
                    path=project_root,
                    name="TestProject",
                    kind=ProjectKind.FOLDER,
                ),
            },
        )

        checkpoint = CheckpointData(
            checkpoint_id="ckpt-fallback",
            session_name="missing",
            project="test",
            objective="Fallback path",
        )
        prompt = _build_from_checkpoint(
            config, checkpoint,
            provider=ProviderKind.CLAUDE,
            task_prompt="",
            max_chars=DEFAULT_MAX_CHARS,
            # Intentionally omit session_name to exercise the fallback.
        )
        rendered = prompt.render()
        assert "Branch: main" in rendered


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
#
# The sqlite-fanout variant of this test (``TestPendingInboxSection``) was
# removed by Slice K (#1737). It drove the per-project ``state.db``
# walk in ``recovery_prompt._pending_inbox_section``, which under the
# pg backend is dead code — pg pulls inbox tasks via a single grouped
# query. Pg-side tracked-filter coverage lives elsewhere (see the
# ``cockpit_pg_aggregates`` test suite).
# ---------------------------------------------------------------------------
