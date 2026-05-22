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
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pollypm.config import (
    AccountConfig,
    MemorySettings,
    PluginSettings,
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


@pytest.fixture(autouse=True)
def _clear_briefing_inflight() -> "Iterator[None]":
    """Reset the module-level in-flight registry between tests.

    The regenerate endpoint dedupes concurrent runs by `(type, project)`
    using a module-level dict; a previous test that triggered a 504
    leaves the background thread running, which would 409 the next
    request for the same scope. We drop the entry on teardown so each
    test starts with a clean inflight table.
    """
    from pollypm.web_api.routes import briefings as br

    with br._INFLIGHT_LOCK:
        br._INFLIGHT.clear()
    yield
    with br._INFLIGHT_LOCK:
        br._INFLIGHT.clear()


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


# ---------------------------------------------------------------------------
# Codex round-1 regressions (refs #2059)
# ---------------------------------------------------------------------------


def test_briefings_uses_injected_config_not_default(
    api_config: PollyPMConfig,
    token_path: Path,
    token: str,  # noqa: ARG001 — fixture forces token write
    auth_headers: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_morning_regenerate` must honor ``config.config_path``, not the
    global default.

    Asserts the regression for Codex round-1 P0 #1: previously the
    adapter called ``load_briefing_settings(resolve_config_path(
    DEFAULT_CONFIG_PATH))`` even when ``pm serve --config /tmp/foo``
    had loaded a non-default file. We stub ``load_briefing_settings``
    to record its incoming path and assert it matches the injected
    config's ``config_path``.
    """
    from pollypm.web_api import create_app
    from pollypm.web_api.routes.briefings import _morning_regenerate

    # Construct a non-default TOML on disk so resolve_config_path can
    # return a real path. Contents don't matter — the stub intercepts.
    custom_toml = tmp_path / "custom_pollypm.toml"
    custom_toml.write_text("# custom config used by test\n")
    api_config.config_path = custom_toml

    captured_paths: list[Path] = []

    def _fake_load(path: Path):  # noqa: ANN202
        captured_paths.append(Path(path))
        # Return whatever default the real impl would; the regenerate
        # path past this point is mocked by replacing fire_briefing.
        from pollypm.plugins_builtin.morning_briefing.settings import (
            BriefingSettings,
        )
        return BriefingSettings()

    monkeypatch.setattr(
        "pollypm.plugins_builtin.morning_briefing.settings.load_briefing_settings",
        _fake_load,
    )
    # Short-circuit the heavy regenerate machinery — we only care that
    # ``load_briefing_settings`` was called with the injected path.
    monkeypatch.setattr(
        "pollypm.plugins_builtin.morning_briefing.handlers.briefing_tick.fire_briefing",
        lambda **_kw: {
            "fired": True,
            "emitted": False,
            "draft": {"date_local": "2026-05-22", "mode": "test", "markdown": "x"},
        },
    )
    # Stub state.load_state since we never wrote a real one.
    monkeypatch.setattr(
        "pollypm.plugins_builtin.morning_briefing.state.load_state",
        lambda _base: None,
    )

    # Build a fresh client that uses the real ``_morning_regenerate``
    # (no patched_registry fixture). The shared executor is module
    # level, so we don't need to recreate it.
    app = create_app(config=api_config, token_path=token_path)
    client = TestClient(app)

    # Mark the morning provider as "available" without the real plugin
    # tree. Patch only the availability probe — render is unused here.
    from pollypm.web_api.routes import briefings as br

    monkeypatch.setattr(br, "_morning_available", lambda _c: True)
    # The registry was built at import time; rebuild it with the new
    # availability callable so the endpoint trusts the stub.
    br._REGISTRY["morning"] = br._BriefingAdapter(
        name="morning",
        description="real-adapter test",
        available=lambda _c: True,
        render_last=lambda _c: None,
        regenerate=_morning_regenerate,
    )

    response = client.post(
        "/api/v1/briefings/morning/regenerate",
        json={},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.json()
    # The stub captured at least one path. Crucially it must equal the
    # *custom* path, not the default ``~/.pollypm/pollypm.toml``.
    assert captured_paths, "load_briefing_settings was not called"
    assert captured_paths[0] == custom_toml.resolve()


def test_briefings_regenerate_timeout_is_non_blocking(
    client: TestClient,
    auth_headers: dict[str, str],
    patched_registry: dict[str, _BriefingAdapter],  # noqa: ARG001
    fake_state: _FakeAdapterState,
) -> None:
    """504 must return near the timeout, not after the provider sleep.

    Codex round-1 P0 #3: the old code wrapped ``ThreadPoolExecutor`` in
    a ``with`` block, so context-manager exit blocked on
    ``shutdown(wait=True)``. A 60 s provider with a 1 s timeout still
    took ~60 s to respond. With the shared executor, the request
    should return in roughly ``timeout_seconds`` (plus a small
    bookkeeping budget).
    """
    fake_state.regen_delay = 60.0  # simulate a wedged provider
    start = time.monotonic()
    response = client.post(
        "/api/v1/briefings/morning/regenerate?timeout_seconds=1",
        json={"project": None},
        headers=auth_headers,
    )
    elapsed = time.monotonic() - start
    assert response.status_code == 504, response.json()
    # Give CI a generous ceiling but well under the 60 s sleep — if
    # the executor still blocks on shutdown, ``elapsed`` would be 60+.
    assert elapsed < 5.0, (
        f"timeout response took {elapsed:.2f}s; expected <5s "
        "(non-blocking executor regression)"
    )

    # Reset state so the lingering background thread doesn't keep the
    # in-flight registry full for the next test (it will eventually
    # clear itself, but be polite).
    fake_state.regen_delay = 0.0


def test_briefings_morning_rejects_project_param(
    api_config: PollyPMConfig,
    token_path: Path,
    token: str,  # noqa: ARG001
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``project`` on a ``morning`` regenerate is 400, not silently dropped.

    Codex round-1 P1: the request schema documented ``project`` as a
    scope narrower; the morning adapter ignored it and silently
    produced a workspace-wide briefing anyway. The new contract is
    "reject for morning, accept-and-honor for future plugin types".
    """
    from pollypm.web_api import create_app
    from pollypm.web_api.routes.briefings import _morning_regenerate
    from pollypm.web_api.routes import briefings as br

    api_config.config_path = tmp_path / "pollypm.toml"
    api_config.config_path.write_text("")
    monkeypatch.setattr(br, "_morning_available", lambda _c: True)
    br._REGISTRY["morning"] = br._BriefingAdapter(
        name="morning",
        description="real-adapter test",
        available=lambda _c: True,
        render_last=lambda _c: None,
        regenerate=_morning_regenerate,
    )
    app = create_app(config=api_config, token_path=token_path)
    client = TestClient(app)

    response = client.post(
        "/api/v1/briefings/morning/regenerate",
        json={"project": "myproj"},
        headers=auth_headers,
    )
    assert response.status_code == 400, response.json()
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    assert "morning" in body["error"]["message"].lower() or "project" in body[
        "error"
    ]["message"].lower()


# ---------------------------------------------------------------------------
# Codex round-2 regressions (refs #2059)
# ---------------------------------------------------------------------------


def test_briefings_endpoint_available_in_default_create_app(
    api_config: PollyPMConfig,
    token_path: Path,
    token: str,  # noqa: ARG001 — fixture forces token write
    auth_headers: dict[str, str],
) -> None:
    """`create_app` must wire the morning-briefing provider itself.

    Asserts the regression for Codex round-2 P0 #2: previously
    ``pm serve`` only called ``load_config`` + ``create_app`` and
    never bootstrapped the plugin host, so the morning provider stayed
    ``None`` in the registry. ``GET /api/v1/briefings`` returned
    ``morning.available=false`` and render/regenerate 503'd in
    production unless tests monkeypatched ``_REGISTRY`` /
    ``_morning_available``.

    No fixtures touch the registry here — the app factory itself must
    populate the provider for the built-in ``morning`` type.
    """
    # Clear the registry first so we prove ``create_app`` itself
    # repopulates it (rather than relying on stale module state from
    # an earlier test that imported the plugin tree).
    from pollypm.briefings_registry import register_briefing_provider
    register_briefing_provider(None)

    app = create_app(config=api_config, token_path=token_path)
    client = TestClient(app)

    response = client.get("/api/v1/briefings", headers=auth_headers)
    assert response.status_code == 200, response.json()
    morning = next(
        entry for entry in response.json()["types"] if entry["name"] == "morning"
    )
    assert morning["available"] is True, (
        "morning provider was not wired by create_app; "
        "render/regenerate would 503 in production"
    )


# ---------------------------------------------------------------------------
# Codex round-3 regressions (refs #2059)
# ---------------------------------------------------------------------------


def test_disabled_morning_plugin_reports_unavailable(
    api_config: PollyPMConfig,
    token_path: Path,
    token: str,  # noqa: ARG001 — fixture forces token write
    auth_headers: dict[str, str],
) -> None:
    """`create_app` must honor ``[plugins].disabled`` before wiring.

    Codex round-3 P0: round-2 wired the provider unconditionally, so a
    config with ``[plugins].disabled = ["morning_briefing"]`` still
    reported ``morning.available=true`` through the API — bypassing the
    plugin host's filter-before-register contract. The disablement is
    the operator rollback/recovery path; the API must not resurrect a
    disabled plugin.

    Asserts: with ``morning_briefing`` disabled in config,
    ``GET /api/v1/briefings`` reports ``morning.available=false`` and
    ``POST /api/v1/briefings/morning/regenerate`` returns 503
    (service_unavailable).
    """
    # Restore the canonical morning adapter — earlier tests in this
    # file mutate ``_REGISTRY["morning"]`` directly (without monkeypatch
    # revert), leaving a stub whose ``available`` always returns True.
    # We need the real ``_morning_available`` so this test exercises
    # the production availability path.
    from pollypm.web_api.routes import briefings as br
    from pollypm.web_api.routes.briefings import (
        _morning_available,
        _morning_regenerate,
        _morning_render_last,
    )

    br._REGISTRY["morning"] = br._BriefingAdapter(
        name="morning",
        description="Daily morning briefing — canonical adapter (test restore)",
        available=_morning_available,
        render_last=_morning_render_last,
        regenerate=_morning_regenerate,
    )

    # Pre-seed a provider to prove disabled-config app clears it
    # (the host's filter-before-register semantics — a previously
    # enabled run's stale registration must not leak through).
    from pollypm.briefings_registry import (
        is_briefing_provider_registered,
        register_briefing_provider,
    )

    def _stub_provider(_base, *, status="open", limit=None):  # noqa: ARG001
        return []

    register_briefing_provider(_stub_provider)
    assert is_briefing_provider_registered()

    api_config.plugins = PluginSettings(disabled=("morning_briefing",))

    app = create_app(config=api_config, token_path=token_path)
    client = TestClient(app)

    # GET /api/v1/briefings — morning.available must be False
    response = client.get("/api/v1/briefings", headers=auth_headers)
    assert response.status_code == 200, response.json()
    morning = next(
        entry for entry in response.json()["types"] if entry["name"] == "morning"
    )
    assert morning["available"] is False, (
        "morning provider was wired despite [plugins].disabled containing "
        "morning_briefing — API bypassed the plugin-disable contract"
    )

    # POST /api/v1/briefings/morning/regenerate — 503 service_unavailable
    response = client.post(
        "/api/v1/briefings/morning/regenerate",
        json={},
        headers=auth_headers,
    )
    assert response.status_code == 503, response.json()
    body = response.json()
    assert body["error"]["code"] == "service_unavailable"

    # Restore a clean registry for sibling tests that share module state.
    register_briefing_provider(None)
