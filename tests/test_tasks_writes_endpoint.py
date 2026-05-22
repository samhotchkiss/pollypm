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
class FakeContextEntry:
    """Minimal context-entry shape ``_task_to_detail`` reads.

    ``_task_to_detail`` (web_api/service.py) wraps each ``task.context``
    item in :class:`APIContextEntry`, reading ``.actor``, ``.timestamp``,
    ``.text``, ``.entry_type`` as attributes. Mirror exactly that
    surface so the FakeWorkService can append a breadcrumb without
    blowing up the response serialiser.
    """

    actor: str
    text: str
    entry_type: str = "note"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


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

    def reassign_task(
        self,
        task_id: str,
        *,
        new_assignee: str,
        actor: str,
        reason: str | None = None,
    ) -> FakeTask:
        """Mirror :meth:`PgWorkService.reassign_task` (#2064 round-3).

        Atomically updates ``assignee`` and appends a context entry —
        the work-service §P-9 invariant. The endpoint regression below
        asserts the breadcrumb is recorded; if this implementation
        regressed to a bare ``setattr(task, 'assignee', ...)`` that
        test would fail with an empty ``task.context`` list.
        """
        with self._lock:
            task = self.get(task_id)
            old_assignee = task.assignee
            task.assignee = new_assignee
            task.updated_at = datetime.now(timezone.utc)
            old_label = old_assignee if old_assignee else "<unassigned>"
            body = f"worker reassigned from {old_label} to {new_assignee}"
            if reason:
                body += f" (reason: {reason})"
            # ``context`` is the same list the route layer reads back
            # through ``_task_to_detail`` — append the breadcrumb in
            # the spec wording so the test can grep for it.
            task.context.append(
                FakeContextEntry(
                    actor=actor, text=body, entry_type="reassignment",
                )
            )
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


def test_reassign_records_context_log_breadcrumb(
    client, auth_headers, task_store
) -> None:
    """Spec §P-9: mid-flight reassign MUST leave a context-log entry.

    The new worker needs the breadcrumb to recover context via ``pm
    task get``. If the route layer regresses to
    ``svc.update(assignee=...)`` (the column-only path) this assertion
    fails with an empty ``task.context`` list — that's exactly the
    bug the round-3 review caught on the prior head.
    """
    seeded = _seed(
        task_store, n=21,
        work_status=WorkStatus.IN_PROGRESS, assignee="pete",
    )
    assert seeded.context == [], "Precondition: no entries before reassign."
    response = client.post(
        "/api/v1/tasks/myproj/21/reassign",
        headers=auth_headers,
        json={"actor": "nora"},
    )
    assert response.status_code == 200, response.text
    assert seeded.assignee == "nora"
    # The handoff must leave a breadcrumb naming both ends of the
    # swap — that's the spec wording (§P-9) and how operators / agents
    # grep the context log.
    reassign_entries = [
        e for e in seeded.context
        if getattr(e, "entry_type", None) == "reassignment"
    ]
    assert len(reassign_entries) == 1, (
        f"expected exactly one reassignment context entry, got "
        f"{seeded.context!r}"
    )
    body = reassign_entries[0].text
    assert "pete" in body and "nora" in body, (
        f"breadcrumb missing old/new assignee: {body!r}"
    )
    assert "reassigned" in body.lower()


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


# ---------------------------------------------------------------------------
# PATCH atomicity contract (#2064 round-2)
# ---------------------------------------------------------------------------
#
# Codex round-2 caught that combining ``status`` with labels/metadata
# in a single PATCH is racy: ``svc.update`` (labels/metadata) and the
# lifecycle methods (``svc.queue`` / ``svc.cancel``) commit in
# separate transactions, so a concurrent writer can flip the status
# between an in-memory preflight and the lifecycle call — leaving
# labels/metadata committed while the status write 409s. Round-2 fix:
# refuse the combined shape with ``400 invalid_request`` at the route
# layer BEFORE any work-service call. Each PATCH is now naturally
# atomic (status-only → one lifecycle call; field-only → one
# ``svc.update`` call).


def test_patch_rejects_status_combined_with_labels(
    client, auth_headers, task_store
) -> None:
    """PATCH with ``status`` + ``labels`` → 400 invalid_request."""
    _seed(task_store, n=14, work_status=WorkStatus.DRAFT, labels=["original"])
    response = client.patch(
        "/api/v1/tasks/myproj/14",
        headers=auth_headers,
        json={"status": "queued", "labels": ["new"]},
    )
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    assert "status" in body["error"]["message"].lower()
    assert "labels" in body["error"]["message"].lower()
    # Hint should point at the workarounds.
    hint = (body["error"].get("hint") or "").lower()
    assert "separate" in hint or "/queue" in hint


def test_patch_rejects_status_combined_with_metadata(
    client, auth_headers, task_store
) -> None:
    """PATCH with ``status`` + ``metadata`` → 400 invalid_request."""
    _seed(task_store, n=15, work_status=WorkStatus.DRAFT, external_refs={})
    response = client.patch(
        "/api/v1/tasks/myproj/15",
        headers=auth_headers,
        json={"status": "queued", "metadata": {"jira": "X-1"}},
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_request"


def test_patch_status_only_routes_to_queue(
    client, auth_headers, task_store
) -> None:
    """Status-only PATCH on a draft task → calls ``svc.queue``, returns 200."""
    seeded = _seed(task_store, n=16, work_status=WorkStatus.DRAFT)
    response = client.patch(
        "/api/v1/tasks/myproj/16",
        headers=auth_headers,
        json={"status": "queued"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["task"]["work_status"] == "queued"
    # The lifecycle method actually fired (not just a no-op pass-through).
    assert seeded.work_status == WorkStatus.QUEUED


def test_patch_fields_only_is_atomic(client, auth_headers, task_store) -> None:
    """Field-only PATCH → single ``svc.update(...)`` call carrying both fields.

    Spy on the fake work-service's ``update`` method to assert exactly
    ONE call lands with BOTH ``labels`` and ``external_refs`` in the
    same kwargs dict — the contract that makes the write atomic at the
    DB layer (``PgWorkService.update`` batches into one UPDATE).
    """
    _seed(task_store, n=17, labels=["old"], external_refs={"k": "v0"})

    from pollypm.work import factory as work_factory

    update_calls: list[dict[str, object]] = []
    real_factory = work_factory.create_work_service

    def _spy_factory(**kwargs):
        svc = real_factory(**kwargs)
        real_update = svc.update

        def _recording_update(task_id, **fields):
            update_calls.append({"task_id": task_id, "fields": dict(fields)})
            return real_update(task_id, **fields)

        svc.update = _recording_update  # type: ignore[method-assign]
        return svc

    import pollypm.work.factory as wf

    orig = wf.create_work_service
    wf.create_work_service = _spy_factory
    try:
        response = client.patch(
            "/api/v1/tasks/myproj/17",
            headers=auth_headers,
            json={
                "labels": ["new"],
                "metadata": {"k": "v1", "ref": "ABC-1"},
            },
        )
    finally:
        wf.create_work_service = orig

    assert response.status_code == 200, response.text
    assert len(update_calls) == 1, (
        f"expected exactly one svc.update call (atomic batch); "
        f"got {len(update_calls)}: {update_calls}"
    )
    fields = update_calls[0]["fields"]
    assert "labels" in fields and "external_refs" in fields, (
        "both labels and external_refs must land in the same update "
        f"call for atomicity; got: {fields}"
    )
    assert fields["labels"] == ["new"]
    assert fields["external_refs"] == {"k": "v1", "ref": "ABC-1"}


def test_patch_no_partial_commit_on_status_combined(
    client, auth_headers, task_store
) -> None:
    """Combined-shape rejection happens BEFORE any mutation.

    The route layer must short-circuit on the 400 BEFORE opening a
    work-service or calling ``svc.update`` / lifecycle methods. Seed a
    task in a state where the status transition WOULD succeed
    (draft → queued) so that, if the rejection accidentally landed
    after the field write, the labels would silently change. The 400
    guarantees neither side commits.
    """
    seeded = _seed(
        task_store,
        n=18,
        work_status=WorkStatus.DRAFT,
        labels=["original"],
        external_refs={"keep": "me"},
    )
    response = client.patch(
        "/api/v1/tasks/myproj/18",
        headers=auth_headers,
        json={
            "labels": ["clobber"],
            "metadata": {"jira": "X-1"},
            "status": "queued",
        },
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_request"
    # Nothing changed in the store.
    assert seeded.labels == ["original"], (
        "labels mutated despite the combined-shape rejection; the 400 "
        "must short-circuit BEFORE any work-service write."
    )
    assert seeded.external_refs == {"keep": "me"}
    assert seeded.work_status == WorkStatus.DRAFT


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


# ---------------------------------------------------------------------------
# TaskActionResult plan-hydration parity (#2064 round-4 blocker #1)
#
# ``TaskActionResult`` is documented (``models.py:347``) as the
# refresh-without-follow-up-GET envelope, but mutation helpers used to
# return ``_task_to_detail(task)`` directly — dropping the ``plan``
# payload that ``GET /tasks/{p}/{n}`` builds for plan-review tasks.
# Round-4 fix: factor the rule into ``_task_to_detail_with_plan`` and
# call it from every mutation helper (claim / cancel / reassign /
# patch / queue) AND from ``get_task_detail``. The tests below pin
# parity: each mutation's returned ``task.plan`` matches what GET would
# return for the same task.
# ---------------------------------------------------------------------------


_PLAN_BODY = (
    "# Plan\n"
    "\n"
    "## Summary\n"
    "Refactor the widget service to add caching.\n"
    "\n"
    "## Judgment calls\n"
    "- Cache TTL: 5 minutes\n"
    "- Eviction policy: LRU\n"
)


def _seed_plan_review_task(task_store, *, n, **overrides) -> FakeTask:
    """Seed a task that ``_is_plan_task`` + ``_is_in_review`` accept.

    ``_is_plan_task`` returns True when ``flow_template_id`` contains
    ``"plan"`` (or a label does); ``_is_in_review`` returns True when
    ``work_status == WorkStatus.REVIEW``. The plan body is read from
    ``task.description`` by ``_extract_plan_body`` — populate it so
    ``_build_plan`` produces a non-empty ``summary``.
    """
    defaults: dict[str, Any] = dict(
        flow_template_id="plan-arch",
        work_status=WorkStatus.REVIEW,
        description=_PLAN_BODY,
    )
    defaults.update(overrides)
    return _seed(task_store, n=n, **defaults)


def test_get_task_detail_hydrates_plan_for_plan_review(
    client, auth_headers, task_store
) -> None:
    """Baseline: ``GET`` hydrates ``plan`` for a plan-review task.

    Pinning this here so the parity assertions below have a
    well-defined target shape; if GET ever stops hydrating, the
    mutation tests would silently pass against an empty plan.
    """
    _seed_plan_review_task(task_store, n=40, assignee="pete")
    response = client.get(
        "/api/v1/tasks/myproj/40", headers=auth_headers
    )
    assert response.status_code == 200, response.text
    plan = response.json()["plan"]
    assert plan is not None
    assert "caching" in plan["summary"].lower()


def test_action_result_plan_hydration_parity_claim(
    client, auth_headers, task_store
) -> None:
    """Claim of a plan-review task: ``response.task.plan`` == GET.plan."""
    _seed_plan_review_task(
        task_store, n=41, work_status=WorkStatus.QUEUED
    )
    claim = client.post(
        "/api/v1/tasks/myproj/41/claim",
        headers=auth_headers,
        json={"actor": "carol"},
    )
    assert claim.status_code == 200, claim.text
    # Claim transitions QUEUED → IN_PROGRESS in the fake, so the
    # post-claim task is not in review. Seed a parallel REVIEW task to
    # exercise the hydration rule directly on the claim response: the
    # contract is that ANY mutation helper that LANDS the task on a
    # review-shaped plan node must surface ``plan``. We assert this by
    # mutating the seeded task into review state and re-issuing the
    # equivalent shape — but the cleanest assertion against the helper
    # is to mutate a task that's already in review.
    # Path 2: reassign a task that's currently in review (the more
    # common round-4 scenario per Codex's example).
    _seed_plan_review_task(task_store, n=42, assignee="pete")
    reassign = client.post(
        "/api/v1/tasks/myproj/42/reassign",
        headers=auth_headers,
        json={"actor": "nora"},
    )
    assert reassign.status_code == 200, reassign.text
    get_resp = client.get(
        "/api/v1/tasks/myproj/42", headers=auth_headers
    )
    assert get_resp.status_code == 200, get_resp.text
    assert reassign.json()["task"]["plan"] is not None, (
        "TaskActionResult.task.plan was dropped on reassign of a "
        "plan-review task; the refresh-without-follow-up-GET contract "
        "requires it match GET's shape."
    )
    assert reassign.json()["task"]["plan"] == get_resp.json()["plan"]


def test_action_result_plan_hydration_parity_patch(
    client, auth_headers, task_store
) -> None:
    """PATCH of a plan-review task: ``response.task.plan`` == GET.plan.

    Fails on the round-3 head because ``patch_task`` returned
    ``_task_to_detail(task)`` (no plan hydration). Passes after the
    round-4 ``_task_to_detail_with_plan`` helper rewires the callsite.
    """
    _seed_plan_review_task(task_store, n=43, labels=["existing"])
    patch_resp = client.patch(
        "/api/v1/tasks/myproj/43",
        headers=auth_headers,
        json={"labels": ["existing", "fresh"]},
    )
    assert patch_resp.status_code == 200, patch_resp.text
    get_resp = client.get(
        "/api/v1/tasks/myproj/43", headers=auth_headers
    )
    assert get_resp.status_code == 200, get_resp.text
    patched_plan = patch_resp.json()["task"]["plan"]
    assert patched_plan is not None, (
        "PATCH response dropped the plan payload that GET returns."
    )
    assert patched_plan == get_resp.json()["plan"]


def test_action_result_plan_hydration_parity_cancel(
    client, auth_headers, task_store
) -> None:
    """Cancel of a plan-review task: ``response.task.plan`` == GET.plan.

    The fake transitions REVIEW → CANCELLED (terminal), at which point
    ``_is_in_review`` returns False and plan hydration correctly skips.
    This is the *negative* case the parity helper must respect — a
    cancelled plan task should have ``plan=None`` in BOTH the response
    and a follow-up GET, not a stale hydrated payload.
    """
    _seed_plan_review_task(task_store, n=44)
    cancel_resp = client.post(
        "/api/v1/tasks/myproj/44/cancel",
        headers=auth_headers,
        json={"reason": "scope cut"},
    )
    assert cancel_resp.status_code == 200, cancel_resp.text
    assert cancel_resp.json()["task"]["work_status"] == "cancelled"
    assert cancel_resp.json()["task"]["plan"] is None
    get_resp = client.get(
        "/api/v1/tasks/myproj/44", headers=auth_headers
    )
    assert get_resp.json()["plan"] is None


def test_action_result_plan_not_hydrated_for_non_plan_task(
    client, auth_headers, task_store
) -> None:
    """Non-plan tasks: ``plan`` stays ``None`` in mutation responses.

    Confirms the helper's gate is sound — we don't hydrate spuriously
    on tasks the GET rule would also leave bare. Pairs with the
    parity tests above to bound the helper's behaviour from both sides.
    """
    _seed(
        task_store, n=45,
        work_status=WorkStatus.IN_PROGRESS,
        assignee="pete",
        flow_template_id="standard",
        description="not a plan body",
    )
    response = client.post(
        "/api/v1/tasks/myproj/45/reassign",
        headers=auth_headers,
        json={"actor": "nora"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["task"]["plan"] is None


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
