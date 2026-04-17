"""Tests for the activity_feed event projector (lf01).

Covers:
    * FeedEntry projection from the StateStore events table via the
      ``activity_events`` view.
    * FeedEntry projection from per-project ``work_transitions`` rows.
    * Noise filter (tick-with-no-decision, poll-unchanged, heartbeat
      snapshot no-op).
    * Back-compat: plain-string messages render as kind+actor fallbacks.
    * Structured messages: JSON-encoded payload overrides summary /
      severity / project.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pollypm.plugins_builtin.activity_feed.handlers.event_projector import (
    EventProjector,
    FeedEntry,
    ensure_activity_events_view,
)
from pollypm.plugins_builtin.activity_feed.plugin import build_projector, plugin
from pollypm.storage.state import StateStore
from pollypm.work.schema import create_work_tables


def _seed_event(store: StateStore, *, session: str, event_type: str, message: str) -> None:
    store.record_event(session, event_type, message)


def _seed_work_transition(
    db_path: Path,
    *,
    project: str,
    task: int,
    from_state: str,
    to_state: str,
    actor: str = "worker",
    reason: str | None = None,
    ts: str | None = None,
) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        create_work_tables(conn)
        created_at = ts or datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT INTO work_tasks (project, task_number, title, type, "
            "flow_template_id, created_at, created_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (project, task, "seed", "task", "default", created_at, "test", created_at),
        )
        conn.execute(
            "INSERT INTO work_transitions (task_project, task_number, "
            "from_state, to_state, actor, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (project, task, from_state, to_state, actor, reason, created_at),
        )
        conn.commit()
    finally:
        conn.close()


def test_ensure_activity_events_view_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    store = StateStore(db)
    conn = sqlite3.connect(str(db))
    try:
        ensure_activity_events_view(conn)
        ensure_activity_events_view(conn)  # second call must not raise
        row = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='view' AND name='activity_events'"
        ).fetchone()
        assert row[0] == 1
    finally:
        conn.close()
        store.close()


def test_project_from_state_events(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    store = StateStore(db)
    _seed_event(store, session="worker-demo", event_type="start", message="Started worker")
    _seed_event(store, session="worker-demo", event_type="alert", message="disk full")
    store.close()

    projector = EventProjector(db)
    entries = projector.project(limit=10)

    assert len(entries) == 2
    kinds = {entry.kind for entry in entries}
    assert {"start", "alert"} == kinds
    alert = next(e for e in entries if e.kind == "alert")
    # Alerts surface with recommendation severity until lf02 promotes
    # them to the structured form (where severity is explicit).
    assert alert.severity == "recommendation"
    assert alert.actor == "worker-demo"
    assert alert.summary == "disk full"
    assert alert.source == "events"


def test_project_reverse_chronological(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    store = StateStore(db)
    _seed_event(store, session="a", event_type="k1", message="first")
    _seed_event(store, session="a", event_type="k2", message="second")
    _seed_event(store, session="a", event_type="k3", message="third")
    store.close()

    entries = EventProjector(db).project(limit=10)
    summaries = [e.summary for e in entries]
    assert summaries == ["third", "second", "first"]


def test_since_id_filter(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    store = StateStore(db)
    _seed_event(store, session="a", event_type="k", message="one")
    _seed_event(store, session="a", event_type="k", message="two")
    _seed_event(store, session="a", event_type="k", message="three")
    store.close()

    projector = EventProjector(db)
    first = projector.project(limit=10)
    # Grab the numeric id of the oldest entry (last in the reverse list)
    # then ask for only newer rows.
    oldest_id = int(first[-1].id.split(":")[1])
    newer = projector.project(since_id=oldest_id, limit=10)
    summaries = [e.summary for e in newer]
    assert summaries == ["three", "two"]


def test_noise_filtered_out(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    store = StateStore(db)
    _seed_event(store, session="a", event_type="heartbeat", message="Recorded heartbeat snapshot")
    _seed_event(store, session="a", event_type="tick_noop", message="tick")
    _seed_event(store, session="a", event_type="poll_unchanged", message="")
    _seed_event(store, session="a", event_type="alert", message="real alert")
    store.close()

    entries = EventProjector(db).project(limit=10)
    kinds = {e.kind for e in entries}
    # Only the alert survives.
    assert kinds == {"alert"}


def test_structured_summary_overrides_fallback(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    store = StateStore(db)
    payload = {
        "summary": "Worker landed commit abc123",
        "severity": "routine",
        "verb": "committed",
        "subject": "polly/42",
        "project": "polly",
        "sha": "abc123",
    }
    _seed_event(store, session="worker-polly-42", event_type="commit", message=json.dumps(payload))
    store.close()

    entry = EventProjector(db).project(limit=5)[0]
    assert entry.summary == "Worker landed commit abc123"
    assert entry.verb == "committed"
    assert entry.subject == "polly/42"
    assert entry.project == "polly"
    assert entry.payload.get("sha") == "abc123"


def test_work_transition_projection(tmp_path: Path) -> None:
    state_db = tmp_path / "state.db"
    work_db = tmp_path / "demo" / ".pollypm" / "state.db"
    work_db.parent.mkdir(parents=True)
    StateStore(state_db).close()
    _seed_work_transition(
        work_db, project="demo", task=7,
        from_state="queued", to_state="in_progress",
        actor="worker-demo-7",
    )
    _seed_work_transition(
        work_db, project="demo", task=8,
        from_state="in_progress", to_state="blocked",
        actor="worker-demo-8", reason="missing spec",
    )

    projector = EventProjector(state_db, [("demo", work_db)])
    entries = projector.project(limit=20)
    assert len(entries) == 2

    by_task = {e.subject: e for e in entries}
    assert "demo/7" in by_task
    assert "demo/8" in by_task

    t7 = by_task["demo/7"]
    assert t7.kind == "task_transition"
    assert t7.severity == "routine"
    assert "queued \u2192 in_progress" in t7.summary
    assert t7.source == "work_transitions"

    t8 = by_task["demo/8"]
    # blocked is elevated to recommendation severity.
    assert t8.severity == "recommendation"
    assert "missing spec" in t8.summary


def test_since_timedelta_filter(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    store = StateStore(db)
    # Seed two rows — one ancient, one fresh. Patch the timestamp on the
    # ancient row to back-date it past the 1-hour window.
    _seed_event(store, session="a", event_type="k", message="ancient")
    store.execute(
        "UPDATE events SET created_at = ? WHERE message = ?",
        ("2000-01-01T00:00:00+00:00", "ancient"),
    )
    store.commit()
    _seed_event(store, session="a", event_type="k", message="fresh")
    store.close()

    entries = EventProjector(db).project(
        since=timedelta(hours=1), limit=10,
    )
    summaries = {e.summary for e in entries}
    assert "fresh" in summaries
    assert "ancient" not in summaries


def test_filter_by_kind_and_project(tmp_path: Path) -> None:
    state_db = tmp_path / "state.db"
    work_db = tmp_path / "demo" / ".pollypm" / "state.db"
    work_db.parent.mkdir(parents=True)
    store = StateStore(state_db)
    _seed_event(store, session="polly", event_type="session", message=json.dumps(
        {"summary": "polly started", "project": "polly"},
    ))
    store.close()
    _seed_work_transition(
        work_db, project="demo", task=1, from_state="queued", to_state="in_progress",
    )

    projector = EventProjector(state_db, [("demo", work_db)])

    only_demo = projector.project(projects=["demo"], limit=10)
    assert {e.project for e in only_demo} == {"demo"}

    only_sessions = projector.project(kinds=["session"], limit=10)
    assert {e.kind for e in only_sessions} == {"session"}


def test_feedentry_as_dict_round_trip() -> None:
    entry = FeedEntry(
        id="evt:1",
        timestamp="2026-04-16T00:00:00+00:00",
        project="polly",
        kind="alert",
        actor="operator",
        subject="operator",
        verb="alert",
        summary="Something",
        severity="critical",
        payload={"a": 1},
    )
    data = entry.as_dict()
    assert data["severity"] == "critical"
    assert data["payload"] == {"a": 1}
    assert data["source"] == "events"


def test_build_projector_returns_none_without_config() -> None:
    assert build_projector(None) is None


def test_build_projector_uses_config(tmp_path: Path) -> None:
    class _Project:
        state_db = tmp_path / "state.db"

    class _Config:
        project = _Project()
        projects = {}

    StateStore(tmp_path / "state.db").close()
    projector = build_projector(_Config())
    assert projector is not None
    # Empty DB → empty projection.
    assert projector.project(limit=5) == []


def test_plugin_initialize_installs_view(tmp_path: Path) -> None:
    class _Project:
        state_db = tmp_path / "state.db"

    class _Config:
        project = _Project()
        projects = {}

    StateStore(tmp_path / "state.db").close()

    class _API:
        def __init__(self) -> None:
            self.config = _Config()
            self.emitted: list[tuple[str, dict]] = []

        def emit_event(self, name: str, payload=None) -> None:
            self.emitted.append((name, payload or {}))

    api = _API()
    plugin.initialize(api)

    conn = sqlite3.connect(str(tmp_path / "state.db"))
    try:
        row = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='view' AND name='activity_events'"
        ).fetchone()
        assert row[0] == 1
    finally:
        conn.close()
    assert api.emitted and api.emitted[0][0] == "loaded"
