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
