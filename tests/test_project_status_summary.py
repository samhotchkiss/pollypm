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


def test_compute_project_monitor_summary_buckets_tasks_correctly(tmp_path):
    """#782 — the summary helper walks the work service and fills
    every field on ``ProjectMonitorSummary`` so durable monitor records
    carry completions, stalls, human blockers, and the explicit
    zero-completion flag.
    """
    from datetime import UTC, datetime, timedelta
    from pollypm.project_status_summary import compute_project_monitor_summary
    from pollypm.work.models import WorkStatus

    class _FakeTask:
        def __init__(self, *, task_id, status, updated_at, completed_at=None):
            self.task_id = task_id
            self.work_status = WorkStatus(status)
            self.updated_at = updated_at
            self.transitions = []
            if completed_at is not None:
                self.transitions.append(
                    {"to_state": "done", "timestamp": completed_at}
                )

    now = datetime.now(UTC)

    class _FakeService:
        def list_tasks(self, *, project):
            assert project == "demo"
            return [
                # Done within window → counts as completion.
                _FakeTask(
                    task_id="demo/1", status="done",
                    updated_at=now - timedelta(minutes=5),
                    completed_at=now - timedelta(minutes=5),
                ),
                # In-progress, last update outside window → stalled.
                _FakeTask(
                    task_id="demo/2", status="in_progress",
                    updated_at=now - timedelta(hours=6),
                ),
                # In-progress, recent update → not stalled.
                _FakeTask(
                    task_id="demo/3", status="in_progress",
                    updated_at=now - timedelta(minutes=10),
                ),
                # Blocked → human blocker.
                _FakeTask(
                    task_id="demo/4", status="blocked",
                    updated_at=now - timedelta(hours=24),
                ),
                # On-hold → human blocker.
                _FakeTask(
                    task_id="demo/5", status="on_hold",
                    updated_at=now - timedelta(hours=2),
                ),
                # Done outside window → does NOT count as recent completion.
                _FakeTask(
                    task_id="demo/6", status="done",
                    updated_at=now - timedelta(hours=12),
                    completed_at=now - timedelta(hours=12),
                ),
            ]

    summary = compute_project_monitor_summary(
        work_service=_FakeService(),
        project_key="demo",
    )

    assert summary.project == "demo"
    assert summary.completed_since_last == ["demo/1"]
    assert summary.stalled_tasks == ["demo/2"]
    assert sorted(summary.human_blockers) == ["demo/4", "demo/5"]
    assert summary.zero_completion_window is False
    assert summary.window_started_at is not None


def test_compute_project_monitor_summary_uses_done_transition_time(tmp_path):
    """#818: editing an old done task is not a new completion."""
    from datetime import UTC, datetime, timedelta
    from pollypm.project_status_summary import compute_project_monitor_summary
    from pollypm.work.models import WorkStatus

    now = datetime.now(UTC)

    class _FakeTask:
        task_id = "demo/1"
        work_status = WorkStatus.DONE
        # Fresh edit inside the monitor window.
        updated_at = now - timedelta(minutes=5)
        # Actual completion happened yesterday.
        transitions = [
            {"to_state": "done", "timestamp": now - timedelta(days=1)}
        ]

    class _FakeService:
        def list_tasks(self, *, project):
            return [_FakeTask()]

    summary = compute_project_monitor_summary(
        work_service=_FakeService(),
        project_key="demo",
        since=now - timedelta(hours=2),
    )

    assert summary.completed_since_last == []
    assert summary.zero_completion_window is True


def test_compute_project_monitor_summary_flags_zero_completion(tmp_path):
    """When no tasks completed in the lookback window, the
    ``zero_completion_window`` flag is True so renderers don't have to
    re-derive it from an empty completed list (which is also the
    "haven't checked yet" shape)."""
    from datetime import UTC, datetime, timedelta
    from pollypm.project_status_summary import compute_project_monitor_summary
    from pollypm.work.models import WorkStatus

    class _FakeTask:
        def __init__(self, *, task_id, status, updated_at):
            self.task_id = task_id
            self.work_status = WorkStatus(status)
            self.updated_at = updated_at

    now = datetime.now(UTC)

    class _FakeService:
        def list_tasks(self, *, project):
            return [
                _FakeTask(
                    task_id="demo/1", status="in_progress",
                    updated_at=now - timedelta(hours=24),
                ),
            ]

    summary = compute_project_monitor_summary(
        work_service=_FakeService(),
        project_key="demo",
    )
    assert summary.completed_since_last == []
    assert summary.zero_completion_window is True


def test_monitor_summary_persists_zero_completion_flag(tmp_path):
    """The persisted event payload carries the new
    ``zero_completion_window`` + ``window_started_at`` keys so any
    later renderer can read them without recomputing."""
    from pollypm.project_status_summary import (
        ProjectMonitorSummary,
        record_project_monitor_summary,
    )

    store = SQLAlchemyStore(f"sqlite:///{tmp_path / 'state.db'}")
    try:
        record_project_monitor_summary(
            store=store,
            summary=ProjectMonitorSummary(
                project="demo",
                completed_since_last=[],
                stalled_tasks=[],
                human_blockers=[],
                automatic_next_actions=[],
                zero_completion_window=True,
                window_started_at="2026-04-26T12:00:00+00:00",
            ),
        )
        rows = store.query_messages(type="event", scope="demo")
        assert rows[0]["payload"]["zero_completion_window"] is True
        assert rows[0]["payload"]["window_started_at"] == "2026-04-26T12:00:00+00:00"
    finally:
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
