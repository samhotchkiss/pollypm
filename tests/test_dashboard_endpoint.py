"""Integration tests for ``GET /api/v1/dashboard`` (Phase 2 §3.1).

Mirrors the test style of ``tests/test_chat_messages_endpoint.py``:
FastAPI ``TestClient`` against an in-process app, with the heavy
``pollypm.dashboard_data.gather`` call monkeypatched so the suite
never touches a live pg pool or tmux server.

Run with ``pytest --noconftest tests/test_dashboard_endpoint.py -v
--timeout=120`` — the per-project conftest requires a Postgres
harness which these tests intentionally avoid.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pollypm.config import (
    AccountConfig,
    MemorySettings,
    PollyPMConfig,
    PollyPMSettings,
    ProjectSettings,
)
from pollypm.dashboard_data import (
    AccountQuotaUsage,
    CommitInfo,
    CompletedItem,
    DashboardData,
    InboxPreview,
    SessionActivity,
)
from pollypm.models import KnownProject, ProjectKind, ProviderKind, RuntimeKind
from pollypm.web_api import create_app, ensure_token
from pollypm.web_api.routes import dashboard as dashboard_routes


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
def other_project_root(tmp_path: Path) -> Path:
    root = tmp_path / "otherproj"
    root.mkdir()
    (root / ".pollypm").mkdir()
    return root


@pytest.fixture
def config(
    workspace: Path, project_root: Path, other_project_root: Path,
) -> PollyPMConfig:
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
            "otherproj": KnownProject(
                key="otherproj",
                path=other_project_root,
                name="Other Project",
                tracked=False,
                kind=ProjectKind.GIT,
            ),
        },
        memory=MemorySettings(backend="file"),
    )


@pytest.fixture
def empty_config(workspace: Path) -> PollyPMConfig:
    """Config with no projects + no accounts (spec §3.3 empty-config case)."""
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
        projects={},
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


@pytest.fixture(autouse=True)
def patch_fresh_alert_count(monkeypatch: pytest.MonkeyPatch):
    """Keep dashboard endpoint tests isolated from live alert storage."""
    monkeypatch.setattr(
        dashboard_routes, "_fresh_dashboard_alert_count", lambda _config: None,
    )
    monkeypatch.setattr(
        dashboard_routes,
        "_fresh_dashboard_project_alerts",
        lambda _config, _project: None,
    )


# ---------------------------------------------------------------------------
# Stub helpers
# ---------------------------------------------------------------------------


def _make_data(
    *,
    active_sessions: list[SessionActivity] | None = None,
    recent_commits: list[CommitInfo] | None = None,
    completed_items: list[CompletedItem] | None = None,
    recent_messages: list[InboxPreview] | None = None,
    daily_tokens: list[tuple[str, int]] | None = None,
    today_tokens: int = 0,
    total_tokens: int = 0,
    sweep_count_24h: int = 0,
    message_count_24h: int = 0,
    recovery_count_24h: int = 0,
    inbox_count: int = 0,
    alert_count: int = 0,
    alert_counts_by_project: dict[str, int] | None = None,
    account_usages: list[AccountQuotaUsage] | None = None,
    briefing: str = "",
) -> DashboardData:
    return DashboardData(
        active_sessions=active_sessions or [],
        recent_commits=recent_commits or [],
        completed_items=completed_items or [],
        recent_messages=recent_messages or [],
        daily_tokens=daily_tokens or [],
        today_tokens=today_tokens,
        total_tokens=total_tokens,
        sweep_count_24h=sweep_count_24h,
        message_count_24h=message_count_24h,
        recovery_count_24h=recovery_count_24h,
        inbox_count=inbox_count,
        alert_count=alert_count,
        alert_counts_by_project=alert_counts_by_project or {},
        account_usages=account_usages or [],
        briefing=briefing,
    )


@pytest.fixture
def patch_gather(monkeypatch: pytest.MonkeyPatch):
    """Factory that swaps ``_gather_dashboard`` for a stub."""

    def install(data: DashboardData) -> None:
        monkeypatch.setattr(
            dashboard_routes, "_gather_dashboard",
            lambda _config: data,
        )
    return install


@pytest.fixture
def patch_list_projects(monkeypatch: pytest.MonkeyPatch):
    """Factory that stubs ``list_projects`` so tests don't open a work-service.

    The real :func:`pollypm.web_api.service.list_projects` tries to open
    a work-service per project to gather state counts; without pg this
    raises immediately and falls back to empty counts but spams logs.
    Tests can install a deterministic project list via this fixture.
    """

    def install(projects: list[object]) -> None:
        monkeypatch.setattr(
            dashboard_routes, "list_projects",
            lambda _config, **_kw: list(projects),
        )
    return install


def _api_project(
    key: str,
    *,
    tracked: bool = True,
    open_inbox_count: int = 0,
    pending_plan_review: bool = False,
    name: str | None = None,
    path: str = "/tmp/proj",
) -> object:
    # Use the real Project pydantic model so list_projects -> response
    # round-trips correctly.
    from pollypm.web_api.models import Project

    return Project(
        key=key,
        name=name or key,
        path=path,
        tracked=tracked,
        kind="git",
        persona_name=None,
        state=None,
        glyph="ok",
        task_counts={},
        open_inbox_count=open_inbox_count,
        pending_plan_review=pending_plan_review,
    )


def test_list_projects_uses_pg_bulk_task_snapshot(
    config: PollyPMConfig, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dashboard project rows should not open the work service per project."""
    from types import SimpleNamespace

    from pollypm.inbox.kind import InboxItemKind
    from pollypm.web_api import service as api_service

    real_inbox_task = SimpleNamespace(
        project="myproj",
        task_id="myproj/2",
        work_status="queued",
        flow_template_id="chat",
        labels=[],
        roles={"requester": "user", "operator": "polly"},
        kind="legacy",
    )
    stale_watchdog_task = SimpleNamespace(
        project="myproj",
        task_id="myproj/198",
        work_status="queued",
        flow_template_id="chat",
        labels=["notify", "watchdog", "notify_message:123"],
        roles={"requester": "user", "operator": "user"},
        kind=InboxItemKind.WATCHDOG_OPERATOR_DISPATCH.value,
    )
    grouped = {
        "myproj": [
            SimpleNamespace(
                project="myproj",
                task_id="myproj/1",
                work_status="review",
                flow_template_id="plan_review",
                labels=[],
                roles={},
                kind="legacy",
            ),
            real_inbox_task,
            stale_watchdog_task,
        ],
        "otherproj": [],
    }
    inbox_grouped = {
        "myproj": [real_inbox_task, stale_watchdog_task],
        "otherproj": [],
    }

    monkeypatch.setattr(
        "pollypm.storage._backend_dispatch.is_pg_backend",
        lambda _config: True,
    )
    monkeypatch.setattr(
        "pollypm.cockpit_pg_aggregates._all_tasks_grouped_uncached",
        lambda _config: grouped,
    )
    monkeypatch.setattr(
        "pollypm.cockpit_pg_aggregates.all_tasks_for_project",
        lambda rows, _config, key: list(rows.get(key, [])),
    )
    monkeypatch.setattr(
        "pollypm.cockpit_pg_aggregates.inbox_tasks_grouped",
        lambda _config: inbox_grouped,
    )
    monkeypatch.setattr(
        api_service,
        "_open_work_service_readonly",
        lambda **_kw: pytest.fail(
            "pg project list should use one bulk task snapshot"
        ),
    )

    items = api_service.list_projects(config)

    by_key = {item.key: item for item in items}
    assert by_key["myproj"].pending_plan_review is True
    assert by_key["myproj"].open_inbox_count == 1
    assert by_key["myproj"].task_counts["review"] == 1


def test_dashboard_loads_projects_and_gather_concurrently(
    client, auth_headers, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_started = threading.Event()
    gather_started = threading.Event()

    def fake_list_projects(_config, *, operator_facing: bool = False):
        assert operator_facing is True
        project_started.set()
        assert gather_started.wait(1.0)
        return [_api_project("myproj")]

    def fake_gather(_config):
        gather_started.set()
        assert project_started.wait(1.0)
        return _make_data()

    monkeypatch.setattr(dashboard_routes, "list_projects", fake_list_projects)
    monkeypatch.setattr(dashboard_routes, "_gather_dashboard", fake_gather)

    response = client.get("/api/v1/dashboard", headers=auth_headers)

    assert response.status_code == 200, response.text
    assert project_started.is_set()
    assert gather_started.is_set()


def test_dashboard_route_gather_bypasses_state_cache(
    config: PollyPMConfig, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_gather(_config, _store, *, use_state_cache):
        seen["use_state_cache"] = use_state_cache
        return _make_data()

    monkeypatch.setattr(
        "pollypm.dashboard_data.gather",
        fake_gather,
    )

    dashboard_routes._gather_dashboard(config)

    assert seen == {"use_state_cache": False}


def test_dashboard_cache_hit_skips_repeated_expensive_loads(
    client, auth_headers, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"list": 0, "gather": 0}

    def fake_list_projects(_config, *, operator_facing: bool = False):
        assert operator_facing is True
        calls["list"] += 1
        return [_api_project("myproj")]

    def fake_gather(_config):
        calls["gather"] += 1
        return _make_data(total_tokens=42)

    monkeypatch.setattr(dashboard_routes, "list_projects", fake_list_projects)
    monkeypatch.setattr(dashboard_routes, "_gather_dashboard", fake_gather)

    first = client.get("/api/v1/dashboard", headers=auth_headers)
    second = client.get("/api/v1/dashboard", headers=auth_headers)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["tokens"]["total"] == 42
    assert calls == {"list": 1, "gather": 1}


def test_dashboard_cold_refresh_normalizes_cached_alert_count(
    client, auth_headers, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"gather": 0, "fresh": 0}

    monkeypatch.setattr(
        dashboard_routes,
        "list_projects",
        lambda _config, *, operator_facing=False: [_api_project("myproj")],
    )

    def fake_gather(_config):
        calls["gather"] += 1
        return _make_data(alert_count=32)

    def fake_fresh(_config):
        calls["fresh"] += 1
        return 3

    monkeypatch.setattr(dashboard_routes, "_gather_dashboard", fake_gather)
    monkeypatch.setattr(
        dashboard_routes, "_fresh_dashboard_alert_count", fake_fresh,
    )

    first = client.get("/api/v1/dashboard", headers=auth_headers)
    second = client.get("/api/v1/dashboard", headers=auth_headers)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["rollups"]["alert_count"] == 3
    assert second.json()["rollups"]["alert_count"] == 3
    assert calls == {"gather": 1, "fresh": 1}


def test_dashboard_cache_miss_response_uses_snapshot_normalized_alert_count(
    app, client, auth_headers, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"load": 0}

    def fake_load_dashboard_snapshot(_config):
        calls["load"] += 1
        return dashboard_routes.DashboardSnapshot(
            generated_at=datetime.now(timezone.utc),
            projects=(_api_project("myproj"),),
            data=_make_data(alert_count=29),
            refreshed_at_monotonic=time.monotonic(),
            normalized_alert_count=3,
        )

    monkeypatch.setattr(
        dashboard_routes,
        "_load_dashboard_snapshot",
        fake_load_dashboard_snapshot,
    )

    first = client.get("/api/v1/dashboard", headers=auth_headers)
    second = client.get("/api/v1/dashboard", headers=auth_headers)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["rollups"]["alert_count"] == 3
    assert second.json()["rollups"]["alert_count"] == 3
    assert calls == {"load": 1}


def test_dashboard_background_refresh_caches_normalized_alert_count(
    app, client, auth_headers, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app.state.dashboard_snapshot_cache = dashboard_routes.DashboardSnapshotCache(
        stale_after_seconds=0.01,
        max_stale_seconds=5.0,
    )
    calls = {"gather": 0, "fresh": 0}
    refresh_started = threading.Event()
    release_refresh = threading.Event()

    monkeypatch.setattr(
        dashboard_routes,
        "list_projects",
        lambda _config, *, operator_facing=False: [_api_project("myproj")],
    )

    def fake_gather(_config):
        calls["gather"] += 1
        if calls["gather"] == 1:
            return _make_data(alert_count=3, total_tokens=1)
        refresh_started.set()
        assert release_refresh.wait(2.0)
        return _make_data(alert_count=31, total_tokens=2)

    def fake_fresh(_config):
        calls["fresh"] += 1
        return 3

    monkeypatch.setattr(dashboard_routes, "_gather_dashboard", fake_gather)
    monkeypatch.setattr(
        dashboard_routes, "_fresh_dashboard_alert_count", fake_fresh,
    )

    first = client.get("/api/v1/dashboard", headers=auth_headers)
    assert first.status_code == 200, first.text
    assert first.json()["rollups"]["alert_count"] == 3

    time.sleep(0.03)
    second = client.get("/api/v1/dashboard", headers=auth_headers)
    assert second.status_code == 200, second.text
    assert second.json()["tokens"]["total"] == 1
    assert second.json()["rollups"]["alert_count"] == 3
    assert refresh_started.wait(1.0)

    release_refresh.set()
    deadline = time.monotonic() + 1.0
    body = second.json()
    while time.monotonic() < deadline:
        third = client.get("/api/v1/dashboard", headers=auth_headers)
        assert third.status_code == 200, third.text
        body = third.json()
        if body["tokens"]["total"] == 2:
            break
        time.sleep(0.02)

    assert body["tokens"]["total"] == 2
    assert body["rollups"]["alert_count"] == 3
    assert calls == {"gather": 2, "fresh": 2}


def test_dashboard_stale_cache_returns_while_refresh_runs(
    app, client, auth_headers, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app.state.dashboard_snapshot_cache = dashboard_routes.DashboardSnapshotCache(
        stale_after_seconds=0.01,
    )
    gather_calls = 0
    refresh_started = threading.Event()
    release_refresh = threading.Event()

    def fake_list_projects(_config, *, operator_facing: bool = False):
        assert operator_facing is True
        return [_api_project("myproj")]

    def fake_gather(_config):
        nonlocal gather_calls
        gather_calls += 1
        if gather_calls == 1:
            return _make_data(total_tokens=1)
        refresh_started.set()
        assert release_refresh.wait(2.0)
        return _make_data(total_tokens=2)

    monkeypatch.setattr(dashboard_routes, "list_projects", fake_list_projects)
    monkeypatch.setattr(dashboard_routes, "_gather_dashboard", fake_gather)

    first = client.get("/api/v1/dashboard", headers=auth_headers)
    assert first.status_code == 200, first.text
    assert first.json()["tokens"]["total"] == 1

    time.sleep(0.03)
    started_at = time.monotonic()
    second = client.get("/api/v1/dashboard", headers=auth_headers)
    elapsed = time.monotonic() - started_at

    assert second.status_code == 200, second.text
    assert second.json()["tokens"]["total"] == 1
    assert elapsed < 0.5
    assert refresh_started.wait(1.0)

    release_refresh.set()
    deadline = time.monotonic() + 2.0
    body = second.json()
    while time.monotonic() < deadline:
        third = client.get("/api/v1/dashboard", headers=auth_headers)
        assert third.status_code == 200, third.text
        body = third.json()
        if body["tokens"]["total"] == 2:
            break
        time.sleep(0.02)
    assert body["tokens"]["total"] == 2


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_dashboard_returns_expected_shape(
    client, auth_headers, patch_gather, patch_list_projects,
):
    patch_list_projects([
        _api_project("myproj", open_inbox_count=3, pending_plan_review=True),
        _api_project("otherproj", tracked=False, open_inbox_count=1),
    ])
    patch_gather(_make_data(
        active_sessions=[
            SessionActivity(
                name="operator", role="operator", project="myproj",
                project_label="My Project", status="running",
                description="thinking", age_seconds=12.5,
            ),
        ],
        recent_commits=[
            CommitInfo(
                hash="abc1234", message="fix: thing", author="sam",
                age_seconds=300.0, project="myproj",
            ),
        ],
        today_tokens=1234,
        total_tokens=98765,
        alert_count=2,
        sweep_count_24h=5,
        message_count_24h=7,
        recovery_count_24h=1,
    ))

    response = client.get("/api/v1/dashboard", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()

    # Envelope keys present
    assert set(body.keys()) >= {
        "generated_at", "daemon_status", "projects", "rollups",
        "active_sessions", "recent_commits", "completed_items",
        "recent_messages", "tokens", "account_usages",
    }
    # Projects passthrough
    assert [p["key"] for p in body["projects"]] == ["myproj", "otherproj"]
    # Rollups derived from project view
    rollups = body["rollups"]
    assert rollups["tracked_count"] == 1  # only myproj is tracked
    assert rollups["open_inbox_count"] == 4
    assert rollups["pending_plan_reviews"] == 1
    assert rollups["alert_count"] == 2
    assert rollups["sweep_count_24h"] == 5
    assert rollups["message_count_24h"] == 7
    assert rollups["recovery_count_24h"] == 1
    # Sessions / commits passthrough
    assert body["active_sessions"][0]["name"] == "operator"
    assert body["recent_commits"][0]["hash"] == "abc1234"
    # Tokens
    assert body["tokens"] == {"today": 1234, "total": 98765}
    # Daemon status driven by sessions presence
    assert body["daemon_status"] == "up"
    # Large token history remains opt-in; the compact briefing is on by default.
    assert body.get("daily_tokens") is None
    assert body.get("briefing") == ""


def test_dashboard_daemon_down_when_no_active_sessions(
    client, auth_headers, patch_gather, patch_list_projects,
):
    # Spec §3.3: daemon-down is a normal mode — 200 with daemon_status="down"
    patch_list_projects([_api_project("myproj")])
    patch_gather(_make_data(active_sessions=[]))
    response = client.get("/api/v1/dashboard", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["daemon_status"] == "down"
    assert body["active_sessions"] == []


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_dashboard_requires_bearer(client, patch_gather, patch_list_projects):
    patch_list_projects([])
    patch_gather(_make_data())
    response = client.get("/api/v1/dashboard")
    assert response.status_code == 401
    assert response.json()["error"]["code"] in {"unauthorized", "invalid_token"}


def test_dashboard_rejects_wrong_token(client, patch_gather, patch_list_projects):
    patch_list_projects([])
    patch_gather(_make_data())
    response = client.get(
        "/api/v1/dashboard",
        headers={"Authorization": "Bearer bogus-token"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


# ---------------------------------------------------------------------------
# Optional filters
# ---------------------------------------------------------------------------


def test_dashboard_project_filter_narrows_view(
    client, auth_headers, patch_gather, patch_list_projects,
):
    patch_list_projects([
        _api_project("myproj", open_inbox_count=3),
        _api_project("otherproj", open_inbox_count=10),
    ])
    patch_gather(_make_data(
        recent_commits=[
            CommitInfo(hash="a", message="m1", author="s", age_seconds=1, project="myproj"),
            CommitInfo(hash="b", message="m2", author="s", age_seconds=1, project="otherproj"),
        ],
        recent_messages=[
            InboxPreview(sender="x", title="hi", project="otherproj",
                         task_id="otherproj/1", age_seconds=1),
        ],
    ))

    body = client.get(
        "/api/v1/dashboard?project=myproj", headers=auth_headers,
    ).json()
    assert [p["key"] for p in body["projects"]] == ["myproj"]
    # Cross-project rows filtered out
    assert [c["hash"] for c in body["recent_commits"]] == ["a"]
    assert body["recent_messages"] == []
    # Rollups follow the narrowed project view
    assert body["rollups"]["open_inbox_count"] == 3


def test_dashboard_unknown_project_returns_404(
    client, auth_headers, patch_gather, patch_list_projects,
):
    patch_list_projects([_api_project("myproj")])
    patch_gather(_make_data())
    response = client.get(
        "/api/v1/dashboard?project=missing", headers=auth_headers,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_dashboard_include_token_history(
    client, auth_headers, patch_gather, patch_list_projects,
):
    patch_list_projects([_api_project("myproj")])
    patch_gather(_make_data(
        daily_tokens=[("2026-05-20", 100), ("2026-05-21", 200)],
        today_tokens=200,
    ))
    body = client.get(
        "/api/v1/dashboard?include_token_history=true", headers=auth_headers,
    ).json()
    assert body["daily_tokens"] == [["2026-05-20", 100], ["2026-05-21", 200]]


def test_dashboard_include_briefing(
    client, auth_headers, patch_gather, patch_list_projects,
):
    patch_list_projects([_api_project("myproj")])
    patch_gather(_make_data(briefing="Last 24 hours: 3 commits."))
    body = client.get(
        "/api/v1/dashboard?include_briefing=true", headers=auth_headers,
    ).json()
    assert body["briefing"] == "Last 24 hours: 3 commits."


def test_dashboard_can_omit_briefing(
    client, auth_headers, patch_gather, patch_list_projects,
):
    patch_list_projects([_api_project("myproj")])
    patch_gather(_make_data(briefing="Morning. All handled."))
    body = client.get(
        "/api/v1/dashboard?include_briefing=false", headers=auth_headers,
    ).json()
    assert body["briefing"] is None


# ---------------------------------------------------------------------------
# Empty / edge cases
# ---------------------------------------------------------------------------


def test_dashboard_empty_config(
    empty_config, token, patch_gather, patch_list_projects,
):
    # Spec §3.3-aligned: a workspace with no projects should still
    # return a well-formed 200 response.
    token_path, value = token
    app = create_app(config=empty_config, token_path=token_path)
    client = TestClient(app)
    patch_list_projects([])
    patch_gather(_make_data())
    response = client.get(
        "/api/v1/dashboard",
        headers={"Authorization": f"Bearer {value}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["projects"] == []
    assert body["active_sessions"] == []
    assert body["rollups"]["tracked_count"] == 0
    assert body["rollups"]["open_inbox_count"] == 0
    assert body["rollups"]["pending_plan_reviews"] == 0
    assert body["daemon_status"] == "down"


# ---------------------------------------------------------------------------
# Service-unavailable handling
# ---------------------------------------------------------------------------


def test_dashboard_503_when_gather_raises(
    client, auth_headers, patch_list_projects, monkeypatch,
):
    patch_list_projects([_api_project("myproj")])

    def _boom(_config):
        raise RuntimeError("pg pool down")

    monkeypatch.setattr(dashboard_routes, "_gather_dashboard", _boom)
    response = client.get("/api/v1/dashboard", headers=auth_headers)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"


def test_dashboard_503_when_list_projects_raises(
    client, auth_headers, patch_gather, monkeypatch,
):
    def _boom(_config, **_kw):
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(dashboard_routes, "list_projects", _boom)
    patch_gather(_make_data())
    response = client.get("/api/v1/dashboard", headers=auth_headers)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"


# ---------------------------------------------------------------------------
# Account usage passthrough
# ---------------------------------------------------------------------------


def test_dashboard_project_filter_scopes_alert_rollup_and_keeps_global_activity_rollups(
    client, auth_headers, patch_gather, patch_list_projects,
):
    """Project filters scope project-owned alerts but keep 24h activity global.

    Under ``?project=myproj`` the response must NOT silently inherit
    cross-project alert counts as if they were narrowed to ``myproj``.
    ``alert_count`` now follows the selected project while the 24h
    sweep / message / recovery counters remain whole-system.
    """
    patch_list_projects([
        _api_project("myproj", open_inbox_count=3, pending_plan_review=True),
        _api_project("otherproj", tracked=False, open_inbox_count=10),
    ])
    # The dashboard data here represents the WHOLE system; sessions /
    # commits / inbox previews for "otherproj" are part of the global
    # picture, and the 24h activity rollups below were computed across
    # both projects.
    patch_gather(_make_data(
        active_sessions=[
            SessionActivity(
                name="otherproj-worker", role="worker", project="otherproj",
                project_label="Other Project", status="running",
                description="other work", age_seconds=5.0,
            ),
        ],
        recent_commits=[
            CommitInfo(hash="a", message="m1", author="s",
                       age_seconds=1, project="otherproj"),
        ],
        alert_count=6,
        alert_counts_by_project={"myproj": 2, "otherproj": 4},
        sweep_count_24h=12,
        message_count_24h=4,
        recovery_count_24h=2,
    ))

    body = client.get(
        "/api/v1/dashboard?project=myproj", headers=auth_headers,
    ).json()

    # Project-derived rollups follow the narrowed projects view.
    rollups = body["rollups"]
    assert rollups["tracked_count"] == 1
    assert rollups["open_inbox_count"] == 3
    assert rollups["pending_plan_reviews"] == 1
    assert rollups["alert_count"] == 2
    # Global 24h activity rollups are surfaced unchanged (documented
    # as not-narrowed via the ``scoped_fields`` contract below).
    assert rollups["sweep_count_24h"] == 12
    assert rollups["message_count_24h"] == 4
    assert rollups["recovery_count_24h"] == 2

    # Contract — the response enumerates exactly which fields the
    # filter narrowed. The 24h global activity counters MUST NOT
    # appear here; the project-derived rollups MUST.
    scoped = set(body["scoped_fields"])
    assert "rollups.tracked_count" in scoped
    assert "rollups.open_inbox_count" in scoped
    assert "rollups.pending_plan_reviews" in scoped
    assert "rollups.alert_count" in scoped
    assert "rollups.sweep_count_24h" not in scoped
    assert "rollups.message_count_24h" not in scoped
    assert "rollups.recovery_count_24h" not in scoped


def test_dashboard_project_filter_uses_fresh_project_alerts(
    client, auth_headers, patch_gather, patch_list_projects, monkeypatch,
):
    """A selected project with only plan_missing alerts must not look clean."""
    patch_list_projects([
        _api_project("myproj"),
        _api_project("otherproj"),
    ])
    patch_gather(_make_data(
        alert_count=6,
        alert_counts_by_project={"myproj": 0},
    ))
    alert = SimpleNamespace(
        session_name="plan_gate-myproj",
        alert_type="plan_missing",
        severity="warn",
        message=(
            "Project 'myproj' has no approved plan yet - queued task "
            "myproj/1 is waiting. Run `pm project plan myproj`."
        ),
        updated_at="2026-06-03T15:00:00+00:00",
    )
    monkeypatch.setattr(
        dashboard_routes,
        "_fresh_dashboard_project_alerts",
        lambda _config, project: [alert] if project == "myproj" else [],
    )

    body = client.get(
        "/api/v1/dashboard?project=myproj", headers=auth_headers,
    ).json()

    assert body["rollups"]["open_inbox_count"] == 0
    assert body["rollups"]["pending_plan_reviews"] == 0
    assert body["rollups"]["alert_count"] == 1
    assert "rollups.alert_count" in body["scoped_fields"]
    assert body["project_alerts"] == [
        {
            "session_name": "plan_gate-myproj",
            "alert_type": "plan_missing",
            "severity": "warn",
            "message": alert.message,
            "updated_at": "2026-06-03T15:00:00+00:00",
        }
    ]


def test_dashboard_scoped_fields_empty_without_filter(
    client, auth_headers, patch_gather, patch_list_projects,
):
    """Without ``?project=``, ``scoped_fields`` must be empty."""
    patch_list_projects([_api_project("myproj")])
    patch_gather(_make_data())
    body = client.get("/api/v1/dashboard", headers=auth_headers).json()
    assert body["scoped_fields"] == []


def test_dashboard_daemon_status_reflects_unfiltered_sessions(
    client, auth_headers, patch_gather, patch_list_projects,
):
    """Regression for Codex PR #2057 P0 #2.

    ``daemon_status`` is a system-wide health signal — it must be
    derived from the UNFILTERED gather result. A caller polling
    ``?project=foo`` (which has no live sessions) must still see
    ``daemon_status="up"`` when the supervisor is healthy and another
    project (``bar``) has active sessions.
    """
    patch_list_projects([
        _api_project("myproj"),
        _api_project("otherproj"),
    ])
    patch_gather(_make_data(
        active_sessions=[
            SessionActivity(
                name="otherproj-worker", role="worker", project="otherproj",
                project_label="Other Project", status="running",
                description="busy", age_seconds=3.0,
            ),
        ],
    ))

    body = client.get(
        "/api/v1/dashboard?project=myproj", headers=auth_headers,
    ).json()
    # Filtered active_sessions is empty because the only live session
    # is for otherproj…
    assert body["active_sessions"] == []
    # …but daemon_status must still reflect the unfiltered truth.
    assert body["daemon_status"] == "up"


def test_dashboard_daemon_status_down_when_only_unknown_sessions(
    client, auth_headers, patch_gather, patch_list_projects,
):
    """Regression for Codex round-2 review on PR #2057.

    ``dashboard_data.gather`` appends a ``SessionActivity`` for every
    planned launch in ``config.projects`` regardless of whether a
    runtime row exists in pg — when no runtime is found, the row is
    stamped with the synthetic ``status="unknown"`` sentinel. On a
    configured workspace with the supervisor down, ``active_sessions``
    is therefore non-empty even though the daemon is genuinely down.
    ``daemon_status`` must look past the placeholder rows and only
    treat real runtime-backed statuses as ``"up"``.
    """
    patch_list_projects([_api_project("myproj")])
    patch_gather(_make_data(
        active_sessions=[
            # Only synthetic "unknown" placeholder rows — no runtime
            # records in pg, daemon is down.
            SessionActivity(
                name="myproj-operator", role="operator", project="myproj",
                project_label="My Project", status="unknown",
                description="unknown", age_seconds=0.0,
            ),
            SessionActivity(
                name="myproj-worker", role="worker", project="myproj",
                project_label="My Project", status="unknown",
                description="unknown", age_seconds=0.0,
            ),
        ],
    ))

    body = client.get("/api/v1/dashboard", headers=auth_headers).json()
    # The placeholder rows pass through (the cockpit panel shows them
    # too), but daemon_status must reflect liveness, not list-length.
    assert len(body["active_sessions"]) == 2
    assert body["daemon_status"] == "down"


def test_dashboard_daemon_status_up_when_mixed_unknown_and_live(
    client, auth_headers, patch_gather, patch_list_projects,
):
    """A single live runtime row is enough to flip daemon_status="up",
    even when surrounded by synthetic ``unknown`` placeholders for
    other configured-but-not-running sessions.
    """
    patch_list_projects([
        _api_project("myproj"),
        _api_project("otherproj"),
    ])
    patch_gather(_make_data(
        active_sessions=[
            SessionActivity(
                name="myproj-operator", role="operator", project="myproj",
                project_label="My Project", status="unknown",
                description="unknown", age_seconds=0.0,
            ),
            SessionActivity(
                name="otherproj-worker", role="worker", project="otherproj",
                project_label="Other Project", status="healthy",
                description="idle", age_seconds=5.0,
            ),
        ],
    ))

    body = client.get("/api/v1/dashboard", headers=auth_headers).json()
    assert body["daemon_status"] == "up"


def test_dashboard_account_usages_passthrough(
    client, auth_headers, patch_gather, patch_list_projects,
):
    patch_list_projects([_api_project("myproj")])
    patch_gather(_make_data(
        account_usages=[
            AccountQuotaUsage(
                account_name="codex_primary",
                provider="codex",
                email="codex@example.com",
                used_pct=42,
                summary="42% used",
                severity="ok",
                limit_label="5h limit",
                reset_at="2026-05-22T01:00:00Z",
            ),
        ],
    ))
    body = client.get("/api/v1/dashboard", headers=auth_headers).json()
    assert len(body["account_usages"]) == 1
    row = body["account_usages"][0]
    assert row["account_name"] == "codex_primary"
    assert row["used_pct"] == 42
    assert row["reset_at"] == "2026-05-22T01:00:00Z"
