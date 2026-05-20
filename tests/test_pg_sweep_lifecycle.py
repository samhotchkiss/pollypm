"""Pg re-coverage for sweep / lifecycle regressions (#1788).

Restores meaningful assertions from five sqlite-only test modules that
were deleted in Slice K-tests part 5 (#1737, commit 04285152):

* ``test_kickoff_sweep_force_push.py`` (#922 / #923 lineage) — partial
  port. The force-push branch is guarded by
  ``PgWorkService.kickoff_sent_at``, which is **not yet implemented on
  the pg backend** (see ``src/pollypm/work/pg_service.py`` — the
  ``kickoff bookkeeping`` line in the Slice A docstring still tracks
  this gap). Without the method ``_kickoff_pending`` always returns
  False and the sweep takes the standard idle-gated path, so #922's
  observable behaviour can't be re-asserted end-to-end here. We pin
  the fallback contract instead: a worker task whose work-service does
  not expose ``kickoff_sent_at`` still gets a kickoff via the standard
  path, never the forced one. When the pg port lands, port the full
  scenario here.
* ``test_work_progress_sweep.py`` (#249) — full port. The 5-min stuck
  in_progress task sweep runs against the long-lived
  :class:`PgWorkService` exposed by ``pg_sweep_harness``, ticked
  multiple times to assert dedupe / busy-skip / no-session-skip.
* ``test_state_drift_reconciliation.py`` (#296) — pure-function
  coverage. The ``reconcile_expected_advance`` decision table doesn't
  touch the work-service; we keep that surface backend-neutral.
* ``test_worker_turn_end_reprompt.py`` (#302) — pure-heuristic coverage
  (``determine_worker_response``, ``is_worker_session_name``,
  ``send_standard_reprompt``) plus the inbox-item create path against
  the pg work service.
* ``test_work_session_integration_regressions.py`` — the approve /
  session-teardown contract against the pg work service.

Backend
-------

The harness builds one :class:`PgWorkService` per test (via
``pg_sweep_harness`` in ``conftest_pg.py``). Tests drive multiple sweep
ticks against the same long-lived service — the sqlite idiom of
closing+reopening to simulate a restart is a no-op on pg and was the
primary reason these files were deleted rather than ported in place.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from pollypm.plugins_builtin.core_recurring.sweeps import (
    work_progress_sweep_handler,
)
from pollypm.plugins_builtin.task_assignment_notify.handlers.sweep import (
    task_assignment_sweep_handler,
)
from pollypm.recovery.state_reconciliation import (
    MIN_PLAN_SIZE_BYTES,
    reconcile_expected_advance,
)
from pollypm.recovery.worker_turn_end import (
    WORKER_REPROMPT_TEXT,
    create_blocking_question_inbox_item,
    determine_worker_response,
    is_worker_session_name,
    send_standard_reprompt,
)
from pollypm.runtime_services import _RuntimeServices
from pollypm.store.backends.pg_store import PgStore
from pollypm.work import task_assignment as bus
from pollypm.work.models import (
    Artifact,
    ArtifactKind,
    OutputType,
    WorkOutput,
    WorkStatus,
)


# ---------------------------------------------------------------------------
# Test doubles — mirror the deleted suites' fakes so the assertions
# remain comparable.
# ---------------------------------------------------------------------------


@dataclass
class FakeHandle:
    name: str


@dataclass
class FakeSessionService:
    """Minimal session service: ``list`` / ``send`` / ``is_turn_active``.

    Mirrors the FakeSessionService used by the deleted sqlite tests so
    assertion shapes carry over unchanged.
    """

    handles: list[FakeHandle]
    sent: list[tuple[str, str]] = field(default_factory=list)
    busy: set[str] = field(default_factory=set)
    capture_text: str = ""
    tmux: object | None = None

    def list(self) -> list[FakeHandle]:
        return list(self.handles)

    def send(self, name: str, text: str, *, press_enter: bool = True) -> None:
        self.sent.append((name, text))

    def is_turn_active(self, name: str) -> bool:
        return name in self.busy

    def capture(self, name: str, lines: int = 200) -> str:
        return self.capture_text

    def storage_closet_session_name(self) -> str:
        return "pollypm-storage-closet"


@dataclass
class FakeProject:
    """Project shape consumed by ``handle_worker_turn_end``."""

    path: Path
    persona_name: str | None = None


@dataclass
class FakeConfig:
    projects: dict[str, FakeProject] = field(default_factory=dict)


@dataclass
class FakeTask:
    """Task shape used by the pure-function reconcile / heuristic tests."""

    project: str
    task_number: int
    flow_template_id: str = "standard"
    current_node_id: str = "do_work"
    title: str = "Do the work"

    @property
    def task_id(self) -> str:
        return f"{self.project}/{self.task_number}"


# ---------------------------------------------------------------------------
# Helpers — common task seeding against PgWorkService.
# ---------------------------------------------------------------------------


def _claim_worker_task(work, *, project: str, title: str):
    task = work.create(
        title=title,
        description="Implement the thing",
        type="task",
        project=project,
        flow_template="standard",
        roles={"worker": "worker", "reviewer": "reviewer"},
        priority="normal",
    )
    work.queue(task.task_id, "pm")
    work.claim(task.task_id, "worker")
    return work.get(task.task_id)


def _install_sweep_loader(monkeypatch, services: _RuntimeServices) -> None:
    """Wire both task-assignment + work-progress sweep loaders.

    Both handlers reach for ``load_runtime_services`` at the top of
    their bodies; patch both call sites so the test's harness drives
    every sweep tick.
    """
    monkeypatch.setattr(
        "pollypm.plugins_builtin.task_assignment_notify.handlers.sweep.load_runtime_services",
        lambda *, config_path=None: services,
    )
    monkeypatch.setattr(
        "pollypm.task_assignment_notify.load_runtime_services",
        lambda *, config_path=None: services,
    )


# ---------------------------------------------------------------------------
# (1) work_progress_sweep — stuck in_progress task gets one resume ping
#     within the 30-min dedupe window. Ported from
#     ``test_work_progress_sweep.py::TestWorkProgressSweep``.
# ---------------------------------------------------------------------------


class TestWorkProgressSweepAgainstPg:
    """The 5-min sweeper finds stuck in_progress tasks and pings their
    claimant session via the task_assignment_notify path, respecting
    the existing 30-min dedupe table.

    The deleted suite re-opened a fresh ``SQLiteWorkService`` per tick
    so it could simulate the production loader's per-tick service. Pg
    has one schema + one pool — the same long-lived service drives every
    tick.
    """

    def test_stuck_task_gets_resume_ping_then_dedupes(
        self, pg_sweep_harness, monkeypatch, tmp_path,
    ):
        work, store, _advance = pg_sweep_harness
        bus.clear_listeners()
        _claim_worker_task(work, project="demo", title="Implement")

        svc = FakeSessionService(handles=[FakeHandle("worker-demo")])
        services = _RuntimeServices(
            session_service=svc,
            state_store=store,
            work_service=work,
            project_root=tmp_path,
            msg_store=store,
        )
        _install_sweep_loader(monkeypatch, services)

        # Tick 1 → resume ping fires.
        result1 = work_progress_sweep_handler({})
        assert result1["outcome"] == "swept"
        assert result1["pinged"] == 1, f"expected 1 ping, got {result1!r}"
        assert len(svc.sent) == 1
        name, text = svc.sent[0]
        assert name == "worker-demo"
        assert "Resume work" in text

        # Tick 2 inside the 30-min dedupe window → no re-ping.
        result2 = work_progress_sweep_handler({})
        assert result2["pinged"] == 0
        assert result2["deduped"] >= 1
        assert len(svc.sent) == 1, "sweeper must respect dedupe"

    def test_active_turn_prevents_ping(
        self, pg_sweep_harness, monkeypatch, tmp_path,
    ):
        work, store, _advance = pg_sweep_harness
        bus.clear_listeners()
        _claim_worker_task(work, project="demo", title="Implement")

        svc = FakeSessionService(
            handles=[FakeHandle("worker-demo")], busy={"worker-demo"},
        )
        services = _RuntimeServices(
            session_service=svc,
            state_store=store,
            work_service=work,
            project_root=tmp_path,
            msg_store=store,
        )
        _install_sweep_loader(monkeypatch, services)

        result = work_progress_sweep_handler({})
        assert result["skipped_active_turn"] == 1
        assert result["pinged"] == 0
        assert svc.sent == []

    def test_no_session_for_task_is_skipped(
        self, pg_sweep_harness, monkeypatch, tmp_path,
    ):
        work, store, _advance = pg_sweep_harness
        bus.clear_listeners()
        _claim_worker_task(work, project="demo", title="Implement")

        # Nobody is running.
        svc = FakeSessionService(handles=[])
        services = _RuntimeServices(
            session_service=svc,
            state_store=store,
            work_service=work,
            project_root=tmp_path,
            msg_store=store,
        )
        _install_sweep_loader(monkeypatch, services)

        result = work_progress_sweep_handler({})
        assert result["outcome"] == "swept"
        assert result["skipped_no_session"] == 1
        assert result["pinged"] == 0
        assert svc.sent == []


# ---------------------------------------------------------------------------
# (2) Kickoff sweep — the #922 force-push branch needs
#     ``PgWorkService.kickoff_sent_at`` which is not yet implemented.
#     Pin the fallback contract until the pg port lands.
# ---------------------------------------------------------------------------


class TestKickoffSweepFallbackContract:
    """#922 / #923 — production-code gap until pg gains ``kickoff_sent_at``.

    The force-push branch in
    :func:`pollypm.plugins_builtin.task_assignment_notify.handlers.sweep._kickoff_pending`
    short-circuits to False when the work service has no
    ``kickoff_sent_at`` attribute. That keeps the legacy idle-gated +
    throttled behaviour as the fallback. This regression locks the
    fallback shape so a future ``PgWorkService.kickoff_sent_at`` port
    can re-enable the force branch without surprising the existing
    deployments.
    """

    def test_pg_work_service_does_not_yet_expose_kickoff_sent_at(
        self, pg_sweep_harness,
    ):
        """Lock the production-gap status quo until the pg port lands.

        When the gap closes (``PgWorkService.kickoff_sent_at``), this
        assertion will fail loudly — at which point the deleted #922
        scenario can be ported as a real force-push test against the
        long-lived ``pg_work_service``.
        """
        work, _store, _advance = pg_sweep_harness
        getter = getattr(work, "kickoff_sent_at", None)
        assert not callable(getter), (
            "PgWorkService now exposes `kickoff_sent_at` — port the "
            "#922 force-push scenario from the deleted "
            "test_kickoff_sweep_force_push.py module here and remove "
            "this guard."
        )

    def test_sweep_takes_standard_path_when_kickoff_api_missing(
        self, pg_sweep_harness, monkeypatch, tmp_path,
    ):
        """A queued worker task on pg still gets pinged via the standard
        idle-gated path — the force branch is a no-op when the work
        service lacks the kickoff_sent_at probe.
        """
        work, store, _advance = pg_sweep_harness
        bus.clear_listeners()
        task = work.create(
            title="Implement charts",
            description="Implement the thing",
            type="task",
            project="demo",
            flow_template="standard",
            roles={"worker": "worker"},
            priority="normal",
        )
        work.queue(task.task_id, "pm")

        live_window = "worker-demo"
        svc = FakeSessionService(handles=[FakeHandle(live_window)])
        services = _RuntimeServices(
            session_service=svc,
            state_store=store,
            work_service=work,
            project_root=tmp_path,
            msg_store=store,
        )
        monkeypatch.setattr(
            "pollypm.plugins_builtin.task_assignment_notify.handlers.sweep.load_runtime_services",
            lambda *, config_path=None: services,
        )

        result = task_assignment_sweep_handler({})

        # The force-push outcome is NOT reported (no kickoff_sent_at
        # API on pg yet). The standard path still delivers when the
        # session is idle.
        assert result["by_outcome"].get("forced_kickoff", 0) == 0


# ---------------------------------------------------------------------------
# (3) reconcile_expected_advance — pure-function plan-heuristic.
#     Backend-neutral; ports straight from the deleted suite.
# ---------------------------------------------------------------------------


class TestReconcileExpectedAdvancePure:
    """Pure-function tests. No work-service required — the plan-project
    heuristic short-circuits on the flow id.
    """

    def test_no_deliverables_returns_none(self, tmp_path: Path) -> None:
        task = FakeTask(
            project="demo", task_number=1,
            flow_template_id="plan_project",
            current_node_id="research",
        )
        result = reconcile_expected_advance(
            task, tmp_path, work_service=None, state_store=None,
        )
        assert result is None

    def test_plan_and_notify_routes_to_user_approval(
        self, pg_schema_pool, tmp_path: Path,
    ) -> None:
        _write_plan(tmp_path / "docs" / "plan" / "plan.md")
        # Post-sqlite-ripout (refs #1971): the message store is pg now.
        # ``pg_schema_pool`` bootstraps the per-test schema; ``PgStore``
        # then resolves to that same pool via the process-wide
        # singleton, so events recorded here are visible to
        # ``reconcile_expected_advance``'s ``query_messages`` read.
        store = PgStore(url="postgresql://test/ignored")
        _record_plan_ready_notify(store, project="demo")
        task = FakeTask(
            project="demo", task_number=1,
            flow_template_id="plan_project",
            current_node_id="research",
        )
        result = reconcile_expected_advance(
            task, tmp_path, work_service=None, state_store=store,
        )
        assert result is not None
        assert result.advance_to_node == "user_approval"
        assert "plan" in result.reason.lower()

    def test_plan_file_only_routes_to_synthesize(
        self, pg_schema_pool, tmp_path: Path,
    ) -> None:
        _write_plan(tmp_path / "docs" / "plan" / "plan.md")
        store = PgStore(url="postgresql://test/ignored")
        task = FakeTask(
            project="demo", task_number=1,
            flow_template_id="plan_project",
            current_node_id="research",
        )
        result = reconcile_expected_advance(
            task, tmp_path, work_service=None, state_store=store,
        )
        assert result is not None
        assert result.advance_to_node == "synthesize"

    def test_legacy_project_plan_path_accepted(
        self, pg_schema_pool, tmp_path: Path,
    ) -> None:
        """Tasks that wrote to docs/project-plan.md (pre-spec-revision
        architects) are honoured too."""
        _write_plan(tmp_path / "docs" / "project-plan.md")
        store = PgStore(url="postgresql://test/ignored")
        _record_plan_ready_notify(store, project="demo")
        task = FakeTask(
            project="demo", task_number=1,
            flow_template_id="plan_project",
            current_node_id="research",
        )
        result = reconcile_expected_advance(
            task, tmp_path, work_service=None, state_store=store,
        )
        assert result is not None
        assert result.advance_to_node == "user_approval"


# ---------------------------------------------------------------------------
# (4) Worker turn-end heuristic — pure functions, no service needed.
# ---------------------------------------------------------------------------


class TestDetermineWorkerResponsePure:
    """``determine_worker_response`` is a pure tail-scan classifier."""

    def test_unclear_language_routes_to_blocking_question(self) -> None:
        transcript = (
            "Tried running the tests but the spec is unclear about "
            "whether the retry should be idempotent."
        )
        task = FakeTask(project="demo", task_number=1)
        result = determine_worker_response(
            task, "worker-demo", work_service=None, transcript=transcript,
        )
        assert result.kind == "blocking_question"
        assert "unclear" in result.question_excerpt.lower()

    def test_waiting_for_routes_to_blocking_question(self) -> None:
        transcript = "I'm waiting for clarification from the PM on the shape."
        task = FakeTask(project="demo", task_number=1)
        result = determine_worker_response(
            task, "worker-demo", work_service=None, transcript=transcript,
        )
        assert result.kind == "blocking_question"

    def test_need_decision_routes_to_blocking_question(self) -> None:
        transcript = "I need decision: should we use SQLite or Postgres?"
        task = FakeTask(project="demo", task_number=1)
        result = determine_worker_response(
            task, "worker-demo", work_service=None, transcript=transcript,
        )
        assert result.kind == "blocking_question"

    def test_clean_tail_routes_to_reprompt(self) -> None:
        transcript = (
            "Ran the tests and they pass. Committed as abc123. "
            "Ready to proceed to the next step."
        )
        task = FakeTask(project="demo", task_number=1)
        result = determine_worker_response(
            task, "worker-demo", work_service=None, transcript=transcript,
        )
        assert result.kind == "reprompt"
        assert result.question_excerpt == ""

    def test_empty_transcript_routes_to_reprompt(self) -> None:
        task = FakeTask(project="demo", task_number=1)
        result = determine_worker_response(
            task, "worker-demo", work_service=None, transcript="",
        )
        assert result.kind == "reprompt"


class TestIsWorkerSessionName:
    def test_dash_form_detected(self) -> None:
        assert is_worker_session_name("worker-demo")

    def test_underscore_form_detected(self) -> None:
        assert is_worker_session_name("worker_demo")

    def test_architect_not_worker(self) -> None:
        assert not is_worker_session_name("architect-demo")

    def test_polly_not_worker(self) -> None:
        assert not is_worker_session_name("polly")

    def test_empty_not_worker(self) -> None:
        assert not is_worker_session_name("")


class TestSendStandardReprompt:
    """Reprompt delivery against the canonical copy."""

    def test_sends_canonical_reprompt(self) -> None:
        svc = FakeSessionService(handles=[FakeHandle("worker-demo")])
        task = FakeTask(project="demo", task_number=2)
        result = send_standard_reprompt("worker-demo", task, svc)
        assert result is True
        assert len(svc.sent) == 1
        name, text = svc.sent[0]
        assert name == "worker-demo"
        assert text == WORKER_REPROMPT_TEXT
        assert "pm task done" in text
        assert "pm notify" in text

    def test_soft_fail_on_none_session(self) -> None:
        task = FakeTask(project="demo", task_number=2)
        assert send_standard_reprompt("worker-demo", task, None) is False


# ---------------------------------------------------------------------------
# (5) Inbox-item creation for a blocking question — against pg work svc.
# ---------------------------------------------------------------------------


class TestCreateBlockingQuestionInboxItemPg:
    """The blocking-question inbox item is created via the work service.
    We exercise it against :class:`PgWorkService` so the deleted
    sqlite suite's contract (labels, roles, title shape) is re-locked
    on the pg backend.
    """

    def test_creates_task_with_expected_labels_and_roles(
        self, pg_sweep_harness, tmp_path: Path,
    ) -> None:
        work, store, _advance = pg_sweep_harness
        task = FakeTask(project="demo", task_number=7)
        config = FakeConfig(projects={
            "demo": FakeProject(path=tmp_path, persona_name="Archie"),
        })

        inbox_task = create_blocking_question_inbox_item(
            task,
            "worker-demo",
            "unclear whether retries should be idempotent",
            work,
            config=config,
            state_store=store,
            msg_store=store,
        )
        assert inbox_task is not None
        labels = list(inbox_task.labels or [])
        assert "blocking_question" in labels
        assert "project:demo" in labels
        assert "task:demo/7" in labels
        assert "blocking_worker:worker-demo" in labels

        roles = inbox_task.roles or {}
        assert roles.get("requester") == "worker-demo"
        assert roles.get("operator") == "Archie"

        # Title surfaces the truncated excerpt alongside the task id.
        assert "demo/7" in inbox_task.title

    def test_falls_back_to_polly_when_no_persona(
        self, pg_sweep_harness, tmp_path: Path,
    ) -> None:
        work, _store, _advance = pg_sweep_harness
        task = FakeTask(project="demo", task_number=8)
        config = FakeConfig(projects={
            "demo": FakeProject(path=tmp_path, persona_name=None),
        })
        inbox_task = create_blocking_question_inbox_item(
            task,
            "worker-demo",
            "stuck on the edge case",
            work,
            config=config,
        )
        assert inbox_task is not None
        assert (inbox_task.roles or {}).get("operator") == "polly"

    def test_soft_fail_on_none_work_service(self) -> None:
        task = FakeTask(project="demo", task_number=9)
        result = create_blocking_question_inbox_item(
            task, "worker-demo", "stuck", work_service=None,
        )
        assert result is None


# ---------------------------------------------------------------------------
# (6) Approve + session teardown lifecycle — ported from
#     ``test_work_session_integration_regressions.py``.
# ---------------------------------------------------------------------------


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"],
        cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True,
    )
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def _git_stdout(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def _prepare_review_task_pg(
    pg_schema_pool, tmp_path: Path, *, stay_on_task_branch: bool,
):
    """Build a pg-backed work service with a task already advanced to review.

    Mirrors the helper from the deleted sqlite test, but the work
    service is :class:`PgWorkService` against ``pg_schema_pool``. Task
    lifecycle: create → queue → claim → node_done(code_change). The
    review state is what the approve regression tests need.
    """
    from pollypm.work.pg_service import PgWorkService

    repo = _git_repo(tmp_path)
    svc = PgWorkService(
        pool=pg_schema_pool, ro_pool=None, project_path=repo,
    )
    session_mgr = MagicMock()
    svc.set_session_manager(session_mgr)

    task = svc.create(
        title="Review task",
        description="Exercise approval lifecycle",
        type="task",
        project="proj",
        flow_template="standard",
        roles={"worker": "pete", "reviewer": "polly"},
        priority="normal",
        created_by="tester",
    )
    svc.queue(task.task_id, "pm")
    svc.claim(task.task_id, "pete")

    main_branch = _git_stdout(repo, "rev-parse", "--abbrev-ref", "HEAD")
    task_branch = f"task/{task.project}-{task.task_number}"
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-q", "-b", task_branch],
        check=True,
    )
    (repo / "feature.txt").write_text("done\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "feature.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "feat: worker change"],
        check=True,
    )
    if not stay_on_task_branch:
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "-q", main_branch],
            check=True,
        )

    svc.node_done(
        task.task_id,
        "pete",
        WorkOutput(
            type=OutputType.CODE_CHANGE,
            summary="Implemented feature X",
            artifacts=[
                Artifact(
                    kind=ArtifactKind.COMMIT,
                    description="feat: worker change",
                    ref="abc123",
                )
            ],
        ),
    )
    return repo, svc, task, session_mgr


class TestApproveSessionLifecyclePg:
    """Approve must keep the worker session alive when the integration
    is still pending (worker still on the task branch); it must tear
    the session down only after auto-merge has integrated the work.

    Ported from the deleted
    ``test_work_session_integration_regressions.py`` against
    :class:`PgWorkService`.
    """

    def test_approve_keeps_session_alive_until_work_is_integrated(
        self, pg_schema_pool, tmp_path,
    ):
        repo, svc, task, session_mgr = _prepare_review_task_pg(
            pg_schema_pool, tmp_path, stay_on_task_branch=True,
        )

        result = svc.approve(task.task_id, "polly")

        assert result.work_status == WorkStatus.DONE
        # Worker still on the task branch → integration pending → keep
        # the session alive so the worker can land the merge.
        assert _git_stdout(repo, "rev-parse", "--abbrev-ref", "HEAD") == (
            f"task/{task.project}-{task.task_number}"
        )
        session_mgr.teardown_worker.assert_not_called()

    def test_approve_tears_down_session_after_auto_merge_integration(
        self, pg_schema_pool, tmp_path,
    ):
        _repo, svc, task, session_mgr = _prepare_review_task_pg(
            pg_schema_pool, tmp_path, stay_on_task_branch=False,
        )

        result = svc.approve(task.task_id, "polly")

        assert result.work_status == WorkStatus.DONE
        session_mgr.teardown_worker.assert_called_once_with(task.task_id)


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


def _write_plan(path: Path, size_bytes: int = MIN_PLAN_SIZE_BYTES + 200) -> Path:
    """Write a plan.md of the requested size at ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    filler = "The plan. " * ((size_bytes // 10) + 1)
    path.write_text(filler, encoding="utf-8")
    return path


def _record_plan_ready_notify(
    store: Any, project: str, actor: str = "architect",
) -> None:
    """Record a ``pm notify``-shaped event so the reconciler sees it."""
    store.record_event(
        scope=actor,
        sender=actor,
        subject="inbox.message.created",
        payload={
            "message": (
                f"{actor} -> user: Plan ready for approval on {project} - "
                f"please review docs/plan/plan.md"
            ),
        },
    )
