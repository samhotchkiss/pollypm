"""Integration tests for the Phase 2 doctor endpoints.

Covers:

- ``GET  /api/v1/doctor/checks``
- ``GET  /api/v1/doctor/report``
- ``POST /api/v1/doctor/run``

Per ``~/Desktop/pollypm-phase2-endpoints-spec.md`` §7. Tests use the
FastAPI ``TestClient`` against an in-process app, stubbing the doctor
registry / runner so the suite never touches the user's real machine
(no subprocess spawns, no network probes). Run with
``pytest --noconftest tests/test_doctor_endpoint.py -v``.
"""

from __future__ import annotations

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
from pollypm.doctor import Check, CheckResult, DoctorReport
from pollypm.models import KnownProject, ProjectKind, ProviderKind, RuntimeKind
from pollypm.web_api import create_app, ensure_token
from pollypm.web_api.routes import doctor as doctor_routes


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


@pytest.fixture(autouse=True)
def reset_last_report() -> Any:
    """Clear the process-local last-report cache between tests."""
    doctor_routes._reset_last_report()
    yield
    doctor_routes._reset_last_report()


@pytest.fixture
def app(config, token):
    token_path, _value = token
    return create_app(config=config, token_path=token_path)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Stub helpers
# ---------------------------------------------------------------------------


def _ok(name: str) -> CheckResult:
    return CheckResult(passed=True, status=f"{name}: ok")


def _fail(name: str, *, severity: str = "error", fixable: bool = False) -> CheckResult:
    fix_fn = None
    if fixable:
        def fix_fn() -> tuple[bool, str]:
            return True, f"applied fix for {name}"
    return CheckResult(
        passed=False,
        status=f"{name}: failing",
        severity=severity,
        why=f"{name} is broken",
        fix=f"do the {name} thing",
        fixable=fixable,
        fix_fn=fix_fn,
    )


def _check(name: str, result: CheckResult, *, category: str = "test", severity: str = "error") -> Check:
    return Check(
        name=name,
        run=lambda r=result: r,
        category=category,
        severity=severity,
    )


@pytest.fixture
def patch_registry(monkeypatch: pytest.MonkeyPatch):
    """Install a stub for ``_registered_checks``.

    Returns a callable ``(checks_list)`` that swaps the registry both
    on the route module (where ``_select_checks`` calls it) and on the
    underlying ``pollypm.doctor`` module (where ``run_checks`` resolves
    its default selection).
    """

    def install(checks: list[Check]) -> None:
        monkeypatch.setattr(
            doctor_routes, "_registered_checks", lambda: list(checks),
        )
        # ``run_checks(selected)`` resolves an explicit list directly,
        # so we only need to patch ``_registered_checks`` on the doctor
        # module to keep the "no args" branch consistent — but we go
        # the extra step here for symmetry / future-proofing.
        from pollypm import doctor as doctor_module
        monkeypatch.setattr(
            doctor_module, "_registered_checks", lambda: list(checks),
        )

    return install


# ---------------------------------------------------------------------------
# GET /doctor/checks
# ---------------------------------------------------------------------------


def test_list_checks_happy_path(client, auth_headers, patch_registry):
    """List endpoint returns every registered check with its metadata."""
    patch_registry([
        _check("alpha", _ok("alpha"), category="system"),
        _check("beta", _ok("beta"), category="install", severity="warning"),
    ])
    response = client.get("/api/v1/doctor/checks", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert [c["name"] for c in body["checks"]] == ["alpha", "beta"]
    assert body["checks"][0]["category"] == "system"
    assert body["checks"][1]["severity"] == "warning"
    # Catalog rows declare ``has_auto_fix=False`` (real value comes
    # from a run); make sure the field is present.
    assert body["checks"][0]["has_auto_fix"] is False


def test_list_checks_requires_auth(client, patch_registry):
    """Missing bearer token → 401, not 200 with a public catalog."""
    patch_registry([_check("alpha", _ok("alpha"))])
    response = client.get("/api/v1/doctor/checks")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


# ---------------------------------------------------------------------------
# GET /doctor/report
# ---------------------------------------------------------------------------


def test_report_returns_404_when_no_run_yet(client, auth_headers, patch_registry):
    """Spec §7.1: report endpoint 404s when nothing's cached."""
    patch_registry([_check("alpha", _ok("alpha"))])
    response = client.get("/api/v1/doctor/report", headers=auth_headers)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_report_returns_last_run_after_post(client, auth_headers, patch_registry):
    """After a POST /run, GET /report returns the cached snapshot."""
    patch_registry([
        _check("alpha", _ok("alpha")),
        _check("beta", _fail("beta")),
    ])
    run = client.post("/api/v1/doctor/run", headers=auth_headers, json={})
    assert run.status_code == 200
    report = client.get("/api/v1/doctor/report", headers=auth_headers)
    assert report.status_code == 200
    body = report.json()
    assert {c["name"] for c in body["checks"]} == {"alpha", "beta"}
    assert body["errors"] == 1
    assert body["passed"] == 1
    assert body["ok"] is False


# ---------------------------------------------------------------------------
# POST /doctor/run
# ---------------------------------------------------------------------------


def test_run_all_checks_sync(client, auth_headers, patch_registry):
    """Empty body → run every registered check synchronously."""
    patch_registry([
        _check("alpha", _ok("alpha")),
        _check("beta", _ok("beta")),
        _check("gamma", _fail("gamma", severity="warning"), severity="warning"),
    ])
    response = client.post("/api/v1/doctor/run", headers=auth_headers, json={})
    assert response.status_code == 200
    body = response.json()
    assert [c["name"] for c in body["checks"]] == ["alpha", "beta", "gamma"]
    assert body["passed"] == 2
    assert body["errors"] == 0
    assert body["warnings"] == 1
    assert body["ok"] is True  # warnings don't break ``ok``
    assert body["fixes_applied"] == []


def test_run_single_check_by_name(client, auth_headers, patch_registry):
    """``check`` body field narrows the run to one named check."""
    patch_registry([
        _check("alpha", _ok("alpha")),
        _check("beta", _fail("beta")),
    ])
    response = client.post(
        "/api/v1/doctor/run",
        headers=auth_headers,
        json={"check": "beta"},
    )
    assert response.status_code == 200
    body = response.json()
    assert [c["name"] for c in body["checks"]] == ["beta"]
    assert body["errors"] == 1
    assert body["ok"] is False


def test_run_unknown_check_returns_404(client, auth_headers, patch_registry):
    """Unknown check name → 404 not_found, never silent success."""
    patch_registry([_check("alpha", _ok("alpha"))])
    response = client.post(
        "/api/v1/doctor/run",
        headers=auth_headers,
        json={"check": "does_not_exist"},
    )
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    # Hint should mention the discovery endpoint.
    assert "GET /api/v1/doctor/checks" in (body["error"].get("hint") or "")


def test_run_with_fix_invokes_fix_fn(client, auth_headers, patch_registry, monkeypatch):
    """``fix=true`` runs auto-fixes and verifies post-fix state.

    We stub the underlying check so the first run fails and the
    second (post-fix re-run) succeeds — this exercises the re-run
    branch in the route. The stub uses a counter so each invocation
    flips between the failing and passing result.
    """
    counter = {"n": 0}

    def stateful_run() -> CheckResult:
        counter["n"] += 1
        if counter["n"] == 1:
            return _fail("alpha", fixable=True)
        return _ok("alpha")

    check = Check(name="alpha", run=stateful_run, category="test", severity="error")
    patch_registry([check])
    response = client.post(
        "/api/v1/doctor/run",
        headers=auth_headers,
        json={"check": "alpha", "fix": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["fixes_applied"] == [
        {"name": "alpha", "ok": True, "message": "applied fix for alpha"},
    ]
    # Post-fix re-run replaced the failing result with a passing one.
    [row] = body["checks"]
    assert row["passed"] is True


def test_run_with_fix_false_does_not_invoke_fixes(client, auth_headers, patch_registry):
    """``fix=false`` returns the report without attempting fixes."""
    patch_registry([_check("alpha", _fail("alpha", fixable=True))])
    response = client.post(
        "/api/v1/doctor/run",
        headers=auth_headers,
        json={"fix": False},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["fixes_applied"] == []
    assert body["checks"][0]["passed"] is False
    assert body["checks"][0]["fixable"] is True


def test_run_504_when_check_exceeds_budget(
    client, auth_headers, patch_registry, monkeypatch,
):
    """A real hanging check surfaces 504 within the budget (P0 round-1).

    Earlier revisions of this test stubbed ``time.monotonic`` after
    ``run_checks`` returned — that only proved post-facto
    classification. The real contract is that the HTTP request worker
    is bounded: a check that blocks past the budget must produce 504
    on time. We sleep longer than the budget in a stubbed
    ``run_checks`` and assert (a) status code, (b) wall-clock
    response time stays within budget + small slack (the executor
    abandons the worker thread rather than waiting for it).
    """
    import time as _time

    patch_registry([_check("alpha", _ok("alpha"))])

    hang_event = __import__("threading").Event()

    def hanging_run_checks(checks):  # type: ignore[no-untyped-def]
        # Block well past the request budget. The route's executor
        # abandons this thread on timeout; ``hang_event`` lets the
        # test release it cleanly after assertions.
        hang_event.wait(timeout=30.0)
        return DoctorReport()

    monkeypatch.setattr(doctor_routes, "run_checks", hanging_run_checks)

    t0 = _time.monotonic()
    response = client.post(
        "/api/v1/doctor/run",
        headers=auth_headers,
        json={"check": "alpha"},
        params={"timeout_seconds": 1},
    )
    elapsed = _time.monotonic() - t0

    # Release the leaked worker thread so the suite shuts down cleanly.
    hang_event.set()

    assert response.status_code == 504
    body = response.json()
    assert body["error"]["code"] == "timeout"
    # The 504 must return within budget + slack (executor + HTTP + asserts).
    # If the request waited for the hanging thread, this would be ~30s.
    assert elapsed < 5.0, f"504 took {elapsed:.1f}s; should be near 1s budget"


def test_apply_fixes_hang_returns_504_within_budget(
    client, auth_headers, patch_registry, monkeypatch,
):
    """A hanging ``apply_fixes`` must surface 504 within budget (P0 round-3).

    Before this fix, only ``run_checks`` and the post-fix verify rerun
    were dispatched through ``_run_with_budget``; ``apply_fixes`` was
    called inline, so a hung fix (stuck subprocess, blocked filesystem,
    jammed worktree git op) would hold the request worker indefinitely
    even though the endpoint documents the whole ``run + fix + verify``
    sequence as bounded by ``timeout_seconds``.

    This test simulates that hang: ``run_checks`` returns immediately
    with a fixable failure, ``apply_fixes`` sleeps well past the
    request budget. The endpoint must return 504 within the budget +
    small slack, not block on the sleeping fix. Without the round-3
    fix this test sees a ~60s wait; with it, ~1s.
    """
    import time as _time

    patch_registry([_check("alpha", _fail("alpha", fixable=True))])

    hang_event = __import__("threading").Event()

    def hanging_apply_fixes(report):  # type: ignore[no-untyped-def]
        # Block well past the request budget. The shared executor
        # abandons this thread on timeout; ``hang_event`` lets the
        # test release it cleanly after assertions so the daemon
        # thread doesn't linger across the suite.
        hang_event.wait(timeout=60.0)
        return []

    monkeypatch.setattr(doctor_routes, "apply_fixes", hanging_apply_fixes)

    t0 = _time.monotonic()
    response = client.post(
        "/api/v1/doctor/run",
        headers=auth_headers,
        json={"check": "alpha", "fix": True},
        params={"timeout_seconds": 2},
    )
    elapsed = _time.monotonic() - t0

    # Release the leaked worker thread so the executor slot recovers
    # before the next test runs.
    hang_event.set()

    assert response.status_code == 504, (
        f"expected 504 within budget, got {response.status_code}: {response.json()}"
    )
    body = response.json()
    assert body["error"]["code"] == "timeout"
    # If apply_fixes wasn't bounded, this elapsed would be ~60s. Allow
    # a few seconds of slack for executor dispatch + TestClient.
    assert elapsed < 5.0, (
        f"504 took {elapsed:.1f}s; apply_fixes must be wrapped in the same budget"
    )


def test_run_fix_serialized_under_concurrency(
    client, auth_headers, patch_registry, monkeypatch,
):
    """Two concurrent ``fix=true`` calls: one succeeds, one returns 409.

    Single-flight is enforced via ``_FIX_OPERATION_LOCK`` on the
    route module. We hold ``apply_fixes`` long enough for the second
    request to race in, then assert exactly one 200 and one 409
    ``conflict`` (Codex round-1 P0 on PR #2058).
    """
    import threading

    counter = {"n": 0}

    def stateful_run() -> CheckResult:
        counter["n"] += 1
        return _fail("alpha", fixable=True) if counter["n"] % 2 == 1 else _ok("alpha")

    check = Check(name="alpha", run=stateful_run, category="test", severity="error")
    patch_registry([check])

    # Block ``apply_fixes`` so the first request holds the operation
    # lock long enough for the second one to race in and 409.
    release = threading.Event()
    in_flight = threading.Event()
    real_apply = doctor_routes.apply_fixes

    def slow_apply_fixes(report):  # type: ignore[no-untyped-def]
        in_flight.set()
        release.wait(timeout=10.0)
        return real_apply(report)

    monkeypatch.setattr(doctor_routes, "apply_fixes", slow_apply_fixes)

    results: list[int] = []
    bodies: list[dict[str, Any]] = []

    def fire():
        resp = client.post(
            "/api/v1/doctor/run",
            headers=auth_headers,
            json={"check": "alpha", "fix": True},
        )
        results.append(resp.status_code)
        bodies.append(resp.json())

    t1 = threading.Thread(target=fire, daemon=True)
    t1.start()
    # Wait for the first request to enter ``apply_fixes`` so the
    # operation lock is definitely held when the second fires.
    assert in_flight.wait(timeout=5.0), "first request never entered apply_fixes"

    t2 = threading.Thread(target=fire, daemon=True)
    t2.start()
    t2.join(timeout=5.0)

    # Release the first request and wait for it to finish.
    release.set()
    t1.join(timeout=10.0)

    assert sorted(results) == [200, 409], f"expected one 200 + one 409, got {results}"
    busy_body = next(b for b, code in zip(bodies, results) if code == 409)
    assert busy_body["error"]["code"] == "conflict"


def test_run_rejected_for_non_default_config(
    auth_headers, token, patch_registry, workspace, project_root, tmp_path,
):
    """``pm serve --config /tmp/foo`` → doctor refuses with 400.

    Doctor checks load ``DEFAULT_CONFIG_PATH`` internally; a
    non-default config would cause the route to report on / mutate
    the wrong workspace (Codex round-1 P0 on PR #2058). The reject
    path covers all three endpoints — assert on POST /run as the
    write surface that matters most.
    """
    from pollypm.config import (
        AccountConfig,
        MemorySettings,
        PollyPMConfig,
        PollyPMSettings,
        ProjectSettings,
    )
    from pollypm.models import KnownProject, ProjectKind, ProviderKind, RuntimeKind

    base_dir = workspace / ".pollypm"
    fake_path = tmp_path / "custom" / "pollypm.toml"
    fake_path.parent.mkdir()
    fake_path.write_text("# fake non-default config\n")

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
    )
    cfg.config_path = fake_path  # non-default

    token_path, _value = token
    app = create_app(config=cfg, token_path=token_path)
    custom_client = TestClient(app)

    patch_registry([_check("alpha", _ok("alpha"))])

    resp = custom_client.post(
        "/api/v1/doctor/run",
        headers=auth_headers,
        json={"check": "alpha", "fix": True},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "invalid_request"
    assert "default config" in body["error"]["message"].lower()

    # Also covers GET endpoints.
    resp_checks = custom_client.get("/api/v1/doctor/checks", headers=auth_headers)
    assert resp_checks.status_code == 400


def test_run_requires_auth(client, patch_registry):
    """POST /run without bearer → 401."""
    patch_registry([_check("alpha", _ok("alpha"))])
    response = client.post("/api/v1/doctor/run", json={})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_run_with_invalid_token_returns_401(client, patch_registry):
    """Wrong token → 401 invalid_token, not 500."""
    patch_registry([_check("alpha", _ok("alpha"))])
    response = client.post(
        "/api/v1/doctor/run",
        headers={"Authorization": "Bearer wrong"},
        json={},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


def test_run_records_last_report_for_subsequent_get(
    client, auth_headers, patch_registry,
):
    """Two POST /run calls overwrite the cache (latest wins)."""
    patch_registry([_check("alpha", _ok("alpha"))])
    first = client.post("/api/v1/doctor/run", headers=auth_headers, json={})
    assert first.status_code == 200
    # Swap the registry; second run should overwrite the cached report.
    patch_registry([_check("beta", _fail("beta"))])
    second = client.post("/api/v1/doctor/run", headers=auth_headers, json={})
    assert second.status_code == 200
    cached = client.get("/api/v1/doctor/report", headers=auth_headers)
    assert cached.status_code == 200
    body = cached.json()
    assert [c["name"] for c in body["checks"]] == ["beta"]
    assert body["errors"] == 1


def test_run_with_no_body_defaults_to_run_all(client, auth_headers, patch_registry):
    """POST with no JSON body still runs every check (body is optional)."""
    patch_registry([
        _check("alpha", _ok("alpha")),
        _check("beta", _ok("beta")),
    ])
    response = client.post("/api/v1/doctor/run", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert [c["name"] for c in body["checks"]] == ["alpha", "beta"]
    assert body["passed"] == 2
