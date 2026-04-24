from __future__ import annotations

from pollypm.project_status_summary import (
    ProjectBlockerSummary,
    ProjectMonitorSummary,
    record_project_blocker_summary,
    record_project_monitor_summary,
)
from pollypm.store import SQLAlchemyStore
from pollypm.work.inbox_view import inbox_tasks
from pollypm.work.sqlite_service import SQLiteWorkService


def test_user_owned_blocker_summary_creates_inbox_task_and_event(tmp_path):
    store = SQLAlchemyStore(f"sqlite:///{tmp_path / 'state.db'}")
    work = SQLiteWorkService(db_path=tmp_path / "state.db", project_path=tmp_path)
    try:
        result = record_project_blocker_summary(
            store=store,
            work_service=work,
            summary=ProjectBlockerSummary(
                project="demo",
                reason="Deploy cannot continue until Fly.io is configured.",
                owner="user",
                required_actions=["Create Fly.io app", "Add deploy token"],
                affected_tasks=["demo/2"],
                unblock_condition="fly deploy succeeds",
            ),
            actor="polly",
        )

        assert result["event_id"] == 1
        assert result["task_id"] == "demo/1"
        task = work.get("demo/1")
        assert "project_blocker" in task.labels
        assert task.roles["requester"] == "user"
        assert "Create Fly.io app" in task.description
        assert [item.task_id for item in inbox_tasks(work, project="demo")] == ["demo/1"]

        rows = store.query_messages(type="event", scope="demo")
        assert rows[0]["payload"]["event_type"] == "project_blocker_summary"
        assert rows[0]["payload"]["task_id"] == "demo/1"
    finally:
        work.close()
        store.close()


def test_system_owned_blocker_summary_is_passive_event_only(tmp_path):
    store = SQLAlchemyStore(f"sqlite:///{tmp_path / 'state.db'}")
    work = SQLiteWorkService(db_path=tmp_path / "state.db", project_path=tmp_path)
    try:
        result = record_project_blocker_summary(
            store=store,
            work_service=work,
            summary=ProjectBlockerSummary(
                project="demo",
                reason="Waiting for worker slot.",
                owner="system",
                required_actions=["Retry on next sweep"],
                affected_tasks=["demo/2"],
                unblock_condition="worker slot opens",
            ),
        )

        assert result["task_id"] is None
        assert inbox_tasks(work, project="demo") == []
        rows = store.query_messages(type="event", scope="demo")
        assert rows[0]["payload"]["owner"] == "system"
    finally:
        work.close()
        store.close()


def test_monitor_summary_records_activity_without_inbox_task(tmp_path):
    store = SQLAlchemyStore(f"sqlite:///{tmp_path / 'state.db'}")
    work = SQLiteWorkService(db_path=tmp_path / "state.db", project_path=tmp_path)
    try:
        event_id = record_project_monitor_summary(
            store=store,
            summary=ProjectMonitorSummary(
                project="demo",
                completed_since_last=[],
                stalled_tasks=["demo/3"],
                human_blockers=[],
                automatic_next_actions=["advisor will re-check in 30m"],
                next_check_at="2026-04-24T01:00:00+00:00",
            ),
        )

        assert event_id == 1
        assert inbox_tasks(work, project="demo") == []
        rows = store.query_messages(type="event", scope="demo")
        assert rows[0]["subject"] == "project.monitor_summary"
        assert rows[0]["payload"]["stalled_tasks"] == ["demo/3"]
    finally:
        work.close()
        store.close()
