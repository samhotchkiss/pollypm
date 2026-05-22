"""Integration tests for Phase 2 §11 heartbeats GET endpoints.

Covers ``GET /api/v1/heartbeats`` and
``GET /api/v1/heartbeats/{session_name}`` against the FastAPI
``TestClient``. The route reads the ``pg_heartbeats`` facade; tests
monkeypatch the two read functions (``latest_heartbeat`` /
``recent_heartbeats``) on the route module so the suite never touches
Postgres.

Run with ``pytest --noconftest tests/test_heartbeats_endpoint.py -v
--timeout=120`` so the per-project conftest (which requires a real pg
harness) is skipped — these tests are self-contained.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

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
from pollypm.storage.records import HeartbeatRecord
from pollypm.web_api import create_app, ensure_token
from pollypm.web_api.routes import heartbeats as heartbeats_routes


# ---------------------------------------------------------------------------
# Fixtures
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
        sessions={
            "operator": SessionConfig(
                name="operator",
                role="operator",
                provider=ProviderKind.CLAUDE,
                account="codex_primary",
                cwd=workspace,
                project="myproj",
                window_name="pm-operator",
            ),
            "architect_myproj": SessionConfig(
                name="architect_myproj",
                role="architect",
                provider=ProviderKind.CLAUDE,
                account="codex_primary",
                cwd=workspace,
                project="myproj",
                window_name="pm-architect-myproj",
            ),
            "disabled_session": SessionConfig(
                name="disabled_session",
                role="advisor",
                provider=ProviderKind.CLAUDE,
                account="codex_primary",
                cwd=workspace,
                project="myproj",
                window_name="pm-disabled",
                enabled=False,
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
def token(tmp_path: Path) -> tuple[Path, str]:
    token_path = tmp_path / "api-token"
    value, _ = ensure_token(token_path)
    return token_path, value


@pytest.fixture
def auth_headers(token: tuple[Path, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {token[1]}"}


@pytest.fixture
def client(config: PollyPMConfig, token: tuple[Path, str]) -> TestClient:
    token_path, _ = token
    app = create_app(config=config, token_path=token_path)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _record(
    session_name: str,
    *,
    age_seconds: int = 10,
    pane_dead: bool = False,
    pane_command: str = "claude",
    log_bytes: int = 1024,
    snapshot_hash: str = "abc123",
) -> HeartbeatRecord:
    """Build a :class:`HeartbeatRecord` with a relative-age timestamp."""
    ts = (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat()
    return HeartbeatRecord(
        session_name=session_name,
        tmux_window=f"pollypm-test-storage-closet:{session_name}",
        pane_id="%42",
        pane_command=pane_command,
        pane_dead=pane_dead,
        log_bytes=log_bytes,
        snapshot_path=f"/tmp/snapshots/{session_name}.txt",
        snapshot_hash=snapshot_hash,
        created_at=ts,
    )


def _install_latest(
    monkeypatch: pytest.MonkeyPatch,
    *,
    by_session: dict[str, HeartbeatRecord | None] | None = None,
    raises: Exception | None = None,
) -> list[str]:
    """Stub ``pg_heartbeats.latest_heartbeat`` for the route.

    Routes import the symbol inside the function body, so we patch on
    the source module (``pollypm.storage.pg_heartbeats``) and the
    route's local rebinding resolves to the stub.

    Returns a list the stub appends each session name to — handy for
    asserting which sessions the endpoint enumerated.
    """
    calls: list[str] = []
    table = dict(by_session or {})

    def fake(session_name: str, *, pool=None, config=None):  # noqa: ARG001
        calls.append(session_name)
        if raises is not None:
            raise raises
        return table.get(session_name)

    monkeypatch.setattr(
        "pollypm.storage.pg_heartbeats.latest_heartbeat",
        fake,
    )
    return calls


def _install_recent(
    monkeypatch: pytest.MonkeyPatch,
    *,
    by_session: dict[str, list[HeartbeatRecord]] | None = None,
    raises: Exception | None = None,
) -> list[tuple[str, int]]:
    """Stub ``pg_heartbeats.recent_heartbeats`` for the route."""
    calls: list[tuple[str, int]] = []
    table = dict(by_session or {})

    def fake(session_name: str, limit: int = 3, *, pool=None, config=None):  # noqa: ARG001
        calls.append((session_name, int(limit)))
        if raises is not None:
            raise raises
        return list(table.get(session_name, []))

    monkeypatch.setattr(
        "pollypm.storage.pg_heartbeats.recent_heartbeats",
        fake,
    )
    return calls


# ---------------------------------------------------------------------------
# /heartbeats — list
# ---------------------------------------------------------------------------


def test_list_heartbeats_happy_path_returns_row_per_session(
    client, auth_headers, monkeypatch,
):
    calls = _install_latest(monkeypatch, by_session={
        "operator": _record("operator", age_seconds=15),
        "architect_myproj": _record("architect_myproj", age_seconds=30),
    })
    response = client.get("/api/v1/heartbeats", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    names = [row["session_name"] for row in body["heartbeats"]]
    # Sorted; "disabled_session" filtered out because enabled=False.
    assert names == ["architect_myproj", "operator"]
    assert "disabled_session" not in calls
    # Statuses healthy when age <= 5min.
    statuses = {row["session_name"]: row["status"] for row in body["heartbeats"]}
    assert statuses == {"operator": "healthy", "architect_myproj": "healthy"}
    # Role + project enrichment from config.
    operator = next(row for row in body["heartbeats"] if row["session_name"] == "operator")
    assert operator["role"] == "operator"
    assert operator["project"] == "myproj"
    assert operator["last_tick_ts"] is not None
    assert operator["age_seconds"] is not None and operator["age_seconds"] >= 0
    assert operator["pane_command"] == "claude"
    assert operator["snapshot_hash"] == "abc123"


def test_list_heartbeats_classifies_stale_when_older_than_five_minutes(
    client, auth_headers, monkeypatch,
):
    _install_latest(monkeypatch, by_session={
        "operator": _record("operator", age_seconds=600),  # 10 minutes
        "architect_myproj": _record("architect_myproj", age_seconds=30),
    })
    body = client.get("/api/v1/heartbeats", headers=auth_headers).json()
    statuses = {row["session_name"]: row["status"] for row in body["heartbeats"]}
    assert statuses["operator"] == "stale"
    assert statuses["architect_myproj"] == "healthy"


def test_list_heartbeats_initializing_when_session_never_reported(
    client, auth_headers, monkeypatch,
):
    # ``operator`` has a record; ``architect_myproj`` does not.
    _install_latest(monkeypatch, by_session={
        "operator": _record("operator", age_seconds=10),
    })
    body = client.get("/api/v1/heartbeats", headers=auth_headers).json()
    statuses = {row["session_name"]: row["status"] for row in body["heartbeats"]}
    assert statuses["operator"] == "healthy"
    assert statuses["architect_myproj"] == "initializing"
    arch = next(
        row for row in body["heartbeats"]
        if row["session_name"] == "architect_myproj"
    )
    assert arch["last_tick_ts"] is None
    assert arch["age_seconds"] is None


def test_list_heartbeats_empty_when_no_sessions_configured(
    client, auth_headers, monkeypatch, config,
):
    # Disable every configured session so the list collapses to empty.
    for session in config.sessions.values():
        session.enabled = False
    calls = _install_latest(monkeypatch, by_session={})
    body = client.get("/api/v1/heartbeats", headers=auth_headers).json()
    assert body == {"heartbeats": []}
    assert calls == []


def test_list_heartbeats_facade_outage_returns_503(
    client, auth_headers, monkeypatch,
):
    _install_latest(monkeypatch, raises=RuntimeError("pg pool exhausted"))
    response = client.get("/api/v1/heartbeats", headers=auth_headers)
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "service_unavailable"
    assert "ledger unavailable" in body["error"]["message"]


def test_list_heartbeats_requires_bearer_auth(client):
    response = client.get("/api/v1/heartbeats")
    assert response.status_code == 401
    assert response.json()["error"]["code"] in {"unauthorized", "invalid_token"}


def test_list_heartbeats_rejects_wrong_token(client):
    response = client.get(
        "/api/v1/heartbeats",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


# ---------------------------------------------------------------------------
# /heartbeats/{session_name} — detail
# ---------------------------------------------------------------------------


def test_detail_returns_latest_plus_history(
    client, auth_headers, monkeypatch,
):
    latest = _record("operator", age_seconds=10, snapshot_hash="latest")
    history = [
        _record("operator", age_seconds=10, snapshot_hash="latest"),
        _record("operator", age_seconds=20, snapshot_hash="prev1"),
        _record("operator", age_seconds=30, snapshot_hash="prev2"),
    ]
    _install_latest(monkeypatch, by_session={"operator": latest})
    recent_calls = _install_recent(
        monkeypatch, by_session={"operator": history},
    )
    response = client.get(
        "/api/v1/heartbeats/operator?limit=10", headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["latest"]["session_name"] == "operator"
    assert body["latest"]["status"] == "healthy"
    assert body["latest"]["snapshot_hash"] == "latest"
    assert len(body["ticks"]) == 3
    hashes = [tick["snapshot_hash"] for tick in body["ticks"]]
    assert hashes == ["latest", "prev1", "prev2"]
    assert body["clamped"] is False
    assert recent_calls == [("operator", 10)]


def test_detail_404_when_session_unknown(
    client, auth_headers, monkeypatch,
):
    _install_latest(monkeypatch, by_session={})
    _install_recent(monkeypatch, by_session={})
    response = client.get(
        "/api/v1/heartbeats/does-not-exist", headers=auth_headers,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_detail_returns_initializing_for_configured_but_silent_session(
    client, auth_headers, monkeypatch,
):
    # Session exists in config but no row in ledger → status=initializing,
    # ticks=[]. NOT a 404.
    _install_latest(monkeypatch, by_session={})
    _install_recent(monkeypatch, by_session={})
    response = client.get(
        "/api/v1/heartbeats/operator", headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["latest"]["session_name"] == "operator"
    assert body["latest"]["status"] == "initializing"
    assert body["latest"]["last_tick_ts"] is None
    assert body["ticks"] == []


def test_detail_returns_detail_for_unconfigured_session_with_ledger_row(
    client, auth_headers, monkeypatch,
):
    # Session is not in config (e.g. a worker that's since been
    # reaped) but the ledger remembers it — return the row, don't 404.
    ghost = _record("task-myproj-9", age_seconds=120)
    _install_latest(monkeypatch, by_session={"task-myproj-9": ghost})
    _install_recent(monkeypatch, by_session={"task-myproj-9": [ghost]})
    response = client.get(
        "/api/v1/heartbeats/task-myproj-9", headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["latest"]["session_name"] == "task-myproj-9"
    # No config row → role/project come back as null.
    assert body["latest"]["role"] is None
    assert body["latest"]["project"] is None
    assert len(body["ticks"]) == 1


def test_detail_clamps_limit_at_max(
    client, auth_headers, monkeypatch,
):
    _install_latest(monkeypatch, by_session={
        "operator": _record("operator", age_seconds=5),
    })
    recent_calls = _install_recent(monkeypatch, by_session={"operator": []})
    response = client.get(
        "/api/v1/heartbeats/operator?limit=1000", headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["clamped"] is True
    # Route should pass the clamped value (200) to the facade.
    assert recent_calls == [("operator", 200)]


def test_detail_facade_outage_on_latest_returns_503(
    client, auth_headers, monkeypatch,
):
    _install_latest(monkeypatch, raises=RuntimeError("pg unreachable"))
    _install_recent(monkeypatch, by_session={})
    response = client.get(
        "/api/v1/heartbeats/operator", headers=auth_headers,
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"


def test_detail_facade_outage_on_history_returns_503(
    client, auth_headers, monkeypatch,
):
    _install_latest(monkeypatch, by_session={
        "operator": _record("operator", age_seconds=5),
    })
    _install_recent(monkeypatch, raises=RuntimeError("pg recent unreachable"))
    response = client.get(
        "/api/v1/heartbeats/operator", headers=auth_headers,
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"


def test_detail_requires_bearer_auth(client):
    response = client.get("/api/v1/heartbeats/operator")
    assert response.status_code == 401
    assert response.json()["error"]["code"] in {"unauthorized", "invalid_token"}


def test_detail_rejects_invalid_limit_below_one(
    client, auth_headers, monkeypatch,
):
    _install_latest(monkeypatch, by_session={})
    _install_recent(monkeypatch, by_session={})
    response = client.get(
        "/api/v1/heartbeats/operator?limit=0", headers=auth_headers,
    )
    # Pydantic ``ge=1`` rejects via the spec's 422 envelope.
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


# ---------------------------------------------------------------------------
# Module sanity
# ---------------------------------------------------------------------------


def test_router_module_exports_expected_handlers():
    """Smoke test: the route module surfaces its handlers."""
    assert hasattr(heartbeats_routes, "list_heartbeats_endpoint")
    assert hasattr(heartbeats_routes, "get_heartbeat_endpoint")
    assert heartbeats_routes.router is not None
