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
    load_config,
    write_config,
)
from pollypm.models import (
    KnownProject,
    ProjectKind,
    ProviderKind,
    RuntimeKind,
    SessionConfig,
)
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


# ---------------------------------------------------------------------------
# Codex review regressions on PR #2063 — config write durability invariants.
#
# These four tests pin down behaviour the original Phase 2 patch broke:
#
# 1. rollback on disk-write failure (Codex P0 #1 / service.py:352)
# 2. concurrent-edit preservation (Codex P0 #2 / service.py:355)
# 3. session→project invariant on archive (Codex P0 #3 / service.py:387)
# 4. ``reason`` field actually emitted as an audit event (Codex P1 /
#    routes/projects.py:246)
# ---------------------------------------------------------------------------


def test_pause_rolls_back_in_memory_when_disk_write_fails(
    client: TestClient,
    auth_headers: dict[str, str],
    config: PollyPMConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex P0 #1: failed write_config MUST NOT flip the live config.

    Reproduces the original bug: patch ``pollypm.config.write_config``
    to raise ``OSError`` and confirm:

    * the API returns 503 (mapped via ``service_unavailable``),
    * the in-memory ``cfg.projects['myproj'].tracked`` stays True,
    * a subsequent GET reflects the unchanged value (no in-memory lie).
    """
    assert config.projects["myproj"].tracked is True

    def _fail_write_config(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError("simulated disk-full during config persist")

    monkeypatch.setattr("pollypm.config.write_config", _fail_write_config)

    response = client.post(
        "/api/v1/projects/myproj/pause",
        headers=auth_headers,
        json={"reason": "test rollback"},
    )
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["error"]["code"] == "service_unavailable"

    # Live config was NOT mutated — the in-memory rollback contract.
    assert config.projects["myproj"].tracked is True

    # And a subsequent GET reflects the unchanged value (no stale lie).
    get_response = client.get(
        "/api/v1/projects/myproj", headers=auth_headers
    )
    assert get_response.status_code == 200
    assert get_response.json()["tracked"] is True


def test_pause_preserves_concurrent_external_project_addition(
    client: TestClient,
    auth_headers: dict[str, str],
    config: PollyPMConfig,
    config_path: Path,
    workspace: Path,
    tmp_path: Path,
) -> None:
    """Codex P0 #2: concurrent CLI edit must survive an API tracked-toggle.

    Reproduces the original bug: simulate an external ``pm add-project``
    that lands a new project key ``external`` on disk between server
    boot and the API call, then call the API ``/pause`` on ``myproj``.

    With the original ``force=True`` write-back the API would silently
    drop ``external`` from disk. The fix reloads from disk first.
    """
    # Simulate an external CLI / cockpit edit adding a second project.
    external_root = tmp_path / "external"
    external_root.mkdir()
    (external_root / ".pollypm").mkdir()
    fresh = load_config(config_path)
    fresh.projects["external"] = KnownProject(
        key="external",
        path=external_root,
        name="External Project",
        tracked=True,
        kind=ProjectKind.GIT,
    )
    write_config(fresh, config_path, force=True)

    # Sanity: in-memory ``config`` (the long-lived server snapshot)
    # has NOT seen the external addition yet — that's the whole point.
    assert "external" not in config.projects

    # Now the API call.
    response = client.post(
        "/api/v1/projects/myproj/pause",
        headers=auth_headers,
        json={"reason": "concurrent edit test"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["tracked"] is False

    # Re-read TOML from disk and assert BOTH:
    # * the API mutation landed (myproj.tracked = False), AND
    # * the externally-added project survives.
    reloaded = load_config(config_path)
    assert "myproj" in reloaded.projects
    assert reloaded.projects["myproj"].tracked is False
    assert "external" in reloaded.projects, (
        "external project was clobbered by the API write — "
        "Codex P0 #2 regression"
    )
    assert reloaded.projects["external"].tracked is True


def test_archive_blocked_when_enabled_session_references_project(
    workspace: Path,
    project_root: Path,
    config_path: Path,
    tmp_path: Path,
) -> None:
    """Codex P0 #3: archive must enforce the session→project invariant.

    Build a config with an enabled session referencing ``myproj``, then
    POST ``/archive``. Expect 409 with the session name in the body —
    matches the guard ``pollypm.projects.remove_project`` already
    enforces for the CLI.
    """
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
        sessions={
            "blocker-session": SessionConfig(
                name="blocker-session",
                role="worker",
                provider=ProviderKind.CODEX,
                account="codex_primary",
                cwd=project_root,
                project="myproj",
                enabled=True,
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
        config_path=config_path,
    )
    write_config(cfg, config_path, force=True)

    token_path = tmp_path / "api-token"
    value, _ = ensure_token(token_path)
    app = create_app(config=cfg, token_path=token_path)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {value}"}

    response = client.post(
        "/api/v1/projects/myproj/archive",
        headers=headers,
        json={"reason": "ignored — should 409"},
    )
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error"]["code"] == "conflict"
    assert "blocker-session" in body["error"]["message"], (
        "409 body should name the blocking session — Codex P0 #3"
    )

    # Project was NOT removed in-memory.
    assert "myproj" in cfg.projects


def test_pause_emits_audit_event_with_reason(
    client: TestClient,
    auth_headers: dict[str, str],
    config: PollyPMConfig,
    project_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex P1: ``reason`` must persist somewhere durable.

    Original bug: the API accepted and echoed ``reason`` but the
    service silently dropped it. The fix emits a ``projects.tracked.set``
    audit event with ``reason`` in metadata so operators can grep the
    audit log for "why".

    Pins ``POLLYPM_AUDIT_HOME`` to a tmp dir so this test stays clean
    when run with ``--noconftest`` (the global conftest normally
    redirects audit output, but the file-level docstring instructs
    callers to run with ``--noconftest`` to skip the postgres harness).
    """
    import json

    from pollypm.audit.log import central_log_path, project_log_path

    audit_home = tmp_path / "audit"
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))

    response = client.post(
        "/api/v1/projects/myproj/pause",
        headers=auth_headers,
        json={"reason": "testing-audit-emit"},
    )
    assert response.status_code == 200, response.text

    # Two surfaces accept the event — per-project log + central tail.
    # Either is sufficient for the contract; check both.
    found = False
    for path in (project_log_path(project_root), central_log_path("myproj")):
        if path is None or not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("event") == "projects.tracked.set" and rec.get(
                "metadata", {}
            ).get("reason") == "testing-audit-emit":
                found = True
                break
        if found:
            break

    assert found, (
        "Expected projects.tracked.set audit event with "
        "reason='testing-audit-emit' — Codex P1 regression"
    )
