"""Tests for the notification volume-tiering backend.

Covers the three tiers (`immediate` / `digest` / `silent`), the keyword
classifier, milestone-boundary rollup flushing, regression detection on
re-open after a flushed rollup, the project-idle fallback, and the
30-day staging prune. UI wiring (inbox rollup expansion, jump-to-PM) is
out of scope — this file only exercises the backend.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pollypm import notification_staging as ns
from pollypm.cli import app as root_app
from pollypm.plugins_builtin.core_recurring.plugin import (
    notification_staging_prune_handler,
)
from pollypm.work.models import WorkStatus
from pollypm.work.sqlite_service import SQLiteWorkService


runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Return a fresh SQLite path for the work service."""
    return tmp_path / "state.db"


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """Return a project root containing a ``.pollypm/state.db`` subdir."""
    root = tmp_path / "proj"
    (root / ".pollypm").mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def svc(project_root: Path) -> SQLiteWorkService:
    """Work-service bound to a project root so milestone detection runs."""
    db = project_root / ".pollypm" / "state.db"
    service = SQLiteWorkService(db_path=db, project_path=project_root)
    yield service
    service.close()


def _invoke_notify(db: str, *args: str, input_text: str | None = None):
    """Invoke ``pm notify`` with the given args against ``db``."""
    return runner.invoke(
        root_app,
        ["notify", *args, "--db", db],
        input=input_text,
    )


def _staging_rows(db: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM notification_staging ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return list(rows)


# ---------------------------------------------------------------------------
# Keyword classifier
# ---------------------------------------------------------------------------


class TestClassifyPriority:
    @pytest.mark.parametrize(
        "subject, body",
        [
            ("Deploy blocker", "verification email needed"),
            ("Question", "which provider do we target?"),
            ("PR rejected", "needs a retest"),
            ("Needs decision", "pick the backend"),
            ("Stuck on auth flow", "no tokens coming back"),
            ("Migration failed", "exit 1"),
            ("Persona swap requested", "from reviewer to pm"),
        ],
    )
    def test_immediate_triggers(self, subject, body):
        assert ns.classify_priority(subject, body) == "immediate"

    @pytest.mark.parametrize(
        "subject, body",
        [
            ("Done: homepage rewrite", "Review at link"),
            ("Shipped the redesign", ""),
            ("PR merged", "cleanup will follow"),
            ("Approved", "green light"),
            ("Task completed", "no notes"),
        ],
    )
    def test_digest_triggers(self, subject, body):
        assert ns.classify_priority(subject, body) == "digest"

    @pytest.mark.parametrize(
        "subject, body",
        [
            ("Test pass", "all green"),
            ("Audit", "routine log"),
            ("Recorded", "event emitted"),
        ],
    )
    def test_silent_triggers(self, subject, body):
        assert ns.classify_priority(subject, body) == "silent"

    def test_ambiguous_defaults_to_immediate(self):
        # Neither "hello" nor "status update" match any keyword.
        assert ns.classify_priority("hello", "status update") == "immediate"

    def test_immediate_beats_digest(self):
        # "blocker" wins over "done" so we never swallow urgent work.
        assert (
            ns.classify_priority("Done but blocker", "cannot merge yet")
            == "immediate"
        )

    def test_silent_beats_digest(self):
        # "test pass" audit should stay silent even if body has "done".
        assert (
            ns.classify_priority("test pass", "rollout is done")
            == "silent"
        )


# ---------------------------------------------------------------------------
# pm notify CLI — tier routing
# ---------------------------------------------------------------------------


class TestNotifyCLITiers:
    def test_default_priority_creates_inbox_item(self, db_path):
        result = _invoke_notify(
            str(db_path), "Deploy blocked", "Needs verification email click.",
        )
        assert result.exit_code == 0, result.output
        # Legacy shape: task_id printed, project/number form.
        task_id = result.output.strip().splitlines()[-1]
        assert "/" in task_id
        # Still creates a work-service task (backwards compat).
        svc = SQLiteWorkService(db_path=db_path)
        try:
            task = svc.get(task_id)
            assert task.title == "Deploy blocked"
            assert task.roles.get("requester") == "user"
        finally:
            svc.close()

    def test_digest_priority_does_not_create_inbox_item(self, db_path):
        result = _invoke_notify(
            str(db_path),
            "Done: homepage rewrite",
            "Review at https://…",
            "--priority", "digest",
        )
        assert result.exit_code == 0, result.output
        out = result.output.strip().splitlines()[-1]
        assert out.startswith("digest:"), out

        # No inbox task; one staging row with priority=digest.
        svc = SQLiteWorkService(db_path=db_path)
        try:
            tasks = svc.list_tasks()
            assert tasks == []
        finally:
            svc.close()

        rows = _staging_rows(db_path)
        assert len(rows) == 1
        assert rows[0]["priority"] == "digest"
        assert rows[0]["subject"] == "Done: homepage rewrite"
        assert rows[0]["flushed_at"] is None

    def test_silent_priority_creates_neither(self, db_path):
        result = _invoke_notify(
            str(db_path),
            "Test pass",
            "all green",
            "--priority", "silent",
        )
        assert result.exit_code == 0, result.output
        out = result.output.strip().splitlines()[-1]
        assert out == "silent"

        svc = SQLiteWorkService(db_path=db_path)
        try:
            assert svc.list_tasks() == []
        finally:
            svc.close()

        # No staging rows for silent — the audit event is the whole record.
        assert _staging_rows(db_path) == []

        # Silent must still emit an activity event so the feed projector
        # sees the audit entry.
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT event_type, message FROM events "
                "WHERE event_type = 'inbox.message.silent'"
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1

    def test_auto_priority_infers_digest(self, db_path):
        # --priority omitted → classify_priority picks "digest" from "done".
        result = _invoke_notify(
            str(db_path),
            "Done: sprint wrap",
            "All tickets closed.",
        )
        assert result.exit_code == 0, result.output
        out = result.output.strip().splitlines()[-1]
        assert out.startswith("digest:"), out

        # No inbox task — auto-classified as digest.
        svc = SQLiteWorkService(db_path=db_path)
        try:
            assert svc.list_tasks() == []
        finally:
            svc.close()

    def test_auto_priority_infers_silent(self, db_path):
        result = _invoke_notify(
            str(db_path),
            "Test pass — suite green",
            "all 2240 passed",
        )
        assert result.exit_code == 0, result.output
        assert result.output.strip().splitlines()[-1] == "silent"


# ---------------------------------------------------------------------------
# flush_milestone_digest
# ---------------------------------------------------------------------------


class TestFlushMilestoneDigest:
    def test_empty_staging_is_noop(self, svc):
        result = ns.flush_milestone_digest(
            svc,
            project="demo",
            milestone_key="milestones/01-init",
            project_path=svc._project_path,
        )
        assert result is None
        # No tasks created.
        assert svc.list_tasks(project="demo") == []

    def test_flush_creates_one_rollup_and_marks_staged(self, svc):
        # Stage three digest rows for the same milestone.
        for i in range(3):
            ns.stage_notification(
                svc._conn,
                project="demo",
                subject=f"Task {i} done",
                body=f"PR #{i} merged.",
                actor="polly",
                priority="digest",
                milestone_key="milestones/01-init",
                payload={"pr": f"#{100 + i}"},
            )

        task_id = ns.flush_milestone_digest(
            svc,
            project="demo",
            milestone_key="milestones/01-init",
            project_path=svc._project_path,
        )
        assert task_id is not None and "/" in task_id

        task = svc.get(task_id)
        assert "3 updates" in task.title
        assert "Milestone 01" in task.title or "Milestone 1" in task.title
        # Rollup task surfaces in the inbox (roles requester=user).
        assert task.roles.get("requester") == "user"
        # Each staged subject should appear in the digest body.
        for i in range(3):
            assert f"Task {i} done" in task.description

        # All staged rows now marked flushed + linked to the rollup.
        conn = svc._conn
        rows = conn.execute(
            "SELECT flushed_at, rollup_task_id FROM notification_staging "
            "WHERE project = ? ORDER BY id",
            ("demo",),
        ).fetchall()
        assert len(rows) == 3
        for flushed_at, rollup_id in rows:
            assert flushed_at is not None
            assert rollup_id == task_id

    def test_flush_after_flush_has_nothing_to_do(self, svc):
        ns.stage_notification(
            svc._conn,
            project="demo",
            subject="Done",
            body="body",
            actor="polly",
            priority="digest",
            milestone_key="milestones/01-init",
        )
        first = ns.flush_milestone_digest(
            svc, project="demo", milestone_key="milestones/01-init",
            project_path=svc._project_path,
        )
        assert first is not None
        second = ns.flush_milestone_digest(
            svc, project="demo", milestone_key="milestones/01-init",
            project_path=svc._project_path,
        )
        assert second is None


# ---------------------------------------------------------------------------
# Milestone detection + on-done flush
# ---------------------------------------------------------------------------


class TestMilestoneDetection:
    def _write_milestone(
        self,
        project_root: Path,
        slug: str,
        title: str,
        task_ids: list[str],
    ) -> None:
        md_dir = project_root / "docs" / "plan" / "milestones"
        md_dir.mkdir(parents=True, exist_ok=True)
        body = [f"# {title}", ""]
        for tid in task_ids:
            body.append(f"- {tid}")
        (md_dir / f"{slug}.md").write_text("\n".join(body))

    def test_flush_fires_when_last_milestone_task_goes_done(
        self, project_root, svc,
    ):
        # Create two tasks; associate both with milestone 01.
        a = svc.create(
            title="Task A", description="", type="task", project="demo",
            flow_template="chat",
            roles={"requester": "user", "operator": "polly"},
            created_by="polly",
        )
        b = svc.create(
            title="Task B", description="", type="task", project="demo",
            flow_template="chat",
            roles={"requester": "user", "operator": "polly"},
            created_by="polly",
        )
        self._write_milestone(
            project_root, "01-init", "Milestone 01 — Init",
            task_ids=[a.task_id, b.task_id],
        )

        # Stage one digest row for the milestone.
        ns.stage_notification(
            svc._conn,
            project="demo",
            subject="Task A done",
            body="done",
            actor="polly",
            priority="digest",
            milestone_key="milestones/01-init",
        )

        # Mark A done — milestone NOT yet 100% (B still draft).
        svc.mark_done(a.task_id, actor="polly")
        rollup_tasks = [
            t for t in svc.list_tasks(project="demo")
            if "updates" in t.title
        ]
        assert rollup_tasks == [], "milestone shouldn't flush before all done"

        # Mark B done — milestone flips to 100%, flush fires.
        svc.mark_done(b.task_id, actor="polly")
        rollup_tasks = [
            t for t in svc.list_tasks(project="demo")
            if "updates" in t.title
        ]
        assert len(rollup_tasks) == 1, (
            f"expected one rollup, got: "
            f"{[t.title for t in svc.list_tasks(project='demo')]}"
        )
        rollup = rollup_tasks[0]
        assert "Milestone 01" in rollup.title or "Milestone 1" in rollup.title

    def test_project_idle_fallback_flushes_old_rows(self, svc):
        # No milestones directory → fallback path.
        # Stage an old row and a fresh row; only the old one is required
        # to trip the threshold.
        conn = svc._conn
        ns._ensure_staging_table(conn)
        old_ts = (datetime.now(UTC) - timedelta(minutes=15)).isoformat()
        conn.execute(
            "INSERT INTO notification_staging "
            "(project, subject, body, actor, priority, payload_json, "
            "milestone_key, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "demo", "Old one", "body", "polly", "digest",
                "{}", ns._IDLE_KEY, old_ts,
            ),
        )
        conn.commit()

        # Create + mark a task done so the transition hook fires.
        t = svc.create(
            title="Done-task", description="", type="task", project="demo",
            flow_template="chat",
            roles={"requester": "user", "operator": "polly"},
            created_by="polly",
        )
        svc.mark_done(t.task_id, actor="polly")

        rollup_tasks = [
            task for task in svc.list_tasks(project="demo")
            if "updates" in task.title
        ]
        # The done task itself is the only other task in the project, so
        # the project is now "idle" → idle-bucket flush should fire.
        assert len(rollup_tasks) == 1
        # Row is marked flushed.
        row = conn.execute(
            "SELECT flushed_at FROM notification_staging "
            "WHERE milestone_key = ?",
            (ns._IDLE_KEY,),
        ).fetchone()
        assert row[0] is not None


# ---------------------------------------------------------------------------
# Regression detection
# ---------------------------------------------------------------------------


class TestRegressionOnReopen:
    def test_reopen_after_flush_creates_immediate_item(self, svc):
        # Create a task, stage a digest row referencing it, flush.
        t = svc.create(
            title="Feature X", description="", type="task", project="demo",
            flow_template="chat",
            roles={"requester": "user", "operator": "polly"},
            created_by="polly",
        )
        ns.stage_notification(
            svc._conn,
            project="demo",
            subject="Feature X done",
            body="shipped",
            actor="polly",
            priority="digest",
            milestone_key="milestones/01-init",
            payload={"task_id": t.task_id},
        )
        rollup_id = ns.flush_milestone_digest(
            svc, project="demo", milestone_key="milestones/01-init",
            project_path=svc._project_path,
        )
        assert rollup_id is not None

        before_count = len(svc.list_tasks(project="demo"))

        # Simulate re-open: call the regression hook directly with a
        # "done → in_progress" transition. The engine itself can't
        # produce this today (done is terminal), but a future reopen
        # flow — or direct DB mutation — should be detected.
        result = ns.check_regression_on_reopen(
            svc,
            project="demo",
            task_id=t.task_id,
            from_state="done",
            to_state="in_progress",
            actor="system",
        )
        assert result is not None, "expected a regression inbox task"
        regression = svc.get(result)
        assert "Regression" in regression.title
        assert t.task_id in regression.title
        # Inbox-visible.
        assert regression.roles.get("requester") == "user"
        # It didn't re-flush the original rollup.
        new_count = len(svc.list_tasks(project="demo"))
        assert new_count == before_count + 1

    def test_no_regression_when_no_prior_flush(self, svc):
        t = svc.create(
            title="Feature Y", description="", type="task", project="demo",
            flow_template="chat",
            roles={"requester": "user", "operator": "polly"},
            created_by="polly",
        )
        # No staging rows exist — so no prior flush.
        result = ns.check_regression_on_reopen(
            svc,
            project="demo",
            task_id=t.task_id,
            from_state="done",
            to_state="in_progress",
            actor="system",
        )
        assert result is None


# ---------------------------------------------------------------------------
# Prune
# ---------------------------------------------------------------------------


class TestPrune:
    def test_prune_removes_old_flushed_rows_only(self, db_path):
        conn = sqlite3.connect(str(db_path))
        try:
            ns._ensure_staging_table(conn)
            now = datetime.now(UTC)

            # (a) Flushed 40 days ago — should be pruned.
            conn.execute(
                "INSERT INTO notification_staging "
                "(project, subject, body, actor, priority, payload_json, "
                "milestone_key, created_at, flushed_at, rollup_task_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "demo", "old done", "body", "polly", "digest", "{}",
                    "milestones/01-init",
                    (now - timedelta(days=50)).isoformat(),
                    (now - timedelta(days=40)).isoformat(),
                    "demo/99",
                ),
            )

            # (b) Flushed 5 days ago — keep.
            conn.execute(
                "INSERT INTO notification_staging "
                "(project, subject, body, actor, priority, payload_json, "
                "milestone_key, created_at, flushed_at, rollup_task_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "demo", "recent done", "body", "polly", "digest", "{}",
                    "milestones/01-init",
                    (now - timedelta(days=10)).isoformat(),
                    (now - timedelta(days=5)).isoformat(),
                    "demo/100",
                ),
            )

            # (c) Pending digest from 60 days ago — keep (pending is
            # never pruned, the milestone just hasn't closed yet).
            conn.execute(
                "INSERT INTO notification_staging "
                "(project, subject, body, actor, priority, payload_json, "
                "milestone_key, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "demo", "old pending", "body", "polly", "digest", "{}",
                    "milestones/02-next",
                    (now - timedelta(days=60)).isoformat(),
                ),
            )

            # (d) Silent audit row from 40 days ago — prune.
            conn.execute(
                "INSERT INTO notification_staging "
                "(project, subject, body, actor, priority, payload_json, "
                "milestone_key, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "demo", "old audit", "body", "polly", "silent", "{}",
                    None,
                    (now - timedelta(days=40)).isoformat(),
                ),
            )
            conn.commit()

            summary = ns.prune_old_staging(conn, retain_days=30)
            assert summary["flushed_pruned"] == 1
            assert summary["silent_pruned"] == 1

            remaining = conn.execute(
                "SELECT subject FROM notification_staging ORDER BY subject"
            ).fetchall()
            remaining_subjects = sorted(r[0] for r in remaining)
            assert remaining_subjects == ["old pending", "recent done"]
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Plugin handler wiring
# ---------------------------------------------------------------------------


class TestPruneHandler:
    def test_handler_registered_and_runs(self, tmp_path, monkeypatch):
        # Build a minimal pollypm.toml that the handler's
        # _load_config_and_store path can resolve.
        cfg_path = tmp_path / "pollypm.toml"
        state_db = tmp_path / "state.db"
        cfg_path.write_text(
            "[project]\n"
            f'name = "test"\n'
            f'root = "{tmp_path.as_posix()}"\n'
            f'state_db = "{state_db.as_posix()}"\n'
            f'tmux_session = "test"\n'
            "[runtime]\n"
            "[schedulers]\n"
            "[defaults]\n"
        )

        # Seed a flushed stale row via the direct connection.
        conn = sqlite3.connect(str(state_db))
        try:
            ns._ensure_staging_table(conn)
            now = datetime.now(UTC)
            conn.execute(
                "INSERT INTO notification_staging "
                "(project, subject, body, actor, priority, payload_json, "
                "milestone_key, created_at, flushed_at, rollup_task_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "demo", "stale", "b", "polly", "digest", "{}",
                    "milestones/x",
                    (now - timedelta(days=40)).isoformat(),
                    (now - timedelta(days=31)).isoformat(),
                    "demo/1",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        out = notification_staging_prune_handler(
            {"config_path": str(cfg_path), "retain_days": 30},
        )
        assert out["flushed_pruned"] == 1
