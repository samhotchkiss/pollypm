"""Tests for advisor change detection (ad02).

Covers:

* Commit SHAs gathered via ``git log --since``, via subprocess mock.
* Changed files gathered from ``git diff <earliest>^..HEAD --name-only``.
* Task transitions queried via a work-service stub (list_transitions).
* ``has_changes`` flag: True when ≥1 commit OR ≥1 transition.
* Per-tick cache: same (path, since) returns the same report without
  re-shelling out.
* Integration: a real ``git init`` project with a commit surfaces the
  commit SHA + changed files in the ChangeReport.
"""
from __future__ import annotations

import subprocess
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pollypm.plugins_builtin.advisor.handlers import detect_changes as dc_module
from pollypm.plugins_builtin.advisor.handlers.detect_changes import (
    TaskTransitionRecord,
    clear_cache,
    detect_changes,
)
from pollypm.work.schema import create_work_tables


# ---------------------------------------------------------------------------
# Unit tests with subprocess + work-service mocks
# ---------------------------------------------------------------------------


class TestDetectChangesUnit:
    def setup_method(self) -> None:
        clear_cache()

    def test_no_git_dir_returns_empty_commits(self, tmp_path: Path) -> None:
        project = tmp_path / "not-a-repo"
        project.mkdir()
        report = detect_changes(project, since=None, project_key="x")
        assert report.commit_shas == []
        assert report.changed_files == []
        assert report.has_changes is False

    def test_commits_flag_has_changes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "proj"
        (project / ".git").mkdir(parents=True)

        def fake_run_git(path, args, *, timeout=10.0):
            if args[0] == "log":
                return 0, "aaa1111\nbbb2222\n"
            if args[0] == "diff":
                return 0, "src/foo.py\nsrc/bar.py\n"
            return 1, ""

        monkeypatch.setattr(dc_module, "_run_git", fake_run_git)
        monkeypatch.setattr(
            dc_module, "_gather_task_transitions",
            lambda *a, **kw: [],
        )

        since = datetime(2026, 4, 16, 12, 0, tzinfo=UTC)
        report = detect_changes(project, since=since, project_key="proj")
        # Reversed to chronological order — earliest first.
        assert report.commit_shas == ["bbb2222", "aaa1111"]
        assert report.has_changes is True
        names = [p.name for p in report.changed_files]
        assert "foo.py" in names
        assert "bar.py" in names
        assert "2 commits" in report.files_diff_summary

    def test_files_diff_summary_singular_pluralisation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cycle 67: ``files_diff_summary`` pluralises per count.

        Previously read ``1 commits, 1 files changed, 1 task
        transitions`` at the singular boundary — three copy bugs in
        one line. Pulled in alongside the doctor / cockpit / inbox /
        morning-briefing plural cleanup batch.
        """
        project = tmp_path / "proj"
        (project / ".git").mkdir(parents=True)

        def fake_run_git(path, args, *, timeout=10.0):
            if args[0] == "log":
                return 0, "aaa1111\n"
            if args[0] == "diff":
                return 0, "src/foo.py\n"
            return 1, ""

        monkeypatch.setattr(dc_module, "_run_git", fake_run_git)
        monkeypatch.setattr(
            dc_module, "_gather_task_transitions",
            lambda *a, **kw: [
                TaskTransitionRecord(
                    project="proj",
                    task_number=1,
                    task_title="t",
                    from_state="queued",
                    to_state="in_progress",
                    actor="worker",
                    timestamp="2026-04-16T12:01:00+00:00",
                ),
            ],
        )
        since = datetime(2026, 4, 16, 12, 0, tzinfo=UTC)
        report = detect_changes(project, since=since, project_key="proj")
        assert report.files_diff_summary == (
            "1 commit, 1 file changed, 1 task transition"
        )

    def test_task_transitions_alone_flag_has_changes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "proj"
        (project / ".git").mkdir(parents=True)

        monkeypatch.setattr(dc_module, "_run_git", lambda *a, **kw: (0, ""))

        class FakeWS:
            def list_transitions(self, *, project, since):
                return [
                    TaskTransitionRecord(
                        project=project,
                        task_number=42,
                        task_title="something",
                        from_state="queued",
                        to_state="in_progress",
                        actor="worker",
                        timestamp="2026-04-16T12:01:00+00:00",
                    )
                ]

        report = detect_changes(
            project, since=None, project_key="proj", work_service=FakeWS(),
        )
        assert report.commit_shas == []
        assert report.has_changes is True
        assert len(report.task_transitions) == 1
        assert report.task_transitions[0].task_id == "proj/42"

    def test_task_transitions_sqlite_fallback_uses_storage_query(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = tmp_path / "workspace"
        project = workspace / "proj"
        project.mkdir(parents=True)
        state_dir = workspace / ".pollypm"
        state_dir.mkdir()
        db_path = state_dir / "state.db"

        created_at = "2026-04-16T12:01:00+00:00"
        conn = sqlite3.connect(str(db_path))
        try:
            create_work_tables(conn)
            conn.execute(
                "INSERT INTO work_tasks (project, task_number, title, type, "
                "flow_template_id, created_at, created_by, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "proj",
                    7,
                    "storage boundary",
                    "task",
                    "chat",
                    created_at,
                    "test",
                    created_at,
                ),
            )
            conn.execute(
                "INSERT INTO work_transitions "
                "(task_project, task_number, from_state, to_state, actor, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("proj", 7, "queued", "in_progress", "worker", created_at),
            )
            conn.commit()
        finally:
            conn.close()

        # Post-#1471 the advisor resolves the workspace DB through the
        # canonical resolver (no project_path arg). Pin it to the test's
        # tmp workspace so the SQLite fallback exercises the storage facade.
        monkeypatch.setattr(
            dc_module, "_transitions_db_path", lambda _project_path: db_path,
        )

        rows = dc_module._gather_task_transitions(
            project,
            "proj",
            datetime(2026, 4, 16, 12, 0, tzinfo=UTC),
        )

        assert len(rows) == 1
        assert rows[0] == TaskTransitionRecord(
            project="proj",
            task_number=7,
            task_title="storage boundary",
            from_state="queued",
            to_state="in_progress",
            actor="worker",
            timestamp=created_at,
        )

    def test_no_signals_returns_has_changes_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "proj"
        (project / ".git").mkdir(parents=True)
        monkeypatch.setattr(dc_module, "_run_git", lambda *a, **kw: (0, ""))
        monkeypatch.setattr(
            dc_module, "_gather_task_transitions", lambda *a, **kw: [],
        )

        report = detect_changes(project, since=None, project_key="proj")
        assert report.has_changes is False
        # The flag that gates the advisor session is `has_changes` — a
        # "no-changes" tick must cost exactly zero enqueues.
        assert report.commit_shas == []
        assert report.task_transitions == []

    def test_cache_dedupes_within_tick(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "proj"
        (project / ".git").mkdir(parents=True)

        calls = {"n": 0}

        def counting_run_git(path, args, *, timeout=10.0):
            calls["n"] += 1
            return 0, ""

        monkeypatch.setattr(dc_module, "_run_git", counting_run_git)
        monkeypatch.setattr(
            dc_module, "_gather_task_transitions", lambda *a, **kw: [],
        )

        since = datetime(2026, 4, 16, 12, 0, tzinfo=UTC)
        a = detect_changes(project, since=since, project_key="proj")
        before = calls["n"]
        b = detect_changes(project, since=since, project_key="proj")
        # Second call must hit the cache and add no git shell-outs.
        assert calls["n"] == before
        assert a is b

    def test_clear_cache_forces_refetch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "proj"
        (project / ".git").mkdir(parents=True)

        calls = {"n": 0}

        def counting_run_git(path, args, *, timeout=10.0):
            calls["n"] += 1
            return 0, ""

        monkeypatch.setattr(dc_module, "_run_git", counting_run_git)
        monkeypatch.setattr(
            dc_module, "_gather_task_transitions", lambda *a, **kw: [],
        )

        since = datetime(2026, 4, 16, 12, 0, tzinfo=UTC)
        detect_changes(project, since=since, project_key="proj")
        first = calls["n"]
        clear_cache()
        detect_changes(project, since=since, project_key="proj")
        assert calls["n"] > first

    def test_git_timeout_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "proj"
        (project / ".git").mkdir(parents=True)

        def raising_run(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="git", timeout=0.1)

        monkeypatch.setattr(subprocess, "run", raising_run)
        monkeypatch.setattr(
            dc_module, "_gather_task_transitions", lambda *a, **kw: [],
        )

        report = detect_changes(project, since=None, project_key="proj")
        assert report.commit_shas == []
        assert report.has_changes is False


# ---------------------------------------------------------------------------
# Integration — a real git repo with one commit
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            **{k: v for k, v in __import__("os").environ.items()},
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )


@pytest.fixture
def mini_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "hello.py").write_text("print('hi')\n")
    _git(repo, "add", "hello.py")
    _git(repo, "commit", "-q", "-m", "first commit")
    return repo


class TestDetectChangesIntegration:
    def setup_method(self) -> None:
        clear_cache()

    def test_real_repo_with_recent_commit(self, mini_repo: Path) -> None:
        yesterday = datetime.now(UTC) - timedelta(hours=1)
        report = detect_changes(mini_repo, since=yesterday, project_key="mini")
        assert report.has_changes is True
        assert len(report.commit_shas) == 1
        names = {p.name for p in report.changed_files}
        assert "hello.py" in names

    def test_real_repo_no_recent_commits(self, mini_repo: Path) -> None:
        tomorrow = datetime.now(UTC) + timedelta(days=1)
        report = detect_changes(mini_repo, since=tomorrow, project_key="mini")
        assert report.has_changes is False

    def test_real_repo_first_run_lookback(self, mini_repo: Path) -> None:
        """since=None uses a 24-hour lookback — a fresh commit is picked up."""
        report = detect_changes(mini_repo, since=None, project_key="mini")
        assert report.has_changes is True
        assert len(report.commit_shas) == 1


# ---------------------------------------------------------------------------
# Tick-handler rewiring — the ad01 tick now calls the real detect_changes.
# ---------------------------------------------------------------------------


class TestTickHandlerUsesRealDetectChanges:
    def setup_method(self) -> None:
        clear_cache()

    def test_tick_adapter_returns_bool_from_report(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pollypm.plugins_builtin.advisor.handlers import advisor_tick as tick

        project = tmp_path / "proj"
        (project / ".git").mkdir(parents=True)

        def fake_run_git(path, args, *, timeout=10.0):
            if args[0] == "log":
                return 0, "abc123\n"
            if args[0] == "diff":
                return 0, "x.py\n"
            return 1, ""

        monkeypatch.setattr(dc_module, "_run_git", fake_run_git)
        monkeypatch.setattr(dc_module, "_gather_task_transitions", lambda *a, **kw: [])

        result = tick.detect_changes(project, since=None, project_key="proj")
        assert result is True
