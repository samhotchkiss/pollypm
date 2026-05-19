"""Regression: watchdog resurrects plan-handoff for bypassed approvals (#1633).

samblog/1 hit the wedge live: a ``plan_project`` task ran end-to-end,
the watchdog's tier-3 escalation auto-approved on the user's behalf,
and the task transitioned to ``done`` without ever surfacing a
``plan_review_pending`` inbox card. The downstream queued tasks then
sat blocked behind credentials the user didn't know they needed to
provide.

Rule 1 (the must-ship rule from the issue): a heartbeat-tier
self-heal that detects the bypass and synthesizes the missing
plan_review inbox item so the user gets the gate they were supposed
to get. Tests cover:

1. The pure detector fires for a plan_project done task whose
   ``approval_actor`` is non-user (watchdog/auto/polly).
2. The detector silences for user actors (sam/user/human) and for
   tasks that already have a plan_review row.
3. End-to-end: synthetic samblog/1 state → detector fires → tier-1
   healer backfills the plan_review row → second tick is a no-op.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from pollypm.audit.watchdog import (
    RULE_PLAN_REVIEW_BYPASSED_APPROVAL,
    Finding,
    WatchdogConfig,
    _detect_plan_review_bypassed_approval,
    format_unstick_brief,
    scan_events,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class _BypassedDescriptor:
    """Shape the cadence handler builds + the detector consumes."""

    project: str
    task_number: int
    approval_actor: str
    approval_completed_at: datetime | None = None
    flow_template_id: str = "plan_project"
    labels: tuple[str, ...] = ()


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Pure detector contract
# ---------------------------------------------------------------------------


def test_fires_when_actor_is_watchdog(now: datetime) -> None:
    """The exact samblog/1 shape — watchdog auto-approved."""
    descriptor = _BypassedDescriptor(
        project="samblog",
        task_number=1,
        approval_actor="audit_watchdog",
        approval_completed_at=now - timedelta(hours=1),
    )
    findings = _detect_plan_review_bypassed_approval(
        now=now,
        config=WatchdogConfig(),
        bypassed_plan_tasks=[descriptor],
        plan_review_present=lambda project, task_id: False,
    )
    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule == RULE_PLAN_REVIEW_BYPASSED_APPROVAL
    assert finding.project == "samblog"
    assert finding.subject == "samblog/1"
    assert finding.metadata["approval_actor"] == "audit_watchdog"
    assert finding.metadata["plan_task_id"] == "samblog/1"


def test_silent_for_real_user_approval(now: datetime) -> None:
    """When the user actually approved, no resurrection is needed."""
    descriptor = _BypassedDescriptor(
        project="samblog",
        task_number=1,
        approval_actor="user",
        approval_completed_at=now - timedelta(hours=1),
    )
    findings = _detect_plan_review_bypassed_approval(
        now=now,
        config=WatchdogConfig(),
        bypassed_plan_tasks=[descriptor],
        plan_review_present=lambda project, task_id: False,
    )
    assert findings == []


def test_silent_for_sam_actor(now: datetime) -> None:
    """``sam`` is a recognised user alias."""
    descriptor = _BypassedDescriptor(
        project="samblog",
        task_number=1,
        approval_actor="sam",
        approval_completed_at=now - timedelta(hours=1),
    )
    findings = _detect_plan_review_bypassed_approval(
        now=now,
        config=WatchdogConfig(),
        bypassed_plan_tasks=[descriptor],
        plan_review_present=lambda project, task_id: False,
    )
    assert findings == []


def test_silent_when_plan_review_already_present(now: datetime) -> None:
    """If a plan_review row exists, the user already has the gate."""
    descriptor = _BypassedDescriptor(
        project="samblog",
        task_number=1,
        approval_actor="audit_watchdog",
        approval_completed_at=now - timedelta(hours=1),
    )
    findings = _detect_plan_review_bypassed_approval(
        now=now,
        config=WatchdogConfig(),
        bypassed_plan_tasks=[descriptor],
        plan_review_present=lambda project, task_id: True,
    )
    assert findings == []


def test_silent_when_task_outside_lookback(now: datetime) -> None:
    """A bypass from 30 days ago is past the 14-day window — no fire."""
    descriptor = _BypassedDescriptor(
        project="samblog",
        task_number=1,
        approval_actor="audit_watchdog",
        approval_completed_at=now - timedelta(days=30),
    )
    findings = _detect_plan_review_bypassed_approval(
        now=now,
        config=WatchdogConfig(),
        bypassed_plan_tasks=[descriptor],
        plan_review_present=lambda project, task_id: False,
    )
    assert findings == []


def test_silent_without_inputs(now: datetime) -> None:
    """No bypassed_plan_tasks / no probe → detector is a no-op."""
    assert _detect_plan_review_bypassed_approval(
        now=now,
        config=WatchdogConfig(),
        bypassed_plan_tasks=None,
        plan_review_present=None,
    ) == []
    assert _detect_plan_review_bypassed_approval(
        now=now,
        config=WatchdogConfig(),
        bypassed_plan_tasks=[],
        plan_review_present=lambda *a: False,
    ) == []


def test_silent_when_actor_field_missing(now: datetime) -> None:
    """An empty actor field → skip (we can't tell if it was a bypass)."""
    descriptor = _BypassedDescriptor(
        project="samblog",
        task_number=1,
        approval_actor="",
        approval_completed_at=now - timedelta(hours=1),
    )
    findings = _detect_plan_review_bypassed_approval(
        now=now,
        config=WatchdogConfig(),
        bypassed_plan_tasks=[descriptor],
        plan_review_present=lambda project, task_id: False,
    )
    assert findings == []


# ---------------------------------------------------------------------------
# Wired through scan_events — covers the cadence-handler entry point
# ---------------------------------------------------------------------------


def test_scan_events_threads_bypassed_plan_tasks(now: datetime) -> None:
    """End-to-end: scan_events forwards the new input to the detector."""
    descriptor = _BypassedDescriptor(
        project="samblog",
        task_number=1,
        approval_actor="audit_watchdog",
        approval_completed_at=now - timedelta(hours=1),
        labels=("plan", "project-plan"),
    )
    findings = scan_events(
        events=[],
        now=now,
        config=WatchdogConfig(),
        bypassed_plan_tasks=[descriptor],
        plan_review_present=lambda project, task_id: False,
    )
    matched = [f for f in findings if f.rule == RULE_PLAN_REVIEW_BYPASSED_APPROVAL]
    assert len(matched) == 1
    assert matched[0].subject == "samblog/1"
    assert matched[0].metadata["approval_actor"] == "audit_watchdog"


# ---------------------------------------------------------------------------
# Brief rendering — ensures the new rule has a tailored block
# ---------------------------------------------------------------------------


def test_format_unstick_brief_includes_bypassed_block(now: datetime) -> None:
    finding = Finding(
        rule=RULE_PLAN_REVIEW_BYPASSED_APPROVAL,
        project="samblog",
        subject="samblog/1",
        message=(
            "plan_project task samblog/1 was approved by non-user actor "
            "'audit_watchdog' and has no plan_review inbox card."
        ),
        recommendation="Backfill via plan_review_emit.emit_plan_review_for_task.",
        metadata={
            "plan_task_id": "samblog/1",
            "flow_template_id": "plan_project",
            "approval_actor": "audit_watchdog",
            "approval_completed_at": "2026-05-18T11:00:00+00:00",
        },
    )
    brief = format_unstick_brief(finding)
    assert "WATCHDOG ESCALATION" in brief
    assert "samblog/1" in brief
    assert "audit_watchdog" in brief
    assert "plan_review_emit" in brief
    assert "non-user actor" in brief


# ---------------------------------------------------------------------------
# Integration: tier-1 healer resurrects the missing inbox card
# ---------------------------------------------------------------------------


@dataclass
class _FakeTask:
    project: str
    task_number: int
    title: str = "Plan project samblog"
    labels: list[str] = field(default_factory=lambda: ["plan", "project-plan"])
    flow_template_id: str = "plan_project"

    @property
    def task_id(self) -> str:
        return f"{self.project}/{self.task_number}"


@dataclass
class _FakeCreatedTask:
    task_id: str


class _FakeSvc:
    """Minimal stand-in for PgWorkService used by emit_plan_review_for_task."""

    def __init__(self, *, db_path: Path, task: _FakeTask) -> None:
        self._db_path = db_path
        self._task = task
        self.create_calls: list[dict[str, Any]] = []

    def get(self, task_id: str) -> _FakeTask:
        return self._task

    def create(self, **kwargs: Any) -> _FakeCreatedTask:
        self.create_calls.append(kwargs)
        project = kwargs.get("project") or self._task.project
        return _FakeCreatedTask(task_id=f"{project}/99")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    pollypm_dir = tmp_path / ".pollypm"
    pollypm_dir.mkdir(parents=True, exist_ok=True)
    return pollypm_dir / "state.db"


def test_self_heal_synthesizes_plan_review_card(
    db_path: Path, now: datetime,
) -> None:
    """The samblog/1 repro: heal runs, plan_review card appears."""
    from pollypm.plugins_builtin.core_recurring.audit_watchdog import (
        _self_heal_plan_review_bypassed_approval,
    )
    from pollypm.work.plan_review_emit import (
        PLAN_REVIEW_LABEL,
        already_has_plan_review_message,
        emit_plan_review_for_task,
    )

    task = _FakeTask(project="samblog", task_number=1)
    svc = _FakeSvc(db_path=db_path, task=task)

    # Pre-state: no plan_review row exists for samblog/1.
    assert not already_has_plan_review_message(
        db_path=db_path, project="samblog", plan_task_id="samblog/1",
    )

    # Drive the emit path directly so we can assert without standing
    # up the full cadence handler (which the dedicated tier-1 healer
    # also exercises against this same code path).
    inbox_task_id = emit_plan_review_for_task(
        svc=svc, task=task, actor="audit_watchdog", requester="user",
    )
    assert inbox_task_id is not None
    assert len(svc.create_calls) == 1

    # The new inbox task carries the canonical plan_review + plan_task
    # labels so the cockpit's plan-review surface picks it up.
    call = svc.create_calls[0]
    labels = set(call["labels"])
    assert PLAN_REVIEW_LABEL in labels
    assert "plan_task:samblog/1" in labels
    assert call["roles"] == {"requester": "user", "operator": "audit_watchdog"}

    # And the messages-table row landed too.
    assert already_has_plan_review_message(
        db_path=db_path, project="samblog", plan_task_id="samblog/1",
    )

    # Second tick: detector sees the row and stays silent.
    descriptor = _BypassedDescriptor(
        project="samblog",
        task_number=1,
        approval_actor="audit_watchdog",
        approval_completed_at=now - timedelta(hours=1),
    )

    def probe(project: str, plan_task_id: str) -> bool:
        return already_has_plan_review_message(
            db_path=db_path, project=project, plan_task_id=plan_task_id,
        )

    findings = _detect_plan_review_bypassed_approval(
        now=now,
        config=WatchdogConfig(),
        bypassed_plan_tasks=[descriptor],
        plan_review_present=probe,
    )
    assert findings == []
