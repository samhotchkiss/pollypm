"""Per-emit-site ``kind`` retraining tests (#1567, #1568).

The :class:`InboxItemKind` enum landed in #1565 and the canonical
:func:`awaits_user` predicate in #1566 — both rely on every inbox emit
site stamping the right ``kind`` at construction time. This file pins
the chosen kind value per emit site so a future refactor can't silently
drift one back to ``legacy`` (the safe-default that would round-trip as
"awaits user" forever).

Grouping: one test per emit site, asserting the stored ``kind`` on the
row that site produces. Where the site emits both a message + a work
task, both are checked because the awaits-user predicate runs over both
tables.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from pollypm.inbox.kind import InboxItemKind
from pollypm.store import SQLAlchemyStore


# ---------------------------------------------------------------------------
# Shared in-memory work-service fake
# ---------------------------------------------------------------------------


@dataclass
class _CreatedTask:
    task_id: str
    kwargs: dict[str, Any] = field(default_factory=dict)


class _RecordingWorkService:
    """Captures every ``create()`` call so tests can assert ``kind``."""

    def __init__(self) -> None:
        self.creates: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _CreatedTask:
        self.creates.append(kwargs)
        return _CreatedTask(task_id="demo/1", kwargs=dict(kwargs))


# ---------------------------------------------------------------------------
# task_shipped — emit_task_shipped_card → completion_fyi
# ---------------------------------------------------------------------------


def test_task_shipped_card_is_tagged_completion_fyi() -> None:
    from pollypm.task_shipped import emit_task_shipped_card

    @dataclass
    class _Artifact:
        kind: Any
        ref: str = ""
        external_ref: str = ""
        description: str = ""

    @dataclass
    class _WorkOutput:
        summary: str = ""
        artifacts: list[Any] = field(default_factory=list)

    @dataclass
    class _Execution:
        work_output: Any | None = None

    @dataclass
    class _Task:
        task_id: str
        project: str
        title: str

    svc = _RecordingWorkService()
    work_output = _WorkOutput(summary="phase one shipped")
    execution = _Execution(work_output=work_output)
    task = _Task(task_id="demo/1", project="demo", title="Build feature")

    svc.get = lambda _tid: task  # type: ignore[assignment]
    svc.get_execution = lambda _tid: [execution]  # type: ignore[assignment]

    emit_task_shipped_card(svc, "demo/1", actor="polly")

    assert len(svc.creates) == 1
    assert svc.creates[0]["kind"] == InboxItemKind.COMPLETION_FYI.value


# ---------------------------------------------------------------------------
# worker_milestone — emit_worker_milestone → activity_event (polly addressee)
# ---------------------------------------------------------------------------


def test_worker_milestone_card_is_tagged_activity_event() -> None:
    from pollypm.work.models import Priority, Task, WorkStatus
    from pollypm.worker_milestone import emit_worker_milestone

    fake_task = Task(
        project="demo",
        task_number=2,
        title="Migrate cache",
        type="task",
        flow_template_id="standard",
        flow_template_version=1,
        work_status=WorkStatus.IN_PROGRESS,
        priority=Priority.NORMAL,
    )

    svc = _RecordingWorkService()
    svc.get = lambda _tid: fake_task  # type: ignore[assignment]
    svc.list_tasks = lambda **_: []  # type: ignore[assignment]

    emit_worker_milestone(
        svc, "demo/2", message="cache migration midpoint", actor="worker",
    )

    assert len(svc.creates) == 1
    assert svc.creates[0]["kind"] == InboxItemKind.ACTIVITY_EVENT.value


# ---------------------------------------------------------------------------
# proposals — emit_proposals → approval_request
# ---------------------------------------------------------------------------


def test_emit_proposals_is_tagged_approval_request(tmp_path: Path) -> None:
    from pollypm.plugins_builtin.project_planning.proposals import (
        ImprovementProposal,
        emit_proposals,
    )

    svc = _RecordingWorkService()
    proposals = [
        ImprovementProposal(
            title="Add caching layer",
            rationale="Cache hot read path",
            severity="advisory",
        ),
    ]

    emit_proposals(
        svc,
        project_key="demo",
        proposals=proposals,
        memory_path=tmp_path / "planner_memory.json",
    )

    assert len(svc.creates) == 1
    assert svc.creates[0]["kind"] == InboxItemKind.APPROVAL_REQUEST.value


# ---------------------------------------------------------------------------
# rejection_feedback — emit_rejection_feedback → manual_decision
# ---------------------------------------------------------------------------


def test_emit_rejection_feedback_is_tagged_manual_decision() -> None:
    from pollypm.rejection_feedback import emit_rejection_feedback
    from pollypm.work.models import Priority, Task, WorkStatus

    task = Task(
        project="demo",
        task_number=3,
        title="Refactor module",
        type="task",
        flow_template_id="standard",
        flow_template_version=1,
        work_status=WorkStatus.IN_PROGRESS,
        priority=Priority.NORMAL,
        current_node_id="code_review",
    )

    svc = _RecordingWorkService()
    emit_rejection_feedback(
        svc, task=task, reviewer="reviewer", reason="needs more tests",
    )

    assert len(svc.creates) == 1
    assert svc.creates[0]["kind"] == InboxItemKind.MANUAL_DECISION.value


# ---------------------------------------------------------------------------
# notification_staging — flush_milestone_digest → manual_decision
# ---------------------------------------------------------------------------


@dataclass
class _DigestCandidate:
    payload: dict[str, Any]
    subject: str = "phase shipped"
    body: str = "details"
    actor: str = "worker"
    created_at: str = "2026-05-13T12:00:00+00:00"


class _DigestRollupSvc(_RecordingWorkService):
    def __init__(self, candidates: list[_DigestCandidate]) -> None:
        super().__init__()
        self._candidates = candidates
        self._flushed: list[Any] = []
        self._contexts: list[tuple[str, str]] = []

    def list_digest_rollup_candidates(self, **_: Any) -> list[_DigestCandidate]:
        return list(self._candidates)

    def mark_rollup_candidates_flushed(self, candidates, **_: Any) -> None:
        self._flushed.append(candidates)

    def add_context(self, task_id: str, actor: str, blob: str, **_: Any) -> None:
        self._contexts.append((task_id, blob))


def test_flush_milestone_digest_is_tagged_manual_decision(tmp_path: Path) -> None:
    from pollypm.notification_staging import flush_milestone_digest

    candidates = [_DigestCandidate(payload={"project": "demo"})]
    svc = _DigestRollupSvc(candidates)
    flush_milestone_digest(
        svc,
        project="demo",
        milestone_key="milestones/01-core",
        actor="polly",
        project_path=tmp_path,
    )

    assert len(svc.creates) == 1
    assert svc.creates[0]["kind"] == InboxItemKind.MANUAL_DECISION.value


def test_check_regression_on_reopen_is_tagged_manual_decision() -> None:
    from pollypm.notification_staging import check_regression_on_reopen

    class _Svc(_RecordingWorkService):
        def find_flushed_rollup_milestone(self, **_: Any) -> str:
            return "milestones/01-core"

    svc = _Svc()
    check_regression_on_reopen(
        svc,
        project="demo",
        task_id="demo/9",
        from_state="done",
        to_state="in_progress",
        actor="polly",
    )

    assert len(svc.creates) == 1
    assert svc.creates[0]["kind"] == InboxItemKind.MANUAL_DECISION.value


# ---------------------------------------------------------------------------
# project_status_summary — blocker summary → manual_decision
# ---------------------------------------------------------------------------


def test_record_blocker_summary_user_owner_is_tagged_manual_decision() -> None:
    from pollypm.project_status_summary import (
        ProjectBlockerSummary,
        record_project_blocker_summary,
    )

    class _StubStore:
        def __init__(self) -> None:
            self.events: list[dict[str, Any]] = []
            self.updates: list[Any] = []

        def record_event(self, project, actor, subject, payload=None) -> int:
            self.events.append({
                "project": project, "actor": actor,
                "subject": subject, "payload": payload,
            })
            return 7

        def update_message(self, *args: Any, **kwargs: Any) -> None:
            self.updates.append((args, kwargs))

    store = _StubStore()
    svc = _RecordingWorkService()
    summary = ProjectBlockerSummary(
        project="demo",
        reason="awaiting product decision on rollout",
        owner="user",
        required_actions=["confirm rollout window"],
        affected_tasks=["demo/4"],
        unblock_condition="user confirms",
    )
    record_project_blocker_summary(
        store=store, work_service=svc, summary=summary,
    )

    assert len(svc.creates) == 1
    assert svc.creates[0]["kind"] == InboxItemKind.MANUAL_DECISION.value


# ---------------------------------------------------------------------------
# worker_turn_end — blocking_question → activity_event (polly addressee)
# ---------------------------------------------------------------------------


def test_blocking_question_is_tagged_activity_event() -> None:
    from pollypm.recovery.worker_turn_end import (
        create_blocking_question_inbox_item,
    )
    from pollypm.work.models import Priority, Task, WorkStatus

    fake_task = Task(
        project="demo",
        task_number=5,
        title="Pipeline",
        type="task",
        flow_template_id="standard",
        flow_template_version=1,
        work_status=WorkStatus.IN_PROGRESS,
        priority=Priority.NORMAL,
    )

    svc = _RecordingWorkService()
    create_blocking_question_inbox_item(
        fake_task,
        "worker-demo",
        "Should the migration drop the legacy column?",
        svc,
    )

    assert len(svc.creates) == 1
    assert svc.creates[0]["kind"] == InboxItemKind.ACTIVITY_EVENT.value


# ---------------------------------------------------------------------------
# core_recurring.sweeps.pane_text_classify → activity_event
# ---------------------------------------------------------------------------


def test_pane_text_classify_inbox_task_is_tagged_activity_event() -> None:
    from pollypm.plugins_builtin.core_recurring import sweeps

    svc = _RecordingWorkService()
    svc.list_tasks = lambda **_: []  # type: ignore[assignment]
    sweeps._emit_pane_pattern_inbox_item(
        work_service=svc,
        session_name="worker-demo",
        rule_name="context_full",
        pane_text="approaching limit",
        msg_store=None,
    )
    assert len(svc.creates) == 1
    assert svc.creates[0]["kind"] == InboxItemKind.ACTIVITY_EVENT.value


# ---------------------------------------------------------------------------
# plan_review_emit — backstop emit → plan_review_pending
# ---------------------------------------------------------------------------


def test_plan_review_emit_stamps_plan_review_pending(tmp_path: Path) -> None:
    from pollypm.work.plan_review_emit import emit_plan_review_for_task
    from pollypm.work.sqlite_service import SQLiteWorkService

    db_path = tmp_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    svc = SQLiteWorkService(db_path=db_path, project_path=tmp_path)
    try:
        plan_task = svc.create(
            title="Plan demo",
            description="plan task body",
            type="task",
            project="demo",
            flow_template="chat",
            roles={"requester": "user", "operator": "architect"},
            priority="high",
            created_by="tester",
            labels=["poc-plan"],
        )
        new_task_id = emit_plan_review_for_task(
            svc=svc, task=svc.get(plan_task.task_id),
            actor="audit_watchdog", requester="user",
        )
    finally:
        svc.close()
    assert new_task_id is not None

    # Work-task row kind.
    svc2 = SQLiteWorkService(db_path=db_path, project_path=tmp_path)
    try:
        new_task = svc2.get(new_task_id)
        assert new_task.kind is InboxItemKind.PLAN_REVIEW_PENDING
    finally:
        svc2.close()

    # Messages row kind.
    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        msgs = store.query_messages(scope="demo")
        plan_msgs = [m for m in msgs if (m.get("type") or "") == "notify"]
        assert plan_msgs, "expected plan_review notify message"
        assert all(
            m["kind"] == InboxItemKind.PLAN_REVIEW_PENDING.value
            for m in plan_msgs
        )
    finally:
        store.close()


# ---------------------------------------------------------------------------
# audit_watchdog._create_operator_inbox_task (tier 3) → watchdog_operator_dispatch
# ---------------------------------------------------------------------------


def test_tier3_operator_inbox_task_is_tagged_watchdog_operator_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pollypm.plugins_builtin.core_recurring import audit_watchdog

    # Direct ``resolve_work_db_path`` at the tmp project so the helper
    # writes through the test workspace, not the user's $HOME. The
    # function imports ``resolve_work_db_path`` inline so the patch
    # target is its public module path.
    project_path = tmp_path / "proj"
    project_path.mkdir()
    (project_path / ".pollypm").mkdir()
    db_path = project_path / ".pollypm" / "state.db"

    monkeypatch.setattr(
        "pollypm.work.db_resolver.resolve_work_db_path",
        lambda *_a, **_k: db_path,
    )

    task_id = audit_watchdog._create_operator_inbox_task(
        project_key="demo",
        project_path=project_path,
        subject="watchdog: queue_without_motion on demo/1",
        body="TIER HANDOFF\n...",
        dedup_key="watchdog-operator:queue_without_motion:demo:demo/1",
    )
    assert task_id is not None

    # Messages row.
    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        msgs = store.query_messages(scope="demo")
        notifies = [m for m in msgs if (m.get("type") or "") == "notify"]
        assert notifies, "expected operator-dispatch notify row"
        assert all(
            m["kind"] == InboxItemKind.WATCHDOG_OPERATOR_DISPATCH.value
            for m in notifies
        )
    finally:
        store.close()

    # Work-task row.
    from pollypm.work.sqlite_service import SQLiteWorkService
    svc = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        task = svc.get(task_id)
        assert task.kind is InboxItemKind.WATCHDOG_OPERATOR_DISPATCH
    finally:
        svc.close()


# ---------------------------------------------------------------------------
# audit_watchdog._create_operator_tier4_inbox_task → watchdog_operator_dispatch
# ---------------------------------------------------------------------------


def test_tier4_operator_inbox_task_is_tagged_watchdog_operator_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pollypm.plugins_builtin.core_recurring import audit_watchdog

    project_path = tmp_path / "proj"
    project_path.mkdir()
    (project_path / ".pollypm").mkdir()
    db_path = project_path / ".pollypm" / "state.db"

    # tier-4 path imports ``_resolve_db_path`` from pollypm.work.cli.
    monkeypatch.setattr(
        "pollypm.work.cli._resolve_db_path",
        lambda *_a, **_k: db_path,
    )

    task_id = audit_watchdog._create_operator_tier4_inbox_task(
        project_key="demo",
        project_path=project_path,
        subject="watchdog tier4: queue_without_motion on demo",
        body="TIER 4 BROADER AUTHORITY DISPATCH\n...",
        dedup_key="watchdog-operator-tier4:rule:demo:hash",
    )
    assert task_id is not None

    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        msgs = store.query_messages(scope="demo")
        notifies = [m for m in msgs if (m.get("type") or "") == "notify"]
        assert notifies
        assert all(
            m["kind"] == InboxItemKind.WATCHDOG_OPERATOR_DISPATCH.value
            for m in notifies
        )
    finally:
        store.close()

    from pollypm.work.sqlite_service import SQLiteWorkService
    svc = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        task = svc.get(task_id)
        assert task.kind is InboxItemKind.WATCHDOG_OPERATOR_DISPATCH
    finally:
        svc.close()


# ---------------------------------------------------------------------------
# audit_watchdog._route_tier4_to_terminal urgent handoff →
# watchdog_operator_dispatch
# ---------------------------------------------------------------------------


def test_tier4_terminal_urgent_handoff_is_tagged_watchdog_operator_dispatch(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime
    from pollypm.plugins_builtin.core_recurring import audit_watchdog

    db_path = tmp_path / "state.db"
    msg_store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        class _Services:
            state_store = None

        services = _Services()
        services.msg_store = msg_store  # type: ignore[attr-defined]

        @dataclass
        class _State:
            tier4_entered_at: datetime
            project: str
            rule: str
            root_cause_hash: str

        state = _State(
            tier4_entered_at=datetime.now(UTC),
            project="demo",
            rule="queue_without_motion",
            root_cause_hash="hash123",
        )
        ok = audit_watchdog._route_tier4_to_terminal(
            state=state, services=services, now=datetime.now(UTC),
        )
        assert ok is True

        msgs = msg_store.query_messages(scope="demo")
        notifies = [m for m in msgs if (m.get("type") or "") == "notify"]
        assert notifies, "expected urgent terminal handoff"
        assert all(
            m["kind"] == InboxItemKind.WATCHDOG_OPERATOR_DISPATCH.value
            for m in notifies
        )
    finally:
        msg_store.close()


# ---------------------------------------------------------------------------
# pm notify CLI — label-keyed kind selection
# ---------------------------------------------------------------------------


def test_notify_kind_helper_picks_plan_review_pending_for_plan_review_label() -> None:
    from pollypm.cli_features.session_runtime import _kind_for_notify

    assert _kind_for_notify(
        ["plan_review", "project:demo"],
        requester="user",
        user_prompt_payload=None,
    ) == InboxItemKind.PLAN_REVIEW_PENDING.value


def test_notify_kind_helper_picks_pm_question_for_user_prompt_payload() -> None:
    from pollypm.cli_features.session_runtime import _kind_for_notify

    payload = {"summary": "please decide", "question": "go or no-go?"}
    assert _kind_for_notify(
        [],
        requester="user",
        user_prompt_payload=payload,
    ) == InboxItemKind.PM_QUESTION_UNANSWERED.value


def test_notify_kind_helper_defaults_to_legacy_for_freeform_notify() -> None:
    from pollypm.cli_features.session_runtime import _kind_for_notify

    assert _kind_for_notify(
        [], requester="user", user_prompt_payload=None,
    ) == InboxItemKind.LEGACY.value


def test_notify_kind_helper_polly_recipient_with_payload_stays_legacy() -> None:
    """``--requester polly`` routes inbox to Polly, not the user — the
    structured user_prompt is informational on the user surface."""
    from pollypm.cli_features.session_runtime import _kind_for_notify

    payload = {"summary": "fyi"}
    assert _kind_for_notify(
        [], requester="polly", user_prompt_payload=payload,
    ) == InboxItemKind.LEGACY.value


# ---------------------------------------------------------------------------
# work_tasks ensure_human_review_request_task → approval_request
# ---------------------------------------------------------------------------


def test_ensure_human_review_request_task_is_tagged_approval_request(
    tmp_path: Path,
) -> None:
    from pollypm.work.sqlite_service import SQLiteWorkService

    db_path = tmp_path / "state.db"
    svc = SQLiteWorkService(db_path=db_path, project_path=tmp_path)
    try:
        target = svc.create(
            title="Implement feature",
            description="Do the thing",
            type="task",
            project="demo",
            flow_template="standard",
            roles={"worker": "worker", "reviewer": "reviewer"},
            requires_human_review=True,
            created_by="tester",
        )
        review_task = svc.ensure_human_review_request_task(
            target.task_id, actor="polly",
        )
        assert review_task.kind is InboxItemKind.APPROVAL_REQUEST
    finally:
        svc.close()


# ---------------------------------------------------------------------------
# first_shipped milestone → completion_fyi
# ---------------------------------------------------------------------------


def test_record_first_shipped_activity_is_tagged_completion_fyi(
    tmp_path: Path,
) -> None:
    from pollypm.work.sqlite_service import _record_first_shipped_activity

    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / ".pollypm").mkdir()
    _record_first_shipped_activity(
        project_path=project_path,
        project_key="demo",
    )

    db_path = project_path / ".pollypm" / "state.db"
    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        msgs = store.query_messages(scope="polly")
        shipped = [m for m in msgs if (m.get("subject") or "") .endswith("first_shipped") or "first_shipped" in (m.get("subject") or "")]
        assert shipped, "expected first_shipped row"
        assert all(
            m["kind"] == InboxItemKind.COMPLETION_FYI.value for m in shipped
        )
    finally:
        store.close()
