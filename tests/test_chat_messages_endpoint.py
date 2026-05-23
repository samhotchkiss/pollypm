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
        # ``pollypm.web_api.service`` — Blocker 3 fix.) Accepts the
        # ``strict`` kwarg that ``_find_surface`` passes when probing
        # a worker session (v4 blocker 3).
        monkeypatch.setattr(
            chat_messages_routes,
            "_build_work_service_stub",
            lambda _config, **_kw: None,
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

    Signature mirrors what the chat-messages route actually calls
    today (:func:`pollypm.web_api.chat.transcripts.parse_events_jsonl`
    with ``actor_fallback`` + ``strict`` only). The real parser also
    accepts an ``include_thinking`` kwarg (restored in #2048 — this
    PR), but the route does not pass it: thinking promotion to the
    HTTP surface is deferred to #2082. Tests that need real parser
    behavior (Blocker 1) drive the on-disk parser directly via a JSONL
    fixture instead of installing this stub.
    """
    def install(envelopes_by_path: dict[Path, list[MessageEnvelope]]) -> None:
        def fake(path, *, actor_fallback="agent", strict=False):
            # ``strict`` is accepted for forward-compat with the
            # ``source=auto`` unreadable-archive fallback added in the
            # round-6 fix; this stub treats it as a no-op (returns the
            # same envelopes regardless of strict mode).
            del strict
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
    """Thinking blocks are filtered out at the HTTP route boundary.

    As of #2048 (this PR) the parser CAN emit
    :class:`ParserInternalType.THINKING` envelopes when callers pass
    ``include_thinking=True`` to ``parse_events_jsonl``. The
    ``GET /messages`` route, however, still calls the parser with the
    default ``include_thinking=False`` AND defensively drops any
    parser-internal types downstream, so the public response catalog
    stays equal to the public :class:`MessageType` enum. Promoting
    thinking blocks onto the HTTP surface (route query param + OpenAPI
    enum entry) is parked until follow-up #2082.
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
    """``include_thinking`` is not yet exposed on the HTTP route.

    #2048 (this PR) restored parser-level support for thinking blocks
    via :class:`ParserInternalType.THINKING`, but promoting them onto
    the HTTP surface — wiring an ``include_thinking`` query param and
    adding ``thinking`` to the OpenAPI ``MessageType`` enum — is
    deferred to follow-up #2082. FastAPI ignores unknown query params
    by default, so we confirm passing the param has no effect: the
    route still calls the parser with ``include_thinking=False`` and
    filters parser-internal types, so ``thinking`` never appears in
    the response.
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


def test_messages_endpoint_no_subagent_inlining_by_default(
    client, auth_headers, patch_registry, patch_parser, tmp_path,
):
    """Without ``include_subagents=true`` no ``subagent_transcript`` is added.

    The parent envelope still carries ``output_file`` (the
    task-notification reference) but inlining stays opt-in to keep the
    default response shape stable (#2052).
    """
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
# Security: subagent path-traversal allowlist (#2052)
# ---------------------------------------------------------------------------


def test_messages_endpoint_include_subagents_false_explicit_is_ok(
    client, auth_headers, patch_registry, patch_parser, tmp_path,
):
    """Explicit ``include_subagents=false`` must return 200."""
    archive = tmp_path / "events.jsonl"
    archive.write_text("x")
    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR, persona="Polly",
        transcript_path=archive,
    )])
    patch_parser({archive: [_env("a", text="hi")]})
    response = client.get(
        "/api/v1/chat/operator/messages?include_subagents=false",
        headers=auth_headers,
    )
    assert response.status_code == 200


def test_messages_endpoint_include_subagents_rejects_path_traversal(
    client, auth_headers, patch_registry, patch_parser, tmp_path,
):
    """``output_file`` pointing outside the project transcripts root is skipped.

    The allowlist forecloses on ``/etc/passwd`` / ``../../../escape.jsonl``
    exploits: an envelope referencing a path outside the project +
    workspace transcripts roots silently skips inlining (no raise, no
    metadata.subagent_transcript) so a malformed transcript can't 500
    the endpoint or stat arbitrary files.
    """
    archive = tmp_path / "events.jsonl"
    archive.write_text("x")
    escape = tmp_path / "escape.jsonl"
    escape.write_text('{"type":"assistant","message":{"content":"leaked"}}\n')
    parent = _env(
        "result_1",
        type_=MessageType.SUBAGENT_RESULT,
        text="Subagent done.",
        metadata={
            "subagent_id": "abc",
            "output_file": str(escape),  # outside transcripts roots
        },
    )
    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR, persona="Polly",
        transcript_path=archive,
    )])
    patch_parser({archive: [parent]})
    response = client.get(
        "/api/v1/chat/operator/messages?include_subagents=true",
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert "subagent_transcript" not in body["messages"][0]["metadata"]


def test_messages_endpoint_include_subagents_inlines_with_real_parser(
    client, auth_headers, patch_registry, project_root,
    monkeypatch, tmp_path,
):
    """Real-parser regression for ``include_subagents=true`` (issue #2052).

    Does NOT monkeypatch ``parse_events_jsonl`` or ``parse_raw_subagent_jsonl``;
    instead writes both a real normalized ``events.jsonl`` archive
    (parent) and a real raw Claude subagent JSONL (child) under the
    project's ``.pollypm/transcripts/`` root so the allowlist accepts
    the path. Asserts the inlined ``metadata.subagent_transcript``
    actually contains envelopes derived from the raw shape — proves
    the round-4 contract gap (events.jsonl parser called on raw JSONL)
    is closed.
    """
    import json

    from pollypm.projects import project_transcripts_dir

    transcripts_root = project_transcripts_dir(project_root)
    # Parent surface archive (normalized shape).
    parent_dir = transcripts_root / "parent-session"
    parent_dir.mkdir(parents=True)
    parent_archive = parent_dir / "events.jsonl"
    # Child subagent JSONL (raw Claude shape) — must live under the
    # project transcripts root so the allowlist admits it.
    child_dir = transcripts_root / "child-subagent"
    child_dir.mkdir(parents=True)
    child_jsonl = child_dir / "raw.jsonl"
    child_jsonl.write_text(
        "\n".join([
            json.dumps({
                "type": "user",
                "sessionId": "child-1",
                "cwd": str(project_root),
                "timestamp": "2026-05-21T10:00:00Z",
                "message": {"content": "kick off subagent"},
            }),
            json.dumps({
                "type": "assistant",
                "sessionId": "child-1",
                "cwd": str(project_root),
                "timestamp": "2026-05-21T10:00:05Z",
                "message": {
                    "model": "claude-opus-4-7",
                    "content": [{"type": "text", "text": "subagent reply"}],
                },
            }),
        ]) + "\n"
    )

    # Parent normalized events.jsonl: spawn + result with the
    # task-notification pointing at the child raw JSONL.
    parent_events = [
        {
            "timestamp": "2026-05-21T09:59:00Z",
            "event_type": "tool_call",
            "session_id": "parent-1",
            "account_name": "claude_main",
            "provider": "claude",
            "project_key": "myproj",
            "source_path": str(parent_archive),
            "source_offset": 0,
            "cwd": str(project_root),
            "model_name": "claude-opus-4-7",
            "payload": {
                "type": "tool_use",
                "id": "toolu_sub_real",
                "name": "Task",
                "input": {"description": "Run the subagent"},
            },
        },
        {
            "timestamp": "2026-05-21T10:00:10Z",
            "event_type": "tool_result",
            "session_id": "parent-1",
            "account_name": "claude_main",
            "provider": "claude",
            "project_key": "myproj",
            "source_path": str(parent_archive),
            "source_offset": 1,
            "cwd": str(project_root),
            "model_name": "claude-opus-4-7",
            "payload": {
                "type": "tool_result",
                "tool_use_id": "toolu_sub_real",
                "content": [
                    {"type": "text", "text": "Subagent done."},
                    {
                        "type": "task-notification",
                        "task-id": "child-1",
                        "output-file": str(child_jsonl),
                        "duration-ms": 1234,
                        "total-tokens": 567,
                        "worktree-path": str(project_root),
                    },
                ],
            },
        },
    ]
    parent_archive.write_text(
        "\n".join(json.dumps(e) for e in parent_events) + "\n"
    )

    # Drop the in-process parse cache so a previous test's cached
    # entry can't shadow our fixture.
    from pollypm.web_api.chat.transcripts import _parse_cache_clear

    _parse_cache_clear()

    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR, persona="Polly",
        transcript_path=parent_archive,
    )])

    response = client.get(
        "/api/v1/chat/operator/messages?include_subagents=true&direction=asc",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # Locate the subagent_result envelope.
    result_envs = [
        m for m in body["messages"] if m["type"] == "subagent_result"
    ]
    assert len(result_envs) == 1, body
    metadata = result_envs[0]["metadata"]
    assert metadata["output_file"] == str(child_jsonl)
    transcript = metadata.get("subagent_transcript")
    assert isinstance(transcript, list)
    assert len(transcript) == 2
    # First child envelope: user turn with "kick off" text; second:
    # assistant turn with the reply.
    assert transcript[0]["role"] == "user"
    assert transcript[0]["type"] == "text"
    assert "kick off" in transcript[0]["text"]
    assert transcript[1]["role"] == "assistant"
    assert transcript[1]["type"] == "text"
    assert transcript[1]["text"] == "subagent reply"
    # Provenance marker so downstream consumers can distinguish
    # inlined-from-raw envelopes from the normalized stream.
    assert transcript[0]["metadata"]["source"] == "subagent_jsonl"
    assert transcript[1]["metadata"]["model"] == "claude-opus-4-7"


def test_include_subagents_does_not_poison_cache_for_subsequent_default_request(
    client, auth_headers, patch_registry, project_root,
    monkeypatch, tmp_path,
):
    """Cache-poisoning regression for ``include_subagents`` (Codex review #2083).

    The transcript parser cache stores ``MessageEnvelope`` instances by
    reference (the cache only copies the outer list, not the dataclass
    or its mutable ``metadata`` dict). A prior implementation mutated
    ``envelope.metadata['subagent_transcript']`` in place, so a single
    ``?include_subagents=true`` request would leave the inlined payload
    on the cached envelope and a subsequent default
    ``include_subagents=false`` (or omitted) request against the same
    archive/mtime would still return the inlined data — even though the
    client never opted in.

    This test uses the REAL parser / cache (no monkeypatched parser):

    1. First request with ``?include_subagents=true`` — envelope must
       carry ``metadata.subagent_transcript``.
    2. Second request with default opt-out (no query) against the SAME
       archive (mtime unchanged → cache HIT) — envelope must NOT carry
       ``metadata.subagent_transcript``.
    """
    import json

    from pollypm.projects import project_transcripts_dir

    transcripts_root = project_transcripts_dir(project_root)
    parent_dir = transcripts_root / "parent-session-cache"
    parent_dir.mkdir(parents=True)
    parent_archive = parent_dir / "events.jsonl"
    child_dir = transcripts_root / "child-subagent-cache"
    child_dir.mkdir(parents=True)
    child_jsonl = child_dir / "raw.jsonl"
    child_jsonl.write_text(
        "\n".join([
            json.dumps({
                "type": "user",
                "sessionId": "child-cache",
                "cwd": str(project_root),
                "timestamp": "2026-05-22T10:00:00Z",
                "message": {"content": "kick off subagent"},
            }),
            json.dumps({
                "type": "assistant",
                "sessionId": "child-cache",
                "cwd": str(project_root),
                "timestamp": "2026-05-22T10:00:05Z",
                "message": {
                    "model": "claude-opus-4-7",
                    "content": [{"type": "text", "text": "subagent reply"}],
                },
            }),
        ]) + "\n"
    )

    parent_events = [
        {
            "timestamp": "2026-05-22T09:59:00Z",
            "event_type": "tool_call",
            "session_id": "parent-cache",
            "account_name": "claude_main",
            "provider": "claude",
            "project_key": "myproj",
            "source_path": str(parent_archive),
            "source_offset": 0,
            "cwd": str(project_root),
            "model_name": "claude-opus-4-7",
            "payload": {
                "type": "tool_use",
                "id": "toolu_sub_cache",
                "name": "Task",
                "input": {"description": "Run the subagent"},
            },
        },
        {
            "timestamp": "2026-05-22T10:00:10Z",
            "event_type": "tool_result",
            "session_id": "parent-cache",
            "account_name": "claude_main",
            "provider": "claude",
            "project_key": "myproj",
            "source_path": str(parent_archive),
            "source_offset": 1,
            "cwd": str(project_root),
            "model_name": "claude-opus-4-7",
            "payload": {
                "type": "tool_result",
                "tool_use_id": "toolu_sub_cache",
                "content": [
                    {"type": "text", "text": "Subagent done."},
                    {
                        "type": "task-notification",
                        "task-id": "child-cache",
                        "output-file": str(child_jsonl),
                        "duration-ms": 1234,
                        "total-tokens": 567,
                        "worktree-path": str(project_root),
                    },
                ],
            },
        },
    ]
    parent_archive.write_text(
        "\n".join(json.dumps(e) for e in parent_events) + "\n"
    )

    # Capture the archive mtime so we can assert the second request
    # actually hits the parser cache (same mtime → cache HIT, which is
    # precisely the path that previously exposed the mutation bug).
    initial_mtime = parent_archive.stat().st_mtime

    # Drop any cached parse from prior tests so this run is clean.
    from pollypm.web_api.chat.transcripts import (
        _PARSE_CACHE,
        _parse_cache_clear,
    )

    _parse_cache_clear()

    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR, persona="Polly",
        transcript_path=parent_archive,
    )])

    # --- First request: opt IN to inlined subagent transcript. ---
    response_in = client.get(
        "/api/v1/chat/operator/messages?include_subagents=true&direction=asc",
        headers=auth_headers,
    )
    assert response_in.status_code == 200, response_in.text
    body_in = response_in.json()
    result_envs_in = [
        m for m in body_in["messages"] if m["type"] == "subagent_result"
    ]
    assert len(result_envs_in) == 1, body_in
    transcript_in = result_envs_in[0]["metadata"].get("subagent_transcript")
    assert isinstance(transcript_in, list)
    assert len(transcript_in) == 2

    # The parser cache should now hold a single entry for the parent
    # archive. The cached envelopes are the same objects we just
    # enriched; if the enrichment was in-place they would still carry
    # ``subagent_transcript`` and leak it into the next request.
    assert (parent_archive.resolve(), False) in _PARSE_CACHE
    cached_mtime, cached_envelopes, _ = _PARSE_CACHE[
        (parent_archive.resolve(), False)
    ]
    assert cached_mtime == initial_mtime
    for env in cached_envelopes:
        assert "subagent_transcript" not in (env.metadata or {}), (
            "Cached envelope metadata was mutated by include_subagents=true; "
            "later default requests will leak the inlined transcript."
        )

    # --- Second request: default (no ``include_subagents``). ---
    # Archive mtime is unchanged so the parser cache is hit; if the
    # enrichment had mutated the cached envelopes, this response would
    # still expose ``subagent_transcript`` even though the client did
    # not opt in.
    assert parent_archive.stat().st_mtime == initial_mtime
    response_default = client.get(
        "/api/v1/chat/operator/messages?direction=asc",
        headers=auth_headers,
    )
    assert response_default.status_code == 200, response_default.text
    body_default = response_default.json()
    result_envs_default = [
        m for m in body_default["messages"] if m["type"] == "subagent_result"
    ]
    assert len(result_envs_default) == 1, body_default
    metadata_default = result_envs_default[0]["metadata"]
    assert "subagent_transcript" not in metadata_default, (
        "Cache poisoning: a prior include_subagents=true request leaked "
        "metadata.subagent_transcript into a later default response."
    )


def test_messages_endpoint_include_subagents_default_no_inline(
    client, auth_headers, patch_registry, patch_parser, tmp_path,
):
    """Omitting ``include_subagents`` leaves ``subagent_transcript`` absent."""
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
# monkeypatch with a stale signature (e.g. accepting an
# ``include_thinking`` kwarg the route doesn't pass) or a fake
# capture that raised instead of the real fail-soft helper previously
# hid two TypeError / silent-empty bugs in production.
# ---------------------------------------------------------------------------


def test_messages_endpoint_jsonl_uses_real_parser_no_typeerror(
    client, auth_headers, patch_registry, tmp_path,
):
    """REAL ``parse_events_jsonl`` call — Blocker 1 regression.

    Historically the route's call signature drifted from the real
    parser's (e.g. the route briefly passed ``include_thinking=...``
    when the parser had dropped the kwarg in #2044). Tests passed
    because the fixture stub silently accepted the extra kwarg; in
    production every ``source=jsonl`` request raised ``TypeError``.
    #2048 (this PR) restored ``include_thinking`` on the parser, but
    the route still does NOT pass it — promoting thinking blocks to
    the HTTP surface is deferred to #2082. This test drives the real
    parser via an on-disk JSONL fixture so the wiring is exercised
    end-to-end and any future signature drift fails loudly.
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


# ---------------------------------------------------------------------------
# Mixed tz-aware/naive timestamps regression (PR #2045 v4 blocker 2)
# ---------------------------------------------------------------------------


def test_messages_endpoint_mixed_tz_timestamps_no_typeerror(
    client, auth_headers, patch_registry, patch_parser, tmp_path,
):
    """Mixed/missing ``ts`` values must NOT raise TypeError in sort.

    Before the fix, ``_apply_filters_and_paginate`` sorted with
    ``_parse_envelope_ts(...) or datetime.min`` — the sentinel was
    naive while the parsed values were aware, so a single row with
    ``ts=""`` or unparseable ``ts`` raised ``TypeError: can't compare
    offset-naive and offset-aware datetimes`` and crashed the
    endpoint with 500.

    All 3 rows must come back in the page (the unparseable ones sort
    to the head; their ``ts`` field surfaces as-is in the response).
    """
    archive = tmp_path / "events.jsonl"
    archive.write_text("x")
    envelopes = [
        _env("blank", ts=""),
        _env("good", ts="2026-05-21T10:00:00Z"),
        _env("garbage", ts="not-a-timestamp-at-all"),
    ]
    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR, persona="Polly",
        transcript_path=archive,
    )])
    patch_parser({archive: envelopes})

    response = client.get(
        "/api/v1/chat/operator/messages?direction=asc",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    ids = [m["id"] for m in body["messages"]]
    # All 3 envelopes round-trip; unparseable rows sort to the head
    # (sentinel is the tz-aware UTC ``datetime.min``).
    assert set(ids) == {"blank", "good", "garbage"}
    assert ids[-1] == "good"  # parseable timestamp sorts last in asc order


def test_messages_endpoint_naive_ts_does_not_crash_sort(
    client, auth_headers, patch_registry, patch_parser, tmp_path,
):
    """A naive ``ts`` (no Z / no offset) must coerce to UTC, not crash.

    The transcript writer normalizes to ``...Z`` but capture-mode
    paths and stale archives might surface ``2026-05-21T10:00:00``
    (no suffix). Before the fix, mixing one such row with a normal
    ``...Z`` row raised TypeError from the sort comparator.
    """
    archive = tmp_path / "events.jsonl"
    archive.write_text("x")
    envelopes = [
        _env("naive", ts="2026-05-21T09:00:00"),  # no tz suffix
        _env("aware", ts="2026-05-21T10:00:00Z"),  # explicit Z
    ]
    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR, persona="Polly",
        transcript_path=archive,
    )])
    patch_parser({archive: envelopes})
    response = client.get(
        "/api/v1/chat/operator/messages?direction=asc",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # Naive treated as UTC → "naive" (09:00) sorts before "aware" (10:00).
    assert [m["id"] for m in body["messages"]] == ["naive", "aware"]


# ---------------------------------------------------------------------------
# Lazy work-service open for non-worker lookups (PR #2045 v4 blocker 3)
# ---------------------------------------------------------------------------


def test_messages_endpoint_operator_lookup_skips_work_service(
    client, auth_headers, monkeypatch, tmp_path,
):
    """Operator lookup must NOT open the work-service.

    Before the fix, every chat-messages request called
    ``_build_work_service_stub`` regardless of whether the
    ``session_name`` matched the worker pattern. That made pg the
    critical path for operator/architect/advisor lookups and turned
    a pool outage into a misleading 404.
    """
    archive = tmp_path / "events.jsonl"
    archive.write_text("x")

    open_count = {"n": 0}

    def counting_stub(config, *, strict=False):
        open_count["n"] += 1
        return None

    monkeypatch.setattr(
        chat_messages_routes, "_build_work_service_stub", counting_stub,
    )
    monkeypatch.setattr(
        chat_messages_routes, "_build_tmux_client", lambda: None,
    )

    def fake_enumerate(config, work_service=None, tmux_client=None):
        return [_surface(
            "operator", SurfaceType.OPERATOR, persona="Polly",
            transcript_path=archive,
        )]

    monkeypatch.setattr(
        chat_messages_routes, "enumerate_chat_surfaces", fake_enumerate,
    )
    monkeypatch.setattr(
        chat_messages_routes, "parse_events_jsonl",
        lambda path, *, actor_fallback="agent", strict=False: [],
    )
    response = client.get(
        "/api/v1/chat/operator/messages",
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert open_count["n"] == 0, (
        "non-worker lookup must not probe the work-service"
    )


def test_messages_endpoint_architect_lookup_skips_work_service(
    client, auth_headers, monkeypatch, tmp_path,
):
    """Same as operator — architect_<project> lookups skip work-service."""
    archive = tmp_path / "events.jsonl"
    archive.write_text("x")

    open_count = {"n": 0}

    def counting_stub(config, *, strict=False):
        open_count["n"] += 1
        return None

    monkeypatch.setattr(
        chat_messages_routes, "_build_work_service_stub", counting_stub,
    )
    monkeypatch.setattr(
        chat_messages_routes, "_build_tmux_client", lambda: None,
    )

    def fake_enumerate(config, work_service=None, tmux_client=None):
        return [_surface(
            "architect_myproj", SurfaceType.ARCHITECT, persona="Archie",
            project="myproj", transcript_path=archive,
        )]

    monkeypatch.setattr(
        chat_messages_routes, "enumerate_chat_surfaces", fake_enumerate,
    )
    monkeypatch.setattr(
        chat_messages_routes, "parse_events_jsonl",
        lambda path, *, actor_fallback="agent", strict=False: [],
    )
    response = client.get(
        "/api/v1/chat/architect_myproj/messages",
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert open_count["n"] == 0


def test_messages_endpoint_worker_lookup_opens_work_service(
    client, auth_headers, monkeypatch, tmp_path,
):
    """Worker lookup MUST probe the work-service (positive control).

    Pairs with the two skip-tests above so a future refactor can't
    "fix" the skip tests by removing the worker probe entirely.
    """
    archive = tmp_path / "task.jsonl"
    archive.write_text("x")

    open_count = {"n": 0}

    def counting_stub(config):
        open_count["n"] += 1
        return None

    # Worker lookups go through the strict facade variant — that's the
    # one whose call we need to count (the fail-soft variant is only
    # invoked from the discovery endpoint).
    monkeypatch.setattr(
        chat_messages_routes,
        "_build_work_service_stub_strict",
        counting_stub,
    )
    monkeypatch.setattr(
        chat_messages_routes, "_build_work_service_stub",
        lambda config: None,
    )
    monkeypatch.setattr(
        chat_messages_routes, "_build_tmux_client", lambda: None,
    )

    def fake_enumerate(config, work_service=None, tmux_client=None):
        return [_surface(
            "task-myproj-7", SurfaceType.WORKER, persona=None,
            project="myproj", task_id=7, transcript_path=archive,
        )]

    monkeypatch.setattr(
        chat_messages_routes, "enumerate_chat_surfaces", fake_enumerate,
    )
    monkeypatch.setattr(
        chat_messages_routes, "parse_events_jsonl",
        lambda path, *, actor_fallback="agent", strict=False: [],
    )
    response = client.get(
        "/api/v1/chat/task-myproj-7/messages",
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert open_count["n"] == 1


def test_messages_endpoint_worker_lookup_503s_when_facade_unavailable(
    client, auth_headers, monkeypatch, tmp_path,
):
    """Worker lookup + facade outage → 503 service_unavailable, not 404.

    Before the fix, ``_build_work_service_stub`` swallowed every
    exception and returned ``None``, which made
    ``enumerate_chat_surfaces`` skip worker enumeration. A worker
    lookup then fell through to 404 ``session_unknown`` — the client
    was told "this surface doesn't exist" when really pg was down.
    """
    # Simulate the strict-mode failure: the strict stub builder
    # raises _WorkerFacadeUnavailable when the facade can't be opened.
    def boom(config):
        raise chat_messages_routes._WorkerFacadeUnavailable(
            "pg pool drained"
        )

    monkeypatch.setattr(
        chat_messages_routes, "_build_work_service_stub_strict", boom,
    )
    monkeypatch.setattr(
        chat_messages_routes, "_build_work_service_stub",
        lambda config: None,
    )
    monkeypatch.setattr(
        chat_messages_routes, "_build_tmux_client", lambda: None,
    )
    response = client.get(
        "/api/v1/chat/task-myproj-7/messages",
        headers=auth_headers,
    )
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "service_unavailable"
    assert "work-service" in body["error"]["message"]


def test_is_worker_session_pattern():
    """``_is_worker_session`` matches the canonical task-<project>-<n> form."""
    assert chat_messages_routes._is_worker_session("task-myproj-7")
    assert chat_messages_routes._is_worker_session("task-my-proj-123")
    assert chat_messages_routes._is_worker_session("task-myproj.v2-1")
    # Negative cases — non-worker session names.
    assert not chat_messages_routes._is_worker_session("operator")
    assert not chat_messages_routes._is_worker_session("architect_myproj")
    assert not chat_messages_routes._is_worker_session("advisor_myproj")
    assert not chat_messages_routes._is_worker_session("task-myproj")  # no n
    assert not chat_messages_routes._is_worker_session("task-myproj-abc")
