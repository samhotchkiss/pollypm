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

    def _get_session_runtime(self, name: str) -> Any:  # noqa: ARG002
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


# ---------------------------------------------------------------------------
# POST /sessions/{name}/restart
# ---------------------------------------------------------------------------


def test_restart_routes_through_supervisor_facade(
    client, auth_headers, patch_heartbeat, patch_tmux_windows,
    patch_tmux_service, patch_supervisor,
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
    svc = patch_tmux_service(_FakeTmuxService(turn_active=False))
    sup = patch_supervisor(_FakeSupervisor())
    response = client.post(
        "/api/v1/sessions/operator/restart", headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert "operator" in (body.get("message") or "")
    # The route did NOT poke the raw destroy/create helpers — the
    # facade owns that pair.
    assert svc.calls == []
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
    patch_tmux_service, patch_supervisor,
):
    """When the runtime carries an ``effective_account`` it wins.

    A session that previously failed over to a recovery account
    should keep using that account on operator-driven restart —
    otherwise the relaunch slams the original (failed) account and
    immediately re-triggers the same recovery loop.
    """
    patch_heartbeat({"operator": "2026-05-21T10:00:00Z"})
    patch_tmux_windows(["operator"])
    patch_tmux_service(_FakeTmuxService(turn_active=False))
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
    patch_tmux_service, patch_supervisor,
):
    """``Supervisor.restart_session`` raising → 503, not 500."""
    patch_heartbeat({"operator": "2026-05-21T10:00:00Z"})
    patch_tmux_windows(["operator"])
    patch_tmux_service(_FakeTmuxService(turn_active=False))
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
    patch_tmux_service, patch_supervisor,
):
    patch_heartbeat({"operator": "2026-05-21T10:00:00Z"})
    patch_tmux_windows(["operator"])
    svc = patch_tmux_service(_FakeTmuxService(turn_active=True))
    sup = patch_supervisor(_FakeSupervisor())
    response = client.post(
        "/api/v1/sessions/operator/restart", headers=auth_headers,
    )
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "unsafe_mid_turn"
    # neither the raw service nor the facade was touched
    assert svc.calls == []
    assert sup.restart_calls == []


def test_restart_strict_fail_closed_when_probe_raises(
    client, auth_headers, patch_heartbeat, patch_tmux_windows,
    patch_tmux_service, patch_supervisor,
):
    """Strict-mode probe failure → 503 ``unsafe_mid_turn_unknown``.

    PR #2061 P0 #2 regression: the previous ``_is_turn_active``
    swallowed every exception and returned ``False``, so a transient
    tmux/parse failure looked like "agent is idle" and the route
    happily destroyed an actively-working agent. Strict mode (the
    default) must fail *closed* — the safety gate refuses the restart
    when it cannot evaluate the mid-turn signal.
    """
    patch_heartbeat({"operator": "2026-05-21T10:00:00Z"})
    patch_tmux_windows(["operator"])
    svc = patch_tmux_service(
        _FakeTmuxService(turn_active_raises=RuntimeError("tmux probe timeout")),
    )
    sup = patch_supervisor(_FakeSupervisor())
    response = client.post(
        "/api/v1/sessions/operator/restart", headers=auth_headers,
    )
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["error"]["code"] == "unsafe_mid_turn_unknown"
    # The destructive path was NEVER reached.
    assert svc.calls == []
    assert sup.restart_calls == []


def test_restart_force_bypasses_safety_probe_entirely(
    client, auth_headers, patch_heartbeat, patch_tmux_windows,
    patch_tmux_service, patch_supervisor,
):
    """``?safety=force`` skips the probe even when it would raise.

    Operator-escalation override: force is destructive *by design*,
    so a flaky probe must not block it.
    """
    patch_heartbeat({"operator": "2026-05-21T10:00:00Z"})
    patch_tmux_windows(["operator"])
    patch_tmux_service(
        _FakeTmuxService(turn_active_raises=RuntimeError("tmux probe timeout")),
    )
    sup = patch_supervisor(_FakeSupervisor())
    response = client.post(
        "/api/v1/sessions/operator/restart?safety=force",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert len(sup.restart_calls) == 1


def test_restart_force_overrides_mid_turn(
    client, auth_headers, patch_heartbeat, patch_tmux_windows,
    patch_tmux_service, patch_supervisor,
):
    patch_heartbeat({"operator": "2026-05-21T10:00:00Z"})
    patch_tmux_windows(["operator"])
    patch_tmux_service(_FakeTmuxService(turn_active=True))
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
    patch_tmux_service, patch_supervisor,
):
    patch_heartbeat({})
    patch_tmux_windows([])
    patch_tmux_service(_FakeTmuxService())
    patch_supervisor(_FakeSupervisor())
    response = client.post(
        "/api/v1/sessions/nope/restart", headers=auth_headers,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_restart_503_when_tmux_service_unavailable(
    client, auth_headers, patch_heartbeat, patch_tmux_windows,
    patch_tmux_service, patch_supervisor,
):
    patch_heartbeat({})
    patch_tmux_windows([])
    patch_tmux_service(None)  # tmux service construction failed
    patch_supervisor(_FakeSupervisor())
    response = client.post(
        "/api/v1/sessions/operator/restart", headers=auth_headers,
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "daemon_unavailable"


def test_restart_503_when_supervisor_unavailable(
    client, auth_headers, patch_heartbeat, patch_tmux_windows,
    patch_tmux_service, patch_supervisor,
):
    """Supervisor construction failure → 503 ``daemon_unavailable``."""
    patch_heartbeat({})
    patch_tmux_windows([])
    patch_tmux_service(_FakeTmuxService(turn_active=False))
    patch_supervisor(None)  # Supervisor() failed
    response = client.post(
        "/api/v1/sessions/operator/restart", headers=auth_headers,
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "daemon_unavailable"


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


def test_pause_response_labels_marker_informational(
    client, auth_headers, patch_heartbeat, patch_tmux_windows,
):
    """PR #2061 P0 #3: pause must NOT imply daemon-side enforcement.

    The supervisor / recovery / dispatch loops don't consume the
    pause marker yet. The response body must surface that fact so an
    operator knows they have *tagged* the session, not quiesced the
    daemon. Until the loops learn to consult the marker, this is the
    contract we keep — narrow the API rather than mislead.
    """
    patch_heartbeat({})
    patch_tmux_windows(["operator"])
    response = client.post(
        "/api/v1/sessions/operator/pause", headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    msg = (body.get("message") or "").lower()
    assert "informational" in msg, msg
    assert "do not" in msg or "does not" in msg or "not yet" in msg, msg


def test_resume_response_labels_marker_informational(
    client, auth_headers, patch_heartbeat, patch_tmux_windows,
):
    """Same P0 #3 contract on the resume side."""
    patch_heartbeat({})
    patch_tmux_windows(["operator"])
    # Pause then resume to hit the "clear" branch.
    client.post("/api/v1/sessions/operator/pause", headers=auth_headers)
    response = client.post(
        "/api/v1/sessions/operator/resume", headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    msg = (response.json().get("message") or "").lower()
    assert "informational" in msg, msg


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
