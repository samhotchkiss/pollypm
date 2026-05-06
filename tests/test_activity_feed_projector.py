"""Tests for the activity_feed event projector (lf01).

Covers:
    * FeedEntry projection from the unified ``messages`` table.
    * FeedEntry projection from per-project ``work_transitions`` rows.
    * Noise filter (tick-with-no-decision, poll-unchanged, heartbeat
      snapshot no-op).
    * Back-compat: plain-string messages render as kind+actor fallbacks.
    * Structured messages: JSON-encoded payload overrides summary /
      severity / project.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pollypm.plugins_builtin.activity_feed.handlers.event_projector import (
    EventProjector,
    FeedEntry,
)
from pollypm.plugins_builtin.activity_feed.plugin import build_projector, plugin
from pollypm.store import SQLAlchemyStore
from pollypm.storage.state import StateStore
from pollypm.work.schema import create_work_tables


def _seed_event(
    store: StateStore, *, session: str, event_type: str, message: str,
) -> None:
    """Write an event row via the unified :class:`Store`.

    #342 retired the legacy ``events`` table — the projector now reads
    ``messages WHERE type='event'``. Open a short-lived
    :class:`SQLAlchemyStore` on the same DB path and insert an event
    row whose ``body`` carries the caller-provided ``message`` string
    (the projector treats ``body`` as the free-form payload, exactly
    like the old ``events.message`` column).
    """
    import json as _json

    from sqlalchemy import insert as _insert

    from pollypm.store.schema import messages as _messages

    db_path = store.path
    msg_store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        row = {
            "scope": session,
            "type": "event",
            "tier": "immediate",
            "recipient": "*",
            "sender": session,
            "state": "open",
            "subject": event_type,
            "body": message,
            "payload_json": _json.dumps({"event_type": event_type}),
            "labels": "[]",
        }
        with msg_store.transaction() as conn:
            conn.execute(_insert(_messages), row)
    finally:
        msg_store.close()


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


def test_project_inferred_from_task_ref_in_message(tmp_path: Path) -> None:
    """Regression: alerts emitted by ``task_assignment_notify`` (and
    other supervisor paths) don't carry an explicit ``project`` field
    in the payload, so the activity feed Project column rendered ``—``
    for every such row even when the message body literally named a
    task by ``<project>/<N>`` reference. Infer the project from the
    text as a fallback so the column carries useful info.
    """
    db = tmp_path / "state.db"
    store = StateStore(db)
    _seed_event(
        store,
        session="task_assignment",
        event_type="alert",
        message=(
            "Task polly_remote/12 was routed to the worker role but no "
            "matching session is running."
        ),
    )
    store.close()

    entries = EventProjector(db).project(limit=10)
    assert len(entries) == 1
    assert entries[0].project == "polly_remote"


def test_project_inference_skipped_when_payload_already_carries_project(
    tmp_path: Path,
) -> None:
    """Explicit payload project always wins over body inference — we
    only fall back to scanning the body when nothing else is available.
    """
    from pollypm.plugins_builtin.activity_feed.handlers.event_projector import (
        _project_from_text,
    )
    # Direct unit assertion on the helper. Empty / no-match returns
    # None; a clear ref returns the project key.
    assert _project_from_text("") is None
    assert _project_from_text("no refs here") is None
    assert _project_from_text(
        "Task polly_remote/12 routed elsewhere"
    ) == "polly_remote"
    # Underscored project keys are valid.
    assert _project_from_text(
        "polly_e2e_proj/3 review"
    ) == "polly_e2e_proj"
    # #929 regression — hyphenated project keys must round-trip whole.
    # Before the fix the regex truncated ``blackjack-trainer/3`` to
    # ``trainer`` because ``\b`` matched after the hyphen, and the
    # activity feed surfaced a phantom ``[trainer]`` project.
    assert _project_from_text(
        "[Alert] Task blackjack-trainer/3 was routed to the worker role"
    ) == "blackjack-trainer"
    assert _project_from_text(
        "blackjack-trainer/12 review"
    ) == "blackjack-trainer"


def test_project_from_actor_extracts_role_prefixed_project_key() -> None:
    """Per-project agent sessions follow ``<role>_<project_key>``
    naming — ``worker_pollypm``, ``architect_polly_remote``. When a
    silent_worker_prompt or send_input event has no body ref to grep
    for, the actor name is the next-best signal for the Project
    column.
    """
    from pollypm.plugins_builtin.activity_feed.handlers.event_projector import (
        _project_from_actor,
    )

    assert _project_from_actor("worker_pollypm") == "pollypm"
    assert _project_from_actor("architect_polly_remote") == "polly_remote"
    assert _project_from_actor("architect_booktalk") == "booktalk"
    assert _project_from_actor("reviewer_demo") == "demo"
    assert _project_from_actor("operator_pm_demo") == "demo"
    # #929 — dash-separated session names also occur in the wild
    # (``worker-blackjack-trainer``); strip the role prefix and keep
    # the canonical project key intact, hyphens and all.
    assert (
        _project_from_actor("worker-blackjack-trainer") == "blackjack-trainer"
    )
    assert _project_from_actor("architect-booktalk") == "booktalk"
    # Unknown prefix → return None rather than claim the project is
    # ``assignment``. Generic supervisor senders shouldn't poison the
    # Project column.
    assert _project_from_actor("task_assignment") is None
    assert _project_from_actor("auto_claim_sweep") is None
    assert _project_from_actor("error_log") is None
    # Falsy / shapes without a project key.
    assert _project_from_actor(None) is None
    assert _project_from_actor("") is None
    assert _project_from_actor("worker_") is None  # prefix only, no key
    assert _project_from_actor("worker") is None  # no prefix


def test_project_inferred_from_actor_when_body_has_no_ref(
    tmp_path: Path,
) -> None:
    """End-to-end: silent_worker_prompt events from ``worker_pollypm``
    surface with project=``pollypm`` even though the message body is
    just the verb name and no payload project field is set.
    """
    db = tmp_path / "state.db"
    store = StateStore(db)
    _seed_event(
        store,
        session="worker_pollypm",
        event_type="silent_worker_prompt",
        message="silent_worker_prompt",
    )
    store.close()

    entries = EventProjector(db).project(limit=10)
    assert len(entries) == 1
    assert entries[0].project == "pollypm"


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


def test_historic_fixture_error_alerts_filtered_out(tmp_path: Path) -> None:
    """Known test-fixture error alerts must not pollute the live feed."""
    db = tmp_path / "state.db"
    StateStore(db).close()
    fixture_messages = [
        "Sync adapter boomer failed on create for proj/1",
        "Sync adapter failing failed on create for proj/1",
        "GitHub sync: failed to create issue for proj/1: gh not found",
        "session listener '_boom' raised for x",
        "Plugin bad register_roster hook failed: plugin exploded",
        "HeartbeatRail tick failed; continuing",
        "Rail item workflows.Activity visibility predicate raised — hiding item",
        "Rail item workflows.Activity badge_provider raised — rendering without badge",
    ]
    store = SQLAlchemyStore(f"sqlite:///{db}")
    try:
        for index, message in enumerate(fixture_messages):
            store.upsert_alert(
                "error_log",
                f"critical_error:fixture-{index}",
                "critical",
                message,
            )
        store.upsert_alert(
            "error_log",
            "critical_error:real",
            "critical",
            "SQLite database locked while writing state.db",
        )
    finally:
        store.close()

    entries = EventProjector(db).project(limit=20)
    summaries = [entry.summary for entry in entries]

    assert any("SQLite database locked" in summary for summary in summaries)
    for message in fixture_messages:
        assert all(message not in summary for summary in summaries)


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


def test_missing_work_transitions_table_short_circuits_without_error_log(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state_db = tmp_path / "state.db"
    work_db = tmp_path / "demo" / ".pollypm" / "state.db"
    work_db.parent.mkdir(parents=True)
    StateStore(state_db).close()
    sqlite3.connect(str(work_db)).close()

    caplog.set_level(
        logging.ERROR,
        logger="pollypm.plugins_builtin.activity_feed.handlers.event_projector",
    )

    entries = EventProjector(state_db, [("demo", work_db)]).project(limit=20)

    assert entries == []
    assert "work-db projection failed" not in caplog.text


def test_cross_source_entries_sort_chronologically(tmp_path: Path) -> None:
    """#791: state-store events use SQLite ``YYYY-MM-DD HH:MM:SS`` and
    work_transitions use Python ``isoformat()`` (``YYYY-MM-DDTHH...``).
    A naïve lexical sort orders space (0x20) before ``T`` (0x54), so a
    work-transition that happened *after* a state event lands behind it
    in the feed. The projector must normalize these into a single
    chronological order.
    """
    state_db = tmp_path / "state.db"
    work_db = tmp_path / "demo" / ".pollypm" / "state.db"
    work_db.parent.mkdir(parents=True)

    # State event "older": SQLite-style timestamp at 12:00:00 (with
    # the bare-space separator the SQLite ``CURRENT_TIMESTAMP``
    # default produces). Insert directly via sqlite3 so the literal
    # string round-trips without SQLAlchemy parsing it.
    older_ts = "2026-04-26 12:00:00"
    StateStore(state_db).close()
    raw = sqlite3.connect(str(state_db))
    try:
        raw.execute(
            "INSERT INTO messages (scope, type, tier, recipient, sender, "
            "state, subject, body, payload_json, labels, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "worker", "event", "immediate", "*", "worker",
                "open", "older_event", "older state event",
                json.dumps({"event_type": "older_event"}), "[]",
                older_ts,
            ),
        )
        raw.commit()
    finally:
        raw.close()

    # Work transition "newer": ISO timestamp 30 minutes later.
    newer_ts = "2026-04-26T12:30:00+00:00"
    _seed_work_transition(
        work_db, project="demo", task=1,
        from_state="queued", to_state="in_progress",
        actor="worker-demo-1", ts=newer_ts,
    )

    projector = EventProjector(state_db, [("demo", work_db)])
    entries = projector.project(limit=20)
    # Newer transition must land first under reverse-chronological sort.
    assert len(entries) == 2
    assert entries[0].source == "work_transitions"
    assert entries[1].source == "events"


def test_since_timedelta_filter(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    store = StateStore(db)
    store.close()
    # Seed two rows — one ancient, one fresh. Patch the timestamp on the
    # ancient row in the unified ``messages`` table so the projector's
    # ``since`` filter must exclude it.
    _seed_event(store, session="a", event_type="k", message="ancient")
    msg_store = SQLAlchemyStore(f"sqlite:///{db}")
    try:
        with msg_store.transaction() as conn:
            from sqlalchemy import text as _text

            conn.execute(
                _text(
                    "UPDATE messages SET created_at = :created_at "
                    "WHERE body = :body AND type = 'event'"
                ),
                {
                    "created_at": "2000-01-01T00:00:00+00:00",
                    "body": "ancient",
                },
            )
    finally:
        msg_store.close()
    _seed_event(store, session="a", event_type="k", message="fresh")

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


def test_project_filter_skips_unrelated_work_dbs(tmp_path: Path) -> None:
    state_db = tmp_path / "state.db"
    StateStore(state_db).close()
    calls: list[str] = []

    class RecordingProjector(EventProjector):
        def _project_from_state_store(self, **_kwargs):
            return []

        def _project_from_work_db(self, project_key, work_db, **_kwargs):
            calls.append(project_key)
            return []

    projector = RecordingProjector(
        state_db,
        [
            ("demo", tmp_path / "demo" / ".pollypm" / "state.db"),
            ("other", tmp_path / "other" / ".pollypm" / "state.db"),
        ],
    )

    projector.project(projects=["demo"], limit=10)

    assert calls == ["demo"]


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


def test_plugin_initialize_emits_loaded_event_without_view_side_effects(tmp_path: Path) -> None:
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
        assert row[0] == 0
    finally:
        conn.close()
    assert api.emitted and api.emitted[0][0] == "loaded"


# ---------------------------------------------------------------------------
# #1033 — alert.cleared lifecycle visibility in the activity feed.
# ---------------------------------------------------------------------------


def test_alert_cleared_event_surfaces_in_feed(tmp_path: Path) -> None:
    """A ``clear_alert`` call writes an ``alert.cleared`` event that the
    activity feed surfaces alongside the original alert create.
    """
    db = tmp_path / "state.db"
    store = SQLAlchemyStore(f"sqlite:///{db}")
    try:
        store.upsert_alert(
            session_name="worker_demo",
            alert_type="plan_gate",
            severity="warn",
            message="missing plan",
        )
        store.clear_alert(
            "worker_demo", "plan_gate", who_cleared="auto:test-cycle",
        )
    finally:
        store.close()

    entries = EventProjector(db).project(limit=20)
    kinds = [e.kind for e in entries]
    # Both create (kind=alert) and clear (kind=alert.cleared) should land.
    assert "alert" in kinds
    assert "alert.cleared" in kinds

    cleared = next(e for e in entries if e.kind == "alert.cleared")
    assert cleared.payload.get("who_cleared") == "auto:test-cycle"
    assert cleared.payload.get("alert_type") == "plan_gate"
    # ``alert.cleared`` is good news — render at routine severity so a
    # wall of clears doesn't shout louder than active alerts.
    assert cleared.severity == "routine"
    # Summary should be a human-readable string sourced from the
    # payload (not a `kind on actor` fallback).
    assert "Cleared plan_gate" in cleared.summary


def test_kind_filter_alert_matches_cleared_family(tmp_path: Path) -> None:
    """``--kind alert`` filter expands to the alert lifecycle family so
    a single CLI invocation shows both creates and clears (#1033)."""
    db = tmp_path / "state.db"
    store = SQLAlchemyStore(f"sqlite:///{db}")
    try:
        store.upsert_alert(
            session_name="worker_demo",
            alert_type="plan_gate",
            severity="warn",
            message="missing plan",
        )
        store.clear_alert(
            "worker_demo", "plan_gate", who_cleared="manual:pm-alert-clear",
        )
        # An unrelated event must NOT leak through the alert filter.
        store.record_event(
            scope="worker_demo",
            sender="worker_demo",
            subject="heartbeat",
            payload={},
        )
    finally:
        store.close()

    entries = EventProjector(db).project(kinds=["alert"], limit=20)
    kinds = sorted({e.kind for e in entries})
    assert kinds == ["alert", "alert.cleared"]


def test_clear_alert_no_op_does_not_emit_event(tmp_path: Path) -> None:
    """No-op clears (heartbeat sweep, already-cleared retry) must NOT
    spam the activity feed with phantom ``alert.cleared`` rows."""
    db = tmp_path / "state.db"
    store = SQLAlchemyStore(f"sqlite:///{db}")
    try:
        store.clear_alert("nothing", "never_opened")
    finally:
        store.close()

    entries = EventProjector(db).project(limit=20)
    cleared = [e for e in entries if e.kind == "alert.cleared"]
    assert cleared == []


def test_alert_cleared_via_v1_service_emits_event(tmp_path: Path) -> None:
    """``pm alert clear <id>`` (v1 service path) emits an
    ``alert.cleared`` event whose payload attributes the close to the
    explicit CLI invocation (#1033).
    """
    db = tmp_path / "state.db"
    store = SQLAlchemyStore(f"sqlite:///{db}")
    try:
        store.upsert_alert(
            session_name="worker_demo",
            alert_type="plan_gate",
            severity="warn",
            message="missing plan",
        )
        # Resolve the alert id then close it via the same shape that
        # ``ServiceAPIv1.clear_alert`` uses.
        rows = store.query_messages(
            type="alert", state="open", scope="worker_demo",
        )
        assert rows
        target = rows[0]
        alert_id = int(target["id"])
        store.close_message(alert_id)
        store.record_event(
            scope="worker_demo",
            sender="plan_gate",
            subject="alert.cleared",
            payload={
                "event_type": "alert.cleared",
                "alert_id": alert_id,
                "alert_type": "plan_gate",
                "session_name": "worker_demo",
                "severity": "warn",
                "who_cleared": "manual:pm-alert-clear",
                "summary": (
                    f"Cleared alert #{alert_id} plan_gate on worker_demo"
                    " (manual:pm-alert-clear)"
                ),
                "message": "missing plan",
                "opened_at": str(target.get("created_at") or ""),
            },
        )
    finally:
        store.close()

    entries = EventProjector(db).project(kinds=["alert"], limit=20)
    cleared = [e for e in entries if e.kind == "alert.cleared"]
    assert len(cleared) == 1
    assert cleared[0].payload["who_cleared"] == "manual:pm-alert-clear"
    assert cleared[0].payload["alert_id"] == alert_id
