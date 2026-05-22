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
    client: TestClient,
    auth_headers: dict[str, str],
    config: PollyPMConfig,
    config_path: Path,
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
    # Side effect: persisted TOML reflects the flip. Post-#2056 the
    # route reloads via ``load_config(config_path)`` per request, so the
    # original fixture ``config`` object is no longer what the route
    # mutates — assert against disk (Pattern A).
    reloaded = load_config(config_path)
    assert reloaded.projects["myproj"].tracked is False


def test_pause_is_idempotent_on_already_paused(
    client: TestClient,
    auth_headers: dict[str, str],
    config: PollyPMConfig,
    config_path: Path,
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
    # Disk reflects the paused state (Pattern A — post-#2056 per-request
    # reload, the fixture ``config`` object is not the route's snapshot).
    reloaded = load_config(config_path)
    assert reloaded.projects["myproj"].tracked is False


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


def test_resume_happy_path_sets_tracked_true(
    client: TestClient,
    auth_headers: dict[str, str],
    config: PollyPMConfig,
    config_path: Path,
) -> None:
    # Start from paused — write to disk so the per-request load_config
    # reload sees ``tracked=False`` (post-#2056 the route reloads via
    # ``load_config(config_path)`` per request, so an in-memory mutation
    # to the fixture ``config`` would be invisible to the route).
    paused = load_config(config_path)
    paused.projects["myproj"].tracked = False
    write_config(paused, config_path, force=True)

    response = client.post(
        "/api/v1/projects/myproj/resume", headers=auth_headers, json={}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tracked"] is True
    # Assert against disk (Pattern A).
    reloaded = load_config(config_path)
    assert reloaded.projects["myproj"].tracked is True


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
    client: TestClient,
    auth_headers: dict[str, str],
    config: PollyPMConfig,
    config_path: Path,
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
    # Disk reflects the removal (Pattern A — post-#2056 per-request
    # reload, the fixture ``config`` object is not the route's snapshot).
    reloaded = load_config(config_path)
    assert "myproj" not in reloaded.projects


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


# ---------------------------------------------------------------------------
# Codex round 2 on PR #2063 — stale-snapshot idempotency.
#
# The route-level "already at target" short-circuit decided idempotency
# against the long-lived ConfigDep snapshot. When disk has been edited
# externally to the opposite value, the route returned 200 with the live
# (stale) state and never wrote — disk stayed out of sync.
#
# Fix: idempotency lives in ``set_project_tracked`` after the fresh
# ``load_config(config_path)``. Routes delegate unconditionally.
# ---------------------------------------------------------------------------


def test_resume_with_live_true_but_disk_false_reflects_disk(
    client: TestClient,
    auth_headers: dict[str, str],
    config: PollyPMConfig,
    config_path: Path,
) -> None:
    """Codex round 2: stale live=True vs disk=False on /resume.

    Reproduces the route-bypass bug: server boots with ``tracked=True``
    in memory, an external editor flips disk to ``tracked=False``, then
    ``POST /resume`` lands. Pre-fix the route returned 200 with the
    stale live value and disk stayed ``tracked=False``. Post-fix the
    service helper reloads disk, sees ``False`` != target ``True``,
    writes ``True``, and both surfaces agree.
    """
    # Sanity: live thinks tracked.
    assert config.projects["myproj"].tracked is True

    # External editor flips disk to tracked=False without touching the
    # live in-memory snapshot.
    fresh = load_config(config_path)
    fresh.projects["myproj"].tracked = False
    write_config(fresh, config_path, force=True)
    # Live snapshot is still stale on purpose.
    assert config.projects["myproj"].tracked is True

    response = client.post(
        "/api/v1/projects/myproj/resume", headers=auth_headers, json={}
    )
    assert response.status_code == 200, response.text
    assert response.json()["tracked"] is True

    # Disk MUST reflect the resume — pre-fix this stayed False because
    # the route short-circuited on the stale live value and never wrote.
    reloaded = load_config(config_path)
    assert reloaded.projects["myproj"].tracked is True, (
        "Disk did not reflect /resume target — Codex round-2 regression "
        "(stale-snapshot idempotency on the route bypassed the durable "
        "write path)."
    )
    # Live snapshot is now in sync too.
    assert config.projects["myproj"].tracked is True


def test_pause_with_live_false_but_disk_true_reflects_disk(
    client: TestClient,
    auth_headers: dict[str, str],
    config: PollyPMConfig,
    config_path: Path,
) -> None:
    """Codex round 2: stale live=False vs disk=True on /pause (mirror case).

    Boot live as ``tracked=False`` (already-paused snapshot), have an
    external editor flip disk to ``tracked=True``, then ``POST /pause``.
    Pre-fix the route short-circuited on live=False and returned 200
    without writing; disk stayed ``True``. Post-fix the service helper
    reloads, sees ``True`` != target ``False``, writes ``False``, and
    both surfaces agree.
    """
    # Flip live to False so the pre-fix route would short-circuit.
    config.projects["myproj"].tracked = False

    # External editor flips disk back to tracked=True.
    fresh = load_config(config_path)
    fresh.projects["myproj"].tracked = True
    write_config(fresh, config_path, force=True)
    # Live snapshot is intentionally stale (False vs disk True).
    assert config.projects["myproj"].tracked is False

    response = client.post(
        "/api/v1/projects/myproj/pause",
        headers=auth_headers,
        json={"reason": "stale-snapshot regression"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["tracked"] is False

    # Disk MUST reflect the pause — pre-fix this stayed True.
    reloaded = load_config(config_path)
    assert reloaded.projects["myproj"].tracked is False, (
        "Disk did not reflect /pause target — Codex round-2 regression "
        "(stale-snapshot idempotency on the route bypassed the durable "
        "write path)."
    )
    assert config.projects["myproj"].tracked is False


def test_resume_idempotent_when_disk_already_true_syncs_live(
    client: TestClient,
    auth_headers: dict[str, str],
    config: PollyPMConfig,
    config_path: Path,
) -> None:
    """Codex round 2 (restated post-#2056): idempotent /resume reflects disk.

    Post-#2056 the route reloads via ``load_config(config_path)`` per
    request, so the "live snapshot" concept that round-2 pinned is gone:
    every request gets the freshly-loaded disk state. The invariant we
    still care about is that an idempotent /resume (disk already at the
    target) returns 200 with the disk-backed snapshot AND a follow-up
    GET keeps reflecting the disk value. Pattern A — assert via disk +
    a subsequent GET, not via the fixture ``config`` object.
    """
    # Disk = True (already at /resume's target).
    fresh = load_config(config_path)
    fresh.projects["myproj"].tracked = True
    write_config(fresh, config_path, force=True)

    response = client.post(
        "/api/v1/projects/myproj/resume", headers=auth_headers, json={}
    )
    assert response.status_code == 200, response.text
    assert response.json()["tracked"] is True

    # Disk is unchanged at True.
    reloaded = load_config(config_path)
    assert reloaded.projects["myproj"].tracked is True
    # A follow-up GET reflects disk too (the per-request reload picks up
    # the same disk state).
    get_response = client.get(
        "/api/v1/projects/myproj", headers=auth_headers
    )
    assert get_response.status_code == 200
    assert get_response.json()["tracked"] is True


# ---------------------------------------------------------------------------
# Codex round 3 on PR #2063 — full live-snapshot refresh after every mutation.
#
# Round-2 idempotency only copied tracked/path/name from disk back into the
# long-lived ConfigDep object. Round 3 caught that other KnownProject fields
# (persona_name, kind, role assignments, worker caps, plan enforcement, ...)
# stayed stale when an external CLI / cockpit edit changed them on the same
# project before the API call. Mirror gap on archive: external project
# additions on disk before the archive stayed invisible to GET /projects
# until restart.
#
# Fix: ``_refresh_live_projects`` does a full ``clear() + update()`` from
# the freshly-loaded disk config after every mutation path.
# ---------------------------------------------------------------------------


def test_pause_idempotent_refreshes_external_metadata_edit(
    client: TestClient,
    auth_headers: dict[str, str],
    config: PollyPMConfig,
    config_path: Path,
) -> None:
    """Codex round 3 (restated post-#2056): idempotent /pause reflects disk metadata.

    Start with ``tracked=False, persona_name="new"`` on disk (the post-
    external-edit state). POST /pause hits the idempotent branch (target
    matches disk) and MUST return a response Project whose
    ``persona_name`` reflects the disk value, plus a follow-up GET that
    sees the same value.

    Post-#2056 the route reloads via ``load_config(config_path)`` per
    request, so the "live snapshot refresh" the round-3 patch added is
    no longer necessary for the NEXT request — that request reloads from
    disk anyway. What we still pin is that the response built from the
    in-request reload reflects all disk fields, not just ``tracked``.
    """
    # Disk = tracked=False, persona_name="new" — the state after an
    # external editor changed both ``tracked`` (to match the target) and
    # ``persona_name`` between server boot and this call.
    disk = load_config(config_path)
    disk.projects["myproj"].tracked = False
    disk.projects["myproj"].persona_name = "new"
    write_config(disk, config_path, force=True)

    # POST /pause — target tracked=False matches disk, so the helper
    # takes the idempotent (no-write) branch.
    response = client.post(
        "/api/v1/projects/myproj/pause",
        headers=auth_headers,
        json={"reason": "round-3 idempotent metadata refresh"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tracked"] is False
    # Response Project surfaces persona_name — must reflect the disk
    # value the route reloaded.
    assert body["persona_name"] == "new"

    # A subsequent GET still reflects disk (Pattern A).
    get_response = client.get(
        "/api/v1/projects/myproj", headers=auth_headers
    )
    assert get_response.status_code == 200
    assert get_response.json()["persona_name"] == "new"
    # Disk unchanged.
    reloaded = load_config(config_path)
    assert reloaded.projects["myproj"].persona_name == "new"
    assert reloaded.projects["myproj"].tracked is False


def test_resume_write_success_refreshes_external_metadata_edit(
    client: TestClient,
    auth_headers: dict[str, str],
    config: PollyPMConfig,
    config_path: Path,
) -> None:
    """Codex round 3 (restated post-#2056): write-success /resume preserves external metadata.

    Disk has ``tracked=False, persona_name="new"`` after an external
    editor changed both fields. POST /resume's target (True) differs
    from disk (False) so the helper writes. The write MUST preserve the
    external ``persona_name`` edit (concurrent-edit preservation, Codex
    P0 #2 still applies) and the response MUST surface it.

    Post-#2056 the route reloads via ``load_config(config_path)`` per
    request, so we don't pin a "live snapshot refresh" anymore — the
    next request reloads from disk anyway. What we pin is that the
    durable write preserves concurrent external metadata edits and the
    response reflects the post-write disk state.
    """
    # Disk: tracked=False (so /resume must write True), persona_name=
    # "new" from an external edit we must not clobber.
    external = load_config(config_path)
    external.projects["myproj"].tracked = False
    external.projects["myproj"].persona_name = "new"
    write_config(external, config_path, force=True)

    # POST /resume — target True != disk False, so the helper writes.
    response = client.post(
        "/api/v1/projects/myproj/resume", headers=auth_headers, json={}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tracked"] is True
    # Response Project surfaces persona_name — must reflect the
    # external edit the write preserved.
    assert body["persona_name"] == "new"

    # Disk must reflect the resume write AND preserve persona_name.
    reloaded = load_config(config_path)
    assert reloaded.projects["myproj"].tracked is True
    assert reloaded.projects["myproj"].persona_name == "new", (
        "Write-success resume clobbered concurrent external persona_name "
        "edit — Codex P0 #2 (concurrent-edit preservation) regression."
    )


def test_archive_surfaces_concurrent_external_project_addition(
    client: TestClient,
    auth_headers: dict[str, str],
    config: PollyPMConfig,
    config_path: Path,
    tmp_path: Path,
) -> None:
    """Codex round 3 (restated post-#2056): /archive preserves external additions.

    Start with ``myproj`` only on disk. An external CLI lands a second
    project ``external`` on disk between server boot and the API call.
    POST /archive removes ``myproj``; the durable write MUST preserve
    ``external`` (Codex P0 #2 concurrent-edit preservation), and a
    subsequent GET /projects reloads disk and surfaces ``external``.

    Post-#2056 the route reloads via ``load_config(config_path)`` per
    request, so we don't pin a "live snapshot refresh" anymore — we
    pin the durable invariant (disk reflects archive + preserves the
    concurrent add) and the GET round-trip.
    """
    # External CLI adds a second project on disk.
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

    # POST /archive on myproj.
    response = client.post(
        "/api/v1/projects/myproj/archive",
        headers=auth_headers,
        json={"reason": "round-3 archive refresh"},
    )
    assert response.status_code == 200, response.text

    # Disk MUST drop myproj AND preserve external (Pattern A).
    reloaded = load_config(config_path)
    assert "myproj" not in reloaded.projects
    assert "external" in reloaded.projects, (
        "Archive clobbered concurrent external project addition — "
        "Codex P0 #2 (concurrent-edit preservation) regression."
    )

    # And GET /projects sees external too (end-to-end via the per-request
    # reload).
    list_response = client.get(
        "/api/v1/projects", headers=auth_headers
    )
    assert list_response.status_code == 200
    keys = [item["key"] for item in list_response.json()["items"]]
    assert "external" in keys, (
        "GET /projects did not surface concurrent external addition after "
        "archive."
    )
    assert "myproj" not in keys


# ---------------------------------------------------------------------------
# Codex round 4 on PR #2063 — rollback on the cached load_config path.
#
# Round 1 added a rollback regression but constructed the live config by hand
# (i.e. NOT via ``load_config``) so the cached-singleton aliasing never came
# into play. Codex round 4 reproduced the bug against the production path:
# ``pm serve`` builds ``ConfigDep`` via ``load_config(config_path)``, and
# ``set_project_tracked`` then calls ``load_config(config_path)`` AGAIN — but
# ``load_config`` memoises by path, so the "fresh" snapshot is literally the
# same Python object as the live ``ConfigDep``. Mutating
# ``fresh.projects[key].tracked`` before ``write_config`` therefore flipped
# the live snapshot too; if the write then raised, the API returned 503 but
# subsequent GETs returned the flipped (stale-lie) value.
#
# Fix: deep-copy the loaded snapshot before mutating it; live ``ConfigDep``
# is only touched via ``_refresh_live_projects`` AFTER the write succeeds.
# Archive has the analogous bug through the ``remove_project`` facade
# (``del config.projects[key]`` on the cached object before ``write_config``);
# the service-level fix snapshots the live entry and restores it on OSError.
# ---------------------------------------------------------------------------


def test_pause_rolls_back_with_real_load_config_cache(
    workspace: Path,
    project_root: Path,
    config_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex round 4: rollback must hold when live config came via load_config.

    Reproduce the production path exactly:

    * write a seed config to disk,
    * call ``load_config(config_path)`` to get the LIVE snapshot (which
      enters the module-level cache),
    * build the FastAPI app on that live snapshot,
    * monkeypatch ``pollypm.config.write_config`` to raise OSError,
    * POST ``/pause``.

    Expect 503, AND the live snapshot's ``tracked`` MUST stay True, AND
    a follow-up ``load_config(config_path)`` (cache hit — same object)
    MUST also stay True. Pre-fix the live snapshot flipped to False
    because ``fresh = load_config(...)`` returned the very same object
    we'd handed to FastAPI.
    """
    from pollypm.config import (
        AccountConfig,
        MemorySettings,
        PollyPMConfig,
        PollyPMSettings,
        ProjectSettings,
        load_config,
        write_config,
    )

    base_dir = workspace / ".pollypm"
    seed = PollyPMConfig(
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
    write_config(seed, config_path, force=True)

    # CRITICAL: build the live snapshot via load_config so the API and the
    # service helper share the SAME cached object (production path).
    live = load_config(config_path)
    assert live.projects["myproj"].tracked is True

    token_path = tmp_path / "api-token"
    value, _ = ensure_token(token_path)
    app = create_app(config=live, token_path=token_path)
    client_ = TestClient(app)
    headers = {"Authorization": f"Bearer {value}"}

    # Sanity: a second load_config returns the EXACT same object (cache
    # hit). This is what would have made the original mutate-then-write
    # bug undetectable in the hand-built-config rollback test.
    assert load_config(config_path) is live, (
        "load_config did not return the cached object; this test's "
        "assumptions about the cached-singleton path are invalid."
    )

    def _fail_write_config(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError("simulated disk-full during config persist")

    monkeypatch.setattr("pollypm.config.write_config", _fail_write_config)

    response = client_.post(
        "/api/v1/projects/myproj/pause",
        headers=headers,
        json={"reason": "round-4 cached-config rollback"},
    )
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "service_unavailable"

    # Live snapshot MUST stay True — this is the round-4 invariant.
    assert live.projects["myproj"].tracked is True, (
        "Live ConfigDep flipped to False on a failed write — Codex "
        "round-4 regression (deep-copy of the cached load_config "
        "snapshot is missing)."
    )

    # Cache-hit reload MUST also stay True (same object as ``live``).
    cached_again = load_config(config_path)
    assert cached_again.projects["myproj"].tracked is True

    # And a subsequent GET reflects the unchanged value (no stale lie).
    get_response = client_.get("/api/v1/projects/myproj", headers=headers)
    assert get_response.status_code == 200
    assert get_response.json()["tracked"] is True


def test_archive_rolls_back_with_real_load_config_cache(
    workspace: Path,
    project_root: Path,
    config_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex round 4 mirror: archive's rollback on the cached path.

    The ``archive_project`` service helper delegates to
    ``pollypm.projects.remove_project``, which does
    ``config = load_config(config_path); del config.projects[key];
    write_config(...)``. On the cached path the ``del`` mutates the live
    snapshot BEFORE the write — so a failed write would leave the live
    config missing the project even though disk still has it. The
    service-level fix snapshots the live entry and restores it on
    OSError.
    """
    from pollypm.config import (
        AccountConfig,
        MemorySettings,
        PollyPMConfig,
        PollyPMSettings,
        ProjectSettings,
        load_config,
        write_config,
    )

    base_dir = workspace / ".pollypm"
    seed = PollyPMConfig(
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
    write_config(seed, config_path, force=True)

    live = load_config(config_path)
    assert "myproj" in live.projects

    token_path = tmp_path / "api-token"
    value, _ = ensure_token(token_path)
    app = create_app(config=live, token_path=token_path)
    client_ = TestClient(app)
    headers = {"Authorization": f"Bearer {value}"}

    def _fail_write_config(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError("simulated disk-full during config persist")

    # Archive routes through ``pollypm.projects.remove_project`` which
    # does ``from pollypm.config import write_config`` at module load,
    # so the patched name MUST be the one bound inside
    # ``pollypm.projects`` — patching ``pollypm.config.write_config``
    # alone leaves the facade's binding pointing at the real impl.
    monkeypatch.setattr("pollypm.projects.write_config", _fail_write_config)

    response = client_.post(
        "/api/v1/projects/myproj/archive",
        headers=headers,
        json={"reason": "round-4 archive rollback"},
    )
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "service_unavailable"

    # Live snapshot MUST still hold ``myproj`` — pre-fix it was del'd
    # by the facade before the write failed.
    assert "myproj" in live.projects, (
        "Live ConfigDep lost ``myproj`` on a failed archive write — "
        "Codex round-4 regression (cached-load_config path: facade's "
        "in-place del leaked into the live snapshot)."
    )

    # And a subsequent GET still surfaces the project.
    get_response = client_.get("/api/v1/projects/myproj", headers=headers)
    assert get_response.status_code == 200
    assert get_response.json()["key"] == "myproj"


# ---------------------------------------------------------------------------
# Codex round 6 on PR #2063 — lost-update race across concurrent API writers.
#
# Two clients pausing different projects concurrently each deepcopy the same
# cached load_config snapshot, mutate their own project, then both write with
# ``force=True``. The second writer's snapshot still has the first writer's
# project at the OLD value — so the first write is silently reverted on disk.
# Per-request reloads (post-#2056) don't help because the race is between two
# requests, both of which reload at the same moment before either writes.
#
# Fix: ``set_project_tracked`` and ``archive_project`` hold an exclusive
# ``fcntl.flock`` on a sibling lockfile across the entire load → mutate →
# write → reload sequence. Two writers serialise at the lock; the loser
# reloads AFTER the winner's mtime updates and merges the winner's mutation
# forward.
#
# These tests call the service helpers directly with a ``threading.Barrier``
# so the race window is real — exercising via TestClient would funnel through
# Starlette's threadpool and dilute the reproduction. Pattern borrowed from
# ``tests/test_pg_notifications_race.py``.
# ---------------------------------------------------------------------------


def test_concurrent_pause_on_different_projects_preserves_both(
    workspace: Path,
    project_root: Path,
    config_path: Path,
    tmp_path: Path,
) -> None:
    """Codex round 6: two concurrent pauses on DIFFERENT projects.

    Without ``_config_write_lock`` both threads load the same cached
    snapshot, deepcopy, flip their own ``tracked``, and write — the
    second writer's snapshot still has the first writer's project at
    ``tracked=True``, so the second write silently reverts the first.
    Under the lock the loser reloads AFTER the winner's write commits;
    final TOML reflects BOTH ``a.tracked=False`` and ``b.tracked=False``.
    """
    import threading

    from pollypm.web_api.service import set_project_tracked

    project_a_root = tmp_path / "project_a"
    project_a_root.mkdir()
    (project_a_root / ".pollypm").mkdir()
    project_b_root = tmp_path / "project_b"
    project_b_root.mkdir()
    (project_b_root / ".pollypm").mkdir()

    base_dir = workspace / ".pollypm"
    seed = PollyPMConfig(
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
            "project_a": KnownProject(
                key="project_a",
                path=project_a_root,
                name="Project A",
                tracked=True,
                kind=ProjectKind.GIT,
            ),
            "project_b": KnownProject(
                key="project_b",
                path=project_b_root,
                name="Project B",
                tracked=True,
                kind=ProjectKind.GIT,
            ),
        },
        memory=MemorySettings(backend="file"),
        config_path=config_path,
    )
    write_config(seed, config_path, force=True)

    # Two distinct live snapshots — one per "request". In production
    # post-#2056 each request gets its own per-request reload, but
    # load_config caches on mtime so both still resolve to the same
    # cached object. The lock has to serialise the RMW regardless.
    live_a = load_config(config_path)
    live_b = load_config(config_path)

    barrier = threading.Barrier(2)
    errors: list[BaseException | None] = [None, None]

    def _worker(slot: int, live: PollyPMConfig, key: str) -> None:
        try:
            barrier.wait(timeout=10)
            set_project_tracked(
                live,
                key,
                tracked=False,
                reason=f"round-6 concurrent {key}",
                actor="api",
            )
        except BaseException as exc:  # noqa: BLE001
            errors[slot] = exc

    t1 = threading.Thread(target=_worker, args=(0, live_a, "project_a"))
    t2 = threading.Thread(target=_worker, args=(1, live_b, "project_b"))
    t1.start()
    t2.start()
    t1.join(timeout=20)
    t2.join(timeout=20)

    assert errors == [None, None], f"workers raised: {errors!r}"

    # BOTH mutations must survive on disk — the round-6 invariant.
    final = load_config(config_path)
    assert final.projects["project_a"].tracked is False, (
        "project_a's pause was reverted by project_b's write — Codex "
        "round-6 lost-update race regression. Lock missing on the "
        "config RMW path."
    )
    assert final.projects["project_b"].tracked is False, (
        "project_b's pause was reverted by project_a's write — Codex "
        "round-6 lost-update race regression. Lock missing on the "
        "config RMW path."
    )


def test_concurrent_archive_and_pause_preserves_both(
    workspace: Path,
    project_root: Path,
    config_path: Path,
    tmp_path: Path,
) -> None:
    """Codex round 6 mirror: concurrent archive + pause on different keys.

    Archive of ``project_a`` and pause of ``project_b`` must both
    survive. Without the lock the loser reverts the winner's mutation
    (either the archive resurrects, or the pause is reverted to
    tracked=True). Under the lock both land durably.
    """
    import threading

    from pollypm.web_api.service import archive_project, set_project_tracked

    project_a_root = tmp_path / "project_a"
    project_a_root.mkdir()
    (project_a_root / ".pollypm").mkdir()
    project_b_root = tmp_path / "project_b"
    project_b_root.mkdir()
    (project_b_root / ".pollypm").mkdir()

    base_dir = workspace / ".pollypm"
    seed = PollyPMConfig(
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
            "project_a": KnownProject(
                key="project_a",
                path=project_a_root,
                name="Project A",
                tracked=True,
                kind=ProjectKind.GIT,
            ),
            "project_b": KnownProject(
                key="project_b",
                path=project_b_root,
                name="Project B",
                tracked=True,
                kind=ProjectKind.GIT,
            ),
        },
        memory=MemorySettings(backend="file"),
        config_path=config_path,
    )
    write_config(seed, config_path, force=True)

    live_a = load_config(config_path)
    live_b = load_config(config_path)

    barrier = threading.Barrier(2)
    errors: list[BaseException | None] = [None, None]

    def _archive_worker() -> None:
        try:
            barrier.wait(timeout=10)
            archive_project(
                live_a,
                "project_a",
                reason="round-6 concurrent archive",
                actor="api",
            )
        except BaseException as exc:  # noqa: BLE001
            errors[0] = exc

    def _pause_worker() -> None:
        try:
            barrier.wait(timeout=10)
            set_project_tracked(
                live_b,
                "project_b",
                tracked=False,
                reason="round-6 concurrent pause",
                actor="api",
            )
        except BaseException as exc:  # noqa: BLE001
            errors[1] = exc

    t1 = threading.Thread(target=_archive_worker)
    t2 = threading.Thread(target=_pause_worker)
    t1.start()
    t2.start()
    t1.join(timeout=20)
    t2.join(timeout=20)

    assert errors == [None, None], f"workers raised: {errors!r}"

    # BOTH mutations must survive on disk.
    final = load_config(config_path)
    assert "project_a" not in final.projects, (
        "project_a's archive was reverted by project_b's pause — Codex "
        "round-6 lost-update race regression on the archive path."
    )
    assert "project_b" in final.projects, (
        "project_b was clobbered by the archive write — concurrent-edit "
        "preservation regression."
    )
    assert final.projects["project_b"].tracked is False, (
        "project_b's pause was reverted by project_a's archive — Codex "
        "round-6 lost-update race regression."
    )


# ---------------------------------------------------------------------------
# Codex round 7 on PR #2063 — shared config_rmw_lock invariant across API
# and non-API writers.
#
# Round 6's API-local ``_config_write_lock`` closed the API-vs-API
# lost-update window, but a CLI / cockpit writer that does its own
# ``load_config → mutate → write_config`` outside the lock still raced the
# API path: a CLI ``pm projects remove`` (or cockpit role-assignment edit)
# that landed between an API helper's ``load_config`` and ``write_config``
# was silently overwritten by the API's older snapshot. Codex reproduced
# this by monkey-patching ``pollypm.config.write_config`` to flip a
# project's ``persona_name`` immediately before the API pause wrote its
# snapshot for a different project — the final TOML had the API mutation
# but the external persona_name edit was lost.
#
# Fix: ``pollypm.config.config_rmw_lock`` is the shared lock primitive.
# Every in-tree config writer wraps its full RMW under it: API helpers
# (``set_project_tracked`` / ``archive_project``), CLI helpers
# (``pollypm.projects.remove_project`` / ``register_project`` /
# ``rename_project`` / ``enable_tracked_project`` / ``set_workspace_root``),
# accounts (``add_account_via_login`` / ``remove_account`` /
# ``set_controller_account`` / ``set_open_permissions_default`` /
# ``toggle_failover_account`` / ``relogin_account``), workers, onboarding,
# the cockpit project-settings + roles editors, and the project-planning
# plugin session-purge. ``write_config`` ALSO acquires the lock for defence
# in depth (re-entrant per thread so nested wraps don't deadlock).
#
# These tests pin the cross-writer invariant: a CLI writer running
# concurrently with the API on the same config must preserve both
# mutations.
# ---------------------------------------------------------------------------


def test_concurrent_api_pause_and_cli_remove_preserves_both(
    workspace: Path,
    project_root: Path,
    config_path: Path,
    tmp_path: Path,
) -> None:
    """Codex round 7: API pause + CLI remove on different projects.

    Real reproduction: thread 1 calls the API ``set_project_tracked``
    helper to pause ``project_a``; thread 2 calls
    ``pollypm.projects.remove_project`` (the same code path the CLI
    ``pm projects remove`` uses) to drop ``project_c``. Both threads
    sync via a ``threading.Barrier`` so they race the RMW.

    Pre-fix (round-6 lock local to web_api): the CLI helper had no
    lock, so its ``load_config`` happened concurrently with the API's,
    its mutation ran outside the API's serialised window, and either
    the API's write reverted the CLI remove (``project_c`` resurrects)
    or the CLI's write reverted the API pause (``project_a.tracked``
    stays True). Post-fix (round 7): both writers acquire
    ``pollypm.config.config_rmw_lock``, so the loser reloads AFTER the
    winner's mtime updates and BOTH mutations land on disk.
    """
    import threading

    from pollypm.projects import remove_project
    from pollypm.web_api.service import set_project_tracked

    project_a_root = tmp_path / "project_a"
    project_a_root.mkdir()
    (project_a_root / ".pollypm").mkdir()
    project_c_root = tmp_path / "project_c"
    project_c_root.mkdir()
    (project_c_root / ".pollypm").mkdir()

    base_dir = workspace / ".pollypm"
    seed = PollyPMConfig(
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
            "project_a": KnownProject(
                key="project_a",
                path=project_a_root,
                name="Project A",
                tracked=True,
                kind=ProjectKind.GIT,
            ),
            "project_c": KnownProject(
                key="project_c",
                path=project_c_root,
                name="Project C",
                tracked=True,
                kind=ProjectKind.GIT,
            ),
        },
        memory=MemorySettings(backend="file"),
        config_path=config_path,
    )
    write_config(seed, config_path, force=True)

    live_a = load_config(config_path)

    barrier = threading.Barrier(2)
    errors: list[BaseException | None] = [None, None]

    def _api_pause_worker() -> None:
        try:
            barrier.wait(timeout=10)
            set_project_tracked(
                live_a,
                "project_a",
                tracked=False,
                reason="round-7 cross-writer pause",
                actor="api",
            )
        except BaseException as exc:  # noqa: BLE001
            errors[0] = exc

    def _cli_remove_worker() -> None:
        try:
            barrier.wait(timeout=10)
            # CLI path — does its own load_config + mutate +
            # write_config under the shared RMW lock.
            remove_project(config_path, "project_c")
        except BaseException as exc:  # noqa: BLE001
            errors[1] = exc

    t1 = threading.Thread(target=_api_pause_worker)
    t2 = threading.Thread(target=_cli_remove_worker)
    t1.start()
    t2.start()
    t1.join(timeout=20)
    t2.join(timeout=20)

    assert errors == [None, None], f"workers raised: {errors!r}"

    # BOTH mutations must survive on disk — the round-7 invariant.
    final = load_config(config_path)
    assert "project_a" in final.projects, (
        "project_a vanished — CLI remove clobbered the API pause's "
        "project list. Codex round-7 cross-writer invariant violated."
    )
    assert final.projects["project_a"].tracked is False, (
        "project_a's API pause was reverted by the CLI remove's write — "
        "Codex round-7 lost-update across API + CLI writers."
    )
    assert "project_c" not in final.projects, (
        "project_c was resurrected by the API pause's write — Codex "
        "round-7 lost-update across API + CLI writers."
    )


def test_config_rmw_lock_is_reentrant_within_thread(
    workspace: Path,
    project_root: Path,
    config_path: Path,
) -> None:
    """Codex round 7: ``config_rmw_lock`` must be re-entrant per thread.

    ``write_config`` ALSO acquires ``config_rmw_lock`` (defence in depth
    for stray callers that forget to wrap the full RMW). Without per-
    thread re-entrancy, the API helpers — which wrap the full RMW and
    THEN call ``write_config`` inside that block — would deadlock the
    moment ``write_config`` tries to acquire the lock its caller already
    holds.

    Pin the invariant: nested ``with config_rmw_lock(path):`` inside the
    same thread completes without deadlock AND a ``write_config`` call
    inside the outer wrap also returns. A regression in the depth
    counter would hang this test until the pytest timeout fires.
    """
    from pollypm.config import config_rmw_lock

    base_dir = workspace / ".pollypm"
    seed = PollyPMConfig(
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
    write_config(seed, config_path, force=True)

    with config_rmw_lock(config_path):
        with config_rmw_lock(config_path):
            # write_config takes the lock internally; this is the
            # API-helper pattern in production.
            fresh = load_config(config_path)
            fresh.projects["myproj"].tracked = False
            write_config(fresh, config_path, force=True)

    # Sanity: the write actually landed on disk.
    reloaded = load_config(config_path)
    assert reloaded.projects["myproj"].tracked is False


# ---------------------------------------------------------------------------
# Codex round 8 regressions: per-path re-entrancy + first-write race
# ---------------------------------------------------------------------------


def _hold_lock_child(lock_path_str: str, hold_seconds: float, ready_path_str: str) -> None:
    """Helper executed in a child process to hold an exclusive flock.

    Opens ``lock_path_str`` (must be the canonical sibling lockfile
    of some config path), acquires ``LOCK_EX``, touches
    ``ready_path_str`` so the parent knows the lock is held, then
    sleeps for ``hold_seconds`` before releasing.
    """
    import fcntl
    import time
    from pathlib import Path as _Path

    lock_path = _Path(lock_path_str)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        _Path(ready_path_str).write_text("ready", encoding="utf-8")
        time.sleep(hold_seconds)
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def test_config_rmw_lock_does_not_bypass_different_path(
    tmp_path: Path,
) -> None:
    """Codex round 8: nested acquire on a DIFFERENT path must take that path's flock.

    Round 7's depth counter was path-blind: once any config_rmw_lock
    was held by the thread, a nested acquire on a different config
    path skipped the flock entirely, letting another process hold the
    second path's lock while this thread was supposedly inside it.

    Repro: child process holds ``path_b``'s flock for HOLD_SECONDS.
    Parent thread enters ``config_rmw_lock(path_a)`` and then nests
    ``config_rmw_lock(path_b)``. The inner acquire MUST block until
    the child releases — i.e. elapsed wall-clock time inside the
    nested acquire must be close to HOLD_SECONDS, not ~0.
    """
    import multiprocessing
    import time

    from pollypm.config import _config_lock_path, config_rmw_lock

    path_a = tmp_path / "a" / ".pollypm" / "pollypm.toml"
    path_b = tmp_path / "b" / ".pollypm" / "pollypm.toml"
    path_a.parent.mkdir(parents=True, exist_ok=True)
    path_b.parent.mkdir(parents=True, exist_ok=True)

    ready_marker = tmp_path / "child_ready.flag"
    hold_seconds = 2.0
    lock_b_path = _config_lock_path(path_b)

    # Use spawn so the child does not inherit any thread-local lock
    # state from the parent process.
    ctx = multiprocessing.get_context("spawn")
    child = ctx.Process(
        target=_hold_lock_child,
        args=(str(lock_b_path), hold_seconds, str(ready_marker)),
    )
    child.start()
    try:
        # Wait until the child has actually acquired the flock.
        deadline = time.monotonic() + 10.0
        while not ready_marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready_marker.exists(), "child process did not signal ready"

        start = time.monotonic()
        with config_rmw_lock(path_a):
            with config_rmw_lock(path_b):
                inner_acquired_at = time.monotonic()
        elapsed = inner_acquired_at - start

        # The inner acquire must have BLOCKED on the child's flock.
        # Round 7's path-blind no-op returned in microseconds; the
        # per-path fix should wait for the child to release. Allow a
        # generous margin for scheduling jitter.
        assert elapsed >= hold_seconds * 0.5, (
            f"nested acquire on different path bypassed flock: "
            f"elapsed={elapsed:.3f}s, expected >= {hold_seconds * 0.5:.3f}s"
        )
    finally:
        child.join(timeout=10.0)
        if child.is_alive():  # pragma: no cover — safety net
            child.terminate()
            child.join(timeout=5.0)


def test_first_write_race_serialized_by_lock(
    workspace: Path,
    project_root: Path,
    config_path: Path,
) -> None:
    """Codex round 8: ``write_config(force=False)`` must serialise the existence check.

    Round 7 evaluated ``if path.exists() and not force`` BEFORE taking
    the lock. Two writers racing to initialise the same fresh config
    could both observe ``False``, both enter the lock sequentially, and
    both write — silently clobbering the first writer. With the
    existence check inside the lock, exactly one writer wins and the
    other gets ``FileExistsError``.
    """
    import threading

    base_dir = workspace / ".pollypm"

    def _make_seed(persona: str) -> PollyPMConfig:
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
                    name=f"My Project ({persona})",
                    tracked=True,
                    kind=ProjectKind.GIT,
                ),
            },
            memory=MemorySettings(backend="file"),
            config_path=config_path,
        )

    # Ensure no leftover config from any prior fixture wiring.
    if config_path.exists():
        config_path.unlink()

    barrier = threading.Barrier(2)
    results: list[tuple[str, BaseException | None]] = []
    results_lock = threading.Lock()

    def _writer(persona: str) -> None:
        seed = _make_seed(persona)
        barrier.wait()
        try:
            write_config(seed, config_path, force=False)
            with results_lock:
                results.append((persona, None))
        except BaseException as exc:  # noqa: BLE001 — captured for the assert
            with results_lock:
                results.append((persona, exc))

    t1 = threading.Thread(target=_writer, args=("alpha",), daemon=True)
    t2 = threading.Thread(target=_writer, args=("bravo",), daemon=True)
    t1.start()
    t2.start()
    t1.join(timeout=15.0)
    t2.join(timeout=15.0)
    assert not t1.is_alive() and not t2.is_alive(), "writers deadlocked"

    successes = [r for r in results if r[1] is None]
    failures = [r for r in results if r[1] is not None]

    assert len(successes) == 1, (
        f"expected exactly one writer to succeed, got "
        f"successes={[r[0] for r in successes]}, "
        f"failures={[(p, type(e).__name__) for p, e in failures]}"
    )
    assert len(failures) == 1, (
        f"expected exactly one writer to raise FileExistsError, got "
        f"failures={[(p, type(e).__name__, str(e)) for p, e in failures]}"
    )
    assert isinstance(failures[0][1], FileExistsError), (
        f"loser must raise FileExistsError, got {type(failures[0][1]).__name__}: "
        f"{failures[0][1]!r}"
    )
