"""Integration tests for the Phase 2 inbox write endpoints.

Covers ``POST /api/v1/inbox/{id}/{archive,snooze,promote-to-task,
mark-read,reply}`` per the Phase 2 endpoints spec §4
(``~/Desktop/pollypm-phase2-endpoints-spec.md``).

The repo's full conftest spins up a Postgres testcontainer for every
work-service test; that's too slow for endpoint-level coverage. We
take the same approach the chat-send / chat-messages endpoint suites
take: ``--noconftest`` to skip the heavy fixtures, then monkeypatch
the service-layer write helpers so the FastAPI route ↔ handler ↔
error-envelope plumbing is exercised end-to-end without touching
Postgres. The underlying work-service writers themselves are
exercised by ``tests/test_pg_inbox_actions.py``.

Run with ``pytest --noconftest tests/test_inbox_writes_endpoint.py -v``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
from pollypm.models import KnownProject, ProjectKind, ProviderKind, RuntimeKind
from pollypm.web_api import create_app, ensure_token
from pollypm.web_api.errors import APIError
from pollypm.web_api.models import (
    TaskDetail,
    TaskRelationships,
)
from pollypm.web_api.routes import inbox as inbox_routes


# ---------------------------------------------------------------------------
# Fixtures (self-contained — do not rely on tests/web_api/conftest.py)
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
def token(tmp_path: Path) -> tuple[Path, str]:
    token_path = tmp_path / "api-token"
    value, _ = ensure_token(token_path)
    return token_path, value


@pytest.fixture
def auth_headers(token: tuple[Path, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {token[1]}"}


@pytest.fixture
def app(config, token):
    token_path, _ = token
    return create_app(config=config, token_path=token_path)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


def _make_task_detail(
    *,
    task_id: str = "myproj/1",
    title: str = "Inbox item",
    work_status: str = "in_progress",
    description: str = "hello from the inbox",
) -> TaskDetail:
    project, num = task_id.split("/", 1)
    return TaskDetail(
        task_id=task_id,
        project=project,
        task_number=int(num),
        title=title,
        work_status=work_status,
        type="task",
        priority="normal",
        assignee=None,
        current_node_id=None,
        plan_version=1,
        updated_at=datetime(2026, 5, 21, 12, 0, 0, tzinfo=timezone.utc),
        description=description,
        acceptance_criteria=None,
        constraints=None,
        labels=[],
        relevant_files=[],
        relationships=TaskRelationships(),
        flow_template_id="chat",
        flow_template_version=1,
        requires_human_review=False,
        predecessor_task_id=None,
        transitions=[],
        executions=[],
        context=[],
        external_refs={},
        total_input_tokens=0,
        total_output_tokens=0,
        session_count=0,
        created_at=datetime(2026, 5, 20, 9, 0, 0, tzinfo=timezone.utc),
        created_by="tester",
    )


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------


def test_archive_happy_path_returns_task_detail(
    client, auth_headers, monkeypatch,
) -> None:
    """Archive flips the source row to ``done`` via the work-service.

    The handler should call ``archive_inbox_item`` with the path id
    and the optional ``reason``, returning the post-transition
    ``TaskDetail`` so the client can re-render.
    """
    captured: dict[str, object] = {}

    def fake_archive(config, item_id, *, reason=None, actor="api"):
        captured["item_id"] = item_id
        captured["reason"] = reason
        return _make_task_detail(task_id=item_id, work_status="done")

    monkeypatch.setattr(inbox_routes, "archive_inbox_item", fake_archive)
    response = client.post(
        "/api/v1/inbox/myproj/1/archive",
        headers=auth_headers,
        json={"reason": "no longer relevant"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["task_id"] == "myproj/1"
    assert body["work_status"] == "done"
    assert captured == {"item_id": "myproj/1", "reason": "no longer relevant"}


def test_archive_accepts_empty_body(client, auth_headers, monkeypatch) -> None:
    """The ``reason`` field is optional — empty body must work."""

    def fake_archive(config, item_id, *, reason=None, actor="api"):
        assert reason is None
        return _make_task_detail(task_id=item_id, work_status="done")

    monkeypatch.setattr(inbox_routes, "archive_inbox_item", fake_archive)
    response = client.post(
        "/api/v1/inbox/myproj/1/archive", headers=auth_headers,
    )
    assert response.status_code == 200, response.text


def test_archive_409_when_already_archived(
    client, auth_headers, monkeypatch,
) -> None:
    """Spec §4.3: archiving a terminal item returns 409 invalid_state."""

    def fake_archive(config, item_id, *, reason=None, actor="api"):
        raise APIError(
            status_code=409,
            code="invalid_state",
            message=f"Inbox item {item_id} is already done.",
        )

    monkeypatch.setattr(inbox_routes, "archive_inbox_item", fake_archive)
    response = client.post(
        "/api/v1/inbox/myproj/1/archive", headers=auth_headers,
    )
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "invalid_state"


def test_archive_404_for_unknown_item(client, auth_headers, monkeypatch) -> None:
    """Unknown ids surface as 404 ``not_found``."""
    from pollypm.web_api.errors import not_found

    def fake_archive(config, item_id, **kwargs):
        raise not_found(f"Inbox item not found: {item_id}")

    monkeypatch.setattr(inbox_routes, "archive_inbox_item", fake_archive)
    response = client.post(
        "/api/v1/inbox/myproj/9999/archive", headers=auth_headers,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_archive_requires_auth(client) -> None:
    response = client.post("/api/v1/inbox/myproj/1/archive")
    assert response.status_code == 401
    assert response.json()["error"]["code"] in {"unauthorized", "invalid_token"}


# ---------------------------------------------------------------------------
# Snooze
# ---------------------------------------------------------------------------


def test_snooze_with_duration_seconds(
    client, auth_headers, monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_snooze(
        config, item_id, *, duration_seconds=None, until=None, reason=None,
        actor="api",
    ):
        captured["duration_seconds"] = duration_seconds
        captured["until"] = until
        captured["reason"] = reason
        return _make_task_detail(task_id=item_id)

    monkeypatch.setattr(inbox_routes, "snooze_inbox_item", fake_snooze)
    response = client.post(
        "/api/v1/inbox/myproj/1/snooze",
        headers=auth_headers,
        json={"duration_seconds": 3600, "reason": "later"},
    )
    assert response.status_code == 200, response.text
    assert captured["duration_seconds"] == 3600
    assert captured["until"] is None
    assert captured["reason"] == "later"


def test_snooze_with_until_timestamp(
    client, auth_headers, monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_snooze(
        config, item_id, *, duration_seconds=None, until=None, reason=None,
        actor="api",
    ):
        captured["until"] = until
        captured["duration_seconds"] = duration_seconds
        return _make_task_detail(task_id=item_id)

    monkeypatch.setattr(inbox_routes, "snooze_inbox_item", fake_snooze)
    target = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    response = client.post(
        "/api/v1/inbox/myproj/1/snooze",
        headers=auth_headers,
        json={"until": target},
    )
    assert response.status_code == 200, response.text
    assert captured["duration_seconds"] is None
    # FastAPI / Pydantic deserialises the iso string to a datetime.
    assert isinstance(captured["until"], datetime)


def test_snooze_requires_body(client, auth_headers) -> None:
    """Spec: snooze must carry duration_seconds or until."""
    response = client.post(
        "/api/v1/inbox/myproj/1/snooze",
        headers=auth_headers,
    )
    # Missing body fails Pydantic body validation → 422.
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_snooze_400_for_invalid_window(
    client, auth_headers, monkeypatch,
) -> None:
    """Spec §4.3: 400 invalid_request when window is empty / past / >30d."""

    def fake_snooze(config, item_id, **kwargs):
        raise APIError(
            status_code=400,
            code="invalid_request",
            message="Snooze until must be in the future.",
        )

    monkeypatch.setattr(inbox_routes, "snooze_inbox_item", fake_snooze)
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    response = client.post(
        "/api/v1/inbox/myproj/1/snooze",
        headers=auth_headers,
        json={"until": past},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


# ---------------------------------------------------------------------------
# Promote
# ---------------------------------------------------------------------------


def test_promote_creates_new_task(client, auth_headers, monkeypatch) -> None:
    """``promote-to-task`` returns the new TaskDetail, not the source's."""
    captured: dict[str, object] = {}

    def fake_promote(
        config, item_id, *, target_project=None, prompt=None, title=None,
        actor="api",
    ):
        captured["item_id"] = item_id
        captured["target_project"] = target_project
        captured["prompt"] = prompt
        captured["title"] = title
        # Return a new id to mimic "create": myproj/1 → myproj/2.
        return _make_task_detail(
            task_id="myproj/2", title=title or "From inbox: Source",
        )

    monkeypatch.setattr(inbox_routes, "promote_inbox_to_task", fake_promote)
    response = client.post(
        "/api/v1/inbox/myproj/1/promote-to-task",
        headers=auth_headers,
        json={"title": "Ship the feature", "prompt": "Description body"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["task_id"] == "myproj/2"
    assert captured["title"] == "Ship the feature"
    assert captured["prompt"] == "Description body"


def test_promote_accepts_empty_body(client, auth_headers, monkeypatch) -> None:
    """All overrides are optional — empty body falls through to defaults."""
    captured: dict[str, object] = {}

    def fake_promote(
        config, item_id, *, target_project=None, prompt=None, title=None,
        actor="api",
    ):
        captured.update(
            target_project=target_project, prompt=prompt, title=title,
        )
        return _make_task_detail(task_id="myproj/2")

    monkeypatch.setattr(inbox_routes, "promote_inbox_to_task", fake_promote)
    response = client.post(
        "/api/v1/inbox/myproj/1/promote-to-task", headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert captured == {"target_project": None, "prompt": None, "title": None}


# ---------------------------------------------------------------------------
# Mark read
# ---------------------------------------------------------------------------


def test_mark_read_updates_state(client, auth_headers, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_mark(config, item_id, *, actor="api"):
        captured["item_id"] = item_id
        captured["actor"] = actor
        return _make_task_detail(task_id=item_id)

    monkeypatch.setattr(inbox_routes, "mark_read_inbox_item", fake_mark)
    response = client.post(
        "/api/v1/inbox/myproj/1/mark-read", headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["task_id"] == "myproj/1"
    assert captured["item_id"] == "myproj/1"
    # Default actor falls through to "api" when body is omitted.
    assert captured["actor"] == "api"


def test_mark_read_honors_actor_override(
    client, auth_headers, monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_mark(config, item_id, *, actor="api"):
        captured["actor"] = actor
        return _make_task_detail(task_id=item_id)

    monkeypatch.setattr(inbox_routes, "mark_read_inbox_item", fake_mark)
    response = client.post(
        "/api/v1/inbox/myproj/1/mark-read",
        headers=auth_headers,
        json={"actor": "operator"},
    )
    assert response.status_code == 200, response.text
    assert captured["actor"] == "operator"


# ---------------------------------------------------------------------------
# Reply
# ---------------------------------------------------------------------------


def test_reply_happy_path(client, auth_headers, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_reply(config, item_id, *, body, owner=None, actor="api"):
        captured["item_id"] = item_id
        captured["body"] = body
        captured["owner"] = owner
        return _make_task_detail(task_id=item_id)

    monkeypatch.setattr(inbox_routes, "reply_inbox_item", fake_reply)
    response = client.post(
        "/api/v1/inbox/myproj/1/reply",
        headers=auth_headers,
        json={"body": "thanks for the update", "owner": "pm"},
    )
    assert response.status_code == 200, response.text
    assert captured["body"] == "thanks for the update"
    assert captured["owner"] == "pm"


def test_reply_422_on_empty_body(client, auth_headers) -> None:
    """``body`` is min_length=1; Pydantic surfaces a 422."""
    response = client.post(
        "/api/v1/inbox/myproj/1/reply",
        headers=auth_headers,
        json={"body": ""},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


# ---------------------------------------------------------------------------
# Cross-cutting: 404 propagation, auth, slashes in ids
# ---------------------------------------------------------------------------


def test_unknown_item_returns_404_across_endpoints(
    client, auth_headers, monkeypatch,
) -> None:
    """Every write endpoint should surface a not_found error verbatim."""
    from pollypm.web_api.errors import not_found

    def boom(config, item_id, **kwargs):
        raise not_found(f"Inbox item not found: {item_id}")

    monkeypatch.setattr(inbox_routes, "archive_inbox_item", boom)
    monkeypatch.setattr(inbox_routes, "snooze_inbox_item", boom)
    monkeypatch.setattr(inbox_routes, "promote_inbox_to_task", boom)
    monkeypatch.setattr(inbox_routes, "mark_read_inbox_item", boom)
    monkeypatch.setattr(inbox_routes, "reply_inbox_item", boom)

    # snooze + reply require a body; supply minimal valid ones.
    cases = [
        ("archive", {}),
        ("snooze", {"duration_seconds": 60}),
        ("promote-to-task", {}),
        ("mark-read", {}),
        ("reply", {"body": "x"}),
    ]
    for verb, payload in cases:
        r = client.post(
            f"/api/v1/inbox/myproj/9999/{verb}",
            headers=auth_headers,
            json=payload,
        )
        assert r.status_code == 404, (verb, r.text)
        assert r.json()["error"]["code"] == "not_found", verb


def test_id_with_slash_round_trips_for_all_write_verbs(
    client, auth_headers, monkeypatch,
) -> None:
    """Inbox IDs carry a slash (``project/n``). The ``{id:path}`` mount
    must accept them on every POST verb."""
    seen_ids: list[str] = []

    def capture(config, item_id, **kwargs):
        seen_ids.append(item_id)
        return _make_task_detail(task_id=item_id)

    monkeypatch.setattr(inbox_routes, "archive_inbox_item", capture)
    monkeypatch.setattr(inbox_routes, "snooze_inbox_item", capture)
    monkeypatch.setattr(inbox_routes, "promote_inbox_to_task", capture)
    monkeypatch.setattr(inbox_routes, "mark_read_inbox_item", capture)
    monkeypatch.setattr(inbox_routes, "reply_inbox_item", capture)

    cases = [
        ("archive", {}),
        ("snooze", {"duration_seconds": 60}),
        ("promote-to-task", {}),
        ("mark-read", {}),
        ("reply", {"body": "hi"}),
    ]
    for verb, payload in cases:
        r = client.post(
            f"/api/v1/inbox/myproj/7/{verb}",
            headers=auth_headers,
            json=payload,
        )
        assert r.status_code == 200, (verb, r.text)
    assert seen_ids == ["myproj/7"] * len(cases)


def test_all_write_endpoints_require_auth(client) -> None:
    """No Authorization header → 401 across every write endpoint."""
    cases = [
        ("archive", {}),
        ("snooze", {"duration_seconds": 60}),
        ("promote-to-task", {}),
        ("mark-read", {}),
        ("reply", {"body": "hi"}),
    ]
    for verb, payload in cases:
        r = client.post(f"/api/v1/inbox/myproj/1/{verb}", json=payload)
        assert r.status_code == 401, verb
        assert r.json()["error"]["code"] in {"unauthorized", "invalid_token"}, verb
