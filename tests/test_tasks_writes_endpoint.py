"""Phase 2 surface #3 — tasks claim/cancel/reassign + PATCH tests.

Run via::

    pytest --noconftest tests/test_tasks_writes_endpoint.py -v --timeout=180

The ``--noconftest`` flag skips the repo's heavy conftest (which spins
up real pg pools etc.); every fixture this test needs is defined
inline. The work-service factory is patched to return an in-memory
fake whose surface matches the slice of ``PgWorkService`` the new
write helpers (``claim_task`` / ``cancel_task`` / ``reassign_task`` /
``patch_task`` in :mod:`pollypm.web_api.service`) reach for.

The fake is intentionally tiny — just enough state to assert on
claim/cancel/reassign/patch transitions + the typed-error envelope.
The pg integration suite (``tests/test_pg_work_service*``) already
covers the underlying ``svc.claim`` / ``svc.cancel`` / ``svc.update``
contracts; here we're testing the route → service → work-service
plumbing.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
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
from pollypm.models import KnownProject, ProjectKind, ProviderKind, RuntimeKind
from pollypm.web_api import create_app, ensure_token
from pollypm.work.models import Priority, TaskType, WorkStatus
from pollypm.work.service_support import (
    InvalidTransitionError,
    TaskNotFoundError,
    ValidationError as WorkValidationError,
)


# ---------------------------------------------------------------------------
# In-memory fake work-service
# ---------------------------------------------------------------------------


@dataclass
class FakeTask:
    """Subset of :class:`pollypm.work.models.Task` the API helpers read.

    ``_task_to_detail`` (the converter in ``web_api/service.py``) reads
    a long list of attributes; we declare exactly that surface here so
    the converter doesn't ``AttributeError`` on a missing slot.
    """

    project: str
    task_number: int
    title: str = "Test task"
    description: str = "desc"
    work_status: WorkStatus = WorkStatus.QUEUED
    type: TaskType = TaskType.TASK
    priority: Priority = Priority.NORMAL
    assignee: str | None = None
    current_node_id: str | None = "node-start"
    plan_version: int = 1
    predecessor_task_id: str | None = None
    flow_template_id: str = "standard"
    flow_template_version: int = 1
    requires_human_review: bool = False
    acceptance_criteria: str | None = None
    constraints: str | None = None
    labels: list[str] = field(default_factory=list)
    relevant_files: list[str] = field(default_factory=list)
    parent_project: str | None = None
    parent_task_number: int | None = None
    blocks: list[tuple[str, int]] = field(default_factory=list)
    blocked_by: list[tuple[str, int]] = field(default_factory=list)
    relates_to: list[tuple[str, int]] = field(default_factory=list)
    children: list[tuple[str, int]] = field(default_factory=list)
    supersedes_project: str | None = None
    supersedes_task_number: int | None = None
    superseded_by_project: str | None = None
    superseded_by_task_number: int | None = None
    roles: dict[str, str] = field(default_factory=dict)
    external_refs: dict[str, str] = field(default_factory=dict)
    created_at: datetime | None = None
    created_by: str = "tester"
    updated_at: datetime | None = None
    transitions: list[Any] = field(default_factory=list)
    executions: list[Any] = field(default_factory=list)
    context: list[Any] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    session_count: int = 0

    def __post_init__(self) -> None:
        now = datetime.now(timezone.utc)
        if self.created_at is None:
            self.created_at = now
        if self.updated_at is None:
            self.updated_at = now

    @property
    def task_id(self) -> str:
        return f"{self.project}/{self.task_number}"


class FakeWorkService:
    """Mimics the slice of ``PgWorkService`` exercised by Phase 2 writes.

    Implements ``get``, ``queue``, ``claim``, ``cancel`` and ``update``
    against a dict keyed by ``task_id``. Each method updates the
    in-memory state the same way the pg path does so the integration
    tests can re-read through ``get_task_detail`` and see the
    transition.
    """

    def __init__(self, store: dict[str, FakeTask]):
        self._store = store
        self._lock = threading.Lock()

    # The context-manager protocol — ``create_work_service`` returns
    # something usable as ``with create_work_service(...) as svc:``.
    def __enter__(self) -> "FakeWorkService":
        return self

    def __exit__(self, *exc: object) -> None:  # noqa: D401 — context-mgr
        return None

    def get(self, task_id: str) -> FakeTask:
        task = self._store.get(task_id)
        if task is None:
            raise TaskNotFoundError(f"Task '{task_id}' not found.")
        return task

    def queue(self, task_id: str, actor: str) -> FakeTask:  # noqa: ARG002
        task = self.get(task_id)
        if task.work_status != WorkStatus.DRAFT:
            raise InvalidTransitionError(
                f"Cannot queue task in '{task.work_status.value}' state. "
                "Only draft tasks can be queued."
            )
        task.work_status = WorkStatus.QUEUED
        task.updated_at = datetime.now(timezone.utc)
        return task

    def claim(self, task_id: str, actor: str) -> FakeTask:
        task = self.get(task_id)
        if task.work_status == WorkStatus.IN_PROGRESS:
            raise InvalidTransitionError(
                f"Task {task_id} is already claimed by "
                f"'{task.assignee or 'another actor'}'."
            )
        if task.work_status != WorkStatus.QUEUED:
            raise InvalidTransitionError(
                f"Cannot claim task in '{task.work_status.value}' state."
            )
        task.work_status = WorkStatus.IN_PROGRESS
        task.assignee = actor
        task.updated_at = datetime.now(timezone.utc)
        return task

    def cancel(self, task_id: str, actor: str, reason: str) -> FakeTask:  # noqa: ARG002
        task = self.get(task_id)
        if task.work_status in (WorkStatus.DONE, WorkStatus.CANCELLED):
            raise InvalidTransitionError(
                f"Cannot cancel task in terminal state "
                f"{task.work_status.value!r}."
            )
        task.work_status = WorkStatus.CANCELLED
        task.updated_at = datetime.now(timezone.utc)
        return task

    _UPDATE_ALLOWED = {
        "title", "description", "priority", "labels", "roles",
        "acceptance_criteria", "constraints", "relevant_files",
        "assignee", "external_refs",
    }

    def update(self, task_id: str, **fields: object) -> FakeTask:
        # Mirror PgWorkService.update — refuse work_status and reject
        # unknown columns so the API surface assertions match the real
        # backend behaviour.
        if "work_status" in fields:
            raise WorkValidationError(
                "Cannot change work_status via update()."
            )
        with self._lock:
            task = self.get(task_id)
            for key, value in fields.items():
                if key not in self._UPDATE_ALLOWED:
                    raise WorkValidationError(
                        f"Field '{key}' is not updatable."
                    )
                setattr(task, key, value)
            task.updated_at = datetime.now(timezone.utc)
            return task


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "myproj"
    root.mkdir()
    (root / ".pollypm").mkdir()
    return root


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / ".pollypm").mkdir()
    return root


@pytest.fixture
def api_config(project_root: Path, workspace_root: Path) -> PollyPMConfig:
    base_dir = workspace_root / ".pollypm"
    state_db = base_dir / "state.db"
    return PollyPMConfig(
        project=ProjectSettings(
            name="PollyPM",
            root_dir=workspace_root,
            tmux_session="pollypm-test",
            workspace_root=workspace_root,
            base_dir=base_dir,
            logs_dir=base_dir / "logs",
            snapshots_dir=base_dir / "snapshots",
            state_db=state_db,
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
def token_path(tmp_path: Path) -> Path:
    return tmp_path / "api-token"


@pytest.fixture
def token(token_path: Path) -> str:
    value, _ = ensure_token(token_path)
    return value


@pytest.fixture
def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def task_store() -> dict[str, FakeTask]:
    """Shared in-memory task store. Per-test isolation via fixture scope."""
    return {}


@pytest.fixture(autouse=True)
def patched_work_service(
    task_store: dict[str, FakeTask], monkeypatch: pytest.MonkeyPatch
):
    """Replace ``create_work_service`` with a fake.

    The web-api service module imports ``create_work_service`` lazily
    inside each write helper (``queue_task`` / ``claim_task`` / etc.),
    so we patch the factory module itself rather than the import-site.
    """
    from pollypm.work import factory as work_factory

    def _fake_factory(**_kwargs: object) -> FakeWorkService:
        return FakeWorkService(task_store)

    monkeypatch.setattr(work_factory, "create_work_service", _fake_factory)
    # The web-api READ helpers (``_open_work_service_readonly``)
    # already wrap ``create_work_service`` via ``contextlib.contextmanager``
    # so the patch on the factory module reaches both call sites.
    yield


@pytest.fixture
def client(api_config: PollyPMConfig, token_path: Path, token: str) -> TestClient:  # noqa: ARG001
    app = create_app(config=api_config, token_path=token_path)
    return TestClient(app)


def _seed(
    task_store: dict[str, FakeTask],
    *,
    project: str = "myproj",
    n: int = 1,
    **kwargs: object,
) -> FakeTask:
    """Add a task to the in-memory store and return it."""
    task = FakeTask(project=project, task_number=n, **kwargs)  # type: ignore[arg-type]
    task_store[task.task_id] = task
    return task


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------


def test_claim_happy_path(client, auth_headers, task_store) -> None:
    _seed(task_store, n=1, work_status=WorkStatus.QUEUED)
    response = client.post(
        "/api/v1/tasks/myproj/1/claim",
        headers=auth_headers,
        json={"actor": "alice"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["task"]["work_status"] == "in_progress"
    assert body["task"]["assignee"] == "alice"
    assert "myproj/1" in body["message"]


def test_claim_already_claimed_returns_409(client, auth_headers, task_store) -> None:
    _seed(
        task_store, n=2,
        work_status=WorkStatus.IN_PROGRESS, assignee="bob",
    )
    response = client.post(
        "/api/v1/tasks/myproj/2/claim",
        headers=auth_headers,
        json={"actor": "alice"},
    )
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error"]["code"] == "invalid_state"
    assert "already claimed" in body["error"]["message"].lower()


def test_claim_nonexistent_returns_404(client, auth_headers) -> None:
    response = client.post(
        "/api/v1/tasks/myproj/9999/claim",
        headers=auth_headers,
        json={"actor": "alice"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_claim_unknown_project_returns_404(client, auth_headers) -> None:
    response = client.post(
        "/api/v1/tasks/no-such-project/1/claim",
        headers=auth_headers,
        json={"actor": "alice"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


def test_cancel_happy_path(client, auth_headers, task_store) -> None:
    _seed(task_store, n=3, work_status=WorkStatus.IN_PROGRESS, assignee="bob")
    response = client.post(
        "/api/v1/tasks/myproj/3/cancel",
        headers=auth_headers,
        json={"reason": "scope cut"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["task"]["work_status"] == "cancelled"


def test_cancel_without_reason(client, auth_headers, task_store) -> None:
    """``reason`` is optional per spec §5.3."""
    _seed(task_store, n=4, work_status=WorkStatus.QUEUED)
    response = client.post(
        "/api/v1/tasks/myproj/4/cancel", headers=auth_headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["task"]["work_status"] == "cancelled"


def test_cancel_already_cancelled_returns_409(client, auth_headers, task_store) -> None:
    _seed(task_store, n=5, work_status=WorkStatus.CANCELLED)
    response = client.post(
        "/api/v1/tasks/myproj/5/cancel",
        headers=auth_headers,
        json={"reason": "duplicate"},
    )
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error"]["code"] == "invalid_state"


# ---------------------------------------------------------------------------
# Reassign
# ---------------------------------------------------------------------------


def test_reassign_happy_path(client, auth_headers, task_store) -> None:
    _seed(
        task_store, n=6,
        work_status=WorkStatus.IN_PROGRESS, assignee="bob",
    )
    response = client.post(
        "/api/v1/tasks/myproj/6/reassign",
        headers=auth_headers,
        json={"actor": "carol"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["task"]["assignee"] == "carol"
    # Status must NOT change on a reassign — that's exactly the
    # contract that distinguishes ``/reassign`` from ``/claim``.
    assert body["task"]["work_status"] == "in_progress"


def test_reassign_nonexistent_returns_404(client, auth_headers) -> None:
    response = client.post(
        "/api/v1/tasks/myproj/9999/reassign",
        headers=auth_headers,
        json={"actor": "carol"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# PATCH
# ---------------------------------------------------------------------------


def test_patch_labels_replaces_set(client, auth_headers, task_store) -> None:
    _seed(task_store, n=7, labels=["a", "b"])
    response = client.patch(
        "/api/v1/tasks/myproj/7",
        headers=auth_headers,
        json={"labels": ["b", "c"]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # Lists replace, not merge (spec §5.4).
    assert sorted(body["task"]["labels"]) == ["b", "c"]


def test_patch_labels_empty_list_clears(client, auth_headers, task_store) -> None:
    _seed(task_store, n=8, labels=["old"])
    response = client.patch(
        "/api/v1/tasks/myproj/8",
        headers=auth_headers,
        json={"labels": []},
    )
    assert response.status_code == 200, response.text
    assert response.json()["task"]["labels"] == []


def test_patch_status_to_cancelled_routes_through_cancel(
    client, auth_headers, task_store
) -> None:
    """PATCH ``status=cancelled`` proxies the call to ``svc.cancel``."""
    _seed(task_store, n=9, work_status=WorkStatus.QUEUED)
    response = client.patch(
        "/api/v1/tasks/myproj/9",
        headers=auth_headers,
        json={"status": "cancelled"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["task"]["work_status"] == "cancelled"


def test_patch_status_in_progress_returns_422(
    client, auth_headers, task_store
) -> None:
    """Statuses without a direct PATCH→transition map raise 422.

    ``in_progress`` and ``review`` reach the task only via flow
    advance (``claim`` / ``next``). Spec §5.4 framing is "selective
    field updates" — silently no-op'ing a non-edge transition would
    confuse callers, so we surface a hint pointing at the dedicated
    endpoint instead.
    """
    _seed(task_store, n=10, work_status=WorkStatus.IN_PROGRESS)
    response = client.patch(
        "/api/v1/tasks/myproj/10",
        headers=auth_headers,
        json={"status": "review"},
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert "transition" in (body["error"].get("hint") or "").lower()


def test_patch_metadata(client, auth_headers, task_store) -> None:
    _seed(task_store, n=11, external_refs={"github_issue": "old"})
    response = client.patch(
        "/api/v1/tasks/myproj/11",
        headers=auth_headers,
        json={"metadata": {"github_issue": "1234", "slack_thread": "abc"}},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # ``metadata`` lands in ``external_refs`` (work-service column).
    refs = body["task"]["external_refs"]
    assert refs["github_issue"] == "1234"
    assert refs["slack_thread"] == "abc"


def test_patch_invalid_status_name_returns_422(
    client, auth_headers, task_store
) -> None:
    _seed(task_store, n=12)
    response = client.patch(
        "/api/v1/tasks/myproj/12",
        headers=auth_headers,
        json={"status": "not-a-real-status"},
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"]["code"] == "validation_error"


def test_patch_multiple_fields_one_request(
    client, auth_headers, task_store
) -> None:
    """Labels + metadata in one PATCH — both land in the response snapshot."""
    _seed(task_store, n=13, labels=["x"], external_refs={})
    response = client.patch(
        "/api/v1/tasks/myproj/13",
        headers=auth_headers,
        json={
            "labels": ["x", "y"],
            "metadata": {"ref": "ABC-1"},
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert sorted(body["task"]["labels"]) == ["x", "y"]
    assert body["task"]["external_refs"] == {"ref": "ABC-1"}


def test_patch_nonexistent_task_returns_404(client, auth_headers) -> None:
    response = client.patch(
        "/api/v1/tasks/myproj/9999",
        headers=auth_headers,
        json={"labels": ["x"]},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("POST", "/api/v1/tasks/myproj/1/claim", {"actor": "alice"}),
        ("POST", "/api/v1/tasks/myproj/1/cancel", {}),
        ("POST", "/api/v1/tasks/myproj/1/reassign", {"actor": "carol"}),
        ("PATCH", "/api/v1/tasks/myproj/1", {"labels": []}),
    ],
)
def test_writes_require_auth(client, task_store, method, path, body) -> None:
    """Every new write endpoint refuses anonymous requests with 401."""
    _seed(task_store, n=1, work_status=WorkStatus.QUEUED)
    response = client.request(method, path, json=body)
    assert response.status_code == 401, response.text


# ---------------------------------------------------------------------------
# Concurrent writer scenario — last-writer-wins
# ---------------------------------------------------------------------------


def test_concurrent_writers_last_writer_wins(
    client, auth_headers, task_store
) -> None:
    """Two reassigns hit the same task back-to-back; the second wins.

    Spec §2.6: without ``If-Match`` (deferred per cross-cutting Q11),
    default behaviour is last-writer-wins. This locks in that
    contract — no 409 ``unsafe_concurrent_writer`` is raised because
    the optimistic-lock header is absent.
    """
    _seed(
        task_store, n=14,
        work_status=WorkStatus.IN_PROGRESS, assignee="alice",
    )
    first = client.post(
        "/api/v1/tasks/myproj/14/reassign",
        headers=auth_headers,
        json={"actor": "bob"},
    )
    second = client.post(
        "/api/v1/tasks/myproj/14/reassign",
        headers=auth_headers,
        json={"actor": "carol"},
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["task"]["assignee"] == "bob"
    # The second writer wins; no conflict signal because ``If-Match``
    # is not enforced in this PR.
    assert second.json()["task"]["assignee"] == "carol"


# ---------------------------------------------------------------------------
# Module-level smoke — the FastAPI app actually wires the new routes.
# ---------------------------------------------------------------------------


def test_routes_registered_on_app(api_config, token_path, token) -> None:  # noqa: ARG001
    """OpenAPI surface advertises the four new operation IDs."""
    app = create_app(config=api_config, token_path=token_path)
    op_ids = {
        operation.get("operationId")
        for path in app.openapi().get("paths", {}).values()
        for operation in path.values()
        if isinstance(operation, dict)
    }
    assert {"claimTask", "cancelTask", "reassignTask", "patchTask"} <= op_ids
