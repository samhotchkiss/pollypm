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

import json
import subprocess
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
from pollypm.models import (
    KnownProject,
    ProjectKind,
    ProviderKind,
    RuntimeKind,
    SessionConfig,
)
from pollypm.projects import project_transcripts_dir
from pollypm.web_api import create_app, ensure_token
from pollypm.web_api.auth import SESSION_COOKIE_NAME
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
from pollypm.web_api import service as web_api_service


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


def test_worker_session_facade_forwards_project_filter(
    config: PollyPMConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    class FakeWorkService:
        def list_worker_sessions(
            self, *, project: str | None = None, active_only: bool = True
        ) -> list[object]:
            seen["project"] = project
            seen["active_only"] = active_only
            return []

    class FakeWorkServiceContext:
        def __enter__(self) -> FakeWorkService:
            return FakeWorkService()

        def __exit__(self, *exc: object) -> bool:
            return False

    monkeypatch.setattr(
        web_api_service,
        "_open_work_service_readonly",
        lambda **_kwargs: FakeWorkServiceContext(),
    )

    assert web_api_service.list_active_worker_sessions_strict(
        config, project="myproj"
    ) == []
    assert seen == {"project": "myproj", "active_only": True}

    seen.clear()
    assert web_api_service.list_active_worker_sessions(
        config, project="other"
    ) == []
    assert seen == {"project": "other", "active_only": True}


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
        def fake_find(config, session_name, work_service=None, tmux_client=None):
            for surface in surfaces:
                if surface.session_name == session_name:
                    return surface
            return None
        monkeypatch.setattr(
            chat_messages_routes, "enumerate_chat_surfaces", fake,
        )
        monkeypatch.setattr(
            chat_messages_routes, "find_chat_surface", fake_find,
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
        monkeypatch.setattr(
            chat_messages_routes,
            "_build_work_service_stub_strict",
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

    Signature mirrors the real parser on main
    (:func:`pollypm.web_api.chat.transcripts.parse_events_jsonl`),
    including the ``include_thinking`` knob the route plumbs through
    (#2082). Tests that need real parser behavior (Blocker 1) drive
    the on-disk parser directly via a JSONL fixture instead of
    installing this stub.
    """
    def install(envelopes_by_path: dict[Path, list[MessageEnvelope]]) -> None:
        def fake(
            path, *, actor_fallback="agent", strict=False,
            include_thinking=False,
        ):
            # ``strict`` is accepted for forward-compat with the
            # ``source=auto`` unreadable-archive fallback added in the
            # round-6 fix; this stub treats it as a no-op (returns the
            # same envelopes regardless of strict mode). Likewise the
            # ``include_thinking`` flag is accepted so the route's
            # plumbing path (#2082) doesn't trip a TypeError; the stub
            # returns the same list either way and individual tests
            # assert the route's filtering / non-filtering behavior.
            del strict, include_thinking
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


def test_sessions_endpoint_cookie_auth_uses_bounded_tmux_probe(
    client, auth_headers, config, workspace, monkeypatch,
):
    class SlowTmuxClient:
        timeout: int | None = None

        def list_windows(self, name: str, *, timeout: int | None = None):
            self.timeout = timeout
            raise subprocess.TimeoutExpired(
                cmd=["tmux", "list-windows", "-t", name],
                timeout=timeout,
            )

    tmux = SlowTmuxClient()
    token = auth_headers["Authorization"].removeprefix("Bearer ")
    config.sessions["operator"] = SessionConfig(
        name="operator",
        role="operator-pm",
        provider=ProviderKind.CODEX,
        account="codex_primary",
        cwd=workspace,
        project="myproj",
        window_name="pm-operator",
    )
    monkeypatch.setattr(
        chat_messages_routes, "_build_tmux_client", lambda: tmux,
    )
    monkeypatch.setattr(
        chat_messages_routes,
        "_build_work_service_stub",
        lambda _config, **_kw: None,
    )

    boot = client.get("/ui/", headers=auth_headers)
    assert boot.status_code == 200
    assert boot.cookies.get(SESSION_COOKIE_NAME) == token

    response = client.get("/api/v1/chat/sessions")
    assert response.status_code == 200, response.text
    body = response.json()
    assert [session["session_name"] for session in body["sessions"]] == [
        "operator",
    ]
    assert body["sessions"][0]["window"]["present"] is False
    assert tmux.timeout == 1


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
    assert "transcript_path" not in body
    assert [m["id"] for m in body["messages"]] == ["msg_1", "msg_2"]
    assert body["has_more"] is False
    assert body["next_cursor"] is None


def test_messages_endpoint_resolves_one_config_surface_without_full_enumeration(
    client,
    auth_headers,
    config,
    workspace,
    project_root,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The message hot path must not build the whole session sidebar.

    A single configured session can be resolved from ``config.sessions``
    and that project's transcript index. Regressing to
    ``enumerate_chat_surfaces`` would rescan every project transcript
    root and reopen the worker facade before every message fetch.
    """
    transcript_dir = project_transcripts_dir(project_root) / "provider-session"
    transcript_dir.mkdir(parents=True)
    archive = transcript_dir / "events.jsonl"
    archive.write_text(
        json.dumps({
            "timestamp": "2026-05-21T10:00:00Z",
            "event_type": "assistant_turn",
            "session_id": "provider-session",
            "account_name": "codex_primary",
            "provider": "codex",
            "project_key": "myproj",
            "source_path": "/tmp/raw.jsonl",
            "source_offset": 0,
            "cwd": str(workspace),
            "model_name": "gpt-5",
            "payload": {"text": "direct lookup"},
        }) + "\n",
        encoding="utf-8",
    )
    config.sessions["operator"] = SessionConfig(
        name="operator",
        role="operator-pm",
        provider=ProviderKind.CODEX,
        account="codex_primary",
        cwd=workspace,
        project="myproj",
        window_name="operator",
    )
    monkeypatch.setattr(chat_messages_routes, "_build_tmux_client", lambda: None)
    monkeypatch.setattr(
        chat_messages_routes,
        "_build_work_service_stub_strict",
        lambda *_args, **_kwargs: pytest.fail(
            "non-worker lookup should not open work-service"
        ),
    )
    monkeypatch.setattr(
        chat_messages_routes,
        "enumerate_chat_surfaces",
        lambda *_args, **_kwargs: pytest.fail(
            "message lookup should not enumerate every surface"
        ),
    )

    response = client.get(
        "/api/v1/chat/operator/messages?source=jsonl&direction=asc",
        headers=auth_headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["session_name"] == "operator"
    assert [message["text"] for message in body["messages"]] == [
        "direct lookup",
    ]


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
    assert "transcript_path" not in body


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


def test_messages_endpoint_drops_thinking_envelopes_by_default(
    client, auth_headers, patch_registry, tmp_path,
):
    """Default ``include_thinking=false`` — thinking envelopes are
    gated at the parser layer and never reach the response.

    Drives the real parser via an on-disk JSONL fixture so the route's
    default-False call to ``parse_events_jsonl`` is exercised end-to-end.
    """
    import json as _json

    archive = tmp_path / "events.jsonl"
    events = [
        {
            "timestamp": "2026-05-21T10:00:00Z",
            "event_type": "thinking",
            "session_id": "s",
            "account_name": "claude_main",
            "provider": "claude",
            "project_key": "myproj",
            "source_path": "/tmp/raw.jsonl",
            "source_offset": 0,
            "cwd": "/tmp/repo",
            "model_name": "claude-opus-4-7",
            "payload": {
                "text": "I should think first.",
                "signature": "opaque",
                "raw": {
                    "type": "thinking",
                    "thinking": "I should think first.",
                    "signature": "opaque",
                },
            },
        },
        {
            "timestamp": "2026-05-21T10:00:01Z",
            "event_type": "assistant_turn",
            "session_id": "s",
            "account_name": "claude_main",
            "provider": "claude",
            "project_key": "myproj",
            "source_path": "/tmp/raw.jsonl",
            "source_offset": 1,
            "cwd": "/tmp/repo",
            "model_name": "claude-opus-4-7",
            "payload": {"text": "Public reply."},
        },
    ]
    archive.write_text("\n".join(_json.dumps(ev) for ev in events) + "\n")
    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR, persona="Polly",
        transcript_path=archive,
    )])
    # NOTE: no patch_parser — drive the real parser so the
    # default-False include_thinking branch is exercised.
    from pollypm.web_api.chat.transcripts import _parse_cache_clear
    _parse_cache_clear()
    body = client.get(
        "/api/v1/chat/operator/messages?direction=asc",
        headers=auth_headers,
    ).json()
    types = [m["type"] for m in body["messages"]]
    assert "thinking" not in types
    assert "text" in types


def test_messages_endpoint_emits_thinking_envelopes_when_include_thinking_true(
    client, auth_headers, patch_registry, tmp_path,
):
    """Opt-in ``include_thinking=true`` — thinking envelopes appear in
    the response alongside normal turns (#2082).

    Drives the real parser via an on-disk JSONL fixture so the route's
    plumbing of the flag into ``parse_events_jsonl`` is exercised
    end-to-end.
    """
    import json as _json

    archive = tmp_path / "events.jsonl"
    events = [
        {
            "timestamp": "2026-05-21T10:00:00Z",
            "event_type": "thinking",
            "session_id": "s",
            "account_name": "claude_main",
            "provider": "claude",
            "project_key": "myproj",
            "source_path": "/tmp/raw.jsonl",
            "source_offset": 0,
            "cwd": "/tmp/repo",
            "model_name": "claude-opus-4-7",
            "payload": {
                "text": "I should think first.",
                "signature": "opaque",
                "raw": {
                    "type": "thinking",
                    "thinking": "I should think first.",
                    "signature": "opaque",
                },
            },
        },
        {
            "timestamp": "2026-05-21T10:00:01Z",
            "event_type": "assistant_turn",
            "session_id": "s",
            "account_name": "claude_main",
            "provider": "claude",
            "project_key": "myproj",
            "source_path": "/tmp/raw.jsonl",
            "source_offset": 1,
            "cwd": "/tmp/repo",
            "model_name": "claude-opus-4-7",
            "payload": {"text": "Public reply."},
        },
    ]
    archive.write_text("\n".join(_json.dumps(ev) for ev in events) + "\n")
    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR, persona="Polly",
        transcript_path=archive,
    )])
    from pollypm.web_api.chat.transcripts import _parse_cache_clear
    _parse_cache_clear()
    body = client.get(
        "/api/v1/chat/operator/messages?include_thinking=true&direction=asc",
        headers=auth_headers,
    ).json()
    types = [m["type"] for m in body["messages"]]
    # Both the thinking envelope and the assistant turn surface, in order.
    assert types == ["thinking", "text"]
    thinking_msg = body["messages"][0]
    assert thinking_msg["role"] == "assistant"
    assert thinking_msg["text"] == "I should think first."
    assert thinking_msg["metadata"]["signature"] == "opaque"


def test_messages_endpoint_include_thinking_surfaces_under_default_curl(
    client, auth_headers, patch_registry, tmp_path,
):
    """Regression for #2160: the user-facing default curl

        GET /api/v1/chat/<name>/messages?include_thinking=true&limit=200

    must return at least one ``type:thinking`` envelope when the
    archive contains any, even when the archive is large enough that
    the route's tail-read optimization (issue #2070) would otherwise
    activate.

    Wave 4A verified that #2208's stale-archive fix did not reach the
    user-facing path: 39/39 sessions returned zero thinking under the
    default curl. Root cause: when ``limit <= 200`` + ``direction=desc``
    + ``source=auto``, the route set ``tail_hint = limit + 1`` and
    used :func:`parse_events_jsonl_tail`, which reads only the last
    ~``chunk_size`` bytes of the file. Thinking events written earlier
    in a long conversation were silently dropped from the response.

    This test exercises the exact user-facing shape (default
    direction=desc, ``limit=200``) against a multi-thousand-line
    archive whose thinking block falls outside the tail-read window
    but inside the desc-200 page by timestamp. Before the fix the
    response is text-only; after the fix the thinking envelope is
    surfaced alongside the recent turns.
    """
    import json as _json

    archive = tmp_path / "events.jsonl"
    events: list[dict[str, Any]] = []
    # The thinking block lives at the HEAD of the file (model-side
    # reasoning at the start of a long task). Its timestamp is
    # arranged so it falls inside the desc-200 page when a full
    # forward parse is used — i.e., a working response includes it.
    events.append({
        "timestamp": "2026-05-21T20:00:00Z",
        "event_type": "thinking",
        "session_id": "s",
        "account_name": "claude_main",
        "provider": "claude",
        "project_key": "myproj",
        "source_path": "/tmp/raw.jsonl",
        "source_offset": 0,
        "cwd": "/tmp/repo",
        "model_name": "claude-opus-4-7",
        "payload": {
            "text": "Hidden reasoning the user opted in to see.",
            "signature": "opt-in-sig",
            "raw": {
                "type": "thinking",
                "thinking": "Hidden reasoning the user opted in to see.",
                "signature": "opt-in-sig",
            },
        },
    })
    # Push the archive past the 1 MB tail-read ceiling so the
    # tail-read path NEVER reaches byte 0 (where the thinking lives).
    # Each padding event is ~5 KB so 250+ events comfortably exceeds
    # 1 MB. Their timestamps are OLDER than the thinking event's so
    # they don't crowd the desc-200 page out from under it.
    big_pad = "y" * 5000
    for i in range(250):
        events.append({
            "timestamp": f"2026-05-21T10:{i // 60:02d}:{i % 60:02d}Z",
            "event_type": "assistant_turn",
            "session_id": "s",
            "account_name": "claude_main",
            "provider": "claude",
            "project_key": "myproj",
            "source_path": "/tmp/raw.jsonl",
            "source_offset": i + 1,
            "cwd": "/tmp/repo",
            "model_name": "claude-opus-4-7",
            "payload": {"text": f"old pad turn {i} {big_pad}"},
        })
    # Recent text turns at the file tail. These DO appear in the
    # tail-read window (so tail-read returns 30+ envelopes from this
    # bunch) and they all have NEWER timestamps than the padding —
    # but older than the thinking. After full-parse desc-sort the
    # thinking is the newest envelope of all 281 in the file.
    for i in range(30):
        events.append({
            "timestamp": f"2026-05-21T19:{i:02d}:00Z",
            "event_type": "assistant_turn",
            "session_id": "s",
            "account_name": "claude_main",
            "provider": "claude",
            "project_key": "myproj",
            "source_path": "/tmp/raw.jsonl",
            "source_offset": 251 + i,
            "cwd": "/tmp/repo",
            "model_name": "claude-opus-4-7",
            "payload": {"text": f"recent turn {i}"},
        })
    archive.write_text("\n".join(_json.dumps(ev) for ev in events) + "\n")
    # Sanity: file must exceed the largest tail-read chunk (1 MB)
    # so the thinking at byte 0 is never inside the tail window.
    assert archive.stat().st_size > 1_100_000
    patch_registry([_surface(
        "operator", SurfaceType.OPERATOR, persona="Polly",
        transcript_path=archive,
    )])
    from pollypm.web_api.chat.transcripts import _parse_cache_clear
    _parse_cache_clear()
    # Default direction is ``desc`` and limit=200 — the exact shape
    # the cockpit / users send. Before the fix the route would
    # tail-read only the last ~64 KB (~250-300 short envelopes worth)
    # and drop the thinking block that lived just ahead of that window.
    response = client.get(
        "/api/v1/chat/operator/messages?include_thinking=true&limit=200",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["transcript_source"] == "jsonl"
    types = [m["type"] for m in body["messages"]]
    assert "thinking" in types, (
        f"expected at least one thinking envelope, got types={set(types)} "
        f"(count={len(types)}); regression for #2160"
    )
    thinking_msgs = [m for m in body["messages"] if m["type"] == "thinking"]
    assert thinking_msgs[0]["text"] == "Hidden reasoning the user opted in to see."
    assert thinking_msgs[0]["metadata"]["signature"] == "opt-in-sig"


def test_messages_endpoint_include_thinking_reaches_ingested_surface_archive(
    client, auth_headers, config, project_root, monkeypatch,
):
    """A real surface + ingested Claude transcript can return thinking.

    Regression for #2160: ``source=auto`` used to choose stale tmux
    capture before reading the archive, so ``include_thinking=true``
    returned zero thinking envelopes even when the raw Claude JSONL had
    an Anthropic ``thinking`` block. This drives the real ingestor,
    real surface registry, and real REST route; only tmux is faked.
    """
    import json as _json

    from pollypm.transcript_ingest import sync_transcripts_once
    from pollypm.tmux.client import TmuxWindow
    from pollypm.web_api.chat.transcripts import _parse_cache_clear

    account_home = project_root / ".pollypm/homes/claude_main"
    config.accounts["claude_main"] = AccountConfig(
        name="claude_main",
        provider=ProviderKind.CLAUDE,
        home=account_home,
    )
    config.pollypm.controller_account = "claude_main"
    config.sessions["operator"] = SessionConfig(
        name="operator",
        role="operator-pm",
        provider=ProviderKind.CLAUDE,
        account="claude_main",
        cwd=project_root,
        project="myproj",
        window_name="operator",
    )

    raw_path = (
        account_home
        / ".claude/projects/demo/session-rest-thinking.jsonl"
    )
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(_json.dumps({
        "type": "assistant",
        "sessionId": "session-rest-thinking",
        "timestamp": "2026-05-21T10:00:00Z",
        "cwd": str(project_root),
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-7",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "Route-level thought.",
                    "signature": "sig-route",
                },
                {"type": "text", "text": "Visible reply."},
            ],
        },
    }) + "\n")
    sync_transcripts_once(config)
    archive = (
        project_root
        / ".pollypm/transcripts/session-rest-thinking/events.jsonl"
    )
    assert archive.exists()
    _parse_cache_clear()

    class _LiveTmuxClient:
        def list_windows(self, target, timeout=None):  # noqa: ARG002
            return [TmuxWindow(
                session=target,
                index=0,
                name="operator",
                active=True,
                pane_id="%2160",
                pane_current_command="claude",
                pane_current_path=str(project_root),
                pane_dead=False,
            )]

        def capture_pane(self, target, lines=3000):  # noqa: ARG002
            return "live capture without hidden thinking"

    monkeypatch.setattr(
        chat_messages_routes,
        "_build_tmux_client",
        lambda: _LiveTmuxClient(),
    )
    monkeypatch.setattr(
        chat_messages_routes,
        "is_archive_stale",
        lambda path, **kw: True,
    )

    default_response = client.get(
        "/api/v1/chat/operator/messages?direction=asc",
        headers=auth_headers,
    )
    assert default_response.status_code == 200, default_response.text
    default_body = default_response.json()
    assert default_body["transcript_source"] == "capture"
    assert [m["type"] for m in default_body["messages"]] == ["text"]

    thinking_response = client.get(
        "/api/v1/chat/operator/messages?include_thinking=true&direction=asc",
        headers=auth_headers,
    )
    assert thinking_response.status_code == 200, thinking_response.text
    body = thinking_response.json()
    assert body["transcript_source"] == "jsonl"
    assert [m["type"] for m in body["messages"]] == ["thinking", "text"]
    assert body["messages"][0]["text"] == "Route-level thought."
    assert body["messages"][0]["metadata"]["signature"] == "sig-route"
    assert body["messages"][1]["text"] == "Visible reply."


def test_messages_endpoint_include_thinking_uses_effective_account_archive(
    client, auth_headers, config, workspace, project_root, monkeypatch,
):
    """Failover-account transcripts remain registered REST surfaces."""
    import json as _json

    from pollypm.web_api.chat.transcripts import _parse_cache_clear

    config.accounts["claude_primary"] = AccountConfig(
        name="claude_primary",
        provider=ProviderKind.CLAUDE,
        home=project_root / ".pollypm/homes/claude_primary",
    )
    config.accounts["claude_backup"] = AccountConfig(
        name="claude_backup",
        provider=ProviderKind.CLAUDE,
        home=project_root / ".pollypm/homes/claude_backup",
    )
    config.pollypm.failover_enabled = True
    config.pollypm.failover_accounts = ["claude_backup"]
    config.sessions["operator"] = SessionConfig(
        name="operator",
        role="operator-pm",
        provider=ProviderKind.CLAUDE,
        account="claude_primary",
        cwd=workspace,
        project="myproj",
        window_name="operator",
    )

    transcripts = project_root / ".pollypm/transcripts"

    def event(
        session_id: str,
        *,
        account_name: str,
        event_type: str,
        timestamp: str,
        text: str,
    ) -> dict[str, Any]:
        payload = (
            {"text": text, "signature": "sig-effective"}
            if event_type == "thinking"
            else {"text": text}
        )
        return {
            "timestamp": timestamp,
            "event_type": event_type,
            "session_id": session_id,
            "account_name": account_name,
            "provider": "claude",
            "project_key": "myproj",
            "source_path": "/tmp/raw.jsonl",
            "source_offset": 0,
            "cwd": str(project_root),
            "model_name": "claude-opus-4-7",
            "payload": payload,
        }

    primary_archive = transcripts / "session-primary/events.jsonl"
    primary_archive.parent.mkdir(parents=True)
    primary_archive.write_text(_json.dumps(event(
        "session-primary",
        account_name="claude_primary",
        event_type="assistant_turn",
        timestamp="2026-05-21T09:00:00Z",
        text="Primary account text.",
    )) + "\n")

    backup_archive = transcripts / "session-backup/events.jsonl"
    backup_archive.parent.mkdir(parents=True)
    backup_archive.write_text(
        _json.dumps(event(
            "session-backup",
            account_name="claude_backup",
            event_type="thinking",
            timestamp="2026-05-21T10:00:00Z",
            text="Failover thought.",
        ))
        + "\n"
        + _json.dumps(event(
            "session-backup",
            account_name="claude_backup",
            event_type="assistant_turn",
            timestamp="2026-05-21T10:00:01Z",
            text="Failover visible reply.",
        ))
        + "\n"
    )

    monkeypatch.setattr(
        chat_messages_routes,
        "_build_tmux_client",
        lambda: None,
    )
    monkeypatch.setattr(
        chat_messages_routes,
        "_build_effective_account_map",
        lambda _config: {"operator": "claude_backup"},
    )
    _parse_cache_clear()

    sessions_response = client.get(
        "/api/v1/chat/sessions",
        headers=auth_headers,
    )
    assert sessions_response.status_code == 200, sessions_response.text
    operator = next(
        s for s in sessions_response.json()["sessions"]
        if s["session_name"] == "operator"
    )
    assert operator["transcript"]["source"] == "jsonl"
    assert operator["transcript"]["path"] == str(backup_archive)

    response = client.get(
        "/api/v1/chat/operator/messages?include_thinking=true&limit=200",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["transcript_source"] == "jsonl"
    types = [m["type"] for m in body["messages"]]
    assert "thinking" in types
    thinking = next(m for m in body["messages"] if m["type"] == "thinking")
    assert thinking["text"] == "Failover thought."
    assert thinking["metadata"]["signature"] == "sig-effective"


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
    assert "transcript_path" not in body
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
# pin the production wiring by driving the REAL helpers — a fake
# capture that raised instead of the real fail-soft helper (or a
# parser-stub signature mismatch like the one Blocker 1 caught)
# previously hid TypeError / silent-empty bugs in production.
# ---------------------------------------------------------------------------


def test_messages_endpoint_jsonl_uses_real_parser_no_typeerror(
    client, auth_headers, patch_registry, tmp_path,
):
    """REAL ``parse_events_jsonl`` call — Blocker 1 regression.

    Pins the route → parser kwarg surface. Historically this test caught
    a ``include_thinking=...`` plumbing mismatch where the fixture stub
    accepted a kwarg the real parser did not (#2044). Today the parser
    accepts ``include_thinking`` (#2082) and the route forwards it
    through; the test still pins the real wiring so any future
    signature drift trips here instead of in production.
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
    assert "transcript_path" not in body
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
        lambda path, *, actor_fallback="agent", strict=False,
        include_thinking=False: [],
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
        lambda path, *, actor_fallback="agent", strict=False,
        include_thinking=False: [],
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
        lambda path, *, actor_fallback="agent", strict=False,
        include_thinking=False: [],
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
