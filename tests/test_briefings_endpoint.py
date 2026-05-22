"""Phase 2 §9 — briefings endpoint tests.

Run via::

    pytest --noconftest tests/test_briefings_endpoint.py -v --timeout=120

The ``--noconftest`` flag skips the repo's heavy conftest (which spins
up pg pools etc.); every fixture this test needs is defined inline.

Coverage targets the three endpoints from
``~/Desktop/pollypm-phase2-endpoints-spec.md`` §9:

- ``GET /api/v1/briefings`` — list types
- ``GET /api/v1/briefings/{type_name}`` — render last generated
- ``POST /api/v1/briefings/{type_name}/regenerate`` — force regen

The morning-briefing plugin's regenerate pipeline is fully mocked — we
swap the adapter's callables for in-memory stubs so the tests never
touch pg, git, or any LLM call.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
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
)
from pollypm.web_api import create_app, ensure_token
from pollypm.web_api.routes import briefings as briefings_routes
from pollypm.web_api.routes.briefings import (
    BriefingResponse,
    RegenerateRequest,
    _BriefingAdapter,
)


# ---------------------------------------------------------------------------
# Config / app fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / ".pollypm").mkdir()
    return root


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "myproj"
    root.mkdir()
    return root


@pytest.fixture
def api_config(project_root: Path, workspace_root: Path) -> PollyPMConfig:
    base_dir = workspace_root / ".pollypm"
    return PollyPMConfig(
        project=ProjectSettings(
            name="PollyPM",
            root_dir=workspace_root,
            tmux_session="pollypm-test",
            workspace_root=workspace_root,
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
# Registry stubs
# ---------------------------------------------------------------------------


def _make_response(
    *,
    type_name: str = "morning",
    markdown: str = "# Today\n\nThings happened.",
    mode: str = "synthesized",
    date_local: str = "2026-05-22",
    metadata: dict | None = None,
) -> BriefingResponse:
    return BriefingResponse(
        type=type_name,
        generated_at=datetime(2026, 5, 22, 6, 30, 0, tzinfo=UTC),
        date_local=date_local,
        mode=mode,
        markdown=markdown,
        metadata=metadata or {"yesterday": "Test day", "priorities": []},
    )


class _FakeAdapterState:
    """Mutable knobs the test fixture exposes for the patched adapter."""

    def __init__(self) -> None:
        self.available: bool = True
        self.render: BriefingResponse | None = _make_response()
        self.regen: BriefingResponse = _make_response(
            markdown="# Regenerated\n\nFresh body.",
        )
        self.regen_delay: float = 0.0
        self.regen_raise: BaseException | None = None


@pytest.fixture
def fake_state() -> _FakeAdapterState:
    """Mutable adapter-control state shared across the test + the fixture."""
    return _FakeAdapterState()


@pytest.fixture
def patched_registry(
    monkeypatch: pytest.MonkeyPatch,
    fake_state: _FakeAdapterState,
) -> dict[str, _BriefingAdapter]:
    """Install a controllable registry for tests.

    The :class:`_FakeAdapterState` returned by :func:`fake_state` is the
    knob clients tweak to drive behavior (timeout, unavailable, raise).
    """

    def _available(_config) -> bool:
        return fake_state.available

    def _render(_config) -> BriefingResponse | None:
        return fake_state.render

    def _regenerate(_config, _body: RegenerateRequest) -> BriefingResponse:
        if fake_state.regen_delay > 0:
            time.sleep(fake_state.regen_delay)
        if fake_state.regen_raise is not None:
            raise fake_state.regen_raise
        return fake_state.regen

    fake = _BriefingAdapter(
        name="morning",
        description="Test morning briefing",
        available=_available,
        render_last=_render,
        regenerate=_regenerate,
    )
    registry: dict[str, _BriefingAdapter] = {"morning": fake}
    monkeypatch.setattr(briefings_routes, "_REGISTRY", registry)
    return registry


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_list_briefing_types(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_registry: dict[str, _BriefingAdapter],  # noqa: ARG001
) -> None:
    """GET /api/v1/briefings returns the registry shape."""
    response = client.get("/api/v1/briefings", headers=auth_headers)
    assert response.status_code == 200, response.json()
    body = response.json()
    assert "types" in body
    assert isinstance(body["types"], list)
    names = {entry["name"] for entry in body["types"]}
    assert "morning" in names
    morning = next(entry for entry in body["types"] if entry["name"] == "morning")
    assert morning["available"] is True
    assert "description" in morning and morning["description"]


def test_list_briefing_types_marks_unavailable(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_registry: dict[str, _BriefingAdapter],  # noqa: ARG001
    fake_state: _FakeAdapterState,
) -> None:
    """A registered-but-not-loaded provider reports available=False."""
    fake_state.available = False
    response = client.get("/api/v1/briefings", headers=auth_headers)
    assert response.status_code == 200
    morning = next(
        entry for entry in response.json()["types"] if entry["name"] == "morning"
    )
    assert morning["available"] is False


def test_render_existing_briefing(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_registry: dict[str, _BriefingAdapter],  # noqa: ARG001
) -> None:
    """GET /api/v1/briefings/morning returns the cached briefing body."""
    response = client.get("/api/v1/briefings/morning", headers=auth_headers)
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["type"] == "morning"
    assert body["markdown"].startswith("# Today")
    assert body["date_local"] == "2026-05-22"
    assert body["mode"] == "synthesized"
    assert isinstance(body["metadata"], dict)


def test_render_unknown_type_returns_404(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_registry: dict[str, _BriefingAdapter],  # noqa: ARG001
) -> None:
    """Unknown briefing types are a 404 ``not_found``."""
    response = client.get("/api/v1/briefings/nonsense", headers=auth_headers)
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert "nonsense" in body["error"]["message"]


def test_render_missing_briefing_returns_404(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_registry: dict[str, _BriefingAdapter],  # noqa: ARG001
    fake_state: _FakeAdapterState,
) -> None:
    """No cached briefing on disk → 404 with a regenerate hint."""
    fake_state.render = None
    response = client.get("/api/v1/briefings/morning", headers=auth_headers)
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    # Hint points the client at the regenerate verb.
    assert "regenerate" in body["error"].get("hint", "").lower()


def test_render_unavailable_provider_returns_503(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_registry: dict[str, _BriefingAdapter],  # noqa: ARG001
    fake_state: _FakeAdapterState,
) -> None:
    """When provider isn't loaded, render returns 503 service_unavailable."""
    fake_state.available = False
    response = client.get("/api/v1/briefings/morning", headers=auth_headers)
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "service_unavailable"


def test_regenerate_happy_path_sync(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_registry: dict[str, _BriefingAdapter],  # noqa: ARG001
) -> None:
    """POST regenerate returns the freshly produced briefing body."""
    response = client.post(
        "/api/v1/briefings/morning/regenerate",
        json={},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["type"] == "morning"
    assert body["markdown"].startswith("# Regenerated")


def test_regenerate_accepts_empty_body(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_registry: dict[str, _BriefingAdapter],  # noqa: ARG001
) -> None:
    """Body is optional — clients can omit JSON entirely."""
    response = client.post(
        "/api/v1/briefings/morning/regenerate",
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert response.json()["type"] == "morning"


def test_regenerate_rejects_blank_project_field(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_registry: dict[str, _BriefingAdapter],  # noqa: ARG001
) -> None:
    """Empty-string project is a client bug; surface as 400."""
    response = client.post(
        "/api/v1/briefings/morning/regenerate",
        json={"project": "   "},
        headers=auth_headers,
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "invalid_request"


def test_regenerate_timeout_returns_504(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_registry: dict[str, _BriefingAdapter],  # noqa: ARG001
    fake_state: _FakeAdapterState,
) -> None:
    """Slow regenerate exceeds the budget → 504 ``timeout``."""
    fake_state.regen_delay = 2.0
    response = client.post(
        "/api/v1/briefings/morning/regenerate?timeout_seconds=1",
        json={},
        headers=auth_headers,
    )
    assert response.status_code == 504, response.json()
    body = response.json()
    assert body["error"]["code"] == "timeout"
    assert "regenerate" in body["error"]["message"]


def test_regenerate_unknown_type_returns_404(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_registry: dict[str, _BriefingAdapter],  # noqa: ARG001
) -> None:
    """Regenerate against an unknown type is 404."""
    response = client.post(
        "/api/v1/briefings/nope/regenerate",
        json={},
        headers=auth_headers,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_regenerate_provider_exception_returns_503(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_registry: dict[str, _BriefingAdapter],  # noqa: ARG001
    fake_state: _FakeAdapterState,
) -> None:
    """Provider raises → 503 service_unavailable (not 500)."""
    fake_state.regen_raise = RuntimeError("herald exploded")
    response = client.post(
        "/api/v1/briefings/morning/regenerate",
        json={},
        headers=auth_headers,
    )
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "service_unavailable"


def test_auth_required_for_all_briefings_routes(
    client: TestClient,
    patched_registry: dict[str, _BriefingAdapter],  # noqa: ARG001
) -> None:
    """Every briefings route demands a bearer token (spec §2.1)."""
    no_auth: dict[str, str] = {}
    # list
    response = client.get("/api/v1/briefings", headers=no_auth)
    assert response.status_code == 401
    # render
    response = client.get("/api/v1/briefings/morning", headers=no_auth)
    assert response.status_code == 401
    # regenerate
    response = client.post(
        "/api/v1/briefings/morning/regenerate",
        json={},
        headers=no_auth,
    )
    assert response.status_code == 401
