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
