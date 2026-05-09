"""Per-endpoint integration tests for Phase 1 read endpoints.

Each test exercises the FastAPI app against a real
:class:`SQLiteWorkService` + tmp config. The work-service is opened
through the canonical :func:`pollypm.work.factory.create_work_service`
so the test path matches what the cockpit uses.
"""

from __future__ import annotations

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
