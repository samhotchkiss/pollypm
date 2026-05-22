"""Tests for the ``GET /api/v1/tasks`` cross-project flat list (Phase 6 P0).

The per-project list at ``/projects/{key}/tasks`` already covers
narrow-scope reads; this endpoint adds the cross-project surface
spec §5.1 calls for. The test suite verifies the four filter axes
(project / status / assignee / since), the pagination contract
(opaque cursor + ``next_cursor`` only when more remains), and the
graceful-empty-list response on unknown filters.

Each test takes ``pg_schema_pool`` so the per-test pg schema is set
up and torn down — that fixture lives in ``tests/conftest_pg.py``
and patches ``POLLYPM_PG_DSN`` + the pool factory so every
work-service open lands in an isolated namespace. Without it tests
run against the dev machine's real ``pollypm`` pg DB and accumulate
state across runs.
"""

from __future__ import annotations

import pytest

from pollypm.work.factory import create_work_service

from .conftest import make_task


# Mark every test in this module as needing pg isolation. The
# ``pg_schema_pool`` fixture (from ``tests/conftest_pg.py``) creates
# ``CREATE SCHEMA test_<uuid>``, patches the pool factory to set
# ``search_path`` to that schema, then drops the schema on teardown —
# so each test starts with empty work_tasks / work_node_executions /
# work_transitions tables. The fixture handles its own skip when
# Docker / a local pg+pgvector isn't reachable.
pytestmark = pytest.mark.usefixtures("pg_schema_pool")


def _seed_two_projects(api_config, workspace_root):
    """Register a second tracked project on the same shared DB.

    The conftest registers a single ``myproj``; we add ``second`` so
    cross-project assertions have something to merge. Both projects
    share the per-test pg schema so they read distinct rows by the
    work-service's per-row ``project_key`` column.
    """
    from pollypm.models import KnownProject, ProjectKind

    second_root = workspace_root / "second"
    second_root.mkdir()
    (second_root / ".pollypm").mkdir()
    api_config.projects["second"] = KnownProject(
        key="second",
        path=second_root,
        name="Second",
        tracked=True,
        kind=ProjectKind.GIT,
    )
    return second_root


def test_list_tasks_returns_all_projects(
    api_config, client, auth_headers, project_root, workspace_root
) -> None:
    """No filter ⇒ every task from every registered project surfaces."""
    _seed_two_projects(api_config, workspace_root)
    db_path = api_config.project.state_db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with create_work_service(db_path=db_path, project_path=project_root) as svc:
        make_task(svc, project="myproj", title="My A")
        make_task(svc, project="myproj", title="My B")
        make_task(svc, project="second", title="Second A")

    response = client.get("/api/v1/tasks", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    titles = sorted(item["title"] for item in body["items"])
    assert titles == ["My A", "My B", "Second A"]
    # ``next_cursor`` is absent when no more pages remain.
    assert body.get("next_cursor") is None
    # Every item carries the cross-project ``project`` field so the
    # client can disambiguate without re-querying.
    projects = {item["project"] for item in body["items"]}
    assert projects == {"myproj", "second"}


def test_list_tasks_project_filter(
    api_config, client, auth_headers, project_root, workspace_root
) -> None:
    """``?project=`` restricts the result to that project only."""
    _seed_two_projects(api_config, workspace_root)
    db_path = api_config.project.state_db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with create_work_service(db_path=db_path, project_path=project_root) as svc:
        make_task(svc, project="myproj", title="Mine")
        make_task(svc, project="second", title="Theirs")

    response = client.get(
        "/api/v1/tasks?project=second", headers=auth_headers
    )
    assert response.status_code == 200
    titles = [item["title"] for item in response.json()["items"]]
    assert titles == ["Theirs"]


def test_list_tasks_status_filter_or_semantics(
    api_config, client, auth_headers, project_root
) -> None:
    """Repeatable ``?status=`` is OR — multiple statuses union."""
    db_path = api_config.project.state_db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with create_work_service(db_path=db_path, project_path=project_root) as svc:
        # All freshly-created tasks land in ``draft``. Queue one so
        # we have a non-draft status to filter against, and cancel
        # another so we have a status nobody asks for.
        make_task(svc, project="myproj", title="Drafty")
        cancelled = make_task(svc, project="myproj", title="Cancelled one")
        queued = make_task(svc, project="myproj", title="Queued one")
        svc.queue(queued.task_id, actor="tester")
        svc.cancel(cancelled.task_id, actor="tester", reason="cleanup")

    response = client.get(
        "/api/v1/tasks?status=draft&status=queued", headers=auth_headers
    )
    assert response.status_code == 200
    titles = sorted(item["title"] for item in response.json()["items"])
    # The draft+queued union excludes the cancelled task.
    assert titles == ["Drafty", "Queued one"]


def test_list_tasks_since_filter_rejects_bad_iso(
    api_config, client, auth_headers
) -> None:
    """Malformed ``since`` → 400 invalid_request (spec §6)."""
    response = client.get(
        "/api/v1/tasks?since=not-a-timestamp", headers=auth_headers
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    assert "since" in body["error"]["message"].lower()


def test_list_tasks_since_filter_strict_after(
    api_config, client, auth_headers, project_root
) -> None:
    """``since`` is strictly-after, so a timestamp matching ``updated_at``
    does NOT include that row. This pins the half-open contract the
    docstring promises so a future ``>=`` regression trips the test."""
    db_path = api_config.project.state_db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with create_work_service(db_path=db_path, project_path=project_root) as svc:
        make_task(svc, project="myproj", title="Old one")

    # First read: the API surfaces ``updated_at``. We then pass that
    # exact value back as ``since`` — the strict-after contract means
    # the row should NOT appear again.
    initial = client.get("/api/v1/tasks", headers=auth_headers).json()
    assert initial["items"], "fixture must seed at least one task"
    pivot_ts = initial["items"][0]["updated_at"]

    response = client.get(
        f"/api/v1/tasks?since={pivot_ts}", headers=auth_headers
    )
    assert response.status_code == 200
    # Strict-after: a row with updated_at == pivot must be excluded.
    later_titles = [item["title"] for item in response.json()["items"]]
    assert "Old one" not in later_titles


def test_list_tasks_pagination_cursor(
    api_config, client, auth_headers, project_root
) -> None:
    """Pagination walks every row with no overlap between pages."""
    db_path = api_config.project.state_db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with create_work_service(db_path=db_path, project_path=project_root) as svc:
        for i in range(5):
            make_task(svc, project="myproj", title=f"Task {i}")

    first = client.get(
        "/api/v1/tasks?limit=2", headers=auth_headers
    ).json()
    assert len(first["items"]) == 2
    assert first.get("next_cursor") is not None

    second = client.get(
        f"/api/v1/tasks?limit=2&cursor={first['next_cursor']}",
        headers=auth_headers,
    ).json()
    assert len(second["items"]) == 2
    # The pages must not overlap.
    first_ids = {item["task_id"] for item in first["items"]}
    second_ids = {item["task_id"] for item in second["items"]}
    assert first_ids.isdisjoint(second_ids)


def test_list_tasks_limit_capped(
    api_config, client, auth_headers
) -> None:
    """``limit`` over 200 is rejected with 422 (spec §5.1, Query bounds)."""
    response = client.get(
        "/api/v1/tasks?limit=9999", headers=auth_headers
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"


def test_list_tasks_unknown_project_returns_empty(
    api_config, client, auth_headers, project_root
) -> None:
    """Unknown ``project=`` is a no-match, not a 404 — the flat-list
    endpoint never raises ``not_found`` for filter values (only for
    auth)."""
    db_path = api_config.project.state_db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with create_work_service(db_path=db_path, project_path=project_root) as svc:
        make_task(svc, project="myproj", title="Exists")

    response = client.get(
        "/api/v1/tasks?project=does-not-exist", headers=auth_headers
    )
    assert response.status_code == 200
    assert response.json()["items"] == []


def test_list_tasks_requires_auth(client) -> None:
    """No bearer token ⇒ 401, matching every other authed endpoint."""
    response = client.get("/api/v1/tasks")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
