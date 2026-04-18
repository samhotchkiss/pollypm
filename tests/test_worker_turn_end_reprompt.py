"""Tests for #302 — worker turn-end auto-reprompt.

Covers:

* ``determine_worker_response`` — the pure heuristic classifying a
  transcript tail as blocking_question vs reprompt.
* ``create_blocking_question_inbox_item`` — label shape + role
  targeting on the inbox task written to the work-service.
* ``send_standard_reprompt`` — session_service.send is invoked with
  the canonical reprompt copy.
* ``handle_worker_turn_end`` — the dispatcher picks exactly one
  action path, never both, for a worker session; non-worker sessions
  short-circuit.
* Sweep integration — drift on a worker session (simulated) runs the
  worker-specific path; drift on a non-worker session skips it.
* Inbox UI — ``blocking_question`` label is detected; the hint bar
  renders PM-appropriate copy; reply routes via the supervisor's
  ``send_input`` with ``force=True``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from pollypm.recovery.worker_turn_end import (
    WORKER_REPROMPT_TEXT,
    WorkerResponse,
    create_blocking_question_inbox_item,
    determine_worker_response,
    handle_worker_turn_end,
    is_worker_session_name,
    send_standard_reprompt,
)
from pollypm.storage.state import StateStore
from pollypm.work import task_assignment as bus
from pollypm.work.sqlite_service import SQLiteWorkService


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeHandle:
    name: str


@dataclass
class FakeSessionService:
    """Minimal session service exposing ``list`` / ``send`` / ``capture``.

    ``capture_text`` is a single canned transcript string; ``sent``
    captures every ``send`` call for assertions.
    """

    handles: list[FakeHandle]
    capture_text: str = ""
    sent: list[tuple[str, str]] = field(default_factory=list)
    busy: set[str] = field(default_factory=set)

    def list(self) -> list[FakeHandle]:
        return list(self.handles)

    def send(self, name: str, text: str, *, press_enter: bool = True) -> None:
        self.sent.append((name, text))

    def is_turn_active(self, name: str) -> bool:
        return name in self.busy

    def capture(self, name: str, lines: int = 200) -> str:
        return self.capture_text


@dataclass
class FakeProject:
    path: Path
    persona_name: str | None = None


@dataclass
class FakeConfig:
    projects: dict[str, FakeProject]


@dataclass
class FakeTask:
    project: str
    task_number: int
    current_node_id: str = "do_work"
    flow_template_id: str = "standard"
    title: str = "Do the work"

    @property
    def task_id(self) -> str:
        return f"{self.project}/{self.task_number}"


# ---------------------------------------------------------------------------
# determine_worker_response — pure heuristic
# ---------------------------------------------------------------------------


class TestDetermineWorkerResponse:
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

    def test_pm_notify_attempt_routes_to_blocking_question(self) -> None:
        transcript = (
            "About to hand off.\n"
            "pm notify 'demo/3: how should errors be surfaced?' "
            "--priority immediate"
        )
        task = FakeTask(project="demo", task_number=3)
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

    def test_long_transcript_only_tail_scanned(self) -> None:
        """Only the last ~2000 chars are scanned so an old 'unclear'
        mention far from the tail doesn't falsely trip the classifier.
        """
        head = "unclear unclear unclear\n" + ("noise line\n" * 300)
        transcript = head + "All good, ready to move on."
        task = FakeTask(project="demo", task_number=1)
        result = determine_worker_response(
            task, "worker-demo", work_service=None, transcript=transcript,
        )
        # Tail (~2k chars) still contains "unclear" only if the head is
        # shorter than the tail window. Build a transcript where the
        # head is definitively past the tail window.
        head = "unclear " * 300  # ~2400 chars of "unclear"
        filler = ("x" * 80 + "\n") * 30  # another ~2.5k chars
        clean_tail = "Everything passes. Proceeding to next step."
        transcript = head + filler + clean_tail
        assert len(transcript) > 4000
        result = determine_worker_response(
            task, "worker-demo", work_service=None, transcript=transcript,
        )
        assert result.kind == "reprompt", (
            "tail-only scan should have missed the 'unclear' head"
        )


# ---------------------------------------------------------------------------
# is_worker_session_name
# ---------------------------------------------------------------------------


class TestIsWorkerSessionName:
    def test_dash_form_detected(self) -> None:
        assert is_worker_session_name("worker-demo")

    def test_underscore_form_detected(self) -> None:
        assert is_worker_session_name("worker_demo")

    def test_architect_not_worker(self) -> None:
        assert not is_worker_session_name("architect-demo")

    def test_reviewer_not_worker(self) -> None:
        assert not is_worker_session_name("reviewer")

    def test_polly_not_worker(self) -> None:
        assert not is_worker_session_name("polly")

    def test_empty_not_worker(self) -> None:
        assert not is_worker_session_name("")


# ---------------------------------------------------------------------------
# create_blocking_question_inbox_item — label + role shape
# ---------------------------------------------------------------------------


class TestCreateBlockingQuestionInboxItem:
    def test_creates_task_with_expected_labels_and_roles(
        self, tmp_path: Path,
    ) -> None:
        work = SQLiteWorkService(
            db_path=tmp_path / "work.db",
            project_path=tmp_path,
        )
        task = FakeTask(project="demo", task_number=7)
        store = StateStore(tmp_path / "state.db")
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

        # Event landed on the ledger.
        events = [
            e for e in store.recent_events(limit=10)
            if e.event_type == "inbox.blocking_question.created"
        ]
        assert len(events) == 1
        assert events[0].session_name == "worker-demo"
        work.close()

    def test_falls_back_to_polly_when_no_persona(
        self, tmp_path: Path,
    ) -> None:
        work = SQLiteWorkService(
            db_path=tmp_path / "work.db",
            project_path=tmp_path,
        )
        task = FakeTask(project="demo", task_number=7)
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
        work.close()

    def test_soft_fail_on_none_work_service(self) -> None:
        task = FakeTask(project="demo", task_number=7)
        result = create_blocking_question_inbox_item(
            task, "worker-demo", "stuck", work_service=None,
        )
        assert result is None


# ---------------------------------------------------------------------------
# send_standard_reprompt
# ---------------------------------------------------------------------------


class TestSendStandardReprompt:
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

    def test_records_event_on_state_store(self, tmp_path: Path) -> None:
        svc = FakeSessionService(handles=[FakeHandle("worker-demo")])
        store = StateStore(tmp_path / "state.db")
        task = FakeTask(project="demo", task_number=2)
        assert send_standard_reprompt(
            "worker-demo", task, svc, state_store=store,
        ) is True
        events = [
            e for e in store.recent_events(limit=10)
            if e.event_type == "inbox.worker_reprompted"
        ]
        assert len(events) == 1
        assert "demo/2" in events[0].message

    def test_soft_fail_on_none_session(self) -> None:
        task = FakeTask(project="demo", task_number=2)
        assert send_standard_reprompt("worker-demo", task, None) is False


# ---------------------------------------------------------------------------
# handle_worker_turn_end — dispatcher: exactly one path runs
# ---------------------------------------------------------------------------


class TestHandleWorkerTurnEnd:
    def test_worker_with_blocker_creates_inbox_item_only(
        self, tmp_path: Path,
    ) -> None:
        work = SQLiteWorkService(
            db_path=tmp_path / "work.db",
            project_path=tmp_path,
        )
        task = FakeTask(project="demo", task_number=5)
        svc = FakeSessionService(
            handles=[FakeHandle("worker-demo")],
            capture_text=(
                "Trying to implement the retry. The spec is unclear "
                "about idempotency — need guidance."
            ),
        )
        store = StateStore(tmp_path / "state.db")
        config = FakeConfig(projects={
            "demo": FakeProject(path=tmp_path, persona_name="Polly"),
        })

        outcome = handle_worker_turn_end(
            task, "worker-demo",
            work_service=work,
            session_service=svc,
            state_store=store,
            config=config,
        )
        assert outcome == "blocking_question"
        # No reprompt was sent — one path only.
        assert svc.sent == []
        # But an inbox task was created.
        inbox_tasks = work.list_tasks(project="demo")
        blocking = [
            t for t in inbox_tasks
            if "blocking_question" in (t.labels or [])
        ]
        assert len(blocking) == 1
        work.close()

    def test_worker_with_clean_tail_reprompts_only(
        self, tmp_path: Path,
    ) -> None:
        work = SQLiteWorkService(
            db_path=tmp_path / "work.db",
            project_path=tmp_path,
        )
        task = FakeTask(project="demo", task_number=6)
        svc = FakeSessionService(
            handles=[FakeHandle("worker-demo")],
            capture_text="Tests pass. Committed. Ready for the next step.",
        )
        store = StateStore(tmp_path / "state.db")
        config = FakeConfig(projects={
            "demo": FakeProject(path=tmp_path, persona_name="Polly"),
        })

        outcome = handle_worker_turn_end(
            task, "worker-demo",
            work_service=work,
            session_service=svc,
            state_store=store,
            config=config,
        )
        assert outcome == "reprompt"
        # A reprompt was sent to the worker.
        assert len(svc.sent) == 1
        assert svc.sent[0][0] == "worker-demo"
        assert svc.sent[0][1] == WORKER_REPROMPT_TEXT
        # No blocking_question inbox task was created.
        inbox_tasks = work.list_tasks(project="demo")
        blocking = [
            t for t in inbox_tasks
            if "blocking_question" in (t.labels or [])
        ]
        assert blocking == []
        work.close()

    def test_non_worker_session_skipped(self, tmp_path: Path) -> None:
        work = SQLiteWorkService(
            db_path=tmp_path / "work.db",
            project_path=tmp_path,
        )
        task = FakeTask(project="demo", task_number=8)
        svc = FakeSessionService(
            handles=[FakeHandle("architect-demo")],
            capture_text="Even with unclear language here.",
        )
        store = StateStore(tmp_path / "state.db")

        outcome = handle_worker_turn_end(
            task, "architect-demo",
            work_service=work,
            session_service=svc,
            state_store=store,
        )
        assert outcome == "skipped"
        assert svc.sent == []
        inbox_tasks = work.list_tasks(project="demo")
        assert [t for t in inbox_tasks if "blocking_question" in (
            t.labels or []
        )] == []
        work.close()

    def test_operator_session_skipped(self, tmp_path: Path) -> None:
        work = SQLiteWorkService(
            db_path=tmp_path / "work.db",
            project_path=tmp_path,
        )
        task = FakeTask(project="demo", task_number=9)
        svc = FakeSessionService(
            handles=[FakeHandle("operator")],
            capture_text="unclear",
        )
        outcome = handle_worker_turn_end(
            task, "operator",
            work_service=work,
            session_service=svc,
            state_store=None,
        )
        assert outcome == "skipped"
        work.close()


# ---------------------------------------------------------------------------
# Sweep integration — drift on a worker session routes through the
# worker-specific path. We simulate drift by monkey-patching
# ``reconcile_expected_advance`` since the v1 heuristic only fires on
# plan_project flows (targeting architect sessions).
# ---------------------------------------------------------------------------


class TestSweepRoutesWorkerDrift:
    def _patch_resolver(self, monkeypatch, tmp_path, svc, store):
        from pollypm.plugins_builtin.task_assignment_notify.resolver import (
            _RuntimeServices,
        )

        def _fake_loader(*, config_path=None):
            work = SQLiteWorkService(
                db_path=tmp_path / "work.db",
                project_path=tmp_path,
            )
            return _RuntimeServices(
                session_service=svc,
                state_store=store,
                work_service=work,
                project_root=tmp_path,
            )

        monkeypatch.setattr(
            "pollypm.plugins_builtin.task_assignment_notify.resolver.load_runtime_services",
            _fake_loader,
        )

    def _force_drift_everywhere(self, monkeypatch):
        """Force ``reconcile_expected_advance`` to return a drift action
        regardless of flow. Lets us exercise the worker branch without
        depending on plan_project's heuristic narrowness.

        Patches the canonical module the sweep handler imports from
        (``pollypm.recovery.state_reconciliation``) rather than
        re-binding the name inside the handler — the handler does a
        local import each sweep, so the patch is picked up on the
        next call.
        """
        from pollypm.recovery.state_reconciliation import (
            ReconciliationAction,
        )

        def _always_drift(
            task, project_path, work_service,
            *, state_store=None, now=None,
        ):
            return ReconciliationAction(
                advance_to_node="next",
                reason="forced drift for test",
            )

        monkeypatch.setattr(
            "pollypm.recovery.state_reconciliation."
            "reconcile_expected_advance",
            _always_drift,
        )

    def test_worker_drift_runs_worker_specific_path(
        self, tmp_path, monkeypatch,
    ):
        from pollypm.plugins_builtin.core_recurring.plugin import (
            work_progress_sweep_handler,
        )

        bus.clear_listeners()
        seed = SQLiteWorkService(
            db_path=tmp_path / "work.db",
            project_path=tmp_path,
        )
        # Standard flow → worker actor → target resolves to "worker-demo".
        task = seed.create(
            title="Do the thing",
            description="desc",
            type="task",
            project="demo",
            flow_template="standard",
            roles={"worker": "worker", "reviewer": "reviewer"},
            priority="normal",
        )
        seed.queue(task.task_id, "pm")
        seed.claim(task.task_id, "worker")
        seed.close()

        store = StateStore(tmp_path / "state.db")
        # Fresh capture_text: clean → reprompt path.
        svc = FakeSessionService(
            handles=[FakeHandle("worker-demo")],
            capture_text="All tests pass. Committed.",
        )
        self._patch_resolver(monkeypatch, tmp_path, svc, store)
        self._force_drift_everywhere(monkeypatch)

        result = work_progress_sweep_handler({})
        assert result["outcome"] == "swept"
        assert result["drift_detected"] >= 1
        # The worker-specific path chose reprompt.
        assert result["worker_reprompts"] == 1
        assert result["worker_blocking_questions"] == 0
        # And the reprompt actually landed on the session service.
        assert any(
            name == "worker-demo" and WORKER_REPROMPT_TEXT in text
            for name, text in svc.sent
        )

    def test_worker_drift_with_blocker_creates_inbox_item(
        self, tmp_path, monkeypatch,
    ):
        from pollypm.plugins_builtin.core_recurring.plugin import (
            work_progress_sweep_handler,
        )

        bus.clear_listeners()
        seed = SQLiteWorkService(
            db_path=tmp_path / "work.db",
            project_path=tmp_path,
        )
        task = seed.create(
            title="Do the thing",
            description="desc",
            type="task",
            project="demo",
            flow_template="standard",
            roles={"worker": "worker", "reviewer": "reviewer"},
            priority="normal",
        )
        seed.queue(task.task_id, "pm")
        seed.claim(task.task_id, "worker")
        seed.close()

        store = StateStore(tmp_path / "state.db")
        svc = FakeSessionService(
            handles=[FakeHandle("worker-demo")],
            capture_text=(
                "Hit a wall — unclear whether we should retry on 5xx "
                "or surface the error."
            ),
        )
        self._patch_resolver(monkeypatch, tmp_path, svc, store)
        self._force_drift_everywhere(monkeypatch)

        result = work_progress_sweep_handler({})
        assert result["drift_detected"] >= 1
        assert result["worker_blocking_questions"] == 1
        assert result["worker_reprompts"] == 0
        # No reprompt was sent — inbox item path is exclusive.
        assert svc.sent == []

    def test_non_worker_drift_skips_worker_path(
        self, tmp_path, monkeypatch,
    ):
        """Architect / reviewer drift falls through to log+alert only —
        no reprompt, no blocking_question item."""
        from pollypm.plugins_builtin.core_recurring.plugin import (
            work_progress_sweep_handler,
        )

        bus.clear_listeners()
        seed = SQLiteWorkService(
            db_path=tmp_path / "work.db",
            project_path=tmp_path,
        )
        task = seed.create(
            title="Plan it",
            description="desc",
            type="task",
            project="demo",
            flow_template="plan_project",
            roles={"architect": "architect"},
            priority="normal",
        )
        seed.queue(task.task_id, "pm")
        seed.claim(task.task_id, "architect")
        seed.close()

        store = StateStore(tmp_path / "state.db")
        svc = FakeSessionService(
            handles=[FakeHandle("architect-demo")],
            capture_text="unclear unclear unclear",
        )
        self._patch_resolver(monkeypatch, tmp_path, svc, store)
        self._force_drift_everywhere(monkeypatch)

        result = work_progress_sweep_handler({})
        assert result["drift_detected"] >= 1
        assert result["worker_blocking_questions"] == 0
        assert result["worker_reprompts"] == 0
        # Architect should not receive a reprompt — non-worker roles
        # keep the v1 log+alert-only behaviour.
        assert svc.sent == []


# ---------------------------------------------------------------------------
# Inbox UI — blocking_question label detection + hint bar
# ---------------------------------------------------------------------------


class TestInboxUIBlockingQuestion:
    def test_extract_meta_parses_sidecar_labels(self) -> None:
        from pollypm.cockpit_ui import _extract_blocking_question_meta

        labels = [
            "blocking_question",
            "project:demo",
            "task:demo/7",
            "blocking_worker:worker-demo",
        ]
        meta = _extract_blocking_question_meta(labels)
        assert meta.get("task_id") == "demo/7"
        assert meta.get("blocking_worker") == "worker-demo"
        assert meta.get("project") == "demo"

    def test_extract_meta_ignores_unrelated_labels(self) -> None:
        from pollypm.cockpit_ui import _extract_blocking_question_meta

        meta = _extract_blocking_question_meta([
            "blocking_question", "other", "proposal",
        ])
        assert "task_id" not in meta
        assert "blocking_worker" not in meta

    def test_hint_bar_copy_covers_pm_actions(self) -> None:
        """The hint bar for a blocking_question item surfaces the three
        actions the PM needs: reply to worker, jump to worker, archive.
        """
        from pollypm.cockpit_ui import PollyInboxApp

        hint = PollyInboxApp._BLOCKING_QUESTION_HINT
        assert "reply to worker" in hint
        assert "jump to worker" in hint
        assert "archive" in hint

    def test_pm_reply_routes_via_supervisor_send_with_force(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """When the PM presses ``r`` and sends a reply on a
        blocking_question item, the text is forwarded to the worker
        session via ``supervisor.send_input`` with ``force=True`` —
        the ``pm send --force`` semantics from #261.
        """
        from pollypm.cockpit_ui import PollyInboxApp

        # Partially-constructed instance — we only exercise the method
        # directly, so __init__ isn't needed.
        app = PollyInboxApp.__new__(PollyInboxApp)
        app.config_path = tmp_path / "config.toml"

        calls: list[dict[str, Any]] = []

        class _FakeSupervisor:
            def send_input(
                self, name: str, text: str, *, owner: str,
                force: bool, press_enter: bool,
            ) -> None:
                calls.append({
                    "name": name, "text": text, "owner": owner,
                    "force": force, "press_enter": press_enter,
                })

        class _FakeService:
            def __init__(self, _path): ...
            def load_supervisor(self): return _FakeSupervisor()

        monkeypatch.setattr(
            "pollypm.service_api.v1.PollyPMService", _FakeService,
        )

        notifications: list[tuple[str, str]] = []
        app.notify = lambda msg, **kw: notifications.append(  # type: ignore[assignment]
            (msg, kw.get("severity", "info")),
        )
        app._emit_event = lambda *args, **kw: None  # type: ignore[assignment]

        app._send_reply_to_worker(
            "demo/42", "worker-demo", "retry on 5xx, surface on 4xx",
        )
        assert len(calls) == 1
        call = calls[0]
        assert call["name"] == "worker-demo"
        assert call["force"] is True
        assert call["press_enter"] is True
        assert "retry on 5xx" in call["text"]
