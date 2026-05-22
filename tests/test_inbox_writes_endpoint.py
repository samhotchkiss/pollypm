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


# ---------------------------------------------------------------------------
# OpenAPI contract — Idempotency-Key header is NOT advertised (#2060 P0 #1)
#
# An earlier draft of these handlers declared an ``Idempotency-Key``
# request header on every POST. There is no idempotency store wired
# into the API today, so retries duplicated promoted tasks / replies
# / snooze rows while the client reasonably believed the header
# protected it. The header was stripped from the contract; this test
# locks that decision so a future refactor can't silently re-introduce
# it without also implementing the dedup cache.
# ---------------------------------------------------------------------------


def test_openapi_does_not_advertise_idempotency_key_header(app) -> None:
    schema = app.openapi()
    write_paths = [
        "/api/v1/inbox/{id}/archive",
        "/api/v1/inbox/{id}/snooze",
        "/api/v1/inbox/{id}/promote-to-task",
        "/api/v1/inbox/{id}/mark-read",
        "/api/v1/inbox/{id}/reply",
    ]
    for path in write_paths:
        op = schema["paths"][path]["post"]
        header_params = [
            p for p in op.get("parameters", [])
            if p.get("in") == "header"
        ]
        names = [p.get("name") for p in header_params]
        assert "Idempotency-Key" not in names, (
            f"{path} re-introduced the Idempotency-Key header "
            "without an idempotency store — strip it or wire the "
            "real dedup cache first (#2060)."
        )


# ---------------------------------------------------------------------------
# Snooze visibility regression (#2060 P0 #2)
#
# The snooze write helper persists ``entry_type='snooze'`` rows whose
# text starts with the structured marker ``until_iso=<ISO>``. The
# inbox-list predicate (``_active_snoozed_ids`` /
# ``_parse_snooze_until``) reads that marker and filters out items
# whose snooze hasn't expired. Without this, the endpoint returned
# 200 while the row stayed visible — a pure broken feature.
# ---------------------------------------------------------------------------


def test_parse_snooze_until_reads_structured_marker() -> None:
    from pollypm.web_api.service import _parse_snooze_until

    text = "until_iso=2026-06-01T12:00:00+00:00; snoozed until 2026-06-01T12:00:00+00:00; reason: later"
    parsed = _parse_snooze_until(text)
    assert parsed is not None
    assert parsed == datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_parse_snooze_until_falls_back_to_legacy_shape() -> None:
    """Rows written before the structured marker landed still parse."""
    from pollypm.web_api.service import _parse_snooze_until

    text = "snoozed until 2026-06-01T12:00:00+00:00"
    parsed = _parse_snooze_until(text)
    assert parsed == datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_parse_snooze_until_returns_none_when_no_timestamp() -> None:
    from pollypm.web_api.service import _parse_snooze_until

    assert _parse_snooze_until("reason: later") is None
    assert _parse_snooze_until("") is None
    assert _parse_snooze_until("until_iso=not-a-date") is None


class _StubContextEntry:
    def __init__(self, text: str) -> None:
        self.text = text
        self.actor = "api"
        self.entry_type = "snooze"


class _StubTask:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id


class _StubSvc:
    """Minimal stand-in for ``PgWorkService.get_context`` semantics."""

    def __init__(self, entries_by_id: dict[str, list[str]]) -> None:
        self._entries = entries_by_id

    def get_context(self, task_id, *, entry_type=None, limit=None):
        assert entry_type == "snooze"
        texts = self._entries.get(task_id, [])
        # Real svc returns DESC (most recent first); ``limit=1`` picks
        # the latest. Mirror that ordering so the helper sees the same
        # shape it does in production.
        rows = [_StubContextEntry(t) for t in reversed(texts)]
        if limit is not None:
            rows = rows[: int(limit)]
        return rows


def test_active_snoozed_ids_excludes_expired_snooze() -> None:
    from pollypm.web_api.service import _active_snoozed_ids

    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    past = (now - timedelta(hours=1)).isoformat()
    svc = _StubSvc({"myproj/1": [f"until_iso={past}"]})
    snoozed = _active_snoozed_ids(svc, [_StubTask("myproj/1")], now=now)
    assert snoozed == set()


def test_active_snoozed_ids_hides_future_snooze() -> None:
    """Regression for the original bug: the row stayed visible."""
    from pollypm.web_api.service import _active_snoozed_ids

    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    future = (now + timedelta(hours=2)).isoformat()
    svc = _StubSvc({"myproj/1": [f"until_iso={future}"]})
    snoozed = _active_snoozed_ids(svc, [_StubTask("myproj/1")], now=now)
    assert snoozed == {"myproj/1"}


def test_active_snoozed_ids_uses_latest_snooze_entry() -> None:
    """When an item is snoozed multiple times, the latest one wins.

    The reader sorts by ``id DESC`` (most-recent first) and takes
    ``limit=1``; so an item snoozed for an hour and then re-snoozed
    for ten seconds (now expired) must NOT be hidden.
    """
    from pollypm.web_api.service import _active_snoozed_ids

    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    old_future = (now + timedelta(hours=2)).isoformat()
    new_past = (now - timedelta(minutes=5)).isoformat()
    svc = _StubSvc({
        # Order = insertion order; the stub reverses it to mimic
        # ``ORDER BY id DESC``. So the second entry is "newer".
        "myproj/1": [
            f"until_iso={old_future}",
            f"until_iso={new_past}",
        ],
    })
    snoozed = _active_snoozed_ids(svc, [_StubTask("myproj/1")], now=now)
    assert snoozed == set(), (
        "An item whose latest snooze has expired must be visible "
        "again — the older still-future snooze should not mask it."
    )


def test_active_snoozed_ids_skips_items_with_no_snooze() -> None:
    from pollypm.web_api.service import _active_snoozed_ids

    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    svc = _StubSvc({})
    snoozed = _active_snoozed_ids(svc, [_StubTask("myproj/1")], now=now)
    assert snoozed == set()


def test_snooze_writer_persists_structured_until_iso_marker(
    config, monkeypatch,
) -> None:
    """The snooze helper must persist ``until_iso=...`` so the reader
    can parse the wake-time back without regex-guessing on free-form
    text. Regression for the original P0: the writer encoded the wake
    time only inside the human ``snoozed until <iso>`` blob, which
    the reader never honored."""
    from pollypm.web_api import service as web_service
    from pollypm.work.models import WorkStatus

    class _StubInboxTask:
        task_id = "myproj/1"
        work_status = WorkStatus.IN_PROGRESS
        project = "myproj"
        task_number = 1
        title = "Inbox item"
        description = "body"
        # Canonical inbox predicate (#2060 round-5) requires a user
        # role / plan_review label / current human node — chat-flow
        # alone is not sufficient. Give this stub a ``requester=user``
        # so ``_roles_match_user`` accepts it as an inbox member.
        flow_template_id = "chat"
        labels: list[str] = []
        roles: dict = {"requester": "user"}
        current_node_id = None

    captured: dict[str, object] = {}

    class _RecordingSvc:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, item_id):
            return _StubInboxTask()

        def add_context(self, item_id, actor, text, *, entry_type):
            captured["text"] = text
            captured["entry_type"] = entry_type

    def fake_factory(*, config, project_key, project_path):
        return _RecordingSvc()

    monkeypatch.setattr(
        "pollypm.work.factory.create_work_service", fake_factory,
    )
    # The service-layer helper calls ``_task_to_detail(svc.get(...))``
    # at the tail; the StubInboxTask isn't a full Task so short-circuit
    # by patching the detail builder too.
    monkeypatch.setattr(
        web_service, "_task_to_detail",
        lambda task: _make_task_detail(task_id="myproj/1"),
    )

    web_service.snooze_inbox_item(
        config, "myproj/1", duration_seconds=3600, reason="later",
    )
    text = str(captured.get("text", ""))
    assert captured.get("entry_type") == "snooze"
    assert text.startswith("until_iso="), (
        f"snooze row must lead with the parseable marker; got {text!r}"
    )
    # And the reader is happy with it round-trip.
    parsed = web_service._parse_snooze_until(text)
    assert parsed is not None
    assert parsed > datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Inbox list bulk snooze path (#2060 round-2)
#
# Round 1 of #2060 shipped ``_active_snoozed_ids`` as a per-task
# ``svc.get_context(entry_type='snooze', limit=1)`` loop. Codex's
# round-2 review flagged the N+1: for a 10-task inbox page that
# means 10 round-trips to the work-service just to decide visibility.
# Round 2 routes the call through ``svc.latest_snoozes_bulk`` (one
# SQL on pg) and falls back to the per-task loop only when the
# backing service lacks the bulk method. These tests pin both
# behaviours so a refactor can't silently regress to N+1.
# ---------------------------------------------------------------------------


def test_active_snoozed_ids_uses_bulk_helper_when_available() -> None:
    """A backing service with ``latest_snoozes_bulk`` is called ONCE.

    Mocks both ``latest_snoozes_bulk`` and ``get_context`` on the
    same stub; the bulk-aware path must call the bulk helper exactly
    once and must NOT touch ``get_context`` (the per-task fallback).
    """
    from pollypm.web_api.service import _active_snoozed_ids

    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    future = (now + timedelta(hours=2)).isoformat()
    past = (now - timedelta(hours=2)).isoformat()

    class _StubEntry:
        def __init__(self, text: str) -> None:
            self.text = text
            self.actor = "api"
            self.entry_type = "snooze"

    class _StubTask:
        def __init__(self, tid: str) -> None:
            self.task_id = tid

    class _BulkSvc:
        def __init__(self) -> None:
            self.bulk_calls = 0
            self.bulk_keys: list = []
            self.per_task_calls = 0

        def latest_snoozes_bulk(self, keys):
            self.bulk_calls += 1
            self.bulk_keys = list(keys)
            # Half the tasks are snoozed (future), half expired (past),
            # rest absent. Mirrors the round-1 unit tests but at the
            # bulk boundary.
            return {
                ("myproj", 1): _StubEntry(f"until_iso={future}"),
                ("myproj", 2): _StubEntry(f"until_iso={past}"),
                ("myproj", 3): _StubEntry(f"until_iso={future}"),
                ("myproj", 4): _StubEntry(f"until_iso={past}"),
                ("myproj", 5): _StubEntry(f"until_iso={future}"),
                # ids 6..10 absent (not snoozed).
            }

        def get_context(self, *a, **kw):  # pragma: no cover
            self.per_task_calls += 1
            raise AssertionError(
                "bulk-aware backend must not fall back to per-task "
                "get_context (#2060 round-2)"
            )

    tasks = [_StubTask(f"myproj/{i}") for i in range(1, 11)]
    svc = _BulkSvc()
    snoozed = _active_snoozed_ids(svc, tasks, now=now)
    assert svc.bulk_calls == 1, (
        "inbox list path must call latest_snoozes_bulk exactly once; "
        f"saw {svc.bulk_calls}"
    )
    assert svc.per_task_calls == 0
    # Bulk call received all 10 keys so the helper has the full set
    # to scan in one shot, not N calls of one key each.
    assert len(svc.bulk_keys) == 10
    # Active set is the future-wake ids only.
    assert snoozed == {"myproj/1", "myproj/3", "myproj/5"}


def test_active_snoozed_ids_falls_back_to_per_task_when_bulk_missing() -> None:
    """A backend without the bulk helper still works (legacy path).

    This protects the mock-service / sqlite shape: bulk is a pg
    optimisation, but the predicate must still return correct data
    when the method is absent. Falls through to the round-1 loop.
    """
    from pollypm.web_api.service import _active_snoozed_ids

    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    future = (now + timedelta(hours=2)).isoformat()

    class _StubEntry:
        def __init__(self, text: str) -> None:
            self.text = text
            self.actor = "api"
            self.entry_type = "snooze"

    class _StubTask:
        def __init__(self, tid: str) -> None:
            self.task_id = tid

    class _LegacySvc:
        # NB: no ``latest_snoozes_bulk`` attribute.
        def __init__(self) -> None:
            self.calls = 0

        def get_context(self, task_id, *, entry_type=None, limit=None):
            self.calls += 1
            assert entry_type == "snooze"
            if task_id == "myproj/1":
                return [_StubEntry(f"until_iso={future}")]
            return []

    svc = _LegacySvc()
    tasks = [_StubTask("myproj/1"), _StubTask("myproj/2")]
    snoozed = _active_snoozed_ids(svc, tasks, now=now)
    assert snoozed == {"myproj/1"}
    # One call per task — the documented legacy behaviour.
    assert svc.calls == 2


def test_shared_snooze_predicate_lives_in_work_module() -> None:
    """The parser + ``is_snooze_active`` predicate live in
    ``pollypm.work.inbox_snooze`` so cockpit can adopt them without
    pulling in the web layer (#2060 round-2).

    This test exists to keep the module from being silently moved
    back into ``pollypm.web_api.service`` — the whole point of the
    extraction is shareability with cockpit code that must not
    depend on FastAPI.
    """
    from pollypm.work import inbox_snooze

    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    future = (now + timedelta(hours=2)).isoformat()
    past = (now - timedelta(hours=2)).isoformat()
    assert inbox_snooze.is_snooze_active(
        f"until_iso={future}", now=now,
    ) is True
    assert inbox_snooze.is_snooze_active(
        f"until_iso={past}", now=now,
    ) is False
    assert inbox_snooze.parse_snooze_until(
        f"until_iso={future}"
    ) is not None


# ---------------------------------------------------------------------------
# Inbox membership guard (#2060 round-3, blocker 1)
#
# Round-2 left every write helper probing ``svc.get(...)`` alone before
# mutating. That returns ANY task with the id, not just inbox rows —
# so a caller could POST /inbox/<id>/{archive,snooze,reply,mark-read,
# promote-to-task} against a plain-task flow row and close / mutate
# it via this endpoint even though GET /inbox would 404 the same id.
# The fix is a small ``_is_inbox_member`` predicate (chat-flow OR
# plan-review label / flow, matching ``_task_to_inbox_item``) that
# every write helper consults right after the existence probe and
# raises ``not_found`` for non-members. These tests pin the gate so
# a refactor can't drop it silently. Codex round-3 explicitly named
# archive + snooze; we cover all five for symmetry.
# ---------------------------------------------------------------------------


class _NonInboxStubTask:
    """Pretends to be a regular work task (not an inbox row).

    Post-#2060 round-5 the membership predicate is the canonical
    :func:`pollypm.work.inbox_view.is_inbox_task` (cockpit / rail /
    dashboard). That requires a ``user`` role, exact ``plan_review``
    label, or a current human flow node — none of which this stub
    carries — so the write helpers must reject it.
    """

    task_id = "myproj/1"
    project = "myproj"
    task_number = 1
    title = "Regular work task"
    description = "not an inbox row"
    flow_template_id = "standard"  # NOT 'chat'
    labels: list[str] = []  # NO plan_review label
    roles: dict = {}  # NO user role
    current_node_id = None  # NO current human node

    @property
    def work_status(self):  # noqa: D401
        from pollypm.work.models import WorkStatus
        return WorkStatus.IN_PROGRESS


class _MembershipRecordingSvc:
    """Records whether any mutating method was called.

    Membership guard must reject the request BEFORE any of these run.
    Each ``raise AssertionError`` would surface in pytest if the
    write helper slipped past the guard.
    """

    def __init__(self) -> None:
        self.archive_calls = 0
        self.add_context_calls = 0
        self.add_reply_calls = 0
        self.mark_read_calls = 0
        self.create_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, item_id):
        return _NonInboxStubTask()

    def archive_task(self, *a, **kw):
        self.archive_calls += 1
        raise AssertionError(
            "membership guard must block archive_task on a non-inbox row"
        )

    def add_context(self, *a, **kw):
        self.add_context_calls += 1
        raise AssertionError(
            "membership guard must block add_context on a non-inbox row"
        )

    def add_reply(self, *a, **kw):
        self.add_reply_calls += 1
        raise AssertionError(
            "membership guard must block add_reply on a non-inbox row"
        )

    def mark_read(self, *a, **kw):
        self.mark_read_calls += 1
        raise AssertionError(
            "membership guard must block mark_read on a non-inbox row"
        )

    def create(self, *a, **kw):
        self.create_calls += 1
        raise AssertionError(
            "membership guard must block create on a non-inbox row"
        )


def _install_membership_stub(monkeypatch) -> _MembershipRecordingSvc:
    """Patch ``create_work_service`` to return the recording stub."""
    svc = _MembershipRecordingSvc()

    def fake_factory(*, config, project_key, project_path):
        return svc

    monkeypatch.setattr(
        "pollypm.work.factory.create_work_service", fake_factory,
    )
    return svc


def test_archive_rejects_non_inbox_task(config, monkeypatch) -> None:
    """``POST /inbox/<id>/archive`` on a non-inbox row must 404 cleanly."""
    from pollypm.web_api import service as web_service
    from pollypm.web_api.errors import APIError

    svc = _install_membership_stub(monkeypatch)
    with pytest.raises(APIError) as excinfo:
        web_service.archive_inbox_item(config, "myproj/1", reason="oops")
    assert excinfo.value.status_code == 404
    assert excinfo.value.code == "not_found"
    # Critical: NEITHER the canonical archive transition NOR the
    # archive-reason context note ran. The stub asserts on either.
    assert svc.archive_calls == 0
    assert svc.add_context_calls == 0


def test_snooze_rejects_non_inbox_task(config, monkeypatch) -> None:
    """``POST /inbox/<id>/snooze`` on a non-inbox row must 404 cleanly."""
    from pollypm.web_api import service as web_service
    from pollypm.web_api.errors import APIError

    svc = _install_membership_stub(monkeypatch)
    with pytest.raises(APIError) as excinfo:
        web_service.snooze_inbox_item(
            config, "myproj/1", duration_seconds=3600,
        )
    assert excinfo.value.status_code == 404
    assert excinfo.value.code == "not_found"
    # Snooze-row write (entry_type='snooze') must not be persisted.
    assert svc.add_context_calls == 0


def test_mark_read_rejects_non_inbox_task(config, monkeypatch) -> None:
    from pollypm.web_api import service as web_service
    from pollypm.web_api.errors import APIError

    svc = _install_membership_stub(monkeypatch)
    with pytest.raises(APIError) as excinfo:
        web_service.mark_read_inbox_item(config, "myproj/1")
    assert excinfo.value.status_code == 404
    assert excinfo.value.code == "not_found"
    assert svc.mark_read_calls == 0


def test_reply_rejects_non_inbox_task(config, monkeypatch) -> None:
    from pollypm.web_api import service as web_service
    from pollypm.web_api.errors import APIError

    svc = _install_membership_stub(monkeypatch)
    with pytest.raises(APIError) as excinfo:
        web_service.reply_inbox_item(config, "myproj/1", body="ping")
    assert excinfo.value.status_code == 404
    assert excinfo.value.code == "not_found"
    assert svc.add_reply_calls == 0


def test_promote_rejects_non_inbox_task(config, monkeypatch) -> None:
    from pollypm.web_api import service as web_service
    from pollypm.web_api.errors import APIError

    svc = _install_membership_stub(monkeypatch)
    with pytest.raises(APIError) as excinfo:
        web_service.promote_inbox_to_task(config, "myproj/1")
    assert excinfo.value.status_code == 404
    assert excinfo.value.code == "not_found"
    # Most damaging side effect: a derived task gets created.
    assert svc.create_calls == 0


def test_membership_helper_matches_canonical_inbox_predicate() -> None:
    """The API membership helper must mirror the canonical predicate.

    Post-#2060 round-5 the web layer routes through
    :func:`pollypm.work.inbox_view.is_inbox_task` — the same predicate
    cockpit, the rail, and the dashboard use. The contract is:

    * Chat-flow with a ``user`` role -> inbox member.
    * Exact ``plan_review`` label -> inbox member.
    * Plain task with no user role / no exact plan_review label /
      no current human node -> NOT a member, even if the flow_id
      contains the substring ``plan``.

    Substring matches on ``plan_review`` (e.g. ``not_plan_review``,
    ``planning``) MUST be rejected — the round-3 web helper's
    ``"plan_review" in lbl`` widening is what Codex round-5 caught.
    """
    from pollypm.web_api.service import _is_inbox_member

    class _ChatWithUserRole:
        flow_template_id = "chat"
        labels: list[str] = []
        roles = {"requester": "user"}
        current_node_id = None

    class _PlanReviewLabel:
        flow_template_id = "standard"
        labels = ["plan_review"]
        roles: dict = {}
        current_node_id = None

    class _ChatNoUserRole:
        # Chat-flow but no user role + no current node — would have
        # been accepted by the old web-layer predicate, must now be
        # rejected (canonical contract).
        flow_template_id = "chat"
        labels: list[str] = []
        roles: dict = {}
        current_node_id = None

    class _NearMissPlanLabel:
        # Substring would have matched the old helper; canonical
        # predicate requires exact ``plan_review``.
        flow_template_id = "standard"
        labels = ["not_plan_review"]
        roles: dict = {}
        current_node_id = None

    class _NonInbox:
        flow_template_id = "standard"
        labels: list[str] = []
        roles: dict = {}
        current_node_id = None

    assert _is_inbox_member(_ChatWithUserRole()) is True
    assert _is_inbox_member(_PlanReviewLabel()) is True
    assert _is_inbox_member(_ChatNoUserRole()) is False
    assert _is_inbox_member(_NearMissPlanLabel()) is False
    assert _is_inbox_member(_NonInbox()) is False


# ---------------------------------------------------------------------------
# Round-5 canonical-predicate negative coverage (#2060, Codex 15:41 UTC).
#
# Round-3 added a web-layer ``_is_inbox_member`` that accepted any
# chat-flow task plus substring plan labels. Codex round-5 caught the
# drift: cockpit / rail / dashboard use the stricter
# ``pollypm.work.inbox_view.is_inbox_task`` (user role / exact
# plan_review / human current node), so a chat task that the read
# surface 404s could still be mutated through the write endpoints.
#
# Codex's ask, verbatim:
#   "at minimum, add negative tests for a chat-flow task with no user
#    role/current human node and for near-miss plan labels so the
#    write surface cannot widen by substring accident."
#
# These tests are designed to FAIL on the round-3 predicate (chat
# alone passes; ``not_plan_review`` substring-matches) and PASS once
# the write helpers route through ``is_inbox_task``.
# ---------------------------------------------------------------------------


class _ChatNoUserRoleStubTask:
    """Chat-flow task with no user role / no human current node.

    Old (round-3) predicate: ``flow_template_id == 'chat'`` -> accept.
    Canonical predicate: chat alone is insufficient; needs a ``user``
    role / exact ``plan_review`` label / current human node.
    """

    task_id = "myproj/1"
    project = "myproj"
    task_number = 1
    title = "Headless chat-flow row"
    description = "should not be mutable via /inbox writes"
    flow_template_id = "chat"
    labels: list[str] = []
    roles: dict = {}  # no user role
    current_node_id = None  # no current human node

    @property
    def work_status(self):
        from pollypm.work.models import WorkStatus
        return WorkStatus.IN_PROGRESS


class _NearMissPlanLabelStubTask:
    """Task whose only ``plan``-flavoured label substring-matches
    ``plan_review`` but is not exactly that label.

    Old (round-3) predicate: ``"plan_review" in lbl`` -> accept on
    ``not_plan_review``. Canonical predicate: exact match only.
    """

    task_id = "myproj/1"
    project = "myproj"
    task_number = 1
    title = "Near-miss plan label"
    description = "label is not_plan_review, not plan_review"
    flow_template_id = "standard"
    labels = ["not_plan_review", "planning"]
    roles: dict = {}
    current_node_id = None

    @property
    def work_status(self):
        from pollypm.work.models import WorkStatus
        return WorkStatus.IN_PROGRESS


class _CanonicalRejectingSvc:
    """Recording stub whose ``get`` returns a configurable task.

    Like ``_MembershipRecordingSvc`` but parameterised on which fake
    task to return, so the round-5 negative tests can exercise the
    two specific shapes Codex called out without duplicating the
    fake-factory wiring.
    """

    def __init__(self, task) -> None:
        self._task = task
        self.archive_calls = 0
        self.add_context_calls = 0
        self.add_reply_calls = 0
        self.mark_read_calls = 0
        self.create_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, item_id):
        return self._task

    def archive_task(self, *a, **kw):
        self.archive_calls += 1
        raise AssertionError(
            "canonical predicate must block archive on non-inbox row"
        )

    def add_context(self, *a, **kw):
        self.add_context_calls += 1
        raise AssertionError(
            "canonical predicate must block add_context on non-inbox row"
        )

    def add_reply(self, *a, **kw):
        self.add_reply_calls += 1
        raise AssertionError(
            "canonical predicate must block add_reply on non-inbox row"
        )

    def mark_read(self, *a, **kw):
        self.mark_read_calls += 1
        raise AssertionError(
            "canonical predicate must block mark_read on non-inbox row"
        )

    def create(self, *a, **kw):
        self.create_calls += 1
        raise AssertionError(
            "canonical predicate must block create on non-inbox row"
        )


def _install_canonical_stub(monkeypatch, task) -> _CanonicalRejectingSvc:
    svc = _CanonicalRejectingSvc(task)

    def fake_factory(*, config, project_key, project_path):
        return svc

    monkeypatch.setattr(
        "pollypm.work.factory.create_work_service", fake_factory,
    )
    return svc


def test_archive_rejects_chat_flow_task_with_no_user_role(
    config, monkeypatch,
) -> None:
    """Chat-flow without a user role is NOT an inbox member.

    The old round-3 predicate accepted any ``flow_template_id ==
    'chat'`` and would have allowed this archive to proceed —
    write-side drift past the cockpit / rail / dashboard inbox
    surface. The canonical :func:`is_inbox_task` requires a user
    role / exact plan_review label / current human node.
    """
    from pollypm.web_api import service as web_service

    svc = _install_canonical_stub(monkeypatch, _ChatNoUserRoleStubTask())
    with pytest.raises(APIError) as excinfo:
        web_service.archive_inbox_item(config, "myproj/1", reason="oops")
    assert excinfo.value.status_code == 404
    assert excinfo.value.code == "not_found"
    assert svc.archive_calls == 0
    assert svc.add_context_calls == 0


def test_archive_rejects_near_miss_plan_label(
    config, monkeypatch,
) -> None:
    """Labels like ``not_plan_review`` / ``planning`` MUST NOT widen
    the write surface. Round-3 used ``"plan_review" in lbl`` which
    matched both; canonical predicate is exact equality."""
    from pollypm.web_api import service as web_service

    svc = _install_canonical_stub(monkeypatch, _NearMissPlanLabelStubTask())
    with pytest.raises(APIError) as excinfo:
        web_service.archive_inbox_item(config, "myproj/1", reason="oops")
    assert excinfo.value.status_code == 404
    assert excinfo.value.code == "not_found"
    assert svc.archive_calls == 0
    assert svc.add_context_calls == 0


def test_snooze_rejects_chat_flow_task_with_no_user_role(
    config, monkeypatch,
) -> None:
    """Snooze must agree with archive's canonical predicate.

    Otherwise a caller could snooze a row the cockpit never surfaces
    — the row gets a future ``until_iso`` marker that nothing reads.
    """
    from pollypm.web_api import service as web_service

    svc = _install_canonical_stub(monkeypatch, _ChatNoUserRoleStubTask())
    with pytest.raises(APIError) as excinfo:
        web_service.snooze_inbox_item(
            config, "myproj/1", duration_seconds=3600,
        )
    assert excinfo.value.status_code == 404
    assert excinfo.value.code == "not_found"
    assert svc.add_context_calls == 0


def test_snooze_rejects_near_miss_plan_label(
    config, monkeypatch,
) -> None:
    from pollypm.web_api import service as web_service

    svc = _install_canonical_stub(monkeypatch, _NearMissPlanLabelStubTask())
    with pytest.raises(APIError) as excinfo:
        web_service.snooze_inbox_item(
            config, "myproj/1", duration_seconds=3600,
        )
    assert excinfo.value.status_code == 404
    assert excinfo.value.code == "not_found"
    assert svc.add_context_calls == 0


def test_promote_rejects_chat_flow_task_with_no_user_role(
    config, monkeypatch,
) -> None:
    """Promote is the most damaging widening surface: it derives a
    new task from the source. Canonical predicate must reject so
    the API cannot mint tasks from rows cockpit never surfaced."""
    from pollypm.web_api import service as web_service

    svc = _install_canonical_stub(monkeypatch, _ChatNoUserRoleStubTask())
    with pytest.raises(APIError) as excinfo:
        web_service.promote_inbox_to_task(config, "myproj/1")
    assert excinfo.value.status_code == 404
    assert excinfo.value.code == "not_found"
    assert svc.create_calls == 0


def test_promote_rejects_near_miss_plan_label(
    config, monkeypatch,
) -> None:
    from pollypm.web_api import service as web_service

    svc = _install_canonical_stub(monkeypatch, _NearMissPlanLabelStubTask())
    with pytest.raises(APIError) as excinfo:
        web_service.promote_inbox_to_task(config, "myproj/1")
    assert excinfo.value.status_code == 404
    assert excinfo.value.code == "not_found"
    assert svc.create_calls == 0


# ---------------------------------------------------------------------------
# Archive reason ordering (#2060 round-4, blocker 2)
#
# Round-3 wrote the ``archive reason:`` context note BEFORE calling
# the strict archive. If the strict archive lost a concurrent race
# (rowcount == 0 → InvalidTransitionError) the API surfaced 409 to
# the caller, but the reason note had already been persisted —
# stamping a "this is why I archived" annotation onto a task this
# caller never actually archived. Round-4 reorders: strict archive
# first, then (only on success) the reason note. These tests pin the
# ordering so the regression can't slip back in.
# ---------------------------------------------------------------------------


class _ConcurrentLoserStubTask:
    """Looks like an inbox item so the membership guard accepts it;
    the conflict is raised by ``archive_task``, not by the predicate.

    Canonical inbox predicate (#2060 round-5) requires a user role
    in addition to chat-flow, so include ``requester=user`` to mimic
    a real chat thread surfaced via cockpit / GET /inbox.
    """

    task_id = "myproj/1"
    project = "myproj"
    task_number = 1
    title = "Inbox item"
    description = "ping"
    flow_template_id = "chat"
    labels: list[str] = []
    roles: dict = {"requester": "user"}
    current_node_id = None

    @property
    def work_status(self):  # noqa: D401
        from pollypm.work.models import WorkStatus
        return WorkStatus.IN_PROGRESS


class _ArchiveRaceLoserSvc:
    """Records every mutation and raises on ``archive_task``.

    Mirrors the pg ``strict=True`` rowcount==0 path: the conditional
    UPDATE returned zero rows because a concurrent archiver beat us,
    so the canonical writer raises ``InvalidTransitionError``. The
    test asserts the reason note was NOT persisted before that raise.
    """

    def __init__(self) -> None:
        self.archive_calls = 0
        self.add_context_calls: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, item_id):
        return _ConcurrentLoserStubTask()

    def archive_task(self, item_id, *, actor, strict=False):
        self.archive_calls += 1
        from pollypm.work.service_support import InvalidTransitionError
        raise InvalidTransitionError(
            f"Inbox item {item_id} already terminal (concurrent race loser)."
        )

    def add_context(self, *args, **kwargs):
        # Record the call so the test can assert it never happened.
        self.add_context_calls.append((args, kwargs))


def test_archive_strict_loser_does_not_persist_reason_note(
    config, monkeypatch,
) -> None:
    """Concurrent archive loser must not stamp a reason note.

    Reproduces #2060 round-4 blocker 2: when ``archive_task(strict=True)``
    raises ``InvalidTransitionError`` (rowcount==0 because another
    writer won the race), the API surfaces 409. Before the fix, the
    handler had already persisted ``archive reason: <text>`` as a
    context note — falsely attributing an archive to a caller whose
    transition lost. The fix reorders so the note is only written
    after the strict transition succeeds.

    This test fails on the broken ordering (note persisted) and
    passes after the fix (note skipped).
    """
    from pollypm.web_api import service as web_service
    from pollypm.web_api.errors import APIError

    svc = _ArchiveRaceLoserSvc()

    def fake_factory(*, config, project_key, project_path):
        return svc

    monkeypatch.setattr(
        "pollypm.work.factory.create_work_service", fake_factory,
    )

    with pytest.raises(APIError) as excinfo:
        web_service.archive_inbox_item(
            config, "myproj/1", reason="testing", actor="api",
        )
    # 1. Loser sees the documented 409 invalid_state envelope.
    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "invalid_state"
    # 2. Strict archive WAS attempted (otherwise we wouldn't have
    #    raised, and the ordering question would be moot).
    assert svc.archive_calls == 1
    # 3. Critical: reason note was NOT persisted. A failed archive
    #    must leave no ``archive reason:`` audit row.
    assert svc.add_context_calls == [], (
        "archive_inbox_item persisted a reason note for a 409-loser "
        "concurrent archive. The reason write must happen AFTER the "
        "strict transition succeeds (#2060 round-4)."
    )


def test_archive_strict_success_persists_reason_note_after_transition(
    config, monkeypatch,
) -> None:
    """Happy path still records the reason — but only after archive.

    The fix must not regress the existing behaviour: on a successful
    archive, the reason note IS persisted. This guards against a
    too-aggressive fix that drops the note entirely.
    """
    from pollypm.web_api import service as web_service

    class _SuccessSvc:
        def __init__(self) -> None:
            self.archive_calls = 0
            self.add_context_calls: list[tuple] = []
            self.call_order: list[str] = []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, item_id):
            return _ConcurrentLoserStubTask()

        def archive_task(self, item_id, *, actor, strict=False):
            self.archive_calls += 1
            self.call_order.append("archive_task")
            return None

        def add_context(self, *args, **kwargs):
            self.add_context_calls.append((args, kwargs))
            self.call_order.append("add_context")

    svc = _SuccessSvc()

    def fake_factory(*, config, project_key, project_path):
        return svc

    # Bypass the final ``_task_to_detail`` since the stub task does
    # not carry every relationship attribute — this test cares only
    # about the call ordering of archive_task vs add_context, not
    # the response envelope shape (that's pinned elsewhere).
    monkeypatch.setattr(
        "pollypm.work.factory.create_work_service", fake_factory,
    )
    monkeypatch.setattr(
        web_service, "_task_to_detail", lambda task, plan=None: None,
    )
    web_service.archive_inbox_item(
        config, "myproj/1", reason="testing", actor="api",
    )
    # Both ran, and archive_task ran FIRST (the ordering is the fix).
    assert svc.archive_calls == 1
    assert len(svc.add_context_calls) == 1
    assert svc.call_order.index("archive_task") < svc.call_order.index(
        "add_context"
    ), (
        "Reason note was written before archive_task — the round-4 "
        "ordering fix has regressed (#2060)."
    )
    # Reason payload is intact.
    args, kwargs = svc.add_context_calls[0]
    # Positional shape: (item_id, actor, text, entry_type=...).
    assert args[0] == "myproj/1"
    assert "archive reason: testing" in args[2]
