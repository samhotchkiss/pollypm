"""Tests for ``pollypm.task_shipped.emit_task_shipped_card``.

These tests focus on the emit hook itself — what gets pulled from the
worker's WorkOutput, how the card is shaped, and that the function is
robust to missing/empty data. The hook is wired into the
``service_transitions.on_task_done`` flow; the wire-up is exercised by
the integration test at the bottom.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pollypm.task_shipped import (
    _build_card_body,
    _build_card_title,
    _extract_commit_refs,
    _extract_deploy_urls,
    _latest_work_output,
    emit_task_shipped_card,
)
from pollypm.work.models import (
    Artifact,
    ArtifactKind,
    OutputType,
    WorkOutput,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class _FakeExecution:
    work_output: Any | None = None


@dataclass
class _FakeTask:
    task_id: str
    title: str
    project: str


@dataclass
class _FakeCard:
    task_id: str


class _FakeSvc:
    """Minimal stand-in for SQLiteWorkService that records create() calls."""

    def __init__(
        self,
        executions: list[_FakeExecution],
        task: _FakeTask,
        *,
        get_execution_raises: Exception | None = None,
        get_raises: Exception | None = None,
        create_raises: Exception | None = None,
    ) -> None:
        self._executions = executions
        self._task = task
        self._get_execution_raises = get_execution_raises
        self._get_raises = get_raises
        self._create_raises = create_raises
        self.create_calls: list[dict] = []

    def get_execution(self, task_id: str) -> list[_FakeExecution]:
        if self._get_execution_raises is not None:
            raise self._get_execution_raises
        return self._executions

    def get(self, task_id: str) -> _FakeTask:
        if self._get_raises is not None:
            raise self._get_raises
        return self._task

    def create(self, **kwargs: Any) -> _FakeCard:
        if self._create_raises is not None:
            raise self._create_raises
        self.create_calls.append(kwargs)
        return _FakeCard(task_id=f"{kwargs['project']}/card-{len(self.create_calls)}")


def _wo(
    summary: str = "shipped",
    *,
    output_type: OutputType = OutputType.MIXED,
    artifacts: list[Artifact] | None = None,
) -> WorkOutput:
    return WorkOutput(
        type=output_type,
        summary=summary,
        artifacts=list(artifacts or []),
    )


# ---------------------------------------------------------------------------
# Helper-level tests
# ---------------------------------------------------------------------------


class TestExtractCommitRefs:
    def test_pulls_from_commit_artifacts(self) -> None:
        wo = _wo(
            artifacts=[
                Artifact(kind=ArtifactKind.COMMIT, ref="abcdef0123456789", description="land"),
                Artifact(kind=ArtifactKind.NOTE, description="not a commit"),
                Artifact(kind=ArtifactKind.COMMIT, ref="0123abc4567890", description="bugfix"),
            ],
        )
        assert _extract_commit_refs(wo) == ["abcdef012345", "0123abc45678"]

    def test_falls_back_to_external_ref(self) -> None:
        wo = _wo(
            artifacts=[
                Artifact(kind=ArtifactKind.COMMIT, external_ref="0123456789abcdef", description="x"),
            ],
        )
        assert _extract_commit_refs(wo) == ["0123456789ab"]

    def test_skips_when_no_ref(self) -> None:
        wo = _wo(artifacts=[Artifact(kind=ArtifactKind.COMMIT, description="missing ref")])
        assert _extract_commit_refs(wo) == []

    def test_empty_artifacts(self) -> None:
        assert _extract_commit_refs(_wo(artifacts=[])) == []


class TestExtractDeployUrls:
    def test_action_external_ref(self) -> None:
        wo = _wo(
            artifacts=[
                Artifact(
                    kind=ArtifactKind.ACTION,
                    description="deploy",
                    external_ref="https://coffeeboardnm.itsalive.co",
                ),
            ],
        )
        assert _extract_deploy_urls(wo) == ["https://coffeeboardnm.itsalive.co"]

    def test_extracts_url_from_description_when_no_external_ref(self) -> None:
        wo = _wo(
            artifacts=[
                Artifact(
                    kind=ArtifactKind.ACTION,
                    description="Live at https://example.test/page (verified ok=True).",
                ),
            ],
        )
        assert _extract_deploy_urls(wo) == ["https://example.test/page"]

    def test_dedupes_repeats(self) -> None:
        wo = _wo(
            artifacts=[
                Artifact(
                    kind=ArtifactKind.ACTION,
                    description="x",
                    external_ref="https://example.test",
                ),
                Artifact(
                    kind=ArtifactKind.ACTION,
                    description="see https://example.test for more",
                ),
            ],
        )
        assert _extract_deploy_urls(wo) == ["https://example.test"]

    def test_only_action_artifacts(self) -> None:
        wo = _wo(
            artifacts=[
                Artifact(
                    kind=ArtifactKind.NOTE,
                    description="see https://nope.test",
                ),
                Artifact(kind=ArtifactKind.COMMIT, ref="abc1234", description="land"),
            ],
        )
        assert _extract_deploy_urls(wo) == []

    def test_trims_trailing_punctuation(self) -> None:
        wo = _wo(
            artifacts=[
                Artifact(
                    kind=ArtifactKind.ACTION,
                    description="deploy at https://example.test/ok.",
                ),
            ],
        )
        assert _extract_deploy_urls(wo) == ["https://example.test/ok"]


class TestLatestWorkOutput:
    def test_returns_most_recent(self) -> None:
        first = _wo(summary="old")
        second = _wo(summary="new")
        svc = _FakeSvc(
            executions=[_FakeExecution(first), _FakeExecution(second)],
            task=_FakeTask("p/1", "t", "p"),
        )
        result = _latest_work_output(svc, "p/1")
        assert result is second

    def test_skips_executions_without_output(self) -> None:
        only = _wo(summary="only")
        svc = _FakeSvc(
            executions=[_FakeExecution(only), _FakeExecution(None)],
            task=_FakeTask("p/1", "t", "p"),
        )
        # reversed walk hits the None first; should skip and find ``only``.
        assert _latest_work_output(svc, "p/1") is only

    def test_returns_none_on_lookup_error(self) -> None:
        svc = _FakeSvc(
            executions=[],
            task=_FakeTask("p/1", "t", "p"),
            get_execution_raises=RuntimeError("boom"),
        )
        assert _latest_work_output(svc, "p/1") is None

    def test_returns_none_when_no_executions(self) -> None:
        svc = _FakeSvc(executions=[], task=_FakeTask("p/1", "t", "p"))
        assert _latest_work_output(svc, "p/1") is None


class TestBuildCardTitle:
    def test_url_takes_priority_over_summary(self) -> None:
        title = _build_card_title(
            "demo/1", "did the thing", ["https://example.test"]
        )
        assert title == "✓ demo/1 SHIPPED — https://example.test"

    def test_falls_back_to_summary_when_no_url(self) -> None:
        title = _build_card_title("demo/1", "did the thing", [])
        assert title == "✓ demo/1 SHIPPED — did the thing"

    def test_truncates_long_summary(self) -> None:
        long_summary = "x" * 200
        title = _build_card_title("demo/1", long_summary, [])
        assert title.endswith("x" * 80)
        assert "x" * 81 not in title

    def test_just_marker_when_neither(self) -> None:
        assert _build_card_title("demo/1", "", []) == "✓ demo/1 SHIPPED"


class TestBuildCardBody:
    def test_full_card(self) -> None:
        body = _build_card_body(
            "Build the POC plan",
            "delivered",
            ["https://example.test"],
            ["abc1234567"],
        )
        assert "Build the POC plan" in body
        assert "delivered" in body
        assert "**Deployed:**" in body
        assert "- https://example.test" in body
        assert "**Commits:** `abc1234567`" in body

    def test_no_commits_no_url(self) -> None:
        body = _build_card_body("Build the POC", "summary text", [], [])
        assert "Deployed" not in body
        assert "Commits" not in body
        assert "summary text" in body
        assert "Build the POC" in body


# ---------------------------------------------------------------------------
# emit_task_shipped_card behavior
# ---------------------------------------------------------------------------


class TestEmitTaskShippedCard:
    def test_emits_card_with_url_and_commit(self) -> None:
        wo = _wo(
            summary="POC plan rendered, deployed, verified ok=True",
            artifacts=[
                Artifact(kind=ArtifactKind.COMMIT, ref="abc12345678abc", description="land"),
                Artifact(
                    kind=ArtifactKind.ACTION,
                    description="deployed",
                    external_ref="https://coffeeboardnm.itsalive.co",
                ),
            ],
        )
        svc = _FakeSvc(
            executions=[_FakeExecution(wo)],
            task=_FakeTask(
                task_id="coffeeboardnm/1",
                title="POC plan: ingest every NM event May–Jul 2026",
                project="coffeeboardnm",
            ),
        )

        result = emit_task_shipped_card(svc, "coffeeboardnm/1", "polly")

        assert result == "coffeeboardnm/card-1"
        assert len(svc.create_calls) == 1
        call = svc.create_calls[0]
        assert "coffeeboardnm/1 SHIPPED" in call["title"]
        assert "https://coffeeboardnm.itsalive.co" in call["title"]
        assert "https://coffeeboardnm.itsalive.co" in call["description"]
        assert "abc12345678a" in call["description"]
        assert "task_shipped" in call["labels"]
        assert "has_deliverable_url" in call["labels"]
        assert call["flow_template"] == "chat"
        assert call["project"] == "coffeeboardnm"
        assert call["roles"] == {"requester": "user", "operator": "polly"}

    def test_no_emit_when_no_work_output(self) -> None:
        svc = _FakeSvc(
            executions=[_FakeExecution(None)],
            task=_FakeTask("p/1", "t", "p"),
        )
        result = emit_task_shipped_card(svc, "p/1", "polly")
        assert result is None
        assert svc.create_calls == []

    def test_no_emit_when_no_executions(self) -> None:
        svc = _FakeSvc(executions=[], task=_FakeTask("p/1", "t", "p"))
        result = emit_task_shipped_card(svc, "p/1", "polly")
        assert result is None
        assert svc.create_calls == []

    def test_emits_summary_only_when_no_artifacts(self) -> None:
        # A worker that calls pm task done with just a summary still
        # gets a delivery card — graceful absence of richer signal.
        wo = _wo(summary="documentation written")
        svc = _FakeSvc(
            executions=[_FakeExecution(wo)],
            task=_FakeTask("docs/3", "Update README", "docs"),
        )

        result = emit_task_shipped_card(svc, "docs/3", "polly")
        assert result == "docs/card-1"
        call = svc.create_calls[0]
        assert call["title"] == "✓ docs/3 SHIPPED — documentation written"
        assert "has_deliverable_url" not in call["labels"]
        assert "task_shipped" in call["labels"]

    def test_swallows_create_failure(self) -> None:
        wo = _wo(summary="done")
        svc = _FakeSvc(
            executions=[_FakeExecution(wo)],
            task=_FakeTask("p/1", "t", "p"),
            create_raises=RuntimeError("inbox down"),
        )
        # Best-effort: the transition path must not raise.
        result = emit_task_shipped_card(svc, "p/1", "polly")
        assert result is None

    def test_swallows_get_failure(self) -> None:
        wo = _wo(summary="done")
        svc = _FakeSvc(
            executions=[_FakeExecution(wo)],
            task=_FakeTask("p/1", "t", "p"),
            get_raises=RuntimeError("db gone"),
        )
        result = emit_task_shipped_card(svc, "p/1", "polly")
        assert result is None
        assert svc.create_calls == []

    def test_actor_default_polly(self) -> None:
        wo = _wo(summary="done")
        svc = _FakeSvc(
            executions=[_FakeExecution(wo)],
            task=_FakeTask("p/1", "t", "p"),
        )
        emit_task_shipped_card(svc, "p/1", "")
        assert svc.create_calls[0]["roles"]["operator"] == "polly"


# ---------------------------------------------------------------------------
# Wire-up: on_task_done invokes emit_task_shipped_card
# ---------------------------------------------------------------------------


class TestOnTaskDoneWireup:
    def test_on_task_done_calls_emit_after_digest_flush(self, monkeypatch) -> None:
        from pollypm.work import service_transitions

        flush_calls: list[tuple] = []
        emit_calls: list[tuple] = []

        def fake_flush(svc, **kwargs):  # noqa: ANN001
            flush_calls.append((kwargs.get("project"), kwargs.get("actor")))
            return None

        def fake_emit(svc, task_id, actor):  # noqa: ANN001
            emit_calls.append((task_id, actor))
            return "p/card-1"

        monkeypatch.setattr(
            "pollypm.notification_staging.check_and_flush_on_done", fake_flush
        )
        monkeypatch.setattr(
            "pollypm.task_shipped.emit_task_shipped_card", fake_emit
        )

        @dataclass
        class _Task:
            project: str
            task_id: str

        class _Svc:
            _project_path = None

            def get(self, task_id):  # noqa: ANN001
                return _Task(project="p", task_id=task_id)

        service_transitions.on_task_done(_Svc(), "p/1", "polly")

        assert flush_calls == [("p", "polly")]
        assert emit_calls == [("p/1", "polly")]

    def test_on_task_done_swallows_emit_failure(self, monkeypatch) -> None:
        from pollypm.work import service_transitions

        def boom(svc, task_id, actor):  # noqa: ANN001
            raise RuntimeError("inbox surface broken")

        # Make digest flush a no-op so we isolate the emit failure.
        monkeypatch.setattr(
            "pollypm.notification_staging.check_and_flush_on_done",
            lambda svc, **k: None,
        )
        monkeypatch.setattr(
            "pollypm.task_shipped.emit_task_shipped_card", boom
        )

        @dataclass
        class _Task:
            project: str
            task_id: str

        class _Svc:
            _project_path = None

            def get(self, task_id):  # noqa: ANN001
                return _Task(project="p", task_id=task_id)

        # Must not raise — this is the protective swallow.
        service_transitions.on_task_done(_Svc(), "p/1", "polly")
