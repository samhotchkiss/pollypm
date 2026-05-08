"""Tests for the plan-review backstop emit + watchdog backfill (#1511).

The fix has two surfaces:

* :mod:`pollypm.work.plan_review_emit` — backstop emit invoked from
  ``service_transitions.on_task_done``. Any plan-shaped task (label
  ``poc-plan`` / ``plan`` / ``project-plan``) on a non-plan_project
  flow that reaches done gets a ``plan_review`` notify message + inbox
  task auto-emitted.
* :func:`pollypm.audit.watchdog._detect_plan_review_missing` —
  forensic scan that fires for any tracked project whose recent ``done``
  plan-shaped task has no matching ``plan_review`` row in the messages
  table. The cadence handler routes the finding to the same emit path
  so old projects (coffeeboardnm, the live witness) self-heal on first
  watchdog tick after the fix lands.

Tests cover three contracts:

1. The post-done hook fires for the standard-flow plan-shaped case and
   skips for plan_project / non-plan-shaped tasks.
2. The watchdog detector fires when no plan_review exists and silences
   when one already does.
3. Idempotence: running the watchdog twice does not double-emit. The
   second run sees the first emit's row and no-ops.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from pollypm.audit.watchdog import (
    RULE_PLAN_REVIEW_MISSING,
    Finding,
    WatchdogConfig,
    _detect_plan_review_missing,
    format_unstick_brief,
    scan_events,
)
from pollypm.work.models import WorkStatus
from pollypm.work.plan_review_emit import (
    PLAN_REVIEW_LABEL,
    PLAN_SHAPED_LABELS,
    already_has_plan_review_message,
    emit_plan_review_for_task,
    is_plan_shaped_task,
    maybe_emit_plan_review_on_task_done,
    task_is_eligible_for_backstop,
)


# ---------------------------------------------------------------------------
# Fixtures — fakes for the work service / store
# ---------------------------------------------------------------------------


@dataclass
class _FakeTask:
    project: str
    task_number: int
    title: str = "Plan task"
    labels: list[str] = field(default_factory=list)
    work_status: WorkStatus = WorkStatus.DONE
    flow_template_id: str = "standard"
    created_at: datetime | None = None

    @property
    def task_id(self) -> str:
        return f"{self.project}/{self.task_number}"


@dataclass
class _FakeCreatedTask:
    task_id: str


class _FakeSvc:
    """Minimal stand-in for :class:`SQLiteWorkService`.

    Records ``svc.create`` calls so tests can assert on the inbox task
    that the emit path produces. ``_db_path`` is used by the emit
    module to open the messages store; tests pass a tmp_path.
    """

    def __init__(self, *, db_path: Path, task: _FakeTask) -> None:
        self._db_path = db_path
        self._task = task
        self.create_calls: list[dict[str, Any]] = []
        self._next_task_number = task.task_number + 100

    def get(self, task_id: str) -> _FakeTask:
        return self._task

    def create(self, **kwargs: Any) -> _FakeCreatedTask:
        self.create_calls.append(kwargs)
        project = kwargs.get("project") or self._task.project
        number = self._next_task_number
        self._next_task_number += 1
        return _FakeCreatedTask(task_id=f"{project}/{number}")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Bare workspace state.db that ``SQLAlchemyStore`` can open.

    The emit path writes through ``store.enqueue_message`` and
    re-opens to ``store.update_message``; both succeed against an
    auto-migrating empty DB.
    """
    pollypm_dir = tmp_path / ".pollypm"
    pollypm_dir.mkdir(parents=True, exist_ok=True)
    return pollypm_dir / "state.db"


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 5, 8, 17, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Heuristic + eligibility predicates
# ---------------------------------------------------------------------------


def test_plan_shaped_labels_recognised() -> None:
    """The conservative label set covers the live witnesses."""
    assert PLAN_SHAPED_LABELS == frozenset({"poc-plan", "plan", "project-plan"})
    assert is_plan_shaped_task(_FakeTask(project="p", task_number=1, labels=["poc-plan"]))
    assert is_plan_shaped_task(_FakeTask(project="p", task_number=1, labels=["plan", "x"]))
    assert is_plan_shaped_task(_FakeTask(project="p", task_number=1, labels=["project-plan"]))


def test_non_plan_labels_rejected() -> None:
    assert not is_plan_shaped_task(
        _FakeTask(project="p", task_number=1, labels=["bug", "infra"])
    )
    assert not is_plan_shaped_task(_FakeTask(project="p", task_number=1, labels=[]))


def test_eligibility_requires_done_status() -> None:
    base_kwargs = dict(project="p", task_number=1, labels=["poc-plan"])
    assert task_is_eligible_for_backstop(_FakeTask(**base_kwargs))
    in_progress = _FakeTask(**base_kwargs, work_status=WorkStatus.IN_PROGRESS)
    assert not task_is_eligible_for_backstop(in_progress)


def test_eligibility_excludes_plan_project_flow() -> None:
    """The reflection node owns the canonical emit on plan_project."""
    plan_project = _FakeTask(
        project="p",
        task_number=1,
        labels=["poc-plan"],
        flow_template_id="plan_project",
    )
    assert not task_is_eligible_for_backstop(plan_project)
    standard = _FakeTask(
        project="p",
        task_number=1,
        labels=["poc-plan"],
        flow_template_id="standard",
    )
    assert task_is_eligible_for_backstop(standard)


# ---------------------------------------------------------------------------
# Part A — post-done hook + idempotence
# ---------------------------------------------------------------------------


def test_emit_creates_message_and_task(db_path: Path) -> None:
    """First emit writes both the messages row and the inbox task."""
    task = _FakeTask(
        project="coffeeboardnm",
        task_number=1,
        labels=["visual-explainer", "research", "poc-plan"],
        flow_template_id="standard",
        work_status=WorkStatus.DONE,
    )
    svc = _FakeSvc(db_path=db_path, task=task)

    inbox_task_id = emit_plan_review_for_task(
        svc=svc, task=task, actor="audit_watchdog", requester="user",
    )

    # An inbox task was created with the right shape.
    assert inbox_task_id is not None
    assert len(svc.create_calls) == 1
    call = svc.create_calls[0]
    assert call["title"].startswith("Plan ready for review:")
    assert call["project"] == "coffeeboardnm"
    assert call["flow_template"] == "chat"
    assert call["roles"] == {"requester": "user", "operator": "audit_watchdog"}
    labels = set(call["labels"])
    assert PLAN_REVIEW_LABEL in labels
    assert "plan_task:coffeeboardnm/1" in labels
    assert "project:coffeeboardnm" in labels
    assert "notify" in labels

    # And a plan_review message lives in the messages table now.
    assert already_has_plan_review_message(
        db_path=db_path,
        project="coffeeboardnm",
        plan_task_id="coffeeboardnm/1",
    )


def test_emit_is_idempotent(db_path: Path) -> None:
    """A second emit for the same plan_task_id no-ops."""
    task = _FakeTask(
        project="coffeeboardnm",
        task_number=1,
        labels=["poc-plan"],
        flow_template_id="standard",
    )
    svc = _FakeSvc(db_path=db_path, task=task)

    first = emit_plan_review_for_task(svc=svc, task=task)
    assert first is not None
    second = emit_plan_review_for_task(svc=svc, task=task)
    assert second is None
    # Only the first call created an inbox task; the second short-circuits.
    assert len(svc.create_calls) == 1


def test_maybe_emit_skips_plan_project_flow(db_path: Path) -> None:
    """The post-done hook is no-op for plan_project tasks."""
    task = _FakeTask(
        project="p",
        task_number=2,
        labels=["plan", "project-plan"],
        flow_template_id="plan_project",
    )
    svc = _FakeSvc(db_path=db_path, task=task)
    result = maybe_emit_plan_review_on_task_done(svc, task.task_id, "polly")
    assert result is None
    assert svc.create_calls == []
    assert not already_has_plan_review_message(
        db_path=db_path, project="p", plan_task_id=task.task_id,
    )


def test_maybe_emit_skips_non_plan_shaped(db_path: Path) -> None:
    """Tasks without plan-shaped labels are ignored."""
    task = _FakeTask(
        project="p",
        task_number=3,
        labels=["bug", "infra"],
        flow_template_id="standard",
    )
    svc = _FakeSvc(db_path=db_path, task=task)
    result = maybe_emit_plan_review_on_task_done(svc, task.task_id, "polly")
    assert result is None
    assert svc.create_calls == []


def test_maybe_emit_fires_for_standard_flow_plan(db_path: Path) -> None:
    """The savethenovel/coffeeboardnm class — standard-flow + poc-plan."""
    task = _FakeTask(
        project="coffeeboardnm",
        task_number=1,
        labels=["visual-explainer", "research", "poc-plan"],
        flow_template_id="standard",
    )
    svc = _FakeSvc(db_path=db_path, task=task)

    result = maybe_emit_plan_review_on_task_done(svc, task.task_id, "polly")

    assert result is not None
    assert len(svc.create_calls) == 1
    assert already_has_plan_review_message(
        db_path=db_path,
        project="coffeeboardnm",
        plan_task_id="coffeeboardnm/1",
    )


# ---------------------------------------------------------------------------
# Part B — watchdog detector
# ---------------------------------------------------------------------------


def test_watchdog_fires_when_no_plan_review_present(now: datetime) -> None:
    """Stranded plan-shaped done task with no plan_review row → finding."""
    config = WatchdogConfig()
    task = _FakeTask(
        project="coffeeboardnm",
        task_number=1,
        labels=["poc-plan"],
        flow_template_id="standard",
        created_at=now - timedelta(hours=2),
    )
    findings = _detect_plan_review_missing(
        now=now,
        config=config,
        done_plan_tasks=[task],
        plan_review_present=lambda project, task_id: False,
    )
    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule == RULE_PLAN_REVIEW_MISSING
    assert finding.project == "coffeeboardnm"
    assert finding.subject == "coffeeboardnm/1"
    assert finding.metadata["plan_task_id"] == "coffeeboardnm/1"
    assert finding.metadata["flow_template_id"] == "standard"


def test_watchdog_silent_when_plan_review_already_present(now: datetime) -> None:
    """Probe returns True → no finding (idempotence)."""
    config = WatchdogConfig()
    task = _FakeTask(
        project="coffeeboardnm",
        task_number=1,
        labels=["poc-plan"],
        flow_template_id="standard",
        created_at=now - timedelta(hours=2),
    )
    findings = _detect_plan_review_missing(
        now=now,
        config=config,
        done_plan_tasks=[task],
        plan_review_present=lambda project, task_id: True,
    )
    assert findings == []


def test_watchdog_silent_when_task_outside_lookback(now: datetime) -> None:
    """A plan-shaped task created 30 days ago is past the 14-day window."""
    config = WatchdogConfig()
    task = _FakeTask(
        project="oldproj",
        task_number=1,
        labels=["plan"],
        flow_template_id="standard",
        created_at=now - timedelta(days=30),
    )
    findings = _detect_plan_review_missing(
        now=now,
        config=config,
        done_plan_tasks=[task],
        plan_review_present=lambda project, task_id: False,
    )
    assert findings == []


def test_watchdog_noop_without_inputs(now: datetime) -> None:
    """No done_plan_tasks / no probe → detector is a no-op."""
    config = WatchdogConfig()
    assert _detect_plan_review_missing(
        now=now, config=config, done_plan_tasks=None, plan_review_present=None,
    ) == []
    assert _detect_plan_review_missing(
        now=now, config=config, done_plan_tasks=[], plan_review_present=lambda *a: False,
    ) == []


def test_scan_events_threads_plan_review_inputs(now: datetime) -> None:
    """End-to-end: scan_events forwards the new inputs to the detector."""
    config = WatchdogConfig()
    task = _FakeTask(
        project="coffeeboardnm",
        task_number=1,
        labels=["poc-plan"],
        flow_template_id="standard",
        created_at=now - timedelta(hours=2),
    )
    findings = scan_events(
        events=[],
        now=now,
        config=config,
        done_plan_tasks=[task],
        plan_review_present=lambda project, task_id: False,
    )
    matched = [f for f in findings if f.rule == RULE_PLAN_REVIEW_MISSING]
    assert len(matched) == 1
    assert matched[0].subject == "coffeeboardnm/1"


# ---------------------------------------------------------------------------
# Idempotence: simulating two watchdog ticks
# ---------------------------------------------------------------------------


def test_watchdog_double_run_does_not_double_emit(
    db_path: Path, now: datetime,
) -> None:
    """Two-tick idempotence — Part A backfills, Part B sees the row, silences.

    Models the cadence-handler flow: tick 1 fires the finding, the
    cadence calls ``emit_plan_review_for_task`` to backfill, and tick 2
    runs the detector again — the probe now reports True so no second
    finding fires.
    """
    config = WatchdogConfig()
    task = _FakeTask(
        project="coffeeboardnm",
        task_number=1,
        labels=["poc-plan"],
        flow_template_id="standard",
        created_at=now - timedelta(hours=2),
    )
    svc = _FakeSvc(db_path=db_path, task=task)

    def probe(project: str, plan_task_id: str) -> bool:
        return already_has_plan_review_message(
            db_path=db_path, project=project, plan_task_id=plan_task_id,
        )

    # Tick 1: finding fires, cadence handler emits the missing card.
    tick1 = _detect_plan_review_missing(
        now=now,
        config=config,
        done_plan_tasks=[task],
        plan_review_present=probe,
    )
    assert len(tick1) == 1
    backfilled = emit_plan_review_for_task(svc=svc, task=task, actor="audit_watchdog")
    assert backfilled is not None
    assert len(svc.create_calls) == 1

    # Tick 2: same scan, but the probe now returns True so no finding.
    tick2 = _detect_plan_review_missing(
        now=now,
        config=config,
        done_plan_tasks=[task],
        plan_review_present=probe,
    )
    assert tick2 == []

    # And calling emit again is also idempotent.
    second_emit = emit_plan_review_for_task(svc=svc, task=task)
    assert second_emit is None
    assert len(svc.create_calls) == 1


# ---------------------------------------------------------------------------
# Brief rendering — ensures the new rule has its tailored block
# ---------------------------------------------------------------------------


def test_format_unstick_brief_includes_plan_review_block() -> None:
    finding = Finding(
        rule=RULE_PLAN_REVIEW_MISSING,
        project="coffeeboardnm",
        subject="coffeeboardnm/1",
        message="Plan-shaped task coffeeboardnm/1 reached done with no plan_review.",
        recommendation="Backfill via plan_review_emit.",
        metadata={
            "plan_task_id": "coffeeboardnm/1",
            "flow_template_id": "standard",
            "labels": ["poc-plan", "research"],
            "created_at": "2026-05-01T00:00:00+00:00",
        },
    )
    brief = format_unstick_brief(finding)
    assert "WATCHDOG ESCALATION" in brief
    assert "plan_review_missing" in brief
    assert "coffeeboardnm/1" in brief
    assert "flow=standard" in brief
    assert "plan_review_emit" in brief
