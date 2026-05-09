"""SSE stream tests.

Covers spec §4:
- ``GET /api/v1/events`` returns ``text/event-stream``.
- Each message has ``event: audit\\nid: <ts>\\ndata: <json>``.
- ``?since=`` replays events with ``ts > since``.
- ``?project=`` and ``?event=`` filter server-side.
- Server emits ``: keep-alive`` comment lines for idle connections.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pollypm.audit.log import emit


@pytest.fixture(autouse=True)
def _fast_sse_cadence(monkeypatch):
    """Shrink SSE cadence knobs so tests don't wait the real 15s keep-alive."""
    monkeypatch.setattr("pollypm.web_api.sse.KEEPALIVE_INTERVAL_S", 0.1)
    monkeypatch.setattr("pollypm.web_api.sse.TAIL_POLL_INTERVAL_S", 0.02)


def _write_event(audit_home: Path, project_root: Path, *, project: str = "myproj", event: str = "task.status_changed", **kwargs) -> None:
    emit(event=event, project=project, project_path=project_root, **kwargs)


def _read_stream(client, path: str, headers: dict[str, str], *, max_bytes: int = 4096) -> bytes:
    """Drain the SSE response up to ``max_bytes`` and close.

    The TestClient streams synchronously; we read the response body
    as bytes via ``stream=True`` semantics. ``httpx.Response.iter_raw``
    surfaces the raw chunks.
    """
    with client.stream("GET", path, headers=headers) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        data = bytearray()
        for chunk in response.iter_raw():
            data.extend(chunk)
            if len(data) >= max_bytes:
                break
        return bytes(data)


def test_events_requires_auth(client) -> None:
    response = client.get("/api/v1/events")
    assert response.status_code == 401


def test_events_returns_event_stream_content_type(client, auth_headers) -> None:
    with client.stream("GET", "/api/v1/events", headers=auth_headers) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        # Drain a small amount so the response closes cleanly.
        for _ in response.iter_raw():
            break


def test_events_replay_since_returns_recent_event(
    client, auth_headers, project_root, audit_home
) -> None:
    _write_event(
        audit_home,
        project_root,
        project="myproj",
        event="task.status_changed",
        subject="myproj/1",
        actor="pm",
        status="done",
    )
    payload = _read_stream(
        client,
        "/api/v1/events?since=2000-01-01T00:00:00Z",
        auth_headers,
    )
    text = payload.decode("utf-8", errors="replace")
    assert "event: audit" in text
    assert "task.status_changed" in text
    assert "myproj/1" in text


def test_events_filter_by_project(client, auth_headers, project_root, audit_home, api_config) -> None:
    """A subscription scoped to ``?project=myproj`` ignores other projects."""
    # Add a second registered project so the central tail has rows for both.
    from pollypm.models import KnownProject, ProjectKind
    other_root = project_root.parent / "otherproj"
    other_root.mkdir()
    (other_root / ".pollypm").mkdir()
    api_config.projects["other"] = KnownProject(
        key="other",
        path=other_root,
        name="Other",
        tracked=True,
        kind=ProjectKind.GIT,
    )
    _write_event(audit_home, project_root, project="myproj", event="task.created", subject="myproj/1")
    _write_event(audit_home, other_root, project="other", event="task.created", subject="other/1")

    payload = _read_stream(
        client,
        "/api/v1/events?since=2000-01-01T00:00:00Z&project=myproj",
        auth_headers,
    )
    text = payload.decode("utf-8")
    assert "myproj/1" in text
    assert "other/1" not in text


def test_events_filter_by_event_glob(client, auth_headers, project_root, audit_home) -> None:
    _write_event(audit_home, project_root, project="myproj", event="task.created", subject="myproj/1")
    _write_event(audit_home, project_root, project="myproj", event="plan.version_incremented", subject="myproj/1")
    payload = _read_stream(
        client,
        "/api/v1/events?since=2000-01-01T00:00:00Z&event=task.*",
        auth_headers,
    )
    text = payload.decode("utf-8")
    assert "task.created" in text
    assert "plan.version_incremented" not in text


def test_events_keep_alive_emitted_when_idle(client, auth_headers) -> None:
    """When no events arrive, the server emits a keep-alive comment."""
    payload = _read_stream(client, "/api/v1/events", auth_headers, max_bytes=64)
    text = payload.decode("utf-8")
    assert ": keep-alive" in text
