"""Tests for the task workflow invariant checker (#886)."""

from __future__ import annotations

import pytest

from pollypm.task_invariants import (
    InvariantViolation,
    StateOwner,
    TASK_TRANSITION_TABLE,
    TaskCheckContext,
    TaskRow,
    ViolationKind,
    all_statuses_have_metadata,
    check_task_invariants,
    is_capacity_consuming,
    is_transition_allowed,
    metadata_for,
    requires_recovery_lane,
    validate_transition,
)
from pollypm.work.models import WorkStatus


# ---------------------------------------------------------------------------
# Coverage: every WorkStatus has metadata
# ---------------------------------------------------------------------------


def test_every_workstatus_has_metadata() -> None:
    """The release gate (#889) consults this. An enum value
    missing from the table is a launch blocker."""
    assert all_statuses_have_metadata() == ()


def test_metadata_for_returns_canonical_entry() -> None:
    meta = metadata_for(WorkStatus.IN_PROGRESS)
    assert meta.status is WorkStatus.IN_PROGRESS
    assert meta.owner is StateOwner.WORKER
    assert meta.consumes_capacity is True
    assert meta.requires_recovery_lane is True


def test_terminal_states_are_marked() -> None:
    """``DONE`` and ``CANCELLED`` are terminal — no allowed
    transitions out, no capacity consumption, hidden by default."""
    for status in (WorkStatus.DONE, WorkStatus.CANCELLED):
        meta = metadata_for(status)
        assert meta.is_terminal is True
        assert meta.allowed_next == frozenset()
        assert meta.consumes_capacity is False
        assert meta.visible_in_cockpit_default is False


def test_done_counts_as_done_cancelled_does_not() -> None:
    """Metric accounting must distinguish completion from cancel."""
    assert metadata_for(WorkStatus.DONE).counts_as_done is True
    assert metadata_for(WorkStatus.CANCELLED).counts_as_done is False


# ---------------------------------------------------------------------------
# Capacity contract — the #816 fix
# ---------------------------------------------------------------------------


def test_in_progress_consumes_capacity() -> None:
    assert is_capacity_consuming(WorkStatus.IN_PROGRESS) is True


def test_rework_also_consumes_capacity() -> None:
    """The audit's #816 root cause: REWORK was missed by the
    capacity manager because its inline allowlist did not
    include it. Reading from the table fixes that class of bug."""
    assert is_capacity_consuming(WorkStatus.REWORK) is True


def test_review_does_not_consume_capacity() -> None:
    """REVIEW waits on the user — no worker capacity used."""
    assert is_capacity_consuming(WorkStatus.REVIEW) is False


def test_queued_does_not_consume_capacity() -> None:
    assert is_capacity_consuming(WorkStatus.QUEUED) is False


# ---------------------------------------------------------------------------
# Recovery lane contract — the #770 / #771 / #816 fix
# ---------------------------------------------------------------------------


def test_in_progress_requires_recovery_lane() -> None:
    assert requires_recovery_lane(WorkStatus.IN_PROGRESS) is True


def test_rework_requires_recovery_lane() -> None:
    """#816: REWORK must be visited by recovery so dead-worker
    rework does not get stuck."""
    assert requires_recovery_lane(WorkStatus.REWORK) is True


def test_terminal_does_not_require_recovery() -> None:
    assert requires_recovery_lane(WorkStatus.DONE) is False
    assert requires_recovery_lane(WorkStatus.CANCELLED) is False


# ---------------------------------------------------------------------------
# Transition validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "from_s, to_s",
    [
        (WorkStatus.QUEUED, WorkStatus.IN_PROGRESS),
        (WorkStatus.IN_PROGRESS, WorkStatus.REVIEW),
        (WorkStatus.REVIEW, WorkStatus.DONE),
        (WorkStatus.REVIEW, WorkStatus.REWORK),
        (WorkStatus.REWORK, WorkStatus.IN_PROGRESS),
    ],
)
def test_canonical_transitions_allowed(
    from_s: WorkStatus, to_s: WorkStatus
) -> None:
    """The happy-path transitions every flow needs."""
    assert is_transition_allowed(from_s, to_s) is True


@pytest.mark.parametrize(
    "from_s, to_s",
    [
        (WorkStatus.DONE, WorkStatus.IN_PROGRESS),
        (WorkStatus.CANCELLED, WorkStatus.QUEUED),
        (WorkStatus.QUEUED, WorkStatus.DONE),
        (WorkStatus.DRAFT, WorkStatus.REVIEW),
    ],
)
def test_invalid_transitions_rejected(
    from_s: WorkStatus, to_s: WorkStatus
) -> None:
    """Transitions outside the canonical table must be rejected.
    The audit's #806 case (recovery jumping to flow-start) must
    not be allowed."""
    assert is_transition_allowed(from_s, to_s) is False


def test_validate_transition_returns_none_on_allowed() -> None:
    assert (
        validate_transition(
            task_id="t1",
            from_status=WorkStatus.QUEUED,
            to_status=WorkStatus.IN_PROGRESS,
        )
        is None
    )


def test_validate_transition_returns_violation_on_forbidden() -> None:
    """The work service uses this to refuse forbidden transitions
    at write time. #806's recovery-deletes-history came from
    not validating; the validator catches it."""
    v = validate_transition(
        task_id="t1",
        from_status=WorkStatus.DONE,
        to_status=WorkStatus.IN_PROGRESS,
    )
    assert v is not None
    assert v.kind is ViolationKind.INVALID_TRANSITION
    assert "done" in v.detail
    assert "in_progress" in v.detail


# ---------------------------------------------------------------------------
# check_task_invariants — the doctor
# ---------------------------------------------------------------------------


def test_doctor_clean_run_returns_no_violations() -> None:
    """A healthy snapshot must produce zero violations."""
    context = TaskCheckContext(
        tasks=(
            TaskRow(
                task_id="demo/1",
                status=WorkStatus.IN_PROGRESS,
                current_role="worker",
            ),
        ),
        live_worker_session_task_ids=frozenset({"demo/1"}),
        reachable_role_sessions=frozenset({"worker", "reviewer"}),
        recovered_task_ids=frozenset(),
        capacity_consumed_task_ids=frozenset({"demo/1"}),
    )
    assert check_task_invariants(context) == ()


def test_doctor_flags_in_progress_without_owner() -> None:
    """The audit's #770 / #771 root cause."""
    context = TaskCheckContext(
        tasks=(
            TaskRow(
                task_id="demo/1",
                status=WorkStatus.IN_PROGRESS,
                current_role="worker",
            ),
        ),
        live_worker_session_task_ids=frozenset(),
        reachable_role_sessions=frozenset({"worker"}),
    )
    out = check_task_invariants(context)
    assert any(
        v.kind is ViolationKind.IN_PROGRESS_NO_OWNER for v in out
    )


def test_doctor_flags_rework_outside_recovery() -> None:
    """The audit's #816 root cause."""
    context = TaskCheckContext(
        tasks=(
            TaskRow(
                task_id="demo/2",
                status=WorkStatus.REWORK,
                current_role="worker",
            ),
        ),
        live_worker_session_task_ids=frozenset({"demo/2"}),
        reachable_role_sessions=frozenset({"worker"}),
        recovered_task_ids=frozenset(),  # Not visited.
    )
    out = check_task_invariants(context)
    kinds = {v.kind for v in out}
    assert ViolationKind.REWORK_OUTSIDE_RECOVERY_LANE in kinds


def test_doctor_flags_queued_without_role_session() -> None:
    """The audit's case: queued tasks waiting for a role that
    has no live session anywhere."""
    context = TaskCheckContext(
        tasks=(
            TaskRow(
                task_id="demo/3",
                status=WorkStatus.QUEUED,
                current_role="reviewer",
            ),
        ),
        reachable_role_sessions=frozenset({"worker"}),
    )
    out = check_task_invariants(context)
    assert any(
        v.kind is ViolationKind.QUEUED_NO_ROLE_SESSION for v in out
    )


def test_doctor_flags_blocked_with_no_unblock_path() -> None:
    """Blocked task with no recorded dependency and no unblock
    signal is genuinely stuck — surface it so the user can
    decide to cancel or unblock manually."""
    context = TaskCheckContext(
        tasks=(
            TaskRow(
                task_id="demo/4",
                status=WorkStatus.BLOCKED,
                current_role=None,
                blocked_on_dependency=False,
                last_unblock_signal_seconds_ago=None,
            ),
        ),
    )
    out = check_task_invariants(context)
    assert any(
        v.kind is ViolationKind.BLOCKED_NO_UNBLOCK_PATH for v in out
    )


def test_doctor_does_not_flag_blocked_with_dependency() -> None:
    """Blocked + dependency recorded is the normal case — no
    violation."""
    context = TaskCheckContext(
        tasks=(
            TaskRow(
                task_id="demo/5",
                status=WorkStatus.BLOCKED,
                current_role=None,
                blocked_on_dependency=True,
            ),
        ),
    )
    out = check_task_invariants(context)
    assert all(
        v.kind is not ViolationKind.BLOCKED_NO_UNBLOCK_PATH for v in out
    )


def test_doctor_flags_dead_claim_consuming_capacity() -> None:
    """Capacity reservation outliving the worker session is a
    waste of capacity slot — the cockpit must see it (#816)."""
    context = TaskCheckContext(
        tasks=(
            TaskRow(
                task_id="demo/6",
                status=WorkStatus.IN_PROGRESS,
                current_role="worker",
            ),
        ),
        live_worker_session_task_ids=frozenset(),  # No live session.
        capacity_consumed_task_ids=frozenset({"demo/6"}),  # Still counted.
    )
    out = check_task_invariants(context)
    kinds = {v.kind for v in out}
    assert ViolationKind.DEAD_CLAIM_CONSUMES_CAPACITY in kinds


def test_doctor_flags_planner_critic_synthetic_leak() -> None:
    """The audit's #395 root cause: critic / planner synthetic
    tasks leaking into the user task list."""
    context = TaskCheckContext(
        tasks=(
            TaskRow(
                task_id="critic-evaluate-1",
                status=WorkStatus.IN_PROGRESS,
                current_role="worker",
            ),
        ),
        live_worker_session_task_ids=frozenset({"critic-evaluate-1"}),
    )
    out = check_task_invariants(context)
    kinds = {v.kind for v in out}
    assert ViolationKind.PLANNER_CRITIC_LEAKED_INTO_TASKS in kinds


def test_doctor_emits_one_violation_per_kind_per_task() -> None:
    """A task in REWORK without recovery and without a live
    worker should report both the IN_PROGRESS_NO_OWNER and
    REWORK_OUTSIDE_RECOVERY_LANE violations — they're different
    classes of failure."""
    context = TaskCheckContext(
        tasks=(
            TaskRow(
                task_id="demo/7",
                status=WorkStatus.REWORK,
                current_role="worker",
            ),
        ),
        live_worker_session_task_ids=frozenset(),
        recovered_task_ids=frozenset(),
    )
    out = check_task_invariants(context)
    kinds = {v.kind for v in out}
    assert ViolationKind.IN_PROGRESS_NO_OWNER in kinds
    assert ViolationKind.REWORK_OUTSIDE_RECOVERY_LANE in kinds


def _seed_task(svc) -> tuple[str, int]:
    """Create one task on ``svc`` and return ``(project, task_number)``.

    Used by the validator-wiring tests so ``_record_transition``
    has a real foreign-key target. Returns the task identity
    instead of the full Task object — the tests only need the
    project + task_number."""
    task = svc.create(
        title="invariant test",
        description="",
        type="task",
        project="demo",
        flow_template="standard",
        roles={"requester": "user", "worker": "test", "reviewer": "test"},
        priority="normal",
        created_by="test",
    )
    return task.project, task.task_number


def test_record_transition_validates_against_canonical_table(tmp_path) -> None:
    """#899 — work-service _record_transition runs the canonical
    validator and logs a warning on violation. Regression guard
    so a refactor cannot quietly drop the validator wiring."""
    import logging

    from pollypm.work.sqlite_service import SQLiteWorkService

    db_path = tmp_path / "state.db"
    captured: list[str] = []

    class _CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    handler = _CaptureHandler(level=logging.WARNING)
    work_logger = logging.getLogger("pollypm.work.sqlite_service")
    work_logger.addHandler(handler)
    work_logger.setLevel(logging.WARNING)
    try:
        with SQLiteWorkService(db_path=db_path) as svc:
            project, task_number = _seed_task(svc)
            # Force a known-invalid transition through the central
            # write site. ``done -> in_progress`` is forbidden by
            # the canonical table.
            svc._record_transition(
                project=project,
                task_number=task_number,
                from_state="done",
                to_state="in_progress",
                actor="test",
            )
    finally:
        work_logger.removeHandler(handler)

    assert any(
        "violates canonical invariant" in msg
        and "done -> in_progress" in msg
        for msg in captured
    )


def test_record_transition_swallows_validator_exceptions(
    monkeypatch, tmp_path,
) -> None:
    """#899 — a broken validator must never break a write. The
    transition completes; an unexpected validator failure logs
    via logger.exception (visible in tests as an ERROR record)."""
    from pollypm.work.sqlite_service import SQLiteWorkService

    def _explode(**_kwargs):
        raise RuntimeError("synthetic validator failure")

    monkeypatch.setattr(
        "pollypm.task_invariants.validate_transition",
        _explode,
    )

    db_path = tmp_path / "state.db"
    with SQLiteWorkService(db_path=db_path) as svc:
        project, task_number = _seed_task(svc)
        # Must not raise even though the validator does.
        svc._record_transition(
            project=project,
            task_number=task_number,
            from_state="queued",
            to_state="in_progress",
            actor="test",
        )

        # Row is still written.
        rows = svc._conn.execute(
            "SELECT count(*) FROM work_transitions "
            "WHERE task_project = ? AND task_number = ?",
            (project, task_number),
        ).fetchone()
        assert rows[0] == 1


def test_project_idle_uses_canonical_terminal_table() -> None:
    """#899 / #816 — the ``_project_is_idle`` helper now derives
    its non-terminal set from the canonical metadata table, so a
    REWORK task correctly keeps a project non-idle. Earlier the
    inline list omitted REWORK and quietly broke milestone digest
    flushing for rejected tasks."""
    from pollypm.notification_staging import _project_is_idle
    from pollypm.work.models import WorkStatus

    class _Task:
        def __init__(self, status: WorkStatus) -> None:
            self.work_status = status

    class _Svc:
        def __init__(self, statuses: list[WorkStatus]) -> None:
            self._tasks = [_Task(s) for s in statuses]

        def list_tasks(self, project: str):
            return self._tasks

    # REWORK keeps the project non-idle.
    assert _project_is_idle(_Svc([WorkStatus.REWORK]), "demo") is False
    # Only terminal states → idle.
    assert _project_is_idle(_Svc([WorkStatus.DONE, WorkStatus.CANCELLED]), "demo") is True
    # Empty project → idle (matches docstring contract).
    assert _project_is_idle(_Svc([]), "demo") is True
    # Drafts alone → idle (drafts don't block per docstring).
    assert _project_is_idle(_Svc([WorkStatus.DRAFT]), "demo") is True


def test_violation_summary_is_actionable() -> None:
    """The summary is rendered in the cockpit. It must name the
    task id and the violation kind clearly."""
    v = InvariantViolation(
        kind=ViolationKind.IN_PROGRESS_NO_OWNER,
        task_id="demo/9",
        detail="no live worker session",
    )
    s = v.summary
    assert "demo/9" in s
    assert "in_progress_no_owner" in s
    assert "no live worker session" in s
