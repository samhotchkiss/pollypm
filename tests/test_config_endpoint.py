"""Tests for the Phase 2 read-only config endpoints (surface §13).

Covers ``GET /api/v1/config`` and ``GET /api/v1/config/projects/{key}``:

- happy path returns the full redacted config
- credential-shaped fields (``auth_token``, ``api_key``, account
  ``env`` secrets) are replaced with ``"***"``
- per-project filter returns the right block; unknown key → 404
- auth required on both endpoints
- minimal config (no projects, no sessions) doesn't crash

The test is self-contained: it builds a :class:`PollyPMConfig`
in-memory and constructs the FastAPI app via
:func:`pollypm.web_api.create_app`. Run with
``pytest --noconftest tests/test_config_endpoint.py -v --timeout=120``.
"""

from __future__ import annotations

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
from pollypm.web_api import create_app, ensure_token
from pollypm.web_api.routes.config import REDACTED


# ---------------------------------------------------------------------------
# Local fixtures (no conftest — runner uses --noconftest)
# ---------------------------------------------------------------------------


def _build_config(
    *,
    workspace_root: Path,
    project_root: Path,
    include_session_with_token: bool = True,
    include_account_env_secret: bool = True,
    projects: dict[str, KnownProject] | None = None,
) -> PollyPMConfig:
    base_dir = workspace_root / ".pollypm"
    base_dir.mkdir(parents=True, exist_ok=True)
    state_db = base_dir / "state.db"

    accounts: dict[str, AccountConfig] = {
        "codex_primary": AccountConfig(
            name="codex_primary",
            provider=ProviderKind.CODEX,
            email="codex@example.com",
            runtime=RuntimeKind.LOCAL,
            home=base_dir / "homes" / "codex_primary",
            env=(
                {
                    "OPENAI_API_KEY": "sk-super-secret-12345",
                    "PUBLIC_FLAG": "ok",
                }
                if include_account_env_secret
                else {}
            ),
        ),
    }

    sessions: dict[str, SessionConfig] = {}
    if include_session_with_token:
        sessions["operator"] = SessionConfig(
            name="operator",
            role="operator",
            provider=ProviderKind.CODEX,
            account="codex_primary",
            cwd=workspace_root,
            project="myproj",
            auth_token="deadbeef" * 8,  # 64-char hex; should be redacted
        )

    if projects is None:
        projects = {
            "myproj": KnownProject(
                key="myproj",
                path=project_root,
                name="My Project",
                tracked=True,
                kind=ProjectKind.GIT,
            ),
        }

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
        accounts=accounts,
        sessions=sessions,
        projects=projects,
        memory=MemorySettings(backend="file"),
    )


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "myproj"
    root.mkdir()
    (root / ".pollypm").mkdir()
    return root


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
def api_config(workspace_root: Path, project_root: Path) -> PollyPMConfig:
    return _build_config(workspace_root=workspace_root, project_root=project_root)


@pytest.fixture
def client(api_config: PollyPMConfig, token_path: Path, token: str) -> TestClient:
    _ = token  # ensure the token file exists before the app reads it
    app = create_app(config=api_config, token_path=token_path)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_get_config_happy_path_returns_full_config(client: TestClient, auth_headers: dict[str, str]) -> None:
    """``GET /api/v1/config`` returns the loaded config envelope."""
    response = client.get("/api/v1/config", headers=auth_headers)
    assert response.status_code == 200

    body = response.json()
    assert "config" in body, body
    cfg = body["config"]

    # Sanity: the obvious top-level fields are present.
    assert cfg["project"]["name"] == "PollyPM"
    assert cfg["project"]["tmux_session"] == "pollypm-test"
    assert "myproj" in cfg["projects"]
    assert cfg["projects"]["myproj"]["name"] == "My Project"
    # Enum (StrEnum) → ``.value``; not the repr.
    assert cfg["projects"]["myproj"]["kind"] == "git"


def test_get_config_paths_coerced_to_strings(client: TestClient, auth_headers: dict[str, str], workspace_root: Path) -> None:
    """``pathlib.Path`` values flatten to strings so the body is JSON-safe."""
    body = client.get("/api/v1/config", headers=auth_headers).json()
    root_dir = body["config"]["project"]["root_dir"]
    assert isinstance(root_dir, str)
    assert root_dir == str(workspace_root)


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_session_auth_token_is_redacted(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Session ``auth_token`` must never round-trip through the API."""
    body = client.get("/api/v1/config", headers=auth_headers).json()
    sessions = body["config"]["sessions"]
    assert "operator" in sessions
    assert sessions["operator"]["auth_token"] == REDACTED
    # The literal token value must not appear anywhere in the response.
    assert "deadbeef" not in response_text(body)


def test_account_env_api_key_is_redacted(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Account ``env`` entries whose keys look secret are redacted.

    The redaction walks every nested dict; an account's ``env`` map
    holds the OS-level credential exports, so ``OPENAI_API_KEY`` must
    flatten to ``"***"`` even though it's two levels deep.
    """
    body = client.get("/api/v1/config", headers=auth_headers).json()
    env = body["config"]["accounts"]["codex_primary"]["env"]
    assert env["OPENAI_API_KEY"] == REDACTED
    # Non-secret env keys pass through unchanged.
    assert env["PUBLIC_FLAG"] == "ok"
    assert "sk-super-secret-12345" not in response_text(body)


def test_empty_auth_token_not_overwritten(
    workspace_root: Path,
    project_root: Path,
    token_path: Path,
    token: str,
    auth_headers: dict[str, str],
) -> None:
    """Legacy ``auth_token=""`` must stay empty (not become ``"***"``).

    Sessions written before Lever 2 (#2012) hold an empty auth_token;
    redacting that to ``"***"`` would misrepresent the on-disk state
    (the operator can no longer tell "field unset" from "field hidden").
    """
    _ = token
    config = _build_config(
        workspace_root=workspace_root,
        project_root=project_root,
        include_session_with_token=False,
    )
    # Add a session with an explicitly-empty auth_token.
    config.sessions["legacy"] = SessionConfig(
        name="legacy",
        role="operator",
        provider=ProviderKind.CODEX,
        account="codex_primary",
        cwd=workspace_root,
        project="myproj",
        auth_token="",
    )
    app = create_app(config=config, token_path=token_path)
    with TestClient(app) as client:
        body = client.get("/api/v1/config", headers=auth_headers).json()
    assert body["config"]["sessions"]["legacy"]["auth_token"] == ""


# ---------------------------------------------------------------------------
# Per-project filter
# ---------------------------------------------------------------------------


def test_project_filter_returns_single_block(client: TestClient, auth_headers: dict[str, str]) -> None:
    """``GET /config/projects/{key}`` returns just that project's block."""
    response = client.get("/api/v1/config/projects/myproj", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["key"] == "myproj"
    assert body["project"]["name"] == "My Project"
    assert body["project"]["kind"] == "git"
    assert body["project"]["tracked"] is True


def test_project_filter_unknown_key_returns_404(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Unknown project key → 404 ``not_found`` with the spec envelope."""
    response = client.get("/api/v1/config/projects/does-not-exist", headers=auth_headers)
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert "does-not-exist" in body["error"]["message"]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_get_config_requires_auth(client: TestClient) -> None:
    """No bearer → 401 (matches the rest of the API surface)."""
    response = client.get("/api/v1/config")
    assert response.status_code == 401


def test_get_project_config_requires_auth(client: TestClient) -> None:
    response = client.get("/api/v1/config/projects/myproj")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_minimal_config_does_not_crash(
    workspace_root: Path,
    token_path: Path,
    token: str,
    auth_headers: dict[str, str],
) -> None:
    """Empty projects + empty sessions still serialises cleanly.

    Fresh installs (or test fixtures that skip account setup) shouldn't
    crash the endpoint just because dict-valued fields are empty.
    """
    _ = token
    base_dir = workspace_root / ".pollypm"
    base_dir.mkdir(parents=True, exist_ok=True)
    config = PollyPMConfig(
        project=ProjectSettings(
            name="PollyPM",
            root_dir=workspace_root,
            tmux_session="pollypm-min",
            workspace_root=workspace_root,
            base_dir=base_dir,
            logs_dir=base_dir / "logs",
            snapshots_dir=base_dir / "snapshots",
            state_db=base_dir / "state.db",
        ),
        pollypm=PollyPMSettings(controller_account="codex_primary"),
        accounts={},
        sessions={},
        projects={},
        memory=MemorySettings(backend="file"),
    )
    app = create_app(config=config, token_path=token_path)
    with TestClient(app) as client:
        response = client.get("/api/v1/config", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["config"]["projects"] == {}
    assert body["config"]["sessions"] == {}
    assert body["config"]["accounts"] == {}


def test_known_project_credentials_redacted_in_per_project_view(
    workspace_root: Path,
    project_root: Path,
    token_path: Path,
    token: str,
    auth_headers: dict[str, str],
) -> None:
    """Per-project view also redacts credential-shaped fields.

    ``KnownProject`` doesn't carry secrets today, but the per-project
    serialiser shares the redaction walker with the full-config one —
    this test pins the contract so a future credential-shaped field on
    ``KnownProject`` can't accidentally leak.
    """
    _ = token
    config = _build_config(
        workspace_root=workspace_root,
        project_root=project_root,
        projects={
            "myproj": KnownProject(
                key="myproj",
                path=project_root,
                name="My Project",
                tracked=True,
                kind=ProjectKind.GIT,
            ),
        },
    )
    app = create_app(config=config, token_path=token_path)
    with TestClient(app) as client:
        body = client.get(
            "/api/v1/config/projects/myproj", headers=auth_headers,
        ).json()
    # Every key that looks credential-shaped must be ``"***"``; nothing
    # in the canonical KnownProject shape should match today, so the
    # block stays unredacted — but the walker still runs.
    for key, value in body["project"].items():
        if any(needle in key.lower() for needle in ("token", "secret", "api_key")):
            assert value == REDACTED, f"unredacted credential-shaped field: {key}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def response_text(body: object) -> str:
    """Stringify the response for substring leak-checks."""
    import json

    return json.dumps(body, default=str)
