"""Integration tests for the Phase 2 sessions-admin endpoints.

Covers ``/api/v1/sessions`` plus the ``restart`` / ``pause`` / ``resume``
mutations per ``~/Desktop/pollypm-phase2-endpoints-spec.md`` §10.

These tests are self-contained (no Postgres harness): we monkeypatch
the heartbeat read, tmux probe, and TmuxSessionService construction on
the route module so the suite runs against an in-process FastAPI app
with no live infra.

Run via ``pytest --noconftest tests/test_sessions_admin_endpoint.py -v``
so the per-project conftest (which spins up a pg test schema) is
skipped — these tests don't need it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
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
from pollypm.models import (
    KnownProject,
    ProjectKind,
    ProviderKind,
    RuntimeKind,
    SessionConfig,
)
from pollypm.web_api import create_app, ensure_token
from pollypm.web_api.routes import sessions_admin as sessions_admin_routes


# ---------------------------------------------------------------------------
# Fixtures
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
            controller_account="claude_primary",
            open_permissions_by_default=False,
            failover_enabled=False,
            failover_accounts=[],
            heartbeat_backend="local",
            scheduler_backend="inline",
            lease_timeout_minutes=30,
        ),
        accounts={
            "claude_primary": AccountConfig(
                name="claude_primary",
                provider=ProviderKind.CLAUDE,
                email="claude@example.com",
                runtime=RuntimeKind.LOCAL,
                home=base_dir / "homes" / "claude_primary",
            ),
        },
        sessions={
            "operator": SessionConfig(
                name="operator",
                role="operator-pm",
                provider=ProviderKind.CLAUDE,
                account="claude_primary",
                cwd=workspace,
                project="pollypm",
                window_name="operator",
                auth_token="deadbeef" * 8,
            ),
            "advisor_myproj": SessionConfig(
                name="advisor_myproj",
                role="advisor",
                provider=ProviderKind.CLAUDE,
                account="claude_primary",
                cwd=project_root,
                project="myproj",
                window_name="advisor_myproj",
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
    )


@pytest.fixture
def token(tmp_path: Path) -> tuple[Path, str]:
    token_path = tmp_path / "api-token"
    value, _generated = ensure_token(token_path)
    return token_path, value


@pytest.fixture
def auth_headers(token: tuple[Path, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {token[1]}"}


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


def _fake_heartbeat(iso_ts: str | None) -> Any:
    """Build a duck-typed heartbeat record with just ``.created_at``."""
    return SimpleNamespace(created_at=iso_ts)


def _fake_window(window_name: str) -> Any:
    """Tmux window stub (only ``.name`` is read by the route)."""
    return SimpleNamespace(name=window_name, pane_id="%9", pane_dead=False)


class _FakeTmuxService:
    """Stand-in for :class:`TmuxSessionService` used in restart tests.

    Tracks calls to ``destroy`` / ``create`` and lets tests pre-program
    ``is_turn_active`` + ``health`` return values.

    Post-#2061: the destructive restart path no longer talks to
    ``_FakeTmuxService.destroy`` / ``create`` directly — those calls
    happen inside the ``Supervisor.restart_session`` facade (covered by
    :class:`_FakeSupervisor`). This stub still drives the mid-turn
    safety probe (``is_turn_active``) and the detail-endpoint health
    snapshot.
    """

    def __init__(
        self,
        *,
        turn_active: bool = False,
        turn_active_raises: Exception | None = None,
        destroy_raises: Exception | None = None,
        create_raises: Exception | None = None,
        window_present: bool = True,
    ) -> None:
        self.turn_active = turn_active
        self.turn_active_raises = turn_active_raises
        self.destroy_raises = destroy_raises
        self.create_raises = create_raises
        self.window_present = window_present
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def destroy(self, name: str) -> None:
        self.calls.append(("destroy", {"name": name}))
        if self.destroy_raises:
            raise self.destroy_raises

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(("create", kwargs))
        if self.create_raises:
            raise self.create_raises
        return SimpleNamespace(name=kwargs["name"], window_name=kwargs.get("window_name"))

    def is_turn_active(self, name: str) -> bool:  # noqa: ARG002
        if self.turn_active_raises is not None:
            raise self.turn_active_raises
        return self.turn_active

    def health(self, name: str, *, capture_lines: int = 200) -> Any:  # noqa: ARG002
        return SimpleNamespace(
            window_present=self.window_present,
            pane_alive=self.window_present,
            pane_dead=not self.window_present,
            pane_command="claude" if self.window_present else None,
            pane_text="",
        )


class _FakeSupervisor:
    """Stand-in for :class:`pollypm.supervisor.Supervisor` for restart tests.

    Records every ``restart_session`` invocation so the test can assert
    the route invoked the **canonical facade** (Codex PR #2061 P0 #1)
    rather than reaching past it to the raw ``destroy``+``create``
    helpers. Optionally raises to exercise the
    ``daemon_unavailable`` recovery branch.
    """

    def __init__(
        self,
        *,
        restart_raises: Exception | None = None,
        effective_account: str | None = None,
    ) -> None:
        self.restart_raises = restart_raises
        self.effective_account = effective_account
        self.restart_calls: list[dict[str, Any]] = []

    def get_session_runtime(self, name: str) -> Any:  # noqa: ARG002
        # Public Supervisor method (Codex PR #2061 round 5 blocker 2 —
        # the route stopped reaching into ``_get_session_runtime`` and
        # now uses the public wrapper that has been on Supervisor since
        # the #1830 cluster-A pg facade landed).
        if self.effective_account is None:
            return None
        return SimpleNamespace(effective_account=self.effective_account)

    def restart_session(
        self, session_name: str, account_name: str, *, failure_type: str,
    ) -> None:
        self.restart_calls.append({
            "session_name": session_name,
            "account_name": account_name,
            "failure_type": failure_type,
        })
        if self.restart_raises is not None:
            raise self.restart_raises


class _FakeInterruptTmux:
    def __init__(
        self,
        windows: list[Any],
        *,
        run_raises: Exception | None = None,
    ) -> None:
        self.windows = windows
        self.run_raises = run_raises
        self.list_calls: list[str] = []
        self.run_calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    def list_windows(self, name: str) -> list[Any]:
        self.list_calls.append(name)
        return list(self.windows)

    def run(self, *args: str, **kwargs: Any) -> Any:
        self.run_calls.append((args, kwargs))
        if self.run_raises is not None:
            raise self.run_raises
        return SimpleNamespace(returncode=0, stdout="", stderr="")


@pytest.fixture
def patch_heartbeat(monkeypatch: pytest.MonkeyPatch):
    """Install a stub for ``_latest_heartbeat`` on the route module."""
    def install(values: dict[str, str | None]) -> None:
        def fake(_config, name: str):
            iso = values.get(name)
            if iso is None:
                return None
            return _fake_heartbeat(iso)
        monkeypatch.setattr(
            sessions_admin_routes, "_latest_heartbeat", fake,
        )
    return install


@pytest.fixture
def patch_tmux_windows(monkeypatch: pytest.MonkeyPatch):
    """Install a stub for ``_list_storage_closet_windows``."""
    def install(present_windows: list[str]) -> None:
        def fake(_session: str) -> dict[str, Any]:
            return {name: _fake_window(name) for name in present_windows}
        monkeypatch.setattr(
            sessions_admin_routes, "_list_storage_closet_windows", fake,
        )
    return install


@pytest.fixture
def patch_tmux_service(monkeypatch: pytest.MonkeyPatch):
    """Install a stub for ``_build_tmux_service``; returns the fake service."""
    def install(service: _FakeTmuxService | None) -> _FakeTmuxService | None:
        monkeypatch.setattr(
            sessions_admin_routes, "_build_tmux_service",
            lambda _config: service,
        )
        return service
    return install


@pytest.fixture
def patch_strict_probe(monkeypatch: pytest.MonkeyPatch):
    """Install a stub for ``_probe_strict_turn_active`` on the route module.

    Codex PR #2061 round 6: the destructive restart safety gate now
    calls :func:`pollypm.session_health.probe_strict_turn_active`
    directly (config + tmux client, no :class:`TmuxSessionService` /
    legacy :class:`StateStore`). Tests assert the route consults this
    config/tmux-direct probe by patching the route's reference to it.

    Pass ``mid_turn=True`` to force the 409 unsafe_mid_turn branch,
    ``raises=RuntimeError(...)`` to force the 503
    unsafe_mid_turn_unknown branch.
    """
    from pollypm.session_health import TmuxProbeUnavailable

    def install(
        *,
        mid_turn: bool = False,
        raises: Exception | None = None,
    ) -> dict[str, Any]:
        calls: list[tuple[Any, str, Any]] = []

        def fake(
            config: Any, name: str, tmux_client: Any, **kwargs: Any,
        ) -> bool:
            calls.append((config, name, tmux_client))
            if raises is not None:
                if isinstance(raises, TmuxProbeUnavailable):
                    raise raises
                raise TmuxProbeUnavailable(str(raises)) from raises
            return mid_turn

        monkeypatch.setattr(
            sessions_admin_routes, "_probe_strict_turn_active", fake,
        )
        return {"calls": calls}
    return install


@pytest.fixture
def patch_supervisor(monkeypatch: pytest.MonkeyPatch):
    """Install a stub for ``_build_supervisor`` on the route module.

    Post-#2061 the restart endpoint routes through
    :meth:`Supervisor.restart_session` (the canonical facade) instead
    of poking ``TmuxSessionService.create()`` directly. Tests use this
    fixture to assert the facade was called with the right
    ``(session_name, account_name, failure_type)``.
    """
    def install(supervisor: _FakeSupervisor | None) -> _FakeSupervisor | None:
        monkeypatch.setattr(
            sessions_admin_routes, "_build_supervisor",
            lambda _config: supervisor,
        )
        return supervisor
    return install


@pytest.fixture
def patch_interrupt_tmux(monkeypatch: pytest.MonkeyPatch):
    def install(tmux: _FakeInterruptTmux) -> _FakeInterruptTmux:
        from pollypm.tmux import client as tmux_client_module

        monkeypatch.setattr(tmux_client_module, "TmuxClient", lambda: tmux)
        return tmux

    return install


@pytest.fixture
def patch_pg_outage(monkeypatch: pytest.MonkeyPatch):
    """Simulate a pg backend outage on the heartbeat read path.

    The route is fail-soft for the GET list/detail surfaces: pg pool
    outages must NOT 500 or 503 — they collapse to
    ``last_heartbeat_iso=null`` / ``status="unknown"`` so an operator
    in a degraded environment still sees their session inventory.
    The 503 surface is reserved for mutation endpoints where the
    tmux service itself can't be constructed (covered separately in
    :func:`test_restart_503_when_service_unavailable`).

    We patch :func:`pollypm.storage.pg_heartbeats.latest_heartbeat`
    (not the route helper that wraps it) so the route's own
    ``try/except`` is exercised — that's the contract under test.
    """
    def install() -> None:
        from pollypm.storage import pg_heartbeats

        def boom(_session_name, *, pool=None, config=None):  # noqa: ARG001
            raise RuntimeError("pg pool unavailable")
        monkeypatch.setattr(
            pg_heartbeats, "latest_heartbeat", boom,
        )
    return install


# ---------------------------------------------------------------------------
# GET /sessions — list
# ---------------------------------------------------------------------------


def test_list_sessions_returns_all_configured(
    client, auth_headers, patch_heartbeat, patch_tmux_windows,
):
    patch_heartbeat({
        "operator": "2026-05-21T10:00:00Z",
        "advisor_myproj": None,
    })
    patch_tmux_windows(["operator", "advisor_myproj"])
    response = client.get("/api/v1/sessions", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    names = [s["name"] for s in body["sessions"]]
    assert names == ["advisor_myproj", "operator"]  # sorted by name
    by_name = {s["name"]: s for s in body["sessions"]}
    # operator has a heartbeat row and a present window — depending on
    # the heartbeat ts vs "now" the status is either healthy or stale;
    # both are valid here. Just assert the runtime classified it as
    # one of the known states and surfaced the heartbeat.
    assert by_name["operator"]["status"] in {"healthy", "stale"}
    assert by_name["operator"]["last_heartbeat_iso"] == "2026-05-21T10:00:00Z"
    assert by_name["operator"]["window_present"] is True
    assert by_name["operator"]["auth_token_present"] is True
    # advisor has no heartbeat row → unknown
    assert by_name["advisor_myproj"]["status"] == "unknown"
    assert by_name["advisor_myproj"]["last_heartbeat_iso"] is None
    assert by_name["advisor_myproj"]["auth_token_present"] is False


def test_list_sessions_marks_missing_when_window_absent(
    client, auth_headers, patch_heartbeat, patch_tmux_windows,
):
    patch_heartbeat({"operator": "2026-05-21T10:00:00Z"})
    patch_tmux_windows([])  # no windows in tmux
    body = client.get("/api/v1/sessions", headers=auth_headers).json()
    by_name = {s["name"]: s for s in body["sessions"]}
    # Missing window beats heartbeat staleness (matches CLI behavior).
    assert by_name["operator"]["status"] == "missing"
    assert by_name["operator"]["window_present"] is False


def test_list_sessions_requires_bearer_auth(client):
    response = client.get("/api/v1/sessions")
    assert response.status_code == 401
    assert response.json()["error"]["code"] in {"unauthorized", "invalid_token"}


def test_list_sessions_rejects_wrong_token(client):
    response = client.get(
        "/api/v1/sessions",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


def test_list_sessions_pg_outage_degrades_to_unknown(
    client, auth_headers, patch_pg_outage, patch_tmux_windows,
):
    """pg facade outage on the read path collapses to ``status=unknown``.

    The list endpoint is fail-soft (spec §10.4 "daemon goes down" /
    blocker-class behaviour from the chat_messages route): a backing-
    store outage must not 503 the list; it just means heartbeats can't
    be evaluated, so every row reports ``unknown``.
    """
    patch_pg_outage()
    patch_tmux_windows(["operator", "advisor_myproj"])
    response = client.get("/api/v1/sessions", headers=auth_headers)
    assert response.status_code == 200, response.text
    for row in response.json()["sessions"]:
        assert row["status"] in {"unknown", "missing"}
        assert row["last_heartbeat_iso"] is None


# ---------------------------------------------------------------------------
# GET /sessions/{name} — detail
# ---------------------------------------------------------------------------


def test_get_session_detail_returns_config_health(
    client, auth_headers, patch_heartbeat, patch_tmux_windows,
    patch_tmux_service,
):
    patch_heartbeat({"operator": "2026-05-21T10:00:00Z"})
    patch_tmux_windows(["operator"])
    patch_tmux_service(_FakeTmuxService(turn_active=False))
    response = client.get("/api/v1/sessions/operator", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["info"]["name"] == "operator"
    assert body["config"]["name"] == "operator"
    assert body["config"]["role"] == "operator-pm"
    assert body["config"]["auth_token_present"] is True
    assert body["health"]["window_present"] is True
    assert body["health"]["pane_alive"] is True
    assert body["is_turn_active"] is False


def test_get_session_404_unknown(
    client, auth_headers, patch_heartbeat, patch_tmux_windows,
    patch_tmux_service,
):
    patch_heartbeat({})
    patch_tmux_windows([])
    patch_tmux_service(_FakeTmuxService())
    response = client.get("/api/v1/sessions/does-not-exist", headers=auth_headers)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_get_session_window_present_consistent_between_info_and_health(
    client, auth_headers, patch_heartbeat, patch_tmux_windows,
    patch_tmux_service,
):
    """``info.window_present`` and ``health.window_present`` must agree.

    Codex PR #2061 round 6: ``info.window_present`` is built from the
    shared :func:`pollypm.session_health.list_storage_closet_windows`
    helper (config + tmux direct), while ``health.window_present``
    used to come from :meth:`TmuxSessionService.health` →
    ``self._store.list_sessions()`` on the legacy :class:`StateStore`.
    On a pg-backed install with no legacy-StateStore session rows the
    two fields could disagree for the same configured session — one
    reporting ``True`` (window genuinely live), the other ``False``
    (in-service ``get()`` saw no StateStore record so returned a
    "window absent" health snapshot).

    The fix single-sources ``health.window_present`` to the value
    already computed for ``info`` from the canonical helper. This test
    pins the contract: a live window is reported as present on BOTH
    fields, an absent window as missing on BOTH.
    """
    patch_heartbeat({"operator": "2026-05-21T10:00:00Z"})
    patch_tmux_windows(["operator"])

    # _FakeTmuxService has window_present=True by default → in the
    # baseline this matched info. We force window_present=False on the
    # in-service health snapshot to simulate the round-6 split-source
    # bug (StateStore-empty install): without the fix,
    # health.window_present is False while info.window_present is True
    # (because list_storage_closet_windows DID find the window).
    patch_tmux_service(_FakeTmuxService(
        turn_active=False, window_present=False,
    ))

    response = client.get(
        "/api/v1/sessions/operator", headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["info"]["window_present"] is True
    # Without the round-6 fix this would be False.
    assert body["health"]["window_present"] == body["info"]["window_present"], (
        "info.window_present and health.window_present must single-source "
        "from the same config/tmux helper — Codex PR #2061 round 6."
    )

    # And the negative direction: when the window is absent both
    # fields must report False.
    patch_tmux_windows([])
    patch_tmux_service(_FakeTmuxService(
        turn_active=False, window_present=True,
    ))
    response = client.get(
        "/api/v1/sessions/operator", headers=auth_headers,
    )
    body = response.json()
    assert body["info"]["window_present"] is False
    assert body["health"]["window_present"] == body["info"]["window_present"]


# ---------------------------------------------------------------------------
# POST /sessions/{name}/restart
# ---------------------------------------------------------------------------


def test_restart_routes_through_supervisor_facade(
    client, auth_headers, patch_heartbeat, patch_tmux_windows,
    patch_strict_probe, patch_supervisor,
):
    """Restart goes through ``Supervisor.restart_session`` (PR #2061 P0 #1).

    Regression: the previous restart implementation called
    ``TmuxSessionService.create()`` with no command, which falls back
    to ``echo 'No command for {name}'`` and destroys live agents.
    The route must hand off to the same facade ``pm
    switch-session-account`` / the cockpit account-switch button use,
    which consults the launch planner for the real provider command.
    """
    patch_heartbeat({"operator": "2026-05-21T10:00:00Z"})
    patch_tmux_windows(["operator"])
    probe = patch_strict_probe(mid_turn=False)
    sup = patch_supervisor(_FakeSupervisor())
    response = client.post(
        "/api/v1/sessions/operator/restart", headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert "operator" in (body.get("message") or "")
    # The strict probe was consulted (config/tmux-direct, not the
    # legacy StateStore-dependent in-service probe).
    assert len(probe["calls"]) == 1
    # The facade was called once with the session, the configured
    # account (effective_account is None in this stub), and the
    # api_restart failure_type so the recovery audit chain attributes
    # the relaunch correctly.
    assert len(sup.restart_calls) == 1
    call = sup.restart_calls[0]
    assert call["session_name"] == "operator"
    assert call["account_name"] == "claude_primary"
    assert call["failure_type"] == "api_restart"


def test_restart_prefers_effective_account_over_configured(
    client, auth_headers, patch_heartbeat, patch_tmux_windows,
    patch_strict_probe, patch_supervisor,
):
    """When the runtime carries an ``effective_account`` it wins.

    A session that previously failed over to a recovery account
    should keep using that account on operator-driven restart —
    otherwise the relaunch slams the original (failed) account and
    immediately re-triggers the same recovery loop.
    """
    patch_heartbeat({"operator": "2026-05-21T10:00:00Z"})
    patch_tmux_windows(["operator"])
    patch_strict_probe(mid_turn=False)
    sup = patch_supervisor(
        _FakeSupervisor(effective_account="recovery_account"),
    )
    response = client.post(
        "/api/v1/sessions/operator/restart", headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert sup.restart_calls[0]["account_name"] == "recovery_account"


def test_restart_facade_failure_503_daemon_unavailable(
    client, auth_headers, patch_heartbeat, patch_tmux_windows,
    patch_strict_probe, patch_supervisor,
):
    """``Supervisor.restart_session`` raising → 503, not 500."""
    patch_heartbeat({"operator": "2026-05-21T10:00:00Z"})
    patch_tmux_windows(["operator"])
    patch_strict_probe(mid_turn=False)
    patch_supervisor(
        _FakeSupervisor(restart_raises=RuntimeError("tmux server down")),
    )
    response = client.post(
        "/api/v1/sessions/operator/restart", headers=auth_headers,
    )
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "daemon_unavailable"


def test_restart_refuses_mid_turn(
    client, auth_headers, patch_heartbeat, patch_tmux_windows,
    patch_strict_probe, patch_supervisor,
):
    patch_heartbeat({"operator": "2026-05-21T10:00:00Z"})
    patch_tmux_windows(["operator"])
    patch_strict_probe(mid_turn=True)
    sup = patch_supervisor(_FakeSupervisor())
    response = client.post(
        "/api/v1/sessions/operator/restart", headers=auth_headers,
    )
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "unsafe_mid_turn"
    # The destructive facade was NEVER touched.
    assert sup.restart_calls == []


def test_restart_strict_fail_closed_when_probe_raises(
    client, auth_headers, patch_heartbeat, patch_tmux_windows,
    patch_strict_probe, patch_supervisor,
):
    """Strict-mode probe failure → 503 ``unsafe_mid_turn_unknown``.

    PR #2061 P0 #2 + round 6: a transient tmux failure must not look
    like "agent is idle" and let the route destroy an actively-working
    agent. The route now consults the config/tmux-direct
    :func:`pollypm.session_health.probe_strict_turn_active` helper,
    which raises :class:`TmuxProbeUnavailable` on tmux outage.
    """
    patch_heartbeat({"operator": "2026-05-21T10:00:00Z"})
    patch_tmux_windows(["operator"])
    patch_strict_probe(raises=RuntimeError("tmux probe timeout"))
    sup = patch_supervisor(_FakeSupervisor())
    response = client.post(
        "/api/v1/sessions/operator/restart", headers=auth_headers,
    )
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["error"]["code"] == "unsafe_mid_turn_unknown"
    # The destructive path was NEVER reached.
    assert sup.restart_calls == []


def test_restart_force_bypasses_safety_probe_entirely(
    client, auth_headers, patch_heartbeat, patch_tmux_windows,
    patch_strict_probe, patch_supervisor,
):
    """``?safety=force`` skips the probe even when it would raise.

    Operator-escalation override: force is destructive *by design*,
    so a flaky probe must not block it.
    """
    patch_heartbeat({"operator": "2026-05-21T10:00:00Z"})
    patch_tmux_windows(["operator"])
    probe = patch_strict_probe(raises=RuntimeError("tmux probe timeout"))
    sup = patch_supervisor(_FakeSupervisor())
    response = client.post(
        "/api/v1/sessions/operator/restart?safety=force",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert len(sup.restart_calls) == 1
    # force MUST bypass the probe entirely.
    assert probe["calls"] == []


def test_restart_force_overrides_mid_turn(
    client, auth_headers, patch_heartbeat, patch_tmux_windows,
    patch_strict_probe, patch_supervisor,
):
    patch_heartbeat({"operator": "2026-05-21T10:00:00Z"})
    patch_tmux_windows(["operator"])
    patch_strict_probe(mid_turn=True)
    sup = patch_supervisor(_FakeSupervisor())
    response = client.post(
        "/api/v1/sessions/operator/restart?safety=force",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert len(sup.restart_calls) == 1
    assert sup.restart_calls[0]["session_name"] == "operator"


def test_restart_404_unknown_session(
    client, auth_headers, patch_heartbeat, patch_tmux_windows,
    patch_strict_probe, patch_supervisor,
):
    patch_heartbeat({})
    patch_tmux_windows([])
    patch_strict_probe(mid_turn=False)
    patch_supervisor(_FakeSupervisor())
    response = client.post(
        "/api/v1/sessions/nope/restart", headers=auth_headers,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_restart_strict_blocks_when_state_store_empty_but_tmux_window_live(
    client, config, auth_headers, monkeypatch, patch_heartbeat,
    patch_tmux_windows, patch_supervisor,
):
    """Codex PR #2061 round 6 regression — the headline fail-open case.

    Production session registration is in pg
    (``supervisor._record_launch`` → ``pollypm.storage.pg_sessions.upsert_session``),
    so the legacy :class:`StateStore.list_sessions()` row set can be
    empty/stale even when the configured session has a live, actively-
    working tmux pane. The prior round-5 strict probe (
    :meth:`TmuxSessionService.is_turn_active_strict`) saw "no
    StateStore row" → returned ``False`` (looks-idle) → the destructive
    restart proceeded and could destroy a working agent.

    The round-6 fix: the strict probe reads from the SAME source as
    the rest of the API contract — ``config.sessions[name]`` +
    :func:`pollypm.session_health.list_storage_closet_windows` + direct
    :class:`pollypm.tmux.client.TmuxClient` calls. With the legacy
    StateStore empty but a live tmux window and active-turn pane text,
    default restart MUST return 409 ``unsafe_mid_turn`` and MUST NOT
    call :meth:`Supervisor.restart_session`.

    This test is constructed to FAIL against the round-5
    implementation (StateStore-dependent probe → False → 200 restart)
    and PASS against the round-6 implementation (config/tmux-direct
    probe → True → 409). Verified by reverting the new helper +
    restart call site and re-running.
    """
    # Stub list_storage_closet_windows so the route's helper sees the
    # configured "operator" window as present + live.
    patch_tmux_windows(["operator"])
    patch_heartbeat({"operator": "2026-05-21T10:00:00Z"})

    # Stub TmuxClient at the construction site so the strict probe's
    # has_session + list_windows + list_panes + capture_pane don't
    # shell out to a real tmux. Round 7: the probe now owns window
    # discovery, so the fake must implement ``has_session`` /
    # ``list_windows`` too — not just the pane-level calls.
    # The pane text mimics Codex's mid-turn marker.
    class _FakePane:
        def __init__(self, pane_id: str, *, active: bool = True) -> None:
            self.pane_id = pane_id
            self.active = active

    class _FakeWindow:
        def __init__(self, name: str, pane_id: str) -> None:
            self.name = name
            self.pane_id = pane_id
            self.pane_dead = False
            self.session = "pollypm-test-storage-closet"

    class _FakeTmuxClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, Any]] = []

        def has_session(self, name: str) -> bool:
            self.calls.append(("has_session", name))
            return True

        def list_windows(self, name: str):  # noqa: ARG002
            self.calls.append(("list_windows", name))
            return [_FakeWindow("operator", "%9")]

        def list_panes(self, target: str):  # noqa: ARG002
            self.calls.append(("list_panes", target))
            return [_FakePane("%9", active=True)]

        def capture_pane(self, pane_id: str, lines: int = 200):  # noqa: ARG002
            self.calls.append(("capture_pane", pane_id))
            return "working (10s) esc to interrupt\n"

    fake_client = _FakeTmuxClient()
    monkeypatch.setattr(
        "pollypm.tmux.client.TmuxClient", lambda: fake_client,
    )

    sup = patch_supervisor(_FakeSupervisor())

    response = client.post(
        "/api/v1/sessions/operator/restart", headers=auth_headers,
    )
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error"]["code"] == "unsafe_mid_turn"
    # The destructive facade was NEVER touched even though no
    # legacy-StateStore row exists for "operator".
    assert sup.restart_calls == []
    # The strict probe DID consult the live tmux pane.
    assert any(call[0] == "capture_pane" for call in fake_client.calls)


def test_restart_strict_503_when_tmux_probe_raises(
    client, config, auth_headers, monkeypatch, patch_heartbeat,
    patch_tmux_windows, patch_supervisor,
):
    """Tmux ``capture_pane`` outage → strict probe fails closed (503).

    Codex PR #2061 round 3 + round 6: with a live tmux window the
    strict probe still must propagate a ``capture_pane`` failure as
    :class:`pollypm.session_health.TmuxProbeUnavailable` so the route
    returns ``503 unsafe_mid_turn_unknown`` (fail-closed). Carried
    forward from round 3 against the new config/tmux-direct probe.
    """
    patch_tmux_windows(["operator"])
    patch_heartbeat({"operator": "2026-05-21T10:00:00Z"})

    import subprocess

    class _FakeWindow:
        def __init__(self, name: str, pane_id: str) -> None:
            self.name = name
            self.pane_id = pane_id
            self.pane_dead = False
            self.session = "pollypm-test-storage-closet"

    class _FakeTmuxClient:
        def has_session(self, name: str) -> bool:  # noqa: ARG002
            return True

        def list_windows(self, name: str):  # noqa: ARG002
            return [_FakeWindow("operator", "%9")]

        def list_panes(self, target: str):  # noqa: ARG002
            return []

        def capture_pane(self, pane_id: str, lines: int = 200):  # noqa: ARG002
            raise subprocess.CalledProcessError(
                returncode=1, cmd=["tmux", "capture-pane"],
            )

    monkeypatch.setattr(
        "pollypm.tmux.client.TmuxClient", lambda: _FakeTmuxClient(),
    )
    sup = patch_supervisor(_FakeSupervisor())

    response = client.post(
        "/api/v1/sessions/operator/restart", headers=auth_headers,
    )
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["error"]["code"] == "unsafe_mid_turn_unknown"
    assert sup.restart_calls == []


def test_restart_strict_503_when_tmux_has_session_times_out(
    client, config, auth_headers, monkeypatch, patch_heartbeat,
    patch_supervisor,
):
    """Codex PR #2061 round 8 — rc=124 timeout from has-session → 503.

    The real :meth:`pollypm.tmux.client.TmuxClient.run` synthesises a
    ``CompletedProcess`` with ``returncode=124`` when the underlying
    ``subprocess.TimeoutExpired`` fires under ``check=False``. The
    prior fail-soft :meth:`TmuxClient.has_session` then returned
    ``False`` for **any** non-zero rc, conflating a real "session
    absent" (rc=1) with a wedged tmux server (rc=124). The strict
    probe therefore treated a tmux outage as a definitive "nothing to
    interrupt" and the destructive restart proceeded against an
    unobservable agent — exact reproduction Codex documented in round 8.

    Round 8 fix: a new :meth:`TmuxClient.has_session_strict` returns
    ``False`` only on rc=1 and raises
    :class:`pollypm.session_health.TmuxProbeUnavailable` on rc=124 /
    any other rc / ``TimeoutExpired``. The strict probe in
    :func:`pollypm.session_health.probe_strict_turn_active` now calls
    that strict variant so the route maps tmux-server outage to
    ``503 unsafe_mid_turn_unknown`` (fail-closed).

    Reproducer reference: this test FAILS against round 7 (revert
    ``has_session_strict`` to the fail-soft ``has_session`` call in
    :func:`probe_strict_turn_active` → run → observe 200 ``ok`` with
    ``Supervisor.restart_session`` called once instead of 503).
    """
    patch_heartbeat({"operator": "2026-05-21T10:00:00Z"})

    import subprocess

    from pollypm.tmux.client import TmuxClient

    # Patch ``TmuxClient.run`` to return the rc=124 CompletedProcess
    # the real wrapper would synthesise after a wedged-tmux timeout.
    # We patch the method (not a stub class) so ``has_session_strict``
    # exercises its real branch logic — the round-8 fix lives there.
    def fake_run(self, *args, **kwargs):  # noqa: ARG001
        return subprocess.CompletedProcess(
            args=["tmux", *args],
            returncode=124,
            stdout="",
            stderr="tmux command timed out after 15s",
        )

    monkeypatch.setattr(TmuxClient, "run", fake_run)
    sup = patch_supervisor(_FakeSupervisor())

    response = client.post(
        "/api/v1/sessions/operator/restart", headers=auth_headers,
    )
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["error"]["code"] == "unsafe_mid_turn_unknown"
    # The destructive facade was NEVER touched — round 8 fail-closed.
    assert sup.restart_calls == []


def test_restart_strict_503_when_tmux_has_session_raises_timeout(
    client, config, auth_headers, monkeypatch, patch_heartbeat,
    patch_supervisor,
):
    """Codex PR #2061 round 8 — ``subprocess.TimeoutExpired`` → 503.

    Defensive companion to
    :func:`test_restart_strict_503_when_tmux_has_session_times_out`.
    Today :meth:`TmuxClient.run` converts ``TimeoutExpired`` into
    rc=124 under ``check=False``, but a future signature change or
    test stub could re-raise. The strict variant must propagate
    either flavour as :class:`TmuxProbeUnavailable` → 503.
    """
    patch_heartbeat({"operator": "2026-05-21T10:00:00Z"})

    import subprocess

    from pollypm.tmux.client import TmuxClient

    def fake_run(self, *args, **kwargs):  # noqa: ARG001
        raise subprocess.TimeoutExpired(cmd=["tmux", *args], timeout=15)

    monkeypatch.setattr(TmuxClient, "run", fake_run)
    sup = patch_supervisor(_FakeSupervisor())

    response = client.post(
        "/api/v1/sessions/operator/restart", headers=auth_headers,
    )
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["error"]["code"] == "unsafe_mid_turn_unknown"
    assert sup.restart_calls == []


def test_get_session_does_not_construct_supervisor(
    client, auth_headers, monkeypatch, patch_heartbeat, patch_tmux_windows,
):
    """Codex PR #2061 round 8 — GET detail path must not build Supervisor.

    The previous GET detail wiring went through
    :func:`_build_tmux_service` → :attr:`Supervisor.session_service`
    purely to call :meth:`TmuxSessionService.health` +
    :meth:`is_turn_active`. ``Supervisor.__init__`` opens the legacy
    sqlite ``state.db`` (and runs migrations) as a side-effect, and
    the GET path never called ``Supervisor.stop()`` → repeated polling
    leaked fds + sqlite connections.

    Round 8 fix: the GET path computes ``health`` directly from the
    ``TmuxWindow`` already returned by
    :func:`list_storage_closet_windows` (``pane_id`` / ``pane_dead`` /
    ``pane_current_command``) and runs a small fail-soft
    ``capture_pane`` for ``is_turn_active``. No Supervisor, no
    StateStore, no transient connections.

    This test pins the contract by exploding if either
    :func:`_build_supervisor` or :func:`_build_tmux_service` is called
    during a GET — fail-LOUDER than the silent leak it replaced.
    """
    patch_heartbeat({"operator": "2026-05-21T10:00:00Z"})
    patch_tmux_windows(["operator"])

    def _boom_build_supervisor(_config):
        raise AssertionError(
            "GET /api/v1/sessions/{name} MUST NOT construct a "
            "Supervisor — Codex PR #2061 round 8 lifecycle cleanup.",
        )

    def _boom_build_tmux_service(_config):
        raise AssertionError(
            "GET /api/v1/sessions/{name} MUST NOT build a transient "
            "TmuxSessionService (which constructs a Supervisor) — "
            "Codex PR #2061 round 8 lifecycle cleanup.",
        )

    monkeypatch.setattr(
        sessions_admin_routes, "_build_supervisor", _boom_build_supervisor,
    )
    monkeypatch.setattr(
        sessions_admin_routes, "_build_tmux_service",
        _boom_build_tmux_service,
    )

    response = client.get(
        "/api/v1/sessions/operator", headers=auth_headers,
    )
    # 200 — the GET path computed the detail payload entirely from
    # config + the shared list_storage_closet_windows helper +
    # latest_heartbeat. Neither Supervisor nor TmuxSessionService was
    # constructed (which would have tripped the asserts above and
    # surfaced as a 500).
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["info"]["name"] == "operator"
    assert body["info"]["window_present"] is True
    # The health snapshot still reports the live window — derived
    # directly from the TmuxWindow returned by
    # list_storage_closet_windows.
    assert body["health"]["window_present"] is True
    assert body["health"]["pane_alive"] is True


def test_restart_strict_503_when_window_listing_raises(
    client, config, auth_headers, monkeypatch, patch_heartbeat,
    patch_supervisor,
):
    """Codex PR #2061 round 7 — window listing failure → fail-closed 503.

    The round-6 implementation threaded the fail-soft
    :func:`_list_storage_closet_windows` helper into the probe; that
    helper catches every tmux failure (``CalledProcessError``,
    ``FileNotFoundError``, timeout, etc.) and returns ``{}``. The
    probe then saw "no window matching this session" → returned
    ``False`` (looks idle) → the destructive restart proceeded against
    an unobservable agent. Round 7 fix: the probe owns window
    discovery directly via ``TmuxClient.has_session`` /
    ``TmuxClient.list_windows`` so a tmux failure raises
    :class:`TmuxProbeUnavailable` → 503 ``unsafe_mid_turn_unknown``.

    Pointedly we do NOT patch ``_list_storage_closet_windows`` here —
    the round-6 path would route through that fail-soft helper and
    swallow the ``CalledProcessError`` into ``{}`` → "absent" → 200
    restart. Round 7 bypasses the helper entirely, so a raise from
    ``has_session`` propagates as ``TmuxProbeUnavailable`` → 503.
    Reproducer reference: this test FAILS against round 6 (revert the
    ``windows=`` signature in :func:`probe_strict_turn_active` and the
    route's ``_list_storage_closet_windows`` threading → run the test
    → observe 200 ``ok`` with ``Supervisor.restart_session`` called
    instead of 503).
    """
    patch_heartbeat({"operator": "2026-05-21T10:00:00Z"})

    import subprocess

    class _FakeTmuxClient:
        """Tmux outage: has_session raises CalledProcessError."""

        def has_session(self, name: str):  # noqa: ARG002
            raise subprocess.CalledProcessError(
                returncode=1, cmd=["tmux", "has-session"],
            )

        def list_windows(self, name: str):  # noqa: ARG002 — defensive
            raise subprocess.CalledProcessError(
                returncode=1, cmd=["tmux", "list-windows"],
            )

    monkeypatch.setattr(
        "pollypm.tmux.client.TmuxClient", lambda: _FakeTmuxClient(),
    )
    sup = patch_supervisor(_FakeSupervisor())

    response = client.post(
        "/api/v1/sessions/operator/restart", headers=auth_headers,
    )
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["error"]["code"] == "unsafe_mid_turn_unknown"
    # The destructive facade was NEVER touched — round 7 fail-closed.
    assert sup.restart_calls == []


def test_restart_strict_proceeds_when_window_genuinely_absent(
    client, config, auth_headers, monkeypatch, patch_heartbeat,
    patch_supervisor,
):
    """Codex PR #2061 round 7 — definitive "no window" still allows restart.

    The fail-closed gate added in round 7 must NOT regress the
    legitimate "window is genuinely absent" case: tmux is up, replies
    successfully, but there's no storage-closet window for this
    session (e.g. first boot, after a crash). The probe should return
    ``False`` (nothing to interrupt) so the restart can create a fresh
    window — same behaviour as round 6, just now reached via a
    successful tmux call instead of a swallowed exception.
    """
    patch_heartbeat({"operator": "2026-05-21T10:00:00Z"})

    class _FakeTmuxClient:
        """Tmux works fine, just no storage-closet session yet."""

        def has_session(self, name: str) -> bool:  # noqa: ARG002
            # Reliable "no" from tmux — analogous to returncode=1 with
            # "session not found" — NOT a server outage.
            return False

        def list_windows(self, name: str):  # noqa: ARG002 — not reached
            raise AssertionError(
                "list_windows must not be called when has_session is False",
            )

    monkeypatch.setattr(
        "pollypm.tmux.client.TmuxClient", lambda: _FakeTmuxClient(),
    )
    sup = patch_supervisor(_FakeSupervisor())

    response = client.post(
        "/api/v1/sessions/operator/restart", headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    # The relaunch facade was invoked — definitive "no window" is a
    # green-light for the strict gate.
    assert len(sup.restart_calls) == 1
    assert sup.restart_calls[0]["session_name"] == "operator"


def test_restart_force_still_bypasses_window_listing_failure(
    client, config, auth_headers, monkeypatch, patch_heartbeat,
    patch_supervisor,
):
    """Codex PR #2061 round 7 — ``?safety=force`` still bypasses a broken probe.

    Operator escalation override: ``force`` is destructive *by design*
    and exists precisely so a flaky probe can be worked around. A
    tmux outage that would 503 the default strict path must still let
    ``?safety=force`` reach :meth:`Supervisor.restart_session`. This
    pins that the round-7 fail-closed contract was applied to the
    strict gate, not to the entire restart path.
    """
    patch_heartbeat({"operator": "2026-05-21T10:00:00Z"})

    import subprocess

    class _FakeTmuxClient:
        def has_session(self, name: str):  # noqa: ARG002
            raise subprocess.CalledProcessError(
                returncode=1, cmd=["tmux", "has-session"],
            )

    monkeypatch.setattr(
        "pollypm.tmux.client.TmuxClient", lambda: _FakeTmuxClient(),
    )
    sup = patch_supervisor(_FakeSupervisor())

    response = client.post(
        "/api/v1/sessions/operator/restart?safety=force",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    # Force bypassed the probe entirely → facade called even though
    # tmux is broken.
    assert len(sup.restart_calls) == 1
    assert sup.restart_calls[0]["session_name"] == "operator"


def test_restart_strict_503_when_supervisor_unavailable(
    client, auth_headers, patch_heartbeat, patch_tmux_windows,
    patch_strict_probe, patch_supervisor,
):
    """Supervisor construction failure → 503 ``daemon_unavailable``.

    Codex PR #2061 round 6 lifecycle note: supervisor construction is
    deferred until AFTER the strict probe passes, so this only fires
    on a healthy strict probe (no transient writable Supervisor on the
    probe path). The route still returns 503 ``daemon_unavailable``
    when the supervisor cannot be built for the actual restart.
    """
    patch_heartbeat({"operator": "2026-05-21T10:00:00Z"})
    patch_tmux_windows(["operator"])
    patch_strict_probe(mid_turn=False)
    patch_supervisor(None)  # Supervisor() failed
    response = client.post(
        "/api/v1/sessions/operator/restart", headers=auth_headers,
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "daemon_unavailable"


def test_restart_strict_probe_runs_before_supervisor_constructed(
    client, auth_headers, monkeypatch, patch_heartbeat,
    patch_tmux_windows, patch_strict_probe,
):
    """Codex PR #2061 round 6 lifecycle: probe-failure path skips Supervisor().

    The route must NOT construct a transient writable Supervisor (which
    opens the legacy sqlite ``state.db`` and runs migrations as a
    side-effect) until after the strict safety probe passes. Otherwise
    every probe-failure 503 still pays the sqlite open/migrate cost,
    defeating the lifecycle cleanup.
    """
    patch_heartbeat({"operator": "2026-05-21T10:00:00Z"})
    patch_tmux_windows(["operator"])
    patch_strict_probe(raises=RuntimeError("tmux probe timeout"))

    # If ``_build_supervisor`` is called on the probe-failure path,
    # this will explode (the AssertionError propagates out of the
    # route as a 500, which is fail-LOUDER than the silent leak we
    # used to have).
    def _boom_build_supervisor(_config):
        raise AssertionError(
            "Supervisor MUST NOT be constructed on a probe-failure path — "
            "Codex PR #2061 round 6 lifecycle cleanup."
        )

    monkeypatch.setattr(
        sessions_admin_routes,
        "_build_supervisor",
        _boom_build_supervisor,
    )

    response = client.post(
        "/api/v1/sessions/operator/restart", headers=auth_headers,
    )
    # Probe failed → 503 unsafe_mid_turn_unknown (not a 500 from the
    # boom_build_supervisor sentinel).
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "unsafe_mid_turn_unknown"


def test_restart_strict_supervisor_closed_after_use(
    client, auth_headers, monkeypatch, patch_heartbeat,
    patch_tmux_windows, patch_strict_probe,
):
    """Per-request Supervisor is closed after the restart returns.

    Codex PR #2061 round 6 lifecycle note: ``Supervisor.__init__``
    opens the legacy sqlite ``state.db``; the route uses the
    supervisor for one or two public calls and then discards it. The
    route now explicitly calls ``supervisor.stop()`` in a ``finally``
    so the connection doesn't leak across restart calls.
    """
    patch_heartbeat({"operator": "2026-05-21T10:00:00Z"})
    patch_tmux_windows(["operator"])
    patch_strict_probe(mid_turn=False)

    class _CloseTrackingSupervisor(_FakeSupervisor):
        def __init__(self) -> None:
            super().__init__()
            self.stop_calls = 0

        def stop(self) -> None:
            self.stop_calls += 1

    sup = _CloseTrackingSupervisor()
    monkeypatch.setattr(
        sessions_admin_routes, "_build_supervisor", lambda _c: sup,
    )

    response = client.post(
        "/api/v1/sessions/operator/restart", headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert len(sup.restart_calls) == 1
    assert sup.stop_calls == 1, (
        "Supervisor.stop() must be called in finally to close the "
        "legacy sqlite state.db connection (Codex PR #2061 round 6 "
        "lifecycle note)."
    )


# ---------------------------------------------------------------------------
# POST /sessions/{name}/interrupt
# ---------------------------------------------------------------------------


def test_interrupt_sends_escape_to_configured_session(
    client, auth_headers, patch_interrupt_tmux,
):
    tmux = patch_interrupt_tmux(_FakeInterruptTmux([
        _fake_window("operator"),
    ]))
    response = client.post(
        "/api/v1/sessions/operator/interrupt", headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["message"] == "sent Escape to operator"
    assert tmux.list_calls == ["pollypm-test-storage-closet"]
    assert tmux.run_calls == [
        (("send-keys", "-t", "%9", "Escape"), {}),
    ]


def test_interrupt_allows_registered_task_worker_window(
    client, auth_headers, patch_interrupt_tmux,
):
    tmux = patch_interrupt_tmux(_FakeInterruptTmux([
        _fake_window("task-myproj-7"),
    ]))
    response = client.post(
        "/api/v1/sessions/task-myproj-7/interrupt", headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["message"] == "sent Escape to task-myproj-7"
    assert tmux.run_calls == [
        (("send-keys", "-t", "%9", "Escape"), {}),
    ]


def test_interrupt_returns_503_when_window_missing(
    client, auth_headers, patch_interrupt_tmux,
):
    patch_interrupt_tmux(_FakeInterruptTmux([]))
    response = client.post(
        "/api/v1/sessions/operator/interrupt", headers=auth_headers,
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "window_missing"


def test_interrupt_unknown_non_task_session_404(
    client, auth_headers, patch_interrupt_tmux,
):
    patch_interrupt_tmux(_FakeInterruptTmux([
        _fake_window("not-a-config-session"),
    ]))
    response = client.post(
        "/api/v1/sessions/not-a-config-session/interrupt",
        headers=auth_headers,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# POST /sessions/{name}/pause + resume
# ---------------------------------------------------------------------------


def test_pause_happy_path_then_visible_in_list(
    client, auth_headers, patch_heartbeat, patch_tmux_windows,
):
    """Pause sets ``paused=True`` but does NOT mutate ``status``.

    PR #2061 round 2: pause is informational only, so ``status``
    must continue to reflect runtime health. With a fresh heartbeat
    + present window the row is ``healthy`` AND ``paused=True``.
    See :func:`test_status_paused_but_missing_returns_health_classification`
    and :func:`test_status_paused_but_stale_returns_stale_with_paused_flag`
    for the regressions this round-2 contract prevents.
    """
    patch_heartbeat({"operator": "2026-05-21T10:00:00Z"})
    patch_tmux_windows(["operator"])

    response = client.post(
        "/api/v1/sessions/operator/pause", headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True

    # list reflects the paused state on the dedicated boolean field
    # — status stays as the runtime-health classification.
    listing = client.get("/api/v1/sessions", headers=auth_headers).json()
    by_name = {s["name"]: s for s in listing["sessions"]}
    assert by_name["operator"]["paused"] is True
    # status must be a real health value, not the legacy "paused" string.
    assert by_name["operator"]["status"] in {"healthy", "stale"}
    assert by_name["operator"]["status"] != "paused"


def test_status_paused_but_missing_returns_health_classification(
    client, auth_headers, patch_heartbeat, patch_tmux_windows,
):
    """Pausing a missing session must NOT mask the missing classification.

    PR #2061 round 2 regression: previously the route surfaced
    ``status="paused"`` for any tagged session, even when the tmux
    window was absent. Because the supervisor / recovery / dispatch
    loops do NOT consume the marker (#2068), the operator would see
    "paused" and assume the daemon had quiesced when in fact the
    session had simply gone missing. ``status`` must reflect runtime
    health regardless of the pause marker.
    """
    patch_heartbeat({"operator": "2026-05-21T10:00:00Z"})
    patch_tmux_windows([])  # tmux window absent → missing

    # Pause then list.
    pause = client.post(
        "/api/v1/sessions/operator/pause", headers=auth_headers,
    )
    assert pause.status_code == 200, pause.text
    listing = client.get("/api/v1/sessions", headers=auth_headers).json()
    row = next(s for s in listing["sessions"] if s["name"] == "operator")

    # The whole point of round 2: missing wins over the informational
    # pause tag.
    assert row["status"] == "missing"
    assert row["paused"] is True


def test_status_paused_but_stale_returns_stale_with_paused_flag(
    client, auth_headers, patch_heartbeat, patch_tmux_windows,
):
    """Pausing a stale session must NOT mask the stale classification.

    Same PR #2061 round 2 contract on the heartbeat-age side. A
    long-quiet session needs to be visible as ``stale`` so the
    operator knows the heartbeat has aged out, even if it has also
    been tagged informationally as paused.
    """
    # Heartbeat well past the 5-min stale threshold (>1 day old).
    patch_heartbeat({"operator": "2025-01-01T00:00:00Z"})
    patch_tmux_windows(["operator"])  # window present so we exercise stale, not missing

    pause = client.post(
        "/api/v1/sessions/operator/pause", headers=auth_headers,
    )
    assert pause.status_code == 200, pause.text
    listing = client.get("/api/v1/sessions", headers=auth_headers).json()
    row = next(s for s in listing["sessions"] if s["name"] == "operator")

    assert row["status"] == "stale"
    assert row["paused"] is True


def test_status_unpaused_session_reports_paused_false(
    client, auth_headers, patch_heartbeat, patch_tmux_windows,
):
    """Baseline: an un-tagged session reports ``paused=False``.

    Sanity-anchor for the round-2 contract — confirms the new
    boolean field defaults correctly when no pause marker exists.
    """
    patch_heartbeat({"operator": "2026-05-21T10:00:00Z"})
    patch_tmux_windows(["operator"])
    listing = client.get("/api/v1/sessions", headers=auth_headers).json()
    row = next(s for s in listing["sessions"] if s["name"] == "operator")
    assert row["paused"] is False
    assert row["status"] != "paused"


def test_pause_is_idempotent(
    client, auth_headers, patch_heartbeat, patch_tmux_windows,
):
    patch_heartbeat({})
    patch_tmux_windows(["operator"])
    first = client.post(
        "/api/v1/sessions/operator/pause", headers=auth_headers,
    )
    assert first.status_code == 200
    second = client.post(
        "/api/v1/sessions/operator/pause", headers=auth_headers,
    )
    assert second.status_code == 200
    body = second.json()
    assert body["ok"] is True
    msg = (body.get("message") or "").lower()
    assert "already" in msg


def test_pause_response_labels_marker_partial_enforcement(
    client, auth_headers, patch_heartbeat, patch_tmux_windows,
):
    """PR #2081: pause response must spell out PARTIAL enforcement.

    PR ``feat/sessions-pause-marker-wire-loops-1-3`` wired the marker
    into the recovery loops (``no_session_spawn``,
    ``Supervisor.maybe_recover_session``), but the remaining dispatch /
    cockpit / heartbeat loops still don't consume it (tracked under
    #2068). The response body must surface BOTH sides — which loops
    honor the marker and which don't — so an operator knows exactly
    what they have quiesced. "Informational only" used to be the
    contract; now the correct word is "honored by recovery loops",
    plus a NOT-yet caveat for the rest.
    """
    patch_heartbeat({})
    patch_tmux_windows(["operator"])
    response = client.post(
        "/api/v1/sessions/operator/pause", headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    msg = (body.get("message") or "").lower()
    # Recovery side: must positively call out that the loops HONOR the
    # marker (else operators still think it's a pure tag).
    assert "honored" in msg or "honor" in msg, msg
    assert "recovery" in msg, msg
    # Remaining-gaps side: must keep flagging what DOES NOT yet
    # consume the marker so operators don't over-trust pause.
    assert "do not" in msg or "does not" in msg or "not yet" in msg, msg
    # #2068 is the follow-up ticket — must be referenced so the gap is
    # discoverable from the response without grepping docs.
    assert "#2068" in msg or "2068" in msg, msg


def test_resume_response_labels_marker_partial_enforcement(
    client, auth_headers, patch_heartbeat, patch_tmux_windows,
):
    """Same PR #2081 contract on the resume side."""
    patch_heartbeat({})
    patch_tmux_windows(["operator"])
    # Pause then resume to hit the "clear" branch.
    client.post("/api/v1/sessions/operator/pause", headers=auth_headers)
    response = client.post(
        "/api/v1/sessions/operator/resume", headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    msg = (response.json().get("message") or "").lower()
    assert "honored" in msg or "honor" in msg, msg
    assert "recovery" in msg, msg
    assert "do not" in msg or "does not" in msg or "not yet" in msg, msg
    assert "#2068" in msg or "2068" in msg, msg


def test_resume_happy_path(
    client, auth_headers, patch_heartbeat, patch_tmux_windows,
):
    patch_heartbeat({})
    patch_tmux_windows(["operator"])
    client.post("/api/v1/sessions/operator/pause", headers=auth_headers)

    response = client.post(
        "/api/v1/sessions/operator/resume", headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True

    listing = client.get("/api/v1/sessions", headers=auth_headers).json()
    by_name = {s["name"]: s for s in listing["sessions"]}
    assert by_name["operator"]["paused"] is False
    assert by_name["operator"]["status"] != "paused"


def test_resume_is_idempotent_when_not_paused(
    client, auth_headers, patch_heartbeat, patch_tmux_windows,
):
    patch_heartbeat({})
    patch_tmux_windows(["operator"])
    response = client.post(
        "/api/v1/sessions/operator/resume", headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert "already" in (body.get("message") or "").lower()


def test_pause_404_unknown_session(client, auth_headers):
    response = client.post(
        "/api/v1/sessions/does-not-exist/pause", headers=auth_headers,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_pause_requires_auth(client):
    response = client.post("/api/v1/sessions/operator/pause")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# PR #2081 round 3 — finding 2: pause/resume MUST refuse to overwrite an
# unreadable marker
# ---------------------------------------------------------------------------


@pytest.fixture
def isolate_audit_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Redirect ``POLLYPM_AUDIT_HOME`` + reset the per-process marker
    transition state for the unreadable-marker regression tests.

    PR #2081 round 3 — the marker reader now routes diagnostics
    through :func:`pollypm.audit.log.emit`, which mirrors to a central
    tail under ``~/.pollypm/audit/`` by default. The unreadable-marker
    regression tests deliberately drive that path, so we pin the env
    var to a tmp dir to keep the user's real audit home untouched.

    We also reset the process-global ``_LAST_MARKER_KIND`` /
    ``_LAST_UNREADABLE_EMITTED`` bookkeeping so the throttle from a
    prior test in the same session cannot mask the audit-emit path
    this test relies on.
    """
    from pollypm.session_paused import _reset_skip_throttle_for_tests

    audit_home = tmp_path / "audit-home"
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))
    _reset_skip_throttle_for_tests()
    return audit_home


def test_pause_unreadable_marker_returns_503_and_preserves_file(
    client,
    auth_headers,
    config: PollyPMConfig,
    patch_heartbeat,
    patch_tmux_windows,
    isolate_audit_home: Path,
):
    """An unreadable marker must NOT be silently overwritten.

    Previously the pause endpoint went through ``load_paused_names``
    which collapses a corrupt marker to ``set()`` — combined with the
    read-modify-write logic, ``pause`` would write a fresh marker
    containing only the newly requested name and silently drop every
    other paused session. The recovery loops fail closed on the
    unreadable state, so the operator would see a successful 200 while
    the daemon stayed quiesced (and the original paused-session list
    was gone). Codex PR #2081 round 3 finding 2: refuse the mutation
    with a typed 503 and leave the marker untouched so the operator
    can repair it manually.
    """
    patch_heartbeat({})
    patch_tmux_windows(["operator"])

    # Plant a corrupt marker that ``load_paused_state`` will classify
    # as ``unreadable``.
    marker = config.project.base_dir / "paused-sessions.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    corrupt_bytes = b"{not json - someone else's paused list lives here}"
    marker.write_bytes(corrupt_bytes)

    response = client.post(
        "/api/v1/sessions/operator/pause", headers=auth_headers,
    )
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["error"]["code"] == "marker_unreadable", body
    # Message names the marker path + reason so the operator knows
    # WHERE to look and WHY we refused.
    assert "paused-sessions.json" in body["error"]["message"]
    assert "unreadable" in body["error"]["message"].lower()
    # Hint must tell the operator the recovery loops are still
    # quiesced and they must repair manually.
    hint = (body["error"].get("hint") or "").lower()
    assert "repair" in hint or "delete" in hint

    # CRITICAL: the corrupt marker must be untouched. A regression that
    # falls back to the empty-set reader would have overwritten this
    # file with ``["operator"]``.
    assert marker.read_bytes() == corrupt_bytes


def test_resume_unreadable_marker_returns_503_and_preserves_file(
    client,
    auth_headers,
    config: PollyPMConfig,
    patch_heartbeat,
    patch_tmux_windows,
    isolate_audit_home: Path,
):
    """Resume against a corrupt marker must NOT return ``200 already untagged``.

    Same finding-2 case on the resume side: the best-effort reader
    would collapse to an empty set, the ``name not in names`` branch
    would fire, and the response would be a cheerful 200 while the
    recovery loops stayed quiesced (failing closed on the unreadable
    state). The operator would walk away thinking they'd lifted the
    pause when in fact NOTHING changed.

    The fix: refuse with a typed 503 ``marker_unreadable`` and leave
    the marker file untouched.
    """
    patch_heartbeat({})
    patch_tmux_windows(["operator"])

    marker = config.project.base_dir / "paused-sessions.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    corrupt_bytes = b"not even close to JSON"
    marker.write_bytes(corrupt_bytes)

    response = client.post(
        "/api/v1/sessions/operator/resume", headers=auth_headers,
    )
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["error"]["code"] == "marker_unreadable", body
    assert "paused-sessions.json" in body["error"]["message"]

    # Marker file must be unchanged.
    assert marker.read_bytes() == corrupt_bytes


def test_pause_wrong_shape_marker_returns_503(
    client,
    auth_headers,
    config: PollyPMConfig,
    patch_heartbeat,
    patch_tmux_windows,
    isolate_audit_home: Path,
):
    """A JSON document that parses but is not a list also collapses
    to ``unreadable`` — the pause endpoint must still refuse with a
    503 rather than overwrite the operator's intent.

    The original ad-hoc reader treated any non-list as "no sessions
    paused"; the discriminated state reader now flags it ``unreadable``
    so the route fails closed alongside corrupt-JSON cases.
    """
    patch_heartbeat({})
    patch_tmux_windows(["operator"])

    marker = config.project.base_dir / "paused-sessions.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    wrong_shape = b'{"paused": ["someone-else"]}'
    marker.write_bytes(wrong_shape)

    response = client.post(
        "/api/v1/sessions/operator/pause", headers=auth_headers,
    )
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "marker_unreadable"
    # File preserved — operator can still recover the intent.
    assert marker.read_bytes() == wrong_shape
