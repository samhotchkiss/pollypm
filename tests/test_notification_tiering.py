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
import sqlalchemy
from typer.testing import CliRunner

from pollypm import notification_staging as ns
from pollypm.cli import app as root_app
from pollypm.plugins_builtin.core_recurring.plugin import (
    notification_staging_prune_handler,
)
from pollypm.store.classifier import classify_priority  # noqa: F401 — re-bind below
from pollypm.work.models import WorkStatus
from pollypm.work.sqlite_service import SQLiteWorkService


# The classifier moved to :mod:`pollypm.store.classifier` in #340 and the
# back-compat re-export on :mod:`pollypm.notification_staging` was deleted
# in #342. Bind the imported name onto ``ns`` so the existing
# ``ns.classify_priority(...)`` call sites below keep working without a
# per-site rewrite.
ns.classify_priority = classify_priority  # type: ignore[attr-defined]


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

    # ---- Action-requiring completion upgrades (issue: Sam's "all
    # subagents clear — ready for account switch" was silently staged
    # as digest because the classifier only saw "clear" and missed the
    # action-requiring "ready for …" phrase). Completion messages that
    # imply the user must do something NOW must land in the inbox.
    @pytest.mark.parametrize(
        "subject, body",
        [
            # Regression: the exact subject that was swallowed.
            ("All subagents clear — ready for account switch", ""),
            # Plain "ready for testing" completion.
            ("Ready for testing: dashboard + stage transitions live", ""),
            # "MERGED" + "safe to test" — both completion and action.
            ("#271 MERGED — safe to test", ""),
            # Action phrase hiding in the body, completion in subject.
            ("Task done", "please verify the new flow"),
            # Awaiting-approval variant.
            ("Shipped v2", "awaiting your approval to cut the tag"),
            # Even an otherwise-silent "test pass" must upgrade when
            # the body explicitly requests action.
            ("Test pass", "please verify the rollout"),
        ],
    )
    def test_action_requiring_completion_upgrades_to_immediate(
        self, subject, body,
    ):
        assert ns.classify_priority(subject, body) == "immediate"

    def test_routine_completion_still_digest(self):
        # No action-requiring phrase → classic digest path unchanged.
        assert (
            ns.classify_priority(
                "Routine progress: 3 tasks complete", "nothing to action",
            )
            == "digest"
        )

    def test_test_pass_subject_alone_still_silent(self):
        # Bare "Test pass" subject with a no-action body stays silent
        # — the existing audit-trail behaviour must not regress.
        assert ns.classify_priority("Test pass", "") == "silent"

    # ---- Bug-report shape upgrades (operator-surfaced regression:
    # dogfood findings #3 and #4 were auto-classified as digest because
    # the literal "done" keyword in "Archie skips per-stage pm task
    # done" hit ``_DIGEST_KEYWORDS``. Bug-report-shape keywords must
    # take precedence so findings reach the inbox immediately rather
    # than waiting for the next milestone flush.
    def test_bug_finding_classifies_immediate(self):
        # Polly's literal repro string — co-occurring "done" must NOT
        # downgrade this to digest because "skips" flags a bug shape.
        assert (
            ns.classify_priority(
                "#4: Archie skips per-stage pm task done", "",
            )
            == "immediate"
        )

    def test_gap_finding_classifies_immediate(self):
        assert (
            ns.classify_priority(
                "Gap A fallback doesn't fire from cron pm heartbeat", "",
            )
            == "immediate"
        )

    def test_dogfood_finding_classifies_immediate(self):
        assert (
            ns.classify_priority(
                "dogfood finding: replan misclassification", "",
            )
            == "immediate"
        )

    def test_regression_classifies_immediate(self):
        assert ns.classify_priority("regression in foo bar", "") == "immediate"

    def test_bug_keyword_classifies_immediate(self):
        # Even when paired with a digest keyword, "bug" wins.
        assert (
            ns.classify_priority("Bug: deploy completed but page 500s", "")
            == "immediate"
        )

    def test_broken_keyword_classifies_immediate(self):
        assert (
            ns.classify_priority("Broken: heartbeat skipped a tick", "")
            == "immediate"
        )

    def test_misclassification_keyword_classifies_immediate(self):
        assert (
            ns.classify_priority("replan misclassification shipped", "")
            == "immediate"
        )

    def test_non_bug_done_remains_digest(self):
        # The new bug-shape keywords must NOT swallow routine
        # completions. "Build done." has no bug keyword and should
        # still classify as digest so milestone rollups keep working.
        assert ns.classify_priority("Build done.", "") == "digest"

    def test_debug_does_not_trigger_bug(self):
        # Word-boundary safety: "debug" / "bugfix" must not match the
        # bare "bug" token (otherwise routine "debug logs shipped"
        # status updates would over-notify).
        assert (
            ns.classify_priority("debug logs shipped", "") == "digest"
        )


# ---------------------------------------------------------------------------
# pm notify CLI — tier routing
# ---------------------------------------------------------------------------


class TestNotifyCLITiers:
    """After issue #340, ``pm notify`` writes to the unified ``messages``
    table (via :meth:`Store.enqueue_message`) instead of splitting across
    ``work_tasks`` / ``notification_staging`` / ``events``.

    The classifier behaviour is unchanged — these tests verify the tier
    still drives the stored row's ``tier`` + ``state`` and the CLI's
    stdout shape (``digest:<id>`` / ``silent`` / bare id).
    """

    def _messages(self, db_path):
        from pollypm.store import SQLAlchemyStore
        store = SQLAlchemyStore(f"sqlite:///{db_path}")
        try:
            with store.read_engine.connect() as conn:
                rows = conn.execute(
                    sqlalchemy.text(
                        "SELECT type, tier, state, subject, body "
                        "FROM messages ORDER BY id ASC"
                    )
                ).mappings().all()
            return [dict(r) for r in rows]
        finally:
            store.close()

    def test_default_priority_creates_immediate_message(self, db_path):
        result = _invoke_notify(
            str(db_path), "Deploy blocked", "Needs verification email click.",
        )
        assert result.exit_code == 0, result.output
        row_id = result.output.strip().splitlines()[-1]
        assert row_id.isdigit(), row_id

        rows = self._messages(db_path)
        assert len(rows) == 1
        assert rows[0]["type"] == "notify"
        assert rows[0]["tier"] == "immediate"
        assert rows[0]["state"] == "open"
        assert "Deploy blocked" in rows[0]["subject"]
        assert rows[0]["subject"].startswith("[Action]")

    def test_digest_priority_lands_staged(self, db_path):
        result = _invoke_notify(
            str(db_path),
            "Done: homepage rewrite",
            "Review at https://…",
            "--priority", "digest",
        )
        assert result.exit_code == 0, result.output
        out = result.output.strip().splitlines()[-1]
        assert out.startswith("digest:"), out

        rows = self._messages(db_path)
        assert len(rows) == 1
        assert rows[0]["tier"] == "digest"
        assert rows[0]["state"] == "staged"
        assert rows[0]["subject"].startswith("[FYI]")

    def test_silent_priority_lands_closed(self, db_path):
        result = _invoke_notify(
            str(db_path),
            "Test pass",
            "all green",
            "--priority", "silent",
        )
        assert result.exit_code == 0, result.output
        assert result.output.strip().splitlines()[-1] == "silent"

        rows = self._messages(db_path)
        assert len(rows) == 1
        assert rows[0]["tier"] == "silent"
        assert rows[0]["state"] == "closed"
        assert rows[0]["subject"].startswith("[Audit]")

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

        rows = self._messages(db_path)
        assert len(rows) == 1
        assert rows[0]["tier"] == "digest"

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

    def test_flush_picks_up_messages_table_digest_rows(self, svc):
        """#341: digest rows in ``messages`` contribute to the rollup.

        ``pm notify --priority digest`` (post-#340) writes rows to the
        unified ``messages`` table with ``state='staged'``; the flush
        must pick them up as well as anything still in the legacy
        ``notification_staging`` table.
        """
        from pollypm.store import SQLAlchemyStore

        store = SQLAlchemyStore(f"sqlite:///{svc._db_path}")
        try:
            # Two digest notifications land in messages; one legacy
            # staging row lands via stage_notification.
            for i in range(2):
                store.enqueue_message(
                    type="notify",
                    tier="digest",
                    recipient="user",
                    sender="polly",
                    subject=f"new-path update {i}",
                    body=f"notes {i}",
                    scope="demo",
                    payload={
                        "actor": "polly",
                        "project": "demo",
                        "milestone_key": "milestones/01-init",
                    },
                    state="staged",
                )
        finally:
            store.close()

        ns.stage_notification(
            svc._conn,
            project="demo",
            subject="legacy staged",
            body="old-path body",
            actor="polly",
            priority="digest",
            milestone_key="milestones/01-init",
        )

        task_id = ns.flush_milestone_digest(
            svc,
            project="demo",
            milestone_key="milestones/01-init",
            project_path=svc._project_path,
        )
        assert task_id is not None
        task = svc.get(task_id)
        assert "3 updates" in task.title
        # Body mentions each subject from both sources.
        assert "legacy staged" in task.description
        assert "new-path update 0" in task.description
        assert "new-path update 1" in task.description

        # Messages-table rows were closed so they don't re-flush.
        store = SQLAlchemyStore(f"sqlite:///{svc._db_path}")
        try:
            staged = store.query_messages(
                type="notify", tier="digest", state="staged",
            )
            assert staged == []
        finally:
            store.close()


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
