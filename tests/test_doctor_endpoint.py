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
from typing import Any, Iterator

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
    """No-op shim — the last-report cache is per-app, owned by lifespan.

    The ``client`` fixture below builds a fresh app each test whose
    lifespan starts with ``app.state.doctor_last_report = None``, so
    cross-test bleed is impossible. Kept as an empty autouse fixture so
    the signature stays stable for any test that explicitly references
    it (Codex round-5 on PR #2058).
    """
    yield


@pytest.fixture
def app(config, token):
    token_path, _value = token
    return create_app(config=config, token_path=token_path)


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    """TestClient wired through ``with`` so the FastAPI lifespan fires.

    Lifespan owns the doctor ThreadPoolExecutor + single-flight fix
    lock + last-report cache on ``app.state`` (Codex round-5 on
    #2058). Without the ``with`` block, ``TestClient`` skips
    startup/shutdown and the run endpoint would 503 on the missing
    executor.
    """
    with TestClient(app) as test_client:
        yield test_client


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


def test_fix_retry_after_504_returns_busy_while_first_still_running(
    client, app, auth_headers, patch_registry, monkeypatch,
):
    """After a 504 from a hung fix, an immediate retry must 409, not start a second fix.

    Codex round-4 (PR #2058) flagged: the prior single-flight lock was
    released in a ``finally`` block, which runs when the HTTP request
    times out and raises 504 — even though the worker thread is still
    inside ``apply_fixes`` mutating shared state. A retry within seconds
    of the 504 would acquire the freshly-released lock and start a
    *second* concurrent fix on the same filesystem/session/storage.

    Contract: the lock must be held until the worker thread truly
    terminates. The fix uses ``Future.add_done_callback`` to release the
    lock when the worker completes; the request's 504 path does not
    release it.

    This test:
      1. Stubs ``apply_fixes`` to block on an event well past the request
         budget so the first POST times out with 504.
      2. Asserts the first request returns 504 within budget + slack.
      3. Fires a second POST while the worker is still blocked, asserts
         it returns 409 ``conflict`` (NOT 504 from a second hang, which
         is what round-3 would have produced).
      4. Releases the worker so the suite shuts down cleanly.
    """
    import threading
    import time as _time

    patch_registry([_check("alpha", _fail("alpha", fixable=True))])

    release = threading.Event()
    in_flight = threading.Event()

    def hanging_apply_fixes(report):  # type: ignore[no-untyped-def]
        in_flight.set()
        # Block well past the request budget; the executor abandons this
        # thread on timeout, but the lock-release callback only fires
        # when ``release`` is set (after assertions).
        release.wait(timeout=30.0)
        return []

    monkeypatch.setattr(doctor_routes, "apply_fixes", hanging_apply_fixes)

    # First request: hangs in apply_fixes past the 1s budget → 504.
    t0 = _time.monotonic()
    first = client.post(
        "/api/v1/doctor/run",
        headers=auth_headers,
        json={"check": "alpha", "fix": True},
        params={"timeout_seconds": 1},
    )
    elapsed = _time.monotonic() - t0

    assert first.status_code == 504, (
        f"expected first request to time out, got {first.status_code}: {first.json()}"
    )
    assert elapsed < 5.0, f"504 took {elapsed:.1f}s; should be near 1s budget"

    # Confirm the worker is still inside ``apply_fixes`` — the
    # in_flight event was set when we entered, and we haven't released
    # yet. This is the racy window the fix protects.
    assert in_flight.is_set(), "test setup bug: worker never entered apply_fixes"

    # Second request fired immediately: the worker is still alive
    # holding shared state, so the route MUST refuse with 409 busy
    # rather than start a second concurrent fix (which would either
    # interleave mutations or — pre-fix — also hang for another 1s and
    # return a second 504).
    second = client.post(
        "/api/v1/doctor/run",
        headers=auth_headers,
        json={"check": "alpha", "fix": True},
        params={"timeout_seconds": 1},
    )

    # Release the leaked worker so the lock-release callback fires
    # before the next test runs.
    release.set()

    assert second.status_code == 409, (
        f"expected 409 busy while first worker still running, "
        f"got {second.status_code}: {second.json()}. "
        "This is the round-4 bug: lock was released on 504, allowing "
        "a second concurrent fix while the first worker was still alive."
    )
    body = second.json()
    assert body["error"]["code"] == "conflict"
    assert "in progress" in body["error"]["message"].lower()

    # Wait for the worker's done callback to release the per-app lock
    # before the next test runs (otherwise we leak across the suite).
    # The lock lives on ``app.state.doctor_fix_lock`` (Codex round-5 on
    # PR #2058) — pre-round-5 this was a module-level global.
    fix_lock = app.state.doctor_fix_lock
    deadline = _time.monotonic() + 5.0
    while _time.monotonic() < deadline:
        if fix_lock.acquire(blocking=False):
            fix_lock.release()
            break
        _time.sleep(0.05)
    else:
        pytest.fail("fix lock not released by done callback within 5s")


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

    patch_registry([_check("alpha", _ok("alpha"))])

    # ``with`` so the FastAPI lifespan attaches the doctor executor /
    # locks (Codex round-5 on PR #2058) — otherwise the run endpoint
    # would 503 on the missing executor, masking the 400 we're
    # asserting.
    with TestClient(app) as custom_client:
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
        resp_checks = custom_client.get(
            "/api/v1/doctor/checks", headers=auth_headers,
        )
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


# ---------------------------------------------------------------------------
# Codex round-5 regressions (refs #2058) — app-scoped doctor executor
# ---------------------------------------------------------------------------


def test_doctor_executor_app_scoped(config, token) -> None:
    """Two ``create_app`` instances get independent doctor executors / locks.

    Pins the module-globals → ``app.state`` migration: the executor,
    single-flight fix lock, and last-report cache must be per-app so
    one ``pm serve`` invocation can't share a wedged worker slot (or
    deadlock the fix lock) with a sibling FastAPI app in the same
    process (tests, embedded uses). Previously the module-level
    ``_DOCTOR_EXECUTOR`` / ``_FIX_OPERATION_LOCK`` were shared across
    every app in the process — Codex round-5 on PR #2058.
    """
    token_path, _value = token
    app_a = create_app(config=config, token_path=token_path)
    app_b = create_app(config=config, token_path=token_path)

    with TestClient(app_a), TestClient(app_b):
        # Distinct executor + locks + cache slot per app.
        assert app_a.state.doctor_executor is not app_b.state.doctor_executor
        assert app_a.state.doctor_fix_lock is not app_b.state.doctor_fix_lock
        assert (
            app_a.state.doctor_last_report_lock
            is not app_b.state.doctor_last_report_lock
        )

        # Holding app_a's fix lock must not affect app_b's.
        assert app_a.state.doctor_fix_lock.acquire(blocking=False)
        try:
            assert app_b.state.doctor_fix_lock.acquire(blocking=False), (
                "app_b fix lock leaked from app_a — locks are not per-app"
            )
            app_b.state.doctor_fix_lock.release()
        finally:
            app_a.state.doctor_fix_lock.release()

        # Poking app_a's last-report slot must not leak into app_b.
        sentinel = object()
        app_a.state.doctor_last_report = sentinel  # type: ignore[assignment]
        assert app_b.state.doctor_last_report is None


def test_doctor_timed_out_worker_logged_on_late_completion(
    client, auth_headers, patch_registry, monkeypatch, caplog,
) -> None:
    """A 504 from a hung check still emits a log line when the worker finishes.

    Codex round-5 on PR #2058: pre-fix, a timed-out doctor worker was
    abandoned silently — the operator had no signal that the
    underlying check eventually returned (or raised). The done-callback
    on every submitted future now logs the late completion outcome.
    """
    import logging
    import threading
    import time as _time

    patch_registry([_check("alpha", _ok("alpha"))])

    release = threading.Event()
    in_flight = threading.Event()

    def hanging_run_checks(checks):  # type: ignore[no-untyped-def]
        in_flight.set()
        # Hang past the 1s budget; release lets the worker finish so
        # the done-callback fires before we assert on the log.
        release.wait(timeout=10.0)
        return DoctorReport()

    monkeypatch.setattr(doctor_routes, "run_checks", hanging_run_checks)

    with caplog.at_level(logging.INFO, logger="pollypm.web_api.routes.doctor"):
        t0 = _time.monotonic()
        response = client.post(
            "/api/v1/doctor/run",
            headers=auth_headers,
            json={"check": "alpha"},
            params={"timeout_seconds": 1},
        )
        elapsed = _time.monotonic() - t0
        assert response.status_code == 504, response.json()
        assert elapsed < 5.0, f"504 took {elapsed:.1f}s; should be near 1s budget"
        assert in_flight.is_set(), "worker never entered run_checks"

        # Release the worker; the done-callback should log the late
        # completion once the future resolves.
        release.set()

        # Wait up to 5s for the done-callback log line to land.
        deadline = _time.monotonic() + 5.0
        late_completion_msg = "check worker completed"
        while _time.monotonic() < deadline:
            if any(
                late_completion_msg in record.getMessage()
                for record in caplog.records
            ):
                break
            _time.sleep(0.05)
        else:
            pytest.fail(
                "expected a 'check worker completed' log line after "
                f"the timed-out worker finished. Records seen: "
                f"{[r.getMessage() for r in caplog.records]}"
            )


def test_doctor_app_teardown_returns_promptly_with_running_worker(
    config, token, auth_headers, patch_registry, monkeypatch,
) -> None:
    """App teardown completes promptly even with a wedged in-flight check.

    Codex round-5 on PR #2058: the prior module-level executor had no
    shutdown hook, so ``pm serve`` exit left worker threads alive past
    teardown with no log line. The lifespan now calls
    ``shutdown(wait=False, cancel_futures=True)`` and waits at most
    :data:`pollypm.web_api.app._DOCTOR_SHUTDOWN_GRACE_S` for cooperative
    drain — a hung worker is logged + leaked, not waited on.

    This test fires a slow check that we abandon via 504, then exits
    the lifespan and asserts the whole teardown completed in well
    under 10s even though the worker is still alive. The wedged
    thread is released afterwards so it doesn't linger across the
    suite.
    """
    import threading
    import time as _time

    token_path, _value = token
    app = create_app(config=config, token_path=token_path)

    patch_registry([_check("alpha", _ok("alpha"))])

    release = threading.Event()
    in_flight = threading.Event()

    def hanging_run_checks(checks):  # type: ignore[no-untyped-def]
        in_flight.set()
        # Hang well past the grace period; lifespan must NOT wait on us.
        release.wait(timeout=30.0)
        return DoctorReport()

    monkeypatch.setattr(doctor_routes, "run_checks", hanging_run_checks)

    teardown_t0: list[float] = []
    teardown_elapsed: list[float] = []

    with TestClient(app) as test_client:
        resp = test_client.post(
            "/api/v1/doctor/run",
            headers=auth_headers,
            json={"check": "alpha"},
            params={"timeout_seconds": 1},
        )
        assert resp.status_code == 504, resp.json()
        assert in_flight.is_set(), "worker never entered run_checks"
        teardown_t0.append(_time.monotonic())

    # Lifespan has exited.
    teardown_elapsed.append(_time.monotonic() - teardown_t0[0])

    # Release the leaked worker so it doesn't linger across the suite.
    release.set()

    # Should be ~5s (the documented grace), and definitely under 10s.
    # Pre-fix (no lifespan / no shutdown hook) the worker thread is
    # still alive but the app teardown returns instantly — what
    # actually changed is that we now LOG the leak and explicitly
    # cancel queued work. Verify the grace bound holds either way.
    assert teardown_elapsed[0] < 10.0, (
        f"app teardown took {teardown_elapsed[0]:.1f}s with a wedged "
        f"worker; must be bounded by the grace period."
    )

    # Executor should have been marked shut down by the lifespan.
    assert app.state.doctor_executor._shutdown is True, (
        "lifespan did not shut down the doctor executor"
    )
