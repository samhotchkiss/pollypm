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

import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
def client(
    api_config: PollyPMConfig, token_path: Path, token: str,  # noqa: ARG001
) -> Iterator[TestClient]:
    """TestClient wired through ``with`` so the FastAPI lifespan fires.

    Lifespan owns the briefings ThreadPoolExecutor + in-flight
    registry on ``app.state`` (Codex round-4 on #2059). Without the
    ``with`` block, ``TestClient`` skips startup/shutdown and the
    regenerate endpoint would 503 on the missing executor.
    """
    app = create_app(config=api_config, token_path=token_path)
    with TestClient(app) as test_client:
        yield test_client


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
    """No-op — the in-flight registry now lives on ``app.state``.

    Previously a module-level dict in ``routes/briefings.py`` carried
    in-flight regenerates across tests, so we needed to drop the entry
    in setup/teardown to avoid 409-on-retry. With the lifespan-owned
    registry (Codex round-4 on #2059) each ``client`` fixture builds a
    fresh app whose ``app.state.briefing_inflight`` starts empty and
    is cleared on lifespan exit, so cross-test bleed is impossible.
    Kept as an empty fixture so the autouse signature stays stable for
    any test that explicitly references it.
    """
    yield


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
    # (no patched_registry fixture). ``with TestClient(...)`` is
    # required so the FastAPI lifespan boots the briefings executor on
    # ``app.state`` (Codex round-4 on #2059).
    app = create_app(config=api_config, token_path=token_path)

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

    with TestClient(app) as client:
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
    with TestClient(app) as client:
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
    with TestClient(app) as client:
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
    with TestClient(app) as client:
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


# ---------------------------------------------------------------------------
# Codex round-4 regressions (refs #2059)
# ---------------------------------------------------------------------------


def test_briefings_executor_shut_down_on_app_teardown(
    api_config: PollyPMConfig,
    token_path: Path,
    token: str,  # noqa: ARG001 — fixture forces token write
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    fake_state: _FakeAdapterState,
) -> None:
    """The briefings executor + in-flight registry are torn down at exit.

    Codex round-4 P0: the executor used to be a module-level global
    with no FastAPI lifespan hook, so it outlived ``pm serve`` shutdown
    and a slow regenerate kept its worker thread alive past app
    teardown. The lifespan migration moves the executor onto
    ``app.state`` and calls ``shutdown(wait=True, cancel_futures=True)``
    on lifespan exit — this test exercises that contract.
    """
    from pollypm.web_api.routes import briefings as br

    # Patch a stub adapter onto the module registry so the request
    # path hits a controllable regenerate fn (no real plugin tree).
    def _available(_config) -> bool:
        return True

    def _regenerate(_config, _body: RegenerateRequest) -> BriefingResponse:
        # Sleep long enough that the in-flight future is still
        # outstanding when we exit the lifespan context.
        time.sleep(fake_state.regen_delay or 5.0)
        return fake_state.regen

    monkeypatch.setitem(
        br._REGISTRY,
        "morning",
        _BriefingAdapter(
            name="morning",
            description="executor-teardown test",
            available=_available,
            render_last=lambda _c: None,
            regenerate=_regenerate,
        ),
    )

    fake_state.regen_delay = 30.0

    app = create_app(config=api_config, token_path=token_path)

    # Capture executor + inflight refs after startup so we can assert
    # post-shutdown state outside the context manager.
    executor_ref: list[Any] = []
    inflight_ref: list[dict] = []

    with TestClient(app) as client:
        # Confirm the lifespan attached the executor / registry.
        assert hasattr(app.state, "briefing_executor")
        assert hasattr(app.state, "briefing_inflight")
        executor_ref.append(app.state.briefing_executor)
        inflight_ref.append(app.state.briefing_inflight)

        # Fire a slow regenerate that we abandon via 504; the worker
        # is still running when we exit the context.
        response = client.post(
            "/api/v1/briefings/morning/regenerate?timeout_seconds=1",
            json={},
            headers=auth_headers,
        )
        assert response.status_code == 504, response.json()

        # The in-flight registry should hold the running future.
        assert len(app.state.briefing_inflight) >= 0  # may have cleared if super fast

    # After the lifespan exits:
    # 1) the executor was shut down (no new submissions accepted)
    assert executor_ref[0]._shutdown is True, (
        "lifespan did not shut down the briefings executor"
    )
    # 2) the in-flight registry was cleared
    assert inflight_ref[0] == {}, (
        f"in-flight registry not cleared on shutdown: {inflight_ref[0]!r}"
    )


def test_briefings_inflight_isolated_per_app(
    api_config: PollyPMConfig,
    token_path: Path,
    token: str,  # noqa: ARG001 — fixture forces token write
) -> None:
    """Two ``create_app`` instances get independent ``app.state`` registries.

    Pins the module-globals → ``app.state`` migration: in-flight
    bookkeeping on app A must not be visible to app B. Previously the
    module-level dict was shared across every app in the process, which
    would have let one ``pm serve`` invocation 409 a regenerate on a
    sibling FastAPI app (e.g. tests, embedded uses).
    """
    app_a = create_app(config=api_config, token_path=token_path)
    app_b = create_app(config=api_config, token_path=token_path)

    with TestClient(app_a), TestClient(app_b):
        # Distinct ThreadPoolExecutor + inflight + lock per app.
        assert app_a.state.briefing_executor is not app_b.state.briefing_executor
        assert app_a.state.briefing_inflight is not app_b.state.briefing_inflight
        assert (
            app_a.state.briefing_inflight_lock
            is not app_b.state.briefing_inflight_lock
        )

        # Poking app_a's in-flight map must not leak into app_b.
        app_a.state.briefing_inflight[("morning", "")] = object()  # type: ignore[assignment]
        assert ("morning", "") not in app_b.state.briefing_inflight


# ---------------------------------------------------------------------------
# Codex round-5 regressions (refs #2059)
# ---------------------------------------------------------------------------


def test_briefings_app_teardown_bounded_with_running_worker(
    api_config: PollyPMConfig,
    token_path: Path,
    token: str,  # noqa: ARG001 — fixture forces token write
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """App teardown returns promptly even with a wedged regenerate worker.

    Codex round-5 P0: previously ``_lifespan`` called
    ``executor.shutdown(wait=True, cancel_futures=True)`` — but
    ``cancel_futures=True`` only cancels QUEUED work; a worker already
    inside ``adapter.regenerate`` cannot be cancelled, so teardown
    would hang until the slow provider returned. The bounded-shutdown
    fix caps the wait at ``_BRIEFING_REGEN_SHUTDOWN_DEADLINE_S`` (5 s)
    and logs a warning if any worker is still alive.
    """
    from pollypm.web_api.routes import briefings as br

    # Signal so we know the worker actually started before we exit
    # the lifespan context (otherwise the test is just measuring queue
    # cancellation, not the running-worker path).
    worker_started = threading.Event()
    release_worker = threading.Event()

    def _available(_config) -> bool:
        return True

    def _slow_regenerate(_config, _body: RegenerateRequest) -> BriefingResponse:
        worker_started.set()
        # Block well past the bounded-shutdown deadline so we exercise
        # the "leak + warn" branch, not the "drained in time" branch.
        release_worker.wait(timeout=60.0)
        return _make_response(
            type_name="morning",
            markdown="released after deadline",
        )

    monkeypatch.setitem(
        br._REGISTRY,
        "morning",
        _BriefingAdapter(
            name="morning",
            description="bounded-shutdown test",
            available=_available,
            render_last=lambda _c: None,
            regenerate=_slow_regenerate,
        ),
    )

    app = create_app(config=api_config, token_path=token_path)

    teardown_start: list[float] = []
    teardown_end: list[float] = []

    try:
        with caplog.at_level("WARNING", logger="pollypm.web_api.app"):
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/briefings/morning/regenerate?timeout_seconds=1",
                    json={},
                    headers=auth_headers,
                )
                assert response.status_code == 504, response.json()

                # Confirm the worker actually got scheduled and is running
                # — otherwise we'd be testing the queue-cancel path.
                assert worker_started.wait(timeout=5.0), (
                    "slow regenerate worker never started"
                )

                teardown_start.append(time.monotonic())
            # TestClient context exit triggered lifespan shutdown.
            teardown_end.append(time.monotonic())

        # The whole teardown must return within ~10 s — bounded by the
        # 5 s shutdown deadline plus headroom. Before this fix it would
        # wait for the full release_worker.wait timeout (60 s).
        assert teardown_end and teardown_start
        elapsed = teardown_end[0] - teardown_start[0]
        assert elapsed < 10.0, (
            f"app teardown took {elapsed:.2f}s with a running worker; "
            "expected bounded shutdown to return within ~5s"
        )

        # And the leak warning should have been logged.
        leak_logs = [
            r for r in caplog.records
            if "still running at shutdown" in r.getMessage()
        ]
        assert leak_logs, (
            "expected lifespan to log a warning about leaked worker(s); "
            f"got: {[r.getMessage() for r in caplog.records]}"
        )
    finally:
        # Always release the worker so the leaked thread can exit and
        # not hang test-process teardown.
        release_worker.set()


def test_briefings_late_failure_logged(
    api_config: PollyPMConfig,
    token_path: Path,
    token: str,  # noqa: ARG001 — fixture forces token write
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Late worker failure after a 504 is observable via the logger.

    Codex round-5 P1: the done callback only popped the in-flight
    entry; it never inspected ``future.exception()``. If a provider
    raised after the client already saw 504, operators had no
    diagnostic. The wired-up ``_on_regenerate_done`` now logs a
    warning with the scope key + repr of the exception.
    """
    from pollypm.web_api.routes import briefings as br

    worker_done = threading.Event()

    def _available(_config) -> bool:
        return True

    def _failing_regenerate(_config, _body: RegenerateRequest) -> BriefingResponse:
        try:
            # Long enough that the HTTP request 504s first.
            time.sleep(1.5)
            raise RuntimeError("herald exploded after timeout")
        finally:
            worker_done.set()

    monkeypatch.setitem(
        br._REGISTRY,
        "morning",
        _BriefingAdapter(
            name="morning",
            description="late-failure test",
            available=_available,
            render_last=lambda _c: None,
            regenerate=_failing_regenerate,
        ),
    )

    app = create_app(config=api_config, token_path=token_path)

    with caplog.at_level("WARNING", logger="pollypm.web_api.routes.briefings"):
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/briefings/morning/regenerate?timeout_seconds=1",
                json={},
                headers=auth_headers,
            )
            assert response.status_code == 504, response.json()

            # Wait for the worker to actually finish (with its raise),
            # plus a small grace so the done-callback runs on the
            # executor thread.
            assert worker_done.wait(timeout=5.0), (
                "failing regenerate worker never completed"
            )
            time.sleep(0.2)

        # Done-callback should have observed the exception and logged it.
        late_logs = [
            r for r in caplog.records
            if "failed after worker completed" in r.getMessage()
        ]
        assert late_logs, (
            "expected late-failure log line; got: "
            f"{[r.getMessage() for r in caplog.records]}"
        )
        # And the scope identifier should appear in the message.
        assert any(
            "morning" in r.getMessage() and "herald exploded" in r.getMessage()
            for r in late_logs
        ), (
            f"expected scope + exception repr in log; got: "
            f"{[r.getMessage() for r in late_logs]}"
        )


# ---------------------------------------------------------------------------
# Codex round-6 regressions (refs #2059)
# ---------------------------------------------------------------------------


def test_briefings_executor_workers_are_daemons(
    api_config: PollyPMConfig,
    token_path: Path,
    token: str,  # noqa: ARG001 — fixture forces token write
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All briefings-executor worker threads must be daemon threads.

    Codex round-6 P0: the round-5 bounded-shutdown fix made FastAPI
    lifespan teardown return promptly, but workers were still
    non-daemon ``threading.Thread`` instances. Any live non-daemon
    thread keeps the CPython interpreter alive past ``sys.exit``,
    so a wedged regenerate provider would still block ``pm serve``
    shutdown / restart even after the route 504'd and the lifespan
    "leaked" the worker. The fix is :class:`_DaemonThreadPoolExecutor`,
    which mirrors CPython's ``_adjust_thread_count`` but sets
    ``t.daemon = True`` before ``t.start()``.

    This test forces the pool to actually spin up a worker (the
    executor is lazy — ``_threads`` is empty until a future is
    submitted) by firing a no-op regenerate, then asserts every
    thread in ``executor._threads`` is a daemon.
    """
    from pollypm.web_api.routes import briefings as br

    def _available(_config) -> bool:
        return True

    def _fast_regenerate(_config, _body: RegenerateRequest) -> BriefingResponse:
        return _make_response(type_name="morning", markdown="ok")

    monkeypatch.setitem(
        br._REGISTRY,
        "morning",
        _BriefingAdapter(
            name="morning",
            description="daemon-thread test",
            available=_available,
            render_last=lambda _c: _make_response(
                type_name="morning", markdown="ok",
            ),
            regenerate=_fast_regenerate,
        ),
    )

    app = create_app(config=api_config, token_path=token_path)

    with TestClient(app) as client:
        # Trigger a regenerate so the pool spins up at least one
        # worker thread. Without this the executor is lazy and
        # ``_threads`` is empty.
        response = client.post(
            "/api/v1/briefings/morning/regenerate?timeout_seconds=5",
            json={},
            headers=auth_headers,
        )
        assert response.status_code == 200, response.json()

        executor = app.state.briefing_executor
        threads = list(getattr(executor, "_threads", []) or [])
        assert threads, (
            "briefing executor spawned no worker threads even after "
            "a successful regenerate — test is not exercising the "
            "daemon-flag code path"
        )
        non_daemons = [t for t in threads if not t.daemon]
        assert not non_daemons, (
            "briefing executor worker(s) are non-daemon — leaked "
            "threads will keep pm serve alive past shutdown: "
            f"{[t.name for t in non_daemons]}"
        )
