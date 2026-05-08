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


def test_cadence_handler_dispatches_stuck_draft(
    now: datetime, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale draft finding triggers an architect dispatch (#1440)."""
    from pollypm.plugins_builtin.core_recurring.audit_watchdog import (
        _scan_one_project,
    )

    task = _StatefulTask(
        project="savethenovel",
        task_number=8,
        work_status="draft",
        created_at=now - timedelta(hours=12),
        updated_at=now - timedelta(hours=12),
        created_by="architect",
        title="Decide next chapter plan",
    )

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
        lambda key, path: [task],
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

    assert counters["findings"] == 1
    assert counters["dispatches_sent"] == 1
    assert counters["dispatches_throttled"] == 0
    assert len(sent) == 1
    target, brief = sent[0]
    assert target == "pollypm-storage-closet:architect-savethenovel"
    assert RULE_STUCK_DRAFT in brief
    assert "savethenovel/8" in brief

    rows = [
        json.loads(line)
        for line in central_log_path("savethenovel")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    dispatch_rows = [
        r for r in rows if r["event"] == "watchdog.escalation_dispatched"
    ]
    assert len(dispatch_rows) == 1
    assert dispatch_rows[0]["metadata"]["finding_type"] == RULE_STUCK_DRAFT


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


# ---------------------------------------------------------------------------
# Regression: _gather_open_tasks against canonical workspace DB (#1419)
# ---------------------------------------------------------------------------


def test_gather_open_tasks_routes_through_factory_to_workspace_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, now: datetime,
) -> None:
    """``_gather_open_tasks`` must read the canonical workspace DB.

    Regression for #1419. Before the fix, the watchdog hardcoded
    ``<project_path>/.pollypm/state.db`` — a path that #1004 collapsed
    into the workspace-root DB. On post-#1004 layouts, the per-project
    file is either missing or empty, so the legacy lookup silently
    returned ``[]`` and ``role_session_missing`` no-oped in production.

    This test seeds a real ``in_progress`` task into a workspace-layout
    DB, points the resolver at that DB, leaves the per-project path
    empty, and confirms ``_gather_open_tasks`` finds the task. It then
    runs the full ``_scan_one_project`` and asserts
    ``role_session_missing`` actually fires.
    """
    from pollypm.audit.watchdog import RULE_ROLE_SESSION_MISSING
    from pollypm.plugins_builtin.core_recurring.audit_watchdog import (
        _gather_open_tasks,
        _scan_one_project,
    )
    from pollypm.work import create_work_service

    # Workspace-layout DB at <workspace_root>/.pollypm/state.db.
    workspace_root = tmp_path / "workspace"
    canonical_db = workspace_root / ".pollypm" / "state.db"
    canonical_db.parent.mkdir(parents=True, exist_ok=True)

    # Project path that, in the legacy layout, would have its own DB.
    # We deliberately leave <project_path>/.pollypm/state.db absent to
    # prove the fix doesn't depend on it existing.
    project_path = tmp_path / "workspace" / "savethenovel"
    project_path.mkdir(parents=True, exist_ok=True)
    legacy_per_project_db = project_path / ".pollypm" / "state.db"
    assert not legacy_per_project_db.exists(), (
        "fixture precondition: per-project DB must NOT exist so we can "
        "prove the fix reads the canonical workspace DB instead."
    )

    # Pin the resolver to the workspace-layout DB. This is the same
    # surface the production code path takes — load_config() resolves
    # workspace_root, and the canonical DB sits at <root>/.pollypm/state.db.
    monkeypatch.setattr(
        "pollypm.work.db_resolver.resolve_work_db_path",
        lambda *a, **kw: canonical_db,
    )

    # Seed an in_progress task in the canonical DB so the
    # role_session_missing rule has fuel to fire on.
    with create_work_service(
        db_path=canonical_db, project_path=project_path,
    ) as svc:
        task = svc.create(
            title="reviewer agent never spawned",
            description="savethenovel/N — reviewer-savethenovel window absent",
            type="task",
            project="savethenovel",
            flow_template="standard",
            roles={"worker": "claude:worker", "reviewer": "claude:reviewer"},
            priority="normal",
            created_by="tester",
        )
        # Drop it into in_progress directly. We only care that the
        # downstream query yields a non-terminal row whose work_status
        # the rule accepts; transition_manager bookkeeping isn't part
        # of the contract being regressed here.
        svc._conn.execute(
            "UPDATE work_tasks SET work_status = ? "
            "WHERE project = ? AND task_number = ?",
            ("in_progress", task.project, task.task_number),
        )
        svc._conn.commit()
        task_number = task.task_number

    # 1. Direct contract: _gather_open_tasks finds the seeded task.
    open_tasks = _gather_open_tasks("savethenovel", project_path)
    assert len(open_tasks) == 1, (
        "Expected the canonical workspace DB to surface 1 open task; "
        f"got {len(open_tasks)}. The legacy per-project lookup would "
        "have returned [] here."
    )
    assert open_tasks[0].project == "savethenovel"
    assert open_tasks[0].task_number == task_number

    # 2. End-to-end: role_session_missing fires for that task.
    # The seeded task is in_progress with a ``worker`` role assigned, so
    # the rule expects a ``worker-savethenovel`` window. We deliberately
    # leave that window out of the storage closet to mirror savethenovel's
    # production failure shape (the role agent never spawned).
    monkeypatch.setattr(
        "pollypm.plugins_builtin.core_recurring.audit_watchdog._gather_storage_windows",
        lambda name: ["architect-savethenovel"],
    )
    # Capture send-keys instead of touching tmux.
    monkeypatch.setattr(
        "pollypm.plugins_builtin.core_recurring.audit_watchdog._send_brief_to_architect",
        lambda target, brief: True,
    )

    store = _RecordingStore()
    counters = _scan_one_project(
        project_key="savethenovel",
        project_path=project_path,
        msg_store=store,
        state_store=None,
        now=now,
        config=WatchdogConfig(),
        storage_closet_name="pollypm-storage-closet",
    )

    role_alerts = [
        a for a in store.alerts
        if RULE_ROLE_SESSION_MISSING in a[0]
    ]
    assert len(role_alerts) == 1, (
        f"role_session_missing should fire exactly once for the "
        f"seeded in_progress task. Captured alerts: {store.alerts}"
    )
    assert counters["findings"] >= 1


def test_gather_open_tasks_returns_empty_on_factory_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``create_work_service`` raises, the watchdog stays alive.

    The rule must no-op for the project rather than crash the cadence
    handler. Mirrors the broad-except in the production code; this
    test pins the contract so a future "raise on missing config"
    refactor of the factory can't silently break the heartbeat.
    """
    from pollypm.plugins_builtin.core_recurring.audit_watchdog import (
        _gather_open_tasks,
    )

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated factory failure")

    monkeypatch.setattr("pollypm.work.create_work_service", _boom)
    monkeypatch.setattr(
        "pollypm.plugins_builtin.core_recurring.audit_watchdog.create_work_service",
        _boom,
        raising=False,
    )

    assert _gather_open_tasks("demo", None) == []


# ---------------------------------------------------------------------------
# #1424 — task_on_hold_stale rule + on_hold escalation dispatch
# ---------------------------------------------------------------------------


def test_task_on_hold_stale_fires_after_threshold(now: datetime) -> None:
    """A task at status=on_hold for >threshold fires the new rule."""
    from pollypm.audit.watchdog import RULE_TASK_ON_HOLD_STALE

    events = [
        _make_event(
            event=EVENT_TASK_STATUS_CHANGED,
            project="savethenovel",
            subject="savethenovel/11",
            metadata={
                "from": "review",
                "to": "on_hold",
                "reason": (
                    "[architect-actionable] Footer.astro placeholder "
                    "copy fails 'No placeholders'"
                ),
            },
            ts=now - timedelta(minutes=20),
        ),
    ]
    findings = scan_events(events, now=now)
    matched = [f for f in findings if f.rule == RULE_TASK_ON_HOLD_STALE]
    assert len(matched) == 1
    f = matched[0]
    assert f.subject == "savethenovel/11"
    assert f.project == "savethenovel"
    assert f.metadata["routing"] == "architect-actionable"
    assert "Footer.astro" in (f.metadata.get("reason") or "")
    assert "review" == f.metadata["from"]


def test_task_on_hold_stale_silent_within_grace(now: datetime) -> None:
    """An on_hold transition within the grace window doesn't fire."""
    from pollypm.audit.watchdog import RULE_TASK_ON_HOLD_STALE

    events = [
        _make_event(
            event=EVENT_TASK_STATUS_CHANGED,
            project="demo",
            subject="demo/3",
            metadata={"from": "review", "to": "on_hold", "reason": "..."},
            # Default threshold is 900s = 15 min; 5 min ago is fresh.
            ts=now - timedelta(minutes=5),
        ),
    ]
    findings = scan_events(events, now=now)
    assert not any(f.rule == RULE_TASK_ON_HOLD_STALE for f in findings)


def test_task_on_hold_stale_silent_when_resumed(now: datetime) -> None:
    """A later transition out of on_hold suppresses the finding."""
    from pollypm.audit.watchdog import RULE_TASK_ON_HOLD_STALE

    events = [
        _make_event(
            event=EVENT_TASK_STATUS_CHANGED,
            project="demo",
            subject="demo/3",
            metadata={"from": "review", "to": "on_hold"},
            ts=now - timedelta(minutes=45),
        ),
        _make_event(
            event=EVENT_TASK_STATUS_CHANGED,
            project="demo",
            subject="demo/3",
            metadata={"from": "on_hold", "to": "queued"},
            ts=now - timedelta(minutes=5),
        ),
    ]
    findings = scan_events(events, now=now)
    assert not any(f.rule == RULE_TASK_ON_HOLD_STALE for f in findings)


def test_task_on_hold_stale_does_not_disturb_review_stale(now: datetime) -> None:
    """Existing review_stale rule still fires only on status=review.

    Regression guard for #1424's hard constraint: the new rule MUST be
    additive. A status=review task should never trigger the on_hold rule
    and vice-versa.
    """
    from pollypm.audit.watchdog import (
        RULE_TASK_ON_HOLD_STALE,
        RULE_TASK_REVIEW_STALE,
    )

    events = [
        _make_event(
            event=EVENT_TASK_STATUS_CHANGED,
            project="demo",
            subject="demo/4",
            metadata={"from": "in_progress", "to": "review"},
            ts=now - timedelta(minutes=45),
        ),
        _make_event(
            event=EVENT_TASK_STATUS_CHANGED,
            project="demo",
            subject="demo/5",
            metadata={"from": "review", "to": "on_hold", "reason": "x"},
            ts=now - timedelta(minutes=20),
        ),
    ]
    findings = scan_events(events, now=now)
    review = [f for f in findings if f.rule == RULE_TASK_REVIEW_STALE]
    on_hold = [f for f in findings if f.rule == RULE_TASK_ON_HOLD_STALE]
    assert len(review) == 1
    assert len(on_hold) == 1
    assert review[0].subject == "demo/4"
    assert on_hold[0].subject == "demo/5"


def test_task_on_hold_stale_human_needed_routing(now: datetime) -> None:
    """A reviewer-tagged ``[human-needed]`` reason routes to human."""
    from pollypm.audit.watchdog import (
        ON_HOLD_HUMAN_NEEDED_TAG,
        RULE_TASK_ON_HOLD_STALE,
    )

    events = [
        _make_event(
            event=EVENT_TASK_STATUS_CHANGED,
            project="demo",
            subject="demo/9",
            metadata={
                "from": "review",
                "to": "on_hold",
                "reason": "[human-needed] Need product call on copy direction",
            },
            ts=now - timedelta(minutes=20),
        ),
    ]
    findings = scan_events(events, now=now)
    matched = [f for f in findings if f.rule == RULE_TASK_ON_HOLD_STALE]
    assert len(matched) == 1
    assert matched[0].metadata["routing"] == ON_HOLD_HUMAN_NEEDED_TAG


def test_format_unstick_brief_on_hold_includes_evidence_and_default() -> None:
    """The brief lists the reviewer's reason, evidence, and 'fix and re-submit' default."""
    from pollypm.audit.watchdog import (
        RULE_TASK_ON_HOLD_STALE,
        format_unstick_brief,
    )

    finding = Finding(
        rule=RULE_TASK_ON_HOLD_STALE,
        project="savethenovel",
        subject="savethenovel/11",
        message="Task savethenovel/11 has been at on_hold for ~20 min",
        recommendation="Architect: re-read the rationale.",
        metadata={
            "stuck_minutes": 20,
            "on_hold_since": "2026-05-07T08:30:00+00:00",
            "from": "review",
            "reason": (
                "[architect-actionable] Footer.astro:20 placeholder "
                "copy + untracked planning docs"
            ),
            "routing": "architect-actionable",
            "reviewer_evidence": [
                "reviewer exec [code_review @ 2026-05-07T08:25:00+00:00] "
                "decision=rejected reason: placeholder copy fails 'No placeholders'",
                "inbox msg from russell: review of savethenovel/11 — "
                "Footer.astro:20 has TODO copy, untracked docs",
            ],
        },
    )
    brief = format_unstick_brief(finding)
    assert brief.startswith("WATCHDOG ESCALATION")
    assert "Project: savethenovel" in brief
    assert "Finding: task_on_hold_stale" in brief
    assert "20 minutes" in brief
    # The on_hold transition reason and reviewer evidence must both be visible.
    assert "Footer.astro:20" in brief
    assert "On-hold transition reason:" in brief
    assert "Routing: architect-actionable" in brief
    # Reviewer evidence section must list both lines.
    assert "Recent reviewer/inbox rationale evidence" in brief
    assert "code_review" in brief
    assert "russell" in brief
    # Default action must steer the architect to fix and re-queue.
    assert "DEFAULT" in brief or "default" in brief.lower()
    assert "pm task queue savethenovel/11" in brief
    assert "pm task approve savethenovel/11" in brief
    # Escalation path is mentioned but framed as last-resort.
    assert "pm notify" in brief


def test_format_unstick_brief_on_hold_distinguishes_transition_reason() -> None:
    """A policy/merge hold reason must not be mislabeled as reviewer rationale."""
    from pollypm.audit.watchdog import (
        RULE_TASK_ON_HOLD_STALE,
        format_unstick_brief,
    )

    finding = Finding(
        rule=RULE_TASK_ON_HOLD_STALE,
        project="savethenovel",
        subject="savethenovel/11",
        metadata={
            "stuck_minutes": 126,
            "on_hold_since": "2026-05-07T15:46:17.005979+00:00",
            "from": "review",
            "reason": (
                "Waiting on operator: review passed, but approve auto-merge "
                "is blocked because the project root has untracked planning docs."
            ),
            "routing": "architect-actionable",
            "reviewer_evidence": [
                "inbox msg from reviewer_savethenovel: review decision blocked "
                "savethenovel/11 — Decision would be reject: Criterion 3 "
                "(No placeholders) fails. Live evidence: "
                "src/components/Footer.astro:20-22 renders placeholder copy.",
            ],
        },
    )

    brief = format_unstick_brief(finding)

    assert "Reviewer rationale: Waiting on operator" not in brief
    assert "On-hold transition reason: Waiting on operator" in brief
    assert "Recent reviewer/inbox rationale evidence" in brief
    assert "Decision would be reject" in brief
    assert "Footer.astro:20-22" in brief
    assert brief.index("Decision would be reject") < brief.index(
        "On-hold transition reason"
    )


def test_format_unstick_brief_on_hold_handles_missing_evidence() -> None:
    """When no reviewer evidence is available, the brief still renders cleanly."""
    from pollypm.audit.watchdog import (
        RULE_TASK_ON_HOLD_STALE,
        format_unstick_brief,
    )

    finding = Finding(
        rule=RULE_TASK_ON_HOLD_STALE,
        project="demo",
        subject="demo/1",
        metadata={
            "stuck_minutes": 25,
            "on_hold_since": "2026-05-07T08:00:00+00:00",
            "from": "in_progress",
            "reason": None,
            "routing": "architect-actionable",
        },
    )
    brief = format_unstick_brief(finding)
    assert "no transition reason" in brief.lower()
    # Don't include a stale reviewer-evidence header.
    assert "No additional reviewer execution rows" in brief


def test_gather_reviewer_evidence_checks_project_and_global_inbox_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy reviewer notifies may be global inbox rows, not project-scoped rows."""
    from pollypm.plugins_builtin.core_recurring.audit_watchdog import (
        _gather_reviewer_evidence,
    )

    def _boom_create_work_service(*_args, **_kwargs):
        raise RuntimeError("skip execution lookup")

    monkeypatch.setattr(
        "pollypm.work.create_work_service",
        _boom_create_work_service,
    )

    class _MsgStore:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def query_messages(self, **filters):  # noqa: ANN001
            self.calls.append(filters)
            if filters.get("scope") == "savethenovel":
                return [
                    {
                        "id": 1,
                        "subject": "savethenovel/11 approval blocked",
                        "body": "Reviewed savethenovel/11 as approve-blocked.",
                        "sender": "heartbeat",
                        "created_at": "2026-05-07T15:46:31+00:00",
                    },
                ]
            if filters.get("scope") == "inbox":
                return [
                    {
                        "id": 4,
                        "subject": "savethenovel/11 review completed but task is on_hold",
                        "body": (
                            "Russell completed code review for savethenovel/11. "
                            "Verdict would be APPROVE after the hold is cleared."
                        ),
                        "sender": "heartbeat",
                        "created_at": "2026-05-07T15:49:50+00:00",
                    },
                    {
                        "id": 3,
                        "subject": "savethenovel/11 review complete but task is on_hold",
                        "body": (
                            "Russell completed code review for savethenovel/11 "
                            "and would approve it, but pm task approve is blocked."
                        ),
                        "sender": "heartbeat",
                        "created_at": "2026-05-07T15:48:08+00:00",
                    },
                    {
                        "id": 2,
                        "subject": "review decision blocked: savethenovel/11 on_hold",
                        "body": (
                            "Decision would be reject: Criterion 3 "
                            "(No placeholders) fails. Live evidence: "
                            "src/components/Footer.astro:20-22 renders "
                            "placeholder copy."
                        ),
                        "sender": "reviewer_savethenovel",
                        "created_at": "2026-05-07T15:47:13+00:00",
                    },
                ]
            return []

    store = _MsgStore()

    evidence = _gather_reviewer_evidence(
        project_key="savethenovel",
        project_path=None,
        subject="savethenovel/11",
        msg_store=store,
    )

    assert {"scope": "savethenovel"} in store.calls
    assert {"scope": "inbox"} in store.calls
    assert any("Decision would be reject" in line for line in evidence)
    assert any("Footer.astro:20-22" in line for line in evidence)


def test_cadence_handler_dispatches_on_hold_with_reviewer_evidence(
    now: datetime, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """End-to-end: an on_hold transition triggers a brief carrying evidence.

    Seeds an on_hold transition into the central audit log and a real
    reviewer rejection into a workspace-layout DB. The cadence handler
    should fire ``task_on_hold_stale``, fold the reviewer's exec row +
    inbox message into the brief, and send the result to the architect's
    pane via the (stubbed) tmux send_keys.
    """
    from pollypm.audit.log import central_log_path
    from pollypm.audit.watchdog import RULE_TASK_ON_HOLD_STALE
    from pollypm.plugins_builtin.core_recurring.audit_watchdog import (
        _scan_one_project,
    )
    from pollypm.work import create_work_service

    workspace_root = tmp_path / "workspace"
    canonical_db = workspace_root / ".pollypm" / "state.db"
    canonical_db.parent.mkdir(parents=True, exist_ok=True)
    project_path = workspace_root / "savethenovel"
    project_path.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        "pollypm.work.db_resolver.resolve_work_db_path",
        lambda *a, **kw: canonical_db,
    )

    # Seed the task and a reviewer rejection row into the work-service DB.
    with create_work_service(
        db_path=canonical_db, project_path=project_path,
    ) as svc:
        task = svc.create(
            title="hero copy",
            description="hero copy work",
            type="task",
            project="savethenovel",
            flow_template="standard",
            roles={"worker": "claude:worker", "reviewer": "claude:reviewer"},
            priority="normal",
            created_by="tester",
        )
        # Insert a reviewer execution row directly so the test stays
        # decoupled from the flow engine's transition wiring.
        svc._conn.execute(
            "INSERT INTO work_node_executions "
            "(task_project, task_number, node_id, visit, status, "
            " decision, decision_reason, started_at, completed_at, "
            " work_output) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task.project,
                task.task_number,
                "code_review",
                1,
                "completed",
                "rejected",
                "Footer.astro:20 placeholder copy fails No placeholders",
                "2026-05-07T08:20:00+00:00",
                "2026-05-07T08:25:00+00:00",
                None,
            ),
        )
        svc._conn.execute(
            "UPDATE work_tasks SET work_status = ? "
            "WHERE project = ? AND task_number = ?",
            ("on_hold", task.project, task.task_number),
        )
        svc._conn.commit()
        task_number = task.task_number

    subject = f"savethenovel/{task_number}"

    # Seed a stale on_hold transition into the central audit tail.
    central = central_log_path("savethenovel")
    central.parent.mkdir(parents=True, exist_ok=True)
    stale_ts = (now - timedelta(minutes=20)).isoformat()
    payload = {
        "schema": 1,
        "ts": stale_ts,
        "project": "savethenovel",
        "event": EVENT_TASK_STATUS_CHANGED,
        "subject": subject,
        "actor": "russell",
        "status": "ok",
        "metadata": {
            "from": "review",
            "to": "on_hold",
            "reason": (
                "[architect-actionable] Footer.astro:20 placeholder "
                "copy + untracked planning docs"
            ),
        },
    }
    central.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    # Capture tmux send-keys instead of touching tmux.
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "pollypm.plugins_builtin.core_recurring.audit_watchdog._send_brief_to_architect",
        lambda target, brief: sent.append((target, brief)) or True,
    )
    monkeypatch.setattr(
        "pollypm.plugins_builtin.core_recurring.audit_watchdog._gather_storage_windows",
        lambda name: [],
    )

    class _MsgStore:
        """Minimal stand-in that captures upserts AND surfaces a reviewer message."""

        def __init__(self) -> None:
            self.alerts: list = []

        def upsert_alert(
            self, scope: str, alert_type: str, severity: str, message: str,
        ) -> None:
            self.alerts.append((scope, alert_type, severity, message))

        def query_messages(self, **filters):  # noqa: ANN001
            return [
                {
                    "title": f"Russell: review of {subject}",
                    "subject": subject,
                    "body": (
                        f"Rejecting {subject}: untracked planning docs at "
                        "project root + Footer.astro:20 placeholder copy."
                    ),
                    "requester": "russell",
                    "actor": "russell",
                    "created_at": "2026-05-07T08:30:00+00:00",
                    "project": "savethenovel",
                },
            ]

    store = _MsgStore()
    counters = _scan_one_project(
        project_key="savethenovel",
        project_path=project_path,
        msg_store=store,
        state_store=None,
        now=now,
        config=WatchdogConfig(),
        storage_closet_name="pollypm-storage-closet",
    )

    assert counters["findings"] >= 1
    assert counters["dispatches_sent"] == 1
    assert len(sent) == 1
    target, brief = sent[0]
    assert target == "pollypm-storage-closet:architect-savethenovel"
    assert RULE_TASK_ON_HOLD_STALE in brief
    assert subject in brief
    # Reviewer rationale (from the on_hold reason) must be in the brief.
    assert "Footer.astro:20" in brief
    # Reviewer execution row must be folded in via the enrichment path.
    assert "code_review" in brief
    assert "rejected" in brief
    # Inbox-message lookup must surface russell's review message.
    assert "russell" in brief.lower()
    # Default-fix framing.
    assert "pm task queue " + subject in brief


def test_cadence_handler_throttles_on_hold_repeat_dispatch(
    now: datetime, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 30-min throttle applies to on_hold escalations too."""
    from pollypm.audit.log import central_log_path
    from pollypm.plugins_builtin.core_recurring.audit_watchdog import (
        _scan_one_project,
    )

    central = central_log_path("savethenovel")
    central.parent.mkdir(parents=True, exist_ok=True)
    stale_ts = (now - timedelta(minutes=20)).isoformat()
    payload = {
        "schema": 1,
        "ts": stale_ts,
        "project": "savethenovel",
        "event": EVENT_TASK_STATUS_CHANGED,
        "subject": "savethenovel/11",
        "actor": "russell",
        "status": "ok",
        "metadata": {
            "from": "review",
            "to": "on_hold",
            "reason": "[architect-actionable] x",
        },
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
    monkeypatch.setattr(
        "pollypm.plugins_builtin.core_recurring.audit_watchdog._gather_reviewer_evidence",
        lambda **kw: [],
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
    assert len(sent) == 1


def test_classify_on_hold_reason_defaults_to_architect() -> None:
    """Untagged or unknown-tag reason routes to architect-actionable."""
    from pollypm.audit.watchdog import (
        ON_HOLD_ARCHITECT_TAG,
        ON_HOLD_HUMAN_NEEDED_TAG,
        _classify_on_hold_reason,
    )

    assert _classify_on_hold_reason(None) == ON_HOLD_ARCHITECT_TAG
    assert _classify_on_hold_reason("") == ON_HOLD_ARCHITECT_TAG
    assert _classify_on_hold_reason("just stuck") == ON_HOLD_ARCHITECT_TAG
    assert _classify_on_hold_reason("[architect-actionable] x") == ON_HOLD_ARCHITECT_TAG
    assert _classify_on_hold_reason("architect-actionable: x") == ON_HOLD_ARCHITECT_TAG
    assert _classify_on_hold_reason("[human-needed] copy") == ON_HOLD_HUMAN_NEEDED_TAG
    assert _classify_on_hold_reason("human-needed: y") == ON_HOLD_HUMAN_NEEDED_TAG
    # Case-insensitive
    assert _classify_on_hold_reason("[HUMAN-NEEDED] z") == ON_HOLD_HUMAN_NEEDED_TAG


# ---------------------------------------------------------------------------
# #1433 — state-based detection for review / on_hold / stuck_draft
#
# Issue #1433: previously these rules only fired when a matching
# transition event landed inside the audit-log scan window (~1h). Tasks
# that entered the watched state earlier were invisible — savethenovel/11
# sat at on_hold for ~110 min and never got escalated. The state-based
# path queries the live ``work_tasks`` view via ``open_tasks`` so the
# detection horizon no longer depends on the audit log's retention.
# ---------------------------------------------------------------------------


class _FakeTransition:
    """Minimal stand-in for ``work.models.Transition`` (frozen-ish)."""

    def __init__(
        self,
        *,
        from_state: str,
        to_state: str,
        timestamp: datetime,
        actor: str = "polly",
        reason: str | None = None,
    ) -> None:
        self.from_state = from_state
        self.to_state = to_state
        self.timestamp = timestamp
        self.actor = actor
        self.reason = reason


class _StatefulTask:
    """Stand-in for ``work.models.Task`` with state + transitions + timestamps."""

    class _Status:
        def __init__(self, value: str) -> None:
            self.value = value

    def __init__(
        self,
        *,
        project: str,
        task_number: int,
        work_status: str,
        transitions: list[_FakeTransition] | None = None,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
        created_by: str = "polly",
        title: str = "",
    ) -> None:
        self.project = project
        self.task_number = task_number
        self.work_status = self._Status(work_status)
        self.transitions = list(transitions or [])
        self.created_at = created_at
        self.updated_at = updated_at
        self.created_by = created_by
        self.title = title
        # Keep the role-session detector happy when reused.
        self.roles: dict[str, str] = {}
        self.assignee: str | None = None


def test_task_review_stale_state_based_fires_for_old_entry(now: datetime) -> None:
    """A task at status=review whose review transition predates the
    audit-event scan window still fires via the state-based path."""
    from pollypm.audit.watchdog import RULE_TASK_REVIEW_STALE

    # Transition is 4h old — well outside the default scan window.
    task = _StatefulTask(
        project="savethenovel",
        task_number=10,
        work_status="review",
        transitions=[
            _FakeTransition(
                from_state="in_progress",
                to_state="review",
                timestamp=now - timedelta(hours=4),
                actor="russell",
            ),
        ],
        created_at=now - timedelta(hours=5),
        updated_at=now - timedelta(hours=4),
    )
    findings = scan_events([], now=now, open_tasks=[task])
    matched = [f for f in findings if f.rule == RULE_TASK_REVIEW_STALE]
    assert len(matched) == 1
    f = matched[0]
    assert f.subject == "savethenovel/10"
    assert f.metadata["detected_via"] == "state"
    assert f.metadata["stuck_minutes"] >= 30


def test_task_review_stale_state_based_silent_when_briefly_in_state(
    now: datetime,
) -> None:
    """A task that just entered review (within grace) does not fire."""
    from pollypm.audit.watchdog import RULE_TASK_REVIEW_STALE

    task = _StatefulTask(
        project="demo",
        task_number=3,
        work_status="review",
        transitions=[
            _FakeTransition(
                from_state="in_progress",
                to_state="review",
                timestamp=now - timedelta(minutes=2),
                actor="polly",
            ),
        ],
        updated_at=now - timedelta(minutes=2),
    )
    findings = scan_events([], now=now, open_tasks=[task])
    assert not any(f.rule == RULE_TASK_REVIEW_STALE for f in findings)


def test_task_review_stale_state_based_silent_when_no_longer_in_state(
    now: datetime,
) -> None:
    """A task that has moved out of review is not reported."""
    from pollypm.audit.watchdog import RULE_TASK_REVIEW_STALE

    task = _StatefulTask(
        project="demo",
        task_number=3,
        work_status="done",  # No longer in review
        transitions=[
            _FakeTransition(
                from_state="in_progress",
                to_state="review",
                timestamp=now - timedelta(hours=4),
            ),
            _FakeTransition(
                from_state="review",
                to_state="done",
                timestamp=now - timedelta(minutes=10),
            ),
        ],
        updated_at=now - timedelta(minutes=10),
    )
    findings = scan_events([], now=now, open_tasks=[task])
    assert not any(f.rule == RULE_TASK_REVIEW_STALE for f in findings)


def test_task_review_stale_state_dedupes_with_event_path(now: datetime) -> None:
    """A task visible to BOTH paths fires exactly once (state path wins)."""
    from pollypm.audit.watchdog import RULE_TASK_REVIEW_STALE

    task = _StatefulTask(
        project="demo",
        task_number=4,
        work_status="review",
        transitions=[
            _FakeTransition(
                from_state="in_progress",
                to_state="review",
                timestamp=now - timedelta(minutes=45),
            ),
        ],
        updated_at=now - timedelta(minutes=45),
    )
    events = [
        _make_event(
            event=EVENT_TASK_STATUS_CHANGED,
            project="demo",
            subject="demo/4",
            metadata={"from": "in_progress", "to": "review"},
            ts=now - timedelta(minutes=45),
        ),
    ]
    findings = scan_events(events, now=now, open_tasks=[task])
    matched = [f for f in findings if f.rule == RULE_TASK_REVIEW_STALE]
    assert len(matched) == 1
    assert matched[0].metadata["detected_via"] == "state"


def test_task_review_stale_state_falls_back_to_updated_at(now: datetime) -> None:
    """A task with no hydrated transitions still fires using updated_at."""
    from pollypm.audit.watchdog import RULE_TASK_REVIEW_STALE

    task = _StatefulTask(
        project="demo",
        task_number=5,
        work_status="review",
        transitions=[],  # no history loaded
        updated_at=now - timedelta(hours=2),
        created_at=now - timedelta(hours=3),
    )
    findings = scan_events([], now=now, open_tasks=[task])
    matched = [f for f in findings if f.rule == RULE_TASK_REVIEW_STALE]
    assert len(matched) == 1
    assert matched[0].metadata["detected_via"] == "state"


def test_task_on_hold_stale_state_based_fires_for_old_entry(
    now: datetime,
) -> None:
    """savethenovel/11 class — on_hold > 1h, no recent transition events."""
    from pollypm.audit.watchdog import RULE_TASK_ON_HOLD_STALE

    task = _StatefulTask(
        project="savethenovel",
        task_number=11,
        work_status="on_hold",
        transitions=[
            _FakeTransition(
                from_state="review",
                to_state="on_hold",
                timestamp=now - timedelta(minutes=110),
                actor="russell",
                reason="[architect-actionable] Footer.astro:20 placeholder copy",
            ),
        ],
        updated_at=now - timedelta(minutes=110),
    )
    findings = scan_events([], now=now, open_tasks=[task])
    matched = [f for f in findings if f.rule == RULE_TASK_ON_HOLD_STALE]
    assert len(matched) == 1
    f = matched[0]
    assert f.subject == "savethenovel/11"
    assert f.metadata["detected_via"] == "state"
    assert f.metadata["routing"] == "architect-actionable"
    assert "Footer.astro" in (f.metadata.get("reason") or "")
    assert f.metadata["stuck_minutes"] >= 90


def test_task_on_hold_stale_state_based_silent_when_briefly_in_state(
    now: datetime,
) -> None:
    """An on_hold task entered <threshold ago does not fire."""
    from pollypm.audit.watchdog import RULE_TASK_ON_HOLD_STALE

    task = _StatefulTask(
        project="demo",
        task_number=3,
        work_status="on_hold",
        transitions=[
            _FakeTransition(
                from_state="review",
                to_state="on_hold",
                timestamp=now - timedelta(minutes=5),
            ),
        ],
        updated_at=now - timedelta(minutes=5),
    )
    findings = scan_events([], now=now, open_tasks=[task])
    assert not any(f.rule == RULE_TASK_ON_HOLD_STALE for f in findings)


def test_task_on_hold_stale_state_dedupes_with_event_path(now: datetime) -> None:
    """A task visible to BOTH paths fires once; state path wins."""
    from pollypm.audit.watchdog import RULE_TASK_ON_HOLD_STALE

    task = _StatefulTask(
        project="demo",
        task_number=6,
        work_status="on_hold",
        transitions=[
            _FakeTransition(
                from_state="review",
                to_state="on_hold",
                timestamp=now - timedelta(minutes=20),
                reason="[architect-actionable] x",
            ),
        ],
        updated_at=now - timedelta(minutes=20),
    )
    events = [
        _make_event(
            event=EVENT_TASK_STATUS_CHANGED,
            project="demo",
            subject="demo/6",
            metadata={"from": "review", "to": "on_hold", "reason": "[architect-actionable] x"},
            ts=now - timedelta(minutes=20),
        ),
    ]
    findings = scan_events(events, now=now, open_tasks=[task])
    matched = [f for f in findings if f.rule == RULE_TASK_ON_HOLD_STALE]
    assert len(matched) == 1
    assert matched[0].metadata["detected_via"] == "state"


def test_stuck_draft_state_based_fires_for_old_draft(now: datetime) -> None:
    """A task currently at status=draft older than threshold fires."""
    task = _StatefulTask(
        project="demo",
        task_number=2,
        work_status="draft",
        transitions=[],
        created_at=now - timedelta(hours=2),
        updated_at=now - timedelta(hours=2),
        created_by="polly",
        title="Plan the next chapter",
    )
    findings = scan_events([], now=now, open_tasks=[task])
    matched = [f for f in findings if f.rule == RULE_STUCK_DRAFT]
    assert len(matched) == 1
    f = matched[0]
    assert f.subject == "demo/2"
    assert f.metadata["detected_via"] == "state"
    assert f.metadata["title"] == "Plan the next chapter"


def test_stuck_draft_state_based_silent_when_promoted(now: datetime) -> None:
    """A task that has moved out of draft is not reported."""
    task = _StatefulTask(
        project="demo",
        task_number=2,
        work_status="queued",  # promoted
        created_at=now - timedelta(hours=2),
        updated_at=now - timedelta(minutes=10),
    )
    findings = scan_events([], now=now, open_tasks=[task])
    assert not any(f.rule == RULE_STUCK_DRAFT for f in findings)


def test_stuck_draft_state_dedupes_with_event_path(now: datetime) -> None:
    """A draft visible to BOTH paths fires once; state path wins."""
    task = _StatefulTask(
        project="demo",
        task_number=2,
        work_status="draft",
        created_at=now - timedelta(minutes=15),
        updated_at=now - timedelta(minutes=15),
    )
    events = [
        _make_event(
            event=EVENT_TASK_CREATED,
            subject="demo/2",
            metadata={"title": "Plan the next chapter"},
            ts=now - timedelta(minutes=15),
        ),
    ]
    findings = scan_events(events, now=now, open_tasks=[task])
    matched = [f for f in findings if f.rule == RULE_STUCK_DRAFT]
    assert len(matched) == 1
    assert matched[0].metadata["detected_via"] == "state"


def test_state_based_detectors_silent_when_open_tasks_empty(now: datetime) -> None:
    """No open_tasks → state path no-ops, original event path still works."""
    from pollypm.audit.watchdog import (
        RULE_TASK_ON_HOLD_STALE,
        RULE_TASK_REVIEW_STALE,
    )

    findings = scan_events([], now=now, open_tasks=[])
    # No events, no tasks → nothing should fire.
    assert not any(
        f.rule in (RULE_TASK_REVIEW_STALE, RULE_TASK_ON_HOLD_STALE, RULE_STUCK_DRAFT)
        for f in findings
    )
