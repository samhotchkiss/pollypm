"""Tests for the architect stage-transition contract (#295).

When Archie finishes a stage of the ``plan_project`` flow he is
supposed to drive the node transition himself by calling
``pm task done`` (which invokes ``SQLiteWorkService.node_done``). The
flow engine reads ``next_node`` from ``plan_project.yaml`` and advances
the task. Without that explicit call the task stays frozen on the
current node even though artifacts exist on disk — the real-world
failure mode that motivated #295.

These tests simulate the architect's side of that contract. For each
stage of interest we:

1. Get the task into the stage by claiming + walking the flow.
2. Write the stage's artifact on disk (when gates require it).
3. Call ``node_done`` with the architect as actor.
4. Assert the task advanced to the expected next node.

We also cover:

* the "no artifact" refusal at ``synthesize`` (the ``log_present``
  gate must block the advance so we don't get a broken handoff), and
* the ``user_approval`` HALT contract — ``node_done`` must refuse
  because that node is a review node, not a work node.

The architect prompt is also asserted to mention the
``<stage_transitions>`` section so we can't silently regress the
agent-side fix.

Tests use real work-service methods against a fresh SQLite DB —
mocking is limited to the architect's "turn" (we don't spawn a real
session).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pollypm.plugins_builtin.project_planning import plugin as _planning_plugin
from pollypm.work.sqlite_service import (
    InvalidTransitionError,
    SQLiteWorkService,
    ValidationError,
)


ARCHITECT_PROFILE_PATH = (
    Path(_planning_plugin.__file__).resolve().parent / "profiles" / "architect.md"
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh project dir that we ``chdir`` into so gates resolve cwd."""
    root = tmp_path / "demo"
    root.mkdir()
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def svc(project_root: Path) -> SQLiteWorkService:
    """SQLiteWorkService bound to the project root so gates + flow
    resolution pick up the right plugin wiring."""
    return SQLiteWorkService(
        db_path=project_root / "state.db", project_path=project_root,
    )


def _done_output(stage: str, path: str) -> dict:
    """Standard work-output payload the architect would send at stage end."""
    return {
        "type": "code_change",
        "summary": f"Stage {stage} complete; artifact written",
        "artifacts": [
            {"kind": "file_change", "description": f"{stage} artifact", "path": path},
        ],
    }


def _create_and_claim_plan_task(svc: SQLiteWorkService) -> str:
    """Create a plan_project task, queue + claim it, return its id."""
    task = svc.create(
        title="Plan demo",
        description="Plan the demo project.",
        type="task",
        project="demo",
        flow_template="plan_project",
        roles={"architect": "architect"},
        priority="normal",
    )
    svc.queue(task.task_id, "pm")
    svc.claim(task.task_id, "architect")
    return task.task_id


def _advance_to(
    svc: SQLiteWorkService, task_id: str, target_node: str, project_root: Path,
) -> None:
    """Walk the linear plan_project chain up to (but not through)
    ``target_node`` by calling ``node_done`` at each intermediate stage.
    Writes stage artifacts as needed to satisfy gates.
    """
    chain = [
        ("research", "docs/planning-context.md"),
        ("discover", "docs/planning-discover.md"),
        ("decompose", "docs/plan/candidates.md"),
        ("test_strategy", "docs/plan/test-strategy.md"),
        ("magic", "docs/plan/magic.md"),
        ("critic_panel", "docs/plan/critic-panel.md"),
        ("synthesize", "docs/project-plan.md"),
    ]
    for stage, path in chain:
        if stage == target_node:
            return
        # Write the stage's artifact so gates on downstream stages
        # (log_present on synthesize, etc) succeed.
        artifact = project_root / path
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(f"# {stage} artifact\nNon-empty body.\n", encoding="utf-8")
        # Synthesize also needs the session log (log_present gate).
        if stage == "synthesize":
            (project_root / "docs" / "planning-session-log.md").write_text(
                "# Planning session log\nNarrative of the session.\n",
                encoding="utf-8",
            )
        svc.node_done(task_id, "architect", _done_output(stage, path))


# ---------------------------------------------------------------------------
# (1) Each stage's node_done advances to next_node
# ---------------------------------------------------------------------------


def test_research_done_advances_to_discover(
    svc: SQLiteWorkService, project_root: Path,
) -> None:
    """After writing ``docs/planning-context.md``, calling ``node_done``
    moves the task from ``research`` to ``discover`` (the flow's
    declared ``next_node``)."""
    task_id = _create_and_claim_plan_task(svc)
    assert svc.get(task_id).current_node_id == "research"

    # Write the research artifact (not strictly needed — research has no
    # hard gates in the flow — but the prompt's contract is "don't
    # advance without the artifact", so we simulate the happy path).
    (project_root / "docs").mkdir(exist_ok=True)
    (project_root / "docs" / "planning-context.md").write_text(
        "# Context\nReal body.\n", encoding="utf-8",
    )

    result = svc.node_done(
        task_id, "architect", _done_output("research", "docs/planning-context.md"),
    )
    assert result.current_node_id == "discover"
    assert result.work_status.value == "in_progress"


def test_discover_done_advances_to_decompose(
    svc: SQLiteWorkService, project_root: Path,
) -> None:
    task_id = _create_and_claim_plan_task(svc)
    _advance_to(svc, task_id, "discover", project_root)
    assert svc.get(task_id).current_node_id == "discover"

    (project_root / "docs" / "planning-discover.md").write_text(
        "# Discover\nClarifying answers.\n", encoding="utf-8",
    )
    result = svc.node_done(
        task_id, "architect", _done_output("discover", "docs/planning-discover.md"),
    )
    assert result.current_node_id == "decompose"


def test_decompose_done_advances_to_test_strategy(
    svc: SQLiteWorkService, project_root: Path,
) -> None:
    task_id = _create_and_claim_plan_task(svc)
    _advance_to(svc, task_id, "decompose", project_root)
    assert svc.get(task_id).current_node_id == "decompose"

    (project_root / "docs" / "plan").mkdir(parents=True, exist_ok=True)
    (project_root / "docs" / "plan" / "candidates.md").write_text(
        "# Candidates\n## A\n## B\n", encoding="utf-8",
    )
    result = svc.node_done(
        task_id, "architect",
        _done_output("decompose", "docs/plan/candidates.md"),
    )
    assert result.current_node_id == "test_strategy"


# ---------------------------------------------------------------------------
# (2) Synthesize → user_approval, with gate enforcement
# ---------------------------------------------------------------------------


def test_synthesize_done_advances_to_user_approval_and_flips_to_review(
    svc: SQLiteWorkService, project_root: Path,
) -> None:
    """Synthesize is the last work node before the review stop-point.
    A successful ``node_done`` both advances the node AND flips status
    to ``review`` (user_approval is a review node)."""
    task_id = _create_and_claim_plan_task(svc)
    _advance_to(svc, task_id, "synthesize", project_root)
    assert svc.get(task_id).current_node_id == "synthesize"

    # Both artifacts the synthesize stage must produce.
    (project_root / "docs" / "project-plan.md").write_text(
        "# Project plan\nModule list and risks.\n", encoding="utf-8",
    )
    (project_root / "docs" / "planning-session-log.md").write_text(
        "# Session log\nWhat Archie decided and why.\n", encoding="utf-8",
    )

    result = svc.node_done(
        task_id, "architect",
        _done_output("synthesize", "docs/project-plan.md"),
    )
    assert result.current_node_id == "user_approval"
    assert result.work_status.value == "review"


def test_synthesize_done_refuses_without_session_log(
    svc: SQLiteWorkService, project_root: Path,
) -> None:
    """The ``log_present`` gate on synthesize blocks advance when
    ``docs/planning-session-log.md`` is missing — covers the "do not
    advance without the artifact" half of the prompt contract."""
    task_id = _create_and_claim_plan_task(svc)
    _advance_to(svc, task_id, "synthesize", project_root)
    # Remove the session log that _advance_to wrote (it was written for
    # the stage BEFORE synthesize so those chained node_dones worked).
    log_path = project_root / "docs" / "planning-session-log.md"
    if log_path.exists():
        log_path.unlink()

    # Write the project plan but NOT the session log.
    (project_root / "docs" / "project-plan.md").write_text(
        "# Plan\nbody", encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="log_present|session log|log "):
        svc.node_done(
            task_id, "architect",
            _done_output("synthesize", "docs/project-plan.md"),
        )

    # Task stays on synthesize — the prompt tells Archie exactly this:
    # stay put, produce the artifact, retry.
    assert svc.get(task_id).current_node_id == "synthesize"


# ---------------------------------------------------------------------------
# (3) user_approval HALT — node_done must refuse on review nodes
# ---------------------------------------------------------------------------


def test_user_approval_refuses_node_done(
    svc: SQLiteWorkService, project_root: Path,
) -> None:
    """At ``user_approval`` the architect's contract is HALT + notify,
    NOT another ``pm task done``. ``node_done`` on a review node must
    raise so an over-eager agent can't accidentally skip the user."""
    task_id = _create_and_claim_plan_task(svc)
    _advance_to(svc, task_id, "synthesize", project_root)
    # Advance through synthesize so the task is now at user_approval.
    (project_root / "docs" / "project-plan.md").write_text(
        "# Plan\nbody\n", encoding="utf-8",
    )
    (project_root / "docs" / "planning-session-log.md").write_text(
        "# Log\nbody\n", encoding="utf-8",
    )
    svc.node_done(
        task_id, "architect",
        _done_output("synthesize", "docs/project-plan.md"),
    )
    task = svc.get(task_id)
    assert task.current_node_id == "user_approval"

    # Any further node_done call must be rejected — either because the
    # task is in ``review`` state (not ``in_progress``) or because the
    # node isn't a work node. Both error paths live in node_done.
    with pytest.raises(InvalidTransitionError):
        svc.node_done(
            task_id, "architect",
            _done_output("user_approval", "docs/project-plan.md"),
        )


# ---------------------------------------------------------------------------
# (4) Prompt carries the stage-transitions contract
# ---------------------------------------------------------------------------


def test_architect_prompt_includes_stage_transitions_block() -> None:
    """The architect persona markdown must ship a ``<stage_transitions>``
    section that names ``pm task done`` as the transition command. If
    this assertion fails, the prompt regressed and Archie will drift
    back to the #295 failure mode."""
    text = ARCHITECT_PROFILE_PATH.read_text(encoding="utf-8")
    assert "<stage_transitions>" in text
    assert "</stage_transitions>" in text
    assert "pm task done" in text
    # Mentions every work stage by name so the agent can match its
    # current node to an instruction.
    for stage in (
        "research", "discover", "decompose", "test_strategy",
        "magic", "critic_panel", "synthesize", "user_approval", "emit",
    ):
        assert stage in text, f"stage '{stage}' missing from architect prompt"
    # HALT instruction at user_approval is load-bearing — without it
    # Archie might try to advance past the human touchpoint.
    assert "HALT" in text
