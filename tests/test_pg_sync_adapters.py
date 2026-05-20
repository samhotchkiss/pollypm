"""Pg re-coverage of sync adapters + SyncManager + work-service sync API (#1785).

Replaces the sqlite-bound ``tests/test_sync_adapters.py`` that Slice
K-tests part 4 (#1786) deleted. The deleted module covered:

* :class:`FileSyncAdapter` — file/markdown writes, status mapping,
  auto-commit, scaffold-issue-tracker idempotence (backend-neutral).
* :class:`GitHubSyncAdapter` — ``gh`` shell-outs for create / transition /
  update; status label mapping (mostly backend-neutral; one test
  exercises the work-service constructor's ``sync_manager`` kwarg).
* :class:`SyncManager` — fan-out + failure isolation (backend-neutral).
* :mod:`pollypm.work.migrate` — ``migrate_issues`` happy-path + idempotence
  (touches work-service ``create`` / ``list_tasks``).
* The work-service's ``sync_status`` / ``trigger_sync`` Protocol surface —
  this is the part that was blocked by #1775 (now closed): pg's
  :class:`PgWorkService` constructor accepts ``sync_manager`` and the
  protocol methods land sync_state rows.

This module re-ports the work-service-coupled tests (TestSyncStatus +
TestTriggerSync + the GitHub-ref persistence smoke + the migrate
happy-path) against the ``pg_work_service`` fixture so the contract
re-enters CI under the pg backend. The adapter-only tests
(FileSyncAdapter, GitHubSyncAdapter, SyncManager, _parse_filename,
_parse_content) are backend-agnostic — they don't go through the
work-service at all — so they're omitted here to keep the file
focused on the actual gap (#1785). Those backend-neutral tests can
be restored separately if they're not covered elsewhere.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pollypm.work.migrate import migrate_issues
from pollypm.work.models import WorkStatus
from pollypm.work.service_support import TaskNotFoundError
from pollypm.work.sync import SyncManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(svc, title="Test task", description="A description", **kwargs):
    """Standard task on the pg work-service."""
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


class _RecordingAdapter:
    """Adapter that records every dispatch and can be told to fail."""

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


# ---------------------------------------------------------------------------
# PgWorkService.sync_status — Protocol surface (#1785)
# ---------------------------------------------------------------------------


class TestSyncStatus:
    def test_sync_status_unknown_task_raises(self, pg_work_service):
        with pytest.raises(TaskNotFoundError):
            pg_work_service.sync_status("proj/999")

    def test_sync_status_no_adapters(self, pg_work_service):
        """A task with no sync adapters returns an empty dict."""
        task = _make_task(pg_work_service)
        assert pg_work_service.sync_status(task.task_id) == {}

    def test_sync_status_lists_registered_adapters_with_no_row(
        self, pg_schema_pool
    ):
        """Adapters that have never synced show as ``attempts=0``."""
        from pollypm.work.pg_service import PgWorkService

        mgr = SyncManager()
        mgr.register(_RecordingAdapter(name="rec"))
        svc = PgWorkService(pool=pg_schema_pool, ro_pool=None, sync_manager=mgr)
        task = _make_task(svc)

        status = svc.sync_status(task.task_id)
        assert "rec" in status
        assert status["rec"]["attempts"] == 0
        assert status["rec"]["last_synced_at"] is None
        assert status["rec"]["last_error"] is None


# ---------------------------------------------------------------------------
# PgWorkService.trigger_sync — Protocol surface (#1785)
# ---------------------------------------------------------------------------


class TestTriggerSync:
    def test_trigger_sync_all_tasks(self, pg_schema_pool):
        from pollypm.work.pg_service import PgWorkService

        mgr = SyncManager()
        ad = _RecordingAdapter(name="rec")
        mgr.register(ad)
        svc = PgWorkService(pool=pg_schema_pool, ro_pool=None, sync_manager=mgr)

        t1 = _make_task(svc, title="A")
        t2 = _make_task(svc, title="B")
        # Drop the on_create dispatches that fired during svc.create().
        ad.creates.clear()

        result = svc.trigger_sync()
        assert result["synced"] == 2
        assert set(ad.creates) == {t1.task_id, t2.task_id}

        # sync_status reflects the successful sync with attempts incremented
        status = svc.sync_status(t1.task_id)
        assert status["rec"]["attempts"] >= 1
        assert status["rec"]["last_error"] is None
        assert status["rec"]["last_synced_at"] is not None

    def test_trigger_sync_specific_task(self, pg_schema_pool):
        from pollypm.work.pg_service import PgWorkService

        mgr = SyncManager()
        ad = _RecordingAdapter(name="rec")
        mgr.register(ad)
        svc = PgWorkService(pool=pg_schema_pool, ro_pool=None, sync_manager=mgr)

        t1 = _make_task(svc, title="A")
        _make_task(svc, title="B")
        ad.creates.clear()

        result = svc.trigger_sync(task_id=t1.task_id)
        assert result["synced"] == 1
        assert ad.creates == [t1.task_id]

    def test_trigger_sync_adapter_filter(self, pg_schema_pool):
        from pollypm.work.pg_service import PgWorkService

        mgr = SyncManager()
        a1 = _RecordingAdapter(name="one")
        a2 = _RecordingAdapter(name="two")
        mgr.register(a1)
        mgr.register(a2)
        svc = PgWorkService(pool=pg_schema_pool, ro_pool=None, sync_manager=mgr)

        t = _make_task(svc)
        a1.creates.clear()
        a2.creates.clear()

        result = svc.trigger_sync(adapter="one")
        assert result["synced"] == 1
        assert a1.creates == [t.task_id]
        assert a2.creates == []

    def test_trigger_sync_records_errors(self, pg_schema_pool):
        from pollypm.work.pg_service import PgWorkService

        mgr = SyncManager()
        failing = _RecordingAdapter(name="boomer", fail=True)
        mgr.register(failing)
        svc = PgWorkService(pool=pg_schema_pool, ro_pool=None, sync_manager=mgr)
        task = _make_task(svc)

        result = svc.trigger_sync()
        assert result["synced"] == 0
        assert task.task_id in result["errors"]["boomer"]

        # sync_status must surface the last_error and a bumped attempt count
        status = svc.sync_status(task.task_id)
        assert "boom" in status["boomer"]["last_error"]
        assert status["boomer"]["attempts"] >= 1

    def test_trigger_sync_unknown_task_raises(self, pg_work_service):
        with pytest.raises(TaskNotFoundError):
            pg_work_service.trigger_sync(task_id="proj/999")

    def test_trigger_sync_no_adapters(self, pg_work_service):
        """Without a sync manager attached, returns an empty summary."""
        _make_task(pg_work_service)
        result = pg_work_service.trigger_sync()
        assert result == {"synced": 0, "errors": {}}


# ---------------------------------------------------------------------------
# GitHub adapter — work-service round-trip of external_refs (#1785)
# ---------------------------------------------------------------------------


class TestGitHubAdapterWorkServiceIntegration:
    """Verify the ``sync_manager=`` constructor kwarg actually wires
    the GitHub adapter so the ``external_refs`` it stamps survive the
    create transaction. This is the half of the deleted test that was
    blocked by #1775."""

    def test_work_service_persists_created_issue_ref(self, pg_schema_pool):
        from unittest.mock import MagicMock, patch

        from pollypm.work.pg_service import PgWorkService
        from pollypm.work.sync_github import GitHubSyncAdapter

        manager = SyncManager()
        manager.register(GitHubSyncAdapter(repo="owner/repo"))
        svc = PgWorkService(
            pool=pg_schema_pool, ro_pool=None, sync_manager=manager
        )

        with patch("pollypm.work.sync_github._run_gh") as mock_gh:
            mock_gh.return_value = MagicMock(
                stdout="https://github.com/owner/repo/issues/42\n"
            )
            task = _make_task(svc)

        reloaded = svc.get(task.task_id)
        assert reloaded.external_refs.get("github_issue") == "42"


# ---------------------------------------------------------------------------
# Migration tool — work-service create() round-trip (#1785)
# ---------------------------------------------------------------------------


class TestMigration:
    def _setup_issue_file(
        self, issues_dir: Path, folder: str, filename: str, content: str
    ):
        dirpath = issues_dir / folder
        dirpath.mkdir(parents=True, exist_ok=True)
        filepath = dirpath / filename
        filepath.write_text(content, encoding="utf-8")
        return filepath

    def test_creates_tasks(self, pg_work_service, tmp_path):
        svc = pg_work_service
        issues_dir = tmp_path / "issues"
        self._setup_issue_file(
            issues_dir,
            "00-not-ready",
            "0001-setup-project.md",
            "# Setup Project\n\nInitial setup tasks.",
        )
        self._setup_issue_file(
            issues_dir,
            "01-ready",
            "0002-add-auth.md",
            "# Add Authentication\n\nImplement login flow.",
        )
        self._setup_issue_file(
            issues_dir,
            "02-in-progress",
            "0003-fix-bug.md",
            "# Fix Bug\n\nResolve the crash.",
        )

        result = migrate_issues(issues_dir, svc, project="proj")

        assert result.created == 3
        assert result.skipped == 0
        assert result.errors == []

        tasks = svc.list_tasks(project="proj")
        assert len(tasks) == 3

    def test_preserves_content(self, pg_work_service, tmp_path):
        svc = pg_work_service
        issues_dir = tmp_path / "issues"
        self._setup_issue_file(
            issues_dir,
            "00-not-ready",
            "0001-my-task.md",
            "# My Task\n\nThis is the detailed description.\n\n"
            "With multiple paragraphs.",
        )

        result = migrate_issues(issues_dir, svc, project="proj")

        assert result.created == 1
        task = svc.list_tasks(project="proj")[0]
        assert "detailed description" in task.description
        assert "multiple paragraphs" in task.description

    def test_idempotent(self, pg_work_service, tmp_path):
        svc = pg_work_service
        issues_dir = tmp_path / "issues"
        self._setup_issue_file(
            issues_dir,
            "00-not-ready",
            "0001-task-a.md",
            "# Task A\n\nDescription A.",
        )
        self._setup_issue_file(
            issues_dir,
            "01-ready",
            "0002-task-b.md",
            "# Task B\n\nDescription B.",
        )

        result1 = migrate_issues(issues_dir, svc, project="proj")
        assert result1.created == 2

        result2 = migrate_issues(issues_dir, svc, project="proj")
        assert result2.created == 0
        assert result2.skipped == 2

        tasks = svc.list_tasks(project="proj")
        assert len(tasks) == 2

    def test_handles_completed(self, pg_work_service, tmp_path):
        """Completed issues end up as done in work service."""
        svc = pg_work_service
        issues_dir = tmp_path / "issues"
        self._setup_issue_file(
            issues_dir,
            "05-completed",
            "0001-old-task.md",
            "# Old Task\n\nThis was already done.",
        )

        result = migrate_issues(issues_dir, svc, project="proj")

        assert result.created == 1
        task = svc.list_tasks(project="proj")[0]
        assert task.work_status == WorkStatus.DONE

    def test_handles_ready_as_queued(self, pg_work_service, tmp_path):
        svc = pg_work_service
        issues_dir = tmp_path / "issues"
        self._setup_issue_file(
            issues_dir,
            "01-ready",
            "0001-ready-task.md",
            "# Ready Task\n\nWaiting to start.",
        )

        result = migrate_issues(issues_dir, svc, project="proj")

        assert result.created == 1
        task = svc.list_tasks(project="proj")[0]
        assert task.work_status == WorkStatus.QUEUED

    def test_handles_missing_dir(self, pg_work_service, tmp_path):
        svc = pg_work_service
        missing_dir = tmp_path / "nonexistent"
        result = migrate_issues(missing_dir, svc, project="proj")
        assert result.created == 0
        assert len(result.errors) == 1

    def test_skips_non_md_files(self, pg_work_service, tmp_path):
        svc = pg_work_service
        issues_dir = tmp_path / "issues"
        state_dir = issues_dir / "00-not-ready"
        state_dir.mkdir(parents=True)
        (state_dir / "notes.txt").write_text("not a task")
        self._setup_issue_file(
            issues_dir,
            "00-not-ready",
            "0001-real-task.md",
            "# Real Task\n\nActual task.",
        )

        result = migrate_issues(issues_dir, svc, project="proj")
        assert result.created == 1
