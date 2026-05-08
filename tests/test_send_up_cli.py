"""Tests for ``pm send-up`` worker milestone CLI + emit module.

The CLI is a thin shim; the bulk of the behavior lives in
:mod:`pollypm.worker_milestone`. Tests cover both layers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from typer.testing import CliRunner

from pollypm.worker_milestone import (
    _build_milestone_body,
    _build_milestone_title,
    _is_recent_duplicate,
    emit_worker_milestone,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeTask:
    task_id: str
    project: str
    title: str = ""


@dataclass
class _FakeCard:
    task_id: str


@dataclass
class _ListEntry:
    description: str
    labels: list[str] = field(default_factory=list)
    created_at: float | None = None


class _FakeSvc:
    def __init__(
        self,
        task: _FakeTask | None = None,
        recent: list[_ListEntry] | None = None,
        *,
        get_raises: Exception | None = None,
        list_raises: Exception | None = None,
        create_raises: Exception | None = None,
    ) -> None:
        self._task = task
        self._recent = recent or []
        self._get_raises = get_raises
        self._list_raises = list_raises
        self._create_raises = create_raises
        self.create_calls: list[dict] = []

    def get(self, task_id: str) -> _FakeTask:
        if self._get_raises is not None:
            raise self._get_raises
        if self._task is None:
            raise LookupError(f"no task {task_id}")
        return self._task

    def list_tasks(self, **kwargs: Any) -> list[_ListEntry]:
        if self._list_raises is not None:
            raise self._list_raises
        return self._recent

    def create(self, **kwargs: Any) -> _FakeCard:
        if self._create_raises is not None:
            raise self._create_raises
        self.create_calls.append(kwargs)
        return _FakeCard(task_id=f"{kwargs['project']}/card-{len(self.create_calls)}")


# ---------------------------------------------------------------------------
# Helper-level tests
# ---------------------------------------------------------------------------


class TestBuildMilestoneTitle:
    def test_short_message_kept_intact(self) -> None:
        assert _build_milestone_title("foo/1", "outline ready") == "foo/1: outline ready"

    def test_long_message_truncated_with_ellipsis(self) -> None:
        msg = "x" * 200
        out = _build_milestone_title("foo/1", msg)
        assert out.startswith("foo/1: ")
        assert out.endswith("…")
        assert len(out) <= len("foo/1: ") + 80

    def test_message_stripped(self) -> None:
        assert _build_milestone_title("foo/1", "  hi  ") == "foo/1: hi"


class TestBuildMilestoneBody:
    def test_includes_task_title_when_present(self) -> None:
        task = _FakeTask(task_id="foo/1", project="foo", title="Render plan")
        body = _build_milestone_body(task, "outline drafted")
        assert "foo/1 — Render plan" in body
        assert "outline drafted" in body

    def test_omits_task_title_when_absent(self) -> None:
        task = _FakeTask(task_id="foo/1", project="foo", title="")
        body = _build_milestone_body(task, "outline drafted")
        assert "foo/1 — " not in body
        assert "Task:** foo/1" in body
        assert "outline drafted" in body


class TestRecentDuplicate:
    def test_no_match_when_label_missing(self) -> None:
        svc = _FakeSvc(recent=[_ListEntry(description="hi", labels=[])])
        assert _is_recent_duplicate(svc, "foo/1", "hi", now_seconds=1000.0) is False

    def test_match_when_label_and_text_match(self) -> None:
        svc = _FakeSvc(
            recent=[
                _ListEntry(
                    description="**Task:** foo/1\n\nhi",
                    labels=["worker_milestone"],
                    created_at=999.0,
                )
            ]
        )
        assert _is_recent_duplicate(svc, "foo/1", "hi", now_seconds=1000.0) is True

    def test_outside_window_returns_false(self) -> None:
        svc = _FakeSvc(
            recent=[
                _ListEntry(
                    description="hi",
                    labels=["worker_milestone"],
                    created_at=0.0,
                )
            ]
        )
        # 1000s ago > 300s default window
        assert _is_recent_duplicate(svc, "foo/1", "hi", now_seconds=1000.0) is False

    def test_query_failure_emits(self) -> None:
        svc = _FakeSvc(list_raises=RuntimeError("db down"))
        assert _is_recent_duplicate(svc, "foo/1", "hi", now_seconds=1000.0) is False


# ---------------------------------------------------------------------------
# emit_worker_milestone behavior
# ---------------------------------------------------------------------------


class TestEmitWorkerMilestone:
    def test_happy_path_creates_card(self) -> None:
        task = _FakeTask(task_id="foo/1", project="foo", title="Render plan")
        svc = _FakeSvc(task=task)

        card_id = emit_worker_milestone(
            svc, "foo/1", "outline drafted", actor="worker", now_seconds=1000.0
        )

        assert card_id == "foo/card-1"
        assert len(svc.create_calls) == 1
        call = svc.create_calls[0]
        assert call["project"] == "foo"
        assert call["title"] == "foo/1: outline drafted"
        assert "outline drafted" in call["description"]
        assert call["labels"] == ["worker_milestone"]
        assert call["created_by"] == "worker"
        assert call["roles"]["operator"] == "polly"
        assert call["priority"] == "normal"

    def test_empty_message_no_emit(self) -> None:
        svc = _FakeSvc(task=_FakeTask("foo/1", "foo"))
        assert emit_worker_milestone(svc, "foo/1", "   ") is None
        assert svc.create_calls == []

    def test_missing_task_no_emit(self) -> None:
        svc = _FakeSvc(task=None, get_raises=LookupError("nope"))
        assert emit_worker_milestone(svc, "foo/99", "hi") is None
        assert svc.create_calls == []

    def test_recent_duplicate_no_emit(self) -> None:
        task = _FakeTask("foo/1", "foo")
        recent = [
            _ListEntry(
                description="**Task:** foo/1\n\nrendering",
                labels=["worker_milestone"],
                created_at=999.0,
            )
        ]
        svc = _FakeSvc(task=task, recent=recent)

        assert (
            emit_worker_milestone(svc, "foo/1", "rendering", now_seconds=1000.0)
            is None
        )
        assert svc.create_calls == []

    def test_create_failure_returns_none(self) -> None:
        svc = _FakeSvc(
            task=_FakeTask("foo/1", "foo"),
            create_raises=RuntimeError("boom"),
        )
        assert emit_worker_milestone(svc, "foo/1", "hi") is None

    def test_actor_threaded_through(self) -> None:
        svc = _FakeSvc(task=_FakeTask("foo/1", "foo"))
        emit_worker_milestone(svc, "foo/1", "hi", actor="worker_render")
        call = svc.create_calls[0]
        assert call["created_by"] == "worker_render"
        assert call["roles"]["operator"] == "polly"


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


class TestSendUpCli:
    """Exercise the Typer command via CliRunner with the underlying svc mocked."""

    def _runner_app_and_svc(self, monkeypatch: pytest.MonkeyPatch, **svc_kwargs):
        import typer

        from pollypm.cli_features import send_up as send_up_mod

        # Build a fresh Typer app and register the send-up command.
        # A second no-op command is registered so Typer treats this as
        # a multi-command app — otherwise a single-command Typer app
        # collapses the command name and fails to recognize ``send-up``
        # as a subcommand prefix.
        app = typer.Typer()
        helpers = type(
            "H",
            (),
            {
                "_load_supervisor": lambda *a, **k: None,
                "_discover_config_path": lambda p: p,
            },
        )()
        send_up_mod.register_send_up_commands(app, helpers=helpers)

        @app.command("noop", hidden=True)
        def _noop() -> None:  # pragma: no cover — keeps app multi-command
            pass

        svc = _FakeSvc(**svc_kwargs)
        monkeypatch.setattr(send_up_mod, "_load_work_service", lambda db, tid: svc)
        return app, svc

    def test_happy_path_emits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app, svc = self._runner_app_and_svc(
            monkeypatch, task=_FakeTask("foo/1", "foo")
        )
        runner = CliRunner()
        result = runner.invoke(app, ["send-up", "outline drafted", "--task", "foo/1"])
        assert result.exit_code == 0, result.output
        assert "emitted" in result.output
        assert len(svc.create_calls) == 1
        assert svc.create_calls[0]["title"] == "foo/1: outline drafted"

    def test_missing_task_id_clean_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Ensure POLLYPM_TASK_ID is not set
        monkeypatch.delenv("POLLYPM_TASK_ID", raising=False)
        app, _svc = self._runner_app_and_svc(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(app, ["send-up", "hi"])
        assert result.exit_code == 1
        assert "no task id" in result.output.lower()

    def test_env_var_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POLLYPM_TASK_ID", "foo/1")
        app, svc = self._runner_app_and_svc(
            monkeypatch, task=_FakeTask("foo/1", "foo")
        )
        runner = CliRunner()
        result = runner.invoke(app, ["send-up", "hi"])
        assert result.exit_code == 0, result.output
        assert len(svc.create_calls) == 1

    def test_dedupe_no_emit_path_succeeds_silently(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recent = [
            _ListEntry(
                description="**Task:** foo/1\n\nhi",
                labels=["worker_milestone"],
                created_at=None,  # treated as recent
            )
        ]
        app, svc = self._runner_app_and_svc(
            monkeypatch, task=_FakeTask("foo/1", "foo"), recent=recent
        )
        runner = CliRunner()
        result = runner.invoke(app, ["send-up", "hi", "--task", "foo/1"])
        assert result.exit_code == 0
        assert "nothing emitted" in result.output
        assert svc.create_calls == []

    def test_json_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import json

        app, svc = self._runner_app_and_svc(
            monkeypatch, task=_FakeTask("foo/1", "foo")
        )
        runner = CliRunner()
        result = runner.invoke(
            app, ["send-up", "outline drafted", "--task", "foo/1", "--json"]
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["task_id"] == "foo/1"
        assert payload["card_id"] == "foo/card-1"
        assert payload["deduped"] is False
