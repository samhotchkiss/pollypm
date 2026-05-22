"""P3 chat-endpoints spec — POST /api/v1/chat/{session}/send tests.

Run via:

    pytest --noconftest tests/test_chat_send_endpoint.py -v

The ``--noconftest`` flag skips the repo's heavy conftest (which spins
up pg pools etc.); every fixture this test needs is defined inline.

Coverage targets §2.3 (endpoint contract) and §4.1 / §4.3 / §4.5
(safety gates + AskUserQuestion answer translation) of
``~/Desktop/pollypm-chat-endpoints-spec.md``. The TmuxClient is fully
mocked — these are router-level integration tests, not real tmux.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
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
from pollypm.tmux.client import DeadPaneError
from pollypm.web_api import create_app, ensure_token
from pollypm.web_api.routes import chat_send as chat_send_routes


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeWindow:
    name: str
    pane_dead: bool = False
    pane_id: str = "%0"


class FakeTmuxClient:
    """In-memory stand-in for :class:`pollypm.tmux.client.TmuxClient`.

    Captures send_keys calls so tests can assert on the target / text /
    press_enter triple. Configurable per-instance windows + send_keys
    side effects make the negative paths (window missing, pane dead)
    trivial to assert without monkeypatching subprocess.
    """

    windows_by_session: dict[str, list[FakeWindow]] = {}
    send_side_effect: Exception | None = None
    send_calls: list[tuple[str, str, bool]] = []

    def list_windows(self, session: str) -> list[FakeWindow]:
        return list(self.windows_by_session.get(session, []))

    def send_keys(self, target: str, text: str, press_enter: bool = True) -> None:
        if self.send_side_effect is not None:
            raise self.send_side_effect
        self.send_calls.append((target, text, press_enter))


@pytest.fixture(autouse=True)
def _reset_fake_tmux():
    FakeTmuxClient.windows_by_session = {}
    FakeTmuxClient.send_side_effect = None
    FakeTmuxClient.send_calls = []
    yield


@pytest.fixture
def patched_tmux(monkeypatch: pytest.MonkeyPatch) -> type[FakeTmuxClient]:
    """Swap :class:`TmuxClient` in the router module for the fake."""
    monkeypatch.setattr(chat_send_routes, "TmuxClient", FakeTmuxClient)
    return FakeTmuxClient


# ---------------------------------------------------------------------------
# Config / app fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "myproj"
    root.mkdir()
    (root / ".pollypm").mkdir()
    (root / ".pollypm" / "transcripts").mkdir()
    return root


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / ".pollypm").mkdir()
    return root


@pytest.fixture
def api_config(project_root: Path, workspace_root: Path) -> PollyPMConfig:
    base_dir = workspace_root / ".pollypm"
    state_db = base_dir / "state.db"
    return PollyPMConfig(
        project=ProjectSettings(
            name="PollyPM",
            root_dir=workspace_root,
            tmux_session="pollypm-test",
            workspace_root=workspace_root,
            base_dir=base_dir,
            logs_dir=base_dir / "logs",
            snapshots_dir=base_dir / "snapshots",
            state_db=state_db,
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
        sessions={
            "operator": SessionConfig(
                name="operator",
                role="operator",
                provider=ProviderKind.CLAUDE,
                account="codex_primary",
                cwd=workspace_root,
                project="myproj",
                window_name="pm-operator",
            ),
            "architect_myproj": SessionConfig(
                name="architect_myproj",
                role="architect",
                provider=ProviderKind.CLAUDE,
                account="codex_primary",
                cwd=workspace_root,
                project="myproj",
                window_name="pm-architect-myproj",
            ),
        },
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
def token_path(tmp_path: Path) -> Path:
    return tmp_path / "api-token"


@pytest.fixture
def token(token_path: Path) -> str:
    value, _ = ensure_token(token_path)
    return value


@pytest.fixture
def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(api_config: PollyPMConfig, token_path: Path, token: str) -> TestClient:  # noqa: ARG001
    app = create_app(config=api_config, token_path=token_path)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers to seed transcript / heartbeat fixtures
# ---------------------------------------------------------------------------


def _write_events_jsonl(
    project_root: Path,
    session_id: str,
    events: list[dict[str, Any]],
) -> Path:
    transcripts = project_root / ".pollypm" / "transcripts" / session_id
    transcripts.mkdir(parents=True, exist_ok=True)
    path = transcripts / "events.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event) + "\n")
    return path


def _set_storage_closet_windows(
    patched_tmux: type[FakeTmuxClient],
    window_names: list[str],
    *,
    storage: str = "pollypm-test-storage-closet",
    dead_windows: tuple[str, ...] = (),
) -> None:
    patched_tmux.windows_by_session = {
        storage: [
            FakeWindow(name=name, pane_dead=(name in dead_windows))
            for name in window_names
        ]
    }


def _patch_heartbeat_age(monkeypatch: pytest.MonkeyPatch, seconds: float | None) -> None:
    def _fake(config, session_name):  # noqa: ARG001
        return seconds

    monkeypatch.setattr(chat_send_routes, "_heartbeat_age_seconds", _fake)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_text_send(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hello operator"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["ok"] is True
    assert body["session_name"] == "operator"
    assert body["window_target"] == "pollypm-test-storage-closet:pm-operator"
    assert body["characters_sent"] == len("hello operator")
    assert body["method"] == "send_keys"
    assert body["message_id"].startswith("msg_")
    assert body["press_enter_at"] is not None
    assert patched_tmux.send_calls == [
        ("pollypm-test-storage-closet:pm-operator", "hello operator", True),
    ]


def test_happy_path_long_text_uses_paste_buffer(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    long_text = "x" * 250
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": long_text},
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["method"] == "paste_buffer"
    assert body["characters_sent"] == 250


def test_happy_path_architect_session(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_storage_closet_windows(patched_tmux, ["pm-architect-myproj"])
    _patch_heartbeat_age(monkeypatch, None)
    response = client.post(
        "/api/v1/chat/architect_myproj/send",
        json={"text": "hi archie"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["window_target"] == (
        "pollypm-test-storage-closet:pm-architect-myproj"
    )


def test_happy_path_per_task_worker(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_storage_closet_windows(patched_tmux, ["task-myproj-42"])
    _patch_heartbeat_age(monkeypatch, None)
    response = client.post(
        "/api/v1/chat/task-myproj-42/send",
        json={"text": "ping worker"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["window_target"] == (
        "pollypm-test-storage-closet:task-myproj-42"
    )


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_unknown_session_returns_404(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],  # noqa: ARG001
) -> None:
    response = client.post(
        "/api/v1/chat/nope/send",
        json={"text": "hello"},
        headers=auth_headers,
    )
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "session_unknown"


def test_missing_window_returns_503(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # storage-closet has *other* windows but not the operator one.
    _set_storage_closet_windows(patched_tmux, ["pm-architect-myproj"])
    _patch_heartbeat_age(monkeypatch, None)
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hello"},
        headers=auth_headers,
    )
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "window_missing"


def test_pane_dead_returns_409(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_storage_closet_windows(
        patched_tmux,
        ["pm-operator"],
        dead_windows=("pm-operator",),
    )
    _patch_heartbeat_age(monkeypatch, None)
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hello"},
        headers=auth_headers,
    )
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "pane_dead"


def test_dead_pane_error_from_send_keys_maps_to_409(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    patched_tmux.send_side_effect = DeadPaneError("pane %5 is dead")
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hello"},
        headers=auth_headers,
    )
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "pane_dead"


# ---------------------------------------------------------------------------
# Safety: mid-tool
# ---------------------------------------------------------------------------


def _assistant_with_open_tool(tool_use_id: str = "toolu_open") -> list[dict[str, Any]]:
    return [
        {"event_type": "user_turn", "payload": {"text": "do thing"}},
        {"event_type": "assistant_turn", "payload": {"text": "running"}},
        {
            "event_type": "tool_call",
            "payload": {
                "type": "tool_use",
                "id": tool_use_id,
                "name": "Bash",
                "input": {"command": "true"},
            },
        },
    ]


def _assistant_with_closed_tool(tool_use_id: str = "toolu_closed") -> list[dict[str, Any]]:
    return _assistant_with_open_tool(tool_use_id) + [
        {
            "event_type": "tool_result",
            "payload": {"type": "tool_result", "tool_use_id": tool_use_id, "content": "ok"},
        },
    ]


def test_unsafe_mid_tool_returns_409(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
    project_root: Path,
) -> None:
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    _write_events_jsonl(project_root, "session-open", _assistant_with_open_tool())
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hello"},
        headers=auth_headers,
    )
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "unsafe_mid_tool"


def test_closed_tool_allows_send(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
    project_root: Path,
) -> None:
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    _write_events_jsonl(project_root, "session-closed", _assistant_with_closed_tool())
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hello"},
        headers=auth_headers,
    )
    assert response.status_code == 200


def test_safety_force_bypasses_mid_tool(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
    project_root: Path,
) -> None:
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, 0.5)  # also fresh heartbeat
    _write_events_jsonl(project_root, "session-open", _assistant_with_open_tool())
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hello", "safety": "force"},
        headers=auth_headers,
    )
    assert response.status_code == 200


def test_safety_loose_still_enforces_mid_tool(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
    project_root: Path,
) -> None:
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    _write_events_jsonl(project_root, "session-open", _assistant_with_open_tool())
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hello", "safety": "loose"},
        headers=auth_headers,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "unsafe_mid_tool"


# ---------------------------------------------------------------------------
# Safety: mid-stream
# ---------------------------------------------------------------------------


def test_unsafe_mid_stream_returns_409(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, 0.4)
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hello"},
        headers=auth_headers,
    )
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "unsafe_mid_stream"


def test_safety_loose_bypasses_mid_stream_with_warning_header(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, 0.4)
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hello", "safety": "loose"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert response.headers.get("X-PollyPM-Warning") == "agent-may-be-streaming"


def test_safety_loose_no_warning_when_heartbeat_stale(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, 30.0)
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hello", "safety": "loose"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert "X-PollyPM-Warning" not in response.headers


def test_safety_force_bypasses_mid_stream(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, 0.1)
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hello", "safety": "force"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert "X-PollyPM-Warning" not in response.headers


def test_stale_heartbeat_allows_strict_send(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, 5.0)
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hello"},
        headers=auth_headers,
    )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# AskUserQuestion (§4.5)
# ---------------------------------------------------------------------------


def _ask_user_event(
    *,
    message_id: str = "msg_q1",
    options: list[str] | None = None,
    multi: bool = False,
) -> dict[str, Any]:
    return {
        "event_type": "tool_call",
        "uuid": message_id,
        "payload": {
            "type": "tool_use",
            "id": message_id,
            "name": "AskUserQuestion",
            "input": {
                "questions": [
                    {
                        "question": "Pick one",
                        "header": "Q1",
                        "multiSelect": multi,
                        "options": [
                            {"label": label, "description": ""}
                            for label in (options or ["alpha", "bravo", "charlie"])
                        ],
                    }
                ]
            },
        },
    }


def test_answer_to_selections_happy_path(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
    project_root: Path,
) -> None:
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    _write_events_jsonl(project_root, "session-q", [
        {"event_type": "assistant_turn", "payload": {"text": "Question incoming"}},
        _ask_user_event(message_id="msg_q1", options=["alpha", "bravo"]),
        # Match the open tool_use so the mid-tool gate doesn't fire on
        # the AskUserQuestion event itself.
        {
            "event_type": "tool_result",
            "payload": {"type": "tool_result", "tool_use_id": "msg_q1"},
        },
    ])
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"answer_to": "msg_q1", "selections": ["alpha"]},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.json()
    assert patched_tmux.send_calls == [
        ("pollypm-test-storage-closet:pm-operator", "alpha", True),
    ]


def test_answer_to_with_notes_appends_freeform(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
    project_root: Path,
) -> None:
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    _write_events_jsonl(project_root, "session-q", [
        {"event_type": "assistant_turn", "payload": {"text": "Pick"}},
        _ask_user_event(message_id="msg_q2"),
        {"event_type": "tool_result", "payload": {"type": "tool_result", "tool_use_id": "msg_q2"}},
    ])
    response = client.post(
        "/api/v1/chat/operator/send",
        json={
            "answer_to": "msg_q2",
            "selections": ["alpha"],
            "notes": "actually prefer luxon",
        },
        headers=auth_headers,
    )
    assert response.status_code == 200
    target, text, _enter = patched_tmux.send_calls[0]
    assert text == "alpha\nactually prefer luxon"
    assert target == "pollypm-test-storage-closet:pm-operator"


def test_answer_to_multiselect_newline_joined(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
    project_root: Path,
) -> None:
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    _write_events_jsonl(project_root, "session-q", [
        {"event_type": "assistant_turn", "payload": {"text": "Pick many"}},
        _ask_user_event(message_id="msg_q3", multi=True),
        {"event_type": "tool_result", "payload": {"type": "tool_result", "tool_use_id": "msg_q3"}},
    ])
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"answer_to": "msg_q3", "selections": ["alpha", "bravo"]},
        headers=auth_headers,
    )
    assert response.status_code == 200
    _target, text, _enter = patched_tmux.send_calls[0]
    assert text == "alpha\nbravo"


def test_answer_to_missing_returns_400(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
    project_root: Path,  # noqa: ARG001
) -> None:
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    # No events.jsonl written at all.
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"answer_to": "msg_ghost", "selections": ["alpha"]},
        headers=auth_headers,
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "answer_to_missing"


def test_answer_to_not_ask_user_returns_400(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
    project_root: Path,
) -> None:
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    _write_events_jsonl(project_root, "session-q", [
        {"event_type": "assistant_turn", "payload": {"text": "hi"}, "uuid": "msg_text"},
        # No AskUserQuestion — answer_to references a plain text message.
    ])
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"answer_to": "msg_text", "selections": ["alpha"]},
        headers=auth_headers,
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "selections_no_question"


def test_selections_invalid_returns_400(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
    project_root: Path,
) -> None:
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    _write_events_jsonl(project_root, "session-q", [
        {"event_type": "assistant_turn", "payload": {"text": "pick"}},
        _ask_user_event(message_id="msg_q1", options=["alpha", "bravo"]),
        {"event_type": "tool_result", "payload": {"type": "tool_result", "tool_use_id": "msg_q1"}},
    ])
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"answer_to": "msg_q1", "selections": ["delta"]},
        headers=auth_headers,
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "selections_invalid"


def test_answer_to_freeform_text_allowed(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
    project_root: Path,
) -> None:
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    _write_events_jsonl(project_root, "session-q", [
        {"event_type": "assistant_turn", "payload": {"text": "pick"}},
        _ask_user_event(message_id="msg_q1", options=["alpha", "bravo"]),
        {"event_type": "tool_result", "payload": {"type": "tool_result", "tool_use_id": "msg_q1"}},
    ])
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"answer_to": "msg_q1", "text": "neither, give me dayjs"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    _target, text, _enter = patched_tmux.send_calls[0]
    assert text == "neither, give me dayjs"


# ---------------------------------------------------------------------------
# Misc: press_enter, pane, payload validation
# ---------------------------------------------------------------------------


def test_press_enter_false_skips_enter(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "draft only", "press_enter": False},
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["press_enter_at"] is None
    assert patched_tmux.send_calls == [
        ("pollypm-test-storage-closet:pm-operator", "draft only", False),
    ]


def test_pane_index_targets_split_pane(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "split pane", "pane": 1},
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["window_target"] == (
        "pollypm-test-storage-closet:pm-operator.1"
    )


def test_pane_zero_targets_default_pane(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hello", "pane": 0},
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    # pane=0 stays on the default window-level target (matches the
    # existing send_keys behaviour).
    assert body["window_target"] == "pollypm-test-storage-closet:pm-operator"


def test_no_text_no_answer_to_returns_400(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    response = client.post(
        "/api/v1/chat/operator/send",
        json={},
        headers=auth_headers,
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "invalid_request"


def test_auth_required(
    client: TestClient,
    patched_tmux: type[FakeTmuxClient],  # noqa: ARG001
) -> None:
    response = client.post("/api/v1/chat/operator/send", json={"text": "hi"})
    # The bearer-auth dependency rejects before the route runs.
    assert response.status_code == 401


def test_invalid_token_returns_401(
    client: TestClient,
    patched_tmux: type[FakeTmuxClient],  # noqa: ARG001
) -> None:
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hi"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert response.status_code == 401


def test_unknown_task_session_404s(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``task-<garbage>`` doesn't parse to (project, int) so the
    # resolver raises 404 even though the prefix is suggestive.
    _set_storage_closet_windows(patched_tmux, ["task-myproj-42"])
    _patch_heartbeat_age(monkeypatch, None)
    response = client.post(
        "/api/v1/chat/task-only/send",
        json={"text": "hi"},
        headers=auth_headers,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "session_unknown"


# ---------------------------------------------------------------------------
# Unit-level tail/parser checks (executed via direct module access)
# ---------------------------------------------------------------------------


def test_open_tool_ids_helper_returns_unmatched(tmp_path: Path) -> None:
    events = [
        {"event_type": "assistant_turn", "payload": {"text": "x"}},
        {"event_type": "tool_call", "payload": {"id": "toolu_a"}},
        {"event_type": "tool_call", "payload": {"id": "toolu_b"}},
        {"event_type": "tool_result", "payload": {"tool_use_id": "toolu_a"}},
    ]
    open_ids = chat_send_routes._last_assistant_open_tool_ids(events)
    assert open_ids == {"toolu_b"}


def test_open_tool_ids_helper_empty_when_no_assistant() -> None:
    events = [{"event_type": "user_turn", "payload": {"text": "hi"}}]
    assert chat_send_routes._last_assistant_open_tool_ids(events) == set()


def test_open_tool_ids_helper_resets_per_assistant_turn() -> None:
    # Earlier turn had unmatched tools, but a fresh assistant_turn
    # supersedes them — only the LATEST turn's open ids matter.
    events = [
        {"event_type": "assistant_turn", "payload": {"text": "first"}},
        {"event_type": "tool_call", "payload": {"id": "stale_open"}},
        {"event_type": "assistant_turn", "payload": {"text": "second"}},
    ]
    assert chat_send_routes._last_assistant_open_tool_ids(events) == set()


def test_parse_task_session_name_canonical() -> None:
    assert chat_send_routes._parse_task_session_name("task-myproj-42") == ("myproj", 42)


def test_parse_task_session_name_rejects_garbage() -> None:
    assert chat_send_routes._parse_task_session_name("operator") is None
    assert chat_send_routes._parse_task_session_name("task-only") is None
    assert chat_send_routes._parse_task_session_name("task-foo-bar") is None


def test_build_answer_text_selections_only() -> None:
    assert chat_send_routes._build_answer_text(["a", "b"], None) == "a\nb"


def test_build_answer_text_with_notes() -> None:
    assert chat_send_routes._build_answer_text(["a"], "extra") == "a\nextra"


def test_build_answer_text_notes_only() -> None:
    assert chat_send_routes._build_answer_text([], "just notes") == "just notes"


def test_read_events_tail_handles_truncated_first_line(tmp_path: Path) -> None:
    # Write events larger than max_bytes so the tail reader trims line 1.
    events_path = tmp_path / "events.jsonl"
    with events_path.open("w", encoding="utf-8") as fh:
        for idx in range(50):
            fh.write(json.dumps({"event_type": "user_turn", "i": idx, "pad": "x" * 200}) + "\n")
    parsed = chat_send_routes._read_events_tail(events_path, max_bytes=512)
    # We dropped the first (likely truncated) line; remaining are valid JSON.
    assert all(isinstance(e, dict) and "event_type" in e for e in parsed)
    assert len(parsed) >= 1


def test_read_events_tail_empty_file(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("")
    assert chat_send_routes._read_events_tail(events_path) == []


def test_read_events_tail_missing_file(tmp_path: Path) -> None:
    assert chat_send_routes._read_events_tail(tmp_path / "nope.jsonl") == []
