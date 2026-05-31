"""Forensic audit log — writer + reader unit coverage.

Born from the savethenovel post-mortem (2026-05-06): we had no
forensic trail when the ``work_tasks`` table got wiped wholesale.
The audit log is the foundation for a future heartbeat that will
detect orphan / stuck / "table just got wiped" conditions, so
its writer must be:

* round-trippable (every event we emit must parse back),
* multi-event safe (append-only),
* per-project AND central-tail (heartbeat reads central; project
  log is the project-local source of truth),
* tolerant of malformed lines on the read side (a partial write
  must not crash the reader).

The previous module also held a suite of integration tests that
wired end-to-end task creation through the sqlite work-service and
asserted the audit hook fired in both per-project and central
locations. Those were removed in Slice K (#1737); pg-side audit
emission parity is tracked in #1787.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pollypm.audit import (
    AuditEvent,
    EVENT_AGENT_INJECTION_FLAGGED,
    EVENT_AGENT_REFUSAL,
    audit_record_agent_refusal,
    central_log_path,
    emit,
    project_log_path,
    read_events,
)
from pollypm.audit.log import (
    EVENT_TASK_CREATED,
    EVENT_TASK_STATUS_CHANGED,
    SCHEMA_VERSION,
)


@pytest.fixture(autouse=True)
def _isolate_audit_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the central-tail root so tests never touch ~/.pollypm/."""
    audit_home = tmp_path / "audit-home"
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))
    monkeypatch.delenv("POLLYPM_DISABLE_WORK_DB_OPENED_AUDIT", raising=False)
    return audit_home


# ---------------------------------------------------------------------------
# Writer / round-trip
# ---------------------------------------------------------------------------


def test_emit_writes_to_central_tail(tmp_path: Path) -> None:
    emit(
        event=EVENT_TASK_CREATED,
        project="demo",
        subject="demo/1",
        actor="polly",
        metadata={"title": "first task"},
    )

    central = central_log_path("demo")
    assert central.exists(), "central tail should be created on first emit"
    lines = [
        line
        for line in central.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == EVENT_TASK_CREATED
    assert record["project"] == "demo"
    assert record["subject"] == "demo/1"
    assert record["actor"] == "polly"
    assert record["status"] == "ok"
    assert record["metadata"] == {"title": "first task"}
    assert record["schema"] == SCHEMA_VERSION
    # ts is ISO-8601 UTC with timezone offset
    assert "T" in record["ts"]
    assert record["ts"].endswith("+00:00") or record["ts"].endswith("Z")


def test_emit_writes_to_per_project_log_when_path_given(tmp_path: Path) -> None:
    project_root = tmp_path / "project-root"
    (project_root / ".pollypm").mkdir(parents=True)

    emit(
        event=EVENT_TASK_CREATED,
        project="demo",
        subject="demo/1",
        actor="polly",
        project_path=project_root,
    )

    per_project = project_log_path(project_root)
    assert per_project is not None
    assert per_project.exists(), "per-project log should land under <root>/.pollypm/audit.jsonl"
    assert per_project == project_root / ".pollypm" / "audit.jsonl"

    central = central_log_path("demo")
    assert central.exists(), "central tail mirrors the per-project line"

    # Same payload in both files
    project_record = json.loads(per_project.read_text(encoding="utf-8").strip())
    central_record = json.loads(central.read_text(encoding="utf-8").strip())
    # ts may differ by microseconds because we don't share a clock
    # snapshot between the two writes — strip it before comparing.
    project_record.pop("ts")
    central_record.pop("ts")
    assert project_record == central_record


def test_emit_appends_multiple_events_in_order(tmp_path: Path) -> None:
    project_root = tmp_path / "p"
    (project_root / ".pollypm").mkdir(parents=True)

    for i in range(5):
        emit(
            event=EVENT_TASK_CREATED,
            project="multi",
            subject=f"multi/{i + 1}",
            actor="system",
            metadata={"i": i},
            project_path=project_root,
        )

    central = central_log_path("multi")
    lines = [
        line
        for line in central.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == 5
    decoded = [json.loads(line) for line in lines]
    assert [r["metadata"]["i"] for r in decoded] == [0, 1, 2, 3, 4]
    # All distinct timestamps OR equal-but-ordered — either way, the
    # append order is preserved.
    timestamps = [r["ts"] for r in decoded]
    assert timestamps == sorted(timestamps)


def test_emit_swallows_non_serializable_metadata(tmp_path: Path) -> None:
    """A metadata object that can't be JSON-serialized must not crash
    the caller — we record a stripped event so the audit trail still
    notes that *something* happened."""

    class NotSerializable:
        pass

    emit(
        event=EVENT_TASK_CREATED,
        project="demo",
        subject="demo/1",
        metadata={"thing": NotSerializable()},
    )

    central = central_log_path("demo")
    record = json.loads(central.read_text(encoding="utf-8").strip())
    assert record["event"] == EVENT_TASK_CREATED
    assert record["metadata"] == {"_error": "metadata_not_serializable"}


def test_emit_never_raises_on_filesystem_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit writes are best-effort — a permissions / disk-full
    failure must not propagate into the caller's mutation path."""

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("pollypm.audit.log._append_line", _boom)

    # Should not raise even though every append is failing.
    emit(event=EVENT_TASK_CREATED, project="demo", subject="demo/1")


def test_record_agent_refusal_emits_flagged_and_refusal_events(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project-root"
    (project_root / ".pollypm").mkdir(parents=True)

    audit_record_agent_refusal(
        project="demo",
        actor="architect-demo",
        reason="bad-auth-marker",
        source="pollypm-auth",
        subject="watchdog-brief",
        project_path=project_root,
    )

    events = read_events("demo", project_path=project_root)
    assert [event.event for event in events] == [
        EVENT_AGENT_INJECTION_FLAGGED,
        EVENT_AGENT_REFUSAL,
    ]
    assert {event.status for event in events} == {"warn"}
    assert {event.actor for event in events} == {"architect-demo"}
    assert {event.subject for event in events} == {"watchdog-brief"}
    for event in events:
        assert event.metadata["reason"] == "bad-auth-marker"
        assert event.metadata["source"] == "pollypm-auth"
        assert "token" not in json.dumps(event.metadata).lower()


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


def test_read_events_returns_chronological_list(tmp_path: Path) -> None:
    for i in range(3):
        emit(
            event=EVENT_TASK_CREATED,
            project="r1",
            subject=f"r1/{i + 1}",
            metadata={"i": i},
        )

    events = read_events("r1")
    assert len(events) == 3
    assert all(isinstance(e, AuditEvent) for e in events)
    assert [e.subject for e in events] == ["r1/1", "r1/2", "r1/3"]


def test_read_events_filters_by_event_name(tmp_path: Path) -> None:
    emit(event=EVENT_TASK_CREATED, project="f1", subject="f1/1")
    emit(event=EVENT_TASK_STATUS_CHANGED, project="f1", subject="f1/1",
         metadata={"from": "draft", "to": "queued"})
    emit(event=EVENT_TASK_STATUS_CHANGED, project="f1", subject="f1/1",
         metadata={"from": "queued", "to": "in_progress"})

    transitions = read_events("f1", event=EVENT_TASK_STATUS_CHANGED)
    assert len(transitions) == 2
    assert all(e.event == EVENT_TASK_STATUS_CHANGED for e in transitions)


def test_read_events_filters_by_since_timestamp(tmp_path: Path) -> None:
    emit(event=EVENT_TASK_CREATED, project="t1", subject="t1/1")
    # Capture a marker timestamp between events.
    central = central_log_path("t1")
    first_line = json.loads(central.read_text(encoding="utf-8").splitlines()[0])
    cutoff = first_line["ts"]

    emit(event=EVENT_TASK_CREATED, project="t1", subject="t1/2")
    emit(event=EVENT_TASK_CREATED, project="t1", subject="t1/3")

    after = read_events("t1", since=cutoff)
    subjects = [e.subject for e in after]
    # The first event is at-or-before the cutoff; subsequent events
    # are strictly after. The strict comparison guarantees we don't
    # double-count an event whose ts equals the heartbeat's last-seen
    # marker.
    assert "t1/1" not in subjects
    assert "t1/2" in subjects
    assert "t1/3" in subjects


def test_read_events_limit_returns_last_n(tmp_path: Path) -> None:
    for i in range(10):
        emit(event=EVENT_TASK_CREATED, project="L", subject=f"L/{i + 1}")

    events = read_events("L", limit=3)
    assert len(events) == 3
    assert [e.subject for e in events] == ["L/8", "L/9", "L/10"]


def test_read_events_prefers_per_project_log(tmp_path: Path) -> None:
    """When ``project_path`` is provided AND the per-project log
    exists, the reader must source from it — that file is the
    project's source of truth and survives central-tail rotation."""
    project_root = tmp_path / "pref"
    (project_root / ".pollypm").mkdir(parents=True)

    emit(event=EVENT_TASK_CREATED, project="pref", subject="pref/1",
         project_path=project_root)

    # Wipe the central tail to prove the read came from per-project.
    central = central_log_path("pref")
    central.unlink()

    events = read_events("pref", project_path=project_root)
    assert len(events) == 1
    assert events[0].subject == "pref/1"


def test_read_events_skips_truncated_tail_lines(tmp_path: Path) -> None:
    """A process killed mid-write can leave a partial JSON line at
    the tail. The reader must skip it rather than crashing — the
    heartbeat must keep working past a junk byte."""
    emit(event=EVENT_TASK_CREATED, project="trunc", subject="trunc/1")
    central = central_log_path("trunc")
    # Append a deliberately-corrupt line.
    with open(central, "a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026-05-06T00:00:00+00:00", "event": "task.cre')
        # No newline / no closing brace — truncated mid-write.

    events = read_events("trunc")
    assert len(events) == 1
    assert events[0].subject == "trunc/1"


def test_emit_recovers_after_truncated_central_tail(tmp_path: Path) -> None:
    """A fresh append after a corrupt tail must not get joined onto
    the partial JSON line and disappear from readers."""
    emit(event=EVENT_TASK_CREATED, project="recover", subject="recover/1")
    central = central_log_path("recover")
    with open(central, "a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026-05-06T00:00:00+00:00", "event": "task.cre')

    emit(event=EVENT_TASK_CREATED, project="recover", subject="recover/2")

    lines = central.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert json.loads(lines[-1])["subject"] == "recover/2"
    events = read_events("recover")
    assert [event.subject for event in events] == ["recover/1", "recover/2"]


def test_emit_recovers_after_truncated_per_project_tail(tmp_path: Path) -> None:
    """The per-project audit log is the source of truth, so tail
    recovery must work there too."""
    project_root = tmp_path / "recover-project"
    (project_root / ".pollypm").mkdir(parents=True)

    emit(
        event=EVENT_TASK_CREATED,
        project="recoverproj",
        subject="recoverproj/1",
        project_path=project_root,
    )
    per_project = project_log_path(project_root)
    assert per_project is not None
    with open(per_project, "a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026-05-06T00:00:00+00:00", "event": "task.cre')

    emit(
        event=EVENT_TASK_CREATED,
        project="recoverproj",
        subject="recoverproj/2",
        project_path=project_root,
    )

    lines = per_project.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert json.loads(lines[-1])["subject"] == "recoverproj/2"
    events = read_events("recoverproj", project_path=project_root)
    assert [event.subject for event in events] == [
        "recoverproj/1",
        "recoverproj/2",
    ]


def test_read_events_empty_when_no_log_exists(tmp_path: Path) -> None:
    assert read_events("never-emitted") == []


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def test_central_log_path_sanitizes_project_keys(_isolate_audit_home: Path) -> None:
    """A project key with separators must not escape the central root."""
    p = central_log_path("../escape")
    assert p.parent == _isolate_audit_home
    assert ".." not in p.name
    assert "/" not in p.name


def test_emit_with_empty_project_only_writes_per_project(tmp_path: Path) -> None:
    """An empty project key skips the central tail (no project file
    to write to) but the per-project log still receives the event
    when a project_path is supplied."""
    project_root = tmp_path / "anon"
    (project_root / ".pollypm").mkdir(parents=True)

    emit(
        event="task.created",
        project="",
        subject="anon/1",
        project_path=project_root,
    )

    per_project = project_log_path(project_root)
    assert per_project is not None
    assert per_project.exists()


# ---------------------------------------------------------------------------
# Rotation + retention (#2023)
# ---------------------------------------------------------------------------


@pytest.fixture
def _tiny_rotation(monkeypatch: pytest.MonkeyPatch):
    """Pin a tiny rotation threshold so tests fire rotation without
    building 50 MB of audit data. Yields a helper that lets a test
    tune the retention count too.
    """
    import pollypm.audit.log as log_mod

    monkeypatch.setattr(log_mod, "_test_rotate_size_bytes", 512)
    monkeypatch.setattr(log_mod, "_test_retention_count", 4)
    # Ensure no env-var override leaks across tests.
    monkeypatch.delenv(log_mod._DISABLE_ENV, raising=False)
    return log_mod


def _gz_archives(audit_path: Path) -> list[Path]:
    prefix = audit_path.name + "."
    return sorted(
        sibling
        for sibling in audit_path.parent.iterdir()
        if sibling.name.startswith(prefix) and sibling.name.endswith(".gz")
    )


def test_rotation_fires_when_size_exceeds_threshold(
    tmp_path: Path, _tiny_rotation
) -> None:
    """Write past the threshold; the live file should be empty (or
    contain only the post-rotation line) and a single .gz archive
    should exist with the pre-rotation events."""
    project_root = tmp_path / "rotproj"
    (project_root / ".pollypm").mkdir(parents=True)
    audit_path = project_root / ".pollypm" / "audit.jsonl"

    # Write enough events to push us past the 512-byte threshold but
    # leave the file small enough that we can inspect every line.
    for i in range(20):
        emit(
            event="task.created",
            project="rotproj",
            subject=f"rotproj/{i}",
            metadata={"i": i, "padding": "x" * 64},
            project_path=project_root,
        )

    archives = _gz_archives(audit_path)
    assert len(archives) >= 1, (
        f"expected at least one .gz rotation, found {archives}"
    )
    # The live file must still exist and (because the last append
    # fired after the most recent rotation) hold at most the events
    # written since the last rotation — strictly smaller than the
    # original 20-event payload.
    assert audit_path.exists()
    live_lines = [
        line
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(live_lines) < 20


def test_rotation_archives_contain_pre_rotation_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gzipped archive must hold a readable JSONL chunk of the
    events written before the rotation fired.

    Uses a generous retention so events written before rotation are
    not also lost to over-aggressive pruning — the unit under test
    is gz contents, not retention prune logic (covered separately).
    """
    import pollypm.audit.log as log_mod

    # Big retention so multi-rotation runs don't lose history to
    # prune; that path is exercised in
    # ``test_rotation_retention_prunes_old_archives``.
    monkeypatch.setattr(log_mod, "_test_rotate_size_bytes", 2048)
    monkeypatch.setattr(log_mod, "_test_retention_count", 50)
    monkeypatch.delenv(log_mod._DISABLE_ENV, raising=False)

    project_root = tmp_path / "gzproj"
    (project_root / ".pollypm").mkdir(parents=True)
    audit_path = project_root / ".pollypm" / "audit.jsonl"

    for i in range(40):
        emit(
            event="task.created",
            project="gzproj",
            subject=f"gzproj/{i}",
            metadata={"i": i, "padding": "y" * 64},
            project_path=project_root,
        )

    archives = _gz_archives(audit_path)
    assert archives, "expected at least one .gz archive"
    import gzip as _gz

    archived_subjects: list[str] = []
    for gz_path in archives:
        with _gz.open(gz_path, "rt", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                record = json.loads(stripped)
                archived_subjects.append(record["subject"])
                assert record["event"] == "task.created"
                assert record["project"] == "gzproj"

    # Plus whatever is in the live file currently.
    live_subjects = [
        json.loads(line)["subject"]
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    # With retention=50 no archive can have been pruned, so every
    # subject we emitted must appear somewhere (archives + live).
    all_subjects = set(archived_subjects) | set(live_subjects)
    expected = {f"gzproj/{i}" for i in range(40)}
    assert expected.issubset(all_subjects), (
        f"missing subjects after rotation: {expected - all_subjects}"
    )


def test_rotation_retention_prunes_old_archives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When N rotations have already happened, the next rotation must
    delete the oldest archive so we hold steady at N total."""
    import pollypm.audit.log as log_mod

    monkeypatch.setattr(log_mod, "_test_rotate_size_bytes", 256)
    monkeypatch.setattr(log_mod, "_test_retention_count", 2)
    monkeypatch.delenv(log_mod._DISABLE_ENV, raising=False)

    project_root = tmp_path / "retproj"
    (project_root / ".pollypm").mkdir(parents=True)
    audit_path = project_root / ".pollypm" / "audit.jsonl"

    # Hammer enough events to trigger multiple rotations. Sleep
    # between batches so each archive picks up a distinct mtime and
    # the prune sort can order them deterministically.
    import time as _time

    for batch in range(5):
        for i in range(10):
            emit(
                event="task.created",
                project="retproj",
                subject=f"retproj/b{batch}-{i}",
                metadata={"padding": "z" * 64},
                project_path=project_root,
            )
        _time.sleep(0.02)

    archives = _gz_archives(audit_path)
    assert len(archives) <= 2, (
        f"retention=2 should cap archives at 2, got {len(archives)}: {archives}"
    )
    # The live file must still be writable (and contain the latest
    # append — proving the audit pipeline never broke).
    emit(
        event="task.created",
        project="retproj",
        subject="retproj/final",
        project_path=project_root,
    )
    final_lines = [
        line
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(
        json.loads(line)["subject"] == "retproj/final" for line in final_lines
    ), "post-rotation append must still land in the live file"


def test_rotation_failure_does_not_break_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If rotation raises (read-only dir, disk full, etc.), the
    append must still happen — the audit log is load-bearing for
    the watchdog and housekeeping must never lose writes."""
    import pollypm.audit.log as log_mod

    monkeypatch.setattr(log_mod, "_test_rotate_size_bytes", 256)
    monkeypatch.setattr(log_mod, "_test_retention_count", 4)
    monkeypatch.delenv(log_mod._DISABLE_ENV, raising=False)

    project_root = tmp_path / "failproj"
    (project_root / ".pollypm").mkdir(parents=True)
    audit_path = project_root / ".pollypm" / "audit.jsonl"

    # Prime the audit log past the threshold so the next emit would
    # normally trigger a rotation.
    for i in range(20):
        emit(
            event="task.created",
            project="failproj",
            subject=f"failproj/{i}",
            metadata={"padding": "p" * 64},
            project_path=project_root,
        )

    # Force every subsequent rename in the rotation path to fail.
    def _boom_rename(*_args, **_kwargs):
        raise OSError("simulated rotation failure")

    monkeypatch.setattr(log_mod.os, "rename", _boom_rename)

    # The append MUST still succeed.
    emit(
        event="task.created",
        project="failproj",
        subject="failproj/after-failure",
        project_path=project_root,
    )

    lines = [
        line
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    subjects = [json.loads(line)["subject"] for line in lines]
    assert "failproj/after-failure" in subjects, (
        "audit append must succeed even when rotation fails"
    )


def test_rotation_disabled_via_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The env-var escape hatch short-circuits rotation entirely so
    incident debuggers can pin a continuous file mid-investigation."""
    import pollypm.audit.log as log_mod

    monkeypatch.setattr(log_mod, "_test_rotate_size_bytes", 256)
    monkeypatch.setattr(log_mod, "_test_retention_count", 4)
    monkeypatch.setenv(log_mod._DISABLE_ENV, "1")

    project_root = tmp_path / "noproj"
    (project_root / ".pollypm").mkdir(parents=True)
    audit_path = project_root / ".pollypm" / "audit.jsonl"

    for i in range(20):
        emit(
            event="task.created",
            project="noproj",
            subject=f"noproj/{i}",
            metadata={"padding": "q" * 64},
            project_path=project_root,
        )

    archives = _gz_archives(audit_path)
    assert archives == [], (
        f"rotation disabled should produce zero archives, got {archives}"
    )
    # All 20 lines should be in the live file because nothing rotated.
    lines = [
        line
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == 20


def test_read_events_walks_gz_archives_after_rotation(
    tmp_path: Path,
) -> None:
    """Regression for the #2032 codex blocker: ``read_events()`` must
    walk rotated ``.gz`` archives so events that landed in the
    rotated chunk remain visible to consumers like the watchdog
    dedupe window.

    Without rotation-aware reads, a rotation between escalation
    dispatches would hide the prior ``watchdog.escalation_dispatched``
    row and allow a duplicate dispatch.

    Test shape:

    1. Seed a ``audit.jsonl.<ts>.gz`` archive with a
       ``watchdog.escalation_dispatched`` event.
    2. Leave the live ``audit.jsonl`` either empty or with
       unrelated events only.
    3. Call ``read_events(event="watchdog.escalation_dispatched",
       since=<old_iso>)``.
    4. Assert the archived event is returned.
    """
    import gzip as _gz

    project_root = tmp_path / "gzread"
    (project_root / ".pollypm").mkdir(parents=True)
    audit_path = project_root / ".pollypm" / "audit.jsonl"

    # Seed an archived rotation: a single .gz file holding the
    # historical watchdog.escalation_dispatched event.
    archived_event = {
        "schema": SCHEMA_VERSION,
        "ts": "2026-05-19T12:00:00+00:00",
        "project": "gzread",
        "event": "watchdog.escalation_dispatched",
        "subject": "gzread/finding-A",
        "actor": "watchdog",
        "status": "ok",
        "metadata": {
            "finding_type": "stuck_draft",
            "root_cause_hash": "abc123",
        },
    }
    gz_archive = audit_path.with_suffix(audit_path.suffix + ".1234567890.gz")
    with _gz.open(gz_archive, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(archived_event) + "\n")

    # Live file: empty (touch only). The post-rotation append in
    # production lands the next event here; for this regression the
    # rotated chunk is what matters.
    audit_path.touch()

    # Query as the watchdog would: filter on the dedupe event +
    # an old since-cutoff that should clearly include the archived
    # event.
    events = read_events(
        "gzread",
        project_path=project_root,
        event="watchdog.escalation_dispatched",
        since="2026-05-01T00:00:00+00:00",
    )

    subjects = [e.subject for e in events]
    assert "gzread/finding-A" in subjects, (
        "read_events must walk .gz archives — the rotated "
        "watchdog.escalation_dispatched row went missing, which "
        "would allow duplicate dispatches in production"
    )
    archived_match = next(
        (e for e in events if e.subject == "gzread/finding-A"), None
    )
    assert archived_match is not None
    assert archived_match.metadata.get("root_cause_hash") == "abc123"


def test_read_events_limit_uses_live_tail_without_full_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Project detail asks for the last 25 audit rows.

    That path must stay proportional to the requested page size, not to
    the full audit history. When the live log has enough rows, the
    limited reader should tail the JSONL directly and never invoke the
    forward full-file iterator.
    """
    import pollypm.audit.log as log_mod

    project_root = tmp_path / "tailfast"
    (project_root / ".pollypm").mkdir(parents=True)
    audit_path = project_root / ".pollypm" / "audit.jsonl"

    rows = []
    for i in range(200):
        rows.append(json.dumps({
            "schema": SCHEMA_VERSION,
            "ts": f"2026-05-20T00:{i // 60:02d}:{i % 60:02d}+00:00",
            "project": "tailfast",
            "event": "task.status_changed",
            "subject": f"tailfast/{i}",
            "actor": "test",
            "status": "ok",
            "metadata": {},
        }))
    audit_path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    def fail_forward_scan(_path):
        raise AssertionError("limited read should not scan from file head")

    monkeypatch.setattr(log_mod, "_iter_log_lines", fail_forward_scan)

    events = log_mod.read_events("tailfast", project_path=project_root, limit=5)

    assert [event.subject for event in events] == [
        "tailfast/195",
        "tailfast/196",
        "tailfast/197",
        "tailfast/198",
        "tailfast/199",
    ]


def test_read_events_since_uses_live_tail_without_full_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Watchdog sweeps query recent windows with ``since``.

    That path must not walk a large live audit log from the head just to
    discard old rows. It should read newest-first and stop at the first
    row outside the requested window.
    """
    import pollypm.audit.log as log_mod

    project_root = tmp_path / "sincefast"
    (project_root / ".pollypm").mkdir(parents=True)
    audit_path = project_root / ".pollypm" / "audit.jsonl"

    rows = []
    for i in range(500):
        rows.append(json.dumps({
            "schema": SCHEMA_VERSION,
            "ts": f"2026-05-30T00:{i // 60:02d}:{i % 60:02d}+00:00",
            "project": "sincefast",
            "event": "task.status_changed",
            "subject": f"sincefast/old-{i}",
            "actor": "test",
            "status": "ok",
            "metadata": {"padding": "x" * 64},
        }))
    for i in range(3):
        rows.append(json.dumps({
            "schema": SCHEMA_VERSION,
            "ts": f"2026-05-31T10:00:0{i}+00:00",
            "project": "sincefast",
            "event": "task.status_changed",
            "subject": f"sincefast/recent-{i}",
            "actor": "test",
            "status": "ok",
            "metadata": {},
        }))
    audit_path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    def fail_forward_scan(_path):
        raise AssertionError("since read should not scan from file head")

    decode_calls = 0
    original_decode = log_mod._decode_audit_line

    def counting_decode(raw):
        nonlocal decode_calls
        decode_calls += 1
        return original_decode(raw)

    monkeypatch.setattr(log_mod, "_TAIL_READ_CHUNK_BYTES", 256)
    monkeypatch.setattr(log_mod, "_iter_log_lines", fail_forward_scan)
    monkeypatch.setattr(log_mod, "_decode_audit_line", counting_decode)

    events = log_mod.read_events(
        "sincefast",
        project_path=project_root,
        since="2026-05-31T09:59:59+00:00",
    )

    assert [event.subject for event in events] == [
        "sincefast/recent-0",
        "sincefast/recent-1",
        "sincefast/recent-2",
    ]
    assert decode_calls < 20, (
        "since reads should stop at the recent-window boundary, not "
        "decode the full historical corpus"
    )


def test_read_events_chains_live_and_gz_in_chronological_order(
    tmp_path: Path,
) -> None:
    """When events exist across BOTH a .gz archive and the live file,
    ``read_events()`` must return them in chronological order so
    downstream consumers (heartbeat, briefings, doctor) see a
    coherent timeline."""
    import gzip as _gz

    project_root = tmp_path / "chain"
    (project_root / ".pollypm").mkdir(parents=True)
    audit_path = project_root / ".pollypm" / "audit.jsonl"

    # Older event in an archive.
    old_event = {
        "schema": SCHEMA_VERSION,
        "ts": "2026-05-01T00:00:00+00:00",
        "project": "chain",
        "event": "task.created",
        "subject": "chain/old",
        "actor": "test",
        "status": "ok",
        "metadata": {},
    }
    gz_archive = audit_path.with_suffix(audit_path.suffix + ".1700000000.gz")
    with _gz.open(gz_archive, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(old_event) + "\n")

    # Newer event in the live file (after rotation).
    new_event = {
        "schema": SCHEMA_VERSION,
        "ts": "2026-05-20T00:00:00+00:00",
        "project": "chain",
        "event": "task.created",
        "subject": "chain/new",
        "actor": "test",
        "status": "ok",
        "metadata": {},
    }
    audit_path.write_text(json.dumps(new_event) + "\n", encoding="utf-8")

    events = read_events("chain", project_path=project_root)
    subjects = [e.subject for e in events]
    assert subjects == ["chain/old", "chain/new"], (
        f"events should be chronological across .gz + live, got {subjects}"
    )


def test_read_events_walks_gz_archives_for_central_tail(
    tmp_path: Path,
) -> None:
    """The rotation walker must apply to the central-tail fallback
    too (no project_path), so the workspace-level audit grep / SSE
    consumers also see rotated history."""
    import gzip as _gz

    central = central_log_path("centralgz")
    central.parent.mkdir(parents=True, exist_ok=True)

    archived_event = {
        "schema": SCHEMA_VERSION,
        "ts": "2026-05-10T00:00:00+00:00",
        "project": "centralgz",
        "event": "watchdog.escalation_dispatched",
        "subject": "centralgz/finding-Z",
        "actor": "watchdog",
        "status": "ok",
        "metadata": {"root_cause_hash": "deadbeef"},
    }
    gz_archive = central.with_suffix(central.suffix + ".1700000001.gz")
    with _gz.open(gz_archive, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(archived_event) + "\n")
    # Live central tail: empty.
    central.touch()

    events = read_events(
        "centralgz",
        event="watchdog.escalation_dispatched",
        since="2026-05-01T00:00:00+00:00",
    )
    assert [e.subject for e in events] == ["centralgz/finding-Z"]


# ---------------------------------------------------------------------------
# Sqlite-integration tests REMOVED for Slice K (#1737).
#
# The eight test_workservice_* tests that lived here exercised the
# sqlite work-service path (task.created / task.status_changed /
# task.deleted / work_db.opened audit emission). PgWorkService does
# not emit these events yet (see pg-gap #1787). Re-add equivalent
# coverage against pg_work_service once #1787 lands.
# ---------------------------------------------------------------------------
