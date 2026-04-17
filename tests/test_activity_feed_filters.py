"""Tests for activity_feed filters + detail view (lf04).

Covers:
    * FeedFilter construction + describe().
    * apply_filter AND composition across project / kind / actor /
      time-window.
    * render_entry_detail — absolute + relative timestamp, payload
      JSON, task / session link hints.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pollypm.plugins_builtin.activity_feed.cockpit.feed_panel import (
    FeedFilter,
    apply_filter,
    render_entry_detail,
)
from pollypm.plugins_builtin.activity_feed.handlers.event_projector import (
    FeedEntry,
)


NOW = datetime(2026, 4, 16, 12, 0, 0, tzinfo=UTC)


def _entry(
    *,
    eid: str = "evt:1",
    project: str | None = "polly",
    kind: str = "alert",
    actor: str = "operator",
    subject: str | None = "operator",
    ts: datetime | None = None,
    severity: str = "recommendation",
    summary: str = "Something happened",
    payload: dict | None = None,
    source: str = "events",
) -> FeedEntry:
    return FeedEntry(
        id=eid,
        timestamp=(ts or NOW).isoformat(),
        project=project,
        kind=kind,
        actor=actor,
        subject=subject,
        verb=kind,
        summary=summary,
        severity=severity,
        payload=payload or {},
        source=source,
    )


# ---------------------------------------------------------------------------
# FeedFilter.
# ---------------------------------------------------------------------------


def test_feed_filter_default_is_empty() -> None:
    f = FeedFilter()
    assert f.is_empty()
    assert f.describe() == "all activity"


def test_feed_filter_describes_fields() -> None:
    f = FeedFilter(
        projects=("polly",),
        kinds=("alert",),
        actors=("operator",),
        time_window="hour",
    )
    desc = f.describe()
    assert "project=polly" in desc
    assert "kind=alert" in desc
    assert "actor=operator" in desc
    assert "window=hour" in desc


def test_feed_filter_with_project_replaces() -> None:
    f = FeedFilter(projects=("a",)).with_project("b")
    assert f.projects == ("b",)
    # Passing None clears it.
    assert FeedFilter(projects=("a",)).with_project(None).projects == ()


def test_feed_filter_time_window_falls_back_to_all() -> None:
    f = FeedFilter().with_time_window("bogus")
    assert f.time_window == "all"


# ---------------------------------------------------------------------------
# apply_filter composition.
# ---------------------------------------------------------------------------


def test_apply_filter_empty_passes_all() -> None:
    entries = [_entry(eid="evt:1"), _entry(eid="evt:2", project="demo")]
    assert len(apply_filter(entries, FeedFilter())) == 2


def test_apply_filter_project_and_kind_and_actor_compose() -> None:
    entries = [
        _entry(eid="evt:1", project="polly", kind="alert", actor="operator"),
        _entry(eid="evt:2", project="demo", kind="alert", actor="operator"),
        _entry(eid="evt:3", project="polly", kind="nudge", actor="operator"),
        _entry(eid="evt:4", project="polly", kind="alert", actor="reviewer"),
    ]
    f = FeedFilter(projects=("polly",), kinds=("alert",), actors=("operator",))
    kept = apply_filter(entries, f)
    assert [e.id for e in kept] == ["evt:1"]


def test_apply_filter_time_window(monkeypatch) -> None:
    """Hour window drops entries older than 1h from 'now'."""
    import pollypm.plugins_builtin.activity_feed.cockpit.feed_panel as panel_mod

    fixed_now = datetime(2026, 4, 16, 12, 0, 0, tzinfo=UTC)

    class _FixedDT:
        @staticmethod
        def now(tz=UTC):
            return fixed_now

        @staticmethod
        def fromisoformat(s):
            return datetime.fromisoformat(s)

    monkeypatch.setattr(panel_mod, "datetime", _FixedDT)

    entries = [
        _entry(eid="evt:1", ts=fixed_now - timedelta(minutes=30)),
        _entry(eid="evt:2", ts=fixed_now - timedelta(hours=3)),
    ]
    kept = apply_filter(entries, FeedFilter(time_window="hour"))
    assert [e.id for e in kept] == ["evt:1"]


def test_apply_filter_unparseable_timestamp_drops_when_windowed() -> None:
    """Bad timestamps get dropped under a time filter, not raised."""
    bad_entry = FeedEntry(
        id="evt:bad",
        timestamp="not a real timestamp",
        project="polly",
        kind="alert",
        actor="operator",
        subject="op",
        verb="alert",
        summary="weird",
        severity="routine",
    )
    kept = apply_filter([bad_entry], FeedFilter(time_window="hour"))
    assert kept == []


# ---------------------------------------------------------------------------
# render_entry_detail.
# ---------------------------------------------------------------------------


def test_render_entry_detail_core_fields() -> None:
    entry = _entry(
        eid="evt:42",
        kind="alert",
        project="polly",
        actor="operator",
        subject="operator",
        severity="critical",
        summary="Pane died",
        payload={"reason": "oom"},
    )
    text = render_entry_detail(entry, now=NOW)
    assert "evt:42" in text
    assert NOW.isoformat() in text
    assert "polly" in text
    assert "operator" in text
    assert "Pane died" in text
    assert "critical" in text
    # Payload is pretty-printed.
    assert '"reason": "oom"' in text


def test_render_entry_detail_task_link() -> None:
    entry = _entry(
        eid="wt:demo/5:1",
        kind="task_transition",
        project="demo",
        actor="worker-demo-5",
        subject="demo/5",
        payload={"task_project": "demo", "task_number": 5, "from_state": "queued", "to_state": "in_progress"},
        source="work_transitions",
    )
    text = render_entry_detail(entry, now=NOW)
    assert "Related task: project:demo:task:5" in text


def test_render_entry_detail_session_link_for_events() -> None:
    entry = _entry(
        eid="evt:7",
        kind="nudge",
        actor="worker-polly-3",
        source="events",
    )
    text = render_entry_detail(entry, now=NOW)
    assert "Related session: worker-polly-3" in text
