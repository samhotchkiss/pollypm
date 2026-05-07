"""Tests for the audit-log watchdog (savethenovel follow-up).

Exercises the four pure detectors against synthetic event lists and
confirms the integration path (read events from ``central_log_path``,
detect findings, emit forensic ``audit.finding`` events) works
end-to-end against an isolated audit home.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pollypm.audit import emit
from pollypm.audit.log import (
    EVENT_MARKER_CREATED,
    EVENT_MARKER_LEAKED,
    EVENT_MARKER_RELEASED,
    EVENT_TASK_CREATED,
    EVENT_TASK_STATUS_CHANGED,
    AuditEvent,
    central_log_path,
)
from pollypm.audit.watchdog import (
    EVENT_AUDIT_FINDING,
    EVENT_HEARTBEAT_TICK,
    RULE_CANCEL_NO_PROMOTION,
    RULE_MARKER_LEAKED,
    RULE_ORPHAN_MARKER,
    RULE_STUCK_DRAFT,
    Finding,
    WatchdogConfig,
    emit_finding,
    emit_heartbeat_tick,
    format_finding_message,
    scan_events,
    scan_project,
    watchdog_alert_session_name,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_audit_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the central-tail root so tests never touch ~/.pollypm/."""
    audit_home = tmp_path / "audit-home"
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))
    return audit_home


@pytest.fixture
def now() -> datetime:
    """Fixed wall-clock — gives every detector deterministic windowing."""
    return datetime(2026, 5, 6, 17, 0, 0, tzinfo=timezone.utc)


def _make_event(
    *,
    event: str,
    project: str = "demo",
    subject: str = "",
    actor: str = "polly",
    status: str = "ok",
    metadata: dict | None = None,
    ts: datetime,
) -> AuditEvent:
    return AuditEvent(
        ts=ts.isoformat(),
        project=project,
        event=event,
        subject=subject,
        actor=actor,
        status=status,
        metadata=metadata or {},
    )


# ---------------------------------------------------------------------------
# Rule 1: orphan marker
# ---------------------------------------------------------------------------


def test_orphan_marker_detected_when_no_release_or_terminal(now: datetime) -> None:
    """A marker.created with neither release nor terminal transition fires."""
    events = [
        _make_event(
            event=EVENT_MARKER_CREATED,
            project="demo",
            subject="/proj/demo/.pollypm/worker-markers/task-demo-1.fresh",
            ts=now - timedelta(minutes=20),
        ),
    ]
    findings = scan_events(events, now=now)
    orphans = [f for f in findings if f.rule == RULE_ORPHAN_MARKER]
    assert len(orphans) == 1
    assert orphans[0].project == "demo"
    assert orphans[0].subject == "demo/1"
    assert "demo/1" in orphans[0].message
    assert "pm task cancel demo/1" in orphans[0].recommendation


def test_orphan_marker_silenced_by_release(now: datetime) -> None:
    marker = "/proj/demo/.pollypm/worker-markers/task-demo-1.fresh"
    events = [
        _make_event(
            event=EVENT_MARKER_CREATED, subject=marker,
            ts=now - timedelta(minutes=20),
        ),
        _make_event(
            event=EVENT_MARKER_RELEASED, subject=marker,
            ts=now - timedelta(minutes=10),
        ),
    ]
    findings = [f for f in scan_events(events, now=now) if f.rule == RULE_ORPHAN_MARKER]
    assert findings == []


def test_orphan_marker_silenced_by_terminal_transition(now: datetime) -> None:
    """Cancellation transition closes out the orphan check (savethenovel/1)."""
    marker = "/proj/demo/.pollypm/worker-markers/task-demo-1.fresh"
    events = [
        _make_event(
            event=EVENT_MARKER_CREATED, subject=marker,
            ts=now - timedelta(minutes=25),
        ),
        _make_event(
            event=EVENT_TASK_STATUS_CHANGED,
            subject="demo/1",
            metadata={"from": "in_progress", "to": "cancelled"},
            ts=now - timedelta(minutes=24),
        ),
    ]
    findings = [f for f in scan_events(events, now=now) if f.rule == RULE_ORPHAN_MARKER]
    assert findings == []


def test_orphan_marker_outside_window_ignored(now: datetime) -> None:
    config = WatchdogConfig(window_seconds=600)
    marker = "/proj/demo/.pollypm/worker-markers/task-demo-1.fresh"
    events = [
        _make_event(
            event=EVENT_MARKER_CREATED, subject=marker,
            ts=now - timedelta(hours=2),
        ),
    ]
    findings = scan_events(events, now=now, config=config)
    assert [f for f in findings if f.rule == RULE_ORPHAN_MARKER] == []


def test_orphan_marker_skips_unrecognised_subject(now: datetime) -> None:
    """Marker subjects we can't parse don't fire the rule."""
    events = [
        _make_event(
            event=EVENT_MARKER_CREATED,
            subject="/proj/demo/.pollypm/worker-markers/advisor-something.fresh",
            ts=now - timedelta(minutes=10),
        ),
    ]
    findings = [f for f in scan_events(events, now=now) if f.rule == RULE_ORPHAN_MARKER]
    assert findings == []


# ---------------------------------------------------------------------------
# Rule 2: marker leaked
# ---------------------------------------------------------------------------


def test_marker_leaked_event_surfaces_finding(now: datetime) -> None:
    events = [
        _make_event(
            event=EVENT_MARKER_LEAKED,
            subject="/proj/demo/.pollypm/worker-markers/task-demo-2.fresh",
            metadata={"reason": "persona_swap_detected"},
            ts=now - timedelta(minutes=5),
        ),
    ]
    findings = [f for f in scan_events(events, now=now) if f.rule == RULE_MARKER_LEAKED]
    assert len(findings) == 1
    assert findings[0].subject == "demo/2"
    assert "persona-swap" in findings[0].message.lower()


def test_marker_leaked_outside_window_ignored(now: datetime) -> None:
    events = [
        _make_event(
            event=EVENT_MARKER_LEAKED,
            subject="/proj/demo/.pollypm/worker-markers/task-demo-2.fresh",
            ts=now - timedelta(hours=2),
        ),
    ]
    assert [f for f in scan_events(events, now=now) if f.rule == RULE_MARKER_LEAKED] == []


# ---------------------------------------------------------------------------
# Rule 3: stuck draft
# ---------------------------------------------------------------------------


def test_stuck_draft_detected_when_never_promoted(now: datetime) -> None:
    """A draft older than ``stuck_draft_seconds`` with no promotion fires."""
    events = [
        _make_event(
            event=EVENT_TASK_CREATED,
            subject="demo/2",
            metadata={"title": "Plan the next chapter"},
            ts=now - timedelta(minutes=15),
        ),
    ]
    findings = [f for f in scan_events(events, now=now) if f.rule == RULE_STUCK_DRAFT]
    assert len(findings) == 1
    assert findings[0].subject == "demo/2"
    assert "pm task queue demo/2" in findings[0].recommendation


def test_stuck_draft_silenced_by_promotion(now: datetime) -> None:
    events = [
        _make_event(
            event=EVENT_TASK_CREATED, subject="demo/2",
            ts=now - timedelta(minutes=15),
        ),
        _make_event(
            event=EVENT_TASK_STATUS_CHANGED,
            subject="demo/2",
            metadata={"from": "draft", "to": "queued"},
            ts=now - timedelta(minutes=10),
        ),
    ]
    findings = [f for f in scan_events(events, now=now) if f.rule == RULE_STUCK_DRAFT]
    assert findings == []


def test_stuck_draft_recent_create_within_grace(now: datetime) -> None:
    """A freshly-created draft (<stuck_draft_seconds) does not yet fire."""
    events = [
        _make_event(
            event=EVENT_TASK_CREATED, subject="demo/2",
            ts=now - timedelta(seconds=60),
        ),
    ]
    findings = [f for f in scan_events(events, now=now) if f.rule == RULE_STUCK_DRAFT]
    assert findings == []


def test_stuck_draft_silenced_by_cancellation(now: datetime) -> None:
    """Cancelling a draft also clears the stuck-draft check."""
    events = [
        _make_event(
            event=EVENT_TASK_CREATED, subject="demo/2",
            ts=now - timedelta(minutes=15),
        ),
        _make_event(
            event=EVENT_TASK_STATUS_CHANGED,
            subject="demo/2",
            metadata={"from": "draft", "to": "cancelled"},
            ts=now - timedelta(minutes=10),
        ),
    ]
    findings = [f for f in scan_events(events, now=now) if f.rule == RULE_STUCK_DRAFT]
    assert findings == []


# ---------------------------------------------------------------------------
# Rule 4: cancellation without promotion
# ---------------------------------------------------------------------------


def test_cancel_without_replacement_fires(now: datetime) -> None:
    """savethenovel/1 — cancel with no follow-up create."""
    events = [
        _make_event(
            event=EVENT_TASK_CREATED, subject="demo/1",
            ts=now - timedelta(minutes=30),
        ),
        _make_event(
            event=EVENT_TASK_STATUS_CHANGED,
            subject="demo/1",
            metadata={"from": "in_progress", "to": "cancelled"},
            ts=now - timedelta(minutes=15),
        ),
    ]
    findings = [
        f for f in scan_events(events, now=now)
        if f.rule == RULE_CANCEL_NO_PROMOTION
    ]
    assert len(findings) == 1
    assert findings[0].subject == "demo/1"


def test_cancel_with_followup_create_silenced(now: datetime) -> None:
    events = [
        _make_event(
            event=EVENT_TASK_STATUS_CHANGED,
            subject="demo/1",
            metadata={"to": "cancelled"},
            ts=now - timedelta(minutes=15),
        ),
        _make_event(
            event=EVENT_TASK_CREATED, subject="demo/2",
            ts=now - timedelta(minutes=12),
        ),
    ]
    findings = [
        f for f in scan_events(events, now=now)
        if f.rule == RULE_CANCEL_NO_PROMOTION
    ]
    assert findings == []


def test_cancel_within_grace_window_silenced(now: datetime) -> None:
    """A cancel that's still inside the grace window does not yet fire."""
    config = WatchdogConfig(cancel_grace_seconds=300)
    events = [
        _make_event(
            event=EVENT_TASK_STATUS_CHANGED,
            subject="demo/1",
            metadata={"to": "cancelled"},
            ts=now - timedelta(seconds=120),  # within 5min grace
        ),
    ]
    findings = [
        f for f in scan_events(events, now=now, config=config)
        if f.rule == RULE_CANCEL_NO_PROMOTION
    ]
    assert findings == []


# ---------------------------------------------------------------------------
# Empty-state + sanity
# ---------------------------------------------------------------------------


def test_empty_event_list_no_findings(now: datetime) -> None:
    assert scan_events([], now=now) == []


def test_scan_requires_tz_aware_now() -> None:
    naive = datetime(2026, 5, 6, 17, 0, 0)
    with pytest.raises(ValueError):
        scan_events([], now=naive)


def test_format_finding_message_includes_recommendation() -> None:
    finding = Finding(
        rule=RULE_STUCK_DRAFT,
        project="demo",
        subject="demo/2",
        message="Draft demo/2 is stuck.",
        recommendation="Run pm task queue demo/2.",
    )
    rendered = format_finding_message(finding)
    assert "Draft demo/2 is stuck." in rendered
    assert "Recommendation: Run pm task queue demo/2." in rendered


def test_alert_session_name_is_stable() -> None:
    name = watchdog_alert_session_name(RULE_STUCK_DRAFT, "demo", "demo/2")
    assert name.startswith("audit-stuck_draft-demo-")
    # Must not contain raw '/' so the session-name space stays clean.
    assert "/" not in name


# ---------------------------------------------------------------------------
# Integration — round-trip through the on-disk audit log
# ---------------------------------------------------------------------------


def test_scan_project_against_empty_log_returns_no_findings(now: datetime) -> None:
    """Fresh, empty audit home produces no findings."""
    findings = scan_project("demo", now=now)
    assert findings == []


def test_scan_project_against_synthetic_log(now: datetime, tmp_path: Path) -> None:
    """Write events through ``emit`` then read via ``scan_project``.

    Exercises the central-tail read path the cadence handler will use
    in production.
    """
    # Older orphan marker event (within 30 min window).
    old_ts = now - timedelta(minutes=20)
    # Use the writer with a manual ts bypass — emit() stamps "now",
    # so we shape the event by writing JSON directly to the central
    # tail. This mirrors what an in-process producer would have
    # written 20 minutes ago.
    central = central_log_path("savethenovel")
    central.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "ts": old_ts.isoformat(),
        "project": "savethenovel",
        "event": EVENT_MARKER_CREATED,
        "subject": "/x/savethenovel/.pollypm/worker-markers/task-savethenovel-1.fresh",
        "actor": "polly",
        "status": "ok",
        "metadata": {"window_name": "task-savethenovel-1"},
    }
    central.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    findings = scan_project("savethenovel", now=now)
    assert any(f.rule == RULE_ORPHAN_MARKER for f in findings)


def test_emit_heartbeat_tick_lands_in_audit_log() -> None:
    emit_heartbeat_tick(project="demo", metadata={"cadence": "@every 5m"})
    central = central_log_path("demo")
    assert central.exists()
    rows = [
        json.loads(line)
        for line in central.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    tick_rows = [r for r in rows if r["event"] == EVENT_HEARTBEAT_TICK]
    assert len(tick_rows) == 1
    assert tick_rows[0]["actor"] == "audit_watchdog"


def test_emit_finding_writes_audit_finding_event() -> None:
    finding = Finding(
        rule=RULE_STUCK_DRAFT,
        project="demo",
        subject="demo/2",
        message="msg",
        recommendation="rec",
    )
    emit_finding(finding)
    central = central_log_path("demo")
    rows = [
        json.loads(line)
        for line in central.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    finding_rows = [r for r in rows if r["event"] == EVENT_AUDIT_FINDING]
    assert len(finding_rows) == 1
    assert finding_rows[0]["metadata"]["rule"] == RULE_STUCK_DRAFT
    assert finding_rows[0]["metadata"]["recommendation"] == "rec"


# ---------------------------------------------------------------------------
# Cadence handler — alert routing
# ---------------------------------------------------------------------------


class _RecordingStore:
    """Captures ``upsert_alert`` calls — mirrors blocked_chain test pattern."""

    def __init__(self) -> None:
        self.alerts: list[tuple[str, str, str, str]] = []

    def upsert_alert(
        self, scope: str, alert_type: str, severity: str, message: str,
    ) -> None:
        self.alerts.append((scope, alert_type, severity, message))


def test_cadence_handler_routes_finding_to_alert_sink(now: datetime) -> None:
    """The handler's per-project scan loop upserts one alert per finding."""
    from pollypm.plugins_builtin.core_recurring.audit_watchdog import (
        WATCHDOG_ALERT_TYPE,
        _scan_one_project,
    )

    # Seed an orphan-marker event into the central tail.
    central = central_log_path("demo")
    central.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "ts": (now - timedelta(minutes=20)).isoformat(),
        "project": "demo",
        "event": EVENT_MARKER_CREATED,
        "subject": "/x/demo/.pollypm/worker-markers/task-demo-1.fresh",
        "actor": "polly",
        "status": "ok",
        "metadata": {},
    }
    central.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    store = _RecordingStore()
    counters = _scan_one_project(
        project_key="demo",
        project_path=None,
        msg_store=store,
        state_store=None,
        now=now,
        config=WatchdogConfig(),
    )

    assert counters["findings"] >= 1
    assert counters["alerts_raised"] >= 1
    assert counters["alert_failures"] == 0
    rules_alerted = {a[1] for a in store.alerts}
    assert WATCHDOG_ALERT_TYPE in rules_alerted


# ---------------------------------------------------------------------------
# #1414 — auto-unstick rules
# ---------------------------------------------------------------------------


def test_task_review_stale_fires_after_threshold(now: datetime) -> None:
    """A task at status=review whose latest transition is >30min old fires."""
    from pollypm.audit.watchdog import RULE_TASK_REVIEW_STALE

    events = [
        _make_event(
            event=EVENT_TASK_STATUS_CHANGED,
            project="savethenovel",
            subject="savethenovel/10",
            metadata={"from": "in_progress", "to": "review"},
            ts=now - timedelta(minutes=45),
        ),
    ]
    findings = scan_events(events, now=now)
    review_findings = [f for f in findings if f.rule == RULE_TASK_REVIEW_STALE]
    assert len(review_findings) == 1
    f = review_findings[0]
    assert f.subject == "savethenovel/10"
    assert f.project == "savethenovel"
    assert "review" in f.message
    assert "savethenovel/10" in f.message


def test_task_review_stale_silent_when_within_grace(now: datetime) -> None:
    """A review that just transitioned doesn't fire."""
    from pollypm.audit.watchdog import RULE_TASK_REVIEW_STALE

    events = [
        _make_event(
            event=EVENT_TASK_STATUS_CHANGED,
            project="demo",
            subject="demo/3",
            metadata={"from": "in_progress", "to": "review"},
            ts=now - timedelta(minutes=5),
        ),
    ]
    findings = scan_events(events, now=now)
    assert not any(f.rule == RULE_TASK_REVIEW_STALE for f in findings)


def test_task_review_stale_silent_when_later_transition_exists(now: datetime) -> None:
    """Latest transition isn't to review → no fire even if older one was."""
    from pollypm.audit.watchdog import RULE_TASK_REVIEW_STALE

    events = [
        _make_event(
            event=EVENT_TASK_STATUS_CHANGED,
            project="demo",
            subject="demo/3",
            metadata={"from": "in_progress", "to": "review"},
            ts=now - timedelta(minutes=45),
        ),
        _make_event(
            event=EVENT_TASK_STATUS_CHANGED,
            project="demo",
            subject="demo/3",
            metadata={"from": "review", "to": "done"},
            ts=now - timedelta(minutes=10),
        ),
    ]
    findings = scan_events(events, now=now)
    assert not any(f.rule == RULE_TASK_REVIEW_STALE for f in findings)


class _FakeTask:
    """Minimal stand-in for ``work.models.Task`` for the role-session rule."""

    class _Status:
        def __init__(self, value: str) -> None:
            self.value = value

    def __init__(
        self,
        *,
        project: str,
        task_number: int,
        work_status: str,
        roles: dict[str, str] | None = None,
        assignee: str | None = None,
    ) -> None:
        self.project = project
        self.task_number = task_number
        self.work_status = self._Status(work_status)
        self.roles = roles or {}
        self.assignee = assignee


def test_role_session_missing_fires_when_window_absent(now: datetime) -> None:
    """A review-state task with no reviewer-<project> window fires."""
    from pollypm.audit.watchdog import RULE_ROLE_SESSION_MISSING

    task = _FakeTask(
        project="savethenovel",
        task_number=10,
        work_status="review",
        roles={"reviewer": "claude:reviewer"},
    )
    findings = scan_events(
        [],
        now=now,
        open_tasks=[task],
        storage_window_names=["worker-savethenovel", "architect-savethenovel"],
        project="savethenovel",
    )
    matched = [f for f in findings if f.rule == RULE_ROLE_SESSION_MISSING]
    assert len(matched) == 1
    f = matched[0]
    assert f.subject == "savethenovel/10"
    assert f.metadata["expected_window"] == "reviewer-savethenovel"
    assert f.metadata["role"] == "reviewer"


def test_role_session_missing_silent_when_window_present(now: datetime) -> None:
    """When the storage closet has the role window, no finding fires."""
    from pollypm.audit.watchdog import RULE_ROLE_SESSION_MISSING

    task = _FakeTask(
        project="demo",
        task_number=1,
        work_status="in_progress",
        assignee="alice",
    )
    findings = scan_events(
        [],
        now=now,
        open_tasks=[task],
        storage_window_names=["worker-demo"],
        project="demo",
    )
    assert not any(f.rule == RULE_ROLE_SESSION_MISSING for f in findings)


def test_role_session_missing_noop_without_inputs(now: datetime) -> None:
    """Without open_tasks / storage_window_names the rule is a no-op."""
    from pollypm.audit.watchdog import RULE_ROLE_SESSION_MISSING

    findings = scan_events([], now=now)
    assert not any(f.rule == RULE_ROLE_SESSION_MISSING for f in findings)


def test_worker_session_dead_loop_fires_at_threshold(now: datetime) -> None:
    """3+ worker.session_reaped for the same task in 10min fires."""
    from pollypm.audit.log import EVENT_WORKER_SESSION_REAPED
    from pollypm.audit.watchdog import RULE_WORKER_SESSION_DEAD_LOOP

    events = [
        _make_event(
            event=EVENT_WORKER_SESSION_REAPED,
            project="demo",
            subject="demo/4",
            metadata={"reason": "spawn_failed"},
            ts=now - timedelta(minutes=delta),
        )
        for delta in (8, 5, 2)
    ]
    findings = scan_events(events, now=now)
    matched = [f for f in findings if f.rule == RULE_WORKER_SESSION_DEAD_LOOP]
    assert len(matched) == 1
    assert matched[0].subject == "demo/4"
    assert matched[0].metadata["reap_count"] == 3


def test_worker_session_dead_loop_below_threshold_silent(now: datetime) -> None:
    """Two reaps don't fire."""
    from pollypm.audit.log import EVENT_WORKER_SESSION_REAPED
    from pollypm.audit.watchdog import RULE_WORKER_SESSION_DEAD_LOOP

    events = [
        _make_event(
            event=EVENT_WORKER_SESSION_REAPED,
            project="demo",
            subject="demo/4",
            ts=now - timedelta(minutes=delta),
        )
        for delta in (8, 5)
    ]
    findings = scan_events(events, now=now)
    assert not any(f.rule == RULE_WORKER_SESSION_DEAD_LOOP for f in findings)


def test_worker_session_dead_loop_outside_window_silent(now: datetime) -> None:
    """3 reaps but spread across > 10min don't fire — only ones in-window count."""
    from pollypm.audit.log import EVENT_WORKER_SESSION_REAPED
    from pollypm.audit.watchdog import RULE_WORKER_SESSION_DEAD_LOOP

    events = [
        _make_event(
            event=EVENT_WORKER_SESSION_REAPED,
            project="demo",
            subject="demo/4",
            ts=now - timedelta(minutes=delta),
        )
        # 30, 25, 20 are all > 10 min ago
        for delta in (30, 25, 20)
    ]
    findings = scan_events(events, now=now)
    assert not any(f.rule == RULE_WORKER_SESSION_DEAD_LOOP for f in findings)


# ---------------------------------------------------------------------------
# Brief formatting per rule
# ---------------------------------------------------------------------------


def test_format_unstick_brief_review_stale_includes_options() -> None:
    from pollypm.audit.watchdog import (
        RULE_TASK_REVIEW_STALE,
        format_unstick_brief,
    )

    finding = Finding(
        rule=RULE_TASK_REVIEW_STALE,
        project="savethenovel",
        subject="savethenovel/10",
        message="Task savethenovel/10 has been at status=review for ~45 min...",
        recommendation="Spawn a reviewer.",
        metadata={"stuck_minutes": 45, "review_since": "2026-05-07T08:37:00+00:00"},
    )
    brief = format_unstick_brief(finding)
    assert brief.startswith("WATCHDOG ESCALATION")
    assert "Project: savethenovel" in brief
    assert "Finding: task_review_stale" in brief
    assert "Subject: savethenovel/10" in brief
    assert "45 minutes" in brief
    assert "2026-05-07T08:37:00+00:00" in brief
    assert "pm task done savethenovel/10" in brief
    assert "pm notify" in brief


def test_format_unstick_brief_role_session_missing_names_window() -> None:
    from pollypm.audit.watchdog import (
        RULE_ROLE_SESSION_MISSING,
        format_unstick_brief,
    )

    finding = Finding(
        rule=RULE_ROLE_SESSION_MISSING,
        project="savethenovel",
        subject="savethenovel/10",
        message="...",
        metadata={
            "expected_window": "reviewer-savethenovel",
            "role": "reviewer",
            "status": "review",
        },
    )
    brief = format_unstick_brief(finding)
    assert "reviewer-savethenovel" in brief
    assert "pm chat savethenovel --role reviewer" in brief
    assert "pm notify" in brief


def test_format_unstick_brief_dead_loop_quotes_count() -> None:
    from pollypm.audit.watchdog import (
        RULE_WORKER_SESSION_DEAD_LOOP,
        format_unstick_brief,
    )

    finding = Finding(
        rule=RULE_WORKER_SESSION_DEAD_LOOP,
        project="demo",
        subject="demo/4",
        message="reaped 5 times",
        metadata={"reap_count": 5, "latest_reason": "spawn_failed"},
    )
    brief = format_unstick_brief(finding)
    assert "5 reaps" in brief
    assert "spawn_failed" in brief
    assert "pm task cancel demo/4" in brief


# ---------------------------------------------------------------------------
# Throttle — was_recently_dispatched
# ---------------------------------------------------------------------------


def test_was_recently_dispatched_false_on_empty_log(now: datetime) -> None:
    from pollypm.audit.watchdog import was_recently_dispatched

    assert not was_recently_dispatched(
        project="demo",
        finding_type="task_review_stale",
        subject="demo/1",
        now=now,
    )


def test_was_recently_dispatched_true_after_emit(now: datetime) -> None:
    from pollypm.audit.watchdog import (
        emit_escalation_dispatched,
        was_recently_dispatched,
    )

    emit_escalation_dispatched(
        project="demo",
        finding_type="task_review_stale",
        subject="demo/1",
        brief="WATCHDOG ESCALATION ...",
    )
    assert was_recently_dispatched(
        project="demo",
        finding_type="task_review_stale",
        subject="demo/1",
        now=now + timedelta(minutes=10),
    )


def test_was_recently_dispatched_expires_after_window(now: datetime) -> None:
    """A dispatch older than the throttle window doesn't dedupe."""
    from pollypm.audit.watchdog import (
        ESCALATION_THROTTLE_SECONDS,
        was_recently_dispatched,
    )

    # Manually seed a dispatch event >throttle ago via the central log.
    from pollypm.audit.log import (
        EVENT_WATCHDOG_ESCALATION_DISPATCHED,
        central_log_path,
    )
    central = central_log_path("demo")
    central.parent.mkdir(parents=True, exist_ok=True)
    old_ts = now - timedelta(seconds=ESCALATION_THROTTLE_SECONDS + 60)
    payload = {
        "schema": 1,
        "ts": old_ts.isoformat(),
        "project": "demo",
        "event": EVENT_WATCHDOG_ESCALATION_DISPATCHED,
        "subject": "demo/1",
        "actor": "audit_watchdog",
        "status": "warn",
        "metadata": {
            "finding_type": "task_review_stale",
            "subject": "demo/1",
            "brief": "...",
        },
    }
    central.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    assert not was_recently_dispatched(
        project="demo",
        finding_type="task_review_stale",
        subject="demo/1",
        now=now,
    )


def test_was_recently_dispatched_distinguishes_finding_types(now: datetime) -> None:
    """A dispatch for finding A doesn't suppress a different finding type."""
    from pollypm.audit.watchdog import (
        emit_escalation_dispatched,
        was_recently_dispatched,
    )

    emit_escalation_dispatched(
        project="demo",
        finding_type="task_review_stale",
        subject="demo/1",
        brief="...",
    )
    assert not was_recently_dispatched(
        project="demo",
        finding_type="role_session_missing",
        subject="demo/1",
        now=now + timedelta(minutes=5),
    )


# ---------------------------------------------------------------------------
# Cadence handler — dispatch path (mocked architect)
# ---------------------------------------------------------------------------


def test_cadence_handler_dispatches_eligible_finding(
    now: datetime, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task_review_stale finding triggers an architect dispatch."""
    from pollypm.audit.log import central_log_path
    from pollypm.audit.watchdog import RULE_TASK_REVIEW_STALE
    from pollypm.plugins_builtin.core_recurring.audit_watchdog import (
        _scan_one_project,
    )

    # Seed a stale review transition into the central tail.
    central = central_log_path("savethenovel")
    central.parent.mkdir(parents=True, exist_ok=True)
    stale_ts = (now - timedelta(minutes=45)).isoformat()
    payload = {
        "schema": 1,
        "ts": stale_ts,
        "project": "savethenovel",
        "event": EVENT_TASK_STATUS_CHANGED,
        "subject": "savethenovel/10",
        "actor": "polly",
        "status": "ok",
        "metadata": {"from": "in_progress", "to": "review"},
    }
    central.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    # Capture send-keys calls instead of touching tmux.
    sent: list[tuple[str, str]] = []

    def _fake_send(target: str, brief: str) -> bool:
        sent.append((target, brief))
        return True

    monkeypatch.setattr(
        "pollypm.plugins_builtin.core_recurring.audit_watchdog._send_brief_to_architect",
        _fake_send,
    )
    # Stub list-windows / open-task gathering so the dispatch path doesn't
    # require a live tmux server or work-service db.
    monkeypatch.setattr(
        "pollypm.plugins_builtin.core_recurring.audit_watchdog._gather_storage_windows",
        lambda name: [],
    )
    monkeypatch.setattr(
        "pollypm.plugins_builtin.core_recurring.audit_watchdog._gather_open_tasks",
        lambda key, path: [],
    )

    store = _RecordingStore()
    counters = _scan_one_project(
        project_key="savethenovel",
        project_path=None,
        msg_store=store,
        state_store=None,
        now=now,
        config=WatchdogConfig(),
        storage_closet_name="pollypm-storage-closet",
    )

    assert counters["findings"] >= 1
    assert counters["dispatches_sent"] == 1
    assert counters["dispatches_throttled"] == 0
    assert len(sent) == 1
    target, brief = sent[0]
    assert target == "pollypm-storage-closet:architect-savethenovel"
    assert RULE_TASK_REVIEW_STALE in brief
    assert "savethenovel/10" in brief

    # Dispatch event should have landed in the audit log.
    rows = [
        json.loads(line)
        for line in central.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    dispatch_rows = [
        r for r in rows if r["event"] == "watchdog.escalation_dispatched"
    ]
    assert len(dispatch_rows) == 1
    assert dispatch_rows[0]["metadata"]["finding_type"] == RULE_TASK_REVIEW_STALE


def test_cadence_handler_throttles_repeat_dispatch(
    now: datetime, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two ticks within 30 min → only one architect send."""
    from pollypm.audit.log import central_log_path
    from pollypm.plugins_builtin.core_recurring.audit_watchdog import (
        _scan_one_project,
    )

    # Seed a stale review transition.
    central = central_log_path("savethenovel")
    central.parent.mkdir(parents=True, exist_ok=True)
    stale_ts = (now - timedelta(minutes=45)).isoformat()
    payload = {
        "schema": 1,
        "ts": stale_ts,
        "project": "savethenovel",
        "event": EVENT_TASK_STATUS_CHANGED,
        "subject": "savethenovel/10",
        "actor": "polly",
        "status": "ok",
        "metadata": {"from": "in_progress", "to": "review"},
    }
    central.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    sent: list[tuple[str, str]] = []

    def _fake_send(target: str, brief: str) -> bool:
        sent.append((target, brief))
        return True

    monkeypatch.setattr(
        "pollypm.plugins_builtin.core_recurring.audit_watchdog._send_brief_to_architect",
        _fake_send,
    )
    monkeypatch.setattr(
        "pollypm.plugins_builtin.core_recurring.audit_watchdog._gather_storage_windows",
        lambda name: [],
    )
    monkeypatch.setattr(
        "pollypm.plugins_builtin.core_recurring.audit_watchdog._gather_open_tasks",
        lambda key, path: [],
    )

    store = _RecordingStore()
    first = _scan_one_project(
        project_key="savethenovel",
        project_path=None,
        msg_store=store,
        state_store=None,
        now=now,
        config=WatchdogConfig(),
        storage_closet_name="pollypm-storage-closet",
    )
    second = _scan_one_project(
        project_key="savethenovel",
        project_path=None,
        msg_store=store,
        state_store=None,
        now=now + timedelta(minutes=5),
        config=WatchdogConfig(),
        storage_closet_name="pollypm-storage-closet",
    )

    assert first["dispatches_sent"] == 1
    assert second["dispatches_sent"] == 0
    assert second["dispatches_throttled"] == 1
    # Architect should only have received one brief.
    assert len(sent) == 1


def test_cadence_handler_skips_dispatch_for_legacy_rules(
    now: datetime, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """orphan_marker is NOT in the dispatchable set — no architect call."""
    from pollypm.plugins_builtin.core_recurring.audit_watchdog import (
        _scan_one_project,
    )

    central = central_log_path("demo")
    central.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "ts": (now - timedelta(minutes=20)).isoformat(),
        "project": "demo",
        "event": EVENT_MARKER_CREATED,
        "subject": "/x/demo/.pollypm/worker-markers/task-demo-1.fresh",
        "actor": "polly",
        "status": "ok",
        "metadata": {},
    }
    central.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "pollypm.plugins_builtin.core_recurring.audit_watchdog._send_brief_to_architect",
        lambda target, brief: sent.append((target, brief)) or True,
    )
    monkeypatch.setattr(
        "pollypm.plugins_builtin.core_recurring.audit_watchdog._gather_storage_windows",
        lambda name: [],
    )
    monkeypatch.setattr(
        "pollypm.plugins_builtin.core_recurring.audit_watchdog._gather_open_tasks",
        lambda key, path: [],
    )

    store = _RecordingStore()
    counters = _scan_one_project(
        project_key="demo",
        project_path=None,
        msg_store=store,
        state_store=None,
        now=now,
        config=WatchdogConfig(),
        storage_closet_name="pollypm-storage-closet",
    )

    assert counters["findings"] >= 1
    assert counters["dispatches_sent"] == 0
    assert counters["dispatches_throttled"] == 0
    assert sent == []


# ---------------------------------------------------------------------------
# #1420 — auto-unstick brief must be SUBMITTED, not just typed
# ---------------------------------------------------------------------------


def test_send_brief_to_architect_presses_enter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for #1420.

    PR #1415 originally called ``tmux.send_keys(..., press_enter=False)``,
    so the brief sat in the architect pane's input buffer until a human
    pressed Enter — undermining the "no user intervention" goal.

    The dispatch helper must call ``send_keys`` with ``press_enter=True``
    (or pass a positional True) so the architect agent actually processes
    the turn.
    """
    from pollypm.plugins_builtin.core_recurring.audit_watchdog import (
        _send_brief_to_architect,
    )

    captured: list[dict] = []

    class _FakeTmux:
        def send_keys(
            self, target: str, text: str, press_enter: bool = True,
        ) -> None:
            captured.append(
                {"target": target, "text": text, "press_enter": press_enter},
            )

    # Patch the import inside the helper so it picks up our fake.
    import pollypm.tmux.client as tmux_mod

    monkeypatch.setattr(tmux_mod, "TmuxClient", _FakeTmux)

    ok = _send_brief_to_architect(
        "pollypm-storage-closet:architect-savethenovel",
        "WATCHDOG ESCALATION ...",
    )
    assert ok is True
    assert len(captured) == 1
    call = captured[0]
    assert call["target"] == "pollypm-storage-closet:architect-savethenovel"
    assert call["press_enter"] is True, (
        "Auto-unstick must submit the brief (press_enter=True); otherwise "
        "the architect agent never processes it. See issue #1420."
    )
