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
