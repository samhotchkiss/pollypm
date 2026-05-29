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
    EVENT_STUCK_DRAFT_TERMINATED,
    RULE_CANCEL_NO_PROMOTION,
    RULE_CANCELLATION_CHURN,
    RULE_MARKER_LEAKED,
    RULE_ORPHAN_MARKER,
    RULE_STUCK_DRAFT,
    RULE_TASK_PROGRESS_STALE,
    RULE_TASK_REWORK_STALE,
    STUCK_DRAFT_TERMINATOR_THRESHOLD,
    TIER_2,
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
    """An old marker.created with neither release nor terminal transition fires."""
    events = [
        _make_event(
            event=EVENT_MARKER_CREATED,
            project="demo",
            subject="/proj/demo/.pollypm/worker-markers/task-demo-1.fresh",
            ts=now - timedelta(minutes=40),
        ),
    ]
    findings = scan_events(events, now=now)
    orphans = [f for f in findings if f.rule == RULE_ORPHAN_MARKER]
    assert len(orphans) == 1
    assert orphans[0].project == "demo"
    assert orphans[0].subject == "demo/1"
    assert "demo/1" in orphans[0].message
    assert "pm task cancel demo/1" in orphans[0].recommendation


def test_orphan_marker_recent_marker_ignored_even_when_task_is_old(
    now: datetime,
) -> None:
    """Regression #1436: threshold age is marker age, not task age."""
    marker = "/proj/demo/.pollypm/worker-markers/task-demo-1.fresh"
    events = [
        _make_event(
            event=EVENT_TASK_CREATED,
            subject="demo/1",
            ts=now - timedelta(hours=6),
        ),
        _make_event(
            event=EVENT_MARKER_CREATED,
            subject=marker,
            ts=now - timedelta(minutes=4),
        ),
    ]
    findings = [
        f for f in scan_events(events, now=now)
        if f.rule == RULE_ORPHAN_MARKER
    ]
    assert findings == []


def test_orphan_marker_silenced_by_release(now: datetime) -> None:
    marker = "/proj/demo/.pollypm/worker-markers/task-demo-1.fresh"
    events = [
        _make_event(
            event=EVENT_MARKER_CREATED, subject=marker,
            ts=now - timedelta(minutes=40),
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
            ts=now - timedelta(minutes=40),
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


def test_orphan_marker_within_threshold_ignored(now: datetime) -> None:
    config = WatchdogConfig(window_seconds=600)
    marker = "/proj/demo/.pollypm/worker-markers/task-demo-1.fresh"
    events = [
        _make_event(
            event=EVENT_MARKER_CREATED, subject=marker,
            ts=now - timedelta(minutes=9),
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


def test_stuck_draft_terminator_breadcrumbs_collected_after_threshold_and_idempotent(
    now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2333 / #2349 (round 3): after STUCK_DRAFT_TERMINATOR_THRESHOLD
    prior findings for a subject, ``scan_events`` stops surfacing new
    stuck_draft findings AND surfaces a single
    :class:`_TerminatorBreadcrumb` for that subject through the optional
    side-channel. A subsequent scan that also sees the terminator
    breadcrumb in the events sequence must NOT re-collect it —
    otherwise the breadcrumb itself cascades.

    #2349 (round 3) hardening: ``scan_events`` is PURE — collecting the
    breadcrumb is via an explicit list passed by the caller, never via
    a direct ``pollypm.audit.log.emit`` call. The spy proves that even
    when the side-channel is engaged, ``scan_events`` itself performs
    zero durable writes.
    """
    from pollypm.audit.watchdog import _TerminatorBreadcrumb

    emitted: list[tuple[str, str | None]] = []

    def _record(*, event: str, subject: str | None = None, **_: object) -> None:
        emitted.append((event, subject))

    monkeypatch.setattr("pollypm.audit.log.emit", _record)

    # Scan 1: 3 prior stuck_draft findings → terminator engages.
    create_ts = now - timedelta(minutes=15)
    prior_findings = [
        _make_event(
            event=EVENT_AUDIT_FINDING,
            subject="demo/2",
            metadata={"rule": RULE_STUCK_DRAFT},
            ts=now - timedelta(minutes=30 + 5 * i),
        )
        for i in range(STUCK_DRAFT_TERMINATOR_THRESHOLD)
    ]
    create_event = _make_event(
        event=EVENT_TASK_CREATED, subject="demo/2", ts=create_ts,
    )
    breadcrumbs_scan1: list[_TerminatorBreadcrumb] = []
    findings = [
        f for f in scan_events(
            [create_event, *prior_findings],
            now=now,
            stuck_draft_terminator_breadcrumbs=breadcrumbs_scan1,
        )
        if f.rule == RULE_STUCK_DRAFT
    ]
    assert findings == [], "terminator should suppress the new finding"
    assert len(breadcrumbs_scan1) == 1
    assert breadcrumbs_scan1[0].subject == "demo/2"
    assert breadcrumbs_scan1[0].project == "demo"
    assert [
        ev for ev in emitted if ev[0] == EVENT_STUCK_DRAFT_TERMINATED
    ] == [], (
        "scan_events must be pure — no direct audit.log.emit call even "
        "when collecting terminator breadcrumbs"
    )

    # Scan 2: same prior findings + the terminator breadcrumb from
    # scan 1 in the events sequence. The terminator MUST NOT re-collect.
    emitted.clear()
    breadcrumb_event = _make_event(
        event=EVENT_STUCK_DRAFT_TERMINATED,
        subject="demo/2",
        metadata={"rule": RULE_STUCK_DRAFT},
        ts=now - timedelta(minutes=1),
    )
    breadcrumbs_scan2: list[_TerminatorBreadcrumb] = []
    findings_scan2 = [
        f for f in scan_events(
            [create_event, *prior_findings, breadcrumb_event],
            now=now,
            stuck_draft_terminator_breadcrumbs=breadcrumbs_scan2,
        )
        if f.rule == RULE_STUCK_DRAFT
    ]
    assert findings_scan2 == [], "still suppressed across scans"
    assert breadcrumbs_scan2 == [], (
        "terminator must NOT cascade — already_terminated_subjects guard"
    )
    assert [
        ev for ev in emitted if ev[0] == EVENT_STUCK_DRAFT_TERMINATED
    ] == [], "scan_events remains pure across scans"


def test_scan_events_remains_pure_when_terminator_threshold_hit(
    now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2349 (round 3) regression: ``scan_events`` MUST NOT call
    ``pollypm.audit.log.emit`` even when the cascade-terminator
    threshold is crossed.

    Direct/synthetic ``scan_events`` callers (unit tests, ad-hoc
    detector probes) feed event sequences in repeatedly; if the
    detector emitted ``audit.stuck_draft_terminated`` rows during the
    scan, the same input scanned twice would emit twice (or, worse,
    feed back into the next read window and re-cascade). The cadence
    path (:func:`scan_project`) is the sole authorised emitter.

    Contract under test (the round-3 split):
      * The repeat stuck_draft finding for the subject IS suppressed
        (terminator dedup still works in-process).
      * ``pollypm.audit.log.emit`` is called ZERO times by
        ``scan_events`` itself.
    """
    spy_calls: list[dict[str, object]] = []

    def _spy(**kwargs: object) -> None:
        spy_calls.append(kwargs)

    monkeypatch.setattr("pollypm.audit.log.emit", _spy)

    create_ts = now - timedelta(minutes=15)
    subject = "demo/4"
    prior_findings = [
        _make_event(
            event=EVENT_AUDIT_FINDING,
            subject=subject,
            metadata={"rule": RULE_STUCK_DRAFT},
            ts=now - timedelta(minutes=40 + 5 * i),
        )
        for i in range(STUCK_DRAFT_TERMINATOR_THRESHOLD)
    ]
    create_event = _make_event(
        event=EVENT_TASK_CREATED, subject=subject, ts=create_ts,
    )

    findings = [
        f for f in scan_events([create_event, *prior_findings], now=now)
        if f.rule == RULE_STUCK_DRAFT
    ]
    # The repeating stuck_draft is still suppressed by the in-process
    # terminator dedup — the detector remains correct, just pure.
    assert findings == [], (
        "scan_events must still suppress the repeating stuck_draft "
        "finding once the terminator threshold is hit"
    )
    # ZERO writes — this is the round-3 hardening contract.
    assert spy_calls == [], (
        "scan_events performed audit.log.emit calls — detector must be "
        "pure (no I/O). Cadence path (scan_project) is the only "
        "authorised emitter for stuck_draft_terminated breadcrumbs. "
        f"Observed: {spy_calls!r}"
    )


def test_stuck_draft_terminator_durable_across_scan_project_calls(
    now: datetime,
    tmp_path: Path,
) -> None:
    """#2349 regression: the terminator must NOT cascade when the
    cadence handler reads from a per-project audit log.

    Repro: production ``scan_project`` calls ``read_events`` which
    prefers the per-project log when one exists. If prior
    ``audit.finding`` rows are written without ``project_path`` they
    only land on the central tail and ``scan_project`` does not see
    them — so the threshold never counts the prior findings, OR the
    threshold counts only synthetic rows and the terminator
    breadcrumb itself lives only on the central tail (so the next
    scan re-emits it forever).

    Fix: ``emit_finding`` and ``_emit_stuck_draft_terminator`` now
    accept ``project_path`` and both rows land in the per-project log
    alongside the events that drive dedupe.

    This test exercises the cadence-adjacent path: pre-seed the
    per-project log with prior findings via the real
    ``emit_finding`` writer (with ``project_path``), then call
    ``scan_project`` twice with the same ``project_path``. The first
    scan should emit exactly one terminator breadcrumb (also into the
    per-project log). The second scan must see that breadcrumb and
    NOT re-emit.
    """
    from pollypm.audit.log import emit as audit_emit, project_log_path
    from pollypm.audit.watchdog import (
        STUCK_DRAFT_TERMINATOR_THRESHOLD,
        emit_finding,
        Finding,
    )

    project = "savethenovel"
    project_path = tmp_path / project
    (project_path / ".pollypm").mkdir(parents=True)
    per_project = project_log_path(project_path)
    assert per_project is not None

    subject = f"{project}/7"

    # Seed prior findings via the production ``emit_finding`` writer with
    # ``project_path`` so they land in the per-project log — the source
    # ``scan_project`` will read on the next tick.
    for i in range(STUCK_DRAFT_TERMINATOR_THRESHOLD):
        finding = Finding(
            rule=RULE_STUCK_DRAFT,
            project=project,
            subject=subject,
            message=f"prior stuck_draft finding #{i}",
            recommendation="rec",
        )
        emit_finding(finding, project_path=project_path)

    # Also write an old ``task.created`` row so the synthetic-event
    # path inside ``_detect_stuck_drafts`` has a candidate to flag —
    # this is the row that the terminator would suppress on the next
    # scan. Use ``audit_emit`` with ``project_path`` so it lands in
    # the per-project log too (mirroring what work-service does).
    audit_emit(
        event=EVENT_TASK_CREATED,
        project=project,
        subject=subject,
        actor="polly",
        project_path=project_path,
    )

    # Fix up the ``task.created`` ts so it's older than the
    # stuck_draft threshold (the writer stamps "now"). Rewrite the
    # file with the doctored ts. Per-project log content:
    # finding rows + task.created.
    lines = per_project.read_text(encoding="utf-8").splitlines()
    rewritten: list[str] = []
    create_ts = (now - timedelta(minutes=15)).isoformat()
    finding_ts_base = now - timedelta(minutes=45)
    finding_idx = 0
    for raw in lines:
        if not raw.strip():
            continue
        obj = json.loads(raw)
        if obj.get("event") == EVENT_TASK_CREATED:
            obj["ts"] = create_ts
        elif obj.get("event") == "audit.finding":
            # Space the prior findings out in the past so they all
            # fall inside the scan_project lookback window.
            obj["ts"] = (
                finding_ts_base + timedelta(minutes=finding_idx)
            ).isoformat()
            finding_idx += 1
        rewritten.append(json.dumps(obj))
    per_project.write_text("\n".join(rewritten) + "\n", encoding="utf-8")

    # First scan: terminator should engage. The cadence handler
    # passes ``project_path`` so ``scan_project`` reads from the
    # per-project log AND ``_emit_stuck_draft_terminator`` lands the
    # breadcrumb in that same log.
    findings_scan1 = [
        f
        for f in scan_project(project, project_path=project_path, now=now)
        if f.rule == RULE_STUCK_DRAFT
    ]
    assert findings_scan1 == [], (
        "terminator should suppress finding once threshold met"
    )

    # Verify the breadcrumb landed in the per-project log (not just
    # the central tail).
    rows_after_1 = [
        json.loads(line)
        for line in per_project.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    terminator_rows_1 = [
        r for r in rows_after_1
        if r["event"] == EVENT_STUCK_DRAFT_TERMINATED
        and r.get("subject") == subject
    ]
    assert len(terminator_rows_1) == 1, (
        "first scan must write exactly one terminator breadcrumb to "
        "the per-project log"
    )

    # Second scan: the per-project log now contains the breadcrumb
    # from scan 1. ``scan_project`` will read it and the terminator
    # must NOT re-emit — otherwise the breadcrumb itself cascades.
    findings_scan2 = [
        f
        for f in scan_project(project, project_path=project_path, now=now)
        if f.rule == RULE_STUCK_DRAFT
    ]
    assert findings_scan2 == [], "still suppressed across scans"
    rows_after_2 = [
        json.loads(line)
        for line in per_project.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    terminator_rows_2 = [
        r for r in rows_after_2
        if r["event"] == EVENT_STUCK_DRAFT_TERMINATED
        and r.get("subject") == subject
    ]
    assert len(terminator_rows_2) == 1, (
        "second scan must NOT re-emit the terminator — read/write "
        "sources must agree (per-project log)"
    )


def test_stuck_draft_terminator_counts_central_findings_when_project_log_misses(
    now: datetime,
    tmp_path: Path,
) -> None:
    """Production regression: per-project logs may miss audit.finding rows.

    ``scan_project`` still reads lifecycle events from the per-project
    log, but the stuck_draft terminator must count the central
    ``audit.finding`` rows that were durably written there.
    """
    from pollypm.audit.log import emit as audit_emit, project_log_path

    project = "booktalk"
    project_path = tmp_path / project
    (project_path / ".pollypm").mkdir(parents=True)
    per_project = project_log_path(project_path)
    assert per_project is not None

    subject = f"{project}/158"
    for i in range(STUCK_DRAFT_TERMINATOR_THRESHOLD):
        emit_finding(
            Finding(
                rule=RULE_STUCK_DRAFT,
                project=project,
                subject=subject,
                message=f"central-only stuck_draft finding #{i}",
                recommendation="rec",
            ),
            project_path=None,
        )

    audit_emit(
        event=EVENT_TASK_CREATED,
        project=project,
        subject=subject,
        actor="audit_watchdog",
        metadata={
            "title": (
                "Project booktalk has 3 queued task(s) but no claim / "
                "execution / status-change activity for the entire scan window."
            ),
        },
        project_path=project_path,
    )

    central = central_log_path(project)
    central_lines: list[str] = []
    finding_ts_base = now - timedelta(minutes=45)
    finding_idx = 0
    for raw in central.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        obj = json.loads(raw)
        if obj.get("event") == "audit.finding":
            obj["ts"] = (
                finding_ts_base + timedelta(minutes=finding_idx)
            ).isoformat()
            finding_idx += 1
        central_lines.append(json.dumps(obj))
    central.write_text("\n".join(central_lines) + "\n", encoding="utf-8")

    per_project_lines: list[str] = []
    for raw in per_project.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        obj = json.loads(raw)
        if obj.get("event") == EVENT_TASK_CREATED:
            obj["ts"] = (now - timedelta(minutes=15)).isoformat()
        per_project_lines.append(json.dumps(obj))
    per_project.write_text(
        "\n".join(per_project_lines) + "\n",
        encoding="utf-8",
    )

    findings = [
        f
        for f in scan_project(project, project_path=project_path, now=now)
        if f.rule == RULE_STUCK_DRAFT
    ]
    assert findings == [], (
        "central audit.finding rows should trip the terminator even "
        "when the per-project lifecycle log has no finding rows"
    )

    rows = [
        json.loads(line)
        for line in per_project.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    terminator_rows = [
        r for r in rows
        if r["event"] == EVENT_STUCK_DRAFT_TERMINATED
        and r.get("subject") == subject
    ]
    assert len(terminator_rows) == 1


def test_stuck_draft_event_path_cross_checks_open_tasks_status(now: datetime) -> None:
    """#1869: when ``open_tasks`` is supplied, the event-based fallback
    must not fire for tasks that are no longer in draft state.

    Reproduces the false-positive sweep that was raising stuck_draft
    against tasks that the canonical store shows as ``done`` /
    ``cancelled`` — the event-window can lose the matching
    ``task.status_changed`` after a log rotation while still re-reading
    an old ``task.created``. The state cross-check is the safety net.
    """
    class _FakeTaskMin:
        class _Status:
            def __init__(self, value: str) -> None:
                self.value = value

        def __init__(self, project: str, task_number: int, work_status: str) -> None:
            self.project = project
            self.task_number = task_number
            self.work_status = self._Status(work_status)

    events = [
        _make_event(
            event=EVENT_TASK_CREATED,
            subject="pollypm/9",
            project="pollypm",
            ts=now - timedelta(minutes=30),
        ),
    ]
    open_tasks = [_FakeTaskMin("pollypm", 9, "done")]
    findings = [
        f
        for f in scan_events(events, now=now, open_tasks=open_tasks)
        if f.rule == RULE_STUCK_DRAFT
    ]
    assert findings == []


def test_stuck_draft_event_path_still_fires_for_actual_draft_in_open_tasks(
    now: datetime,
) -> None:
    """#1869: a task still in draft state (and present in open_tasks)
    must still surface via either path. State path is authoritative,
    but the event path acting on the same subject must not be
    suppressed when the task is genuinely draft."""
    class _FakeTaskMin:
        class _Status:
            def __init__(self, value: str) -> None:
                self.value = value

        def __init__(self, project: str, task_number: int, work_status: str,
                     created_at: datetime | None = None) -> None:
            self.project = project
            self.task_number = task_number
            self.work_status = self._Status(work_status)
            self.created_at = created_at
            self.created_by = "polly"
            self.title = "draft test"

    events = [
        _make_event(
            event=EVENT_TASK_CREATED,
            subject="demo/2",
            project="demo",
            ts=now - timedelta(minutes=30),
        ),
    ]
    # State-based path fires (created_at supplied, status draft, older
    # than cutoff). Event path would dedupe to one finding.
    open_tasks = [
        _FakeTaskMin("demo", 2, "draft", created_at=now - timedelta(minutes=30)),
    ]
    findings = [
        f
        for f in scan_events(events, now=now, open_tasks=open_tasks)
        if f.rule == RULE_STUCK_DRAFT
    ]
    assert len(findings) == 1
    assert findings[0].subject == "demo/2"


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
    assert findings[0].tier == TIER_2
    assert findings[0].evidence["cancelled_task_id"] == "demo/1"
    assert (
        findings[0].evidence["required_decision"]
        == "queue_replacement_or_mark_project_intentionally_parked"
    )


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


def test_cancel_no_promotion_is_volume_aware(now: datetime) -> None:
    """One later create suppresses one cancel, not every cancel in a burst."""
    events: list[AuditEvent] = []
    for idx in range(6):
        events.append(_make_event(
            event=EVENT_TASK_STATUS_CHANGED,
            subject=f"demo/{idx + 1}",
            metadata={"to": "cancelled"},
            ts=now - timedelta(minutes=20 - idx),
        ))
    events.append(_make_event(
        event=EVENT_TASK_CREATED,
        subject="demo/99",
        ts=now - timedelta(minutes=10),
    ))

    findings = [
        f for f in scan_events(events, now=now)
        if f.rule == RULE_CANCEL_NO_PROMOTION
    ]

    assert len(findings) == 5
    assert {f.subject for f in findings} == {
        "demo/2",
        "demo/3",
        "demo/4",
        "demo/5",
        "demo/6",
    }


def test_cancellation_churn_fires_with_interleaved_creates(now: datetime) -> None:
    """Create/cancel/recreate loops must not mask cancellation volume."""
    events: list[AuditEvent] = []
    for idx in range(5):
        minute = 25 - (idx * 3)
        events.append(_make_event(
            event=EVENT_TASK_STATUS_CHANGED,
            subject=f"demo/{idx + 1}",
            metadata={"from": "draft", "to": "cancelled"},
            ts=now - timedelta(minutes=minute),
        ))
        events.append(_make_event(
            event=EVENT_TASK_CREATED,
            subject=f"demo/{idx + 20}",
            ts=now - timedelta(minutes=minute - 1),
        ))

    findings = [
        f for f in scan_events(events, now=now)
        if f.rule == RULE_CANCELLATION_CHURN
    ]

    assert len(findings) == 1
    assert findings[0].project == "demo"
    assert findings[0].tier == TIER_2
    assert findings[0].metadata["cancel_count"] == 5
    assert findings[0].evidence["required_decision"] == "stop_cancel_recreate_loop"


def test_format_unstick_brief_cancellation_no_promotion_is_decisive() -> None:
    from pollypm.audit.watchdog import format_unstick_brief

    finding = Finding(
        rule=RULE_CANCEL_NO_PROMOTION,
        project="demo",
        subject="demo/1",
        message="Task demo/1 was cancelled but no replacement was queued.",
        metadata={
            "cancelled_at": "2026-05-06T16:45:00+00:00",
            "from": "in_progress",
            "actor": "worker",
        },
        tier=TIER_2,
    )

    brief = format_unstick_brief(finding)

    assert "Finding: cancellation_no_promotion" in brief
    assert "in_progress -> cancelled" in brief
    assert "No later task.created" in brief
    assert "queue replacement work" in brief
    assert "intentionally park" not in brief
    assert "Do not reply with analysis alone" in brief


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
    # Older orphan marker event (past the 30 min threshold).
    old_ts = now - timedelta(minutes=40)
    # Use the writer with a manual ts bypass — emit() stamps "now",
    # so we shape the event by writing JSON directly to the central
    # tail. This mirrors what an in-process producer would have
    # written 40 minutes ago.
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
        "ts": (now - timedelta(minutes=40)).isoformat(),
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
    """A review-state task with no reviewer-<project>-<N> window fires.

    #1737 — reviewers moved from per-project (``reviewer-<project>``) to
    per-task ephemeral (``reviewer-<project>-<N>``). The detector now
    checks the task-scoped window so legacy per-project windows that
    were retired no longer mask a missing per-task reviewer.
    """
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
        # Even the legacy per-project ``reviewer-savethenovel`` is now
        # treated as a stranger window — the detector demands the
        # task-scoped form.
        storage_window_names=[
            "worker-savethenovel",
            "architect-savethenovel",
            "reviewer-savethenovel",
        ],
        project="savethenovel",
    )
    matched = [f for f in findings if f.rule == RULE_ROLE_SESSION_MISSING]
    assert len(matched) == 1
    f = matched[0]
    assert f.subject == "savethenovel/10"
    assert f.metadata["expected_window"] == "reviewer-savethenovel-10"
    assert f.metadata["role"] == "reviewer"
    assert f.metadata["task_id"] == "savethenovel/10"


def test_role_session_missing_silent_when_per_task_reviewer_present(
    now: datetime,
) -> None:
    """A review-state task whose per-task reviewer window IS in the
    storage closet must NOT fire ``role_session_missing`` (#1737)."""
    from pollypm.audit.watchdog import RULE_ROLE_SESSION_MISSING

    task = _FakeTask(
        project="samblog",
        task_number=26,
        work_status="review",
        roles={"reviewer": "claude:reviewer"},
    )
    findings = scan_events(
        [],
        now=now,
        open_tasks=[task],
        storage_window_names=["reviewer-samblog-26"],
        project="samblog",
    )
    assert not any(f.rule == RULE_ROLE_SESSION_MISSING for f in findings)


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


def test_format_unstick_brief_fallback_is_imperative() -> None:
    """Stuck-draft / orphan-marker / cancel_no_promotion findings route
    through ``_brief_fallback``. Issue #1974 (Mode B): the cross-project
    audit log showed architects ack'ing these briefs with text-only
    analysis ("X is already cancelled, skipping") and never executing
    the queue/cancel command, so the cascade never resolved.

    The template must be **imperative**:
    - contains the verb ``execute`` (the action lead-in)
    - contains both ``pm task queue`` and ``pm task cancel`` with the
      finding's subject substituted (so it's copy-pasteable)
    - forbids analysis-only replies (``Reply only AFTER`` clause), so
      the architect cannot satisfy the brief by replying with text.
    - drops the old advisory phrasings ("Your job: investigate", "rather
      than ratifying the recommendation") that read as optional.
    """
    from pollypm.audit.watchdog import (
        RULE_STUCK_DRAFT,
        format_unstick_brief,
    )

    finding = Finding(
        rule=RULE_STUCK_DRAFT,
        project="pollypm",
        subject="pollypm/29",
        message="Draft task pollypm/29 has sat unpromoted for >30 min.",
        recommendation=(
            "Promote with `pm task queue pollypm/29` or discard "
            "with `pm task cancel pollypm/29`."
        ),
        metadata={"detected_via": "state"},
    )
    brief = format_unstick_brief(finding)

    # Imperative shape required by #1974 (Lever 3).
    assert "ACTION REQUIRED" in brief
    assert "execute" in brief
    assert "pm task queue pollypm/29" in brief
    assert "pm task cancel pollypm/29" in brief
    assert "Reply only AFTER" in brief

    # Old advisory phrasings must be gone — they were the failure mode.
    assert "Your job: investigate" not in brief
    assert "ratifying" not in brief

    # Evidence section still leads (the finding's message/recommendation
    # are not deleted — only the trailing advisory block is replaced).
    evidence_idx = brief.index("Observed evidence:")
    action_idx = brief.index("ACTION REQUIRED")
    assert evidence_idx < action_idx


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


def test_cadence_handler_dispatches_cancellation_no_promotion(
    now: datetime, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancel-without-replacement is tier-2 self-heal via architect dispatch."""
    from pollypm.plugins_builtin.core_recurring.audit_watchdog import (
        _scan_one_project,
    )

    central = central_log_path("savethenovel")
    central.parent.mkdir(parents=True, exist_ok=True)
    central.write_text(
        json.dumps({
            "schema": 1,
            "ts": (now - timedelta(minutes=15)).isoformat(),
            "project": "savethenovel",
            "event": EVENT_TASK_STATUS_CHANGED,
            "subject": "savethenovel/1",
            "actor": "worker",
            "status": "ok",
            "metadata": {"from": "in_progress", "to": "cancelled"},
        }) + "\n",
        encoding="utf-8",
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

    assert counters["findings"] == 1
    assert counters["dispatches_sent"] == 1
    assert len(sent) == 1
    target, brief = sent[0]
    assert target == "pollypm-storage-closet:architect-savethenovel"
    assert RULE_CANCEL_NO_PROMOTION in brief
    assert "queue replacement work" in brief
    assert "intentionally park" not in brief

    rows = [
        json.loads(line)
        for line in central.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    dispatch_rows = [
        r for r in rows if r["event"] == "watchdog.escalation_dispatched"
    ]
    assert len(dispatch_rows) == 1
    assert dispatch_rows[0]["metadata"]["finding_type"] == RULE_CANCEL_NO_PROMOTION


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
        "ts": (now - timedelta(minutes=40)).isoformat(),
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


# Sqlite-bound test (lines 1229-1350) removed for Slice K (#1737):
# patched sqlite resolver / private connection raw SQL on the sqlite path.
# See pg-gap #1787 / #1788 for re-coverage tracking.

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


# Sqlite-bound test (lines 1720-1883) removed for Slice K (#1737):
# patched sqlite resolver / private connection raw SQL on the sqlite path.
# See pg-gap #1787 / #1788 for re-coverage tracking.

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


class _FakeContext:
    """Minimal stand-in for ``work.models.ContextEntry``."""

    def __init__(
        self,
        *,
        timestamp: datetime,
        entry_type: str = "note",
    ) -> None:
        self.timestamp = timestamp
        self.entry_type = entry_type


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
        context: list[_FakeContext] | None = None,
        assignee: str | None = None,
        current_node_id: str | None = None,
    ) -> None:
        self.project = project
        self.task_number = task_number
        self.work_status = self._Status(work_status)
        self.transitions = list(transitions or [])
        self.created_at = created_at
        self.updated_at = updated_at
        self.created_by = created_by
        self.title = title
        self.context = list(context or [])
        # Keep the role-session detector happy when reused.
        self.roles: dict[str, str] = {}
        self.assignee: str | None = assignee
        self.current_node_id = current_node_id


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


def test_task_rework_stale_state_based_fires_for_old_rework(
    now: datetime,
) -> None:
    task = _StatefulTask(
        project="savethenovel",
        task_number=16,
        work_status="rework",
        transitions=[
            _FakeTransition(
                from_state="review",
                to_state="rework",
                timestamp=now - timedelta(minutes=35),
                actor="reviewer",
                reason="tests still failing",
            ),
        ],
        updated_at=now - timedelta(minutes=35),
        assignee="worker",
        current_node_id="implement",
    )

    findings = scan_events([], now=now, open_tasks=[task])
    matched = [f for f in findings if f.rule == RULE_TASK_REWORK_STALE]

    assert len(matched) == 1
    assert matched[0].subject == "savethenovel/16"
    assert matched[0].tier == TIER_2
    assert matched[0].metadata["detected_via"] == "state"
    assert matched[0].metadata["reason"] == "tests still failing"


def test_task_rework_stale_event_path_silent_when_resumed(
    now: datetime,
) -> None:
    events = [
        _make_event(
            event=EVENT_TASK_STATUS_CHANGED,
            project="demo",
            subject="demo/16",
            metadata={"from": "review", "to": "rework"},
            ts=now - timedelta(minutes=35),
        ),
        _make_event(
            event=EVENT_TASK_STATUS_CHANGED,
            project="demo",
            subject="demo/16",
            metadata={"from": "rework", "to": "queued"},
            ts=now - timedelta(minutes=5),
        ),
    ]

    findings = scan_events(events, now=now)

    assert not any(f.rule == RULE_TASK_REWORK_STALE for f in findings)


def test_task_progress_stale_state_based_fires_for_old_in_progress(
    now: datetime,
) -> None:
    """#1444 — an old in_progress claim with no activity escalates."""
    task = _StatefulTask(
        project="savethenovel",
        task_number=15,
        work_status="in_progress",
        transitions=[
            _FakeTransition(
                from_state="queued",
                to_state="in_progress",
                timestamp=now - timedelta(minutes=25),
                actor="worker",
            ),
        ],
        updated_at=now - timedelta(minutes=25),
        assignee="claude:worker",
        current_node_id="implement",
    )
    findings = scan_events([], now=now, open_tasks=[task])
    matched = [f for f in findings if f.rule == RULE_TASK_PROGRESS_STALE]
    assert len(matched) == 1
    f = matched[0]
    assert f.subject == "savethenovel/15"
    assert f.metadata["detected_via"] == "state"
    assert f.metadata["last_activity_kind"] == "state_entry"
    assert f.metadata["stuck_minutes"] >= 20
    assert "auth" in f.recommendation


def test_task_progress_stale_state_based_silent_when_recent_context(
    now: datetime,
) -> None:
    """A recent task-context progress note keeps an old claim fresh."""
    task = _StatefulTask(
        project="demo",
        task_number=7,
        work_status="in_progress",
        transitions=[
            _FakeTransition(
                from_state="queued",
                to_state="in_progress",
                timestamp=now - timedelta(minutes=45),
            ),
        ],
        updated_at=now - timedelta(minutes=45),
        context=[
            _FakeContext(
                timestamp=now - timedelta(minutes=5),
                entry_type="note",
            ),
        ],
    )
    findings = scan_events([], now=now, open_tasks=[task])
    assert not any(f.rule == RULE_TASK_PROGRESS_STALE for f in findings)


def test_task_progress_stale_state_path_dedupes_event_fallback_with_context(
    now: datetime,
) -> None:
    """Recent task context suppresses stale event fallback for the same task."""
    task = _StatefulTask(
        project="demo",
        task_number=7,
        work_status="in_progress",
        transitions=[
            _FakeTransition(
                from_state="queued",
                to_state="in_progress",
                timestamp=now - timedelta(minutes=45),
            ),
        ],
        updated_at=now - timedelta(minutes=45),
        context=[
            _FakeContext(
                timestamp=now - timedelta(minutes=5),
                entry_type="note",
            ),
        ],
    )
    events = [
        _make_event(
            event=EVENT_TASK_STATUS_CHANGED,
            project="demo",
            subject="demo/7",
            metadata={"from": "queued", "to": "in_progress"},
            ts=now - timedelta(minutes=45),
        ),
    ]
    findings = scan_events(events, now=now, open_tasks=[task])
    assert not any(f.rule == RULE_TASK_PROGRESS_STALE for f in findings)


def test_task_progress_stale_event_path_silent_with_recent_worker_heartbeat(
    now: datetime,
) -> None:
    """A worker.heartbeat after the in_progress transition suppresses the finding."""
    events = [
        _make_event(
            event=EVENT_TASK_STATUS_CHANGED,
            project="demo",
            subject="demo/8",
            metadata={"from": "queued", "to": "in_progress"},
            ts=now - timedelta(minutes=45),
        ),
        _make_event(
            event="worker.heartbeat",
            project="demo",
            subject="demo/8",
            metadata={"task_id": "demo/8"},
            ts=now - timedelta(minutes=5),
        ),
    ]
    findings = scan_events(events, now=now)
    assert not any(f.rule == RULE_TASK_PROGRESS_STALE for f in findings)


def test_task_progress_stale_event_path_fires_without_heartbeat(
    now: datetime,
) -> None:
    events = [
        _make_event(
            event=EVENT_TASK_STATUS_CHANGED,
            project="demo",
            subject="demo/8",
            metadata={"from": "queued", "to": "in_progress"},
            ts=now - timedelta(minutes=45),
        ),
    ]
    findings = scan_events(events, now=now)
    matched = [f for f in findings if f.rule == RULE_TASK_PROGRESS_STALE]
    assert len(matched) == 1
    assert matched[0].subject == "demo/8"
    assert matched[0].metadata["detected_via"] == "event"


def test_format_unstick_brief_progress_stale_mentions_auth() -> None:
    from pollypm.audit.watchdog import format_unstick_brief

    finding = Finding(
        rule=RULE_TASK_PROGRESS_STALE,
        project="savethenovel",
        subject="savethenovel/15",
        message="Task savethenovel/15 has been in_progress with no activity.",
        metadata={
            "stuck_minutes": 25,
            "in_progress_minutes": 25,
            "in_progress_since": "2026-05-07T20:03:09+00:00",
            "last_activity_at": "2026-05-07T20:03:09+00:00",
            "last_activity_kind": "state_entry",
            "assignee": "claude:worker",
            "current_node_id": "implement",
        },
    )
    brief = format_unstick_brief(finding)
    assert "task_progress_stale" in brief
    assert "auth" in brief.lower()
    assert "sandbox" in brief.lower()
    assert "pm task cancel savethenovel/15" in brief


def test_cadence_handler_dispatches_task_progress_stale(
    now: datetime, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The new in_progress detector is part of the architect dispatch set."""
    from pollypm.plugins_builtin.core_recurring.audit_watchdog import (
        _scan_one_project,
    )

    task = _StatefulTask(
        project="savethenovel",
        task_number=15,
        work_status="in_progress",
        transitions=[
            _FakeTransition(
                from_state="queued",
                to_state="in_progress",
                timestamp=now - timedelta(minutes=25),
                actor="worker",
            ),
        ],
        updated_at=now - timedelta(minutes=25),
        assignee="claude:worker",
        current_node_id="implement",
    )
    monkeypatch.setattr(
        "pollypm.plugins_builtin.core_recurring.audit_watchdog._gather_open_tasks",
        lambda key, path: [task],
    )
    monkeypatch.setattr(
        "pollypm.plugins_builtin.core_recurring.audit_watchdog._gather_storage_windows",
        lambda name: ["worker-savethenovel", "architect-savethenovel"],
    )
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "pollypm.plugins_builtin.core_recurring.audit_watchdog._send_brief_to_architect",
        lambda target, brief: sent.append((target, brief)) or True,
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
    assert len(sent) == 1
    target, brief = sent[0]
    assert target == "pollypm-storage-closet:architect-savethenovel"
    assert RULE_TASK_PROGRESS_STALE in brief
    assert "savethenovel/15" in brief


def test_state_based_detectors_silent_when_open_tasks_empty(now: datetime) -> None:
    """No open_tasks → state path no-ops, original event path still works."""
    from pollypm.audit.watchdog import (
        RULE_TASK_PROGRESS_STALE,
        RULE_TASK_ON_HOLD_STALE,
        RULE_TASK_REVIEW_STALE,
    )

    findings = scan_events([], now=now, open_tasks=[])
    # No events, no tasks → nothing should fire.
    assert not any(
        f.rule in (
            RULE_TASK_REVIEW_STALE,
            RULE_TASK_ON_HOLD_STALE,
            RULE_TASK_PROGRESS_STALE,
            RULE_STUCK_DRAFT,
        )
        for f in findings
    )


# ---------------------------------------------------------------------------
# Rule (#1510): duplicate advisor tasks
# ---------------------------------------------------------------------------


class _AdvisorTask:
    """Stand-in for ``work.models.Task`` carrying the fields the
    duplicate-advisor detector reads."""

    class _Status:
        def __init__(self, value: str) -> None:
            self.value = value

    def __init__(
        self,
        *,
        project: str,
        task_number: int,
        work_status: str = "queued",
        labels: list[str] | None = None,
        created_at: datetime | None = None,
    ) -> None:
        self.project = project
        self.task_number = task_number
        self.work_status = self._Status(work_status)
        self.labels = list(labels or ["advisor"])
        self.created_at = created_at


def test_duplicate_advisor_tasks_detected_and_keep_newest(now: datetime) -> None:
    """5 non-terminal advisor tasks → finding cancels 4, keeps newest."""
    from pollypm.audit.watchdog import RULE_DUPLICATE_ADVISOR_TASKS

    tasks = [
        _AdvisorTask(
            project="coffeeboardnm",
            task_number=n,
            work_status="queued",
            labels=["advisor"],
            created_at=now - timedelta(minutes=60 - n * 5),
        )
        for n in (16, 17, 18, 24, 27)
    ]
    findings = scan_events([], now=now, open_tasks=tasks)
    matched = [f for f in findings if f.rule == RULE_DUPLICATE_ADVISOR_TASKS]
    assert len(matched) == 1
    f = matched[0]
    assert f.project == "coffeeboardnm"
    # Newest is task_number=27 (created at now-25 min, latest of the bunch).
    assert f.subject == "coffeeboardnm/27"
    assert f.metadata["keep_task_id"] == "coffeeboardnm/27"
    assert sorted(f.metadata["duplicate_ids"]) == [
        "coffeeboardnm/16",
        "coffeeboardnm/17",
        "coffeeboardnm/18",
        "coffeeboardnm/24",
    ]
    assert f.metadata["total_count"] == 5


def test_duplicate_advisor_tasks_silent_with_one_task(now: datetime) -> None:
    """A single advisor task is the steady state — must not fire."""
    from pollypm.audit.watchdog import RULE_DUPLICATE_ADVISOR_TASKS

    tasks = [
        _AdvisorTask(
            project="coffeeboardnm",
            task_number=27,
            work_status="queued",
            created_at=now - timedelta(minutes=10),
        )
    ]
    findings = scan_events([], now=now, open_tasks=tasks)
    assert not any(f.rule == RULE_DUPLICATE_ADVISOR_TASKS for f in findings)


def test_duplicate_advisor_tasks_ignores_non_advisor_labels(now: datetime) -> None:
    """Non-advisor tasks with the same project must not be folded in."""
    from pollypm.audit.watchdog import RULE_DUPLICATE_ADVISOR_TASKS

    tasks = [
        _AdvisorTask(
            project="proj",
            task_number=1,
            work_status="queued",
            labels=["advisor"],
            created_at=now - timedelta(minutes=10),
        ),
        _AdvisorTask(
            project="proj",
            task_number=2,
            work_status="queued",
            labels=["feature"],
            created_at=now - timedelta(minutes=5),
        ),
    ]
    findings = scan_events([], now=now, open_tasks=tasks)
    assert not any(f.rule == RULE_DUPLICATE_ADVISOR_TASKS for f in findings)


def test_duplicate_advisor_tasks_ignores_terminal_states(now: datetime) -> None:
    """Cancelled / done duplicates are not counted."""
    from pollypm.audit.watchdog import RULE_DUPLICATE_ADVISOR_TASKS

    tasks = [
        _AdvisorTask(
            project="proj",
            task_number=1,
            work_status="cancelled",
            created_at=now - timedelta(minutes=30),
        ),
        _AdvisorTask(
            project="proj",
            task_number=2,
            work_status="queued",
            created_at=now - timedelta(minutes=5),
        ),
    ]
    findings = scan_events([], now=now, open_tasks=tasks)
    assert not any(f.rule == RULE_DUPLICATE_ADVISOR_TASKS for f in findings)


# Sqlite-bound test (lines 2696-2784) removed for Slice K (#1737):
# patched sqlite resolver / private connection raw SQL on the sqlite path.
# See pg-gap #1787 / #1788 for re-coverage tracking.

# ---------------------------------------------------------------------------
# Rule (#1519): legacy DB shadows canonical
# ---------------------------------------------------------------------------


class _Shadow:
    """Stand-in for the cadence handler's ``_LegacyDbShadow`` dataclass.

    The detector duck-types every attribute via ``getattr`` so any
    object with the right field set works. Tests prefer this lightweight
    shape over importing the cadence-handler dataclass to keep the unit
    tests pure.
    """

    def __init__(
        self,
        *,
        project_key: str,
        canonical_db: Path,
        legacy_db: Path,
        canonical_row_count: int,
        legacy_row_count: int,
        project_path: Path | None = None,
    ) -> None:
        self.project_key = project_key
        self.project_path = project_path or Path("/tmp/proj")
        self.canonical_db = canonical_db
        self.legacy_db = legacy_db
        self.canonical_row_count = canonical_row_count
        self.legacy_row_count = legacy_row_count


def test_legacy_db_shadow_fires_when_both_dbs_have_rows(now: datetime) -> None:
    """#1519 — finding fires when canonical AND legacy both carry rows."""
    from pollypm.audit.watchdog import RULE_LEGACY_DB_SHADOW

    shadows = [_Shadow(
        project_key="coffeeboardnm",
        canonical_db=Path("/dev/.pollypm/state.db"),
        legacy_db=Path("/dev/coffeeboardnm/.pollypm/state.db"),
        canonical_row_count=1,
        legacy_row_count=5,
    )]
    findings = scan_events([], now=now, legacy_db_shadows=shadows)
    matched = [f for f in findings if f.rule == RULE_LEGACY_DB_SHADOW]
    assert len(matched) == 1
    f = matched[0]
    assert f.project == "coffeeboardnm"
    assert f.subject == "/dev/coffeeboardnm/.pollypm/state.db"
    assert f.metadata["canonical_row_count"] == 1
    assert f.metadata["legacy_row_count"] == 5
    assert f.metadata["canonical_db"] == "/dev/.pollypm/state.db"
    assert f.metadata["legacy_db"] == "/dev/coffeeboardnm/.pollypm/state.db"


def test_legacy_db_shadow_silent_when_canonical_empty(now: datetime) -> None:
    """Pure-legacy projects don't fire — Part A's read fallback already
    routes them correctly. Migrating proactively would be a behaviour
    change orthogonal to the bug; this rule is scoped to actual
    divergence (rows on both sides).
    """
    from pollypm.audit.watchdog import RULE_LEGACY_DB_SHADOW

    shadows = [_Shadow(
        project_key="demo",
        canonical_db=Path("/dev/.pollypm/state.db"),
        legacy_db=Path("/dev/demo/.pollypm/state.db"),
        canonical_row_count=0,
        legacy_row_count=5,
    )]
    findings = scan_events([], now=now, legacy_db_shadows=shadows)
    assert not any(f.rule == RULE_LEGACY_DB_SHADOW for f in findings)


def test_legacy_db_shadow_silent_when_legacy_empty(now: datetime) -> None:
    """Legacy file with no rows for the project is not a shadow."""
    from pollypm.audit.watchdog import RULE_LEGACY_DB_SHADOW

    shadows = [_Shadow(
        project_key="demo",
        canonical_db=Path("/dev/.pollypm/state.db"),
        legacy_db=Path("/dev/demo/.pollypm/state.db"),
        canonical_row_count=3,
        legacy_row_count=0,
    )]
    findings = scan_events([], now=now, legacy_db_shadows=shadows)
    assert not any(f.rule == RULE_LEGACY_DB_SHADOW for f in findings)


def test_legacy_db_shadow_noop_when_input_none(now: datetime) -> None:
    """``legacy_db_shadows=None`` (the default) disables the rule."""
    from pollypm.audit.watchdog import RULE_LEGACY_DB_SHADOW

    findings = scan_events([], now=now)
    assert not any(f.rule == RULE_LEGACY_DB_SHADOW for f in findings)


def test_legacy_db_shadow_self_heal_drains_legacy_db(tmp_path: Path) -> None:
    """#1519 — cadence self-heal calls ``migrate_one`` and archives the
    legacy DB. After the heal, the legacy file has been renamed to
    ``state.db.legacy-1004`` and the canonical DB carries the merged
    rows. A re-run is a no-op (the source is gone).
    """
    import sqlite3
    from pollypm.audit.watchdog import (
        Finding,
        RULE_LEGACY_DB_SHADOW,
    )
    from pollypm.plugins_builtin.core_recurring.audit_watchdog import (
        _self_heal_legacy_db_shadow,
    )

    workspace_root = tmp_path / "dev"
    workspace_root.mkdir()
    project_path = workspace_root / "demo"
    project_path.mkdir()
    canonical_db = workspace_root / ".pollypm" / "state.db"
    legacy_db = project_path / ".pollypm" / "state.db"
    canonical_db.parent.mkdir(parents=True, exist_ok=True)
    legacy_db.parent.mkdir(parents=True, exist_ok=True)

    # Minimal schema that matches what migrate_one looks for.
    for db in (canonical_db, legacy_db):
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE work_tasks ("
            "project TEXT NOT NULL, task_number INTEGER NOT NULL, "
            "title TEXT NOT NULL DEFAULT '', work_status TEXT NOT NULL DEFAULT 'queued', "
            "PRIMARY KEY (project, task_number))"
        )
        conn.commit()
        conn.close()

    # Canonical: 1 row. Legacy: 5 rows under different task_numbers so
    # the migration copies them in.
    canon = sqlite3.connect(canonical_db)
    canon.execute(
        "INSERT INTO work_tasks (project, task_number, title) VALUES (?, ?, ?)",
        ("demo", 100, "canonical row"),
    )
    canon.commit()
    canon.close()

    legacy = sqlite3.connect(legacy_db)
    for n in range(5):
        legacy.execute(
            "INSERT INTO work_tasks (project, task_number, title) VALUES (?, ?, ?)",
            ("demo", 200 + n, f"legacy stale row {n}"),
        )
    legacy.commit()
    legacy.close()

    finding = Finding(
        rule=RULE_LEGACY_DB_SHADOW,
        project="demo",
        subject=str(legacy_db),
        message="legacy shadows canonical",
        recommendation="auto-migrate",
        metadata={
            "project_key": "demo",
            "canonical_db": str(canonical_db),
            "legacy_db": str(legacy_db),
            "canonical_row_count": 1,
            "legacy_row_count": 5,
        },
    )

    counters = _self_heal_legacy_db_shadow(
        finding,
        project_key="demo",
        project_path=project_path,
    )
    assert counters == {"migrated": 1, "failed": 0}

    # Legacy file has been archived (migrate_one renames after a clean
    # copy). The original path no longer exists.
    assert not legacy_db.exists()
    archived = legacy_db.with_suffix(legacy_db.suffix + ".legacy-1004")
    assert archived.exists()

    # Canonical now carries 1 + 5 = 6 demo rows.
    with sqlite3.connect(canonical_db) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM work_tasks WHERE project = 'demo'"
        ).fetchone()[0]
    assert count == 6

    # Idempotency — running again is a clean no-op (source gone).
    counters_redux = _self_heal_legacy_db_shadow(
        finding,
        project_key="demo",
        project_path=project_path,
    )
    # ``migrate_one`` returns ``skipped_reason='no_per_project_db'`` — that
    # counts as success for our purposes (nothing to do, no errors).
    assert counters_redux == {"migrated": 1, "failed": 0}


def test_legacy_db_shadow_gather_skips_workspace_self(tmp_path: Path) -> None:
    """``_gather_legacy_db_shadows`` must skip a project whose ``path``
    *is* the workspace root — its "legacy" file IS the canonical one.
    """
    from types import SimpleNamespace
    from pollypm.plugins_builtin.core_recurring.audit_watchdog import (
        _gather_legacy_db_shadows,
    )

    workspace_root = tmp_path / "dev"
    workspace_root.mkdir()
    canonical_db = workspace_root / ".pollypm" / "state.db"
    canonical_db.parent.mkdir(parents=True, exist_ok=True)
    canonical_db.touch()

    services = SimpleNamespace(
        config=SimpleNamespace(
            project=SimpleNamespace(workspace_root=str(workspace_root)),
        ),
        known_projects=(
            SimpleNamespace(key="ws", path=workspace_root),
        ),
    )

    shadows = _gather_legacy_db_shadows(services=services)
    # Workspace-root project is filtered out — no shadow to migrate.
    assert shadows == []


def test_legacy_db_shadow_gather_finds_shadowed_project(tmp_path: Path) -> None:
    """``_gather_legacy_db_shadows`` must return a descriptor for a
    project whose canonical AND legacy DBs both carry rows.
    """
    import sqlite3
    from types import SimpleNamespace
    from pollypm.plugins_builtin.core_recurring.audit_watchdog import (
        _gather_legacy_db_shadows,
    )

    workspace_root = tmp_path / "dev"
    workspace_root.mkdir()
    project_path = workspace_root / "demo"
    project_path.mkdir()
    canonical_db = workspace_root / ".pollypm" / "state.db"
    legacy_db = project_path / ".pollypm" / "state.db"
    canonical_db.parent.mkdir(parents=True, exist_ok=True)
    legacy_db.parent.mkdir(parents=True, exist_ok=True)

    for db, count in ((canonical_db, 2), (legacy_db, 5)):
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE work_tasks ("
            "project TEXT NOT NULL, task_number INTEGER NOT NULL, "
            "PRIMARY KEY (project, task_number))"
        )
        for n in range(count):
            conn.execute(
                "INSERT INTO work_tasks (project, task_number) VALUES (?, ?)",
                ("demo", (1000 if db is canonical_db else 2000) + n),
            )
        conn.commit()
        conn.close()

    services = SimpleNamespace(
        config=SimpleNamespace(
            project=SimpleNamespace(workspace_root=str(workspace_root)),
        ),
        known_projects=(
            SimpleNamespace(key="demo", path=project_path),
        ),
    )

    shadows = _gather_legacy_db_shadows(services=services)
    assert len(shadows) == 1
    s = shadows[0]
    assert s.project_key == "demo"
    assert s.canonical_row_count == 2
    assert s.legacy_row_count == 5
    assert s.canonical_db == canonical_db
    assert s.legacy_db == legacy_db


# ---------------------------------------------------------------------------
# Rule (#1524): plan_missing alert churn
# ---------------------------------------------------------------------------


class _PlanMissingClearStub:
    """Stand-in for the cadence handler's ``_PlanMissingClear`` dataclass.

    Mirrors the duck-typed contract the detector reads: ``scope`` (the
    synthetic ``plan_gate-<project>`` alert scope) + ``cleared_at``
    (tz-aware datetime).
    """

    def __init__(self, *, scope: str, cleared_at: datetime) -> None:
        self.scope = scope
        self.cleared_at = cleared_at


def test_plan_missing_alert_churn_fires_when_threshold_exceeded(
    now: datetime,
) -> None:
    """#1524 — N+1 clears in the window → finding fires for that project."""
    from pollypm.audit.watchdog import RULE_PLAN_MISSING_ALERT_CHURN

    config = WatchdogConfig()
    # Threshold defaults to 3; seed 4 clears in the last 5 minutes for
    # ``coffeeboardnm`` and 1 clear (not enough) for ``savethenovel``.
    clears = [
        _PlanMissingClearStub(
            scope="plan_gate-coffeeboardnm",
            cleared_at=now - timedelta(minutes=4),
        ),
        _PlanMissingClearStub(
            scope="plan_gate-coffeeboardnm",
            cleared_at=now - timedelta(minutes=3),
        ),
        _PlanMissingClearStub(
            scope="plan_gate-coffeeboardnm",
            cleared_at=now - timedelta(minutes=2),
        ),
        _PlanMissingClearStub(
            scope="plan_gate-coffeeboardnm",
            cleared_at=now - timedelta(minutes=1),
        ),
        _PlanMissingClearStub(
            scope="plan_gate-savethenovel",
            cleared_at=now - timedelta(minutes=1),
        ),
    ]
    findings = scan_events(
        [], now=now, config=config, plan_missing_clears=clears,
    )
    matched = [f for f in findings if f.rule == RULE_PLAN_MISSING_ALERT_CHURN]
    assert len(matched) == 1
    f = matched[0]
    assert f.project == "coffeeboardnm"
    assert f.subject == "plan_gate-coffeeboardnm"
    assert f.metadata["clear_count"] == 4
    assert f.metadata["project_key"] == "coffeeboardnm"
    # Recommendation calls out the producer to inspect.
    assert "_precompute_plan_missing_projects" in f.recommendation


def test_plan_missing_alert_churn_silent_below_threshold(
    now: datetime,
) -> None:
    """Two clears in the window — below the default threshold of 3 — no finding."""
    from pollypm.audit.watchdog import RULE_PLAN_MISSING_ALERT_CHURN

    config = WatchdogConfig()
    clears = [
        _PlanMissingClearStub(
            scope="plan_gate-demo",
            cleared_at=now - timedelta(minutes=2),
        ),
        _PlanMissingClearStub(
            scope="plan_gate-demo",
            cleared_at=now - timedelta(minutes=1),
        ),
    ]
    findings = scan_events(
        [], now=now, config=config, plan_missing_clears=clears,
    )
    assert not any(
        f.rule == RULE_PLAN_MISSING_ALERT_CHURN for f in findings
    )


def test_plan_missing_alert_churn_drops_clears_outside_window(
    now: datetime,
) -> None:
    """Clears older than the window don't count toward the threshold.

    Three clears total but only one inside the 10-minute window — no finding.
    """
    from pollypm.audit.watchdog import RULE_PLAN_MISSING_ALERT_CHURN

    config = WatchdogConfig()
    clears = [
        _PlanMissingClearStub(
            scope="plan_gate-demo",
            cleared_at=now - timedelta(minutes=30),  # outside
        ),
        _PlanMissingClearStub(
            scope="plan_gate-demo",
            cleared_at=now - timedelta(minutes=20),  # outside
        ),
        _PlanMissingClearStub(
            scope="plan_gate-demo",
            cleared_at=now - timedelta(minutes=2),  # inside
        ),
    ]
    findings = scan_events(
        [], now=now, config=config, plan_missing_clears=clears,
    )
    assert not any(
        f.rule == RULE_PLAN_MISSING_ALERT_CHURN for f in findings
    )


def test_plan_missing_alert_churn_noop_when_input_none(
    now: datetime,
) -> None:
    """``plan_missing_clears=None`` (the default) disables the rule."""
    from pollypm.audit.watchdog import RULE_PLAN_MISSING_ALERT_CHURN

    findings = scan_events([], now=now)
    assert not any(
        f.rule == RULE_PLAN_MISSING_ALERT_CHURN for f in findings
    )


def test_plan_missing_alert_churn_skips_non_plan_gate_scopes(
    now: datetime,
) -> None:
    """Clear records whose scope isn't ``plan_gate-<project>`` are ignored.

    A clear whose scope happens to be ``worker-foo`` (the no_session
    family lives there) is unrelated to plan-missing churn even if it
    somehow surfaced through the gather; the regex scope filter drops it.
    """
    from pollypm.audit.watchdog import RULE_PLAN_MISSING_ALERT_CHURN

    config = WatchdogConfig()
    clears = [
        _PlanMissingClearStub(
            scope="worker-demo",
            cleared_at=now - timedelta(minutes=1),
        ),
        _PlanMissingClearStub(
            scope="worker-demo",
            cleared_at=now - timedelta(minutes=2),
        ),
        _PlanMissingClearStub(
            scope="worker-demo",
            cleared_at=now - timedelta(minutes=3),
        ),
        _PlanMissingClearStub(
            scope="worker-demo",
            cleared_at=now - timedelta(minutes=4),
        ),
    ]
    findings = scan_events(
        [], now=now, config=config, plan_missing_clears=clears,
    )
    assert not any(
        f.rule == RULE_PLAN_MISSING_ALERT_CHURN for f in findings
    )


# ---------------------------------------------------------------------------
# #1884 — worker-cap back-pressure probe threads config to work service
# ---------------------------------------------------------------------------


def test_gather_worker_cap_back_pressure_threads_config_to_factory(
    tmp_path: Path, monkeypatch,
) -> None:
    """#1884 — the probe must pass ``config=`` to ``create_work_service``.

    Pre-fix the probe called ``create_work_service(project_path=...)``
    without forwarding the loaded config. On a pg workspace the
    factory then fell back to the sqlite branch, queried an empty
    sidecar DB, and produced phantom back-pressure findings (or
    quietly suppressed legitimate ``role_session_missing`` alerts).
    """
    from pollypm.plugins_builtin.core_recurring import audit_watchdog as aw

    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        """
[project]
name = "PollyPM"
tmux_session = "pollypm"

[pollypm]
controller_account = "claude_primary"

[accounts.claude_primary]
provider = "claude"
home = ".pollypm/homes/claude_primary"

[sessions.heartbeat]
role = "heartbeat-supervisor"
provider = "claude"
account = "claude_primary"
cwd = "."

[sessions.operator]
role = "operator-pm"
provider = "claude"
account = "claude_primary"
cwd = "."

[projects.demo]
path = "demo"
max_parallel_workers = 2
"""
    )
    project_path = tmp_path / "demo"
    project_path.mkdir()

    captured: dict[str, object] = {}

    class _Stub:
        def list_worker_sessions(self, *, project=None, active_only=True):
            return []

        def close(self) -> None:
            pass

    def _fake_factory(**kwargs):
        captured.update(kwargs)
        return _Stub()

    import pollypm.work as work_mod
    monkeypatch.setattr(work_mod, "create_work_service", _fake_factory)

    aw._gather_worker_cap_back_pressure(
        "demo", project_path, config_path,
    )

    assert "config" in captured, captured
    assert captured["config"] is not None
    assert captured.get("project_key") == "demo"
    assert captured.get("project_path") == project_path


# ---------------------------------------------------------------------------
# #1887 — reviewer role-session self-heal threads config to factory
# ---------------------------------------------------------------------------


def test_self_heal_reviewer_spawn_threads_config_to_factory(
    tmp_path: Path, monkeypatch,
) -> None:
    """#1887 — the reviewer self-heal branch must forward ``config=``.

    Pre-fix the branch called ``create_work_service(project_path=...,
    project_key=...)`` without ``config=cfg``. On a pg workspace the
    factory then opened a sqlite sidecar DB, the reviewer
    existing-window lookup queried an empty table, and the self-heal
    either silently no-oped or duplicated the spawn next tick.
    """
    from pollypm.audit.watchdog import Finding
    from pollypm.plugins_builtin.core_recurring import audit_watchdog as aw

    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        """
[project]
name = "PollyPM"
tmux_session = "pollypm"

[pollypm]
controller_account = "claude_primary"

[accounts.claude_primary]
provider = "claude"
home = ".pollypm/homes/claude_primary"

[sessions.heartbeat]
role = "heartbeat-supervisor"
provider = "claude"
account = "claude_primary"
cwd = "."

[sessions.operator]
role = "operator-pm"
provider = "claude"
account = "claude_primary"
cwd = "."

[projects.demo]
path = "demo"
"""
    )
    project_path = tmp_path / "demo"
    project_path.mkdir()

    captured: dict[str, object] = {}

    class _Stub:
        def ensure_worker_session_schema(self):
            pass

        def list_worker_sessions(self, *, project=None, active_only=True):
            return []

        def close(self) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_factory(**kwargs):
        captured.update(kwargs)
        return _Stub()

    import pollypm.work as work_mod
    monkeypatch.setattr(work_mod, "create_work_service", _fake_factory)

    class _StubMgr:
        def __init__(self, *a, **kw):
            pass

        def provision_reviewer(self, task_id):
            return "reviewer-demo-4"

    import pollypm.work.session_manager as sm_mod
    monkeypatch.setattr(sm_mod, "SessionManager", _StubMgr)

    # Stub the TmuxClient so a missing tmux binary doesn't blow up.
    class _StubTmux:
        pass

    import pollypm.tmux.client as _tmux_mod
    monkeypatch.setattr(_tmux_mod, "TmuxClient", _StubTmux)

    finding = Finding(
        rule="role_session_missing",
        project="demo",
        subject="demo/4",
        severity="warn",
        message="reviewer for demo/4 missing",
        recommendation="spawn reviewer",
        metadata={"role": "reviewer", "task_id": "demo/4"},
    )

    counters = aw._self_heal_role_session_missing(
        finding,
        project_key="demo",
        project_path=project_path,
        config_path=config_path,
    )

    assert "config" in captured, captured
    assert captured["config"] is not None
    # The self-heal succeeded -> reviewer-spawn counter incremented.
    assert counters["worker_lane_spawned"] == 1


# ---------------------------------------------------------------------------
# #2015 — dispatch_dedup_hash + root-cause-keyed throttle
# ---------------------------------------------------------------------------


def test_dispatch_dedup_hash_collapses_across_sibling_subjects() -> None:
    """Same finding-body across different subjects → same dedup hash.

    The samblog/32-35 case: the watchdog emits one ``stuck_draft``
    finding per draft task, so subjects differ (samblog/32, /33, /34,
    /35) but ``rule`` / ``project`` / ``evidence`` are identical. The
    subject-independent hash must be equal across the four findings.
    """
    from pollypm.audit.watchdog import dispatch_dedup_hash

    common_evidence = {
        "queued_subjects": ["samblog/21", "samblog/26", "samblog/29",
                            "samblog/31"],
        "queued_last_updated": "2026-05-19T13:30:00+00:00",
    }
    finding_template = dict(
        rule=RULE_STUCK_DRAFT,
        project="samblog",
        evidence=common_evidence,
    )
    hashes = {
        dispatch_dedup_hash(Finding(subject=f"samblog/{n}", **finding_template))
        for n in (32, 33, 34, 35)
    }
    assert len(hashes) == 1


def test_dispatch_dedup_hash_distinguishes_different_evidence() -> None:
    """Different finding-bodies (different evidence) → different hashes."""
    from pollypm.audit.watchdog import dispatch_dedup_hash

    a = Finding(
        rule=RULE_STUCK_DRAFT, project="samblog", subject="samblog/32",
        evidence={"queued_subjects": ["samblog/21"]},
    )
    b = Finding(
        rule=RULE_STUCK_DRAFT, project="samblog", subject="samblog/32",
        evidence={"queued_subjects": ["samblog/99"]},
    )
    assert dispatch_dedup_hash(a) != dispatch_dedup_hash(b)


def test_dispatch_dedup_hash_distinguishes_different_rules() -> None:
    from pollypm.audit.watchdog import dispatch_dedup_hash

    a = Finding(
        rule=RULE_STUCK_DRAFT, project="samblog", subject="samblog/32",
        evidence={"x": 1},
    )
    b = Finding(
        rule=RULE_TASK_PROGRESS_STALE, project="samblog",
        subject="samblog/32", evidence={"x": 1},
    )
    assert dispatch_dedup_hash(a) != dispatch_dedup_hash(b)


def test_was_recently_dispatched_dedupes_by_root_cause_hash(
    now: datetime,
) -> None:
    """#2015 regression — samblog/32-35 burst collapses to one dispatch.

    Pre-fix: ``was_recently_dispatched`` matched on
    ``(project, finding_type, subject)``. Four draft tasks with the
    same root cause all had different subjects, so the throttle never
    fired across them and the operator got four inbox entries asking
    the same question.

    Post-fix: passing the same ``dedup_hash`` across sibling subjects
    matches the seeded event, so the second/third/fourth would-be
    dispatch returns ``True`` (throttled) even though the subjects
    differ from the first.
    """
    from pollypm.audit.watchdog import (
        dispatch_dedup_hash,
        emit_escalation_dispatched,
        was_recently_dispatched,
    )

    common_evidence = {
        "queued_subjects": ["samblog/21", "samblog/26", "samblog/29",
                            "samblog/31"],
    }
    findings = [
        Finding(
            rule=RULE_STUCK_DRAFT,
            project="samblog",
            subject=f"samblog/{n}",
            evidence=common_evidence,
        )
        for n in (32, 33, 34, 35)
    ]
    # All four share the same finding-body hash.
    shared_hash = dispatch_dedup_hash(findings[0])
    assert all(dispatch_dedup_hash(f) == shared_hash for f in findings)

    # Seed only the first dispatch (samblog/32).
    emit_escalation_dispatched(
        project="samblog",
        finding_type=RULE_STUCK_DRAFT,
        subject="samblog/32",
        brief="...",
        dedup_hash=shared_hash,
    )

    # The other three siblings must see themselves as throttled even
    # though their subjects differ from the seeded one. Pre-fix this
    # assertion failed for /33, /34, /35 — each presented a unique
    # subject so the legacy subject-keyed throttle let them through.
    for f in findings:
        assert was_recently_dispatched(
            project=f.project,
            finding_type=f.rule,
            subject=f.subject,
            now=now + timedelta(minutes=5),
            dedup_hash=shared_hash,
        ), f"throttle leaked for {f.subject}"


def test_was_recently_dispatched_legacy_rows_fall_back_to_subject(
    now: datetime,
) -> None:
    """Pre-#2015 rows have no ``dedup_hash`` in metadata. The throttle
    must still honour them via the subject-match fallback so old
    throttle windows don't evaporate at the moment of the rollout.
    """
    from pollypm.audit.log import (
        EVENT_WATCHDOG_ESCALATION_DISPATCHED,
        central_log_path,
    )
    from pollypm.audit.watchdog import was_recently_dispatched

    central = central_log_path("legacydemo")
    central.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "ts": (now - timedelta(minutes=5)).isoformat(),
        "project": "legacydemo",
        "event": EVENT_WATCHDOG_ESCALATION_DISPATCHED,
        "subject": "legacydemo/7",
        "actor": "audit_watchdog",
        "status": "warn",
        # No "dedup_hash" key — simulates a pre-fix row.
        "metadata": {
            "finding_type": RULE_STUCK_DRAFT,
            "subject": "legacydemo/7",
            "brief": "...",
        },
    }
    central.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    # Caller passes a dedup_hash; row has none → fall back to subject match.
    assert was_recently_dispatched(
        project="legacydemo",
        finding_type=RULE_STUCK_DRAFT,
        subject="legacydemo/7",
        now=now,
        dedup_hash="deadbeefdeadbeef",
    )


def test_cadence_dispatch_throttles_samblog_burst_by_root_cause(
    now: datetime, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end #2015 regression: four sibling drafts → one dispatch.

    Drives the real cadence ``_maybe_dispatch_to_architect`` four
    times with Finding objects matching the samblog/32-35 burst:
    same rule, project, evidence; subjects differ. Pre-fix all four
    pass the throttle. Post-fix only the first emits; the next three
    return ``"throttled"``.
    """
    from pollypm.plugins_builtin.core_recurring import audit_watchdog as aw

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        aw, "_send_brief_to_architect",
        lambda target, brief: sent.append((target, brief)) or True,
    )

    common_evidence = {
        "queued_subjects": ["samblog/21", "samblog/26", "samblog/29",
                            "samblog/31"],
        "queued_last_updated": "2026-05-19T13:30:00+00:00",
    }
    outcomes: list[str] = []
    for n in (32, 33, 34, 35):
        finding = Finding(
            rule=RULE_STUCK_DRAFT,
            tier="2",
            project="samblog",
            subject=f"samblog/{n}",
            message=f"Draft task samblog/{n} has sat unpromoted ...",
            recommendation="Promote or cancel.",
            evidence=common_evidence,
        )
        outcomes.append(
            aw._maybe_dispatch_to_architect(
                finding,
                project_path=None,
                storage_closet_name="pollypm-storage-closet",
                now=now,
            )
        )

    assert outcomes[0] == "dispatched"
    # The remaining three siblings must be throttled — pre-fix every
    # one would have been "dispatched" because the subject differed.
    assert outcomes[1:] == ["throttled", "throttled", "throttled"], outcomes
    # Only one brief was actually sent.
    assert len(sent) == 1


def test_cadence_operator_dispatch_throttles_burst_by_root_cause(
    now: datetime, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same-condition tier-3 findings throttle; changed evidence dispatches."""
    from pollypm.audit.watchdog import RULE_QUEUE_WITHOUT_MOTION
    from pollypm.plugins_builtin.core_recurring import audit_watchdog as aw

    created: list[dict] = []

    def _stub_create(**kwargs):
        created.append(kwargs)
        return f"task-{len(created)}"

    monkeypatch.setattr(aw, "_create_operator_inbox_task", _stub_create)

    common_evidence = {
        "queued_subjects": ["proj/1", "proj/2"],
        "queued_last_updated": "2026-05-19T13:30:00+00:00",
    }
    outcomes: list[str] = []
    for tag in ("a", "b", "c", "d"):
        finding = Finding(
            rule=RULE_QUEUE_WITHOUT_MOTION,
            tier="3",
            project="proj",
            subject=f"proj-{tag}",
            evidence=common_evidence,
        )
        outcomes.append(
            aw._maybe_dispatch_to_operator(
                finding, project_path=None, now=now,
            )
        )

    assert outcomes[0] == "dispatched"
    assert outcomes[1:] == ["throttled", "throttled", "throttled"], outcomes
    assert len(created) == 1
    first_dedup_key = created[0]["dedup_key"]

    changed_condition = Finding(
        rule=RULE_QUEUE_WITHOUT_MOTION,
        tier="3",
        project="proj",
        subject="proj-e",
        evidence={
            "queued_subjects": ["proj/1", "proj/2", "proj/9"],
            "queued_last_updated": "2026-05-19T13:30:00+00:00",
        },
    )
    assert aw._maybe_dispatch_to_operator(
        changed_condition, project_path=None, now=now,
    ) == "dispatched"
    assert len(created) == 2
    assert created[0]["dedup_key"] == first_dedup_key
    assert created[1]["dedup_key"] != first_dedup_key


# ---------------------------------------------------------------------------
# PR #2020 review — dispatch_dedup_hash no-evidence fallback regression
# ---------------------------------------------------------------------------


def test_dispatch_dedup_hash_no_evidence_falls_back_to_subject() -> None:
    """No ``evidence`` and no metadata dedup key → ``subject`` must distinguish.

    The PR #2020 blocker (Codex review): production detectors that
    don't populate ``evidence`` (``stuck_draft`` is the canonical
    example — it only sets ``metadata``) collapsed to the same hash
    just because they shared rule+project. Two unrelated draft tasks
    ``demo/1`` and ``demo/2`` therefore over-deduped through the
    throttle. The fallback path must include ``subject`` so distinct
    findings get distinct hashes.
    """
    from pollypm.audit.watchdog import dispatch_dedup_hash

    f1 = Finding(rule=RULE_STUCK_DRAFT, project="demo", subject="demo/1")
    f2 = Finding(rule=RULE_STUCK_DRAFT, project="demo", subject="demo/2")

    h1 = dispatch_dedup_hash(f1)
    h2 = dispatch_dedup_hash(f2)
    assert h1 != h2, (
        "no-evidence findings must not collapse across subjects "
        f"(got {h1} == {h2})"
    )


def test_dispatch_dedup_hash_same_subject_no_evidence_stable() -> None:
    """Same rule+project+subject, no evidence → same hash (stable fallback)."""
    from pollypm.audit.watchdog import dispatch_dedup_hash

    f1 = Finding(rule=RULE_STUCK_DRAFT, project="demo", subject="demo/7")
    f2 = Finding(rule=RULE_STUCK_DRAFT, project="demo", subject="demo/7",
                 message="cosmetic copy varies")
    assert dispatch_dedup_hash(f1) == dispatch_dedup_hash(f2)


def test_dispatch_dedup_hash_evidence_collapses_across_subjects() -> None:
    """With evidence populated, sibling subjects must still collapse.

    Pins the #2015 contract: when a detector DOES populate ``evidence``,
    the throttle should dedupe across different subjects (samblog/32-35
    burst). The fallback only kicks in when evidence is empty.
    """
    from pollypm.audit.watchdog import dispatch_dedup_hash

    common = {"queued_subjects": ["demo/21", "demo/26"]}
    f1 = Finding(rule=RULE_STUCK_DRAFT, project="demo", subject="demo/1",
                 evidence=common)
    f2 = Finding(rule=RULE_STUCK_DRAFT, project="demo", subject="demo/2",
                 evidence=common)
    assert dispatch_dedup_hash(f1) == dispatch_dedup_hash(f2)


def test_dispatch_dedup_hash_metadata_root_cause_key_collapses() -> None:
    """When evidence is empty but metadata has ``root_cause_hash`` /
    ``dedup_key``, collapse across subjects using that key instead.

    Lets a detector opt into root-cause dedup without restructuring
    its payload as ``evidence`` — used by callers that already
    computed a stable hash upstream (tier-4 promotion tracker).
    """
    from pollypm.audit.watchdog import dispatch_dedup_hash

    f1 = Finding(
        rule=RULE_STUCK_DRAFT, project="demo", subject="demo/1",
        metadata={"root_cause_hash": "abc123", "detected_via": "state"},
    )
    f2 = Finding(
        rule=RULE_STUCK_DRAFT, project="demo", subject="demo/2",
        metadata={"root_cause_hash": "abc123", "detected_via": "event"},
    )
    f3 = Finding(
        rule=RULE_STUCK_DRAFT, project="demo", subject="demo/3",
        metadata={"root_cause_hash": "different"},
    )
    assert dispatch_dedup_hash(f1) == dispatch_dedup_hash(f2)
    assert dispatch_dedup_hash(f1) != dispatch_dedup_hash(f3)


def test_dispatch_dedup_hash_real_stuck_draft_detector_distinguishes_subjects(
    now: datetime,
) -> None:
    """Drive the actual ``_detect_stuck_drafts`` path and assert findings
    for different tasks get different hashes.

    Pre-fix Codex repro:

        Finding(rule=RULE_STUCK_DRAFT, project="demo", subject="demo/1")
        Finding(rule=RULE_STUCK_DRAFT, project="demo", subject="demo/2")
        → both hash to d14d2be490dfea41 (collision).

    The real detector only populates ``metadata`` (no ``evidence``),
    so the fallback path is exercised. This is the "use the actual
    detector, not hand-built findings" regression that Codex
    specifically requested.
    """
    from pollypm.audit.watchdog import dispatch_dedup_hash

    events = [
        _make_event(
            event=EVENT_TASK_CREATED, subject="demo/1",
            metadata={"title": "first"},
            ts=now - timedelta(minutes=20),
        ),
        _make_event(
            event=EVENT_TASK_CREATED, subject="demo/2",
            metadata={"title": "second"},
            ts=now - timedelta(minutes=20),
        ),
    ]
    findings = [
        f for f in scan_events(events, now=now) if f.rule == RULE_STUCK_DRAFT
    ]
    assert len(findings) == 2, findings
    # Every real-detector finding sets metadata but NOT evidence.
    for f in findings:
        assert f.evidence == {}, (
            "regression — _detect_stuck_drafts should not populate "
            "evidence; the fallback test depends on this shape"
        )
        assert f.metadata, "_detect_stuck_drafts should populate metadata"

    hashes = {dispatch_dedup_hash(f) for f in findings}
    assert len(hashes) == len(findings), (
        f"real-detector findings collapsed: {hashes}"
    )
