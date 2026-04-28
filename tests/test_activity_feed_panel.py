"""Tests for activity_feed cockpit panel + rail registration (lf03).

Covers:
    * Plain-text render helpers (relative-time labels, row formatting,
      empty-feed placeholder).
    * ``render_activity_feed_text`` integration with the projector.
    * Badge counter math via ``new_event_count``.
    * Rail registration: the plugin installs one item in the
      ``workflows`` section with the right metadata.
    * Persisting the "last seen" cursor when the rail handler fires.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pollypm.plugin_api.v1 import (
    PluginAPI,
    RailAPI,
    RailContext,
    RailRegistry,
)
from pollypm.plugins_builtin.activity_feed import plugin as activity_plugin
from pollypm.plugins_builtin.activity_feed.cockpit.feed_panel import (
    compute_project_column_width,
    format_entry_row,
    format_relative_time,
    new_event_count,
    render_activity_feed_text,
    render_entries_as_text,
)
from pollypm.plugins_builtin.activity_feed.handlers.event_projector import (
    EventProjector,
    FeedEntry,
)
from pollypm.plugins_builtin.activity_feed.summaries import activity_summary
from pollypm.storage.state import StateStore


def _seed_event(state_db: Path, session: str, event_type: str, message: str) -> None:
    """Insert a ``type='event'`` row on the unified ``messages`` table.

    #342 retired the legacy ``events`` table; panel tests previously
    called ``StateStore.record_event`` which no longer feeds the
    projector. Seed via :class:`SQLAlchemyStore` so
    :class:`EventProjector` sees the row.
    """
    import json as _json

    from sqlalchemy import insert as _insert

    from pollypm.store import SQLAlchemyStore
    from pollypm.store.schema import messages as _messages

    msg_store = SQLAlchemyStore(f"sqlite:///{state_db}")
    try:
        with msg_store.transaction() as conn:
            conn.execute(
                _insert(_messages),
                {
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
                },
            )
    finally:
        msg_store.close()


# ---------------------------------------------------------------------------
# Plain-text render helpers.
# ---------------------------------------------------------------------------


def test_format_relative_time_seconds() -> None:
    now = datetime(2026, 4, 16, 12, 0, 0, tzinfo=UTC)
    ts = (now - timedelta(seconds=30)).isoformat()
    assert format_relative_time(ts, now=now) == "30s ago"


def test_format_relative_time_minutes() -> None:
    now = datetime(2026, 4, 16, 12, 0, 0, tzinfo=UTC)
    ts = (now - timedelta(minutes=5)).isoformat()
    assert format_relative_time(ts, now=now) == "5m ago"


def test_format_relative_time_hours() -> None:
    now = datetime(2026, 4, 16, 12, 0, 0, tzinfo=UTC)
    ts = (now - timedelta(hours=2)).isoformat()
    assert format_relative_time(ts, now=now) == "2h ago"


def test_format_relative_time_days() -> None:
    now = datetime(2026, 4, 16, 12, 0, 0, tzinfo=UTC)
    ts = (now - timedelta(days=3)).isoformat()
    assert format_relative_time(ts, now=now) == "3d ago"


def test_format_relative_time_future() -> None:
    """Clock skew → 'just now' rather than negative deltas."""
    now = datetime(2026, 4, 16, 12, 0, 0, tzinfo=UTC)
    ts = (now + timedelta(seconds=5)).isoformat()
    assert format_relative_time(ts, now=now) == "just now"


def test_format_relative_time_unparseable() -> None:
    assert format_relative_time("nope") == "nope"


def test_format_entry_row_critical_prefix() -> None:
    entry = FeedEntry(
        id="evt:1",
        timestamp=datetime.now(UTC).isoformat(),
        project="polly",
        kind="alert",
        actor="operator",
        subject="operator",
        verb="alerted",
        summary="Pane died",
        severity="critical",
    )
    row = format_entry_row(entry)
    assert row.startswith("!")
    assert "polly" in row
    assert "operator" in row
    assert "Pane died" in row


def test_format_entry_row_routine_no_prefix() -> None:
    entry = FeedEntry(
        id="evt:1",
        timestamp=datetime.now(UTC).isoformat(),
        project="demo",
        kind="event",
        actor="worker",
        subject="task",
        verb="updated",
        summary="touched file",
        severity="routine",
    )
    row = format_entry_row(entry)
    assert not row.startswith("!")


def test_render_entries_as_text_empty() -> None:
    rendered = render_entries_as_text([])
    assert "No activity yet" in rendered


def test_render_entries_as_text_populated() -> None:
    now = datetime.now(UTC)
    entries = [
        FeedEntry(
            id=f"evt:{i}",
            timestamp=(now - timedelta(minutes=i)).isoformat(),
            project="polly",
            kind="event",
            actor="operator",
            subject="op",
            verb="happened",
            summary=f"thing {i}",
            severity="routine",
        )
        for i in range(3)
    ]
    rendered = render_entries_as_text(entries)
    assert "thing 0" in rendered
    assert "thing 2" in rendered
    assert rendered.count("\n") == 2  # three rows → two newlines


# ---------------------------------------------------------------------------
# Regression tests for #929 — long project keys ("blackjack-trainer") used to
# render as ``[trainer]`` because the project column wasn't auto-fit and the
# upstream regex truncated the key. The CLI now auto-fits to the widest active
# project key so every row aligns and the canonical key survives rendering.
# ---------------------------------------------------------------------------


def _entry(project: str | None, *, idx: int = 0, summary: str = "x") -> FeedEntry:
    """Compact FeedEntry fixture for column-width tests."""
    return FeedEntry(
        id=f"evt:{idx}",
        timestamp=datetime.now(UTC).isoformat(),
        project=project,
        kind="event",
        actor="actor",
        subject="subj",
        verb="happened",
        summary=summary,
        severity="routine",
    )


def test_format_entry_row_preserves_full_long_project_key() -> None:
    """``blackjack-trainer`` must render in full, never as ``trainer`` (#929)."""
    row = format_entry_row(_entry("blackjack-trainer"))
    assert "[blackjack-trainer]" in row
    # The misfire would have produced ``[trainer]`` with no leading text.
    assert "[trainer]" not in row


def test_compute_project_column_width_picks_widest_key() -> None:
    entries = [
        _entry("booktalk", idx=0),
        _entry("polly_remote", idx=1),
        _entry("blackjack-trainer", idx=2),
        _entry("russell", idx=3),
    ]
    assert compute_project_column_width(entries) == len("blackjack-trainer")


def test_compute_project_column_width_handles_empty() -> None:
    """No entries → minimum width fallback (no crash, sane default)."""
    assert compute_project_column_width([]) == 1


def test_compute_project_column_width_handles_missing_project() -> None:
    """Empty / None project keys fall back to ``-`` for column math."""
    assert compute_project_column_width([_entry(None)]) == 1
    assert compute_project_column_width([_entry("")]) == 1


def test_render_entries_as_text_aligns_mixed_length_project_keys() -> None:
    """Auto-fit: every row shares the same project-column width."""
    now = datetime.now(UTC)
    entries = [
        FeedEntry(
            id=f"evt:{i}",
            timestamp=(now - timedelta(minutes=i)).isoformat(),
            project=key,
            kind="event",
            actor="actor",
            subject="s",
            verb="v",
            summary=f"row {i}",
            severity="routine",
        )
        for i, key in enumerate(
            ["booktalk", "polly_remote", "blackjack-trainer", "russell"]
        )
    ]
    rendered = render_entries_as_text(entries)
    lines = rendered.splitlines()
    assert len(lines) == 4
    # The canonical long key survives in full…
    assert any("[blackjack-trainer]" in line for line in lines)
    assert all("[trainer]" not in line for line in lines)
    # …and every row uses the same column width, so the actor bracket
    # opens at the same byte offset on every line.
    actor_offsets = [line.index("[actor]") for line in lines]
    assert len(set(actor_offsets)) == 1, (
        f"project column not aligned across rows: {actor_offsets!r}"
    )


def test_render_entries_as_text_empty_project_falls_back_gracefully() -> None:
    """A row with no project key renders ``[-]`` (padded) and doesn't crash."""
    rendered = render_entries_as_text([_entry(None, summary="orphan")])
    assert "orphan" in rendered
    assert "[-]" in rendered  # single-row width matches the dash placeholder.


# ---------------------------------------------------------------------------
# render_activity_feed_text integration.
# ---------------------------------------------------------------------------


class _FakeProject:
    def __init__(self, state_db: Path) -> None:
        self.state_db = state_db


class _FakeConfig:
    def __init__(self, state_db: Path) -> None:
        self.project = _FakeProject(state_db)
        self.projects = {}


def test_render_activity_feed_text_no_config() -> None:
    rendered = render_activity_feed_text(None)
    assert "No state store configured" in rendered


def test_render_activity_feed_text_empty_db(tmp_path: Path) -> None:
    state_db = tmp_path / "state.db"
    StateStore(state_db).close()
    config = _FakeConfig(state_db)
    rendered = render_activity_feed_text(config)
    assert "Activity Feed" in rendered
    assert "No activity yet" in rendered


def test_render_activity_feed_text_with_events(tmp_path: Path) -> None:
    state_db = tmp_path / "state.db"
    StateStore(state_db).close()
    _seed_event(
        state_db, "operator", "alert",
        activity_summary(
            summary="Disk full",
            severity="critical",
            verb="alerted",
            subject="disk",
        ),
    )
    rendered = render_activity_feed_text(_FakeConfig(state_db))
    assert "Disk full" in rendered
    assert "alerted" in rendered


# ---------------------------------------------------------------------------
# Badge provider.
# ---------------------------------------------------------------------------


def test_new_event_count_counts_only_newer(tmp_path: Path) -> None:
    state_db = tmp_path / "state.db"
    StateStore(state_db).close()
    _seed_event(state_db, "a", "k", "first")
    _seed_event(state_db, "a", "k", "second")
    _seed_event(state_db, "a", "k", "third")

    projector = EventProjector(state_db)
    # With no cursor we see all three.
    assert new_event_count(projector, last_seen_id=None) == 3
    # Fetch the oldest id — the first row of the projection is newest,
    # the last is oldest.
    entries = projector.project(limit=10)
    oldest_id = int(entries[-1].id.split(":")[1])
    # Cursor at the oldest → only the two newer ones.
    assert new_event_count(projector, last_seen_id=oldest_id) == 2


def test_new_event_count_ignores_heartbeat_noise(tmp_path: Path) -> None:
    state_db = tmp_path / "state.db"
    StateStore(state_db).close()
    _seed_event(state_db, "heartbeat", "heartbeat", "heartbeat")
    _seed_event(state_db, "heartbeat", "token_ledger", "token_ledger")
    _seed_event(state_db, "worker", "task.done", "Shipped the task")

    projector = EventProjector(state_db)

    assert new_event_count(projector, last_seen_id=None) == 1


def test_new_event_count_none_projector() -> None:
    assert new_event_count(None, last_seen_id=None) == 0


def _seed_alert(
    state_db: Path, session: str, alert_type: str, message: str,
) -> None:
    """Insert one ``type='alert'`` row directly so we can prove the
    projector dedupes repeats (#867) without relying on upsert semantics."""
    import json as _json

    from sqlalchemy import insert as _insert

    from pollypm.store import SQLAlchemyStore
    from pollypm.store.schema import messages as _messages

    msg_store = SQLAlchemyStore(f"sqlite:///{state_db}")
    try:
        with msg_store.transaction() as conn:
            conn.execute(
                _insert(_messages),
                {
                    "scope": session,
                    "type": "alert",
                    "tier": "immediate",
                    "recipient": "user",
                    "sender": alert_type,
                    "state": "open",
                    "subject": message,
                    "body": "",
                    "payload_json": _json.dumps({"severity": "warn"}),
                    "labels": "[]",
                },
            )
    finally:
        msg_store.close()


def test_event_projector_dedupes_repeated_alerts(tmp_path: Path) -> None:
    """The same plan_gate alert recurring N times collapses to one row (#867)."""
    state_db = tmp_path / "state.db"
    StateStore(state_db).close()

    for _ in range(5):
        _seed_alert(
            state_db,
            "plan_gate-demo",
            "plan_missing",
            "Project 'demo' has no approved plan yet — press c to plan it.",
        )

    projector = EventProjector(state_db)
    entries = projector.project(limit=50)
    alert_entries = [e for e in entries if e.kind == "alert"]
    assert len(alert_entries) == 1, (
        f"expected 1 deduped alert entry, got {len(alert_entries)}"
    )


# ---------------------------------------------------------------------------
# Rail registration.
# ---------------------------------------------------------------------------


def test_initialize_registers_activity_rail_item(tmp_path: Path) -> None:
    state_db = tmp_path / "state.db"
    StateStore(state_db).close()
    config = _FakeConfig(state_db)
    registry = RailRegistry()
    rail_api = RailAPI(plugin_name="activity_feed", registry=registry)
    api = PluginAPI(
        plugin_name="activity_feed",
        roster_api=None,
        jobs_api=None,
        host=None,
        config=config,
        state_store=None,
        rail_api=rail_api,
    )

    activity_plugin.plugin.initialize(api)

    items = registry.items_for_section("workflows")
    activity = [r for r in items if r.label == "Activity"]
    assert len(activity) == 1
    reg = activity[0]
    assert reg.index == 30
    assert reg.key == "activity"
    # State provider returns the monitor hint even with no extras.
    assert reg.state_provider(RailContext()) == "watch"


def test_badge_provider_counts_unread(tmp_path: Path) -> None:
    state_db = tmp_path / "state.db"
    StateStore(state_db).close()
    _seed_event(state_db, "a", "k", "first")
    _seed_event(state_db, "a", "k", "second")

    config = _FakeConfig(state_db)
    registry = RailRegistry()
    rail_api = RailAPI(plugin_name="activity_feed", registry=registry)
    api = PluginAPI(
        plugin_name="activity_feed", roster_api=None, jobs_api=None,
        host=None, config=config, state_store=None, rail_api=rail_api,
    )
    activity_plugin.plugin.initialize(api)

    reg = registry.items_for_section("workflows")[0]
    badge = reg.badge_provider(RailContext())
    # #873: badge is now a "<n> new" string so the user can tell the
    # number is "events since last visit", not an unlabelled total.
    assert badge == "2 new"


def test_handler_persists_last_seen_cursor(tmp_path: Path) -> None:
    state_db = tmp_path / "state.db"
    StateStore(state_db).close()
    _seed_event(state_db, "a", "k", "first")

    config = _FakeConfig(state_db)
    registry = RailRegistry()
    rail_api = RailAPI(plugin_name="activity_feed", registry=registry)
    api = PluginAPI(
        plugin_name="activity_feed", roster_api=None, jobs_api=None,
        host=None, config=config, state_store=None, rail_api=rail_api,
    )
    activity_plugin.plugin.initialize(api)

    reg = registry.items_for_section("workflows")[0]
    # Handler is a no-op without a router — passing a stub router that
    # ignores route_selected is enough to exercise the cursor save.
    class _StubRouter:
        routed: list[str] = []

        def route_selected(self, key: str) -> None:
            self.routed.append(key)

    router = _StubRouter()
    reg.handler(RailContext(extras={"router": router}))
    assert router.routed == ["activity"]
    # Badge should now read 0 — cursor caught up.
    assert reg.badge_provider(RailContext()) is None
