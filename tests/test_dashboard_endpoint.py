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

from pathlib import Path

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
    # Optional fields default off
    assert body.get("daily_tokens") is None
    assert body.get("briefing") is None


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
