from __future__ import annotations

from pollypm import task_review_summary
from pollypm.task_review_summary import (
    PLAIN_SUMMARY_ENTRY_TYPE,
    REVIEW_SUMMARY_ACTOR,
    backfill_review_plain_summaries,
)
from pollypm.work.models import Artifact, ArtifactKind, OutputType, WorkOutput, WorkStatus
from pollypm.work.sqlite_service import SQLiteWorkService


def _svc(tmp_path):
    return SQLiteWorkService(db_path=tmp_path / "work.db")


def _task(svc):
    return svc.create(
        title="Scraper infrastructure",
        description="Build venue scraping infrastructure for v1.",
        type="task",
        project="demo",
        flow_template="standard",
        roles={"worker": "pete", "reviewer": "polly"},
        priority="high",
        created_by="tester",
    )


def _output(summary: str = "Implemented the scraper changes.") -> WorkOutput:
    return WorkOutput(
        type=OutputType.CODE_CHANGE,
        summary=summary,
        artifacts=[
            Artifact(
                kind=ArtifactKind.COMMIT,
                description="implementation commit",
                ref="abc123",
            ),
        ],
    )


def _move_to_review(svc, task_id: str, *, summary: str = "Implemented the work."):
    svc.queue(task_id, "pm")
    svc.claim(task_id, "pete")
    return svc.node_done(task_id, "pete", _output(summary))


def test_node_done_writes_llm_generated_plain_summary(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("POLLYPM_DISABLE_AGENTIC_REVIEW_SUMMARIES", raising=False)
    prompts: list[str] = []

    def fake_invocation(prompt: str) -> str:
        prompts.append(prompt)
        return (
            "You're being asked to approve the scraper infrastructure handoff.\n"
            "Approve if the implementation is ready for v1 testing; reject if "
            "it still needs proof or a concrete change."
        )

    monkeypatch.setattr(task_review_summary, "review_summary_invocation", fake_invocation)
    svc = _svc(tmp_path)
    task = _task(svc)

    result = _move_to_review(svc, task.task_id, summary="Implemented CDP scraping.")

    assert result.work_status == WorkStatus.REVIEW
    entries = svc.get_context(task.task_id, entry_type=PLAIN_SUMMARY_ENTRY_TYPE)
    assert len(entries) == 1
    assert entries[0].actor == REVIEW_SUMMARY_ACTOR
    assert "You're being asked to approve" in entries[0].text
    assert "Implemented CDP scraping" in prompts[0]
    assert "Do not tell the user to run pm commands" in prompts[0]


def test_rework_review_summary_prompt_includes_previous_rejection(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.delenv("POLLYPM_DISABLE_AGENTIC_REVIEW_SUMMARIES", raising=False)
    prompts: list[str] = []

    def fake_invocation(prompt: str) -> str:
        prompts.append(prompt)
        return (
            "You're being asked to approve two deviations from the original plan.\n"
            "1. Browser requirement: strike browser use from v1 because the "
            "current path meets the goal without it.\n"
            "2. Eventbrite: move it to v2 so v1 can get to testing."
        )

    monkeypatch.setattr(task_review_summary, "review_summary_invocation", fake_invocation)
    monkeypatch.setenv("POLLYPM_DISABLE_AGENTIC_REVIEW_SUMMARIES", "1")
    svc = _svc(tmp_path)
    task = _task(svc)
    _move_to_review(svc, task.task_id, summary="Initial implementation.")
    svc.reject(
        task.task_id,
        "polly",
        (
            "1. needs_real_browser not demonstrated via CLI.\n"
            "2. eventbrite_embed strategy not implemented."
        ),
    )

    monkeypatch.delenv("POLLYPM_DISABLE_AGENTIC_REVIEW_SUMMARIES", raising=False)
    svc.node_done(
        task.task_id,
        "pete",
        _output(
            "We can meet v1 goals without browser use. "
            "Eventbrite live proof should move to v2."
        ),
    )

    entries = svc.get_context(task.task_id, entry_type=PLAIN_SUMMARY_ENTRY_TYPE)
    assert entries[0].text.startswith(
        "You're being asked to approve two deviations"
    )
    assert "needs_real_browser not demonstrated" in prompts[-1]
    assert "eventbrite_embed strategy not implemented" in prompts[-1]
    assert "without browser use" in prompts[-1]


def test_backfill_populates_existing_review_tasks(tmp_path, monkeypatch) -> None:
    svc = _svc(tmp_path)
    task = _task(svc)
    _move_to_review(svc, task.task_id, summary="Ready for review.")
    assert svc.get_context(task.task_id, entry_type=PLAIN_SUMMARY_ENTRY_TYPE) == []

    monkeypatch.delenv("POLLYPM_DISABLE_AGENTIC_REVIEW_SUMMARIES", raising=False)
    monkeypatch.setattr(
        task_review_summary,
        "review_summary_invocation",
        lambda _prompt: "You're being asked to approve the ready-for-review handoff.",
    )

    results = backfill_review_plain_summaries(svc, project="demo")

    assert [(result.task_id, result.outcome) for result in results] == [
        (task.task_id, "generated")
    ]
    entries = svc.get_context(task.task_id, entry_type=PLAIN_SUMMARY_ENTRY_TYPE)
    assert entries[0].text == "You're being asked to approve the ready-for-review handoff."


def test_hold_on_work_node_does_not_generate_review_summary(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.delenv("POLLYPM_DISABLE_AGENTIC_REVIEW_SUMMARIES", raising=False)

    def fail_invocation(_prompt: str) -> str:
        raise AssertionError("should not call LLM")

    monkeypatch.setattr(
        task_review_summary,
        "review_summary_invocation",
        fail_invocation,
    )
    svc = _svc(tmp_path)
    task = _task(svc)
    svc.queue(task.task_id, "pm")
    svc.claim(task.task_id, "pete")

    svc.hold(task.task_id, "pm", "blocked waiting for dependency")

    assert svc.get_context(task.task_id, entry_type=PLAIN_SUMMARY_ENTRY_TYPE) == []
