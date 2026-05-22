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
        # Also stub the work-service stub factory so we never try to
        # open Postgres during these tests. (The route uses the
        # public ``list_active_worker_sessions`` facade in
        # ``pollypm.web_api.service`` — Blocker 3 fix.)
        monkeypatch.setattr(
            chat_messages_routes,
            "_build_work_service_stub",
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
    """Factory installing a stub for ``parse_events_jsonl``.

    Signature mirrors the real parser on main
    (:func:`pollypm.web_api.chat.transcripts.parse_events_jsonl`) — the
    ``include_thinking`` knob was removed when thinking-block
    round-tripping was parked (follow-up #2048). Tests that need real
    parser behavior (Blocker 1) drive the on-disk parser directly via
    a JSONL fixture instead of installing this stub.
    """
    def install(envelopes_by_path: dict[Path, list[MessageEnvelope]]) -> None:
        def fake(path, *, actor_fallback="agent"):
            return list(envelopes_by_path.get(Path(path), []))
        monkeypatch.setattr(
            chat_messages_routes, "parse_events_jsonl", fake,
        )
    return install


@pytest.fixture
def patch_capture(monkeypatch: pytest.MonkeyPatch):
    """Factory installing a stub for ``capture_envelopes``.

    Signature mirrors the real helper on main — including the
    ``strict`` knob added in this PR (Blocker 2 fix). Tests that need
    the production strict-mode behavior (capture_pane raising →
    capture_failed) drive the real helper via a fake tmux client with
    a raising ``capture_pane`` instead of installing this stub.
    """
    def install(envelopes: list[MessageEnvelope]) -> None:
        def fake(
            tmux_client, *, session_name, target,
            actor_fallback="agent", lines=3000, timestamp=None,
            role=MessageRole.ASSISTANT, strict=False,
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


def test_messages_endpoint_drops_thinking_envelopes(
    client, auth_headers, patch_registry, patch_parser, tmp_path,
):
    """Thinking blocks are filtered out — the parser on main never
    surfaces them, but the router keeps a defensive filter so any
    future capture-mode emitter can't smuggle them through.

    The ``include_thinking`` query param + thinking round-tripping is
    parked until follow-up #2048 wires the ingestor side; until then
    the API never emits ``type=thinking`` envelopes.
    """
    archive = tmp_path / "events.jsonl"
    archive.write_text("x")
    envelopes = [
        _env("t", type_="thinking", text="(thinking)"),
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


def test_messages_endpoint_rejects_include_thinking_query_param(
    client, auth_headers, patch_registry, patch_parser, tmp_path,
):
    """``include_thinking`` was removed from the endpoint signature
    (follow-up #2048 will reinstate it once the ingestor preserves
    thinking blocks). FastAPI ignores unknown query params by default,
    so we just confirm the param is no longer wired — passing it has
    no effect on the response.
    """
    archive = tmp_path / "events.jsonl"
    archive.write_text("x")
    envelopes = [
        _env("t", type_="thinking", text="(thinking)"),
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
    # include_thinking has no effect — thinking is still filtered out.
    assert "thinking" not in types


def test_messages_endpoint_inlines_subagent_transcript_when_requested(
    client, auth_headers, patch_registry, monkeypatch, tmp_path, project_root,
):
    archive = tmp_path / "events.jsonl"
    archive.write_text("x")
    # Place the subagent transcript inside the project's allowed
    # transcripts root so the path-traversal allowlist (blocker 3
    # fix) permits inlining.
    transcripts_root = project_root / ".pollypm" / "transcripts"
    transcripts_root.mkdir(parents=True, exist_ok=True)
    sub_archive = transcripts_root / "subagent.jsonl"
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

    def fake_parser(path, *, actor_fallback="agent"):
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


# ---------------------------------------------------------------------------
# Regression: descending cursor pagination (PR #2045 blocker 2)
# ---------------------------------------------------------------------------


def test_messages_endpoint_desc_cursor_pagination_no_overlap(
    client, auth_headers, patch_registry, patch_parser, tmp_path,
):
    """25 messages, limit=10, default direction=desc.

    Walks page 1 -> next_cursor -> page 2 and asserts no message id
    appears in both pages. Before the fix, ``_apply_filters_and_paginate``
    applied ``since_id`` in source order while paginating in reversed
    order — so the desc page 2 was a duplicate of page 1.
    """
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

    page1 = client.get(
        "/api/v1/chat/operator/messages?limit=10",
        headers=auth_headers,
    ).json()
    page1_ids = [m["id"] for m in page1["messages"]]
    assert len(page1_ids) == 10
    assert page1["has_more"] is True
    # desc order: newest first.
    assert page1_ids[0] == "msg_024"
    assert page1_ids[-1] == "msg_015"
    assert page1["next_cursor"] == "msg_015"

    page2 = client.get(
        f"/api/v1/chat/operator/messages?limit=10&since_id={page1['next_cursor']}",
        headers=auth_headers,
    ).json()
    page2_ids = [m["id"] for m in page2["messages"]]
    assert len(page2_ids) == 10
    # Strictly after cursor in desc order: msg_014..msg_005.
    assert page2_ids[0] == "msg_014"
    assert page2_ids[-1] == "msg_005"
    # No overlap between pages — the duplication bug shipped both
    # pages as msg_024..msg_015 before the fix.
    assert set(page1_ids).isdisjoint(set(page2_ids))


# ---------------------------------------------------------------------------
# Security: subagent path-traversal allowlist (PR #2045 blocker 3)
# ---------------------------------------------------------------------------


def test_messages_endpoint_rejects_absolute_path_traversal_in_subagent(
    client, auth_headers, patch_registry, monkeypatch, tmp_path,
):
    """``output_file=/etc/passwd`` must NOT cause the API to stat/read it."""
    archive = tmp_path / "events.jsonl"
    archive.write_text("x")
    parent = _env(
        "result_1",
        type_=MessageType.SUBAGENT_RESULT,
        text="Subagent done.",
        metadata={
            "subagent_id": "abc",
            "output_file": "/etc/passwd",
        },
    )
    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR, persona="Polly",
        transcript_path=archive,
    )])

    calls: list[Path] = []

    def fake_parser(path, *, actor_fallback="agent"):
        # Track every disk access — must NOT include /etc/passwd.
        calls.append(Path(path))
        if Path(path) == archive:
            return [parent]
        return [_env("leaked", text="should never reach here")]

    monkeypatch.setattr(
        chat_messages_routes, "parse_events_jsonl", fake_parser,
    )
    body = client.get(
        "/api/v1/chat/operator/messages?include_subagents=true",
        headers=auth_headers,
    ).json()
    # Endpoint succeeds (no raise) but subagent_transcript is NOT
    # inlined for the rejected path.
    assert len(body["messages"]) == 1
    assert "subagent_transcript" not in body["messages"][0]["metadata"]
    # Confirm the subagent_loader was never called for /etc/passwd.
    assert Path("/etc/passwd") not in calls
    assert Path("/etc/passwd").resolve() not in calls


def test_messages_endpoint_rejects_relative_path_traversal_in_subagent(
    client, auth_headers, patch_registry, monkeypatch, tmp_path,
):
    """``output_file=../../../escape.jsonl`` must not escape transcript roots."""
    archive = tmp_path / "events.jsonl"
    archive.write_text("x")
    # Create the escape target somewhere accessible so we'd be able to
    # detect a successful read if the allowlist were missing.
    escape = tmp_path / "escape.jsonl"
    escape.write_text("would be leaked without allowlist")
    parent = _env(
        "result_1",
        type_=MessageType.SUBAGENT_RESULT,
        text="Subagent done.",
        metadata={
            "subagent_id": "abc",
            "output_file": "../../../escape.jsonl",
        },
    )
    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR, persona="Polly",
        transcript_path=archive,
    )])

    def fake_parser(path, *, actor_fallback="agent"):
        if Path(path) == archive:
            return [parent]
        # Any access for a non-archive path means the allowlist
        # let an escape through.
        return [_env("leaked", text="boundary breach")]

    monkeypatch.setattr(
        chat_messages_routes, "parse_events_jsonl", fake_parser,
    )
    body = client.get(
        "/api/v1/chat/operator/messages?include_subagents=true",
        headers=auth_headers,
    ).json()
    assert "subagent_transcript" not in body["messages"][0]["metadata"]


# ---------------------------------------------------------------------------
# source=capture strict failure modes (PR #2045 blocker 5)
# ---------------------------------------------------------------------------


def test_messages_endpoint_source_capture_503s_when_window_missing(
    client, auth_headers, patch_registry,
):
    """Explicit ``source=capture`` + missing window → 503 window_missing."""
    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR, persona="Polly",
        transcript_path=None, present=False,
    )])
    response = client.get(
        "/api/v1/chat/operator/messages?source=capture",
        headers=auth_headers,
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "window_missing"


def test_messages_endpoint_source_capture_503s_when_tmux_unavailable(
    client, auth_headers, patch_registry, monkeypatch, tmp_path,
):
    """Explicit ``source=capture`` + TmuxClient None → 503 capture_unavailable."""
    archive = tmp_path / "events.jsonl"
    archive.write_text("x")
    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR, persona="Polly",
        transcript_path=archive, present=True,
    )])
    # patch_registry already stubs _build_tmux_client → None. With
    # window.present=True, source=capture should now raise
    # capture_unavailable instead of returning 200 + [].
    response = client.get(
        "/api/v1/chat/operator/messages?source=capture",
        headers=auth_headers,
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "capture_unavailable"


def test_messages_endpoint_source_capture_503s_when_capture_raises(
    client, auth_headers, patch_registry, monkeypatch, tmp_path,
):
    """Explicit ``source=capture`` + capture_envelopes raises → 503 capture_failed."""
    archive = tmp_path / "events.jsonl"
    archive.write_text("x")
    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR, persona="Polly",
        transcript_path=archive, present=True,
    )])
    # Give the route a non-None tmux client so it gets past the
    # availability check, then make capture_envelopes blow up.
    monkeypatch.setattr(
        chat_messages_routes, "_build_tmux_client", lambda: object(),
    )

    def boom(*args, **kwargs):
        raise RuntimeError("tmux pipe closed")

    monkeypatch.setattr(
        chat_messages_routes, "capture_envelopes", boom,
    )
    response = client.get(
        "/api/v1/chat/operator/messages?source=capture",
        headers=auth_headers,
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "capture_failed"
    assert "tmux pipe closed" in response.json()["error"]["message"]


def test_messages_endpoint_source_auto_still_fail_soft_on_capture_error(
    client, auth_headers, patch_registry, patch_parser, monkeypatch, tmp_path,
):
    """``source=auto`` keeps the fail-soft contract.

    When the archive is stale and capture explodes, ``auto`` falls
    back to whatever JSONL exists — never 503s. This is the contract
    boundary that blocker 5 carved out: only the explicit
    ``source=capture`` gets strict errors.
    """
    archive = tmp_path / "events.jsonl"
    archive.write_text("x")
    monkeypatch.setattr(
        chat_messages_routes, "is_archive_stale",
        lambda path, **kw: True,
    )
    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR, persona="Polly",
        transcript_path=archive, present=True,
    )])
    patch_parser({archive: [_env("from_jsonl", text="recovered")]})

    def boom(*args, **kwargs):
        raise RuntimeError("tmux pipe closed")

    monkeypatch.setattr(
        chat_messages_routes, "_build_tmux_client", lambda: object(),
    )
    monkeypatch.setattr(
        chat_messages_routes, "capture_envelopes", boom,
    )

    response = client.get(
        "/api/v1/chat/operator/messages?source=auto",
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    # Falls back to whatever JSONL holds.
    assert body["transcript_source"] == "jsonl"
    assert [m["id"] for m in body["messages"]] == ["from_jsonl"]


# ---------------------------------------------------------------------------
# REAL-helper regression tests (PR #2045 v3 blockers)
#
# The fixtures above monkeypatch ``parse_events_jsonl`` /
# ``capture_envelopes`` for speed and isolation. The two tests below
# pin the production wiring by driving the REAL helpers — a
# monkeypatch that accepted the now-removed ``include_thinking`` kwarg
# (or a fake capture that raised instead of the real fail-soft helper)
# previously hid two TypeError / silent-empty bugs in production.
# ---------------------------------------------------------------------------


def test_messages_endpoint_jsonl_uses_real_parser_no_typeerror(
    client, auth_headers, patch_registry, tmp_path,
):
    """REAL ``parse_events_jsonl`` call — Blocker 1 regression.

    Previously the route passed ``include_thinking=...`` to
    ``parse_events_jsonl``, but the authoritative parser on main no
    longer accepts that kwarg (it was removed in #2044). Tests passed
    because the fixture stub accepted the kwarg; in production every
    ``source=jsonl`` request raised ``TypeError``. This test drives the
    real parser via an on-disk JSONL fixture so the wiring is exercised
    end-to-end.
    """
    import json as _json

    archive = tmp_path / "events.jsonl"
    # One Claude-shaped user_turn event — same shape the real ingestor
    # writes (mirrors tests/test_chat_transcripts.py::_claude_event).
    event = {
        "timestamp": "2026-05-21T10:00:00Z",
        "event_type": "user_turn",
        "session_id": "session-real",
        "account_name": "claude_main",
        "provider": "claude",
        "project_key": "myproj",
        "source_path": "/tmp/raw.jsonl",
        "source_offset": 0,
        "cwd": "/tmp/repo",
        "model_name": "claude-opus-4-7",
        "payload": {"text": "hello from real parser"},
    }
    archive.write_text(_json.dumps(event) + "\n")

    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR, persona="Polly",
        transcript_path=archive,
    )])
    # NOTE: deliberately NOT installing patch_parser — we want the real
    # parser to fire. patch_registry already stubs the registry +
    # work-service so we never hit Postgres.
    response = client.get(
        "/api/v1/chat/operator/messages?source=jsonl&direction=asc",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["transcript_source"] == "jsonl"
    assert body["transcript_path"] == str(archive)
    assert len(body["messages"]) == 1
    assert body["messages"][0]["text"] == "hello from real parser"
    assert body["messages"][0]["type"] == "text"


def test_messages_endpoint_capture_uses_real_helper_with_raising_pane(
    client, auth_headers, patch_registry, monkeypatch, tmp_path,
):
    """REAL ``capture_envelopes`` call — Blocker 2 regression.

    Previously ``capture_envelopes`` swallowed every ``capture_pane``
    exception and returned ``[]``; the strict-mode 503 contract was
    bypassed in production because nothing ever raised back to the
    route. Tests passed because they monkeypatched
    ``chat_messages_routes.capture_envelopes`` itself to raise.

    This test drives the real helper end-to-end with a fake tmux
    client whose ``capture_pane`` raises. The real strict-mode path
    (added in this PR) propagates the exception so the route maps it
    to ``503 capture_failed``.
    """
    archive = tmp_path / "events.jsonl"
    archive.write_text("x")
    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR, persona="Polly",
        transcript_path=archive, present=True,
    )])

    class _RaisingTmuxClient:
        def capture_pane(self, target, lines=3000):  # noqa: ARG002
            raise RuntimeError("tmux pipe closed")

    # Install a fake TmuxClient — but DO NOT stub capture_envelopes;
    # the real helper must run so the strict-mode propagation is
    # exercised end-to-end.
    monkeypatch.setattr(
        chat_messages_routes, "_build_tmux_client",
        lambda: _RaisingTmuxClient(),
    )

    response = client.get(
        "/api/v1/chat/operator/messages?source=capture",
        headers=auth_headers,
    )
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["error"]["code"] == "capture_failed"
    assert "tmux pipe closed" in body["error"]["message"]
