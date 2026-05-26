"""Audit coverage for session pause/resume admin routes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pollypm.audit.log import (
    EVENT_SESSION_PAUSE_PAUSED,
    EVENT_SESSION_PAUSE_REFUSED,
    EVENT_SESSION_PAUSE_RESUMED,
)
from pollypm.models import ProviderKind, SessionConfig
from pollypm.session_paused import _reset_skip_throttle_for_tests


@pytest.fixture(autouse=True)
def _reset_pause_audit_state() -> None:
    _reset_skip_throttle_for_tests()


@pytest.fixture
def configured_session(api_config, project_root: Path) -> str:
    api_config.sessions["operator"] = SessionConfig(
        name="operator",
        role="operator-pm",
        provider=ProviderKind.CODEX,
        account="codex_primary",
        cwd=project_root,
        project="myproj",
        window_name="operator",
    )
    return "operator"


def _audit_records(audit_home: Path, project: str = "PollyPM") -> list[dict]:
    path = audit_home / f"{project}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_pause_session_emits_operator_audit_event_without_deduping(
    client,
    auth_headers,
    audit_home,
    configured_session: str,
) -> None:
    payload = {"actor": "sam", "reason": "maintenance window"}

    first = client.post(
        f"/api/v1/sessions/{configured_session}/pause",
        headers=auth_headers,
        json=payload,
    )
    second = client.post(
        f"/api/v1/sessions/{configured_session}/pause",
        headers=auth_headers,
        json=payload,
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    pause_events = [
        row
        for row in _audit_records(audit_home)
        if row["event"] == EVENT_SESSION_PAUSE_PAUSED
    ]
    assert len(pause_events) == 2
    assert [row["actor"] for row in pause_events] == ["sam", "sam"]
    for row in pause_events:
        assert row["subject"] == configured_session
        assert row["status"] == "ok"
        assert row["metadata"] == {
            "session_name": configured_session,
            "actor": "sam",
            "reason": "maintenance window",
            "paused_count_after": 1,
        }


def test_resume_session_emits_operator_audit_event(
    client,
    auth_headers,
    audit_home,
    configured_session: str,
) -> None:
    pause = client.post(
        f"/api/v1/sessions/{configured_session}/pause",
        headers=auth_headers,
        json={"actor": "sam", "reason": "prepare resume test"},
    )
    assert pause.status_code == 200, pause.text

    response = client.post(
        f"/api/v1/sessions/{configured_session}/resume",
        headers=auth_headers,
        json={"actor": "sam", "reason": "work can continue"},
    )

    assert response.status_code == 200, response.text
    resume_events = [
        row
        for row in _audit_records(audit_home)
        if row["event"] == EVENT_SESSION_PAUSE_RESUMED
    ]
    assert len(resume_events) == 1
    event = resume_events[0]
    assert event["subject"] == configured_session
    assert event["actor"] == "sam"
    assert event["status"] == "ok"
    assert event["metadata"] == {
        "session_name": configured_session,
        "actor": "sam",
        "reason": "work can continue",
        "paused_count_after": 0,
    }


def test_pause_session_emits_refused_audit_event_for_unreadable_marker(
    api_config,
    client,
    auth_headers,
    audit_home,
    configured_session: str,
) -> None:
    marker = api_config.project.base_dir / "paused-sessions.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{not json")

    response = client.post(
        f"/api/v1/sessions/{configured_session}/pause",
        headers=auth_headers,
        json={"actor": "sam", "reason": "operator requested pause"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "marker_unreadable"
    refused_events = [
        row
        for row in _audit_records(audit_home)
        if row["event"] == EVENT_SESSION_PAUSE_REFUSED
    ]
    assert len(refused_events) == 1
    event = refused_events[0]
    assert event["subject"] == configured_session
    assert event["actor"] == "sam"
    assert event["status"] == "warn"
    metadata = event["metadata"]
    assert metadata["session_name"] == configured_session
    assert metadata["actor"] == "sam"
    assert metadata["reason"] == "operator requested pause"
    assert metadata["paused_count_after"] is None
    assert metadata["operation"] == "pause"
    assert metadata["refused_reason"] == "marker_unreadable"
    assert metadata["marker_path"] == str(marker)
    assert "malformed JSON" in metadata["marker_reason"]
