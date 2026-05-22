"""Integration tests for the P2 chat GET endpoints.

Covers ``GET /api/v1/chat/sessions`` and
``GET /api/v1/chat/{session_name}/messages`` per the chat-endpoints
spec §2.1 / §2.2 (``~/Desktop/pollypm-chat-endpoints-spec.md``).

Tests use FastAPI ``TestClient`` against an in-process app, swapping
out the real registry / transcript reader / tmux capture helpers via
monkeypatching so the suite never touches disk or a live tmux server.
Run with ``pytest --noconftest tests/test_chat_messages_endpoint.py -v``
so the per-project conftest (which requires a Postgres harness) is
skipped — these tests are self-contained.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pollypm.config import (
    AccountConfig,
    MemorySettings,
    PollyPMConfig,
    PollyPMSettings,
    ProjectSettings,
)
from pollypm.models import KnownProject, ProjectKind, ProviderKind, RuntimeKind
from pollypm.web_api import create_app, ensure_token
from pollypm.web_api.chat.envelope import (
    MessageEnvelope,
    MessageRole,
    MessageType,
)
from pollypm.web_api.chat.registry import (
    ChatSurface,
    SurfaceType,
    TmuxWindowState,
)
from pollypm.web_api.routes import chat_messages as chat_messages_routes


# ---------------------------------------------------------------------------
# Fixtures (self-contained — do not rely on tests/web_api/conftest.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / ".pollypm").mkdir()
    return root


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "myproj"
    root.mkdir()
    (root / ".pollypm").mkdir()
    return root


@pytest.fixture
def config(workspace: Path, project_root: Path) -> PollyPMConfig:
    base_dir = workspace / ".pollypm"
    return PollyPMConfig(
        project=ProjectSettings(
            name="PollyPM",
            root_dir=workspace,
            tmux_session="pollypm-test",
            workspace_root=workspace,
            base_dir=base_dir,
            logs_dir=base_dir / "logs",
            snapshots_dir=base_dir / "snapshots",
            state_db=base_dir / "state.db",
        ),
        pollypm=PollyPMSettings(
            controller_account="codex_primary",
            open_permissions_by_default=False,
            failover_enabled=False,
            failover_accounts=[],
            heartbeat_backend="local",
            scheduler_backend="inline",
            lease_timeout_minutes=30,
        ),
        accounts={
            "codex_primary": AccountConfig(
                name="codex_primary",
                provider=ProviderKind.CODEX,
                email="codex@example.com",
                runtime=RuntimeKind.LOCAL,
                home=base_dir / "homes" / "codex_primary",
            ),
        },
        sessions={},
        projects={
            "myproj": KnownProject(
                key="myproj",
                path=project_root,
                name="My Project",
                tracked=True,
                kind=ProjectKind.GIT,
            ),
        },
        memory=MemorySettings(backend="file"),
    )


@pytest.fixture
def token(tmp_path: Path) -> tuple[Path, str]:
    token_path = tmp_path / "api-token"
    value, _generated = ensure_token(token_path)
    return token_path, value


@pytest.fixture
def auth_headers(token: tuple[Path, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {token[1]}"}


@pytest.fixture
def app(config, token):
    token_path, _value = token
    return create_app(config=config, token_path=token_path)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Surface + envelope fixtures
# ---------------------------------------------------------------------------


def _surface(
    session_name: str,
    surface_type: SurfaceType,
    *,
    persona: str | None = None,
    project: str | None = None,
    task_id: int | None = None,
    transcript_path: Path | None = None,
    present: bool = True,
) -> ChatSurface:
    return ChatSurface(
        session_name=session_name,
        surface_type=surface_type,
        persona=persona,
        project=project,
        window=TmuxWindowState(
            tmux_session="pollypm-test-storage-closet",
            window_name=session_name,
            present=present,
            pane_id="%99" if present else None,
        ),
        transcript_path=transcript_path,
        task_id=task_id,
        cwd=None,
        provider="claude",
        auth_token_present=True,
        worktree_path=None,
    )


def _all_four_surfaces(tp: Path | None = None) -> list[ChatSurface]:
    return [
        _surface("operator", SurfaceType.OPERATOR, persona="Polly",
                 transcript_path=tp),
        _surface("architect_myproj", SurfaceType.ARCHITECT, persona="Archie",
                 project="myproj", transcript_path=tp),
        _surface("advisor_myproj", SurfaceType.ADVISOR, persona="Advisor",
                 project="myproj", transcript_path=tp),
        _surface("task-myproj-7", SurfaceType.WORKER, persona=None,
                 project="myproj", task_id=7, transcript_path=tp),
    ]


def _env(
    msg_id: str,
    *,
    ts: str = "2026-05-21T10:00:00Z",
    role: MessageRole = MessageRole.ASSISTANT,
    actor: str = "Polly",
    type_: MessageType = MessageType.TEXT,
    text: str = "hello",
    metadata: dict[str, Any] | None = None,
) -> MessageEnvelope:
    return MessageEnvelope(
        id=msg_id, ts=ts, role=role, actor=actor, type=type_,
        text=text, metadata=metadata or {},
    )


@pytest.fixture
def patch_registry(monkeypatch: pytest.MonkeyPatch):
    """Factory returning a monkeypatch helper for ``enumerate_chat_surfaces``.

    Returns a callable ``(surfaces_list)`` that installs a stub on
    the route module so both the discovery endpoint and the
    ``_find_surface`` helper used by the messages endpoint resolve
    against the same surface list.
    """
    def install(surfaces: list[ChatSurface]) -> None:
        def fake(config, work_service=None, tmux_client=None):
            return list(surfaces)
        monkeypatch.setattr(
            chat_messages_routes, "enumerate_chat_surfaces", fake,
        )
        # Also stub the work-service handle factory so we never try to
        # open Postgres during these tests.
        monkeypatch.setattr(
            chat_messages_routes,
            "_open_work_service_for_discovery",
            lambda _config: None,
        )
        # Stop the route from constructing a real TmuxClient (spawning
        # a subprocess at import time on systems with tmux installed).
        monkeypatch.setattr(
            chat_messages_routes, "_build_tmux_client", lambda: None,
        )
    return install


@pytest.fixture
def patch_parser(monkeypatch: pytest.MonkeyPatch):
    """Factory installing a stub for ``parse_events_jsonl``."""
    def install(envelopes_by_path: dict[Path, list[MessageEnvelope]]) -> None:
        def fake(path, *, include_thinking=False, actor_fallback="agent"):
            result = envelopes_by_path.get(Path(path), [])
            if not include_thinking:
                result = [e for e in result if str(e.type) != "thinking"]
            return list(result)
        monkeypatch.setattr(
            chat_messages_routes, "parse_events_jsonl", fake,
        )
    return install


@pytest.fixture
def patch_capture(monkeypatch: pytest.MonkeyPatch):
    """Factory installing a stub for ``capture_envelopes``."""
    def install(envelopes: list[MessageEnvelope]) -> None:
        def fake(
            tmux_client, *, session_name, target,
            actor_fallback="agent", lines=3000, timestamp=None,
            role=MessageRole.ASSISTANT,
        ):
            return list(envelopes)
        monkeypatch.setattr(
            chat_messages_routes, "capture_envelopes", fake,
        )
        # Capture path needs a non-None tmux client to proceed.
        monkeypatch.setattr(
            chat_messages_routes, "_build_tmux_client",
            lambda: object(),
        )
    return install


# ---------------------------------------------------------------------------
# /sessions discovery
# ---------------------------------------------------------------------------


def test_sessions_endpoint_lists_all_four_surface_types(
    client, auth_headers, patch_registry, tmp_path,
):
    archive = tmp_path / "events.jsonl"
    archive.write_text("")
    patch_registry(_all_four_surfaces(archive))
    response = client.get("/api/v1/chat/sessions", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    types = [s["surface_type"] for s in body["sessions"]]
    assert types == ["operator", "architect", "advisor", "worker"]
    names = [s["session_name"] for s in body["sessions"]]
    assert names == [
        "operator", "architect_myproj", "advisor_myproj", "task-myproj-7",
    ]


def test_sessions_endpoint_surfaces_window_state(
    client, auth_headers, patch_registry,
):
    patch_registry(_all_four_surfaces())
    body = client.get("/api/v1/chat/sessions", headers=auth_headers).json()
    op = body["sessions"][0]
    assert op["window"]["present"] is True
    assert op["window"]["window_name"] == "operator"
    assert op["window"]["pane_id"] == "%99"
    assert op["transcript"]["source"] is None  # no archive in this fixture


def test_sessions_endpoint_requires_bearer_auth(client, patch_registry):
    patch_registry(_all_four_surfaces())
    response = client.get("/api/v1/chat/sessions")
    assert response.status_code == 401
    assert response.json()["error"]["code"] in {"unauthorized", "invalid_token"}


def test_sessions_endpoint_rejects_wrong_token(client, patch_registry):
    patch_registry(_all_four_surfaces())
    response = client.get(
        "/api/v1/chat/sessions",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


def test_sessions_endpoint_empty_when_no_config_sessions(
    client, auth_headers, patch_registry,
):
    patch_registry([])
    body = client.get("/api/v1/chat/sessions", headers=auth_headers).json()
    assert body["sessions"] == []


# ---------------------------------------------------------------------------
# /{session_name}/messages happy paths
# ---------------------------------------------------------------------------


def test_messages_endpoint_returns_envelopes_for_known_session(
    client, auth_headers, patch_registry, patch_parser, tmp_path,
):
    archive = tmp_path / "events.jsonl"
    archive.write_text("placeholder")
    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR,
        persona="Polly", transcript_path=archive,
    )])
    patch_parser({archive: [
        _env("msg_1", text="hello"),
        _env("msg_2", ts="2026-05-21T10:01:00Z", text="world"),
    ]})
    response = client.get(
        "/api/v1/chat/operator/messages?direction=asc",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["session_name"] == "operator"
    assert body["surface_type"] == "operator"
    assert body["persona"] == "Polly"
    assert body["transcript_source"] == "jsonl"
    assert body["transcript_path"] == str(archive)
    assert [m["id"] for m in body["messages"]] == ["msg_1", "msg_2"]
    assert body["has_more"] is False
    assert body["next_cursor"] is None


def test_messages_endpoint_404s_unknown_session(
    client, auth_headers, patch_registry,
):
    patch_registry(_all_four_surfaces())
    response = client.get(
        "/api/v1/chat/does-not-exist/messages",
        headers=auth_headers,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "session_unknown"


def test_messages_endpoint_empty_when_no_transcript_yet(
    client, auth_headers, patch_registry, monkeypatch,
):
    # spec §4.8: new session, no transcript — return messages: [],
    # transcript_source: null, NOT an error.
    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR, persona="Polly",
        transcript_path=None,
    )])
    # capture also returns nothing (no window).
    monkeypatch.setattr(
        chat_messages_routes, "capture_envelopes",
        lambda *a, **kw: [],
    )
    response = client.get(
        "/api/v1/chat/operator/messages",
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["messages"] == []
    assert body["transcript_source"] is None
    assert body["transcript_path"] is None


# ---------------------------------------------------------------------------
# Pagination + filtering
# ---------------------------------------------------------------------------


def test_messages_endpoint_respects_limit(
    client, auth_headers, patch_registry, patch_parser, tmp_path,
):
    archive = tmp_path / "events.jsonl"
    archive.write_text("x")
    envelopes = [
        _env(f"msg_{i:03d}", ts=f"2026-05-21T10:{i:02d}:00Z")
        for i in range(25)
    ]
    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR, persona="Polly",
        transcript_path=archive,
    )])
    patch_parser({archive: envelopes})
    body = client.get(
        "/api/v1/chat/operator/messages?limit=10&direction=asc",
        headers=auth_headers,
    ).json()
    assert len(body["messages"]) == 10
    assert body["has_more"] is True
    assert body["next_cursor"] == "msg_009"


def test_messages_endpoint_since_id_walks_cursor(
    client, auth_headers, patch_registry, patch_parser, tmp_path,
):
    archive = tmp_path / "events.jsonl"
    archive.write_text("x")
    envelopes = [
        _env(f"msg_{i:03d}", ts=f"2026-05-21T10:{i:02d}:00Z")
        for i in range(10)
    ]
    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR, persona="Polly",
        transcript_path=archive,
    )])
    patch_parser({archive: envelopes})
    body = client.get(
        "/api/v1/chat/operator/messages?direction=asc&since_id=msg_003",
        headers=auth_headers,
    ).json()
    ids = [m["id"] for m in body["messages"]]
    # strictly-after: msg_003 itself is excluded
    assert ids[0] == "msg_004"
    assert "msg_003" not in ids


def test_messages_endpoint_since_filters_iso_lower_bound(
    client, auth_headers, patch_registry, patch_parser, tmp_path,
):
    archive = tmp_path / "events.jsonl"
    archive.write_text("x")
    envelopes = [
        _env("old", ts="2026-05-20T00:00:00Z"),
        _env("new", ts="2026-05-22T00:00:00Z"),
    ]
    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR, persona="Polly",
        transcript_path=archive,
    )])
    patch_parser({archive: envelopes})
    body = client.get(
        "/api/v1/chat/operator/messages?since=2026-05-21T00:00:00Z&direction=asc",
        headers=auth_headers,
    ).json()
    assert [m["id"] for m in body["messages"]] == ["new"]


def test_messages_endpoint_invalid_since_returns_400(
    client, auth_headers, patch_registry, patch_parser, tmp_path,
):
    archive = tmp_path / "events.jsonl"
    archive.write_text("x")
    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR, persona="Polly",
        transcript_path=archive,
    )])
    patch_parser({archive: []})
    response = client.get(
        "/api/v1/chat/operator/messages?since=not-a-timestamp",
        headers=auth_headers,
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    assert "since" in body["error"]["message"]


def test_messages_endpoint_direction_asc_vs_desc(
    client, auth_headers, patch_registry, patch_parser, tmp_path,
):
    archive = tmp_path / "events.jsonl"
    archive.write_text("x")
    envelopes = [
        _env("a", ts="2026-05-21T10:00:00Z"),
        _env("b", ts="2026-05-21T10:01:00Z"),
        _env("c", ts="2026-05-21T10:02:00Z"),
    ]
    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR, persona="Polly",
        transcript_path=archive,
    )])
    patch_parser({archive: envelopes})
    asc = client.get(
        "/api/v1/chat/operator/messages?direction=asc",
        headers=auth_headers,
    ).json()
    desc = client.get(
        "/api/v1/chat/operator/messages?direction=desc",
        headers=auth_headers,
    ).json()
    assert [m["id"] for m in asc["messages"]] == ["a", "b", "c"]
    assert [m["id"] for m in desc["messages"]] == ["c", "b", "a"]


def test_messages_endpoint_limit_capped_at_500(
    client, auth_headers, patch_registry, patch_parser, tmp_path,
):
    archive = tmp_path / "events.jsonl"
    archive.write_text("x")
    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR, persona="Polly",
        transcript_path=archive,
    )])
    patch_parser({archive: []})
    response = client.get(
        "/api/v1/chat/operator/messages?limit=1000",
        headers=auth_headers,
    )
    # Pydantic ``Query(le=500)`` rejects out-of-range values with the
    # spec's ``validation_error`` envelope. That's the contract: clients
    # don't get silently clipped — they're told their value is too big.
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"


def test_messages_endpoint_accepts_limit_500(
    client, auth_headers, patch_registry, patch_parser, tmp_path,
):
    archive = tmp_path / "events.jsonl"
    archive.write_text("x")
    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR, persona="Polly",
        transcript_path=archive,
    )])
    patch_parser({archive: []})
    response = client.get(
        "/api/v1/chat/operator/messages?limit=500",
        headers=auth_headers,
    )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Thinking + subagent expansion
# ---------------------------------------------------------------------------


def test_messages_endpoint_excludes_thinking_by_default(
    client, auth_headers, patch_registry, patch_parser, tmp_path,
):
    archive = tmp_path / "events.jsonl"
    archive.write_text("x")
    envelopes = [
        _env("t", type_=MessageType.THINKING, text="(thinking)"),
        _env("text", type_=MessageType.TEXT, text="visible"),
    ]
    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR, persona="Polly",
        transcript_path=archive,
    )])
    patch_parser({archive: envelopes})
    body = client.get(
        "/api/v1/chat/operator/messages?direction=asc",
        headers=auth_headers,
    ).json()
    types = [m["type"] for m in body["messages"]]
    assert "thinking" not in types
    assert "text" in types


def test_messages_endpoint_includes_thinking_when_requested(
    client, auth_headers, patch_registry, patch_parser, tmp_path,
):
    archive = tmp_path / "events.jsonl"
    archive.write_text("x")
    envelopes = [
        _env("t", type_=MessageType.THINKING, text="(thinking)"),
        _env("text", type_=MessageType.TEXT, text="visible"),
    ]
    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR, persona="Polly",
        transcript_path=archive,
    )])
    patch_parser({archive: envelopes})
    body = client.get(
        "/api/v1/chat/operator/messages?include_thinking=true&direction=asc",
        headers=auth_headers,
    ).json()
    types = [m["type"] for m in body["messages"]]
    assert "thinking" in types


def test_messages_endpoint_inlines_subagent_transcript_when_requested(
    client, auth_headers, patch_registry, monkeypatch, tmp_path,
):
    archive = tmp_path / "events.jsonl"
    archive.write_text("x")
    sub_archive = tmp_path / "subagent.jsonl"
    sub_archive.write_text("y")
    parent = _env(
        "result_1",
        type_=MessageType.SUBAGENT_RESULT,
        text="Subagent done.",
        metadata={
            "subagent_id": "abc",
            "tool_use_id": "abc",
            "output_file": str(sub_archive),
            "summary": "Subagent done.",
        },
    )
    sub_envelopes = [
        _env("sub_1", text="step 1"),
        _env("sub_2", text="step 2"),
    ]
    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR, persona="Polly",
        transcript_path=archive,
    )])

    def fake_parser(path, *, include_thinking=False, actor_fallback="agent"):
        if Path(path) == archive:
            return [parent]
        if Path(path) == sub_archive:
            return list(sub_envelopes)
        return []

    monkeypatch.setattr(
        chat_messages_routes, "parse_events_jsonl", fake_parser,
    )
    body = client.get(
        "/api/v1/chat/operator/messages?include_subagents=true",
        headers=auth_headers,
    ).json()
    assert len(body["messages"]) == 1
    sub_transcript = body["messages"][0]["metadata"]["subagent_transcript"]
    assert [m["id"] for m in sub_transcript] == ["sub_1", "sub_2"]


def test_messages_endpoint_no_subagent_inlining_by_default(
    client, auth_headers, patch_registry, patch_parser, tmp_path,
):
    archive = tmp_path / "events.jsonl"
    archive.write_text("x")
    parent = _env(
        "result_1",
        type_=MessageType.SUBAGENT_RESULT,
        text="Subagent done.",
        metadata={
            "subagent_id": "abc",
            "output_file": str(tmp_path / "subagent.jsonl"),
        },
    )
    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR, persona="Polly",
        transcript_path=archive,
    )])
    patch_parser({archive: [parent]})
    body = client.get(
        "/api/v1/chat/operator/messages",
        headers=auth_headers,
    ).json()
    assert "subagent_transcript" not in body["messages"][0]["metadata"]


# ---------------------------------------------------------------------------
# Source modes (auto / jsonl / capture)
# ---------------------------------------------------------------------------


def test_messages_endpoint_source_jsonl_404_when_archive_missing(
    client, auth_headers, patch_registry,
):
    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR, persona="Polly",
        transcript_path=None,
    )])
    response = client.get(
        "/api/v1/chat/operator/messages?source=jsonl",
        headers=auth_headers,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "archive_missing"


def test_messages_endpoint_source_capture_uses_tmux_fallback(
    client, auth_headers, patch_registry, patch_capture, tmp_path,
):
    archive = tmp_path / "events.jsonl"
    archive.write_text("ignored")
    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR, persona="Polly",
        transcript_path=archive,
    )])
    patch_capture([
        _env("cap_1", text="line 1"),
        _env("cap_2", text="line 2"),
    ])
    body = client.get(
        "/api/v1/chat/operator/messages?source=capture&direction=asc",
        headers=auth_headers,
    ).json()
    assert body["transcript_source"] == "capture"
    assert body["transcript_path"] is None
    assert [m["id"] for m in body["messages"]] == ["cap_1", "cap_2"]


def test_messages_endpoint_source_auto_prefers_jsonl(
    client, auth_headers, patch_registry, patch_parser, monkeypatch, tmp_path,
):
    archive = tmp_path / "events.jsonl"
    archive.write_text("x")
    # Force "not stale" so auto picks jsonl.
    monkeypatch.setattr(
        chat_messages_routes, "is_archive_stale",
        lambda path, **kw: False,
    )
    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR, persona="Polly",
        transcript_path=archive,
    )])
    patch_parser({archive: [_env("a")]})
    body = client.get(
        "/api/v1/chat/operator/messages?source=auto",
        headers=auth_headers,
    ).json()
    assert body["transcript_source"] == "jsonl"
    assert body["messages"][0]["id"] == "a"


def test_messages_endpoint_source_auto_falls_back_when_stale(
    client, auth_headers, patch_registry, patch_capture, monkeypatch, tmp_path,
):
    archive = tmp_path / "events.jsonl"
    archive.write_text("x")
    # Force "stale" so auto falls through to capture.
    monkeypatch.setattr(
        chat_messages_routes, "is_archive_stale",
        lambda path, **kw: True,
    )
    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR, persona="Polly",
        transcript_path=archive,
    )])
    patch_capture([_env("cap_1", text="live")])
    body = client.get(
        "/api/v1/chat/operator/messages?source=auto",
        headers=auth_headers,
    ).json()
    assert body["transcript_source"] == "capture"
    assert body["messages"][0]["id"] == "cap_1"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_messages_endpoint_requires_bearer_auth(
    client, patch_registry, tmp_path,
):
    archive = tmp_path / "events.jsonl"
    archive.write_text("x")
    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR, persona="Polly",
        transcript_path=archive,
    )])
    response = client.get("/api/v1/chat/operator/messages")
    assert response.status_code == 401


def test_messages_endpoint_rejects_wrong_token(
    client, patch_registry, tmp_path,
):
    archive = tmp_path / "events.jsonl"
    archive.write_text("x")
    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR, persona="Polly",
        transcript_path=archive,
    )])
    response = client.get(
        "/api/v1/chat/operator/messages",
        headers={"Authorization": "Bearer nope"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


# ---------------------------------------------------------------------------
# Surface variety (workers + non-operator personas)
# ---------------------------------------------------------------------------


def test_messages_endpoint_resolves_worker_session(
    client, auth_headers, patch_registry, patch_parser, tmp_path,
):
    archive = tmp_path / "worker.jsonl"
    archive.write_text("x")
    patch_registry([_surface(
        "task-myproj-7", SurfaceType.WORKER, persona=None,
        project="myproj", task_id=7, transcript_path=archive,
    )])
    patch_parser({archive: [_env("w1", actor="worker")]})
    body = client.get(
        "/api/v1/chat/task-myproj-7/messages",
        headers=auth_headers,
    ).json()
    assert body["surface_type"] == "worker"
    assert body["persona"] is None
    assert [m["id"] for m in body["messages"]] == ["w1"]


def test_messages_endpoint_resolves_architect_session(
    client, auth_headers, patch_registry, patch_parser, tmp_path,
):
    archive = tmp_path / "arch.jsonl"
    archive.write_text("x")
    patch_registry([_surface(
        "architect_myproj", SurfaceType.ARCHITECT, persona="Archie",
        project="myproj", transcript_path=archive,
    )])
    patch_parser({archive: [_env("a1", actor="Archie")]})
    body = client.get(
        "/api/v1/chat/architect_myproj/messages",
        headers=auth_headers,
    ).json()
    assert body["surface_type"] == "architect"
    assert body["persona"] == "Archie"
