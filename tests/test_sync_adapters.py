"""Tests for sync adapters, sync manager, and migration tool."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pollypm.work.migrate import (
    MigrationResult,
    migrate_issues,
    _parse_filename,
    _parse_content,
)
from pollypm.work.models import Task, TaskType, WorkStatus, Priority
from pollypm.work.sqlite_service import SQLiteWorkService
from pollypm.work.sync import SyncManager
from pollypm.work.sync_file import FileSyncAdapter, STATUS_TO_FOLDER, _slugify
from pollypm.work.sync_github import GitHubSyncAdapter, STATUS_TO_LABEL


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def svc(tmp_path):
    """Fresh SQLiteWorkService."""
    db_path = tmp_path / "work.db"
    return SQLiteWorkService(db_path=db_path)


@pytest.fixture
def issues_root(tmp_path):
    """Temporary issues directory."""
    root = tmp_path / "issues"
    root.mkdir()
    return root


def _make_task(svc, title="Test task", description="A description", **kwargs):
    """Helper to create a standard task."""
    defaults = dict(
        title=title,
        description=description,
        type="task",
        project="proj",
        flow_template="standard",
        roles={"worker": "agent-1", "reviewer": "agent-2"},
        priority="normal",
        created_by="tester",
    )
    defaults.update(kwargs)
    return svc.create(**defaults)


# ---------------------------------------------------------------------------
# File Adapter Tests
# ---------------------------------------------------------------------------


class TestFileSyncAdapter:
    def test_creates_issue_file(self, svc, issues_root):
        """on_create writes a markdown file in the correct state directory."""
        task = _make_task(svc)
        adapter = FileSyncAdapter(issues_root=issues_root)

        adapter.on_create(task)

        expected_dir = issues_root / "00-not-ready"
        assert expected_dir.is_dir()
        files = list(expected_dir.glob("*.md"))
        assert len(files) == 1
        content = files[0].read_text()
        assert "# Test task" in content
        assert "A description" in content

    def test_moves_on_transition(self, svc, issues_root):
        """Transition from draft to queued moves the file."""
        task = _make_task(svc)
        adapter = FileSyncAdapter(issues_root=issues_root)

        adapter.on_create(task)

        # Simulate transition
        task = svc.queue(task.task_id, "tester")
        adapter.on_transition(task, "draft", "queued")

        old_dir = issues_root / "00-not-ready"
        new_dir = issues_root / "01-ready"
        assert not list(old_dir.glob("*.md"))
        assert len(list(new_dir.glob("*.md"))) == 1

    def test_updates_content(self, svc, issues_root):
        """on_update rewrites the markdown content."""
        task = _make_task(svc)
        adapter = FileSyncAdapter(issues_root=issues_root)
        adapter.on_create(task)

        # Update description
        task = svc.update(task.task_id, description="Updated description")
        adapter.on_update(task, ["description"])

        files = list((issues_root / "00-not-ready").glob("*.md"))
        assert len(files) == 1
        content = files[0].read_text()
        assert "Updated description" in content

    def test_status_mapping_complete(self):
        """Every WorkStatus value maps to a folder."""
        for status in WorkStatus:
            assert status.value in STATUS_TO_FOLDER, f"Missing mapping for {status}"

    def test_all_status_folder_mappings(self, issues_root):
        """Verify each work_status maps to the correct folder."""
        expected = {
            "draft": "00-not-ready",
            "queued": "01-ready",
            "in_progress": "02-in-progress",
            "review": "03-needs-review",
            "done": "05-completed",
            "cancelled": "05-completed",
            "blocked": "02-in-progress",
            "on_hold": "00-not-ready",
        }
        for status, folder in expected.items():
            assert STATUS_TO_FOLDER[status] == folder

    def test_cancelled_gets_prefix(self, svc, issues_root):
        """Cancelled tasks get [CANCELLED] prefix in the title."""
        task = _make_task(svc)
        adapter = FileSyncAdapter(issues_root=issues_root)

        # Cancel the task
        task = svc.cancel(task.task_id, "tester", "no longer needed")
        adapter.on_create(task)

        files = list((issues_root / "05-completed").glob("*.md"))
        assert len(files) == 1
        content = files[0].read_text()
        assert "[CANCELLED]" in content

    def test_on_transition_creates_if_missing(self, svc, issues_root):
        """on_transition creates the file if it doesn't exist yet."""
        task = _make_task(svc)
        adapter = FileSyncAdapter(issues_root=issues_root)

        # Transition without creating first
        task = svc.queue(task.task_id, "tester")
        adapter.on_transition(task, "draft", "queued")

        files = list((issues_root / "01-ready").glob("*.md"))
        assert len(files) == 1

    def test_on_update_creates_if_missing(self, svc, issues_root):
        """on_update creates the file if it doesn't exist yet."""
        task = _make_task(svc)
        adapter = FileSyncAdapter(issues_root=issues_root)

        adapter.on_update(task, ["description"])

        files = list((issues_root / "00-not-ready").glob("*.md"))
        assert len(files) == 1


class TestFileSyncAutoCommit:
    """#1022: every issues/ side-effect must auto-commit to the project repo
    so the working tree never accumulates uncommitted agent state."""

    @staticmethod
    def _git(args, cwd):
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=False,
            capture_output=True,
            text=True,
        )

    @pytest.fixture
    def project_repo(self, tmp_path):
        """Real git repo with a single seed commit."""
        repo = tmp_path / "proj"
        repo.mkdir()
        self._git(["init", "-q", "-b", "main"], cwd=repo)
        self._git(["config", "user.email", "seed@local"], cwd=repo)
        self._git(["config", "user.name", "Seed"], cwd=repo)
        (repo / "README.md").write_text("# project\n")
        self._git(["add", "README.md"], cwd=repo)
        self._git(["commit", "-q", "-m", "seed"], cwd=repo)
        return repo

    def _porcelain(self, repo):
        return self._git(["status", "--porcelain"], cwd=repo).stdout

    def _log_subjects(self, repo):
        out = self._git(["log", "--format=%s"], cwd=repo).stdout
        return [line for line in out.splitlines() if line]

    def test_on_create_commits_issues(self, svc, project_repo):
        adapter = FileSyncAdapter(issues_root=project_repo / "issues")
        task = _make_task(svc, title="Auto commit task")
        adapter.on_create(task)

        # Working tree must be clean — issues/ was committed.
        assert self._porcelain(project_repo) == ""
        subjects = self._log_subjects(project_repo)
        assert any("chore(issues): add" in s for s in subjects), subjects

    def test_on_transition_commits_move(self, svc, project_repo):
        adapter = FileSyncAdapter(issues_root=project_repo / "issues")
        task = _make_task(svc)
        adapter.on_create(task)

        task = svc.queue(task.task_id, "tester")
        adapter.on_transition(task, "draft", "queued")

        assert self._porcelain(project_repo) == ""
        subjects = self._log_subjects(project_repo)
        assert any(
            "draft -> queued" in s for s in subjects
        ), subjects

    def test_on_update_commits_changes(self, svc, project_repo):
        adapter = FileSyncAdapter(issues_root=project_repo / "issues")
        task = _make_task(svc)
        adapter.on_create(task)

        task = svc.update(task.task_id, description="Different description")
        adapter.on_update(task, ["description"])

        assert self._porcelain(project_repo) == ""
        subjects = self._log_subjects(project_repo)
        assert any("update " in s for s in subjects), subjects

    def test_no_op_when_not_a_git_repo(self, svc, tmp_path):
        """Bare directory (no .git) must not raise — tests rely on this."""
        plain = tmp_path / "plain"
        plain.mkdir()
        adapter = FileSyncAdapter(issues_root=plain / "issues")
        task = _make_task(svc)
        # Should not raise.
        adapter.on_create(task)
        adapter.on_transition(task, "draft", "queued")
        adapter.on_update(task, ["description"])

    def test_skips_when_merge_in_progress(self, svc, project_repo):
        """A live MERGE_HEAD means committing would clobber the merge."""
        adapter = FileSyncAdapter(issues_root=project_repo / "issues")
        task = _make_task(svc)

        # Simulate a live merge.
        merge_head = project_repo / ".git" / "MERGE_HEAD"
        merge_head.write_text("deadbeef\n")
        try:
            adapter.on_create(task)
        finally:
            merge_head.unlink()

        # File written but NOT committed (merge in progress).
        assert (project_repo / "issues").exists()
        subjects = self._log_subjects(project_repo)
        # Only the seed commit; no chore(issues) line.
        assert not any("chore(issues)" in s for s in subjects), subjects

    def test_uses_pollypm_identity(self, svc, project_repo):
        """Commit should be attributed to PollyPM, not the seed user."""
        # Drop the local user.email/user.name overrides to ensure the
        # inline -c flags are what's actually being honored.
        self._git(["config", "--unset", "user.email"], cwd=project_repo)
        self._git(["config", "--unset", "user.name"], cwd=project_repo)

        adapter = FileSyncAdapter(issues_root=project_repo / "issues")
        task = _make_task(svc)
        adapter.on_create(task)

        # Repo is clean and the latest commit author is pollypm.
        assert self._porcelain(project_repo) == ""
        author = self._git(
            ["log", "-1", "--format=%an <%ae>"], cwd=project_repo
        ).stdout.strip()
        assert "pollypm@local" in author

    def test_on_transition_commits_under_busy_db_simulation(self, svc, project_repo, caplog):
        """#1046 — lifecycle moves must commit even when the work-service
        DB is contended. ``_autocommit_issues`` does not touch SQLite, so
        a simulated db-locked condition on adjacent code paths must not
        prevent the issues/ commit from landing. This is the "fix
        forward" half of #1046's failure mode A: prove the auto-commit
        path itself is robust regardless of upstream lock contention."""
        adapter = FileSyncAdapter(issues_root=project_repo / "issues")
        task = _make_task(svc)
        adapter.on_create(task)

        task = svc.queue(task.task_id, "tester")
        # Patch the SQLite work service connection to *raise* on every
        # call — simulates the worst-case #1030 contention. The adapter
        # must still commit the move because it doesn't touch the DB.
        from unittest.mock import patch

        with patch.object(
            svc, "get", side_effect=Exception("database is locked")
        ):
            with caplog.at_level(logging.WARNING):
                adapter.on_transition(task, "draft", "queued")

        # Move was captured by git despite the db-lock simulation.
        assert self._porcelain(project_repo) == ""
        subjects = self._log_subjects(project_repo)
        assert any("draft -> queued" in s for s in subjects), subjects

    def test_failure_logged_at_warning(self, svc, project_repo, caplog):
        """#1046 — a swallowed auto-commit failure must surface at
        WARNING (not DEBUG) so the silent-regression in #1046's failure
        mode A can never repeat without operator visibility."""
        adapter = FileSyncAdapter(issues_root=project_repo / "issues")
        task = _make_task(svc)
        adapter.on_create(task)

        # Force the next git invocation to fail by writing junk into
        # ``.git/index.lock`` so the next ``git add`` errors out.
        lock = project_repo / ".git" / "index.lock"
        lock.write_text("locked\n")

        try:
            with caplog.at_level(logging.WARNING):
                task2 = _make_task(svc, title="Second task")
                adapter.on_create(task2)
        finally:
            if lock.exists():
                lock.unlink()

        # Failure must surface at WARNING, not buried at DEBUG.
        warning_records = [
            r for r in caplog.records
            if r.levelno >= logging.WARNING and "issues/ auto-commit" in r.getMessage()
        ]
        assert warning_records, (
            "Expected a WARNING-level log for the failed auto-commit; "
            f"got: {[(r.levelname, r.getMessage()) for r in caplog.records]}"
        )

    def test_returns_true_on_commit_false_on_noop(self, svc, project_repo):
        """#1046 — return value lets callers like scaffold_issue_tracker
        and future loud-alert plumbing distinguish "commit landed" from
        "nothing to do"."""
        from pollypm.work.sync_file import _autocommit_issues

        # No issues/ tree yet -> returns False.
        assert _autocommit_issues(project_repo, "noop") is False

        # Scaffold the tree and check that the commit is reported.
        (project_repo / "issues" / "00-not-ready").mkdir(parents=True)
        (project_repo / "issues" / "notes.md").write_text("# notes\n")
        assert _autocommit_issues(project_repo, "first") is True

        # Re-running with no churn returns False.
        assert _autocommit_issues(project_repo, "noop again") is False


class TestScaffoldIssueTrackerAutoCommit:
    """#1046 failure mode B: scaffolding ``issues/`` for the first time
    must land as the first auto-commit, even if the project never sees a
    lifecycle transition. Without this, projects sit with a fully-
    untracked ``issues/`` tree forever (camptown, coin-flip, pomodoro,
    russell, smoketest in the original repro)."""

    @staticmethod
    def _git(args, cwd):
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=False,
            capture_output=True,
            text=True,
        )

    @pytest.fixture
    def project_repo(self, tmp_path):
        repo = tmp_path / "proj"
        repo.mkdir()
        self._git(["init", "-q", "-b", "main"], cwd=repo)
        self._git(["config", "user.email", "seed@local"], cwd=repo)
        self._git(["config", "user.name", "Seed"], cwd=repo)
        (repo / "README.md").write_text("# project\n")
        self._git(["add", "README.md"], cwd=repo)
        self._git(["commit", "-q", "-m", "seed"], cwd=repo)
        return repo

    def _porcelain(self, repo):
        return self._git(["status", "--porcelain"], cwd=repo).stdout

    def _log_subjects(self, repo):
        out = self._git(["log", "--format=%s"], cwd=repo).stdout
        return [line for line in out.splitlines() if line]

    def test_initial_scaffold_is_committed(self, project_repo):
        """#1046 — first scaffold of issues/ produces a commit even
        when no tasks are ever created (failure mode B)."""
        from pollypm.projects import scaffold_issue_tracker

        scaffold_issue_tracker(project_repo)

        # No issues/ files left dangling — the scaffold landed in a commit.
        porcelain = self._porcelain(project_repo)
        assert "issues/" not in porcelain, (
            f"expected issues/ committed, got dirty status: {porcelain!r}"
        )
        subjects = self._log_subjects(project_repo)
        assert any(
            "chore(issues): scaffold" in s for s in subjects
        ), subjects

    def test_scaffold_does_not_gitignore_issues(self, project_repo):
        """#1046 — ``issues/`` must NOT be in ``.gitignore`` after
        scaffolding. The legacy ``_ensure_gitignore_entry`` call
        contradicted #1022 and silently disabled every subsequent
        auto-commit because ``git add -- issues`` no-ops on ignored
        paths."""
        from pollypm.projects import scaffold_issue_tracker

        scaffold_issue_tracker(project_repo)

        gitignore = project_repo / ".gitignore"
        if gitignore.exists():
            text = gitignore.read_text()
            assert "issues/" not in text.splitlines(), (
                f"issues/ must not be gitignored after scaffold; got:\n{text}"
            )

    def test_scaffold_idempotent_no_redundant_commit(self, project_repo):
        """Calling scaffold twice must not create a second empty commit."""
        from pollypm.projects import scaffold_issue_tracker

        scaffold_issue_tracker(project_repo)
        first_subjects = self._log_subjects(project_repo)

        scaffold_issue_tracker(project_repo)
        second_subjects = self._log_subjects(project_repo)

        # Same number of commits — second call was a clean no-op.
        assert first_subjects == second_subjects, (first_subjects, second_subjects)


# ---------------------------------------------------------------------------
# GitHub Adapter Tests
# ---------------------------------------------------------------------------


class TestGitHubSyncAdapter:
    def test_creates_issue(self):
        """on_create calls gh issue create with correct args."""
        adapter = GitHubSyncAdapter(repo="owner/repo")
        task = Task(
            project="proj",
            task_number=1,
            title="Fix the bug",
            type=TaskType.TASK,
            work_status=WorkStatus.DRAFT,
            description="Something is broken",
        )

        with patch("pollypm.work.sync_github._run_gh") as mock_gh:
            mock_gh.return_value = MagicMock(
                stdout="https://github.com/owner/repo/issues/42\n"
            )
            adapter.on_create(task)

            mock_gh.assert_called_once()
            args = mock_gh.call_args[0][0]
            assert "issue" in args
            assert "create" in args
            assert "--title" in args
            assert "Fix the bug" in args
            assert "--label" in args
            assert "polly:not-ready" in args

        # Should have stored the issue number
        assert task.external_refs.get("github_issue") == "42"

    def test_work_service_persists_created_issue_ref(self, tmp_path: Path):
        """GitHub sync refs survive the create transaction round-trip."""
        manager = SyncManager()
        manager.register(GitHubSyncAdapter(repo="owner/repo"))
        svc = SQLiteWorkService(db_path=tmp_path / "work.db", sync_manager=manager)

        with patch("pollypm.work.sync_github._run_gh") as mock_gh:
            mock_gh.return_value = MagicMock(
                stdout="https://github.com/owner/repo/issues/42\n"
            )
            task = _make_task(svc)

        reloaded = svc.get(task.task_id)
        assert reloaded.external_refs.get("github_issue") == "42"

    def test_transitions_labels(self):
        """on_transition swaps labels via gh issue edit."""
        adapter = GitHubSyncAdapter(repo="owner/repo")
        task = Task(
            project="proj",
            task_number=1,
            title="Fix the bug",
            type=TaskType.TASK,
            work_status=WorkStatus.IN_PROGRESS,
            external_refs={"github_issue": "42"},
        )

        with patch("pollypm.work.sync_github._run_gh") as mock_gh:
            adapter.on_transition(task, "queued", "in_progress")

            mock_gh.assert_called_once()
            args = mock_gh.call_args[0][0]
            assert "issue" in args
            assert "edit" in args
            assert "42" in args
            assert "--remove-label" in args
            assert "polly:ready" in args
            assert "--add-label" in args
            assert "polly:in-progress" in args

    def test_failure_doesnt_raise(self, caplog):
        """gh failure is logged but doesn't raise an exception."""
        adapter = GitHubSyncAdapter(repo="owner/repo")
        task = Task(
            project="proj",
            task_number=1,
            title="Fix the bug",
            type=TaskType.TASK,
            work_status=WorkStatus.DRAFT,
            description="Broken",
        )

        with patch("pollypm.work.sync_github._run_gh") as mock_gh:
            mock_gh.side_effect = FileNotFoundError("gh not found")

            # Should not raise
            adapter.on_create(task)

    def test_transition_without_issue_number(self, caplog):
        """Transition with no github_issue logs a warning."""
        adapter = GitHubSyncAdapter(repo="owner/repo")
        task = Task(
            project="proj",
            task_number=1,
            title="Fix the bug",
            type=TaskType.TASK,
            work_status=WorkStatus.IN_PROGRESS,
        )

        with patch("pollypm.work.sync_github._run_gh") as mock_gh:
            adapter.on_transition(task, "draft", "in_progress")
            mock_gh.assert_not_called()

    def test_update_title_and_body(self):
        """on_update calls gh issue edit with title and body."""
        adapter = GitHubSyncAdapter(repo="owner/repo")
        task = Task(
            project="proj",
            task_number=1,
            title="Updated title",
            type=TaskType.TASK,
            work_status=WorkStatus.IN_PROGRESS,
            description="Updated body",
            external_refs={"github_issue": "42"},
        )

        with patch("pollypm.work.sync_github._run_gh") as mock_gh:
            adapter.on_update(task, ["title", "description"])

            mock_gh.assert_called_once()
            args = mock_gh.call_args[0][0]
            assert "--title" in args
            assert "Updated title" in args
            assert "--body" in args

    def test_status_mapping_complete(self):
        """Every WorkStatus value has a label mapping."""
        for status in WorkStatus:
            assert status.value in STATUS_TO_LABEL, f"Missing label for {status}"

    def test_blocked_and_on_hold_reuse_tracker_labels(self):
        """GitHub sync stays on the six-label tracker contract."""
        assert STATUS_TO_LABEL[WorkStatus.BLOCKED.value] == "polly:in-progress"
        assert STATUS_TO_LABEL[WorkStatus.ON_HOLD.value] == "polly:not-ready"


# ---------------------------------------------------------------------------
# SyncManager Tests
# ---------------------------------------------------------------------------


class TestSyncManager:
    def test_dispatches_to_all(self):
        """Register 2 adapters; both receive on_create."""
        manager = SyncManager()
        adapter1 = MagicMock()
        adapter1.name = "a1"
        adapter2 = MagicMock()
        adapter2.name = "a2"

        manager.register(adapter1)
        manager.register(adapter2)

        task = Task(
            project="proj",
            task_number=1,
            title="Test",
            type=TaskType.TASK,
        )
        manager.on_create(task)

        adapter1.on_create.assert_called_once_with(task)
        adapter2.on_create.assert_called_once_with(task)

    def test_isolates_failures(self):
        """One adapter raising doesn't prevent the other from running."""
        manager = SyncManager()

        failing = MagicMock()
        failing.name = "failing"
        failing.on_create.side_effect = RuntimeError("boom")

        succeeding = MagicMock()
        succeeding.name = "succeeding"

        manager.register(failing)
        manager.register(succeeding)

        task = Task(
            project="proj",
            task_number=1,
            title="Test",
            type=TaskType.TASK,
        )
        manager.on_create(task)

        failing.on_create.assert_called_once()
        succeeding.on_create.assert_called_once()

    def test_dispatches_transition(self):
        """on_transition is dispatched to all adapters."""
        manager = SyncManager()
        adapter = MagicMock()
        adapter.name = "test"
        manager.register(adapter)

        task = Task(
            project="proj",
            task_number=1,
            title="Test",
            type=TaskType.TASK,
        )
        manager.on_transition(task, "draft", "queued")
        adapter.on_transition.assert_called_once_with(task, "draft", "queued")

    def test_dispatches_update(self):
        """on_update is dispatched to all adapters."""
        manager = SyncManager()
        adapter = MagicMock()
        adapter.name = "test"
        manager.register(adapter)

        task = Task(
            project="proj",
            task_number=1,
            title="Test",
            type=TaskType.TASK,
        )
        manager.on_update(task, ["title"])
        adapter.on_update.assert_called_once_with(task, ["title"])


# ---------------------------------------------------------------------------
# Migration Tests
# ---------------------------------------------------------------------------


class TestMigration:
    def _setup_issue_file(self, issues_dir: Path, folder: str, filename: str, content: str):
        """Helper to create an issue file in the issues directory."""
        dirpath = issues_dir / folder
        dirpath.mkdir(parents=True, exist_ok=True)
        filepath = dirpath / filename
        filepath.write_text(content, encoding="utf-8")
        return filepath

    def test_creates_tasks(self, svc, tmp_path):
        """Migration creates tasks from issue files in various state dirs."""
        issues_dir = tmp_path / "issues"
        self._setup_issue_file(
            issues_dir, "00-not-ready", "0001-setup-project.md",
            "# Setup Project\n\nInitial setup tasks."
        )
        self._setup_issue_file(
            issues_dir, "01-ready", "0002-add-auth.md",
            "# Add Authentication\n\nImplement login flow."
        )
        self._setup_issue_file(
            issues_dir, "02-in-progress", "0003-fix-bug.md",
            "# Fix Bug\n\nResolve the crash."
        )

        result = migrate_issues(issues_dir, svc, project="proj")

        assert result.created == 3
        assert result.skipped == 0
        assert result.errors == []

        tasks = svc.list_tasks(project="proj")
        assert len(tasks) == 3

    def test_preserves_ids(self, svc, tmp_path):
        """Migration preserves task numbers... as sequential IDs in the work service.

        Note: The work service assigns sequential task_numbers, so the original
        file number becomes the source identity. The task title is preserved.
        """
        issues_dir = tmp_path / "issues"
        self._setup_issue_file(
            issues_dir, "00-not-ready", "0042-fix-auth.md",
            "# Fix Auth\n\nFix authentication."
        )

        result = migrate_issues(issues_dir, svc, project="proj")

        assert result.created == 1
        tasks = svc.list_tasks(project="proj")
        assert len(tasks) == 1
        assert tasks[0].title == "Fix Auth"

    def test_preserves_content(self, svc, tmp_path):
        """Migration preserves description from file content."""
        issues_dir = tmp_path / "issues"
        self._setup_issue_file(
            issues_dir, "00-not-ready", "0001-my-task.md",
            "# My Task\n\nThis is the detailed description.\n\nWith multiple paragraphs."
        )

        result = migrate_issues(issues_dir, svc, project="proj")

        assert result.created == 1
        task = svc.list_tasks(project="proj")[0]
        assert "detailed description" in task.description
        assert "multiple paragraphs" in task.description

    def test_idempotent(self, svc, tmp_path):
        """Running migration twice creates no duplicates."""
        issues_dir = tmp_path / "issues"
        self._setup_issue_file(
            issues_dir, "00-not-ready", "0001-task-a.md",
            "# Task A\n\nDescription A."
        )
        self._setup_issue_file(
            issues_dir, "01-ready", "0002-task-b.md",
            "# Task B\n\nDescription B."
        )

        result1 = migrate_issues(issues_dir, svc, project="proj")
        assert result1.created == 2

        result2 = migrate_issues(issues_dir, svc, project="proj")
        assert result2.created == 0
        assert result2.skipped == 2

        tasks = svc.list_tasks(project="proj")
        assert len(tasks) == 2

    def test_handles_completed(self, svc, tmp_path):
        """Completed issues end up as done in work service."""
        issues_dir = tmp_path / "issues"
        self._setup_issue_file(
            issues_dir, "05-completed", "0001-old-task.md",
            "# Old Task\n\nThis was already done."
        )

        result = migrate_issues(issues_dir, svc, project="proj")

        assert result.created == 1
        task = svc.list_tasks(project="proj")[0]
        assert task.work_status == WorkStatus.DONE

    def test_handles_ready_as_queued(self, svc, tmp_path):
        """Ready issues end up as queued."""
        issues_dir = tmp_path / "issues"
        self._setup_issue_file(
            issues_dir, "01-ready", "0001-ready-task.md",
            "# Ready Task\n\nWaiting to start."
        )

        result = migrate_issues(issues_dir, svc, project="proj")

        assert result.created == 1
        task = svc.list_tasks(project="proj")[0]
        assert task.work_status == WorkStatus.QUEUED

    def test_handles_missing_dir(self, svc, tmp_path):
        """Migration gracefully handles a non-existent issues directory."""
        missing_dir = tmp_path / "nonexistent"
        result = migrate_issues(missing_dir, svc, project="proj")
        assert result.created == 0
        assert len(result.errors) == 1

    def test_skips_non_md_files(self, svc, tmp_path):
        """Migration ignores non-.md files."""
        issues_dir = tmp_path / "issues"
        state_dir = issues_dir / "00-not-ready"
        state_dir.mkdir(parents=True)
        (state_dir / "notes.txt").write_text("not a task")
        self._setup_issue_file(
            issues_dir, "00-not-ready", "0001-real-task.md",
            "# Real Task\n\nActual task."
        )

        result = migrate_issues(issues_dir, svc, project="proj")
        assert result.created == 1

    def test_parse_filename(self):
        """Filename parser extracts number and slug."""
        assert _parse_filename("0042-fix-auth.md") == (42, "fix-auth")
        assert _parse_filename("0001-setup.md") == (1, "setup")
        assert _parse_filename("bad-name.md") is None
        assert _parse_filename("not-a-task.txt") is None

    def test_parse_content(self):
        """Content parser extracts title and description."""
        title, desc = _parse_content("# My Title\n\nThe description.")
        assert title == "My Title"
        assert desc == "The description."

        title, desc = _parse_content("no heading here")
        assert title == ""
        assert desc == "no heading here"


# ---------------------------------------------------------------------------
# sync_status / trigger_sync — SQLiteWorkService implementation of Protocol
# ---------------------------------------------------------------------------


class _RecordingAdapter:
    """Simple adapter that records every call and can be told to fail."""

    def __init__(self, name: str = "rec", fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.creates: list[str] = []
        self.transitions: list[tuple[str, str, str]] = []
        self.updates: list[tuple[str, list[str]]] = []

    def on_create(self, task):
        if self.fail:
            raise RuntimeError(f"{self.name}: boom")
        self.creates.append(task.task_id)

    def on_transition(self, task, old_status, new_status):
        self.transitions.append((task.task_id, old_status, new_status))

    def on_update(self, task, changed_fields):
        self.updates.append((task.task_id, list(changed_fields)))


class TestSyncStatus:
    def test_sync_status_unknown_task_raises(self, svc):
        from pollypm.work.sqlite_service import TaskNotFoundError

        with pytest.raises(TaskNotFoundError):
            svc.sync_status("proj/999")

    def test_sync_status_no_adapters(self, svc):
        """A task with no sync adapters returns an empty dict."""
        task = _make_task(svc)
        assert svc.sync_status(task.task_id) == {}

    def test_sync_status_lists_registered_adapters_with_no_row(self, tmp_path):
        """Adapters that have never synced show as attempts=0 entries."""
        mgr = SyncManager()
        mgr.register(_RecordingAdapter(name="rec"))
        svc = SQLiteWorkService(db_path=tmp_path / "work.db", sync_manager=mgr)
        task = _make_task(svc)

        status = svc.sync_status(task.task_id)
        assert "rec" in status
        assert status["rec"]["attempts"] == 0
        assert status["rec"]["last_synced_at"] is None
        assert status["rec"]["last_error"] is None


class TestTriggerSync:
    def test_trigger_sync_all_tasks(self, tmp_path):
        mgr = SyncManager()
        ad = _RecordingAdapter(name="rec")
        mgr.register(ad)
        svc = SQLiteWorkService(db_path=tmp_path / "work.db", sync_manager=mgr)

        t1 = _make_task(svc, title="A")
        t2 = _make_task(svc, title="B")
        # Clear creates accumulated by on_create during svc.create()
        ad.creates.clear()

        result = svc.trigger_sync()
        assert result["synced"] == 2
        # Adapter was called for both tasks
        assert set(ad.creates) == {t1.task_id, t2.task_id}

        # sync_status reflects the successful sync with attempts incremented
        status = svc.sync_status(t1.task_id)
        assert status["rec"]["attempts"] >= 1
        assert status["rec"]["last_error"] is None
        assert status["rec"]["last_synced_at"] is not None

    def test_trigger_sync_specific_task(self, tmp_path):
        mgr = SyncManager()
        ad = _RecordingAdapter(name="rec")
        mgr.register(ad)
        svc = SQLiteWorkService(db_path=tmp_path / "work.db", sync_manager=mgr)

        t1 = _make_task(svc, title="A")
        _make_task(svc, title="B")
        ad.creates.clear()

        result = svc.trigger_sync(task_id=t1.task_id)
        assert result["synced"] == 1
        assert ad.creates == [t1.task_id]

    def test_trigger_sync_adapter_filter(self, tmp_path):
        mgr = SyncManager()
        a1 = _RecordingAdapter(name="one")
        a2 = _RecordingAdapter(name="two")
        mgr.register(a1)
        mgr.register(a2)
        svc = SQLiteWorkService(db_path=tmp_path / "work.db", sync_manager=mgr)

        t = _make_task(svc)
        a1.creates.clear()
        a2.creates.clear()

        result = svc.trigger_sync(adapter="one")
        assert result["synced"] == 1
        assert a1.creates == [t.task_id]
        assert a2.creates == []

    def test_trigger_sync_records_errors(self, tmp_path):
        mgr = SyncManager()
        failing = _RecordingAdapter(name="boomer", fail=True)
        mgr.register(failing)
        svc = SQLiteWorkService(db_path=tmp_path / "work.db", sync_manager=mgr)
        task = _make_task(svc)

        result = svc.trigger_sync()
        assert result["synced"] == 0
        assert task.task_id in result["errors"]["boomer"]

        # sync_status must surface the last_error and a bumped attempt count
        status = svc.sync_status(task.task_id)
        assert "boom" in status["boomer"]["last_error"]
        assert status["boomer"]["attempts"] >= 1

    def test_trigger_sync_unknown_task_raises(self, svc):
        from pollypm.work.sqlite_service import TaskNotFoundError

        with pytest.raises(TaskNotFoundError):
            svc.trigger_sync(task_id="proj/999")

    def test_trigger_sync_no_adapters(self, svc):
        """Without a sync manager attached, returns an empty summary."""
        _make_task(svc)
        result = svc.trigger_sync()
        assert result == {"synced": 0, "errors": {}}
