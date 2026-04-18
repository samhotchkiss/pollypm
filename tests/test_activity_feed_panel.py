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


def test_new_event_count_none_projector() -> None:
    assert new_event_count(None, last_seen_id=None) == 0


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
    assert badge == 2


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
