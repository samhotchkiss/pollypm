"""Tests for planner improvement proposals (#275).

Covers the full pipeline:

1. ``emit_proposals`` writes an inbox task with ``proposal`` labels.
2. Previously-rejected memkeys get filtered out on the next planner run.
3. Inbox Accept creates a new ``work_tasks`` row from the spec and
   archives the originating proposal with a context entry.
4. Inbox Reject with a rationale stores the rejection in planner memory
   and archives with the matching context entry.
5. Plain ``a`` (archive) on a proposal warns — it does NOT silently
   archive the item; the user must press A or X explicitly.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pollypm.work.sqlite_service import SQLiteWorkService
from pollypm.plugins_builtin.project_planning import memory as planner_memory
from pollypm.plugins_builtin.project_planning.proposals import (
    ImprovementProposal,
    emit_proposals,
    filter_rejected,
    is_proposal_task,
    memkey_from_labels,
    render_proposal_body,
)


# ---------------------------------------------------------------------------
# Fixtures shared with the cockpit UI tests — a single-project workspace
# seeded with a proposal inbox task.
# ---------------------------------------------------------------------------


def _write_minimal_config(project_path: Path, config_path: Path) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "[project]\n"
        f'tmux_session = "pollypm-test"\n'
        f'workspace_root = "{project_path.parent}"\n'
        "\n"
        f'[projects.demo]\n'
        f'key = "demo"\n'
        f'name = "Demo"\n'
        f'path = "{project_path}"\n'
    )


def _load_config_compatible(config_path: Path) -> bool:
    try:
        from pollypm.config import load_config
        cfg = load_config(config_path)
        return "demo" in getattr(cfg, "projects", {})
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture
def proposal_env(tmp_path: Path):
    """Seed a project DB and emit one proposal via the public emitter."""
    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_minimal_config(project_path, config_path)
    db_path = project_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    rejections_path = tmp_path / "planner_rejections.jsonl"

    proposal = ImprovementProposal(
        title="Add coverage gate for flow_engine",
        rationale="Coverage on the flow engine is below 70%; a gate stops drift.",
        proposed_task_spec={
            "title": "Add coverage gate for flow_engine",
            "description": "Wire a coverage-ratchet check into CI.",
            "acceptance_criteria": (
                "CI fails when coverage for src/pollypm/work/flow_engine.py "
                "drops below the last-recorded value."
            ),
        },
        severity="advisory",
    )

    svc = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        task_ids = emit_proposals(
            svc,
            project_key="demo",
            proposals=[proposal],
            memory_path=rejections_path,
        )
    finally:
        svc.close()
    assert task_ids, "expected at least one proposal task emitted"

    return {
        "config_path": config_path,
        "project_path": project_path,
        "db_path": db_path,
        "rejections_path": rejections_path,
        "proposal": proposal,
        "proposal_task_id": task_ids[0],
    }


# ---------------------------------------------------------------------------
# 1. emit_proposals writes a proposal inbox task with the expected labels
# ---------------------------------------------------------------------------


def test_emit_proposals_writes_inbox_task_with_proposal_labels(
    proposal_env,
) -> None:
    svc = SQLiteWorkService(
        db_path=proposal_env["db_path"],
        project_path=proposal_env["project_path"],
    )
    try:
        task = svc.get(proposal_env["proposal_task_id"])
    finally:
        svc.close()
    assert is_proposal_task(task)
    assert "proposal" in task.labels
    assert "project:demo" in task.labels
    # Severity label normalised to one of the three accepted values.
    severity_labels = [l for l in task.labels if l.startswith("severity:")]
    assert severity_labels == ["severity:advisory"]
    # The memkey is a short hex digest — present + non-empty.
    memkey = memkey_from_labels(task.labels)
    assert memkey and len(memkey) >= 8
    # Rationale + preview markdown both present in the body.
    assert "Coverage on the flow engine" in task.description
    assert "## Proposed task" in task.description
    assert "acceptance criteria" in task.description


# ---------------------------------------------------------------------------
# 2. Previously-rejected memkey is filtered on the next planner run
# ---------------------------------------------------------------------------


def test_previously_rejected_proposals_are_filtered_on_next_run(
    tmp_path: Path,
) -> None:
    rejections_path = tmp_path / "planner_rejections.jsonl"
    proposal = ImprovementProposal(
        title="Add latency SLO dashboard",
        rationale="We have no visibility into p95 latency by flow.",
        proposed_task_spec={
            "title": "Latency SLO dashboard",
            "description": "Expose p50/p95 per flow template in the cockpit.",
        },
    )
    memkey = proposal.derive_memory_key("demo")

    # Before any rejection — proposal survives filtering.
    kept = filter_rejected(
        [proposal], project_key="demo", memory_path=rejections_path,
    )
    assert len(kept) == 1

    # Record the rejection.
    planner_memory.record_proposal_rejection(
        project_key="demo",
        planner_memory_key=memkey,
        rationale="Not now — we're focused on P1 blockers.",
        path=rejections_path,
    )

    # After rejection — the proposal is filtered out.
    kept_after = filter_rejected(
        [proposal], project_key="demo", memory_path=rejections_path,
    )
    assert kept_after == []

    # And emit_proposals honours the filter end-to-end: no task written.
    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    db_path = project_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    svc = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        task_ids = emit_proposals(
            svc,
            project_key="demo",
            proposals=[proposal],
            memory_path=rejections_path,
        )
    finally:
        svc.close()
    assert task_ids == []


def test_is_proposal_rejected_cross_project_does_not_leak(
    tmp_path: Path,
) -> None:
    """Rejection for project 'alpha' must not filter a same-title proposal for 'beta'."""
    rejections_path = tmp_path / "planner_rejections.jsonl"
    proposal = ImprovementProposal(title="X", rationale="why")
    memkey_alpha = proposal.derive_memory_key("alpha")
    planner_memory.record_proposal_rejection(
        project_key="alpha",
        planner_memory_key=memkey_alpha,
        rationale="nope",
        path=rejections_path,
    )
    # Same logical title, different project — memkey differs, not rejected.
    assert planner_memory.is_proposal_rejected(
        project_key="alpha",
        planner_memory_key=memkey_alpha,
        path=rejections_path,
    ) is True
    memkey_beta = proposal.derive_memory_key("beta")
    assert planner_memory.is_proposal_rejected(
        project_key="beta",
        planner_memory_key=memkey_beta,
        path=rejections_path,
    ) is False


# ---------------------------------------------------------------------------
# Body rendering sanity check (used by the Accept spec-recovery path)
# ---------------------------------------------------------------------------


def test_render_proposal_body_embeds_spec_preview() -> None:
    body = render_proposal_body(ImprovementProposal(
        title="T",
        rationale="Because.",
        proposed_task_spec={
            "title": "T",
            "description": "Do the thing.",
            "acceptance_criteria": "It works.",
        },
    ))
    assert "Because." in body
    assert "## Proposed task" in body
    assert "Do the thing." in body
    assert "It works." in body


# ---------------------------------------------------------------------------
# 3, 4, 5 — Inbox UI behaviour. Mirrors the pattern in
# tests/test_cockpit_inbox_ui.py: Pilot-driven Textual harness.
# ---------------------------------------------------------------------------


def _run(coro):
    asyncio.run(coro)


@pytest.fixture
def inbox_app(proposal_env):
    if not _load_config_compatible(proposal_env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyInboxApp
    return PollyInboxApp(proposal_env["config_path"])


def _select_proposal(app, proposal_env) -> None:
    target = proposal_env["proposal_task_id"]
    for idx, task in enumerate(app._tasks):
        if task.task_id == target:
            app.list_view.index = idx
            return
    raise AssertionError(f"proposal task {target} not visible in inbox list")


def test_inbox_accept_creates_follow_on_task_and_archives(
    proposal_env, inbox_app,
) -> None:
    async def body() -> None:
        async with inbox_app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            _select_proposal(inbox_app, proposal_env)
            await pilot.press("enter")
            await pilot.pause()
            proposal_id = proposal_env["proposal_task_id"]
            assert inbox_app._selected_task_id == proposal_id

            # Hint bar switched to the proposal keybindings.
            hint_rendered = str(inbox_app.hint.render())
            assert "A accept" in hint_rendered
            assert "X reject" in hint_rendered

            # Spec was cached for the Accept path.
            spec = inbox_app._proposal_specs.get(proposal_id)
            assert spec is not None
            assert "title" in spec

            # Capital A triggers Accept.
            await pilot.press("A")
            await pilot.pause()

            # Inbox list shrank (the proposal row is archived).
            assert all(t.task_id != proposal_id for t in inbox_app._tasks)

            # Underlying DB state: original archived DONE, new task exists.
            svc = SQLiteWorkService(
                db_path=proposal_env["db_path"],
                project_path=proposal_env["project_path"],
            )
            try:
                original = svc.get(proposal_id)
                assert original.work_status.value == "done"
                # A proposal_accepted context entry was written.
                accepted_ctx = svc.get_context(
                    proposal_id, entry_type="proposal_accepted",
                )
                assert accepted_ctx, "expected proposal_accepted context row"

                # Look up every task in the project and find the one
                # labelled from_proposal — that's the follow-on.
                all_tasks = svc.list_tasks(project="demo")
                follow_ons = [
                    t for t in all_tasks
                    if "from_proposal" in (t.labels or [])
                ]
                assert len(follow_ons) == 1
                follow_on = follow_ons[0]
                assert follow_on.task_id != proposal_id
                # Spec fields populated.
                assert follow_on.title == "Add coverage gate for flow_engine"
                ac = (follow_on.acceptance_criteria or "")
                assert "coverage" in ac.lower()
                assert "flow_engine" in ac
            finally:
                svc.close()

            # Hint bar snaps back to the default.
            hint_after = str(inbox_app.hint.render())
            assert "A accept" not in hint_after
    _run(body())


def test_inbox_reject_persists_rationale_and_archives(
    proposal_env, inbox_app,
) -> None:
    async def body() -> None:
        async with inbox_app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            _select_proposal(inbox_app, proposal_env)
            await pilot.press("enter")
            await pilot.pause()
            proposal_id = proposal_env["proposal_task_id"]

            # Reject a rationale path may write to the REAL user home —
            # redirect it to the per-test path the fixture seeded.
            rejections_path = proposal_env["rejections_path"]
            import pollypm.plugins_builtin.project_planning.memory as pm_mem
            original = pm_mem.record_proposal_rejection
            captured_paths: list[Path] = []

            def _record_override(**kwargs):
                kwargs.setdefault("path", rejections_path)
                captured_paths.append(kwargs["path"])
                return original(**kwargs)
            pm_mem.record_proposal_rejection = _record_override  # type: ignore[assignment]

            try:
                await pilot.press("X")
                await pilot.pause()
                # Reject focuses the reply Input in "rationale" mode.
                assert inbox_app._awaiting_rejection_task_id == proposal_id
                assert inbox_app.reply_input.has_focus
                assert "reject" in (inbox_app.reply_input.placeholder or "").lower()

                inbox_app.reply_input.value = "Too speculative for this milestone."
                await pilot.press("enter")
                await pilot.pause()
            finally:
                pm_mem.record_proposal_rejection = original  # type: ignore[assignment]

            # Inbox row archived; underlying task is DONE.
            assert all(t.task_id != proposal_id for t in inbox_app._tasks)
            svc = SQLiteWorkService(
                db_path=proposal_env["db_path"],
                project_path=proposal_env["project_path"],
            )
            try:
                task = svc.get(proposal_id)
                assert task.work_status.value == "done"
                rejected_ctx = svc.get_context(
                    proposal_id, entry_type="proposal_rejected",
                )
                assert rejected_ctx
                assert "Too speculative" in rejected_ctx[0].text
            finally:
                svc.close()

            # Planner memory got the rejection — the memkey is stored.
            assert rejections_path.exists()
            content = rejections_path.read_text()
            assert "Too speculative" in content
            assert '"project": "demo"' in content

            # And the filter function now excludes the proposal.
            from pollypm.plugins_builtin.project_planning.proposals import (
                filter_rejected as _filter,
            )
            remaining = _filter(
                [proposal_env["proposal"]],
                project_key="demo",
                memory_path=rejections_path,
            )
            assert remaining == []
    _run(body())


def test_plain_a_on_proposal_warns_and_does_not_archive(
    proposal_env, inbox_app,
) -> None:
    async def body() -> None:
        async with inbox_app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            _select_proposal(inbox_app, proposal_env)
            await pilot.press("enter")
            await pilot.pause()
            proposal_id = proposal_env["proposal_task_id"]
            initial_count = len(inbox_app._tasks)

            # Lowercase ``a`` MUST NOT silently archive a proposal.
            await pilot.press("a")
            await pilot.pause()

            # Row still present — nothing got dropped.
            assert len(inbox_app._tasks) == initial_count
            assert any(
                t.task_id == proposal_id for t in inbox_app._tasks
            )
            svc = SQLiteWorkService(
                db_path=proposal_env["db_path"],
                project_path=proposal_env["project_path"],
            )
            try:
                task = svc.get(proposal_id)
                # Still live (not DONE).
                assert task.work_status.value != "done"
            finally:
                svc.close()
    _run(body())
