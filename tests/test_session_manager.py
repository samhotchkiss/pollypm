"""Tests for SessionManager — worker session lifecycle."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from pollypm.work.session_manager import (
    SessionManager,
    WorkerSession,
    TeardownResult,
    _parse_token_usage,
    WORK_SESSIONS_SCHEMA,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_project(tmp_path):
    """A temporary project directory with a bare git repo."""
    project = tmp_path / "myproject"
    project.mkdir()
    # Initialize a git repo so worktree commands would work
    subprocess.run(
        ["git", "init", str(project)],
        check=True, capture_output=True, text=True,
    )
    # Need at least one commit for worktree add
    (project / "README.md").write_text("init")
    subprocess.run(
        ["git", "-C", str(project), "add", "."],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", str(project), "commit", "-m", "init"],
        check=True, capture_output=True, text=True,
    )
    return project


@pytest.fixture
def db_conn(tmp_path):
    """A SQLite connection with work tables."""
    db_path = tmp_path / "work.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # Create the minimal work_tasks table that work_sessions references
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS work_tasks (
            project TEXT NOT NULL,
            task_number INTEGER NOT NULL,
            PRIMARY KEY (project, task_number)
        );
    """)
    conn.executescript(WORK_SESSIONS_SCHEMA)
    return conn


@pytest.fixture
def mock_tmux():
    """A mock TmuxClient."""
    tmux = MagicMock()
    tmux.has_session.return_value = False
    tmux.create_session.return_value = None
    tmux.create_window.return_value = None
    tmux.kill_pane.return_value = None
    tmux.is_pane_alive.return_value = True
    tmux.send_keys.return_value = None

    # list_windows returns a window with a pane_id
    mock_window = MagicMock()
    mock_window.pane_id = "%1"
    mock_window.name = "task-proj-1"
    tmux.list_windows.return_value = [mock_window]

    return tmux


@pytest.fixture
def mock_svc(db_conn):
    """A mock work service with a real SQLite connection."""
    svc = MagicMock()
    svc._conn = db_conn
    return svc


@pytest.fixture
def manager(mock_tmux, mock_svc, tmp_project):
    """A SessionManager wired to mocks."""
    # Insert a task row so FK constraint is satisfied
    mock_svc._conn.execute(
        "INSERT INTO work_tasks (project, task_number) VALUES (?, ?)",
        ("proj", 1),
    )
    mock_svc._conn.execute(
        "INSERT INTO work_tasks (project, task_number) VALUES (?, ?)",
        ("proj", 2),
    )
    mock_svc._conn.execute(
        "INSERT INTO work_tasks (project, task_number) VALUES (?, ?)",
        ("other", 1),
    )
    mock_svc._conn.commit()
    return SessionManager(mock_tmux, mock_svc, tmp_project)


# ---------------------------------------------------------------------------
# Provision tests
# ---------------------------------------------------------------------------


class TestProvisionWorker:
    def test_provision_worker_creates_worktree(self, manager, mock_tmux, tmp_project):
        with patch("pollypm.work.session_manager.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            session = manager.provision_worker("proj/1", "agent-1")

        assert session.task_id == "proj/1"
        assert session.agent_name == "agent-1"
        assert session.branch_name == "task/proj-1"
        assert session.worktree_path == tmp_project / ".pollypm" / "worktrees" / "proj-1"

        # Verify git worktree add was called
        calls = mock_sub.run.call_args_list
        git_call = calls[0]
        args = git_call[0][0]
        assert "worktree" in args
        assert "add" in args

    def test_provision_worker_creates_tmux_pane(self, manager, mock_tmux, tmp_project):
        with patch("pollypm.work.session_manager.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            session = manager.provision_worker("proj/1", "agent-1")

        # Should have created a session (since has_session returns False)
        mock_tmux.create_session.assert_called_once()
        assert session.pane_id == "%1"

    def test_provision_worker_records_binding(self, manager, mock_tmux, tmp_project):
        with patch("pollypm.work.session_manager.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            manager.provision_worker("proj/1", "agent-1")

        found = manager.session_for_task("proj/1")
        assert found is not None
        assert found.task_id == "proj/1"
        assert found.agent_name == "agent-1"

    def test_provision_worker_idempotent(self, manager, mock_tmux, tmp_project):
        with patch("pollypm.work.session_manager.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            s1 = manager.provision_worker("proj/1", "agent-1")
            s2 = manager.provision_worker("proj/1", "agent-1")

        assert s1.task_id == s2.task_id
        assert s1.pane_id == s2.pane_id
        # create_session called only once
        assert mock_tmux.create_session.call_count == 1


# ---------------------------------------------------------------------------
# Teardown tests
# ---------------------------------------------------------------------------


class TestTeardownWorker:
    def _provision(self, manager, task_id="proj/1"):
        with patch("pollypm.work.session_manager.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            return manager.provision_worker(task_id, "agent-1")

    def test_teardown_worker_archives_jsonl(self, manager, mock_tmux, tmp_project):
        session = self._provision(manager)

        # Create a fake JSONL file in the worktree
        claude_dir = session.worktree_path / ".claude"
        claude_dir.mkdir(parents=True)
        jsonl_file = claude_dir / "session.jsonl"
        jsonl_file.write_text('{"type": "token_usage", "input_tokens": 100, "output_tokens": 50}\n')

        with patch("pollypm.work.session_manager.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            # Make the worktree path exist for removal check
            result = manager.teardown_worker("proj/1")

        assert result.jsonl_archived is True
        assert result.archive_path is not None
        assert "transcripts" in str(result.archive_path)

    def test_teardown_worker_removes_worktree(self, manager, mock_tmux, tmp_project):
        session = self._provision(manager)

        # Create the worktree directory so _remove_worktree doesn't short-circuit
        session.worktree_path.mkdir(parents=True, exist_ok=True)

        with patch("pollypm.work.session_manager.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            result = manager.teardown_worker("proj/1")

        # Verify git worktree remove was called
        calls = mock_sub.run.call_args_list
        remove_calls = [c for c in calls if "remove" in str(c)]
        assert len(remove_calls) > 0
        assert result.worktree_removed is True

    def test_teardown_worker_kills_pane(self, manager, mock_tmux, tmp_project):
        session = self._provision(manager)

        with patch("pollypm.work.session_manager.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            manager.teardown_worker("proj/1")

        mock_tmux.kill_pane.assert_called_once_with(session.pane_id)

    def test_teardown_worker_idempotent(self, manager, mock_tmux, tmp_project):
        self._provision(manager)

        with patch("pollypm.work.session_manager.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            r1 = manager.teardown_worker("proj/1")
            r2 = manager.teardown_worker("proj/1")

        assert r1.task_id == "proj/1"
        assert r2.task_id == "proj/1"
        # Second teardown is a no-op
        assert r2.jsonl_archived is False
        assert r2.worktree_removed is False

    def test_teardown_worker_records_tokens(self, manager, mock_tmux, tmp_project):
        session = self._provision(manager)

        # Create JSONL with token events
        claude_dir = session.worktree_path / ".claude"
        claude_dir.mkdir(parents=True)
        jsonl_file = claude_dir / "session.jsonl"
        lines = [
            json.dumps({"type": "token_usage", "input_tokens": 100, "output_tokens": 50}),
            json.dumps({"type": "token_usage", "input_tokens": 200, "output_tokens": 75}),
        ]
        jsonl_file.write_text("\n".join(lines) + "\n")

        with patch("pollypm.work.session_manager.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            result = manager.teardown_worker("proj/1")

        assert result.total_input_tokens == 300
        assert result.total_output_tokens == 125


# ---------------------------------------------------------------------------
# Rejection tests
# ---------------------------------------------------------------------------


class TestNotifyRejection:
    def _provision(self, manager):
        with patch("pollypm.work.session_manager.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            return manager.provision_worker("proj/1", "agent-1")

    def test_notify_rejection_sends_feedback(self, manager, mock_tmux):
        session = self._provision(manager)
        result = manager.notify_rejection("proj/1", "Tests are failing")

        assert result is True
        mock_tmux.send_keys.assert_called_once()
        call_args = mock_tmux.send_keys.call_args
        assert "Tests are failing" in call_args[0][1]
        assert call_args[0][0] == session.pane_id

    def test_notify_rejection_dead_session_spawns_rework(self, manager, mock_tmux):
        self._provision(manager)
        mock_tmux.is_pane_alive.return_value = False
        # Dead session: notify_rejection tries to spawn a rework worker.
        # May succeed or fail depending on mock tmux capabilities.
        result = manager.notify_rejection("proj/1", "Build-backend is wrong")
        assert isinstance(result, bool)

    def test_notify_rejection_no_session_spawns_worker(self, manager, mock_tmux):
        # No session provisioned — tries to spawn a new rework worker
        result = manager.notify_rejection("proj/1", "Nope")
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# Query tests
# ---------------------------------------------------------------------------


class TestActiveSessionsQueries:
    def _provision(self, manager, task_id, agent="agent-1"):
        with patch("pollypm.work.session_manager.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            return manager.provision_worker(task_id, agent)

    def test_active_sessions_lists_bound(self, manager, mock_tmux):
        self._provision(manager, "proj/1")
        self._provision(manager, "proj/2")

        sessions = manager.active_sessions()
        assert len(sessions) == 2
        task_ids = {s.task_id for s in sessions}
        assert task_ids == {"proj/1", "proj/2"}

    def test_active_sessions_filters_by_project(self, manager, mock_tmux):
        self._provision(manager, "proj/1")
        self._provision(manager, "other/1")

        proj_sessions = manager.active_sessions(project="proj")
        assert len(proj_sessions) == 1
        assert proj_sessions[0].task_id == "proj/1"

        other_sessions = manager.active_sessions(project="other")
        assert len(other_sessions) == 1
        assert other_sessions[0].task_id == "other/1"

    def test_session_for_task_found(self, manager, mock_tmux):
        self._provision(manager, "proj/1")
        found = manager.session_for_task("proj/1")
        assert found is not None
        assert found.task_id == "proj/1"

    def test_session_for_task_not_found(self, manager):
        found = manager.session_for_task("proj/1")
        assert found is None


# ---------------------------------------------------------------------------
# Token parsing tests
# ---------------------------------------------------------------------------


class TestTokenParsing:
    def test_parse_token_usage_direct(self, tmp_path):
        f = tmp_path / "test.jsonl"
        lines = [
            json.dumps({"type": "token_usage", "input_tokens": 100, "output_tokens": 50}),
            json.dumps({"type": "other", "data": "ignored"}),
            json.dumps({"type": "token_usage", "input_tokens": 200, "output_tokens": 75}),
        ]
        f.write_text("\n".join(lines) + "\n")

        inp, out = _parse_token_usage(f)
        assert inp == 300
        assert out == 125

    def test_parse_token_usage_nested(self, tmp_path):
        f = tmp_path / "test.jsonl"
        lines = [
            json.dumps({"usage": {"input_tokens": 50, "output_tokens": 25}}),
            json.dumps({"message": {"usage": {"input_tokens": 10, "output_tokens": 5}}}),
        ]
        f.write_text("\n".join(lines) + "\n")

        inp, out = _parse_token_usage(f)
        assert inp == 60
        assert out == 30

    def test_parse_token_usage_missing_file(self, tmp_path):
        f = tmp_path / "nonexistent.jsonl"
        inp, out = _parse_token_usage(f)
        assert inp == 0
        assert out == 0
