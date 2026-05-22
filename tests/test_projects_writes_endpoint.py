"""Integration tests for Phase 2 project write endpoints.

Covers ``POST /api/v1/projects/{key}/pause``,
``POST /api/v1/projects/{key}/resume``,
``POST /api/v1/projects/{key}/archive``, and
``POST /api/v1/projects/{key}/init-guide`` per the Phase 2 endpoints
spec §6.2.

Run with ``pytest --noconftest tests/test_projects_writes_endpoint.py -v
--timeout=120`` so the per-project conftest (which requires a Postgres
harness) is skipped — these tests exercise the FastAPI app via
``TestClient`` and never reach the work-service backing store.
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
    write_config,
)
from pollypm.models import KnownProject, ProjectKind, ProviderKind, RuntimeKind
from pollypm.web_api import create_app, ensure_token


# ---------------------------------------------------------------------------
# Fixtures — self-contained (do not rely on tests/web_api/conftest.py)
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
def config_path(workspace: Path) -> Path:
    return workspace / ".pollypm" / "pollypm.toml"


@pytest.fixture
def config(workspace: Path, project_root: Path, config_path: Path) -> PollyPMConfig:
    base_dir = workspace / ".pollypm"
    cfg = PollyPMConfig(
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
        config_path=config_path,
    )
    # Seed the global TOML on disk so write_config (force=True) can
    # round-trip without raising FileExistsError on its first read.
    write_config(cfg, config_path, force=True)
    return cfg


@pytest.fixture
def token(tmp_path: Path) -> tuple[Path, str]:
    token_path = tmp_path / "api-token"
    value, _generated = ensure_token(token_path)
    return token_path, value


@pytest.fixture
def auth_headers(token: tuple[Path, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {token[1]}"}


@pytest.fixture
def app(config: PollyPMConfig, token: tuple[Path, str]):
    token_path, _value = token
    return create_app(config=config, token_path=token_path)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Pause
# ---------------------------------------------------------------------------


def test_pause_happy_path_sets_tracked_false(
    client: TestClient, auth_headers: dict[str, str], config: PollyPMConfig
) -> None:
    response = client.post(
        "/api/v1/projects/myproj/pause",
        headers=auth_headers,
        json={"reason": "operator pause"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["key"] == "myproj"
    assert body["tracked"] is False
    # Side effect: in-memory config flipped + persisted TOML reflects it.
    assert config.projects["myproj"].tracked is False


def test_pause_is_idempotent_on_already_paused(
    client: TestClient, auth_headers: dict[str, str], config: PollyPMConfig
) -> None:
    """Already-paused → 200 with the current snapshot.

    DECISION: pause is idempotent (returns 200, no churn) per spec §6.3
    "Pause a paused project. Idempotent; returns 200 with current
    state, no audit churn." A 409 here would force clients to special-
    case a 'pause if not already paused' workflow that the cockpit
    doesn't.
    """
    # First call: 200, flips tracked.
    first = client.post("/api/v1/projects/myproj/pause", headers=auth_headers)
    assert first.status_code == 200, first.text
    assert first.json()["tracked"] is False

    # Second call: still 200, tracked stays false.
    second = client.post("/api/v1/projects/myproj/pause", headers=auth_headers)
    assert second.status_code == 200, second.text
    assert second.json()["tracked"] is False
    assert config.projects["myproj"].tracked is False


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


def test_resume_happy_path_sets_tracked_true(
    client: TestClient, auth_headers: dict[str, str], config: PollyPMConfig
) -> None:
    # Start from paused.
    config.projects["myproj"].tracked = False
    response = client.post(
        "/api/v1/projects/myproj/resume", headers=auth_headers, json={}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tracked"] is True
    assert config.projects["myproj"].tracked is True


def test_resume_is_idempotent_on_already_tracked(
    client: TestClient, auth_headers: dict[str, str], config: PollyPMConfig
) -> None:
    # Already tracked — call should still 200 and leave state intact.
    response = client.post(
        "/api/v1/projects/myproj/resume", headers=auth_headers, json={}
    )
    assert response.status_code == 200, response.text
    assert response.json()["tracked"] is True
    assert config.projects["myproj"].tracked is True


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------


def test_archive_happy_path_removes_from_config(
    client: TestClient, auth_headers: dict[str, str], config: PollyPMConfig
) -> None:
    response = client.post(
        "/api/v1/projects/myproj/archive",
        headers=auth_headers,
        json={"reason": "deprecated"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert "archived" in body["message"].lower()
    assert "deprecated" in body["message"]
    assert "myproj" not in config.projects


def test_archive_irreversible_resume_after_archive_returns_404(
    client: TestClient, auth_headers: dict[str, str], config: PollyPMConfig
) -> None:
    """Archive is irreversible from the API.

    DECISION: archive removes the project from config (per spec §6.2).
    Subsequent resume / pause / init-guide calls 404 because the key
    is gone. Re-onboarding uses the CLI's ``pm add-project``.
    """
    archived = client.post(
        "/api/v1/projects/myproj/archive", headers=auth_headers, json={}
    )
    assert archived.status_code == 200

    resume_attempt = client.post(
        "/api/v1/projects/myproj/resume", headers=auth_headers, json={}
    )
    assert resume_attempt.status_code == 404
    assert resume_attempt.json()["error"]["code"] == "not_found"

    pause_attempt = client.post(
        "/api/v1/projects/myproj/pause", headers=auth_headers, json={}
    )
    assert pause_attempt.status_code == 404


# ---------------------------------------------------------------------------
# Init-guide
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["architect", "reviewer", "worker"])
def test_init_guide_happy_per_role(
    client: TestClient,
    auth_headers: dict[str, str],
    project_root: Path,
    role: str,
) -> None:
    response = client.post(
        "/api/v1/projects/myproj/init-guide",
        headers=auth_headers,
        json={"role": role},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["role"] == role
    assert body["body"]
    # File actually landed on disk under .pollypm/project-guides/<role>.md.
    written = Path(body["path"])
    assert written.exists()
    assert written.parent == project_root / ".pollypm" / "project-guides"


def test_init_guide_unknown_role_returns_422(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/v1/projects/myproj/init-guide",
        headers=auth_headers,
        json={"role": "operator_pm"},
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"]["code"] == "validation_error"


def test_init_guide_existing_without_force_returns_409(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    # Seed once — succeeds.
    first = client.post(
        "/api/v1/projects/myproj/init-guide",
        headers=auth_headers,
        json={"role": "architect"},
    )
    assert first.status_code == 200

    # Re-send without force — server refuses with 409.
    second = client.post(
        "/api/v1/projects/myproj/init-guide",
        headers=auth_headers,
        json={"role": "architect"},
    )
    assert second.status_code == 409, second.text
    body = second.json()
    assert body["error"]["code"] == "conflict"
    assert body["error"]["hint"] and "force" in body["error"]["hint"].lower()


def test_init_guide_existing_with_force_returns_200(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    first = client.post(
        "/api/v1/projects/myproj/init-guide",
        headers=auth_headers,
        json={"role": "worker"},
    )
    assert first.status_code == 200
    first_path = Path(first.json()["path"])
    # Tamper the on-disk file so we can detect the overwrite.
    first_path.write_text("# stale guide body\n", encoding="utf-8")

    second = client.post(
        "/api/v1/projects/myproj/init-guide",
        headers=auth_headers,
        json={"role": "worker", "force": True},
    )
    assert second.status_code == 200, second.text
    # Body re-rendered from the built-in template; the stale content is gone.
    assert "# stale guide body" not in second.json()["body"]


# ---------------------------------------------------------------------------
# Cross-cutting — auth + unknown project
# ---------------------------------------------------------------------------


def test_pause_requires_auth(client: TestClient) -> None:
    response = client.post("/api/v1/projects/myproj/pause", json={})
    assert response.status_code == 401


def test_archive_requires_auth(client: TestClient) -> None:
    response = client.post("/api/v1/projects/myproj/archive", json={})
    assert response.status_code == 401


def test_init_guide_requires_auth(client: TestClient) -> None:
    response = client.post(
        "/api/v1/projects/myproj/init-guide", json={"role": "architect"}
    )
    assert response.status_code == 401


def test_pause_unknown_project_returns_404(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/v1/projects/no-such-project/pause",
        headers=auth_headers,
        json={},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_resume_unknown_project_returns_404(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/v1/projects/no-such-project/resume",
        headers=auth_headers,
        json={},
    )
    assert response.status_code == 404


def test_archive_unknown_project_returns_404(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/v1/projects/no-such-project/archive",
        headers=auth_headers,
        json={},
    )
    assert response.status_code == 404


def test_init_guide_unknown_project_returns_404(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/v1/projects/no-such-project/init-guide",
        headers=auth_headers,
        json={"role": "architect"},
    )
    assert response.status_code == 404
