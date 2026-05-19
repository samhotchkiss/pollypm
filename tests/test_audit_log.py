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
    lines = [l for l in central.read_text(encoding="utf-8").splitlines() if l.strip()]
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
    lines = [l for l in central.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 5
    decoded = [json.loads(l) for l in lines]
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
# Sqlite-integration tests REMOVED for Slice K (#1737).
#
# The eight test_workservice_* tests that lived here exercised the
# sqlite work-service path (task.created / task.status_changed /
# task.deleted / work_db.opened audit emission). PgWorkService does
# not emit these events yet (see pg-gap #1787). Re-add equivalent
# coverage against pg_work_service once #1787 lands.
# ---------------------------------------------------------------------------
