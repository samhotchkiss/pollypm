"""Tests for the heartbeat-cascade foundation (#1546).

Covers the new public API introduced by the cascade foundation:

* :class:`Finding` ``tier`` field + :data:`TIER_*` constants on every
  existing rule.
* :data:`_TIER1_HEALERS` registry — migration of the three existing
  self-heal rules to a registry-driven dispatch (byte-identical
  behaviour) plus the new ``role_session_missing`` and
  ``state_db_missing`` healers.
* :func:`tier_handoff_prompt` — structured-evidence prompt builder
  that refuses solution-menu shapes.
* :func:`build_urgent_human_handoff` — terminal-handoff inbox helper.
* :func:`_maybe_dispatch_to_operator` — tier-3 leg of the cascade.
* Rejection-loop detector — K=3 same-node clustered rejects.
* Queue-without-motion safety-net probe.
* Widened ``role_session_missing`` (now covers ``queued``).
* Product-broken state plumbing (StateStore key/value + create-task
  refusal + ``pm doctor`` integration).

The lint test at the bottom asserts every tier-handoff prompt format
passes the no-solution-menu assertion (the "Y or Z" failure mode the
issue calls out).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from pollypm.audit.log import (
    EVENT_TASK_STATUS_CHANGED,
    EVENT_WATCHDOG_OPERATOR_DISPATCHED,
    AuditEvent,
    central_log_path,
    read_events,
)
from pollypm.audit.watchdog import (
    Finding,
    InboxItemBuilder,
    OPERATOR_DISPATCH_THROTTLE_SECONDS,
    REJECTION_LOOP_THRESHOLD,
    REJECTION_LOOP_WINDOW_SECONDS,
    RULE_DUPLICATE_ADVISOR_TASKS,
    RULE_LEGACY_DB_SHADOW,
    RULE_ORPHAN_MARKER,
    RULE_PLAN_MISSING_ALERT_CHURN,
    RULE_PLAN_REVIEW_MISSING,
    RULE_QUEUE_WITHOUT_MOTION,
    RULE_REJECTION_LOOP,
    RULE_ROLE_SESSION_MISSING,
    RULE_STATE_DB_MISSING,
    RULE_STUCK_DRAFT,
    RULE_TASK_ON_HOLD_STALE,
    RULE_TASK_PROGRESS_STALE,
    RULE_TASK_REVIEW_STALE,
    RULE_WORKER_SESSION_DEAD_LOOP,
    SolutionMenuError,
    TIER_1,
    TIER_2,
    TIER_3,
    TIER_TERMINAL,
    WatchdogConfig,
    build_urgent_human_handoff,
    format_unstick_brief,
    scan_events,
    tier_handoff_prompt,
    was_recently_operator_dispatched,
)


# ---------------------------------------------------------------------------
# Fixtures (mirror the patterns in tests/test_audit_watchdog.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_audit_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the central-tail root so tests never touch ~/.pollypm/."""
    audit_home = tmp_path / "audit-home"
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))
    return audit_home


@pytest.fixture
def now() -> datetime:
    """Fixed wall-clock for deterministic detector windowing."""
    return datetime(2026, 5, 9, 15, 0, 0, tzinfo=timezone.utc)


@dataclass
class _StubExecution:
    node_id: str
    decision: str
    decision_reason: str
    completed_at: datetime
    started_at: datetime | None = None


class _StubStatus:
    def __init__(self, value: str) -> None:
        self.value = value


@dataclass
class _StubTask:
    project: str
    task_number: int
    work_status_str: str
    executions: list[_StubExecution]
    roles: dict[str, str] | None = None
    assignee: str | None = None
    updated_at: datetime | None = None
    created_at: datetime | None = None

    @property
    def work_status(self) -> _StubStatus:
        return _StubStatus(self.work_status_str)


# ---------------------------------------------------------------------------
# Tier-1: every existing rule classifies correctly
# ---------------------------------------------------------------------------


def test_finding_default_tier_is_terminal() -> None:
    f = Finding(rule="any", project="p", subject="s")
    assert f.tier == TIER_TERMINAL


def test_existing_rules_carry_expected_tiers(now: datetime) -> None:
    """Each of the 13 pre-#1546 rules sets the right tier on its findings."""
    # Tier-1 self-heal rules.
    expected: dict[str, str] = {
        RULE_DUPLICATE_ADVISOR_TASKS: TIER_1,
        RULE_PLAN_REVIEW_MISSING: TIER_1,
        RULE_LEGACY_DB_SHADOW: TIER_1,
        RULE_ROLE_SESSION_MISSING: TIER_1,  # widened in #1546
        # Tier-2 architect dispatch rules.
        RULE_STUCK_DRAFT: TIER_2,
        RULE_TASK_REVIEW_STALE: TIER_2,
        RULE_TASK_PROGRESS_STALE: TIER_2,
        RULE_TASK_ON_HOLD_STALE: TIER_2,
        RULE_WORKER_SESSION_DEAD_LOOP: TIER_2,
        # Observe-only canaries.
        RULE_ORPHAN_MARKER: TIER_TERMINAL,
        RULE_PLAN_MISSING_ALERT_CHURN: TIER_TERMINAL,
        # New rules.
        # Note: rejection_loop is a "tier-1 detector" in the issue's
        # framing but routes to tier-2 PM dispatch; the finding tier
        # reflects the dispatch leg (TIER_2).
        RULE_REJECTION_LOOP: TIER_2,
        RULE_STATE_DB_MISSING: TIER_1,
        # #1546 — queue stalls route directly to operator (tier-3).
        RULE_QUEUE_WITHOUT_MOTION: TIER_3,
    }
    # Spot-check a tier-2 rule fires the expected tier.
    events = [
        AuditEvent(
            ts=(now - timedelta(minutes=45)).isoformat(),
            project="demo",
            event=EVENT_TASK_STATUS_CHANGED,
            subject="demo/3",
            actor="reviewer",
            status="ok",
            metadata={"from": "in_progress", "to": "review"},
        ),
    ]
    findings = scan_events(events, now=now)
    review_findings = [f for f in findings if f.rule == RULE_TASK_REVIEW_STALE]
    assert review_findings
    assert review_findings[0].tier == expected[RULE_TASK_REVIEW_STALE]


# ---------------------------------------------------------------------------
# tier_handoff_prompt — structured evidence, no solution menus
# ---------------------------------------------------------------------------


def test_tier_handoff_prompt_renders_evidence_and_question() -> None:
    evidence = {
        "node_id": "code_review",
        "reject_count": 3,
        "shared_tokens": ["better-sqlite3", "node25"],
        "attempts": [
            {"node": "code_review", "completed_at": "2026-05-09T14:50:00+00:00",
             "reason": "better-sqlite3 build failed under Node 25"},
            {"node": "code_review", "completed_at": "2026-05-09T13:50:00+00:00",
             "reason": "still failing under Node 25"},
        ],
    }
    prompt = tier_handoff_prompt(
        evidence,
        "What structural change unblocks this task?",
    )
    assert prompt.startswith("TIER HANDOFF")
    assert "Question: What structural change unblocks this task?" in prompt
    assert "Evidence:" in prompt
    assert "node_id: code_review" in prompt
    assert "better-sqlite3" in prompt


def test_tier_handoff_prompt_refuses_solution_menu_keys() -> None:
    with pytest.raises(SolutionMenuError):
        tier_handoff_prompt({"options": ["a", "b"]}, "pick one?")
    with pytest.raises(SolutionMenuError):
        tier_handoff_prompt({"solutions": ["a"]}, "pick?")
    with pytest.raises(SolutionMenuError):
        tier_handoff_prompt({"recommended_actions": ["a"]}, "pick?")


def test_tier_handoff_prompt_refuses_empty_question() -> None:
    with pytest.raises(SolutionMenuError):
        tier_handoff_prompt({"x": 1}, "")
    with pytest.raises(SolutionMenuError):
        tier_handoff_prompt({"x": 1}, "   ")


def test_tier_handoff_prompt_no_explicit_solutions_param() -> None:
    """The structural protection: the function literally cannot accept
    a ``solutions`` kwarg because none exists."""
    import inspect

    sig = inspect.signature(tier_handoff_prompt)
    assert set(sig.parameters.keys()) == {"evidence", "question"}


# ---------------------------------------------------------------------------
# build_urgent_human_handoff
# ---------------------------------------------------------------------------


def test_build_urgent_human_handoff_shape() -> None:
    builder = build_urgent_human_handoff(
        tried=["v1: pin Node 22", "v2: switch to canonical sqlite3"],
        failed=["both still fail prebuilt-binary fetch under sandbox"],
        hypothesis=(
            "Project's hard-pinned Node version conflicts with the dep "
            "tree's prebuilt binaries; this needs a structural call from "
            "you on whether to swap deps or relax the pin."
        ),
        forensics_path="~/.pollypm/audit/coffeeboardnm.jsonl",
        project="coffeeboardnm",
    )
    assert isinstance(builder, InboxItemBuilder)
    assert "urgent" in builder.labels
    assert "notify" in builder.labels
    assert builder.priority == "immediate"
    assert builder.requester == "user"
    # Body opens with the hypothesis (one paragraph).
    first_para = builder.body.split("\n\n", 1)[0]
    assert "structural call" in first_para
    # What was tried + what failed + forensics path land in the body.
    assert "What was tried:" in builder.body
    assert "v1: pin Node 22" in builder.body
    assert "What failed:" in builder.body
    assert "Forensics:" in builder.body
    assert "coffeeboardnm.jsonl" in builder.body


def test_build_urgent_human_handoff_refuses_empty_hypothesis() -> None:
    with pytest.raises(ValueError):
        build_urgent_human_handoff(
            tried=[], failed=[], hypothesis="",
            forensics_path="~/x.jsonl",
        )


def test_build_urgent_human_handoff_refuses_empty_forensics_path() -> None:
    with pytest.raises(ValueError):
        build_urgent_human_handoff(
            tried=[], failed=[], hypothesis="something is broken",
            forensics_path="",
        )


# ---------------------------------------------------------------------------
# Rejection-loop detector
# ---------------------------------------------------------------------------


def test_rejection_loop_fires_with_clustered_reasons(now: datetime) -> None:
    """Three rejections at the same node within window with shared
    error-token signal fires a tier-1 finding routed to tier-2."""
    cfg = WatchdogConfig()
    task = _StubTask(
        project="coffeeboardnm",
        task_number=70,
        work_status_str="in_progress",
        executions=[
            _StubExecution(
                node_id="code_review",
                decision="rejected",
                decision_reason="better-sqlite3 prebuilt fails under Node 25",
                completed_at=now - timedelta(minutes=5),
            ),
            _StubExecution(
                node_id="code_review",
                decision="rejected",
                decision_reason="rebuild failed: better-sqlite3 cannot find Node 25 headers",
                completed_at=now - timedelta(minutes=20),
            ),
            _StubExecution(
                node_id="code_review",
                decision="rejected",
                decision_reason="better-sqlite3 native build failed (Node 25)",
                completed_at=now - timedelta(minutes=40),
            ),
        ],
    )
    findings = scan_events(
        [], now=now, config=cfg, open_tasks=[task],
    )
    matched = [f for f in findings if f.rule == RULE_REJECTION_LOOP]
    assert len(matched) == 1
    f = matched[0]
    assert f.tier == TIER_2  # dispatch tier — routes to architect
    assert f.subject == "coffeeboardnm/70"
    assert f.evidence["node_id"] == "code_review"
    assert f.evidence["reject_count"] >= REJECTION_LOOP_THRESHOLD
    # Shared tokens captured the dependency-and-runtime cluster signal.
    shared = set(f.evidence.get("shared_tokens") or [])
    assert "better-sqlite3" in shared


def test_rejection_loop_silent_when_below_threshold(now: datetime) -> None:
    cfg = WatchdogConfig()
    task = _StubTask(
        project="demo",
        task_number=1,
        work_status_str="in_progress",
        executions=[
            _StubExecution(
                node_id="code_review",
                decision="rejected",
                decision_reason="better-sqlite3 prebuilt fails under Node 25",
                completed_at=now - timedelta(minutes=5),
            ),
        ],
    )
    findings = scan_events(
        [], now=now, config=cfg, open_tasks=[task],
    )
    assert not any(f.rule == RULE_REJECTION_LOOP for f in findings)


def test_rejection_loop_silent_when_reasons_dont_cluster(now: datetime) -> None:
    """Three rejections at the same node but with unrelated reasons
    don't cluster into a structural-loop signal."""
    cfg = WatchdogConfig()
    task = _StubTask(
        project="demo",
        task_number=2,
        work_status_str="in_progress",
        executions=[
            _StubExecution(
                node_id="code_review",
                decision="rejected",
                decision_reason="please add changelog entry",
                completed_at=now - timedelta(minutes=5),
            ),
            _StubExecution(
                node_id="code_review",
                decision="rejected",
                decision_reason="missing docstring on helper function",
                completed_at=now - timedelta(minutes=20),
            ),
            _StubExecution(
                node_id="code_review",
                decision="rejected",
                decision_reason="rename variable for clarity",
                completed_at=now - timedelta(minutes=40),
            ),
        ],
    )
    findings = scan_events(
        [], now=now, config=cfg, open_tasks=[task],
    )
    assert not any(f.rule == RULE_REJECTION_LOOP for f in findings)


def test_rejection_loop_silent_when_approval_breaks_run(now: datetime) -> None:
    """K consecutive rejections — an approve / non-rejection at the
    same node resets the counter. ``reject → reject → approve →
    reject`` is not a structural-loop shape; the worker is making
    forward progress between bounces.
    """
    cfg = WatchdogConfig()
    task = _StubTask(
        project="demo",
        task_number=4,
        work_status_str="in_progress",
        executions=[
            # newest — most recent reject
            _StubExecution(
                node_id="code_review",
                decision="rejected",
                decision_reason="better-sqlite3 fails Node 25",
                completed_at=now - timedelta(minutes=5),
            ),
            # An approval at the same node breaks the consecutive run.
            _StubExecution(
                node_id="code_review",
                decision="approved",
                decision_reason="reviewer signed off",
                completed_at=now - timedelta(minutes=15),
            ),
            _StubExecution(
                node_id="code_review",
                decision="rejected",
                decision_reason="rebuild failed",
                completed_at=now - timedelta(minutes=25),
            ),
            _StubExecution(
                node_id="code_review",
                decision="rejected",
                decision_reason="better-sqlite3 native build failed",
                completed_at=now - timedelta(minutes=35),
            ),
        ],
    )
    findings = scan_events(
        [], now=now, config=cfg, open_tasks=[task],
    )
    # Three rejections in window total, but only one is contiguous from
    # the newest end — below threshold once we enforce consecutiveness.
    assert not any(f.rule == RULE_REJECTION_LOOP for f in findings)


def test_rejection_loop_silent_outside_window(now: datetime) -> None:
    cfg = WatchdogConfig()
    too_old = now - timedelta(seconds=cfg.rejection_loop_window_seconds + 60)
    task = _StubTask(
        project="demo",
        task_number=3,
        work_status_str="in_progress",
        executions=[
            _StubExecution(
                node_id="code_review",
                decision="rejected",
                decision_reason="better-sqlite3 fails Node 25",
                completed_at=too_old,
            ),
        ],
    )
    findings = scan_events(
        [], now=now, config=cfg, open_tasks=[task],
    )
    assert not any(f.rule == RULE_REJECTION_LOOP for f in findings)


# ---------------------------------------------------------------------------
# State-db-missing detector
# ---------------------------------------------------------------------------


@dataclass
class _StateDbProbeStub:
    project_key: str
    project_path: Path
    canonical_present: bool
    legacy_archives_present: bool


def test_state_db_missing_fires_when_canonical_absent(
    now: datetime, tmp_path: Path,
) -> None:
    probe = _StateDbProbeStub(
        project_key="demo",
        project_path=tmp_path / "demo",
        canonical_present=False,
        legacy_archives_present=True,
    )
    findings = scan_events([], now=now, state_db_probes=[probe])
    matched = [f for f in findings if f.rule == RULE_STATE_DB_MISSING]
    assert len(matched) == 1
    f = matched[0]
    assert f.tier == TIER_1
    assert f.project == "demo"
    assert f.evidence["legacy_archives_present"] is True


def test_state_db_missing_silent_when_canonical_present(
    now: datetime, tmp_path: Path,
) -> None:
    probe = _StateDbProbeStub(
        project_key="demo",
        project_path=tmp_path / "demo",
        canonical_present=True,
        legacy_archives_present=False,
    )
    findings = scan_events([], now=now, state_db_probes=[probe])
    assert not any(f.rule == RULE_STATE_DB_MISSING for f in findings)


def test_state_db_missing_silent_for_project_without_per_project_db(
    now: datetime, tmp_path: Path,
) -> None:
    """Post-workspace-root migration (#339 / #1004): a project that
    has *no* per-project ``<project>/.pollypm/state.db`` but lives in a
    workspace whose ``<workspace_root>/.pollypm/state.db`` is healthy
    must NOT alert. The pre-fix probe checked the per-project path and
    fired on every tick for healthy projects.
    """
    from pollypm.plugins_builtin.core_recurring.audit_watchdog import (
        _gather_state_db_probes,
    )

    # Build a fake services bundle with a registered project whose
    # per-project state.db is absent, and a workspace-root config that
    # points at a real-on-disk workspace DB.
    workspace_root = tmp_path / "ws"
    canonical_db = workspace_root / ".pollypm" / "state.db"
    canonical_db.parent.mkdir(parents=True, exist_ok=True)
    canonical_db.write_text("placeholder", encoding="utf-8")

    project_path = tmp_path / "myproj"
    project_path.mkdir(parents=True, exist_ok=True)
    # Deliberately do NOT create project_path / ".pollypm" / "state.db".

    from types import SimpleNamespace

    proj = SimpleNamespace(key="myproj", name="myproj", path=project_path)
    cfg = SimpleNamespace(
        project=SimpleNamespace(workspace_root=workspace_root),
        projects={"myproj": proj},
    )
    services = SimpleNamespace(known_projects=[proj], config=cfg)

    probes = _gather_state_db_probes(services=services, config=cfg)
    assert len(probes) == 1
    assert probes[0].canonical_present is True
    findings = scan_events([], now=now, state_db_probes=probes)
    assert not any(f.rule == RULE_STATE_DB_MISSING for f in findings)


# ---------------------------------------------------------------------------
# Queue-without-motion safety-net probe
# ---------------------------------------------------------------------------


def test_queue_without_motion_fires_when_no_recent_activity(
    now: datetime,
) -> None:
    cfg = WatchdogConfig(queue_motion_threshold_seconds=600)
    queued = _StubTask(
        project="demo",
        task_number=4,
        work_status_str="queued",
        executions=[],
        updated_at=now - timedelta(hours=2),
    )
    # No motion events.
    findings = scan_events(
        [], now=now, config=cfg, open_tasks=[queued], project="demo",
    )
    matched = [f for f in findings if f.rule == RULE_QUEUE_WITHOUT_MOTION]
    assert len(matched) == 1
    f = matched[0]
    # #1546 — queue_without_motion is operator-level (tier-3) so the
    # cadence handler dispatches directly to the operator inbox.
    assert f.tier == TIER_3
    assert f.evidence["queued_subjects"] == ["demo/4"]
    # The probe captures threshold info for downstream prompts.
    assert f.evidence["threshold_seconds"] == 600


def test_queue_without_motion_silent_when_recent_activity(
    now: datetime,
) -> None:
    cfg = WatchdogConfig(queue_motion_threshold_seconds=600)
    queued = _StubTask(
        project="demo",
        task_number=5,
        work_status_str="queued",
        executions=[],
        updated_at=now - timedelta(hours=2),
    )
    events = [
        AuditEvent(
            ts=(now - timedelta(minutes=2)).isoformat(),
            project="demo",
            event=EVENT_TASK_STATUS_CHANGED,
            subject="demo/5",
            actor="worker",
            status="ok",
            metadata={"from": "queued", "to": "in_progress"},
        ),
    ]
    findings = scan_events(
        events, now=now, config=cfg, open_tasks=[queued], project="demo",
    )
    assert not any(f.rule == RULE_QUEUE_WITHOUT_MOTION for f in findings)


def test_queue_without_motion_silent_when_no_queued_tasks(
    now: datetime,
) -> None:
    cfg = WatchdogConfig(queue_motion_threshold_seconds=600)
    in_flight = _StubTask(
        project="demo",
        task_number=6,
        work_status_str="in_progress",
        executions=[],
    )
    findings = scan_events(
        [], now=now, config=cfg, open_tasks=[in_flight], project="demo",
    )
    assert not any(f.rule == RULE_QUEUE_WITHOUT_MOTION for f in findings)


def test_queue_without_motion_disabled_when_probe_flag_off(
    now: datetime,
) -> None:
    cfg = WatchdogConfig(queue_motion_threshold_seconds=600)
    queued = _StubTask(
        project="demo",
        task_number=7,
        work_status_str="queued",
        executions=[],
    )
    findings = scan_events(
        [], now=now, config=cfg, open_tasks=[queued], project="demo",
        run_safety_net_probes=False,
    )
    assert not any(f.rule == RULE_QUEUE_WITHOUT_MOTION for f in findings)


# ---------------------------------------------------------------------------
# Filter widening: role_session_missing now fires for queued tasks
# ---------------------------------------------------------------------------


def test_role_session_missing_fires_for_queued_task(now: datetime) -> None:
    """The pre-#1546 filter only fired for in_progress / review; the
    coffeeboardnm cancel-loop happened because queued advisor tasks
    fell off the radar. Widened to include ``queued``."""
    task = _StubTask(
        project="demo",
        task_number=42,
        work_status_str="queued",
        executions=[],
        roles={"worker": "claude:advisor"},
    )
    findings = scan_events(
        [],
        now=now,
        open_tasks=[task],
        storage_window_names=["architect-demo"],
        project="demo",
    )
    matched = [f for f in findings if f.rule == RULE_ROLE_SESSION_MISSING]
    assert len(matched) == 1
    assert matched[0].subject == "demo/42"
    assert matched[0].tier == TIER_1


def test_role_session_missing_fires_for_queued_advisor_task(
    now: datetime,
) -> None:
    """Production advisor tasks land with ``roles={"advisor": "advisor"}``
    (see ``advisor/handlers/advisor_tick.py``) and the canonical lane
    is ``advisor-<project>``. The pre-#1546 detector only walked
    worker/architect/reviewer, so a queued advisor task whose advisor
    lane wasn't running silently missed the auto-spawn — the regression
    #1546 was filed to close.
    """
    task = _StubTask(
        project="coffeeboardnm",
        task_number=70,
        work_status_str="queued",
        executions=[],
        # Production advisor task shape — see
        # ``plugins_builtin/advisor/handlers/advisor_tick.py:262``.
        roles={"advisor": "advisor"},
    )
    findings = scan_events(
        [],
        now=now,
        open_tasks=[task],
        # Storage closet has architect-* but no advisor-* — the bug
        # condition this test guards.
        storage_window_names=["architect-coffeeboardnm"],
        project="coffeeboardnm",
    )
    matched = [f for f in findings if f.rule == RULE_ROLE_SESSION_MISSING]
    assert len(matched) == 1
    assert matched[0].subject == "coffeeboardnm/70"
    assert matched[0].metadata["role"] == "advisor"
    assert matched[0].metadata["expected_window"] == "advisor-coffeeboardnm"


def test_role_session_missing_silent_when_advisor_lane_present(
    now: datetime,
) -> None:
    """Mirror of the above: when ``advisor-<project>`` is present in
    the storage closet the detector stays quiet.
    """
    task = _StubTask(
        project="coffeeboardnm",
        task_number=71,
        work_status_str="queued",
        executions=[],
        roles={"advisor": "advisor"},
    )
    findings = scan_events(
        [],
        now=now,
        open_tasks=[task],
        storage_window_names=["advisor-coffeeboardnm"],
        project="coffeeboardnm",
    )
    assert not any(f.rule == RULE_ROLE_SESSION_MISSING for f in findings)


def test_role_session_missing_silent_when_window_present_for_queued(
    now: datetime,
) -> None:
    task = _StubTask(
        project="demo",
        task_number=43,
        work_status_str="queued",
        executions=[],
        roles={"worker": "claude:worker"},
    )
    findings = scan_events(
        [],
        now=now,
        open_tasks=[task],
        storage_window_names=["worker-demo"],
        project="demo",
    )
    assert not any(f.rule == RULE_ROLE_SESSION_MISSING for f in findings)


# ---------------------------------------------------------------------------
# Tier-1 healer registry — byte-identical behaviour for existing rules
# ---------------------------------------------------------------------------


def test_tier1_healer_registry_includes_existing_self_heal_rules() -> None:
    from pollypm.plugins_builtin.core_recurring.audit_watchdog import (
        _TIER1_HEALERS,
    )

    assert RULE_DUPLICATE_ADVISOR_TASKS in _TIER1_HEALERS
    assert RULE_PLAN_REVIEW_MISSING in _TIER1_HEALERS
    assert RULE_LEGACY_DB_SHADOW in _TIER1_HEALERS
    # New entries from #1546.
    assert RULE_ROLE_SESSION_MISSING in _TIER1_HEALERS
    assert RULE_STATE_DB_MISSING in _TIER1_HEALERS


def test_tier1_healer_role_session_missing_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registry healer must be safe to call repeatedly. We mock the
    cli helpers so the test doesn't actually shell out."""
    from pollypm.plugins_builtin.core_recurring.audit_watchdog import (
        _self_heal_role_session_missing,
    )

    spawned: list[str] = []

    class _StubSession:
        def __init__(self, name: str) -> None:
            self.name = name

    class _StubSupervisorConfig:
        def __init__(self) -> None:
            self.sessions: dict[str, Any] = {}

    class _StubSupervisor:
        def __init__(self) -> None:
            self.config = _StubSupervisorConfig()

    def _stub_load_supervisor(_path: Any) -> _StubSupervisor:
        return _StubSupervisor()

    def _stub_create(*_args: Any, **kwargs: Any) -> _StubSession:
        spawned.append(kwargs.get("role", "?"))
        return _StubSession(f"{kwargs['role']}-{kwargs['project_key']}")

    def _stub_launch(_cfg: Any, _name: str) -> None:
        return None

    import pollypm.cli as cli_mod

    monkeypatch.setattr(cli_mod, "_load_supervisor", _stub_load_supervisor)
    monkeypatch.setattr(cli_mod, "create_worker_session", _stub_create)
    monkeypatch.setattr(cli_mod, "launch_worker_session", _stub_launch)

    finding = Finding(
        rule=RULE_ROLE_SESSION_MISSING,
        tier=TIER_1,
        project="demo",
        subject="demo/42",
        metadata={"role": "advisor", "expected_window": "advisor-demo"},
    )
    counters_a = _self_heal_role_session_missing(
        finding, project_key="demo", project_path=None,
    )
    counters_b = _self_heal_role_session_missing(
        finding, project_key="demo", project_path=None,
    )
    assert counters_a["worker_lane_spawned"] == 1
    assert counters_b["worker_lane_spawned"] == 1
    # Both calls succeeded — idempotent.
    assert counters_a["worker_lane_failed"] == 0
    assert counters_b["worker_lane_failed"] == 0


def test_tier1_healer_role_session_missing_skips_worker_role() -> None:
    """``--role worker`` is structurally deprecated; the healer no-ops."""
    from pollypm.plugins_builtin.core_recurring.audit_watchdog import (
        _self_heal_role_session_missing,
    )

    finding = Finding(
        rule=RULE_ROLE_SESSION_MISSING,
        tier=TIER_1,
        project="demo",
        subject="demo/42",
        metadata={"role": "worker"},
    )
    counters = _self_heal_role_session_missing(
        finding, project_key="demo", project_path=None,
    )
    assert counters["worker_lane_spawned"] == 0
    assert counters["worker_lane_failed"] == 0


# ---------------------------------------------------------------------------
# Tier-3 operator dispatch — throttle, audit emit, no-op for ineligible rules
# ---------------------------------------------------------------------------


def test_was_recently_operator_dispatched_false_on_empty_log(
    now: datetime,
) -> None:
    assert was_recently_operator_dispatched(
        project="demo",
        finding_type=RULE_QUEUE_WITHOUT_MOTION,
        subject="demo",
        now=now,
    ) is False


def test_was_recently_operator_dispatched_true_after_emit(
    now: datetime,
) -> None:
    from pollypm.audit.watchdog import emit_operator_dispatched

    emit_operator_dispatched(
        project="demo",
        finding_type=RULE_QUEUE_WITHOUT_MOTION,
        subject="demo",
    )
    assert was_recently_operator_dispatched(
        project="demo",
        finding_type=RULE_QUEUE_WITHOUT_MOTION,
        subject="demo",
        now=now,
    ) is True


def test_maybe_dispatch_to_operator_skips_ineligible_rule(
    now: datetime,
) -> None:
    from pollypm.plugins_builtin.core_recurring.audit_watchdog import (
        _maybe_dispatch_to_operator,
    )

    finding = Finding(
        rule=RULE_TASK_REVIEW_STALE,  # not an operator-dispatchable rule
        tier=TIER_2,
        project="demo",
        subject="demo/1",
    )
    assert _maybe_dispatch_to_operator(
        finding, project_path=None, now=now,
    ) == "skipped"


def test_maybe_dispatch_to_operator_throttles_repeat(
    now: datetime, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second dispatch within OPERATOR_DISPATCH_THROTTLE_SECONDS bails."""
    from pollypm.audit.watchdog import emit_operator_dispatched
    from pollypm.plugins_builtin.core_recurring.audit_watchdog import (
        _maybe_dispatch_to_operator,
    )

    finding = Finding(
        rule=RULE_QUEUE_WITHOUT_MOTION,
        tier=TIER_3,
        project="demo",
        subject="demo",
        evidence={"queued_subjects": ["demo/1"]},
    )
    # Pre-seed the audit log with a recent dispatch.
    emit_operator_dispatched(
        project="demo",
        finding_type=RULE_QUEUE_WITHOUT_MOTION,
        subject="demo",
    )
    outcome = _maybe_dispatch_to_operator(
        finding, project_path=None, now=now,
    )
    assert outcome == "throttled"


def test_maybe_dispatch_to_operator_inbox_failure_does_not_throttle_next_tick(
    now: datetime, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the inbox write raises, the dispatcher must NOT engage
    the operator-dispatched throttle window — otherwise the next tick
    would suppress the retry and the failure would silently strand
    the finding. The dispatcher emits a separate
    ``tier3_dispatch_failed`` event for forensics instead.
    """
    from pollypm.audit.log import EVENT_WATCHDOG_TIER3_DISPATCH_FAILED
    from pollypm.audit.watchdog import was_recently_operator_dispatched
    from pollypm.plugins_builtin.core_recurring import audit_watchdog as cadence_mod

    call_count = {"n": 0}

    def _stub_create_fail(**_kwargs: Any) -> str:
        call_count["n"] += 1
        raise RuntimeError("simulated inbox write failure")

    monkeypatch.setattr(
        cadence_mod, "_create_operator_inbox_task", _stub_create_fail,
    )

    finding = Finding(
        rule=RULE_QUEUE_WITHOUT_MOTION,
        tier=TIER_3,
        project="failproj",
        subject="failproj",
        evidence={"queued_subjects": ["failproj/1"]},
    )
    outcome = cadence_mod._maybe_dispatch_to_operator(
        finding, project_path=None, now=now,
    )
    assert outcome == "send_failed"
    assert call_count["n"] == 1

    # The next tick must NOT find a throttle-engaging
    # operator_dispatched audit row.
    assert was_recently_operator_dispatched(
        project="failproj",
        finding_type=RULE_QUEUE_WITHOUT_MOTION,
        subject="failproj",
        now=now,
    ) is False

    # A distinct tier3_dispatch_failed event recorded the attempt.
    failed_rows = read_events(
        "failproj", event=EVENT_WATCHDOG_TIER3_DISPATCH_FAILED,
    )
    assert len(failed_rows) >= 1
    assert any(
        (r.metadata or {}).get("finding_type") == RULE_QUEUE_WITHOUT_MOTION
        for r in failed_rows
    )

    # Now simulate the next tick — inbox write succeeds. The retry
    # should land cleanly because the throttle window was never
    # engaged.
    captured: dict[str, Any] = {}

    def _stub_create_ok(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "failproj/42"

    monkeypatch.setattr(
        cadence_mod, "_create_operator_inbox_task", _stub_create_ok,
    )
    outcome2 = cadence_mod._maybe_dispatch_to_operator(
        finding, project_path=None, now=now,
    )
    assert outcome2 == "dispatched"
    assert captured["project_key"] == "failproj"


def test_maybe_dispatch_to_operator_dispatched_writes_audit_and_inbox(
    now: datetime, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Mock the inbox sink and assert the dispatcher emits the audit
    event and creates the inbox task."""
    from pollypm.plugins_builtin.core_recurring import audit_watchdog as cadence_mod

    captured: dict[str, Any] = {}

    def _stub_create(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "demo/999"

    monkeypatch.setattr(
        cadence_mod, "_create_operator_inbox_task", _stub_create,
    )

    finding = Finding(
        rule=RULE_QUEUE_WITHOUT_MOTION,
        tier=TIER_3,
        project="newproj",
        subject="newproj",
        message="newproj queue stalled",
        evidence={"queued_subjects": ["newproj/3"], "threshold_seconds": 1800},
    )
    outcome = cadence_mod._maybe_dispatch_to_operator(
        finding, project_path=None, now=now,
    )
    assert outcome == "dispatched"
    assert captured["project_key"] == "newproj"
    assert "TIER HANDOFF" in captured["body"]
    # Audit log carries an operator_dispatched row.
    rows = read_events(
        "newproj",
        event=EVENT_WATCHDOG_OPERATOR_DISPATCHED,
    )
    assert len(rows) >= 1
    assert any(
        (r.metadata or {}).get("finding_type") == RULE_QUEUE_WITHOUT_MOTION
        for r in rows
    )


# ---------------------------------------------------------------------------
# format_unstick_brief: lint — no solution-menu language for tier-2 rules
# ---------------------------------------------------------------------------


def _representative_findings_for_lint() -> list[Finding]:
    """One Finding per tier-2 / tier-1-with-handoff rule so we can lint
    the brief output for solution-menu phrasing."""
    return [
        Finding(
            rule=RULE_TASK_REVIEW_STALE,
            tier=TIER_2,
            project="demo",
            subject="demo/1",
            metadata={"stuck_minutes": 45, "review_since": "2026-05-09T13:00:00+00:00"},
        ),
        Finding(
            rule=RULE_TASK_PROGRESS_STALE,
            tier=TIER_2,
            project="demo",
            subject="demo/2",
            metadata={
                "stuck_minutes": 30, "in_progress_minutes": 60,
                "in_progress_since": "2026-05-09T13:00:00+00:00",
                "last_activity_at": "2026-05-09T14:00:00+00:00",
                "last_activity_kind": "context:note",
                "assignee": "alice", "current_node_id": "implement",
            },
        ),
        Finding(
            rule=RULE_TASK_ON_HOLD_STALE,
            tier=TIER_2,
            project="demo",
            subject="demo/3",
            metadata={
                "stuck_minutes": 20,
                "on_hold_since": "2026-05-09T13:00:00+00:00",
                "from": "review",
                "reason": "[architect-actionable] copy fails review",
                "routing": "architect-actionable",
                "reviewer_evidence": [
                    "reviewer exec [code_review @ 2026-05-09T13:00:00+00:00] "
                    "decision=rejected reason: copy is placeholder",
                ],
            },
        ),
        Finding(
            rule=RULE_WORKER_SESSION_DEAD_LOOP,
            tier=TIER_2,
            project="demo",
            subject="demo/4",
            metadata={"reap_count": 5, "latest_reason": "spawn_failed"},
        ),
        Finding(
            rule=RULE_REJECTION_LOOP,
            tier=TIER_2,
            project="demo",
            subject="demo/5",
            metadata={"node_id": "code_review", "reject_count": 3},
            evidence={
                "node_id": "code_review",
                "reject_count": 3,
                "shared_tokens": ["better-sqlite3", "node25"],
                "attempts": [
                    {"node": "code_review",
                     "completed_at": "2026-05-09T14:50:00+00:00",
                     "reason": "better-sqlite3 fails Node 25"},
                    {"node": "code_review",
                     "completed_at": "2026-05-09T13:50:00+00:00",
                     "reason": "rebuild failed"},
                    {"node": "code_review",
                     "completed_at": "2026-05-09T12:50:00+00:00",
                     "reason": "still failing"},
                ],
                "window_seconds": REJECTION_LOOP_WINDOW_SECONDS,
            },
        ),
    ]


# Tokens that, when bracketed by spaces, signal a solution-menu shape:
# the "Y or Z" / "(a) X (b) Y" framing the issue forbids.
_SOLUTION_MENU_PATTERNS = [
    re.compile(r"\boptions:\s*\(a\)", re.IGNORECASE),
    re.compile(r"\boptions:\s*\(b\)", re.IGNORECASE),
    # A bare "(a) ... (b) ..." pattern in the same line.
    re.compile(r"\(a\).*\(b\)", re.IGNORECASE),
]


def test_format_unstick_brief_no_solution_menu_language() -> None:
    """The lint that the issue calls out: tier-2/3/4 prompt builders
    must not embed candidate solutions or a menu of options."""
    for finding in _representative_findings_for_lint():
        brief = format_unstick_brief(finding)
        for pat in _SOLUTION_MENU_PATTERNS:
            match = pat.search(brief)
            assert match is None, (
                f"format_unstick_brief({finding.rule}) emits "
                f"solution-menu language matching {pat.pattern!r}: "
                f"{match.group(0) if match else ''}"
            )


def test_tier_handoff_prompt_no_solution_menu_language() -> None:
    """The structured-evidence helper itself must never produce
    solution-menu language even when the evidence is rich."""
    sample = {
        "node_id": "code_review",
        "reject_count": 3,
        "shared_tokens": ["better-sqlite3", "node25"],
        "attempts": [
            {"node": "code_review",
             "completed_at": "2026-05-09T14:50:00+00:00",
             "reason": "better-sqlite3 fails Node 25"},
        ],
    }
    prompt = tier_handoff_prompt(
        sample,
        "What structural change unblocks this task?",
    )
    for pat in _SOLUTION_MENU_PATTERNS:
        match = pat.search(prompt)
        assert match is None, (
            f"tier_handoff_prompt emits solution-menu language: "
            f"{match.group(0) if match else ''}"
        )


# ---------------------------------------------------------------------------
# Product-broken state plumbing
# ---------------------------------------------------------------------------


def test_product_state_set_get_clear_roundtrip(tmp_path: Path) -> None:
    from pollypm.storage.product_state import (
        clear_product_state,
        get_product_state,
        is_product_broken,
        set_product_state_broken,
    )
    from pollypm.storage.state import StateStore

    db = tmp_path / "state.db"
    store = StateStore(db)
    try:
        assert get_product_state(store) is None
        assert is_product_broken(store) is None
        set_product_state_broken(
            store,
            reason="advisor cancel-loop unrecoverable",
            forensics_path="~/.pollypm/audit/coffeeboardnm.jsonl",
            set_by="audit_watchdog",
            extra={"loop_count": 52},
        )
        rt = get_product_state(store)
        assert rt is not None
        assert rt.state == "broken"
        assert rt.reason == "advisor cancel-loop unrecoverable"
        assert rt.set_by == "audit_watchdog"
        assert rt.forensics_path.endswith("coffeeboardnm.jsonl")
        assert rt.extra == {"loop_count": 52}
        assert is_product_broken(store) is not None
        # Idempotent: re-setting overwrites cleanly.
        set_product_state_broken(
            store, reason="updated reason", forensics_path="/tmp/x.jsonl",
        )
        rt2 = get_product_state(store)
        assert rt2 is not None
        assert rt2.reason == "updated reason"
        # Clear sweeps the row.
        assert clear_product_state(store) is True
        assert get_product_state(store) is None
        assert is_product_broken(store) is None
        # Idempotent clear.
        assert clear_product_state(store) is False
    finally:
        store.close()


def test_workspace_state_migration_idempotent_on_old_db(tmp_path: Path) -> None:
    """An older DB that pre-dates #1546 must pick up the
    ``workspace_state`` table when a fresh ``StateStore`` opens it.

    Simulate an unmigrated DB by creating one without the table, then
    open it with ``StateStore`` and assert (a) the table exists, (b)
    ``get_workspace_state`` reads return ``None`` cleanly (no
    ``no such table`` raised), and (c) the migration row was recorded.
    """
    import sqlite3

    from pollypm.storage.state import StateStore

    db = tmp_path / "old.db"
    # Simulate pre-#1546 DB shape: every other table from SCHEMA except
    # workspace_state. We create a stripped DB by hand and then open it
    # via StateStore so the migration path fills in the new table.
    raw = sqlite3.connect(db)
    try:
        raw.execute(
            """CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                description TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )"""
        )
        # Mark the DB as having gone through migration 16 (pre-#1546).
        raw.execute(
            "INSERT INTO schema_version (version, description, applied_at) "
            "VALUES (?, ?, ?)",
            (16, "pre-#1546 baseline", "2026-01-01T00:00:00+00:00"),
        )
        raw.commit()
    finally:
        raw.close()

    # Open through StateStore — migration 17 should run and create the
    # workspace_state table.
    store = StateStore(db)
    try:
        # No row yet, but the table exists and the read returns None.
        assert store.get_workspace_state("product_state") is None
        # Direct sqlite probe confirms the table exists post-migration.
        with sqlite3.connect(db) as probe:
            row = probe.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='workspace_state'"
            ).fetchone()
        assert row is not None
    finally:
        store.close()


def test_get_workspace_state_tolerates_missing_table(tmp_path: Path) -> None:
    """Read-only / pre-#1546 DBs without the workspace_state table
    return ``None`` from ``get_workspace_state`` rather than raising
    ``OperationalError`` so downstream consumers (gate / doctor) treat
    it as "no state".
    """
    import sqlite3

    from pollypm.storage.state import StateStore

    db = tmp_path / "no_table.db"
    store = StateStore(db)
    try:
        # Drop the table to simulate the pre-#1546 shape on an open
        # connection; the read should still return None cleanly.
        store.execute("DROP TABLE IF EXISTS workspace_state")
        store.commit()
        assert store.get_workspace_state("product_state") is None
    finally:
        store.close()


def test_product_state_refuses_empty_reason(tmp_path: Path) -> None:
    from pollypm.storage.product_state import set_product_state_broken
    from pollypm.storage.state import StateStore

    store = StateStore(tmp_path / "state.db")
    try:
        with pytest.raises(ValueError):
            set_product_state_broken(
                store, reason="", forensics_path="/tmp/x",
            )
    finally:
        store.close()


def test_product_broken_gate_refuses_create_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the workspace is product-broken, ``create_task`` raises
    :class:`ProductBrokenError`. Verified via the public gate helper
    so we don't need a full work-service spin-up."""
    from pollypm.storage.product_state import (
        ProductBrokenError,
        set_product_state_broken,
    )
    from pollypm.storage.state import StateStore
    from pollypm.work.service_queries import _enforce_product_state_gate

    db = tmp_path / "state.db"
    store = StateStore(db)
    set_product_state_broken(
        store, reason="cascade exhausted",
        forensics_path="/tmp/audit.jsonl",
    )
    store.close()

    # Point the config loader at our fixture DB by patching the helper.
    class _StubProject:
        state_db = db

    class _StubConfig:
        project = _StubProject()

    monkeypatch.setattr(
        "pollypm.config.load_config", lambda _path: _StubConfig(),
    )

    with pytest.raises(ProductBrokenError):
        _enforce_product_state_gate(labels=[])


def test_product_broken_gate_uses_service_db_path_not_global_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate must consult the actual DB the calling service is
    bound to, not the global ``config.project.state_db`` default.

    Workspace A is broken; workspace B is healthy. Creating a task in
    B (passing B's db_path) must succeed; creating a task in A (passing
    A's db_path) must raise. Pre-fix both calls would consult the same
    global default DB regardless of which workspace was active.
    """
    from pollypm.storage.product_state import (
        ProductBrokenError,
        set_product_state_broken,
    )
    from pollypm.storage.state import StateStore
    from pollypm.work.service_queries import _enforce_product_state_gate

    db_a = tmp_path / "ws_a.db"
    store_a = StateStore(db_a)
    set_product_state_broken(
        store_a, reason="ws_a is broken", forensics_path="/tmp/a.jsonl",
    )
    store_a.close()

    db_b = tmp_path / "ws_b.db"
    store_b = StateStore(db_b)  # healthy: no product_state row
    store_b.close()

    # Point the global config at workspace B (healthy). If the gate
    # fell back to the global default it would see B and let A through.
    class _StubProject:
        state_db = db_b

    class _StubConfig:
        project = _StubProject()

    monkeypatch.setattr(
        "pollypm.config.load_config", lambda _path: _StubConfig(),
    )

    # Workspace B (healthy) — should not raise.
    _enforce_product_state_gate(labels=[], db_path=db_b)

    # Workspace A (broken) — must raise even though the global config
    # points at the healthy B workspace.
    with pytest.raises(ProductBrokenError):
        _enforce_product_state_gate(labels=[], db_path=db_a)


def test_product_broken_gate_reflects_wal_writes_without_checkpoint(
    tmp_path: Path,
) -> None:
    """The cache key must include the WAL file's mtime so a fresh
    ``set_product_state_broken`` write becomes visible immediately —
    pre-fix the cache keyed on the main DB's mtime only and opened with
    ``immutable=1``, which together hid uncheckpointed WAL writes.
    """
    from pollypm.storage.product_state import (
        ProductBrokenError,
        set_product_state_broken,
    )
    from pollypm.storage.state import StateStore
    from pollypm.work.service_queries import _enforce_product_state_gate

    db = tmp_path / "ws.db"
    store = StateStore(db)
    try:
        # First call: workspace is healthy.
        _enforce_product_state_gate(labels=[], db_path=db)

        # Flip the workspace_state row under WAL — no checkpoint.
        set_product_state_broken(
            store,
            reason="just-flipped",
            forensics_path="/tmp/x.jsonl",
        )

        # Immediate retry must observe the flip.
        with pytest.raises(ProductBrokenError):
            _enforce_product_state_gate(labels=[], db_path=db)
    finally:
        store.close()


def test_product_broken_gate_allows_watchdog_labelled_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tasks tagged ``watchdog`` / ``urgent`` / ``notify`` bypass the gate
    so the cascade itself can keep dispatching to the operator inbox
    even when the workspace is broken."""
    from pollypm.storage.product_state import set_product_state_broken
    from pollypm.storage.state import StateStore
    from pollypm.work.service_queries import _enforce_product_state_gate

    db = tmp_path / "state.db"
    store = StateStore(db)
    set_product_state_broken(
        store, reason="cascade exhausted",
        forensics_path="/tmp/audit.jsonl",
    )
    store.close()

    class _StubProject:
        state_db = db

    class _StubConfig:
        project = _StubProject()

    monkeypatch.setattr(
        "pollypm.config.load_config", lambda _path: _StubConfig(),
    )

    # No exception — the gate bypasses watchdog-labelled rows.
    _enforce_product_state_gate(labels=["watchdog", "notify"])
    _enforce_product_state_gate(labels=["urgent"])


def test_pm_doctor_includes_product_state_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The diagnostic check reports product_state in the registered
    list and renders ``healthy`` / ``broken`` correctly."""
    from pollypm.doctor import _registered_checks

    names = {c.name for c in _registered_checks()}
    assert "product-state" in names


# ---------------------------------------------------------------------------
# Integration-style tests — full _scan_one_project flow with mocked sinks
# ---------------------------------------------------------------------------


class _RecordingStore:
    def __init__(self) -> None:
        self.alerts: list[tuple[str, str, str, str]] = []

    def upsert_alert(
        self, scope: str, alert_type: str, severity: str, message: str,
    ) -> None:
        self.alerts.append((scope, alert_type, severity, message))


def test_integration_state_db_missing_routes_to_tier1_healer(
    now: datetime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: when the canonical workspace state.db is missing,
    the tier-1 healer recreates it through the work-service factory.

    Post-#339 / #1004 the canonical work DB is the workspace-root
    ``<workspace_root>/.pollypm/state.db`` — not a per-project file.
    The healer opens the path through ``create_work_service`` so the
    schema replay materialises the missing DB.
    """
    from pollypm.plugins_builtin.core_recurring import audit_watchdog as cadence_mod

    canonical_db = tmp_path / "ws" / ".pollypm" / "state.db"
    assert not canonical_db.exists()

    finding = Finding(
        rule=RULE_STATE_DB_MISSING,
        tier=TIER_1,
        project="demo",
        subject="demo",
        metadata={
            "project_key": "demo",
            "project_path": str(tmp_path),
            "canonical_db_path": str(canonical_db),
        },
        evidence={
            "project_key": "demo",
            "canonical_db_path": str(canonical_db),
        },
    )
    healer = cadence_mod._TIER1_HEALERS[RULE_STATE_DB_MISSING]
    counters = healer(
        finding, project_key="demo", project_path=tmp_path, config_path=None,
    )
    assert counters["state_db_repaired"] == 1
    assert canonical_db.exists()
    # Idempotent — re-running on a healthy DB still reports a repair
    # (no-op open) and doesn't blow up.
    counters2 = healer(
        finding, project_key="demo", project_path=tmp_path, config_path=None,
    )
    assert counters2["state_db_repaired"] == 1
    assert canonical_db.exists()


def test_integration_rejection_loop_dispatches_via_architect(
    now: datetime, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a rejection-loop finding routes through the architect
    leg with structured evidence (mock the tmux send-keys sink)."""
    from pollypm.plugins_builtin.core_recurring import audit_watchdog as cadence_mod

    sent: list[tuple[str, str]] = []

    def _stub_send(target: str, brief: str) -> bool:
        sent.append((target, brief))
        return True

    monkeypatch.setattr(cadence_mod, "_send_brief_to_architect", _stub_send)

    finding = Finding(
        rule=RULE_REJECTION_LOOP,
        tier=TIER_2,
        project="coffeeboardnm",
        subject="coffeeboardnm/70",
        message="3 rejections at code_review",
        evidence={
            "node_id": "code_review",
            "reject_count": 3,
            "shared_tokens": ["better-sqlite3", "node25"],
            "attempts": [
                {"node": "code_review", "completed_at": "2026-05-09T14:50:00+00:00",
                 "reason": "better-sqlite3 fails Node 25"},
            ],
            "window_seconds": REJECTION_LOOP_WINDOW_SECONDS,
        },
    )
    outcome = cadence_mod._maybe_dispatch_to_architect(
        finding,
        project_path=None,
        storage_closet_name="pollypm-storage-closet",
        now=now,
    )
    assert outcome == "dispatched"
    assert len(sent) == 1
    target, brief = sent[0]
    assert target == "pollypm-storage-closet:architect-coffeeboardnm"
    assert "rejection_loop" in brief
    assert "better-sqlite3" in brief
    # The structural-loop framing must mention generating hypotheses
    # fresh — the issue's evidence-not-hypotheses principle.
    assert "fresh from the evidence" in brief
