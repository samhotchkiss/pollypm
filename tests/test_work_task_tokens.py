"""Per-task token usage aggregated across worker sessions (#86).

Every task can spawn one or more worker sessions, each tracking
``total_input_tokens`` / ``total_output_tokens`` in the ``work_sessions``
table (populated by #150). This module verifies those counts roll up
into the ``Task`` view returned by ``WorkService.get`` / ``list_tasks``
and that the CLI surfaces them via ``pm task get`` / ``pm task list
--with-tokens``.

The token numbers are derived, not stored on ``work_tasks`` — every
read recomputes from ``work_sessions`` so a later re-claim that
resets the session row is visible immediately.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from pollypm.work.cli import task_app
from pollypm.work.mock_service import MockWorkService
from pollypm.work.sqlite_service import SQLiteWorkService


runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def svc(tmp_path):
    db_path = tmp_path / "work.db"
    svc = SQLiteWorkService(db_path=db_path)
    # Ensure the work_sessions schema is present — in production this is
    # invoked by SessionManager at construction.
    svc.ensure_worker_session_schema()
    return svc


def _make_task(svc, project="proj", title="T", roles=None):
    return svc.create(
        title=title,
        description="",
        type="task",
        project=project,
        flow_template="standard",
        roles=roles or {"worker": "agent-1", "reviewer": "agent-2"},
        priority="normal",
        created_by="tester",
    )


def _seed_worker_session(
    svc: SQLiteWorkService,
    *,
    project: str,
    task_number: int,
    tokens_in: int,
    tokens_out: int,
    agent_name: str = "worker",
    pane_id: str = "%1",
    worktree_path: str = "/tmp/wt",
    branch_name: str = "task/1",
    ended: bool = True,
) -> None:
    """Insert a work_sessions row with the given token counts.

    We write directly to the table because the schema holds exactly one
    row per task (PRIMARY KEY on ``(task_project, task_number)``). The
    public ``upsert_worker_session`` zeros the counters on insert, which
    is the wrong affordance for a test that wants to assert aggregation.
    """
    svc._conn.execute(
        "INSERT OR REPLACE INTO work_sessions "
        "(task_project, task_number, agent_name, pane_id, worktree_path, "
        "branch_name, started_at, ended_at, total_input_tokens, "
        "total_output_tokens, archive_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
        (
            project,
            task_number,
            agent_name,
            pane_id,
            worktree_path,
            branch_name,
            "2026-04-16T00:00:00+00:00",
            "2026-04-16T01:00:00+00:00" if ended else None,
            tokens_in,
            tokens_out,
        ),
    )
    svc._conn.commit()


# ---------------------------------------------------------------------------
# Task view aggregation
# ---------------------------------------------------------------------------


class TestTaskTokenAggregation:
    def test_task_without_sessions_reports_zero(self, svc):
        t = _make_task(svc, title="No sessions yet")
        got = svc.get(t.task_id)
        assert got.total_input_tokens == 0
        assert got.total_output_tokens == 0
        assert got.session_count == 0

    def test_task_view_surfaces_session_tokens(self, svc):
        t = _make_task(svc)
        _seed_worker_session(
            svc,
            project=t.project,
            task_number=t.task_number,
            tokens_in=1200,
            tokens_out=340,
        )
        got = svc.get(t.task_id)
        assert got.total_input_tokens == 1200
        assert got.total_output_tokens == 340
        assert got.session_count == 1

    def test_list_tasks_aggregates_per_task(self, svc):
        a = _make_task(svc, title="A")
        b = _make_task(svc, title="B")
        _seed_worker_session(
            svc, project=a.project, task_number=a.task_number,
            tokens_in=100, tokens_out=10,
        )
        _seed_worker_session(
            svc, project=b.project, task_number=b.task_number,
            tokens_in=500, tokens_out=50,
        )
        tasks = {t.task_id: t for t in svc.list_tasks()}
        assert tasks[a.task_id].total_input_tokens == 100
        assert tasks[a.task_id].total_output_tokens == 10
        assert tasks[a.task_id].session_count == 1
        assert tasks[b.task_id].total_input_tokens == 500
        assert tasks[b.task_id].total_output_tokens == 50
        assert tasks[b.task_id].session_count == 1

    def test_list_tasks_unrelated_sessions_dont_leak(self, svc):
        """A task's aggregate must exclude other tasks' session rows."""
        a = _make_task(svc, project="proj-a", title="A")
        b = _make_task(svc, project="proj-b", title="B")
        _seed_worker_session(
            svc, project=b.project, task_number=b.task_number,
            tokens_in=9999, tokens_out=9999,
        )
        a_view = svc.get(a.task_id)
        assert a_view.total_input_tokens == 0
        assert a_view.session_count == 0
        b_view = svc.get(b.task_id)
        assert b_view.total_input_tokens == 9999
        assert b_view.session_count == 1

    def test_bulk_loader_matches_per_task_lookup(self, svc):
        """The batch helper used by list_tasks and the single-task helper
        must agree on the aggregate for any given task."""
        t = _make_task(svc)
        _seed_worker_session(
            svc, project=t.project, task_number=t.task_number,
            tokens_in=42, tokens_out=7,
        )
        per_task = svc._load_task_token_sum(t.project, t.task_number)
        bulk = svc._load_task_token_sums_bulk()
        assert per_task == bulk[(t.project, t.task_number)]


class TestMockServiceTaskTokens:
    """The in-memory mock must satisfy the same contract so TUI tests
    built on it render identical token columns."""

    def test_mock_aggregates_tokens(self):
        mock = MockWorkService()
        t = mock.create(
            title="Mock task",
            description="",
            type="task",
            project="proj",
            flow_template="standard",
            roles={"worker": "agent-1", "reviewer": "agent-2"},
            priority="normal",
            created_by="tester",
        )
        mock.upsert_worker_session(
            task_project=t.project,
            task_number=t.task_number,
            agent_name="worker",
            pane_id="%1",
            worktree_path="/tmp/wt",
            branch_name="task/1",
            started_at="2026-04-16T00:00:00+00:00",
        )
        mock.end_worker_session(
            task_project=t.project,
            task_number=t.task_number,
            ended_at="2026-04-16T01:00:00+00:00",
            total_input_tokens=55,
            total_output_tokens=11,
            archive_path=None,
        )
        got = mock.get(t.task_id)
        assert got.total_input_tokens == 55
        assert got.total_output_tokens == 11
        assert got.session_count == 1
        listed = mock.list_tasks()[0]
        assert listed.total_input_tokens == 55
        assert listed.total_output_tokens == 11
        assert listed.session_count == 1


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def _cli_create(db_path: str, title: str = "T") -> None:
    res = runner.invoke(
        task_app,
        [
            "create", title,
            "--project", "proj",
            "--flow", "standard",
            "--priority", "normal",
            "--description", "",
            "--type", "task",
            "--role", "worker=agent-1",
            "--role", "reviewer=agent-2",
            "--db", db_path,
        ],
    )
    assert res.exit_code == 0, res.output


def _open_svc_and_seed(db_path: str, task_number: int, tokens_in: int, tokens_out: int) -> None:
    svc = SQLiteWorkService(db_path=db_path)
    try:
        svc.ensure_worker_session_schema()
        _seed_worker_session(
            svc,
            project="proj",
            task_number=task_number,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
    finally:
        svc.close()


class TestCliTokens:
    def test_task_get_shows_tokens_in_text(self, tmp_path):
        db_path = str(tmp_path / "db.sqlite")
        _cli_create(db_path)
        _open_svc_and_seed(db_path, 1, tokens_in=321, tokens_out=22)
        res = runner.invoke(task_app, ["get", "proj/1", "--db", db_path])
        assert res.exit_code == 0, res.output
        assert "Tokens:" in res.output
        assert "in=321" in res.output
        assert "out=22" in res.output
        assert "sessions=1" in res.output

    def test_task_get_json_includes_token_fields(self, tmp_path):
        db_path = str(tmp_path / "db.sqlite")
        _cli_create(db_path)
        _open_svc_and_seed(db_path, 1, tokens_in=100, tokens_out=25)
        res = runner.invoke(
            task_app, ["get", "proj/1", "--db", db_path, "--json"]
        )
        assert res.exit_code == 0, res.output
        data = json.loads(res.output)
        assert data["tokens_in"] == 100
        assert data["tokens_out"] == 25
        assert data["session_count"] == 1

    def test_task_list_without_flag_omits_token_columns(self, tmp_path):
        db_path = str(tmp_path / "db.sqlite")
        _cli_create(db_path, title="A")
        _open_svc_and_seed(db_path, 1, tokens_in=77, tokens_out=3)
        res = runner.invoke(task_app, ["list", "--db", db_path])
        assert res.exit_code == 0, res.output
        assert "TokIn" not in res.output
        # Default table doesn't leak raw token numbers.
        assert "77" not in res.output

    def test_task_list_with_tokens_flag_shows_columns(self, tmp_path):
        db_path = str(tmp_path / "db.sqlite")
        _cli_create(db_path, title="A")
        _cli_create(db_path, title="B")
        _open_svc_and_seed(db_path, 1, tokens_in=77, tokens_out=3)
        _open_svc_and_seed(db_path, 2, tokens_in=200, tokens_out=40)
        res = runner.invoke(
            task_app, ["list", "--db", db_path, "--with-tokens"]
        )
        assert res.exit_code == 0, res.output
        assert "TokIn" in res.output
        assert "TokOut" in res.output
        assert "Sess" in res.output
        assert "77" in res.output
        assert "200" in res.output

    def test_task_list_json_includes_tokens(self, tmp_path):
        db_path = str(tmp_path / "db.sqlite")
        _cli_create(db_path, title="A")
        _open_svc_and_seed(db_path, 1, tokens_in=9, tokens_out=1)
        res = runner.invoke(
            task_app, ["list", "--db", db_path, "--json"]
        )
        assert res.exit_code == 0, res.output
        data = json.loads(res.output)
        assert len(data) == 1
        assert data[0]["tokens_in"] == 9
        assert data[0]["tokens_out"] == 1
        assert data[0]["session_count"] == 1
