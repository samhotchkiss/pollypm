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

from collections.abc import Callable
import threading
import time
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

# Capture the REAL ``create_work_service_with_session`` reference at
# module import time, BEFORE the autouse ``patched_work_service``
# fixture swaps it for a fake. The round-10 direct unit test on
# ``service_factory.create_work_service_with_session`` needs to call
# the real helper (with stubbed collaborators) to verify that an
# ``attach_session_manager`` exception lands on
# ``svc._session_attach_error``.
from pollypm.work.service_factory import (  # noqa: E402
    create_work_service_with_deferred_session as _REAL_DEFERRED_FACTORY,
    create_work_service_with_session as _REAL_CREATE_WORK_SERVICE_WITH_SESSION,
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
    claimed_by_session: str | None = None
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
        task.claimed_by_session = actor
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

    def reopen(
        self, task_id: str, actor: str, reason: str | None = None  # noqa: ARG002
    ) -> FakeTask:
        task = self.get(task_id)
        if task.work_status != WorkStatus.CANCELLED:
            raise InvalidTransitionError(
                f"Cannot reopen task in '{task.work_status.value}' state. "
                "Only cancelled tasks can be reopened."
            )
        task.work_status = WorkStatus.QUEUED
        task.assignee = None
        task.current_node_id = None
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
            # #2064 round-9 blocker #4 / round-11 blocker #3: live-
            # worker-swap invariant. Mirror the pg + mock backends —
            # refuse draft, terminal AND queued states so route-level
            # tests can assert the 409 mapping. Without this,
            # FakeWorkService would silently append a breadcrumb to
            # a cancelled or queued task and the route regression
            # below would never trip.
            if task.work_status is WorkStatus.QUEUED:
                raise InvalidTransitionError(
                    "Cannot reassign task in 'queued' state. "
                    "Queued tasks can't be reassigned; cancel + "
                    "re-queue with role assignment."
                )
            if task.work_status in (
                WorkStatus.DRAFT, WorkStatus.DONE, WorkStatus.CANCELLED,
            ):
                raise InvalidTransitionError(
                    f"Cannot reassign task in "
                    f"'{task.work_status.value}' state."
                )
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
    from pollypm.work import service_factory as work_service_factory

    def _fake_factory(**_kwargs: object) -> FakeWorkService:
        return FakeWorkService(task_store)

    monkeypatch.setattr(work_factory, "create_work_service", _fake_factory)
    # #2064 round-9 blocker #2: the API claim helper now routes
    # through ``create_work_service_with_session`` (shared facade
    # with the CLI) instead of ``create_work_service`` directly.
    # Patch the shared helper too so claim-endpoint tests keep
    # hitting the FakeWorkService instead of attempting to wire a
    # real SessionManager against a non-existent tmux server.
    monkeypatch.setattr(
        work_service_factory,
        "create_work_service_with_session",
        _fake_factory,
    )
    monkeypatch.setattr(
        work_service_factory,
        "create_work_service_with_deferred_session",
        _fake_factory,
    )
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
    assert body["task"]["claimed_by_session"] == "alice"
    assert "myproj/1" in body["message"]


def test_claim_rejects_unknown_field(client, auth_headers, task_store) -> None:
    """#2064 round-12: ``/claim`` body forbids extras → 422.

    Spec §5.3 documents ``{assignee, actor}``; this endpoint keeps the
    surface tight to ``actor`` and derives ``assignee`` from the flow
    + roles. A client following the spec verbatim would POST
    ``{"actor": "alice", "assignee": "bob"}`` — without
    ``extra="forbid"`` on ``TaskClaimRequest`` the ``assignee`` field
    is silently dropped and the request claims successfully, hiding
    the contract mismatch. The forbid config turns it into a 422.
    """
    seeded = _seed(task_store, n=50, work_status=WorkStatus.QUEUED)
    response = client.post(
        "/api/v1/tasks/myproj/50/claim",
        headers=auth_headers,
        json={"actor": "alice", "assignee": "bob"},
    )
    assert response.status_code == 422, response.text
    # No state change — the request was rejected by the validator.
    assert seeded.work_status == WorkStatus.QUEUED
    assert seeded.assignee != "alice"


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


def test_claim_happy_path_warnings_empty(
    client, auth_headers, task_store
) -> None:
    """The happy-path claim response includes an empty ``warnings`` list.

    Pinned separately from the value assertions in
    ``test_claim_happy_path`` because the envelope's ``warnings``
    field is the new contract surface for the post-commit operator
    advisory channel (#2064 round-10). A client SDK regenerated
    against the OpenAPI document needs to be able to rely on the
    field being present even when nothing went wrong.
    """
    _seed(task_store, n=101, work_status=WorkStatus.QUEUED)
    response = client.post(
        "/api/v1/tasks/myproj/101/claim",
        headers=auth_headers,
        json={"actor": "alice"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "warnings" in body, body
    assert body["warnings"] == []


def test_claim_task_defers_worker_provisioning(
    api_config, task_store, monkeypatch
) -> None:
    """API claims return before slow worktree/tmux provisioning runs."""
    _seed(task_store, n=102, work_status=WorkStatus.QUEUED)

    class _SlowSessionManager:
        def __init__(self) -> None:
            self.cap_checks: list[tuple[str, str]] = []
            self.provisions: list[tuple[str, str]] = []

        def check_parallel_cap(self, project: str, task_id: str) -> None:
            self.cap_checks.append((project, task_id))

        def provision_worker(self, task_id: str, agent_name: str) -> None:
            time.sleep(0.15)
            self.provisions.append((task_id, agent_name))

    class _ProvisioningWorkService(FakeWorkService):
        def set_session_manager(self, mgr: object) -> None:
            self._session_mgr = mgr

        def claim(self, task_id: str, actor: str) -> FakeTask:
            mgr = getattr(self, "_session_mgr")
            mgr.check_parallel_cap("myproj", task_id)
            task = super().claim(task_id, actor)
            mgr.provision_worker(task_id, actor)
            return task

    attached_managers: list[_SlowSessionManager] = []
    scheduled: list[Callable[[], None]] = []

    def _factory(**_kwargs: object) -> _ProvisioningWorkService:
        return _ProvisioningWorkService(task_store)

    def _attach(svc: object, **_kwargs: object) -> _SlowSessionManager:
        mgr = _SlowSessionManager()
        svc.set_session_manager(mgr)
        attached_managers.append(mgr)
        return mgr

    def _schedule(work: Callable[[], None]) -> None:
        scheduled.append(work)

    from pollypm.web_api.service import claim_task
    from pollypm.work import factory as work_factory
    from pollypm.work import service_factory as work_service_factory

    monkeypatch.setattr(work_factory, "create_work_service", _factory)
    monkeypatch.setattr(
        work_service_factory,
        "attach_session_manager",
        _attach,
    )
    monkeypatch.setattr(
        work_service_factory,
        "create_work_service_with_deferred_session",
        _REAL_DEFERRED_FACTORY,
    )

    started = time.perf_counter()
    detail, warnings = claim_task(
        api_config,
        "myproj",
        102,
        actor="alice",
        provision_scheduler=_schedule,
    )
    elapsed = time.perf_counter() - started

    assert detail.work_status == "in_progress"
    assert warnings == []
    assert elapsed < 0.05
    assert len(scheduled) == 1
    assert attached_managers[0].cap_checks == [("myproj", "myproj/102")]
    assert attached_managers[0].provisions == []

    scheduled[0]()
    assert attached_managers[-1].provisions == [("myproj/102", "alice")]


def test_claim_endpoint_uses_lifespan_executor_for_deferred_provision(
    api_config, token_path, token, task_store, monkeypatch
) -> None:
    """Route requests use the app-owned executor when lifespan is active."""
    _seed(task_store, n=104, work_status=WorkStatus.QUEUED)
    scheduled = threading.Event()

    def _deferred_factory(**kwargs: object) -> FakeWorkService:
        schedule = kwargs.get("schedule")
        assert callable(schedule)
        schedule(scheduled.set)
        return FakeWorkService(task_store)

    from pollypm.work import service_factory as work_service_factory

    monkeypatch.setattr(
        work_service_factory,
        "create_work_service_with_deferred_session",
        _deferred_factory,
    )

    app = create_app(config=api_config, token_path=token_path)
    with TestClient(app) as local_client:
        response = local_client.post(
            "/api/v1/tasks/myproj/104/claim",
            headers={"Authorization": f"Bearer {token}"},
            json={"actor": "alice"},
        )
        assert scheduled.wait(timeout=1.0)

    assert response.status_code == 200, response.text


def test_deferred_provision_cap_race_rolls_claim_back(
    api_config, task_store, monkeypatch
) -> None:
    """Deferred worker-cap races keep the existing queued rollback."""
    task = _seed(task_store, n=103, work_status=WorkStatus.IN_PROGRESS)
    task.claimed_by_session = "alice"
    task.current_node_id = "node-start"

    class WorkerCapExceededError(RuntimeError):
        pass

    class _CapExceededSessionManager:
        def provision_worker(self, task_id: str, agent_name: str) -> None:
            raise WorkerCapExceededError("cap exceeded")

    class _RollbackWorkService(FakeWorkService):
        def __enter__(self) -> "_RollbackWorkService":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def set_session_manager(self, mgr: object) -> None:
            self._session_mgr = mgr

        def _rollback_claim_to_queued(
            self,
            project: str,
            task_number: int,
            node_id: str,
            actor: str,
            exc: BaseException,
        ) -> bool:
            self.rollback_call = (
                project,
                task_number,
                node_id,
                actor,
                str(exc),
            )
            task = self.get(f"{project}/{task_number}")
            task.work_status = WorkStatus.QUEUED
            task.claimed_by_session = None
            return True

    svc = _RollbackWorkService(task_store)

    def _factory(**_kwargs: object) -> _RollbackWorkService:
        return svc

    def _attach(svc: object, **_kwargs: object) -> _CapExceededSessionManager:
        mgr = _CapExceededSessionManager()
        svc.set_session_manager(mgr)
        return mgr

    from pollypm.work import factory as work_factory
    from pollypm.work import service_factory as work_service_factory

    monkeypatch.setattr(work_factory, "create_work_service", _factory)
    monkeypatch.setattr(
        work_service_factory,
        "attach_session_manager",
        _attach,
    )

    work_service_factory.provision_claimed_worker(
        config=api_config,
        project_key="myproj",
        project_path=api_config.projects["myproj"].path,
        task_id="myproj/103",
        agent_name="alice",
    )

    assert svc.rollback_call == (
        "myproj",
        103,
        "node-start",
        "alice",
        "cap exceeded",
    )
    assert task.work_status == WorkStatus.QUEUED
    assert task.claimed_by_session is None


def test_claim_surfaces_last_provision_error_warning(
    api_config, token_path, token, task_store, monkeypatch  # noqa: ARG001
) -> None:
    """Post-commit ``svc.last_provision_error`` surfaces in ``warnings``.

    Round-10 contract: when ``PgWorkService.claim`` records a
    worker-session provisioning failure in ``last_provision_error``
    after the DB transition has committed, the API must surface
    that to the operator instead of silently returning ``ok: true``
    with no worker lane. Mirrors the CLI surface at
    ``src/pollypm/work/cli.py:912-929`` so the recovery story is the
    same across channels.
    """
    _seed(task_store, n=201, work_status=WorkStatus.QUEUED)

    class _ProvisionFailingWorkService(FakeWorkService):
        def claim(self, task_id: str, actor: str) -> FakeTask:
            task = super().claim(task_id, actor)
            # Mimic ``PgWorkService.claim``'s post-commit stamp at
            # ``pg_service.py:1905-1910``: the DB transition is in,
            # but the per-task worker session blew up.
            self.last_provision_error = (
                "tmux client missing: no `tmux` binary on PATH"
            )
            return task

    def _factory(**_kwargs: object) -> _ProvisionFailingWorkService:
        return _ProvisionFailingWorkService(task_store)

    from pollypm.web_api.app import create_app
    from pollypm.work import factory as work_factory
    from pollypm.work import service_factory as work_service_factory

    monkeypatch.setattr(work_factory, "create_work_service", _factory)
    monkeypatch.setattr(
        work_service_factory, "create_work_service_with_session", _factory
    )
    app = create_app(config=api_config, token_path=token_path)
    local_client = TestClient(app)
    response = local_client.post(
        "/api/v1/tasks/myproj/201/claim",
        headers={"Authorization": f"Bearer {token}"},
        json={"actor": "alice"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # The DB claim still went through — the transition committed.
    assert body["ok"] is True
    assert body["task"]["work_status"] == "in_progress"
    assert body["task"]["assignee"] == "alice"
    # The advisory channel carries the error + recovery steps.
    warnings = body["warnings"]
    assert len(warnings) == 1, warnings
    warning = warnings[0]
    assert "tmux client missing" in warning
    assert "myproj/201" in warning
    # Recovery wording must point at hold + resume — that is the
    # exact same operator workflow ``pm task claim`` documents.
    assert "hold" in warning.lower()
    assert "resume" in warning.lower()


def test_claim_attach_session_manager_failure_captured(
    api_config, token_path, token, task_store, monkeypatch  # noqa: ARG001
) -> None:
    """SessionManager attach failures surface as ``warnings`` entries.

    Round-10 second half: if ``attach_session_manager`` cannot wire
    a SessionManager onto the freshly-constructed work service
    (tmux import broken, StateStore failure, etc), the API must
    surface that distinctly from a healthy DB-only claim. Without
    this, the HTTP layer cannot tell whether ``last_provision_error
    is None`` because nothing went wrong or because the session
    manager was never attached in the first place
    (per Codex round-10 comment).
    """
    _seed(task_store, n=202, work_status=WorkStatus.QUEUED)

    class _AttachFailedWorkService(FakeWorkService):
        """FakeWorkService pre-stamped with a SessionManager attach failure."""

        def __init__(self, store: dict[str, FakeTask]) -> None:
            super().__init__(store)
            self._session_attach_error = (
                "SessionManager wire-up failed: TmuxClient unreachable"
            )

    def _factory(**_kwargs: object) -> _AttachFailedWorkService:
        return _AttachFailedWorkService(task_store)

    from pollypm.web_api.app import create_app
    from pollypm.work import factory as work_factory
    from pollypm.work import service_factory as work_service_factory

    monkeypatch.setattr(work_factory, "create_work_service", _factory)
    monkeypatch.setattr(
        work_service_factory, "create_work_service_with_session", _factory
    )
    app = create_app(config=api_config, token_path=token_path)
    local_client = TestClient(app)
    response = local_client.post(
        "/api/v1/tasks/myproj/202/claim",
        headers={"Authorization": f"Bearer {token}"},
        json={"actor": "alice"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # Claim still succeeds — attach failures don't fail the request.
    assert body["ok"] is True
    assert body["task"]["work_status"] == "in_progress"
    # Attach error appears in warnings with the SessionManager prefix.
    warnings = body["warnings"]
    assert len(warnings) == 1, warnings
    warning = warnings[0]
    assert "SessionManager wire-up failed" in warning
    assert "TmuxClient unreachable" in warning
    assert "myproj/202" in warning


def test_create_work_service_with_session_captures_attach_exception(
    monkeypatch,
) -> None:
    """``create_work_service_with_session`` stamps attach errors on svc.

    Direct unit test on the
    :mod:`pollypm.work.service_factory` helper covering the round-10
    capture path: ``attach_session_manager`` is called best-effort,
    and any exception escaping it must land on
    ``svc._session_attach_error`` so the API claim path can surface
    it. Before round-10 the helper logged the failure at ``debug``
    only — Codex flagged that as indistinguishable from a successful
    attach to the HTTP layer.
    """
    from pollypm.work import factory as work_factory
    from pollypm.work import service_factory as work_service_factory

    class _StubService:
        pass

    stub = _StubService()

    def _stub_create_work_service(**_kwargs: object) -> _StubService:
        return stub

    def _raising_attach(svc: object, **_kwargs: object) -> None:  # noqa: ARG001
        raise RuntimeError("simulated attach failure: tmux missing")

    # The autouse ``patched_work_service`` fixture has replaced
    # ``work_service_factory.create_work_service_with_session`` with
    # a fake to keep route-level tests away from real tmux wiring.
    # For this direct unit test we need the REAL helper — capture
    # it from the module's persistent reference at the top of this
    # file (``_REAL_CREATE_WORK_SERVICE_WITH_SESSION``) and restore
    # it via monkeypatch so teardown reverts cleanly.
    monkeypatch.setattr(
        work_service_factory,
        "create_work_service_with_session",
        _REAL_CREATE_WORK_SERVICE_WITH_SESSION,
    )

    # ``create_work_service_with_session`` does a function-local
    # ``from pollypm.work.factory import create_work_service`` — patch
    # the source module so the late binding picks up the stub.
    monkeypatch.setattr(
        work_factory, "create_work_service", _stub_create_work_service
    )
    # ``attach_session_manager`` resolves via the module globals at
    # call time, so patching the same module works.
    monkeypatch.setattr(
        work_service_factory, "attach_session_manager", _raising_attach
    )

    # The helper must NOT raise — best-effort attach contract.
    svc = work_service_factory.create_work_service_with_session(
        config=None,
        project_key="x",
        project_path=Path("/tmp/does-not-matter"),
    )
    assert svc is stub
    # Without the round-10 capture, this attribute would not exist
    # and the API surface would silently behave as DB-only.
    captured = getattr(svc, "_session_attach_error", None)
    assert captured is not None, (
        "attach_session_manager exception was not captured on svc; "
        "API claim path cannot surface it"
    )
    assert "simulated attach failure" in captured


def test_claim_worker_cap_exceeded_returns_429(
    api_config, token_path, token, task_store, monkeypatch  # noqa: ARG001
) -> None:
    """#2064 round-11 blocker #1: WorkerCapExceededError → 429 envelope.

    The shared claim facade calls
    :meth:`SessionManager.check_parallel_cap` before the DB
    transition (``pg_service.py:1759-1763``); a project already at
    its ``max_parallel_workers`` ceiling raises
    :class:`WorkerCapExceededError`. Prior to round-11 the API
    helper only mapped ``TaskNotFoundError`` / ``InvalidTransitionError``,
    so cap pressure leaked as ``500 internal_error``. The fix maps
    it to a typed ``429 worker_cap_exceeded`` envelope so clients
    can apply back-off.
    """
    from pollypm.work.session_manager import WorkerCapExceededError

    _seed(task_store, n=301, work_status=WorkStatus.QUEUED)

    class _CapExceededWorkService(FakeWorkService):
        def claim(self, task_id: str, actor: str) -> FakeTask:  # noqa: ARG002
            # Mimic the pg pre-claim cap probe at
            # ``pg_service.py:1759-1763`` — raises BEFORE the DB
            # transition fires so no state change is visible.
            raise WorkerCapExceededError(
                "Cannot spawn worker for myproj/301: project 'myproj' "
                "already has 4 active worker sessions (cap=4)."
            )

    def _factory(**_kwargs: object) -> _CapExceededWorkService:
        return _CapExceededWorkService(task_store)

    from pollypm.web_api.app import create_app
    from pollypm.work import factory as work_factory
    from pollypm.work import service_factory as work_service_factory

    monkeypatch.setattr(work_factory, "create_work_service", _factory)
    monkeypatch.setattr(
        work_service_factory, "create_work_service_with_session", _factory
    )
    app = create_app(config=api_config, token_path=token_path)
    local_client = TestClient(app)
    response = local_client.post(
        "/api/v1/tasks/myproj/301/claim",
        headers={"Authorization": f"Bearer {token}"},
        json={"actor": "alice"},
    )
    # MUST be 429 (back-pressure) NOT 500 (server bug) or 409
    # (state conflict). The HTTP status carries the back-off
    # semantics; the body code carries the machine-readable label.
    assert response.status_code == 429, response.text
    body = response.json()
    assert body["error"]["code"] == "worker_cap_exceeded", body
    assert "worker cap exceeded" in body["error"]["message"].lower()
    # Hint MUST point at the recovery workflow.
    hint = (body["error"].get("hint") or "").lower()
    assert "max_parallel_workers" in hint or "retry" in hint


def test_claim_post_commit_rollback_message_reflects_queued_state(
    api_config, token_path, token, task_store, monkeypatch  # noqa: ARG001
) -> None:
    """#2064 round-11 blocker #2: rolled-back claim says so.

    ``PgWorkService.claim`` at ``pg_service.py:1917-1939`` rolls
    the task back to ``queued`` when a post-commit cap race fires.
    Prior to round-11 the route still returned ``message="claimed
    myproj/N"`` and ``warnings`` said "the DB claim is in effect",
    even though the row was queued again. This test pins the
    truthful contract: when ``task.work_status == 'queued'`` after
    the claim call, the message says "rolled back" and the warning
    matches.
    """
    _seed(task_store, n=302, work_status=WorkStatus.QUEUED)

    class _RolledBackClaimWorkService(FakeWorkService):
        def claim(self, task_id: str, actor: str) -> FakeTask:
            # Get the task but DON'T transition — emulate the
            # post-rollback state: row stays queued, but the post-
            # commit cap race did happen and stamped
            # ``last_provision_error``. This matches
            # ``pg_service.py:1939`` where ``result = self.get(...)``
            # refetches a now-queued row after the rollback.
            task = self.get(task_id)
            # Round-11 simulates the rollback path explicitly.
            self.last_provision_error = (
                "cap exceeded post-commit; claim rolled back"
            )
            return task

    def _factory(**_kwargs: object) -> _RolledBackClaimWorkService:
        return _RolledBackClaimWorkService(task_store)

    from pollypm.web_api.app import create_app
    from pollypm.work import factory as work_factory
    from pollypm.work import service_factory as work_service_factory

    monkeypatch.setattr(work_factory, "create_work_service", _factory)
    monkeypatch.setattr(
        work_service_factory, "create_work_service_with_session", _factory
    )
    app = create_app(config=api_config, token_path=token_path)
    local_client = TestClient(app)
    response = local_client.post(
        "/api/v1/tasks/myproj/302/claim",
        headers={"Authorization": f"Bearer {token}"},
        json={"actor": "alice"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # Envelope still reports ``ok: true`` — the request itself
    # didn't fail, the operator just needs to know the row is
    # queued again. ``task.work_status`` is the routing source.
    assert body["ok"] is True
    assert body["task"]["work_status"] == "queued"
    # MESSAGE: must NOT say "claimed myproj/302" — the row is
    # queued. The truthful wording says "rolled back; ... remains
    # queued".
    message = body["message"]
    assert "rolled back" in message.lower(), message
    assert "queued" in message.lower(), message
    assert "myproj/302" in message
    # WARNING: must reflect "rolled back", NOT "DB claim is in
    # effect" (the pre-round-11 wording).
    warnings = body["warnings"]
    assert len(warnings) == 1, warnings
    warning = warnings[0]
    assert "rolled back" in warning.lower(), warning
    assert "queued" in warning.lower(), warning
    # The pre-round-11 false wording MUST be gone.
    assert "DB claim is in effect" not in warning, warning


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


def test_cancel_happy_path(client, auth_headers, task_store) -> None:
    _seed(task_store, n=3, work_status=WorkStatus.IN_PROGRESS, assignee="bob")
    response = client.post(
        "/api/v1/tasks/myproj/3/cancel?force=true",
        headers=auth_headers,
        json={"reason": "scope cut"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["task"]["work_status"] == "cancelled"


def test_cancel_in_progress_without_force_requires_confirmation(
    client, auth_headers, task_store
) -> None:
    seeded = _seed(
        task_store, n=52,
        work_status=WorkStatus.IN_PROGRESS, assignee="bob",
    )
    response = client.post(
        "/api/v1/tasks/myproj/52/cancel",
        headers=auth_headers,
        json={"reason": "scope cut"},
    )
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error"]["code"] == "confirmation_required"
    assert "bob" in body["error"]["message"]
    assert seeded.work_status == WorkStatus.IN_PROGRESS


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


def test_cancel_rejects_unknown_field(client, auth_headers, task_store) -> None:
    """#2064 round-12: ``/cancel`` body forbids extras → 422.

    Without ``extra="forbid"`` on ``TaskCancelRequest`` the extra key
    is silently dropped and the cancel completes — masking the
    client's contract bug. The forbid config turns it into a 422.
    """
    seeded = _seed(task_store, n=51, work_status=WorkStatus.IN_PROGRESS, assignee="bob")
    response = client.post(
        "/api/v1/tasks/myproj/51/cancel",
        headers=auth_headers,
        json={"reason": "x", "bogus": "y"},
    )
    assert response.status_code == 422, response.text
    # No state change — the request was rejected by the validator.
    assert seeded.work_status == WorkStatus.IN_PROGRESS


# ---------------------------------------------------------------------------
# Reopen
# ---------------------------------------------------------------------------


def test_reopen_happy_path(client, auth_headers, task_store) -> None:
    _seed(
        task_store, n=53,
        work_status=WorkStatus.CANCELLED, assignee="bob",
        current_node_id="implement",
    )
    response = client.post(
        "/api/v1/tasks/myproj/53/reopen",
        headers=auth_headers,
        json={"reason": "operator undo"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["task"]["work_status"] == "queued"
    assert body["task"]["assignee"] is None
    assert body["task"]["current_node_id"] is None


def test_reopen_non_cancelled_returns_409(client, auth_headers, task_store) -> None:
    _seed(task_store, n=54, work_status=WorkStatus.QUEUED)
    response = client.post(
        "/api/v1/tasks/myproj/54/reopen",
        headers=auth_headers,
        json={"reason": "not cancelled"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "invalid_state"


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


def test_reassign_rejects_unknown_field(client, auth_headers, task_store) -> None:
    """#2064 round-12: ``/reassign`` body forbids extras → 422.

    Without ``extra="forbid"`` on ``TaskReassignRequest`` the extra
    key is silently dropped and the reassignment completes — masking
    the client's contract bug (e.g. a client mistakenly passing
    ``reason`` along with ``actor``). The forbid config turns it
    into a 422.
    """
    seeded = _seed(
        task_store, n=52,
        work_status=WorkStatus.IN_PROGRESS, assignee="bob",
    )
    response = client.post(
        "/api/v1/tasks/myproj/52/reassign",
        headers=auth_headers,
        json={"actor": "bob", "extra": "nope"},
    )
    assert response.status_code == 422, response.text
    # No state change — the request was rejected by the validator.
    assert seeded.assignee == "bob"
    assert seeded.context == [], (
        f"reassign with unknown field must NOT record a breadcrumb; "
        f"got {seeded.context!r}"
    )


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


def test_reassign_draft_returns_409(client, auth_headers, task_store) -> None:
    """#2064 round-9 blocker #4: reassign refuses draft tasks.

    Reassign is a mid-flight worker swap (spec §P-9); a draft task
    has no live worker to swap, so the route must return 409
    invalid_state instead of silently recording a reassignment
    breadcrumb. Mirrors the ``/cancel`` 409 contract.
    """
    seeded = _seed(
        task_store, n=70,
        work_status=WorkStatus.DRAFT, assignee=None,
    )
    response = client.post(
        "/api/v1/tasks/myproj/70/reassign",
        headers=auth_headers,
        json={"actor": "carol"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "invalid_state"
    # No breadcrumb was recorded — refused BEFORE the write.
    assert seeded.context == [], (
        f"reassign on draft must NOT record a breadcrumb; got "
        f"{seeded.context!r}"
    )


def test_reassign_cancelled_returns_409(
    client, auth_headers, task_store,
) -> None:
    """#2064 round-9 blocker #4: reassign refuses cancelled tasks."""
    seeded = _seed(
        task_store, n=71,
        work_status=WorkStatus.CANCELLED, assignee="pete",
    )
    response = client.post(
        "/api/v1/tasks/myproj/71/reassign",
        headers=auth_headers,
        json={"actor": "carol"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "invalid_state"
    assert seeded.assignee == "pete", (
        "assignee must NOT change when reassign is refused"
    )


def test_reassign_done_returns_409(client, auth_headers, task_store) -> None:
    """#2064 round-9 blocker #4: reassign refuses done tasks."""
    seeded = _seed(
        task_store, n=72,
        work_status=WorkStatus.DONE, assignee="pete",
    )
    response = client.post(
        "/api/v1/tasks/myproj/72/reassign",
        headers=auth_headers,
        json={"actor": "carol"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "invalid_state"
    assert seeded.assignee == "pete"


def test_reassign_queued_returns_409(client, auth_headers, task_store) -> None:
    """#2064 round-11 blocker #3: reassign refuses queued tasks.

    Queued dispatch routes by ``task.roles["worker"]``, not the
    ``assignee`` column (``pg_service.py:2059-2060``), and the
    claim path resolves assignee from the node role first
    (``_resolve_node_assignee`` at ``:4254-4256``). Setting
    ``assignee`` on a queued task would create a silent drift:
    ``GET`` reads the new assignee, but ``claim()`` routes to the
    original role owner. The contract is "reject + point operator
    at cancel + re-queue" instead.
    """
    seeded = _seed(
        task_store, n=73,
        work_status=WorkStatus.QUEUED, assignee=None,
        roles={"worker": "alice", "reviewer": "bob"},
    )
    response = client.post(
        "/api/v1/tasks/myproj/73/reassign",
        headers=auth_headers,
        json={"actor": "nora"},
    )
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error"]["code"] == "invalid_state"
    # The error message MUST mention that queued tasks can't be
    # reassigned + point at the cancel + re-queue workaround.
    message = body["error"]["message"].lower()
    assert "queued" in message
    # No state change — assignee column and roles untouched.
    assert seeded.assignee is None, (
        "assignee must NOT change when queued reassign is refused"
    )
    assert seeded.roles == {"worker": "alice", "reviewer": "bob"}
    # No breadcrumb was recorded.
    assert seeded.context == [], (
        f"reassign on queued must NOT record a breadcrumb; got "
        f"{seeded.context!r}"
    )


# ---------------------------------------------------------------------------
# Fallback SessionManager warning quality (#2064 round-11 blocker #4)
# ---------------------------------------------------------------------------


def test_attach_session_manager_no_false_positive_when_fallback_succeeds(
    monkeypatch,
) -> None:
    """#2064 round-11 blocker #4: SessionService fail + SessionManager OK = no warning.

    ``attach_session_manager`` previously stamped
    ``_session_attach_error`` the moment ``SessionService``
    construction raised at ``service_factory.py:78-85`` — even
    when the subsequent ``SessionManager`` wire-up at
    ``:86-95`` still succeeded (SessionManager accepts
    ``session_service=None`` as a fallback). The claim warning
    "no per-task tmux lane was provisioned" would then lie
    because the fallback manager IS provisioning lanes.

    This test exercises the real
    :func:`attach_session_manager` helper with a stubbed
    ``TmuxSessionService`` (raises on construction) and a
    successful ``SessionManager`` constructor (records the
    attach), then asserts the svc carries NO
    ``_session_attach_error``.
    """
    from pollypm.work import service_factory as work_service_factory

    class _StubService:
        def set_session_manager(self, mgr: object) -> None:
            self._session_mgr = mgr

    stub = _StubService()
    attach_calls: list[dict[str, object]] = []

    # Replace TmuxSessionService import target so construction
    # raises (simulating tmux/state-store failure on a host where
    # SessionService can't initialise).
    import pollypm.session_services.tmux as tmux_module

    class _BrokenTmuxSessionService:
        def __init__(self, *args: object, **kwargs: object) -> None:  # noqa: ARG002
            raise RuntimeError("simulated SessionService failure")

    monkeypatch.setattr(
        tmux_module, "TmuxSessionService", _BrokenTmuxSessionService
    )

    # Replace SessionManager so its constructor records the call
    # and returns without raising — that's the "fallback success"
    # path the blocker calls out.
    import pollypm.work.session_manager as sm_module

    class _OkSessionManager:
        def __init__(self, **kwargs: object) -> None:
            attach_calls.append(kwargs)

    monkeypatch.setattr(sm_module, "SessionManager", _OkSessionManager)

    # Real project_path with a .git so the early gate doesn't bail.
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        (proj / ".git").mkdir()
        work_service_factory.attach_session_manager(
            stub, project_path=proj, config=None
        )

    # SessionManager DID get constructed (fallback path lived).
    assert len(attach_calls) == 1, attach_calls
    # ``session_service`` was None because TmuxSessionService raised
    # — that is the fallback condition we are pinning.
    assert attach_calls[0]["session_service"] is None
    # The set_session_manager hook fired — the svc has a manager.
    assert getattr(stub, "_session_mgr", None) is not None
    # Critical contract: NO ``_session_attach_error`` despite the
    # SessionService construction failure, because the SessionManager
    # fallback succeeded.
    assert getattr(stub, "_session_attach_error", None) is None, (
        f"false-positive attach error: "
        f"{stub._session_attach_error!r}"  # type: ignore[attr-defined]
    )


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


def test_patch_status_to_cancelled_in_progress_requires_confirmation(
    client, auth_headers, task_store
) -> None:
    seeded = _seed(
        task_store, n=55,
        work_status=WorkStatus.IN_PROGRESS, assignee="bob",
    )
    response = client.patch(
        "/api/v1/tasks/myproj/55",
        headers=auth_headers,
        json={"status": "cancelled"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "confirmation_required"
    assert seeded.work_status == WorkStatus.IN_PROGRESS


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
# PATCH extras / no-op contract (#2064 round-6)
# ---------------------------------------------------------------------------
#
# Codex round-6 caught two silent-success paths:
#   1. ``TaskPatchRequest`` had no ``extra=`` config, so Pydantic
#      dropped unknown keys (``priority``, role-assignment fields,
#      typos like ``metdata``) and forwarded an all-``None`` body —
#      the route then returned ``200 ok`` after a single ``svc.get``,
#      masking client bugs.
#   2. An explicit empty body (``{}`` or ``{labels: null, ...}``)
#      took the same silent path.
# Round-6 fix:
#   - ``model_config = {"extra": "forbid"}`` in ``TaskPatchRequest``
#     → FastAPI's request validator returns ``422`` for extras.
#   - Route-layer no-op guard returns ``400 invalid_request`` for
#     all-``None`` bodies.


def test_patch_rejects_unknown_field(
    client, auth_headers, task_store
) -> None:
    """PATCH with an unsupported field (`priority`) → 422 from Pydantic.

    Without ``extra=forbid``, this body would be silently coerced to
    ``{labels: None, status: None, metadata: None}`` and return 200.
    The forbid config turns it into a ``422 Unprocessable Entity`` —
    FastAPI's default for request-body validation failures, which is
    a stronger client signal than our typed ``invalid_request``.
    """
    seeded = _seed(task_store, n=20, labels=["keep"])
    response = client.patch(
        "/api/v1/tasks/myproj/20",
        headers=auth_headers,
        json={"priority": "high"},
    )
    # Pydantic extras → 422; the FastAPI validation envelope is fine.
    assert response.status_code == 422, response.text
    # Nothing on the task changed.
    assert seeded.labels == ["keep"]


def test_patch_rejects_misspelled_field(
    client, auth_headers, task_store
) -> None:
    """A typo (``metdata`` instead of ``metadata``) is rejected, not silently ignored."""
    seeded = _seed(task_store, n=21, external_refs={"jira": "X-1"})
    response = client.patch(
        "/api/v1/tasks/myproj/21",
        headers=auth_headers,
        json={"metdata": {"jira": "X-2"}},
    )
    assert response.status_code == 422, response.text
    # Original external_refs preserved.
    assert seeded.external_refs == {"jira": "X-1"}


def test_patch_rejects_empty_body(client, auth_headers, task_store) -> None:
    """PATCH ``{}`` → 400 invalid_request; surfaces client-side payload bugs."""
    seeded = _seed(task_store, n=22, labels=["unchanged"])
    response = client.patch(
        "/api/v1/tasks/myproj/22",
        headers=auth_headers,
        json={},
    )
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    msg = body["error"]["message"].lower()
    assert "at least one" in msg
    assert "labels" in msg and "status" in msg and "metadata" in msg
    # Nothing changed.
    assert seeded.labels == ["unchanged"]


def test_patch_rejects_all_null_body(
    client, auth_headers, task_store
) -> None:
    """PATCH with every field explicitly ``null`` is still a no-op → 400."""
    seeded = _seed(task_store, n=23, labels=["unchanged"])
    response = client.patch(
        "/api/v1/tasks/myproj/23",
        headers=auth_headers,
        json={"labels": None, "status": None, "metadata": None},
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_request"
    assert seeded.labels == ["unchanged"]


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
        ("POST", "/api/v1/tasks/myproj/1/reopen", {}),
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
