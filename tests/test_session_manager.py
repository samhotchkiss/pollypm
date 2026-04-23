"""Tests for SessionManager — worker session lifecycle."""

from __future__ import annotations

import base64
import json
import shlex
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
)
from pollypm.models import (
    AccountConfig,
    PollyPMConfig,
    PollyPMSettings,
    ProjectSettings,
    ProviderKind,
    RuntimeKind,
)
from pollypm.work.models import WorkerSessionRecord


# Mirror of the schema the sqlite work service maintains, used by the
# minimal in-test work-service stub below.
WORK_SESSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS work_sessions (
    task_project TEXT NOT NULL,
    task_number INTEGER NOT NULL,
    agent_name TEXT NOT NULL,
    pane_id TEXT,
    worktree_path TEXT,
    branch_name TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    total_input_tokens INTEGER DEFAULT 0,
    total_output_tokens INTEGER DEFAULT 0,
    archive_path TEXT,
    PRIMARY KEY (task_project, task_number),
    FOREIGN KEY (task_project, task_number) REFERENCES work_tasks(project, task_number)
);
"""


class _StubWorkService:
    """Minimal work-service stand-in that backs the new worker-session
    protocol methods with a real sqlite connection. Tests inspect the
    connection via ``.conn`` to assert on persisted state."""

    def __init__(self, conn):
        self.conn = conn
        # Back-compat alias — a few tests still reference _conn directly.
        self._conn = conn

    def ensure_worker_session_schema(self):
        self.conn.executescript(WORK_SESSIONS_SCHEMA)

    def _row_to_record(self, row):
        if row is None:
            return None
        return WorkerSessionRecord(
            task_project=row["task_project"],
            task_number=int(row["task_number"]),
            agent_name=row["agent_name"],
            pane_id=row["pane_id"],
            worktree_path=row["worktree_path"],
            branch_name=row["branch_name"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            total_input_tokens=int(row["total_input_tokens"] or 0),
            total_output_tokens=int(row["total_output_tokens"] or 0),
            archive_path=row["archive_path"],
        )

    def upsert_worker_session(self, *, task_project, task_number, agent_name,
                              pane_id, worktree_path, branch_name, started_at):
        self.conn.execute(
            "INSERT INTO work_sessions "
            "(task_project, task_number, agent_name, pane_id, worktree_path, "
            "branch_name, started_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (task_project, task_number) DO UPDATE SET "
            "pane_id=excluded.pane_id, "
            "worktree_path=excluded.worktree_path, "
            "branch_name=excluded.branch_name, "
            "started_at=excluded.started_at, "
            "ended_at=NULL, "
            "archive_path=NULL, "
            "total_input_tokens=0, "
            "total_output_tokens=0",
            (task_project, task_number, agent_name, pane_id, worktree_path,
             branch_name, started_at),
        )
        self.conn.commit()

    def get_worker_session(self, *, task_project, task_number, active_only=False):
        if active_only:
            row = self.conn.execute(
                "SELECT * FROM work_sessions "
                "WHERE task_project = ? AND task_number = ? AND ended_at IS NULL",
                (task_project, task_number),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM work_sessions "
                "WHERE task_project = ? AND task_number = ?",
                (task_project, task_number),
            ).fetchone()
        return self._row_to_record(row)

    def list_worker_sessions(self, *, project=None, active_only=True):
        clauses = []
        params = []
        if active_only:
            clauses.append("ended_at IS NULL")
        if project is not None:
            clauses.append("task_project = ?")
            params.append(project)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT * FROM work_sessions{where}", tuple(params),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def end_worker_session(self, *, task_project, task_number, ended_at,
                           total_input_tokens, total_output_tokens, archive_path):
        self.conn.execute(
            "UPDATE work_sessions SET ended_at = ?, total_input_tokens = ?, "
            "total_output_tokens = ?, archive_path = ? "
            "WHERE task_project = ? AND task_number = ?",
            (ended_at, total_input_tokens, total_output_tokens, archive_path,
             task_project, task_number),
        )
        self.conn.commit()

    def update_worker_session_tokens(self, *, task_project, task_number,
                                     total_input_tokens, total_output_tokens,
                                     archive_path):
        self.conn.execute(
            "UPDATE work_sessions SET total_input_tokens = ?, "
            "total_output_tokens = ?, archive_path = ? "
            "WHERE task_project = ? AND task_number = ?",
            (total_input_tokens, total_output_tokens, archive_path,
             task_project, task_number),
        )
        self.conn.commit()

    # Stub for _build_task_prompt which calls self._svc.get(task_id).
    # Raise so the prompt builder falls through to its minimal fallback
    # (the tests don't care about prompt content).
    def get(self, task_id):
        raise RuntimeError("no task lookups in these tests")


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
    """A minimal work service backed by a real SQLite connection.

    Implements just the worker-session protocol methods SessionManager
    uses. Tests that need to inspect DB state can use ``svc._conn``.
    """
    return _StubWorkService(db_conn)


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


def _decode_local_runtime_payload(command: str) -> dict[str, object]:
    outer = shlex.split(command)
    assert outer[:2] == ["sh", "-lc"]
    inner = shlex.split(outer[2])
    payload = inner[-1]
    padded = payload + ("=" * (-len(payload) % 4))
    return json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))


def _worker_launch_config(
    tmp_project: Path,
    *,
    account_name: str,
    provider: ProviderKind,
    runtime: RuntimeKind = RuntimeKind.LOCAL,
    open_permissions_by_default: bool = False,
    home: Path | None = None,
) -> PollyPMConfig:
    base_dir = tmp_project / ".pollypm"
    return PollyPMConfig(
        project=ProjectSettings(
            name="Fixture",
            root_dir=tmp_project,
            tmux_session="pollypm",
            base_dir=base_dir,
            logs_dir=base_dir / "logs",
            snapshots_dir=base_dir / "snapshots",
            state_db=base_dir / "state.db",
        ),
        pollypm=PollyPMSettings(
            controller_account=account_name,
            open_permissions_by_default=open_permissions_by_default,
        ),
        accounts={
            account_name: AccountConfig(
                name=account_name,
                provider=provider,
                runtime=runtime,
                home=home,
            )
        },
        sessions={},
    )


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

    def test_provision_worker_acquires_session_lock(self, manager, mock_tmux, tmp_project):
        """provision_worker must acquire the per-task session lock so two
        concurrent claims on the same task can't both race git worktree add."""
        with patch("pollypm.work.session_manager.subprocess") as mock_sub, \
             patch("pollypm.projects.ensure_session_lock") as mock_ensure, \
             patch("pollypm.projects.release_session_lock") as mock_release:
            mock_sub.run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            manager.provision_worker("proj/1", "agent-1")

        assert mock_ensure.call_count == 1
        # lock is scoped to the task (session_id == "task-proj-1")
        args, _ = mock_ensure.call_args
        assert args[1] == "task-proj-1"
        # and released afterwards
        mock_release.assert_called_once()

    def test_provision_worker_returns_existing_on_lock_conflict(self, manager, mock_tmux, tmp_project, mock_svc):
        """When ensure_session_lock raises (another provision is holding
        the lock) but a session has meanwhile appeared in the DB, we
        should return the existing session rather than blowing up."""
        def conflict_then_seed(*args, **kwargs):
            # Simulate the other provision finishing between our
            # initial session_for_task() check and the lock acquisition.
            mock_svc._conn.execute(
                "INSERT INTO work_sessions (task_project, task_number, "
                "agent_name, pane_id, worktree_path, branch_name, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("proj", 1, "other-agent", "%99", str(tmp_project / "wt"),
                 "task/proj-1", "2024-01-01T00:00:00+00:00"),
            )
            mock_svc._conn.commit()
            raise RuntimeError("Session lock conflict")

        with patch(
            "pollypm.projects.ensure_session_lock",
            side_effect=conflict_then_seed,
        ):
            session = manager.provision_worker("proj/1", "agent-1")

        # Should have fallen back to the session the other provision created
        assert session.task_id == "proj/1"
        assert session.agent_name == "other-agent"

    def test_create_worktree_add_has_timeout(self, manager, mock_tmux, tmp_project):
        """git worktree add must be called with a timeout so a hung git
        op can't wedge claim()."""
        with patch("pollypm.work.session_manager.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            manager.provision_worker("proj/1", "agent-1")

        add_calls = [
            call for call in mock_sub.run.call_args_list
            if "worktree" in str(call) and "add" in str(call)
        ]
        assert add_calls, "expected at least one git worktree add call"
        for call in add_calls:
            kwargs = call.kwargs
            assert kwargs.get("timeout") == 300, (
                f"git worktree add call missing timeout=300: {call}"
            )

    def test_create_worktree_removes_dangling_dir(self, manager, mock_tmux, tmp_project):
        """If the worktree dir exists but isn't registered with git, the
        fast-path must drop it before running `git worktree add`."""
        # Create an unregistered directory at the target location.
        slug = "proj-1"
        dangling = tmp_project / ".pollypm" / "worktrees" / slug
        dangling.mkdir(parents=True)
        (dangling / "leftover.txt").write_text("junk")

        calls_log = []

        def fake_run(cmd, *args, **kwargs):
            calls_log.append(cmd)
            if "list" in cmd and "--porcelain" in cmd:
                # Report a registered worktree that is NOT this one.
                return MagicMock(
                    returncode=0,
                    stdout="worktree /somewhere/else\nHEAD abc\nbranch refs/heads/main\n\n",
                    stderr="",
                )
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("pollypm.work.session_manager.subprocess") as mock_sub:
            mock_sub.run.side_effect = fake_run
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired
            manager.provision_worker("proj/1", "agent-1")

        # `git worktree add` should have been invoked (not short-circuited
        # by the exists()-fast-path).
        add_calls = [c for c in calls_log if "add" in c and "worktree" in c]
        assert add_calls, "expected git worktree add to be called for the dangling dir case"

    def test_provision_worker_falls_back_to_new_window_when_session_exists(
        self, manager, mock_tmux, tmp_project,
    ):
        """#243: claim against an existing storage-closet session must
        succeed by routing through ``create_window`` rather than raising
        from a failed ``new-session``.

        Mirrors the E2E failure:
          has_session() → True
          → path should take the create_window branch.
        """
        # Storage-closet session already exists.
        mock_tmux.has_session.return_value = True

        with patch("pollypm.work.session_manager.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            session = manager.provision_worker("proj/1", "agent-1")

        # Should NOT have called create_session (would fail with "session
        # already exists" in real tmux).
        assert mock_tmux.create_session.call_count == 0
        # Should have gone through create_window instead.
        assert mock_tmux.create_window.call_count == 1
        # Task still bound.
        assert session.task_id == "proj/1"

    def test_provision_worker_falls_back_on_new_session_race(
        self, manager, mock_tmux, tmp_project,
    ):
        """#243: if ``has_session`` reports False but ``create_session``
        then fails (race/inconsistency) AND the session now exists,
        provision should swallow the error and use ``create_window``."""
        # has_session returns False the first time (decision point), then
        # True after create_session fails (race resolution).
        mock_tmux.has_session.side_effect = [False, True, True, True, True, True]

        def create_session_raises(*args, **kwargs):
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=["tmux", "new-session", "-d", "-s", "pollypm-storage-closet"],
                stderr="duplicate session: pollypm-storage-closet",
            )

        mock_tmux.create_session.side_effect = create_session_raises

        with patch("pollypm.work.session_manager.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            session = manager.provision_worker("proj/1", "agent-1")

        # Fallback path kicked in.
        assert mock_tmux.create_window.call_count == 1
        assert session.task_id == "proj/1"

    def test_provision_worker_raises_provision_error_on_true_failure(
        self, manager, mock_tmux, tmp_project,
    ):
        """#243: if tmux is actually broken (new-session fails AND
        has_session still reports False), surface a ProvisionError so
        ``pm task claim`` can show an actionable message instead of
        silently continuing."""
        from pollypm.work.session_manager import ProvisionError

        mock_tmux.has_session.return_value = False

        def create_session_raises(*args, **kwargs):
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=["tmux", "new-session"],
                stderr="server exited unexpectedly",
            )

        mock_tmux.create_session.side_effect = create_session_raises

        with patch("pollypm.work.session_manager.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            with pytest.raises(ProvisionError) as excinfo:
                manager.provision_worker("proj/1", "agent-1")

        # Error guidance should reference a recovery command (three-
        # question rule).
        msg = str(excinfo.value)
        assert "tmux" in msg
        assert "pm task release" in msg or "retry" in msg

    def test_provision_worker_raises_when_tmux_reports_no_windows(
        self, manager, mock_tmux, tmp_project,
    ):
        from pollypm.work.session_manager import ProvisionError

        mock_tmux.has_session.return_value = False
        mock_tmux.list_windows.return_value = []

        with patch("pollypm.work.session_manager.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            with pytest.raises(ProvisionError) as excinfo:
                manager.provision_worker("proj/1", "agent-1")

        msg = str(excinfo.value)
        assert "did not report a worker pane" in msg
        assert "pm task claim" in msg

    def test_provision_worker_reprovision_after_teardown(
        self, manager, mock_tmux, tmp_project,
    ):
        """After teardown stamps ended_at, a re-claim must succeed (not
        blow up on the primary-key constraint)."""
        with patch("pollypm.work.session_manager.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            manager.provision_worker("proj/1", "agent-1")
            manager.teardown_worker("proj/1")
            # Re-claim — previously hit PK constraint and was swallowed.
            s2 = manager.provision_worker("proj/1", "agent-2")

        assert s2.task_id == "proj/1"
        # Row is reused with ended_at cleared; the active session query
        # must return the freshly-upserted binding.
        found = manager.session_for_task("proj/1")
        assert found is not None
        assert found.task_id == "proj/1"

    def test_provision_worker_uses_runtime_wrapped_codex_launch(
        self, mock_tmux, mock_svc, tmp_project, tmp_path,
    ):
        mock_svc._conn.execute(
            "INSERT INTO work_tasks (project, task_number) VALUES (?, ?)",
            ("proj", 1),
        )
        mock_svc._conn.commit()
        session_service = MagicMock()
        session_service.create.return_value = MagicMock(pane_id="%9")
        config = _worker_launch_config(
            tmp_project,
            account_name="codex-main",
            provider=ProviderKind.CODEX,
            home=tmp_path / "codex-home",
        )
        manager = SessionManager(
            mock_tmux,
            mock_svc,
            tmp_project,
            config=config,
            session_service=session_service,
        )

        with patch("pollypm.work.session_manager.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            manager.provision_worker("proj/1", "worker")

        kwargs = session_service.create.call_args.kwargs
        assert kwargs["provider"] == "codex"
        assert kwargs["account"] == "codex-main"
        payload = _decode_local_runtime_payload(kwargs["command"])
        assert payload["argv"] == [
            "codex",
            "--sandbox",
            "workspace-write",
            "--ask-for-approval",
            "never",
            "--model",
            "gpt-5.4",
        ]

    def test_provision_worker_aborts_when_prompt_write_fails(
        self, manager, mock_tmux, tmp_project,
    ):
        from pollypm.work.session_manager import ProvisionError

        worktree_path = tmp_project / ".pollypm" / "worktrees" / "proj-1"
        worktree_path.mkdir(parents=True, exist_ok=True)

        with patch.object(manager, "_create_worktree", return_value=worktree_path), \
             patch.object(manager, "_remove_worktree", return_value=True) as mock_remove, \
             patch.object(manager, "_launch_worker_window") as mock_launch, \
             patch("pathlib.Path.write_text", side_effect=OSError("disk full")):
            with pytest.raises(ProvisionError) as excinfo:
                manager.provision_worker("proj/1", "agent-1")

        assert "worker prompt" in str(excinfo.value)
        assert "Cleanup succeeded" in str(excinfo.value)
        mock_remove.assert_called_once_with(worktree_path)
        mock_launch.assert_not_called()

    def test_provision_worker_honors_closed_permissions_for_claude(
        self, mock_tmux, mock_svc, tmp_project, tmp_path,
    ):
        mock_svc._conn.execute(
            "INSERT INTO work_tasks (project, task_number) VALUES (?, ?)",
            ("proj", 1),
        )
        mock_svc._conn.commit()
        session_service = MagicMock()
        session_service.create.return_value = MagicMock(pane_id="%9")
        config = _worker_launch_config(
            tmp_project,
            account_name="claude-main",
            provider=ProviderKind.CLAUDE,
            open_permissions_by_default=False,
            home=tmp_path / "claude-home",
        )
        manager = SessionManager(
            mock_tmux,
            mock_svc,
            tmp_project,
            config=config,
            session_service=session_service,
        )

        with patch("pollypm.work.session_manager.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            manager.provision_worker("proj/1", "worker")

        kwargs = session_service.create.call_args.kwargs
        assert kwargs["provider"] == "claude"
        payload = _decode_local_runtime_payload(kwargs["command"])
        assert payload["argv"] == ["claude"]


# ---------------------------------------------------------------------------
# Teardown tests
# ---------------------------------------------------------------------------


class TestTeardownWorker:
    def _provision(self, manager, task_id="proj/1"):
        with patch("pollypm.work.session_manager.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            return manager.provision_worker(task_id, "agent-1")

    def test_teardown_worker_archives_jsonl(self, manager, mock_tmux, tmp_project, tmp_path, monkeypatch):
        session = self._provision(manager)

        # Claude writes JSONL to $CLAUDE_CONFIG_DIR/projects/<encoded-cwd>/
        claude_home = tmp_path / "claude-home"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
        from pollypm.work.session_manager import _encode_claude_cwd
        encoded = _encode_claude_cwd(session.worktree_path.resolve())
        proj_dir = claude_home / "projects" / encoded
        proj_dir.mkdir(parents=True)
        jsonl_file = proj_dir / "session.jsonl"
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

    def test_teardown_worker_kills_window(self, manager, mock_tmux, tmp_project):
        self._provision(manager)

        with patch("pollypm.work.session_manager.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            manager.teardown_worker("proj/1")

        # Kill the window (not just the pane) so remain-on-exit=on can't
        # leave a stale window blocking re-provision.
        mock_tmux.kill_window.assert_called_once_with(
            "pollypm-storage-closet:task-proj-1"
        )

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

    def test_teardown_continues_on_archive_failure(self, manager, mock_tmux, tmp_project):
        """If _archive_jsonl blows up, teardown must still kill the
        window and attempt worktree removal."""
        self._provision(manager)

        with patch("pollypm.work.session_manager.subprocess") as mock_sub, \
             patch.object(manager, "_archive_jsonl", side_effect=RuntimeError("disk full")):
            mock_sub.run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            manager.teardown_worker("proj/1")

        mock_tmux.kill_window.assert_called_once()

    def test_teardown_leaves_ended_at_null_when_kill_fails(self, manager, mock_tmux, tmp_project, mock_svc):
        """If we fail to kill the pane/window, don't stamp ended_at —
        a future pass needs to be able to retry."""
        self._provision(manager)

        mock_tmux.kill_window.side_effect = RuntimeError("tmux is dead")

        with patch("pollypm.work.session_manager.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            manager.teardown_worker("proj/1")

        row = mock_svc._conn.execute(
            "SELECT ended_at FROM work_sessions WHERE task_project = ? AND task_number = ?",
            ("proj", 1),
        ).fetchone()
        assert row is not None
        assert row["ended_at"] is None, (
            "ended_at must remain NULL when the kill phase failed"
        )

    def test_teardown_worker_records_tokens(self, manager, mock_tmux, tmp_project, tmp_path, monkeypatch):
        session = self._provision(manager)

        # Create JSONL with token events in the Claude config dir
        claude_home = tmp_path / "claude-home"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
        from pollypm.work.session_manager import _encode_claude_cwd
        encoded = _encode_claude_cwd(session.worktree_path.resolve())
        proj_dir = claude_home / "projects" / encoded
        proj_dir.mkdir(parents=True)
        jsonl_file = proj_dir / "session.jsonl"
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


# ---------------------------------------------------------------------------
# Claude cwd encoding tests
# ---------------------------------------------------------------------------


class TestClaudeCwdEncoding:
    def test_encode_cwd_replaces_slash_and_dot(self):
        from pollypm.work.session_manager import _encode_claude_cwd

        encoded = _encode_claude_cwd(Path("/Users/sam/dev/foo/.pollypm/worktrees/foo-1"))
        assert encoded == "-Users-sam-dev-foo--pollypm-worktrees-foo-1"
