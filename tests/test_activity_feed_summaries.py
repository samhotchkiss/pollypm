"""Tests for activity_summary helper + emission-site integration (lf02).

Covers:
    * ``activity_summary`` produces JSON that the projector decodes back
      into a structured FeedEntry.
    * Unknown severities are coerced to ``routine``.
    * Emission sites in ``service_api.v1.PollyPMService.raise_alert``
      and friends now pack structured payloads by default.
"""

from __future__ import annotations

import json

import pytest

from pollypm.plugins_builtin.activity_feed.summaries import activity_summary
from pollypm.plugins_builtin.activity_feed.handlers.event_projector import (
    _parse_message,
)


def test_activity_summary_minimal() -> None:
    raw = activity_summary(summary="hello")
    data = json.loads(raw)
    assert data == {"summary": "hello", "severity": "routine"}


def test_activity_summary_all_fields() -> None:
    raw = activity_summary(
        summary="task demo/5 queued",
        severity="recommendation",
        verb="queued",
        subject="demo/5",
        project="demo",
        task_number=5,
    )
    data = json.loads(raw)
    assert data == {
        "summary": "task demo/5 queued",
        "severity": "recommendation",
        "verb": "queued",
        "subject": "demo/5",
        "project": "demo",
        "task_number": 5,
    }


def test_activity_summary_coerces_unknown_severity() -> None:
    raw = activity_summary(summary="x", severity="nuclear")
    assert json.loads(raw)["severity"] == "routine"


def test_activity_summary_roundtrips_through_parser() -> None:
    raw = activity_summary(
        summary="Plugin foo loaded",
        severity="routine",
        verb="loaded",
        subject="foo",
    )
    parsed = _parse_message(raw)
    assert parsed.summary == "Plugin foo loaded"
    assert parsed.severity == "routine"
    assert parsed.verb == "loaded"
    assert parsed.subject == "foo"


def test_plain_string_not_parsed_as_structured() -> None:
    parsed = _parse_message("Recorded heartbeat snapshot")
    assert parsed.summary is None
    assert parsed.severity is None


def test_activity_summary_skips_none_extras() -> None:
    raw = activity_summary(summary="x", ignore_me=None)
    data = json.loads(raw)
    assert "ignore_me" not in data


def test_structured_emission_surfaces_as_feedentry(tmp_path) -> None:
    """End-to-end: write a structured message, project it, see fields."""
    import json as _json

    from sqlalchemy import insert as _insert

    from pollypm.plugins_builtin.activity_feed.handlers.event_projector import (
        EventProjector,
    )
    from pollypm.store import SQLAlchemyStore
    from pollypm.store.schema import messages as _messages
    from pollypm.storage.state import StateStore

    db = tmp_path / "state.db"
    StateStore(db).close()

    payload = activity_summary(
        summary="Plugin foo errored",
        severity="critical",
        verb="errored",
        subject="foo",
    )
    # #342 routes events through ``messages``; the projector reads
    # ``body`` as the free-form message payload.
    msg_store = SQLAlchemyStore(f"sqlite:///{db}")
    try:
        with msg_store.transaction() as conn:
            conn.execute(
                _insert(_messages),
                {
                    "scope": "plugin",
                    "type": "event",
                    "tier": "immediate",
                    "recipient": "*",
                    "sender": "plugin",
                    "state": "open",
                    "subject": "plugin_error",
                    "body": payload,
                    "payload_json": _json.dumps({"event_type": "plugin_error"}),
                    "labels": "[]",
                },
            )
    finally:
        msg_store.close()

    entries = EventProjector(db).project(limit=5)
    assert len(entries) == 1
    assert entries[0].severity == "critical"
    assert entries[0].summary == "Plugin foo errored"
    assert entries[0].verb == "errored"
    assert entries[0].subject == "foo"
