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
    provider TEXT,
    provider_home TEXT,
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

        def _safe(key):
            try:
                return row[key]
            except (IndexError, KeyError):
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
            provider=_safe("provider"),
            provider_home=_safe("provider_home"),
        )

    def upsert_worker_session(self, *, task_project, task_number, agent_name,
                              pane_id, worktree_path, branch_name, started_at,
                              provider=None, provider_home=None):
        # Stub mirrors the real upsert (see ``service_worker_sessions``)
        # — the real one carries provider/provider_home (#809) and as of
        # #1014 (Bug B) preserves token counters across re-claim cycles
        # (the row's PK is ``(task_project, task_number)`` so a fresh
        # provision is the same row replayed; zeroing counters here was
        # the silent cause of ``pm task get`` showing ``in=0 out=0`` on
        # bikepath/8 despite real spend).
        self.conn.execute(
            "INSERT INTO work_sessions "
            "(task_project, task_number, agent_name, pane_id, worktree_path, "
            "branch_name, started_at, provider, provider_home) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (task_project, task_number) DO UPDATE SET "
            "pane_id=excluded.pane_id, "
            "worktree_path=excluded.worktree_path, "
            "branch_name=excluded.branch_name, "
            "started_at=excluded.started_at, "
            "provider=excluded.provider, "
            "provider_home=excluded.provider_home, "
            "ended_at=NULL, "
            "archive_path=NULL",
            (task_project, task_number, agent_name, pane_id, worktree_path,
             branch_name, started_at, provider, provider_home),
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

    def mark_worker_session_ended(self, *, task_project, task_number, ended_at):
        """Stamp ``ended_at`` only — preserves token counters (#1014)."""
        self.conn.execute(
            "UPDATE work_sessions SET ended_at = ? "
            "WHERE task_project = ? AND task_number = ?",
            (ended_at, task_project, task_number),
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


def test_build_task_prompt_points_at_builtin_worker_guide(manager, tmp_project) -> None:
    prompt = manager._build_task_prompt(
        "proj/1",
        tmp_project / ".pollypm" / "worktrees" / "proj-1",
    )

    assert "## Worker Guide" in prompt
    # The default kickoff routes through `pm help worker` (which prints
    # the playbook from a packaged resource) instead of pointing at
    # docs/worker-guide.md by path. Workers spawn in worktrees where
    # docs/ is rarely checked out — the savethenovel forensic showed
    # this contributed to a 37s thrash on the missing path.
    assert "pm help worker" in prompt
    assert "docs/worker-guide.md" not in prompt


def test_build_task_prompt_prefers_project_local_worker_guide(manager, tmp_project) -> None:
    guide_path = tmp_project / ".pollypm" / "project-guides" / "worker.md"
    guide_path.parent.mkdir(parents=True, exist_ok=True)
    guide_path.write_text("---\nforked_from: test-sha\n---\n\n# Custom worker guide\n")

    prompt = manager._build_task_prompt(
        "proj/1",
        tmp_project / ".pollypm" / "worktrees" / "proj-1",
    )

    assert str(guide_path.resolve()) in prompt
    assert "docs/worker-guide.md" not in prompt
    # Project-local override path still names `pm help worker` as the
    # built-in fallback in case the local file is missing.
    assert "pm help worker" in prompt


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

    def test_provision_worker_reaps_dead_existing_session(
        self, manager, mock_tmux, mock_svc, tmp_project,
    ):
        """#1012: when an existing ``work_sessions`` row points at a tmux
        pane that's no longer alive, ``provision_worker`` must drop the
        orphan and re-provision instead of returning the dead row.

        Pre-fix the auto-claim sweep accumulated 60+ ``build vN
        abandoned`` rows on bikepath/8 because each ``provision_worker``
        call short-circuited on the orphan record and never spawned a
        new tmux window.
        """
        with patch("pollypm.work.session_manager.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stderr="", stdout="")

            # First claim: builds the worker_sessions row + tmux window.
            first = manager.provision_worker("proj/1", "agent-1")
            assert first.pane_id == "%1"
            create_session_calls_before = mock_tmux.create_session.call_count

            # Simulate the bikepath/8 failure mode: tmux window was
            # killed externally but the work_sessions row still has
            # ``ended_at IS NULL``.
            mock_tmux.is_pane_alive.return_value = False

            # Re-provision must reap the dead row and spawn a new window
            # rather than silently returning the orphan.
            second = manager.provision_worker("proj/1", "agent-1")

        assert second.task_id == "proj/1"
        # tmux create-session/window must have run AGAIN — the orphan was
        # not silently honoured.
        assert (
            mock_tmux.create_session.call_count
            + mock_tmux.create_window.call_count
            > create_session_calls_before
        ), "expected fresh tmux launch after dead-pane reap (#1012)"

    def test_provision_worker_preserves_tokens_across_reclaim(
        self, manager, mock_tmux, mock_svc, tmp_project,
    ):
        """#1014 (Bug B): re-provisioning a worker must not zero the
        token counters that an earlier session's teardown wrote.

        Pre-fix the SQLite ``upsert_worker_session`` reset
        ``total_input_tokens`` / ``total_output_tokens`` to 0 on
        ``ON CONFLICT``. The schema's PK is ``(task_project,
        task_number)`` so every re-claim of the same task hits that
        ON CONFLICT path — meaning the running token tally surfaced by
        ``pm task get`` was always the *current* in-flight session's
        contribution, not the cumulative spend across cycles. On
        ``bikepath/8`` the user saw ``in=0 out=0`` despite obvious
        token spend across multiple build / review handoffs.
        """
        with patch("pollypm.work.session_manager.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stderr="", stdout="")

            manager.provision_worker("proj/1", "agent-1")

            # Simulate teardown writing tokens. We bypass teardown_worker's
            # archival path (which depends on JSONL files on disk) and just
            # call end_worker_session directly to model the post-archive
            # state.
            mock_svc.end_worker_session(
                task_project="proj",
                task_number=1,
                ended_at="2026-04-30T00:00:00+00:00",
                total_input_tokens=5000,
                total_output_tokens=1500,
                archive_path="/tmp/archive",
            )

            # Re-provision (e.g. a fresh claim after rejection bounces
            # the task back through the queue).
            manager.provision_worker("proj/1", "agent-1")

        row = mock_svc._conn.execute(
            "SELECT total_input_tokens, total_output_tokens "
            "FROM work_sessions WHERE task_project = ? AND task_number = ?",
            ("proj", 1),
        ).fetchone()
        assert row is not None
        assert row["total_input_tokens"] == 5000, (
            "re-provision must NOT zero the input-token counter (#1014)"
        )
        assert row["total_output_tokens"] == 1500, (
            "re-provision must NOT zero the output-token counter (#1014)"
        )

    def test_orphan_reap_preserves_token_counters(
        self, manager, mock_tmux, mock_svc, tmp_project,
    ):
        """#1014 (Bug B): the dead-pane reap path must not zero the
        token counters either.

        The #1012 reap helper called ``end_worker_session(...,
        total_input_tokens=0, total_output_tokens=0, ...)``, which in
        the live ``bikepath/8`` repro silently wiped any prior
        archival tokens every time the supervisor resurrected a
        crashed pane. Switching to ``mark_worker_session_ended``
        (added in #1014) keeps the counters untouched on reap; only
        the canonical ``teardown_worker`` archive scan ever overwrites
        them.
        """
        with patch("pollypm.work.session_manager.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stderr="", stdout="")

            manager.provision_worker("proj/1", "agent-1")
            mock_svc.end_worker_session(
                task_project="proj",
                task_number=1,
                ended_at="2026-04-30T00:00:00+00:00",
                total_input_tokens=4200,
                total_output_tokens=900,
                archive_path="/tmp/archive",
            )
            # Resurrect the row so it looks like an active orphan
            # (ended_at IS NULL with a dead pane). We toggle ended_at
            # back to NULL via the SQLite stub directly because the
            # public API doesn't expose a "reopen" path.
            mock_svc._conn.execute(
                "UPDATE work_sessions SET ended_at = NULL "
                "WHERE task_project = ? AND task_number = ?",
                ("proj", 1),
            )
            mock_svc._conn.commit()

            # Pane is now reported dead; provision_worker should reap.
            mock_tmux.is_pane_alive.return_value = False
            manager.provision_worker("proj/1", "agent-1")

        row = mock_svc._conn.execute(
            "SELECT total_input_tokens, total_output_tokens "
            "FROM work_sessions WHERE task_project = ? AND task_number = ?",
            ("proj", 1),
        ).fetchone()
        assert row is not None
        assert row["total_input_tokens"] == 4200, (
            "orphan reap must NOT zero token counters (#1014 Bug B)"
        )
        assert row["total_output_tokens"] == 900

    def test_provision_worker_returns_existing_when_pane_still_alive(
        self, manager, mock_tmux, tmp_project,
    ):
        """#1012 negative: a live pane keeps the legacy idempotent
        behaviour. The dead-pane reap must not destabilise the common
        re-claim path where the worker is still up and running.
        """
        with patch("pollypm.work.session_manager.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stderr="", stdout="")

            first = manager.provision_worker("proj/1", "agent-1")
            mock_tmux.is_pane_alive.return_value = True
            second = manager.provision_worker("proj/1", "agent-1")

        assert first.pane_id == second.pane_id
        # create_session ran exactly once — the second call short-circuited.
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
        argv = payload["argv"]
        # #965 — argv[0] is resolved to an absolute path before
        # serialization into the runtime_launcher payload, so the
        # launcher's ``os.execvpe`` doesn't have to search a sanitized
        # child PATH. Basename remains ``codex``.
        from pathlib import Path as _P
        assert _P(argv[0]).name == "codex"
        assert argv[1:] == [
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
        argv = payload["argv"]
        # #965 — see neighboring test; argv[0] is now an absolute path.
        from pathlib import Path as _P
        assert _P(argv[0]).name == "claude"
        assert argv[1:] == []


class TestWorkerLaunchBundleAccountFilter:
    """Regression tests for #1437 (Bug B): per-task worker spawn must
    skip accounts whose ``account_runtime.status`` marks them
    auth_broken/exhausted/etc., or the relaunch after an auth-block
    just lands back on the same wedged account."""

    def _multi_account_config(
        self, tmp_project: Path, *, primary_home: Path, fallback_home: Path
    ) -> PollyPMConfig:
        base_dir = tmp_project / ".pollypm"
        base_dir.mkdir(parents=True, exist_ok=True)
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
                controller_account="claude_main",
                failover_accounts=["claude_backup"],
                failover_enabled=True,
            ),
            accounts={
                "claude_main": AccountConfig(
                    name="claude_main",
                    provider=ProviderKind.CLAUDE,
                    runtime=RuntimeKind.LOCAL,
                    home=primary_home,
                ),
                "claude_backup": AccountConfig(
                    name="claude_backup",
                    provider=ProviderKind.CLAUDE,
                    runtime=RuntimeKind.LOCAL,
                    home=fallback_home,
                ),
            },
            sessions={},
        )

    def test_worker_launch_bundle_skips_auth_broken_primary(
        self, mock_tmux, mock_svc, tmp_project, tmp_path,
    ):
        """When the primary account has runtime status ``auth_broken``
        (the canonical underscore form supervisor + heartbeat writers
        emit), ``_worker_launch_bundle`` must pick the configured
        failover account instead. Pre-#1437 the primary was selected
        unconditionally, so a heartbeat-marked auth break could not
        clear without manual ``pm reset``."""
        from pollypm.storage.state import StateStore

        primary_home = tmp_path / "claude-main-home"
        fallback_home = tmp_path / "claude-backup-home"
        config = self._multi_account_config(
            tmp_project, primary_home=primary_home, fallback_home=fallback_home,
        )

        # Mark the primary account auth_broken in the project state DB.
        with StateStore(config.project.state_db) as store:
            store.upsert_account_runtime(
                account_name="claude_main",
                provider=ProviderKind.CLAUDE.value,
                status="auth_broken",
                reason="live session reported authentication failure",
            )

        manager = SessionManager(
            mock_tmux,
            mock_svc,
            tmp_project,
            config=config,
            session_service=MagicMock(),
        )

        worktree_path = tmp_project / ".pollypm" / "worktrees" / "proj-1"
        worktree_path.mkdir(parents=True, exist_ok=True)
        (
            _command,
            _provider_name,
            account_name,
            _fresh_marker,
            _provider_home,
        ) = manager._worker_launch_bundle(
            worktree_path=worktree_path,
            agent_name="claude_main",
            window_name="task-proj-1",
            project="proj",
        )

        assert account_name == "claude_backup", (
            "Worker spawn must avoid the auth_broken primary and pick the "
            f"failover; got {account_name!r}"
        )

    def test_worker_launch_bundle_accepts_legacy_hyphen_form(
        self, mock_tmux, mock_svc, tmp_project, tmp_path,
    ):
        """Legacy rows (or UI writes) may still use ``"auth-broken"``
        with a hyphen — the filter must skip them too."""
        from pollypm.storage.state import StateStore

        primary_home = tmp_path / "claude-main-home"
        fallback_home = tmp_path / "claude-backup-home"
        config = self._multi_account_config(
            tmp_project, primary_home=primary_home, fallback_home=fallback_home,
        )

        with StateStore(config.project.state_db) as store:
            store.upsert_account_runtime(
                account_name="claude_main",
                provider=ProviderKind.CLAUDE.value,
                status="auth-broken",
                reason="legacy hyphen form",
            )

        manager = SessionManager(
            mock_tmux,
            mock_svc,
            tmp_project,
            config=config,
            session_service=MagicMock(),
        )

        worktree_path = tmp_project / ".pollypm" / "worktrees" / "proj-1"
        worktree_path.mkdir(parents=True, exist_ok=True)
        (_cmd, _prov, account_name, _fresh, _home) = manager._worker_launch_bundle(
            worktree_path=worktree_path,
            agent_name="claude_main",
            window_name="task-proj-1",
            project="proj",
        )

        assert account_name == "claude_backup"

    def test_worker_launch_bundle_keeps_healthy_primary(
        self, mock_tmux, mock_svc, tmp_project, tmp_path,
    ):
        """Sanity check: when no account is marked auth_broken, the
        existing preference order still wins (primary keeps the slot)."""
        primary_home = tmp_path / "claude-main-home"
        fallback_home = tmp_path / "claude-backup-home"
        config = self._multi_account_config(
            tmp_project, primary_home=primary_home, fallback_home=fallback_home,
        )

        manager = SessionManager(
            mock_tmux,
            mock_svc,
            tmp_project,
            config=config,
            session_service=MagicMock(),
        )

        worktree_path = tmp_project / ".pollypm" / "worktrees" / "proj-1"
        worktree_path.mkdir(parents=True, exist_ok=True)
        (_cmd, _prov, account_name, _fresh, _home) = manager._worker_launch_bundle(
            worktree_path=worktree_path,
            agent_name="claude_main",
            window_name="task-proj-1",
            project="proj",
        )

        assert account_name == "claude_main"


# ---------------------------------------------------------------------------
# Teardown tests
# ---------------------------------------------------------------------------


class TestTeardownWorker:
    def _provision(self, manager, task_id="proj/1"):
        with patch("pollypm.work.session_manager.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            return manager.provision_worker(task_id, "agent-1")

    def test_archival_uses_persisted_provider_home_over_ambient_env(
        self, manager, mock_tmux, tmp_project, tmp_path, monkeypatch,
    ):
        """#809: per-task transcript archival must locate the right
        Claude tree even when the supervisor process's ambient
        ``CLAUDE_CONFIG_DIR`` differs from the worker's account-isolated
        home. Pre-#809 the archive scanned ``os.environ`` directly, so
        an isolated Claude account's transcripts and token totals were
        silently lost on teardown.

        Drive the persisted-home path directly: the launcher writes
        ``provider`` + ``provider_home`` onto the worker session row
        at upsert time; teardown reads them back.
        """
        from pollypm.work.session_manager import _encode_claude_cwd

        session = self._provision(manager)

        # Two different homes: ambient (process env) vs the account
        # the worker actually launched under.
        ambient_home = tmp_path / "ambient-home"
        account_home = tmp_path / "account-home"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(ambient_home))

        encoded = _encode_claude_cwd(session.worktree_path.resolve())
        # Plant the JSONL only under the account-isolated home.
        proj_dir = account_home / ".claude" / "projects" / encoded
        proj_dir.mkdir(parents=True)
        jsonl_file = proj_dir / "session.jsonl"
        jsonl_file.write_text(
            '{"type": "token_usage", "input_tokens": 11, "output_tokens": 7}\n'
        )

        # Re-upsert through the work-service stub so the row carries
        # provider/provider_home — emulates what the post-#809
        # SessionManager records at provision time.
        manager._svc.upsert_worker_session(
            task_project="proj",
            task_number=1,
            agent_name="agent-1",
            pane_id=session.pane_id or "%0",
            worktree_path=str(session.worktree_path),
            branch_name=session.branch_name,
            started_at=session.started_at.isoformat(),
            provider="claude",
            provider_home=str(account_home),
        )

        with patch("pollypm.work.session_manager.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            result = manager.teardown_worker("proj/1")

        assert result.jsonl_archived is True
        assert result.total_input_tokens == 11
        assert result.total_output_tokens == 7

    def test_codex_token_parser_handles_total_tokens_and_total_token_usage(
        self, tmp_path,
    ):
        """#814: ``_extract_token_usage`` must not drop Codex rows that
        only carry ``last_token_usage.total_tokens`` (no split) or that
        only have ``total_token_usage`` (no ``last_token_usage`` at
        all). Pre-fix the parser returned ``(0, 0)`` for both shapes,
        leaving ``work_sessions.total_input_tokens`` /
        ``total_output_tokens`` at zero on teardown — so cockpit token
        summaries under-reported every Codex worker.

        Contract (mirrors ``transcript_ledger`` / ``transcript_ingest``):

        * Split counters in ``last_token_usage`` win.
        * If ``last_token_usage`` is missing, fall back to
          ``total_token_usage``.
        * If only ``total_tokens`` is available (no split), attribute
          the whole value to ``output_tokens`` — the schema only has
          input + output columns today and dropping the value is the
          worse failure for cost dashboards.
        """
        from pollypm.work.session_manager import _extract_token_usage

        # 1) Per-turn ``last_token_usage`` with only ``total_tokens``.
        last_total_only = {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"last_token_usage": {"total_tokens": 21}},
            },
        }
        assert _extract_token_usage(last_total_only) == (0, 21)

        # 2) Only ``total_token_usage`` (cumulative snapshot, split
        #    counters present, no ``last_token_usage``).
        total_usage_only = {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 300,
                        "cached_input_tokens": 20,
                        "output_tokens": 30,
                        "reasoning_output_tokens": 5,
                        "total_tokens": 355,
                    },
                },
            },
        }
        assert _extract_token_usage(total_usage_only) == (320, 35)

        # 3) Only ``total_token_usage.total_tokens`` (no split).
        total_total_only = {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"total_tokens": 99}},
            },
        }
        assert _extract_token_usage(total_total_only) == (0, 99)

        # 4) Pre-#814 happy path — split ``last_token_usage`` still wins.
        last_split = {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 300,
                        "cached_input_tokens": 20,
                        "output_tokens": 30,
                        "reasoning_output_tokens": 5,
                    },
                },
            },
        }
        assert _extract_token_usage(last_split) == (320, 35)

        # 5) Both ``last`` and ``total`` present — ``last`` wins
        #    (per-turn delta is the canonical contribution).
        both = {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 5,
                        "output_tokens": 7,
                    },
                    "total_token_usage": {
                        "input_tokens": 999,
                        "output_tokens": 999,
                    },
                },
            },
        }
        assert _extract_token_usage(both) == (5, 7)

        # 6) Genuinely empty ``info`` still returns (0, 0).
        empty = {
            "type": "event_msg",
            "payload": {"type": "token_count", "info": {}},
        }
        assert _extract_token_usage(empty) == (0, 0)

    def test_codex_archival_filters_rollouts_to_task_worktree(
        self, manager, mock_tmux, tmp_project, tmp_path,
    ):
        """#812: Codex stores every rollout under
        ``CODEX_HOME/.codex/sessions/...`` regardless of cwd, so a
        single account home contains rollouts for every task that
        ran under that account. The archival finder must filter to
        rollouts whose ``session_meta`` / ``turn_context`` cwd
        matches this task's worktree — otherwise per-task archives
        get contaminated with unrelated transcripts.

        Plus #813: Codex token data lives in ``event_msg`` rows with
        ``payload.type=="token_count"`` and counters under
        ``payload.info.last_token_usage``. The Claude-shaped parser
        returned ``(0, 0)``; the new parser sums Codex per-turn
        deltas (input + cached_input, output + reasoning_output).
        """
        import json as _json

        session = self._provision(manager)
        worktree_cwd = str(session.worktree_path.resolve())
        other_cwd = str((tmp_path / "other-task-worktree").resolve())

        codex_home = tmp_path / "codex-account"
        sessions_root = codex_home / ".codex" / "sessions" / "2026" / "04" / "26"
        sessions_root.mkdir(parents=True)

        matching = sessions_root / "rollout-matching.jsonl"
        with matching.open("w") as fh:
            fh.write(_json.dumps({
                "type": "session_meta",
                "payload": {"id": "match", "cwd": worktree_cwd},
            }) + "\n")
            fh.write(_json.dumps({
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 300,
                            "cached_input_tokens": 20,
                            "output_tokens": 30,
                            "reasoning_output_tokens": 5,
                        },
                    },
                },
            }) + "\n")

        unrelated = sessions_root / "rollout-other.jsonl"
        with unrelated.open("w") as fh:
            fh.write(_json.dumps({
                "type": "session_meta",
                "payload": {"id": "other", "cwd": other_cwd},
            }) + "\n")
            fh.write(_json.dumps({
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 9999,
                            "output_tokens": 9999,
                        },
                    },
                },
            }) + "\n")

        manager._svc.upsert_worker_session(
            task_project="proj",
            task_number=1,
            agent_name="agent-1",
            pane_id=session.pane_id or "%0",
            worktree_path=str(session.worktree_path),
            branch_name=session.branch_name,
            started_at=session.started_at.isoformat(),
            provider="codex",
            provider_home=str(codex_home),
        )

        with patch("pollypm.work.session_manager.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            result = manager.teardown_worker("proj/1")

        assert result.jsonl_archived is True
        # Only the matching rollout's tokens count: 300+20=320 in,
        # 30+5=35 out. The unrelated rollout's 9999/9999 must not
        # contribute.
        assert result.total_input_tokens == 320
        assert result.total_output_tokens == 35
        # Archive contains only the matching file. The archival
        # destination is rooted at the project path (not the
        # worktree), under ``.pollypm/transcripts/tasks/<task_id>/``.
        archive_dir = (
            tmp_project / ".pollypm" / "transcripts" / "tasks" / "proj" / "1"
        )
        archived_names = sorted(p.name for p in archive_dir.glob("*.jsonl"))
        assert archived_names == ["rollout-matching.jsonl"]

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
