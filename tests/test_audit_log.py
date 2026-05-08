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

The integration test wires an end-to-end task creation through
``SQLiteWorkService`` and asserts the audit hook fired in both
locations.
"""

from __future__ import annotations

import json
import sqlite3
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
    EVENT_TASK_DELETED,
    EVENT_TASK_STATUS_CHANGED,
    EVENT_WORK_DB_OPENED,
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
# Integration: work-service create + transition fire audit hooks
# ---------------------------------------------------------------------------


def test_workservice_create_emits_task_created(tmp_path: Path) -> None:
    """End-to-end: ``service.create()`` writes a ``task.created`` event
    to BOTH the per-project log and the central tail. This is the
    minimum reproducer for the savethenovel post-mortem — without
    this hook, a wiped ``work_tasks`` row leaves no forensic trail
    that the task ever existed."""
    from pollypm.work.sqlite_service import SQLiteWorkService

    project_root = tmp_path / "savethenovel"
    (project_root / ".pollypm").mkdir(parents=True)
    db_path = tmp_path / "work.db"

    svc = SQLiteWorkService(db_path=db_path, project_path=project_root)
    try:
        task = svc.create(
            title="Plan the rescue",
            description="kick-off planning task",
            type="task",
            project="savethenovel",
            flow_template="standard",
            roles={"worker": "agent-1", "reviewer": "agent-2"},
            priority="normal",
            created_by="polly",
        )
    finally:
        svc.close()

    # Per-project log
    per_project = project_log_path(project_root)
    assert per_project is not None and per_project.exists(), (
        "task.created hook should land in <project>/.pollypm/audit.jsonl"
    )
    project_lines = [
        json.loads(l)
        for l in per_project.read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    created = [r for r in project_lines if r["event"] == "task.created"]
    assert len(created) == 1
    assert created[0]["subject"] == task.task_id
    assert created[0]["project"] == "savethenovel"
    assert created[0]["actor"] == "polly"
    assert created[0]["metadata"]["title"] == "Plan the rescue"
    assert created[0]["metadata"]["flow_template"] == "standard"

    # Central tail mirror
    central = central_log_path("savethenovel")
    assert central.exists()
    central_records = [
        json.loads(l)
        for l in central.read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    central_creates = [r for r in central_records if r["event"] == "task.created"]
    assert len(central_creates) == 1
    assert central_creates[0]["subject"] == task.task_id

    # Reader picks up the same event via the public API
    events = read_events("savethenovel", project_path=project_root,
                        event="task.created")
    assert len(events) == 1
    assert events[0].subject == task.task_id


def test_workservice_transition_emits_status_changed(tmp_path: Path) -> None:
    """``_record_transition`` is the single chokepoint for every
    state change. Hooking there means queue/claim/done/cancel all
    audit consistently with no per-callsite plumbing."""
    from pollypm.work.sqlite_service import SQLiteWorkService

    project_root = tmp_path / "p"
    (project_root / ".pollypm").mkdir(parents=True)
    db_path = tmp_path / "work.db"

    svc = SQLiteWorkService(db_path=db_path, project_path=project_root)
    try:
        task = svc.create(
            title="t",
            description="real description so the queue gate passes",
            type="task",
            project="p",
            flow_template="standard",
            roles={"worker": "w", "reviewer": "r"},
            created_by="tester",
        )
        # queue() drives _record_transition under the hood
        svc.queue(task.task_id, "tester")
    finally:
        svc.close()

    transitions = read_events(
        "p", project_path=project_root, event="task.status_changed",
    )
    assert transitions, "queue() should fire a task.status_changed audit event"
    # The transition row carries from/to in metadata so the heartbeat
    # can reconstruct the state machine without re-reading work_transitions.
    last = transitions[-1]
    assert last.subject == task.task_id
    assert "from" in last.metadata
    assert "to" in last.metadata
    assert last.metadata["to"] == "queued"


def test_workservice_flushes_raw_work_task_delete_to_audit(
    tmp_path: Path,
) -> None:
    """A raw SQL sweeper cannot make a work_tasks row vanish silently.

    The delete trigger records the old task id in an outbox inside the
    work DB. The next work-service open flushes that outbox to the JSONL
    audit stream as ``task.deleted``.
    """
    from pollypm.work.sqlite_service import SQLiteWorkService

    project_root = tmp_path / "savethenovel"
    (project_root / ".pollypm").mkdir(parents=True)
    db_path = tmp_path / "work.db"

    svc = SQLiteWorkService(db_path=db_path, project_path=project_root)
    try:
        task = svc.create(
            title="Advisor review for savethenovel",
            description="Review recent project trajectory.",
            type="task",
            project="savethenovel",
            flow_template="advisor_review",
            roles={"advisor": "advisor"},
            priority="normal",
            created_by="advisor.tick",
            labels=["advisor"],
        )
        svc.queue(task.task_id, "advisor.tick")
    finally:
        svc.close()

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "DELETE FROM work_transitions "
            "WHERE task_project = ? AND task_number = ?",
            (task.project, task.task_number),
        )
        conn.execute(
            "DELETE FROM work_tasks WHERE project = ? AND task_number = ?",
            (task.project, task.task_number),
        )
        conn.commit()

    svc2 = SQLiteWorkService(db_path=db_path, project_path=project_root)
    svc2.close()

    deleted = read_events(
        "savethenovel", project_path=project_root, event=EVENT_TASK_DELETED,
    )
    assert len(deleted) == 1
    assert deleted[0].subject == task.task_id
    assert deleted[0].metadata["flow_template"] == "advisor_review"
    assert deleted[0].metadata["previous_status"] == "queued"


# ---------------------------------------------------------------------------
# Integration: SQLiteWorkService DB-open breadcrumb
# ---------------------------------------------------------------------------


def _read_central_records(audit_home: Path) -> list[dict]:
    """Read every JSONL row across the central tail.

    The ``work_db.opened`` event uses ``project=""`` (workspace-level,
    not project-scoped), so it lands only in the central tail under
    the sanitized ``"_unknown"`` filename. We scan every file in the
    audit home so the test stays robust if the sanitizer changes.
    """
    records: list[dict] = []
    if not audit_home.exists():
        return records
    for f in audit_home.iterdir():
        if f.is_file() and f.suffix == ".jsonl":
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    records.append(json.loads(line))
    return records


def test_workservice_open_emits_work_db_opened_on_fresh_db(
    tmp_path: Path, _isolate_audit_home: Path,
) -> None:
    """Opening a brand-new SQLite file emits ``work_db.opened`` with
    ``had_messages_table_pre_open=False`` and ``tables_created=True`` —
    the canonical "first-time workspace open" signal."""
    from pollypm.work.sqlite_service import SQLiteWorkService

    db_path = tmp_path / "fresh.db"
    svc = SQLiteWorkService(db_path=db_path)
    try:
        records = _read_central_records(_isolate_audit_home)
        opens = [r for r in records if r["event"] == EVENT_WORK_DB_OPENED]
        assert len(opens) == 1, f"expected one work_db.opened row, got {len(opens)}"
        row = opens[0]
        assert row["subject"] == str(db_path)
        assert row["actor"] == "system"
        assert row["metadata"]["had_messages_table_pre_open"] is False
        assert row["metadata"]["tables_created"] is True
    finally:
        svc.close()


def test_workservice_open_flags_messages_side_db(
    tmp_path: Path, _isolate_audit_home: Path,
) -> None:
    """The dual-DB confusion case: a messages-side DB has a
    ``messages`` table. When ``SQLiteWorkService`` is pointed at one,
    the audit row carries ``had_messages_table_pre_open=True`` so a
    grep over the audit log surfaces the misroute immediately."""
    import sqlite3

    from pollypm.work.sqlite_service import SQLiteWorkService

    db_path = tmp_path / "messages-side.db"
    # Pre-create a messages-side DB by stamping the marker table.
    pre = sqlite3.connect(db_path)
    try:
        pre.execute(
            "CREATE TABLE messages ("
            "id INTEGER PRIMARY KEY, body TEXT, created_at TEXT"
            ")"
        )
        pre.commit()
    finally:
        pre.close()

    svc = SQLiteWorkService(db_path=db_path)
    try:
        records = _read_central_records(_isolate_audit_home)
        opens = [r for r in records if r["event"] == EVENT_WORK_DB_OPENED]
        assert len(opens) == 1
        row = opens[0]
        # The warning signal — work_tasks didn't exist yet (so we
        # stamped them on), but the DB ALREADY had a messages table.
        # That combination = wrong DB.
        assert row["metadata"]["had_messages_table_pre_open"] is True
        assert row["metadata"]["tables_created"] is True
    finally:
        svc.close()


def test_workservice_open_reports_tables_created_false_on_existing_work_db(
    tmp_path: Path, _isolate_audit_home: Path,
) -> None:
    """Re-opening a workspace DB that already has work tables emits
    ``tables_created=False`` — the steady-state signal that
    distinguishes a normal restart from a first-time stamp."""
    from pollypm.work.sqlite_service import SQLiteWorkService

    db_path = tmp_path / "existing.db"
    # First open materializes work_tasks et al.
    svc1 = SQLiteWorkService(db_path=db_path)
    svc1.close()

    # Wipe the audit log so we only see the second open's row.
    for f in _isolate_audit_home.iterdir() if _isolate_audit_home.exists() else []:
        if f.is_file():
            f.unlink()

    svc2 = SQLiteWorkService(db_path=db_path)
    try:
        records = _read_central_records(_isolate_audit_home)
        opens = [r for r in records if r["event"] == EVENT_WORK_DB_OPENED]
        assert len(opens) == 1
        row = opens[0]
        assert row["metadata"]["had_messages_table_pre_open"] is False
        assert row["metadata"]["tables_created"] is False
    finally:
        svc2.close()


def test_workservice_open_per_project_log_when_project_path_supplied(
    tmp_path: Path, _isolate_audit_home: Path,
) -> None:
    """When the caller passes a ``project_path``, the breadcrumb also
    lands in ``<project>/.pollypm/audit.jsonl`` so the row travels
    with the project tree (matches the per-project + central
    pattern used by every other audit hook in this module)."""
    from pollypm.work.sqlite_service import SQLiteWorkService

    project_root = tmp_path / "proj"
    (project_root / ".pollypm").mkdir(parents=True)
    db_path = tmp_path / "p.db"

    svc = SQLiteWorkService(db_path=db_path, project_path=project_root)
    try:
        per_project = project_log_path(project_root)
        assert per_project is not None and per_project.exists()
        rows = [
            json.loads(l)
            for l in per_project.read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
        opens = [r for r in rows if r["event"] == EVENT_WORK_DB_OPENED]
        assert len(opens) == 1
        assert opens[0]["metadata"]["project_path"] == str(project_root)
    finally:
        svc.close()


def test_workservice_open_audit_can_be_disabled_for_pytest_isolation(
    tmp_path: Path,
    _isolate_audit_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pytest's global guard suppresses the DB-open breadcrumb entirely.

    The central tail is already redirected during tests, but the per-project
    audit log intentionally ignores that redirect. Suppressing only this
    high-frequency lifecycle event keeps broad test sweeps out of a real
    workspace's ``.pollypm/audit.jsonl``.
    """
    from pollypm.work.sqlite_service import SQLiteWorkService

    monkeypatch.setenv("POLLYPM_DISABLE_WORK_DB_OPENED_AUDIT", "1")

    project_root = tmp_path / "proj"
    (project_root / ".pollypm").mkdir(parents=True)
    db_path = tmp_path / "disabled.db"

    svc = SQLiteWorkService(db_path=db_path, project_path=project_root)
    try:
        records = _read_central_records(_isolate_audit_home)
        assert [r for r in records if r["event"] == EVENT_WORK_DB_OPENED] == []
        assert not (project_root / ".pollypm" / "audit.jsonl").exists()
    finally:
        svc.close()
