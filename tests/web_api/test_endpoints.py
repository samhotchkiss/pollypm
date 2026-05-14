"""Per-endpoint integration tests for Phase 1 read endpoints.

Each test exercises the FastAPI app against a real
:class:`SQLiteWorkService` + tmp config. The work-service is opened
through the canonical :func:`pollypm.work.factory.create_work_service`
so the test path matches what the cockpit uses.
"""

from __future__ import annotations

import sqlite3

import pytest

from pollypm.work.factory import create_work_service

from .conftest import make_task


def test_health_returns_status_ok(client) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert isinstance(body["schema_version"], int)


def test_list_projects_returns_registered_project(client, auth_headers) -> None:
    response = client.get("/api/v1/projects", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["items"], list)
    keys = [item["key"] for item in body["items"]]
    assert "myproj" in keys
    project = next(item for item in body["items"] if item["key"] == "myproj")
    assert project["tracked"] is True
    assert project["name"] == "My Project"
    assert "task_counts" in project
    assert "open_inbox_count" in project
    assert "pending_plan_review" in project
    assert project["glyph"] in {"green", "amber", "red", "paused", "unknown"}


def test_list_projects_tracked_filter(api_config, client, auth_headers) -> None:
    # Add a non-tracked project to confirm the filter works.
    from pollypm.models import KnownProject, ProjectKind

    api_config.projects["second"] = KnownProject(
        key="second",
        path=api_config.projects["myproj"].path,
        name="Second",
        tracked=False,
        kind=ProjectKind.GIT,
    )
    response = client.get("/api/v1/projects?tracked=true", headers=auth_headers)
    assert response.status_code == 200
    keys = [item["key"] for item in response.json()["items"]]
    assert keys == ["myproj"]


def test_get_project_drilldown_returns_404_for_unknown(client, auth_headers) -> None:
    response = client.get("/api/v1/projects/nope", headers=auth_headers)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_get_project_drilldown_renders_known_project(client, auth_headers) -> None:
    response = client.get("/api/v1/projects/myproj", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["key"] == "myproj"
    assert "recent_activity" in body
    assert "top_tasks" in body


def test_list_project_tasks_returns_tasks(api_config, client, auth_headers, project_root) -> None:
    db_path = api_config.project.state_db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with create_work_service(db_path=db_path, project_path=project_root) as svc:
        make_task(svc, project="myproj", title="First")
        make_task(svc, project="myproj", title="Second")

    response = client.get("/api/v1/projects/myproj/tasks", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    titles = [item["title"] for item in body["items"]]
    assert "First" in titles
    assert "Second" in titles


def test_list_project_tasks_pagination_cursor(api_config, client, auth_headers, project_root) -> None:
    db_path = api_config.project.state_db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with create_work_service(db_path=db_path, project_path=project_root) as svc:
        for i in range(5):
            make_task(svc, project="myproj", title=f"Task {i}")

    first = client.get(
        "/api/v1/projects/myproj/tasks?limit=2", headers=auth_headers
    ).json()
    assert len(first["items"]) == 2
    assert first.get("next_cursor") is not None

    second = client.get(
        f"/api/v1/projects/myproj/tasks?limit=2&cursor={first['next_cursor']}",
        headers=auth_headers,
    ).json()
    assert len(second["items"]) == 2
    # Pages don't overlap.
    first_ids = {item["task_id"] for item in first["items"]}
    second_ids = {item["task_id"] for item in second["items"]}
    assert first_ids.isdisjoint(second_ids)


def test_list_project_tasks_404_for_unknown_project(client, auth_headers) -> None:
    response = client.get("/api/v1/projects/nope/tasks", headers=auth_headers)
    assert response.status_code == 404


def test_get_task_detail_renders_full_record(api_config, client, auth_headers, project_root) -> None:
    db_path = api_config.project.state_db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with create_work_service(db_path=db_path, project_path=project_root) as svc:
        task = make_task(svc, project="myproj", title="Detail", description="hello")

    response = client.get(
        f"/api/v1/tasks/myproj/{task.task_number}", headers=auth_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Detail"
    assert body["description"] == "hello"
    assert body["task_id"] == task.task_id
    assert "transitions" in body
    assert "executions" in body
    assert "relationships" in body


def test_get_task_detail_404_for_unknown_task(client, auth_headers) -> None:
    response = client.get("/api/v1/tasks/myproj/9999", headers=auth_headers)
    assert response.status_code == 404


def test_get_task_detail_passes_through_production_entry_types(
    api_config, client, auth_headers, project_root
) -> None:
    """MED-4 regression: ``ContextEntry.entry_type`` previously was a
    ``Literal["note","reply","read"]`` so any task carrying a real
    production label like ``human_review_approved`` 500'd on
    serialization. The field is now an open string — the detail
    response should round-trip the production value verbatim."""
    db_path = api_config.project.state_db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with create_work_service(db_path=db_path, project_path=project_root) as svc:
        task = make_task(svc, project="myproj", title="Review me")
        # ``human_review_approved`` is a real production entry_type
        # written by SQLiteWorkService.transition() on approve.
        svc.add_context(
            task.task_id,
            actor="pm",
            text="approved by operator",
            entry_type="human_review_approved",
        )
    response = client.get(
        f"/api/v1/tasks/myproj/{task.task_number}", headers=auth_headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    entry_types = [c["entry_type"] for c in (body.get("context") or [])]
    assert "human_review_approved" in entry_types


def test_get_project_plan_404_when_no_plan(client, auth_headers) -> None:
    response = client.get("/api/v1/projects/myproj/plan", headers=auth_headers)
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert "hint" in body["error"]


def test_list_inbox_empty_state(client, auth_headers) -> None:
    response = client.get("/api/v1/inbox", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []


def test_list_inbox_returns_chat_tasks(api_config, client, auth_headers, project_root) -> None:
    db_path = api_config.project.state_db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with create_work_service(db_path=db_path, project_path=project_root) as svc:
        make_task(
            svc,
            project="myproj",
            title="Chat thread",
            description="Hello there",
            flow_template="chat",
            roles={"requester": "user", "operator": "pm"},
        )
    response = client.get("/api/v1/inbox", headers=auth_headers)
    assert response.status_code == 200
    titles = [item["subject"] for item in response.json()["items"]]
    assert "Chat thread" in titles


def test_get_inbox_item_404_for_unknown(client, auth_headers) -> None:
    response = client.get("/api/v1/inbox/nope%2F1", headers=auth_headers)
    assert response.status_code == 404


def test_inbox_detail_round_trip_with_returned_id(
    api_config, client, auth_headers, project_root
) -> None:
    """HIGH-4 regression: the inbox-list response returns IDs like
    ``myproj/1``. The detail route at ``/inbox/{id}`` must accept
    those IDs verbatim — encoded slashes 404'd before the
    ``{id:path}`` fix."""
    db_path = api_config.project.state_db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with create_work_service(db_path=db_path, project_path=project_root) as svc:
        make_task(
            svc,
            project="myproj",
            title="Round-trip thread",
            description="From the inbox list",
            flow_template="chat",
            roles={"requester": "user", "operator": "pm"},
        )
    listing = client.get("/api/v1/inbox", headers=auth_headers).json()
    assert listing["items"], "expected at least one inbox item"
    returned_id = listing["items"][0]["id"]
    # The list returned ``myproj/1``-style IDs; the detail route
    # must accept them without the client URL-encoding the slash.
    assert "/" in returned_id
    detail = client.get(f"/api/v1/inbox/{returned_id}", headers=auth_headers)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["id"] == returned_id


def test_cors_preflight_allows_loopback_origin(client) -> None:
    """Spec §10 has the frontend posting from a separate-origin Vite
    dev server with ``Authorization: Bearer …``. Without CORS the
    preflight returns 405 and the browser never sends the real
    request. Confirm we allow loopback origins on any port."""
    response = client.options(
        "/api/v1/projects",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization,Content-Type",
        },
    )
    assert response.status_code in {200, 204}
    assert response.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert response.headers.get("access-control-allow-credentials") == "true"
    allow_headers = response.headers.get("access-control-allow-headers", "").lower()
    assert "authorization" in allow_headers


def test_cors_rejects_non_loopback_origin(client) -> None:
    """Non-loopback origins are not mirrored — the access-control
    headers should be absent so the browser blocks the request."""
    response = client.options(
        "/api/v1/projects",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization",
        },
    )
    # Either no allow-origin header, or one that doesn't match the
    # attacker's origin. The CORS middleware only sets the header for
    # origins matching ``allow_origin_regex``.
    allow = response.headers.get("access-control-allow-origin")
    assert allow != "https://evil.example.com"


def test_validation_error_shape_for_bad_query(client, auth_headers) -> None:
    response = client.get(
        "/api/v1/projects/myproj/tasks?limit=999", headers=auth_headers
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert "details" in body
    assert isinstance(body["details"], list)
    assert all("field" in row and "message" in row for row in body["details"])


# ---------------------------------------------------------------------------
# Backing-store failures during work-service construction map to 503.
#
# Round-2 Codex finding: the round-1 fix wrapped some service calls
# with ``_BACKING_STORE_ERRORS`` but not the construction step itself.
# ``_open_work_service_readonly`` calls
# :func:`pollypm.work.factory.create_work_service`, which can raise
# :class:`sqlite3.OperationalError` during DB open / pragmas / schema
# create / migrations. Spec §6 maps backing-store failures to 503
# ``service_unavailable``; the tests below confirm that wrapping the
# entire ``with`` block (not just the body) gets us there for all
# three endpoints.
# ---------------------------------------------------------------------------


@pytest.fixture
def backing_store_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``_open_work_service_readonly`` to fail on construction.

    Patches the lazy import target —
    :func:`pollypm.work.factory.create_work_service` — to raise the
    same ``OperationalError`` SQLite emits when the DB file can't be
    opened or a pragma fails. This simulates the construction-time
    failure mode the round-2 review flagged.
    """

    def _boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(
        "pollypm.work.factory.create_work_service", _boom
    )


def test_list_project_tasks_returns_503_when_construction_fails(
    client, auth_headers, backing_store_unavailable
) -> None:
    response = client.get("/api/v1/projects/myproj/tasks", headers=auth_headers)
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["error"]["code"] == "service_unavailable"
    # The hint nudges the operator toward retry / doctor — keeps the
    # contract from MED-6 round 1.
    assert "hint" in body["error"]


def test_get_task_detail_returns_503_when_construction_fails(
    client, auth_headers, backing_store_unavailable
) -> None:
    response = client.get("/api/v1/tasks/myproj/1", headers=auth_headers)
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["error"]["code"] == "service_unavailable"
    assert "hint" in body["error"]


def test_get_active_plan_returns_503_when_construction_fails(
    client, auth_headers, backing_store_unavailable
) -> None:
    response = client.get("/api/v1/projects/myproj/plan", headers=auth_headers)
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["error"]["code"] == "service_unavailable"
    assert "hint" in body["error"]


def test_get_task_detail_still_404s_for_genuinely_missing_task(
    api_config, client, auth_headers, project_root
) -> None:
    """Backing-store wrap must not swallow legitimate 404s.

    ``svc.get(...)`` raises ``TaskNotFoundError`` (a non-DB error) for
    a missing task; the inner swallow returns ``None`` and the route
    maps that to 404. Only ``OperationalError`` /
    ``sqlite3.DatabaseError`` / ``OSError`` get reclassified to 503.
    """
    db_path = api_config.project.state_db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with create_work_service(db_path=db_path, project_path=project_root) as svc:
        make_task(svc, project="myproj", title="Real task")
    # Ask for a task number that doesn't exist — should still 404.
    response = client.get("/api/v1/tasks/myproj/9999", headers=auth_headers)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
