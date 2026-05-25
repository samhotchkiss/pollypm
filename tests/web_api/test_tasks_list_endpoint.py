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

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from pollypm.work.models import TaskSummaryProjection
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


def _seed_untracked_project(api_config, workspace_root, key: str = "paused"):
    """Register an untracked project that still has work rows in PG."""
    from pollypm.models import KnownProject, ProjectKind

    project_root = workspace_root / key
    project_root.mkdir()
    (project_root / ".pollypm").mkdir()
    api_config.projects[key] = KnownProject(
        key=key,
        path=project_root,
        name=key.title(),
        tracked=False,
        kind=ProjectKind.GIT,
    )
    return project_root


def _parse_api_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _stamp_task_timing(
    pg_schema_pool,
    *,
    project: str,
    task_number: int,
    created_at: datetime,
    state: str,
    state_entered_at: datetime,
) -> None:
    with pg_schema_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE work_tasks SET created_at = %s, updated_at = %s "
            "WHERE project = %s AND task_number = %s",
            (created_at, state_entered_at, project, task_number),
        )
        cur.execute(
            "UPDATE work_transitions SET created_at = %s "
            "WHERE task_project = %s AND task_number = %s AND to_state = %s",
            (state_entered_at, project, task_number, state),
        )


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
    assert body["total"] == 3
    assert body["has_more"] is False
    for item in body["items"]:
        assert item["created_at"] is not None
        assert item["state_entered_at"] is not None
        assert isinstance(item["age_seconds"], int)
        assert isinstance(item["dwell_seconds"], int)
        assert item["age_seconds"] >= item["dwell_seconds"] >= 0
    # ``next_cursor`` is absent when no more pages remain.
    assert body.get("next_cursor") is None
    # Every item carries the cross-project ``project`` field so the
    # client can disambiguate without re-querying.
    projects = {item["project"] for item in body["items"]}
    assert projects == {"myproj", "second"}


def test_list_tasks_timing_uses_latest_transition(
    api_config, client, auth_headers, project_root, pg_schema_pool
) -> None:
    db_path = api_config.project.state_db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with create_work_service(db_path=db_path, project_path=project_root) as svc:
        task = make_task(
            svc,
            project="myproj",
            title="Queued timing",
            description="queueable task",
        )
        svc.queue(task.task_id, actor="tester")

    now = datetime.now(timezone.utc).replace(microsecond=0)
    created_at = now - timedelta(hours=3)
    state_entered_at = now - timedelta(minutes=7)
    _stamp_task_timing(
        pg_schema_pool,
        project="myproj",
        task_number=task.task_number,
        created_at=created_at,
        state="queued",
        state_entered_at=state_entered_at,
    )

    response = client.get("/api/v1/tasks?project=myproj", headers=auth_headers)
    assert response.status_code == 200, response.text
    [item] = response.json()["items"]
    assert item["work_status"] == "queued"
    assert _parse_api_datetime(item["created_at"]) == created_at
    assert _parse_api_datetime(item["state_entered_at"]) == state_entered_at
    assert item["age_seconds"] > item["dwell_seconds"]


def test_list_all_tasks_uses_list_rows_without_per_item_refetch(
    api_config, monkeypatch
) -> None:
    """The flat task list must not turn a page into N extra ``get`` calls."""
    from contextlib import contextmanager

    from pollypm.web_api import service as svc_mod

    now = datetime.now(timezone.utc).replace(microsecond=0)

    class FakeTask(SimpleNamespace):
        @property
        def task_id(self) -> str:
            return f"{self.project}/{self.task_number}"

    task = FakeTask(
        project="myproj",
        task_number=1,
        title="No refetch",
        work_status="queued",
        type="task",
        priority="normal",
        assignee=None,
        claimed_by_session=None,
        current_node_id=None,
        plan_version=1,
        created_at=now - timedelta(hours=1),
        updated_at=now,
        transitions=[
            SimpleNamespace(to_state="queued", timestamp=now - timedelta(minutes=5))
        ],
    )

    class FakeService:
        def list_tasks(self, *, project=None, **_kwargs):
            if project in (None, "myproj"):
                return [task]
            return []

        def get(self, task_id):  # pragma: no cover - failure path
            raise AssertionError(f"unexpected per-item refetch: {task_id}")

    @contextmanager
    def fake_open(**_kwargs):
        yield FakeService()

    monkeypatch.setattr(svc_mod, "_open_work_service_readonly", fake_open)

    items, next_cursor, warnings, total = svc_mod.list_all_tasks(
        api_config, limit=10
    )

    assert [item.task_id for item in items] == ["myproj/1"]
    assert items[0].state_entered_at == now - timedelta(minutes=5)
    assert next_cursor is None
    assert warnings == []
    assert total == 1


def test_list_all_tasks_uses_summary_page_without_full_hydration(
    api_config, monkeypatch
) -> None:
    from contextlib import contextmanager

    from pollypm.web_api import service as svc_mod

    now = datetime.now(timezone.utc).replace(microsecond=0)

    class FakeService:
        def list_task_summary_page(self, **kwargs):
            assert kwargs["projects"] == ("myproj",)
            assert kwargs["limit"] == 10
            return (
                [
                    TaskSummaryProjection(
                        task_id="myproj/1",
                        project="myproj",
                        task_number=1,
                        title="Projected",
                        work_status="queued",
                        type="task",
                        priority="normal",
                        created_at=now - timedelta(hours=1),
                        state_entered_at=now - timedelta(minutes=5),
                        updated_at=now,
                    )
                ],
                None,
                1,
            )

        def count_task_summary_matches(self, **_kwargs):
            return 0

        def list_tasks(self, **_kwargs):  # pragma: no cover - failure path
            raise AssertionError("summary page must avoid full task hydration")

    @contextmanager
    def fake_open(**_kwargs):
        yield FakeService()

    monkeypatch.setattr(svc_mod, "_open_work_service_readonly", fake_open)

    items, next_cursor, warnings, total = svc_mod.list_all_tasks(
        api_config, limit=10
    )

    assert [item.task_id for item in items] == ["myproj/1"]
    assert items[0].state_entered_at == now - timedelta(minutes=5)
    assert next_cursor is None
    assert warnings == []
    assert total == 1


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


def test_list_tasks_warns_when_tracked_scope_drops_untracked_rows(
    api_config, client, auth_headers, project_root, workspace_root
) -> None:
    """Default list scope is tracked projects, but the truncation is explicit."""
    _seed_untracked_project(api_config, workspace_root)
    db_path = api_config.project.state_db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with create_work_service(db_path=db_path, project_path=project_root) as svc:
        make_task(svc, project="myproj", title="Visible")
        make_task(svc, project="paused", title="Hidden")

    response = client.get("/api/v1/tasks", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["title"] for item in body["items"]] == ["Visible"]
    assert body["warnings"] == [
        {
            "code": "untracked_filtered",
            "dropped_count": 1,
            "reason": "untracked_projects",
        }
    ]


def test_list_tasks_include_untracked_returns_hidden_rows(
    api_config, client, auth_headers, project_root, workspace_root
) -> None:
    """``include_untracked=true`` opts into rows outside the tracked set."""
    _seed_untracked_project(api_config, workspace_root)
    db_path = api_config.project.state_db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with create_work_service(db_path=db_path, project_path=project_root) as svc:
        make_task(svc, project="myproj", title="Visible")
        make_task(svc, project="paused", title="Hidden")

    response = client.get(
        "/api/v1/tasks?include_untracked=true", headers=auth_headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    titles = sorted(item["title"] for item in body["items"])
    assert titles == ["Hidden", "Visible"]
    assert body.get("warnings") is None


def test_list_tasks_project_filter_warns_for_unregistered_pg_rows(
    api_config, client, auth_headers, project_root
) -> None:
    """A project key absent from config is a no-match unless explicitly included."""
    db_path = api_config.project.state_db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with create_work_service(db_path=db_path, project_path=project_root) as svc:
        make_task(svc, project="orphan", title="Orphan row")

    response = client.get(
        "/api/v1/tasks?project=orphan", headers=auth_headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"] == []
    assert body["warnings"] == [
        {
            "code": "untracked_filtered",
            "dropped_count": 1,
            "reason": "untracked_projects",
        }
    ]

    included = client.get(
        "/api/v1/tasks?project=orphan&include_untracked=true",
        headers=auth_headers,
    )
    assert included.status_code == 200, included.text
    included_body = included.json()
    assert [item["title"] for item in included_body["items"]] == ["Orphan row"]
    assert included_body.get("warnings") is None


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
        queued = make_task(
            svc,
            project="myproj",
            title="Queued one",
            description="queueable task",
        )
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
    assert first["total"] == 5
    assert first["has_more"] is True
    assert first.get("next_cursor") is not None

    second = client.get(
        f"/api/v1/tasks?limit=2&cursor={first['next_cursor']}",
        headers=auth_headers,
    ).json()
    assert len(second["items"]) == 2
    assert second["total"] == 5
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


# ---------------------------------------------------------------------------
# PR #2067 Codex round 1 — P0 regressions
# ---------------------------------------------------------------------------


def test_cross_project_list_partial_failure_envelope(
    api_config, client, auth_headers, project_root, workspace_root, monkeypatch
) -> None:
    """P0 #1: one project failing must surface as a `warnings` entry
    and NOT silently drop. Other projects' data still comes back."""
    import psycopg

    from pollypm.web_api import service as svc_mod

    second_root = _seed_two_projects(api_config, workspace_root)  # noqa: F841
    db_path = api_config.project.state_db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with create_work_service(db_path=db_path, project_path=project_root) as svc:
        make_task(svc, project="myproj", title="Healthy A")
        make_task(svc, project="second", title="Healthy B")

    # Stub the readonly work-service open so the *second* project
    # raises a psycopg error (mirroring a pg outage on one DB),
    # while ``myproj`` opens normally.
    real_open = svc_mod._open_work_service_readonly

    from contextlib import contextmanager

    @contextmanager
    def flaky_open(*, config, project_key, project_path):
        if project_key == "second":
            raise psycopg.OperationalError("simulated pg outage")
        with real_open(
            config=config, project_key=project_key, project_path=project_path
        ) as svc:
            yield svc

    monkeypatch.setattr(svc_mod, "_open_work_service_readonly", flaky_open)

    response = client.get("/api/v1/tasks", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    # Surviving project's row is still present — partial > none.
    titles = sorted(item["title"] for item in body["items"])
    assert "Healthy A" in titles
    # Failed project does NOT appear in items (the read failed).
    assert "Healthy B" not in titles
    # ...but the operator sees an explicit warning so they can act.
    assert body.get("warnings"), "partial failure must surface in warnings"
    failed = [w for w in body["warnings"] if w["project"] == "second"]
    assert failed, f"expected `second` in warnings, got {body['warnings']!r}"
    assert failed[0]["error"] == "service_unavailable"


def test_cross_project_list_rejects_naive_since(
    api_config, client, auth_headers
) -> None:
    """P0 #2: a syntactically-valid naive ISO `since` used to crash
    with TypeError (offset-naive vs offset-aware) → 500. Reject it as
    a typed 400 instead."""
    # No `Z` suffix, no `+00:00` — pure naive ISO.
    response = client.get(
        "/api/v1/tasks?since=2026-05-22T10:00:00", headers=auth_headers
    )
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    assert "timezone" in body["error"]["message"].lower()


def test_cross_project_list_stale_cursor_returns_400(
    api_config, client, auth_headers, project_root
) -> None:
    """P0 #3: when the cursor's anchor item is updated/deleted between
    page requests, the helper used to silently restart at page one
    (duplicate rows / infinite pagination). It must now 400 so the
    client restarts explicitly."""
    db_path = api_config.project.state_db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    task_ids: list[str] = []
    with create_work_service(db_path=db_path, project_path=project_root) as svc:
        for i in range(3):
            task = make_task(svc, project="myproj", title=f"Task {i}")
            task_ids.append(task.task_id)

    # Take page one (limit=1) so we get a cursor.
    first = client.get(
        "/api/v1/tasks?limit=1", headers=auth_headers
    ).json()
    cursor = first.get("next_cursor")
    assert cursor, "fixture must produce a next_cursor"

    # Mutate the task the cursor refers to. The cursor anchor is the
    # *last item of the previous page* (per the helper's contract).
    # Cancelling bumps ``updated_at``, which changes the cursor key —
    # so the next page request can no longer locate the anchor and
    # must surface stale-cursor explicitly.
    tail_id = first["items"][-1]["task_id"]
    with create_work_service(db_path=db_path, project_path=project_root) as svc:
        svc.cancel(tail_id, actor="tester", reason="stale-cursor test")

    # The next request must fail explicitly, not silently page-one.
    response = client.get(
        f"/api/v1/tasks?limit=1&cursor={cursor}", headers=auth_headers
    )
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    assert "cursor" in body["error"]["message"].lower()
