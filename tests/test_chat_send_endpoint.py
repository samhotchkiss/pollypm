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


@dataclass
class FakePane:
    """Subset of :class:`pollypm.tmux.client.TmuxPane` the router reads."""

    pane_index: int
    pane_dead: bool = False


class FakeTmuxClient:
    """In-memory stand-in for :class:`pollypm.tmux.client.TmuxClient`.

    Captures send_keys calls so tests can assert on the target / text /
    press_enter triple. Configurable per-instance windows / panes +
    send_keys side effects make the negative paths (window missing,
    pane dead, pane invalid) trivial to assert without monkeypatching
    subprocess.
    """

    windows_by_session: dict[str, list[FakeWindow]] = {}
    panes_by_target: dict[str, list[FakePane]] = {}
    send_side_effect: Exception | None = None
    send_calls: list[tuple[str, str, bool]] = []
    list_panes_side_effect: Exception | None = None
    list_windows_side_effect: Exception | None = None

    def list_windows(self, session: str) -> list[FakeWindow]:
        if self.list_windows_side_effect is not None:
            raise self.list_windows_side_effect
        return list(self.windows_by_session.get(session, []))

    def list_panes(self, target: str) -> list[FakePane]:
        if self.list_panes_side_effect is not None:
            raise self.list_panes_side_effect
        # Tests register panes by either window target ("session:window")
        # or session, depending on the call shape; check both.
        return list(self.panes_by_target.get(target, []))

    def send_keys(self, target: str, text: str, press_enter: bool = True) -> None:
        if self.send_side_effect is not None:
            raise self.send_side_effect
        self.send_calls.append((target, text, press_enter))


@pytest.fixture(autouse=True)
def _reset_fake_tmux():
    FakeTmuxClient.windows_by_session = {}
    FakeTmuxClient.panes_by_target = {}
    FakeTmuxClient.send_side_effect = None
    FakeTmuxClient.send_calls = []
    FakeTmuxClient.list_panes_side_effect = None
    FakeTmuxClient.list_windows_side_effect = None
    yield


@pytest.fixture(autouse=True)
def _default_no_workers(monkeypatch: pytest.MonkeyPatch):
    """Default: no per-task workers registered.

    Tests that need worker validation override this via
    :func:`_patch_worker_sessions`. Keeps the router off the real
    work-service factory (pg) during unit tests.

    Both the legacy facade-wrapped seam (``_list_worker_sessions``)
    and the v6 strict seam (``_list_worker_sessions_strict``) are
    patched: the route reads the strict seam now (Codex #2043 review
    v6 blocker 1) but tests that pre-date v6 still reach for the
    legacy name and we want them to keep working.

    Codex #2043 review v7 blocker 1: stash the original strict helper
    on the module as ``_list_worker_sessions_strict_original`` so tests
    that need to exercise the real public-facade-bypass path (#2043 v6
    regression) can restore it without relying on ``__wrapped__``
    (the lambda below has no ``__wrapped__``).
    """
    monkeypatch.setattr(
        chat_send_routes,
        "_list_worker_sessions_strict_original",
        chat_send_routes._list_worker_sessions_strict,
        raising=False,
    )
    monkeypatch.setattr(
        chat_send_routes, "_list_worker_sessions", lambda config: [],
    )
    monkeypatch.setattr(
        chat_send_routes, "_list_worker_sessions_strict", lambda config: [],
    )


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
    *,
    cwd: Path | str | None = None,
    provider: str = "claude",
    account_name: str = "codex_primary",
) -> Path:
    """Write a synthetic events.jsonl with the ``_event_base`` envelope.

    The chat-send safety gates use P1's :func:`resolve_transcript_path`
    which fingerprints transcripts by ``(cwd, account, provider)``
    from the FIRST line of each ``events.jsonl``. We stamp those
    fields on every event so the resolver returns an "exact" match
    (and the strict-mode fail-closed gate stays quiet on legitimate
    sends).
    """
    transcripts = project_root / ".pollypm" / "transcripts" / session_id
    transcripts.mkdir(parents=True, exist_ok=True)
    path = transcripts / "events.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for event in events:
            payload = dict(event)
            payload.setdefault("session_id", session_id)
            payload.setdefault("provider", provider)
            payload.setdefault("account_name", account_name)
            if cwd is not None and "cwd" not in payload:
                payload["cwd"] = str(cwd)
            fh.write(json.dumps(payload) + "\n")
    return path


@dataclass
class FakeWorkerSessionRecord:
    """Minimal :class:`WorkerSessionRecord` subset the registry reads."""

    task_project: str
    task_number: int
    agent_name: str = "worker"
    pane_id: str | None = None
    worktree_path: str | None = None
    branch_name: str | None = None
    started_at: str = "2026-05-21T00:00:00Z"
    ended_at: str | None = None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    archive_path: str | None = None
    provider: str | None = "claude"
    provider_home: str | None = None


def _patch_worker_sessions(
    monkeypatch: pytest.MonkeyPatch,
    records: list[FakeWorkerSessionRecord],
) -> None:
    """Make the chat-send worker seams return ``records``.

    Avoids spinning up pg / the work-service in the router-level tests
    — the gate we actually care about is whether the resolver accepts
    a session_name only when the active worker-session row exists.

    Patches both the legacy facade-wrapped seam and the v6 strict
    seam so tests work regardless of which one the production code
    paths through (Codex #2043 review v6 blocker 1 swung the live
    route from ``_list_worker_sessions`` to
    ``_list_worker_sessions_strict``).
    """
    monkeypatch.setattr(
        chat_send_routes,
        "_list_worker_sessions",
        lambda config: list(records),
    )
    monkeypatch.setattr(
        chat_send_routes,
        "_list_worker_sessions_strict",
        lambda config: list(records),
    )


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
    _patch_worker_sessions(
        monkeypatch,
        [FakeWorkerSessionRecord(task_project="myproj", task_number=42)],
    )
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


def test_unregistered_worker_session_returns_404(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex #2043 review block 2 — `task-{project}-{id}` is not enough.

    Even when a tmux window with the canonical name exists, the
    router must refuse the send unless the work-service has an active
    worker-session row for that exact ``(project, task_number)``. A
    stale window or a guessed name otherwise lets an authenticated
    caller poke an unrelated session.
    """
    _set_storage_closet_windows(patched_tmux, ["task-myproj-42"])
    _patch_heartbeat_age(monkeypatch, None)
    # Default fixture leaves _list_worker_sessions returning [].
    response = client.post(
        "/api/v1/chat/task-myproj-42/send",
        json={"text": "ping worker"},
        headers=auth_headers,
    )
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "session_unknown"


def test_ended_worker_session_returns_404(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only *active* worker sessions accept sends.

    A row with ``ended_at`` set is the post-teardown state — sending
    into the storage-closet window at that point is racing with
    cleanup. Strict 404 so the operator notices.
    """
    _set_storage_closet_windows(patched_tmux, ["task-myproj-42"])
    _patch_heartbeat_age(monkeypatch, None)
    _patch_worker_sessions(
        monkeypatch,
        [FakeWorkerSessionRecord(
            task_project="myproj",
            task_number=42,
            ended_at="2026-05-21T00:00:00Z",
        )],
    )
    response = client.post(
        "/api/v1/chat/task-myproj-42/send",
        json={"text": "ping worker"},
        headers=auth_headers,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "session_unknown"


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


def test_send_keys_subprocess_window_missing_maps_to_503_window_missing(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex #2043 review v3 block 2 — window disappears post-validation.

    ``list_windows`` saw the window during validation, but tmux tore
    it down before the ``send-keys`` shell-out completed. The
    subprocess returns exit code 1 with ``can't find window`` on
    stderr. Pre-fix this surfaced as a generic 500; we now map it
    to a typed 503 ``window_missing`` so the operator knows the
    surface vanished mid-flight (vs. tmux being entirely down).
    """
    import subprocess as _subprocess

    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    patched_tmux.send_side_effect = _subprocess.CalledProcessError(
        returncode=1,
        cmd=["tmux", "send-keys"],
        stderr="can't find window: pm-operator",
    )
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hello"},
        headers=auth_headers,
    )
    assert response.status_code == 503, response.json()
    assert response.json()["error"]["code"] == "window_missing"


def test_send_keys_generic_subprocess_failure_maps_to_503_send_failed(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subprocess error that isn't a missing window → ``send_failed``.

    E.g. permissions/escape issues that tmux rejects. The operator
    gets the underlying stderr in ``message`` and a documented 503
    code instead of a generic 500.
    """
    import subprocess as _subprocess

    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    patched_tmux.send_side_effect = _subprocess.CalledProcessError(
        returncode=2,
        cmd=["tmux", "send-keys"],
        stderr="usage: send-keys ...",
    )
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hello"},
        headers=auth_headers,
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "send_failed"


def test_send_keys_tmux_missing_binary_maps_to_503_tmux_unavailable(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``FileNotFoundError`` from subprocess.run → ``tmux_unavailable``."""
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    patched_tmux.send_side_effect = FileNotFoundError(
        "[Errno 2] No such file or directory: 'tmux'",
    )
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hello"},
        headers=auth_headers,
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "tmux_unavailable"


def test_send_keys_tmux_timeout_maps_to_503_tmux_unavailable(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``subprocess.TimeoutExpired`` (wedged tmux) → ``tmux_unavailable``."""
    import subprocess as _subprocess

    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    patched_tmux.send_side_effect = _subprocess.TimeoutExpired(
        cmd=["tmux", "send-keys"], timeout=15,
    )
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hello"},
        headers=auth_headers,
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "tmux_unavailable"


def test_send_keys_paste_buffer_oserror_maps_to_503_send_failed(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OSError on long-text paste-buffer path → ``send_failed`` (not 500)."""
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    patched_tmux.send_side_effect = OSError("disk full while writing paste buffer")
    response = client.post(
        "/api/v1/chat/operator/send",
        # >100 chars to trigger the paste_buffer path in production
        # (though the fake just raises regardless — the gate we care
        # about is the route's exception handler).
        json={"text": "x" * 250},
        headers=auth_headers,
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "send_failed"


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
    workspace_root: Path,
) -> None:
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    _write_events_jsonl(
        project_root, "session-open", _assistant_with_open_tool(),
        cwd=workspace_root,
    )
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
    workspace_root: Path,
) -> None:
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    _write_events_jsonl(
        project_root, "session-closed", _assistant_with_closed_tool(),
        cwd=workspace_root,
    )
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
    workspace_root: Path,
) -> None:
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, 0.5)  # also fresh heartbeat
    _write_events_jsonl(
        project_root, "session-open", _assistant_with_open_tool(),
        cwd=workspace_root,
    )
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
    workspace_root: Path,
) -> None:
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    _write_events_jsonl(
        project_root, "session-open", _assistant_with_open_tool(),
        cwd=workspace_root,
    )
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
    workspace_root: Path,
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
    ], cwd=workspace_root)
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
    workspace_root: Path,
) -> None:
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    _write_events_jsonl(project_root, "session-q", [
        {"event_type": "assistant_turn", "payload": {"text": "Pick"}},
        _ask_user_event(message_id="msg_q2"),
        {"event_type": "tool_result", "payload": {"type": "tool_result", "tool_use_id": "msg_q2"}},
    ], cwd=workspace_root)
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
    workspace_root: Path,
) -> None:
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    _write_events_jsonl(project_root, "session-q", [
        {"event_type": "assistant_turn", "payload": {"text": "Pick many"}},
        _ask_user_event(message_id="msg_q3", multi=True),
        {"event_type": "tool_result", "payload": {"type": "tool_result", "tool_use_id": "msg_q3"}},
    ], cwd=workspace_root)
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
    project_root: Path,
    workspace_root: Path,
) -> None:
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    # Write a transcript so the strict-mode mapping is exact; the
    # specific answer_to id is missing from the tail, which is the
    # 400 path we want to hit (not the "no transcript at all" path).
    _write_events_jsonl(project_root, "session-q", [
        {"event_type": "assistant_turn", "payload": {"text": "hi"}},
    ], cwd=workspace_root)
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
    workspace_root: Path,
) -> None:
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    _write_events_jsonl(project_root, "session-q", [
        {"event_type": "assistant_turn", "payload": {"text": "hi"}, "uuid": "msg_text"},
        # No AskUserQuestion — answer_to references a plain text message.
    ], cwd=workspace_root)
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
    workspace_root: Path,
) -> None:
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    _write_events_jsonl(project_root, "session-q", [
        {"event_type": "assistant_turn", "payload": {"text": "pick"}},
        _ask_user_event(message_id="msg_q1", options=["alpha", "bravo"]),
        {"event_type": "tool_result", "payload": {"type": "tool_result", "tool_use_id": "msg_q1"}},
    ], cwd=workspace_root)
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
    workspace_root: Path,
) -> None:
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    _write_events_jsonl(project_root, "session-q", [
        {"event_type": "assistant_turn", "payload": {"text": "pick"}},
        _ask_user_event(message_id="msg_q1", options=["alpha", "bravo"]),
        {"event_type": "tool_result", "payload": {"type": "tool_result", "tool_use_id": "msg_q1"}},
    ], cwd=workspace_root)
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
    # Codex #2043 review block 5 — pane targets now go through
    # list_panes for existence + liveness validation. Seed two live
    # panes so the requested ``pane=1`` resolves.
    patched_tmux.panes_by_target = {
        "pollypm-test-storage-closet:pm-operator": [
            FakePane(pane_index=0),
            FakePane(pane_index=1),
        ],
    }
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "split pane", "pane": 1},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["window_target"] == (
        "pollypm-test-storage-closet:pm-operator.1"
    )


def test_explicit_pane_zero_validates_via_list_panes(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex #2043 review v3 block 1 — explicit pane=0 != active pane.

    tmux's active pane is NOT necessarily index 0 (the user can split
    and focus the new pane). The pre-fix router treated
    ``pane is None`` and ``pane == 0`` the same and shipped the send
    to the window-level target (active pane). That bypasses
    ``list_panes`` validation AND can land in the wrong pane.

    The fix: any explicit integer (including 0) routes through
    ``list_panes`` and addresses ``session:window.0`` explicitly.
    Here we seed pane 0 alive and pane 1 alive — the request must
    land on ``...:pm-operator.0``, NOT the window-level target.
    """
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    patched_tmux.panes_by_target = {
        "pollypm-test-storage-closet:pm-operator": [
            FakePane(pane_index=0),
            FakePane(pane_index=1),  # active in tmux but we asked for 0
        ],
    }
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hello", "pane": 0},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.json()
    body = response.json()
    # Explicit pane=0 now resolves to the indexed target. The
    # pre-fix code returned the window-level target and would have
    # landed on tmux's active pane (potentially pane 1).
    assert body["window_target"] == (
        "pollypm-test-storage-closet:pm-operator.0"
    )
    target, _text, _enter = patched_tmux.send_calls[0]
    assert target == "pollypm-test-storage-closet:pm-operator.0", (
        "explicit pane=0 must not fall through to the window-level "
        "(active-pane) target"
    )


def test_explicit_pane_zero_409s_when_pane_zero_dead(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Companion to the above: dead pane 0 must 409 instead of silent fallthrough.

    Pre-fix, ``pane=0`` aliased to the window-level target whose
    ``pane_dead`` bit reflected the ACTIVE pane. If the active pane
    happened to be alive (pane 1) the request would succeed and the
    text would land in pane 1 — the operator never finds out pane 0
    was dead.
    """
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    patched_tmux.panes_by_target = {
        "pollypm-test-storage-closet:pm-operator": [
            FakePane(pane_index=0, pane_dead=True),
            FakePane(pane_index=1),  # alive but the caller asked for 0
        ],
    }
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hello", "pane": 0},
        headers=auth_headers,
    )
    assert response.status_code == 409, response.json()
    assert response.json()["error"]["code"] == "pane_dead"
    # And critically: nothing was sent.
    assert patched_tmux.send_calls == []


def test_default_pane_none_uses_window_level_target(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``pane`` omitted (None) still uses the window-level target.

    This is the only case that goes to tmux's active pane without
    a ``list_panes`` probe — explicit indexes always validate.
    """
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    # Note: no panes_by_target — list_panes would fail. Default-pane
    # path must not call it.
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hello"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["window_target"] == "pollypm-test-storage-closet:pm-operator"


def test_negative_pane_returns_409_invalid(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex #2043 review block 5 — pane >= 0 must hold."""
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hi", "pane": -1},
        headers=auth_headers,
    )
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "pane_invalid"


def test_nonexistent_pane_returns_409_invalid(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An in-range pane index that isn't actually on the window 409s."""
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    patched_tmux.panes_by_target = {
        "pollypm-test-storage-closet:pm-operator": [FakePane(pane_index=0)],
    }
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hi", "pane": 7},
        headers=auth_headers,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "pane_invalid"


def test_dead_split_pane_returns_409_pane_dead(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live window with a dead split pane 409s on that pane."""
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    patched_tmux.panes_by_target = {
        "pollypm-test-storage-closet:pm-operator": [
            FakePane(pane_index=0),
            FakePane(pane_index=1, pane_dead=True),
        ],
    }
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hi", "pane": 1},
        headers=auth_headers,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "pane_dead"


def test_list_panes_failure_returns_409_invalid(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A list_panes failure short-circuits to a typed 409.

    Without this gate the subprocess error would surface as a 500;
    the operator can't tell whether it's safe to retry. Returning
    pane_invalid pushes the operator to pick a known-good pane.
    """
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    patched_tmux.list_panes_side_effect = RuntimeError("no such target")
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hi", "pane": 2},
        headers=auth_headers,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "pane_invalid"


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
    # A name that doesn't appear in config.sessions AND has no
    # matching active worker-session record returns 404 — this is
    # the post-Codex-#2043 enforcement: registry-only resolution.
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
# Codex #2043 review v4 — AskUserQuestion gate carveout
# ---------------------------------------------------------------------------


def test_answer_to_unmatched_ask_user_does_not_409_mid_tool(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
    project_root: Path,
    workspace_root: Path,
) -> None:
    """Codex #2043 review v4 block 1 — real AskUserQuestion is unmatched.

    Pre-fix the mid-tool gate counted every unmatched ``tool_call``,
    including AskUserQuestion. A real ask_user has NO ``tool_result``
    until the user answers it, so strict/loose ``answer_to`` requests
    would 409 ``unsafe_mid_tool`` before they could submit the
    selection. The fix exempts the specific tool_use_id being answered
    from the mid-tool count.
    """
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    # Note: NO matching tool_result for the AskUserQuestion — this is
    # the real-world shape (the user hasn't answered yet).
    _write_events_jsonl(project_root, "session-real-ask", [
        {"event_type": "user_turn", "payload": {"text": "pick"}},
        _ask_user_event(message_id="toolu_ask", options=["alpha", "bravo"]),
    ], cwd=workspace_root)
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"answer_to": "toolu_ask", "selections": ["alpha"]},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.json()
    assert patched_tmux.send_calls == [
        ("pollypm-test-storage-closet:pm-operator", "alpha", True),
    ]


def test_answer_to_does_not_exempt_unrelated_open_tool(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
    project_root: Path,
    workspace_root: Path,
) -> None:
    """The carveout is narrow — only the answered ask_user id is exempt.

    If the assistant has an open AskUserQuestion AND a separate open
    Bash call (e.g. running in parallel), answering the ask_user must
    still 409 because there's still an unmatched tool. Otherwise a
    user could ack the question while the assistant is mid-Bash and
    corrupt the conversation.
    """
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    _write_events_jsonl(project_root, "session-mixed-open", [
        {"event_type": "user_turn", "payload": {"text": "pick"}},
        _ask_user_event(message_id="toolu_ask", options=["alpha"]),
        # A separate Bash call is also mid-flight; the gate must
        # still trip even though the caller is answering the ask_user.
        {
            "event_type": "tool_call",
            "payload": {
                "type": "tool_use",
                "id": "toolu_bash_open",
                "name": "Bash",
                "input": {"command": "sleep 5"},
            },
        },
    ], cwd=workspace_root)
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"answer_to": "toolu_ask", "selections": ["alpha"]},
        headers=auth_headers,
    )
    assert response.status_code == 409, response.json()
    assert response.json()["error"]["code"] == "unsafe_mid_tool"


def test_answer_to_msg_envelope_id_resolves(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
    project_root: Path,
    workspace_root: Path,
) -> None:
    """Codex #2043 review v4 block 2 — accept the P1 envelope id form.

    P1's ``parse_events_jsonl`` (transcripts.py:398-415) returns the
    envelope id as ``msg_<tool_use_id>``. A client that read
    ``GET /chat/{session}/messages`` will POST
    ``answer_to=msg_toolu_ask``; pre-fix the route searched the raw
    ``toolu_ask`` value and returned 400 ``answer_to_missing``. The
    fix normalises by stripping the ``msg_`` prefix before lookup.
    """
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    _write_events_jsonl(project_root, "session-envelope-id", [
        {"event_type": "user_turn", "payload": {"text": "pick"}},
        _ask_user_event(message_id="toolu_ask", options=["alpha"]),
    ], cwd=workspace_root)
    response = client.post(
        "/api/v1/chat/operator/send",
        # The frontend hands ``msg_toolu_ask`` (the P1 envelope id),
        # NOT the raw ``toolu_ask``.
        json={"answer_to": "msg_toolu_ask", "selections": ["alpha"]},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.json()


# ---------------------------------------------------------------------------
# Codex #2043 review v4 — narrow tmux validation errors
# ---------------------------------------------------------------------------


def test_list_windows_filenotfound_maps_to_503_tmux_unavailable(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex #2043 review v4 block 3 — tmux binary missing on probe.

    Pre-fix every ``list_windows`` exception collapsed to
    ``window_missing``. A missing tmux binary is a deployment issue —
    telling the operator the window doesn't exist would send them
    chasing the wrong thing.
    """
    _patch_heartbeat_age(monkeypatch, None)
    patched_tmux.list_windows_side_effect = FileNotFoundError(
        "[Errno 2] No such file or directory: 'tmux'",
    )
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hi"},
        headers=auth_headers,
    )
    assert response.status_code == 503, response.json()
    assert response.json()["error"]["code"] == "tmux_unavailable"


def test_list_windows_timeout_maps_to_503_tmux_unavailable(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wedged tmux server on the validation probe → ``tmux_unavailable``."""
    import subprocess as _subprocess

    _patch_heartbeat_age(monkeypatch, None)
    patched_tmux.list_windows_side_effect = _subprocess.TimeoutExpired(
        cmd=["tmux", "list-windows"], timeout=15,
    )
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hi"},
        headers=auth_headers,
    )
    assert response.status_code == 503, response.json()
    assert response.json()["error"]["code"] == "tmux_unavailable"


def test_list_panes_filenotfound_maps_to_503_tmux_unavailable(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex #2043 review v4 block 3 — tmux missing during pane probe.

    Pre-fix every ``list_panes`` exception collapsed to
    ``pane_invalid``. ``FileNotFoundError`` is "tmux is gone" — the
    operator should restart tmux, not pick a different pane.
    """
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    patched_tmux.list_panes_side_effect = FileNotFoundError(
        "[Errno 2] No such file or directory: 'tmux'",
    )
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hi", "pane": 1},
        headers=auth_headers,
    )
    assert response.status_code == 503, response.json()
    assert response.json()["error"]["code"] == "tmux_unavailable"


def test_list_panes_timeout_maps_to_503_tmux_unavailable(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wedged tmux during pane probe → ``tmux_unavailable`` (not pane_invalid)."""
    import subprocess as _subprocess

    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    patched_tmux.list_panes_side_effect = _subprocess.TimeoutExpired(
        cmd=["tmux", "list-panes"], timeout=15,
    )
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hi", "pane": 1},
        headers=auth_headers,
    )
    assert response.status_code == 503, response.json()
    assert response.json()["error"]["code"] == "tmux_unavailable"


# ---------------------------------------------------------------------------
# Codex #2043 review v4 — CORS exposes X-PollyPM-Warning
# ---------------------------------------------------------------------------


def test_cors_exposes_x_pollypm_warning_header(
    api_config: PollyPMConfig,
    token_path: Path,
    token: str,  # noqa: ARG001
) -> None:
    """Codex #2043 review v4 block 4 — ``X-PollyPM-Warning`` must be CORS-exposed.

    The send endpoint returns ``X-PollyPM-Warning: agent-may-be-
    streaming`` for ``safety=loose`` sends, but the frontend can't
    read it from JS unless ``Access-Control-Expose-Headers`` lists
    it. The pre-fix CORS config only exposed ``Last-Event-ID``.
    """
    from pollypm.web_api import create_app

    app = create_app(config=api_config, token_path=token_path)
    client = TestClient(app)
    # A CORS preflight against any endpoint surfaces the
    # Access-Control-Expose-Headers value the middleware will emit
    # on the actual request.
    response = client.get(
        "/api/v1/health",
        headers={"Origin": "http://localhost:5173"},
    )
    expose = response.headers.get("access-control-expose-headers", "")
    assert "X-PollyPM-Warning" in expose, (
        f"X-PollyPM-Warning must be CORS-exposed; got {expose!r}"
    )


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


def test_open_tool_ids_helper_empty_when_only_user_turn() -> None:
    """A user_turn alone is not "mid-tool" — empty open-id set."""
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


def test_open_tool_ids_helper_tool_only_assistant_no_assistant_turn() -> None:
    """Codex #2043 review block 4 — mandatory regression.

    The :class:`TranscriptIngestor` only emits ``assistant_turn`` when
    the assistant message carries text. A tool-only assistant response
    (Claude returning a single ``tool_use`` block with no preface text)
    produces a ``tool_call`` event in the JSONL stream WITHOUT a
    preceding ``assistant_turn`` anchor.

    The pre-fix ``_last_assistant_open_tool_ids`` walked back for the
    most-recent ``assistant_turn`` and returned an empty set when it
    didn't find one, making the mid-tool gate fail open and let the
    operator type into a window whose Claude is still waiting on a
    Bash tool to come back.

    The fix anchors on the ``user_turn`` boundary instead so the
    tool-only assistant span is still caught.
    """
    events = [
        {"event_type": "user_turn", "payload": {"text": "go ahead"}},
        # Claude responded with ONLY a tool_use — no text → no
        # assistant_turn ever emitted.
        {"event_type": "tool_call", "payload": {"id": "toolu_silent"}},
    ]
    open_ids = chat_send_routes._last_assistant_open_tool_ids(events)
    assert open_ids == {"toolu_silent"}, (
        "tool-only assistant turn must still trip the mid-tool gate"
    )


def test_open_tool_ids_helper_tool_only_assistant_end_to_end_409(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
    project_root: Path,
    workspace_root: Path,
) -> None:
    """End-to-end mandate for Codex #2043 review block 4.

    Synthesize an events.jsonl whose ONLY assistant artifact is a raw
    ``tool_call`` — no preceding ``assistant_turn``. Assert the
    chat-send strict-mode gate returns 409 ``unsafe_mid_tool``.
    """
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    _write_events_jsonl(project_root, "session-tool-only", [
        {"event_type": "user_turn", "payload": {"text": "do thing"}},
        {
            "event_type": "tool_call",
            "payload": {
                "type": "tool_use",
                "id": "toolu_silent",
                "name": "Bash",
                "input": {"command": "true"},
            },
        },
    ], cwd=workspace_root)
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "ping"},
        headers=auth_headers,
    )
    assert response.status_code == 409, response.json()
    assert response.json()["error"]["code"] == "unsafe_mid_tool"


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


# ---------------------------------------------------------------------------
# Codex #2043 review v5 — strict mode fails closed on unavailable signals
# ---------------------------------------------------------------------------


def _force_transcript_unavailable(monkeypatch: pytest.MonkeyPatch, detail: str = "chmod 000") -> None:
    """Make ``_read_events_tail`` raise :class:`TranscriptUnavailable`.

    Simulates an unreadable ``events.jsonl`` (chmod 000, IO error,
    truncated mid-rotation). Pre-v5 the route collapsed every read
    failure into "no events" → "no open tool" → strict send proceeded.
    """
    def _raise(events_path, max_bytes=None):  # noqa: ARG001
        raise chat_send_routes.TranscriptUnavailable(detail)

    monkeypatch.setattr(chat_send_routes, "_read_events_tail", _raise)


def _force_heartbeat_unavailable(monkeypatch: pytest.MonkeyPatch, detail: str = "db down") -> None:
    """Make ``_heartbeat_age_seconds`` raise :class:`HeartbeatUnavailable`.

    Simulates a pg pool / network outage on the heartbeat read. Pre-v5
    this collapsed to ``None`` → "not streaming" → strict send through.
    """
    def _raise(config, session_name):  # noqa: ARG001
        raise chat_send_routes.HeartbeatUnavailable(detail)

    monkeypatch.setattr(chat_send_routes, "_heartbeat_age_seconds", _raise)


def test_strict_fails_closed_on_unreadable_transcript(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
    project_root: Path,
    workspace_root: Path,
) -> None:
    """Codex #2043 review v5 blocker 1 — strict 409 on read failure.

    A real-world chmod 000 on ``events.jsonl`` with an open
    ``tool_call`` previously let a strict send through because the
    tail reader returned ``[]`` for every read failure. The fix raises
    :class:`TranscriptUnavailable`, which the strict gate maps to
    409 ``unsafe_unavailable_transcript``.
    """
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    # Seed a transcript so the resolver picks it up (exact match), then
    # force the tail reader to fail — that's the chmod 000 scenario.
    _write_events_jsonl(
        project_root, "session-unreadable", _assistant_with_open_tool(),
        cwd=workspace_root,
    )
    _force_transcript_unavailable(monkeypatch)
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hello"},
        headers=auth_headers,
    )
    assert response.status_code == 409, response.json()
    assert response.json()["error"]["code"] == "unsafe_unavailable_transcript"
    # Critically: nothing was sent.
    assert patched_tmux.send_calls == []


def test_loose_continues_with_warning_on_unreadable_transcript(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
    project_root: Path,
    workspace_root: Path,
) -> None:
    """Loose mode continues past an unreadable transcript with a warning.

    Per Codex #2043 review v5 blocker 1: loose can't enforce the
    mid-tool gate without a readable transcript, but the operator
    explicitly opted into best-effort. Emit the
    ``transcript-unavailable`` warning header so it's visible.
    """
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    _write_events_jsonl(
        project_root, "session-unreadable-loose", _assistant_with_open_tool(),
        cwd=workspace_root,
    )
    _force_transcript_unavailable(monkeypatch)
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hello", "safety": "loose"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.json()
    assert response.headers.get("X-PollyPM-Warning") == "transcript-unavailable"


def test_force_bypasses_unreadable_transcript(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
    project_root: Path,
    workspace_root: Path,
) -> None:
    """Force ignores transcript-unavailable entirely — no header, no 409."""
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)
    _write_events_jsonl(
        project_root, "session-unreadable-force", _assistant_with_open_tool(),
        cwd=workspace_root,
    )
    _force_transcript_unavailable(monkeypatch)
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hello", "safety": "force"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.json()
    assert "X-PollyPM-Warning" not in response.headers


def test_strict_fails_closed_on_heartbeat_unavailable(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex #2043 review v5 blocker 2 — strict 409 on pg outage.

    The pre-v5 helper swallowed ``latest_heartbeat`` exceptions and
    returned ``None``, which the mid-stream gate treated as "not
    streaming". A pg outage therefore allowed the strict send. The fix
    raises :class:`HeartbeatUnavailable`, mapping to
    ``unsafe_unavailable_heartbeat``.
    """
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _force_heartbeat_unavailable(monkeypatch)
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hello"},
        headers=auth_headers,
    )
    assert response.status_code == 409, response.json()
    assert response.json()["error"]["code"] == "unsafe_unavailable_heartbeat"
    assert patched_tmux.send_calls == []


def test_loose_continues_with_warning_on_heartbeat_unavailable(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loose continues past pg outage with ``heartbeat-unavailable`` header."""
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _force_heartbeat_unavailable(monkeypatch)
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hello", "safety": "loose"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.json()
    assert response.headers.get("X-PollyPM-Warning") == "heartbeat-unavailable"


def test_force_bypasses_heartbeat_unavailable(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force ignores the heartbeat outage entirely."""
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _force_heartbeat_unavailable(monkeypatch)
    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hello", "safety": "force"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.json()
    assert "X-PollyPM-Warning" not in response.headers


def test_heartbeat_unavailable_raises_on_latest_heartbeat_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct unit test: pg failure → :class:`HeartbeatUnavailable`.

    Reproduces Codex's exact diagnostic: ``latest_heartbeat`` raising
    ``RuntimeError("db down")`` previously caused
    ``_heartbeat_age_seconds`` to return ``None``. Now it raises.
    """
    import pollypm.storage.pg_heartbeats as pg_heartbeats

    def _raise(*args, **kwargs):  # noqa: ARG001
        raise RuntimeError("db down")

    monkeypatch.setattr(pg_heartbeats, "latest_heartbeat", _raise)
    with pytest.raises(chat_send_routes.HeartbeatUnavailable):
        chat_send_routes._heartbeat_age_seconds(None, "operator")


def test_heartbeat_none_when_no_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """No row → ``None`` (genuine "not streaming"), not HeartbeatUnavailable."""
    import pollypm.storage.pg_heartbeats as pg_heartbeats

    monkeypatch.setattr(
        pg_heartbeats,
        "latest_heartbeat",
        lambda *a, **kw: None,  # noqa: ARG005
    )
    assert chat_send_routes._heartbeat_age_seconds(None, "operator") is None


def test_read_events_tail_raises_on_open_failure(tmp_path: Path) -> None:
    """Codex #2043 review v5 blocker 1 — chmod 000 raises TranscriptUnavailable.

    Reproduces Codex's exact diagnostic: an ``events.jsonl`` chmod'd
    unreadable previously made ``_read_events_tail`` return ``[]``.
    Now it raises.
    """
    import os as _os

    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps({"event_type": "tool_call", "payload": {"id": "x"}}) + "\n",
    )
    try:
        _os.chmod(events_path, 0o000)
        # Skip if running as root (chmod 000 doesn't block root reads).
        if _os.access(events_path, _os.R_OK):
            pytest.skip("running as root; chmod 000 does not block reads")
        with pytest.raises(chat_send_routes.TranscriptUnavailable):
            chat_send_routes._read_events_tail(events_path)
    finally:
        _os.chmod(events_path, 0o600)


# ---------------------------------------------------------------------------
# Codex #2043 review v5 blocker 3 — worker facade is lazy
# ---------------------------------------------------------------------------


def test_operator_send_does_not_open_worker_facade(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator/architect/advisor sends must not pay worker-facade cost.

    Pre-v5 ``_resolve_surface`` called ``_list_worker_sessions``
    unconditionally — every send opened the work-service even when
    the requested ``session_name`` syntactically could not be a
    worker. Operator surfaces are workspace-wide and never need the
    worker enumeration.
    """
    _set_storage_closet_windows(patched_tmux, ["pm-operator"])
    _patch_heartbeat_age(monkeypatch, None)

    call_count = {"n": 0}

    def _track(config):  # noqa: ARG001
        call_count["n"] += 1
        return []

    monkeypatch.setattr(chat_send_routes, "_list_worker_sessions", _track)
    monkeypatch.setattr(
        chat_send_routes, "_list_worker_sessions_strict", _track,
    )

    response = client.post(
        "/api/v1/chat/operator/send",
        json={"text": "hello"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.json()
    assert call_count["n"] == 0, (
        "operator send must not open the worker-service facade"
    )


def test_architect_send_does_not_open_worker_facade(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same as the operator test for the architect surface."""
    _set_storage_closet_windows(patched_tmux, ["pm-architect-myproj"])
    _patch_heartbeat_age(monkeypatch, None)

    call_count = {"n": 0}

    def _track(config):  # noqa: ARG001
        call_count["n"] += 1
        return []

    monkeypatch.setattr(chat_send_routes, "_list_worker_sessions", _track)
    monkeypatch.setattr(
        chat_send_routes, "_list_worker_sessions_strict", _track,
    )

    response = client.post(
        "/api/v1/chat/architect_myproj/send",
        json={"text": "hi"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.json()
    assert call_count["n"] == 0


def test_worker_session_send_calls_worker_facade(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker-pattern session_name DOES open the facade exactly once."""
    _set_storage_closet_windows(patched_tmux, ["task-myproj-42"])
    _patch_heartbeat_age(monkeypatch, None)

    call_count = {"n": 0}

    def _track(config):  # noqa: ARG001
        call_count["n"] += 1
        return [FakeWorkerSessionRecord(task_project="myproj", task_number=42)]

    # Route reads the strict seam now (Codex v6 blocker 1). Patching
    # the legacy seam too keeps the assertion stable if a future
    # refactor swaps which one runs first.
    monkeypatch.setattr(chat_send_routes, "_list_worker_sessions", _track)
    monkeypatch.setattr(
        chat_send_routes, "_list_worker_sessions_strict", _track,
    )

    response = client.post(
        "/api/v1/chat/task-myproj-42/send",
        json={"text": "hello"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.json()
    assert call_count["n"] == 1


def test_worker_facade_failure_returns_503_service_unavailable(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex #2043 review v5 blocker 3 — worker lookup failure ≠ 404.

    Pre-v5 the facade's ``[]`` fallback on outage masqueraded as
    ``404 session_unknown``, hiding pg outages from the operator. For
    a worker-pattern session_name we now distinguish "lookup failed"
    (``503 service_unavailable``) from "no such worker" (404).
    """
    _set_storage_closet_windows(patched_tmux, ["task-myproj-42"])
    _patch_heartbeat_age(monkeypatch, None)

    def _raise(config):  # noqa: ARG001
        raise RuntimeError("pg pool exhausted")

    # v6: route reads the strict seam — patch it so the synthetic
    # outage is what _resolve_surface actually sees.
    monkeypatch.setattr(chat_send_routes, "_list_worker_sessions", _raise)
    monkeypatch.setattr(
        chat_send_routes, "_list_worker_sessions_strict", _raise,
    )

    response = client.post(
        "/api/v1/chat/task-myproj-42/send",
        json={"text": "hello"},
        headers=auth_headers,
    )
    assert response.status_code == 503, response.json()
    assert response.json()["error"]["code"] == "service_unavailable"


def test_non_worker_session_with_worker_facade_failure_still_404(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-worker name that doesn't match config sessions stays 404.

    ``arbitrary-name`` is not worker-pattern, so the facade is never
    queried — even if it would fail. The route returns the same
    ``session_unknown`` it always has.
    """
    def _raise(config):  # noqa: ARG001
        raise RuntimeError("would fail if asked")

    monkeypatch.setattr(chat_send_routes, "_list_worker_sessions", _raise)
    monkeypatch.setattr(
        chat_send_routes, "_list_worker_sessions_strict", _raise,
    )

    response = client.post(
        "/api/v1/chat/arbitrary-name/send",
        json={"text": "hello"},
        headers=auth_headers,
    )
    assert response.status_code == 404, response.json()
    assert response.json()["error"]["code"] == "session_unknown"


def test_looks_like_worker_session_pattern() -> None:
    """Worker pattern: ``task-<project>-<n>`` only."""
    assert chat_send_routes._looks_like_worker_session("task-myproj-42")
    assert chat_send_routes._looks_like_worker_session("task-pollypm-1")
    assert chat_send_routes._looks_like_worker_session("task-foo_bar-99")
    assert not chat_send_routes._looks_like_worker_session("operator")
    assert not chat_send_routes._looks_like_worker_session("architect_myproj")
    assert not chat_send_routes._looks_like_worker_session("task-myproj")
    assert not chat_send_routes._looks_like_worker_session("task-myproj-abc")
    assert not chat_send_routes._looks_like_worker_session("task--42")


# ---------------------------------------------------------------------------
# Codex #2043 review v6 blocker 1 — strict path bypasses public facade
# ---------------------------------------------------------------------------


def test_real_open_work_service_failure_returns_503(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_tmux: type[FakeTmuxClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex #2043 review v6 blocker 1 — real facade-outage path.

    Pre-v6 the v5 regression test monkeypatched
    ``_list_worker_sessions`` directly, but the live facade
    :func:`pollypm.web_api.service.list_active_worker_sessions`
    catches ``_open_work_service_readonly`` failures and returns
    ``[]`` — so a real pg outage still collapsed into a misleading
    ``404 session_unknown`` in production.

    The v6 fix in chat_send.py routes the strict worker lookup
    through :func:`_list_worker_sessions_strict`, which opens
    :func:`pollypm.web_api.service._open_work_service_readonly`
    directly and propagates failures as
    :class:`_WorkerFacadeUnavailable` → ``503 service_unavailable``.

    This test exercises the real public-facade-bypass path: we
    monkeypatch the contextmanager so it raises on enter, then verify
    a worker-pattern POST returns 503 (NOT 404) and the autouse
    ``_default_no_workers`` patch doesn't short-circuit the new path.
    """
    _set_storage_closet_windows(patched_tmux, ["task-myproj-7"])
    _patch_heartbeat_age(monkeypatch, None)

    # Override the autouse fixture's strict-seam stub so the strict
    # helper actually executes (and reaches the real
    # ``_open_work_service_readonly``). The autouse fixture stashed
    # the unpatched original on the module as
    # ``_list_worker_sessions_strict_original`` for exactly this case
    # (Codex #2043 review v7 blocker 1).
    monkeypatch.setattr(
        chat_send_routes,
        "_list_worker_sessions_strict",
        chat_send_routes._list_worker_sessions_strict_original,
    )

    import pollypm.web_api.service as service_mod

    from contextlib import contextmanager

    @contextmanager
    def _boom(*args, **kwargs):  # noqa: ARG001
        raise RuntimeError("pg pool down")
        yield  # pragma: no cover — generator protocol shim

    monkeypatch.setattr(service_mod, "_open_work_service_readonly", _boom)

    response = client.post(
        "/api/v1/chat/task-myproj-7/send",
        json={"text": "hello"},
        headers=auth_headers,
    )
    assert response.status_code == 503, response.json()
    assert response.json()["error"]["code"] == "service_unavailable"
    assert "pg pool down" in response.json()["error"]["message"]


def test_list_worker_sessions_strict_returns_empty_when_no_workers(
    api_config: PollyPMConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct unit: strict helper returns ``[]`` for a real empty list.

    A successful ``_open_work_service_readonly`` whose
    ``list_worker_sessions`` returns ``[]`` is the genuine "no active
    workers" case — must NOT raise. Only open/list failures raise.
    """
    from contextlib import contextmanager

    class _StubSvc:
        @staticmethod
        def list_worker_sessions(*, active_only: bool = True) -> list[Any]:
            return []

    @contextmanager
    def _stub(*args, **kwargs):  # noqa: ARG001
        yield _StubSvc()

    import pollypm.web_api.service as service_mod

    monkeypatch.setattr(service_mod, "_open_work_service_readonly", _stub)

    # Restore the original strict helper (autouse fixture replaces it).
    monkeypatch.setattr(
        chat_send_routes,
        "_list_worker_sessions_strict",
        chat_send_routes._list_worker_sessions_strict_original,
    )

    assert chat_send_routes._list_worker_sessions_strict(api_config) == []


def test_list_worker_sessions_strict_raises_typed_on_facade_open_failure(
    api_config: PollyPMConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct unit: open failure → :class:`_WorkerFacadeUnavailable`."""
    from contextlib import contextmanager

    @contextmanager
    def _boom(*args, **kwargs):  # noqa: ARG001
        raise RuntimeError("connect refused")
        yield  # pragma: no cover

    import pollypm.web_api.service as service_mod

    monkeypatch.setattr(service_mod, "_open_work_service_readonly", _boom)

    # Codex #2043 review v7 blocker 1: the autouse fixture replaces
    # the module-level strict helper with a no-op lambda. Restore the
    # original (stashed by that fixture) before exercising the unit.
    monkeypatch.setattr(
        chat_send_routes,
        "_list_worker_sessions_strict",
        chat_send_routes._list_worker_sessions_strict_original,
    )

    with pytest.raises(chat_send_routes._WorkerFacadeUnavailable):
        chat_send_routes._list_worker_sessions_strict(api_config)
